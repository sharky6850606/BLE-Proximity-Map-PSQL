import time
import threading

from database import get_db
from services.beacon_logic import latest_messages, format_samoa_time
from config import BEACON_TTL_SECONDS, DEVICE_TTL_SECONDS, STILL_INTERVAL_SECONDS, DUP_SUPPRESS_SECONDS, EVALUATOR_INTERVAL_SECONDS


def _samoa_iso_now():
    return format_samoa_time(time.time()).replace(" ", "T")


def _emit_notification(conn, ntype: str, name: str, event_time: str, device_ident: str | None = None, distance=None):
    """Insert a notification row."""
    conn.execute(
        "INSERT INTO notifications (type, beacon_name, event_time, created_at, device_ident, distance) VALUES (%s,%s,%s,%s,%s,%s)",
        (ntype, name, event_time, _samoa_iso_now(), device_ident, distance),
    )


def _should_suppress(last_type, last_ts, new_type, now_ts: int) -> bool:
    if not last_type or not last_ts:
        return False
    if last_type == new_type and (now_ts - int(last_ts)) < DUP_SUPPRESS_SECONDS:
        return True
    return False


def _ensure_rows(conn, table: str, key_col: str, key_val: str):
    if table == "beacon_states":
        conn.execute(
            "INSERT INTO beacon_states (beacon_key, state, last_change_ts, active) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (beacon_key) DO NOTHING",
            (key_val, "unknown", int(time.time()), 1),
        )
    elif table == "device_states":
        conn.execute(
            "INSERT INTO device_states (device_key, state, last_change_ts, device_ident, online) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (device_key) DO NOTHING",
            (key_val, "unknown", int(time.time()), key_val, 0),
        )


def _evaluate_once():
    now_ts = int(time.time())
    now_label = format_samoa_time(now_ts).replace(" ", "T")

    conn = get_db()
    try:
        # -------- Devices: online/offline + still_online/still_offline --------
        dev_rows = conn.execute(
            "SELECT device_key, COALESCE(state,''), COALESCE(last_change_ts,0), COALESCE(device_ident,''), "
            "COALESCE(online,0), COALESCE(last_event_ts,0), COALESCE(last_event_type,''), COALESCE(last_still_ts,0) "
            "FROM device_states"
        ).fetchall()
        dev_state = {r[0]: r for r in dev_rows}

        for device_ident, dev in list(latest_messages.items()):
            if device_ident == "DAILY_REPORT" or not isinstance(dev, dict):
                continue
            last_seen_raw = int(dev.get("timestamp_raw") or 0)
            is_online = 1 if (now_ts - last_seen_raw) <= DEVICE_TTL_SECONDS else 0
            desired_state = "online" if is_online else "offline"

            _ensure_rows(conn, "device_states", "device_key", device_ident)
            row = dev_state.get(device_ident)
            if row:
                _, cur_state, cur_change, _, cur_online, last_event_ts, last_event_type, last_still_ts = row
            else:
                cur_state, cur_online, cur_change, last_event_ts, last_event_type, last_still_ts = "unknown", 0, 0, 0, "", 0

            if (cur_online != is_online) or (cur_state != desired_state):
                if not _should_suppress(last_event_type, last_event_ts, desired_state, now_ts):
                    _emit_notification(conn, desired_state, device_ident, now_label, device_ident=device_ident, distance=None)
                    conn.execute(
                        "UPDATE device_states SET state=%s, online=%s, last_change_ts=%s, last_event_ts=%s, last_event_type=%s WHERE device_key=%s",
                        (desired_state, is_online, now_ts, now_ts, desired_state, device_ident),
                    )
            else:
                # still notifications every 15 min
                if last_still_ts is None:
                    last_still_ts = 0
                if (now_ts - int(last_still_ts or 0)) >= STILL_INTERVAL_SECONDS:
                    still_type = "still_online" if is_online else "still_offline"
                    if not _should_suppress(last_event_type, last_event_ts, still_type, now_ts):
                        _emit_notification(conn, still_type, device_ident, now_label, device_ident=device_ident, distance=None)
                        conn.execute(
                            "UPDATE device_states SET last_still_ts=%s, last_event_ts=%s, last_event_type=%s WHERE device_key=%s",
                            (now_ts, now_ts, still_type, device_ident),
                        )

        # -------- Beacons: in/left + still_in/still_out --------
        b_rows = conn.execute(
            "SELECT beacon_key, COALESCE(state,''), COALESCE(last_change_ts,0), COALESCE(active,1), "
            "COALESCE(last_event_ts,0), COALESCE(last_event_type,''), COALESCE(last_still_ts,0) "
            "FROM beacon_states"
        ).fetchall()
        b_state = {r[0]: r for r in b_rows}

        # Build snapshot of current beacons from memory
        current = {}
        for device_ident, dev in list(latest_messages.items()):
            if device_ident == "DAILY_REPORT" or not isinstance(dev, dict):
                continue
            for b in (dev.get("beacons") or []):
                bid = b.get("id")
                if not bid:
                    continue
                current[bid] = {
                    "device_ident": device_ident,
                    "last_seen_raw": int(b.get("last_seen_raw") or 0),
                    "last_seen": b.get("last_seen") or "",
                    "distance": b.get("distance"),
                }

        for bid, info in current.items():
            last_seen_raw = info["last_seen_raw"]
            in_range = True if last_seen_raw and (now_ts - last_seen_raw) <= BEACON_TTL_SECONDS else False
            desired_state = "in" if in_range else "left"
            event_time = (info["last_seen"] or now_label)
            device_ident = info["device_ident"]
            distance = info.get("distance")

            _ensure_rows(conn, "beacon_states", "beacon_key", bid)
            row = b_state.get(bid)
            if row:
                _, cur_state, _, _, last_event_ts, last_event_type, last_still_ts = row
            else:
                cur_state, last_event_ts, last_event_type, last_still_ts = "unknown", 0, "", 0

            # initial: if unknown -> set and emit IN if in
            if cur_state in ("", "unknown"):
                conn.execute(
                    "UPDATE beacon_states SET state=%s, last_change_ts=%s, last_event_ts=%s, last_event_type=%s, active=1 WHERE beacon_key=%s",
                    (desired_state, now_ts, now_ts, "init_"+desired_state, bid),
                )
                if desired_state == "in":
                    _emit_notification(conn, "in", bid, event_time, device_ident=device_ident, distance=distance)
                    conn.execute(
                        "UPDATE beacon_states SET last_event_ts=%s, last_event_type=%s WHERE beacon_key=%s",
                        (now_ts, "in", bid),
                    )
                continue

            if cur_state != desired_state:
                if not _should_suppress(last_event_type, last_event_ts, desired_state, now_ts):
                    _emit_notification(conn, desired_state, bid, now_label if desired_state=="left" else event_time, device_ident=device_ident, distance=distance)
                    conn.execute(
                        "UPDATE beacon_states SET state=%s, last_change_ts=%s, last_event_ts=%s, last_event_type=%s, active=1 WHERE beacon_key=%s",
                        (desired_state, now_ts, now_ts, desired_state, bid),
                    )
            else:
                # still notifications every 15 min
                if (now_ts - int(last_still_ts or 0)) >= STILL_INTERVAL_SECONDS:
                    still_type = "still_in" if desired_state == "in" else "still_out"
                    if not _should_suppress(last_event_type, last_event_ts, still_type, now_ts):
                        _emit_notification(conn, still_type, bid, now_label, device_ident=device_ident, distance=distance)
                        conn.execute(
                            "UPDATE beacon_states SET last_still_ts=%s, last_event_ts=%s, last_event_type=%s WHERE beacon_key=%s",
                            (now_ts, now_ts, still_type, bid),
                        )

        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def evaluator_loop():
    while True:
        try:
            _evaluate_once()
        except Exception:
            # Never crash the thread
            pass
        time.sleep(EVALUATOR_INTERVAL_SECONDS)


def start_evaluator_thread():
    t = threading.Thread(target=evaluator_loop, daemon=True)
    t.start()
    return t
