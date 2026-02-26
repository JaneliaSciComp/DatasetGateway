"""Tests for the /api/v1/user/cache endpoint."""

import pytest
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import (
    APIKey,
    Dataset,
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
class TestUserCacheView(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create(email="alice@example.org", name="alice", admin=False)
        self.api_key = APIKey.objects.create(user=self.user, key="test-token-123")

    def _auth_header(self, token=None):
        return {"HTTP_AUTHORIZATION": f"Bearer {token or self.api_key.key}"}

    def test_unauthenticated_returns_401(self):
        resp = self.client.get("/api/v1/user/cache")
        self.assertEqual(resp.status_code, 401)

    def test_invalid_token_returns_401(self):
        resp = self.client.get("/api/v1/user/cache", **self._auth_header("bad-token"))
        self.assertEqual(resp.status_code, 401)

    def test_valid_token_returns_cache(self):
        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["id"], self.user.pk)
        self.assertEqual(data["email"], "alice@example.org")
        self.assertEqual(data["name"], "alice")
        self.assertFalse(data["admin"])
        self.assertFalse(data["service_account"])
        self.assertIsNone(data["parent_id"])
        self.assertEqual(data["groups"], [])
        self.assertEqual(data["permissions"], {})
        self.assertEqual(data["permissions_v2"], {})
        self.assertEqual(data["missing_tos"], [])

    def test_admin_flag(self):
        self.user.admin = True
        self.user.save()
        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()
        self.assertTrue(data["admin"])

    def test_groups_in_cache(self):
        group = Group.objects.create(name="researchers")
        UserGroup.objects.create(user=self.user, group=group)
        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()
        self.assertIn("researchers", data["groups"])

    def test_permissions_in_cache(self):
        view_perm = Permission.objects.create(name="view")
        edit_perm = Permission.objects.create(name="edit")
        dataset = Dataset.objects.create(name="fish2")
        group = Group.objects.create(name="fish-team")
        UserGroup.objects.create(user=self.user, group=group)
        GroupDatasetPermission.objects.create(group=group, dataset=dataset, permission=view_perm)
        GroupDatasetPermission.objects.create(group=group, dataset=dataset, permission=edit_perm)

        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()

        self.assertIn("fish2", data["permissions_v2"])
        self.assertIn("view", data["permissions_v2"]["fish2"])
        self.assertIn("edit", data["permissions_v2"]["fish2"])
        self.assertEqual(data["permissions"]["fish2"], 2)  # edit = level 2

    def test_tos_filtering(self):
        """Permissions should be excluded when TOS not accepted."""
        view_perm = Permission.objects.create(name="view")
        tos_doc = TOSDocument.objects.create(name="Terms v1", text="Accept these terms.")
        dataset = Dataset.objects.create(name="fanc", tos=tos_doc)
        group = Group.objects.create(name="fanc-team")
        UserGroup.objects.create(user=self.user, group=group)
        GroupDatasetPermission.objects.create(group=group, dataset=dataset, permission=view_perm)

        # Without TOS acceptance, permissions_v2 should NOT include fanc
        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()
        self.assertNotIn("fanc", data["permissions_v2"])
        # But permissions_v2_ignore_tos should include it
        self.assertIn("fanc", data["permissions_v2_ignore_tos"])
        # And missing_tos should list it
        self.assertTrue(any(m["dataset_name"] == "fanc" for m in data["missing_tos"]))

        # After accepting TOS — clear cache so new permissions are computed
        TOSAcceptance.objects.create(user=self.user, tos_document=tos_doc)
        cache.clear()
        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()
        self.assertIn("fanc", data["permissions_v2"])
        self.assertFalse(any(m["dataset_name"] == "fanc" for m in data["missing_tos"]))

    def test_datasets_admin(self):
        dataset = Dataset.objects.create(name="fish2")
        admin_perm, _ = Permission.objects.get_or_create(name="admin")
        Grant.objects.create(user=self.user, dataset=dataset, permission=admin_perm)
        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()
        self.assertIn("fish2", data["datasets_admin"])

    def test_cookie_authentication(self):
        self.client.cookies["dsg_token"] = self.api_key.key
        resp = self.client.get("/api/v1/user/cache")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["email"], "alice@example.org")

    def test_query_param_authentication(self):
        resp = self.client.get(f"/api/v1/user/cache?dsg_token={self.api_key.key}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["email"], "alice@example.org")
