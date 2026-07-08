"""Unit tests for core.iam — centralized IAM sync logic."""

from unittest.mock import patch

import pytest
from django.test import TestCase

from core.models import (
    BucketIAMBinding,
    Dataset,
    DatasetBucket,
    DatasetVersion,
    Grant,
    Group,
    GroupDatasetPermission,
    Permission,
    Service,
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
        DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")

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

    def test_disabled_user_returns_false(self):
        from core.iam import _user_has_effective_access
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)
        self.user.is_active = False
        self.user.save()
        self.assertFalse(_user_has_effective_access(self.user, self.dataset))

    def test_disabled_service_account_user_returns_false(self):
        from core.iam import _user_has_effective_access
        sa = User.objects.create(email="sa@example.org", parent=self.user, is_active=False)
        Grant.objects.create(user=sa, dataset=self.dataset, permission=self.view_perm)
        self.assertFalse(_user_has_effective_access(sa, self.dataset))

    def test_disabled_parent_gates_service_account(self):
        from core.iam import _user_has_effective_access
        sa = User.objects.create(email="sa@example.org", parent=self.user)
        Grant.objects.create(user=sa, dataset=self.dataset, permission=self.view_perm)
        self.assertTrue(_user_has_effective_access(sa, self.dataset))
        # Disabling the parent kills the robot too
        self.user.is_active = False
        self.user.save()
        sa.refresh_from_db()
        self.assertFalse(_user_has_effective_access(sa, self.dataset))
        # Re-enabling restores it
        self.user.is_active = True
        self.user.save()
        sa.refresh_from_db()
        self.assertTrue(_user_has_effective_access(sa, self.dataset))


@pytest.mark.django_db
class TestSyncUserDatasetIAM(TestCase):
    def setUp(self):
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.user = User.objects.create(email="user@example.org", name="User")
        self.dataset = Dataset.objects.create(name="ds1")
        self.bucket_a = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        self.bucket_b = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-b")
        self.dv1 = DatasetVersion.objects.create(
            dataset=self.dataset, version="v1", branch="main", ordinal=1,
        )
        self.dv1.buckets.add(self.bucket_a)
        self.dv2 = DatasetVersion.objects.create(
            dataset=self.dataset, version="v2", branch="main", ordinal=2,
        )
        self.dv2.buckets.add(self.bucket_b)
        DatasetVersion.objects.create(
            dataset=self.dataset, version="v3", branch="main", ordinal=3,
        )

    def _own(self, *bucket_names, email="user@example.org"):
        for bucket_name in bucket_names:
            BucketIAMBinding.objects.create(bucket_name=bucket_name, email=email)

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_provisions_when_has_access(self, mock_remove, mock_add):
        from core.iam import sync_user_dataset_iam
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)
        mock_add.return_value = "created"

        sync_user_dataset_iam(self.user, self.dataset)

        called_buckets = sorted(c.args[0] for c in mock_add.call_args_list)
        self.assertEqual(called_buckets, ["bucket-a", "bucket-b"])
        mock_remove.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_deprovisions_when_no_access(self, mock_remove, mock_add):
        from core.iam import sync_user_dataset_iam
        mock_remove.return_value = True
        self._own("bucket-a", "bucket-b")

        sync_user_dataset_iam(self.user, self.dataset)

        called_buckets = sorted(c.args[0] for c in mock_remove.call_args_list)
        self.assertEqual(called_buckets, ["bucket-a", "bucket-b"])
        mock_add.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_foreign_binding_survives_not_provisioned_verdict(self, mock_remove, mock_add):
        from core.iam import sync_user_dataset_iam
        tos = TOSDocument.objects.create(name="TOS", text="Terms", dataset=self.dataset)
        self.dataset.tos = tos
        self.dataset.save()
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)
        mock_remove.return_value = True
        self._own("bucket-a")

        sync_user_dataset_iam(self.user, self.dataset)

        called_buckets = sorted(c.args[0] for c in mock_remove.call_args_list)
        self.assertEqual(called_buckets, ["bucket-a"])
        mock_add.assert_not_called()
        self.assertFalse(BucketIAMBinding.objects.filter(bucket_name="bucket-a").exists())

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_skips_global_admin(self, mock_remove, mock_add):
        from core.iam import sync_user_dataset_iam
        admin = User.objects.create(email="admin@example.org", admin=True)
        mock_remove.return_value = True
        self._own("bucket-a", "bucket-b", email="admin@example.org")

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

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_version_grant_reaches_same_branch_ordinal_and_removes_complement(
        self, mock_remove, mock_add
    ):
        from core.iam import sync_user_dataset_iam
        Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            dataset_version=self.dv1,
            permission=self.view_perm,
        )
        mock_add.return_value = "created"
        mock_remove.return_value = True
        self._own("bucket-b")

        sync_user_dataset_iam(self.user, self.dataset)

        mock_add.assert_called_once_with("bucket-a", "user@example.org")
        mock_remove.assert_called_once_with("bucket-b", "user@example.org")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_ordinal_less_anchor_grant_reaches_only_anchor_buckets(
        self, mock_remove, mock_add
    ):
        from core.iam import sync_user_dataset_iam
        bucket_c = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-c")
        unranked = DatasetVersion.objects.create(
            dataset=self.dataset, version="unranked", branch="main"
        )
        unranked.buckets.add(bucket_c)
        Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            dataset_version=unranked,
            permission=self.view_perm,
        )
        mock_add.return_value = "created"
        mock_remove.return_value = True
        self._own("bucket-a", "bucket-b")

        sync_user_dataset_iam(self.user, self.dataset)

        mock_add.assert_called_once_with("bucket-c", "user@example.org")
        removed = {c.args[0] for c in mock_remove.call_args_list}
        self.assertEqual(removed, {"bucket-a", "bucket-b"})

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_explicit_buckets_override_service_filter(self, mock_remove, mock_add):
        from core.iam import sync_user_dataset_iam
        service = Service.objects.create(name="svc")
        grant = Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            service=service,
            permission=self.view_perm,
        )
        grant.buckets.add(self.bucket_b)
        mock_add.return_value = "created"
        mock_remove.return_value = True
        self._own("bucket-a")

        sync_user_dataset_iam(self.user, self.dataset)

        mock_add.assert_called_once_with("bucket-b", "user@example.org")
        mock_remove.assert_called_once_with("bucket-a", "user@example.org")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_service_scoped_and_named_role_grants_do_not_provision(
        self, mock_remove, mock_add
    ):
        from core.iam import permission_source_users, sync_user_dataset_iam
        service = Service.objects.create(name="svc")
        named_perm, _ = Permission.objects.get_or_create(name="annotation_editor")
        Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            service=service,
            permission=self.view_perm,
        )
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=named_perm)
        mock_remove.return_value = True
        self._own("bucket-a", "bucket-b")

        sync_user_dataset_iam(self.user, self.dataset)

        removed = {c.args[0] for c in mock_remove.call_args_list}
        self.assertEqual(removed, {"bucket-a", "bucket-b"})
        mock_add.assert_not_called()
        self.assertIn(self.user, list(permission_source_users(self.dataset)))

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_version_tos_blocks_only_all_blocked_anchor_buckets(self, mock_remove, mock_add):
        from core.iam import sync_user_dataset_iam
        loose_bucket = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-loose")
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)
        version_tos = TOSDocument.objects.create(
            name="v1 TOS", text="Terms", dataset_version=self.dv1
        )
        mock_add.return_value = "created"
        mock_remove.return_value = True
        self._own("bucket-a")

        sync_user_dataset_iam(self.user, self.dataset)

        added = {c.args[0] for c in mock_add.call_args_list}
        self.assertEqual(added, {"bucket-b", "bucket-loose"})
        mock_remove.assert_called_once_with("bucket-a", "user@example.org")

        mock_add.reset_mock()
        mock_remove.reset_mock()
        TOSAcceptance.objects.create(user=self.user, tos_document=version_tos)

        sync_user_dataset_iam(self.user, self.dataset)

        added = {c.args[0] for c in mock_add.call_args_list}
        self.assertEqual(added, {"bucket-a", "bucket-b", "bucket-loose"})
        mock_remove.assert_not_called()


@pytest.mark.django_db
class TestPermissionSourceUsers(TestCase):
    def setUp(self):
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.dataset = Dataset.objects.create(name="ds1")
        self.granted = User.objects.create(email="granted@example.org")
        Grant.objects.create(user=self.granted, dataset=self.dataset, permission=self.view_perm)
        self.group = Group.objects.create(name="lab")
        GroupDatasetPermission.objects.create(
            group=self.group, dataset=self.dataset, permission=self.view_perm,
        )
        self.member = User.objects.create(email="member@example.org")
        UserGroup.objects.create(user=self.member, group=self.group)

    def _emails(self):
        from core.iam import permission_source_users
        return {u.email for u in permission_source_users(self.dataset)}

    def test_includes_grant_holders_and_group_members(self):
        self.assertEqual(self._emails(), {"granted@example.org", "member@example.org"})

    def test_no_tos_gate(self):
        tos = TOSDocument.objects.create(name="TOS", text="Terms", dataset=self.dataset)
        self.dataset.tos = tos
        self.dataset.save()
        # Nobody accepted — enumeration must still include everyone
        self.assertEqual(self._emails(), {"granted@example.org", "member@example.org"})

    def test_no_admin_exclusion(self):
        admin = User.objects.create(email="admin@example.org", admin=True)
        Grant.objects.create(user=admin, dataset=self.dataset, permission=self.view_perm)
        self.assertIn("admin@example.org", self._emails())

    def test_excludes_unrelated_users(self):
        User.objects.create(email="other@example.org")
        self.assertNotIn("other@example.org", self._emails())

    def test_excludes_service_account_grants(self):
        from core.models import ServiceAccount, ServiceAccountGrant
        sa = ServiceAccount.objects.create(name="pipeline")
        ServiceAccountGrant.objects.create(
            service_account=sa, dataset=self.dataset, permission=self.view_perm,
        )
        self.assertNotIn(sa.email, self._emails())

    def test_other_dataset_sources_not_included(self):
        other_ds = Dataset.objects.create(name="ds2")
        other_user = User.objects.create(email="elsewhere@example.org")
        Grant.objects.create(user=other_user, dataset=other_ds, permission=self.view_perm)
        self.assertNotIn("elsewhere@example.org", self._emails())


@pytest.mark.django_db
class TestPermissionSourceDatasets(TestCase):
    def setUp(self):
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.user = User.objects.create(email="user@example.org")
        self.granted_ds = Dataset.objects.create(name="ds-granted")
        Grant.objects.create(user=self.user, dataset=self.granted_ds, permission=self.view_perm)
        self.group = Group.objects.create(name="lab")
        UserGroup.objects.create(user=self.user, group=self.group)
        self.group_ds = Dataset.objects.create(name="ds-group")
        GroupDatasetPermission.objects.create(
            group=self.group, dataset=self.group_ds, permission=self.view_perm,
        )

    def _names(self):
        from core.iam import permission_source_datasets
        return {ds.name for ds in permission_source_datasets(self.user)}

    def test_includes_granted_and_group_datasets(self):
        self.assertEqual(self._names(), {"ds-granted", "ds-group"})

    def test_no_rule_gate(self):
        # Disabled/TOS-pending users must still enumerate their datasets —
        # deprovisioning has to reach the stale IAM.
        tos = TOSDocument.objects.create(name="TOS", text="Terms", dataset=self.granted_ds)
        self.granted_ds.tos = tos
        self.granted_ds.save()
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self._names(), {"ds-granted", "ds-group"})

    def test_excludes_unrelated_datasets(self):
        Dataset.objects.create(name="ds-other")
        self.assertNotIn("ds-other", self._names())

    def test_excludes_other_users_sources(self):
        other = User.objects.create(email="other@example.org")
        other_ds = Dataset.objects.create(name="ds-elsewhere")
        Grant.objects.create(user=other, dataset=other_ds, permission=self.view_perm)
        self.assertNotIn("ds-elsewhere", self._names())


@pytest.mark.django_db
class TestSyncUserIAM(TestCase):
    def setUp(self):
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.user = User.objects.create(email="user@example.org")
        self.ds_a = Dataset.objects.create(name="ds-a")
        DatasetBucket.objects.create(dataset=self.ds_a, name="bucket-a")
        Grant.objects.create(user=self.user, dataset=self.ds_a, permission=self.view_perm)
        self.group = Group.objects.create(name="lab")
        UserGroup.objects.create(user=self.user, group=self.group)
        self.ds_b = Dataset.objects.create(name="ds-b")
        DatasetBucket.objects.create(dataset=self.ds_b, name="bucket-b")
        GroupDatasetPermission.objects.create(
            group=self.group, dataset=self.ds_b, permission=self.view_perm,
        )

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_enabled_user_provisioned_everywhere(self, mock_remove, mock_add):
        from core.iam import sync_user_iam
        mock_add.return_value = "created"

        sync_user_iam(self.user)

        added = {c.args[0] for c in mock_add.call_args_list}
        self.assertEqual(added, {"bucket-a", "bucket-b"})
        mock_remove.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_disabled_user_deprovisioned_everywhere(self, mock_remove, mock_add):
        from core.iam import sync_user_iam
        mock_remove.return_value = True
        BucketIAMBinding.objects.create(bucket_name="bucket-a", email="user@example.org")
        BucketIAMBinding.objects.create(bucket_name="bucket-b", email="user@example.org")
        self.user.is_active = False
        self.user.save()

        sync_user_iam(self.user)

        removed = {c.args[0] for c in mock_remove.call_args_list}
        self.assertEqual(removed, {"bucket-a", "bucket-b"})
        mock_add.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_disabled_parent_deprovisions_service_account(self, mock_remove, mock_add):
        from core.iam import sync_user_iam
        mock_remove.return_value = True
        sa = User.objects.create(email="sa@example.org", parent=self.user)
        Grant.objects.create(user=sa, dataset=self.ds_a, permission=self.view_perm)
        BucketIAMBinding.objects.create(bucket_name="bucket-a", email="sa@example.org")
        self.user.is_active = False
        self.user.save()
        sa.refresh_from_db()

        sync_user_iam(sa)

        removed = {(c.args[0], c.args[1]) for c in mock_remove.call_args_list}
        self.assertEqual(removed, {("bucket-a", "sa@example.org")})
        mock_add.assert_not_called()


@pytest.mark.django_db
class TestSyncDatasetIAM(TestCase):
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
        self.admin = User.objects.create(email="admin@example.org", admin=True)
        Grant.objects.create(user=self.admin, dataset=self.dataset, permission=self.view_perm)

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_rule_decides_direction_per_user(self, mock_remove, mock_add):
        from core.iam import sync_dataset_iam
        mock_add.return_value = "created"
        mock_remove.return_value = True
        BucketIAMBinding.objects.create(bucket_name="bucket-a", email="pending@example.org")
        BucketIAMBinding.objects.create(bucket_name="bucket-a", email="admin@example.org")

        sync_dataset_iam(self.dataset)

        added = {c.args[1] for c in mock_add.call_args_list}
        removed = {c.args[1] for c in mock_remove.call_args_list}
        # Effective user added; TOS-pending user and admin removed, never added
        self.assertEqual(added, {"accepted@example.org"})
        self.assertEqual(removed, {"pending@example.org", "admin@example.org"})

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_explicit_users_override_enumeration(self, mock_remove, mock_add):
        from core.iam import sync_dataset_iam
        mock_remove.return_value = True
        # A user who just lost their only permission source is no longer
        # enumerated — pass the pre-mutation capture explicitly.
        Grant.objects.filter(user=self.pending).delete()
        BucketIAMBinding.objects.create(bucket_name="bucket-a", email="pending@example.org")
        sync_dataset_iam(self.dataset, users=[self.pending])

        removed = {c.args[1] for c in mock_remove.call_args_list}
        self.assertEqual(removed, {"pending@example.org"})
        mock_add.assert_not_called()


@pytest.mark.django_db
class TestDeprovisionBucket(TestCase):
    def setUp(self):
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.dataset = Dataset.objects.create(name="ds1")
        self.tos = TOSDocument.objects.create(name="TOS", text="Terms", dataset=self.dataset)
        self.dataset.tos = self.tos
        self.dataset.save()
        self.accepted = User.objects.create(email="accepted@example.org")
        Grant.objects.create(user=self.accepted, dataset=self.dataset, permission=self.view_perm)
        TOSAcceptance.objects.create(user=self.accepted, tos_document=self.tos)
        self.pending = User.objects.create(email="pending@example.org")
        Grant.objects.create(user=self.pending, dataset=self.dataset, permission=self.view_perm)

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_removes_all_ledger_owned_bindings(self, mock_remove, mock_add):
        from core.iam import deprovision_bucket
        mock_remove.return_value = True
        BucketIAMBinding.objects.create(
            bucket_name="old-bucket", email="accepted@example.org",
        )
        BucketIAMBinding.objects.create(
            bucket_name="old-bucket", email="orphan@example.org",
        )

        # Bucket name deliberately absent from the DB — deprovision targets
        # detached/old names the sync helpers can no longer see.
        deprovision_bucket("old-bucket", self.dataset)

        calls = {(c.args[0], c.args[1]) for c in mock_remove.call_args_list}
        self.assertEqual(calls, {
            ("old-bucket", "accepted@example.org"),
            ("old-bucket", "orphan@example.org"),
        })
        mock_add.assert_not_called()
        self.assertFalse(BucketIAMBinding.objects.filter(bucket_name="old-bucket").exists())

    @patch("ngauth.gcs.remove_user_from_bucket", side_effect=Exception("GCS error"))
    def test_best_effort_does_not_raise(self, mock_remove):
        from core.iam import deprovision_bucket
        BucketIAMBinding.objects.create(
            bucket_name="old-bucket", email="accepted@example.org",
        )
        BucketIAMBinding.objects.create(
            bucket_name="old-bucket", email="pending@example.org",
        )
        deprovision_bucket("old-bucket", self.dataset)
        self.assertEqual(mock_remove.call_count, 2)
        self.assertEqual(BucketIAMBinding.objects.filter(bucket_name="old-bucket").count(), 2)


@pytest.mark.django_db
class TestSyncGroupDatasetsForUser(TestCase):
    def setUp(self):
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.user = User.objects.create(email="user@example.org", name="User")
        self.group = Group.objects.create(name="lab")
        self.dataset = Dataset.objects.create(name="ds1")
        DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        DatasetVersion.objects.create(
            dataset=self.dataset, version="v1",
        )
        GroupDatasetPermission.objects.create(
            group=self.group, dataset=self.dataset, permission=self.view_perm,
        )

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_syncs_all_group_datasets(self, mock_remove, mock_add):
        from core.iam import sync_group_datasets_for_user
        UserGroup.objects.create(user=self.user, group=self.group)
        mock_add.return_value = "created"

        sync_group_datasets_for_user(self.user, self.group)

        mock_add.assert_called_once_with("bucket-a", "user@example.org")
