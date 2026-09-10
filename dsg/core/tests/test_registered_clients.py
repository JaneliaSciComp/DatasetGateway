"""Registered origins, consent uniqueness, and their admin surfaces."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, override_settings

from core.models import APIKey, ClientConsent, RegisteredClient, User

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("origin", [
    "https://navis-org.github.io", "http://localhost:8080",
])
def test_valid_registered_origin(origin):
    client = RegisteredClient(origin=origin, name="CODA", owner="Philipp")
    client.full_clean()
    client.save()
    assert client.enabled
    assert client.allowed_services == []


@pytest.mark.parametrize("origin", [
    "", "null", "file:///tmp/page", "https://example.org/path", "https://example.org/",
    "https://example.org?x=1", "https://example.org#fragment", "https://user@example.org",
    " https://example.org", "https://example.org:99999",
])
def test_invalid_registered_origin(origin):
    with pytest.raises(ValidationError):
        RegisteredClient(origin=origin, name="CODA", owner="Philipp").full_clean()


def test_unique_origin_and_consent_and_client_deletion():
    client = RegisteredClient.objects.create(origin="https://navis-org.github.io", name="CODA", owner="Philipp")
    user = User.objects.create(email="user@example.org")
    ClientConsent.objects.create(user=user, client=client)
    with pytest.raises(IntegrityError), transaction.atomic():
        RegisteredClient.objects.create(origin=client.origin, name="Duplicate", owner="Philipp")
    with pytest.raises(IntegrityError), transaction.atomic():
        ClientConsent.objects.create(user=user, client=client)
    key = APIKey.objects.create(user=user, delegated_client=client)
    client.delete()
    key.refresh_from_db()
    assert key.delegated_client is None
    assert not ClientConsent.objects.exists()


@override_settings(STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
def test_admin_pages_and_readonly_consent():
    user = User.objects.create(email="admin@example.org", admin=True)
    registered = RegisteredClient.objects.create(origin="https://navis-org.github.io", name="CODA", owner="Philipp")
    ClientConsent.objects.create(user=user, client=registered)
    client = Client()
    client.force_login(user)
    listing = client.get("/admin/core/registeredclient/")
    assert listing.status_code == 200
    assert "CODA" in listing.content.decode()
    change = client.get(f"/admin/core/registeredclient/{registered.pk}/change/")
    assert change.status_code == 200
    assert "reserved — not yet enforced" in change.content.decode()
    assert 'name="allowed_services"' not in change.content.decode()
    account = client.get(f"/admin/core/user/{user.pk}/change/")
    assert account.status_code == 200
    assert "CODA" in account.content.decode()
    assert 'name="client_consents-0-client"' not in account.content.decode()
    assert 'name="client_consents-0-DELETE"' not in account.content.decode()
