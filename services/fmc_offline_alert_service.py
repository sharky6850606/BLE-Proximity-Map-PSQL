import threading
import time

from config import FMC_OFFLINE_ALERT_AFTER_SECONDS, FMC_OFFLINE_ALERT_CHECK_SECONDS
from database import get_db
from services.beacon_logic import format_samoa_time
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
    """Send one WhatsApp alert per customer/device offline incident."""
    now_ts = int(time.time())
    cutoff = now_ts - int(FMC_OFFLINE_ALERT_AFTER_SECONDS)
    conn = get_db()
    try:
        ph = _ph(conn)

        # Clear incident locks when a device is healthy again.
        conn.execute(
            f"DELETE FROM fmc_offline_alerts "
            f"WHERE EXISTS ("
            f"  SELECT 1 FROM device_states ds "
            f"  WHERE ds.device_ident = fmc_offline_alerts.device_ident "
            f"  AND ds.last_seen_ts IS NOT NULL AND ds.last_seen_ts > {ph}"
            f")",
            (cutoff,),
        )

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
            f"AND COALESCE(c.whatsapp_recipients, '') <> '' "
            f"AND foa.id IS NULL "
            f"AND (ds.last_seen_ts IS NULL OR ds.last_seen_ts <= {ph}) "
            f"ORDER BY c.id, cd.device_ident",
            (cutoff,),
        ).fetchall()

        sent = 0
        for customer_id, customer_name, slug, recipient_text, device_ident, label, setting_name, global_name, last_seen_ts in rows:
            recipients = _split_recipients(recipient_text)
            if not recipients:
                continue
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
            result = send_whatsapp_webhook(payload)
            _insert_webhook_log(conn, customer_id, recipients, result)
            if result.get("status") == "sent":
                conn.execute(
                    f"INSERT INTO fmc_offline_alerts (customer_id, device_ident, offline_since_ts, alert_sent_at, recovered_at) "
                    f"VALUES ({ph},{ph},{ph},{ph},NULL) "
                    f"ON CONFLICT(customer_id, device_ident) DO UPDATE SET "
                    f"offline_since_ts=excluded.offline_since_ts, alert_sent_at=excluded.alert_sent_at, recovered_at=NULL",
                    (
                        customer_id,
                        str(device_ident),
                        int(last_seen_ts) if last_seen_ts is not None else None,
                        format_samoa_time(time.time()),
                    ),
                )
                sent += 1

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
