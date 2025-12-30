from database import get_db
import time


def save_notification(
    ntype,
    beacon_name,
    event_time,
    device_ident=None,
    distance=None,
):
    """
    Persist a notification safely for both SQLite and Postgres.
    """

    if not ntype or not beacon_name:
        return

    conn = get_db()
    try:
        ph = "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"
        conn.execute(
            f"""
            INSERT INTO notifications
            (type, beacon_name, event_time, created_at, device_ident, distance)
            VALUES ({ph},{ph},{ph},{ph},{ph},{ph})
            """,
            (
                ntype,
                beacon_name,
                event_time,
                time.strftime("%Y-%m-%d %H:%M:%S"),
                device_ident,
                distance,
            ),
        )
        conn.commit()
    finally:
        conn.close()
