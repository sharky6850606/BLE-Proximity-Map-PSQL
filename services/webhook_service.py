import requests

from config import N8N_WEBHOOK_SECRET, N8N_WEBHOOK_TIMEOUT, N8N_WHATSAPP_WEBHOOK_URL


def send_whatsapp_audit_webhook(payload):
    """Post a daily audit payload to n8n and return a delivery result."""
    if not N8N_WHATSAPP_WEBHOOK_URL:
        return {
            "status": "skipped",
            "http_status": None,
            "error": "N8N_WHATSAPP_WEBHOOK_URL is not configured",
            "webhook_url": "",
        }

    headers = {"Content-Type": "application/json"}
    if N8N_WEBHOOK_SECRET:
        headers["X-Webhook-Secret"] = N8N_WEBHOOK_SECRET

    try:
        response = requests.post(
            N8N_WHATSAPP_WEBHOOK_URL,
            json=payload,
            headers=headers,
            timeout=N8N_WEBHOOK_TIMEOUT,
        )
        if response.status_code >= 300:
            return {
                "status": "failed",
                "http_status": response.status_code,
                "error": response.text[:500],
                "webhook_url": N8N_WHATSAPP_WEBHOOK_URL,
            }
        return {
            "status": "sent",
            "http_status": response.status_code,
            "error": "",
            "webhook_url": N8N_WHATSAPP_WEBHOOK_URL,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "http_status": None,
            "error": str(exc)[:500],
            "webhook_url": N8N_WHATSAPP_WEBHOOK_URL,
        }
