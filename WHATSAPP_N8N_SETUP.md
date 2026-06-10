# n8n WhatsApp Audit Webhook

## Render environment variables

- `N8N_WHATSAPP_WEBHOOK_URL`: Production n8n webhook URL.
- `N8N_WEBHOOK_SECRET`: A long random shared secret.
- `N8N_WEBHOOK_TIMEOUT`: Optional timeout in seconds. Default: `20`.

The application sends the secret in the `X-Webhook-Secret` header. Configure
n8n to reject requests that do not contain the same value.

## When the webhook is sent

Each customer has:

- A daily audit time.
- An email and WhatsApp delivery time.
- One or more WhatsApp recipients.

The audit is generated after its scan window closes. At the configured
delivery time, the PDF is emailed and the WhatsApp webhook is sent.

## Webhook payload

```json
{
  "event": "daily_equipment_audit",
  "version": 1,
  "customer": {
    "id": 12,
    "name": "Example Construction",
    "slug": "example-construction"
  },
  "audit": {
    "id": 94,
    "scheduled_for": "2026-06-10 20:00:00",
    "scan_window_start": "2026-06-10 19:45:00",
    "scan_window_end": "2026-06-10 20:15:00",
    "timezone": "Pacific/Apia"
  },
  "whatsapp": {
    "recipients": ["+6857000000"],
    "template_name": "daily_equipment_audit",
    "language": "en"
  },
  "summary": {
    "present": 8,
    "missing": 1,
    "equipment_moved": 1,
    "total": 10
  },
  "devices": [],
  "moved_assets": [],
  "message": {
    "title": "Daily Equipment Audit - Example Construction",
    "body": "Ready-to-send summary text"
  }
}
```

The `devices` array contains every assigned customer device/site, its status
counts, and its asset list. Moved equipment is shown at both its expected site
and its detected site.

## Recommended WhatsApp template

Template name: `daily_equipment_audit`

```text
Daily equipment audit for {{1}} completed at {{2}} Samoa time.

In range: {{3}}
Missing: {{4}}
Equipment moved: {{5}}

Site summary:
{{6}}

Please check the audit report sent by email for full details.
```

Suggested n8n mappings:

1. `customer.name`
2. `audit.scheduled_for`
3. `summary.present`
4. `summary.missing`
5. `summary.equipment_moved`
6. Build a short line per entry in `devices`, or use `message.body` when the
   WhatsApp provider allows a normal text message.

## n8n workflow outline

1. Webhook node receives the POST request.
2. IF node validates `X-Webhook-Secret`.
3. Split Out node processes `whatsapp.recipients`.
4. WhatsApp/HTTP node sends the approved template to each recipient.
5. Optional error workflow alerts the administrator when delivery fails.
