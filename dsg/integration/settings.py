"""Disposable joined-test settings; loaded only by the early opt-in plugin."""

import os

if os.environ.get("DSG_JOINED_BOOTSTRAP") != "1":
    raise RuntimeError("Load joined tests with -p integration.pytest_plugin --run-joined")

from dsg.settings import *  # noqa: F403,E402

DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
AUTH_COOKIE_DOMAIN = ""
CSRF_TRUSTED_ORIGINS = ["http://127.0.0.1", "http://testserver"]
NGAUTH_ALLOWED_ORIGINS = r"http://127\.0\.0\.1:8765"
TOS_RETURN_ALLOWED_ORIGINS = ["http://127.0.0.1:8765"]
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
# The default test DB is SQLite's shared in-memory DB. Set the file-backed
# switch only when diagnosing the documented live-server locking condition.
if os.environ.get("DSG_JOINED_FILE_DB") == "1":
    DATABASES["default"]["TEST"] = {"NAME": os.path.join(os.environ["DSG_JOINED_WORKDIR"], "test.sqlite3")}  # noqa: F405
