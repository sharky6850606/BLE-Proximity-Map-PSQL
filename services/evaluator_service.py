import threading
import time

from database import get_db
from services.beacon_logic import format_samoa_time
from services.notifications_service import emit_notification

# Beacon behavior
IN_RANGE_METERS = 5.0
STILL_INTERVAL_SEC = 10 * 60       # still_in / still_out every 10 minutes
MISSING_AFTER_SEC = 15 * 60        # missing if not seen for 15 minutes (~3 missed 5min packets)


def _is_postgres(conn) -> bool:
    return conn.__class__.__module__.startswith("psycopg")


def _now_unix() -> int:
    return int(time.time())


def _fmt_time(unix_ts: int) -> str:
    return format_samoa_time(unix_ts)


def _ph(pg: bool) -> str:
    return "%s" if pg else "?"


def run_once():
    """Evaluate beacon state from DB and generate server-side notifications.

    We rely on /flespi to continuously update beacon_states.last_seen_ts and last_distance.
    This evaluator:
      - emits IN / LEFT on true state transitions
      - emits STILL_IN / STILL_OUT every 10 minutes while state remains unchanged
      - emits MISSING after 15 minutes unseen, and FOUND when seen again

    All emitted notifications are persisted.
    """
    now = _now_unix()
    event_time = _fmt_time(now)

    conn = get_db()
    pg = _is_postgres(conn)
    ph = _ph(pg)

    cur = conn.cursor()

    # Pull all known beacons (we keep them even if "inactive" so we can detect missing)
    cur.execute(
        """
        SELECT beacon_key,
               device_ident,
               beacon_id,
               state,
               last_seen_ts,
               last_distance,
               last_change_ts,
               last_status_ts,
               missing
        FROM beacon_states
        """
    )
    rows = cur.fetchall() or []

    for row in rows:
        (
            beacon_key,
            device_ident,
            beacon_id,
            state_db,
            last_seen_ts,
            last_distance,
            last_change_ts,
            last_status_ts,
            missing_db,
        ) = row

        if last_seen_ts is None:
            continue

        age = now - int(last_seen_ts)
        is_missing_now = age >= MISSING_AFTER_SEC
        was_missing = bool(missing_db)

        # --- Missing / Found ---
        if is_missing_now and not was_missing:
            emit_notification(
                "missing",
                beacon_id,
                event_time=event_time,
                device_ident=device_ident,
                distance=None,
            )
            cur.execute(
                f"UPDATE beacon_states SET missing=1, last_missing_ts={ph}, last_status_ts={ph} WHERE beacon_key={ph}",
                (now, now, beacon_key),
            )
            continue

        if (not is_missing_now) and was_missing:
            emit_notification(
                "found",
                beacon_id,
                event_time=event_time,
                device_ident=device_ident,
                distance=None,
            )
            cur.execute(
                f"UPDATE beacon_states SET missing=0, last_missing_ts={ph}, last_status_ts={ph} WHERE beacon_key={ph}",
                (now, now, beacon_key),
            )
            # fallthrough: after FOUND we also evaluate in/out state

        # If missing right now, do not emit in/out or still events.
        if is_missing_now:
            continue

        # --- In / Out evaluation ---
        dist = float(last_distance) if last_distance is not None else None
        state_now = "in" if (dist is not None and dist <= IN_RANGE_METERS) else "out"

        if state_db is None:
            # First time baseline
            cur.execute(
                f"UPDATE beacon_states SET state={ph}, last_change_ts={ph}, last_status_ts={ph}, active=1 WHERE beacon_key={ph}",
                (state_now, now, now, beacon_key),
            )
            continue

        state_db = str(state_db)

        if state_db != state_now:
            # Transition
            notif_type = "in" if state_now == "in" else "left"
            emit_notification(
                notif_type,
                beacon_id,
                event_time=event_time,
                device_ident=device_ident,
                distance=dist,
            )
            cur.execute(
                f"UPDATE beacon_states SET state={ph}, last_change_ts={ph}, last_status_ts={ph}, active=1 WHERE beacon_key={ph}",
                (state_now, now, now, beacon_key),
            )
            continue

        # --- Still status ---
        last_status_ts = int(last_status_ts) if last_status_ts is not None else 0
        if now - last_status_ts >= STILL_INTERVAL_SEC:
            notif_type = "still_in" if state_now == "in" else "still_out"
            emit_notification(
                notif_type,
                beacon_id,
                event_time=event_time,
                device_ident=device_ident,
                distance=dist,
            )
            cur.execute(
                f"UPDATE beacon_states SET last_status_ts={ph}, active=1 WHERE beacon_key={ph}",
                (now, beacon_key),
            )

    conn.commit()


def _loop():
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[evaluator] error: {e}")
        time.sleep(60)


def start_evaluator_thread():
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    print("[evaluator] thread started ✅")
    return t
