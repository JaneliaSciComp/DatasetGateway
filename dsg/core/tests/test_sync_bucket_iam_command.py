"""Regression tests for the sync_bucket_iam reconcile command."""

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from core.models import (
    BucketIAMBinding,
    Dataset,
    DatasetBucket,
    DatasetVersion,
    Grant,
    Permission,
    TOSAcceptance,
    TOSDocument,
    User,
)


@pytest.mark.django_db
class TestSyncBucketIAMCommand(TestCase):
    def setUp(self):
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.dataset = Dataset.objects.create(name="ds1")
        DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        self.tos = TOSDocument.objects.create(name="TOS", text="Terms", dataset=self.dataset)
        self.dataset.tos = self.tos
        self.dataset.save()
        self.accepted = User.objects.create(email="accepted@example.org")
        Grant.objects.create(user=self.accepted, dataset=self.dataset, permission=self.view_perm)
        TOSAcceptance.objects.create(user=self.accepted, tos_document=self.tos)
        self.pending = User.objects.create(email="pending@example.org")
        Grant.objects.create(user=self.pending, dataset=self.dataset, permission=self.view_perm)

    def _run(self, *args):
        out = StringIO()
        call_command("sync_bucket_iam", *args, stdout=out)
        return out.getvalue()

    @patch("ngauth.gcs.probe_storage_permission")
    @patch("ngauth.gcs.add_user_to_bucket", return_value="created")
    @patch("ngauth.gcs.remove_user_from_bucket", return_value=True)
    def test_adds_missing_and_skips_foreign_without_claiming(
        self, mock_remove, mock_add, mock_probe,
    ):
        mock_probe.side_effect = lambda email, bucket: email == "pending@example.org"

        out = self._run()

        mock_add.assert_called_once_with("bucket-a", "accepted@example.org")
        mock_remove.assert_not_called()
        self.assertTrue(BucketIAMBinding.objects.filter(
            bucket_name="bucket-a", email="accepted@example.org",
        ).exists())
        self.assertFalse(BucketIAMBinding.objects.filter(
            bucket_name="bucket-a", email="pending@example.org",
        ).exists())
        assert "ADD accepted@example.org -> bucket-a" in out
        assert "SKIP (not DSG-owned) pending@example.org -> bucket-a" in out
        assert "Skipped (not DSG-owned): 1" in out

    @patch("ngauth.gcs.probe_storage_permission", return_value=True)
    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket", return_value=True)
    def test_disabled_owned_user_converges_to_remove(
        self, mock_remove, mock_add, mock_probe,
    ):
        self.accepted.is_active = False
        self.accepted.save()
        BucketIAMBinding.objects.create(
            bucket_name="bucket-a", email="accepted@example.org",
        )

        out = self._run()

        mock_remove.assert_called_once_with("bucket-a", "accepted@example.org")
        assert "REMOVE accepted@example.org -> bucket-a" in out
        mock_add.assert_not_called()
        self.assertFalse(BucketIAMBinding.objects.filter(email="accepted@example.org").exists())

    @patch("ngauth.gcs.probe_storage_permission", return_value=False)
    @patch("ngauth.gcs.add_user_to_bucket", return_value="created")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_dry_run_makes_no_calls_or_ledger_writes(
        self, mock_remove, mock_add, mock_probe,
    ):
        out = self._run("--dry-run")

        mock_add.assert_not_called()
        mock_remove.assert_not_called()
        self.assertFalse(BucketIAMBinding.objects.exists())
        assert "[DRY RUN] ADD accepted@example.org -> bucket-a" in out
        assert "(dry run)" in out

    @patch("ngauth.gcs.probe_storage_permission", return_value=None)
    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_probe_fail_reports_and_exits_nonzero_in_dry_run(
        self, mock_remove, mock_add, mock_probe,
    ):
        out = StringIO()

        with self.assertRaises(CommandError) as ctx:
            call_command("sync_bucket_iam", "--dry-run", stdout=out)

        mock_add.assert_not_called()
        mock_remove.assert_not_called()
        assert "PROBE-FAIL accepted@example.org -> bucket-a" in out.getvalue()
        assert "Failures: 2" in out.getvalue()
        assert "2 failed operation" in str(ctx.exception)

    @patch("ngauth.gcs.probe_storage_permission")
    @patch("ngauth.gcs.add_user_to_bucket", return_value="failed")
    @patch("ngauth.gcs.remove_user_from_bucket", return_value=False)
    def test_failed_gcs_calls_report_and_exit_nonzero(
        self, mock_remove, mock_add, mock_probe,
    ):
        BucketIAMBinding.objects.create(
            bucket_name="bucket-a", email="pending@example.org",
        )
        mock_probe.side_effect = lambda email, bucket: email == "pending@example.org"
        out = StringIO()

        with self.assertRaises(CommandError) as ctx:
            call_command("sync_bucket_iam", stdout=out)

        mock_add.assert_called_once_with("bucket-a", "accepted@example.org")
        mock_remove.assert_called_once_with("bucket-a", "pending@example.org")
        assert "FAILED ADD accepted@example.org -> bucket-a" in out.getvalue()
        assert "FAILED REMOVE pending@example.org -> bucket-a" in out.getvalue()
        assert "Failures: 2" in out.getvalue()
        assert "2 failed operation" in str(ctx.exception)

    @patch("ngauth.gcs.probe_storage_permission", return_value=False)
    @patch("ngauth.gcs.add_user_to_bucket", return_value="created")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_dataset_filter(self, mock_remove, mock_add, mock_probe):
        other = Dataset.objects.create(name="ds2")
        DatasetBucket.objects.create(dataset=other, name="bucket-b")
        other_user = User.objects.create(email="other@example.org")
        Grant.objects.create(user=other_user, dataset=other, permission=self.view_perm)

        self._run("--dataset", "ds1")

        called_buckets = {c.args[0] for c in mock_add.call_args_list}
        assert called_buckets == {"bucket-a"}

    @patch("ngauth.gcs.probe_storage_permission")
    @patch("ngauth.gcs.add_user_to_bucket", return_value="created")
    @patch("ngauth.gcs.remove_user_from_bucket", return_value=True)
    def test_version_scoped_grant_reconciles_per_bucket(
        self, mock_remove, mock_add, mock_probe,
    ):
        versioned = Dataset.objects.create(name="versioned")
        bucket_a = DatasetBucket.objects.create(dataset=versioned, name="version-bucket-a")
        bucket_b = DatasetBucket.objects.create(dataset=versioned, name="version-bucket-b")
        dv1 = DatasetVersion.objects.create(
            dataset=versioned, version="v1", branch="main", ordinal=1
        )
        dv1.buckets.add(bucket_a)
        dv2 = DatasetVersion.objects.create(
            dataset=versioned, version="v2", branch="main", ordinal=2
        )
        dv2.buckets.add(bucket_b)
        user = User.objects.create(email="versioned@example.org")
        Grant.objects.create(
            user=user, dataset=versioned, dataset_version=dv1, permission=self.view_perm
        )
        BucketIAMBinding.objects.create(
            bucket_name="version-bucket-b", email="versioned@example.org",
        )
        mock_probe.side_effect = lambda email, bucket: bucket == "version-bucket-b"

        self._run("--dataset", "versioned")

        mock_add.assert_called_once_with("version-bucket-a", "versioned@example.org")
        mock_remove.assert_called_once_with("version-bucket-b", "versioned@example.org")

    @patch("ngauth.gcs.probe_storage_permission", return_value=False)
    @patch("ngauth.gcs.add_user_to_bucket", return_value="created")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_prunes_owned_row_only_on_definitive_absent_probe(
        self, mock_remove, mock_add, mock_probe,
    ):
        BucketIAMBinding.objects.create(
            bucket_name="bucket-a", email="pending@example.org",
        )

        out = self._run()

        mock_remove.assert_not_called()
        assert "PRUNE ledger pending@example.org -> bucket-a" in out
        self.assertFalse(BucketIAMBinding.objects.filter(email="pending@example.org").exists())

    @patch("ngauth.gcs.probe_storage_permission", return_value=False)
    @patch("ngauth.gcs.add_user_to_bucket", return_value="created")
    @patch("ngauth.gcs.remove_user_from_bucket", return_value=True)
    def test_orphan_sweep_removes_deleted_user_and_renamed_bucket_rows(
        self, mock_remove, mock_add, mock_probe,
    ):
        no_users = Dataset.objects.create(name="no-users")
        DatasetBucket.objects.create(dataset=no_users, name="current-bucket")
        BucketIAMBinding.objects.create(
            bucket_name="current-bucket", email="deleted@example.org",
        )
        BucketIAMBinding.objects.create(
            bucket_name="old-bucket", email="renamed@example.org",
        )

        out = self._run("--dataset", "no-users")
        self.assertNotIn("ORPHAN REMOVE renamed@example.org -> old-bucket", out)

        out = self._run()

        calls = {(c.args[0], c.args[1]) for c in mock_remove.call_args_list}
        self.assertIn(("old-bucket", "renamed@example.org"), calls)
        assert "ORPHAN REMOVE renamed@example.org -> old-bucket" in out
        self.assertFalse(BucketIAMBinding.objects.filter(email="renamed@example.org").exists())

    def test_unknown_dataset_errors(self):
        out = StringIO()
        err = StringIO()
        call_command("sync_bucket_iam", "--dataset", "nope", stdout=out, stderr=err)
        assert "Dataset not found: nope" in err.getvalue()
