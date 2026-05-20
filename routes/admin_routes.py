import re
import time
from datetime import timedelta

from flask import Blueprint, redirect, render_template, request, url_for

from database import get_db
from services.auth_service import admin_required, current_user, hash_password, normalize_email
from services.audit_log_service import log_event
from config import AUDIT_WINDOW_AFTER_MIN
from services.audit_service import run_customer_audit, _local_dt_from_unix
from services.beacon_logic import format_samoa_time

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _ph(conn):
    return "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"


def _now_label():
    return format_samoa_time(time.time()).replace(" ", "T")


def _slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "customer"


@admin_bp.route("/")
@admin_required
def dashboard():
    conn = get_db()
    try:
        customers = conn.execute(
            "SELECT id, name, slug, active, created_at FROM customers ORDER BY name"
        ).fetchall()
        users = conn.execute(
            "SELECT u.id, u.email, u.role, u.active, c.name, u.last_login_at "
            "FROM app_users u LEFT JOIN customers c ON c.id = u.customer_id "
            "WHERE u.deleted_at IS NULL ORDER BY u.id DESC LIMIT 100"
        ).fetchall()
        devices = conn.execute(
            "SELECT cd.id, c.name, cd.device_ident, COALESCE(cds.name, cd.label, d.name, cd.device_ident) AS label, cd.created_at "
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
            "ca.active, ca.status, ca.missing_since, ca.found_at "
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
    )


@admin_bp.route("/customers", methods=["POST"])
@admin_required
def create_customer():
    name = (request.form.get("name") or "").strip()
    if not name:
        return redirect(url_for("admin.dashboard"))
    slug = _slugify(request.form.get("slug") or name)
    conn = get_db()
    try:
        ph = _ph(conn)
        conn.execute(
            f"INSERT INTO customers (name, slug, active, created_at) VALUES ({ph},{ph},{ph},{ph})",
            (name, slug, 1, _now_label()),
        )
        log_event(
            "admin.create_customer",
            target_type="customer",
            target_id=slug,
            details=f"Created customer {name}",
            actor_user=current_user(),
            conn=conn,
        )
        conn.commit()
    finally:
        conn.close()
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
    customer_id = request.form.get("customer_id") or None
    if role != "admin" and not customer_id:
        return redirect(url_for("admin.dashboard"))
    if not email or not password:
        return redirect(url_for("admin.dashboard"))

    conn = get_db()
    try:
        ph = _ph(conn)
        conn.execute(
            f"INSERT INTO app_users (customer_id, email, password_hash, role, active, created_at) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph})",
            (customer_id, email, hash_password(password), role, 1, _now_label()),
        )
        log_event(
            "admin.create_user",
            target_type="user",
            target_id=email,
            details=f"Created {role} account {email}",
            actor_user=current_user(),
            customer_id=customer_id,
            conn=conn,
        )
        conn.commit()
    finally:
        conn.close()
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
