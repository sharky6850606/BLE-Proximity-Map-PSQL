import time
import threading

from database import get_db
from services.beacon_logic import latest_messages
from services.notifications_service import emit_notification
from config import (
    TTL_SECONDS,
    STILL_INTERVAL_SECONDS,
    DUP_SUPPRESS_SECONDS,
    EVALUATOR_INTERVAL_SECONDS,
)

_last_event_ts = {}      # (beacon_id, type) → ts
_last_still_ts = {}      # beacon_id → ts
_presence_state = {}     # beacon_id → bool


def _now():
    return int(time.time())


def _can_emit(key, ts, window):
    last = _last_event_ts.get(key)
    if last and ts - last < window:
        return False
    _last_event_ts[key] = ts
    return True


def evaluator_tick():
    now = _now()

    for device_id, snap in list(latest_messages.items()):
        beacons = snap.get("beacons") or []
        seen = set()

        for b in beacons:
            bid = b.get("id")
            if not bid:
                continue

            seen.add(bid)
            prev = _presence_state.get(bid, False)

            # ---------- IN ----------
            if not prev:
                if _can_emit((bid, "in"), now, DUP_SUPPRESS_SECONDS):
                    emit_notification(
                        "in", bid, device_id=device_id, distance=b.get("distance")
                    )
                _presence_state[bid] = True
                _last_still_ts[bid] = now
                continue

            # ---------- STILL IN ----------
            last_still = _last_still_ts.get(bid, 0)
            if now - last_still >= STILL_INTERVAL_SECONDS:
                emit_notification(
                    "still_in", bid, device_id=device_id, distance=b.get("distance")
                )
                _last_still_ts[bid] = now

        # ---------- LEFT / STILL OUT ----------
        for bid, was_present in list(_presence_state.items()):
            if was_present and bid not in seen:
                last_seen = latest_messages.get(device_id, {}).get("ts", 0)
                if now - last_seen > TTL_SECONDS:
                    if _can_emit((bid, "left"), now, DUP_SUPPRESS_SECONDS):
                        emit_notification("left", bid, device_id=device_id)
                    _presence_state[bid] = False
                    _last_still_ts[bid] = now

            if not _presence_state.get(bid):
                last_still = _last_still_ts.get(bid, 0)
                if now - last_still >= STILL_INTERVAL_SECONDS:
                    emit_notification("still_out", bid)
                    _last_still_ts[bid] = now


def _loop():
    while True:
        try:
            evaluator_tick()
        except Exception as e:
            print("[evaluator] error:", e)
        time.sleep(EVALUATOR_INTERVAL_SECONDS)


def start_evaluator_thread():
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    print(
        f"[evaluator] started interval={EVALUATOR_INTERVAL_SECONDS}s ttl={TTL_SECONDS}s"
    )
