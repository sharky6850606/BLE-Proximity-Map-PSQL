import os
import json
import time
import threading
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

from database import get_db
from services.beacon_logic import latest_messages, format_samoa_time
from config import REPORTS_DIR, ACTIVITY_REPORTS_DIR


def _ensure_dir(path: str) -> str:
    ab = os.path.abspath(path)
    os.makedirs(ab, exist_ok=True)
    return ab


def _status_label(state, missing):
    if missing:
        return "Missing"
    if state == "in":
        return "In range"
    if state == "out":
        return "Out of range"
    return "Unknown"


def _paragraph_text(value) -> str:
    """Return XML-safe text for reportlab Paragraph content."""
    return escape(str(value or ""), {'"': '&quot;', "'": '&apos;'})


def generate_daily_report():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT bn.id, bn.name, bs.state, bs.missing, bs.last_seen_ts, bs.device_ident "
            "FROM beacon_names bn "
            "LEFT JOIN beacon_states bs ON bn.id = bs.beacon_id "
            "ORDER BY bn.id"
        ).fetchall()

        # If a beacon has never been renamed (not in beacon_names), still include it from live state.
        extra = conn.execute(
            "SELECT bs.beacon_id, NULL AS name, bs.state, bs.missing, bs.last_seen_ts, bs.device_ident "
            "FROM beacon_states bs "
            "LEFT JOIN beacon_names bn ON bn.id = bs.beacon_id "
            "WHERE bn.id IS NULL "
            "ORDER BY bs.beacon_id"
        ).fetchall()
        rows.extend(extra)

        device_rows = conn.execute("SELECT id, name FROM devices ORDER BY id").fetchall()
        device_names = {str(r[0]): (r[1] or str(r[0])) for r in device_rows if r and r[0]}
    finally:
        conn.close()

    entries = []
    for bid, bname, state, missing, last_seen_ts, device_ident in rows:
        if not bid:
            continue
        device_ident = str(device_ident) if device_ident else ""
        last_device = device_names.get(device_ident) if device_ident else ""
        entries.append({
            "id": str(bid),
            "name": bname or str(bid),
            "status": _status_label(state, bool(missing)),
            "last_seen": format_samoa_time(int(last_seen_ts)) if last_seen_ts else "",
            "last_device": last_device or device_ident,
        })

    # Deduplicate by beacon id (prefer row with latest last_seen)
    dedup = {}
    for e in entries:
        prev = dedup.get(e["id"])
        if not prev:
            dedup[e["id"]] = e
            continue
        if e.get("last_seen", "") >= prev.get("last_seen", ""):
            dedup[e["id"]] = e
    entries = list(dedup.values())

    entries.sort(key=lambda e: (e.get("name") or "", e.get("id") or ""))

    now = time.time()
    created_at = format_samoa_time(now).replace(" ", "T")
    safe_ts = format_samoa_time(now).replace(":", "-").replace(" ", "_")
    out_dir = _ensure_dir(REPORTS_DIR)
    pdf_path = os.path.join(out_dir, f"report_{safe_ts}.pdf")
    _write_daily_pdf(pdf_path, created_at, entries)

    summary = f"{len(entries)} beacons"
    conn = get_db()
    try:
        ph = _db_ph(conn)
        conn.execute(
            f"INSERT INTO daily_reports (created_at, summary, pdf_path, report_json) VALUES ({ph},{ph},{ph},{ph})",
            (created_at, summary, pdf_path, json.dumps(entries)),
        )
        conn.commit()
    finally:
        conn.close()

    latest_messages["DAILY_REPORT"] = {"timestamp": created_at, "report": entries}
    return pdf_path


def _write_daily_pdf(pdf_path, created_at, entries):
    doc = SimpleDocTemplate(pdf_path, pagesize=A4, rightMargin=32, leftMargin=32, topMargin=32, bottomMargin=32)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("Daily Beacon Report", styles["Title"]))
    story.append(Paragraph(f"Generated at: {_paragraph_text(created_at.replace('T',' '))} (Samoa time)", styles["Normal"]))
    story.append(Spacer(1, 12))

    table_data = [[
        Paragraph("Beacon", styles["BodyText"]),
        Paragraph("Status", styles["BodyText"]),
        Paragraph("Last seen", styles["BodyText"]),
        Paragraph("Last device", styles["BodyText"]),
    ]]
    for e in entries:
        table_data.append([
            Paragraph(_paragraph_text(e.get("name") or e.get("id") or ""), styles["BodyText"]),
            Paragraph(_paragraph_text(e.get("status") or ""), styles["BodyText"]),
            Paragraph(_paragraph_text(e.get("last_seen") or ""), styles["BodyText"]),
            Paragraph(_paragraph_text(e.get("last_device") or ""), styles["BodyText"]),
        ])

    table = Table(table_data, colWidths=[160, 70, 160, 140], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("WORDWRAP", (0, 0), (-1, -1), "CJK"),
    ]))

    story.append(table)
    doc.build(story)


def _db_ph(conn):
    return "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"


def _fetch_notifications(conn, where_clause: str, params: tuple):
    query = (
        "SELECT n.type, "
        "COALESCE(bn.name, n.beacon_name, n.beacon_id) AS beacon_name, "
        "n.beacon_id, "
        "n.event_time, "
        "n.distance, "
        "COALESCE(d.name, n.device_ident) AS device_name "
        "FROM notifications n "
        "LEFT JOIN beacon_names bn ON n.beacon_id = bn.id "
        "LEFT JOIN devices d ON n.device_ident = d.id "
        + where_clause
        + " ORDER BY n.id ASC"
    )
    return conn.execute(query, params).fetchall()


def generate_activity_report(beacon_id: str, start_date: str | None = None, end_date: str | None = None, device_idents=None, customer_id=None):
    """Activity report.

    - Show the selected beacon's events for the date range.
    """
    if not beacon_id:
        return None

    start_date = start_date or format_samoa_time(time.time())[:10]
    end_date = end_date or start_date
    start_ts = f"{start_date} 00:00:00"
    end_ts = f"{end_date} 23:59:59"

    conn = get_db()
    try:
        ph = _db_ph(conn)
        params = [beacon_id, start_ts, end_ts]
        where = f"WHERE beacon_id = {ph} AND event_time >= {ph} AND event_time <= {ph}"
        if device_idents is not None:
            if not device_idents:
                rows = []
            else:
                placeholders = ",".join([ph] * len(device_idents))
                where += f" AND n.device_ident IN ({placeholders})"
                params.extend(sorted(device_idents))
                rows = _fetch_notifications(conn, where, tuple(params))
        else:
            rows = _fetch_notifications(conn, where, tuple(params))
    finally:
        conn.close()

    beacon_display = None
    if rows:
        beacon_display = rows[0][1] or beacon_id
    if beacon_display is None:
        conn = get_db()
        try:
            ph = _db_ph(conn)
            row = conn.execute(f"SELECT name FROM beacon_names WHERE id = {ph}", (beacon_id,)).fetchone()
            if row:
                beacon_display = row[0] or beacon_id
        finally:
            conn.close()
    beacon_display = beacon_display or beacon_id

    now = time.time()
    created_at = format_samoa_time(now).replace(" ", "T")
    safe_ts = format_samoa_time(now).replace(":", "-").replace(" ", "_")
    out_dir = _ensure_dir(ACTIVITY_REPORTS_DIR)
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in beacon_id)
    pdf_path = os.path.join(out_dir, f"activity_{safe_name}_{safe_ts}.pdf")

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=landscape(A4),
        rightMargin=24,
        leftMargin=24,
        topMargin=24,
        bottomMargin=24,
    )
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(f"Activity Report - Beacon {_paragraph_text(beacon_display)}", styles["Title"]))
    story.append(Paragraph(f"Range: {_paragraph_text(start_date)} to {_paragraph_text(end_date)}", styles["Normal"]))
    story.append(Spacer(1, 12))

    if not rows:
        story.append(Paragraph("No events for this selection and date range.", styles["Normal"]))
        doc.build(story)
    else:
        data = []
        for ntype, bname, bid, event_time, dist, device_name in rows:
            data.append([
                _format_event_time(event_time),
                (ntype or "").upper(),
                _format_distance(dist),
                device_name or "",
            ])
        story.extend(_build_events_table(styles, ["Time", "Type", "Distance", "Device"], data, [130, 70, 90, 200]))
        doc.build(story)

    summary = f"{len(rows)} events"
    conn = get_db()
    try:
        ph = _db_ph(conn)
        conn.execute(
            f"INSERT INTO activity_reports (beacon_name, created_at, summary, pdf_path, customer_id) VALUES ({ph},{ph},{ph},{ph},{ph})",
            (beacon_display, created_at, summary, pdf_path, customer_id),
        )
        conn.commit()
    finally:
        conn.close()

    return pdf_path


def _build_events_table(styles, headers, rows, col_widths):
    """Return list of flowables for an events table."""
    table_data = [[Paragraph(h, styles["BodyText"]) for h in headers]]
    for row in rows:
        table_data.append([Paragraph(_paragraph_text(cell), styles["BodyText"]) for cell in row])

    t = Table(table_data, colWidths=col_widths, hAlign='LEFT', repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.whitesmoke, colors.white]),
        ('WORDWRAP', (0,0), (-1,-1), 'CJK'),
    ]))

    return [t]


def _format_distance(value):
    try:
        return f"{float(value):.2f} m"
    except Exception:
        return "-"


def _format_event_time(value):
    if not value:
        return ""
    return str(value).replace("T", " ").replace(" UTC", "")


def _daily_loop():
    while True:
        try:
            from services.audit_service import (
                _local_dt_from_unix,
                run_due_customer_audits,
                send_due_customer_deliveries,
            )

            now_local = _local_dt_from_unix()
            run_due_customer_audits(now_local)
            send_due_customer_deliveries(now_local)
            time.sleep(30)
        except Exception as e:
            print(f"[daily-loop] error: {e}")
            time.sleep(60)


def start_daily_thread():
    t = threading.Thread(target=_daily_loop, daemon=True)
    t.start()
    return t


def generate_device_activity_report(device_ident: str, start_date=None, end_date=None, customer_id=None):
    """Generate a device activity PDF from notifications history (filtered by device_ident)."""
    if not device_ident:
        return None

    conn = get_db()
    try:
        ph = _db_ph(conn)
        params = [device_ident]
        time_filters = ""
        if start_date:
            time_filters += f" AND n.event_time >= {ph}"
            params.append(f"{start_date} 00:00:00")
        if end_date:
            time_filters += f" AND n.event_time <= {ph}"
            params.append(f"{end_date} 23:59:59")
        rows = conn.execute(
            "SELECT COALESCE(bn.name, n.beacon_name, n.beacon_id) AS beacon_name, "
            "n.type, n.event_time, n.created_at, n.distance "
            "FROM notifications n "
            "LEFT JOIN beacon_names bn ON n.beacon_id = bn.id "
            f"WHERE n.device_ident = {ph}"
            + time_filters
            + " ORDER BY n.id ASC",
            tuple(params),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    now = time.time()
    created_at = format_samoa_time(now).replace(" ", "T")
    safe_ts = format_samoa_time(now).replace(":", "-").replace(" ", "_")
    out_dir = _ensure_dir(ACTIVITY_REPORTS_DIR)
    safe_dev = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in device_ident)
    pdf_path = os.path.join(out_dir, f"device_{safe_dev}_{safe_ts}.pdf")

    device_display = device_ident
    conn = get_db()
    try:
        ph = _db_ph(conn)
        row = conn.execute(f"SELECT name FROM devices WHERE id = {ph}", (device_ident,)).fetchone()
        if row and row[0]:
            device_display = row[0]
    finally:
        conn.close()

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=landscape(A4),
        rightMargin=24,
        leftMargin=24,
        topMargin=24,
        bottomMargin=24,
    )
    styles = getSampleStyleSheet()
    story = []
    story.append(Paragraph("Device Activity Report", styles["Title"]))
    story.append(Paragraph(f"Device: {_paragraph_text(device_display)}", styles["Normal"]))
    story.append(Paragraph(f"Generated at: {_paragraph_text(created_at.replace('T',' '))} (Samoa time)", styles["Normal"]))
    if start_date or end_date:
        parts = []
        if start_date:
            parts.append(f"from {start_date}")
        if end_date:
            parts.append(f"to {end_date}" if start_date else f"up to {end_date}")
        story.append(Paragraph(_paragraph_text("Date range: " + " ".join(parts)), styles["Normal"]))
    story.append(Spacer(1, 12))

    table_rows = []
    for beacon_name, typ, event_time, created_at2, distance in rows:
        table_rows.append([
            beacon_name or "",
            (typ or "").upper(),
            _format_event_time(event_time),
            _format_distance(distance),
            _format_event_time(created_at2),
        ])

    story.extend(_build_events_table(
        styles,
        ["Beacon", "Type", "Event time", "Distance", "Recorded at"],
        table_rows,
        [180, 70, 150, 90, 150],
    ))
    doc.build(story)

    # store in history table for the Activity Reports page
    summary = f"{len(rows)} events (device)"
    conn = get_db()
    try:
        ph = _db_ph(conn)
        conn.execute(
            f"INSERT INTO activity_reports (beacon_name, created_at, summary, pdf_path, customer_id) VALUES ({ph},{ph},{ph},{ph},{ph})",
            (f"[Device] {device_ident}", created_at, summary, pdf_path, customer_id),
        )
        conn.commit()
    finally:
        conn.close()

    return pdf_path
