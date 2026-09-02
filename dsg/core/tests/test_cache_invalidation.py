"""Transaction-aware service-account permission-cache invalidation tests."""

import pytest
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from rest_framework.test import APIClient

from core.authentication import TokenAuthentication
from core.models import (
    Dataset,
    Permission,
    ServiceAccount,
    ServiceAccountGrant,
    ServiceAccountToken,
)


class TestCacheSettings(SimpleTestCase):
    def test_database_cache_configuration_matches_permission_ttl(self):
        default_cache = settings.CACHES["default"]

        self.assertEqual(
            default_cache["BACKEND"],
            "django.core.cache.backends.db.DatabaseCache",
        )
        self.assertEqual(default_cache["LOCATION"], "dsg_cache_table")
        self.assertEqual(default_cache["TIMEOUT"], settings.PERMISSION_CACHE_TTL)
        self.assertEqual(default_cache["OPTIONS"]["MAX_ENTRIES"], 10000)
        self.assertEqual(default_cache["OPTIONS"]["CULL_FREQUENCY"], 3)


@pytest.mark.django_db
class TestServiceAccountCacheInvalidation(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.edit_perm, _ = Permission.objects.get_or_create(name="edit")
        self.dataset = Dataset.objects.create(name="cache-target")
        self.sa_a = ServiceAccount.objects.create(name="cache-a")
        self.sa_b = ServiceAccount.objects.create(name="cache-b")
        self.token_a = ServiceAccountToken.objects.create(
            service_account=self.sa_a, key="cache-token-a"
        )
        self.token_b = ServiceAccountToken.objects.create(
            service_account=self.sa_b, key="cache-token-b"
        )

    def _warm(self, token):
        response = self.client.get(
            "/api/v1/user/cache",
            HTTP_AUTHORIZATION=f"Bearer {token.key}",
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _cache_key(self, service_account):
        return f"{TokenAuthentication.CACHE_PREFIX}sa_{service_account.pk}"

    def test_retarget_invalidates_old_and_new_service_accounts(self):
        grant = ServiceAccountGrant.objects.create(
            service_account=self.sa_a,
            dataset=self.dataset,
            permission=self.view_perm,
        )
        self.assertIn(self.dataset.name, self._warm(self.token_a)["permissions_v2"])
        self.assertNotIn(self.dataset.name, self._warm(self.token_b)["permissions_v2"])

        with self.captureOnCommitCallbacks(execute=True):
            grant.service_account = self.sa_b
            grant.save(update_fields=["service_account"])

        self.assertNotIn(self.dataset.name, self._warm(self.token_a)["permissions_v2"])
        self.assertIn(self.dataset.name, self._warm(self.token_b)["permissions_v2"])

    def test_explicit_pk_on_new_grant_tolerates_missing_old_row(self):
        self.assertNotIn(self.dataset.name, self._warm(self.token_a)["permissions_v2"])
        grant = ServiceAccountGrant(
            pk=987654,
            service_account=self.sa_a,
            dataset=self.dataset,
            permission=self.view_perm,
        )

        with self.captureOnCommitCallbacks(execute=True):
            grant.save(force_insert=True)

        self.assertIn(self.dataset.name, self._warm(self.token_a)["permissions_v2"])

    def test_update_fields_uses_committed_owner_not_mutated_instance_owner(self):
        grant = ServiceAccountGrant.objects.create(
            service_account=self.sa_a,
            dataset=self.dataset,
            permission=self.view_perm,
        )
        self.assertEqual(
            self._warm(self.token_a)["permissions_v2"][self.dataset.name],
            ["view"],
        )
        self.assertNotIn(self.dataset.name, self._warm(self.token_b)["permissions_v2"])

        with self.captureOnCommitCallbacks(execute=True):
            grant.service_account = self.sa_b
            grant.permission = self.edit_perm
            grant.save(update_fields=["permission"])

        self.assertEqual(
            self._warm(self.token_a)["permissions_v2"][self.dataset.name],
            ["edit", "view"],
        )
        self.assertNotIn(self.dataset.name, self._warm(self.token_b)["permissions_v2"])

    def test_f_expression_owner_save_requeries_scalar_owner(self):
        grant = ServiceAccountGrant.objects.create(
            service_account=self.sa_a,
            dataset=self.dataset,
            permission=self.view_perm,
        )
        self.assertEqual(
            self._warm(self.token_a)["permissions_v2"][self.dataset.name],
            ["view"],
        )

        with self.captureOnCommitCallbacks(execute=True):
            grant.service_account_id = F("service_account_id")
            grant.permission = self.edit_perm
            grant.save(update_fields=["service_account", "permission"])

        self.assertEqual(
            self._warm(self.token_a)["permissions_v2"][self.dataset.name],
            ["edit", "view"],
        )

    def test_dataset_cascade_invalidates_service_account_cache(self):
        ServiceAccountGrant.objects.create(
            service_account=self.sa_a,
            dataset=self.dataset,
            permission=self.view_perm,
        )
        self._warm(self.token_a)
        self.assertIsNotNone(cache.get(self._cache_key(self.sa_a)))

        with self.captureOnCommitCallbacks(execute=True):
            self.dataset.delete()

        self.assertIsNone(cache.get(self._cache_key(self.sa_a)))


@pytest.mark.django_db(transaction=True)
class TestDeferredCacheInvalidation(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        cache.clear()
        permission, _ = Permission.objects.get_or_create(name="view")
        self.dataset = Dataset.objects.create(name="deferred-target")
        self.service_account = ServiceAccount.objects.create(name="deferred-sa")
        self.grant = ServiceAccountGrant.objects.create(
            service_account=self.service_account,
            dataset=self.dataset,
            permission=permission,
        )
        self.cache_key = (
            f"{TokenAuthentication.CACHE_PREFIX}sa_{self.service_account.pk}"
        )

    def test_grant_deletion_invalidates_only_after_real_commit(self):
        cache.set(self.cache_key, {"warm": True}, timeout=300)

        with transaction.atomic():
            self.grant.delete()
            self.assertEqual(cache.get(self.cache_key), {"warm": True})

        self.assertIsNone(cache.get(self.cache_key))
