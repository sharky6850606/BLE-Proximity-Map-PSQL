import time
from datetime import datetime, timedelta

from config import SAMOA_OFFSET_HOURS, TTL_SECONDS, TX_POWER, PATH_LOSS_N

latest_messages = {}  # device_ident -> snapshot
_beacon_state = {}    # (device_ident, beacon_id) -> beacon snapshot


def format_samoa_time(ts: float) -> str:
    dt = datetime.utcfromtimestamp(float(ts)) + timedelta(hours=SAMOA_OFFSET_HOURS)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _coerce_timestamp(ts_raw):
    try:
        v = float(ts_raw)
    except Exception:
        return time.time()
    if v > 1e12:
        v = v / 1000.0
    return v


def rssi_to_distance(rssi, tx_power=TX_POWER, n=PATH_LOSS_N):
    try:
        rssi = float(rssi)
    except Exception:
        return None
    try:
        return round(10 ** ((tx_power - rssi) / (10 * n)), 2)
    except Exception:
        return None


def voltage_to_percent(v):
    try:
        v = float(v)
    except Exception:
        return None
    lo, hi = 2.0, 3.0
    p = (v - lo) / (hi - lo) * 100.0
    return int(max(0.0, min(100.0, p)))


def simplify_message(msg: dict) -> dict:
    ident = msg.get("ident") or msg.get("device.ident") or msg.get("device_id") or ""
    ident = str(ident).strip()
    if not ident:
        return {}

    ts_raw = msg.get("timestamp") or msg.get("server.timestamp") or time.time()
    ts = _coerce_timestamp(ts_raw)

    lat = msg.get("position.latitude")
    lon = msg.get("position.longitude")

    raw_beacons = msg.get("ble.beacons") or msg.get("ble.beacons.list") or []
    now_ts = time.time()

    if isinstance(raw_beacons, list):
        for b in raw_beacons:
            if not isinstance(b, dict):
                continue
            bid = b.get("id") or b.get("uuid") or b.get("mac") or ""
            bid = str(bid).strip() if bid else ""
            if not bid:
                continue
            rssi = b.get("rssi")
            dist = rssi_to_distance(rssi)
            voltage = b.get("battery.voltage") or (b.get("battery") or {}).get("voltage")

            _beacon_state[(ident, bid)] = {
                "id": bid,
                "rssi": rssi,
                "distance": dist,
                "last_seen_raw": now_ts,
                "last_seen": format_samoa_time(now_ts),
                "battery_percent": voltage_to_percent(voltage),
            }

    beacons = []
    for (dev_id, bid), info in list(_beacon_state.items()):
        if dev_id != ident:
            continue
        last_seen_raw = float(info.get("last_seen_raw") or 0)
        if (now_ts - last_seen_raw) <= TTL_SECONDS:
            beacons.append({
                "id": info.get("id"),
                "rssi": info.get("rssi"),
                "distance": info.get("distance"),
                "last_seen": info.get("last_seen"),
                "battery_percent": info.get("battery_percent"),
            })
        else:
            _beacon_state.pop((dev_id, bid), None)

    beacons.sort(key=lambda x: (x.get("distance") is None, x.get("distance") or 9999))

    return {
        "id": ident,
        "ident": ident,
        "timestamp_raw": ts,
        "timestamp": format_samoa_time(ts),
        "lat": lat,
        "lon": lon,
        "beacons": beacons,
    }
