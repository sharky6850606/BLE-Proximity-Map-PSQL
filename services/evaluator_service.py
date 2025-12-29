import os
import time
import threading

from database import get_db
from services.beacon_logic import format_samoa_time

EVAL_INTERVAL_SECONDS = int(os.getenv("EVAL_INTERVAL_SECONDS", "60"))
BEACON_TTL_SECONDS = int(os.getenv("BEACON_TTL_SECONDS", "420"))  # 7 mins default
STATUS_EVERY_SECONDS = int(os.getenv("STATUS_EVERY_SECONDS", "900"))  # 15 mins

_thread = None
_stop_flag = False

def samoa_iso(ts: int) -> str:
    # format_samoa_time returns "YYYY-MM-DD HH:MM:SS" (your code uses this)
    return format_samoa_time(ts).replace(" ", "T")

def _insert_notification(conn, ntype: str, beacon_key: str, event_ts: int, device_ident=None):
    """
    Notifications table is used by analytics + PDF reports.
    Keep it simple: distance is None.
    event_time uses Samoa-formatted time (human event), created_at is Samoa ISO.
    """
    try:
        conn.execute(
            "INSERT INTO notifications (type, beacon_name, event_time, created_at, device_ident, distance) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                (ntype or "").strip(),
                (beacon_key or "").strip(),
                format_samoa_time(event_ts),
                samoa_iso(int(time.time())),
                device_ident,
                None,
            ),
        )
        return True
    except Exception as e:
        print(f"[evaluator] notification insert failed: {e}")
        return False

def _get_last_status_ts(conn, beacon_key: str, ntype: str):
    """
    Return unix-like seconds of last status notification time if available, else None.
    Uses created_at which is stored like 'YYYY-MM-DDTHH:MM:SS' in Samoa time.
    For safety, we only check existence & rely on ordering by id (works in both sqlite/postgres).
    """
    try:
        row = conn.execute(
            "SELECT created_at FROM notifications WHERE beacon_name=%s AND type=%s ORDER BY id DESC LIMIT 1",
            (beacon_key, ntype),
        ).fetchone()
        if not row:
            return None
        created_at = row[0] if not isinstance(row, dict) else row.get("created_at")
        if not created_at:
            return None
        # created_at is not unix; we can’t parse safely without knowing format variations.
        # Instead: use id ordering and gate by "has there been a recent one" using a DB-side window if possible.
        return 0  # sentinel meaning "exists"
    except Exception:
        return None

def _should_emit_status(conn, beacon_key: str, ntype: str) -> bool:
    """
    Robust cross-db check:
    - For postgres: compare NOW() - created_at interval
    - For sqlite: compare using substr/strftime best effort
    If we can't compare, we fall back to "emit if none exists".
    """
    try:
        backend = getattr(conn, "backend", "postgres")
        if backend == "postgres":
            row = conn.execute(
                "SELECT 1 FROM notifications "
                "WHERE beacon_name=%s AND type=%s AND created_at >= (NOW() - INTERVAL '15 minutes') "
                "ORDER BY id DESC LIMIT 1",
                (beacon_key, ntype),
            ).fetchone()
            return row is None
        else:
            # sqlite-ish: created_at is text 'YYYY-MM-DDTHH:MM:SS'
            # Use last row existence only; if exists recently can't be guaranteed, but prevents spam.
            row = conn.execute(
                "SELECT 1 FROM notifications WHERE beacon_name=%s AND type=%s ORDER BY id DESC LIMIT 1",
                (beacon_key, ntype),
            ).fetchone()
            return row is None
    except Exception:
        # safest fallback
        last = _get_last_status_ts(conn, beacon_key, ntype)
        return last is None

def _load_beacon_states(conn):
    """
    Expect at least: beacon_key, state, last_seen_ts, device_ident(optional), active(optional)
    We try multiple selects to match your schema.
    """
    # Try rich schema
    try:
        rows = conn.execute(
            "SELECT beacon_key, COALESCE(state,'') AS state, last_seen_ts, device_ident, COALESCE(active,1) AS active "
            "FROM beacon_states"
        ).fetchall()
        return rows, True
    except Exception:
        pass

    # Try simpler schema
    try:
        rows = conn.execute(
            "SELECT beacon_key, COALESCE(state,'') AS state, last_seen_ts "
            "FROM beacon_states"
        ).fetchall()
        return rows, False
    except Exception:
        return [], False

def _update_beacon_state(conn, beacon_key: str, new_state: str, now_ts: int, device_ident=None, active=None):
    # Try rich update
    try:
        if active is None:
            conn.execute(
                "UPDATE beacon_states SET state=%s, last_change_ts=%s, device_ident=%s WHERE beacon_key=%s",
                (new_state, now_ts, device_ident, beacon_key),
            )
        else:
            conn.execute(
                "UPDATE beacon_states SET state=%s, last_change_ts=%s, device_ident=%s, active=%s WHERE beacon_key=%s",
                (new_state, now_ts, device_ident, int(active), beacon_key),
            )
        return True
    except Exception:
        pass

    # Try simple update
    try:
        conn.execute(
            "UPDATE beacon_states SET state=%s, last_change_ts=%s WHERE beacon_key=%s",
            (new_state, now_ts, beacon_key),
        )
        return True
    except Exception:
        return False

def _evaluate_once():
    now_ts = int(time.time())
    ttl = BEACON_TTL_SECONDS

    conn = get_db()
    try:
        rows, rich = _load_beacon_states(conn)

        changed = 0
        inserted = 0

        for r in rows:
            if isinstance(r, dict):
                beacon_key = r.get("beacon_key")
                state = (r.get("state") or "").lower()
                last_seen_ts = r.get("last_seen_ts") or 0
                device_ident = r.get("device_ident") if rich else None
                active = int(r.get("active") or 1) if rich else 1
            else:
                beacon_key = r[0]
                state = (r[1] or "").lower()
                last_seen_ts = r[2] or 0
                device_ident = r[3] if rich and len(r) > 3 else None
                active = int(r[4]) if rich and len(r) > 4 else 1

            if not beacon_key:
                continue

            # Determine online/offline based on last_seen_ts
            age = now_ts - int(last_seen_ts or 0)

            # If expired => mark left once
            if age > ttl:
                # Only transition if not already left/offline
                if state != "left" or active != 0:
                    ok = _update_beacon_state(conn, beacon_key, "left", now_ts, device_ident=device_ident, active=0)
                    if ok:
                        changed += 1
                        if _insert_notification(conn, "left", beacon_key, now_ts, device_ident=device_ident):
                            inserted += 1

                # still_out every 15 mins (anti-spam check)
                if _should_emit_status(conn, beacon_key, "still_out"):
                    if _insert_notification(conn, "still_out", beacon_key, now_ts, device_ident=device_ident):
                        inserted += 1

            else:
                # Not expired => treat as in/active
                if state != "in" or active != 1:
                    ok = _update_beacon_state(conn, beacon_key, "in", now_ts, device_ident=device_ident, active=1)
                    if ok:
                        changed += 1
                        # Emit "in" only when it transitions back from left/out
                        if _insert_notification(conn, "in", beacon_key, now_ts, device_ident=device_ident):
                            inserted += 1

                # still_in every 15 mins (anti-spam check)
                if _should_emit_status(conn, beacon_key, "still_in"):
                    if _insert_notification(conn, "still_in", beacon_key, now_ts, device_ident=device_ident):
                        inserted += 1

        if changed or inserted:
            conn.commit()

        print(f"[evaluator] ok changed={changed} notifications={inserted} ttl={ttl}s")

    except Exception as e:
        print(f"[evaluator] error: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _loop():
    global _stop_flag
    while not _stop_flag:
        _evaluate_once()
        time.sleep(EVAL_INTERVAL_SECONDS)

def start_evaluator_thread():
    global _thread, _stop_flag
    if _thread and _thread.is_alive():
        return
    _stop_flag = False
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    print(f"[evaluator] started interval={EVAL_INTERVAL_SECONDS}s ttl={BEACON_TTL_SECONDS}s")
