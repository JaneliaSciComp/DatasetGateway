#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    python scripts/setup.py
fi

# Source .env
set -a
source .env
set +a

echo
echo "Starting development server on port ${DSG_PORT:-8200}..."
exec python manage.py runserver "${DSG_PORT:-8200}"
