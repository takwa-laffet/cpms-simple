"""Configuration settings for CPMS backend."""
import os
from pathlib import Path

# Data storage paths
DATA_DIR = Path("cpms_data")
DATA_DIR.mkdir(exist_ok=True)

EVENTS_FILE = DATA_DIR / "events.jsonl"
STATE_FILE = DATA_DIR / "state_snapshot.json"
BORNE_LOGS_DIR = DATA_DIR / "borne_logs"
BORNE_LOGS_DIR.mkdir(exist_ok=True)
META_FILE = DATA_DIR / "borne_meta.json"
USERS_FILE = DATA_DIR / "users.json"
CHARGE_POINTS_FILE = DATA_DIR / "charge_points.json"

# Frontend paths
FRONTEND_DIR = Path("frontend")
FRONTEND_INDEX_FILE = FRONTEND_DIR / "index.html"
FRONTEND_DASHBOARD_FILE = FRONTEND_DIR / "dashboard.html"

# Auth settings
AUTH_COOKIE_NAME = "cpms_auth"
AUTH_EMAIL = os.environ.get("CPMS_LOGIN_EMAIL", "mvp@prelabel.tn")
AUTH_PASSWORD = os.environ.get("CPMS_LOGIN_PASSWORD", "Cpms_Secure#48Tz@2026")
AUTH_SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", "cityos-dev-secret")
AUTH_SESSION_TTL_SECONDS = int(os.environ.get("AUTH_SESSION_TTL_SECONDS", "86400"))

# Billing settings
BILLING_TARIFF_PER_KWH = float(os.environ.get("CITYOS_TARIFF_PER_KWH", "0.35"))

# Role permissions
SUPERVISION_ROLES = {"admin", "steg"}
FULL_MANAGEMENT_ROLES = {"admin"}
USER_MANAGEMENT_ROLES = {"admin"}