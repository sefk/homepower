"""
Django settings for the 635 Central home power monitor.

One process on studio, LAN-only, SQLite. Secrets come from `.env`
(gitignored) via python-dotenv; see README for the expected keys.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# Runtime state that must survive restarts but stays out of git:
# sqlite db lives in BASE_DIR, logs and cached vendor tokens under var/.
VAR_DIR = BASE_DIR / "var"
LOG_DIR = VAR_DIR / "log"

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    # LAN-only single-user app; a checked-in fallback is acceptable here.
    "homepower-lan-only-not-a-secret",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"

# Serves the whole LAN plus localhost; no auth, no remote access (PRD: out of scope).
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "core",
    "collectors",
    "catalog",
    "billing",
]

MIDDLEWARE = [
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "homepower.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "homepower.wsgi.application"
ASGI_APPLICATION = "homepower.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        # WAL lets the web reads and collector writes coexist; the busy
        # timeout covers the occasional write collision instead of erroring.
        "OPTIONS": {
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;",
            "transaction_mode": "IMMEDIATE",
        },
    }
}

# Samples are stored UTC; analysis happens in house-local time (TOU windows,
# true-up cycle, DST all live in America/Los_Angeles).
LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Los_Angeles"
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = VAR_DIR / "staticfiles"
# Serve straight from static/ — no collectstatic step to forget at deploy.
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- homepower ---

SERVE_HOST = os.environ.get("HOMEPOWER_HOST", "0.0.0.0")
SERVE_PORT = int(os.environ.get("HOMEPOWER_PORT", "8425"))

# Enphase Envoy (ADU solar). Credentials are Enlighten cloud login, used
# only to mint/refresh the local-API JWT (firmware D7+ requirement).
ENVOY_HOST = os.environ.get("ENVOY_HOST", "10.10.0.222")
ENPHASE_USERNAME = os.environ.get("ENPHASE_USERNAME", "")
ENPHASE_PASSWORD = os.environ.get("ENPHASE_PASSWORD", "")
ENPHASE_TOKEN_FILE = VAR_DIR / "enphase_token.json"

# SolarEdge cloud (main solar). The monitoring.solaredge.com portal login
# (no API key needed) and the site id from the portal URL; leave blank to
# disable the collector.
SOLAREDGE_USERNAME = os.environ.get("SOLAREDGE_USERNAME", "")
SOLAREDGE_PASSWORD = os.environ.get("SOLAREDGE_PASSWORD", "")
SOLAREDGE_SITE_ID = os.environ.get("SOLAREDGE_SITE_ID", "")
SOLAREDGE_TOKEN_FILE = VAR_DIR / "solaredge_token.json"

# PG&E (gas). pge.com login, read through the opower library. PG&E texts
# or emails a code on first sign-in; `manage.py pge_auth` takes it once and
# saves the remembered-device cookie to PGE_LOGIN_FILE. Leave blank to
# disable the collector.
PGE_USERNAME = os.environ.get("PGE_USERNAME", "")
PGE_PASSWORD = os.environ.get("PGE_PASSWORD", "")
PGE_LOGIN_FILE = VAR_DIR / "pge_login.json"

# Tesla Fleet API (EV, Model S). Refresh token minted by `manage.py
# tesla_auth`; see ops/tesla-setup.md for the developer-app registration
# and public-key hosting steps. Leave TESLA_CLIENT_ID/TESLA_REFRESH_TOKEN
# blank to disable the collector. TESLA_VIN is optional -- when blank the
# collector reads the account's first vehicle.
TESLA_CLIENT_ID = os.environ.get("TESLA_CLIENT_ID", "")
TESLA_CLIENT_SECRET = os.environ.get("TESLA_CLIENT_SECRET", "")
TESLA_REFRESH_TOKEN = os.environ.get("TESLA_REFRESH_TOKEN", "")
TESLA_VIN = os.environ.get("TESLA_VIN", "")
TESLA_REGION = os.environ.get("TESLA_REGION", "na")
TESLA_TOKEN_FILE = VAR_DIR / "tesla_token.json"
# The Fleet API bills per request ($10/month credit). The collector stops
# calling it for the rest of the month once this many requests are spent.
TESLA_MONTHLY_REQUEST_BUDGET = int(os.environ.get("TESLA_MONTHLY_REQUEST_BUDGET", "4000"))
TESLA_USAGE_FILE = VAR_DIR / "tesla_usage.json"
# Key pair whose public half is hosted on the app's domain; see
# `manage.py tesla_register`.
TESLA_PUBLIC_KEY_FILE = VAR_DIR / "tesla" / "com.tesla.3p.public-key.pem"

# Home coordinates, for the Tesla collector's best-effort home-charging
# geofence (PRD wants home charging cost, not Supercharger stops).
# Blank disables the geofence -- every charging reading counts as home.
_home_lat = os.environ.get("HOME_LAT")
_home_lon = os.environ.get("HOME_LON")
HOME_LAT = float(_home_lat) if _home_lat else None
HOME_LON = float(_home_lon) if _home_lon else None

# Rainforest Eagle 3 (grid). It pushes to /ingest/eagle/; the creds are
# only for configuring the device / its local API, never for ingest.
EAGLE_HOST = os.environ.get("EAGLE_HOST", "10.10.0.216")
EAGLE_CLOUD_ID = os.environ.get("EAGLE_CLOUD_ID", "")
EAGLE_INSTALL_CODE = os.environ.get("EAGLE_INSTALL_CODE", "")
# Nominal push cadence: drives native resolution, staleness (3x) and
# demand-coverage grace (2x).
EAGLE_NOMINAL_INTERVAL_S = int(os.environ.get("EAGLE_NOMINAL_INTERVAL_S", "15"))

# Installed capacity, for /solarhealth/'s W/kW normalization (PRD: "Main vs
# ADU, normalized per kW installed"). Main array: 25 x 345W SunPower X21
# (docs/635-central-energy-analysis-gemini.md). ADU is a PRD assumption --
# 5 x Enphase IQ7+ -- and should be replaced with the real figure read off
# the Envoy once that's available (PRD "Assumptions" table).
SOLAR_MAIN_KW = 8.625
SOLAR_ADU_KW = 1.75

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_DIR / "homepower.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "plain",
            "delay": True,  # don't create the file until first write
        },
    },
    "loggers": {
        "homepower": {"handlers": ["console", "file"], "level": "INFO"},
        "collectors": {"handlers": ["console", "file"], "level": "INFO"},
    },
}

# LOGGING's RotatingFileHandler opens lazily, but the directory must exist.
LOG_DIR.mkdir(parents=True, exist_ok=True)
