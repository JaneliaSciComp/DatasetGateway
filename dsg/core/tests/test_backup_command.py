"""End-to-end + negative tests for backup_db / restore_db.

`age` is not assumed present in this environment, so the encryption tests drive
the pluggable DSG_BACKUP_ENCRYPT_CMD / DSG_BACKUP_DECRYPT_CMD path with a
passphraseless gpg keypair created in a temp GNUPGHOME — a real asymmetric
round-trip proving the bundle is unreadable without the private key. The age
default uses the identical filter mechanism.
"""

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from core import backup

GPG = shutil.which("gpg")
pytestmark = pytest.mark.skipif(GPG is None, reason="gpg not installed")


# --- fixtures ---------------------------------------------------------------

@pytest.fixture(scope="module")
def gpg_filters(tmp_path_factory):
    """A temp gpg keyring; yields encrypt/decrypt filter command strings."""
    home = tmp_path_factory.mktemp("gnupg")
    home.chmod(0o700)
    params = home / "params"
    params.write_text(
        "%no-protection\n"
        "Key-Type: RSA\nKey-Length: 2048\n"
        "Name-Real: DSG Backup Test\nName-Email: dsg-backup-test@example.org\n"
        "Expire-Date: 0\n%commit\n"
    )
    saved = os.environ.get("GNUPGHOME")
    os.environ["GNUPGHOME"] = str(home)
    try:
        subprocess.run(
            [GPG, "--batch", "--gen-key", str(params)],
            check=True, capture_output=True,
        )
        yield {
            "encrypt": (
                f"{GPG} --batch --yes --trust-model always --encrypt "
                "--recipient dsg-backup-test@example.org"
            ),
            "decrypt": f"{GPG} --batch --yes --pinentry-mode loopback --decrypt",
            "home": str(home),
        }
    finally:
        if saved is None:
            os.environ.pop("GNUPGHOME", None)
        else:
            os.environ["GNUPGHOME"] = saved


def _make_db(path: Path, rows=("alice", "bob", "carol")):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t(v) VALUES(?)", [(r,) for r in rows])
    con.commit()
    con.close()


def _artifacts(d: Path, *suffixes):
    return sorted(p.name for p in d.iterdir() if p.name.endswith(suffixes)) if d.exists() else []


# --- happy path -------------------------------------------------------------

def test_backup_then_restore_roundtrip_encrypted(tmp_path, gpg_filters):
    src = tmp_path / "src.sqlite3"
    _make_db(src)
    staging = tmp_path / "staging"
    nearline = tmp_path / "nearline"

    with override_settings(
        DSG_BACKUP_INCLUDE_SECRETS=True,
        DSG_BACKUP_ENCRYPT_CMD=gpg_filters["encrypt"],
        DSG_BACKUP_DECRYPT_CMD=gpg_filters["decrypt"],
    ):
        call_command(
            "backup_db", database_path=str(src),
            staging_dir=str(staging), backup_dir=str(nearline), no_prune=True,
        )

        # Encrypted artifact + sidecar landed; staging is clean.
        assert _artifacts(nearline, ".tar.gz.age") and _artifacts(nearline, ".meta.json")
        assert _artifacts(staging, ".tar.gz.age", ".tar.gz", ".meta.json") == []

        age = nearline / _artifacts(nearline, ".tar.gz.age")[0]
        # Not a gzip: encryption actually happened (gzip magic is 1f 8b).
        assert age.read_bytes()[:2] != b"\x1f\x8b"
        assert oct(age.stat().st_mode)[-3:] == "600"

        # Sidecar is JSON, encrypted=True, and its keys are a known safe set —
        # no token/credential fields. (Value-level check too, in case a future
        # field smuggles one in.)
        meta = json.loads((nearline / _artifacts(nearline, ".meta.json")[0]).read_text())
        assert meta["encrypted"] is True
        assert set(meta) == {
            "name", "created_utc", "git_sha", "git_branch", "django_version",
            "python_version", "include_secrets", "encrypted", "artifact",
            "artifact_sha256", "artifact_size_bytes",
        }
        values = " ".join(str(v) for v in meta.values()).lower()
        for leaked in ("token", "password", "client_secret", "private key", "begin "):
            assert leaked not in values

        # Restore into a scratch DB and confirm the rows survived.
        target = tmp_path / "restored.sqlite3"
        call_command("restore_db", str(age), apply=True, database_path=str(target))
        con = sqlite3.connect(str(target))
        assert sorted(r[0] for r in con.execute("SELECT v FROM t")) == ["alice", "bob", "carol"]
        con.close()


def test_restore_dry_run_does_not_write(tmp_path, gpg_filters):
    src = tmp_path / "src.sqlite3"
    _make_db(src)
    nearline = tmp_path / "nearline"
    with override_settings(
        DSG_BACKUP_INCLUDE_SECRETS=False,
        DSG_BACKUP_ENCRYPT_CMD=gpg_filters["encrypt"],
        DSG_BACKUP_DECRYPT_CMD=gpg_filters["decrypt"],
    ):
        call_command("backup_db", database_path=str(src),
                     staging_dir=str(tmp_path / "s"), backup_dir=str(nearline), no_prune=True)
        age = nearline / _artifacts(nearline, ".tar.gz.age")[0]
        target = tmp_path / "restored.sqlite3"
        call_command("restore_db", str(age), database_path=str(target))  # no --apply
        assert not target.exists()


# --- negative paths ---------------------------------------------------------

def test_refuse_secrets_without_encryption(tmp_path):
    src = tmp_path / "src.sqlite3"
    _make_db(src)
    nearline = tmp_path / "nearline"
    with override_settings(
        DSG_BACKUP_INCLUDE_SECRETS=True,
        DSG_BACKUP_AGE_RECIPIENTS_FILE="",
        DSG_BACKUP_ENCRYPT_CMD="",
    ):
        with pytest.raises(CommandError, match="no encryption is configured"):
            call_command("backup_db", database_path=str(src),
                         staging_dir=str(tmp_path / "s"), backup_dir=str(nearline))
    assert _artifacts(nearline, ".tar.gz.age", ".tar.gz", ".meta.json") == []


def test_torn_db_fails_integrity(tmp_path):
    src = tmp_path / "src.sqlite3"
    _make_db(src, rows=[f"row{i}" for i in range(500)])  # several pages
    # Corrupt the middle of the file (keep the 100-byte header intact).
    data = bytearray(src.read_bytes())
    for i in range(2000, min(6000, len(data))):
        data[i] = 0xFF
    src.write_bytes(bytes(data))

    nearline = tmp_path / "nearline"
    with override_settings(DSG_BACKUP_INCLUDE_SECRETS=False,
                           DSG_BACKUP_AGE_RECIPIENTS_FILE="", DSG_BACKUP_ENCRYPT_CMD=""):
        with pytest.raises(CommandError):
            call_command("backup_db", database_path=str(src),
                         staging_dir=str(tmp_path / "s"), backup_dir=str(nearline), no_prune=True)
    # No "successful" backup was produced.
    assert _artifacts(nearline, ".tar.gz.age", ".tar.gz", ".meta.json") == []


def test_transfer_corruption_detected_local_retained(tmp_path, monkeypatch):
    src = tmp_path / "src.sqlite3"
    _make_db(src)
    staging = tmp_path / "staging"
    nearline = tmp_path / "nearline"

    from core.management.commands import backup_db as cmd_mod
    real_copy2 = shutil.copy2

    def corrupting_copy2(s, d, *a, **k):
        real_copy2(s, d, *a, **k)
        d = Path(d)
        # Only damage the shipped artifact on the nearline side.
        if d.parent == nearline and d.suffix in (".age", ".gz"):
            with open(d, "ab") as f:
                f.write(b"CORRUPTION")

    monkeypatch.setattr(cmd_mod.shutil, "copy2", corrupting_copy2)

    with override_settings(DSG_BACKUP_INCLUDE_SECRETS=False,
                           DSG_BACKUP_AGE_RECIPIENTS_FILE="", DSG_BACKUP_ENCRYPT_CMD=""):
        with pytest.raises(CommandError, match="Transfer verification failed"):
            call_command("backup_db", database_path=str(src),
                         staging_dir=str(staging), backup_dir=str(nearline), no_prune=True)

    # Bad nearline copy removed; local staging copy retained for recovery.
    assert _artifacts(nearline, ".tar.gz", ".tar.gz.age") == []
    assert _artifacts(staging, ".tar.gz", ".tar.gz.age") != []


# --- prune integration ------------------------------------------------------

def test_backup_prunes_old_bundles(tmp_path, gpg_filters):
    import datetime as dt

    src = tmp_path / "src.sqlite3"
    _make_db(src)
    nearline = tmp_path / "nearline"
    nearline.mkdir()

    # Seed five fake old bundles (artifact + sidecar) spanning 100 days.
    base = dt.datetime(2026, 3, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    fakes = []
    for d in (0, 10, 30, 60, 100):
        name = backup.backup_name(base - dt.timedelta(days=d))
        (nearline / f"{name}.tar.gz.age").write_bytes(b"old")
        (nearline / f"{name}.meta.json").write_text(json.dumps({"name": name}))
        fakes.append(name)

    with override_settings(
        DSG_BACKUP_INCLUDE_SECRETS=False,
        DSG_BACKUP_ENCRYPT_CMD=gpg_filters["encrypt"],
        DSG_BACKUP_DECRYPT_CMD=gpg_filters["decrypt"],
        DSG_BACKUP_KEEP_HOURLY=1, DSG_BACKUP_KEEP_DAILY=1, DSG_BACKUP_KEEP_WEEKLY=1,
    ):
        # Pruning runs after this verified backup; the new "now" bundle is newest
        # in every tier, so all five stale fakes (3+ months old) are removed.
        call_command("backup_db", database_path=str(src),
                     staging_dir=str(tmp_path / "s"), backup_dir=str(nearline))

    remaining = _artifacts(nearline, ".tar.gz.age")
    assert len(remaining) == 1
    for name in fakes:
        assert not (nearline / f"{name}.tar.gz.age").exists()
        assert not (nearline / f"{name}.meta.json").exists()
