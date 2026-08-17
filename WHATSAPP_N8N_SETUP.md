# n8n WhatsApp Audit Webhook

## Render environment variables

- `N8N_WHATSAPP_WEBHOOK_URL`: Production n8n webhook URL.
- `N8N_WEBHOOK_SECRET`: A long random shared secret.
- `N8N_WEBHOOK_TIMEOUT`: Optional timeout in seconds. Default: `20`.

The application sends the secret in the `X-Webhook-Secret` header. Configure
n8n to reject requests that do not contain the same value.

## When the webhooks are sent

Each customer has:

- A daily audit time.
- An email and WhatsApp delivery time.
- One or more WhatsApp recipients.

The audit is generated after its scan window closes. At the configured
delivery time, the PDF is emailed and the WhatsApp webhook is sent.

The app also sends an FMC/site offline WhatsApp webhook at any time of day
when an assigned FMC has no heartbeat for 10 minutes. It sends one alert per
offline incident, then resets after that FMC reports again.

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
    "device_offline": 0,
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

### FMC offline payload

```json
{
  "event": "fmc_offline",
  "version": 1,
  "customer": {
    "id": 12,
    "name": "Example Construction",
    "slug": "example-construction"
  },
  "whatsapp": {
    "recipients": ["+6857000000"],
    "template_name": "ble_fmc_offline",
    "language": "en"
  },
  "device": {
    "device_ident": "863719068581413",
    "device_name": "Main Site",
    "last_heartbeat": "2026-08-18 09:25:02",
    "offline_for": "10 minutes"
  }
}
```

## Recommended WhatsApp templates

Template name: `ble_notification`

```text
Daily Equipment Audit for {{1}}
Audit completed: {{2}} Samoa time.

In range: {{3}}
Missing: {{4}}
Equipment moved: {{5}}
Site offline: {{6}}

Site summary:
{{7}}

Please check the audit report sent by email for full details.
```

Variables:

1. `customer.name`
2. `audit.scheduled_for`
3. `summary.present`
4. `summary.missing`
5. `summary.equipment_moved`
6. `summary.device_offline`
7. One short line per entry in `devices`

Template name: `ble_fmc_offline`

```text
FMC/site offline alert for {{1}}

Site: {{2}}
Device IMEI: {{3}}
Last heartbeat: {{4}} Samoa time
Offline for: {{5}}

Please check the FMC power, SIM/data connection, and device installation.
```

Variables:

1. Customer name
2. Site/device label
3. FMC IMEI / ident
4. Last heartbeat
5. Offline duration

## n8n workflow outline

1. Webhook node receives the POST request.
2. IF node validates `X-Webhook-Secret`.
3. Code node detects whether the event is `daily_equipment_audit` or
   `fmc_offline` and prepares the correct template variables.
4. WhatsApp/HTTP node sends the approved template to each recipient.
5. Optional error workflow alerts the administrator when delivery fails.
