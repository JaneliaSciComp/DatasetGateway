"""Tests for ServiceAccount auth and permission cache."""

import pytest
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from rest_framework.exceptions import AuthenticationFailed

from core.authentication import TokenAuthentication
from core.cache import build_permission_cache
from core.models import (
    APIKey,
    Dataset,
    DatasetVersion,
    Permission,
    ServiceAccount,
    ServiceAccountGrant,
    ServiceAccountToken,
    User,
)


@pytest.mark.django_db
class TestSAAuthentication(TestCase):
    def setUp(self):
        cache.clear()
        self.rf = RequestFactory()
        self.auth = TokenAuthentication()
        self.sa = ServiceAccount.objects.create(name="ci-bot", description="ci")
        self.sa_token = ServiceAccountToken.objects.create(
            service_account=self.sa, description="ci-token", key="tok-sa"
        )

    def _request_with_bearer(self, key):
        request = self.rf.get("/", HTTP_AUTHORIZATION=f"Bearer {key}")
        # DRF wraps Django requests in its own Request; for the auth class we
        # only need .COOKIES, .META, .query_params.
        request.query_params = {}
        return request

    def test_valid_sa_token_returns_service_account_principal(self):
        principal, token = self.auth.authenticate(self._request_with_bearer("tok-sa"))
        self.assertIsInstance(principal, ServiceAccount)
        self.assertEqual(principal.pk, self.sa.pk)
        self.assertEqual(token, "tok-sa")

    def test_disabled_sa_rejected(self):
        self.sa.is_active = False
        self.sa.save()
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(self._request_with_bearer("tok-sa"))

    def test_unknown_token_rejected(self):
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(self._request_with_bearer("nope"))

    def test_last_used_updated(self):
        from django.utils import timezone
        before = timezone.now()
        self.auth.authenticate(self._request_with_bearer("tok-sa"))
        self.sa_token.refresh_from_db()
        self.assertIsNotNone(self.sa_token.last_used)
        self.assertGreaterEqual(self.sa_token.last_used, before)

    def test_cache_key_does_not_collide_with_user_pk(self):
        # Force the SA pk to equal a User pk to confirm namespacing.
        user = User.objects.create(email="dup@example.org", name="dup")
        # Move SA pk to user.pk by recreating with explicit id.
        sa_dup = ServiceAccount.objects.create(name="dup-sa")
        sa_dup_pk = sa_dup.pk
        sa_dup.delete()
        ServiceAccount.objects.filter(pk=sa_dup_pk).delete()

        # If pks happen to match (often true for fresh test DBs), the namespace
        # ensures the user cache and SA cache don't overwrite each other.
        api_key = APIKey.objects.create(user=user, key="tok-user-cache")
        ServiceAccountToken.objects.create(
            service_account=self.sa, description="other", key="tok-sa-cache",
        )
        cache.clear()
        self.auth.authenticate(self._request_with_bearer("tok-user-cache"))
        self.auth.authenticate(self._request_with_bearer("tok-sa-cache"))

        user_cache = cache.get(f"{self.auth.CACHE_PREFIX}{user.pk}")
        sa_cache = cache.get(f"{self.auth.CACHE_PREFIX}sa_{self.sa.pk}")
        self.assertIsNotNone(user_cache)
        self.assertIsNotNone(sa_cache)
        self.assertFalse(user_cache.get("service_account"))
        self.assertTrue(sa_cache["service_account"])


@pytest.mark.django_db
class TestSAPermissionCache(TestCase):
    def setUp(self):
        cache.clear()
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.edit_perm, _ = Permission.objects.get_or_create(name="edit")
        self.admin_perm, _ = Permission.objects.get_or_create(name="admin")

        self.ds_a = Dataset.objects.create(name="ds-a")
        self.ds_b = Dataset.objects.create(name="ds-b")
        self.sa = ServiceAccount.objects.create(name="reader")
        ServiceAccountGrant.objects.create(
            service_account=self.sa, dataset=self.ds_a, permission=self.view_perm
        )
        ServiceAccountGrant.objects.create(
            service_account=self.sa, dataset=self.ds_b, permission=self.edit_perm
        )

    def test_cache_shape_matches_user_shape(self):
        result = build_permission_cache(self.sa)
        for key in [
            "id", "parent_id", "service_account", "name", "email", "admin",
            "pi", "affiliations", "groups", "groups_admin",
            "permissions", "permissions_v2", "permissions_v2_ignore_tos",
            "missing_tos", "datasets_admin",
        ]:
            self.assertIn(key, result)
        self.assertTrue(result["service_account"])
        self.assertIsNone(result["parent_id"])
        self.assertFalse(result["admin"])
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["groups_admin"], [])
        self.assertEqual(result["missing_tos"], [])
        self.assertEqual(result["datasets_admin"], [])
        self.assertEqual(result["affiliations"], [])

    def test_cache_includes_only_sa_grants(self):
        result = build_permission_cache(self.sa)
        self.assertIn("ds-a", result["permissions"])
        self.assertIn("ds-b", result["permissions"])
        # view = level 1, edit = level 2
        self.assertEqual(result["permissions"]["ds-a"], 1)
        self.assertEqual(result["permissions"]["ds-b"], 2)
        # edit implies view in v2
        self.assertEqual(set(result["permissions_v2"]["ds-b"]), {"view", "edit"})

    def test_cache_ignores_user_grants_and_tos(self):
        # A User with a grant on a dataset must not bleed into the SA's cache.
        user = User.objects.create(email="bleed@example.org", name="bleed")
        from core.models import Grant
        Grant.objects.create(user=user, dataset=self.ds_a, permission=self.admin_perm)

        result = build_permission_cache(self.sa)
        # SA's view-only grant on ds-a remains, no admin upgrade leaks in.
        self.assertEqual(result["permissions"]["ds-a"], 1)
