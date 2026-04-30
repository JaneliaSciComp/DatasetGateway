#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PID_FILE="serve.pid"

if [ ! -f "${PID_FILE}" ]; then
    echo "No ${PID_FILE} found — no detached serve to stop."
    exit 0
fi

PID=$(cat "${PID_FILE}")

if ! kill -0 "${PID}" 2>/dev/null; then
    echo "Process ${PID} is not running — removing stale ${PID_FILE}."
    rm -f "${PID_FILE}"
    exit 0
fi

echo "Stopping detached serve (PID ${PID})..."
kill "${PID}"

# Wait briefly for graceful shutdown
for _ in 1 2 3 4 5; do
    if ! kill -0 "${PID}" 2>/dev/null; then
        break
    fi
    sleep 1
done

if kill -0 "${PID}" 2>/dev/null; then
    echo "Process did not exit; sending SIGKILL."
    kill -9 "${PID}" || true
fi

rm -f "${PID_FILE}"
echo "Stopped."
