"""Import clio-store auth data from exported JSON into DatasetGateway.

Usage:
    python manage.py import_clio_auth exported_auth.json

Reads the JSON produced by clio-store's scripts/export_auth.py and creates
the corresponding User, Dataset, Grant, Group, and UserGroup records in
DatasetGateway's database.
"""

import json

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import (
    Dataset,
    Grant,
    Group,
    GroupDatasetPermission,
    Permission,
    User,
    UserGroup,
)


class Command(BaseCommand):
    help = "Import clio-store auth data from exported JSON"

    def add_arguments(self, parser):
        parser.add_argument("json_file", help="Path to exported_auth.json")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be created without writing to DB",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        with open(options["json_file"]) as f:
            data = json.load(f)

        dry_run = options["dry_run"]

        # Ensure permissions exist
        view_perm, _ = Permission.objects.get_or_create(name="view")
        edit_perm, _ = Permission.objects.get_or_create(name="edit")
        admin_perm, _ = Permission.objects.get_or_create(name="admin")

        # Ensure a default "user" group exists (for public dataset grants)
        user_group, _ = Group.objects.get_or_create(name="user")

        # --- Create datasets ---
        dataset_objs = {}
        for ds_name, ds_data in data.get("datasets", {}).items():
            ds_obj, created = Dataset.objects.get_or_create(name=ds_name)
            dataset_objs[ds_name] = ds_obj
            action = "Created" if created else "Exists"
            self.stdout.write(f"  {action} dataset: {ds_name}")

            # If public, grant view to the "user" group
            if ds_data.get("public"):
                _, created = GroupDatasetPermission.objects.get_or_create(
                    group=user_group, dataset=ds_obj, permission=view_perm,
                )
                if created:
                    self.stdout.write(
                        f"    Granted view on {ds_name} to 'user' group (public)"
                    )

        # Collect all dataset names for global role expansion
        all_dataset_names = list(dataset_objs.keys())

        # --- Create users ---
        users_data = data.get("users", {})
        for email, udata in users_data.items():
            is_admin = "admin" in udata.get("global_roles", [])
            is_active = not udata.get("disabled", False)
            name = udata.get("name", "")

            user_obj, created = User.objects.get_or_create(
                email=email,
                defaults={
                    "name": name,
                    "admin": is_admin,
                    "is_active": is_active,
                },
            )
            if not created:
                # Update fields if user already exists
                user_obj.admin = user_obj.admin or is_admin
                user_obj.is_active = is_active
                if name and not user_obj.name:
                    user_obj.name = name
                user_obj.save()

            action = "Created" if created else "Updated"
            self.stdout.write(f"  {action} user: {email} (admin={user_obj.admin})")

            # Add user to the "user" group
            UserGroup.objects.get_or_create(user=user_obj, group=user_group)

            # --- Process global roles ---
            global_roles = set(udata.get("global_roles", []))

            # clio_general global → view on every dataset
            if "clio_general" in global_roles:
                for ds_name in all_dataset_names:
                    ds_obj = dataset_objs[ds_name]
                    Grant.objects.get_or_create(
                        user=user_obj, dataset=ds_obj, permission=view_perm,
                    )
                self.stdout.write(f"    Global clio_general → view on all datasets")

            # clio_write global → edit on every dataset
            if "clio_write" in global_roles:
                for ds_name in all_dataset_names:
                    ds_obj = dataset_objs[ds_name]
                    Grant.objects.get_or_create(
                        user=user_obj, dataset=ds_obj, permission=edit_perm,
                    )
                self.stdout.write(f"    Global clio_write → edit on all datasets")

            # --- Process per-dataset roles ---
            for ds_name, roles in udata.get("datasets", {}).items():
                ds_obj = dataset_objs.get(ds_name)
                if ds_obj is None:
                    # Dataset referenced by user but not in datasets export
                    ds_obj, _ = Dataset.objects.get_or_create(name=ds_name)
                    dataset_objs[ds_name] = ds_obj
                    self.stdout.write(f"  Created dataset (from user ref): {ds_name}")

                roles_set = set(roles)
                if "clio_read" in roles_set or "clio_general" in roles_set:
                    Grant.objects.get_or_create(
                        user=user_obj, dataset=ds_obj, permission=view_perm,
                    )
                    self.stdout.write(f"    {ds_name}: view")

                if "clio_write" in roles_set:
                    Grant.objects.get_or_create(
                        user=user_obj, dataset=ds_obj, permission=edit_perm,
                    )
                    self.stdout.write(f"    {ds_name}: edit")

                if "dataset_admin" in roles_set:
                    Grant.objects.get_or_create(
                        user=user_obj, dataset=ds_obj, permission=admin_perm,
                    )
                    self.stdout.write(f"    {ds_name}: admin grant")

            # --- Process groups ---
            for group_name in udata.get("groups", []):
                grp, _ = Group.objects.get_or_create(name=group_name)
                UserGroup.objects.get_or_create(user=user_obj, group=grp)
                self.stdout.write(f"    Group: {group_name}")

        self.stdout.write(
            self.style.SUCCESS(
                f"\nImported {len(users_data)} users and "
                f"{len(dataset_objs)} datasets."
            )
        )
