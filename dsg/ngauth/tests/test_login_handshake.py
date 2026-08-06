"""Tests for the ngauth popup login handshake on GET /login.

Neuroglancer's ngauth client (``waitForLogin`` in
``datasource/ngauth/credentials_provider.ts``) opens ``/login?origin=<client
origin>`` in a popup and waits for a ``postMessage`` from this server carrying
either ``{"token": …}`` or the literal string ``"badorigin"``. Without that
handshake the client hangs forever on the 401 path of ``POST /token``.
"""

import json
import re

import pytest
from django.conf import settings
from django.test import TestCase, override_settings

from core.models import APIKey, User
from ngauth import tokens
from ngauth.views import _get_session_key

ALLOWED = r"^https://clio-dev\.janelia\.org$"


def _json_script_value(html, element_id):
    """Pull a value back out of a Django ``json_script`` block."""
    match = re.search(
        rf'<script id="{element_id}" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match is not None, f"no json_script block {element_id!r} in:\n{html}"
    return json.loads(match.group(1))


@pytest.mark.django_db
@override_settings(NGAUTH_ALLOWED_ORIGINS=ALLOWED)
class TestLoginHandshake(TestCase):
    def setUp(self):
        self.user = User.objects.create(email="user@example.org", name="User")
        self.key = APIKey.objects.create(user=self.user, key="tok-login")

    def _login(self):
        self.client.cookies[settings.AUTH_COOKIE_NAME] = self.key.key

    def test_allowed_origin_logged_in_posts_a_usable_token(self):
        self._login()
        resp = self.client.get("/login", {"origin": "https://clio-dev.janelia.org"})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()

        self.assertEqual(
            _json_script_value(html, "ngauth-origin"), "https://clio-dev.janelia.org"
        )
        payload = _json_script_value(html, "ngauth-payload")
        self.assertIn("token", payload)
        self.assertIn("window.opener.postMessage(payload, origin)", html)

        # The posted token must be exactly what /gcs_token will accept.
        decoded = tokens.decode_user_token(_get_session_key(), payload["token"])
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.user_id, "user@example.org")

    def test_disallowed_origin_posts_badorigin(self):
        self._login()
        resp = self.client.get("/login", {"origin": "https://evil.example.com"})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertEqual(_json_script_value(html, "ngauth-payload"), "badorigin")
        self.assertNotIn("token", _json_script_value(html, "ngauth-payload"))

    def test_disabled_user_does_not_get_a_token(self):
        self.user.is_active = False
        self.user.save()
        self._login()
        resp = self.client.get("/login", {"origin": "https://clio-dev.janelia.org"})
        self.assertEqual(resp.status_code, 200)
        # Falls through to the status page, which carries no payload at all.
        self.assertNotIn("ngauth-payload", resp.content.decode())

    def test_not_logged_in_returns_to_the_handshake_after_oauth(self):
        resp = self.client.get("/login", {"origin": "https://clio-dev.janelia.org"})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn("ngauth-payload", html)
        # The login link must round-trip back to /login with the origin intact,
        # otherwise the popup lands on /web/datasets and never posts a token.
        self.assertIn(
            "/auth/login?next=%2Flogin%3Forigin%3Dhttps%253A%252F%252Fclio-dev.janelia.org",
            html,
        )

    def test_malformed_origin_is_rejected(self):
        self._login()
        bad_origins = [
            "notaurl",
            "https://host/path",
            'https://a"b',
            "https://host:99999",          # port the URL parser rejects
            "https://user:pw@host",        # embedded credentials
            "https://host.",               # trailing dot
            "https://-host",               # malformed DNS label
            "https://host?q=1",            # query
            "https://host#f",              # fragment
            "ftp://host",                  # non-HTTP scheme
            " https://host",               # leading whitespace
        ]
        for bad in bad_origins:
            with self.subTest(origin=bad):
                resp = self.client.get("/login", {"origin": bad})
                self.assertEqual(resp.status_code, 400)

    def test_suffix_lookalike_origin_is_not_admitted(self):
        """An unanchored-looking allowlist must not admit a suffix attack."""
        self._login()
        resp = self.client.get(
            "/login", {"origin": "https://clio-dev.janelia.org.attacker.example"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_json_script_value(resp.content.decode(), "ngauth-payload"), "badorigin")

    def test_no_origin_renders_the_plain_status_page(self):
        self._login()
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn("ngauth-payload", html)
        self.assertIn("user@example.org", html)


@pytest.mark.django_db
@override_settings(NGAUTH_ALLOWED_ORIGINS=r"https://clio-dev\.janelia\.org")
class TestUnanchoredAllowlistIsFullMatched(TestCase):
    """`_is_origin_allowed` must fullmatch, so a pattern without ^…$ is still safe."""

    def setUp(self):
        self.user = User.objects.create(email="user@example.org", name="User")
        self.key = APIKey.objects.create(user=self.user, key="tok-anchor")
        self.client.cookies[settings.AUTH_COOKIE_NAME] = self.key.key

    def test_exact_origin_still_allowed(self):
        resp = self.client.get("/login", {"origin": "https://clio-dev.janelia.org"})
        payload = _json_script_value(resp.content.decode(), "ngauth-payload")
        self.assertIn("token", payload)

    def test_suffix_origin_denied(self):
        resp = self.client.get(
            "/login", {"origin": "https://clio-dev.janelia.org.attacker.example"}
        )
        self.assertEqual(
            _json_script_value(resp.content.decode(), "ngauth-payload"), "badorigin"
        )

    def test_disallowed_origin_gets_no_token_from_gcs_token(self):
        """A simple POST to /gcs_token must be origin-gated, not just OPTIONS."""
        resp = self.client.post(
            "/gcs_token",
            data=json.dumps({"token": "irrelevant", "bucket": "some-bucket"}),
            content_type="application/json",
            HTTP_ORIGIN="https://clio-dev.janelia.org.attacker.example",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", resp.headers)


@pytest.mark.django_db
class TestAuthLoginNextValidation(TestCase):
    def test_relative_next_is_kept(self):
        resp = self.client.get("/auth/login", {"next": "/login?origin=https://x.org"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.session["oauth_next"], "/login?origin=https://x.org")

    def test_absolute_next_is_dropped(self):
        resp = self.client.get("/auth/login", {"next": "https://evil.example.com/steal"})
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("oauth_next", self.client.session)

    def test_protocol_relative_next_is_dropped(self):
        resp = self.client.get("/auth/login", {"next": "//evil.example.com/steal"})
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("oauth_next", self.client.session)

    def test_rejected_next_clears_a_stale_session_value(self):
        session = self.client.session
        session["oauth_next"] = "/login?origin=https://clio-dev.janelia.org"
        session.save()
        self.client.get("/auth/login", {"next": "https://evil.example.com/steal"})
        self.assertNotIn("oauth_next", self.client.session)


@pytest.mark.django_db
class TestLoginRedirectSinkValidation(TestCase):
    """The adapter is the point where oauth_next becomes an actual redirect."""

    def _redirect_for(self, stored):
        from django.test import RequestFactory

        from core.allauth_adapter import AccountAdapter

        request = RequestFactory().get("/accounts/google/login/callback/")
        request.session = {"oauth_next": stored} if stored is not None else {}
        return AccountAdapter().get_login_redirect_url(request)

    def test_relative_target_is_honored(self):
        self.assertEqual(self._redirect_for("/login?origin=https://x.org"),
                         "/login?origin=https://x.org")

    def test_stale_absolute_target_falls_back(self):
        self.assertEqual(self._redirect_for("https://evil.example.com/steal"),
                         "/web/datasets")

    def test_stale_protocol_relative_target_falls_back(self):
        self.assertEqual(self._redirect_for("//evil.example.com/steal"), "/web/datasets")

    def test_missing_target_falls_back(self):
        self.assertEqual(self._redirect_for(None), "/web/datasets")
