import sqlite3
import psycopg
from psycopg.rows import tuple_row

from config import DATABASE_URL, DB_PATH


class SQLiteConnection(sqlite3.Connection):
    """SQLite connection subclass that allows custom attributes."""
    pass

POSTGRES_MIGRATIONS = [
    "ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS device_ident TEXT",
    "ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS beacon_id TEXT",
    "ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS last_seen_ts BIGINT",
    "ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS last_distance DOUBLE PRECISION",
    "ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS last_rssi DOUBLE PRECISION",
    "ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS last_status_ts BIGINT",
    "ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS missing INTEGER DEFAULT 0",
    "ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS last_missing_ts BIGINT",
    "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS beacon_id TEXT",
    "ALTER TABLE device_states ADD COLUMN IF NOT EXISTS last_lat DOUBLE PRECISION",
    "ALTER TABLE device_states ADD COLUMN IF NOT EXISTS last_lon DOUBLE PRECISION",
    "ALTER TABLE device_states ADD COLUMN IF NOT EXISTS last_payload_ts BIGINT",
    "ALTER TABLE activity_reports ADD COLUMN IF NOT EXISTS customer_id BIGINT",
]


def _seed_default_admin(conn, cur):
    """Create the first admin from env vars when no admin exists."""
    import os
    from werkzeug.security import generate_password_hash

    email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not email or not password:
        return

    ph = "%s" if getattr(conn, "backend", "postgres") == "postgres" else "?"
    row = cur.execute(f"SELECT COUNT(*) FROM app_users WHERE role = {ph}", ("admin",)).fetchone()
    if row and int(row[0] or 0) > 0:
        return

    password_hash = generate_password_hash(password)
    cur.execute(
        f"INSERT INTO app_users (email, password_hash, role, active, created_at) VALUES ({ph},{ph},{ph},{ph},{ph})",
        (email, password_hash, "admin", 1, "seeded"),
    )


def get_db():
    """Return a DB connection.

    - Postgres: psycopg v3 (when DATABASE_URL is set)
    - SQLite: fallback for local dev
    """
    if DATABASE_URL:
        conn = psycopg.connect(DATABASE_URL, row_factory=tuple_row)
        conn.backend = "postgres"
        return conn

    conn = sqlite3.connect(DB_PATH, check_same_thread=False, factory=SQLiteConnection)
    conn.row_factory = sqlite3.Row
    conn.backend = "sqlite"
    return conn


def init_db():
    """Create/upgrade schema at startup (Postgres-safe)."""
    conn = get_db()
    cur = conn.cursor()
    try:
        if conn.backend == "postgres":
            # devices
            cur.execute("""
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    color TEXT
                )
            """)

            # beacon names
            cur.execute("""
                CREATE TABLE IF NOT EXISTS beacon_names (
                    id TEXT PRIMARY KEY,
                    name TEXT
                )
            """)

            # device states
            cur.execute("""
                CREATE TABLE IF NOT EXISTS device_states (
                    device_key TEXT PRIMARY KEY
                )
            """)
            cur.execute("ALTER TABLE device_states ADD COLUMN IF NOT EXISTS state TEXT")
            cur.execute("ALTER TABLE device_states ADD COLUMN IF NOT EXISTS last_change_ts BIGINT")
            cur.execute("ALTER TABLE device_states ADD COLUMN IF NOT EXISTS device_ident TEXT")
            cur.execute("ALTER TABLE device_states ADD COLUMN IF NOT EXISTS online INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE device_states ADD COLUMN IF NOT EXISTS last_seen_ts BIGINT")
            cur.execute("ALTER TABLE device_states ADD COLUMN IF NOT EXISTS last_online_ts BIGINT")
            cur.execute("ALTER TABLE device_states ADD COLUMN IF NOT EXISTS last_lat DOUBLE PRECISION")
            cur.execute("ALTER TABLE device_states ADD COLUMN IF NOT EXISTS last_lon DOUBLE PRECISION")
            cur.execute("ALTER TABLE device_states ADD COLUMN IF NOT EXISTS last_payload_ts BIGINT")

            # notifications
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id BIGSERIAL PRIMARY KEY
                )
            """)
            cur.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS type TEXT")
            cur.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS beacon_name TEXT")
            cur.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS event_time TEXT")
            cur.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS created_at TEXT")
            cur.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS device_ident TEXT")
            cur.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS distance REAL")
            cur.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS beacon_id TEXT")

            # beacon states
            cur.execute("""
                CREATE TABLE IF NOT EXISTS beacon_states (
                    beacon_key TEXT PRIMARY KEY
                )
            """)
            cur.execute("ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS state TEXT")
            cur.execute("ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS last_change_ts BIGINT")
            cur.execute("ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS active INTEGER DEFAULT 1")
            # extra beacon state fields (for evaluator + missing/found)
            cur.execute("ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS device_ident TEXT")
            cur.execute("ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS beacon_id TEXT")
            cur.execute("ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS last_seen_ts BIGINT")
            cur.execute("ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS last_distance DOUBLE PRECISION")
            cur.execute("ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS last_rssi DOUBLE PRECISION")
            cur.execute("ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS last_status_ts BIGINT")
            cur.execute("ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS missing INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS last_missing_ts BIGINT")

            # uptime logs
            cur.execute("""
                CREATE TABLE IF NOT EXISTS uptime_logs (
                    id BIGSERIAL PRIMARY KEY
                )
            """)
            cur.execute("ALTER TABLE uptime_logs ADD COLUMN IF NOT EXISTS timestamp TEXT")
            cur.execute("ALTER TABLE uptime_logs ADD COLUMN IF NOT EXISTS device_count INTEGER")
            cur.execute("ALTER TABLE uptime_logs ADD COLUMN IF NOT EXISTS beacon_count INTEGER")
            cur.execute("ALTER TABLE uptime_logs ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'OK'")

            # daily reports
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_reports (
                    id BIGSERIAL PRIMARY KEY
                )
            """)
            cur.execute("ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS created_at TEXT")
            cur.execute("ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS summary TEXT")
            cur.execute("ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS pdf_path TEXT")
            cur.execute("ALTER TABLE daily_reports ADD COLUMN IF NOT EXISTS report_json TEXT")

            # activity reports
            cur.execute("""
                CREATE TABLE IF NOT EXISTS activity_reports (
                    id BIGSERIAL PRIMARY KEY
                )
            """)
            cur.execute("ALTER TABLE activity_reports ADD COLUMN IF NOT EXISTS beacon_name TEXT")
            cur.execute("ALTER TABLE activity_reports ADD COLUMN IF NOT EXISTS created_at TEXT")
            cur.execute("ALTER TABLE activity_reports ADD COLUMN IF NOT EXISTS summary TEXT")
            cur.execute("ALTER TABLE activity_reports ADD COLUMN IF NOT EXISTS pdf_path TEXT")
            cur.execute("ALTER TABLE activity_reports ADD COLUMN IF NOT EXISTS customer_id BIGINT")

            # SaaS tenancy / auth
            cur.execute("""
                CREATE TABLE IF NOT EXISTS customers (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    slug TEXT UNIQUE,
                    active INTEGER DEFAULT 1,
                    created_at TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_users (
                    id BIGSERIAL PRIMARY KEY,
                    customer_id BIGINT REFERENCES customers(id),
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'customer_user',
                    active INTEGER DEFAULT 1,
                    force_password_reset INTEGER DEFAULT 0,
                    last_login_at TEXT,
                    created_at TEXT,
                    deleted_at TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS customer_devices (
                    id BIGSERIAL PRIMARY KEY,
                    customer_id BIGINT REFERENCES customers(id),
                    device_ident TEXT NOT NULL,
                    label TEXT,
                    created_at TEXT,
                    UNIQUE(customer_id, device_ident)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS customer_beacon_names (
                    customer_id BIGINT REFERENCES customers(id),
                    beacon_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    updated_at TEXT,
                    PRIMARY KEY (customer_id, beacon_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS customer_device_settings (
                    customer_id BIGINT REFERENCES customers(id),
                    device_ident TEXT NOT NULL,
                    name TEXT,
                    color TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (customer_id, device_ident)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_devices_device ON customer_devices(device_ident)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_notifications_device_ident ON notifications(device_ident)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_beacon_states_device_ident ON beacon_states(device_ident)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS customer_assets (
                    id BIGSERIAL PRIMARY KEY,
                    customer_id BIGINT REFERENCES customers(id),
                    beacon_id TEXT NOT NULL,
                    name TEXT,
                    expected_device_ident TEXT,
                    active INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'unknown',
                    missing_since TEXT,
                    found_at TEXT,
                    last_seen_ts BIGINT,
                    last_seen_device_ident TEXT,
                    created_at TEXT,
                    UNIQUE(customer_id, beacon_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS beacon_observations (
                    id BIGSERIAL PRIMARY KEY,
                    observed_ts BIGINT,
                    device_ident TEXT,
                    beacon_id TEXT,
                    distance DOUBLE PRECISION,
                    rssi DOUBLE PRECISION,
                    created_at TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS audit_runs (
                    id BIGSERIAL PRIMARY KEY,
                    customer_id BIGINT REFERENCES customers(id),
                    scheduled_for TEXT,
                    scan_window_start TEXT,
                    scan_window_end TEXT,
                    status TEXT,
                    pdf_path TEXT,
                    emailed_at TEXT,
                    created_at TEXT,
                    UNIQUE(customer_id, scheduled_for)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS audit_results (
                    id BIGSERIAL PRIMARY KEY,
                    audit_run_id BIGINT REFERENCES audit_runs(id),
                    asset_id BIGINT REFERENCES customer_assets(id),
                    beacon_id TEXT,
                    status TEXT,
                    last_seen_ts BIGINT,
                    last_seen_device_ident TEXT,
                    last_distance DOUBLE PRECISION,
                    last_rssi DOUBLE PRECISION
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_assets_customer ON customer_assets(customer_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_assets_beacon ON customer_assets(beacon_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_beacon_observations_lookup ON beacon_observations(beacon_id, device_ident, observed_ts)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_runs_customer ON audit_runs(customer_id)")

            # Postgres-specific migrations for existing DBs (ignore errors if columns already exist)
            for stmt in POSTGRES_MIGRATIONS:
                cur.execute(stmt)

            _seed_default_admin(conn, cur)
            conn.commit()
            print("[init_db] postgres schema ready ✅")
            return

        # SQLite fallback
        cur.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT,
                color TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS beacon_names (
                id TEXT PRIMARY KEY,
                name TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS device_states (
                device_key TEXT PRIMARY KEY,
                state TEXT,
                last_change_ts INTEGER,
                device_ident TEXT,
                online INTEGER DEFAULT 0,
                last_seen_ts INTEGER,
                last_online_ts INTEGER,
                last_lat REAL,
                last_lon REAL,
                last_payload_ts INTEGER
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT,
                beacon_name TEXT,
                beacon_id TEXT,
                event_time TEXT,
                created_at TEXT,
                device_ident TEXT,
                distance REAL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS beacon_states (
                beacon_key TEXT PRIMARY KEY,
                state TEXT,
                last_change_ts INTEGER,
                active INTEGER DEFAULT 1,
                device_ident TEXT,
                beacon_id TEXT,
                last_seen_ts INTEGER,
                last_distance REAL,
                last_rssi REAL,
                last_status_ts INTEGER,
                missing INTEGER DEFAULT 0,
                last_missing_ts INTEGER
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS uptime_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                device_count INTEGER,
                beacon_count INTEGER,
                status TEXT DEFAULT 'OK'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT,
                summary TEXT,
                pdf_path TEXT,
                report_json TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS activity_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                beacon_name TEXT,
                created_at TEXT,
                summary TEXT,
                pdf_path TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT UNIQUE,
                active INTEGER DEFAULT 1,
                created_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER REFERENCES customers(id),
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'customer_user',
                active INTEGER DEFAULT 1,
                force_password_reset INTEGER DEFAULT 0,
                last_login_at TEXT,
                created_at TEXT,
                deleted_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER REFERENCES customers(id),
                device_ident TEXT NOT NULL,
                label TEXT,
                created_at TEXT,
                UNIQUE(customer_id, device_ident)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_beacon_names (
                customer_id INTEGER REFERENCES customers(id),
                beacon_id TEXT NOT NULL,
                name TEXT NOT NULL,
                updated_at TEXT,
                PRIMARY KEY (customer_id, beacon_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_device_settings (
                customer_id INTEGER REFERENCES customers(id),
                device_ident TEXT NOT NULL,
                name TEXT,
                color TEXT,
                updated_at TEXT,
                PRIMARY KEY (customer_id, device_ident)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_devices_device ON customer_devices(device_ident)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_notifications_device_ident ON notifications(device_ident)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_beacon_states_device_ident ON beacon_states(device_ident)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER REFERENCES customers(id),
                beacon_id TEXT NOT NULL,
                name TEXT,
                expected_device_ident TEXT,
                active INTEGER DEFAULT 1,
                status TEXT DEFAULT 'unknown',
                missing_since TEXT,
                found_at TEXT,
                last_seen_ts INTEGER,
                last_seen_device_ident TEXT,
                created_at TEXT,
                UNIQUE(customer_id, beacon_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS beacon_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_ts INTEGER,
                device_ident TEXT,
                beacon_id TEXT,
                distance REAL,
                rssi REAL,
                created_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER REFERENCES customers(id),
                scheduled_for TEXT,
                scan_window_start TEXT,
                scan_window_end TEXT,
                status TEXT,
                pdf_path TEXT,
                emailed_at TEXT,
                created_at TEXT,
                UNIQUE(customer_id, scheduled_for)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                audit_run_id INTEGER REFERENCES audit_runs(id),
                asset_id INTEGER REFERENCES customer_assets(id),
                beacon_id TEXT,
                status TEXT,
                last_seen_ts INTEGER,
                last_seen_device_ident TEXT,
                last_distance REAL,
                last_rssi REAL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_assets_customer ON customer_assets(customer_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_assets_beacon ON customer_assets(beacon_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_beacon_observations_lookup ON beacon_observations(beacon_id, device_ident, observed_ts)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_runs_customer ON audit_runs(customer_id)")

        # SQLite migrations for existing DBs
        cur.execute("PRAGMA table_info(device_states)")
        existing_cols = {r[1] for r in cur.fetchall()}
        if "last_lat" not in existing_cols:
            cur.execute("ALTER TABLE device_states ADD COLUMN last_lat REAL")
        if "last_lon" not in existing_cols:
            cur.execute("ALTER TABLE device_states ADD COLUMN last_lon REAL")
        if "last_payload_ts" not in existing_cols:
            cur.execute("ALTER TABLE device_states ADD COLUMN last_payload_ts INTEGER")
        cur.execute("PRAGMA table_info(activity_reports)")
        existing_cols = {r[1] for r in cur.fetchall()}
        if "customer_id" not in existing_cols:
            cur.execute("ALTER TABLE activity_reports ADD COLUMN customer_id INTEGER")

        _seed_default_admin(conn, cur)
        conn.commit()
        print("[init_db] sqlite schema ready ✅")
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()
