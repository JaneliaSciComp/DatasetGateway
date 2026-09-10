"""Web sessions reject delegated credentials and expose browser grants."""

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from core.models import APIKey, AuditLog, ClientConsent, RegisteredClient, User

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    cache.clear()
    user = User.objects.create(email="account@example.org")
    registered = RegisteredClient.objects.create(origin="https://navis-org.github.io", name="CODA", owner="Philipp")
    return user, registered


def cookie_client(key, session_user=None):
    client = Client()
    client.cookies[settings.AUTH_COOKIE_NAME] = key.key
    if session_user is not None:
        session = client.session
        session["user_email"] = session_user.email
        session.save()
    return client


@pytest.mark.parametrize("path", ["/web/my-account", "/web/service-accounts"])
@pytest.mark.parametrize("with_session", [False, True])
def test_delegated_cookie_is_logged_out_without_session_repair(account, path, with_session):
    user, registered = account
    key = APIKey.objects.create(user=user, delegated_client=registered)
    other = User.objects.create(email="session@example.org")
    client = cookie_client(key, other if with_session else None)
    response = client.get(path)
    assert response.status_code == 302
    assert response.url == "/auth/login"
    assert client.session.get("user_email") == (other.email if with_session else None)


def test_delegated_cookie_cannot_create_permanent_token(account):
    user, registered = account
    key = APIKey.objects.create(user=user, delegated_client=registered)
    client = cookie_client(key, user)
    before = APIKey.objects.count()
    response = client.post("/web/my-account", {"action": "create_token", "description": "forbidden"})
    assert response.status_code == 302
    assert response.url == "/auth/login"
    assert APIKey.objects.count() == before


def test_expired_cookie_without_session_is_logged_out(account):
    user, _ = account
    key = APIKey.objects.create(user=user, expires_at=timezone.now())
    client = cookie_client(key)
    response = client.get("/web/my-account")
    assert response.status_code == 302
    assert response.url == "/auth/login"
    assert "user_email" not in client.session


def test_expired_cookie_uses_live_session_user(account):
    user, _ = account
    key = APIKey.objects.create(user=user, expires_at=timezone.now())
    other = User.objects.create(email="session@example.org")
    client = cookie_client(key, other)
    response = client.get("/web/my-account")
    assert response.status_code == 200
    assert response.context["user"].pk == other.pk
    assert client.session["user_email"] == other.email


@pytest.fixture
def signed_in_account(account):
    user, registered = account
    browser_key = APIKey.objects.create(user=user)
    from ngauth.views import _mint_delegated_api_key
    grant = _mint_delegated_api_key(user, registered)
    consent = ClientConsent.objects.create(user=user, client=registered)
    client = Client(enforce_csrf_checks=True)
    client.cookies[settings.AUTH_COOKIE_NAME] = browser_key.key
    response = client.get("/web/my-account")
    assert response.status_code == 200
    return client, grant, consent


def account_post(client, **data):
    data["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
    return client.post("/web/my-account", data)


def test_account_lists_own_live_grants_and_remembered_sites(account, signed_in_account):
    user, registered = account
    client, grant, consent = signed_in_account
    other = User.objects.create(email="other@example.org")
    foreign = APIKey.objects.create(user=other, delegated_client=registered)
    expired = APIKey.objects.create(user=user, delegated_client=registered, expires_at=timezone.now())
    response = client.get("/web/my-account")
    html = response.content.decode()
    for text in ("Sites you have signed in from", "Remembered sites", "CODA", registered.origin,
                 "Created", "Expires", 'value="revoke_delegated"', 'value="forget_client"'):
        assert text in html
    assert [key.pk for key in response.context["delegated_tokens"]] == [grant.pk]
    assert [row.pk for row in response.context["remembered_clients"]] == [consent.pk]
    for key in (grant, foreign, expired):
        if key.key in html:
            pytest.fail("Account page exposed a browser credential", pytrace=False)


def test_revoke_owned_grant_preserves_consent_and_causes_401(signed_in_account):
    client, grant, consent = signed_in_account
    headers = {"HTTP_AUTHORIZATION": f"Bearer {grant.key}"}
    identity = Client().get("/api/v1/user/cache?service=neuprint", **headers)
    assert identity.status_code == 200
    response = account_post(client, action="revoke_delegated", token_id=grant.pk)
    assert response.status_code == 302
    assert response.url == "/web/my-account"
    assert not APIKey.objects.filter(pk=grant.pk).exists()
    assert ClientConsent.objects.filter(pk=consent.pk).exists()
    revoked = Client().get("/api/v1/user/cache?service=neuprint", **headers)
    assert revoked.status_code == 401
    audit = AuditLog.objects.get(action="api_token_revoked")
    assert audit.target_id == str(grant.pk)
    assert audit.before_state["client_origin"] == consent.client.origin
    response = client.get("/login", {"origin": consent.client.origin, "token": "api"})
    assert response.context["remembered"] is True
    assert "ngauth-payload" not in response.content.decode()


def test_forget_owned_consent_keeps_live_key_and_restores_prompt(signed_in_account):
    client, grant, consent = signed_in_account
    response = account_post(client, action="forget_client", client_id=consent.client_id)
    assert response.status_code == 302
    assert response.url == "/web/my-account"
    assert not ClientConsent.objects.filter(pk=consent.pk).exists()
    assert APIKey.objects.filter(pk=grant.pk).exists()
    response = client.get("/login", {"origin": consent.client.origin, "token": "api"})
    assert response.status_code == 200
    assert response.context["remembered"] is False
    assert "ngauth-payload" not in response.content.decode()
    assert AuditLog.objects.filter(action="client_consent_forgotten", target_id=str(consent.pk)).exists()
    identity = Client().get("/api/v1/user/cache?service=neuprint",
                            HTTP_AUTHORIZATION=f"Bearer {grant.key}")
    assert identity.status_code == 200


def test_account_cannot_revoke_or_forget_another_users_rows(account, signed_in_account):
    user, registered = account
    client, own, own_consent = signed_in_account
    other = User.objects.create(email="other@example.org")
    other_site = RegisteredClient.objects.create(origin="https://other.example.org", name="Other", owner="Owner")
    foreign = APIKey.objects.create(user=other, delegated_client=other_site)
    foreign_consent = ClientConsent.objects.create(user=other, client=other_site)
    first = account_post(client, action="revoke_delegated", token_id=foreign.pk)
    second = account_post(client, action="forget_client", client_id=other_site.pk)
    assert first.status_code == second.status_code == 302
    assert APIKey.objects.filter(pk=foreign.pk).exists()
    assert ClientConsent.objects.filter(pk=foreign_consent.pk).exists()
    assert APIKey.objects.filter(pk=own.pk).exists()
    assert ClientConsent.objects.filter(pk=own_consent.pk).exists()
    assert not AuditLog.objects.filter(action__in=["api_token_revoked", "client_consent_forgotten"]).exists()
    from django.contrib.messages import get_messages
    assert [str(message) for message in get_messages(second.wsgi_request)] == [
        "Token not found.", "Remembered site not found.",
    ]


@pytest.mark.parametrize("bad_id", ["garbage", "", "-1", "999999"])
def test_account_rejects_invalid_grant_and_client_ids(signed_in_account, bad_id):
    client, grant, consent = signed_in_account
    for action, field in [("revoke_delegated", "token_id"), ("forget_client", "client_id")]:
        response = account_post(client, action=action, **{field: bad_id})
        assert response.status_code == 302
    assert APIKey.objects.filter(pk=grant.pk).exists()
    assert ClientConsent.objects.filter(pk=consent.pk).exists()


def test_account_revocation_and_forgetting_require_csrf(signed_in_account):
    client, grant, consent = signed_in_account
    for data in ({"action": "revoke_delegated", "token_id": grant.pk},
                 {"action": "forget_client", "client_id": consent.client_id}):
        response = client.post("/web/my-account", data)
        assert response.status_code == 403
    assert APIKey.objects.filter(pk=grant.pk).exists()
    assert ClientConsent.objects.filter(pk=consent.pk).exists()
