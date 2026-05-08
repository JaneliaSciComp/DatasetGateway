"""Tests for service-account principal flow through auth_api endpoints."""

import pytest
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import (
    APIKey,
    Dataset,
    DatasetVersion,
    Permission,
    ServiceAccount,
    ServiceAccountGrant,
    ServiceAccountToken,
    TOSDocument,
    User,
)


@pytest.mark.django_db
class TestSAAuthAPIFlow(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.edit_perm, _ = Permission.objects.get_or_create(name="edit")
        self.admin_perm, _ = Permission.objects.get_or_create(name="admin")

        self.sa = ServiceAccount.objects.create(name="ci-bot", description="CI")
        self.sa_token = ServiceAccountToken.objects.create(
            service_account=self.sa, description="ci", key="tok-sa-flow",
        )

        self.granted_ds = Dataset.objects.create(name="granted")
        self.ungranted_ds = Dataset.objects.create(name="ungranted")
        self.tos_ds = Dataset.objects.create(name="tos-required")
        tos = TOSDocument.objects.create(name="tos1", text="t", dataset=self.tos_ds)
        self.tos_ds.tos = tos
        self.tos_ds.save()

        ServiceAccountGrant.objects.create(
            service_account=self.sa, dataset=self.granted_ds, permission=self.view_perm,
        )
        # Even datasets that gate humans behind TOS are accessible to SAs if
        # explicitly granted — TOS does not apply.
        ServiceAccountGrant.objects.create(
            service_account=self.sa, dataset=self.tos_ds, permission=self.view_perm,
        )

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.sa_token.key}"}

    def test_whoami_returns_sa_shape(self):
        resp = self.client.get("/api/v1/whoami", **self._auth())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["service_account"])
        self.assertFalse(data["admin"])
        self.assertEqual(data["name"], "ci-bot")
        self.assertEqual(data["description"], "CI")
        self.assertEqual(data["email"], "ci-bot@service-account.dsg.local")
        self.assertEqual(data["groups"], [])
        self.assertEqual(data["datasets_admin"], [])

    def test_datasets_list_returns_only_granted(self):
        resp = self.client.get("/api/v1/datasets", **self._auth())
        self.assertEqual(resp.status_code, 200)
        names = {d["name"] for d in resp.json()}
        self.assertEqual(names, {"granted", "tos-required"})

    def test_check_access_grants_sa_with_grant(self):
        resp = self.client.post(
            "/api/v1/check-access",
            data={"dataset": "granted", "permission": "view"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["allowed"])
        self.assertEqual(body["reason"], "service_account_grant")

    def test_check_access_denies_sa_without_grant(self):
        resp = self.client.post(
            "/api/v1/check-access",
            data={"dataset": "ungranted", "permission": "view"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["allowed"])
        self.assertEqual(body["reason"], "no_permission")

    def test_check_access_skips_tos_for_sa(self):
        # The dataset has TOS but SA is granted — must be allowed without TOS.
        resp = self.client.post(
            "/api/v1/check-access",
            data={"dataset": "tos-required", "permission": "view"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["allowed"])
        self.assertEqual(body["reason"], "service_account_grant")

    def test_disabled_sa_token_rejected(self):
        self.sa.is_active = False
        self.sa.save()
        cache.clear()
        resp = self.client.get("/api/v1/whoami", **self._auth())
        self.assertEqual(resp.status_code, 401)
