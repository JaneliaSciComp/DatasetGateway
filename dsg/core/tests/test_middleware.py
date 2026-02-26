"""Tests for the DatasetContextMiddleware."""

import pytest
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import (
    APIKey,
    Dataset,
    Group,
    GroupDatasetPermission,
    Permission,
    ServiceTable,
    User,
    UserGroup,
)


@pytest.mark.django_db
class TestDatasetContextMiddleware(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create(email="alice@example.org", name="alice")
        self.api_key = APIKey.objects.create(user=self.user, key="tok-mid")
        self.ds = Dataset.objects.create(name="fish2")
        ServiceTable.objects.create(
            service_name="pychunkedgraph", table_name="fly_v31", dataset=self.ds
        )
        g = Group.objects.create(name="team")
        UserGroup.objects.create(user=self.user, group=g)
        perm = Permission.objects.create(name="view")
        GroupDatasetPermission.objects.create(group=g, dataset=self.ds, permission=perm)

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_dataset_prefix_stripped(self):
        """Requests like /fish2/cave/api/v1/user/cache should work
        after the middleware strips the dataset/service_type prefix."""
        resp = self.client.get("/fish2/cave/api/v1/user/cache", **self._auth())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["email"], "alice@example.org")

    def test_passthrough_paths_not_stripped(self):
        """Paths like /api/v1/... should work directly without prefix stripping."""
        resp = self.client.get("/api/v1/user/cache", **self._auth())
        self.assertEqual(resp.status_code, 200)

    def test_table_dataset_via_prefix(self):
        """Table/dataset lookup should work through the middleware prefix path."""
        resp = self.client.get(
            "/fish2/pychunkedgraph/api/v1/service/pychunkedgraph/table/fly_v31/dataset",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), "fish2")
