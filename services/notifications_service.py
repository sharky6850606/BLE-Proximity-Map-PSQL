import time
from database import get_db
from services.beacon_logic import format_samoa_time


def _db_ph(conn):
    return "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"


def emit_notification(ntype, beacon_id, event_time=None, device_ident=None, distance=None):
    conn = get_db()
    try:
        event_time = event_time or format_samoa_time(time.time())
        ph = _db_ph(conn)
        conn.execute(
            f"""
            (type, beacon_name, beacon_id, event_time, created_at, device_ident, distance)
            VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})
            """,
            (
                ntype,
                beacon_id,
                beacon_id,
                event_time,
                format_samoa_time(time.time()),
                device_ident,
                distance,
            ),
        )
        conn.commit()
    finally:
        conn.close()
