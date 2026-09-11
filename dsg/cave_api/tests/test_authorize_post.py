"""The POST OAuth entry point constructs URLs without contacting Google."""

from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.utils import timezone

from core.models import APIKey, User

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("requested_with", [None, "", "fetch", "XMLHttpRequest"])
@pytest.mark.parametrize(
    "query_redirect, body_redirect, expected",
    [
        ("https://query.janelia.org/next", "https://body.janelia.org/next", "https://query.janelia.org/next"),
        (None, "https://body.janelia.org/next", "https://body.janelia.org/next"),
        ("https://elsewhere.example/next", "https://body.janelia.org/next", "/"),
        (None, "https://elsewhere.example/next", "/"),
    ],
)
def test_authorize_post_redirect_and_session(api_client, settings, requested_with,
                                             query_redirect, body_redirect, expected):
    settings.GOOGLE_CLIENT_ID = "synthetic-client"
    query = {"tos_id": "17", "service": "neuprint", "dataset": "fixture:v1"}
    if query_redirect is not None:
        query["redirect"] = query_redirect
    headers = {} if requested_with is None else {"HTTP_X_REQUESTED_WITH": requested_with}
    with patch("socket.socket.connect", side_effect=AssertionError("Unexpected outbound call")) as connect:
        response = api_client.post(
            "/api/v1/authorize?" + urlencode(query),
            {"redirect": body_redirect, "tos_id": "ignored", "service": "ignored", "dataset": "ignored"},
            format="json", **headers,
        )
    connect.assert_not_called()
    if requested_with:
        assert response.status_code == 200
        assert set(response.json()) == {"authorization_url"}
        url = response.json()["authorization_url"]
    else:
        assert response.status_code == 302
        url = response["Location"]
    parsed = urlsplit(url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        "https", "accounts.google.com", "/o/oauth2/v2/auth",
    )
    params = parse_qs(parsed.query)
    session = api_client.session
    assert params["state"] == [session["oauth_state"]]
    assert len(session["oauth_state"]) >= 32
    assert params["client_id"] == ["synthetic-client"]
    assert params["redirect_uri"] == ["http://testserver/api/v1/oauth2callback"]
    assert params["response_type"] == ["code"]
    assert params["scope"] == ["openid email profile"]
    assert session["oauth_redirect"] == expected
    assert session["oauth_tos_id"] == "17"
    assert session["oauth_service"] == "neuprint"
    assert session["oauth_dataset"] == "fixture:v1"
    assert "login_hint" not in params
    assert not APIKey.objects.exists()


@pytest.mark.parametrize("cookie_kind", ["absent", "unknown", "existing", "expired"])
def test_authorize_post_cookie_hint_is_a_row_lookup(api_client, settings, cookie_kind):
    user = User.objects.create(email="hint@example.org")
    if cookie_kind in {"existing", "expired"}:
        key = APIKey.objects.create(
            user=user,
            expires_at=timezone.now() - timedelta(seconds=1) if cookie_kind == "expired" else None,
        )
        api_client.cookies[settings.AUTH_COOKIE_NAME] = key.key
    elif cookie_kind == "unknown":
        api_client.cookies[settings.AUTH_COOKIE_NAME] = "unknown-cookie"
    before = list(APIKey.objects.values())
    with patch("socket.socket.connect", side_effect=AssertionError("Unexpected outbound call")) as connect:
        response = api_client.post("/api/v1/authorize", {}, format="json", HTTP_X_REQUESTED_WITH="fetch")
    connect.assert_not_called()
    assert response.status_code == 200
    params = parse_qs(urlsplit(response.json()["authorization_url"]).query)
    if cookie_kind in {"existing", "expired"}:
        assert params["login_hint"] == [user.email]
    else:
        assert "login_hint" not in params
    assert api_client.session["oauth_redirect"] == "/"
    assert not {"oauth_tos_id", "oauth_service", "oauth_dataset"} & set(api_client.session.keys())
    assert list(APIKey.objects.values()) == before
