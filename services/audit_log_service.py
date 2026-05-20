import time

from flask import has_request_context, request

from database import get_db
from services.beacon_logic import format_samoa_time


def _ph(conn):
    return "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"


def _now_label():
    return format_samoa_time(time.time()).replace(" ", "T")


def _client_ip():
    if not has_request_context():
        return ""
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded or request.remote_addr or ""


def log_event(action, target_type=None, target_id=None, details=None, actor_user=None, customer_id=None, conn=None):
    """Record a security/admin audit event.

    `actor_user` is expected to be the auth_service current_user dict when
    available. Login can pass a small dict built from the authenticated row.
    """
    if not action:
        return

    actor_user = actor_user or {}
    resolved_customer_id = customer_id
    if resolved_customer_id is None:
        resolved_customer_id = actor_user.get("customer_id")

    should_close = conn is None
    conn = conn or get_db()
    try:
        ph = _ph(conn)
        conn.execute(
            f"INSERT INTO audit_logs "
            f"(created_at, actor_user_id, actor_email, actor_role, customer_id, action, target_type, target_id, details, ip_address) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
            (
                _now_label(),
                actor_user.get("id"),
                actor_user.get("email"),
                actor_user.get("role"),
                resolved_customer_id,
                action,
                target_type,
                str(target_id) if target_id is not None else None,
                details,
                _client_ip(),
            ),
        )
        if should_close:
            conn.commit()
    finally:
        if should_close:
            conn.close()
