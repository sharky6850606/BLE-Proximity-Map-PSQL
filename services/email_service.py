import base64
import os

import requests

from config import MAIL_FROM, MAIL_PROVIDER, SENDGRID_API_KEY


def send_report_email(to_emails, subject, body, attachment_path=None):
    """Send a report email if a provider is configured.

    Returns True when sent, False when email is skipped or fails. Skipping is
    intentional for local/dev environments that do not have email credentials.
    """
    recipients = [e.strip() for e in (to_emails or []) if e and e.strip()]
    if not recipients:
        return False

    if MAIL_PROVIDER != "sendgrid" or not SENDGRID_API_KEY or not MAIL_FROM:
        print("[email] skipped: configure MAIL_PROVIDER=sendgrid, SENDGRID_API_KEY, and MAIL_FROM")
        return False

    payload = {
        "personalizations": [{"to": [{"email": e} for e in recipients]}],
        "from": {"email": MAIL_FROM},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }

    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        payload["attachments"] = [{
            "content": encoded,
            "filename": os.path.basename(attachment_path),
            "type": "application/pdf",
            "disposition": "attachment",
        }]

    try:
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if resp.status_code >= 300:
            print(f"[email] sendgrid failed {resp.status_code}: {resp.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[email] failed: {e}")
        return False
