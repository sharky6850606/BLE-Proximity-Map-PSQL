import time
from flask import Blueprint, request

from database import get_db
from services.beacon_logic import simplify_message, latest_messages
from services.uptime_service import log_uptime_snapshot

flespi_bp = Blueprint("flespi", __name__)


def _extract_messages(payload):
    if payload is None:
        return None
    if isinstance(payload, dict):
        return payload.get("data") or payload.get("messages") or payload.get("result")
    if isinstance(payload, list):
        return payload
    return None


@flespi_bp.route("/flespi", methods=["POST"])
def flespi_receiver():
    payload = request.get_json(silent=True)
    msgs = _extract_messages(payload)
    if not msgs:
        return "No data", 400

    now_ts = int(time.time())
    processed = 0
    seen_devices = set()

    conn = get_db()

    try:
        for raw in msgs:
            if not isinstance(raw, dict):
                continue

            snap = simplify_message(raw)
            device_ident = snap.get("ident")
            if not device_ident:
                continue

            # ---------------------------
            # Track latest device message
            # ---------------------------
            latest_messages[device_ident] = snap
            seen_devices.add(device_ident)
            processed += 1

            # ---------------------------
            # DEVICE STATE (ONLINE)
            # ---------------------------
            conn.execute(
                """
                INSERT INTO device_states
                    (device_key, state, last_change_ts, device_ident, online, last_seen_ts, last_online_ts)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(device_key)
                DO UPDATE SET
                    state = EXCLUDED.state,
                    last_change_ts = EXCLUDED.last_change_ts,
                    device_ident = EXCLUDED.device_ident,
                    online = EXCLUDED.online,
                    last_seen_ts = EXCLUDED.last_seen_ts,
                    last_online_ts = EXCLUDED.last_online_ts
                """,
                (
                    device_ident,
                    "online",
                    now_ts,
                    device_ident,
                    1,
                    now_ts,
                    now_ts,
                ),
            )

            # ---------------------------
            # BEACON STATE (THIS WAS MISSING)
            # ---------------------------
            beacons = snap.get("beacons") or []
            for b in beacons:
                beacon_id = b.get("id") or b.get("beacon_id")
                if not beacon_id:
                    continue

                conn.execute(
                    """
                    INSERT INTO beacon_states
                        (beacon_key, state, last_change_ts, active)
                    VALUES (%s,%s,%s,1)
                    ON CONFLICT (beacon_key)
                    DO UPDATE SET
                        state = 'in',
                        last_change_ts = EXCLUDED.last_change_ts,
                        active = 1
                    """,
                    (
                        beacon_id,
                        "in",
                        now_ts,
                    ),
                )

        conn.commit()

    except Exception as e:
        print(f"[warn] flespi processing failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass

    finally:
        try:
            conn.close()
        except Exception:
            pass

    # ---------------------------
    # UPTIME SNAPSHOT
    # ---------------------------
    try:
        log_uptime_snapshot()
    except Exception as e:
        print(f"[warn] uptime snapshot failed: {e}")

    print(f"[flespi] received={len(msgs)} processed={processed}")
    return "OK", 200
