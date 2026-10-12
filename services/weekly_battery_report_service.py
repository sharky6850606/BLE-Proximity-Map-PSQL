import time
from datetime import datetime, timedelta, timezone

from config import (
    SAMOA_OFFSET_HOURS,
    WEEKLY_BATTERY_LOW_PERCENT,
    WEEKLY_BATTERY_REPORT_HOUR,
    WEEKLY_BATTERY_REPORT_MINUTE,
)
from database import get_db
from services.beacon_logic import format_samoa_time
from services.email_service import send_report_email_with_results


def _ph(conn):
    return "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"


def _local_now():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=SAMOA_OFFSET_HOURS)


def _customer_recipients(conn, customer_id):
    ph = _ph(conn)
    rows = conn.execute(
        f"SELECT email FROM app_users WHERE customer_id={ph} AND active=1 "
        f"AND deleted_at IS NULL ORDER BY role, email",
        (customer_id,),
    ).fetchall()
    return [str(row[0]).strip() for row in rows if row and row[0] and str(row[0]).strip()]


def _battery_rows(conn, customer_id):
    ph = _ph(conn)
    return conn.execute(
        f"SELECT ca.beacon_id, COALESCE(ca.name, cbn.name, ca.beacon_id), "
        f"ca.expected_device_ident, COALESCE(cds.name, cd.label, d.name, ca.expected_device_ident), "
        f"bs.last_battery_percent, bs.last_battery_voltage, bs.last_seen_ts, bs.device_ident "
        f"FROM customer_assets ca "
        f"LEFT JOIN customer_beacon_names cbn ON cbn.customer_id=ca.customer_id AND cbn.beacon_id=ca.beacon_id "
        f"LEFT JOIN customer_devices cd ON cd.customer_id=ca.customer_id AND cd.device_ident=ca.expected_device_ident "
        f"LEFT JOIN customer_device_settings cds ON cds.customer_id=ca.customer_id AND cds.device_ident=ca.expected_device_ident "
        f"LEFT JOIN devices d ON d.id=ca.expected_device_ident "
        f"LEFT JOIN beacon_states bs ON bs.beacon_key=("
        f"  SELECT bs2.beacon_key FROM beacon_states bs2 WHERE bs2.beacon_id=ca.beacon_id "
        f"  ORDER BY COALESCE(bs2.last_seen_ts, 0) DESC LIMIT 1"
        f") "
        f"WHERE ca.customer_id={ph} AND ca.active=1 "
        f"ORDER BY COALESCE(cds.name, cd.label, d.name, ca.expected_device_ident, ''), "
        f"COALESCE(ca.name, cbn.name, ca.beacon_id)",
        (customer_id,),
    ).fetchall()


def _battery_label(percent, voltage):
    parts = []
    if percent is not None:
        parts.append(f"{int(percent)}%")
    if voltage is not None:
        parts.append(f"{float(voltage):.2f}V")
    return " / ".join(parts) if parts else "Unknown"


def _build_report_body(customer_name, week_start, rows):
    low_count = sum(
        1 for row in rows if row[4] is not None and int(row[4]) <= WEEKLY_BATTERY_LOW_PERCENT
    )
    unknown_count = sum(1 for row in rows if row[4] is None and row[5] is None)
    lines = [
        f"Weekly Beacon Battery Report - {customer_name}",
        f"Week beginning: {week_start}",
        f"Generated: {format_samoa_time(time.time())} Samoa time",
        "",
        f"Assigned beacons: {len(rows)}",
        f"Low battery ({WEEKLY_BATTERY_LOW_PERCENT}% or below): {low_count}",
        f"Battery level unavailable: {unknown_count}",
        "",
    ]
    if not rows:
        lines.append("No active beacons are assigned to this customer.")
        return "\n".join(lines)

    current_site = None
    for beacon_id, beacon_name, expected_ident, site_name, percent, voltage, last_seen_ts, seen_ident in rows:
        site = str(site_name or expected_ident or "Any assigned site")
        if site != current_site:
            if current_site is not None:
                lines.append("")
            lines.append(f"Site: {site}")
            current_site = site
        battery = _battery_label(percent, voltage)
        warning = " - LOW BATTERY" if percent is not None and int(percent) <= WEEKLY_BATTERY_LOW_PERCENT else ""
        last_seen = format_samoa_time(last_seen_ts) if last_seen_ts else "Never reported"
        detected_site = f"; last device {seen_ident}" if seen_ident and str(seen_ident) != str(expected_ident or "") else ""
        lines.append(
            f"- {beacon_name} ({beacon_id}): {battery}{warning}; last seen {last_seen}{detected_site}"
        )
    lines.extend([
        "",
        "Please inspect or replace low batteries and check any beacon with an unknown battery level.",
    ])
    return "\n".join(lines)


def _insert_email_log(conn, customer_id, recipient, subject, result):
    ph = _ph(conn)
    conn.execute(
        f"INSERT INTO email_logs "
        f"(created_at, audit_run_id, customer_id, recipient, subject, provider, status, error, "
        f"attachment_path, send_type, actor_user_id, actor_email) "
        f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
        (
            format_samoa_time(time.time()), None, customer_id, recipient, subject,
            result.get("provider"), result.get("status"), result.get("error") or "",
            None, "weekly_battery", None, None,
        ),
    )


def send_due_weekly_battery_reports(now_local=None):
    """Send one consolidated battery email per active customer each Samoa week."""
    now_local = now_local or _local_now()
    week_start_dt = (now_local - timedelta(days=now_local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    due_at = week_start_dt.replace(
        hour=WEEKLY_BATTERY_REPORT_HOUR,
        minute=WEEKLY_BATTERY_REPORT_MINUTE,
    )
    if now_local < due_at:
        return 0

    week_start = week_start_dt.strftime("%Y-%m-%d")
    scheduled_for = due_at.strftime("%Y-%m-%d %H:%M:%S")
    attempted_at = format_samoa_time(time.time())
    conn = get_db()
    try:
        ph = _ph(conn)
        customers = conn.execute(
            "SELECT id, name FROM customers WHERE active=1 ORDER BY id"
        ).fetchall()
        pending = []
        for customer_id, customer_name in customers:
            existing = conn.execute(
                f"SELECT id FROM weekly_battery_reports WHERE customer_id={ph} AND week_start={ph}",
                (customer_id, week_start),
            ).fetchone()
            if existing:
                continue
            rows = _battery_rows(conn, customer_id)
            recipients = _customer_recipients(conn, customer_id)
            conn.execute(
                f"INSERT INTO weekly_battery_reports "
                f"(customer_id, week_start, scheduled_for, attempted_at, beacon_count, status) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph})",
                (customer_id, week_start, scheduled_for, attempted_at, len(rows), "sending"),
            )
            report_id = conn.execute(
                f"SELECT id FROM weekly_battery_reports WHERE customer_id={ph} AND week_start={ph}",
                (customer_id, week_start),
            ).fetchone()[0]
            pending.append((report_id, customer_id, customer_name, rows, recipients))
        conn.commit()
    finally:
        conn.close()

    sent_total = 0
    for report_id, customer_id, customer_name, rows, recipients in pending:
        subject = f"Weekly Beacon Battery Report - {customer_name} - {week_start}"
        body = _build_report_body(customer_name, week_start, rows)
        results = send_report_email_with_results(recipients, subject, body)
        conn = get_db()
        try:
            ph = _ph(conn)
            for result in results:
                _insert_email_log(conn, customer_id, result.get("recipient") or "", subject, result)
            if not results:
                _insert_email_log(
                    conn, customer_id, "", subject,
                    {"provider": "smtp", "status": "skipped", "error": "No active customer recipients"},
                )
            sent_count = sum(1 for result in results if result.get("status") == "sent")
            sent_total += sent_count
            status = "sent" if sent_count else "failed" if results else "skipped"
            sent_at = format_samoa_time(time.time()) if sent_count else None
            conn.execute(
                f"UPDATE weekly_battery_reports SET status={ph}, sent_at={ph} WHERE id={ph}",
                (status, sent_at, report_id),
            )
            conn.commit()
        finally:
            conn.close()
    return sent_total
