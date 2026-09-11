#!/usr/bin/env bash
# Ship rotated serve.log files to nearline, verify them there, then trim local.
#
# Runs as ExecStartPost of datasetgateway-logrotate.service, right after
# logrotate has produced logs/serve.log-<stamp> files (plaintext, immutable).
# Transfer contract, same shape as backup_db's bundle step:
#   for every logs/serve.log-* (newest first):
#     - already on nearline with a matching sha256  -> counts as shipped
#     - else cp to $DSG_BACKUP_DIR/logs/<name>.part, sha256 the copy against
#       the source, mv into the final name; on mismatch remove the .part, keep
#       the source, and fail the run (non-zero exit, visible in systemctl status)
#   then delete local files that are verified on nearline, except the
#   DSG_LOG_KEEP_LOCAL newest (default 2), which stay for quick reading.
# Nothing is ever deleted locally without a verified nearline copy, a partial
# copy never carries a final name, and re-runs are no-ops. flock serializes a
# manual run against the timer's.
#
# Environment (from .env via the unit's EnvironmentFile, or `set -a; . ./.env`):
#   DSG_BACKUP_DIR      nearline root (e.g. /shared/flyem/dsg). REQUIRED and it
#                       must already exist: an unmounted nearline must fail here
#                       rather than be recreated on the local disk.
#   DSG_LOG_KEEP_LOCAL  how many newest rotated files to keep locally (2).
# Usage: ship-rotated-logs.sh [dsg-dir]   (default: current directory)
# Needs only coreutils + util-linux (sha256sum, find, flock); no Django, no pixi.
set -euo pipefail

DSG_DIR="${1:-$PWD}"
LOCAL_DIR="$DSG_DIR/logs"
KEEP="${DSG_LOG_KEEP_LOCAL:-2}"

: "${DSG_BACKUP_DIR:?DSG_BACKUP_DIR is not set (see .env.example)}"
if [ ! -d "$DSG_BACKUP_DIR" ]; then
    echo "ship-rotated-logs: nearline root $DSG_BACKUP_DIR does not exist (not mounted?); refusing to create it" >&2
    exit 2
fi
REMOTE_DIR="$DSG_BACKUP_DIR/logs"
mkdir -p "$REMOTE_DIR"

if [ ! -d "$LOCAL_DIR" ]; then
    echo "ship-rotated-logs: no $LOCAL_DIR yet; nothing to ship"
    exit 0
fi

exec 9>"$LOCAL_DIR/.ship.lock"
if ! flock -n 9; then
    echo "ship-rotated-logs: another run holds $LOCAL_DIR/.ship.lock; exiting" >&2
    exit 3
fi

sha() { sha256sum "$1" | cut -d' ' -f1; }

# dateformat -%Y%m%dT%H%M%S makes lexical order == chronological order.
mapfile -t files < <(find "$LOCAL_DIR" -maxdepth 1 -type f -name 'serve.log-*' ! -name '*.part' -printf '%f\n' | sort -r)
if [ "${#files[@]}" -eq 0 ]; then
    echo "ship-rotated-logs: no rotated logs in $LOCAL_DIR"
    exit 0
fi

declare -A verified=()
failures=0
for name in "${files[@]}"; do
    src="$LOCAL_DIR/$name"
    dst="$REMOTE_DIR/$name"
    part="$dst.part"
    want="$(sha "$src")"
    if [ -f "$dst" ] && [ "$(sha "$dst")" = "$want" ]; then
        verified["$name"]=1
        continue
    fi
    if cp -pf "$src" "$part" && [ "$(sha "$part")" = "$want" ]; then
        mv -f "$part" "$dst"
        verified["$name"]=1
        echo "shipped $name -> $dst ($(stat -c %s "$src") bytes, sha256 ${want:0:12}...)"
    else
        rm -f "$part"
        echo "ship-rotated-logs: $name FAILED transfer verification; local copy kept, $part removed" >&2
        failures=$((failures + 1))
    fi
done

# Trim: local copies beyond the KEEP newest go only if verified on nearline.
rank=0
for name in "${files[@]}"; do
    rank=$((rank + 1))
    if [ "$rank" -le "$KEEP" ]; then
        continue
    fi
    if [ -n "${verified[$name]:-}" ]; then
        rm -f "$LOCAL_DIR/$name"
        echo "trimmed local $name (verified on nearline)"
    fi
done

if [ "$failures" -gt 0 ]; then
    echo "ship-rotated-logs: $failures transfer(s) failed; will retry next run" >&2
    exit 1
fi
