"""OAuth redirect, logout, and per-device browser session tests."""

from datetime import timedelta
from threading import Barrier, Thread
from unittest.mock import patch

import pytest
from django.conf import settings
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from cave_api.oauth_views import (
    DEFAULT_LONG_LIVED_TOKEN_DESCRIPTION,
    LOGIN_TOKEN_DESCRIPTION,
    OAuth2CallbackView,
)
from core.models import APIKey, User


VALID_REDIRECT = "https://service.janelia.org/after-login?dataset=fish2"
INVALID_REDIRECTS = (
    "https://example.org/after-login",
    "https://evil-janelia.org/after-login",
    "https://janelia.org.evil.example/after-login",
    "https://user:password@service.janelia.org/after-login",
    "//service.janelia.org/after-login",
    "https://service.janelia.org/after-login\nX-Test: bad",
)


class OAuthCallbackMixin:
    callback_url = "/api/v1/oauth2callback"
    user_info = {
        "email": "alice@example.org",
        "sub": "google-alice",
        "name": "Alice",
        "picture": "https://images.example.org/alice.png",
    }

    def prime_callback(self, client, state, redirect=VALID_REDIRECT):
        session = client.session
        session["oauth_state"] = state
        session["oauth_redirect"] = redirect
        session.save()

    def callback(self, client, state):
        self.prime_callback(client, state)
        with (
            patch.object(
                OAuth2CallbackView,
                "_exchange_code",
                return_value={"id_token": "verified-id-token"},
            ),
            patch.object(
                OAuth2CallbackView,
                "_verify_id_token",
                return_value=self.user_info,
            ),
        ):
            return client.get(
                self.callback_url,
                {"code": f"code-{state}", "state": state},
            )


@pytest.mark.django_db
class TestOAuthRedirectValidation(TestCase):
    authorize_url = "/api/v1/authorize"
    logout_url = "/api/v1/logout"

    def setUp(self):
        self.client = APIClient()

    def test_valid_exact_and_subdomain_redirects_are_preserved(self):
        redirects = (
            "http://janelia.org/path",
            "https://service.janelia.org/path?x=1#result",
        )

        for redirect in redirects:
            with self.subTest(redirect=redirect):
                response = self.client.get(
                    self.authorize_url,
                    {"redirect": redirect},
                    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.client.session["oauth_redirect"], redirect)

                response = self.client.get(
                    self.logout_url,
                    {"redirect": redirect},
                )
                self.assertRedirects(
                    response,
                    redirect,
                    fetch_redirect_response=False,
                )

    def test_invalid_redirects_fall_back_on_authorize_and_logout(self):
        for redirect in INVALID_REDIRECTS:
            with self.subTest(redirect=redirect):
                response = self.client.get(
                    self.authorize_url,
                    {"redirect": redirect},
                    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.client.session["oauth_redirect"], "/")

                response = self.client.get(
                    self.logout_url,
                    {"redirect": redirect},
                )
                self.assertRedirects(response, "/", fetch_redirect_response=False)


@pytest.mark.django_db
class TestLogoutView(TestCase):
    url = "/api/v1/logout"

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create(email="logout@example.org", name="Logout")

    def assert_cookie_cleared(self, response, domain=""):
        cookie = response.cookies.get(settings.AUTH_COOKIE_NAME)
        self.assertIsNotNone(cookie)
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(cookie["domain"], domain)
        self.assertEqual(cookie["max-age"], 0)
        self.assertEqual(cookie["expires"], "Thu, 01 Jan 1970 00:00:00 GMT")

    @override_settings(AUTH_COOKIE_DOMAIN=".janelia.org")
    def test_redirect_logout_deletes_login_token_and_clears_matching_cookie(self):
        login_token = APIKey.objects.create(
            user=self.user,
            key="logout-login-token",
            description=LOGIN_TOKEN_DESCRIPTION,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.client.cookies[settings.AUTH_COOKIE_NAME] = login_token.key

        response = self.client.get(self.url, {"redirect": VALID_REDIRECT})

        self.assertRedirects(
            response,
            VALID_REDIRECT,
            fetch_redirect_response=False,
        )
        self.assertFalse(APIKey.objects.filter(pk=login_token.pk).exists())
        self.assert_cookie_cleared(response, domain=".janelia.org")

    def test_post_redirect_logout_is_supported(self):
        response = self.client.post(f"{self.url}?redirect={VALID_REDIRECT}")
        self.assertRedirects(
            response,
            VALID_REDIRECT,
            fetch_redirect_response=False,
        )
        self.assert_cookie_cleared(response)

    def test_no_redirect_preserves_legacy_json_response(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'{"status":"logged out"}')
        self.assert_cookie_cleared(response)

    def test_invalid_cookie_states_always_clear_and_redirect(self):
        expired = APIKey.objects.create(
            user=self.user,
            key="expired-login-token",
            description=LOGIN_TOKEN_DESCRIPTION,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        deleted = APIKey.objects.create(
            user=self.user,
            key="already-deleted-login-token",
            description=LOGIN_TOKEN_DESCRIPTION,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        deleted_key = deleted.key
        deleted.delete()

        cases = (
            ("missing", None),
            ("expired", expired.key),
            ("already deleted", deleted_key),
            ("malformed", "not-a-real-token"),
        )
        for label, token in cases:
            with self.subTest(label=label):
                self.client.cookies.clear()
                if token is not None:
                    self.client.cookies[settings.AUTH_COOKIE_NAME] = token

                response = self.client.get(
                    self.url,
                    {"redirect": VALID_REDIRECT},
                )

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, VALID_REDIRECT)
                self.assert_cookie_cleared(response)

        self.assertFalse(APIKey.objects.filter(pk=expired.pk).exists())

    def test_presented_long_lived_script_token_is_never_deleted(self):
        script_token = APIKey.objects.create(
            user=self.user,
            key="long-lived-script-token",
            description=DEFAULT_LONG_LIVED_TOKEN_DESCRIPTION,
            expires_at=None,
        )
        self.client.cookies[settings.AUTH_COOKIE_NAME] = script_token.key

        response = self.client.get(self.url, {"redirect": VALID_REDIRECT})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(APIKey.objects.filter(pk=script_token.pk).exists())
        self.assert_cookie_cleared(response)


@pytest.mark.django_db
class TestOAuthCallbackSessions(OAuthCallbackMixin, TestCase):
    def setUp(self):
        self.client = APIClient()

    @override_settings(
        AUTH_COOKIE_AGE=1234,
        AUTH_COOKIE_DOMAIN=".janelia.org",
        AUTH_COOKIE_SECURE=True,
    )
    def test_issuance_cookie_and_database_expiry_share_auth_cookie_age(self):
        now = timezone.now().replace(microsecond=0)
        with patch("cave_api.oauth_views.timezone.now", return_value=now):
            response = self.callback(self.client, "ttl-state")

        self.assertEqual(response.status_code, 302)
        api_key = APIKey.objects.get(
            key=response.cookies[settings.AUTH_COOKIE_NAME].value
        )
        self.assertEqual(api_key.expires_at, now + timedelta(seconds=1234))

        cookie = response.cookies[settings.AUTH_COOKIE_NAME]
        self.assertEqual(cookie["domain"], ".janelia.org")
        self.assertEqual(cookie["path"], "/")
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertTrue(cookie["secure"])
        self.assertEqual(cookie["max-age"], 1234)

    @override_settings(AUTH_COOKIE_AGE=2345)
    def test_model_default_expiry_also_uses_auth_cookie_age(self):
        now = timezone.now().replace(microsecond=0)
        user = User.objects.create(email="default@example.org", name="Default")
        with patch("core.models.timezone.now", return_value=now):
            api_key = APIKey.objects.create(user=user, description="browser token")
        self.assertEqual(api_key.expires_at, now + timedelta(seconds=2345))

    def test_second_browser_survives_login_relogin_and_logout(self):
        browser_a = APIClient()
        browser_b = APIClient()

        response_a = self.callback(browser_a, "browser-a")
        token_a = response_a.cookies[settings.AUTH_COOKIE_NAME].value
        response_b = self.callback(browser_b, "browser-b")
        token_b = response_b.cookies[settings.AUTH_COOKIE_NAME].value

        self.assertNotEqual(token_a, token_b)
        self.assertTrue(APIKey.objects.filter(key=token_a).exists())
        self.assertTrue(APIKey.objects.filter(key=token_b).exists())
        self.assertEqual(browser_a.get("/api/v1/user/cache").status_code, 200)
        self.assertEqual(browser_b.get("/api/v1/user/cache").status_code, 200)

        response_b2 = self.callback(browser_b, "browser-b-again")
        token_b2 = response_b2.cookies[settings.AUTH_COOKIE_NAME].value

        self.assertNotEqual(token_b, token_b2)
        self.assertTrue(APIKey.objects.filter(key=token_a).exists())
        self.assertFalse(APIKey.objects.filter(key=token_b).exists())
        self.assertTrue(APIKey.objects.filter(key=token_b2).exists())

        logout = browser_b.get(
            "/api/v1/logout",
            {"redirect": VALID_REDIRECT},
        )
        self.assertEqual(logout.status_code, 302)
        self.assertFalse(APIKey.objects.filter(key=token_b2).exists())
        self.assertTrue(APIKey.objects.filter(key=token_a).exists())
        self.assertEqual(browser_a.get("/api/v1/user/cache").status_code, 200)

    def test_presented_login_token_for_another_user_is_not_rotated(self):
        other_user = User.objects.create(email="bob@example.org", name="Bob")
        other_token = APIKey.objects.create(
            user=other_user,
            key="bob-browser-token",
            description=LOGIN_TOKEN_DESCRIPTION,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.client.cookies[settings.AUTH_COOKIE_NAME] = other_token.key

        response = self.callback(self.client, "foreign-token-state")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(APIKey.objects.filter(pk=other_token.pk).exists())
        alice = User.objects.get(email=self.user_info["email"])
        self.assertEqual(
            APIKey.objects.filter(
                user=alice,
                description=LOGIN_TOKEN_DESCRIPTION,
            ).count(),
            1,
        )

    def test_expired_login_tokens_are_garbage_collected_only(self):
        user = User.objects.create(email=self.user_info["email"], name="Alice")
        expired = APIKey.objects.create(
            user=user,
            key="expired-oauth-token",
            description=LOGIN_TOKEN_DESCRIPTION,
            expires_at=timezone.now(),
        )
        live = APIKey.objects.create(
            user=user,
            key="live-oauth-token",
            description=LOGIN_TOKEN_DESCRIPTION,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        reserved_rows = [
            APIKey.objects.create(
                user=user,
                key=f"reserved-token-{index}",
                description=DEFAULT_LONG_LIVED_TOKEN_DESCRIPTION,
                expires_at=None,
            )
            for index in range(2)
        ]

        response = self.callback(self.client, "gc-state")

        self.assertEqual(response.status_code, 302)
        self.assertFalse(APIKey.objects.filter(pk=expired.pk).exists())
        self.assertTrue(APIKey.objects.filter(pk=live.pk).exists())
        for row in reserved_rows:
            self.assertTrue(APIKey.objects.filter(pk=row.pk).exists())

    def test_cap_evicts_as_many_oldest_tokens_as_needed(self):
        user = User.objects.create(email=self.user_info["email"], name="Alice")
        base_time = timezone.now() - timedelta(days=1)
        existing = []
        for index in range(12):
            token = APIKey.objects.create(
                user=user,
                key=f"cap-token-{index}",
                description=LOGIN_TOKEN_DESCRIPTION,
                expires_at=timezone.now() + timedelta(days=1),
            )
            APIKey.objects.filter(pk=token.pk).update(
                created=base_time + timedelta(seconds=index)
            )
            existing.append(token)

        response = self.callback(self.client, "cap-state")

        self.assertEqual(response.status_code, 302)
        login_tokens = APIKey.objects.filter(
            user=user,
            description=LOGIN_TOKEN_DESCRIPTION,
        )
        self.assertEqual(login_tokens.count(), 10)
        self.assertFalse(
            APIKey.objects.filter(pk__in=[token.pk for token in existing[:3]]).exists()
        )
        self.assertEqual(
            APIKey.objects.filter(
                pk__in=[token.pk for token in existing[3:]]
            ).count(),
            9,
        )


class TestConcurrentOAuthCallbacks(OAuthCallbackMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = User.objects.create(
            email=self.user_info["email"],
            name="Alice",
            google_sub=self.user_info["sub"],
        )
        base_time = timezone.now() - timedelta(days=1)
        self.existing = []
        for index in range(10):
            token = APIKey.objects.create(
                user=self.user,
                key=f"concurrent-token-{index}",
                description=LOGIN_TOKEN_DESCRIPTION,
                expires_at=timezone.now() + timedelta(days=1),
            )
            APIKey.objects.filter(pk=token.pk).update(
                created=base_time + timedelta(seconds=index)
            )
            self.existing.append(token)

    def test_two_callbacks_at_cap_evict_distinct_oldest_rows(self):
        clients = [APIClient(), APIClient()]
        states = ["concurrent-a", "concurrent-b"]
        for client, state in zip(clients, states, strict=True):
            self.prime_callback(client, state)

        barrier = Barrier(2)
        responses = []
        errors = []

        def run_callback(client, state):
            close_old_connections()
            try:
                barrier.wait()
                responses.append(
                    client.get(
                        self.callback_url,
                        {"code": f"code-{state}", "state": state},
                    )
                )
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        with (
            patch.object(
                OAuth2CallbackView,
                "_exchange_code",
                return_value={"id_token": "verified-id-token"},
            ),
            patch.object(
                OAuth2CallbackView,
                "_verify_id_token",
                return_value=self.user_info,
            ),
        ):
            threads = [
                Thread(target=run_callback, args=(client, state))
                for client, state in zip(clients, states, strict=True)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual([response.status_code for response in responses], [302, 302])

        login_tokens = APIKey.objects.filter(
            user=self.user,
            description=LOGIN_TOKEN_DESCRIPTION,
        )
        self.assertEqual(login_tokens.count(), 10)
        self.assertFalse(
            APIKey.objects.filter(
                pk__in=[self.existing[0].pk, self.existing[1].pk]
            ).exists()
        )
        self.assertEqual(
            APIKey.objects.filter(
                pk__in=[token.pk for token in self.existing[2:]]
            ).count(),
            8,
        )
