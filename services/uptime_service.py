import time
from database import get_db
from services.beacon_logic import latest_messages, format_samoa_time

def log_uptime_snapshot():
    conn = get_db()
    try:
        now = time.time()
        device_count = sum(1 for k in latest_messages.keys() if k != "DAILY_REPORT")
        beacon_count = 0
        for k, v in latest_messages.items():
            if k == "DAILY_REPORT":
                continue
            if isinstance(v, dict):
                beacon_count += len(v.get("beacons") or [])
        conn.execute(
            "INSERT INTO uptime_logs (timestamp, device_count, beacon_count, status) VALUES (%s,%s,%s,%s)",
            (format_samoa_time(now), device_count, beacon_count, "OK"),
        )
        conn.commit()
    finally:
        conn.close()
