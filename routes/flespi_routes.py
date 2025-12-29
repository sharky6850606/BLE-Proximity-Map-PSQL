import os
import time
from flask import Blueprint, request

from database import get_db
from services.beacon_logic import simplify_message, latest_messages
from services.uptime_service import log_uptime_snapshot

flespi_bp = Blueprint("flespi", __name__)

# Default TTL should match what you decided (7 minutes = 420s)
DEFAULT_TTL_SECONDS = int(os.getenv("BEACON_TTL_SECONDS", "420"))

def _extract_messages(payload):
    if payload is None:
        return None
    if isinstance(payload, dict):
        return payload.get("data") or payload.get("messages") or payload.get("result")
    if isinstance(payload, list):
        return payload
    return None

def _iter_seen_beacons(snap: dict):
    """
    Try to extract beacon IDs from many possible snap shapes.
    We return a list of beacon keys (strings).
    """
    if not isinstance(snap, dict):
        return []

    # Most common: snap["beacons"] = [{"id": "..."} , ...]
    for key in ("beacons", "ble_beacons", "ble", "ble_scan", "eye", "tags"):
        val = snap.get(key)
        if isinstance(val, list):
            out = []
            for item in val:
                if isinstance(item, dict):
                    bid = item.get("id") or item.get("beacon_id") or item.get("mac") or item.get("uuid") or item.get("key")
                    if bid:
                        out.append(str(bid))
                elif isinstance(item, str):
                    out.append(item)
            if out:
                return out

        # Sometimes beacons are dict keyed by id -> details
        if isinstance(val, dict):
            keys = [str(k) for k in val.keys()]
            if keys:
                return keys

    # Some simplify_message implementations store a flat list
    val = snap.get("seen_beacons")
    if isinstance(val, list) and val:
        return [str(x.get("id") if isinstance(x, dict) else x) for x in val if x]

    return []

def _safe_exec(conn, sql, params):
    try:
        conn.execute(sql, params)
        return True
    except Exception:
        return False

@flespi_bp.route("/flespi", methods=["POST"])
def flespi_receiver():
    payload = request.get_json(silent=True)
    msgs = _extract_messages(payload)
    if not msgs:
        return "No data", 400

    now_ts = int(time.time())
    processed = 0

    seen_devices = set()
    seen_beacons_by_device = {}  # device_ident -> [beacon_key,...]

    for raw in msgs:
        if not isinstance(raw, dict):
            continue

        snap = simplify_message(raw)
        if not isinstance(snap, dict):
            continue

        ident = snap.get("ident") or snap.get("device_ident") or snap.get("device_id")
        if not ident:
            continue

        ident = str(ident)
        latest_messages[ident] = snap  # keep cache for existing map code
        seen_devices.add(ident)

        bkeys = _iter_seen_beacons(snap)
        if bkeys:
            seen_beacons_by_device.setdefault(ident, [])
            # de-dupe but keep order
            for b in bkeys:
                b = str(b).strip()
                if b and b not in seen_beacons_by_device[ident]:
                    seen_beacons_by_device[ident].append(b)

        processed += 1

    # Persist device online states + beacon last-seen states (DB-first so evaluator + reports work)
    conn = None
    try:
        conn = get_db()

        # ---- device_states upsert ----
        for ident in seen_devices:
            conn.execute(
                "INSERT INTO device_states (device_key, state, last_change_ts, device_ident, online, last_seen_ts, last_online_ts) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(device_key) DO UPDATE SET "
                "state=excluded.state, last_change_ts=excluded.last_change_ts, device_ident=excluded.device_ident, "
                "online=excluded.online, last_seen_ts=excluded.last_seen_ts, last_online_ts=excluded.last_online_ts",
                (ident, "online", now_ts, ident, 1, now_ts, now_ts),
            )

        # ---- beacon_states upsert ----
        # We try a "rich" schema first; if it fails, try a simpler schema.
        for ident, bkeys in seen_beacons_by_device.items():
            for beacon_key in bkeys:
                # Attempt 1: common rich schema
                ok = _safe_exec(
                    conn,
                    "INSERT INTO beacon_states (beacon_key, state, last_change_ts, device_ident, active, last_seen_ts) "
                    "VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(beacon_key) DO UPDATE SET "
                    "state=excluded.state, last_change_ts=excluded.last_change_ts, device_ident=excluded.device_ident, "
                    "active=excluded.active, last_seen_ts=excluded.last_seen_ts",
                    (beacon_key, "in", now_ts, ident, 1, now_ts),
                )
                if not ok:
                    # Attempt 2: simpler schema (no device_ident/active)
                    _safe_exec(
                        conn,
                        "INSERT INTO beacon_states (beacon_key, state, last_change_ts, last_seen_ts) "
                        "VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT(beacon_key) DO UPDATE SET "
                        "state=excluded.state, last_change_ts=excluded.last_change_ts, last_seen_ts=excluded.last_seen_ts",
                        (beacon_key, "in", now_ts, now_ts),
                    )

        conn.commit()

    except Exception as e:
        print(f"[warn] DB persist failed: {e}")
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

    # Uptime snapshot best-effort
    try:
        log_uptime_snapshot()
    except Exception as e:
        print(f"[warn] uptime snapshot failed: {e}")

    print(f"[flespi] received={len(msgs)} processed={processed} devices={len(seen_devices)}")
    return "OK", 200
