import threading
import time

from config import FMC_OFFLINE_ALERT_AFTER_SECONDS, FMC_OFFLINE_ALERT_CHECK_SECONDS
from database import get_db
from services.beacon_logic import format_samoa_time
from services.email_service import send_report_email_with_results
from services.webhook_service import send_whatsapp_webhook


def _ph(conn):
    return "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"


def _split_recipients(value):
    recipients = []
    seen = set()
    for part in str(value or "").replace(";", ",").split(","):
        recipient = part.strip()
        if recipient and recipient not in seen:
            recipients.append(recipient)
            seen.add(recipient)
    return recipients


def _duration_label(seconds):
    seconds = max(0, int(seconds or 0))
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''}"


def _device_label(label, setting_name, global_name, ident):
    return str(setting_name or label or global_name or ident)


def _insert_webhook_log(conn, customer_id, recipients, result):
    ph = _ph(conn)
    conn.execute(
        f"INSERT INTO webhook_logs "
        f"(created_at, audit_run_id, customer_id, webhook_url, recipients, status, http_status, error, send_type, actor_user_id, actor_email) "
        f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
        (
            format_samoa_time(time.time()),
            None,
            customer_id,
            result.get("webhook_url") or "",
            ", ".join(recipients),
            result.get("status"),
            result.get("http_status"),
            result.get("error") or "",
            "fmc_offline",
            None,
            None,
        ),
    )


def _customer_email_recipients(conn, customer_id):
    ph = _ph(conn)
    rows = conn.execute(
        f"SELECT email FROM app_users WHERE customer_id={ph} AND active=1 "
        f"AND deleted_at IS NULL ORDER BY role, email",
        (customer_id,),
    ).fetchall()
    return [str(row[0]).strip() for row in rows if row and row[0] and str(row[0]).strip()]


def _insert_email_log(conn, customer_id, recipient, subject, result, send_type="fmc_offline"):
    ph = _ph(conn)
    conn.execute(
        f"INSERT INTO email_logs "
        f"(created_at, audit_run_id, customer_id, recipient, subject, provider, status, error, "
        f"attachment_path, send_type, actor_user_id, actor_email) "
        f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
        (
            format_samoa_time(time.time()),
            None,
            customer_id,
            recipient,
            subject,
            result.get("provider"),
            result.get("status"),
            result.get("error") or "",
            None,
            send_type,
            None,
            None,
        ),
    )


def _send_recovery_emails(conn, cutoff):
    """Email customers once when an offline device starts reporting again."""
    ph = _ph(conn)
    rows = conn.execute(
        f"SELECT foa.id, foa.customer_id, foa.device_ident, foa.offline_since_ts, "
        f"c.name, cd.label, cds.name, d.name, ds.last_seen_ts "
        f"FROM fmc_offline_alerts foa "
        f"JOIN customers c ON c.id=foa.customer_id "
        f"JOIN customer_devices cd ON cd.customer_id=foa.customer_id AND cd.device_ident=foa.device_ident "
        f"LEFT JOIN customer_device_settings cds ON cds.customer_id=foa.customer_id AND cds.device_ident=foa.device_ident "
        f"LEFT JOIN devices d ON d.id=foa.device_ident "
        f"JOIN device_states ds ON ds.device_ident=foa.device_ident "
        f"WHERE c.active=1 AND ds.last_seen_ts IS NOT NULL AND ds.last_seen_ts>{ph} "
        f"ORDER BY foa.customer_id, foa.device_ident",
        (cutoff,),
    ).fetchall()

    sent = 0
    for alert_id, customer_id, device_ident, offline_since_ts, customer_name, label, setting_name, global_name, recovered_ts in rows:
        site_name = _device_label(label, setting_name, global_name, device_ident)
        recovered_label = format_samoa_time(recovered_ts)
        offline_label = format_samoa_time(offline_since_ts) if offline_since_ts else "Unknown"
        outage_seconds = (
            max(0, int(recovered_ts) - int(offline_since_ts))
            if recovered_ts is not None and offline_since_ts is not None
            else 0
        )
        body = (
            f"Device online again for {customer_name}\n"
            f"Site: {site_name}\n"
            f"Device IMEI: {device_ident}\n"
            f"Offline since: {offline_label} Samoa time\n"
            f"Reconnected: {recovered_label} Samoa time\n"
            f"Approximate outage: {_duration_label(outage_seconds)}\n\n"
            "Monitoring has resumed. Please confirm the device and beacon data are updating normally."
        )
        subject = f"Device online again - {site_name}"
        recipients = _customer_email_recipients(conn, customer_id)
        results = send_report_email_with_results(recipients, subject, body)
        if not results:
            _insert_email_log(
                conn,
                customer_id,
                "",
                subject,
                {"provider": "smtp", "status": "skipped", "error": "No active customer recipients"},
                send_type="fmc_online",
            )
        for result in results:
            _insert_email_log(
                conn,
                customer_id,
                result.get("recipient") or "",
                subject,
                result,
                send_type="fmc_online",
            )
        sent += int(any(result.get("status") == "sent" for result in results))
        conn.execute(f"DELETE FROM fmc_offline_alerts WHERE id={ph}", (alert_id,))
    return sent


def _build_payload(customer_id, customer_name, customer_slug, recipients, device_ident, site_name, last_seen_ts):
    offline_since = format_samoa_time(last_seen_ts) if last_seen_ts else "No heartbeat recorded"
    offline_seconds = int(time.time()) - int(last_seen_ts) if last_seen_ts else FMC_OFFLINE_ALERT_AFTER_SECONDS
    return {
        "event": "fmc_offline",
        "version": 1,
        "customer": {
            "id": customer_id,
            "name": customer_name,
            "slug": customer_slug,
        },
        "whatsapp": {
            "recipients": recipients,
            "template_name": "ble_fmc_offline",
            "language": "en",
        },
        "device": {
            "device_ident": str(device_ident),
            "device_name": site_name,
            "last_heartbeat": offline_since,
            "offline_for": _duration_label(offline_seconds),
            "offline_seconds": offline_seconds,
        },
        "message": {
            "title": f"FMC/site offline - {site_name}",
            "body": (
                f"FMC/site offline alert for {customer_name}\n"
                f"Site: {site_name}\n"
                f"Device IMEI: {device_ident}\n"
                f"Last heartbeat: {offline_since} Samoa time\n"
                f"Offline for: {_duration_label(offline_seconds)}\n\n"
                "Please check the FMC power, SIM/data connection, and device installation."
            ),
        },
    }


def check_and_send_fmc_offline_alerts():
    """Send customer email alerts for device outages and recoveries."""
    now_ts = int(time.time())
    cutoff = now_ts - int(FMC_OFFLINE_ALERT_AFTER_SECONDS)
    conn = get_db()
    try:
        ph = _ph(conn)

        sent = _send_recovery_emails(conn, cutoff)

        rows = conn.execute(
            f"SELECT c.id, c.name, c.slug, c.whatsapp_recipients, cd.device_ident, cd.label, "
            f"cds.name, d.name, ds.last_seen_ts "
            f"FROM customer_devices cd "
            f"JOIN customers c ON c.id = cd.customer_id "
            f"LEFT JOIN customer_device_settings cds ON cds.customer_id = cd.customer_id AND cds.device_ident = cd.device_ident "
            f"LEFT JOIN devices d ON d.id = cd.device_ident "
            f"LEFT JOIN device_states ds ON ds.device_ident = cd.device_ident "
            f"LEFT JOIN fmc_offline_alerts foa ON foa.customer_id = cd.customer_id AND foa.device_ident = cd.device_ident "
            f"WHERE c.active = 1 "
            f"AND foa.id IS NULL "
            f"AND (ds.last_seen_ts IS NULL OR ds.last_seen_ts <= {ph}) "
            f"ORDER BY c.id, cd.device_ident",
            (cutoff,),
        ).fetchall()

        for customer_id, customer_name, slug, recipient_text, device_ident, label, setting_name, global_name, last_seen_ts in rows:
            recipients = _split_recipients(recipient_text)
            site_name = _device_label(label, setting_name, global_name, device_ident)
            payload = _build_payload(
                customer_id,
                customer_name,
                slug,
                recipients,
                device_ident,
                site_name,
                int(last_seen_ts) if last_seen_ts is not None else None,
            )
            email_recipients = _customer_email_recipients(conn, customer_id)
            subject = f"Device offline - {site_name}"
            email_results = send_report_email_with_results(
                email_recipients,
                subject,
                payload["message"]["body"],
            )
            if not email_results:
                _insert_email_log(
                    conn,
                    customer_id,
                    "",
                    subject,
                    {"provider": "smtp", "status": "skipped", "error": "No active customer recipients"},
                )
            for email_result in email_results:
                _insert_email_log(
                    conn,
                    customer_id,
                    email_result.get("recipient") or "",
                    subject,
                    email_result,
                )

            whatsapp_result = None
            if recipients:
                whatsapp_result = send_whatsapp_webhook(payload)
                _insert_webhook_log(conn, customer_id, recipients, whatsapp_result)

            delivered = any(result.get("status") == "sent" for result in email_results)
            delivered = delivered or bool(whatsapp_result and whatsapp_result.get("status") == "sent")
            attempted_at = format_samoa_time(time.time())
            conn.execute(
                f"INSERT INTO fmc_offline_alerts (customer_id, device_ident, offline_since_ts, alert_sent_at, recovered_at) "
                f"VALUES ({ph},{ph},{ph},{ph},NULL) "
                f"ON CONFLICT(customer_id, device_ident) DO UPDATE SET "
                f"offline_since_ts=excluded.offline_since_ts, alert_sent_at=excluded.alert_sent_at, recovered_at=NULL",
                (
                    customer_id,
                    str(device_ident),
                    int(last_seen_ts) if last_seen_ts is not None else None,
                    attempted_at,
                ),
            )
            sent += int(delivered)

        conn.commit()
        return sent
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _fmc_offline_loop():
    while True:
        try:
            check_and_send_fmc_offline_alerts()
            time.sleep(max(30, int(FMC_OFFLINE_ALERT_CHECK_SECONDS)))
        except Exception as exc:
            print(f"[fmc-offline] loop error: {exc}")
            time.sleep(60)


def start_fmc_offline_alert_thread():
    t = threading.Thread(target=_fmc_offline_loop, daemon=True)
    t.start()
    print("[fmc-offline] thread started")
