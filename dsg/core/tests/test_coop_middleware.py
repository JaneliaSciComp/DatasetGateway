"""COOP coverage for popup responses and paths retaining the global policy."""

from django.conf import settings
from django.test import TestCase, override_settings

from core.models import APIKey, User
from ngauth.tests.test_login_handshake import _json_script_value


@override_settings(NGAUTH_ALLOWED_ORIGINS=r"^https://clio-dev\.janelia\.org$")
class TestPopupOpenerPolicyMiddleware(TestCase):
    def setUp(self):
        self.user = User.objects.create(email="user@example.org", name="User")
        self.key = APIKey.objects.create(user=self.user, key="tok-coop")

    def _login(self):
        self.client.cookies[settings.AUTH_COOKIE_NAME] = self.key.key

    def _assert_policy(self, response, status, policy):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response["Cross-Origin-Opener-Policy"], policy)

    def test_allowed_origin_logged_in(self):
        self._login()
        response = self.client.get("/login", {"origin": "https://clio-dev.janelia.org"})
        self._assert_policy(response, 200, "unsafe-none")
        payload = _json_script_value(response.content.decode(), "ngauth-payload")
        self.assertIn("token", payload)

    def test_allowed_origin_logged_out(self):
        response = self.client.get("/login", {"origin": "https://clio-dev.janelia.org"})
        self._assert_policy(response, 200, "unsafe-none")
        self.assertNotContains(response, "ngauth-payload")

    def test_disallowed_origin(self):
        for logged_in in (False, True):
            with self.subTest(logged_in=logged_in):
                if logged_in:
                    self._login()
                response = self.client.get(
                    "/login", {"origin": "https://evil.example.com"}
                )
                self._assert_policy(response, 200, "unsafe-none")
                self.assertEqual(
                    _json_script_value(response.content.decode(), "ngauth-payload"),
                    "badorigin",
                )

    def test_no_origin(self):
        for logged_in in (False, True):
            with self.subTest(logged_in=logged_in):
                if logged_in:
                    self._login()
                response = self.client.get("/login")
                self._assert_policy(response, 200, "unsafe-none")
                self.assertNotContains(response, "ngauth-payload")

    def test_auth_login_redirect(self):
        response = self.client.get("/auth/login")
        self._assert_policy(response, 302, "unsafe-none")
        self.assertEqual(response["Location"], "/accounts/google/login/")

    def test_google_login_confirmation(self):
        response = self.client.get("/accounts/google/login/")
        self._assert_policy(response, 200, "unsafe-none")

    def test_google_login_slash_redirect(self):
        response = self.client.get("/accounts/google/login")
        self._assert_policy(response, 301, "unsafe-none")
        self.assertEqual(response["Location"], "/accounts/google/login/")

    def test_google_callback_without_code(self):
        response = self.client.get("/accounts/google/login/callback/")
        self._assert_policy(response, 401, "unsafe-none")

    def test_third_party_login_cancelled(self):
        response = self.client.get("/accounts/3rdparty/login/cancelled/")
        self._assert_policy(response, 200, "unsafe-none")

    def test_third_party_login_error(self):
        response = self.client.get("/accounts/3rdparty/login/error/")
        self._assert_policy(response, 401, "unsafe-none")

    def test_inactive_account(self):
        response = self.client.get("/accounts/inactive/")
        self._assert_policy(response, 200, "unsafe-none")

    def test_social_login_cancelled_redirect(self):
        response = self.client.get("/accounts/social/login/cancelled/")
        self._assert_policy(response, 301, "unsafe-none")

    def test_health(self):
        self._assert_policy(self.client.get("/health"), 200, "same-origin")

    def test_token_without_cookie(self):
        response = self.client.post(
            "/token", HTTP_ORIGIN="https://clio-dev.janelia.org"
        )
        self._assert_policy(response, 401, "same-origin")
        self.assertEqual(response["X-Frame-Options"], "deny")

    def test_gcs_token_empty_json(self):
        response = self.client.post(
            "/gcs_token",
            data="{}",
            content_type="application/json",
            HTTP_ORIGIN="https://clio-dev.janelia.org",
        )
        self._assert_policy(response, 400, "same-origin")

    # Rendering the admin login must not depend on running collectstatic first.
    @override_settings(STORAGES={
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_admin_login(self):
        self._assert_policy(self.client.get("/admin/login/"), 200, "same-origin")

    def test_web_datasets_redirect(self):
        response = self.client.get("/web/datasets")
        self._assert_policy(response, 302, "same-origin")
        self.assertEqual(response["Location"], "/auth/login?next=/web/datasets")

    def test_legacy_api_whoami(self):
        self._assert_policy(self.client.get("/api/v1/whoami"), 401, "same-origin")

    def test_native_api_datasets(self):
        response = self.client.get("/api/dsg/v1/datasets")
        self._assert_policy(response, 401, "same-origin")

    def test_index(self):
        self._assert_policy(self.client.get("/"), 200, "same-origin")

    def test_success(self):
        self._assert_policy(self.client.get("/success"), 200, "same-origin")

    def test_login_suffix_boundary(self):
        self._assert_policy(self.client.get("/loginx"), 404, "same-origin")

    def test_login_slash_boundary(self):
        self._assert_policy(self.client.get("/login/"), 404, "same-origin")

    def test_auth_login_suffix_boundary(self):
        self._assert_policy(self.client.get("/auth/loginx"), 404, "same-origin")

    def test_accounts_without_slash_boundary(self):
        self._assert_policy(self.client.get("/accounts"), 404, "same-origin")
