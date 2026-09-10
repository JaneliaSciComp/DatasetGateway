"""Delegated authority is limited per request, without changing the user row."""

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from core.models import (
    APIKey, Dataset, DatasetTranslation, Grant, Permission, RegisteredClient,
    Service, ServiceAccount, ServiceAccountToken, User,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def keys():
    cache.clear()
    user = User.objects.create(email="admin@example.org", admin=True)
    registered = RegisteredClient.objects.create(origin="https://navis-org.github.io", name="CODA", owner="Philipp")
    normal = APIKey.objects.create(user=user)
    delegated = APIKey.objects.create(user=user, delegated_client=registered)
    return user, normal, delegated


def bearer(key):
    return {"HTTP_AUTHORIZATION": f"Bearer {key.key}"}


@pytest.mark.parametrize("path", [
    "/api/v1/whoami", "/api/v1/user/cache", "/api/v1/user/cache?service=neuprint",
    "/api/dsg/v1/user",
])
def test_delegated_identity_preserves_admin_row_and_normal_cache(keys, path):
    user, normal, delegated = keys
    client = Client()  # No cookies or session: only Bearer authenticates.
    for key, expected_admin in [(normal, True), (delegated, False), (normal, True)]:
        response = client.get(path, **bearer(key))
        assert response.status_code == 200
        assert response.json()["email"] == user.email
        assert response.json()["admin"] is expected_admin
        user.refresh_from_db()
        assert user.admin is True
    assert not client.cookies


def test_delegated_request_does_not_seed_normal_cache(keys):
    user, normal, delegated = keys
    client = Client()
    assert client.get("/api/v1/user/cache", **bearer(delegated)).json()["admin"] is False
    assert client.get("/api/v1/user/cache", **bearer(normal)).json()["admin"] is True


@pytest.mark.parametrize("method,path", [
    ("post", "/api/v1/create_token"), ("get", "/api/v1/long_lived_token"),
    ("get", "/api/v1/user/token"), ("get", "/api/v1/refresh_token"),
])
def test_token_management_refuses_delegated_keys(keys, method, path):
    user, normal, delegated = keys
    client = Client()
    before = APIKey.objects.count()
    response = getattr(client, method)(path, data={}, content_type="application/json", **bearer(delegated))
    assert response.status_code == 403
    assert APIKey.objects.count() == before
    response = getattr(client, method)(path, data={}, content_type="application/json", **bearer(normal))
    assert response.status_code == 200


def test_admin_dataset_listing_and_decision_are_restricted(keys):
    user, normal, delegated = keys
    shared = Dataset.objects.create(name="shared")
    hidden = Dataset.objects.create(name="unshared")
    permission, _ = Permission.objects.get_or_create(name="view")
    Grant.objects.create(user=user, dataset=shared, permission=permission)
    client = Client()
    listed = client.get("/api/v1/datasets", **bearer(delegated))
    assert listed.status_code == 200
    assert {row["name"] for row in listed.json()} == {shared.name}
    normal_list = client.get("/api/v1/datasets", **bearer(normal))
    assert {row["name"] for row in normal_list.json()} == {shared.name, hidden.name}
    for key, expected in [(delegated, False), (normal, True)]:
        response = client.post("/api/v1/check-access", {"dataset": hidden.name}, content_type="application/json", **bearer(key))
        assert response.status_code == 200
        assert response.json()["allowed"] is expected
        assert (response.json()["reason"] == "admin") is expected
        user.refresh_from_db()
        assert user.admin is True


def test_native_authorize_does_not_take_admin_shortcut(keys):
    user, normal, delegated = keys
    service = Service.objects.create(name="neuprint")
    hidden = Dataset.objects.create(name="unshared")
    DatasetTranslation.objects.create(service=service, client_name=hidden.name, dataset=hidden)
    client = Client()
    for key, decision in [(delegated, "deny"), (normal, "allow")]:
        response = client.post("/api/dsg/v1/authorize", {
            "service": service.name, "entries": [{"name": hidden.name}],
        }, content_type="application/json", **bearer(key))
        assert response.status_code == 200
        assert response.json()["entries"][0]["decision"] == decision
        user.refresh_from_db()
        assert user.admin is True


def test_scim_refuses_delegated_admin(keys):
    user, normal, delegated = keys
    client = Client()
    assert client.get("/auth/scim/v2/Users", **bearer(delegated)).status_code == 401
    assert client.get("/auth/scim/v2/Users", **bearer(normal)).status_code == 200


@pytest.mark.parametrize("state", ["expired", "revoked", "garbage", "disabled"])
def test_invalid_delegated_credential_is_401(keys, state):
    user, normal, delegated = keys
    headers = bearer(delegated)
    if state == "expired":
        APIKey.objects.filter(pk=delegated.pk).update(expires_at=timezone.now())
    elif state == "revoked":
        delegated.delete()
    elif state == "garbage":
        headers = {"HTTP_AUTHORIZATION": "Bearer invalid-test-credential"}
    else:
        User.objects.filter(pk=user.pk).update(is_active=False)
    assert Client().get("/api/v1/user/cache?service=neuprint", **headers).status_code == 401


def test_service_account_never_has_delegated_flag(keys):
    sa = ServiceAccount.objects.create(name="pipeline")
    token = ServiceAccountToken.objects.create(service_account=sa)
    response = Client().get("/api/v1/whoami", **bearer(token))
    assert response.status_code == 200
    assert response.json()["email"] == sa.email
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory
    from core.authentication import TokenAuthentication
    request = Request(APIRequestFactory().get("/api/v1/whoami", **bearer(token)))
    principal, _ = TokenAuthentication().authenticate(request)
    assert principal.pk == sa.pk
    assert request.auth_delegated_client is None
    assert Client().post("/api/v1/create_token", **bearer(token)).status_code == 403
