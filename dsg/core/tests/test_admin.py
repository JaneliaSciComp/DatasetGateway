"""Tests for the Django-admin hooks in core/admin.py.

Each test drives the ModelAdmin methods the way the admin views do
(get_form → is_valid → save(commit=False) → save_model, etc.).
"""

import pytest
from django.contrib import admin as django_admin
from django.forms.models import inlineformset_factory
from django.test import RequestFactory, TestCase

from core.admin import (
    DatasetTranslationAdmin,
    DatasetVersionAdmin,
    GrantAdmin,
    PermissionAdmin,
    TOSDocumentAdmin,
    UserAdmin,
)
from core.models import (
    AuditLog,
    Dataset,
    DatasetTranslation,
    DatasetVersion,
    Grant,
    Group,
    Permission,
    Service,
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


@pytest.mark.django_db
class TestPermissionAdmin(_AdminTestBase):
    def test_name_is_editable_on_add_and_read_only_on_change(self):
        model_admin = PermissionAdmin(Permission, django_admin.site)

        self.assertNotIn(
            "name", model_admin.get_readonly_fields(self.request, obj=None)
        )
        self.assertIn(
            "name", model_admin.get_readonly_fields(self.request, obj=self.view_perm)
        )


@pytest.mark.django_db
class TestDatasetTranslationAdmin(_AdminTestBase):
    def setUp(self):
        super().setUp()
        self.ma = DatasetTranslationAdmin(DatasetTranslation, django_admin.site)
        self.service = Service.objects.create(name="neuprint")
        self.dataset = Dataset.objects.create(name="canonical")
        self.other_dataset = Dataset.objects.create(name="other")
        self.version = DatasetVersion.objects.create(dataset=self.dataset, version="v1")
        self.other_version = DatasetVersion.objects.create(
            dataset=self.other_dataset, version="v1"
        )

    def _form(self, data):
        FormClass = self.ma.get_form(self.request)
        return FormClass(data=data)

    def test_admin_configuration(self):
        self.assertEqual(
            self.ma.model._meta.verbose_name_plural,
            "dataset translations",
        )
        self.assertEqual(
            self.ma.list_display,
            (
                "id", "service", "client_name", "client_version", "dataset",
                "dataset_version",
            ),
        )
        self.assertEqual(self.ma.list_filter, ("service",))
        self.assertEqual(
            self.ma.autocomplete_fields,
            ("service", "dataset", "dataset_version"),
        )
        self.assertEqual(
            self.ma.list_select_related,
            ("service", "dataset", "dataset_version"),
        )

    def test_name_level_translation_creation_form_is_valid(self):
        form = self._form({
            "service": self.service.pk,
            "client_name": "client-ds",
            "client_version": "",
            "dataset": self.dataset.pk,
            "dataset_version": "",
        })

        self.assertTrue(form.is_valid(), form.errors)
        translation = form.save()
        self.assertEqual(translation.client_name, "client-ds")
        self.assertIsNone(translation.client_version)
        self.assertIsNone(translation.dataset_version)

    def test_version_mismatch_surfaces_as_form_error(self):
        form = self._form({
            "service": self.service.pk,
            "client_name": "client-ds",
            "client_version": "v1",
            "dataset": self.dataset.pk,
            "dataset_version": self.other_version.pk,
        })

        self.assertFalse(form.is_valid())
        self.assertIn("dataset_version", form.errors)

    def test_null_invariant_violation_surfaces_as_form_error(self):
        form = self._form({
            "service": self.service.pk,
            "client_name": "client-ds",
            "client_version": "v1",
            "dataset": self.dataset.pk,
            "dataset_version": "",
        })

        self.assertFalse(form.is_valid())
        self.assertIn("__all__", form.errors)


@pytest.mark.django_db
class TestGrantAdmin(_AdminTestBase):
    def setUp(self):
        super().setUp()
        self.ma = GrantAdmin(Grant, django_admin.site)
        self.dataset = Dataset.objects.create(name="ds1")
        self.user = User.objects.create(email="user@example.org")

    def test_bulk_delete_audits_each(self):
        other = User.objects.create(email="other@example.org")
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)
        Grant.objects.create(user=other, dataset=self.dataset, permission=self.view_perm)
        self.ma.delete_queryset(self.request, Grant.objects.all())
        self.assertEqual(Grant.objects.count(), 0)
        # The per-object path audits, unlike stock bulk delete
        self.assertEqual(AuditLog.objects.filter(action="grant_deleted").count(), 2)


@pytest.mark.django_db
class TestDatasetVersionAdmin(_AdminTestBase):
    def test_public_field_explains_ancestor_coverage(self):
        ma = DatasetVersionAdmin(DatasetVersion, django_admin.site)
        version = DatasetVersion.objects.create(
            dataset=Dataset.objects.create(name="ds1"), version="v1",
        )
        form_class = ma.get_form(self.request, obj=version, change=True)

        self.assertEqual(
            form_class.base_fields["is_public"].help_text,
            (
                "Covers this version and, when ordinals are set, its "
                "same-branch ancestors."
            ),
        )


@pytest.mark.django_db
class TestTOSDocumentAdmin(_AdminTestBase):
    def setUp(self):
        super().setUp()
        self.ma = TOSDocumentAdmin(TOSDocument, django_admin.site)
        self.dataset = Dataset.objects.create(name="ds1")

    def test_general_tos_autosets_dataset_tos(self):
        tos = self._save_via_admin(self.ma, {
            "name": "TOS", "text": "Terms", "dataset": self.dataset.pk,
            "invite_token": "tok-autoset-1",
            "effective_date_0": "2026-01-01", "effective_date_1": "00:00:00",
        })
        self.dataset.refresh_from_db()
        self.assertEqual(self.dataset.tos_id, tos.pk)

    def test_move_between_datasets_moves_the_gate(self):
        tos = TOSDocument.objects.create(
            name="TOS", text="Terms", dataset=self.dataset, invite_token="tok-move-1",
        )
        self.dataset.tos = tos
        self.dataset.save()
        other_ds = Dataset.objects.create(name="ds2")

        self._save_via_admin(self.ma, {
            "name": "TOS", "text": "Terms", "dataset": other_ds.pk,
            "invite_token": "tok-move-1",
            "effective_date_0": "2026-01-01", "effective_date_1": "00:00:00",
        }, instance=tos)

        self.dataset.refresh_from_db()
        self.assertIsNone(self.dataset.tos_id)
        other_ds.refresh_from_db()
        self.assertEqual(other_ds.tos_id, tos.pk)


@pytest.mark.django_db
class TestMembershipAdmin(_AdminTestBase):
    """User↔group membership changes via UserAdmin save_related are audited."""

    def test_user_admin_changed_membership_row_audits_both_groups(self):
        group_a = Group.objects.create(name="lab-a")
        group_b = Group.objects.create(name="lab-b")
        user = User.objects.create(email="user@example.org")
        ug = UserGroup.objects.create(user=user, group=group_a)
        ma = UserAdmin(User, django_admin.site)
        FormClass = ma.get_form(self.request, obj=user, change=True)
        form = FormClass(data={
            "email": user.email, "name": user.name, "is_active": "on",
        }, instance=user)
        self.assertTrue(form.is_valid(), form.errors)
        form.save(commit=False)
        FormSet = inlineformset_factory(
            User, UserGroup, fields=["group", "is_admin"], extra=0, can_delete=True,
        )
        formset = FormSet(data={
            "ug-TOTAL_FORMS": "1", "ug-INITIAL_FORMS": "1",
            "ug-MIN_NUM_FORMS": "0", "ug-MAX_NUM_FORMS": "1000",
            "ug-0-id": str(ug.pk),
            "ug-0-group": str(group_b.pk),  # row changed, not added/removed
        }, instance=user, prefix="ug")
        self.assertTrue(formset.is_valid(), formset.errors)

        ma.save_related(self.request, form, [formset], change=True)

        added = AuditLog.objects.get(action="member_added")
        removed = AuditLog.objects.get(action="member_removed")
        self.assertEqual(added.after_state, {"user": user.email, "group": "lab-b"})
        self.assertEqual(removed.before_state, {"user": user.email, "group": "lab-a"})
