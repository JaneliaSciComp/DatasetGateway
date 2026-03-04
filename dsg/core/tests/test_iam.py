"""Unit tests for core.iam — centralized IAM sync logic."""

from unittest.mock import patch

import pytest
from django.test import TestCase

from core.models import (
    Dataset,
    DatasetVersion,
    Grant,
    Group,
    GroupDatasetPermission,
    Permission,
    TOSAcceptance,
    TOSDocument,
    User,
    UserGroup,
)


@pytest.mark.django_db
class TestUserHasEffectiveAccess(TestCase):
    def setUp(self):
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.user = User.objects.create(email="user@example.org", name="User")
        self.dataset = Dataset.objects.create(name="ds1")

    def test_no_grant_no_group_returns_false(self):
        from core.iam import _user_has_effective_access
        self.assertFalse(_user_has_effective_access(self.user, self.dataset))

    def test_grant_no_tos_returns_true(self):
        from core.iam import _user_has_effective_access
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)
        self.assertTrue(_user_has_effective_access(self.user, self.dataset))

    def test_grant_tos_accepted_returns_true(self):
        from core.iam import _user_has_effective_access
        tos = TOSDocument.objects.create(name="TOS", text="Terms", dataset=self.dataset)
        self.dataset.tos = tos
        self.dataset.save()
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)
        TOSAcceptance.objects.create(user=self.user, tos_document=tos)
        self.assertTrue(_user_has_effective_access(self.user, self.dataset))

    def test_grant_tos_not_accepted_returns_false(self):
        from core.iam import _user_has_effective_access
        tos = TOSDocument.objects.create(name="TOS", text="Terms", dataset=self.dataset)
        self.dataset.tos = tos
        self.dataset.save()
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)
        self.assertFalse(_user_has_effective_access(self.user, self.dataset))

    def test_global_admin_returns_false(self):
        from core.iam import _user_has_effective_access
        admin = User.objects.create(email="admin@example.org", admin=True)
        Grant.objects.create(user=admin, dataset=self.dataset, permission=self.view_perm)
        self.assertFalse(_user_has_effective_access(admin, self.dataset))

    def test_group_permission_provisions(self):
        from core.iam import _user_has_effective_access
        group = Group.objects.create(name="lab")
        UserGroup.objects.create(user=self.user, group=group)
        GroupDatasetPermission.objects.create(
            group=group, dataset=self.dataset, permission=self.view_perm,
        )
        self.assertTrue(_user_has_effective_access(self.user, self.dataset))

    def test_group_permission_with_tos_not_accepted_returns_false(self):
        from core.iam import _user_has_effective_access
        tos = TOSDocument.objects.create(name="TOS", text="Terms", dataset=self.dataset)
        self.dataset.tos = tos
        self.dataset.save()
        group = Group.objects.create(name="lab")
        UserGroup.objects.create(user=self.user, group=group)
        GroupDatasetPermission.objects.create(
            group=group, dataset=self.dataset, permission=self.view_perm,
        )
        self.assertFalse(_user_has_effective_access(self.user, self.dataset))

    def test_service_account_inherits_parent_tos(self):
        from core.iam import _user_has_effective_access
        tos = TOSDocument.objects.create(name="TOS", text="Terms", dataset=self.dataset)
        self.dataset.tos = tos
        self.dataset.save()
        sa = User.objects.create(email="sa@example.org", parent=self.user)
        Grant.objects.create(user=sa, dataset=self.dataset, permission=self.view_perm)
        # Parent hasn't accepted yet
        self.assertFalse(_user_has_effective_access(sa, self.dataset))
        # Parent accepts
        TOSAcceptance.objects.create(user=self.user, tos_document=tos)
        self.assertTrue(_user_has_effective_access(sa, self.dataset))


@pytest.mark.django_db
class TestSyncUserDatasetIAM(TestCase):
    def setUp(self):
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.user = User.objects.create(email="user@example.org", name="User")
        self.dataset = Dataset.objects.create(name="ds1")
        self.dv1 = DatasetVersion.objects.create(
            dataset=self.dataset, version="v1", gcs_bucket="bucket-a",
        )
        self.dv2 = DatasetVersion.objects.create(
            dataset=self.dataset, version="v2", gcs_bucket="bucket-b",
        )
        DatasetVersion.objects.create(
            dataset=self.dataset, version="v3", gcs_bucket="",
        )

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_provisions_when_has_access(self, mock_remove, mock_add):
        from core.iam import sync_user_dataset_iam
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)
        mock_add.return_value = True

        sync_user_dataset_iam(self.user, self.dataset)

        called_buckets = sorted(c.args[0] for c in mock_add.call_args_list)
        self.assertEqual(called_buckets, ["bucket-a", "bucket-b"])
        mock_remove.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_deprovisions_when_no_access(self, mock_remove, mock_add):
        from core.iam import sync_user_dataset_iam
        mock_remove.return_value = True

        sync_user_dataset_iam(self.user, self.dataset)

        called_buckets = sorted(c.args[0] for c in mock_remove.call_args_list)
        self.assertEqual(called_buckets, ["bucket-a", "bucket-b"])
        mock_add.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_deprovisions_when_tos_not_accepted(self, mock_remove, mock_add):
        from core.iam import sync_user_dataset_iam
        tos = TOSDocument.objects.create(name="TOS", text="Terms", dataset=self.dataset)
        self.dataset.tos = tos
        self.dataset.save()
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)
        mock_remove.return_value = True

        sync_user_dataset_iam(self.user, self.dataset)

        called_buckets = sorted(c.args[0] for c in mock_remove.call_args_list)
        self.assertEqual(called_buckets, ["bucket-a", "bucket-b"])
        mock_add.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_skips_global_admin(self, mock_remove, mock_add):
        from core.iam import sync_user_dataset_iam
        admin = User.objects.create(email="admin@example.org", admin=True)
        mock_remove.return_value = True

        sync_user_dataset_iam(admin, self.dataset)

        # Admin is deprovision target (returns False from effective_access)
        called_buckets = sorted(c.args[0] for c in mock_remove.call_args_list)
        self.assertEqual(called_buckets, ["bucket-a", "bucket-b"])
        mock_add.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket", side_effect=Exception("GCS error"))
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_best_effort_logs_error(self, mock_remove, mock_add):
        from core.iam import sync_user_dataset_iam
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)

        # Should not raise
        sync_user_dataset_iam(self.user, self.dataset)

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_no_buckets_is_noop(self, mock_remove, mock_add):
        from core.iam import sync_user_dataset_iam
        ds_no_buckets = Dataset.objects.create(name="ds-empty")
        sync_user_dataset_iam(self.user, ds_no_buckets)
        mock_add.assert_not_called()
        mock_remove.assert_not_called()


@pytest.mark.django_db
class TestSyncGroupDatasetsForUser(TestCase):
    def setUp(self):
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.user = User.objects.create(email="user@example.org", name="User")
        self.group = Group.objects.create(name="lab")
        self.dataset = Dataset.objects.create(name="ds1")
        DatasetVersion.objects.create(
            dataset=self.dataset, version="v1", gcs_bucket="bucket-a",
        )
        GroupDatasetPermission.objects.create(
            group=self.group, dataset=self.dataset, permission=self.view_perm,
        )

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_syncs_all_group_datasets(self, mock_remove, mock_add):
        from core.iam import sync_group_datasets_for_user
        UserGroup.objects.create(user=self.user, group=self.group)
        mock_add.return_value = True

        sync_group_datasets_for_user(self.user, self.group)

        mock_add.assert_called_once_with("bucket-a", "user@example.org")
