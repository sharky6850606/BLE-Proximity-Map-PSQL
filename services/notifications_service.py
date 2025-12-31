import time
from database import get_db
from services.beacon_logic import format_samoa_time


def emit_notification(ntype, beacon_id, device_id=None, distance=None):
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO notifications
            (type, beacon_name, event_time, created_at, device_ident, distance)
            VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (
                ntype,
                beacon_id,
                format_samoa_time(time.time()),
                format_samoa_time(time.time()),
                device_id,
                distance,
            ),
        )
        conn.commit()
    finally:
        conn.close()
