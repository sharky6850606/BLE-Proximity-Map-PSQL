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
