"""Import users from a CSV file into DatasetGateway.

Usage:
    python manage.py import_csv users.csv --dataset fish2
    python manage.py import_csv users.csv --dataset fish2 --dry-run

Expected CSV columns: email, name, affiliation, notes
(Header row required; column matching is case-insensitive.)

Each user receives a ``view`` grant on the specified dataset.  Existing users
are updated additively: name is filled if blank, affiliations are unioned,
notes are appended.
"""

import csv

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.audit import log_audit
from core.models import (
    Affiliation,
    Dataset,
    Grant,
    Group,
    Permission,
    User,
    UserGroup,
)


class Command(BaseCommand):
    help = "Import users from a CSV file (email, name, affiliation, notes)"

    def add_arguments(self, parser):
        parser.add_argument("csv_file", help="Path to the CSV file")
        parser.add_argument(
            "--dataset",
            required=True,
            help="DSG dataset slug to grant view permission on",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be created without writing to DB",
        )

    def handle(self, *args, **options):
        rows = self._read_csv(options["csv_file"])
        dry_run = options["dry_run"]
        dataset_name = options["dataset"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be made\n"))
            self._dry_run(rows, dataset_name)
            return

        self._import(rows, dataset_name, options["csv_file"])

    def _read_csv(self, path):
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise CommandError(f"CSV file is empty: {path}")
            normalised = {h: h.strip().lower() for h in reader.fieldnames}
            if "email" not in normalised.values():
                raise CommandError(
                    f"CSV must contain an 'email' column. "
                    f"Found: {', '.join(reader.fieldnames)}"
                )
            rows = []
            for raw in reader:
                row = {normalised[k]: v.strip() for k, v in raw.items()}
                if row.get("email"):
                    rows.append(row)
            return rows

    def _dry_run(self, rows, dataset_name):
        ds_exists = Dataset.objects.filter(name=dataset_name).exists()
        ds_status = "exists" if ds_exists else "will be created"
        self.stdout.write(f"  Dataset: {dataset_name} ({ds_status})")

        for row in rows:
            email = row["email"]
            name = row.get("name", "")
            affiliation = row.get("affiliation", "")
            notes = row.get("notes", "")

            exists = User.objects.filter(email=email).exists()
            user_status = "exists" if exists else "new"
            parts = [f"  {email} ({user_status}): view on {dataset_name}"]
            if name:
                parts.append(f"name={name!r}")
            if affiliation:
                parts.append(f"affiliation={affiliation!r}")
            if notes:
                parts.append(f"notes={notes!r}")
            self.stdout.write(", ".join(parts))

        self.stdout.write(f"\nTotal: {len(rows)} users")

    @transaction.atomic
    def _import(self, rows, dataset_name, csv_file):
        view_perm, _ = Permission.objects.get_or_create(name="view")
        user_group, _ = Group.objects.get_or_create(name="user")
        dataset, ds_created = Dataset.objects.get_or_create(name=dataset_name)
        ds_status = "Created" if ds_created else "Exists"
        self.stdout.write(f"  {ds_status} dataset: {dataset_name}")

        added = []
        existed = []

        for row in rows:
            email = row["email"]
            name = row.get("name", "")
            affiliation = row.get("affiliation", "")
            notes = row.get("notes", "")

            user_obj, user_created = User.objects.get_or_create(
                email=email,
                defaults={"name": name},
            )
            if user_created:
                user_obj.set_unusable_password()
                if notes:
                    user_obj.notes = notes
                user_obj.save()
            else:
                changed = False
                if name and not user_obj.name:
                    user_obj.name = name
                    changed = True
                if notes:
                    if user_obj.notes:
                        user_obj.notes = user_obj.notes + "\n" + notes
                    else:
                        user_obj.notes = notes
                    changed = True
                if changed:
                    user_obj.save()

            if affiliation:
                Affiliation.objects.get_or_create(user=user_obj, name=affiliation)

            UserGroup.objects.get_or_create(user=user_obj, group=user_group)

            _, grant_created = Grant.objects.get_or_create(
                user=user_obj,
                dataset=dataset,
                permission=view_perm,
            )
            if grant_created:
                added.append(email)
            else:
                existed.append(email)

            action = "Created" if user_created else "Updated"
            self.stdout.write(f"  {action} user: {email}")

        self.stdout.write("")
        if added:
            self.stdout.write(
                self.style.SUCCESS(f"  Added view grants: {', '.join(added)}")
            )
        if existed:
            self.stdout.write(f"  Already had view grants: {', '.join(existed)}")

        log_audit(None, "bulk_import", "Command", "import_csv", after_state={
            "source": csv_file,
            "dataset": dataset_name,
            "users": len(rows),
            "grants_added": len(added),
        })

        self.stdout.write(
            self.style.SUCCESS(
                f"\nImported {len(rows)} users for dataset {dataset_name}."
            )
        )
