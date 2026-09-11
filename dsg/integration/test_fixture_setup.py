"""In-process checks of the joined seed/consent fixtures; no Go or sockets."""

import json
from unittest.mock import patch

import pytest
from django.test import Client

from core.models import APIKey, TOSAcceptance
from integration.coda_neuprint_check import World

pytestmark = pytest.mark.django_db


def decision(world, label, key=None):
    headers = {} if key is None else {"HTTP_AUTHORIZATION": "Bearer " + key.key}
    response = Client().post("/api/dsg/v1/authorize", json.dumps({
        "service": "neuprint", "entries": [{"name": world.datasets[label].name, "version": "v1"}],
    }), content_type="application/json", **headers)
    assert response.status_code == 200
    return response.json()["entries"][0]["decision"]


def test_joined_seed_and_consent_use_native_contract():
    world = World()
    key = world.issue()
    assert decision(world, "private", key) == "allow"
    assert decision(world, "denied", key) == "deny"
    assert decision(world, "tos", key) == "tos_required"
    assert decision(world, "public") == "allow"
    assert key.delegated_client_id == world.registered.pk
    world.revoke(key)
    response = Client().get("/api/dsg/v1/user", HTTP_AUTHORIZATION="Bearer " + key.key)
    assert response.status_code == 401
    assert not APIKey.objects.filter(pk=key.pk).exists()


def test_joined_terms_fixture_persists_real_acceptance():
    world = World()
    key = world.issue()
    browser = world.browser()
    with patch("core.iam.sync_user_dataset_iam") as iam:
        response = world.post(browser, f"/web/tos/{world.terms.pk}/accept", {})
    assert response.status_code == 302
    iam.assert_called_once_with(world.user, world.datasets["tos"])
    assert TOSAcceptance.objects.filter(user=world.user, tos_document=world.terms).exists()
    assert decision(world, "tos", key) == "allow"


def test_joined_admin_fixture_retains_normal_privileges():
    world = World()
    delegated = world.issue(world.admin)
    assert decision(world, "denied", delegated) == "deny"
    assert decision(world, "denied", world.normal_keys[world.admin.pk]) == "allow"
