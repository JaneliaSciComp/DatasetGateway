#!/usr/bin/env bash
#
# Production WSGI server: gunicorn with DEBUG=False + WhiteNoise static serving.
#
# This is the counterpart to scripts/serve.sh (the DEBUG=True dev runserver).
# It is meant to be launched by systemd (see scripts/datasetgateway.service),
# but is also runnable directly for testing:  pixi run serve-prod
#
# Unlike runserver, this requires production settings in .env:
#   DJANGO_DEBUG=False
#   DJANGO_SECRET_KEY=<strong random secret>
#   DJANGO_ALLOWED_HOSTS=dataset-gateway.janelia.org,dataset-gateway.int.janelia.org,dsg.int.janelia.org
#   DSG_ORIGIN=https://dataset-gateway.janelia.org
# (Django refuses to start with DEBUG=False if SECRET_KEY/ALLOWED_HOSTS are unset.)
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "No .env found. Run: pixi run setup" >&2
    exit 1
fi

# Source .env (simple KEY=VALUE lines) into the environment.
set -a
# shellcheck disable=SC1091
source .env
set +a

# Default to production hardening even if .env forgot to flip it.
export DJANGO_DEBUG="${DJANGO_DEBUG:-False}"

# Bind to localhost so only the host nginx reaches the app port. The dev
# runserver bound 127.0.0.1:8200; keep parity unless GUNICORN_BIND overrides.
PORT="${DSG_PORT:-8200}"
export GUNICORN_BIND="${GUNICORN_BIND:-127.0.0.1:${PORT}}"

# One worker by default: CACHES uses per-process LocMemCache, so the permission
# cache is not shared across workers. Raise GUNICORN_WORKERS only after moving
# to a shared cache backend (Redis/Memcached).
export GUNICORN_WORKERS="${GUNICORN_WORKERS:-1}"

# Refresh collected static so WhiteNoise's manifest (staticfiles/staticfiles.json)
# is current before we start serving.
python manage.py collectstatic --noinput

echo "Starting gunicorn on ${GUNICORN_BIND} (workers=${GUNICORN_WORKERS}, DEBUG=${DJANGO_DEBUG})..."
exec gunicorn -c gunicorn.conf.py dsg.wsgi:application
