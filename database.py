import sqlite3
import psycopg
from psycopg.rows import tuple_row

from config import DATABASE_URL, DB_PATH


def get_db():
    """Return a DB connection.

    - Postgres: psycopg v3 (when DATABASE_URL is set)
    - SQLite: fallback for local dev
    """
    if DATABASE_URL:
        conn = psycopg.connect(DATABASE_URL, row_factory=tuple_row)
        conn.backend = "postgres"
        return conn

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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

            # beacon states
            cur.execute("""
                CREATE TABLE IF NOT EXISTS beacon_states (
                    beacon_key TEXT PRIMARY KEY
                )
            """)
            cur.execute("ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS state TEXT")
            cur.execute("ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS last_change_ts BIGINT")
            cur.execute("ALTER TABLE beacon_states ADD COLUMN IF NOT EXISTS active INTEGER DEFAULT 1")

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
                last_online_ts INTEGER
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT,
                beacon_name TEXT,
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
                active INTEGER DEFAULT 1
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
        conn.commit()
        print("[init_db] sqlite schema ready ✅")
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()
