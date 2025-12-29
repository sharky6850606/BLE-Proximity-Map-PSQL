from flask import Blueprint, jsonify, render_template, redirect, url_for, request

from database import get_db
from services.beacon_logic import latest_messages

map_bp = Blueprint("map", __name__)


@map_bp.route("/")
def root():
    return redirect(url_for("map.map_page"))


@map_bp.route("/map")
def map_page():
    return render_template("index.html")


@map_bp.route("/data")
def data():
    """
    Hybrid data source:
    - Live positions from latest_messages (real-time map)
    - Names/colors from DB (rename persistence)
    - Beacon names from DB
    """

    snapshot = dict(latest_messages)

    beacon_names = {}
    devices_meta = {}

    conn = None
    try:
        conn = get_db()

        # ---- Beacon rename table ----
        rows = conn.execute("SELECT id, name FROM beacon_names").fetchall()
        for bid, nm in rows:
            if bid:
                beacon_names[str(bid)] = nm or ""

        # ---- Device rename + color ----
        rows = conn.execute("SELECT id, name, color FROM devices").fetchall()
        for did, nm, col in rows:
            if did:
                devices_meta[str(did)] = {
                    "name": nm or "",
                    "color": col or "",
                }

    except Exception as e:
        print("[warn] /data db read failed:", e)
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    out = []

    for did, snap in snapshot.items():
        if did == "DAILY_REPORT" or not isinstance(snap, dict):
            continue

        did = str(did)
        meta = devices_meta.get(did, {})

        d = dict(snap)
        d["id"] = did

        # Apply rename + color if exists
        d["name"] = meta.get("name") or None
        d["color"] = meta.get("color") or None

        out.append(d)

    return jsonify({
        "devices": out,
        "beacon_names": beacon_names
    })


# -------------------------
# Rename Beacon
# -------------------------
@map_bp.route("/rename", methods=["POST"])
def rename_beacon():
    data = request.get_json(silent=True) or {}

    beacon_id = (data.get("id") or data.get("beacon_id") or "").strip()
    new_name = (data.get("name") or "").strip()

    if not beacon_id:
        return jsonify({"error": "missing id"}), 400

    conn = get_db()
    try:
        ph = "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"
        conn.execute(
            f"""
            INSERT INTO beacon_names (id, name)
            VALUES ({ph},{ph})
            ON CONFLICT(id)
            DO UPDATE SET name = excluded.name
            """,
            (beacon_id, new_name),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"status": "ok"})


# -------------------------
# Rename Device
# -------------------------
@map_bp.route("/rename_device", methods=["POST"])
def rename_device():
    data = request.get_json(silent=True) or {}

    device_id = (data.get("id") or data.get("device_id") or "").strip()
    new_name = (data.get("name") or "").strip()
    color = (data.get("color") or "").strip() or None

    if not device_id:
        return jsonify({"error": "missing id"}), 400

    conn = get_db()
    try:
        ph = "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"
        conn.execute(
            f"""
            INSERT INTO devices (id, name, color)
            VALUES ({ph},{ph},{ph})
            ON CONFLICT(id)
            DO UPDATE SET
                name = excluded.name,
                color = excluded.color
            """,
            (device_id, new_name, color),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"status": "ok"})
