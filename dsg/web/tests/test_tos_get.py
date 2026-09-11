"""Viewing terms never records consent or an acceptance audit."""

import pytest
from django.test import Client

from core.models import APIKey, AuditLog, TOSAcceptance, TOSDocument, User

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("identity", ["anonymous", "signed_in", "accepted"])
@pytest.mark.parametrize("missing", [False, True])
def test_terms_get_renders_identity_and_existing_acceptance(settings, identity, missing):
    terms = TOSDocument.objects.create(name="Fixture terms", text="Read these fixture terms.")
    user = User.objects.create(email="terms@example.org")
    browser = Client()
    if identity != "anonymous":
        key = APIKey.objects.create(user=user)
        browser.cookies[settings.AUTH_COOKIE_NAME] = key.key
    if identity == "accepted":
        TOSAcceptance.objects.create(user=user, tos_document=terms)
    acceptances = list(TOSAcceptance.objects.values())
    audits = list(AuditLog.objects.filter(action="tos_accepted").values())
    response = browser.get(f"/web/tos/{terms.pk + 1000 if missing else terms.pk}/accept")
    assert response.status_code == (404 if missing else 200)
    if not missing:
        assert "web/tos_accept.html" in [template.name for template in response.templates]
        assert response.context["user"] == (None if identity == "anonymous" else user)
        assert response.context["already_accepted"] is (identity == "accepted")
        assert response.context["tos_doc"] == terms
        assert b"Fixture terms" in response.content
    assert list(TOSAcceptance.objects.values()) == acceptances
    assert list(AuditLog.objects.filter(action="tos_accepted").values()) == audits
