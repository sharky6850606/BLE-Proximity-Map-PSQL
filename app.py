import os
import time
from flask import Flask, request, jsonify, render_template, send_file, redirect, url_for, abort

from database import init_db, get_db
from routes import map_bp, flespi_bp
from services.beacon_logic import format_samoa_time
from services.reporting_service import start_daily_thread, generate_daily_report, generate_activity_report

app = Flask(__name__)

init_db()

app.register_blueprint(map_bp)
app.register_blueprint(flespi_bp)

os.makedirs(os.path.abspath(os.getenv("REPORTS_DIR", "reports")), exist_ok=True)
os.makedirs(os.path.abspath(os.getenv("ACTIVITY_REPORTS_DIR", "activity_reports")), exist_ok=True)

if os.getenv("DISABLE_DAILY_THREAD", "0") != "1":
    start_daily_thread()


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
    # Very simple analytics (counts from notifications)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT beacon_name, type, COUNT(*) FROM notifications WHERE type IN ('in','left') GROUP BY beacon_name, type"
        ).fetchall()
    finally:
        conn.close()

    total = sum(r[2] for r in rows) or 1
    status_breakdown = []
    for beacon, typ, cnt in rows:
        status_breakdown.append({
            "beacon": beacon,
            "status": typ.upper(),
            "count": cnt,
            "percent": round(cnt * 100.0 / total, 1),
        })

    # presence_summary: top 10 beacons by IN events
    in_counts = {}
    for beacon, typ, cnt in rows:
        if (typ or "").lower() == "in":
            in_counts[beacon] = cnt
    top = sorted(in_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    presence_summary = [{"name": b, "value": c} for b, c in top]

    return render_template("analytics.html", status_breakdown=status_breakdown, presence_summary=presence_summary)

@app.route("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
