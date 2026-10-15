import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


_TEMP_PATH = Path(tempfile.mkdtemp(prefix="prox-map-tests-"))

os.environ.update(
    {
        "DB_PATH": str(_TEMP_PATH / "health.db"),
        "REPORTS_DIR": str(_TEMP_PATH / "reports"),
        "ACTIVITY_REPORTS_DIR": str(_TEMP_PATH / "activity_reports"),
        "AUDIT_REPORTS_DIR": str(_TEMP_PATH / "audit_reports"),
        "ADMIN_EMAIL": "health-admin@example.test",
        "ADMIN_PASSWORD": "HealthPass123!",
        "SECRET_KEY": "test-only-secret",
        "DISABLE_BACKGROUND_THREADS": "1",
        "TESTING": "1",
    }
)

from app import app  # noqa: E402
from database import get_db, init_db  # noqa: E402
from services.audit_service import run_customer_audit  # noqa: E402
from services.beacon_logic import _beacon_state, latest_messages  # noqa: E402
from services.evaluator_service import run_once as run_evaluator_once  # noqa: E402
from services.fmc_offline_alert_service import check_and_send_fmc_offline_alerts  # noqa: E402
from services.weekly_battery_report_service import send_due_weekly_battery_reports  # noqa: E402


class ApplicationHealthTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()
        latest_messages.clear()
        _beacon_state.clear()
        self._clear_database()
        init_db()

    def _clear_database(self):
        conn = get_db()
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            for row in tables:
                conn.execute(f'DELETE FROM "{row[0]}"')
            conn.commit()
        finally:
            conn.close()

    def _login(self, email, password):
        return self.client.post(
            "/login",
            data={"email": email, "password": password},
            follow_redirects=False,
        )

    def _logout(self):
        return self.client.get("/logout", follow_redirects=False)

    def _create_customer_fixture(self):
        response = self._login("health-admin@example.test", "HealthPass123!")
        self.assertEqual(response.status_code, 302)

        response = self.client.post(
            "/admin/customers",
            data={
                "name": "Health Customer",
                "slug": "health-customer",
                "audit_time": "18:00",
                "delivery_time": "18:30",
                "whatsapp_recipients": "+6857000000",
            },
        )
        self.assertEqual(response.status_code, 302)

        conn = get_db()
        try:
            customer_id = conn.execute(
                "SELECT id FROM customers WHERE slug = ?", ("health-customer",)
            ).fetchone()[0]
        finally:
            conn.close()

        response = self.client.post(
            "/admin/users",
            data={
                "customer_id": customer_id,
                "email": "customer@example.test",
                "password": "CustomerPass123!",
                "role": "customer_admin",
            },
        )
        self.assertEqual(response.status_code, 302)

        response = self.client.post(
            "/admin/devices",
            data={
                "customer_id": customer_id,
                "device_ident": "FMC-HEALTH-1",
                "label": "Main Site",
            },
        )
        self.assertEqual(response.status_code, 302)

        response = self.client.post(
            "/admin/assets",
            data={
                "customer_id": customer_id,
                "beacon_id": "BEACON-HEALTH-1",
                "name": "Health Asset",
                "expected_device_ident": "FMC-HEALTH-1",
            },
        )
        self.assertEqual(response.status_code, 302)
        return customer_id

    def test_public_health_and_protected_routes(self):
        self.assertEqual(self.client.get("/healthz").get_json(), {"ok": True})
        logo = self.client.get("/static/skyeye-ble-logo.png")
        self.assertEqual(logo.status_code, 200)
        self.assertEqual(logo.mimetype, "image/png")
        logo.close()
        login = self.client.get("/login")
        self.assertIn(b"skyeye-ble-logo.png", login.data)
        for path in ("/map", "/data", "/analytics", "/admin/"):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 302, path)
            self.assertIn("/login", response.headers["Location"])

    def test_flespi_webhook_secret_when_configured(self):
        with patch("routes.flespi_routes.FLESPI_WEBHOOK_SECRET", "ingestion-secret"):
            response = self.client.post("/flespi", json={"data": [{"ident": "FMC-1"}]})
            self.assertEqual(response.status_code, 401)

            response = self.client.post(
                "/flespi",
                json={"data": [{"ident": "FMC-1"}]},
                headers={"X-Webhook-Secret": "wrong"},
            )
            self.assertEqual(response.status_code, 401)

            response = self.client.post(
                "/flespi",
                json={"data": [{"ident": "FMC-1"}]},
                headers={"X-Webhook-Secret": "ingestion-secret"},
            )
            self.assertEqual(response.status_code, 200)

    def test_login_next_rejects_external_scheme_relative_url(self):
        response = self.client.post(
            "/login?next=//example.com",
            data={
                "email": "health-admin@example.test",
                "password": "HealthPass123!",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.headers["Location"], "//example.com")
        self.assertTrue(response.headers["Location"].endswith("/map"))

    def test_end_to_end_customer_ingestion_map_reports_and_audit(self):
        customer_id = self._create_customer_fixture()

        now = int(time.time())
        response = self.client.post(
            "/flespi",
            json={
                "data": [
                    {
                        "ident": "FMC-HEALTH-1",
                        "timestamp": now,
                        "position.latitude": -13.85,
                        "position.longitude": -171.75,
                        "ble.beacons": [
                            {
                                "id": "BEACON-HEALTH-1",
                                "rssi": -62,
                                "battery.voltage": 2.9,
                            }
                        ],
                    },
                    {
                        "ident": "FMC-OTHER-1",
                        "timestamp": now,
                        "position.latitude": -13.86,
                        "position.longitude": -171.76,
                        "ble.beacons": [{"id": "BEACON-OTHER-1", "rssi": -70}],
                    },
                ]
            },
        )
        self.assertEqual(response.status_code, 200)

        self._logout()
        response = self._login("customer@example.test", "CustomerPass123!")
        self.assertEqual(response.status_code, 302)

        response = self.client.get("/data")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual([device["ident"] for device in payload["devices"]], ["FMC-HEALTH-1"])
        self.assertEqual(payload["devices"][0]["beacons"][0]["id"], "BEACON-HEALTH-1")
        self.assertEqual(payload["devices"][0]["beacons"][0]["battery_percent"], 85)

        response = self.client.post(
            "/rename_device", json={"id": "FMC-OTHER-1", "name": "Forbidden"}
        )
        self.assertEqual(response.status_code, 403)

        response = self.client.get("/analytics")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Analytics", response.data)

        samoa_now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=13)
        today = samoa_now.strftime("%Y-%m-%d")
        response = self.client.post(
            "/activity-reports",
            data={
                "report_kind": "beacon",
                "beacon": "BEACON-HEALTH-1",
                "start_date": today,
                "end_date": today,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF"))
        response.close()

        scheduled = samoa_now
        pdf_path = run_customer_audit(customer_id, scheduled_local_dt=scheduled)
        self.assertTrue(pdf_path)
        self.assertTrue(Path(pdf_path).exists())

        conn = get_db()
        try:
            audit_result = conn.execute(
                "SELECT status FROM audit_results WHERE beacon_id = ?",
                ("BEACON-HEALTH-1",),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(audit_result[0], "present")

        response = self.client.get("/audit-reports")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Health Customer", response.data)

    def test_admin_dashboard_and_invalid_login(self):
        response = self._login("health-admin@example.test", "wrong-password")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid email or password", response.data)

        response = self._login("health-admin@example.test", "HealthPass123!")
        self.assertEqual(response.status_code, 302)
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Admin", response.data)

        for path in (
            "/map",
            "/data",
            "/analytics",
            "/notifications/history",
            "/activity-reports",
            "/audit-reports",
            "/reports/history",
            "/uptime",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)

        response = self.client.get("/reports/generate-now")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"%PDF"))
        response.close()

        response = self.client.get("/download/latest-report")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"%PDF"))
        response.close()

    def test_audit_status_classification(self):
        customer_id = self._create_customer_fixture()

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO customer_devices "
                "(customer_id, device_ident, label, created_at) VALUES (?,?,?,?)",
                (customer_id, "FMC-HEALTH-2", "Second Site", "test"),
            )
            conn.execute(
                "INSERT INTO customer_devices "
                "(customer_id, device_ident, label, created_at) VALUES (?,?,?,?)",
                (customer_id, "FMC-OFFLINE", "Offline Site", "test"),
            )
            for beacon_id, name, expected_device in (
                ("BEACON-MOVED", "Moved Asset", "FMC-HEALTH-1"),
                ("BEACON-MISSING", "Missing Asset", "FMC-HEALTH-1"),
                ("BEACON-OFFLINE", "Offline Asset", "FMC-OFFLINE"),
            ):
                conn.execute(
                    "INSERT INTO customer_assets "
                    "(customer_id, beacon_id, name, expected_device_ident, active, status, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (customer_id, beacon_id, name, expected_device, 1, "unknown", "test"),
                )
            conn.commit()
        finally:
            conn.close()

        now = int(time.time())
        response = self.client.post(
            "/flespi",
            json={
                "data": [
                    {
                        "ident": "FMC-HEALTH-1",
                        "timestamp": now,
                        "ble.beacons": [{"id": "BEACON-HEALTH-1", "rssi": -60}],
                    },
                    {
                        "ident": "FMC-HEALTH-2",
                        "timestamp": now,
                        "ble.beacons": [{"id": "BEACON-MOVED", "rssi": -65}],
                    },
                ]
            },
        )
        self.assertEqual(response.status_code, 200)

        samoa_now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=13)
        pdf_path = run_customer_audit(customer_id, scheduled_local_dt=samoa_now)
        self.assertTrue(Path(pdf_path).exists())

        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT beacon_id, status FROM audit_results ORDER BY beacon_id"
            ).fetchall()
        finally:
            conn.close()
        statuses = {row[0]: row[1] for row in rows}
        self.assertEqual(statuses["BEACON-HEALTH-1"], "present")
        self.assertEqual(statuses["BEACON-MOVED"], "equipment_moved")
        self.assertEqual(statuses["BEACON-MISSING"], "missing")
        self.assertEqual(statuses["BEACON-OFFLINE"], "device_offline")

        response = self.client.post(
            "/flespi",
            json={
                "data": [
                    {
                        "ident": "FMC-HEALTH-1",
                        "timestamp": int(time.time()),
                        "ble.beacons": [{"id": "BEACON-MISSING", "rssi": -61}],
                    }
                ]
            },
        )
        self.assertEqual(response.status_code, 200)
        conn = get_db()
        try:
            asset_status = conn.execute(
                "SELECT status FROM customer_assets WHERE beacon_id = ?",
                ("BEACON-MISSING",),
            ).fetchone()[0]
            found_notification = conn.execute(
                "SELECT type FROM notifications WHERE beacon_id = ? ORDER BY id DESC LIMIT 1",
                ("BEACON-MISSING",),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(asset_status, "present")
        self.assertEqual(found_notification, "found")

    def test_evaluator_transition_writes_notification_in_same_transaction(self):
        now = int(time.time())
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO beacon_states "
                "(beacon_key, device_ident, beacon_id, state, last_seen_ts, "
                "last_distance, active, missing, last_status_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("FMC-1:BEACON-1", "FMC-1", "BEACON-1", "out", now, 0.5, 1, 0, now),
            )
            conn.commit()
        finally:
            conn.close()

        run_evaluator_once()

        conn = get_db()
        try:
            row = conn.execute(
                "SELECT type FROM notifications WHERE beacon_id = ? ORDER BY id DESC LIMIT 1",
                ("BEACON-1",),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "in")

    def test_weekly_battery_report_includes_all_customer_beacons_once(self):
        customer_id = self._create_customer_fixture()
        now = int(time.time())
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO customer_devices (customer_id, device_ident, label, created_at) VALUES (?,?,?,?)",
                (customer_id, "FMC-HEALTH-2", "Warehouse", "test"),
            )
            conn.execute(
                "INSERT INTO customer_assets "
                "(customer_id, beacon_id, name, expected_device_ident, active, status, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (customer_id, "BEACON-HEALTH-2", "Warehouse Asset", "FMC-HEALTH-2", 1, "present", "test"),
            )
            conn.execute(
                "INSERT INTO beacon_states "
                "(beacon_key, device_ident, beacon_id, last_seen_ts, last_battery_voltage, last_battery_percent) "
                "VALUES (?,?,?,?,?,?)",
                ("FMC-HEALTH-1:BEACON-HEALTH-1", "FMC-HEALTH-1", "BEACON-HEALTH-1", now, 2.90, 85),
            )
            conn.execute(
                "INSERT INTO beacon_states "
                "(beacon_key, device_ident, beacon_id, last_seen_ts, last_battery_voltage, last_battery_percent) "
                "VALUES (?,?,?,?,?,?)",
                ("FMC-HEALTH-2:BEACON-HEALTH-2", "FMC-HEALTH-2", "BEACON-HEALTH-2", now, 2.20, 10),
            )
            conn.commit()
        finally:
            conn.close()

        delivery = [{
            "recipient": "customer@example.test",
            "provider": "smtp",
            "status": "sent",
            "error": "",
        }]
        monday_at_eight = datetime(2026, 10, 12, 8, 0, 0)
        with patch(
            "services.weekly_battery_report_service.send_report_email_with_results",
            return_value=delivery,
        ) as send_email:
            self.assertEqual(send_due_weekly_battery_reports(monday_at_eight - timedelta(minutes=1)), 0)
            self.assertEqual(send_due_weekly_battery_reports(monday_at_eight), 1)
            self.assertEqual(send_due_weekly_battery_reports(monday_at_eight + timedelta(hours=1)), 0)
            self.assertEqual(send_due_weekly_battery_reports(monday_at_eight + timedelta(days=1)), 0)

        self.assertEqual(send_email.call_count, 1)
        recipients, subject, body = send_email.call_args.args
        self.assertEqual(recipients, ["customer@example.test"])
        self.assertIn("Weekly Beacon Battery Report", subject)
        self.assertIn("Health Asset (BEACON-HEALTH-1): 85% / 2.90V", body)
        self.assertIn("Warehouse Asset (BEACON-HEALTH-2): 10% / 2.20V - LOW BATTERY", body)

        conn = get_db()
        try:
            report = conn.execute(
                "SELECT week_start, beacon_count, status FROM weekly_battery_reports WHERE customer_id=?",
                (customer_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(report), ("2026-10-12", 2, "sent"))

    def test_offline_device_sends_one_customer_email_per_incident(self):
        customer_id = self._create_customer_fixture()
        old_seen = int(time.time()) - 3600
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO device_states (device_key, device_ident, state, online, last_seen_ts) "
                "VALUES (?,?,?,?,?)",
                ("FMC-HEALTH-1", "FMC-HEALTH-1", "offline", 0, old_seen),
            )
            conn.commit()
        finally:
            conn.close()

        email_delivery = [{
            "recipient": "customer@example.test",
            "provider": "smtp",
            "status": "sent",
            "error": "",
        }]
        webhook_delivery = {
            "status": "sent", "http_status": 200, "error": "", "webhook_url": "https://example.test/hook"
        }
        with patch(
            "services.fmc_offline_alert_service.send_report_email_with_results",
            return_value=email_delivery,
        ) as send_email, patch(
            "services.fmc_offline_alert_service.send_whatsapp_webhook",
            return_value=webhook_delivery,
        ) as send_whatsapp:
            self.assertEqual(check_and_send_fmc_offline_alerts(), 1)
            self.assertEqual(check_and_send_fmc_offline_alerts(), 0)

            conn = get_db()
            try:
                conn.execute(
                    "UPDATE device_states SET state=?, online=?, last_seen_ts=? WHERE device_ident=?",
                    ("online", 1, int(time.time()), "FMC-HEALTH-1"),
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(check_and_send_fmc_offline_alerts(), 1)
            self.assertEqual(check_and_send_fmc_offline_alerts(), 0)

        self.assertEqual(send_email.call_count, 2)
        self.assertEqual(send_whatsapp.call_count, 1)
        recipients, subject, body = send_email.call_args_list[0].args
        self.assertEqual(recipients, ["customer@example.test"])
        self.assertEqual(subject, "Device offline - Main Site")
        self.assertIn("Please check the FMC power, SIM/data connection", body)
        recovery_recipients, recovery_subject, recovery_body = send_email.call_args_list[1].args
        self.assertEqual(recovery_recipients, ["customer@example.test"])
        self.assertEqual(recovery_subject, "Device online again - Main Site")
        self.assertIn("Monitoring has resumed", recovery_body)

        conn = get_db()
        try:
            email_logs = conn.execute(
                "SELECT send_type, status FROM email_logs WHERE customer_id=? ORDER BY id",
                (customer_id,),
            ).fetchall()
            incident_count = conn.execute(
                "SELECT COUNT(*) FROM fmc_offline_alerts WHERE customer_id=?",
                (customer_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(
            [tuple(row) for row in email_logs],
            [("fmc_offline", "sent"), ("fmc_online", "sent")],
        )
        self.assertEqual(incident_count, 0)


if __name__ == "__main__":
    unittest.main()
