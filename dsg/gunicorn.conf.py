"""Gunicorn configuration for DatasetGateway."""

import os

# GUNICORN_BIND wins when set (e.g. "127.0.0.1:8200" behind a host nginx, so the
# app port is not exposed on all interfaces). Otherwise bind 0.0.0.0:$PORT, which
# is what the Docker container wants (published explicitly by the orchestrator).
bind = os.environ.get("GUNICORN_BIND") or f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
timeout = 120
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")
