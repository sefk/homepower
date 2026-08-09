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

# SolarEdge cloud (main solar). Key from monitoring.solaredge.com; leave
# blank to disable the collector.
SOLAREDGE_API_KEY = os.environ.get("SOLAREDGE_API_KEY", "")
SOLAREDGE_SITE_ID = os.environ.get("SOLAREDGE_SITE_ID", "")

# Rainforest Eagle 3 (grid). It pushes to /ingest/eagle/; the creds are
# only for configuring the device / its local API, never for ingest.
EAGLE_HOST = os.environ.get("EAGLE_HOST", "10.10.0.216")
EAGLE_CLOUD_ID = os.environ.get("EAGLE_CLOUD_ID", "")
EAGLE_INSTALL_CODE = os.environ.get("EAGLE_INSTALL_CODE", "")
# Nominal push cadence: drives native resolution, staleness (3x) and
# demand-coverage grace (2x).
EAGLE_NOMINAL_INTERVAL_S = int(os.environ.get("EAGLE_NOMINAL_INTERVAL_S", "15"))

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
