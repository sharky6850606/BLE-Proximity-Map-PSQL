from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from database import DB_MODE, get_db


def _utc_iso() -> str:
    # Keep a simple ISO string so it renders nicely on the UI and PDFs.
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def save_notification(msg: Dict[str, Any]) -> None:
    """Persist a notification event.

    Expected keys (best-effort):
      - type
      - name / beacon_name
      - beacon_id
      - device_ident
      - time / event_time
      - distance
    """
    if not msg:
        return

    n_type = msg.get("type")
    beacon_name = msg.get("name") or msg.get("beacon_name")
    beacon_id = msg.get("beacon_id") or msg.get("beaconId")
    device_ident = msg.get("device_ident") or msg.get("device")
    event_time = msg.get("time") or msg.get("event_time")
    distance = msg.get("distance")
    created_at = _utc_iso()

    conn = get_db()
    cur = conn.cursor()

    if DB_MODE == "postgres":
        # Dedupe is enforced by a unique index created in init_db().
        cur.execute(
            """
            INSERT INTO notifications (type, beacon_name, beacon_id, device_ident, event_time, distance, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (n_type, beacon_name, beacon_id, device_ident, event_time, distance, created_at),
        )
        conn.commit()
    else:
        cur.execute(
            """
            INSERT OR IGNORE INTO notifications (type, beacon_name, beacon_id, device_ident, event_time, distance, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (n_type, beacon_name, beacon_id, device_ident, event_time, distance, created_at),
        )
        conn.commit()
