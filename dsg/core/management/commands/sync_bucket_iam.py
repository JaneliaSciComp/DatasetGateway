"""Bulk reconciliation of GCS bucket IAM against effective permissions."""

from django.core.management.base import BaseCommand

from core.iam import _get_dataset_buckets, _user_has_effective_access
from core.models import (
    Dataset,
    Grant,
    GroupDatasetPermission,
    User,
    UserGroup,
)


class Command(BaseCommand):
    help = "Reconcile GCS bucket IAM bindings with effective user permissions"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dataset", type=str, default=None,
            help="Specific dataset name to sync (default: all datasets with buckets)",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Preview changes without modifying bucket IAM",
        )

    def handle(self, *args, **options):
        dataset_name = options["dataset"]
        dry_run = options["dry_run"]

        if dataset_name:
            datasets = Dataset.objects.filter(name=dataset_name)
            if not datasets.exists():
                self.stderr.write(self.style.ERROR(f"Dataset not found: {dataset_name}"))
                return
        else:
            datasets = Dataset.objects.all()

        from ngauth.gcs import add_user_to_bucket, check_storage_permission, remove_user_from_bucket

        total_added = 0
        total_removed = 0

        for ds in datasets:
            buckets = _get_dataset_buckets(ds)
            if not buckets:
                continue

            self.stdout.write(f"\nDataset: {ds.name} ({len(buckets)} bucket(s))")

            # Find all users with any permission source for this dataset
            user_ids = set(
                Grant.objects.filter(dataset=ds).values_list("user_id", flat=True)
            )
            group_ids = GroupDatasetPermission.objects.filter(
                dataset=ds
            ).values_list("group_id", flat=True)
            group_user_ids = set(
                UserGroup.objects.filter(
                    group_id__in=group_ids
                ).values_list("user_id", flat=True)
            )
            user_ids |= group_user_ids

            users = User.objects.filter(pk__in=user_ids)

            for user in users:
                should_provision = _user_has_effective_access(user, ds)

                for bucket in buckets:
                    has_access = check_storage_permission(user.email, bucket)

                    if should_provision and not has_access:
                        action = "ADD"
                        total_added += 1
                        if not dry_run:
                            add_user_to_bucket(bucket, user.email)
                    elif not should_provision and has_access:
                        action = "REMOVE"
                        total_removed += 1
                        if not dry_run:
                            remove_user_from_bucket(bucket, user.email)
                    else:
                        continue

                    prefix = "[DRY RUN] " if dry_run else ""
                    self.stdout.write(
                        f"  {prefix}{action} {user.email} -> {bucket}"
                    )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. Added: {total_added}, Removed: {total_removed}"
                + (" (dry run)" if dry_run else "")
            )
        )
