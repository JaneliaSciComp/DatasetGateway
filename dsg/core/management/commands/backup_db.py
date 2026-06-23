"""DSG-initiated, encrypted, tiered SQLite backup to nearline storage.

Pipeline (see sessions/changes/gateway-0002-backup-restore.md):

  snapshot -> verify -> assemble -> encrypt -> checksum -> ship ->
  verify-transfer -> finalize -> prune

All heavy work happens in a fast local *staging* dir (NVMe); only a finished,
verified, encrypted artifact plus an unencrypted ``.meta.json`` sidecar are
shipped to the slow *nearline* mount. The snapshot DB carries live bearer
tokens, so the command refuses to write an unencrypted bundle when secrets are
included. Run hourly via the systemd timer (scripts/datasetgateway-backup.timer).
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import django
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from core import backup


class Command(BaseCommand):
    help = "Take a consistent, encrypted, tiered backup of the SQLite DB to nearline."

    def add_arguments(self, parser):
        parser.add_argument(
            "--database-path", default=None,
            help="Source SQLite DB to snapshot (default: settings DATABASES['default']['NAME']).",
        )
        parser.add_argument(
            "--staging-dir", default=None,
            help="Fast local scratch dir (default: settings.DSG_BACKUP_STAGING_DIR).",
        )
        parser.add_argument(
            "--backup-dir", default=None,
            help="Nearline target dir (default: settings.DSG_BACKUP_DIR).",
        )
        parser.add_argument(
            "--no-prune", action="store_true",
            help="Skip the tiered-retention prune step after a successful backup.",
        )

    # --- pipeline -----------------------------------------------------------

    def handle(self, *args, **options):
        db_path = Path(
            options["database_path"]
            or settings.DATABASES["default"]["NAME"]
        )
        staging_dir = Path(options["staging_dir"] or settings.DSG_BACKUP_STAGING_DIR)
        backup_dir = Path(options["backup_dir"] or settings.DSG_BACKUP_DIR)
        include_secrets = settings.DSG_BACKUP_INCLUDE_SECRETS

        if not db_path.exists():
            raise CommandError(f"Source database not found: {db_path}")

        encrypt_argv = self._resolve_encrypt_argv()
        if encrypt_argv is None and include_secrets:
            # Decision #4: the snapshot DB holds live bearer tokens. Never write
            # an unencrypted bundle that includes secrets.
            raise CommandError(
                "DSG_BACKUP_INCLUDE_SECRETS is on but no encryption is configured "
                "(set DSG_BACKUP_AGE_RECIPIENTS_FILE or DSG_BACKUP_ENCRYPT_CMD). "
                "Refusing to write cleartext secrets — nothing was written."
            )
        if encrypt_argv is None:
            self.stderr.write(self.style.WARNING(
                "No encryption configured: writing an UNENCRYPTED bundle. The "
                "snapshot DB still contains live bearer tokens — do not ship this "
                "off a trusted host."
            ))

        now = timezone.now()
        name = backup.backup_name(now)
        staging_dir.mkdir(parents=True, exist_ok=True)
        backup_dir.mkdir(parents=True, exist_ok=True)
        workdir = staging_dir / f"{name}.work"
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir()

        staged_artifact = None
        staged_meta = None
        try:
            # 1. Snapshot (consistent; safe while gunicorn serves).
            snapshot = workdir / "db.sqlite3"
            self._snapshot(db_path, snapshot)

            # 2. Verify the snapshot.
            self._integrity_check(snapshot)

            # 3. Assemble the reproduce-bundle in staging.
            members = [("db.sqlite3", snapshot)]
            members += self._gather_secrets(workdir, include_secrets)
            manifest = self._build_manifest(name, now, include_secrets, members)
            manifest_path = workdir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
            members.append(("manifest.json", manifest_path))

            tgz = staging_dir / f"{name}.tar.gz"
            self._make_tarball(tgz, members)

            # 4. Encrypt the whole gzip (or keep it as-is if unconfigured).
            if encrypt_argv is not None:
                staged_artifact = staging_dir / f"{name}.tar.gz.age"
                self._run_filter(encrypt_argv, tgz, staged_artifact, "encrypt")
                tgz.unlink()
                encrypted = True
            else:
                staged_artifact = tgz
                encrypted = False

            # 5. Checksum + unencrypted sidecar (ops visibility, no secrets).
            artifact_sha = backup.sha256_file(staged_artifact)
            staged_meta = staging_dir / f"{name}.meta.json"
            staged_meta.write_text(json.dumps(
                self._sidecar(name, now, manifest, encrypted, staged_artifact, artifact_sha),
                indent=2, sort_keys=True,
            ))

            # 6. Ship to nearline.
            dest_artifact = backup_dir / staged_artifact.name
            dest_meta = backup_dir / staged_meta.name
            shutil.copy2(staged_artifact, dest_artifact)
            shutil.copy2(staged_meta, dest_meta)

            # 7. Verify the transfer; on mismatch keep the local copy, delete the
            #    bad nearline copy, fail loudly. Nothing else is touched.
            dest_sha = backup.sha256_file(dest_artifact)
            if dest_sha != artifact_sha:
                dest_artifact.unlink(missing_ok=True)
                dest_meta.unlink(missing_ok=True)
                raise CommandError(
                    f"Transfer verification failed for {dest_artifact} "
                    f"(sha256 {dest_sha} != {artifact_sha}). Nearline copy removed; "
                    f"local staging copy retained at {staged_artifact}."
                )

            # 8. Finalize: lock down nearline perms, drop the local staging copies.
            dest_artifact.chmod(0o600)
            dest_meta.chmod(0o600)
            staged_artifact.unlink(missing_ok=True)
            staged_meta.unlink(missing_ok=True)
            staged_artifact = staged_meta = None

            self.stdout.write(self.style.SUCCESS(
                f"Backup complete: {dest_artifact} "
                f"({dest_artifact.stat().st_size} bytes, sha256 {artifact_sha[:12]}…, "
                f"{'encrypted' if encrypted else 'UNENCRYPTED'})"
            ))
        finally:
            # The workdir holds the snapshot + assembled cleartext; always remove
            # it. Staged artifact/meta are removed on success (above) and kept on
            # transfer-verify failure (so the local copy survives).
            shutil.rmtree(workdir, ignore_errors=True)

        # 9. Prune (only after a verified new backup).
        if not options["no_prune"]:
            self._prune(backup_dir)

    # --- steps --------------------------------------------------------------

    def _snapshot(self, src: Path, dest: Path):
        """Consistent snapshot via SQLite ``VACUUM INTO`` (compacted single file).

        Runs in its own connection, transactionally consistent against
        concurrent gunicorn readers/writers. ``VACUUM INTO`` requires the
        destination not to exist.
        """
        dest.unlink(missing_ok=True)
        try:
            con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
        except sqlite3.Error as exc:
            raise CommandError(f"Cannot open source DB {src}: {exc}") from exc
        try:
            con.execute("VACUUM INTO ?", (str(dest),))
        except sqlite3.Error as exc:
            dest.unlink(missing_ok=True)
            raise CommandError(
                f"Snapshot (VACUUM INTO) failed — DB may be torn/locked: {exc}"
            ) from exc
        finally:
            con.close()

    def _integrity_check(self, snapshot: Path):
        con = sqlite3.connect(str(snapshot), timeout=60)
        try:
            rows = con.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.Error as exc:
            raise CommandError(f"integrity_check failed to run: {exc}") from exc
        finally:
            con.close()
        result = [r[0] for r in rows]
        if result != ["ok"]:
            snapshot.unlink(missing_ok=True)
            raise CommandError(
                "Snapshot failed PRAGMA integrity_check (not 'ok'): "
                + "; ".join(result[:5])
            )

    def _gather_secrets(self, workdir: Path, include_secrets: bool):
        """Copy .env + client_credentials.json into the workdir; return members."""
        if not include_secrets:
            return []
        members = []
        env_path = settings.BASE_DIR / ".env"
        if env_path.exists():
            staged = workdir / "dotenv"
            shutil.copy2(env_path, staged)
            members.append(("dotenv", staged))
        else:
            self.stderr.write(self.style.WARNING(f"No .env at {env_path}; skipping."))

        # settings.py reads CLIENT_CREDENTIALS_PATH into a local var only, so
        # re-resolve it from env/default here for the bundle.
        creds_path = Path(os.environ.get(
            "CLIENT_CREDENTIALS_PATH",
            settings.BASE_DIR / "secrets" / "client_credentials.json",
        ))
        if creds_path.exists():
            staged = workdir / "client_credentials.json"
            shutil.copy2(creds_path, staged)
            members.append(("secrets/client_credentials.json", staged))
        else:
            self.stderr.write(self.style.WARNING(
                f"No client_credentials.json at {creds_path}; skipping."
            ))
        return members

    def _build_manifest(self, name, now, include_secrets, members):
        return {
            "name": name,
            "created_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "git": self._git_info(),
            "django_version": django.get_version(),
            "python_version": platform.python_version(),
            "include_secrets": include_secrets,
            "applied_migrations": self._applied_migrations(),
            "files": {arc: backup.sha256_file(p) for arc, p in members},
        }

    def _sidecar(self, name, now, manifest, encrypted, artifact, artifact_sha):
        """Unencrypted ops sidecar — must expose nothing sensitive."""
        return {
            "name": name,
            "created_utc": manifest["created_utc"],
            "git_sha": manifest["git"].get("sha"),
            "git_branch": manifest["git"].get("branch"),
            "django_version": manifest["django_version"],
            "python_version": manifest["python_version"],
            "include_secrets": manifest["include_secrets"],
            "encrypted": encrypted,
            "artifact": artifact.name,
            "artifact_sha256": artifact_sha,
            "artifact_size_bytes": artifact.stat().st_size,
        }

    def _make_tarball(self, tgz: Path, members):
        with tarfile.open(tgz, "w:gz") as tar:
            for arcname, path in members:
                tar.add(path, arcname=arcname)

    def _prune(self, backup_dir: Path):
        entries = backup.discover_bundles(backup_dir)
        prunable = backup.select_prunable(
            entries,
            settings.DSG_BACKUP_KEEP_HOURLY,
            settings.DSG_BACKUP_KEEP_DAILY,
            settings.DSG_BACKUP_KEEP_WEEKLY,
        )
        removed = 0
        for base in sorted(prunable):
            for suffix in (".tar.gz.age", ".tar.gz", ".meta.json"):
                p = backup_dir / f"{base}{suffix}"
                if p.exists():
                    p.unlink()
                    removed += 1
        if prunable:
            self.stdout.write(
                f"Pruned {len(prunable)} bundle(s) ({removed} file(s)); "
                f"{len(entries) - len(prunable)} retained."
            )

    # --- helpers ------------------------------------------------------------

    def _resolve_encrypt_argv(self):
        cmd = (settings.DSG_BACKUP_ENCRYPT_CMD or "").strip()
        if cmd:
            return shlex.split(cmd)
        recipients = (settings.DSG_BACKUP_AGE_RECIPIENTS_FILE or "").strip()
        if recipients:
            if not Path(recipients).exists():
                raise CommandError(
                    f"DSG_BACKUP_AGE_RECIPIENTS_FILE is set but not found: {recipients}"
                )
            return ["age", "-R", recipients]
        return None

    def _run_filter(self, argv, src: Path, dst: Path, label: str):
        """Run a stdin->stdout filter (encrypt/decrypt) from src to dst."""
        try:
            with open(src, "rb") as fin, open(dst, "wb") as fout:
                subprocess.run(argv, stdin=fin, stdout=fout, check=True)
        except FileNotFoundError as exc:
            dst.unlink(missing_ok=True)
            raise CommandError(f"{label} command not found: {argv[0]!r}") from exc
        except subprocess.CalledProcessError as exc:
            dst.unlink(missing_ok=True)
            raise CommandError(
                f"{label} command failed (exit {exc.returncode}): {' '.join(argv)}"
            ) from exc

    def _git_info(self):
        info = {"sha": None, "branch": None}
        try:
            root = str(settings.BASE_DIR)
            info["sha"] = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
            info["branch"] = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            pass
        return info

    def _applied_migrations(self):
        try:
            from django.db import connection
            applied = MigrationRecorder(connection).applied_migrations()
            return sorted(f"{app}.{nm}" for app, nm in applied)
        except Exception:  # introspection must never sink a backup
            return None
