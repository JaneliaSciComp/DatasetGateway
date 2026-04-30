"""Tests for the stable long-lived token endpoint."""

import pytest
from django.test import TestCase
from rest_framework.test import APIClient

from cave_api.oauth_views import DEFAULT_LONG_LIVED_TOKEN_DESCRIPTION
from core.models import APIKey, User


@pytest.mark.django_db
class TestLongLivedTokenView(TestCase):
    URL = "/api/v1/long_lived_token"

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create(email="alice@example.org", name="alice")
        # Auth token used to call the endpoint — distinct from the long-lived
        # token row that the endpoint manages.
        self.auth_key = APIKey.objects.create(
            user=self.user,
            description="OAuth login token",
            key="tok-auth-alice",
        )

    def _auth(self, key=None):
        return {"HTTP_AUTHORIZATION": f"Bearer {key or self.auth_key.key}"}

    def _default_tokens(self, user):
        return APIKey.objects.filter(
            user=user,
            description=DEFAULT_LONG_LIVED_TOKEN_DESCRIPTION,
            expires_at__isnull=True,
        )

    def test_unauthenticated_returns_401(self):
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 401)

    def test_first_call_creates_token_and_returns_it(self):
        self.assertEqual(self._default_tokens(self.user).count(), 0)

        resp = self.client.get(self.URL, **self._auth())
        self.assertEqual(resp.status_code, 200)

        body = resp.json()
        self.assertIn("token", body)
        self.assertTrue(body["token"])

        rows = list(self._default_tokens(self.user))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].key, body["token"])
        self.assertIsNone(rows[0].expires_at)

    def test_second_call_returns_same_token_and_does_not_create_a_new_row(self):
        first = self.client.get(self.URL, **self._auth()).json()["token"]
        second = self.client.get(self.URL, **self._auth()).json()["token"]
        self.assertEqual(first, second)
        self.assertEqual(self._default_tokens(self.user).count(), 1)

    def test_does_not_reuse_oauth_login_token(self):
        # The OAuth login token row in setUp() must not be selected as the
        # long-lived token, even though it belongs to the same user.
        resp = self.client.get(self.URL, **self._auth())
        self.assertEqual(resp.status_code, 200)
        self.assertNotEqual(resp.json()["token"], self.auth_key.key)

    def test_does_not_rotate_browser_session_token(self):
        self.client.get(self.URL, **self._auth())
        # The OAuth login token row used for authentication must remain.
        self.assertTrue(APIKey.objects.filter(pk=self.auth_key.pk).exists())

    def test_different_users_get_different_tokens(self):
        bob = User.objects.create(email="bob@example.org", name="bob")
        bob_auth = APIKey.objects.create(
            user=bob, description="OAuth login token", key="tok-auth-bob",
        )

        alice_token = self.client.get(self.URL, **self._auth()).json()["token"]
        bob_token = self.client.get(
            self.URL, **self._auth(bob_auth.key)
        ).json()["token"]

        self.assertNotEqual(alice_token, bob_token)
        self.assertEqual(self._default_tokens(self.user).count(), 1)
        self.assertEqual(self._default_tokens(bob).count(), 1)

    def test_create_token_still_creates_a_new_token_each_call(self):
        # Sanity check: the explicit POST /create_token contract is unchanged.
        before = APIKey.objects.filter(user=self.user).count()
        r1 = self.client.post("/api/v1/create_token", **self._auth())
        r2 = self.client.post("/api/v1/create_token", **self._auth())
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertNotEqual(r1.json(), r2.json())
        self.assertEqual(
            APIKey.objects.filter(user=self.user).count(), before + 2,
        )

    def test_oldest_matching_row_is_returned_when_duplicates_exist(self):
        # Pre-existing duplicate no-expiry rows (e.g. left over from the old
        # frontend behavior) should be reused: the oldest one wins.
        old = APIKey.objects.create(
            user=self.user,
            description=DEFAULT_LONG_LIVED_TOKEN_DESCRIPTION,
            expires_at=None,
            key="tok-old",
        )
        APIKey.objects.create(
            user=self.user,
            description=DEFAULT_LONG_LIVED_TOKEN_DESCRIPTION,
            expires_at=None,
            key="tok-new",
        )

        resp = self.client.get(self.URL, **self._auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["token"], old.key)
