"""Integration tests for CAVE API endpoints (table/dataset, usernames, public data)."""

import pytest
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import (
    APIKey,
    Dataset,
    DatasetVersion,
    Group,
    GroupDatasetPermission,
    Permission,
    PublicRoot,
    ServiceTable,
    User,
    UserGroup,
)


@pytest.mark.django_db
class TestTableDatasetView(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create(email="alice@example.org", name="alice")
        self.api_key = APIKey.objects.create(user=self.user, key="tok-abc")
        self.dataset = Dataset.objects.create(name="fish2")
        self.st = ServiceTable.objects.create(
            service_name="pychunkedgraph", table_name="fly_v31", dataset=self.dataset
        )

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_table_resolves_to_dataset(self):
        resp = self.client.get(
            "/api/v1/service/pychunkedgraph/table/fly_v31/dataset", **self._auth()
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), "fish2")

    def test_table_not_found(self):
        resp = self.client.get(
            "/api/v1/service/pychunkedgraph/table/nonexistent/dataset", **self._auth()
        )
        self.assertEqual(resp.status_code, 404)

    def test_unauthenticated(self):
        resp = self.client.get("/api/v1/service/pychunkedgraph/table/fly_v31/dataset")
        self.assertEqual(resp.status_code, 401)


@pytest.mark.django_db
class TestUsernameView(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.u1 = User.objects.create(email="alice@example.org", name="alice")
        self.u2 = User.objects.create(email="bob@example.org", name="bob")
        self.api_key = APIKey.objects.create(user=self.u1, key="tok-user")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_returns_usernames(self):
        resp = self.client.get(
            f"/api/v1/username?id={self.u1.pk},{self.u2.pk}", **self._auth()
        )
        self.assertEqual(resp.status_code, 200)
        names = {e["id"]: e["name"] for e in resp.json()}
        self.assertIn(self.u1.pk, names)
        self.assertIn(self.u2.pk, names)

    def test_empty_ids(self):
        resp = self.client.get("/api/v1/username?id=", **self._auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_invalid_ids(self):
        resp = self.client.get("/api/v1/username?id=abc", **self._auth())
        self.assertEqual(resp.status_code, 400)


@pytest.mark.django_db
class TestUserListView(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create(
            email="alice@example.org", name="alice", pi="Dr. Smith"
        )
        self.api_key = APIKey.objects.create(user=self.user, key="tok-ulist")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_returns_user_info(self):
        resp = self.client.get(f"/api/v1/user?id={self.user.pk}", **self._auth())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["email"], "alice@example.org")
        self.assertEqual(data[0]["pi"], "Dr. Smith")


@pytest.mark.django_db
class TestUserPermissionsView(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.caller = User.objects.create(email="caller@example.org", name="caller")
        self.api_key = APIKey.objects.create(user=self.caller, key="tok-perm")
        self.target = User.objects.create(email="target@example.org", name="target")
        ds = Dataset.objects.create(name="fish2")
        g = Group.objects.create(name="team")
        UserGroup.objects.create(user=self.target, group=g)
        perm = Permission.objects.create(name="view")
        GroupDatasetPermission.objects.create(group=g, dataset=ds, permission=perm)

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_returns_target_permissions(self):
        resp = self.client.get(
            f"/api/v1/user/{self.target.pk}/permissions", **self._auth()
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["id"], self.target.pk)
        self.assertIn("fish2", data["permissions_v2"])

    def test_user_not_found(self):
        resp = self.client.get("/api/v1/user/99999/permissions", **self._auth())
        self.assertEqual(resp.status_code, 404)


@pytest.mark.django_db
class TestPublicDataViews(TestCase):
    def setUp(self):
        self.client = APIClient()
        ds = Dataset.objects.create(name="fanc")
        self.st = ServiceTable.objects.create(
            service_name="pychunkedgraph", table_name="fanc_v4", dataset=ds
        )
        PublicRoot.objects.create(service_table=self.st, root_id=100)
        PublicRoot.objects.create(service_table=self.st, root_id=200)

    def test_table_has_public(self):
        resp = self.client.get("/api/v1/table/fanc_v4/has_public")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json())

    def test_table_has_no_public(self):
        resp = self.client.get("/api/v1/table/nonexistent/has_public")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json())

    def test_root_is_public(self):
        resp = self.client.get("/api/v1/table/fanc_v4/root/100/is_public")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json())

    def test_root_is_not_public(self):
        resp = self.client.get("/api/v1/table/fanc_v4/root/999/is_public")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json())

    def test_root_all_public_batch(self):
        resp = self.client.post(
            "/api/v1/table/fanc_v4/root_all_public",
            [100, 200, 300],
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [True, True, False])

    def test_root_all_public_invalid_input(self):
        resp = self.client.post(
            "/api/v1/table/fanc_v4/root_all_public",
            "not-a-list",
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
