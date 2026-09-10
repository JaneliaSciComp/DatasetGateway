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
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from django.db import close_old_connections
from django.core.cache import cache
from threading import Barrier, Thread
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from core.models import APIKey, AuditLog, ClientConsent, RegisteredClient, User
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
    assert match is not None, f"no json_script block {element_id!r}"
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


CODA_ORIGIN = "https://navis-org.github.io"


def _assert_api_headers(response):
    assert response["Cache-Control"] == "no-store, private"
    assert response["Referrer-Policy"] == "no-referrer"
    assert response["X-Frame-Options"] == "DENY"
    assert response["Cross-Origin-Opener-Policy"] == "unsafe-none"


def _assert_delivery(response, key, origin=CODA_ORIGIN):
    assert response.status_code == 200
    _assert_api_headers(response)
    html = response.content.decode()
    payload = _json_script_value(html, "ngauth-payload")
    # Do not let assertion rewriting print a delivered credential on failure.
    if payload != {"token": key.key}:
        pytest.fail("Delivery did not contain the expected key", pytrace=False)
    assert _json_script_value(html, "ngauth-origin") == origin
    assert "window.opener.postMessage(payload, origin);" in html
    if key.key in str(dict(response.headers)):
        pytest.fail("Credential appeared in response headers", pytrace=False)


class APILoginMixin:
    def setup_login(self):
        cache.clear()
        self.user = User.objects.create(email="browser@example.org", name="Browser User")
        self.browser_key = APIKey.objects.create(user=self.user)
        self.registered = RegisteredClient.objects.create(
            origin=CODA_ORIGIN, name="CODA", owner="Philipp Schlegel",
        )
        self.client = self.new_browser()

    def new_browser(self):
        client = Client(enforce_csrf_checks=True)
        client.cookies[settings.AUTH_COOKIE_NAME] = self.browser_key.key
        return client

    def get_consent(self, client=None, origin=CODA_ORIGIN):
        return (client or self.client).get("/login", {"origin": origin, "token": "api"})

    def post_decision(self, client=None, **changes):
        client = client or self.client
        data = {"origin": CODA_ORIGIN, "token": "api", "decision": "allow"}
        if "csrftoken" in client.cookies:
            data["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
        data.update(changes)
        return client.post("/login", data)


@pytest.mark.django_db
@override_settings(NGAUTH_ALLOWED_ORIGINS=ALLOWED)
class TestAPILogin(APILoginMixin, TestCase):
    def setUp(self):
        self.setup_login()

    def test_consent_get_and_repeated_get_head_never_mint(self):
        before = APIKey.objects.count()
        response = self.get_consent()
        self.assertEqual(response.status_code, 200)
        _assert_api_headers(response)
        html = response.content.decode()
        for text in (self.user.email, CODA_ORIGIN, self.registered.name, self.registered.owner,
                     'action="/login"', 'name="csrfmiddlewaretoken"', "7-day DatasetGateway API key",
                     "Don't ask again for this site"):
            self.assertIn(text, html)
        self.assertNotIn("ngauth-payload", html)
        self.get_consent()
        response = self.client.head("/login", {"origin": CODA_ORIGIN, "token": "api"})
        _assert_api_headers(response)
        self.assertEqual(APIKey.objects.count(), before)

    def test_allow_with_csrf_delivers_default_expiry_and_safe_audit(self):
        self.get_consent()
        now = timezone.now()
        response = self.post_decision()
        key = APIKey.objects.get(delegated_client=self.registered)
        self.assertEqual(key.user_id, self.user.pk)
        self.assertFalse(key.is_expired)
        self.assertEqual(key.description, f"Browser login token: CODA ({CODA_ORIGIN})")
        delta = (key.expires_at - now).total_seconds()
        self.assertAlmostEqual(delta, settings.AUTH_COOKIE_AGE, delta=5)
        _assert_delivery(response, key)
        self.assertFalse(ClientConsent.objects.exists())
        audit = AuditLog.objects.get(action="api_token_created")
        self.assertEqual(audit.after_state, {
            "client_name": "CODA", "client_origin": CODA_ORIGIN,
            "expires_at": key.expires_at.isoformat(), "key_id": key.pk,
        })
        if key.key in json.dumps(audit.after_state):
            self.fail("Audit leaked credential")
        # The delivered credential itself authenticates without a browser cookie.
        bearer_client = Client()
        identity = bearer_client.get("/api/v1/user/cache?service=neuprint",
                                     HTTP_AUTHORIZATION=f"Bearer {key.key}")
        self.assertEqual(identity.status_code, 200)
        self.assertEqual(identity.json()["email"], self.user.email)
        self.assertFalse(bearer_client.cookies)

    def test_csrf_less_allow_is_rejected(self):
        before = APIKey.objects.count()
        response = self.post_decision()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(APIKey.objects.count(), before)
        # Stock middleware 403s are outside the view-rendered header contract.
        # Also reject a POST missing the form token despite a CSRF cookie.
        self.get_consent()
        response = self.post_decision(csrfmiddlewaretoken="")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(APIKey.objects.count(), before)

    def test_cancel_closes_without_payload_or_mint(self):
        self.get_consent()
        before = APIKey.objects.count()
        response = self.post_decision(decision="cancel")
        self.assertEqual(response.status_code, 200)
        _assert_api_headers(response)
        html = response.content.decode()
        self.assertIn("window.close()", html)
        self.assertNotIn("postMessage", html)
        self.assertNotIn("ngauth-payload", html)
        self.assertEqual(APIKey.objects.count(), before)
        self.assertFalse(ClientConsent.objects.exists())

    def test_reject_invalid_token_origin_and_decision(self):
        self.get_consent()
        before = APIKey.objects.count()
        for token_kind in ("", "bogus"):
            for method in ("get", "post"):
                with self.subTest(token_kind=token_kind, method=method):
                    response = (self.client.get("/login", {"origin": CODA_ORIGIN, "token": token_kind})
                                if method == "get" else self.post_decision(token=token_kind))
                    self.assertEqual(response.status_code, 400)
                    _assert_api_headers(response)
        for origin in ("", "https://example.org/path", "file:///tmp/page", "null"):
            with self.subTest(origin=origin):
                for response in (self.get_consent(origin=origin), self.post_decision(origin=origin)):
                    self.assertEqual(response.status_code, 400)
                    _assert_api_headers(response)
        response = self.post_decision(decision="other")
        self.assertEqual(response.status_code, 400)
        _assert_api_headers(response)
        self.assertEqual(APIKey.objects.count(), before)

    def test_unregistered_disabled_and_lookalike_origins(self):
        self.get_consent()  # obtain CSRF token before testing failures
        before = APIKey.objects.count()
        for origin in ("https://clio-dev.janelia.org", CODA_ORIGIN + ".attacker.example"):
            for response in (self.get_consent(origin=origin), self.post_decision(origin=origin)):
                self.assertEqual(response.status_code, 200)
                _assert_api_headers(response)
                self.assertEqual(_json_script_value(response.content.decode(), "ngauth-payload"), "badorigin")
        # Disabling after the GET is revalidated on POST, including with consent.
        ClientConsent.objects.create(user=self.user, client=self.registered)
        self.registered.enabled = False
        self.registered.save(update_fields=["enabled"])
        for response in (self.get_consent(), self.post_decision()):
            _assert_api_headers(response)
            self.assertEqual(_json_script_value(response.content.decode(), "ngauth-payload"), "badorigin")
        self.registered.delete()
        for response in (self.get_consent(), self.post_decision()):
            _assert_api_headers(response)
            self.assertEqual(_json_script_value(response.content.decode(), "ngauth-payload"), "badorigin")
        self.assertEqual(APIKey.objects.count(), before)

    def test_origin_settled_before_cookie_lookup(self):
        self.get_consent()
        with patch("ngauth.views._api_mode_user", side_effect=AssertionError("Cookie was inspected")):
            self.assertEqual(self.get_consent(origin="invalid").status_code, 400)
            self.assertEqual(self.get_consent(origin="https://unknown.example.org").status_code, 200)
            self.assertEqual(self.post_decision(origin="invalid").status_code, 400)
            self.assertEqual(self.post_decision(origin="https://unknown.example.org").status_code, 200)

    def test_login_url_and_direct_auth_entry_keep_both_params(self):
        client = Client()
        response = self.get_consent(client)
        _assert_api_headers(response)
        html = response.content.decode()
        for text in (self.registered.name, self.registered.owner, CODA_ORIGIN):
            self.assertIn(text, html)
        login_url = response.context["login_url"]
        next_url = parse_qs(urlsplit(login_url).query)["next"][0]
        self.assertEqual(parse_qs(urlsplit(next_url).query), {"origin": [CODA_ORIGIN], "token": ["api"]})
        self.assertEqual(client.get(login_url).status_code, 302)
        self.assertEqual(client.session["oauth_next"], next_url)
        direct = Client()
        self.assertEqual(direct.get("/auth/login", {"next": next_url}).status_code, 302)
        self.assertEqual(direct.session["oauth_next"], next_url)
        from core.allauth_adapter import AccountAdapter
        from django.test import RequestFactory
        request = RequestFactory().get("/accounts/google/login/callback/")
        request.session = {"oauth_next": next_url}
        self.assertEqual(AccountAdapter().get_login_redirect_url(request), next_url)

    def test_disabled_expired_missing_and_delegated_cookies_cannot_grant(self):
        self.get_consent()
        before = APIKey.objects.count()
        # Disabled user is rechecked when Allow is posted.
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertNotIn("ngauth-payload", self.get_consent().content.decode())
        response = self.post_decision()
        self.assertEqual(response.status_code, 401)
        _assert_api_headers(response)
        self.user.is_active = True
        self.user.save(update_fields=["is_active"])
        APIKey.objects.filter(pk=self.browser_key.pk).update(expires_at=timezone.now())
        self.assertNotIn("ngauth-payload", self.get_consent().content.decode())
        self.assertEqual(self.post_decision().status_code, 401)
        self.client.cookies.pop(settings.AUTH_COOKIE_NAME)
        self.assertEqual(self.post_decision().status_code, 401)
        APIKey.objects.filter(pk=self.browser_key.pk).update(
            expires_at=timezone.now() + timezone.timedelta(days=1), delegated_client=self.registered,
        )
        self.client.cookies[settings.AUTH_COOKIE_NAME] = self.browser_key.key
        ClientConsent.objects.create(user=self.user, client=self.registered)
        self.assertNotIn("ngauth-payload", self.get_consent().content.decode())
        self.assertEqual(self.post_decision().status_code, 401)
        self.assertEqual(APIKey.objects.count(), before)

    def test_remember_redelivers_same_key_and_advances_last_used(self):
        self.get_consent()
        response = self.post_decision(remember="1")
        key = APIKey.objects.get(delegated_client=self.registered)
        _assert_delivery(response, key)
        self.assertEqual(ClientConsent.objects.filter(user=self.user, client=self.registered).count(), 1)
        self.assertEqual(AuditLog.objects.filter(action="client_consent_remembered").count(), 1)
        old_time = timezone.now() - timezone.timedelta(hours=1)
        APIKey.objects.filter(pk=key.pk).update(last_used=old_time)
        before = APIKey.objects.count()
        # No opener / foreign navigation is harmless: it never creates a row.
        response = self.get_consent()
        _assert_delivery(response, key)
        key.refresh_from_db()
        self.assertGreater(key.last_used, old_time)
        self.assertEqual(APIKey.objects.count(), before)
        self.assertEqual(AuditLog.objects.filter(action="api_token_redelivered").count(), 1)
        self.client.head("/login", {"origin": CODA_ORIGIN, "token": "api"})
        self.assertEqual(APIKey.objects.count(), before)

    def test_remembered_consent_without_live_key_is_preticked(self):
        ClientConsent.objects.create(user=self.user, client=self.registered)
        key = APIKey.objects.create(user=self.user, delegated_client=self.registered, expires_at=timezone.now())
        for revoked in (False, True):
            if revoked:
                key.delete()
            response = self.get_consent()
            self.assertTrue(response.context["remembered"])
            self.assertIn('name="remember" value="1" checked', response.content.decode())
            self.assertNotIn("ngauth-payload", response.content.decode())

    def test_remembered_key_is_most_recent_live_for_this_user_and_client(self):
        ClientConsent.objects.create(user=self.user, client=self.registered)
        APIKey.objects.create(user=self.user, delegated_client=self.registered)
        recent = APIKey.objects.create(user=self.user, delegated_client=self.registered)
        APIKey.objects.create(user=self.user, delegated_client=self.registered, expires_at=timezone.now())
        other = User.objects.create(email="other@example.org")
        APIKey.objects.create(user=other, delegated_client=self.registered)
        _assert_delivery(self.get_consent(), recent)
        second = RegisteredClient.objects.create(origin="https://another.example.org", name="Another", owner="Owner")
        response = self.get_consent(origin=second.origin)
        self.assertFalse(response.context["remembered"])
        self.assertNotIn("ngauth-payload", response.content.decode())

    def test_eleven_allows_evict_oldest_and_purge_only_this_client(self):
        self.get_consent()
        other_client = RegisteredClient.objects.create(origin="https://other.example.org", name="Other", owner="Owner")
        other_user = User.objects.create(email="other@example.org")
        protected = [self.browser_key.pk]
        for description in ("OAuth login token", "allauth login token", "permanent"):
            protected.append(APIKey.objects.create(user=self.user, description=description, expires_at=None).pk)
        protected.append(APIKey.objects.create(user=self.user, delegated_client=other_client).pk)
        protected.append(APIKey.objects.create(user=other_user, delegated_client=self.registered).pk)
        expired = APIKey.objects.create(user=self.user, delegated_client=self.registered, expires_at=timezone.now())
        first_pk = None
        for index in range(11):
            response = self.post_decision()
            self.assertEqual(response.status_code, 200)
            if index == 0:
                first_pk = APIKey.objects.filter(user=self.user, delegated_client=self.registered).get().pk
        keys = APIKey.objects.filter(user=self.user, delegated_client=self.registered)
        self.assertEqual(keys.count(), 10)
        self.assertFalse(APIKey.objects.filter(pk=first_pk).exists())
        self.assertFalse(APIKey.objects.filter(pk=expired.pk).exists())
        self.assertEqual(APIKey.objects.filter(pk__in=protected).count(), len(protected))

    def test_plain_ngauth_still_delivers_hmac_and_mints_no_api_key(self):
        before = APIKey.objects.count()
        response = self.client.get("/login", {"origin": "https://clio-dev.janelia.org"})
        payload = _json_script_value(response.content.decode(), "ngauth-payload")
        decoded = tokens.decode_user_token(_get_session_key(), payload["token"])
        self.assertEqual(decoded.user_id, self.user.email)
        self.assertEqual(APIKey.objects.count(), before)
        self.assertFalse(APIKey.objects.filter(key=payload["token"]).exists())


class TestConcurrentAPILogin(APILoginMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.setup_login()
        self.existing = []
        created = timezone.now() - timezone.timedelta(days=1)
        for index in range(10):
            key = APIKey.objects.create(user=self.user, delegated_client=self.registered)
            # Tied timestamps exercise the deterministic pk tie-breaker.
            APIKey.objects.filter(pk=key.pk).update(created=created)
            self.existing.append(key.pk)

    def test_two_allows_at_cap_evict_distinct_oldest_rows(self):
        clients = [self.new_browser(), self.new_browser()]
        for client in clients:
            self.get_consent(client)
        barrier = Barrier(2)
        statuses = []
        errors = []

        def allow(client):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = self.post_decision(client)
                statuses.append(response.status_code)
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                close_old_connections()

        threads = [Thread(target=allow, args=(client,)) for client in clients]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(statuses, [200, 200])
        self.assertEqual(APIKey.objects.filter(user=self.user, delegated_client=self.registered).count(), 10)
        self.assertFalse(APIKey.objects.filter(pk__in=self.existing[:2]).exists())
        self.assertEqual(APIKey.objects.filter(pk__in=self.existing[2:]).count(), 8)


@pytest.mark.django_db
def test_api_delivery_never_logs_the_key(caplog):
    browser = APILoginMixin()
    browser.setup_login()
    browser.get_consent()
    with caplog.at_level("DEBUG"):
        response = browser.post_decision(remember="1")
        assert response.status_code == 200
        redelivery = browser.get_consent()
        assert redelivery.status_code == 200
    key = APIKey.objects.get(delegated_client=browser.registered)
    if key.key in caplog.text:
        pytest.fail("Delivered credential was logged", pytrace=False)
    for audit in AuditLog.objects.all():
        if key.key in json.dumps([audit.before_state, audit.after_state]):
            pytest.fail("Delivered credential was audited", pytrace=False)
