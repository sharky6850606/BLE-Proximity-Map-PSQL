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
    seen = set()

    for raw in msgs:
        if not isinstance(raw, dict):
            continue

        snap = simplify_message(raw)

        ident = snap.get("ident")
        if ident is None:
            continue

        ident = str(ident).strip()  # ✅ FORCE STRING ID ALWAYS
        if not ident:
            continue

        snap["ident"] = ident  # ✅ keep inside payload consistent too
        latest_messages[ident] = snap

        seen.add(ident)
        processed += 1

    # Persist online state best-effort
    conn = None
    try:
        conn = get_db()
        for ident in seen:
            conn.execute(
                "INSERT INTO device_states (device_key, state, last_change_ts, device_ident, online, last_seen_ts, last_online_ts) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(device_key) DO UPDATE SET "
                "state=excluded.state, last_change_ts=excluded.last_change_ts, device_ident=excluded.device_ident, "
                "online=excluded.online, last_seen_ts=excluded.last_seen_ts, last_online_ts=excluded.last_online_ts",
                (ident, "online", now_ts, ident, 1, now_ts, now_ts),
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

    print(f"[flespi] received={len(msgs)} processed={processed} devices={len(seen)}")
    return "OK", 200
