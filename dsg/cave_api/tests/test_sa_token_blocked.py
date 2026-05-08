"""Verify ServiceAccount tokens cannot reach user-only oauth endpoints."""

import pytest
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import APIKey, ServiceAccount, ServiceAccountToken, User


@pytest.mark.django_db
class TestSATokenBlockedFromUserEndpoints(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.sa = ServiceAccount.objects.create(name="ci-bot")
        self.sa_token = ServiceAccountToken.objects.create(
            service_account=self.sa, description="ci", key="tok-sa-block",
        )
        # Real user used as a control for the same endpoints.
        self.user = User.objects.create(email="alice@example.org", name="alice")
        self.user_key = APIKey.objects.create(user=self.user, key="tok-user-ok")

    def _sa_auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.sa_token.key}"}

    def _user_auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.user_key.key}"}

    def test_create_token_rejects_sa(self):
        before = APIKey.objects.count()
        resp = self.client.post("/api/v1/create_token", **self._sa_auth())
        self.assertEqual(resp.status_code, 403)
        # Make sure no APIKey row was created with sa.pk as user_id.
        self.assertEqual(APIKey.objects.count(), before)

    def test_long_lived_token_rejects_sa(self):
        resp = self.client.get("/api/v1/long_lived_token", **self._sa_auth())
        self.assertEqual(resp.status_code, 403)

    def test_user_tokens_rejects_sa(self):
        resp = self.client.get("/api/v1/user/token", **self._sa_auth())
        self.assertEqual(resp.status_code, 403)

    def test_refresh_token_rejects_sa(self):
        resp = self.client.get("/api/v1/refresh_token", **self._sa_auth())
        self.assertEqual(resp.status_code, 403)

    def test_user_can_still_use_create_token(self):
        # Sanity: control path for human user is unaffected.
        resp = self.client.post("/api/v1/create_token", **self._user_auth())
        self.assertEqual(resp.status_code, 200)
