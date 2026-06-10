import os
import threading
import time
from datetime import datetime, timedelta

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
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
from services.webhook_service import send_whatsapp_audit_webhook


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


def _parse_clock(value, fallback_hour, fallback_minute):
    try:
        hour_text, minute_text = str(value or "").strip().split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (TypeError, ValueError):
        pass
    return fallback_hour, fallback_minute


def _audit_schedule_for_day(local_dt=None, audit_time=None):
    local_dt = local_dt or _local_dt_from_unix()
    hour, minute = _parse_clock(audit_time, AUDIT_HOUR, AUDIT_MINUTE)
    scheduled = local_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    start = scheduled - timedelta(minutes=AUDIT_WINDOW_BEFORE_MIN)
    end = scheduled + timedelta(minutes=AUDIT_WINDOW_AFTER_MIN)
    return scheduled, start, end


def _audit_email_time_for_day(local_dt=None, delivery_time=None):
    local_dt = local_dt or _local_dt_from_unix()
    hour, minute = _parse_clock(delivery_time, AUDIT_EMAIL_HOUR, AUDIT_EMAIL_MINUTE)
    return local_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _delivery_time_for_audit(scheduled, delivery_time=None):
    delivery = _audit_email_time_for_day(scheduled, delivery_time)
    if delivery <= scheduled:
        delivery += timedelta(days=1)
    return delivery


def _split_recipients(value):
    cleaned = str(value or "").replace(";", ",").replace("\n", ",")
    seen = set()
    recipients = []
    for item in cleaned.split(","):
        recipient = item.strip()
        if recipient and recipient not in seen:
            recipients.append(recipient)
            seen.add(recipient)
    return recipients


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
    existing = conn.execute(
        f"SELECT id FROM audit_runs WHERE customer_id = {ph} AND scheduled_for = {ph}",
        (customer_id, scheduled_for),
    ).fetchone()
    if existing:
        return None
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
    cell_style = ParagraphStyle(
        "AuditCell",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=7,
        leading=8.5,
        spaceAfter=0,
    )
    detail_style = ParagraphStyle(
        "AuditDetail",
        parent=cell_style,
        textColor=colors.HexColor("#475569"),
        fontSize=6.3,
        leading=7.5,
    )
    heading_style = ParagraphStyle(
        "AuditSection",
        parent=styles["Heading2"],
        fontSize=12,
        leading=14,
        spaceBefore=5,
        spaceAfter=6,
    )
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
        story.append(Paragraph(
            f"Device / Site: {_paragraph_text(_section_title(section_key, device_labels))}",
            heading_style,
        ))
        table_data = [[
            Paragraph("Asset / Beacon", cell_style),
            Paragraph("Status", cell_style),
            Paragraph("Expected site", cell_style),
            Paragraph("Detected / Last seen", cell_style),
            Paragraph("Signal", cell_style),
            Paragraph("Details", cell_style),
        ]]
        row_backgrounds = []
        for r in grouped[section_key]:
            note = r.get("note") or r.get("missing_since") or ""
            asset_cell = (
                f"<b>{_paragraph_text(r['asset_name'])}</b><br/>"
                f"<font size='6' color='#475569'>{_paragraph_text(r['beacon_id'])}</font>"
            )
            detected_cell = _paragraph_text(r.get("actual_device_label") or "")
            if r.get("last_seen"):
                detected_cell += f"<br/><font size='6' color='#475569'>{_paragraph_text(r['last_seen'])}</font>"
            signal_parts = []
            if r.get("rssi") not in (None, ""):
                signal_parts.append(f"{_paragraph_text(r['rssi'])} dBm")
            if r.get("distance_label"):
                signal_parts.append(_paragraph_text(r["distance_label"]))
            table_data.append([
                Paragraph(asset_cell, cell_style),
                Paragraph(_paragraph_text(_status_label(r["status"])), cell_style),
                Paragraph(_paragraph_text(r.get("expected_device_label") or ""), cell_style),
                Paragraph(detected_cell, cell_style),
                Paragraph("<br/>".join(signal_parts), detail_style),
                Paragraph(_paragraph_text(note), cell_style),
            ])
            row_backgrounds.append(_status_color(r["status"]))

        table = Table(
            table_data,
            colWidths=[135, 82, 110, 135, 72, 210],
            repeatRows=1,
            splitByRow=1,
            hAlign="LEFT",
        )
        table_style = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
        for idx, bg in enumerate(row_backgrounds, start=1):
            table_style.append(("BACKGROUND", (0, idx), (-1, idx), bg))
        table.setStyle(TableStyle(table_style))
        story.append(table)
        story.append(Spacer(1, 14))
    doc.build(story)


def run_customer_audit(customer_id, scheduled_local_dt=None):
    conn = get_db()
    try:
        ph = _ph(conn)
        customer = conn.execute(
            f"SELECT id, name, COALESCE(audit_time, {ph}) "
            f"FROM customers WHERE id = {ph} AND active = 1",
            (f"{AUDIT_HOUR:02d}:{AUDIT_MINUTE:02d}", customer_id),
        ).fetchone()
        if not customer:
            return None

        if scheduled_local_dt is None:
            scheduled, window_start, window_end = _audit_schedule_for_day(
                _local_dt_from_unix(),
                customer[2],
            )
        else:
            scheduled = scheduled_local_dt.replace(second=0, microsecond=0)
            window_start = scheduled - timedelta(minutes=AUDIT_WINDOW_BEFORE_MIN)
            window_end = scheduled + timedelta(minutes=AUDIT_WINDOW_AFTER_MIN)

        scheduled_for = _fmt_local_dt(scheduled)
        window_start_label = _fmt_local_dt(window_start)
        window_end_label = _fmt_local_dt(window_end)
        start_ts = _unix_from_local_dt(window_start)
        end_ts = _unix_from_local_dt(window_end)
        now_label = format_samoa_time(time.time())

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


def run_due_customer_audits(now_local=None):
    """Generate each active customer's audit after its configured scan window."""
    now_local = now_local or _local_dt_from_unix()
    conn = get_db()
    try:
        ph = _ph(conn)
        rows = conn.execute(
            f"SELECT id, COALESCE(audit_time, {ph}) FROM customers "
            f"WHERE active = 1 ORDER BY id",
            (f"{AUDIT_HOUR:02d}:{AUDIT_MINUTE:02d}",),
        ).fetchall()
    finally:
        conn.close()

    generated = []
    for customer_id, audit_time in rows:
        scheduled, _window_start, audit_end = _audit_schedule_for_day(now_local, audit_time)
        if now_local < audit_end:
            continue
        try:
            path = run_customer_audit(customer_id, scheduled_local_dt=scheduled)
            if path:
                generated.append(path)
        except Exception as exc:
            print(f"[audit] customer {customer_id} failed: {exc}")
    return generated


def _audit_email_recipients(conn, customer_id):
    ph = _ph(conn)
    rows = conn.execute(
        f"SELECT email FROM app_users WHERE customer_id = {ph} AND active = 1 AND deleted_at IS NULL ORDER BY role, email",
        (customer_id,),
    ).fetchall()
    return [str(r[0]).strip() for r in rows if r and r[0] and str(r[0]).strip()]


def _audit_whatsapp_recipients(conn, customer_id):
    ph = _ph(conn)
    row = conn.execute(
        f"SELECT whatsapp_recipients FROM customers WHERE id = {ph}",
        (customer_id,),
    ).fetchone()
    return _split_recipients(row[0] if row else "")


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


def _insert_webhook_log(conn, audit_id, customer_id, recipients, result, send_type, actor_user=None):
    ph = _ph(conn)
    actor_user = actor_user or {}
    conn.execute(
        f"INSERT INTO webhook_logs "
        f"(created_at, audit_run_id, customer_id, webhook_url, recipients, status, http_status, error, send_type, actor_user_id, actor_email) "
        f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
        (
            format_samoa_time(time.time()),
            audit_id,
            customer_id,
            result.get("webhook_url") or "",
            ", ".join(recipients),
            result.get("status"),
            result.get("http_status"),
            result.get("error") or "",
            send_type,
            actor_user.get("id"),
            actor_user.get("email"),
        ),
    )


def _audit_delivery_data(conn, audit_id):
    ph = _ph(conn)
    audit = conn.execute(
        f"SELECT ar.id, ar.customer_id, c.name, c.slug, ar.scheduled_for, "
        f"ar.scan_window_start, ar.scan_window_end, ar.pdf_path, ar.status "
        f"FROM audit_runs ar JOIN customers c ON c.id = ar.customer_id "
        f"WHERE ar.id = {ph}",
        (audit_id,),
    ).fetchone()
    if not audit:
        return None

    rows = conn.execute(
        f"SELECT ar.beacon_id, COALESCE(ca.name, cbn.name, ar.beacon_id), ar.status, "
        f"ca.expected_device_ident, ar.last_seen_device_ident, ar.last_seen_ts, "
        f"ar.last_distance, ar.last_rssi "
        f"FROM audit_results ar "
        f"LEFT JOIN customer_assets ca ON ca.id = ar.asset_id "
        f"LEFT JOIN customer_beacon_names cbn ON cbn.customer_id = {ph} AND cbn.beacon_id = ar.beacon_id "
        f"WHERE ar.audit_run_id = {ph} ORDER BY COALESCE(ca.name, cbn.name, ar.beacon_id)",
        (audit[1], audit_id),
    ).fetchall()
    labels = _get_customer_device_labels(conn, audit[1])
    return audit, rows, labels


def _build_whatsapp_payload(conn, audit_id, recipients):
    delivery_data = _audit_delivery_data(conn, audit_id)
    if not delivery_data:
        return None
    audit, rows, labels = delivery_data
    audit_id, customer_id, customer_name, customer_slug, scheduled_for, window_start, window_end, _pdf_path, _status = audit

    site_map = {
        str(device_ident): {
            "device_ident": str(device_ident),
            "device_name": label,
            "counts": {"present": 0, "missing": 0, "equipment_moved": 0},
            "assets": [],
        }
        for device_ident, label in labels.items()
    }
    summary = {"present": 0, "missing": 0, "equipment_moved": 0, "total": len(rows)}
    moved_assets = []

    for beacon_id, asset_name, status, expected_ident, actual_ident, last_seen_ts, distance, rssi in rows:
        status = status or "missing"
        if status not in summary:
            status = "missing"
        summary[status] += 1
        expected_ident = str(expected_ident) if expected_ident else None
        actual_ident = str(actual_ident) if actual_ident else None
        section_ident = expected_ident or actual_ident or "__unassigned__"
        if section_ident not in site_map:
            site_map[section_ident] = {
                "device_ident": None if section_ident == "__unassigned__" else section_ident,
                "device_name": "Any assigned device" if section_ident == "__unassigned__" else _device_label(section_ident, labels),
                "counts": {"present": 0, "missing": 0, "equipment_moved": 0},
                "assets": [],
            }

        asset_payload = {
            "asset_name": str(asset_name or beacon_id),
            "beacon_id": str(beacon_id),
            "status": status,
            "status_label": _status_label(status),
            "expected_device_ident": expected_ident,
            "expected_device_name": _device_label(expected_ident, labels) if expected_ident else "Any assigned device",
            "detected_device_ident": actual_ident,
            "detected_device_name": _device_label(actual_ident, labels) if actual_ident else "",
            "last_seen": _fmt_ts(last_seen_ts),
            "distance_m": round(float(distance), 2) if distance is not None else None,
            "rssi": float(rssi) if rssi is not None else None,
        }
        site_map[section_ident]["counts"][status] += 1
        site_map[section_ident]["assets"].append(asset_payload)

        if status == "equipment_moved":
            moved_assets.append(asset_payload)
            if actual_ident and actual_ident != section_ident:
                if actual_ident not in site_map:
                    site_map[actual_ident] = {
                        "device_ident": actual_ident,
                        "device_name": _device_label(actual_ident, labels),
                        "counts": {"present": 0, "missing": 0, "equipment_moved": 0},
                        "assets": [],
                    }
                site_map[actual_ident]["counts"]["equipment_moved"] += 1
                site_map[actual_ident]["assets"].append({**asset_payload, "detected_here": True})

    devices = sorted(site_map.values(), key=lambda item: item["device_name"].lower())
    lines = [
        f"Daily Equipment Audit - {customer_name}",
        f"Audit time: {scheduled_for} Samoa time",
        (
            f"Summary: {summary['present']} in range, "
            f"{summary['missing']} missing, {summary['equipment_moved']} moved"
        ),
        "",
    ]
    for device in devices:
        counts = device["counts"]
        lines.append(
            f"{device['device_name']}: {counts['present']} in range, "
            f"{counts['missing']} missing, {counts['equipment_moved']} moved"
        )
        missing_names = [a["asset_name"] for a in device["assets"] if a["status"] == "missing"]
        if missing_names:
            lines.append(f"Missing: {', '.join(missing_names)}")
    if moved_assets:
        lines.append("")
        lines.append("Equipment moved:")
        for asset in moved_assets:
            lines.append(
                f"{asset['asset_name']}: {asset['expected_device_name']} -> "
                f"{asset['detected_device_name'] or 'another site'}"
            )
    lines.extend(["", "Please check the audit report sent by email for full details."])

    return {
        "event": "daily_equipment_audit",
        "version": 1,
        "customer": {
            "id": customer_id,
            "name": customer_name,
            "slug": customer_slug,
        },
        "audit": {
            "id": audit_id,
            "scheduled_for": scheduled_for,
            "scan_window_start": window_start,
            "scan_window_end": window_end,
            "timezone": "Pacific/Apia",
        },
        "whatsapp": {
            "recipients": recipients,
            "template_name": "daily_equipment_audit",
            "language": "en",
        },
        "summary": summary,
        "devices": devices,
        "moved_assets": moved_assets,
        "message": {
            "title": f"Daily Equipment Audit - {customer_name}",
            "body": "\n".join(lines),
        },
    }


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


def send_audit_whatsapp(audit_id, actor_user=None, send_type="manual"):
    conn = get_db()
    try:
        ph = _ph(conn)
        row = conn.execute(
            f"SELECT customer_id, status FROM audit_runs WHERE id = {ph}",
            (audit_id,),
        ).fetchone()
        if not row:
            return False
        customer_id, audit_status = row
        recipients = _audit_whatsapp_recipients(conn, customer_id)
        if audit_status != "complete":
            result = {
                "status": "skipped",
                "http_status": None,
                "error": "Audit report is not complete",
                "webhook_url": "",
            }
            payload = None
        elif not recipients:
            result = {
                "status": "skipped",
                "http_status": None,
                "error": "No WhatsApp recipients configured for this customer",
                "webhook_url": "",
            }
            payload = None
        else:
            payload = _build_whatsapp_payload(conn, audit_id, recipients)
            result = None
    finally:
        conn.close()

    if result is None:
        result = send_whatsapp_audit_webhook(payload)

    conn = get_db()
    try:
        ph = _ph(conn)
        _insert_webhook_log(
            conn,
            audit_id,
            customer_id,
            recipients,
            result,
            send_type,
            actor_user=actor_user,
        )
        now_label = format_samoa_time(time.time())
        conn.execute(
            f"UPDATE audit_runs SET whatsapp_last_attempt_at = {ph} WHERE id = {ph}",
            (now_label, audit_id),
        )
        if result.get("status") == "sent":
            conn.execute(
                f"UPDATE audit_runs SET whatsapp_sent_at = {ph} WHERE id = {ph}",
                (now_label, audit_id),
            )
        conn.commit()
    finally:
        conn.close()
    return result.get("status") == "sent"


def send_due_customer_deliveries(now_local=None):
    """Send email and WhatsApp once each customer's configured delivery time is due."""
    now_local = now_local or _local_dt_from_unix()
    conn = get_db()
    try:
        ph = _ph(conn)
        rows = conn.execute(
            f"SELECT ar.id, ar.scheduled_for, ar.emailed_at, ar.whatsapp_sent_at, "
            f"ar.whatsapp_last_attempt_at, COALESCE(c.delivery_time, {ph}), "
            f"COALESCE(c.audit_time, {ph}) "
            f"FROM audit_runs ar JOIN customers c ON c.id = ar.customer_id "
            f"WHERE c.active = 1 AND ar.status = {ph} AND ar.pdf_path IS NOT NULL "
            f"ORDER BY ar.id DESC LIMIT 500",
            (
                f"{AUDIT_EMAIL_HOUR:02d}:{AUDIT_EMAIL_MINUTE:02d}",
                f"{AUDIT_HOUR:02d}:{AUDIT_MINUTE:02d}",
                "complete",
            ),
        ).fetchall()
    finally:
        conn.close()

    email_count = 0
    whatsapp_count = 0
    for (
        audit_id,
        scheduled_for,
        emailed_at,
        whatsapp_sent_at,
        whatsapp_last_attempt_at,
        delivery_time,
        audit_time,
    ) in rows:
        try:
            scheduled = datetime.strptime(str(scheduled_for), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        configured_hour, configured_minute = _parse_clock(audit_time, AUDIT_HOUR, AUDIT_MINUTE)
        if (scheduled.hour, scheduled.minute) != (configured_hour, configured_minute):
            continue
        if scheduled < now_local - timedelta(days=2):
            continue
        due_at = _delivery_time_for_audit(scheduled, delivery_time)
        if now_local < due_at:
            continue
        if not emailed_at:
            email_count += send_audit_report_email(audit_id, send_type="scheduled")
        if not whatsapp_sent_at and not whatsapp_last_attempt_at:
            whatsapp_count += int(send_audit_whatsapp(audit_id, send_type="scheduled"))

    return {"email_recipients_sent": email_count, "whatsapp_webhooks_sent": whatsapp_count}


def send_pending_audit_report_emails(scheduled_local_dt=None):
    """Backward-compatible wrapper for the per-customer delivery scheduler."""
    return send_due_customer_deliveries(scheduled_local_dt or _local_dt_from_unix())["email_recipients_sent"]


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
    while True:
        try:
            now_local = _local_dt_from_unix()
            run_due_customer_audits(now_local)
            send_due_customer_deliveries(now_local)
            time.sleep(30)
        except Exception as e:
            print(f"[audit] loop error: {e}")
            time.sleep(60)


def start_audit_thread():
    t = threading.Thread(target=_audit_loop, daemon=True)
    t.start()
    print("[audit] thread started ✅")
    return t
