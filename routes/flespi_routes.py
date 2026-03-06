import time
from flask import Blueprint, request

from database import get_db
from services.beacon_logic import simplify_message, latest_messages
from services.uptime_service import log_uptime_snapshot

flespi_bp = Blueprint("flespi", __name__)

# Keep online across expected staggered flespi packets (device/beacon can arrive separately).
OFFLINE_AFTER_SEC = 15 * 60


def _extract_messages(payload):
    if payload is None:
        return None
    if isinstance(payload, dict):
        return payload.get("data") or payload.get("messages") or payload.get("result")
    if isinstance(payload, list):
        return payload
    return None


def _db_ph(conn):
    return "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"


@flespi_bp.route("/flespi", methods=["POST"])
def flespi_receiver():
    payload = request.get_json(silent=True)
    msgs = _extract_messages(payload)
    if not msgs:
        return "No data", 400

    now_ts = int(time.time())
    processed = 0
    seen = {}  # ident -> latest snap
    beacon_updates = []

    for raw in msgs:
        if not isinstance(raw, dict):
            continue
        snap = simplify_message(raw)
        ident = snap.get("ident")
        if not ident:
            continue
        latest_messages[ident] = snap
        seen[ident] = snap
        processed += 1
        for b in snap.get("beacons") or []:
            bid = b.get("id")
            if not bid:
                continue
            beacon_updates.append(
                (
                    f"{ident}:{bid}",
                    ident,
                    bid,
                    now_ts,
                    b.get("distance"),
                    b.get("rssi"),
                )
            )

    # Persist online state best-effort
    conn = None
    try:
        conn = get_db()
        ph = _db_ph(conn)
        for ident, snap in seen.items():
            payload_ts = int(snap.get("timestamp_raw") or now_ts)
            lat = snap.get("lat")
            lon = snap.get("lon")
            conn.execute(
                f"INSERT INTO device_states (device_key, state, last_change_ts, device_ident, online, last_seen_ts, last_online_ts, last_lat, last_lon, last_payload_ts) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph}) "
                "ON CONFLICT(device_key) DO UPDATE SET "
                "state=excluded.state, last_change_ts=excluded.last_change_ts, device_ident=excluded.device_ident, "
                "online=excluded.online, last_seen_ts=excluded.last_seen_ts, last_online_ts=excluded.last_online_ts, "
                "last_lat=COALESCE(excluded.last_lat, device_states.last_lat), "
                "last_lon=COALESCE(excluded.last_lon, device_states.last_lon), "
                "last_payload_ts=excluded.last_payload_ts",
                (ident, "online", now_ts, ident, 1, now_ts, now_ts, lat, lon, payload_ts),
            )
        # Recompute online/offline from freshness so analytics/uptime stay accurate.
        conn.execute(
            f"UPDATE device_states SET online=0, state='offline' WHERE last_seen_ts IS NULL OR last_seen_ts < {ph}",
            (now_ts - OFFLINE_AFTER_SEC,),
        )
        conn.execute(
            f"UPDATE device_states SET online=1, state='online', last_change_ts={ph} WHERE last_seen_ts >= {ph}",
            (now_ts, now_ts - OFFLINE_AFTER_SEC),
        )

        for beacon_key, device_ident, beacon_id, last_seen_ts, last_distance, last_rssi in beacon_updates:
            conn.execute(
                f"INSERT INTO beacon_states "
                f"(beacon_key, device_ident, beacon_id, last_seen_ts, last_distance, last_rssi, active, missing) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},1,0) "
                "ON CONFLICT(beacon_key) DO UPDATE SET "
                "device_ident=excluded.device_ident, beacon_id=excluded.beacon_id, "
                "last_seen_ts=excluded.last_seen_ts, last_distance=excluded.last_distance, "
                "last_rssi=excluded.last_rssi, active=1",
                (beacon_key, device_ident, beacon_id, last_seen_ts, last_distance, last_rssi),
            )
        conn.commit()
    except Exception as e:
        print(f"[warn] device_states write failed: {e}")
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    try:
        log_uptime_snapshot()
    except Exception as e:
        print(f"[warn] uptime snapshot failed: {e}")

    print(f"[flespi] received={len(msgs)} processed={processed}")
    return "OK", 200
