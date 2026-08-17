import os

# =============================
# Database
# =============================
DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or None
DB_PATH = os.getenv("DB_PATH", "beacons.db")  # used only if DATABASE_URL is not set

# =============================
# Time / TTL
# =============================
SAMOA_OFFSET_HOURS = int(os.getenv("SAMOA_OFFSET_HOURS", "13"))
TTL_SECONDS = int(os.getenv("TTL_SECONDS", "900"))  # 15 minutes (flespi packets can be every 5 minutes)

# =============================
# RSSI distance model (simple)
# =============================
TX_POWER = float(os.getenv("TX_POWER", "-59"))
PATH_LOSS_N = float(os.getenv("PATH_LOSS_N", "2.0"))

# =============================
# Files
# =============================
REPORTS_DIR = os.getenv("REPORTS_DIR", "reports")
ACTIVITY_REPORTS_DIR = os.getenv("ACTIVITY_REPORTS_DIR", "activity_reports")
AUDIT_REPORTS_DIR = os.getenv("AUDIT_REPORTS_DIR", "audit_reports")

# =============================
# Daily equipment audit
# =============================
AUDIT_HOUR = int(os.getenv("AUDIT_HOUR", "18"))  # 6 PM Samoa time
AUDIT_MINUTE = int(os.getenv("AUDIT_MINUTE", "0"))
AUDIT_WINDOW_BEFORE_MIN = int(os.getenv("AUDIT_WINDOW_BEFORE_MIN", "15"))
AUDIT_WINDOW_AFTER_MIN = int(os.getenv("AUDIT_WINDOW_AFTER_MIN", "15"))
AUDIT_EMAIL_HOUR = int(os.getenv("AUDIT_EMAIL_HOUR", "18"))  # 6:30 PM Samoa time
AUDIT_EMAIL_MINUTE = int(os.getenv("AUDIT_EMAIL_MINUTE", "30"))

# =============================
# Email
# =============================
MAIL_PROVIDER = os.getenv("MAIL_PROVIDER", "smtp").strip().lower()
MAIL_FROM = os.getenv("MAIL_FROM", "").strip()
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "1").strip().lower() not in ("0", "false", "no", "off")
SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "0").strip().lower() in ("1", "true", "yes", "on")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "").strip()

# =============================
# n8n / WhatsApp audit delivery
# =============================
N8N_WHATSAPP_WEBHOOK_URL = os.getenv("N8N_WHATSAPP_WEBHOOK_URL", "").strip()
N8N_WEBHOOK_SECRET = os.getenv("N8N_WEBHOOK_SECRET", "").strip()
N8N_WEBHOOK_TIMEOUT = int(os.getenv("N8N_WEBHOOK_TIMEOUT", "20"))

# =============================
# Real-time FMC offline alerts
# =============================
FMC_OFFLINE_ALERT_AFTER_SECONDS = int(os.getenv("FMC_OFFLINE_ALERT_AFTER_SECONDS", "600"))
FMC_OFFLINE_ALERT_CHECK_SECONDS = int(os.getenv("FMC_OFFLINE_ALERT_CHECK_SECONDS", "60"))
