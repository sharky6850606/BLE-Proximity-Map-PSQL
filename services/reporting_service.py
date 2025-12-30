import os
import time
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from database import get_db
from services.beacon_logic import format_samoa_time


REPORTS_DIR = os.getenv("REPORTS_DIR", "reports")
ACTIVITY_REPORTS_DIR = os.getenv("ACTIVITY_REPORTS_DIR", "activity_reports")


def start_daily_thread():
    # Daily thread intentionally disabled on Render Free
    return


def generate_daily_report():
    os.makedirs(REPORTS_DIR, exist_ok=True)

    ts = format_samoa_time(time.time()).replace(":", "-")
    pdf_path = os.path.join(REPORTS_DIR, f"daily_report_{ts}.pdf")

    c = canvas.Canvas(pdf_path, pagesize=A4)
    text = c.beginText(40, 800)
    text.setFont("Helvetica", 10)

    text.textLine("Daily BLE Proximity Report")
    text.textLine("")
    text.textLine(f"Generated at: {format_samoa_time(time.time())}")
    text.textLine("")

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT device_ident, beacon_name, type, event_time
            FROM notifications
            ORDER BY created_at DESC
            LIMIT 500
            """
        ).fetchall()

        current_device = None
        for device, beacon, typ, ev_time in rows:
            if device != current_device:
                text.textLine("")
                text.textLine(f"Device: {device}")
                current_device = device

            text.textLine(f"  • {beacon} — {typ.upper()} @ {ev_time}")

    finally:
        conn.close()

    c.drawText(text)
    c.showPage()
    c.save()

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO daily_reports (created_at, summary, pdf_path) VALUES (%s,%s,%s)",
            (format_samoa_time(time.time()), "Auto daily report", pdf_path),
        )
        conn.commit()
    finally:
        conn.close()

    return pdf_path


def generate_activity_report(beacon_id, start_date=None, end_date=None):
    os.makedirs(ACTIVITY_REPORTS_DIR, exist_ok=True)

    ts = format_samoa_time(time.time()).replace(":", "-")
    pdf_path = os.path.join(ACTIVITY_REPORTS_DIR, f"beacon_{beacon_id}_{ts}.pdf")

    c = canvas.Canvas(pdf_path, pagesize=A4)
    text = c.beginText(40, 800)
    text.setFont("Helvetica", 10)

    text.textLine(f"Beacon Activity Report: {beacon_id}")
    text.textLine("")
    text.textLine(f"Generated at: {format_samoa_time(time.time())}")
    text.textLine("")

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT type, event_time
            FROM notifications
            WHERE beacon_name = %s
            ORDER BY created_at
            """,
            (beacon_id,),
        ).fetchall()

        for typ, ev_time in rows:
            text.textLine(f"• {typ.upper()} @ {ev_time}")

    finally:
        conn.close()

    c.drawText(text)
    c.showPage()
    c.save()

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO activity_reports (beacon_name, created_at, summary, pdf_path) VALUES (%s,%s,%s,%s)",
            (beacon_id, format_samoa_time(time.time()), "Beacon activity report", pdf_path),
        )
        conn.commit()
    finally:
        conn.close()

    return pdf_path
