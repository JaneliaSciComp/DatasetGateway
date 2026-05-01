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
    Service,
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

    def test_public_dataset_tos_appears_in_missing_tos(self):
        """Public datasets with a TOS should appear in missing_tos even
        when the user has no grant — clients use this to redirect to DSG."""
        tos_doc = TOSDocument.objects.create(name="Hemibrain TOS", text="Accept.")
        Dataset.objects.create(
            name="hemibrain", tos=tos_doc, access_mode=Dataset.ACCESS_PUBLIC,
        )

        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()
        self.assertTrue(any(m["dataset_name"] == "hemibrain" for m in data["missing_tos"]))
        # User has no grant, so dataset should not appear in permissions_v2
        self.assertNotIn("hemibrain", data["permissions_v2"])

        # After accepting, it should drop out of missing_tos
        TOSAcceptance.objects.create(user=self.user, tos_document=tos_doc)
        cache.clear()
        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()
        self.assertFalse(any(m["dataset_name"] == "hemibrain" for m in data["missing_tos"]))

    def test_closed_dataset_without_grant_not_in_missing_tos(self):
        """Closed datasets without a grant should NOT appear in missing_tos."""
        tos_doc = TOSDocument.objects.create(name="Closed TOS", text="Accept.")
        Dataset.objects.create(
            name="restricted", tos=tos_doc, access_mode=Dataset.ACCESS_CLOSED,
        )

        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()
        self.assertFalse(any(m["dataset_name"] == "restricted" for m in data["missing_tos"]))

    def test_public_dataset_service_tos_in_missing_tos(self):
        """Service-specific TOS on a public dataset must surface in missing_tos
        even without a grant."""
        Dataset.objects.create(name="hemibrain", access_mode=Dataset.ACCESS_PUBLIC)
        dataset = Dataset.objects.get(name="hemibrain")
        svc = Service.objects.create(name="celltyping", display_name="Cell Typing")
        TOSDocument.objects.create(
            name="Hemi CT TOS", text="Service terms.", dataset=dataset, service=svc,
        )

        resp = self.client.get(
            "/api/v1/user/cache?service=celltyping", **self._auth_header()
        )
        data = resp.json()
        self.assertTrue(any(
            m["dataset_name"] == "hemibrain" and m.get("service") == "celltyping"
            for m in data["missing_tos"]
        ))

    def test_service_tos_filtering(self):
        """Service-specific TOS blocks permissions_v2 when not accepted."""
        view_perm = Permission.objects.create(name="view")
        dataset = Dataset.objects.create(name="fish2")
        group = Group.objects.create(name="fish-team")
        UserGroup.objects.create(user=self.user, group=group)
        GroupDatasetPermission.objects.create(group=group, dataset=dataset, permission=view_perm)

        svc = Service.objects.create(name="celltyping", display_name="Cell Typing")
        TOSDocument.objects.create(
            name="Celltyping TOS", text="Service terms.", dataset=dataset, service=svc,
        )

        # Without ?service, fish2 should appear (no general dataset TOS)
        resp = self.client.get("/api/v1/user/cache", **self._auth_header())
        data = resp.json()
        self.assertIn("fish2", data["permissions_v2"])
        self.assertEqual(data["missing_tos"], [])

        # With ?service=celltyping, fish2 should be blocked
        cache.clear()
        resp = self.client.get("/api/v1/user/cache?service=celltyping", **self._auth_header())
        data = resp.json()
        self.assertNotIn("fish2", data["permissions_v2"])
        self.assertTrue(any(
            m["dataset_name"] == "fish2" and m.get("service") == "celltyping"
            for m in data["missing_tos"]
        ))

    def test_service_tos_accepted_then_visible(self):
        """After accepting service-specific TOS, dataset appears in permissions_v2."""
        view_perm = Permission.objects.create(name="view")
        dataset = Dataset.objects.create(name="fish2")
        group = Group.objects.create(name="fish-team")
        UserGroup.objects.create(user=self.user, group=group)
        GroupDatasetPermission.objects.create(group=group, dataset=dataset, permission=view_perm)

        svc = Service.objects.create(name="celltyping", display_name="Cell Typing")
        tos = TOSDocument.objects.create(
            name="Celltyping TOS", text="Service terms.", dataset=dataset, service=svc,
        )
        TOSAcceptance.objects.create(user=self.user, tos_document=tos)

        cache.clear()
        resp = self.client.get("/api/v1/user/cache?service=celltyping", **self._auth_header())
        data = resp.json()
        self.assertIn("fish2", data["permissions_v2"])
        self.assertFalse(any(
            m.get("service") == "celltyping" for m in data["missing_tos"]
        ))

    def test_both_general_and_service_tos(self):
        """Both general and service TOS must be accepted for access."""
        view_perm = Permission.objects.create(name="view")
        general_tos = TOSDocument.objects.create(name="General TOS", text="General terms.")
        dataset = Dataset.objects.create(name="fish2", tos=general_tos)
        Grant.objects.create(user=self.user, dataset=dataset, permission=view_perm)

        svc = Service.objects.create(name="celltyping")
        svc_tos = TOSDocument.objects.create(
            name="CT TOS", text="CT terms.", dataset=dataset, service=svc,
        )

        # Neither accepted → blocked
        cache.clear()
        resp = self.client.get("/api/v1/user/cache?service=celltyping", **self._auth_header())
        data = resp.json()
        self.assertNotIn("fish2", data["permissions_v2"])

        # Accept only general TOS → still blocked by service TOS
        TOSAcceptance.objects.create(user=self.user, tos_document=general_tos)
        cache.clear()
        resp = self.client.get("/api/v1/user/cache?service=celltyping", **self._auth_header())
        data = resp.json()
        self.assertNotIn("fish2", data["permissions_v2"])

        # Accept service TOS too → now visible
        TOSAcceptance.objects.create(user=self.user, tos_document=svc_tos)
        cache.clear()
        resp = self.client.get("/api/v1/user/cache?service=celltyping", **self._auth_header())
        data = resp.json()
        self.assertIn("fish2", data["permissions_v2"])
