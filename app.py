import os
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, send_file, redirect, url_for, abort
import json


from database import init_db, get_db
from routes import map_bp, flespi_bp, admin_bp
from services.beacon_logic import format_samoa_time
from services.reporting_service import start_daily_thread, generate_daily_report, generate_activity_report
from services.evaluator_service import start_evaluator_thread
from services.auth_service import auth_bp, login_required, admin_required, is_admin, current_user, device_scope_clause, can_access_device, allowed_device_idents
from services.audit_log_service import log_event
from config import ACTIVITY_REPORTS_DIR, AUDIT_REPORTS_DIR, REPORTS_DIR

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-change-me")

init_db()

app.register_blueprint(auth_bp)
app.register_blueprint(map_bp)
app.register_blueprint(flespi_bp)
app.register_blueprint(admin_bp)

def _ensure_startup_dir(path: str, fallback: str) -> str:
    """Create report directories without crashing the whole web process."""
    target = os.path.abspath(path or fallback)
    try:
        os.makedirs(target, exist_ok=True)
        return target
    except PermissionError as e:
        fallback_target = os.path.abspath(fallback)
        print(f"[warn] cannot create {target}: {e}; using {fallback_target}")
        os.makedirs(fallback_target, exist_ok=True)
        return fallback_target


_ensure_startup_dir(REPORTS_DIR, "reports")
_ensure_startup_dir(ACTIVITY_REPORTS_DIR, "activity_reports")
_ensure_startup_dir(AUDIT_REPORTS_DIR, "audit_reports")

def _threads_enabled_for_runtime() -> bool:
    """Allow background workers in normal runtime but keep tests deterministic."""
    if os.getenv("DISABLE_BACKGROUND_THREADS", "0") == "1":
        return False
    if os.getenv("TESTING", "0") == "1":
        return False
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    return True


if _threads_enabled_for_runtime() and os.getenv("DISABLE_DAILY_THREAD", "0") != "1":
    start_daily_thread()

if _threads_enabled_for_runtime() and os.getenv("DISABLE_EVALUATOR_THREAD", "0") != "1":
    start_evaluator_thread()


def samoa_iso_now():
    return format_samoa_time(time.time()).replace(" ", "T")


@app.route("/api/notifications", methods=["POST"])
@login_required
def save_notification():
    data = request.get_json(silent=True) or {}
    ntype = (data.get("type") or "").strip().lower()
    name = (data.get("name") or "").strip()
    event_time = (data.get("time") or "").strip()
    beacon_id = (data.get("beacon_id") or data.get("beacon") or "").strip() or None
    device_ident = (data.get("device_ident") or data.get("device") or "").strip() or None
    distance = data.get("distance")

    if not ntype or not name:
        return jsonify({"error": "invalid"}), 400

    conn = get_db()
    try:
        if device_ident and not can_access_device(device_ident, conn=conn):
            return jsonify({"error": "forbidden"}), 403
        ph = "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"
        conn.execute(
            f"INSERT INTO notifications (type, beacon_name, beacon_id, event_time, created_at, device_ident, distance) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})",
            (ntype, name, beacon_id, event_time, samoa_iso_now(), device_ident, distance),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"status": "ok"}), 201



@app.route("/api/notifications/recent")
@login_required
def recent_notifications():
    """Return notifications newer than since_id (for live popups / panel)."""
    since_id = request.args.get("since_id", "0")
    try:
        since_id_int = int(since_id)
    except Exception:
        since_id_int = 0

    conn = get_db()
    try:
        cur = conn.cursor()
        ph = "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"
        scope_sql, scope_params = device_scope_clause(conn, "n.device_ident")
        customer_id = (current_user() or {}).get("customer_id")
        cur.execute(
            """SELECT n.id,
                      n.type,
                      COALESCE(cbn.name, bn.name, n.beacon_name, n.beacon_id) AS beacon_display_name,
                      n.beacon_id,
                      n.event_time,
                      n.device_ident,
                      n.distance,
                      n.created_at
                 FROM notifications n
            LEFT JOIN beacon_names bn
                   ON n.beacon_id = bn.id
            LEFT JOIN customer_beacon_names cbn
                   ON n.beacon_id = cbn.beacon_id AND cbn.customer_id = {customer_ph}
                WHERE n.id > {ph}
                  AND {scope_sql}
                ORDER BY n.id ASC
                LIMIT 200""".format(customer_ph=ph, ph=ph, scope_sql=scope_sql),
            (customer_id, since_id_int) + scope_params,
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    items = []
    for r in rows:
        items.append({
            "id": r[0],
            "type": r[1],
            "beacon_name": r[2],
            "beacon_id": r[3],
            "event_time": r[4],
            "device_ident": r[5],
            "distance": r[6],
            "created_at": r[7],
        })
    return jsonify({"items": items})

@app.route("/reports/history")
@admin_required
def reports_history():
    conn = get_db()
    try:
        reports = conn.execute("SELECT id, created_at, summary, pdf_path FROM daily_reports ORDER BY id DESC LIMIT 200").fetchall()
    finally:
        conn.close()
    return render_template("reports_history.html", reports=reports)


@app.route("/download_daily_report/<int:report_id>")
@admin_required
def download_daily_report(report_id: int):
    conn = get_db()
    try:
        ph = "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"
        row = conn.execute(f"SELECT pdf_path FROM daily_reports WHERE id={ph}", (report_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row[0] or not os.path.exists(row[0]):
        abort(404)
    return send_file(row[0], as_attachment=True)


@app.route("/notifications/history")
@login_required
def notifications_history():
    conn = get_db()
    try:
        ph = "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"
        scope_sql, scope_params = device_scope_clause(conn, "n.device_ident")
        notifications = conn.execute(
            """SELECT n.created_at,
                      n.type,
                      COALESCE(cbn.name, bn.name, n.beacon_name, n.beacon_id) AS beacon_display_name,
                      n.event_time,
                      n.distance,
                      n.device_ident
                 FROM notifications n
            LEFT JOIN beacon_names bn
                   ON n.beacon_id = bn.id
            LEFT JOIN customer_beacon_names cbn
                   ON n.beacon_id = cbn.beacon_id AND cbn.customer_id = {customer_ph}
                WHERE {scope_sql}
                ORDER BY n.id DESC
                LIMIT 500""".format(customer_ph=ph, scope_sql=scope_sql),
            ((current_user() or {}).get("customer_id"),) + scope_params,
        ).fetchall()
    finally:
        conn.close()
    return render_template("notifications_history.html", notifications=notifications)


@app.route("/download_activity_report/<int:report_id>")
@login_required
def download_activity_report(report_id: int):
    conn = get_db()
    try:
        ph = "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"
        if is_admin():
            row = conn.execute(f"SELECT pdf_path FROM activity_reports WHERE id={ph}", (report_id,)).fetchone()
        else:
            row = conn.execute(
                f"SELECT pdf_path FROM activity_reports WHERE id={ph} AND customer_id={ph}",
                (report_id, (current_user() or {}).get("customer_id")),
            ).fetchone()
    finally:
        conn.close()
    if not row or not row[0] or not os.path.exists(row[0]):
        abort(404)
    return send_file(row[0], as_attachment=True)


@app.route("/activity-reports", methods=["GET","POST"])
@login_required
def activity_reports_page():
    beacons = []
    devices = []
    reports = []

    conn = get_db()
    try:
        user = current_user()
        ph = "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"
        scope_sql, scope_params = device_scope_clause(conn, "bs.device_ident", user=user)
        beacons_rows = conn.execute(
            "SELECT DISTINCT bs.beacon_id, COALESCE(cbn.name, bn.name, bs.beacon_id) AS name "
            "FROM beacon_states bs "
            "LEFT JOIN beacon_names bn ON bn.id = bs.beacon_id "
            f"LEFT JOIN customer_beacon_names cbn ON cbn.beacon_id = bs.beacon_id AND cbn.customer_id = {ph} "
            f"WHERE bs.beacon_id IS NOT NULL AND {scope_sql} ORDER BY bs.beacon_id",
            (user.get("customer_id"),) + scope_params,
        ).fetchall()
        dscope_sql, dscope_params = device_scope_clause(conn, "ds.device_ident", user=user)
        devices_rows = conn.execute(
            "SELECT ds.device_ident, COALESCE(cds.name, cd.label, d.name, ds.device_ident) AS name, COALESCE(cds.color, d.color) AS color "
            "FROM device_states ds "
            "LEFT JOIN devices d ON d.id = ds.device_ident "
            f"LEFT JOIN customer_devices cd ON cd.device_ident = ds.device_ident AND cd.customer_id = {ph} "
            f"LEFT JOIN customer_device_settings cds ON cds.device_ident = ds.device_ident AND cds.customer_id = {ph} "
            f"WHERE {dscope_sql} ORDER BY ds.device_ident",
            (user.get("customer_id"), user.get("customer_id")) + dscope_params,
        ).fetchall()
        if is_admin(user):
            reports = conn.execute("SELECT id, beacon_name, created_at, summary, pdf_path FROM activity_reports ORDER BY id DESC LIMIT 200").fetchall()
        else:
            reports = conn.execute(
                f"SELECT id, beacon_name, created_at, summary, pdf_path FROM activity_reports WHERE customer_id={ph} ORDER BY id DESC LIMIT 200",
                (user.get("customer_id"),),
            ).fetchall()

        # Templates expect {ident, label} dictionaries
        for r in beacons_rows:
            bid = r[0] if not isinstance(r, dict) else r.get("id")
            bname = r[1] if not isinstance(r, dict) else r.get("name")
            if bid:
                beacons.append({"ident": str(bid), "label": (bname or str(bid))})

        for r in devices_rows:
            did = r[0] if not isinstance(r, dict) else r.get("id")
            dname = r[1] if not isinstance(r, dict) else r.get("name")
            if did:
                devices.append({"ident": str(did), "label": (dname or str(did))})
    except Exception as e:
        print(f"[warn] activity-reports load failed: {e}")
    finally:
        conn.close()
    if request.method == "POST":
        report_kind = (request.form.get("report_kind") or "beacon").strip()
        start_date = (request.form.get("start_date") or "").strip() or None
        end_date = (request.form.get("end_date") or "").strip() or None

        if report_kind == "device":
            device_ident = (request.form.get("device") or request.form.get("device_ident") or "").strip()
            if not device_ident or not can_access_device(device_ident):
                return redirect(url_for("activity_reports_page"))
            from services.reporting_service import generate_device_activity_report
            pdf_path = generate_device_activity_report(
                device_ident,
                start_date=start_date,
                end_date=end_date,
                customer_id=None if is_admin() else (current_user() or {}).get("customer_id"),
            )
        else:
            beacon_key = (request.form.get("beacon") or request.form.get("beacon_id") or "").strip()
            if not beacon_key:
                return redirect(url_for("activity_reports_page"))
            allowed_devices = None if is_admin() else allowed_device_idents()
            pdf_path = generate_activity_report(
                beacon_key,
                start_date=start_date,
                end_date=end_date,
                device_idents=allowed_devices,
                customer_id=None if is_admin() else (current_user() or {}).get("customer_id"),
            )

        if not pdf_path:
            return redirect(url_for("activity_reports_page"))
        return send_file(pdf_path, as_attachment=True)

    return render_template("activity_reports.html", beacons=beacons, devices=devices, reports=reports)



@app.route("/download/latest-report")
@admin_required
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
@admin_required
def generate_report_now():
    pdf_path = generate_daily_report()
    return send_file(pdf_path, as_attachment=True)


@app.route("/audit-reports")
@login_required
def audit_reports_page():
    conn = get_db()
    try:
        ph = "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"
        if is_admin():
            reports = conn.execute(
                "SELECT ar.id, c.name, ar.scheduled_for, ar.scan_window_start, ar.scan_window_end, ar.status, ar.emailed_at "
                "FROM audit_runs ar JOIN customers c ON c.id = ar.customer_id ORDER BY ar.id DESC LIMIT 200"
            ).fetchall()
        else:
            reports = conn.execute(
                f"SELECT ar.id, c.name, ar.scheduled_for, ar.scan_window_start, ar.scan_window_end, ar.status, ar.emailed_at "
                f"FROM audit_runs ar JOIN customers c ON c.id = ar.customer_id "
                f"WHERE ar.customer_id = {ph} ORDER BY ar.id DESC LIMIT 200",
                ((current_user() or {}).get("customer_id"),),
            ).fetchall()
    finally:
        conn.close()
    return render_template("audit_reports.html", reports=reports)


@app.route("/download_audit_report/<int:audit_id>")
@login_required
def download_audit_report(audit_id: int):
    conn = get_db()
    try:
        ph = "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"
        if is_admin():
            row = conn.execute(f"SELECT pdf_path FROM audit_runs WHERE id={ph}", (audit_id,)).fetchone()
        else:
            row = conn.execute(
                f"SELECT pdf_path FROM audit_runs WHERE id={ph} AND customer_id={ph}",
                (audit_id, (current_user() or {}).get("customer_id")),
            ).fetchone()
    finally:
        conn.close()
    if not row or not row[0] or not os.path.exists(row[0]):
        abort(404)
    return send_file(row[0], as_attachment=True)



@app.route("/uptime")
@admin_required
def uptime_page():
    conn = get_db()
    try:
        latest = conn.execute(
            "SELECT id, timestamp, device_count, beacon_count, status FROM uptime_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        logs = conn.execute(
            "SELECT id, timestamp, device_count, beacon_count, status FROM uptime_logs ORDER BY id DESC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()
    return render_template("uptime.html", latest=latest, logs=logs)


@app.route("/analytics")
@login_required
def analytics_page():
    """Render the full analytics dashboard template (the old UI expects many vars)."""

    def _ph(conn):
        return "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"

    def _parse_time(ts_raw):
        if not ts_raw:
            return None
        ts = str(ts_raw).replace("T", " ").replace(" UTC", "")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(ts, fmt)
            except Exception:
                continue
        return None

    payload = {
        "window_hours": 0,
        "window_label": "",
        "window_start": "",
        "selected_beacon": "",
        "uptime_labels": [],
        "device_counts": [],
        "beacon_counts": [],
        "event_labels": [],
        "event_in": [],
        "event_left": [],
        "event_still_in": [],
        "event_still_out": [],
        "status_labels": [],
        "status_counts": [],
        "total_events": 0,
        "latest_timestamp": "",
        "latest_status": "",
        "latest_devices": 0,
        "latest_beacons": 0,
        "uptime_ok_percent": 0,
        "presence_by_beacon_json": "{}",
        "presence_summary": [],
        "status_breakdown": [],
        "top_beacons": [],
        "total_beacons": 0,
        "beacons_in": 0,
        "beacons_out": 0,
        "beacons_missing": 0,
        "beacons_unknown": 0,
        "devices_online": 0,
        "devices_total": 0,
    }

    conn = get_db()
    try:
        ph = _ph(conn)
        user = current_user()
        device_scope_sql, device_scope_params = device_scope_clause(conn, "device_ident", user=user)
        bs_scope_sql, bs_scope_params = device_scope_clause(conn, "device_ident", user=user)
        notif_plain_scope_sql, notif_plain_scope_params = device_scope_clause(conn, "device_ident", user=user)
        notif_scope_sql, notif_scope_params = device_scope_clause(conn, "n.device_ident", user=user)
        now_ts = time.time()
        # Analytics window resets daily at 12:00 AM (midnight) Samoa time.
        now_local_dt = datetime.utcfromtimestamp(now_ts) + timedelta(hours=13)
        midnight_local_dt = now_local_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        if now_local_dt < midnight_local_dt:
            midnight_local_dt = midnight_local_dt - timedelta(days=1)
        window_start = midnight_local_dt.strftime("%Y-%m-%d %H:%M:%S")
        payload["window_start"] = window_start
        payload["window_hours"] = max(int((now_local_dt - midnight_local_dt).total_seconds() // 3600), 0)
        payload["window_label"] = f"since {midnight_local_dt.strftime('%Y-%m-%d 12:00 AM')}"

        # -------- UPTIME LOGS --------
        try:
            if getattr(conn, "backend", "postgres") == "postgres":
                rows = conn.execute(
                    "SELECT timestamp, device_count, beacon_count, COALESCE(status,'OK') AS status "
                    "FROM uptime_logs WHERE timestamp::timestamp >= %s "
                    "ORDER BY id DESC LIMIT 500",
                    (window_start,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT timestamp, device_count, beacon_count, COALESCE(status,'OK') AS status "
                    "FROM uptime_logs WHERE timestamp >= ? "
                    "ORDER BY id DESC LIMIT 500",
                    (window_start,),
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

        # -------- DEVICE / BEACON LIVE COUNTS --------
        try:
            payload["devices_online"] = int(conn.execute(
                f"SELECT COUNT(*) FROM device_states WHERE online = {ph} AND {device_scope_sql}",
                (1,) + device_scope_params,
            ).fetchone()[0])
            payload["devices_total"] = int(conn.execute(
                f"SELECT COUNT(*) FROM device_states WHERE {device_scope_sql}",
                device_scope_params,
            ).fetchone()[0])
        except Exception:
            payload["devices_online"] = 0
            payload["devices_total"] = 0

        try:
            if getattr(conn, "backend", "postgres") == "postgres":
                srows = conn.execute(
                    f"SELECT state, missing, COUNT(*) FROM beacon_states WHERE {bs_scope_sql} GROUP BY state, missing",
                    bs_scope_params,
                ).fetchall()
            else:
                srows = conn.execute(
                    f"SELECT state, missing, COUNT(*) FROM beacon_states WHERE {bs_scope_sql} GROUP BY state, missing",
                    bs_scope_params,
                ).fetchall()
        except Exception:
            srows = []

        for state, missing, cnt in srows:
            payload["total_beacons"] += int(cnt or 0)
            if missing:
                payload["beacons_missing"] += int(cnt or 0)
                continue
            if state == "in":
                payload["beacons_in"] += int(cnt or 0)
            elif state == "out":
                payload["beacons_out"] += int(cnt or 0)
            else:
                payload["beacons_unknown"] += int(cnt or 0)

        status_labels = ["In range", "Out of range", "Missing", "Unknown"]
        status_counts = [
            payload["beacons_in"],
            payload["beacons_out"],
            payload["beacons_missing"],
            payload["beacons_unknown"],
        ]
        payload["status_labels"] = status_labels
        payload["status_counts"] = status_counts

        # -------- EVENT VOLUME (last window) --------
        event_types = ("in", "left", "still_in", "still_out")
        events_by_hour = {}
        try:
            if getattr(conn, "backend", "postgres") == "postgres":
                erows = conn.execute(
                    "SELECT to_char(date_trunc('hour', created_at::timestamp), 'YYYY-MM-DD HH24:00') AS hr, lower(type) AS event_type, COUNT(*) "
                    "FROM notifications WHERE created_at::timestamp >= %s "
                    "AND lower(type) IN ('in','left','still_in','still_out') "
                    f"AND {notif_plain_scope_sql} "
                    "GROUP BY hr, event_type ORDER BY hr",
                    (window_start,) + notif_plain_scope_params,
                ).fetchall()
            else:
                erows = conn.execute(
                    "SELECT substr(created_at, 1, 13) || ':00' AS hr, lower(type) AS event_type, COUNT(*) "
                    "FROM notifications WHERE created_at >= ? "
                    "AND lower(type) IN ('in','left','still_in','still_out') "
                    f"AND {notif_plain_scope_sql} "
                    "GROUP BY hr, event_type ORDER BY hr",
                    (window_start,) + notif_plain_scope_params,
                ).fetchall()
        except Exception:
            erows = []

        for hr, typ, cnt in erows:
            events_by_hour.setdefault(str(hr), {t: 0 for t in event_types})
            events_by_hour[str(hr)][str(typ)] = int(cnt or 0)

        labels = sorted(events_by_hour.keys())
        payload["event_labels"] = labels
        payload["event_in"] = [events_by_hour[l]["in"] for l in labels]
        payload["event_left"] = [events_by_hour[l]["left"] for l in labels]
        payload["event_still_in"] = [events_by_hour[l]["still_in"] for l in labels]
        payload["event_still_out"] = [events_by_hour[l]["still_out"] for l in labels]

        # -------- TOP BEACONS --------
        try:
            if getattr(conn, "backend", "postgres") == "postgres":
                trows = conn.execute(
                    "SELECT COALESCE(bn.name, n.beacon_name, n.beacon_id, 'Unknown') AS name, lower(n.type) AS event_type, COUNT(*) "
                    "FROM notifications n "
                    "LEFT JOIN beacon_names bn ON bn.id = n.beacon_id "
                    "WHERE n.created_at::timestamp >= %s "
                    "AND lower(n.type) IN ('in','left','still_in','still_out') "
                    f"AND {notif_scope_sql} "
                    "GROUP BY name, event_type",
                    (window_start,) + notif_scope_params,
                ).fetchall()
            else:
                trows = conn.execute(
                    "SELECT COALESCE(bn.name, n.beacon_name, n.beacon_id, 'Unknown') AS name, lower(n.type) AS event_type, COUNT(*) "
                    "FROM notifications n "
                    "LEFT JOIN beacon_names bn ON bn.id = n.beacon_id "
                    "WHERE n.created_at >= ? "
                    "AND lower(n.type) IN ('in','left','still_in','still_out') "
                    f"AND {notif_scope_sql} "
                    "GROUP BY name, event_type",
                    (window_start,) + notif_scope_params,
                ).fetchall()
        except Exception:
            trows = []

        by_beacon = {}
        for name, typ, cnt in trows:
            key = name or "Unknown"
            entry = by_beacon.setdefault(key, {"in": 0, "left": 0, "still_in": 0, "still_out": 0})
            entry[str(typ)] = int(cnt or 0)

        top_items = sorted(
            by_beacon.items(),
            key=lambda kv: kv[1]["in"] + kv[1]["left"] + kv[1]["still_in"] + kv[1]["still_out"],
            reverse=True,
        )[:12]
        payload["top_beacons"] = [
            {
                "name": name,
                "in": data["in"],
                "left": data["left"],
                "still_in": data["still_in"],
                "still_out": data["still_out"],
                "total": data["in"] + data["left"] + data["still_in"] + data["still_out"],
            }
            for name, data in top_items
        ]

        payload["total_events"] = sum(
            d["in"] + d["left"] + d["still_in"] + d["still_out"] for d in by_beacon.values()
        )

        # -------- STATUS BREAKDOWN (uptime) --------
        status_counts = {}
        for r in rows:
            st = (r[3] if not isinstance(r, dict) else r.get("status")) or "OK"
            status_counts[st] = status_counts.get(st, 0) + 1
        total_status = sum(status_counts.values()) or 1
        payload["status_breakdown"] = [
            {
                "status": k,
                "count": v,
                "percent": int((v / total_status) * 100),
            }
            for k, v in sorted(status_counts.items(), key=lambda kv: kv[1], reverse=True)
        ]

        # -------- PRESENCE SUMMARY --------
        presence = {}
        try:
            if getattr(conn, "backend", "postgres") == "postgres":
                prow = conn.execute(
                    "SELECT COALESCE(bn.name, n.beacon_name, n.beacon_id, 'Unknown') AS name, lower(n.type) AS event_type, n.event_time "
                    "FROM notifications n "
                    "LEFT JOIN beacon_names bn ON bn.id = n.beacon_id "
                    "WHERE n.created_at::timestamp >= %s "
                    f"AND lower(n.type) IN ('in','left','still_in','still_out') AND {notif_scope_sql} ORDER BY n.event_time",
                    (window_start,) + notif_scope_params,
                ).fetchall()
            else:
                prow = conn.execute(
                    "SELECT COALESCE(bn.name, n.beacon_name, n.beacon_id, 'Unknown') AS name, lower(n.type) AS event_type, n.event_time "
                    "FROM notifications n "
                    "LEFT JOIN beacon_names bn ON bn.id = n.beacon_id "
                    "WHERE n.created_at >= ? "
                    f"AND lower(n.type) IN ('in','left','still_in','still_out') AND {notif_scope_sql} ORDER BY n.event_time",
                    (window_start,) + notif_scope_params,
                ).fetchall()
        except Exception:
            prow = []

        window_start_dt = _parse_time(window_start)
        window_end_dt = _parse_time(format_samoa_time(now_ts))

        for name, typ, event_time in prow:
            tstamp = _parse_time(event_time)
            if not tstamp:
                continue
            bucket = presence.setdefault(name or "Unknown", {"events": []})
            bucket["events"].append((tstamp, str(typ)))

        presence_summary = []
        presence_by_beacon = {}
        for name, bucket in presence.items():
            events = sorted(bucket["events"], key=lambda e: e[0])
            last_state = None
            last_time = window_start_dt
            in_seconds = 0
            out_seconds = 0

            if events and window_start_dt and events[0][0] > window_start_dt:
                first_type = events[0][1]
                # Infer the state at window start so percentages represent the full window.
                # Example: first event "in" means beacon was out before entering.
                last_state = "out" if first_type == "in" else "in"

            for ts, typ in events:
                state = "in" if typ in ("in", "still_in") else "out"
                if last_state and last_time:
                    delta = (ts - last_time).total_seconds()
                    if last_state == "in":
                        in_seconds += max(delta, 0)
                    else:
                        out_seconds += max(delta, 0)
                last_state = state
                last_time = ts

            if last_state and last_time and window_end_dt:
                delta = (window_end_dt - last_time).total_seconds()
                if last_state == "in":
                    in_seconds += max(delta, 0)
                else:
                    out_seconds += max(delta, 0)

            total = in_seconds + out_seconds
            in_percent = int((in_seconds / total) * 100) if total else 0
            out_percent = 100 - in_percent if total else 0

            item = {
                "name": name,
                "in_percent": in_percent,
                "out_percent": out_percent,
                "in_hours": f"{in_seconds / 3600:.1f}" if total else "0.0",
                "out_hours": f"{out_seconds / 3600:.1f}" if total else "0.0",
                "event_count": len(events),
                "last_state": last_state,
                "last_event_time": events[-1][0].strftime("%Y-%m-%d %H:%M:%S") if events else "",
            }
            presence_summary.append(item)
            presence_by_beacon[name] = item

        presence_summary.sort(key=lambda i: i["event_count"], reverse=True)
        payload["presence_summary"] = presence_summary
        payload["presence_by_beacon_json"] = json.dumps(presence_by_beacon)

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
@login_required
def api_rename_beacon():
    data = request.get_json(force=True)

    beacon_id = data.get("beacon_id") or data.get("id")
    name = data.get("name") or data.get("new_name") or data.get("newName") or data.get("new_name") or data.get("newName")

    if not beacon_id or not name:
        return jsonify({"error": "invalid payload"}), 400

    conn = get_db()
    try:
        ph = "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"
        user = current_user()
        if is_admin(user):
            conn.execute(
                f"INSERT INTO beacon_names (id, name) VALUES ({ph}, {ph}) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
                (beacon_id, name),
            )
            log_customer_id = None
        else:
            log_customer_id = user.get("customer_id")
            conn.execute(
                f"INSERT INTO customer_beacon_names (customer_id, beacon_id, name, updated_at) "
                f"VALUES ({ph},{ph},{ph},{ph}) "
                "ON CONFLICT(customer_id, beacon_id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
                (log_customer_id, beacon_id, name, samoa_iso_now()),
            )
        log_event(
            "rename.beacon",
            target_type="beacon",
            target_id=beacon_id,
            details=f"Renamed beacon {beacon_id} to {name or '(blank)'}",
            actor_user=user,
            customer_id=log_customer_id,
            conn=conn,
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"status": "ok"})

@app.route("/api/rename-device", methods=["POST"])
@login_required
def api_rename_device():
    data = request.get_json(force=True)

    device_id = data.get("device_id") or data.get("id")
    name = data.get("name") or data.get("new_name") or data.get("newName")
    color = data.get("color")

    if not device_id:
        return jsonify({"error": "invalid payload"}), 400

    conn = get_db()
    try:
        ph = "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"
        user = current_user()
        if not can_access_device(device_id, conn=conn, user=user):
            return jsonify({"error": "forbidden"}), 403
        if is_admin(user):
            conn.execute(
                f"INSERT INTO devices (id, name, color) VALUES ({ph}, {ph}, {ph}) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, color = EXCLUDED.color",
                (device_id, name, color),
            )
            log_customer_id = None
        else:
            log_customer_id = user.get("customer_id")
            conn.execute(
                f"INSERT INTO customer_device_settings (customer_id, device_ident, name, color, updated_at) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph}) "
                "ON CONFLICT(customer_id, device_ident) DO UPDATE SET "
                "name=excluded.name, color=excluded.color, updated_at=excluded.updated_at",
                (log_customer_id, device_id, name, color, samoa_iso_now()),
            )
        log_event(
            "rename.device",
            target_type="device",
            target_id=device_id,
            details=f"Renamed device {device_id} to {name or '(blank)'}",
            actor_user=user,
            customer_id=log_customer_id,
            conn=conn,
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
