#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "No .env file found — let's set one up."
    echo

    read -rp "Public origin (e.g., https://dataset-gateway.mydomain.org): " dsg_origin
    if [ -z "$dsg_origin" ]; then
        echo "Error: origin cannot be empty." >&2
        exit 1
    fi

    read -rp "Server port [8200]: " dsg_port
    dsg_port="${dsg_port:-8200}"

    cat > .env <<EOF
DSG_ORIGIN=${dsg_origin}
DSG_PORT=${dsg_port}
EOF

    echo
    echo "Created .env"
else
    echo "Using existing .env"

    source .env
    changed=false

    if [ -z "${DSG_ORIGIN:-}" ]; then
        read -rp "Public origin (e.g., https://dataset-gateway.mydomain.org): " dsg_origin
        if [ -z "$dsg_origin" ]; then
            echo "Error: origin cannot be empty." >&2
            exit 1
        fi
        echo "DSG_ORIGIN=${dsg_origin}" >> .env
        export DSG_ORIGIN="$dsg_origin"
        changed=true
    fi

    if [ -z "${DSG_PORT:-}" ]; then
        read -rp "Server port [8200]: " dsg_port
        echo "DSG_PORT=${dsg_port:-8200}" >> .env
        export DSG_PORT="${dsg_port:-8200}"
        changed=true
    fi

    if [ "$changed" = true ]; then
        echo "Updated .env with missing settings"
    fi
fi

# Check for Google OAuth credentials
if [ ! -f secrets/client_credentials.json ]; then
    echo "Warning: secrets/client_credentials.json not found — Google login will not work."
fi

# Re-source to pick up all values
set -a
source .env
set +a

echo
echo "Starting development server on port ${DSG_PORT:-8200}..."
exec python manage.py runserver "${DSG_PORT:-8200}"
