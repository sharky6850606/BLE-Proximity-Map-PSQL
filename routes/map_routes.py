import time
from flask import Blueprint, jsonify, render_template, redirect, url_for, request

from config import TTL_SECONDS
from database import get_db
from services.audit_log_service import log_event
from services.beacon_logic import latest_messages, format_samoa_time
from services.auth_service import (
    can_access_device,
    current_user,
    device_scope_clause,
    is_admin,
    login_required,
)

map_bp = Blueprint("map", __name__)


def _db_ph(conn):
    return "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"


@map_bp.route("/")
def root():
    return redirect(url_for("map.map_page"))


@map_bp.route("/map")
@login_required
def map_page():
    return render_template("index.html")


@map_bp.route("/data")
@login_required
def data():
    """Serve live map data from DB state to stay correct across multi-worker deployments."""
    beacon_names = {}
    devices_meta = {}
    out = []

    now_ts = int(time.time())
    max_age = int(TTL_SECONDS)

    conn = None
    try:
        conn = get_db()
        ph = _db_ph(conn)

        rows = conn.execute("SELECT id, name FROM beacon_names").fetchall()
        beacon_names = {str(bid): (nm or "") for (bid, nm) in rows if bid}
        user = current_user()
        if not is_admin(user) and user.get("customer_id"):
            rows = conn.execute(
                f"SELECT beacon_id, name FROM customer_beacon_names WHERE customer_id = {ph}",
                (user.get("customer_id"),),
            ).fetchall()
            beacon_names.update({str(bid): (nm or "") for (bid, nm) in rows if bid})

        rows = conn.execute("SELECT id, name, color FROM devices").fetchall()
        for did, nm, col in rows:
            if did:
                devices_meta[str(did)] = {"name": nm or "", "color": col or ""}
        if not is_admin(user) and user.get("customer_id"):
            rows = conn.execute(
                f"SELECT device_ident, name, color FROM customer_device_settings WHERE customer_id = {ph}",
                (user.get("customer_id"),),
            ).fetchall()
            for did, nm, col in rows:
                if did:
                    devices_meta.setdefault(str(did), {})
                    if nm:
                        devices_meta[str(did)]["name"] = nm
                    if col:
                        devices_meta[str(did)]["color"] = col

        # Pull device rows from persisted state (single source of truth)
        scope_sql, scope_params = device_scope_clause(conn, "device_ident", user=user)
        drows = conn.execute(
            "SELECT device_ident, last_seen_ts, last_lat, last_lon "
            "FROM device_states "
            f"WHERE device_ident IS NOT NULL AND {scope_sql}",
            scope_params,
        ).fetchall()

        # Pull fresh beacon rows and group by device
        bscope_sql, bscope_params = device_scope_clause(conn, "device_ident", user=user)
        brows = conn.execute(
            f"SELECT device_ident, beacon_id, last_seen_ts, last_distance, last_rssi, last_battery_voltage, last_battery_percent, missing "
            f"FROM beacon_states "
            f"WHERE beacon_id IS NOT NULL AND last_seen_ts IS NOT NULL AND (missing IS NULL OR missing = 0) "
            f"AND last_seen_ts >= {ph} AND {bscope_sql}",
            (now_ts - max_age,) + bscope_params,
        ).fetchall()

        beacons_by_device = {}
        for device_ident, beacon_id, last_seen_ts, last_distance, last_rssi, battery_voltage, battery_percent, _missing in brows:
            did = str(device_ident or "").strip()
            bid = str(beacon_id or "").strip()
            if not did or not bid:
                continue
            beacons_by_device.setdefault(did, []).append({
                "id": bid,
                "rssi": last_rssi,
                "distance": float(last_distance) if last_distance is not None else None,
                "last_seen": format_samoa_time(int(last_seen_ts)),
                "battery_voltage": float(battery_voltage) if battery_voltage is not None else None,
                "battery_percent": int(battery_percent) if battery_percent is not None else None,
            })

        for device_ident, last_seen_ts, last_lat, last_lon in drows:
            did = str(device_ident or "").strip()
            if not did or last_seen_ts is None:
                continue

            # Keep device visible slightly longer than beacon TTL to survive staggered payloads.
            if (now_ts - int(last_seen_ts)) > (max_age * 2):
                continue

            meta = devices_meta.get(did, {})
            device_beacons = beacons_by_device.get(did, [])
            device_beacons.sort(key=lambda x: (x.get("distance") is None, x.get("distance") or 9999))

            out.append({
                "id": did,
                "ident": did,
                "timestamp_raw": int(last_seen_ts),
                "timestamp": format_samoa_time(int(last_seen_ts)),
                "lat": float(last_lat) if last_lat is not None else None,
                "lon": float(last_lon) if last_lon is not None else None,
                "beacons": device_beacons,
                "name": meta.get("name") or None,
                "color": meta.get("color") or None,
            })

    except Exception as e:
        print("[warn] /data db read failed:", e)
        # Fallback to in-memory snapshots so map still works in degraded mode.
        snapshot = dict(latest_messages)
        for did, snap in snapshot.items():
            if did == "DAILY_REPORT" or not isinstance(snap, dict):
                continue
            meta = devices_meta.get(did, {})
            d = dict(snap)
            d["id"] = did
            d["name"] = meta.get("name") or None
            d["color"] = meta.get("color") or None
            out.append(d)
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    return jsonify({"devices": out, "beacon_names": beacon_names})


@map_bp.route("/rename", methods=["POST"])
@login_required
def rename_beacon():
    data = request.get_json(silent=True) or {}
    # Frontend compatibility:
    # - Old UI sends: { beacon_id: "...", name: "..." }
    # - API expects:  { id: "...", name: "..." }
    beacon_id = (data.get("id") or data.get("beacon_id") or "").strip()
    new_name = ((data.get("name") or data.get("new_name") or data.get("newName") or "")).strip()
    if not beacon_id:
        return jsonify({"error": "missing id"}), 400

    conn = get_db()
    try:
        ph = _db_ph(conn)
        user = current_user()
        if is_admin(user):
            conn.execute(
                f"INSERT INTO beacon_names (id, name) VALUES ({ph},{ph}) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (beacon_id, new_name),
            )
            log_customer_id = None
        else:
            log_customer_id = user.get("customer_id")
            conn.execute(
                f"INSERT INTO customer_beacon_names (customer_id, beacon_id, name, updated_at) "
                f"VALUES ({ph},{ph},{ph},{ph}) "
                "ON CONFLICT(customer_id, beacon_id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
                (log_customer_id, beacon_id, new_name, format_samoa_time(time.time()).replace(" ", "T")),
            )
        log_event(
            "rename.beacon",
            target_type="beacon",
            target_id=beacon_id,
            details=f"Renamed beacon {beacon_id} to {new_name or '(blank)'}",
            actor_user=user,
            customer_id=log_customer_id,
            conn=conn,
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "ok"})


@map_bp.route("/rename_device", methods=["POST"])
@login_required
def rename_device():
    data = request.get_json(silent=True) or {}
    # Frontend compatibility:
    # - Old UI sends: { device_id: "...", name: "...", color: "..." }
    # - API expects:  { id: "...", name: "...", color: "..." }
    device_id = (data.get("id") or data.get("device_id") or "").strip()
    new_name = ((data.get("name") or data.get("new_name") or data.get("newName") or "")).strip()
    color = (data.get("color") or "").strip() or None
    if not device_id:
        return jsonify({"error": "missing id"}), 400

    conn = get_db()
    try:
        ph = _db_ph(conn)
        user = current_user()
        if not can_access_device(device_id, conn=conn, user=user):
            return jsonify({"error": "forbidden"}), 403
        if is_admin(user):
            conn.execute(
                f"INSERT INTO devices (id, name, color) VALUES ({ph},{ph},{ph}) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, color=excluded.color",
                (device_id, new_name, color),
            )
            log_customer_id = None
        else:
            log_customer_id = user.get("customer_id")
            conn.execute(
                f"INSERT INTO customer_device_settings (customer_id, device_ident, name, color, updated_at) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph}) "
                "ON CONFLICT(customer_id, device_ident) DO UPDATE SET "
                "name=excluded.name, color=excluded.color, updated_at=excluded.updated_at",
                (log_customer_id, device_id, new_name, color, format_samoa_time(time.time()).replace(" ", "T")),
            )
        log_event(
            "rename.device",
            target_type="device",
            target_id=device_id,
            details=f"Renamed device {device_id} to {new_name or '(blank)'}",
            actor_user=user,
            customer_id=log_customer_id,
            conn=conn,
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "ok"})
