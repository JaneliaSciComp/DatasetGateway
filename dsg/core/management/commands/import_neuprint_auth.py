"""Import neuprint authorized.json into DatasetGateway.

Usage:
    python manage.py import_neuprint_auth authorized.json --datasets hemibrain manc

Reads neuprint's authorized.json (a flat mapping of email → permission level)
and creates the corresponding User and Grant records in DatasetGateway.

Permission mapping:
    "readonly"  → view grant on each specified dataset
    "readwrite" → edit grant on each specified dataset
    "admin"     → user.admin = True (global admin)
"""

import json
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import (
    Dataset,
    Grant,
    Group,
    Permission,
    User,
    UserGroup,
)

LEVEL_MAP = {
    "readonly": "view",
    "readwrite": "edit",
    "admin": "admin",
}


class Command(BaseCommand):
    help = "Import neuprint authorized.json into DatasetGateway"

    def add_arguments(self, parser):
        parser.add_argument("json_file", help="Path to authorized.json")
        parser.add_argument(
            "--datasets",
            nargs="+",
            required=True,
            help="DSG dataset slug(s) to create grants for",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be created without writing to DB",
        )

    def handle(self, *args, **options):
        with open(options["json_file"]) as f:
            auth_map = json.load(f)

        dry_run = options["dry_run"]
        dataset_names = options["datasets"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be made\n"))
            self._dry_run(auth_map, dataset_names)
            return

        self._import(auth_map, dataset_names)

    def _dry_run(self, auth_map, dataset_names):
        """Preview what would happen without touching the database."""
        for ds_name in dataset_names:
            exists = Dataset.objects.filter(name=ds_name).exists()
            status = "exists" if exists else "will be created"
            self.stdout.write(f"  Dataset: {ds_name} ({status})")

        for email, level_str in auth_map.items():
            level_str = level_str.lower().strip()
            dsg_perm = LEVEL_MAP.get(level_str)
            if dsg_perm is None:
                self.stderr.write(f"  Unknown level '{level_str}' for {email}, skipping")
                continue

            exists = User.objects.filter(email=email).exists()
            user_status = "exists" if exists else "new"

            if level_str == "admin":
                self.stdout.write(f"  {email} ({user_status}): set admin=True")
            else:
                ds_list = ", ".join(dataset_names)
                self.stdout.write(
                    f"  {email} ({user_status}): {dsg_perm} on [{ds_list}]"
                )

        self.stdout.write(
            f"\nTotal: {len(auth_map)} users across {len(dataset_names)} datasets"
        )

    @transaction.atomic
    def _import(self, auth_map, dataset_names):
        # Ensure permissions exist
        view_perm, _ = Permission.objects.get_or_create(name="view")
        edit_perm, _ = Permission.objects.get_or_create(name="edit")
        perm_objs = {"view": view_perm, "edit": edit_perm}

        # Ensure "user" group exists
        user_group, _ = Group.objects.get_or_create(name="user")

        # Create/get datasets
        dataset_objs = {}
        for ds_name in dataset_names:
            ds_obj, created = Dataset.objects.get_or_create(name=ds_name)
            dataset_objs[ds_name] = ds_obj
            status = "Created" if created else "Exists"
            self.stdout.write(f"  {status} dataset: {ds_name}")

        # Per-dataset tracking: added vs already existed
        # {ds_name: {"added": [(email, perm)], "existed": [(email, perm)]}}
        ds_log = {ds: {"added": [], "existed": []} for ds in dataset_names}
        admin_added = []
        admin_existed = []
        skipped = []

        for email, level_str in auth_map.items():
            level_str = level_str.lower().strip()
            dsg_perm = LEVEL_MAP.get(level_str)
            if dsg_perm is None:
                self.stderr.write(f"  Unknown level '{level_str}' for {email}, skipping")
                skipped.append(email)
                continue

            # Create or get user
            user_obj, user_created = User.objects.get_or_create(
                email=email,
                defaults={"name": email.split("@")[0]},
            )
            if user_created:
                user_obj.set_unusable_password()
                user_obj.save()

            # Add to "user" group
            UserGroup.objects.get_or_create(user=user_obj, group=user_group)

            if level_str == "admin":
                if user_obj.admin:
                    admin_existed.append(email)
                else:
                    user_obj.admin = True
                    user_obj.save()
                    admin_added.append(email)
            else:
                perm_obj = perm_objs[dsg_perm]
                for ds_name, ds_obj in dataset_objs.items():
                    _, grant_created = Grant.objects.get_or_create(
                        user=user_obj,
                        dataset=ds_obj,
                        permission=perm_obj,
                    )
                    if grant_created:
                        ds_log[ds_name]["added"].append((email, dsg_perm))
                    else:
                        ds_log[ds_name]["existed"].append((email, dsg_perm))

        # --- Print per-dataset summary ---
        self.stdout.write("")
        for ds_name in dataset_names:
            self.stdout.write(self.style.MIGRATE_HEADING(f"Dataset: {ds_name}"))
            added = ds_log[ds_name]["added"]
            existed = ds_log[ds_name]["existed"]

            if added:
                entries = ", ".join(f"{e} ({p})" for e, p in added)
                self.stdout.write(self.style.SUCCESS(f"  Added: {entries}"))
            else:
                self.stdout.write("  Added: (none)")

            if existed:
                entries = ", ".join(f"{e} ({p})" for e, p in existed)
                self.stdout.write(f"  Already existed: {entries}")

        # Admin summary
        if admin_added or admin_existed:
            self.stdout.write(self.style.MIGRATE_HEADING("\nGlobal admins"))
            if admin_added:
                self.stdout.write(
                    self.style.SUCCESS(f"  Set admin=True: {', '.join(admin_added)}")
                )
            if admin_existed:
                self.stdout.write(
                    f"  Already admin: {', '.join(admin_existed)}"
                )

        if skipped:
            self.stdout.write(
                self.style.WARNING(f"\nSkipped (unknown level): {', '.join(skipped)}")
            )

        total_users = len(auth_map) - len(skipped)
        self.stdout.write(
            self.style.SUCCESS(
                f"\nImported {total_users} users across "
                f"{len(dataset_names)} datasets."
            )
        )
