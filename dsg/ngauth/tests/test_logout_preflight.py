"""Session logout and GCS preflight contracts, including their no-op effects."""

from unittest.mock import patch

import pytest
from django.test import Client

from core.models import APIKey, User

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("domain", ["", ".example.org"])
@pytest.mark.parametrize("signed_in", [False, True])
def test_logout_without_csrf_clears_session_and_cookie_only(settings, domain, signed_in):
    settings.AUTH_COOKIE_DOMAIN = domain
    user = User.objects.create(email="logout@example.org")
    key = APIKey.objects.create(user=user)
    browser = Client(enforce_csrf_checks=True)
    if signed_in:
        browser.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        browser.cookies[settings.AUTH_COOKIE_NAME] = key.key
    session = browser.session
    session["user_email"] = user.email
    session["logout_marker"] = "clear me"
    session.save()
    before = list(APIKey.objects.values())
    assert settings.CSRF_COOKIE_NAME not in browser.cookies
    response = browser.post("/logout")
    assert response.status_code == 200  # This endpoint is deliberately csrf_exempt.
    assert response.json() == {"status": "logged out"}
    assert dict(browser.session) == {}
    cookie = response.cookies[settings.AUTH_COOKIE_NAME]
    assert cookie.value == ""
    assert cookie["max-age"] == 0
    assert cookie["domain"] == domain
    assert cookie["path"] == "/"
    assert cookie["expires"] == "Thu, 01 Jan 1970 00:00:00 GMT"
    assert list(APIKey.objects.values()) == before


@pytest.mark.parametrize("origin", [
    "https://viewer.example.org", "https://elsewhere.example.org",
    "https://viewer.example.org.attacker.example", "not an origin", None,
])
def test_gcs_preflight_headers_without_issuing_credentials(settings, origin):
    settings.NGAUTH_ALLOWED_ORIGINS = r"https://viewer\.example\.org"
    headers = {} if origin is None else {"HTTP_ORIGIN": origin}
    before = list(APIKey.objects.values())
    with (
        patch("ngauth.views.gcs.get_gcs_token_for_user") as cloud,
        patch("ngauth.views._mint_temporary_user_token") as mint,
        patch("socket.socket.connect", side_effect=AssertionError("Unexpected outbound call")) as connect,
    ):
        response = Client().options("/gcs_token", **headers)
    assert response.status_code == 200
    assert response.content == b""
    cors = {key.lower(): value for key, value in response.items() if key.lower().startswith("access-control-")}
    assert cors == ({
        "access-control-allow-origin": origin,
        "access-control-allow-methods": "POST, OPTIONS",
        "access-control-allow-headers": "Content-Type",
        "access-control-allow-credentials": "true",
        "access-control-max-age": "86400",
    } if origin == "https://viewer.example.org" else {})
    cloud.assert_not_called()
    mint.assert_not_called()
    connect.assert_not_called()
    assert list(APIKey.objects.values()) == before
