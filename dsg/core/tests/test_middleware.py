"""Tests for DatasetContextMiddleware and DSGTokenCookieMiddleware."""

import pytest
from django.conf import settings
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


@pytest.mark.django_db
class TestLogoutClearsCookie(TestCase):
    """Verify that POST /web/logout clears the dsg_token cookie."""

    def setUp(self):
        self.user = User.objects.create(email="alice@example.org", name="alice")
        self.api_key = APIKey.objects.create(user=self.user, key="tok-logout-test")

    def test_logout_clears_dsg_token_cookie(self):
        # Authenticate via dsg_token cookie only (no Django session)
        self.client.cookies[settings.AUTH_COOKIE_NAME] = self.api_key.key

        # Verify user is seen as logged in on the web UI
        resp = self.client.get("/web/datasets")
        self.assertContains(resp, "alice@example.org")
        self.assertContains(resp, "Logout")

        # POST to logout
        resp = self.client.post("/web/logout")
        self.assertEqual(resp.status_code, 302)

        # The dsg_token cookie should be cleared (max-age=0)
        cookie = resp.cookies.get(settings.AUTH_COOKIE_NAME)
        self.assertIsNotNone(cookie, "logout response should touch the dsg_token cookie")
        self.assertEqual(cookie["max-age"], 0, "dsg_token cookie should be expired")

    def test_logout_works_without_django_session(self):
        """Logout must work even when there's no Django session — only
        a dsg_token cookie (the common case when the session expires
        before the cookie)."""
        # Only set cookie, no session login
        self.client.cookies[settings.AUTH_COOKIE_NAME] = self.api_key.key

        resp = self.client.post("/web/logout")
        self.assertEqual(resp.status_code, 302)

        cookie = resp.cookies.get(settings.AUTH_COOKIE_NAME)
        self.assertIsNotNone(cookie)
        self.assertEqual(cookie["max-age"], 0)

    def test_logout_redirects_to_root(self):
        self.client.cookies[settings.AUTH_COOKIE_NAME] = self.api_key.key
        resp = self.client.post("/web/logout")
        self.assertRedirects(resp, "/", fetch_redirect_response=False)

    def test_datasets_requires_login(self):
        """Unauthenticated users are redirected to login."""
        resp = self.client.get("/web/datasets")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/login", resp.url)
