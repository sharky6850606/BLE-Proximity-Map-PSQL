# BLE Proximity Map

Flask application for monitoring equipment beacons reported by Flespi-connected
FMC devices. It provides a live Leaflet map, customer-scoped accounts, equipment
audits, PDF reports, email delivery, and n8n WhatsApp notifications.

## Data flow

1. Flespi posts device and BLE observations to `POST /flespi`.
2. The application records device heartbeats, positions, beacon proximity, and
   battery data in PostgreSQL or SQLite.
3. The map and analytics pages read the persisted state.
4. Scheduled audits classify assigned equipment as present, missing, moved to
   another site, or unavailable because its FMC/site is offline.
5. Completed audit PDFs can be downloaded, emailed, and sent as WhatsApp
   summaries through n8n.

## Local setup

Python 3.11 or newer is recommended.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set the required variables in your shell or deployment environment. The app
does not load `.env` files itself. At minimum, production should configure
`DATABASE_URL`, `SECRET_KEY`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, and
`FLESPI_WEBHOOK_SECRET`.

For local SQLite startup:

```powershell
$env:SECRET_KEY = "local-development-only"
$env:ADMIN_EMAIL = "admin@example.test"
$env:ADMIN_PASSWORD = "replace-this"
$env:FLESPI_WEBHOOK_SECRET = "replace-this-too"
.\.venv\Scripts\python.exe app.py
```

The local server listens on `http://127.0.0.1:5000`. Direct execution runs
without debug mode unless `FLASK_DEBUG=1` is explicitly set.

## Tests

The health suite creates an isolated temporary SQLite database and generated
PDFs. It does not use production data or credentials.

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Coverage includes application startup, authentication, redirect safety,
administrator and customer pages, tenant device isolation, Flespi ingestion,
webhook authentication, map data, analytics, PDF generation, daily audit
classification, found-again handling, and evaluator notifications.

The latest verified baseline and outstanding limitations are recorded in
`docs/HEALTH_CHECK_2026-10-07.md`.

## Deployment

The included `Procfile` runs one Gunicorn worker. Keeping a single worker is
important while the in-process audit, evaluator, and offline-alert background
threads are used. A multi-worker deployment would start duplicate schedulers;
move scheduled work to a separate worker before increasing the web worker count.

See `WHATSAPP_N8N_SETUP.md` for n8n and WhatsApp setup. Configure Flespi to send
the `FLESPI_WEBHOOK_SECRET` value in an `X-Webhook-Secret` header. If the variable
is omitted, ingestion remains unauthenticated for backward compatibility.

## Current limitations

- HTML form mutations do not yet use CSRF tokens. Keep the app behind HTTPS and
  trusted access controls until CSRF protection is added.
- Samoa time is represented by a configurable fixed UTC offset. If the business
  rules expand to regions with daylight-saving changes, migrate to IANA timezone
  handling.
- Background scheduling is process-local rather than a durable job queue.
