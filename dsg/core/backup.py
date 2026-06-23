"""Pure helpers shared by the backup_db / restore_db management commands.

This module is deliberately I/O-light: the tiered-retention *selection* is a pure
function over (timestamp, name) pairs so it can be unit-tested with synthetic
timestamps and no filesystem. The naming/parsing/checksum helpers are small and
side-effect free except where noted (sha256_file reads a file).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from pathlib import Path

# Backup bundles are named with a sortable, filesystem-safe UTC stamp:
#   dsg-backup-YYYYMMDDTHHMMSSZ
# The artifact appends .tar.gz (+ .age when encrypted); the sidecar appends
# .meta.json. Parsing the stamp back out of a name drives tiered pruning.
NAME_PREFIX = "dsg-backup-"
_TS_FORMAT = "%Y%m%dT%H%M%SZ"
NAME_RE = re.compile(r"^dsg-backup-(\d{8}T\d{6}Z)$")


def backup_name(when: _dt.datetime) -> str:
    """Build the bundle base name (no extension) for an aware UTC datetime."""
    return f"{NAME_PREFIX}{when.astimezone(_dt.timezone.utc).strftime(_TS_FORMAT)}"


def parse_backup_name(name: str) -> _dt.datetime | None:
    """Recover the aware-UTC timestamp from a bundle base name, or None.

    Accepts the bare base name (``dsg-backup-...Z``); callers strip extensions
    (``.tar.gz``/``.age``/``.meta.json``) first via :func:`base_name_of`.
    """
    m = NAME_RE.match(name)
    if not m:
        return None
    parsed = _dt.datetime.strptime(m.group(1), _TS_FORMAT)
    return parsed.replace(tzinfo=_dt.timezone.utc)


def base_name_of(filename: str) -> str:
    """Strip the known backup extensions from a filename to get its base name.

    ``dsg-backup-...Z.tar.gz.age`` -> ``dsg-backup-...Z``;
    ``dsg-backup-...Z.meta.json``  -> ``dsg-backup-...Z``.
    """
    for ext in (".tar.gz.age", ".tar.gz", ".meta.json"):
        if filename.endswith(ext):
            return filename[: -len(ext)]
    return filename


def _keep_newest_per_bucket(entries, n, key):
    """From ``entries`` (newest-first), keep the newest item in each of the ``n``
    most-recent distinct ``key(ts)`` buckets. Returns a set of kept names.

    ``entries`` is a list of (datetime, name) already sorted newest-first.
    """
    if n <= 0:
        return set()
    newest_in_bucket: dict = {}
    bucket_order: list = []  # bucket keys in newest-first order of first sighting
    for ts, name in entries:
        bucket = key(ts)
        if bucket not in newest_in_bucket:
            newest_in_bucket[bucket] = name
            bucket_order.append(bucket)
    return {newest_in_bucket[b] for b in bucket_order[:n]}


def select_retained(entries, keep_hourly, keep_daily, keep_weekly):
    """GFS tiered retention: return the set of bundle *names* to keep.

    ``entries`` is an iterable of (aware-UTC datetime, name) pairs. A bundle is
    retained if it is the newest within one of the ``keep_hourly`` most-recent
    hour buckets, the ``keep_daily`` most-recent day buckets, or the
    ``keep_weekly`` most-recent ISO-week buckets. The result is the union across
    the three tiers; everything else is prunable.

    "Most-recent buckets" counts only buckets that *contain* a backup (the
    restic/borg ``--keep-hourly N`` semantics), not a wall-clock window. So
    ``keep_hourly=24`` keeps the last 24 hourly snapshots even if a multi-hour
    outage means they span more than 24 wall-clock hours — backups are never
    silently dropped just because the cadence slipped.
    """
    ordered = sorted(entries, key=lambda e: e[0], reverse=True)
    kept = set()
    kept |= _keep_newest_per_bucket(
        ordered, keep_hourly, lambda d: (d.year, d.month, d.day, d.hour)
    )
    kept |= _keep_newest_per_bucket(
        ordered, keep_daily, lambda d: (d.year, d.month, d.day)
    )
    kept |= _keep_newest_per_bucket(
        ordered, keep_weekly, lambda d: d.isocalendar()[:2]
    )
    return kept


def select_prunable(entries, keep_hourly, keep_daily, keep_weekly):
    """Inverse of :func:`select_retained`: the set of names to delete."""
    entries = list(entries)
    kept = select_retained(entries, keep_hourly, keep_daily, keep_weekly)
    return {name for _, name in entries if name not in kept}


def sha256_file(path, chunk_size: int = 1024 * 1024) -> str:
    """Streaming sha256 hex digest of a file (constant memory)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def discover_bundles(backup_dir):
    """Scan ``backup_dir`` for backup sidecars and return [(datetime, name), ...].

    Each backup writes exactly one ``<name>.meta.json`` sidecar, so the sidecars
    are the authoritative inventory. Names whose timestamp cannot be parsed are
    skipped (foreign files are left untouched).
    """
    backup_dir = Path(backup_dir)
    out = []
    if not backup_dir.is_dir():
        return out
    for meta in backup_dir.glob(f"{NAME_PREFIX}*.meta.json"):
        base = base_name_of(meta.name)
        ts = parse_backup_name(base)
        if ts is not None:
            out.append((ts, base))
    return out
