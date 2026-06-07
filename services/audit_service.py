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
from services.email_service import send_report_email_with_results
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
        f"SELECT device_ident FROM customer_devices WHERE customer_id = {ph} ORDER BY device_ident",
        (customer_id,),
    ).fetchall()
    return [str(r[0]) for r in rows if r and r[0]]


def _get_customer_device_labels(conn, customer_id):
    ph = _ph(conn)
    rows = conn.execute(
        f"SELECT cd.device_ident, COALESCE(cds.name, cd.label, d.name, cd.device_ident) AS label "
        f"FROM customer_devices cd "
        f"LEFT JOIN customer_device_settings cds ON cds.customer_id = cd.customer_id AND cds.device_ident = cd.device_ident "
        f"LEFT JOIN devices d ON d.id = cd.device_ident "
        f"WHERE cd.customer_id = {ph} ORDER BY COALESCE(cds.name, cd.label, d.name, cd.device_ident)",
        (customer_id,),
    ).fetchall()
    return {str(r[0]): str(r[1] or r[0]) for r in rows if r and r[0]}


def _device_label(device_ident, device_labels):
    if not device_ident:
        return ""
    return device_labels.get(str(device_ident), str(device_ident))


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


def _latest_observation_excluding(conn, beacon_id, device_idents, excluded_idents, start_ts, end_ts):
    candidates = [str(d) for d in device_idents if d and str(d) not in {str(e) for e in excluded_idents if e}]
    return _latest_observation(conn, beacon_id, candidates, start_ts, end_ts)


def _latest_observation_on_device_before(conn, beacon_id, device_ident, end_ts):
    if not device_ident:
        return None
    ph = _ph(conn)
    return conn.execute(
        f"SELECT observed_ts, device_ident, distance, rssi "
        f"FROM beacon_observations "
        f"WHERE beacon_id = {ph} AND device_ident = {ph} AND observed_ts <= {ph} "
        "ORDER BY observed_ts DESC LIMIT 1",
        (beacon_id, device_ident, end_ts),
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


def _status_label(status):
    if status == "present":
        return "In range"
    if status == "equipment_moved":
        return "Equipment moved"
    return "Missing"


def _status_color(status):
    if status == "present":
        return colors.HexColor("#dcfce7")
    if status == "equipment_moved":
        return colors.HexColor("#fef3c7")
    return colors.HexColor("#fee2e2")


def _section_title(device_ident, device_labels):
    if device_ident == "__unassigned__":
        return "Unassigned / Any assigned device"
    return _device_label(device_ident, device_labels)


def _write_audit_pdf(pdf_path, customer_name, scheduled_for, window_start, window_end, results, device_labels):
    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=landscape(A4),
        rightMargin=24,
        leftMargin=24,
        topMargin=24,
        bottomMargin=24,
    )
    styles = getSampleStyleSheet()
    summary_statuses = {}
    for r in results:
        summary_statuses.setdefault(r.get("asset_id") or r.get("beacon_id"), r["status"])
    present_count = sum(1 for status in summary_statuses.values() if status == "present")
    missing_count = sum(1 for status in summary_statuses.values() if status == "missing")
    moved_count = sum(1 for status in summary_statuses.values() if status == "equipment_moved")

    story = [
        Paragraph("Daily Equipment Audit", styles["Title"]),
        Paragraph(f"Customer: {_paragraph_text(customer_name)}", styles["Normal"]),
        Paragraph(f"Scheduled: {_paragraph_text(scheduled_for)} Samoa time", styles["Normal"]),
        Paragraph(f"Scan window: {_paragraph_text(window_start)} to {_paragraph_text(window_end)}", styles["Normal"]),
        Paragraph(
            f"Summary: {present_count} present, {moved_count} equipment moved, "
            f"{missing_count} missing, {len(summary_statuses)} total assets",
            styles["Normal"],
        ),
        Spacer(1, 12),
    ]

    grouped = {}
    for r in results:
        grouped.setdefault(r.get("section_device") or "__unassigned__", []).append(r)

    section_order = sorted(grouped, key=lambda key: _section_title(key, device_labels).lower())
    for section_key in section_order:
        story.append(Paragraph(f"Device / Site: {_paragraph_text(_section_title(section_key, device_labels))}", styles["Heading2"]))
        table_data = [[
            Paragraph("Asset", styles["BodyText"]),
            Paragraph("Beacon ID", styles["BodyText"]),
            Paragraph("Status", styles["BodyText"]),
            Paragraph("Expected site", styles["BodyText"]),
            Paragraph("Detected site", styles["BodyText"]),
            Paragraph("Last seen", styles["BodyText"]),
            Paragraph("RSSI", styles["BodyText"]),
            Paragraph("Distance", styles["BodyText"]),
            Paragraph("Notes", styles["BodyText"]),
        ]]
        row_backgrounds = []
        for r in grouped[section_key]:
            note = r.get("note") or r.get("missing_since") or ""
            table_data.append([
                Paragraph(_paragraph_text(r["asset_name"]), styles["BodyText"]),
                Paragraph(_paragraph_text(r["beacon_id"]), styles["BodyText"]),
                Paragraph(_paragraph_text(_status_label(r["status"])), styles["BodyText"]),
                Paragraph(_paragraph_text(r.get("expected_device_label") or ""), styles["BodyText"]),
                Paragraph(_paragraph_text(r.get("actual_device_label") or ""), styles["BodyText"]),
                Paragraph(_paragraph_text(r.get("last_seen") or ""), styles["BodyText"]),
                Paragraph(_paragraph_text(r.get("rssi") if r.get("rssi") is not None else ""), styles["BodyText"]),
                Paragraph(_paragraph_text(r.get("distance_label") or ""), styles["BodyText"]),
                Paragraph(_paragraph_text(note), styles["BodyText"]),
            ])
            row_backgrounds.append(_status_color(r["status"]))

        table = Table(table_data, colWidths=[100, 165, 80, 100, 100, 105, 40, 58, 135], repeatRows=1)
        table_style = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]
        for idx, bg in enumerate(row_backgrounds, start=1):
            table_style.append(("BACKGROUND", (0, idx), (-1, idx), bg))
        table.setStyle(TableStyle(table_style))
        story.append(table)
        story.append(Spacer(1, 14))
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
        device_labels = _get_customer_device_labels(conn, customer_id)
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
            expected_device = str(expected_device_ident) if expected_device_ident else None
            device_scope = [expected_device] if expected_device else customer_devices
            expected_obs = _latest_observation(conn, beacon_id, device_scope, start_ts, end_ts)
            moved_obs = None
            if expected_device:
                moved_obs = _latest_observation_excluding(
                    conn,
                    beacon_id,
                    customer_devices,
                    [expected_device],
                    start_ts,
                    end_ts,
                )
            obs = expected_obs or moved_obs
            if expected_obs:
                status = "present"
            elif moved_obs:
                status = "equipment_moved"
            else:
                status = "missing"
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
            elif status == "equipment_moved":
                if previous_status != "equipment_moved":
                    emit_notification(
                        "equipment_moved",
                        beacon_id,
                        event_time=_fmt_ts(last_seen_ts) or now_label,
                        device_ident=last_seen_device,
                        distance=distance,
                    )
                moved_since = missing_since if previous_status == "equipment_moved" and missing_since else now_label
                conn.execute(
                    f"UPDATE customer_assets SET status={ph}, last_seen_ts={ph}, last_seen_device_ident={ph}, "
                    f"missing_since={ph}, found_at=NULL WHERE id={ph}",
                    ("equipment_moved", last_seen_ts, last_seen_device, moved_since, asset_id),
                )
                missing_since = moved_since
            else:
                conn.execute(
                    f"UPDATE customer_assets SET status={ph}, last_seen_ts={ph}, last_seen_device_ident={ph}, missing_since=NULL, found_at=NULL WHERE id={ph}",
                    ("present", last_seen_ts, last_seen_device, asset_id),
                )

            conn.execute(
                f"INSERT INTO audit_results "
                f"(audit_run_id, asset_id, beacon_id, status, last_seen_ts, last_seen_device_ident, last_distance, last_rssi) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (audit_run_id, asset_id, beacon_id, status, last_seen_ts, last_seen_device, distance, rssi),
            )
            result_row = {
                "asset_id": asset_id,
                "asset_name": asset_name,
                "beacon_id": beacon_id,
                "status": status,
                "last_seen": _fmt_ts(last_seen_ts),
                "last_seen_ts": last_seen_ts,
                "device_ident": last_seen_device,
                "expected_device_ident": expected_device,
                "expected_device_label": _device_label(expected_device, device_labels) if expected_device else "Any assigned device",
                "actual_device_label": _device_label(last_seen_device, device_labels) if last_seen_device else "",
                "section_device": last_seen_device if status == "equipment_moved" else (expected_device or "__unassigned__"),
                "distance_label": f"{float(distance):.2f} m" if distance is not None else "",
                "rssi": rssi,
                "missing_since": missing_since if status in ("missing", "equipment_moved") else "",
                "note": (
                    f"Expected at {_device_label(expected_device, device_labels)}; "
                    f"detected at {_device_label(last_seen_device, device_labels)}"
                    if status == "equipment_moved" else
                    (f"Missing since {missing_since}" if status == "missing" and missing_since else "")
                ),
            }
            if status == "equipment_moved" and expected_device:
                expected_last_obs = _latest_observation_on_device_before(conn, beacon_id, expected_device, end_ts)
                expected_last_seen_ts = expected_last_obs[0] if expected_last_obs else None
                results.append({
                    **result_row,
                    "last_seen": _fmt_ts(expected_last_seen_ts),
                    "last_seen_ts": expected_last_seen_ts,
                    "device_ident": expected_device,
                    "actual_device_label": _device_label(last_seen_device, device_labels),
                    "section_device": expected_device,
                    "distance_label": "",
                    "rssi": "",
                    "note": (
                        f"Moved to {_device_label(last_seen_device, device_labels)}. "
                        f"Last seen here {_fmt_ts(expected_last_seen_ts) or 'not recorded'}."
                    ),
                })
                results.append({
                    **result_row,
                    "section_device": last_seen_device,
                    "note": (
                        f"Detected here; expected at {_device_label(expected_device, device_labels)}."
                    ),
                })
            else:
                results.append(result_row)

        safe_customer = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(customer_id))
        safe_ts = scheduled_for.replace(":", "-").replace(" ", "_")
        out_dir = _ensure_dir(AUDIT_REPORTS_DIR)
        pdf_path = os.path.join(out_dir, f"audit_customer_{safe_customer}_{safe_ts}.pdf")
        _write_audit_pdf(pdf_path, customer[1], scheduled_for, window_start_label, window_end_label, results, device_labels)

        conn.execute(
            f"UPDATE audit_runs SET status={ph}, pdf_path={ph} WHERE id={ph}",
            ("complete", pdf_path, audit_run_id),
        )
        recipients = [
            r[0] for r in conn.execute(
                f"SELECT email FROM app_users WHERE customer_id = {ph} AND active = 1 AND deleted_at IS NULL",
                (customer_id,),
            ).fetchall()
        ]
        conn.commit()
    finally:
        conn.close()

    sent = send_report_email(
        recipients,
        f"Daily Equipment Audit - {scheduled_for}",
        f"Attached is the daily equipment audit for {customer[1]}.\n\n"
        f"Scan window: {window_start_label} to {window_end_label} Samoa time.\n"
        f"Assets checked: {len(results)}.",
        attachment_path=pdf_path,
    )
    if sent:
        conn = get_db()
        try:
            ph = _ph(conn)
            conn.execute(
                f"UPDATE audit_runs SET emailed_at={ph} WHERE id={ph}",
                (format_samoa_time(time.time()), audit_run_id),
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


def _insert_email_log(conn, audit_id, customer_id, recipient, subject, result, attachment_path, send_type, actor_user=None):
    ph = _ph(conn)
    actor_user = actor_user or {}
    conn.execute(
        f"INSERT INTO email_logs "
        f"(created_at, audit_run_id, customer_id, recipient, subject, provider, status, error, attachment_path, send_type, actor_user_id, actor_email) "
        f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
        (
            format_samoa_time(time.time()),
            audit_id,
            customer_id,
            recipient,
            subject,
            result.get("provider"),
            result.get("status"),
            result.get("error") or "",
            attachment_path,
            send_type,
            actor_user.get("id"),
            actor_user.get("email"),
        ),
    )


def send_audit_report_email(audit_id, actor_user=None, send_type="manual"):
    """Send one audit report PDF and log each recipient delivery attempt."""
    conn = get_db()
    try:
        ph = _ph(conn)
        row = conn.execute(
            f"SELECT ar.id, ar.customer_id, c.name, ar.scheduled_for, ar.scan_window_start, ar.scan_window_end, ar.pdf_path, ar.status "
            f"FROM audit_runs ar JOIN customers c ON c.id = ar.customer_id WHERE ar.id = {ph}",
            (audit_id,),
        ).fetchone()
        if not row:
            return 0
        audit_id, customer_id, customer_name, scheduled_for, window_start, window_end, pdf_path, status = row
        subject = f"Daily Equipment Audit - {scheduled_for or 'report unavailable'}"
        if status != "complete" or not pdf_path or not os.path.exists(pdf_path):
            _insert_email_log(
                conn,
                audit_id,
                customer_id,
                "",
                subject,
                {"provider": "smtp", "status": "skipped", "error": "Audit report PDF is not ready"},
                pdf_path,
                send_type,
                actor_user=actor_user,
            )
            conn.commit()
            return 0
        recipients = _audit_email_recipients(conn, customer_id)
    finally:
        conn.close()

    body = (
        f"Attached is the daily equipment audit for {customer_name}.\n\n"
        f"Scan window: {window_start} to {window_end} Samoa time."
    )
    results = send_report_email_with_results(recipients, subject, body, attachment_path=pdf_path)

    conn = get_db()
    try:
        ph = _ph(conn)
        sent_count = 0
        if not results:
            _insert_email_log(
                conn,
                audit_id,
                customer_id,
                "",
                subject,
                {"provider": "smtp", "status": "skipped", "error": "No active customer recipients"},
                pdf_path,
                send_type,
                actor_user=actor_user,
            )
        for result in results:
            if result.get("status") == "sent":
                sent_count += 1
            _insert_email_log(
                conn,
                audit_id,
                customer_id,
                result.get("recipient") or "",
                subject,
                result,
                pdf_path,
                send_type,
                actor_user=actor_user,
            )
        if sent_count:
            conn.execute(
                f"UPDATE audit_runs SET emailed_at={ph} WHERE id={ph}",
                (format_samoa_time(time.time()), audit_id),
            )
        conn.commit()
    finally:
        conn.close()
    return sent_count


def send_pending_audit_report_emails(scheduled_local_dt=None):
    """Email completed audit PDFs for the audit day that have not been sent yet."""
    scheduled, _window_start, _window_end = _audit_schedule_for_day(scheduled_local_dt)
    scheduled_for = _fmt_local_dt(scheduled)

    conn = get_db()
    try:
        ph = _ph(conn)
        rows = conn.execute(
            f"SELECT ar.id FROM audit_runs ar JOIN customers c ON c.id = ar.customer_id "
            f"WHERE c.active = 1 AND ar.scheduled_for = {ph} AND ar.status = {ph} "
            f"AND ar.pdf_path IS NOT NULL AND (ar.emailed_at IS NULL OR ar.emailed_at = '') "
            f"ORDER BY c.name",
            (scheduled_for, "complete"),
        ).fetchall()
    finally:
        conn.close()

    sent_count = 0
    for row in rows:
        sent_count += send_audit_report_email(row[0], send_type="scheduled")

    if rows:
        print(f"[audit-email] sent {sent_count} recipient emails across {len(rows)} audit reports for {scheduled_for}")
    return sent_count


def mark_asset_found_if_missing(conn, beacon_id, device_ident, seen_ts, distance=None, rssi=None):
    """Mark a customer's expected asset found when live telemetry sees it again."""
    ph = _ph(conn)
    rows = conn.execute(
        f"SELECT ca.id, ca.customer_id, ca.name, ca.status, ca.expected_device_ident "
        f"FROM customer_assets ca "
        f"JOIN customer_devices cd ON cd.customer_id = ca.customer_id AND cd.device_ident = {ph} "
        f"WHERE ca.beacon_id = {ph} AND ca.active = 1 AND ca.status IN ({ph},{ph})",
        (device_ident, beacon_id, "missing", "equipment_moved"),
    ).fetchall()

    for asset_id, _customer_id, asset_name, _status, expected_device_ident in rows:
        found_at = format_samoa_time(seen_ts)
        expected_device = str(expected_device_ident) if expected_device_ident else None
        moved = expected_device and str(device_ident) != expected_device
        new_status = "equipment_moved" if moved else "present"
        missing_since_expr = "missing_since" if moved else "NULL"
        found_at_value = None if moved else found_at
        conn.execute(
            f"UPDATE customer_assets SET status={ph}, found_at={ph}, missing_since={missing_since_expr}, "
            f"last_seen_ts={ph}, last_seen_device_ident={ph} WHERE id={ph}",
            (new_status, found_at_value, int(seen_ts), device_ident, asset_id),
        )
        emit_notification(
            "equipment_moved" if moved else "found",
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
