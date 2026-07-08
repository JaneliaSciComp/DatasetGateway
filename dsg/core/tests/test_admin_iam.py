"""Tests for the Django-admin IAM hooks in core/admin.py.

Each test drives the ModelAdmin methods the way the admin views do
(get_form → is_valid → save(commit=False) → save_model, etc.) with
ngauth.gcs patched, and asserts on the mock calls — ngauth/gcs.py swallows
errors, so an unmocked call would fail silently rather than loudly.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib import admin as django_admin
from django.forms.models import inlineformset_factory
from django.test import RequestFactory, TestCase

from core.admin import (
    DatasetBucketAdmin,
    DatasetModelAdmin,
    DatasetVersionAdmin,
    GrantAdmin,
    GroupAdmin,
    GroupDatasetPermissionAdmin,
    TOSAcceptanceAdmin,
    TOSDocumentAdmin,
    UserAdmin,
)
from core.models import (
    AuditLog,
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


class _AdminTestBase(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create(email="siteadmin@example.org", admin=True)
        self.request = RequestFactory().post("/admin/")
        self.request.user = self.admin_user
        self.view_perm, _ = Permission.objects.get_or_create(name="view")

    def _save_via_admin(self, model_admin, data, instance=None):
        """Mimic the admin add/change view: form → save_model. Returns obj."""
        change = instance is not None
        FormClass = model_admin.get_form(self.request, obj=instance, change=change)
        form = FormClass(data=data, instance=instance)
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)
        model_admin.save_model(self.request, obj, form, change)
        return obj

    def _own(self, bucket_name, email):
        return BucketIAMBinding.objects.create(bucket_name=bucket_name, email=email)


@pytest.mark.django_db
class TestGrantAdminIAM(_AdminTestBase):
    def setUp(self):
        super().setUp()
        self.ma = GrantAdmin(Grant, django_admin.site)
        self.dataset = Dataset.objects.create(name="ds1")
        self.bucket_a = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        self.user = User.objects.create(email="user@example.org")
        self.service = Service.objects.create(name="svc")

    def _save_grant_m2m_via_admin(self, grant, bucket_ids, mock_remove=None, mock_add=None):
        FormClass = self.ma.get_form(self.request, obj=grant, change=True)
        form = FormClass(data={
            "user": grant.user_id,
            "dataset": grant.dataset_id,
            "dataset_version": grant.dataset_version_id or "",
            "service": grant.service_id or "",
            "permission": grant.permission_id,
            "group": grant.group_id or "",
            "granted_by": grant.granted_by_id or "",
            "source": grant.source,
            "buckets": [str(pk) for pk in bucket_ids],
        }, instance=grant)
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)
        self.ma.save_model(self.request, obj, form, change=True)
        if mock_remove:
            mock_remove.reset_mock()
        if mock_add:
            mock_add.reset_mock()
        self.ma.save_related(self.request, form, [], change=True)
        return obj

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_create_provisions(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        self._save_via_admin(self.ma, {
            "user": self.user.pk, "dataset": self.dataset.pk,
            "permission": self.view_perm.pk, "source": "manual",
        })
        mock_add.assert_called_once_with("bucket-a", "user@example.org")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_retarget_user_deprovisions_old_pair(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        other = User.objects.create(email="other@example.org")
        grant = Grant.objects.create(
            user=self.user, dataset=self.dataset, permission=self.view_perm,
        )
        self._own("bucket-a", "user@example.org")
        self._save_via_admin(self.ma, {
            "user": other.pk, "dataset": self.dataset.pk,
            "permission": self.view_perm.pk, "source": "manual",
        }, instance=grant)
        mock_remove.assert_called_once_with("bucket-a", "user@example.org")
        mock_add.assert_called_once_with("bucket-a", "other@example.org")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_retarget_dataset_deprovisions_old_pair(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        other_ds = Dataset.objects.create(name="ds2")
        DatasetBucket.objects.create(dataset=other_ds, name="bucket-b")
        grant = Grant.objects.create(
            user=self.user, dataset=self.dataset, permission=self.view_perm,
        )
        self._own("bucket-a", "user@example.org")
        self._save_via_admin(self.ma, {
            "user": self.user.pk, "dataset": other_ds.pk,
            "permission": self.view_perm.pk, "source": "manual",
        }, instance=grant)
        mock_remove.assert_called_once_with("bucket-a", "user@example.org")
        mock_add.assert_called_once_with("bucket-b", "user@example.org")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_delete_deprovisions(self, mock_remove, mock_add):
        mock_remove.return_value = True
        grant = Grant.objects.create(
            user=self.user, dataset=self.dataset, permission=self.view_perm,
        )
        self._own("bucket-a", "user@example.org")
        self.ma.delete_model(self.request, grant)
        mock_remove.assert_called_once_with("bucket-a", "user@example.org")
        mock_add.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_bucket_attach_resyncs_after_m2m_save(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        grant = Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            service=self.service,
            permission=self.view_perm,
        )
        bucket_b = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-b")
        self._own("bucket-a", "user@example.org")

        self._save_grant_m2m_via_admin(
            grant, [bucket_b.pk], mock_add=mock_add
        )

        mock_add.assert_called_once_with("bucket-b", "user@example.org")
        mock_remove.assert_called_once_with("bucket-a", "user@example.org")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_bucket_change_resyncs_after_m2m_save(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        grant = Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            service=self.service,
            permission=self.view_perm,
        )
        bucket_b = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-b")
        grant.buckets.add(bucket_b)
        self._own("bucket-b", "user@example.org")

        self._save_grant_m2m_via_admin(
            grant, [self.bucket_a.pk], mock_remove=mock_remove, mock_add=mock_add
        )

        mock_add.assert_called_once_with("bucket-a", "user@example.org")
        mock_remove.assert_called_once_with("bucket-b", "user@example.org")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_bucket_clear_resyncs_after_m2m_save(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        grant = Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            service=self.service,
            permission=self.view_perm,
        )
        bucket_b = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-b")
        grant.buckets.add(bucket_b)
        self._own("bucket-a", "user@example.org")
        self._own("bucket-b", "user@example.org")

        self._save_grant_m2m_via_admin(
            grant, [], mock_add=mock_add
        )

        removed = {c.args[0] for c in mock_remove.call_args_list}
        self.assertEqual(removed, {"bucket-a", "bucket-b"})
        mock_add.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_bulk_delete_deprovisions_each(self, mock_remove, mock_add):
        mock_remove.return_value = True
        other = User.objects.create(email="other@example.org")
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)
        Grant.objects.create(user=other, dataset=self.dataset, permission=self.view_perm)
        self._own("bucket-a", "user@example.org")
        self._own("bucket-a", "other@example.org")
        self.ma.delete_queryset(self.request, Grant.objects.all())
        self.assertEqual(Grant.objects.count(), 0)
        removed = {c.args[1] for c in mock_remove.call_args_list}
        self.assertEqual(removed, {"user@example.org", "other@example.org"})
        # The per-object path also audits, unlike stock bulk delete
        self.assertEqual(AuditLog.objects.filter(action="grant_deleted").count(), 2)


@pytest.mark.django_db
class TestGroupDatasetPermissionAdminIAM(_AdminTestBase):
    def setUp(self):
        super().setUp()
        self.ma = GroupDatasetPermissionAdmin(GroupDatasetPermission, django_admin.site)
        self.dataset = Dataset.objects.create(name="ds1")
        DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        self.group = Group.objects.create(name="lab")
        self.member = User.objects.create(email="member@example.org")
        UserGroup.objects.create(user=self.member, group=self.group)

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_create_provisions_members(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        self._save_via_admin(self.ma, {
            "group": self.group.pk, "dataset": self.dataset.pk,
            "permission": self.view_perm.pk,
        })
        mock_add.assert_called_once_with("bucket-a", "member@example.org")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_delete_deprovisions_members(self, mock_remove, mock_add):
        mock_remove.return_value = True
        gdp = GroupDatasetPermission.objects.create(
            group=self.group, dataset=self.dataset, permission=self.view_perm,
        )
        self._own("bucket-a", "member@example.org")
        self.ma.delete_model(self.request, gdp)
        # The member left the enumeration with the row — pre-capture must
        # still reach them.
        mock_remove.assert_called_once_with("bucket-a", "member@example.org")
        mock_add.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_group_swap_converges_both_groups_members(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        other_group = Group.objects.create(name="other-lab")
        other_member = User.objects.create(email="othermember@example.org")
        UserGroup.objects.create(user=other_member, group=other_group)
        gdp = GroupDatasetPermission.objects.create(
            group=self.group, dataset=self.dataset, permission=self.view_perm,
        )
        self._own("bucket-a", "member@example.org")
        self._save_via_admin(self.ma, {
            "group": other_group.pk, "dataset": self.dataset.pk,
            "permission": self.view_perm.pk,
        }, instance=gdp)
        removed = {c.args[1] for c in mock_remove.call_args_list}
        added = {c.args[1] for c in mock_add.call_args_list}
        self.assertIn("member@example.org", removed)
        self.assertIn("othermember@example.org", added)

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_dataset_swap_converges_both_datasets(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        other_ds = Dataset.objects.create(name="ds2")
        DatasetBucket.objects.create(dataset=other_ds, name="bucket-b")
        gdp = GroupDatasetPermission.objects.create(
            group=self.group, dataset=self.dataset, permission=self.view_perm,
        )
        self._own("bucket-a", "member@example.org")
        self._save_via_admin(self.ma, {
            "group": self.group.pk, "dataset": other_ds.pk,
            "permission": self.view_perm.pk,
        }, instance=gdp)
        self.assertIn(
            ("bucket-a", "member@example.org"),
            {(c.args[0], c.args[1]) for c in mock_remove.call_args_list},
        )
        self.assertIn(
            ("bucket-b", "member@example.org"),
            {(c.args[0], c.args[1]) for c in mock_add.call_args_list},
        )


@pytest.mark.django_db
class TestTOSAcceptanceAdminIAM(_AdminTestBase):
    def setUp(self):
        super().setUp()
        self.ma = TOSAcceptanceAdmin(TOSAcceptance, django_admin.site)
        self.dataset = Dataset.objects.create(name="ds1")
        DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        self.tos = TOSDocument.objects.create(name="TOS", text="Terms", dataset=self.dataset)
        self.dataset.tos = self.tos
        self.dataset.save()
        self.user = User.objects.create(email="user@example.org")
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_create_provisions(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        self._save_via_admin(self.ma, {
            "user": self.user.pk, "tos_document": self.tos.pk,
        })
        mock_add.assert_called_once_with("bucket-a", "user@example.org")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_delete_deprovisions(self, mock_remove, mock_add):
        mock_remove.return_value = True
        acceptance = TOSAcceptance.objects.create(user=self.user, tos_document=self.tos)
        self._own("bucket-a", "user@example.org")
        self.ma.delete_model(self.request, acceptance)
        mock_remove.assert_called_once_with("bucket-a", "user@example.org")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_retarget_user_converges_old_pair(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        other = User.objects.create(email="other@example.org")
        Grant.objects.create(user=other, dataset=self.dataset, permission=self.view_perm)
        acceptance = TOSAcceptance.objects.create(user=self.user, tos_document=self.tos)
        self._own("bucket-a", "user@example.org")
        self._save_via_admin(self.ma, {
            "user": other.pk, "tos_document": self.tos.pk,
        }, instance=acceptance)
        # Old user no longer holds an acceptance → deprovision; new user does
        mock_remove.assert_called_once_with("bucket-a", "user@example.org")
        mock_add.assert_called_once_with("bucket-a", "other@example.org")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_bulk_delete_deprovisions_each(self, mock_remove, mock_add):
        mock_remove.return_value = True
        other = User.objects.create(email="other@example.org")
        Grant.objects.create(user=other, dataset=self.dataset, permission=self.view_perm)
        TOSAcceptance.objects.create(user=self.user, tos_document=self.tos)
        TOSAcceptance.objects.create(user=other, tos_document=self.tos)
        self._own("bucket-a", "user@example.org")
        self._own("bucket-a", "other@example.org")
        self.ma.delete_queryset(self.request, TOSAcceptance.objects.all())
        self.assertEqual(TOSAcceptance.objects.count(), 0)
        removed = {c.args[1] for c in mock_remove.call_args_list}
        self.assertEqual(removed, {"user@example.org", "other@example.org"})


@pytest.mark.django_db
class TestDatasetBucketAdminIAM(_AdminTestBase):
    def setUp(self):
        super().setUp()
        self.ma = DatasetBucketAdmin(DatasetBucket, django_admin.site)
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
    def test_create_provisions_effective_users(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        self._save_via_admin(self.ma, {
            "dataset": self.dataset.pk, "name": "bucket-new",
        })
        added = {(c.args[0], c.args[1]) for c in mock_add.call_args_list}
        self.assertEqual(added, {("bucket-new", "accepted@example.org")})
        mock_remove.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_rename_deprovisions_old_name(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        bucket = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-old")
        self._own("bucket-old", "accepted@example.org")
        self._own("bucket-old", "pending@example.org")
        self._save_via_admin(self.ma, {
            "dataset": self.dataset.pk, "name": "bucket-renamed",
        }, instance=bucket)
        removed_old = {c.args[1] for c in mock_remove.call_args_list if c.args[0] == "bucket-old"}
        # All permission-source users leave the old name, including TOS-pending
        self.assertEqual(removed_old, {"accepted@example.org", "pending@example.org"})
        self.assertIn(
            ("bucket-renamed", "accepted@example.org"),
            {(c.args[0], c.args[1]) for c in mock_add.call_args_list},
        )

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_move_to_other_dataset_deprovisions_old_users(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        other_ds = Dataset.objects.create(name="ds2")
        other_user = User.objects.create(email="otheruser@example.org")
        Grant.objects.create(user=other_user, dataset=other_ds, permission=self.view_perm)
        bucket = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        self._own("bucket-a", "accepted@example.org")
        self._own("bucket-a", "pending@example.org")
        self._save_via_admin(self.ma, {
            "dataset": other_ds.pk, "name": "bucket-a",
        }, instance=bucket)
        removed = {(c.args[0], c.args[1]) for c in mock_remove.call_args_list}
        # Old dataset's permission-source users leave the moved bucket
        self.assertIn(("bucket-a", "accepted@example.org"), removed)
        self.assertIn(("bucket-a", "pending@example.org"), removed)
        # New dataset's effective users get it
        self.assertIn(
            ("bucket-a", "otheruser@example.org"),
            {(c.args[0], c.args[1]) for c in mock_add.call_args_list},
        )

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_delete_deprovisions_old_name(self, mock_remove, mock_add):
        mock_remove.return_value = True
        bucket = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        self._own("bucket-a", "accepted@example.org")
        self._own("bucket-a", "pending@example.org")
        self.ma.delete_model(self.request, bucket)
        removed = {(c.args[0], c.args[1]) for c in mock_remove.call_args_list}
        self.assertEqual(removed, {
            ("bucket-a", "accepted@example.org"),
            ("bucket-a", "pending@example.org"),
        })
        mock_add.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_bulk_delete_deprovisions_each_bucket(self, mock_remove, mock_add):
        mock_remove.return_value = True
        DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        DatasetBucket.objects.create(dataset=self.dataset, name="bucket-b")
        self._own("bucket-a", "accepted@example.org")
        self._own("bucket-b", "accepted@example.org")
        self.ma.delete_queryset(self.request, DatasetBucket.objects.all())
        self.assertEqual(DatasetBucket.objects.count(), 0)
        removed_buckets = {c.args[0] for c in mock_remove.call_args_list}
        self.assertEqual(removed_buckets, {"bucket-a", "bucket-b"})


@pytest.mark.django_db
class TestDatasetModelAdminIAM(_AdminTestBase):
    def setUp(self):
        super().setUp()
        self.ma = DatasetModelAdmin(Dataset, django_admin.site)
        self.dataset = Dataset.objects.create(name="ds1")
        DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        self.user = User.objects.create(email="user@example.org")
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)
        self.tos = TOSDocument.objects.create(name="TOS", text="Terms", dataset=self.dataset)

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_setting_tos_resyncs_dataset(self, mock_remove, mock_add):
        mock_remove.return_value = True
        self._own("bucket-a", "user@example.org")
        self._save_via_admin(self.ma, {
            "name": "ds1", "description": "", "access_mode": Dataset.ACCESS_CLOSED,
            "tos": self.tos.pk,
        }, instance=self.dataset)
        # User has not accepted the newly-required TOS → deprovision
        mock_remove.assert_called_once_with("bucket-a", "user@example.org")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_clearing_tos_resyncs_dataset(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        self.dataset.tos = self.tos
        self.dataset.save()
        self._save_via_admin(self.ma, {
            "name": "ds1", "description": "", "access_mode": Dataset.ACCESS_CLOSED,
            "tos": "",
        }, instance=self.dataset)
        # TOS gate removed → grant-holder becomes effective
        mock_add.assert_called_once_with("bucket-a", "user@example.org")

    def _bucket_formset(self, data):
        FormSet = inlineformset_factory(
            Dataset, DatasetBucket, fields=["name"], extra=1, can_delete=True,
        )
        formset = FormSet(data=data, instance=self.dataset, prefix="buckets")
        self.assertTrue(formset.is_valid(), formset.errors)
        return formset

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_inline_add_provisions(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        formset = self._bucket_formset({
            "buckets-TOTAL_FORMS": "1", "buckets-INITIAL_FORMS": "0",
            "buckets-MIN_NUM_FORMS": "0", "buckets-MAX_NUM_FORMS": "1000",
            "buckets-0-name": "bucket-new",
        })
        self.ma.save_formset(
            self.request, SimpleNamespace(instance=self.dataset), formset, change=True,
        )
        self.assertIn(
            ("bucket-new", "user@example.org"),
            {(c.args[0], c.args[1]) for c in mock_add.call_args_list},
        )

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_inline_rename_deprovisions_old_name(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        bucket = DatasetBucket.objects.get(name="bucket-a")
        self._own("bucket-a", "user@example.org")
        formset = self._bucket_formset({
            "buckets-TOTAL_FORMS": "1", "buckets-INITIAL_FORMS": "1",
            "buckets-MIN_NUM_FORMS": "0", "buckets-MAX_NUM_FORMS": "1000",
            "buckets-0-id": str(bucket.pk),
            "buckets-0-name": "bucket-renamed",
        })
        self.ma.save_formset(
            self.request, SimpleNamespace(instance=self.dataset), formset, change=True,
        )
        self.assertIn(
            ("bucket-a", "user@example.org"),
            {(c.args[0], c.args[1]) for c in mock_remove.call_args_list},
        )
        self.assertIn(
            ("bucket-renamed", "user@example.org"),
            {(c.args[0], c.args[1]) for c in mock_add.call_args_list},
        )

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_inline_delete_deprovisions(self, mock_remove, mock_add):
        mock_remove.return_value = True
        bucket = DatasetBucket.objects.get(name="bucket-a")
        self._own("bucket-a", "user@example.org")
        formset = self._bucket_formset({
            "buckets-TOTAL_FORMS": "1", "buckets-INITIAL_FORMS": "1",
            "buckets-MIN_NUM_FORMS": "0", "buckets-MAX_NUM_FORMS": "1000",
            "buckets-0-id": str(bucket.pk),
            "buckets-0-name": "bucket-a",
            "buckets-0-DELETE": "on",
        })
        self.ma.save_formset(
            self.request, SimpleNamespace(instance=self.dataset), formset, change=True,
        )
        mock_remove.assert_called_once_with("bucket-a", "user@example.org")
        mock_add.assert_not_called()


@pytest.mark.django_db
class TestDatasetVersionAdminIAM(_AdminTestBase):
    def setUp(self):
        super().setUp()
        self.ma = DatasetVersionAdmin(DatasetVersion, django_admin.site)
        self.dataset = Dataset.objects.create(name="ds1")
        self.bucket_a = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        self.bucket_b = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-b")
        self.version = DatasetVersion.objects.create(dataset=self.dataset, version="v1")
        self.version.buckets.add(self.bucket_a)
        self.user = User.objects.create(email="user@example.org")
        Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            dataset_version=self.version,
            permission=self.view_perm,
        )

    def _save_version_buckets_via_admin(self, bucket_ids, mock_remove=None, mock_add=None):
        FormClass = self.ma.get_form(self.request, obj=self.version, change=True)
        form = FormClass(data={
            "dataset": self.dataset.pk,
            "version": self.version.version,
            "branch": self.version.branch,
            "ordinal": "",
            "prefix": self.version.prefix,
            "buckets": [str(pk) for pk in bucket_ids],
        }, instance=self.version)
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)
        self.ma.save_model(self.request, obj, form, change=True)
        if mock_remove:
            mock_remove.reset_mock()
        if mock_add:
            mock_add.reset_mock()
        self.ma.save_related(self.request, form, [], change=True)

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_bucket_change_resyncs_dataset_after_m2m_save(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        self._own("bucket-a", "user@example.org")

        self._save_version_buckets_via_admin(
            [self.bucket_b.pk], mock_remove=mock_remove, mock_add=mock_add
        )

        mock_add.assert_called_once_with("bucket-b", "user@example.org")
        mock_remove.assert_called_once_with("bucket-a", "user@example.org")


@pytest.mark.django_db
class TestTOSDocumentAdminIAM(_AdminTestBase):
    def setUp(self):
        super().setUp()
        self.ma = TOSDocumentAdmin(TOSDocument, django_admin.site)
        self.dataset = Dataset.objects.create(name="ds1")
        DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        self.user = User.objects.create(email="user@example.org")
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_autoset_flip_resyncs_dataset(self, mock_remove, mock_add):
        mock_remove.return_value = True
        self._own("bucket-a", "user@example.org")
        # New general TOS doc auto-sets Dataset.tos → user is now TOS-pending
        self._save_via_admin(self.ma, {
            "name": "TOS", "text": "Terms", "dataset": self.dataset.pk,
            "invite_token": "tok-autoset-1",
            "effective_date_0": "2026-01-01", "effective_date_1": "00:00:00",
        })
        self.dataset.refresh_from_db()
        self.assertIsNotNone(self.dataset.tos)
        mock_remove.assert_called_once_with("bucket-a", "user@example.org")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_move_between_datasets_resyncs_both(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        tos = TOSDocument.objects.create(
            name="TOS", text="Terms", dataset=self.dataset, invite_token="tok-move-1",
        )
        self.dataset.tos = tos
        self.dataset.save()
        other_ds = Dataset.objects.create(name="ds2")
        DatasetBucket.objects.create(dataset=other_ds, name="bucket-b")
        other_user = User.objects.create(email="otheruser@example.org")
        Grant.objects.create(user=other_user, dataset=other_ds, permission=self.view_perm)
        self._own("bucket-b", "otheruser@example.org")

        self._save_via_admin(self.ma, {
            "name": "TOS", "text": "Terms", "dataset": other_ds.pk,
            "invite_token": "tok-move-1",
            "effective_date_0": "2026-01-01", "effective_date_1": "00:00:00",
        }, instance=tos)

        self.dataset.refresh_from_db()
        self.assertIsNone(self.dataset.tos_id)
        # New dataset picked up the gate via auto-set; its user hasn't accepted
        other_ds.refresh_from_db()
        self.assertEqual(other_ds.tos_id, tos.pk)
        self.assertIn(
            ("bucket-b", "otheruser@example.org"),
            {(c.args[0], c.args[1]) for c in mock_remove.call_args_list},
        )
        # Old dataset resynced after the stale gate was cleared, so its user
        # provisions despite never accepting the moved document.
        self.assertIn(
            ("bucket-a", "user@example.org"),
            {(c.args[0], c.args[1]) for c in mock_add.call_args_list},
        )

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_version_tos_retarget_resyncs_dataset(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        bucket_b = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-b")
        v1 = DatasetVersion.objects.create(dataset=self.dataset, version="v1")
        v1.buckets.add(DatasetBucket.objects.get(dataset=self.dataset, name="bucket-a"))
        v2 = DatasetVersion.objects.create(dataset=self.dataset, version="v2")
        v2.buckets.add(bucket_b)
        tos = TOSDocument.objects.create(
            name="Version TOS",
            text="Terms",
            dataset_version=v1,
            invite_token="tok-version-retarget",
        )
        self._own("bucket-b", "user@example.org")

        self._save_via_admin(self.ma, {
            "name": "Version TOS",
            "text": "Terms",
            "dataset": "",
            "dataset_version": v2.pk,
            "service": "",
            "invite_token": "tok-version-retarget",
            "effective_date_0": "2026-01-01",
            "effective_date_1": "00:00:00",
        }, instance=tos)

        self.assertIn(
            ("bucket-a", "user@example.org"),
            {(c.args[0], c.args[1]) for c in mock_add.call_args_list},
        )
        self.assertIn(
            ("bucket-b", "user@example.org"),
            {(c.args[0], c.args[1]) for c in mock_remove.call_args_list},
        )

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_version_tos_retired_date_edit_resyncs_dataset(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        bucket_b = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-b")
        v1 = DatasetVersion.objects.create(dataset=self.dataset, version="v1")
        v1.buckets.add(DatasetBucket.objects.get(dataset=self.dataset, name="bucket-a"))
        v2 = DatasetVersion.objects.create(dataset=self.dataset, version="v2")
        v2.buckets.add(bucket_b)
        tos = TOSDocument.objects.create(
            name="Version TOS",
            text="Terms",
            dataset_version=v1,
            invite_token="tok-version-retire",
        )

        self._save_via_admin(self.ma, {
            "name": "Version TOS",
            "text": "Terms",
            "dataset": "",
            "dataset_version": v1.pk,
            "service": "",
            "invite_token": "tok-version-retire",
            "effective_date_0": "2026-01-01",
            "effective_date_1": "00:00:00",
            "retired_date_0": "2026-01-02",
            "retired_date_1": "00:00:00",
        }, instance=tos)

        added = {c.args[0] for c in mock_add.call_args_list}
        self.assertEqual(added, {"bucket-a", "bucket-b"})
        mock_remove.assert_not_called()


@pytest.mark.django_db
class TestUserFlagAdminIAM(_AdminTestBase):
    """is_active/admin flips via UserAdmin.save_model fan out per-user IAM,
    including the user's user-type service accounts."""

    def setUp(self):
        super().setUp()
        self.ma = UserAdmin(User, django_admin.site)
        self.ds_a = Dataset.objects.create(name="ds-a")
        DatasetBucket.objects.create(dataset=self.ds_a, name="bucket-a")
        self.user = User.objects.create(email="user@example.org")
        Grant.objects.create(user=self.user, dataset=self.ds_a, permission=self.view_perm)
        self.ds_b = Dataset.objects.create(name="ds-b")
        DatasetBucket.objects.create(dataset=self.ds_b, name="bucket-b")
        self.sa_user = User.objects.create(email="robot@example.org", parent=self.user)
        Grant.objects.create(user=self.sa_user, dataset=self.ds_b, permission=self.view_perm)

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_disable_removes_user_and_sa_iam(self, mock_remove, mock_add):
        mock_remove.return_value = True
        self._own("bucket-a", "user@example.org")
        self._own("bucket-b", "robot@example.org")
        # is_active checkbox omitted → False
        self._save_via_admin(self.ma, {
            "email": self.user.email, "name": self.user.name,
        }, instance=self.user)
        removed = {(c.args[0], c.args[1]) for c in mock_remove.call_args_list}
        self.assertEqual(removed, {
            ("bucket-a", "user@example.org"),
            ("bucket-b", "robot@example.org"),
        })
        mock_add.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_reenable_readds_user_and_sa_iam(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        self.user.is_active = False
        self.user.save()
        self._save_via_admin(self.ma, {
            "email": self.user.email, "name": self.user.name, "is_active": "on",
        }, instance=self.user)
        added = {(c.args[0], c.args[1]) for c in mock_add.call_args_list}
        self.assertEqual(added, {
            ("bucket-a", "user@example.org"),
            ("bucket-b", "robot@example.org"),
        })
        mock_remove.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_promote_to_admin_removes_per_user_iam(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        self._own("bucket-a", "user@example.org")
        self._save_via_admin(self.ma, {
            "email": self.user.email, "name": self.user.name,
            "is_active": "on", "admin": "on",
        }, instance=self.user)
        # Admins are never provisioned per-user; the SA resync is a no-op add
        removed = {(c.args[0], c.args[1]) for c in mock_remove.call_args_list}
        self.assertEqual(removed, {("bucket-a", "user@example.org")})

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_demote_from_admin_readds_per_user_iam(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        self.user.admin = True
        self.user.save()
        self._save_via_admin(self.ma, {
            "email": self.user.email, "name": self.user.name, "is_active": "on",
        }, instance=self.user)  # admin checkbox omitted → False
        self.assertIn(
            ("bucket-a", "user@example.org"),
            {(c.args[0], c.args[1]) for c in mock_add.call_args_list},
        )
        mock_remove.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_unrelated_change_does_not_sync(self, mock_remove, mock_add):
        self._save_via_admin(self.ma, {
            "email": self.user.email, "name": "New Name", "is_active": "on",
        }, instance=self.user)
        mock_add.assert_not_called()
        mock_remove.assert_not_called()


@pytest.mark.django_db
class TestMembershipAdminIAM(_AdminTestBase):
    """User↔group membership changes via UserAdmin/GroupAdmin save_related."""

    def setUp(self):
        super().setUp()
        self.group_a = Group.objects.create(name="lab-a")
        self.group_b = Group.objects.create(name="lab-b")
        self.ds_a = Dataset.objects.create(name="ds-a")
        self.ds_b = Dataset.objects.create(name="ds-b")
        DatasetBucket.objects.create(dataset=self.ds_a, name="bucket-a")
        DatasetBucket.objects.create(dataset=self.ds_b, name="bucket-b")
        GroupDatasetPermission.objects.create(
            group=self.group_a, dataset=self.ds_a, permission=self.view_perm,
        )
        GroupDatasetPermission.objects.create(
            group=self.group_b, dataset=self.ds_b, permission=self.view_perm,
        )
        self.user = User.objects.create(email="user@example.org")

    def _user_admin_form(self, user):
        ma = UserAdmin(User, django_admin.site)
        FormClass = ma.get_form(self.request, obj=user, change=True)
        form = FormClass(data={
            "email": user.email, "name": user.name, "is_active": "on",
        }, instance=user)
        self.assertTrue(form.is_valid(), form.errors)
        form.save(commit=False)
        return ma, form

    def _membership_formset(self, parent_model, fk_name, data, instance):
        FormSet = inlineformset_factory(
            parent_model, UserGroup, fields=[fk_name, "is_admin"],
            extra=0, can_delete=True,
        )
        formset = FormSet(data=data, instance=instance, prefix="ug")
        self.assertTrue(formset.is_valid(), formset.errors)
        return formset

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_user_admin_changed_membership_row_converges_both_groups(
            self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        ug = UserGroup.objects.create(user=self.user, group=self.group_a)
        self._own("bucket-a", "user@example.org")
        ma, form = self._user_admin_form(self.user)
        formset = self._membership_formset(User, "group", {
            "ug-TOTAL_FORMS": "1", "ug-INITIAL_FORMS": "1",
            "ug-MIN_NUM_FORMS": "0", "ug-MAX_NUM_FORMS": "1000",
            "ug-0-id": str(ug.pk),
            "ug-0-group": str(self.group_b.pk),  # row changed, not added/removed
        }, self.user)

        ma.save_related(self.request, form, [formset], change=True)

        # Left group-a's dataset, joined group-b's
        mock_remove.assert_called_once_with("bucket-a", "user@example.org")
        mock_add.assert_called_once_with("bucket-b", "user@example.org")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_group_admin_changed_membership_row_converges_both_users(
            self, mock_remove, mock_add):
        mock_add.return_value = "created"
        mock_remove.return_value = True
        other = User.objects.create(email="other@example.org")
        ug = UserGroup.objects.create(user=self.user, group=self.group_a)
        self._own("bucket-a", "user@example.org")
        ma = GroupAdmin(Group, django_admin.site)
        FormClass = ma.get_form(self.request, obj=self.group_a, change=True)
        form = FormClass(data={"name": self.group_a.name}, instance=self.group_a)
        self.assertTrue(form.is_valid(), form.errors)
        form.save(commit=False)
        formset = self._membership_formset(Group, "user", {
            "ug-TOTAL_FORMS": "1", "ug-INITIAL_FORMS": "1",
            "ug-MIN_NUM_FORMS": "0", "ug-MAX_NUM_FORMS": "1000",
            "ug-0-id": str(ug.pk),
            "ug-0-user": str(other.pk),  # row changed, not added/removed
        }, self.group_a)

        ma.save_related(self.request, form, [formset], change=True)

        mock_remove.assert_called_once_with("bucket-a", "user@example.org")
        mock_add.assert_called_once_with("bucket-a", "other@example.org")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_user_admin_added_membership_row_provisions(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        ma, form = self._user_admin_form(self.user)
        formset = self._membership_formset(User, "group", {
            "ug-TOTAL_FORMS": "1", "ug-INITIAL_FORMS": "0",
            "ug-MIN_NUM_FORMS": "0", "ug-MAX_NUM_FORMS": "1000",
            "ug-0-group": str(self.group_a.pk),
        }, self.user)

        ma.save_related(self.request, form, [formset], change=True)

        mock_add.assert_called_once_with("bucket-a", "user@example.org")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_user_admin_removed_membership_row_deprovisions(self, mock_remove, mock_add):
        mock_remove.return_value = True
        ug = UserGroup.objects.create(user=self.user, group=self.group_a)
        self._own("bucket-a", "user@example.org")
        ma, form = self._user_admin_form(self.user)
        formset = self._membership_formset(User, "group", {
            "ug-TOTAL_FORMS": "1", "ug-INITIAL_FORMS": "1",
            "ug-MIN_NUM_FORMS": "0", "ug-MAX_NUM_FORMS": "1000",
            "ug-0-id": str(ug.pk),
            "ug-0-group": str(self.group_a.pk),
            "ug-0-DELETE": "on",
        }, self.user)

        ma.save_related(self.request, form, [formset], change=True)

        mock_remove.assert_called_once_with("bucket-a", "user@example.org")
