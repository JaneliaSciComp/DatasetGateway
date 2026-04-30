#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

DETACH=0
for arg in "$@"; do
    case "$arg" in
        -d|--detach) DETACH=1 ;;
    esac
done

if [ ! -f .env ]; then
    python scripts/setup.py
fi

# Source .env
set -a
source .env
set +a

PORT="${DSG_PORT:-8200}"

if [ "$DETACH" = "1" ]; then
    LOG_FILE="serve.log"
    PID_FILE="serve.pid"

    if [ -f "${PID_FILE}" ]; then
        EXISTING_PID=$(cat "${PID_FILE}")
        if kill -0 "${EXISTING_PID}" 2>/dev/null; then
            echo "Detached serve already running (PID ${EXISTING_PID})."
            echo "Stop it first with: pixi run stop-serve"
            exit 1
        fi
        echo "Removing stale ${PID_FILE} (PID ${EXISTING_PID} not running)."
        rm -f "${PID_FILE}"
    fi

    echo "Starting development server (detached) on port ${PORT}..."
    # --noreload so the saved PID is the actual server process, not the autoreloader parent
    nohup python manage.py runserver --noreload "${PORT}" >> "${LOG_FILE}" 2>&1 &
    PID=$!
    echo "${PID}" > "${PID_FILE}"
    disown "${PID}"
    echo "PID:  ${PID} (saved to dsg/${PID_FILE})"
    echo "Logs: dsg/${LOG_FILE}"
    echo "Stop: pixi run stop-serve"
else
    echo
    echo "Starting development server on port ${PORT}..."
    exec python manage.py runserver "${PORT}"
fi
