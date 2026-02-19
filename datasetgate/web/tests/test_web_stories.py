"""Tests for web access-control user stories."""

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase

from core.models import (
    APIKey,
    Dataset,
    DatasetAdmin,
    Grant,
    Group,
    Permission,
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
