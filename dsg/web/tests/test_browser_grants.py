"""Web sessions reject delegated credentials and expose browser grants."""

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from core.models import APIKey, RegisteredClient, User

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
