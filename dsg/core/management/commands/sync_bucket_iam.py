"""Bulk reconciliation of GCS bucket IAM against effective permissions."""

from django.core.management.base import BaseCommand, CommandError

from core.iam import (
    _get_dataset_buckets,
    deprovision_binding,
    permission_source_users,
    provision_binding,
    provisioned_buckets,
)
from core.models import BucketIAMBinding, Dataset


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

        from ngauth.gcs import probe_storage_permission

        total_added = 0
        total_removed = 0
        total_failed = 0
        total_reasserted = 0
        total_skipped_not_owned = 0
        total_satisfied_foreign = 0
        total_pruned = 0
        total_orphans_removed = 0
        visited = set()
        scope_buckets = set()

        for ds in datasets:
            buckets = _get_dataset_buckets(ds)
            if not buckets:
                continue
            scope_buckets.update(buckets)

            self.stdout.write(f"\nDataset: {ds.name} ({len(buckets)} bucket(s))")

            # All users with any permission source for this dataset
            users = permission_source_users(ds)

            for user in users:
                provisioned = provisioned_buckets(user, ds)

                for bucket in buckets:
                    should_provision = bucket in provisioned
                    email = user.email
                    visited.add((bucket, email))
                    probe = probe_storage_permission(email, bucket)
                    owned = BucketIAMBinding.objects.filter(
                        bucket_name=bucket, email=email,
                    ).exists()
                    prefix = "[DRY RUN] " if dry_run else ""

                    if probe is None:
                        total_failed += 1
                        self.stdout.write(f"  {prefix}PROBE-FAIL {email} -> {bucket}")
                        continue

                    if should_provision and probe is False:
                        if dry_run:
                            if owned:
                                total_reasserted += 1
                                action = "REASSERT"
                            else:
                                total_added += 1
                                action = "ADD"
                        else:
                            result = provision_binding(bucket, email)
                            action = self._provision_action(result)
                            if result == "added":
                                total_added += 1
                            elif result == "reasserted":
                                total_reasserted += 1
                            elif result == "foreign":
                                total_satisfied_foreign += 1
                            elif result == "failed":
                                total_failed += 1
                        if action:
                            self.stdout.write(f"  {prefix}{action} {email} -> {bucket}")
                    elif should_provision and probe is True and owned:
                        continue
                    elif should_provision and probe is True and not owned:
                        total_satisfied_foreign += 1
                        self.stdout.write(
                            f"  {prefix}SATISFIED (foreign) {email} -> {bucket}"
                        )
                    elif not should_provision and probe is True and owned:
                        if dry_run:
                            result = "removed"
                        else:
                            result = deprovision_binding(bucket, email)
                        if result == "removed":
                            total_removed += 1
                            self.stdout.write(f"  {prefix}REMOVE {email} -> {bucket}")
                        elif result == "failed":
                            total_failed += 1
                            self.stdout.write(f"  {prefix}FAILED REMOVE {email} -> {bucket}")
                        elif result == "skipped-not-owned":
                            total_skipped_not_owned += 1
                            self.stdout.write(
                                f"  {prefix}SKIP (not DSG-owned) {email} -> {bucket}"
                            )
                    elif not should_provision and probe is True and not owned:
                        total_skipped_not_owned += 1
                        self.stdout.write(
                            f"  {prefix}SKIP (not DSG-owned) {email} -> {bucket}"
                        )
                    elif not should_provision and probe is False and owned:
                        total_pruned += 1
                        if not dry_run:
                            BucketIAMBinding.objects.filter(
                                bucket_name=bucket, email=email,
                            ).delete()
                        self.stdout.write(f"  {prefix}PRUNE ledger {email} -> {bucket}")
                    else:
                        continue

        orphan_rows = self._orphan_rows(dataset_name, scope_buckets, visited)
        prefix = "[DRY RUN] " if dry_run else ""
        for row in orphan_rows:
            if dry_run:
                result = "removed"
            else:
                result = deprovision_binding(row.bucket_name, row.email)
            if result == "removed":
                total_orphans_removed += 1
                self.stdout.write(
                    f"  {prefix}ORPHAN REMOVE {row.email} -> {row.bucket_name}"
                )
            elif result == "failed":
                total_failed += 1
                self.stdout.write(
                    f"  {prefix}FAILED ORPHAN REMOVE {row.email} -> {row.bucket_name}"
                )

        summary = (
            f"\nDone. Added: {total_added}, Removed: {total_removed}, "
            f"Reasserted: {total_reasserted}, "
            f"Skipped (not DSG-owned): {total_skipped_not_owned}, "
            f"Satisfied (foreign): {total_satisfied_foreign}, "
            f"Pruned: {total_pruned}, Orphans removed: {total_orphans_removed}, "
            f"Failures: {total_failed}"
        )
        if dry_run:
            summary += " (dry run)"

        if total_failed:
            self.stdout.write(self.style.ERROR(summary))
            raise CommandError(
                f"Bucket IAM sync completed with {total_failed} failed operation(s)."
            )

        self.stdout.write(self.style.SUCCESS(summary))

    def _provision_action(self, result):
        if result == "added":
            return "ADD"
        if result == "reasserted":
            return "REASSERT"
        if result == "foreign":
            return "SATISFIED (foreign)"
        if result == "failed":
            return "FAILED ADD"
        return None

    def _orphan_rows(self, dataset_name, scope_buckets, visited):
        qs = BucketIAMBinding.objects.all()
        if dataset_name:
            qs = qs.filter(bucket_name__in=scope_buckets)
        return [
            row
            for row in qs.order_by("bucket_name", "email")
            if (row.bucket_name, row.email) not in visited
        ]
