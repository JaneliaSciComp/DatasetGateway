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


@pytest.mark.django_db
class TestLongLivedTokenRotateView(TestCase):
    URL = "/api/v1/long_lived_token/rotate"

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create(email="alice@example.org", name="alice")
        self.login_key = APIKey.objects.create(
            user=self.user, description="OAuth login token", key="tok-login-alice",
        )
        self.custom_key = APIKey.objects.create(
            user=self.user, description="my script", expires_at=None, key="tok-custom-alice",
        )
        self.default_key = APIKey.objects.create(
            user=self.user,
            description=DEFAULT_LONG_LIVED_TOKEN_DESCRIPTION,
            expires_at=None,
            key="tok-default-alice",
        )

    def _bearer(self, key):
        return {"HTTP_AUTHORIZATION": f"Bearer {key}"}

    def _default_tokens(self):
        return APIKey.objects.filter(
            user=self.user,
            description=DEFAULT_LONG_LIVED_TOKEN_DESCRIPTION,
            expires_at__isnull=True,
        )

    def _rotate(self, key=None):
        return self.client.post(self.URL, **self._bearer(key or self.default_key.key))

    def test_unauthenticated_returns_401(self):
        self.assertEqual(self.client.post(self.URL).status_code, 401)

    def test_get_is_not_allowed(self):
        resp = self.client.get(self.URL, **self._bearer(self.default_key.key))
        self.assertEqual(resp.status_code, 405)

    def test_rotate_replaces_default_token(self):
        resp = self._rotate()
        self.assertEqual(resp.status_code, 200)
        new = resp.json()["token"]
        self.assertTrue(new)
        self.assertNotEqual(new, self.default_key.key)

        self.assertEqual(list(self._default_tokens().values_list("key", flat=True)), [new])
        # The old token no longer authenticates; the new one does and is what
        # the get-or-create endpoint now returns.
        old_resp = self.client.get(
            "/api/v1/long_lived_token", **self._bearer(self.default_key.key)
        )
        self.assertEqual(old_resp.status_code, 401)
        new_resp = self.client.get("/api/v1/long_lived_token", **self._bearer(new))
        self.assertEqual(new_resp.status_code, 200)
        self.assertEqual(new_resp.json()["token"], new)

    def test_rotate_leaves_other_tokens_alone(self):
        self.assertEqual(self._rotate().status_code, 200)
        self.assertTrue(APIKey.objects.filter(pk=self.login_key.pk).exists())
        self.assertTrue(APIKey.objects.filter(pk=self.custom_key.pk).exists())
        self.assertFalse(APIKey.objects.filter(pk=self.default_key.pk).exists())

    def test_rotate_removes_duplicate_default_rows(self):
        APIKey.objects.create(
            user=self.user,
            description=DEFAULT_LONG_LIVED_TOKEN_DESCRIPTION,
            expires_at=None,
            key="tok-default-dup",
        )
        self.assertEqual(self._rotate().status_code, 200)
        self.assertEqual(self._default_tokens().count(), 1)
        self.assertFalse(APIKey.objects.filter(key="tok-default-dup").exists())

    def test_non_default_bearer_is_refused(self):
        # The bearer must be the token being rotated, so a proxy that evicts
        # the presented token evicts exactly the revoked one.
        for key in (self.login_key.key, self.custom_key.key):
            resp = self._rotate(key)
            self.assertEqual(resp.status_code, 403, key)
        self.assertEqual(
            list(self._default_tokens().values_list("key", flat=True)),
            [self.default_key.key],
        )

    def test_cookie_only_authentication_is_refused(self):
        self.client.cookies["dsg_token"] = self.login_key.key
        resp = self.client.post(self.URL)
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(APIKey.objects.filter(pk=self.default_key.pk).exists())

    def test_cookie_plus_default_bearer_is_refused(self):
        # The cookie wins authentication, so the header did not authenticate.
        self.client.cookies["dsg_token"] = self.login_key.key
        resp = self._rotate()
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(APIKey.objects.filter(pk=self.default_key.pk).exists())

    def test_second_rotation_of_same_token_is_stale(self):
        # Two requests authenticated with the same token before either
        # rotated: only the first may succeed, and its replacement survives.
        from cave_api.oauth_views import StaleTokenRotation, rotate_default_long_lived_token

        first, _ = rotate_default_long_lived_token(self.user, self.default_key.key)
        with self.assertRaises(StaleTokenRotation):
            rotate_default_long_lived_token(self.user, self.default_key.key)
        self.assertEqual(
            list(self._default_tokens().values_list("key", flat=True)), [first.key],
        )

    def test_stale_rotation_returns_409(self):
        from unittest import mock

        from cave_api.oauth_views import StaleTokenRotation

        with mock.patch(
            "cave_api.oauth_views.rotate_default_long_lived_token",
            side_effect=StaleTokenRotation,
        ):
            resp = self._rotate()
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(APIKey.objects.filter(pk=self.default_key.pk).exists())

    def test_rotation_is_audited(self):
        from core.models import AuditLog

        resp = self._rotate()
        new_key = APIKey.objects.get(key=resp.json()["token"])
        entry = AuditLog.objects.get(action="api_token_rotated")
        self.assertEqual(str(entry.target_id), str(new_key.pk))
        self.assertEqual(entry.before_state, {"revoked_token_ids": [self.default_key.pk]})
        self.assertNotIn(self.default_key.key, str(entry.before_state) + str(entry.after_state))
        self.assertNotIn(new_key.key, str(entry.before_state) + str(entry.after_state))

    def test_delegated_key_is_refused(self):
        from core.models import RegisteredClient

        site = RegisteredClient.objects.create(
            origin="https://navis-org.github.io", name="CODA", owner="Philipp",
        )
        delegated = APIKey.objects.create(user=self.user, delegated_client=site)
        before = APIKey.objects.count()
        resp = self._rotate(delegated.key)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(APIKey.objects.count(), before)
        self.assertTrue(APIKey.objects.filter(pk=self.default_key.pk).exists())

    def test_service_account_is_refused(self):
        from core.models import ServiceAccount, ServiceAccountToken

        sa = ServiceAccount.objects.create(name="ci-bot")
        ServiceAccountToken.objects.create(service_account=sa, description="ci", key="tok-sa")
        resp = self._rotate("tok-sa")
        self.assertEqual(resp.status_code, 403)
