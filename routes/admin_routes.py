import re
import time
from datetime import timedelta

from flask import Blueprint, redirect, render_template, request, url_for

from database import get_db
from services.auth_service import admin_required, current_user, hash_password, normalize_email
from services.audit_log_service import log_event
from config import AUDIT_WINDOW_AFTER_MIN
from services.audit_service import run_customer_audit, send_audit_report_email, _local_dt_from_unix
from services.beacon_logic import format_samoa_time

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _ph(conn):
    return "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"


def _now_label():
    return format_samoa_time(time.time()).replace(" ", "T")


def _slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "customer"


def _ensure_email_logs_table(conn):
    """Keep the admin console usable if a deployment missed this migration."""
    if getattr(conn, "backend", "postgres") == "postgres":
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_logs (
                id BIGSERIAL PRIMARY KEY,
                created_at TEXT,
                audit_run_id BIGINT REFERENCES audit_runs(id),
                customer_id BIGINT REFERENCES customers(id),
                recipient TEXT,
                subject TEXT,
                provider TEXT,
                status TEXT,
                error TEXT,
                attachment_path TEXT,
                send_type TEXT,
                actor_user_id BIGINT,
                actor_email TEXT
            )
        """)
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT,
                audit_run_id INTEGER REFERENCES audit_runs(id),
                customer_id INTEGER REFERENCES customers(id),
                recipient TEXT,
                subject TEXT,
                provider TEXT,
                status TEXT,
                error TEXT,
                attachment_path TEXT,
                send_type TEXT,
                actor_user_id INTEGER,
                actor_email TEXT
            )
        """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_email_logs_audit ON email_logs(audit_run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_email_logs_created ON email_logs(created_at)")


def _unique_customer_slug(conn, requested_slug, exclude_customer_id=None):
    base = _slugify(requested_slug)
    slug = base
    suffix = 2
    ph = _ph(conn)
    while True:
        if exclude_customer_id is None:
            exists = conn.execute(f"SELECT 1 FROM customers WHERE slug = {ph} LIMIT 1", (slug,)).fetchone()
        else:
            exists = conn.execute(
                f"SELECT 1 FROM customers WHERE slug = {ph} AND id <> {ph} LIMIT 1",
                (slug, exclude_customer_id),
            ).fetchone()
        if not exists:
            return slug
        slug = f"{base}-{suffix}"
        suffix += 1


def _safe_log_event(*args, **kwargs):
    try:
        log_event(*args, **kwargs)
    except Exception as exc:
        print(f"[admin] audit log skipped: {exc}")


@admin_bp.route("/")
@admin_required
def dashboard():
    conn = get_db()
    try:
        _ensure_email_logs_table(conn)
        conn.commit()
        customers = conn.execute(
            "SELECT id, name, slug, active, created_at FROM customers ORDER BY name"
        ).fetchall()
        users = conn.execute(
            "SELECT u.id, u.email, u.role, u.active, c.name, u.last_login_at, u.customer_id "
            "FROM app_users u LEFT JOIN customers c ON c.id = u.customer_id "
            "WHERE u.deleted_at IS NULL ORDER BY u.id DESC LIMIT 100"
        ).fetchall()
        devices = conn.execute(
            "SELECT cd.id, c.name, cd.device_ident, COALESCE(cds.name, cd.label, d.name, cd.device_ident) AS label, cd.created_at, cd.customer_id "
            "FROM customer_devices cd "
            "JOIN customers c ON c.id = cd.customer_id "
            "LEFT JOIN customer_device_settings cds ON cds.customer_id = cd.customer_id AND cds.device_ident = cd.device_ident "
            "LEFT JOIN devices d ON d.id = cd.device_ident "
            "ORDER BY c.name, cd.device_ident"
        ).fetchall()
        active_devices = conn.execute(
            "SELECT device_ident, online, last_seen_ts, last_lat, last_lon FROM device_states ORDER BY device_ident"
        ).fetchall()
        active_beacons = conn.execute(
            "SELECT beacon_id, device_ident, last_seen_ts, last_distance, last_rssi, missing FROM beacon_states "
            "WHERE beacon_id IS NOT NULL ORDER BY last_seen_ts DESC LIMIT 300"
        ).fetchall()
        assets = conn.execute(
            "SELECT ca.id, c.name, ca.beacon_id, COALESCE(ca.name, ca.beacon_id), ca.expected_device_ident, "
            "ca.active, ca.status, ca.missing_since, ca.found_at, ca.customer_id "
            "FROM customer_assets ca JOIN customers c ON c.id = ca.customer_id "
            "ORDER BY c.name, COALESCE(ca.name, ca.beacon_id)"
        ).fetchall()
        audits = conn.execute(
            "SELECT ar.id, c.name, ar.scheduled_for, ar.scan_window_start, ar.scan_window_end, ar.status, ar.pdf_path, ar.emailed_at "
            "FROM audit_runs ar JOIN customers c ON c.id = ar.customer_id "
            "ORDER BY ar.id DESC LIMIT 100"
        ).fetchall()
        audit_logs = conn.execute(
            "SELECT al.created_at, al.actor_email, al.actor_role, c.name, al.action, al.target_type, al.target_id, al.details, al.ip_address "
            "FROM audit_logs al LEFT JOIN customers c ON c.id = al.customer_id "
            "ORDER BY al.id DESC LIMIT 200"
        ).fetchall()
        email_logs = conn.execute(
            "SELECT el.created_at, c.name, el.recipient, el.subject, el.status, el.provider, el.error, el.send_type, el.actor_email, el.audit_run_id "
            "FROM email_logs el LEFT JOIN customers c ON c.id = el.customer_id "
            "ORDER BY el.id DESC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()
    return render_template(
        "admin.html",
        customers=customers,
        users=users,
        devices=devices,
        active_devices=active_devices,
        active_beacons=active_beacons,
        assets=assets,
        audits=audits,
        audit_logs=audit_logs,
        email_logs=email_logs,
    )


@admin_bp.route("/customers", methods=["POST"])
@admin_required
def create_customer():
    name = (request.form.get("name") or "").strip()
    if not name:
        return redirect(url_for("admin.dashboard"))
    requested_slug = request.form.get("slug") or name
    conn = get_db()
    try:
        ph = _ph(conn)
        slug = _unique_customer_slug(conn, requested_slug)
        conn.execute(
            f"INSERT INTO customers (name, slug, active, created_at) VALUES ({ph},{ph},{ph},{ph})",
            (name, slug, 1, _now_label()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _safe_log_event(
        "admin.create_customer",
        target_type="customer",
        target_id=slug,
        details=f"Created customer {name}",
        actor_user=current_user(),
    )
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/customers/<int:customer_id>/edit", methods=["POST"])
@admin_required
def update_customer(customer_id):
    name = (request.form.get("name") or "").strip()
    requested_slug = request.form.get("slug") or name
    if not name:
        return redirect(url_for("admin.dashboard"))
    conn = get_db()
    try:
        ph = _ph(conn)
        slug = _unique_customer_slug(conn, requested_slug, exclude_customer_id=customer_id)
        conn.execute(
            f"UPDATE customers SET name = {ph}, slug = {ph} WHERE id = {ph}",
            (name, slug, customer_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _safe_log_event(
        "admin.update_customer",
        target_type="customer",
        target_id=customer_id,
        details=f"Updated customer {name}",
        actor_user=current_user(),
        customer_id=customer_id,
    )
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/customers/<int:customer_id>/toggle", methods=["POST"])
@admin_required
def toggle_customer(customer_id):
    conn = get_db()
    try:
        ph = _ph(conn)
        row = conn.execute(f"SELECT active FROM customers WHERE id = {ph}", (customer_id,)).fetchone()
        if row:
            new_active = 0 if row[0] else 1
            conn.execute(
                f"UPDATE customers SET active = {ph} WHERE id = {ph}",
                (new_active, customer_id),
            )
            log_event(
                "admin.toggle_customer",
                target_type="customer",
                target_id=customer_id,
                details=f"{'Reactivated' if new_active else 'Suspended'} customer",
                actor_user=current_user(),
                customer_id=customer_id,
                conn=conn,
            )
            conn.commit()
    finally:
        conn.close()
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/users", methods=["POST"])
@admin_required
def create_user():
    email = normalize_email(request.form.get("email"))
    password = request.form.get("password") or ""
    role = request.form.get("role") or "customer_user"
    if role not in {"admin", "customer_admin", "customer_user"}:
        role = "customer_user"

    raw_customer_id = request.form.get("customer_id") or None
    customer_id = None
    if role != "admin":
        try:
            customer_id = int(raw_customer_id) if raw_customer_id else None
        except (TypeError, ValueError):
            customer_id = None
        if not customer_id:
            return redirect(url_for("admin.dashboard"))

    if not email or not password:
        return redirect(url_for("admin.dashboard"))

    conn = get_db()
    try:
        ph = _ph(conn)
        existing = conn.execute(
            f"SELECT id, customer_id, role, active FROM app_users WHERE email = {ph} AND deleted_at IS NULL",
            (email,),
        ).fetchone()
        password_hash = hash_password(password)
        if existing:
            conn.execute(
                f"UPDATE app_users SET customer_id = {ph}, password_hash = {ph}, role = {ph}, "
                f"active = {ph}, force_password_reset = 0 WHERE id = {ph}",
                (customer_id, password_hash, role, 1, existing[0]),
            )
            action = "admin.update_user"
            details = f"Updated existing {role} account {email}"
            target_id = existing[0]
        else:
            conn.execute(
                f"INSERT INTO app_users (customer_id, email, password_hash, role, active, created_at) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph})",
                (customer_id, email, password_hash, role, 1, _now_label()),
            )
            action = "admin.create_user"
            details = f"Created {role} account {email}"
            target_id = email

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _safe_log_event(
        action,
        target_type="user",
        target_id=target_id,
        details=details,
        actor_user=current_user(),
        customer_id=customer_id,
    )
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/users/<int:user_id>/edit", methods=["POST"])
@admin_required
def update_user(user_id):
    email = normalize_email(request.form.get("email"))
    role = request.form.get("role") or "customer_user"
    if role not in {"admin", "customer_admin", "customer_user"}:
        role = "customer_user"
    raw_customer_id = request.form.get("customer_id") or None
    customer_id = None
    if role != "admin":
        try:
            customer_id = int(raw_customer_id) if raw_customer_id else None
        except (TypeError, ValueError):
            customer_id = None
        if not customer_id:
            return redirect(url_for("admin.dashboard"))
    if not email:
        return redirect(url_for("admin.dashboard"))

    conn = get_db()
    try:
        ph = _ph(conn)
        duplicate = conn.execute(
            f"SELECT id FROM app_users WHERE email = {ph} AND id <> {ph} AND deleted_at IS NULL",
            (email, user_id),
        ).fetchone()
        if not duplicate:
            conn.execute(
                f"UPDATE app_users SET email = {ph}, role = {ph}, customer_id = {ph} WHERE id = {ph}",
                (email, role, customer_id, user_id),
            )
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if not duplicate:
        _safe_log_event(
            "admin.update_user",
            target_type="user",
            target_id=user_id,
            details=f"Updated user {email}",
            actor_user=current_user(),
            customer_id=customer_id,
        )
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def toggle_user(user_id):
    conn = get_db()
    try:
        ph = _ph(conn)
        row = conn.execute(f"SELECT active, customer_id, email FROM app_users WHERE id = {ph}", (user_id,)).fetchone()
        if row:
            new_active = 0 if row[0] else 1
            conn.execute(
                f"UPDATE app_users SET active = {ph} WHERE id = {ph}",
                (new_active, user_id),
            )
            log_event(
                "admin.toggle_user",
                target_type="user",
                target_id=user_id,
                details=f"{'Reactivated' if new_active else 'Suspended'} user {row[2]}",
                actor_user=current_user(),
                customer_id=row[1],
                conn=conn,
            )
            conn.commit()
    finally:
        conn.close()
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/users/<int:user_id>/password", methods=["POST"])
@admin_required
def reset_user_password(user_id):
    password = request.form.get("password") or ""
    if not password:
        return redirect(url_for("admin.dashboard"))
    conn = get_db()
    try:
        ph = _ph(conn)
        row = conn.execute(f"SELECT customer_id, email FROM app_users WHERE id = {ph}", (user_id,)).fetchone()
        conn.execute(
            f"UPDATE app_users SET password_hash = {ph}, force_password_reset = 0 WHERE id = {ph}",
            (hash_password(password), user_id),
        )
        log_event(
            "admin.reset_password",
            target_type="user",
            target_id=user_id,
            details=f"Reset password for {row[1] if row else user_id}",
            actor_user=current_user(),
            customer_id=row[0] if row else None,
            conn=conn,
        )
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/devices", methods=["POST"])
@admin_required
def assign_device():
    customer_id = request.form.get("customer_id")
    device_ident = (request.form.get("device_ident") or "").strip()
    label = (request.form.get("label") or "").strip() or None
    if not customer_id or not device_ident:
        return redirect(url_for("admin.dashboard"))
    conn = get_db()
    try:
        ph = _ph(conn)
        conn.execute(
            f"INSERT INTO customer_devices (customer_id, device_ident, label, created_at) "
            f"VALUES ({ph},{ph},{ph},{ph}) "
            "ON CONFLICT(customer_id, device_ident) DO UPDATE SET label=excluded.label",
            (customer_id, device_ident, label, _now_label()),
        )
        log_event(
            "admin.assign_device",
            target_type="device",
            target_id=device_ident,
            details=f"Assigned device {device_ident}" + (f" as {label}" if label else ""),
            actor_user=current_user(),
            customer_id=customer_id,
            conn=conn,
        )
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/devices/<int:assignment_id>/edit", methods=["POST"])
@admin_required
def update_device_assignment(assignment_id):
    customer_id = request.form.get("customer_id")
    device_ident = (request.form.get("device_ident") or "").strip()
    label = (request.form.get("label") or "").strip() or None
    if not customer_id or not device_ident:
        return redirect(url_for("admin.dashboard"))

    conn = get_db()
    try:
        ph = _ph(conn)
        conflict = conn.execute(
            f"SELECT id FROM customer_devices WHERE customer_id = {ph} AND device_ident = {ph} AND id <> {ph}",
            (customer_id, device_ident, assignment_id),
        ).fetchone()
        if conflict:
            conn.execute(f"UPDATE customer_devices SET label = {ph} WHERE id = {ph}", (label, conflict[0]))
            conn.execute(f"DELETE FROM customer_devices WHERE id = {ph}", (assignment_id,))
        else:
            conn.execute(
                f"UPDATE customer_devices SET customer_id = {ph}, device_ident = {ph}, label = {ph} WHERE id = {ph}",
                (customer_id, device_ident, label, assignment_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _safe_log_event(
        "admin.update_device_assignment",
        target_type="device",
        target_id=device_ident,
        details=f"Updated device assignment {device_ident}" + (f" as {label}" if label else ""),
        actor_user=current_user(),
        customer_id=customer_id,
    )
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/devices/<int:assignment_id>/remove", methods=["POST"])
@admin_required
def remove_device(assignment_id):
    conn = get_db()
    try:
        ph = _ph(conn)
        row = conn.execute(
            f"SELECT customer_id, device_ident FROM customer_devices WHERE id = {ph}",
            (assignment_id,),
        ).fetchone()
        conn.execute(f"DELETE FROM customer_devices WHERE id = {ph}", (assignment_id,))
        log_event(
            "admin.remove_device",
            target_type="device",
            target_id=row[1] if row else assignment_id,
            details=f"Removed device assignment {row[1] if row else assignment_id}",
            actor_user=current_user(),
            customer_id=row[0] if row else None,
            conn=conn,
        )
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/assets", methods=["POST"])
@admin_required
def create_asset():
    customer_id = request.form.get("customer_id")
    beacon_id = (request.form.get("beacon_id") or "").strip()
    name = (request.form.get("name") or "").strip() or None
    expected_device_ident = (request.form.get("expected_device_ident") or "").strip() or None
    if not customer_id or not beacon_id:
        return redirect(url_for("admin.dashboard"))
    conn = get_db()
    try:
        ph = _ph(conn)
        conn.execute(
            f"INSERT INTO customer_assets (customer_id, beacon_id, name, expected_device_ident, active, status, created_at) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph}) "
            "ON CONFLICT(customer_id, beacon_id) DO UPDATE SET "
            "name=excluded.name, expected_device_ident=excluded.expected_device_ident, active=1",
            (customer_id, beacon_id, name, expected_device_ident, 1, "unknown", _now_label()),
        )
        log_event(
            "admin.save_expected_asset",
            target_type="asset",
            target_id=beacon_id,
            details=f"Saved expected asset {name or beacon_id}",
            actor_user=current_user(),
            customer_id=customer_id,
            conn=conn,
        )
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/assets/<int:asset_id>/edit", methods=["POST"])
@admin_required
def update_asset(asset_id):
    customer_id = request.form.get("customer_id")
    beacon_id = (request.form.get("beacon_id") or "").strip()
    name = (request.form.get("name") or "").strip() or None
    expected_device_ident = (request.form.get("expected_device_ident") or "").strip() or None
    if not customer_id or not beacon_id:
        return redirect(url_for("admin.dashboard"))

    conn = get_db()
    try:
        ph = _ph(conn)
        conflict = conn.execute(
            f"SELECT id FROM customer_assets WHERE customer_id = {ph} AND beacon_id = {ph} AND id <> {ph}",
            (customer_id, beacon_id, asset_id),
        ).fetchone()
        if conflict:
            conn.execute(
                f"UPDATE customer_assets SET name = {ph}, expected_device_ident = {ph}, active = {ph} WHERE id = {ph}",
                (name, expected_device_ident, 1, conflict[0]),
            )
            conn.execute(f"DELETE FROM customer_assets WHERE id = {ph}", (asset_id,))
        else:
            conn.execute(
                f"UPDATE customer_assets SET customer_id = {ph}, beacon_id = {ph}, name = {ph}, expected_device_ident = {ph} WHERE id = {ph}",
                (customer_id, beacon_id, name, expected_device_ident, asset_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _safe_log_event(
        "admin.update_expected_asset",
        target_type="asset",
        target_id=beacon_id,
        details=f"Updated expected asset {name or beacon_id}",
        actor_user=current_user(),
        customer_id=customer_id,
    )
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/assets/<int:asset_id>/toggle", methods=["POST"])
@admin_required
def toggle_asset(asset_id):
    conn = get_db()
    try:
        ph = _ph(conn)
        row = conn.execute(
            f"SELECT active, customer_id, beacon_id, name FROM customer_assets WHERE id = {ph}",
            (asset_id,),
        ).fetchone()
        if row:
            new_active = 0 if row[0] else 1
            conn.execute(
                f"UPDATE customer_assets SET active = {ph} WHERE id = {ph}",
                (new_active, asset_id),
            )
            log_event(
                "admin.toggle_expected_asset",
                target_type="asset",
                target_id=row[2],
                details=f"{'Reactivated' if new_active else 'Deactivated'} expected asset {row[3] or row[2]}",
                actor_user=current_user(),
                customer_id=row[1],
                conn=conn,
            )
            conn.commit()
    finally:
        conn.close()
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/audits/run", methods=["POST"])
@admin_required
def run_audit_now():
    customer_id = request.form.get("customer_id")
    if customer_id:
        scheduled = _local_dt_from_unix() - timedelta(minutes=AUDIT_WINDOW_AFTER_MIN)
        run_customer_audit(int(customer_id), scheduled_local_dt=scheduled)
        log_event(
            "admin.run_manual_audit",
            target_type="customer",
            target_id=customer_id,
            details="Ran manual audit from admin console",
            actor_user=current_user(),
            customer_id=customer_id,
        )
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/audits/<int:audit_id>/email", methods=["POST"])
@admin_required
def send_audit_email(audit_id):
    sent_count = send_audit_report_email(audit_id, actor_user=current_user(), send_type="manual")
    log_event(
        "admin.send_audit_email",
        target_type="audit_run",
        target_id=audit_id,
        details=f"Manual audit email send attempted; {sent_count} recipient(s) sent",
        actor_user=current_user(),
    )
    return redirect(url_for("admin.dashboard"))
