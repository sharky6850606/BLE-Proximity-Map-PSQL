import time
from database import get_db
from services.beacon_logic import format_samoa_time


def _db_ph(conn):
    return "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"


def log_uptime_snapshot():
    conn = get_db()
    try:
        ph = _db_ph(conn)
        now = time.time()
        now_ts = int(now)

        device_count = int(conn.execute(
            f"SELECT COUNT(*) FROM device_states WHERE online = {ph}",
            (1,),
        ).fetchone()[0] or 0)

        beacon_count = int(conn.execute(
            f"SELECT COUNT(*) FROM beacon_states "
            f"WHERE last_seen_ts IS NOT NULL AND last_seen_ts >= {ph} AND (missing IS NULL OR missing = 0)",
            (now_ts - (15 * 60),),
        ).fetchone()[0] or 0)

        if device_count <= 0:
            status = "NO_DEVICES"
        elif beacon_count <= 0:
            status = "NO_BEACONS"
        else:
            status = "OK"

        conn.execute(
            f"INSERT INTO uptime_logs (timestamp, device_count, beacon_count, status) VALUES ({ph},{ph},{ph},{ph})",
            (format_samoa_time(now), device_count, beacon_count, status),
        )
        conn.commit()
    finally:
        conn.close()
