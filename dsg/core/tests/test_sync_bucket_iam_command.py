"""Regression tests for the sync_bucket_iam reconcile command."""

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from core.models import (
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

    @patch("ngauth.gcs.check_storage_permission")
    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_adds_missing_and_removes_stale(self, mock_remove, mock_add, mock_check):
        # Neither user currently on the bucket: effective user added,
        # TOS-pending user has nothing to remove.
        mock_check.return_value = False
        out = self._run()
        mock_add.assert_called_once_with("bucket-a", "accepted@example.org")
        mock_remove.assert_not_called()
        assert "ADD accepted@example.org -> bucket-a" in out

        mock_add.reset_mock()
        # Both users on the bucket: stale TOS-pending user removed.
        mock_check.return_value = True
        out = self._run()
        mock_add.assert_not_called()
        mock_remove.assert_called_once_with("bucket-a", "pending@example.org")
        assert "REMOVE pending@example.org -> bucket-a" in out

    @patch("ngauth.gcs.check_storage_permission", return_value=True)
    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_disabled_user_converges_to_remove(self, mock_remove, mock_add, mock_check):
        # A disabled user with a grant and live bucket access must show as
        # REMOVE — the rule change flows through without any command change.
        self.accepted.is_active = False
        self.accepted.save()
        out = self._run()
        removed = {c.args[1] for c in mock_remove.call_args_list}
        assert "accepted@example.org" in removed
        assert "REMOVE accepted@example.org -> bucket-a" in out
        mock_add.assert_not_called()

    @patch("ngauth.gcs.check_storage_permission", return_value=False)
    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_dry_run_makes_no_calls(self, mock_remove, mock_add, mock_check):
        out = self._run("--dry-run")
        mock_add.assert_not_called()
        mock_remove.assert_not_called()
        assert "[DRY RUN] ADD accepted@example.org -> bucket-a" in out
        assert "(dry run)" in out

    @patch("ngauth.gcs.check_storage_permission")
    @patch("ngauth.gcs.add_user_to_bucket", return_value=False)
    @patch("ngauth.gcs.remove_user_from_bucket", return_value=False)
    def test_failed_gcs_calls_report_and_exit_nonzero(
        self, mock_remove, mock_add, mock_check,
    ):
        mock_check.side_effect = lambda email, bucket: email == "pending@example.org"
        out = StringIO()

        with self.assertRaises(CommandError) as ctx:
            call_command("sync_bucket_iam", stdout=out)

        mock_add.assert_called_once_with("bucket-a", "accepted@example.org")
        mock_remove.assert_called_once_with("bucket-a", "pending@example.org")
        assert "ADD accepted@example.org -> bucket-a" in out.getvalue()
        assert "REMOVE pending@example.org -> bucket-a" in out.getvalue()
        assert "Failures: 2" in out.getvalue()
        assert "2 failed operation" in str(ctx.exception)

    @patch("ngauth.gcs.check_storage_permission", return_value=False)
    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_dataset_filter(self, mock_remove, mock_add, mock_check):
        other = Dataset.objects.create(name="ds2")
        DatasetBucket.objects.create(dataset=other, name="bucket-b")
        other_user = User.objects.create(email="other@example.org")
        Grant.objects.create(user=other_user, dataset=other, permission=self.view_perm)

        self._run("--dataset", "ds1")
        called_buckets = {c.args[0] for c in mock_add.call_args_list}
        assert called_buckets == {"bucket-a"}

    @patch("ngauth.gcs.check_storage_permission")
    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_version_scoped_grant_reconciles_per_bucket(
        self, mock_remove, mock_add, mock_check
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
        mock_check.side_effect = lambda email, bucket: bucket == "version-bucket-b"

        self._run("--dataset", "versioned")

        mock_add.assert_called_once_with("version-bucket-a", "versioned@example.org")
        mock_remove.assert_called_once_with("version-bucket-b", "versioned@example.org")

    def test_unknown_dataset_errors(self):
        out = StringIO()
        err = StringIO()
        call_command("sync_bucket_iam", "--dataset", "nope", stdout=out, stderr=err)
        assert "Dataset not found: nope" in err.getvalue()
