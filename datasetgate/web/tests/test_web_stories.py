"""Tests for web access-control user stories."""

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase

from core.models import (
    APIKey,
    Dataset,
    DatasetAdmin,
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


def _auth_cookies(api_key):
    """Return a dict suitable for client.cookies to authenticate via dsg_token."""
    return {settings.AUTH_COOKIE_NAME: api_key.key}


class _WebTestBase(TestCase):
    """Shared setUp for web story tests."""

    def setUp(self):
        cache.clear()

        # Users
        self.sc_user = User.objects.create(email="sc@example.org", name="SC Member")
        self.sc_key = APIKey.objects.create(user=self.sc_user, key="tok-sc")

        self.admin_user = User.objects.create(email="admin@example.org", name="Admin", admin=True)
        self.admin_key = APIKey.objects.create(user=self.admin_user, key="tok-admin")

        self.lab_head = User.objects.create(email="labhead@example.org", name="Lab Head")
        self.lab_head_key = APIKey.objects.create(user=self.lab_head, key="tok-labhead")

        self.regular_user = User.objects.create(email="regular@example.org", name="Regular")
        self.regular_key = APIKey.objects.create(user=self.regular_user, key="tok-regular")

        # Groups
        self.sc_group = Group.objects.create(name="sc")
        UserGroup.objects.create(user=self.sc_user, group=self.sc_group)

        # Dataset
        self.dataset = Dataset.objects.create(name="test-dataset")

        # Lab head is a DatasetAdmin (but NOT in sc group)
        DatasetAdmin.objects.create(user=self.lab_head, dataset=self.dataset)

        # Permissions
        self.view_perm = Permission.objects.create(name="view")
        self.edit_perm = Permission.objects.create(name="edit")

    def _login(self, api_key):
        self.client.cookies[settings.AUTH_COOKIE_NAME] = api_key.key


# ──────────────────────────────────────────────────────────────
# Commit 1: DatasetAdmin management (SC promotes lab heads)
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestDatasetAdminManage(_WebTestBase):
    def test_sc_member_can_view(self):
        self._login(self.sc_key)
        resp = self.client.get(f"/web/dataset-admins/{self.dataset.name}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "labhead@example.org")

    def test_global_admin_can_view(self):
        self._login(self.admin_key)
        resp = self.client.get(f"/web/dataset-admins/{self.dataset.name}")
        self.assertEqual(resp.status_code, 200)

    def test_lab_head_denied(self):
        """Lab head (DatasetAdmin only, not SC) cannot manage lab heads."""
        self._login(self.lab_head_key)
        resp = self.client.get(f"/web/dataset-admins/{self.dataset.name}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Access Denied")

    def test_regular_user_denied(self):
        self._login(self.regular_key)
        resp = self.client.get(f"/web/dataset-admins/{self.dataset.name}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Access Denied")

    def test_unauthenticated_redirects(self):
        resp = self.client.get(f"/web/dataset-admins/{self.dataset.name}")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/login", resp.url)

    def test_sc_can_add_lab_head(self):
        self._login(self.sc_key)
        new_user = User.objects.create(email="newlead@example.org", name="New Lead")
        resp = self.client.post(
            f"/web/dataset-admins/{self.dataset.name}",
            {"action": "add", "email": "newlead@example.org"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            DatasetAdmin.objects.filter(user=new_user, dataset=self.dataset).exists()
        )

    def test_add_nonexistent_email_shows_error(self):
        self._login(self.sc_key)
        resp = self.client.post(
            f"/web/dataset-admins/{self.dataset.name}",
            {"action": "add", "email": "nobody@example.org"},
            follow=True,
        )
        self.assertContains(resp, "User not found")

    def test_sc_can_remove_lab_head(self):
        self._login(self.sc_key)
        da = DatasetAdmin.objects.get(user=self.lab_head, dataset=self.dataset)
        resp = self.client.post(
            f"/web/dataset-admins/{self.dataset.name}",
            {"action": "remove", "admin_id": da.pk},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(
            DatasetAdmin.objects.filter(user=self.lab_head, dataset=self.dataset).exists()
        )

    def test_datasets_page_shows_manage_lab_heads_for_sc(self):
        self._login(self.sc_key)
        resp = self.client.get("/web/datasets")
        self.assertContains(resp, "Manage Lab Heads")

    def test_datasets_page_hides_manage_lab_heads_for_regular(self):
        self._login(self.regular_key)
        resp = self.client.get("/web/datasets")
        self.assertNotContains(resp, "Manage Lab Heads")


# ──────────────────────────────────────────────────────────────
# Commit 2: Lab heads add users (enhance GrantManageView)
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestGrantManageEnhanced(_WebTestBase):
    def test_lab_head_grants_existing_user(self):
        self._login(self.lab_head_key)
        resp = self.client.post(
            f"/web/grants/{self.dataset.name}",
            {"action": "grant", "email": "regular@example.org", "permission": self.view_perm.pk},
        )
        self.assertEqual(resp.status_code, 302)
        grant = Grant.objects.get(user=self.regular_user, dataset=self.dataset)
        self.assertEqual(grant.source, Grant.SOURCE_MANUAL)
        self.assertEqual(grant.granted_by, self.lab_head)

    def test_lab_head_grants_new_email_creates_user(self):
        self._login(self.lab_head_key)
        resp = self.client.post(
            f"/web/grants/{self.dataset.name}",
            {"action": "grant", "email": "newbie@example.org", "permission": self.view_perm.pk},
        )
        self.assertEqual(resp.status_code, 302)
        new_user = User.objects.get(email="newbie@example.org")
        self.assertEqual(new_user.name, "newbie")
        self.assertFalse(new_user.has_usable_password())
        grant = Grant.objects.get(user=new_user, dataset=self.dataset)
        self.assertEqual(grant.source, Grant.SOURCE_MANUAL)

    def test_grant_success_message_for_new_user(self):
        self._login(self.lab_head_key)
        resp = self.client.post(
            f"/web/grants/{self.dataset.name}",
            {"action": "grant", "email": "newbie@example.org", "permission": self.view_perm.pk},
            follow=True,
        )
        self.assertContains(resp, "Created user and granted")

    def test_global_admin_can_manage_grants(self):
        self._login(self.admin_key)
        resp = self.client.get(f"/web/grants/{self.dataset.name}")
        self.assertEqual(resp.status_code, 200)

    def test_regular_user_denied(self):
        self._login(self.regular_key)
        resp = self.client.get(f"/web/grants/{self.dataset.name}")
        self.assertContains(resp, "Access Denied")

    def test_unauthenticated_redirects(self):
        resp = self.client.get(f"/web/grants/{self.dataset.name}")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/login", resp.url)

    def test_grant_manage_shows_source_column(self):
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, granted_by=self.lab_head,
            source=Grant.SOURCE_MANUAL,
        )
        self._login(self.lab_head_key)
        resp = self.client.get(f"/web/grants/{self.dataset.name}")
        self.assertContains(resp, "Added by admin or lab head")


# ──────────────────────────────────────────────────────────────
# Commit 3: Enhanced user dashboard
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestMyDatasetsDashboard(_WebTestBase):
    def test_shows_direct_grants(self):
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, source=Grant.SOURCE_MANUAL,
        )
        self._login(self.regular_key)
        resp = self.client.get("/web/my-datasets")
        self.assertContains(resp, "test-dataset")
        self.assertContains(resp, "view")

    def test_shows_group_permissions(self):
        group = Group.objects.create(name="researchers")
        UserGroup.objects.create(user=self.regular_user, group=group)
        GroupDatasetPermission.objects.create(
            group=group, dataset=self.dataset, permission=self.view_perm,
        )
        self._login(self.regular_key)
        resp = self.client.get("/web/my-datasets")
        self.assertContains(resp, "researchers")
        self.assertContains(resp, "Group-Based Permissions")

    def test_shows_admin_datasets(self):
        self._login(self.lab_head_key)
        resp = self.client.get("/web/my-datasets")
        self.assertContains(resp, "Datasets You Admin")
        self.assertContains(resp, "test-dataset")
        self.assertContains(resp, "Manage Grants")

    def test_shows_groups(self):
        self._login(self.sc_key)
        resp = self.client.get("/web/my-datasets")
        self.assertContains(resp, "sc")

    def test_shows_missing_tos_with_invite_token(self):
        tos = TOSDocument.objects.create(
            name="Test TOS", text="Terms here", dataset=self.dataset,
            invite_token="test-invite-token-abc",
        )
        self.dataset.tos = tos
        self.dataset.save()
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm,
        )
        self._login(self.regular_key)
        resp = self.client.get("/web/my-datasets")
        self.assertContains(resp, "Action Required")
        self.assertContains(resp, "test-invite-token-abc")

    def test_unauthenticated_redirects(self):
        resp = self.client.get("/web/my-datasets")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/login", resp.url)
