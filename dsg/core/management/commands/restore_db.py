"""Guided restore/verify of a backup bundle produced by ``backup_db``.

Default is a *dry verify*: decrypt, extract, recompute every file's sha256
against the manifest, and print the git SHA to check out. ``--apply`` then drops
the snapshot into the target DB (and, with ``--restore-secrets``, restores
``.env`` + ``secrets/``). See docs/backups.md for the full runbook.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core import backup

# Bundle member arcname -> how it maps back onto the host at restore time.
_DB_MEMBER = "db.sqlite3"
_ENV_MEMBER = "dotenv"
_CREDS_MEMBER = "secrets/client_credentials.json"


class Command(BaseCommand):
    help = "Verify and (with --apply) restore a backup bundle from backup_db."

    def add_arguments(self, parser):
        parser.add_argument("bundle", help="Path to a .tar.gz.age (or .tar.gz) bundle.")
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write the restored files into place (default: verify only).",
        )
        parser.add_argument(
            "--database-path", default=None,
            help="Restore target DB path (default: settings DATABASES['default']['NAME']).",
        )
        parser.add_argument(
            "--restore-secrets", action="store_true",
            help="With --apply, also restore .env and secrets/ into --base-dir.",
        )
        parser.add_argument(
            "--base-dir", default=None,
            help="Where .env/secrets are restored (default: settings.BASE_DIR).",
        )

    def handle(self, *args, **options):
        bundle = Path(options["bundle"])
        if not bundle.exists():
            raise CommandError(f"Bundle not found: {bundle}")

        with tempfile.TemporaryDirectory(prefix="dsg-restore-") as tmp:
            tmpdir = Path(tmp)

            # 1. Decrypt if needed.
            if bundle.name.endswith(".age") or bundle.name.endswith(".gpg"):
                decrypt_argv = self._resolve_decrypt_argv()
                if decrypt_argv is None:
                    raise CommandError(
                        "Bundle is encrypted but no decryption is configured "
                        "(set DSG_BACKUP_AGE_IDENTITY_FILE or DSG_BACKUP_DECRYPT_CMD)."
                    )
                tgz = tmpdir / "bundle.tar.gz"
                self._run_filter(decrypt_argv, bundle, tgz, "decrypt")
            elif bundle.name.endswith(".tar.gz"):
                tgz = bundle
            else:
                raise CommandError(
                    f"Unrecognized bundle extension: {bundle.name} "
                    "(expected .tar.gz.age or .tar.gz)."
                )

            # 2. Extract (path-traversal safe).
            extract = tmpdir / "extract"
            extract.mkdir()
            with tarfile.open(tgz, "r:gz") as tar:
                tar.extractall(extract, filter="data")

            # 3. Read + verify the manifest.
            manifest_path = extract / "manifest.json"
            if not manifest_path.exists():
                raise CommandError("Bundle has no manifest.json — refusing to trust it.")
            manifest = json.loads(manifest_path.read_text())
            self._verify_checksums(extract, manifest)

            # 4. Report what this bundle is.
            self._report(manifest, extract)

            if not options["apply"]:
                self.stdout.write(self.style.SUCCESS(
                    "Verify OK (dry run). Re-run with --apply to restore."
                ))
                return

            # 5. Apply.
            self._apply(options, extract, manifest)

    # --- steps --------------------------------------------------------------

    def _verify_checksums(self, extract: Path, manifest: dict):
        files = manifest.get("files") or {}
        if not files:
            raise CommandError("Manifest lists no files — refusing to trust it.")
        for arcname, expected in files.items():
            path = extract / arcname
            if not path.exists():
                raise CommandError(f"Manifest file missing from bundle: {arcname}")
            actual = backup.sha256_file(path)
            if actual != expected:
                raise CommandError(
                    f"Checksum mismatch for {arcname}: {actual} != {expected}"
                )
        self.stdout.write(self.style.SUCCESS(
            f"Verified {len(files)} file checksum(s) against manifest."
        ))

    def _report(self, manifest: dict, extract: Path):
        git = manifest.get("git") or {}
        migs = manifest.get("applied_migrations")
        self.stdout.write("Bundle contents:")
        self.stdout.write(f"  created_utc:    {manifest.get('created_utc')}")
        self.stdout.write(f"  git sha:        {git.get('sha')}  (branch {git.get('branch')})")
        self.stdout.write(f"  django/python:  {manifest.get('django_version')} / {manifest.get('python_version')}")
        self.stdout.write(f"  include_secrets:{manifest.get('include_secrets')}")
        self.stdout.write(f"  migrations:     {len(migs) if migs is not None else 'unknown'}")
        present = [m for m in (_DB_MEMBER, _ENV_MEMBER, _CREDS_MEMBER) if (extract / m).exists()]
        self.stdout.write(f"  files:          {', '.join(present)}")
        if git.get("sha"):
            self.stdout.write(self.style.WARNING(
                f"  -> check out matching code first:  git checkout {git['sha']}"
            ))

    def _apply(self, options, extract: Path, manifest: dict):
        db_target = Path(
            options["database_path"] or settings.DATABASES["default"]["NAME"]
        )
        src_db = extract / _DB_MEMBER
        if not src_db.exists():
            raise CommandError("Bundle contains no db.sqlite3 — cannot restore.")
        db_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_db, db_target)
        self.stdout.write(self.style.SUCCESS(f"Restored database -> {db_target}"))

        if options["restore_secrets"]:
            base_dir = Path(options["base_dir"] or settings.BASE_DIR)
            env_src = extract / _ENV_MEMBER
            if env_src.exists():
                env_dst = base_dir / ".env"
                shutil.copy2(env_src, env_dst)
                env_dst.chmod(0o600)
                self.stdout.write(self.style.SUCCESS(f"Restored .env -> {env_dst}"))
            creds_src = extract / _CREDS_MEMBER
            if creds_src.exists():
                creds_dst = base_dir / "secrets" / "client_credentials.json"
                creds_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(creds_src, creds_dst)
                creds_dst.chmod(0o600)
                self.stdout.write(self.style.SUCCESS(f"Restored secrets -> {creds_dst}"))
        elif manifest.get("include_secrets"):
            self.stdout.write(self.style.WARNING(
                "Bundle includes secrets but --restore-secrets was not given; "
                "skipped .env/secrets restore."
            ))

        git = manifest.get("git") or {}
        self.stdout.write(self.style.SUCCESS("Restore applied. Next steps:"))
        if git.get("sha"):
            self.stdout.write(f"  1. git checkout {git['sha']}")
        self.stdout.write("  2. pixi install")
        self.stdout.write("  3. pixi run python manage.py migrate   # should be a no-op")
        self.stdout.write("  4. restart the service (systemctl restart datasetgateway)")

    # --- helpers ------------------------------------------------------------

    def _resolve_decrypt_argv(self):
        cmd = (settings.DSG_BACKUP_DECRYPT_CMD or "").strip()
        if cmd:
            return shlex.split(cmd)
        identity = (settings.DSG_BACKUP_AGE_IDENTITY_FILE or "").strip()
        if identity:
            if not Path(identity).exists():
                raise CommandError(
                    f"DSG_BACKUP_AGE_IDENTITY_FILE is set but not found: {identity}"
                )
            return ["age", "-d", "-i", identity]
        return None

    def _run_filter(self, argv, src: Path, dst: Path, label: str):
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
