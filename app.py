import os
import time
from flask import Flask, request, jsonify, render_template, send_file, redirect, url_for, abort
import json


from database import init_db, get_db
from routes import map_bp, flespi_bp
from services.beacon_logic import format_samoa_time
from services.reporting_service import start_daily_thread, generate_daily_report, generate_activity_report
from services.evaluator_service import start_evaluator_thread

app = Flask(__name__)

init_db()

app.register_blueprint(map_bp)
app.register_blueprint(flespi_bp)

os.makedirs(os.path.abspath(os.getenv("REPORTS_DIR", "reports")), exist_ok=True)
os.makedirs(os.path.abspath(os.getenv("ACTIVITY_REPORTS_DIR", "activity_reports")), exist_ok=True)

if os.getenv("DISABLE_DAILY_THREAD", "0") != "1":
    start_daily_thread()

if os.getenv("DISABLE_EVALUATOR", "0") != "1":
    start_evaluator_thread()


def samoa_iso_now():
    return format_samoa_time(time.time()).replace(" ", "T")


@app.route("/api/notifications", methods=["POST"])
def save_notification():
    data = request.get_json(silent=True) or {}
    ntype = (data.get("type") or "").strip()
    name = (data.get("name") or "").strip()
    event_time = (data.get("time") or "").strip()
    device_ident = (data.get("device") or "").strip() or None
    distance = data.get("distance")

    if not ntype or not name:
        return jsonify({"error": "invalid"}), 400

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO notifications (type, beacon_name, event_time, created_at, device_ident, distance) VALUES (%s,%s,%s,%s,%s,%s)",
            (ntype, name, event_time, samoa_iso_now(), device_ident, distance),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"status": "ok"}), 201


@app.route("/reports/history")
def reports_history():
    conn = get_db()
    try:
        reports = conn.execute("SELECT id, created_at, summary, pdf_path FROM daily_reports ORDER BY id DESC LIMIT 200").fetchall()
    finally:
        conn.close()
    return render_template("reports_history.html", reports=reports)


@app.route("/download_daily_report/<int:report_id>")
def download_daily_report(report_id: int):
    conn = get_db()
    try:
        row = conn.execute("SELECT pdf_path FROM daily_reports WHERE id=%s", (report_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row[0] or not os.path.exists(row[0]):
        abort(404)
    return send_file(row[0], as_attachment=True)


@app.route("/notifications/history")
def notifications_history():
    conn = get_db()
    try:
        notifications = conn.execute("SELECT created_at, type, beacon_name, event_time, distance, device_ident FROM notifications ORDER BY id DESC LIMIT 500").fetchall()
    finally:
        conn.close()
    return render_template("notifications_history.html", notifications=notifications)


@app.route("/download_activity_report/<int:report_id>")
def download_activity_report(report_id: int):
    conn = get_db()
    try:
        row = conn.execute("SELECT pdf_path FROM activity_reports WHERE id=%s", (report_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row[0] or not os.path.exists(row[0]):
        abort(404)
    return send_file(row[0], as_attachment=True)


@app.route("/activity-reports", methods=["GET","POST"])
def activity_reports_page():
    conn = get_db()
    try:
        beacons = conn.execute("SELECT id, name FROM beacon_names ORDER BY id").fetchall()
        devices = conn.execute("SELECT id, name FROM devices ORDER BY id").fetchall()
        reports = conn.execute("SELECT id, beacon_name, created_at, summary, pdf_path FROM activity_reports ORDER BY id DESC LIMIT 200").fetchall()
    finally:
        conn.close()
    if request.method == "POST":
        report_kind = (request.form.get("report_kind") or "beacon").strip()
        start_date = (request.form.get("start_date") or "").strip() or None
        end_date = (request.form.get("end_date") or "").strip() or None

        if report_kind == "device":
            device_ident = (request.form.get("device") or "").strip()
            if not device_ident:
                return redirect(url_for("activity_reports_page"))
            from services.reporting_service import generate_device_activity_report
            pdf_path = generate_device_activity_report(device_ident, start_date=start_date, end_date=end_date)
        else:
            beacon_key = (request.form.get("beacon") or "").strip()
            if not beacon_key:
                return redirect(url_for("activity_reports_page"))
            pdf_path = generate_activity_report(beacon_key, start_date=start_date, end_date=end_date)

        if not pdf_path:
            return redirect(url_for("activity_reports_page"))
        return send_file(pdf_path, as_attachment=True)

    return render_template("activity_reports.html", beacons=beacons, devices=devices, reports=reports)



@app.route("/download/latest-report")
def download_latest_report():
    conn = get_db()
    try:
        row = conn.execute("SELECT pdf_path FROM daily_reports ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()

    if not row or not row[0] or not os.path.exists(row[0]):
        abort(404, description="No reports generated yet.")
    return send_file(row[0], as_attachment=True)


@app.route("/reports/generate-now")
def generate_report_now():
    pdf_path = generate_daily_report()
    return send_file(pdf_path, as_attachment=True)



@app.route("/uptime")
def uptime_page():
    conn = get_db()
    try:
        latest = conn.execute(
            "SELECT timestamp, device_count, beacon_count, status FROM uptime_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        logs = conn.execute(
            "SELECT timestamp, device_count, beacon_count, status FROM uptime_logs ORDER BY id DESC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()
    return render_template("uptime.html", latest=latest, logs=logs)


@app.route("/analytics")
def analytics_page():
    """Render the full analytics dashboard template (the old UI expects many vars)."""

    def _ph(conn):
        return "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"

    payload = {
        "window_hours": 24,
        "selected_beacon": "",
        "uptime_labels": [],
        "device_counts": [],
        "beacon_counts": [],
        "hourly_labels": [],
        "hourly_counts": [],
        "beacon_labels": [],
        "beacon_totals": [],
        "beacon_ins": [],
        "beacon_lefts": [],
        "total_events": 0,
        "most_active_beacon": "",
        "latest_timestamp": "",
        "latest_status": "",
        "latest_devices": 0,
        "latest_beacons": 0,
        "uptime_ok_percent": 0,
        "presence_by_beacon_json": "{}",
    }

    conn = get_db()
    try:
        ph = _ph(conn)

        # -------- UPTIME LOGS --------
        try:
            rows = conn.execute(
                "SELECT timestamp, device_count, beacon_count, COALESCE(status,'OK') AS status "
                "FROM uptime_logs ORDER BY id DESC LIMIT 500"
            ).fetchall()
        except Exception:
            rows = []

        rows = list(reversed(rows))
        ok = 0
        for r in rows:
            ts = r[0] if not isinstance(r, dict) else r.get("timestamp")
            dc = r[1] if not isinstance(r, dict) else r.get("device_count")
            bc = r[2] if not isinstance(r, dict) else r.get("beacon_count")
            st = r[3] if not isinstance(r, dict) else r.get("status")
            payload["uptime_labels"].append(str(ts))
            payload["device_counts"].append(int(dc or 0))
            payload["beacon_counts"].append(int(bc or 0))
            if (st or "OK").upper() == "OK":
                ok += 1
        if rows:
            last = rows[-1]
            payload["latest_timestamp"] = str(last[0] if not isinstance(last, dict) else last.get("timestamp"))
            payload["latest_devices"] = int((last[1] if not isinstance(last, dict) else last.get("device_count")) or 0)
            payload["latest_beacons"] = int((last[2] if not isinstance(last, dict) else last.get("beacon_count")) or 0)
            payload["latest_status"] = str(last[3] if not isinstance(last, dict) else last.get("status") or "OK")
            payload["uptime_ok_percent"] = int((ok / len(rows)) * 100) if rows else 0

        # -------- NOTIFICATIONS COUNTS --------
        try:
            rows = conn.execute(
                "SELECT beacon_name, type, COUNT(*) FROM notifications "
                "WHERE type IN ('in','left') GROUP BY beacon_name, type"
            ).fetchall()
        except Exception:
            rows = []

        by_beacon = {}
        for beacon, typ, cnt in rows:
            b = beacon or "Unknown"
            t = (typ or "").lower()
            by_beacon.setdefault(b, {"in": 0, "left": 0})
            if t in ("in", "left"):
                by_beacon[b][t] += int(cnt or 0)

        payload["total_events"] = sum(v["in"] + v["left"] for v in by_beacon.values())

        items = sorted(
            by_beacon.items(),
            key=lambda kv: kv[1]["in"] + kv[1]["left"],
            reverse=True,
        )[:25]

        payload["beacon_labels"] = [k for k, _ in items]
        payload["beacon_ins"] = [v["in"] for _, v in items]
        payload["beacon_lefts"] = [v["left"] for _, v in items]
        payload["beacon_totals"] = [v["in"] + v["left"] for _, v in items]
        if items:
            payload["most_active_beacon"] = items[0][0]
        payload["presence_by_beacon_json"] = json.dumps(by_beacon)

        # -------- HOURLY TREND (last 24h by created_at) --------
        try:
            if getattr(conn, "backend", "postgres") == "postgres":
                hrows = conn.execute(
                    "SELECT to_char(date_trunc('hour', created_at), 'YYYY-MM-DD HH24:00') AS hr, COUNT(*) "
                    "FROM notifications WHERE created_at >= NOW() - INTERVAL '24 hours' "
                    "AND type IN ('in','left') GROUP BY hr ORDER BY hr"
                ).fetchall()
            else:
                hrows = conn.execute(
                    "SELECT substr(created_at, 1, 13) || ':00' AS hr, COUNT(*) "
                    "FROM notifications WHERE type IN ('in','left') GROUP BY hr ORDER BY hr"
                ).fetchall()
        except Exception:
            hrows = []

        for hr, cnt in hrows:
            payload["hourly_labels"].append(str(hr))
            payload["hourly_counts"].append(int(cnt or 0))

    finally:
        try:
            conn.close()
        except Exception:
            pass

    return render_template("analytics.html", **payload)

@app.route("/healthz")
def healthz():
    return {"ok": True}

@app.route("/api/rename-beacon", methods=["POST"])
def api_rename_beacon():
    data = request.get_json(force=True)

    beacon_id = data.get("beacon_id") or data.get("id")
    name = data.get("name")

    if not beacon_id or not name:
        return jsonify({"error": "invalid payload"}), 400

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO beacon_names (id, name)
            VALUES (%s, %s)
            ON CONFLICT (id)
            DO UPDATE SET name = EXCLUDED.name
            """,
            (beacon_id, name),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"status": "ok"})

@app.route("/api/rename-device", methods=["POST"])
def api_rename_device():
    data = request.get_json(force=True)

    device_id = data.get("device_id") or data.get("id")
    name = data.get("name")
    color = data.get("color")

    if not device_id:
        return jsonify({"error": "invalid payload"}), 400

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO devices (id, name, color)
            VALUES (%s, %s, %s)
            ON CONFLICT (id)
            DO UPDATE SET
                name = EXCLUDED.name,
                color = EXCLUDED.color
            """,
            (device_id, name, color),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
