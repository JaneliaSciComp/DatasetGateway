"""Tests for Grant-based permissions in the user cache."""

import pytest
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import (
    APIKey,
    Dataset,
    DatasetAdmin,
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
class TestGrantPermissions(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create(email="bob@example.org", name="bob")
        self.api_key = APIKey.objects.create(user=self.user, key="grant-test-token")
        self.view_perm = Permission.objects.create(name="view")
        self.edit_perm = Permission.objects.create(name="edit")

    def _auth_header(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_grant_view_appears_in_permissions(self):
        dataset = Dataset.objects.create(name="hemibrain")
        Grant.objects.create(user=self.user, dataset=dataset, permission=self.view_perm)

        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()

        self.assertIn("hemibrain", data["permissions_v2"])
        self.assertIn("view", data["permissions_v2"]["hemibrain"])
        self.assertEqual(data["permissions"]["hemibrain"], 1)  # view = level 1

    def test_grant_edit_appears_in_permissions(self):
        dataset = Dataset.objects.create(name="vnc")
        Grant.objects.create(user=self.user, dataset=dataset, permission=self.edit_perm)

        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()

        self.assertIn("vnc", data["permissions_v2"])
        self.assertIn("edit", data["permissions_v2"]["vnc"])
        self.assertEqual(data["permissions"]["vnc"], 2)

    def test_grant_merged_with_group_permissions(self):
        """Grant and group permissions on the same dataset are merged."""
        dataset = Dataset.objects.create(name="fanc")
        group = Group.objects.create(name="fanc-team")
        UserGroup.objects.create(user=self.user, group=group)
        GroupDatasetPermission.objects.create(
            group=group, dataset=dataset, permission=self.view_perm
        )
        # Grant adds edit on top of group's view
        Grant.objects.create(user=self.user, dataset=dataset, permission=self.edit_perm)

        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()

        self.assertIn("fanc", data["permissions_v2"])
        perms = data["permissions_v2"]["fanc"]
        self.assertIn("view", perms)
        self.assertIn("edit", perms)

    def test_grant_no_duplicate_permissions(self):
        """Same permission from group and grant should not appear twice."""
        dataset = Dataset.objects.create(name="fish2")
        group = Group.objects.create(name="fish-team")
        UserGroup.objects.create(user=self.user, group=group)
        GroupDatasetPermission.objects.create(
            group=group, dataset=dataset, permission=self.view_perm
        )
        Grant.objects.create(user=self.user, dataset=dataset, permission=self.view_perm)

        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()

        self.assertEqual(data["permissions_v2"]["fish2"].count("view"), 1)

    def test_grant_respects_tos(self):
        """Grants on TOS-gated datasets excluded from permissions_v2 until TOS accepted."""
        tos_doc = TOSDocument.objects.create(name="Terms v1", text="Accept.")
        dataset = Dataset.objects.create(name="restricted", tos=tos_doc)
        Grant.objects.create(user=self.user, dataset=dataset, permission=self.view_perm)

        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()

        # Not in permissions_v2 (TOS not accepted)
        self.assertNotIn("restricted", data["permissions_v2"])
        # In permissions_v2_ignore_tos
        self.assertIn("restricted", data["permissions_v2_ignore_tos"])
        # In missing_tos
        self.assertTrue(any(m["dataset_name"] == "restricted" for m in data["missing_tos"]))

        # Accept TOS
        TOSAcceptance.objects.create(user=self.user, tos_document=tos_doc)
        cache.clear()

        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()
        self.assertIn("restricted", data["permissions_v2"])
        self.assertFalse(any(m["dataset_name"] == "restricted" for m in data["missing_tos"]))

    def test_grant_read_only_excludes_edit(self):
        """Read-only users should not see edit grants."""
        self.user.read_only = True
        self.user.save()
        dataset = Dataset.objects.create(name="readonly-ds")
        Grant.objects.create(user=self.user, dataset=dataset, permission=self.view_perm)
        Grant.objects.create(user=self.user, dataset=dataset, permission=self.edit_perm)

        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()

        self.assertIn("readonly-ds", data["permissions_v2"])
        self.assertIn("view", data["permissions_v2"]["readonly-ds"])
        self.assertNotIn("edit", data["permissions_v2"]["readonly-ds"])
