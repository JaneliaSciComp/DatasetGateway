"""Unit tests for the pure tiered-retention (GFS) prune selection.

No Django, no filesystem — synthetic timestamp series only. This is the
"simulated multi-day series" acceptance check from the gateway-0002 plan.
"""

import datetime as dt

from core.backup import (
    backup_name,
    base_name_of,
    parse_backup_name,
    select_prunable,
    select_retained,
)

UTC = dt.timezone.utc


def _entry(when):
    """A (datetime, name) pair whose name encodes the timestamp, as on disk."""
    return when, backup_name(when)


def test_name_roundtrip():
    when = dt.datetime(2026, 6, 23, 4, 5, 6, tzinfo=UTC)
    name = backup_name(when)
    assert name == "dsg-backup-20260623T040506Z"
    assert parse_backup_name(name) == when
    assert base_name_of(name + ".tar.gz.age") == name
    assert base_name_of(name + ".tar.gz") == name
    assert base_name_of(name + ".meta.json") == name
    assert parse_backup_name("not-a-backup") is None


def test_three_in_one_hour_keeps_only_newest():
    # Three backups in the same hour/day/week collapse to one across all tiers.
    base = dt.datetime(2026, 6, 23, 10, 0, 0, tzinfo=UTC)
    entries = [
        _entry(base),
        _entry(base + dt.timedelta(minutes=20)),
        _entry(base + dt.timedelta(minutes=40)),
    ]
    kept = select_retained(entries, keep_hourly=24, keep_daily=14, keep_weekly=8)
    assert kept == {backup_name(base + dt.timedelta(minutes=40))}
    pruned = select_prunable(entries, 24, 14, 8)
    assert pruned == {backup_name(base), backup_name(base + dt.timedelta(minutes=20))}


def test_zero_keep_tiers_retain_nothing():
    entries = [_entry(dt.datetime(2026, 6, 23, h, tzinfo=UTC)) for h in range(5)]
    assert select_retained(entries, 0, 0, 0) == set()


def test_ancient_backup_is_pruned_when_tiers_are_full():
    # Restic-style GFS counts *populated* buckets, so an ancient backup is only
    # pruned once enough newer backups fill every tier. A dense recent hourly
    # series (70 days) fills all 24h/14d/8w buckets, so a 200-day-old outlier
    # falls outside every tier and is pruned.
    now = dt.datetime(2026, 6, 23, 23, 0, 0, tzinfo=UTC)
    dense = [_entry(now - dt.timedelta(hours=i)) for i in range(70 * 24)]
    ancient = _entry(now - dt.timedelta(days=200))
    kept = select_retained([*dense, ancient], 24, 14, 8)
    assert dense[0][1] in kept
    assert ancient[1] not in kept


def test_sparse_history_retains_everything():
    # With fewer backups than buckets, populated-bucket semantics keeps them all
    # (an outage must not cause the only surviving backups to be pruned).
    now = dt.datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC)
    entries = [_entry(now), _entry(now - dt.timedelta(days=200))]
    kept = select_retained(entries, 24, 14, 8)
    assert kept == {entries[0][1], entries[1][1]}


def test_hourly_series_keeps_exactly_configured_gfs_set():
    """An hourly cadence over 84 days, then assert the retained set is exactly
    the GFS union derived independently from first principles.
    """
    kh, kd, kw = 24, 14, 8
    # Anchor "now" at the top of an hour; fixed (no Date.now) for determinism.
    now = dt.datetime(2026, 6, 23, 23, 0, 0, tzinfo=UTC)
    hours = 84 * 24
    times = [now - dt.timedelta(hours=i) for i in range(hours)]
    entries = [_entry(t) for t in times]

    kept = select_retained(entries, kh, kd, kw)

    # --- Independent reference: newest-per-bucket for the N most-recent buckets,
    # computed with grouped max() rather than the impl's single-pass dict. ---
    def reference(keyfn, n):
        groups: dict = {}
        for t in times:
            groups.setdefault(keyfn(t), []).append(t)
        newest_buckets = sorted(groups, reverse=True)[:n]
        return {backup_name(max(groups[b])) for b in newest_buckets}

    expected = set()
    expected |= reference(lambda t: (t.year, t.month, t.day, t.hour), kh)
    expected |= reference(lambda t: (t.year, t.month, t.day), kd)
    expected |= reference(lambda t: t.isocalendar()[:2], kw)

    assert kept == expected

    # --- Concrete, hand-checkable invariants on top of the set-equality. ---
    # The 24 most-recent hourly backups are each their own hour bucket -> all kept.
    newest_24 = {backup_name(now - dt.timedelta(hours=i)) for i in range(kh)}
    assert newest_24 <= kept

    # The latest backup of each of the 14 most-recent days is kept.
    for d in range(kd):
        day = (now - dt.timedelta(days=d)).date()
        latest_that_day = max(t for t in times if t.date() == day)
        assert backup_name(latest_that_day) in kept

    # The latest backup of each of the 8 most-recent ISO weeks is kept.
    weeks_seen = []
    for t in times:
        wk = t.isocalendar()[:2]
        if wk not in weeks_seen:
            weeks_seen.append(wk)
    for wk in weeks_seen[:kw]:
        latest_that_week = max(t for t in times if t.isocalendar()[:2] == wk)
        assert backup_name(latest_that_week) in kept

    # Nothing older than the 8th-most-recent week survives.
    oldest_kept_week = weeks_seen[kw - 1]
    for _, name in entries:
        ts = parse_backup_name(name)
        if ts.isocalendar()[:2] < oldest_kept_week:
            assert name not in kept

    # Sanity: far fewer kept than total (pruning actually happened).
    assert len(kept) < hours
    # Union size is deterministic for this fixed series (regression guard).
    assert len(kept) == len(expected)
