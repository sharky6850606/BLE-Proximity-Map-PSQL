import os
import json
import time
import threading

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

from database import get_db
from services.beacon_logic import latest_messages, format_samoa_time
from config import REPORTS_DIR, ACTIVITY_REPORTS_DIR


def _ensure_dir(path: str) -> str:
    ab = os.path.abspath(path)
    os.makedirs(ab, exist_ok=True)
    return ab


def generate_daily_report():
    conn = get_db()
    try:
        rows = conn.execute("SELECT id, name FROM beacon_names ORDER BY id").fetchall()
        beacon_list = [(r[0], r[1]) for r in rows]
    finally:
        conn.close()

    entries = []
    for bid, bname in beacon_list:
        status = "Offline"
        last_seen = ""
        last_device = ""
        for dev_id, snap in latest_messages.items():
            if dev_id == "DAILY_REPORT" or not isinstance(snap, dict):
                continue
            for b in (snap.get("beacons") or []):
                if b.get("id") == bid:
                    status = "Online"
                    last_seen = b.get("last_seen") or ""
                    last_device = dev_id
                    break
        entries.append({
            "id": bid,
            "name": bname or "",
            "status": status,
            "last_seen": last_seen,
            "last_device": last_device,
        })

    now = time.time()
    created_at = format_samoa_time(now).replace(" ", "T")
    safe_ts = format_samoa_time(now).replace(":", "-").replace(" ", "_")
    out_dir = _ensure_dir(REPORTS_DIR)
    pdf_path = os.path.join(out_dir, f"report_{safe_ts}.pdf")
    _write_daily_pdf(pdf_path, created_at, entries)

    summary = f"{len(entries)} beacons"
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO daily_reports (created_at, summary, pdf_path, report_json) VALUES (%s,%s,%s,%s)",
            (created_at, summary, pdf_path, json.dumps(entries)),
        )
        conn.commit()
    finally:
        conn.close()

    latest_messages["DAILY_REPORT"] = {"timestamp": created_at, "report": entries}
    return pdf_path


def _write_daily_pdf(pdf_path, created_at, entries):
    c = canvas.Canvas(pdf_path, pagesize=A4)
    w, h = A4
    margin = 50
    y = h - margin

    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin, y, "Daily Beacon Report")
    y -= 20
    c.setFont("Helvetica", 10)
    c.drawString(margin, y, f"Generated at: {created_at.replace('T',' ')} (Samoa time)")
    y -= 12
    c.line(margin, y, w - margin, y)
    y -= 16

    c.setFont("Helvetica-Bold", 10)
    headers = ["Beacon ID", "Name", "Status", "Last seen", "Last device"]
    colx = [margin, margin + 130, margin + 270, margin + 350, margin + 470]
    for x, hdr in zip(colx, headers):
        c.drawString(x, y, hdr)
    y -= 12
    c.line(margin, y, w - margin, y)
    y -= 10

    c.setFont("Helvetica", 9)
    for e in entries:
        if y < 60:
            c.showPage()
            y = h - margin
            c.setFont("Helvetica-Bold", 10)
            for x, hdr in zip(colx, headers):
                c.drawString(x, y, hdr)
            y -= 12
            c.line(margin, y, w - margin, y)
            y -= 10
            c.setFont("Helvetica", 9)

        c.drawString(colx[0], y, str(e.get("id") or ""))
        c.drawString(colx[1], y, str(e.get("name") or ""))
        c.drawString(colx[2], y, str(e.get("status") or ""))
        c.drawString(colx[3], y, str(e.get("last_seen") or ""))
        c.drawString(colx[4], y, str(e.get("last_device") or ""))
        y -= 12

    c.save()


def generate_activity_report(beacon_key: str, start_date=None, end_date=None):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT type, event_time, created_at, distance, device_ident FROM notifications "
            "WHERE beacon_name = %s AND type IN ('in','left')"
            + (" AND event_time >= %s" if start_date else "")
            + (" AND event_time <= %s" if end_date else "")
            + " ORDER BY id ASC",
            tuple([beacon_key] + ([f"{start_date} 00:00:00"] if start_date else []) + ([f"{end_date} 23:59:59"] if end_date else [])),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    now = time.time()
    created_at = format_samoa_time(now).replace(" ", "T")
    safe_ts = format_samoa_time(now).replace(":", "-").replace(" ", "_")
    out_dir = _ensure_dir(ACTIVITY_REPORTS_DIR)

    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in beacon_key)
    pdf_path = os.path.join(out_dir, f"activity_{safe_name}_{safe_ts}.pdf")

    _write_activity_pdf(pdf_path, created_at, beacon_key, rows)

    summary = f"{len(rows)} events"
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO activity_reports (beacon_name, created_at, summary, pdf_path) VALUES (%s,%s,%s,%s)",
            (beacon_key, created_at, summary, pdf_path),
        )
        conn.commit()
    finally:
        conn.close()

    return pdf_path


def _write_activity_pdf(pdf_path, created_at, beacon_key, rows):
    c = canvas.Canvas(pdf_path, pagesize=A4)
    w, h = A4
    margin = 50
    y = h - margin

    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin, y, "Beacon Activity Report")
    y -= 20
    c.setFont("Helvetica-Bold", 11)
    c.drawString(margin, y, f"Beacon: {beacon_key}")
    y -= 12
    c.setFont("Helvetica", 9)
    c.drawString(margin, y, f"Generated at: {created_at.replace('T',' ')} (Samoa time)")
    y -= 12
    c.line(margin, y, w - margin, y)
    y -= 16

    c.setFont("Helvetica-Bold", 10)
    headers = ["Type", "Event time", "Distance (m)", "Recorded at"]
    colx = [margin, margin + 80, margin + 250, margin + 360]
    for x, hdr in zip(colx, headers):
        c.drawString(x, y, hdr)
    y -= 12
    c.line(margin, y, w - margin, y)
    y -= 10

    c.setFont("Helvetica", 9)
    for typ, event_time, created_at2, distance, _dev in rows:
        if y < 60:
            c.showPage()
            y = h - margin
            c.setFont("Helvetica-Bold", 10)
            for x, hdr in zip(colx, headers):
                c.drawString(x, y, hdr)
            y -= 12
            c.line(margin, y, w - margin, y)
            y -= 10
            c.setFont("Helvetica", 9)

        c.drawString(colx[0], y, str(typ or "").upper())
        c.drawString(colx[1], y, str(event_time or "").replace("T", " "))
        c.drawString(colx[2], y, f"{distance:.2f}" if distance is not None else "-")
        c.drawString(colx[3], y, str(created_at2 or "").replace("T", " "))
        y -= 12

    c.save()


def _daily_loop():
    while True:
        try:
            label = format_samoa_time(time.time())
            hour = int(label[11:13])
            minute = int(label[14:16])
            if hour == 22 and minute == 0:
                generate_daily_report()
                time.sleep(60)
            time.sleep(30)
        except Exception:
            time.sleep(60)


def start_daily_thread():
    t = threading.Thread(target=_daily_loop, daemon=True)
    t.start()
    return t


def generate_device_activity_report(device_ident: str, start_date=None, end_date=None):
    """Generate a device activity PDF from notifications history (filtered by device_ident)."""
    if not device_ident:
        return None

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT beacon_name, type, event_time, created_at, distance FROM notifications "
            "WHERE device_ident = %s"
            + (" AND event_time >= %s" if start_date else "")
            + (" AND event_time <= %s" if end_date else "")
            + " ORDER BY id ASC",
            tuple([device_ident] + ([f"{start_date} 00:00:00"] if start_date else []) + ([f"{end_date} 23:59:59"] if end_date else [])),
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

    c = canvas.Canvas(pdf_path, pagesize=A4)
    w, h = A4
    margin = 50
    y = h - margin

    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin, y, "Device Activity Report")
    y -= 20
    c.setFont("Helvetica-Bold", 11)
    c.drawString(margin, y, f"Device: {device_ident}")
    y -= 12
    c.setFont("Helvetica", 9)
    c.drawString(margin, y, f"Generated at: {created_at.replace('T',' ')} (Samoa time)")
    y -= 12

    if start_date or end_date:
        parts = []
        if start_date:
            parts.append(f"from {start_date}")
        if end_date:
            parts.append(f"to {end_date}" if start_date else f"up to {end_date}")
        c.drawString(margin, y, "Date range: " + " ".join(parts))
        y -= 12

    c.line(margin, y, w - margin, y)
    y -= 16

    c.setFont("Helvetica-Bold", 10)
    headers = ["Beacon", "Type", "Event time", "Distance (m)", "Recorded at"]
    colx = [margin, margin + 140, margin + 210, margin + 320, margin + 420]
    for x, hdr in zip(colx, headers):
        c.drawString(x, y, hdr)
    y -= 12
    c.line(margin, y, w - margin, y)
    y -= 10

    c.setFont("Helvetica", 9)
    for beacon_name, typ, event_time, created_at2, distance in rows:
        if y < 60:
            c.showPage()
            y = h - margin
            c.setFont("Helvetica-Bold", 10)
            for x, hdr in zip(colx, headers):
                c.drawString(x, y, hdr)
            y -= 12
            c.line(margin, y, w - margin, y)
            y -= 10
            c.setFont("Helvetica", 9)

        c.drawString(colx[0], y, str(beacon_name or ""))
        c.drawString(colx[1], y, str(typ or "").upper())
        c.drawString(colx[2], y, str(event_time or "").replace("T", " "))
        c.drawString(colx[3], y, f"{distance:.2f}" if distance is not None else "-")
        c.drawString(colx[4], y, str(created_at2 or "").replace("T", " "))
        y -= 12

    c.save()

    # store in history table for the Activity Reports page
    summary = f"{len(rows)} events (device)"
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO activity_reports (beacon_name, created_at, summary, pdf_path) VALUES (%s,%s,%s,%s)",
            (f"[Device] {device_ident}", created_at, summary, pdf_path),
        )
        conn.commit()
    finally:
        conn.close()

    return pdf_path
