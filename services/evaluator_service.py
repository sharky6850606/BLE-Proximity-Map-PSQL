from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from database import DB_MODE, get_db
from services.notifications_service import save_notification


# --- Evaluator configuration ---

# Treat <=3m as "IN" range, >3m as "OUT" range
IN_DISTANCE_METERS = 3.0

# Emit STILL events if the beacon has stayed in the same state for this long
STILL_INTERVAL_SECONDS = 15 * 60  # 15 minutes

# If a beacon hasn't been seen for this long, emit MISSING; when it returns emit FOUND
MISSING_THRESHOLD_SECONDS = 30 * 60  # 30 minutes


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _now_unix() -> int:
    return int(time.time())


def _iter_beacons(latest_messages: dict) -> List[dict]:
    """Flattens latest_messages -> list of {device_ident, beacon_id, distance, event_time}."""
    out: List[dict] = []
    if not latest_messages:
        return out

    for device_id, snap in (latest_messages or {}).items():
        if not isinstance(snap, dict):
            continue
        device_ident = snap.get("ident") or snap.get("device_ident") or device_id
        beacons = snap.get("beacons") or []
        # Prefer a timestamp string already computed by your pipeline
        event_time = snap.get("timestamp") or snap.get("timestamp_local") or snap.get("timestamp_raw")
        for b in beacons:
            if not isinstance(b, dict):
                continue
            bid = b.get("id")
            if not bid:
                continue
            dist = b.get("distance")
            try:
                dist_f = float(dist) if dist is not None else None
            except Exception:
                dist_f = None
            out.append(
                {
                    "device_ident": str(device_ident),
                    "beacon_id": str(bid),
                    "distance": dist_f,
                    "event_time": b.get("last_seen") or event_time or "-",
                }
            )
    return out


def _ensure_beacon_state_row(cur, device_ident: str, beacon_id: str, state: str, now_ts: int):
    """Insert baseline state row (no IN/LEFT emitted on first sight)."""
    if DB_MODE == "postgres":
        cur.execute(
            """
            INSERT INTO beacon_states (beacon_id, device_ident, state, last_change_ts, last_seen_ts, last_status_ts, missing, last_missing_ts, active)
            VALUES (%s,%s,%s,%s,%s,%s,0,NULL,1)
            ON CONFLICT (beacon_id) DO UPDATE
              SET device_ident=EXCLUDED.device_ident,
                  state=EXCLUDED.state,
                  last_seen_ts=EXCLUDED.last_seen_ts,
                  last_status_ts=EXCLUDED.last_status_ts,
                  active=1
            """,
            (beacon_id, device_ident, state, now_ts, now_ts, now_ts),
        )
    else:
        cur.execute(
            """
            INSERT OR IGNORE INTO beacon_states (beacon_id, device_ident, state, last_change_ts, last_seen_ts, last_status_ts, missing, last_missing_ts, active)
            VALUES (?,?,?,?,?,?,0,NULL,1)
            """,
            (beacon_id, device_ident, state, now_ts, now_ts, now_ts),
        )


def evaluator_tick(latest_messages: dict):
    """Runs one evaluation cycle.

    This does 3 things:
      1) Emits IN/LEFT when a beacon actually changes state.
      2) Emits STILL_IN/STILL_OUT every STILL_INTERVAL_SECONDS while state is unchanged.
      3) Emits MISSING when not seen for MISSING_THRESHOLD_SECONDS, and FOUND when it reappears.
    """
    now_ts = _now_unix()
    rows = _iter_beacons(latest_messages)
    if not rows:
        # Still perform missing scan (in case everything went quiet)
        _scan_missing(now_ts)
        return

    conn = get_db()
    cur = conn.cursor()

    # Update per-beacon state
    for r in rows:
        device_ident = r["device_ident"]
        beacon_id = r["beacon_id"]
        distance = r["distance"]
        event_time = r["event_time"]

        state_now = "in" if (distance is not None and distance <= IN_DISTANCE_METERS) else "out"

        # Load previous
        if DB_MODE == "postgres":
            cur.execute(
                "SELECT state, last_change_ts, last_seen_ts, last_status_ts, missing FROM beacon_states WHERE beacon_id=%s",
                (beacon_id,),
            )
        else:
            cur.execute(
                "SELECT state, last_change_ts, last_seen_ts, last_status_ts, missing FROM beacon_states WHERE beacon_id=?",
                (beacon_id,),
            )
        prev = cur.fetchone()

        if prev is None:
            _ensure_beacon_state_row(cur, device_ident, beacon_id, state_now, now_ts)
            continue

        prev_state, prev_change, prev_seen, prev_status, missing = prev
        prev_state = prev_state or state_now
        prev_status = prev_status or prev_change or now_ts
        prev_seen = prev_seen or now_ts
        missing = int(missing or 0)

        # Always update last_seen and device_ident
        if DB_MODE == "postgres":
            cur.execute(
                "UPDATE beacon_states SET device_ident=%s, last_seen_ts=%s, active=1 WHERE beacon_id=%s",
                (device_ident, now_ts, beacon_id),
            )
        else:
            cur.execute(
                "UPDATE beacon_states SET device_ident=?, last_seen_ts=?, active=1 WHERE beacon_id=?",
                (device_ident, now_ts, beacon_id),
            )

        # If it was missing and we see it again -> FOUND
        if missing == 1:
            save_notification(
                {
                    "type": "found",
                    "name": beacon_id,
                    "time": event_time,
                    "distance": distance,
                    "beacon_id": beacon_id,
                    "device_ident": device_ident,
                }
            )
            if DB_MODE == "postgres":
                cur.execute(
                    "UPDATE beacon_states SET missing=0, last_missing_ts=NULL WHERE beacon_id=%s",
                    (beacon_id,),
                )
            else:
                cur.execute(
                    "UPDATE beacon_states SET missing=0, last_missing_ts=NULL WHERE beacon_id=?",
                    (beacon_id,),
                )
            # Reset status timer after found so STILL doesn't immediately fire
            prev_status = now_ts

        # State change -> IN/LEFT
        if prev_state != state_now:
            save_notification(
                {
                    "type": "in" if state_now == "in" else "left",
                    "name": beacon_id,
                    "time": event_time,
                    "distance": distance,
                    "beacon_id": beacon_id,
                    "device_ident": device_ident,
                }
            )
            if DB_MODE == "postgres":
                cur.execute(
                    "UPDATE beacon_states SET state=%s, last_change_ts=%s, last_status_ts=%s WHERE beacon_id=%s",
                    (state_now, now_ts, now_ts, beacon_id),
                )
            else:
                cur.execute(
                    "UPDATE beacon_states SET state=?, last_change_ts=?, last_status_ts=? WHERE beacon_id=?",
                    (state_now, now_ts, now_ts, beacon_id),
                )
            continue

        # STILL events
        if (now_ts - int(prev_status)) >= STILL_INTERVAL_SECONDS:
            save_notification(
                {
                    "type": "still_in" if state_now == "in" else "still_out",
                    "name": beacon_id,
                    "time": event_time,
                    "distance": distance,
                    "beacon_id": beacon_id,
                    "device_ident": device_ident,
                }
            )
            if DB_MODE == "postgres":
                cur.execute(
                    "UPDATE beacon_states SET last_status_ts=%s WHERE beacon_id=%s",
                    (now_ts, beacon_id),
                )
            else:
                cur.execute(
                    "UPDATE beacon_states SET last_status_ts=? WHERE beacon_id=?",
                    (now_ts, beacon_id),
                )

    conn.commit()
    conn.close()

    # Missing scan (separate query)
    _scan_missing(now_ts)


def _scan_missing(now_ts: int):
    """Marks beacons as missing if not seen recently; emits MISSING once per outage."""
    cutoff = now_ts - MISSING_THRESHOLD_SECONDS
    conn = get_db()
    cur = conn.cursor()

    if DB_MODE == "postgres":
        cur.execute(
            """
            SELECT beacon_id, device_ident, last_seen_ts
            FROM beacon_states
            WHERE active=1 AND missing=0 AND last_seen_ts IS NOT NULL AND last_seen_ts < %s
            """,
            (cutoff,),
        )
    else:
        cur.execute(
            """
            SELECT beacon_id, device_ident, last_seen_ts
            FROM beacon_states
            WHERE active=1 AND missing=0 AND last_seen_ts IS NOT NULL AND last_seen_ts < ?
            """,
            (cutoff,),
        )
    rows = cur.fetchall() or []

    for beacon_id, device_ident, last_seen_ts in rows:
        save_notification(
            {
                "type": "missing",
                "name": str(beacon_id),
                "time": _utc_iso(),  # event time unknown once missing; log time is separate
                "distance": None,
                "beacon_id": str(beacon_id),
                "device_ident": str(device_ident) if device_ident else None,
            }
        )
        if DB_MODE == "postgres":
            cur.execute(
                "UPDATE beacon_states SET missing=1, last_missing_ts=%s WHERE beacon_id=%s",
                (now_ts, beacon_id),
            )
        else:
            cur.execute(
                "UPDATE beacon_states SET missing=1, last_missing_ts=? WHERE beacon_id=?",
                (now_ts, beacon_id),
            )

    conn.commit()
    conn.close()


def start_evaluator_thread(latest_messages: dict, interval_seconds: int = 60):
    """Starts a background evaluator loop (safe for single-worker deployments)."""

    def _loop():
        while True:
            try:
                evaluator_tick(latest_messages)
            except Exception as e:
                # keep running even if one cycle fails
                print("[evaluator] error:", e, flush=True)
            time.sleep(max(5, int(interval_seconds)))

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t
