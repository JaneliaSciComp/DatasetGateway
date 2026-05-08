"""Web admin UI tests for service accounts."""

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase

from core.models import (
    APIKey,
    Dataset,
    Permission,
    ServiceAccount,
    ServiceAccountGrant,
    ServiceAccountToken,
    User,
)


@pytest.mark.django_db
class TestServiceAccountAdmin(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create(email="admin@example.org", name="admin", admin=True)
        self.admin_key = APIKey.objects.create(user=self.admin, key="tok-admin")
        self.regular = User.objects.create(email="reg@example.org", name="reg")
        self.regular_key = APIKey.objects.create(user=self.regular, key="tok-reg")

        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.edit_perm, _ = Permission.objects.get_or_create(name="edit")
        self.dataset = Dataset.objects.create(name="ds-x")

    def _admin_client(self):
        self.client.cookies.clear()
        self.client.cookies[settings.AUTH_COOKIE_NAME] = self.admin_key.key
        return self.client

    def _regular_client(self):
        self.client.cookies.clear()
        self.client.cookies[settings.AUTH_COOKIE_NAME] = self.regular_key.key
        return self.client

    def test_list_requires_admin(self):
        resp = self._regular_client().get("/web/service-accounts")
        # access_denied template renders 200 OK with the denial UI; check
        # that the admin-only content is NOT rendered.
        self.assertNotContains(resp, "Create Service Account")

    def test_admin_can_create_sa(self):
        resp = self._admin_client().post("/web/service-accounts", {
            "action": "create",
            "name": "ci-bot",
            "description": "CI runner",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ServiceAccount.objects.filter(name="ci-bot").exists())

    def test_create_rejects_non_slug_name(self):
        resp = self._admin_client().post("/web/service-accounts", {
            "action": "create",
            "name": "not a slug!",
            "description": "",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(ServiceAccount.objects.filter(description="").exclude(name="").exists() and
                         ServiceAccount.objects.filter(name="not a slug!").exists())

    def test_create_rejects_duplicate_name(self):
        ServiceAccount.objects.create(name="dup")
        resp = self._admin_client().post("/web/service-accounts", {
            "action": "create",
            "name": "dup",
            "description": "",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ServiceAccount.objects.filter(name="dup").count(), 1)

    def test_mint_token_then_revoke(self):
        sa = ServiceAccount.objects.create(name="t1")
        c = self._admin_client()

        resp = c.post(f"/web/service-accounts/{sa.name}", {
            "action": "mint_token",
            "description": "primary",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(sa.tokens.count(), 1)

        # The plaintext is stashed in session for one-shot display on next GET.
        resp = c.get(f"/web/service-accounts/{sa.name}")
        self.assertContains(resp, "primary")

        token = sa.tokens.first()
        resp = c.post(f"/web/service-accounts/{sa.name}", {
            "action": "revoke_token",
            "token_id": token.pk,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(sa.tokens.count(), 0)

    def test_add_grant_from_sa_detail_then_revoke(self):
        sa = ServiceAccount.objects.create(name="g1")
        c = self._admin_client()

        resp = c.post(f"/web/service-accounts/{sa.name}", {
            "action": "add_grant",
            "dataset_id": self.dataset.pk,
            "permission": self.view_perm.pk,
            "version": "",
        })
        self.assertEqual(resp.status_code, 302)
        grant = ServiceAccountGrant.objects.get(service_account=sa, dataset=self.dataset)
        self.assertEqual(grant.permission, self.view_perm)
        self.assertEqual(grant.granted_by, self.admin)

        resp = c.post(f"/web/service-accounts/{sa.name}", {
            "action": "revoke_grant",
            "grant_id": grant.pk,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(ServiceAccountGrant.objects.filter(pk=grant.pk).exists())

    def test_add_sa_grant_from_dataset_grants_page(self):
        sa = ServiceAccount.objects.create(name="g2")
        c = self._admin_client()

        resp = c.post(f"/web/grants/{self.dataset.name}", {
            "action": "grant_sa",
            "service_account_id": sa.pk,
            "permission": self.view_perm.pk,
            "version": "",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            ServiceAccountGrant.objects.filter(
                service_account=sa, dataset=self.dataset, permission=self.view_perm,
            ).exists()
        )

    def test_dataset_grants_page_shows_sa_section(self):
        sa = ServiceAccount.objects.create(name="g3")
        ServiceAccountGrant.objects.create(
            service_account=sa, dataset=self.dataset, permission=self.view_perm,
        )
        resp = self._admin_client().get(f"/web/grants/{self.dataset.name}")
        self.assertContains(resp, "Service Account Grants")
        self.assertContains(resp, "g3")

    def test_disable_sa_blocks_token_use(self):
        sa = ServiceAccount.objects.create(name="t2")
        token = ServiceAccountToken.objects.create(
            service_account=sa, description="t", key="tok-disable",
        )
        c = self._admin_client()

        # Disable
        c.post(f"/web/service-accounts/{sa.name}", {"action": "toggle_active"})
        sa.refresh_from_db()
        self.assertFalse(sa.is_active)

        # Token stops working
        cache.clear()
        from rest_framework.test import APIClient
        api = APIClient()
        resp = api.get("/api/v1/whoami", HTTP_AUTHORIZATION=f"Bearer {token.key}")
        self.assertEqual(resp.status_code, 401)

    def test_delete_requires_name_confirmation(self):
        sa = ServiceAccount.objects.create(name="delme")
        token = ServiceAccountToken.objects.create(
            service_account=sa, description="t", key="tok-delme",
        )
        c = self._admin_client()

        # Wrong confirmation: nothing happens
        c.post(f"/web/service-accounts/{sa.name}", {
            "action": "delete_sa", "confirm_name": "wrong",
        })
        self.assertTrue(ServiceAccount.objects.filter(pk=sa.pk).exists())

        # Right confirmation: cascade-deletes tokens
        c.post(f"/web/service-accounts/{sa.name}", {
            "action": "delete_sa", "confirm_name": "delme",
        })
        self.assertFalse(ServiceAccount.objects.filter(pk=sa.pk).exists())
        self.assertFalse(ServiceAccountToken.objects.filter(pk=token.pk).exists())

    def test_nav_link_only_for_admin(self):
        admin_resp = self._admin_client().get("/web/my-account")
        self.assertContains(admin_resp, "/web/service-accounts")

        # Regular user — nav link absent.
        # Re-create client to drop admin cookies.
        self.client.cookies.clear()
        regular_resp = self._regular_client().get("/web/my-account")
        self.assertNotContains(regular_resp, "/web/service-accounts")
