import os
import threading
import time
from datetime import datetime, timedelta

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from config import (
    AUDIT_EMAIL_HOUR,
    AUDIT_EMAIL_MINUTE,
    AUDIT_HOUR,
    AUDIT_MINUTE,
    AUDIT_REPORTS_DIR,
    AUDIT_WINDOW_AFTER_MIN,
    AUDIT_WINDOW_BEFORE_MIN,
    SAMOA_OFFSET_HOURS,
)
from database import get_db
from services.beacon_logic import format_samoa_time
from services.email_service import send_report_email
from services.notifications_service import emit_notification
from services.reporting_service import _paragraph_text


def _ph(conn):
    return "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"


def _ensure_dir(path):
    ab = os.path.abspath(path)
    os.makedirs(ab, exist_ok=True)
    return ab


def _local_dt_from_unix(ts=None):
    ts = time.time() if ts is None else ts
    return datetime.utcfromtimestamp(float(ts)) + timedelta(hours=SAMOA_OFFSET_HOURS)


def _unix_from_local_dt(local_dt):
    utc_dt = local_dt - timedelta(hours=SAMOA_OFFSET_HOURS)
    return int(utc_dt.timestamp())


def _audit_schedule_for_day(local_dt=None):
    local_dt = local_dt or _local_dt_from_unix()
    scheduled = local_dt.replace(hour=AUDIT_HOUR, minute=AUDIT_MINUTE, second=0, microsecond=0)
    start = scheduled - timedelta(minutes=AUDIT_WINDOW_BEFORE_MIN)
    end = scheduled + timedelta(minutes=AUDIT_WINDOW_AFTER_MIN)
    return scheduled, start, end


def _audit_email_time_for_day(local_dt=None):
    local_dt = local_dt or _local_dt_from_unix()
    return local_dt.replace(hour=AUDIT_EMAIL_HOUR, minute=AUDIT_EMAIL_MINUTE, second=0, microsecond=0)


def _fmt_local_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_ts(ts):
    return format_samoa_time(int(ts)) if ts else ""


def _get_customer_devices(conn, customer_id):
    ph = _ph(conn)
    rows = conn.execute(
        f"SELECT device_ident FROM customer_devices WHERE customer_id = {ph}",
        (customer_id,),
    ).fetchall()
    return [str(r[0]) for r in rows if r and r[0]]


def _latest_observation(conn, beacon_id, device_idents, start_ts, end_ts):
    if not device_idents:
        return None
    ph = _ph(conn)
    placeholders = ",".join([ph] * len(device_idents))
    return conn.execute(
        f"SELECT observed_ts, device_ident, distance, rssi "
        f"FROM beacon_observations "
        f"WHERE beacon_id = {ph} AND device_ident IN ({placeholders}) "
        f"AND observed_ts >= {ph} AND observed_ts <= {ph} "
        "ORDER BY observed_ts DESC LIMIT 1",
        (beacon_id,) + tuple(device_idents) + (start_ts, end_ts),
    ).fetchone()


def _insert_audit_run(conn, customer_id, scheduled_for, window_start, window_end):
    ph = _ph(conn)
    conn.execute(
        f"INSERT INTO audit_runs (customer_id, scheduled_for, scan_window_start, scan_window_end, status, created_at) "
        f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph}) "
        "ON CONFLICT(customer_id, scheduled_for) DO NOTHING",
        (customer_id, scheduled_for, window_start, window_end, "running", format_samoa_time(time.time())),
    )
    row = conn.execute(
        f"SELECT id FROM audit_runs WHERE customer_id = {ph} AND scheduled_for = {ph}",
        (customer_id, scheduled_for),
    ).fetchone()
    return row[0] if row else None


def _write_audit_pdf(pdf_path, customer_name, scheduled_for, window_start, window_end, results):
    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=landscape(A4),
        rightMargin=24,
        leftMargin=24,
        topMargin=24,
        bottomMargin=24,
    )
    styles = getSampleStyleSheet()
    present_count = sum(1 for r in results if r["status"] == "present")
    missing_count = sum(1 for r in results if r["status"] == "missing")

    story = [
        Paragraph("Daily Equipment Audit", styles["Title"]),
        Paragraph(f"Customer: {_paragraph_text(customer_name)}", styles["Normal"]),
        Paragraph(f"Scheduled: {_paragraph_text(scheduled_for)} Samoa time", styles["Normal"]),
        Paragraph(f"Scan window: {_paragraph_text(window_start)} to {_paragraph_text(window_end)}", styles["Normal"]),
        Paragraph(f"Summary: {present_count} present, {missing_count} missing, {len(results)} total assets", styles["Normal"]),
        Spacer(1, 12),
    ]

    table_data = [[
        Paragraph("Asset", styles["BodyText"]),
        Paragraph("Beacon ID", styles["BodyText"]),
        Paragraph("Status", styles["BodyText"]),
        Paragraph("Last seen", styles["BodyText"]),
        Paragraph("Device", styles["BodyText"]),
        Paragraph("RSSI", styles["BodyText"]),
        Paragraph("Distance", styles["BodyText"]),
        Paragraph("Missing since", styles["BodyText"]),
    ]]
    for r in results:
        table_data.append([
            Paragraph(_paragraph_text(r["asset_name"]), styles["BodyText"]),
            Paragraph(_paragraph_text(r["beacon_id"]), styles["BodyText"]),
            Paragraph(_paragraph_text("In range" if r["status"] == "present" else "Missing"), styles["BodyText"]),
            Paragraph(_paragraph_text(r.get("last_seen") or ""), styles["BodyText"]),
            Paragraph(_paragraph_text(r.get("device_ident") or ""), styles["BodyText"]),
            Paragraph(_paragraph_text(r.get("rssi") if r.get("rssi") is not None else ""), styles["BodyText"]),
            Paragraph(_paragraph_text(r.get("distance_label") or ""), styles["BodyText"]),
            Paragraph(_paragraph_text(r.get("missing_since") or ""), styles["BodyText"]),
        ])

    table = Table(table_data, colWidths=[120, 190, 70, 120, 115, 50, 70, 120], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
    ]))
    story.append(table)
    doc.build(story)


def run_customer_audit(customer_id, scheduled_local_dt=None):
    scheduled, window_start, window_end = _audit_schedule_for_day(scheduled_local_dt)
    scheduled_for = _fmt_local_dt(scheduled)
    window_start_label = _fmt_local_dt(window_start)
    window_end_label = _fmt_local_dt(window_end)
    start_ts = _unix_from_local_dt(window_start)
    end_ts = _unix_from_local_dt(window_end)
    now_label = format_samoa_time(time.time())

    conn = get_db()
    try:
        ph = _ph(conn)
        customer = conn.execute(
            f"SELECT id, name FROM customers WHERE id = {ph} AND active = 1",
            (customer_id,),
        ).fetchone()
        if not customer:
            return None

        audit_run_id = _insert_audit_run(conn, customer_id, scheduled_for, window_start_label, window_end_label)
        if not audit_run_id:
            return None
        conn.execute(f"DELETE FROM audit_results WHERE audit_run_id = {ph}", (audit_run_id,))

        customer_devices = _get_customer_devices(conn, customer_id)
        assets = conn.execute(
            f"SELECT ca.id, ca.beacon_id, COALESCE(ca.name, cbn.name, ca.beacon_id), "
            f"ca.expected_device_ident, ca.status, ca.missing_since "
            f"FROM customer_assets ca "
            f"LEFT JOIN customer_beacon_names cbn ON cbn.customer_id = ca.customer_id AND cbn.beacon_id = ca.beacon_id "
            f"WHERE ca.customer_id = {ph} AND ca.active = 1 ORDER BY COALESCE(ca.name, cbn.name, ca.beacon_id)",
            (customer_id,),
        ).fetchall()

        results = []
        for asset_id, beacon_id, asset_name, expected_device_ident, previous_status, previous_missing_since in assets:
            device_scope = [str(expected_device_ident)] if expected_device_ident else customer_devices
            obs = _latest_observation(conn, beacon_id, device_scope, start_ts, end_ts)
            status = "present" if obs else "missing"
            last_seen_ts = obs[0] if obs else None
            last_seen_device = obs[1] if obs else None
            distance = obs[2] if obs else None
            rssi = obs[3] if obs else None

            missing_since = previous_missing_since
            if status == "missing":
                if previous_status != "missing":
                    missing_since = now_label
                    emit_notification(
                        "missing",
                        beacon_id,
                        event_time=now_label,
                        device_ident=expected_device_ident or (customer_devices[0] if customer_devices else None),
                        distance=None,
                    )
                conn.execute(
                    f"UPDATE customer_assets SET status={ph}, missing_since={ph}, found_at=NULL WHERE id={ph}",
                    ("missing", missing_since, asset_id),
                )
            else:
                conn.execute(
                    f"UPDATE customer_assets SET status={ph}, last_seen_ts={ph}, last_seen_device_ident={ph}, missing_since=NULL WHERE id={ph}",
                    ("present", last_seen_ts, last_seen_device, asset_id),
                )

            conn.execute(
                f"INSERT INTO audit_results "
                f"(audit_run_id, asset_id, beacon_id, status, last_seen_ts, last_seen_device_ident, last_distance, last_rssi) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (audit_run_id, asset_id, beacon_id, status, last_seen_ts, last_seen_device, distance, rssi),
            )
            results.append({
                "asset_name": asset_name,
                "beacon_id": beacon_id,
                "status": status,
                "last_seen": _fmt_ts(last_seen_ts),
                "device_ident": last_seen_device,
                "distance_label": f"{float(distance):.2f} m" if distance is not None else "",
                "rssi": rssi,
                "missing_since": missing_since if status == "missing" else "",
            })

        safe_customer = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(customer_id))
        safe_ts = scheduled_for.replace(":", "-").replace(" ", "_")
        out_dir = _ensure_dir(AUDIT_REPORTS_DIR)
        pdf_path = os.path.join(out_dir, f"audit_customer_{safe_customer}_{safe_ts}.pdf")
        _write_audit_pdf(pdf_path, customer[1], scheduled_for, window_start_label, window_end_label, results)

        conn.execute(
            f"UPDATE audit_runs SET status={ph}, pdf_path={ph} WHERE id={ph}",
            ("complete", pdf_path, audit_run_id),
        )
        conn.commit()
    finally:
        conn.close()

    return pdf_path


def run_daily_audits(scheduled_local_dt=None):
    conn = get_db()
    try:
        rows = conn.execute("SELECT id FROM customers WHERE active = 1 ORDER BY id").fetchall()
    finally:
        conn.close()

    paths = []
    for row in rows:
        try:
            path = run_customer_audit(row[0], scheduled_local_dt=scheduled_local_dt)
            if path:
                paths.append(path)
        except Exception as e:
            print(f"[audit] customer {row[0]} failed: {e}")
    return paths


def _audit_email_recipients(conn, customer_id):
    ph = _ph(conn)
    rows = conn.execute(
        f"SELECT email FROM app_users WHERE customer_id = {ph} AND active = 1 AND deleted_at IS NULL ORDER BY role, email",
        (customer_id,),
    ).fetchall()
    return [str(r[0]).strip() for r in rows if r and r[0] and str(r[0]).strip()]


def send_pending_audit_report_emails(scheduled_local_dt=None):
    """Email completed audit PDFs for the audit day that have not been sent yet."""
    scheduled, _window_start, _window_end = _audit_schedule_for_day(scheduled_local_dt)
    scheduled_for = _fmt_local_dt(scheduled)

    conn = get_db()
    try:
        ph = _ph(conn)
        rows = conn.execute(
            f"SELECT ar.id, ar.customer_id, c.name, ar.pdf_path, ar.scan_window_start, ar.scan_window_end "
            f"FROM audit_runs ar JOIN customers c ON c.id = ar.customer_id "
            f"WHERE c.active = 1 AND ar.scheduled_for = {ph} AND ar.status = {ph} "
            f"AND ar.pdf_path IS NOT NULL AND (ar.emailed_at IS NULL OR ar.emailed_at = '') "
            f"ORDER BY c.name",
            (scheduled_for, "complete"),
        ).fetchall()
        pending = []
        for audit_id, customer_id, customer_name, pdf_path, window_start, window_end in rows:
            pending.append({
                "audit_id": audit_id,
                "customer_id": customer_id,
                "customer_name": customer_name,
                "pdf_path": pdf_path,
                "window_start": window_start,
                "window_end": window_end,
                "recipients": _audit_email_recipients(conn, customer_id),
            })
    finally:
        conn.close()

    sent_count = 0
    for item in pending:
        pdf_path = item["pdf_path"]
        if not pdf_path or not os.path.exists(pdf_path):
            print(f"[audit-email] skipped audit {item['audit_id']}: report file missing")
            continue

        sent = send_report_email(
            item["recipients"],
            f"Daily Equipment Audit - {scheduled_for}",
            f"Attached is the daily equipment audit for {item['customer_name']}.\n\n"
            f"Scan window: {item['window_start']} to {item['window_end']} Samoa time.",
            attachment_path=pdf_path,
        )
        if not sent:
            continue

        conn = get_db()
        try:
            ph = _ph(conn)
            conn.execute(
                f"UPDATE audit_runs SET emailed_at={ph} WHERE id={ph}",
                (format_samoa_time(time.time()), item["audit_id"]),
            )
            conn.commit()
            sent_count += 1
        finally:
            conn.close()

    if pending:
        print(f"[audit-email] sent {sent_count}/{len(pending)} audit report emails for {scheduled_for}")
    return sent_count


def mark_asset_found_if_missing(conn, beacon_id, device_ident, seen_ts, distance=None, rssi=None):
    """Mark a customer's expected asset found when live telemetry sees it again."""
    ph = _ph(conn)
    rows = conn.execute(
        f"SELECT ca.id, ca.customer_id, ca.name, ca.status "
        f"FROM customer_assets ca "
        f"JOIN customer_devices cd ON cd.customer_id = ca.customer_id AND cd.device_ident = {ph} "
        f"WHERE ca.beacon_id = {ph} AND ca.active = 1 AND ca.status = {ph}",
        (device_ident, beacon_id, "missing"),
    ).fetchall()

    for asset_id, _customer_id, asset_name, _status in rows:
        found_at = format_samoa_time(seen_ts)
        conn.execute(
            f"UPDATE customer_assets SET status={ph}, found_at={ph}, missing_since=NULL, "
            f"last_seen_ts={ph}, last_seen_device_ident={ph} WHERE id={ph}",
            ("present", found_at, int(seen_ts), device_ident, asset_id),
        )
        emit_notification(
            "found",
            beacon_id,
            event_time=found_at,
            device_ident=device_ident,
            distance=distance,
        )


def _audit_loop():
    last_run_key = None
    while True:
        try:
            now_local = _local_dt_from_unix()
            scheduled, _start, audit_end = _audit_schedule_for_day(now_local)
            run_key = scheduled.strftime("%Y-%m-%d")
            # Run just after the scan window closes so the 6 PM audit can include
            # packets that arrive shortly after the scheduled audit time.
            if now_local >= audit_end and run_key != last_run_key:
                run_daily_audits(scheduled_local_dt=scheduled)
                last_run_key = run_key
            time.sleep(30)
        except Exception as e:
            print(f"[audit] loop error: {e}")
            time.sleep(60)


def start_audit_thread():
    t = threading.Thread(target=_audit_loop, daemon=True)
    t.start()
    print("[audit] thread started ✅")
    return t
