"""Django settings for DatasetGate."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-key-change-in-production",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() in ("true", "1", "yes")

ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "core",
    "cave_api",
    "auth_api",
    "ngauth",
    "scim",
    "web",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "datasetgate.middleware.DatasetContextMiddleware",
]

ROOT_URLCONF = "datasetgate.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "datasetgate.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "datasetgate-cache",
        "TIMEOUT": 300,
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# DRF configuration
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "core.authentication.CaveTokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    "UNAUTHENTICATED_USER": None,
}

# Session configuration
SESSION_COOKIE_NAME = "datasetgate_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 7  # 7 days

# Google OAuth configuration
# Precedence: env vars > secrets/client_credentials.json
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
    _creds_path = Path(
        os.environ.get("CLIENT_CREDENTIALS_PATH", BASE_DIR / "secrets" / "client_credentials.json")
    )
    if _creds_path.exists():
        import json

        try:
            _creds = json.loads(_creds_path.read_text())
            _web = _creds.get("web") or _creds.get("installed") or {}
            GOOGLE_CLIENT_ID = GOOGLE_CLIENT_ID or _web.get("client_id", "")
            GOOGLE_CLIENT_SECRET = GOOGLE_CLIENT_SECRET or _web.get("client_secret", "")
        except (json.JSONDecodeError, KeyError):
            pass

# ngauth configuration
NGAUTH_ALLOWED_ORIGINS = os.environ.get(
    "NGAUTH_ALLOWED_ORIGINS", r"^https?://.*\.neuroglancer\.org$"
)
NGAUTH_COOKIE_NAME = "ngauth_login"

# CAVE auth token cookie
CAVE_TOKEN_COOKIE_NAME = "middle_auth_token"
CAVE_TOKEN_COOKIE_AGE = 60 * 60 * 24 * 7  # 7 days

# Cross-subdomain cookie domain (e.g., ".example.org" to share cookies
# across auth.example.org and app.example.org). Empty = browser default.
AUTH_COOKIE_DOMAIN = os.environ.get("AUTH_COOKIE_DOMAIN", "")

# Permission cache TTL
PERMISSION_CACHE_TTL = 300  # seconds
