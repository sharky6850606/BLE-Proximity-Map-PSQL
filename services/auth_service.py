from functools import wraps
import time

from flask import Blueprint, g, redirect, render_template, request, session, url_for, flash
from werkzeug.security import check_password_hash, generate_password_hash

from database import get_db
from services.audit_log_service import log_event
from services.beacon_logic import format_samoa_time

auth_bp = Blueprint("auth", __name__)


def _ph(conn):
    return "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"


def _now_label():
    return format_samoa_time(time.time()).replace(" ", "T")


def normalize_email(email):
    return (email or "").strip().lower()


def current_user():
    if hasattr(g, "current_user"):
        return g.current_user

    user_id = session.get("user_id")
    if not user_id:
        g.current_user = None
        return None

    conn = get_db()
    try:
        ph = _ph(conn)
        row = conn.execute(
            "SELECT u.id, u.customer_id, u.email, u.role, u.active, "
            "c.name AS customer_name, c.active AS customer_active "
            f"FROM app_users u LEFT JOIN customers c ON c.id = u.customer_id WHERE u.id = {ph}",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        session.clear()
        g.current_user = None
        return None

    user = {
        "id": row[0],
        "customer_id": row[1],
        "email": row[2],
        "role": row[3],
        "active": bool(row[4]),
        "customer_name": row[5],
        "customer_active": True if row[6] is None else bool(row[6]),
    }
    if not user["active"] or not user["customer_active"]:
        session.clear()
        g.current_user = None
        return None

    g.current_user = user
    return user


def is_admin(user=None):
    user = user or current_user()
    return bool(user and user.get("role") == "admin")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("auth.login", next=request.path))
        if not is_admin():
            return redirect(url_for("map.map_page"))
        return view(*args, **kwargs)

    return wrapped


def allowed_device_idents(conn=None, user=None):
    user = user or current_user()
    if not user:
        return set()
    if is_admin(user):
        return None

    should_close = conn is None
    conn = conn or get_db()
    try:
        ph = _ph(conn)
        rows = conn.execute(
            f"SELECT device_ident FROM customer_devices WHERE customer_id = {ph}",
            (user.get("customer_id"),),
        ).fetchall()
        return {str(r[0]) for r in rows if r and r[0]}
    finally:
        if should_close:
            conn.close()


def device_scope_clause(conn, column_name, user=None):
    allowed = allowed_device_idents(conn=conn, user=user)
    if allowed is None:
        return "1=1", ()
    if not allowed:
        return "1=0", ()
    ph = _ph(conn)
    placeholders = ",".join([ph] * len(allowed))
    return f"{column_name} IN ({placeholders})", tuple(sorted(allowed))


def can_access_device(device_ident, conn=None, user=None):
    user = user or current_user()
    if not user:
        return False
    if is_admin(user):
        return True
    allowed = allowed_device_idents(conn=conn, user=user)
    return str(device_ident) in allowed


@auth_bp.app_context_processor
def inject_auth_user():
    return {"current_user": current_user(), "is_admin_user": is_admin()}


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("admin.dashboard" if is_admin() else "map.map_page"))

    error = None
    if request.method == "POST":
        email = normalize_email(request.form.get("email"))
        password = request.form.get("password") or ""
        conn = get_db()
        try:
            ph = _ph(conn)
            row = conn.execute(
                "SELECT id, password_hash, active FROM app_users "
                f"WHERE email = {ph} AND deleted_at IS NULL",
                (email,),
            ).fetchone()
            if row and bool(row[2]) and check_password_hash(row[1], password):
                session.clear()
                session["user_id"] = row[0]
                user_row = conn.execute(
                    "SELECT u.id, u.customer_id, u.email, u.role, c.name "
                    "FROM app_users u LEFT JOIN customers c ON c.id = u.customer_id "
                    f"WHERE u.id = {ph}",
                    (row[0],),
                ).fetchone()
                actor = {
                    "id": user_row[0],
                    "customer_id": user_row[1],
                    "email": user_row[2],
                    "role": user_row[3],
                    "customer_name": user_row[4],
                } if user_row else {"id": row[0], "email": email}
                conn.execute(
                    f"UPDATE app_users SET last_login_at = {ph} WHERE id = {ph}",
                    (_now_label(), row[0]),
                )
                log_event(
                    "login",
                    target_type="user",
                    target_id=row[0],
                    details=f"{actor.get('email')} signed in",
                    actor_user=actor,
                    conn=conn,
                )
                conn.commit()
                next_url = request.args.get("next") or ""
                if next_url.startswith("/"):
                    return redirect(next_url)
                return redirect(url_for("map.map_page"))
            error = "Invalid email or password."
        finally:
            conn.close()

    return render_template("login.html", error=error)


@auth_bp.route("/logout")
def logout():
    user = current_user()
    if user:
        log_event(
            "logout",
            target_type="user",
            target_id=user.get("id"),
            details=f"{user.get('email')} signed out",
            actor_user=user,
        )
    session.clear()
    return redirect(url_for("auth.login"))


def hash_password(password):
    return generate_password_hash(password)
