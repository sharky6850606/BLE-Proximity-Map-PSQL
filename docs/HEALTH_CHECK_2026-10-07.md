# Health check — 2026-10-07

## Result

The application starts successfully with a fresh SQLite database, serves real
HTTP requests, and passes the automated health suite. The suite completed seven
tests covering startup, authentication, authorization, ingestion, map and
analytics pages, PDF reports, daily audits, found-again handling, and evaluator
notifications.

Verification performed:

- Installed all pinned dependencies in an isolated virtual environment.
- `pip check` reported no broken requirements.
- Parsed all Python source files and the n8n workflow JSON successfully.
- Started the Flask server with an isolated SQLite database.
- Confirmed `GET /healthz` and `GET /login` return HTTP 200 over localhost.
- Ran `python -m unittest discover -s tests -v`: seven tests passed.
- Ran `git diff --check`: no whitespace errors.

No production database, credentials, reports, email recipients, WhatsApp
accounts, or n8n workflows were used.

## Confirmed issues fixed

1. Windows startup could crash after schema initialization when the console
   encoding could not print status-message emoji. Runtime status messages are
   now ASCII-safe.
2. Samoa-local audit and analytics timestamps were converted to Unix time using
   the host computer's timezone. On a non-UTC host this shifted audit windows
   and could incorrectly mark a reporting site offline. Conversions now attach
   UTC explicitly and use the configured Samoa offset consistently.
3. SQLite audits and evaluator transitions opened a second database writer
   while their existing transaction was active. Missing, moved, found, and
   evaluator notifications could fail with `database is locked`. Notification
   writes can now reuse the caller's transaction.
4. A new PostgreSQL database attempted to create `fmc_offline_alerts` before the
   referenced `customers` table. The creation order is now valid.
5. Login accepted scheme-relative `next=//host` values, allowing an external
   redirect after authentication. Only application-local paths are now used.
6. The Flespi ingestion endpoint had no built-in request authentication. It now
   validates `X-Webhook-Secret` with a constant-time comparison whenever
   `FLESPI_WEBHOOK_SECRET` is configured.
7. Direct `python app.py` startup always enabled Flask debug mode. Debug mode is
   now opt-in through `FLASK_DEBUG=1`.

## Not externally verified

- A live PostgreSQL instance was not available. PostgreSQL schema ordering was
  reviewed and corrected, but should still be exercised against a disposable
  PostgreSQL database before the next production deployment.
- Real Flespi, SMTP/SendGrid, n8n, and WhatsApp deliveries were not invoked.
- Browser layout and interactive Leaflet behavior were not visually tested.

## Remaining risks

- State-changing HTML forms do not yet include CSRF tokens.
- Background audit, evaluator, and offline-alert schedulers run inside the web
  process. Multiple Gunicorn workers would duplicate scheduled work.
- Samoa time uses a fixed UTC offset rather than an IANA timezone database.
- Dependency versions are pinned but there is no automated dependency or
  vulnerability update workflow.
