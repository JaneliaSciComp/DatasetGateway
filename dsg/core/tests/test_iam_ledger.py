"""Tests for the bucket IAM provenance ledger and GCS primitives."""

import sys
import types
from unittest.mock import patch

import pytest
from django.test import TestCase

from core.models import BucketIAMBinding


class FakePolicy:
    def __init__(self, bindings=None):
        self.bindings = bindings or []


class FakeBucket:
    def __init__(self, policy):
        self.policy = policy
        self.set_calls = []

    def get_iam_policy(self, requested_policy_version):
        return self.policy

    def set_iam_policy(self, policy):
        self.set_calls.append(policy)


def _storage_modules(bucket):
    storage = types.SimpleNamespace(Client=lambda: types.SimpleNamespace(bucket=lambda name: bucket))
    cloud = types.SimpleNamespace(storage=storage)
    google = types.SimpleNamespace(cloud=cloud)
    return {
        "google": google,
        "google.cloud": cloud,
        "google.cloud.storage": storage,
    }


class TestGCSPrimitive(TestCase):
    def test_add_user_to_bucket_creates_missing_binding(self):
        from ngauth.gcs import add_user_to_bucket

        bucket = FakeBucket(FakePolicy())
        with patch.dict(sys.modules, _storage_modules(bucket)):
            result = add_user_to_bucket("bucket-a", "user@example.org")

        self.assertEqual(result, "created")
        self.assertEqual(bucket.set_calls, [bucket.policy])
        self.assertEqual(bucket.policy.bindings, [{
            "role": "roles/storage.objectViewer",
            "members": {"user:user@example.org"},
        }])

    def test_add_user_to_bucket_does_not_duplicate_existing_member(self):
        from ngauth.gcs import add_user_to_bucket

        bucket = FakeBucket(FakePolicy([{
            "role": "roles/storage.objectViewer",
            "members": {"user:user@example.org"},
        }]))
        with patch.dict(sys.modules, _storage_modules(bucket)):
            result = add_user_to_bucket("bucket-a", "user@example.org")

        self.assertEqual(result, "already_present")
        self.assertEqual(bucket.set_calls, [])

    def test_add_user_to_bucket_maps_errors_to_failed(self):
        from ngauth.gcs import add_user_to_bucket

        class FailingBucket(FakeBucket):
            def set_iam_policy(self, policy):
                raise RuntimeError("etag conflict")

        bucket = FailingBucket(FakePolicy())
        with patch.dict(sys.modules, _storage_modules(bucket)):
            result = add_user_to_bucket("bucket-a", "user@example.org")

        self.assertEqual(result, "failed")

    def test_probe_storage_permission_distinguishes_absent_and_failure(self):
        from ngauth.gcs import probe_storage_permission

        absent = FakeBucket(FakePolicy())
        with patch.dict(sys.modules, _storage_modules(absent)):
            self.assertIs(probe_storage_permission("user@example.org", "bucket-a"), False)

        present = FakeBucket(FakePolicy([{
            "role": "roles/storage.objectViewer",
            "members": {"user:user@example.org"},
        }]))
        with patch.dict(sys.modules, _storage_modules(present)):
            self.assertIs(probe_storage_permission("user@example.org", "bucket-a"), True)

        class FailingBucket(FakeBucket):
            def get_iam_policy(self, requested_policy_version):
                raise RuntimeError("unavailable")

        failed = FailingBucket(FakePolicy())
        with patch.dict(sys.modules, _storage_modules(failed)):
            self.assertIsNone(probe_storage_permission("user@example.org", "bucket-a"))


@pytest.mark.django_db
class TestLedgerWrappers(TestCase):
    @patch("ngauth.gcs.add_user_to_bucket", return_value="created")
    def test_created_binding_writes_ledger_row(self, mock_add):
        from core.iam import provision_binding

        result = provision_binding("bucket-a", "user@example.org")

        self.assertEqual(result, "added")
        self.assertTrue(
            BucketIAMBinding.objects.filter(
                bucket_name="bucket-a", email="user@example.org",
            ).exists()
        )

    @patch("ngauth.gcs.add_user_to_bucket", return_value="already_present")
    def test_preexisting_binding_is_foreign_and_not_claimed(self, mock_add):
        from core.iam import provision_binding

        result = provision_binding("bucket-a", "user@example.org")

        self.assertEqual(result, "foreign")
        self.assertFalse(BucketIAMBinding.objects.exists())

    @patch("ngauth.gcs.add_user_to_bucket", return_value="already_present")
    def test_existing_row_and_existing_binding_is_already_owned(self, mock_add):
        from core.iam import provision_binding

        BucketIAMBinding.objects.create(bucket_name="bucket-a", email="user@example.org")

        self.assertEqual(provision_binding("bucket-a", "user@example.org"), "already-owned")
        self.assertEqual(BucketIAMBinding.objects.count(), 1)

    @patch("ngauth.gcs.add_user_to_bucket", return_value="created")
    def test_existing_row_and_created_binding_is_reasserted(self, mock_add):
        from core.iam import provision_binding

        BucketIAMBinding.objects.create(bucket_name="bucket-a", email="user@example.org")

        self.assertEqual(provision_binding("bucket-a", "user@example.org"), "reasserted")
        self.assertEqual(BucketIAMBinding.objects.count(), 1)

    @patch("ngauth.gcs.add_user_to_bucket", return_value="failed")
    def test_failed_add_never_writes_ledger_row(self, mock_add):
        from core.iam import provision_binding

        self.assertEqual(provision_binding("bucket-a", "user@example.org"), "failed")
        self.assertFalse(BucketIAMBinding.objects.exists())

    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_remove_without_ledger_row_never_calls_gcs(self, mock_remove):
        from core.iam import deprovision_binding

        self.assertEqual(
            deprovision_binding("bucket-a", "user@example.org"),
            "skipped-not-owned",
        )
        mock_remove.assert_not_called()

    @patch("ngauth.gcs.remove_user_from_bucket", return_value=True)
    def test_remove_with_ledger_row_deletes_row_on_success(self, mock_remove):
        from core.iam import deprovision_binding

        BucketIAMBinding.objects.create(bucket_name="bucket-a", email="user@example.org")

        self.assertEqual(deprovision_binding("bucket-a", "user@example.org"), "removed")
        mock_remove.assert_called_once_with("bucket-a", "user@example.org")
        self.assertFalse(BucketIAMBinding.objects.exists())

    @patch("ngauth.gcs.remove_user_from_bucket", return_value=False)
    def test_remove_failure_keeps_ledger_row_for_retry(self, mock_remove):
        from core.iam import deprovision_binding

        BucketIAMBinding.objects.create(bucket_name="bucket-a", email="user@example.org")

        self.assertEqual(deprovision_binding("bucket-a", "user@example.org"), "failed")
        self.assertTrue(BucketIAMBinding.objects.exists())
