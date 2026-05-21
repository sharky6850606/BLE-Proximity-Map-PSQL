import base64
import os
import smtplib
from email.message import EmailMessage

import requests

from config import (
    MAIL_FROM,
    MAIL_PROVIDER,
    SENDGRID_API_KEY,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USERNAME,
    SMTP_USE_SSL,
    SMTP_USE_TLS,
)


def _clean_recipients(to_emails):
    seen = set()
    recipients = []
    for email in to_emails or []:
        cleaned = str(email or "").strip()
        key = cleaned.lower()
        if cleaned and key not in seen:
            recipients.append(cleaned)
            seen.add(key)
    return recipients


def _attachment_bytes(attachment_path):
    if not attachment_path or not os.path.exists(attachment_path):
        return None, None
    with open(attachment_path, "rb") as f:
        return os.path.basename(attachment_path), f.read()


def _send_smtp_email(to_email, subject, body, attachment_path=None):
    sender = MAIL_FROM or SMTP_USERNAME
    if not SMTP_HOST or not SMTP_USERNAME or not SMTP_PASSWORD or not sender:
        print("[email] skipped: configure SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, and MAIL_FROM")
        return False

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body or "")

    filename, content = _attachment_bytes(attachment_path)
    if filename and content is not None:
        msg.add_attachment(
            content,
            maintype="application",
            subtype="pdf",
            filename=filename,
        )

    if SMTP_USE_SSL:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
    try:
        server.ehlo()
        if SMTP_USE_TLS and not SMTP_USE_SSL:
            server.starttls()
            server.ehlo()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)
        return True
    finally:
        server.quit()


def _send_via_smtp(recipients, subject, body, attachment_path=None):
    sent_count = 0
    for recipient in recipients:
        try:
            if _send_smtp_email(recipient, subject, body, attachment_path=attachment_path):
                sent_count += 1
        except Exception as e:
            print(f"[email] smtp failed for {recipient}: {e}")
    if recipients:
        print(f"[email] smtp sent {sent_count}/{len(recipients)}")
    return sent_count > 0


def _send_via_sendgrid(recipients, subject, body, attachment_path=None):
    if not SENDGRID_API_KEY or not MAIL_FROM:
        print("[email] skipped: configure SENDGRID_API_KEY and MAIL_FROM")
        return False

    payload = {
        "personalizations": [{"to": [{"email": e} for e in recipients]}],
        "from": {"email": MAIL_FROM},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }

    filename, content = _attachment_bytes(attachment_path)
    if filename and content is not None:
        payload["attachments"] = [{
            "content": base64.b64encode(content).decode("ascii"),
            "filename": filename,
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
        print(f"[email] sendgrid failed: {e}")
        return False


def send_report_email(to_emails, subject, body, attachment_path=None):
    """Send a report email if a provider is configured.

    SMTP is the default provider for Gmail/Workspace app-password delivery.
    Returns True when at least one intended recipient was sent successfully.
    """
    recipients = _clean_recipients(to_emails)
    if not recipients:
        return False

    provider = MAIL_PROVIDER or "smtp"
    if provider == "sendgrid":
        return _send_via_sendgrid(recipients, subject, body, attachment_path=attachment_path)
    if provider == "smtp":
        return _send_via_smtp(recipients, subject, body, attachment_path=attachment_path)

    print(f"[email] skipped: unsupported MAIL_PROVIDER={provider}")
    return False
