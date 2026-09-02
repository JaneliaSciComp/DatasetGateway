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
    Service,
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
        self.tos_service = Service.objects.create(name="tos-service")
        TOSDocument.objects.create(
            name="service tos",
            text="service terms",
            dataset=self.tos_ds,
            service=self.tos_service,
        )

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
        self.assertEqual(
            resp.json(),
            {"allowed": True, "reason": "service_account_grant"},
        )

    def test_check_access_denies_sa_without_grant(self):
        resp = self.client.post(
            "/api/v1/check-access",
            data={"dataset": "ungranted", "permission": "view"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {"allowed": False, "reason": "no_permission"},
        )

    def test_check_access_skips_tos_for_sa(self):
        # The dataset has TOS but SA is granted — must be allowed without TOS.
        resp = self.client.post(
            "/api/v1/check-access",
            data={"dataset": "tos-required", "permission": "view"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {"allowed": True, "reason": "service_account_grant"},
        )

    def test_check_access_skips_service_specific_tos_for_sa(self):
        resp = self.client.post(
            "/api/v1/check-access",
            data={
                "dataset": "tos-required",
                "permission": "view",
                "service": self.tos_service.name,
            },
            format="json",
            **self._auth(),
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {"allowed": True, "reason": "service_account_grant"},
        )

    def test_disabled_sa_token_rejected(self):
        self.sa.is_active = False
        self.sa.save()
        cache.clear()
        resp = self.client.get("/api/v1/whoami", **self._auth())
        self.assertEqual(resp.status_code, 401)


@pytest.mark.django_db
class TestSAPublicCoverageAuthAPI(TestCase):
    """SA public coverage on the legacy auth_api surfaces (datasets-0028)."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.view_perm, _ = Permission.objects.get_or_create(name="view")

        self.sa = ServiceAccount.objects.create(name="public-bot")
        self.sa_token = ServiceAccountToken.objects.create(
            service_account=self.sa, description="t", key="tok-sa-public",
        )

        self.private_ds = Dataset.objects.create(name="private")
        self.public_ds = Dataset.objects.create(
            name="fully-public", access_mode=Dataset.ACCESS_PUBLIC,
        )
        tos = TOSDocument.objects.create(
            name="public tos", text="t", dataset=self.public_ds,
        )
        self.public_ds.tos = tos
        self.public_ds.save()

        self.version_ds = Dataset.objects.create(name="version-public")
        self.v1 = DatasetVersion.objects.create(
            dataset=self.version_ds, version="v1", branch="main", ordinal=1,
        )
        self.v2 = DatasetVersion.objects.create(
            dataset=self.version_ds, version="v2", branch="main", ordinal=2,
            is_public=True,
        )
        self.v3 = DatasetVersion.objects.create(
            dataset=self.version_ds, version="v3", branch="main", ordinal=3,
        )

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.sa_token.key}"}

    def _check(self, dataset, permission="view", version=None):
        data = {"dataset": dataset, "permission": permission}
        if version:
            data["version"] = version
        resp = self.client.post(
            "/api/v1/check-access", data=data, format="json", **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_check_access_allows_view_on_public_dataset_despite_tos(self):
        body = self._check("fully-public")
        self.assertEqual(body, {"allowed": True, "reason": "public"})

    def test_check_access_denies_non_view_on_public_dataset(self):
        body = self._check("fully-public", permission="edit")
        self.assertFalse(body["allowed"])
        self.assertEqual(body["reason"], "no_permission")

    def test_check_access_public_version_covers_self_and_ancestor_only(self):
        exact = self._check("version-public", version="v2")
        ancestor = self._check("version-public", version="v1")
        descendant = self._check("version-public", version="v3")
        dataset_grain = self._check("version-public")
        unknown = self._check("version-public", version="nope")

        self.assertEqual(exact, {"allowed": True, "reason": "public_version"})
        self.assertTrue(ancestor["allowed"])
        self.assertEqual(ancestor["reason"], "public_version")
        self.assertFalse(descendant["allowed"])
        self.assertFalse(dataset_grain["allowed"])
        self.assertEqual(unknown, {"allowed": False, "reason": "no_permission"})

    def test_check_access_still_denies_private(self):
        body = self._check("private")
        self.assertFalse(body["allowed"])

    def test_datasets_list_includes_public_coverage(self):
        resp = self.client.get("/api/v1/datasets", **self._auth())
        self.assertEqual(resp.status_code, 200)
        names = {d["name"] for d in resp.json()}
        self.assertEqual(names, {"fully-public", "version-public"})

    def test_user_cache_has_public_dataset_view_but_not_version_public(self):
        resp = self.client.get("/api/v1/user/cache", **self._auth())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["permissions_v2"].get("fully-public"), ["view"])
        self.assertEqual(data["permissions"].get("fully-public"), 1)
        self.assertNotIn("version-public", data["permissions_v2"])
        self.assertNotIn("private", data["permissions_v2"])
        self.assertEqual(data["missing_tos"], [])

    def test_disabled_sa_denied_with_warm_permission_cache(self):
        # Warm the permission cache with an authenticated request, then
        # disable the SA: the next request must be rejected at
        # authentication even though the cached permissions still exist.
        warm = self.client.get("/api/v1/user/cache", **self._auth())
        self.assertEqual(warm.status_code, 200)

        self.sa.is_active = False
        self.sa.save(update_fields=["is_active"])

        denied = self.client.get("/api/v1/user/cache", **self._auth())
        self.assertEqual(denied.status_code, 401)
        check = self.client.post(
            "/api/v1/check-access",
            data={"dataset": "fully-public", "permission": "view"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(check.status_code, 401)


@pytest.mark.django_db
class TestSAGrainAndCacheInvalidation(TestCase):
    """Version-grant grain + permission-cache invalidation (datasets-0029)."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.edit_perm, _ = Permission.objects.get_or_create(name="edit")

        self.sa = ServiceAccount.objects.create(name="grain-bot")
        self.sa_token = ServiceAccountToken.objects.create(
            service_account=self.sa, description="t", key="tok-sa-grain",
        )

        self.versioned_ds = Dataset.objects.create(name="versioned")
        self.v1 = DatasetVersion.objects.create(
            dataset=self.versioned_ds, version="v1", branch="main", ordinal=1,
        )
        self.v2 = DatasetVersion.objects.create(
            dataset=self.versioned_ds, version="v2", branch="main", ordinal=2,
        )
        self.v3 = DatasetVersion.objects.create(
            dataset=self.versioned_ds, version="v3", branch="main", ordinal=3,
        )
        self.alt = DatasetVersion.objects.create(
            dataset=self.versioned_ds, version="alt", branch="alt", ordinal=1,
        )
        self.service_a = Service.objects.create(name="service-a")
        self.service_b = Service.objects.create(name="service-b")
        self.dag_service = Service.objects.create(
            name="dag-service", version_eval_mode=Service.VERSION_EVAL_DAG,
        )
        ServiceAccountGrant.objects.create(
            service_account=self.sa,
            dataset=self.versioned_ds,
            dataset_version=self.v2,
            permission=self.view_perm,
        )

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.sa_token.key}"}

    def _check(self, dataset, permission="view", version=None, service=None):
        data = {"dataset": dataset, "permission": permission}
        if version:
            data["version"] = version
        if service is not None:
            data["service"] = service
        resp = self.client.post(
            "/api/v1/check-access", data=data, format="json", **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_version_scoped_grant_uses_ordinal_ancestry(self):
        versionless = self._check("versioned")
        ancestor = self._check("versioned", version="v1")
        exact = self._check("versioned", version="v2")
        descendant = self._check("versioned", version="v3")
        cross_branch = self._check("versioned", version="alt")

        self.assertEqual(versionless, {"allowed": False, "reason": "no_permission"})
        self.assertEqual(
            ancestor, {"allowed": True, "reason": "service_account_grant"}
        )
        self.assertEqual(
            exact, {"allowed": True, "reason": "service_account_grant"}
        )
        self.assertEqual(descendant, {"allowed": False, "reason": "no_permission"})
        self.assertEqual(cross_branch, {"allowed": False, "reason": "no_permission"})

    def test_cross_branch_dag_grant_is_indeterminate_and_denied(self):
        body = self._check(
            "versioned", version="alt", service=self.dag_service.name,
        )

        self.assertEqual(body, {"allowed": False, "reason": "no_permission"})

    def test_edit_grant_satisfies_view_at_dataset_and_version_grains(self):
        ServiceAccountGrant.objects.all().delete()
        ServiceAccountGrant.objects.create(
            service_account=self.sa,
            dataset=self.versioned_ds,
            permission=self.edit_perm,
        )

        dataset_grain = self._check("versioned", permission="view")
        version_grain = self._check(
            "versioned", permission="view", version="v1",
        )

        expected = {"allowed": True, "reason": "service_account_grant"}
        self.assertEqual(dataset_grain, expected)
        self.assertEqual(version_grain, expected)

    def test_service_scope_and_global_grant_semantics(self):
        ServiceAccountGrant.objects.all().delete()
        ServiceAccountGrant.objects.create(
            service_account=self.sa,
            dataset=self.versioned_ds,
            service=self.service_a,
            permission=self.view_perm,
        )

        expected_allow = {"allowed": True, "reason": "service_account_grant"}
        expected_deny = {"allowed": False, "reason": "no_permission"}
        self.assertEqual(
            self._check("versioned", service=self.service_a.name), expected_allow
        )
        self.assertEqual(
            self._check("versioned", service=self.service_b.name), expected_deny
        )
        self.assertEqual(self._check("versioned"), expected_deny)
        self.assertEqual(
            self._check("versioned", service="unknown-service"), expected_deny
        )

        ServiceAccountGrant.objects.all().delete()
        ServiceAccountGrant.objects.create(
            service_account=self.sa,
            dataset=self.versioned_ds,
            permission=self.view_perm,
        )
        self.assertEqual(self._check("versioned"), expected_allow)
        self.assertEqual(
            self._check("versioned", service=self.service_b.name), expected_allow
        )
        self.assertEqual(
            self._check("versioned", service="unknown-service"), expected_allow
        )

    def test_version_scoped_grant_absent_from_user_cache(self):
        resp = self.client.get("/api/v1/user/cache", **self._auth())
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("versioned", resp.json()["permissions_v2"])

    def test_public_flip_invalidates_warm_user_cache(self):
        public = Dataset.objects.create(
            name="flip-me", access_mode=Dataset.ACCESS_PUBLIC,
        )
        warm = self.client.get("/api/v1/user/cache", **self._auth()).json()
        self.assertEqual(warm["permissions_v2"].get("flip-me"), ["view"])

        public.access_mode = Dataset.ACCESS_CLOSED
        with self.captureOnCommitCallbacks(execute=True):
            public.save(update_fields=["access_mode"])

        after = self.client.get("/api/v1/user/cache", **self._auth()).json()
        self.assertNotIn("flip-me", after["permissions_v2"])

    def test_grant_revocation_invalidates_warm_user_cache(self):
        grant = ServiceAccountGrant.objects.create(
            service_account=self.sa,
            dataset=self.versioned_ds,
            permission=self.edit_perm,
        )
        warm = self.client.get("/api/v1/user/cache", **self._auth()).json()
        self.assertIn("versioned", warm["permissions_v2"])

        with self.captureOnCommitCallbacks(execute=True):
            grant.delete()

        after = self.client.get("/api/v1/user/cache", **self._auth()).json()
        self.assertNotIn("versioned", after["permissions_v2"])

    def test_public_view_merges_with_higher_grant_in_all_maps(self):
        merged_ds = Dataset.objects.create(
            name="merged", access_mode=Dataset.ACCESS_PUBLIC,
        )
        ServiceAccountGrant.objects.create(
            service_account=self.sa, dataset=merged_ds, permission=self.edit_perm,
        )

        data = self.client.get("/api/v1/user/cache", **self._auth()).json()
        self.assertEqual(data["permissions_v2"]["merged"], ["edit", "view"])
        self.assertEqual(data["permissions_v2_ignore_tos"]["merged"], ["edit", "view"])
        self.assertEqual(data["permissions"]["merged"], 2)

        with self.captureOnCommitCallbacks(execute=True):
            Dataset.objects.create(
                name="public-only", access_mode=Dataset.ACCESS_PUBLIC,
            )
        data = self.client.get("/api/v1/user/cache", **self._auth()).json()
        self.assertEqual(data["permissions_v2_ignore_tos"]["public-only"], ["view"])

    def test_disabled_sa_denied_on_datasets_and_native_authorize(self):
        self.sa.is_active = False
        self.sa.save(update_fields=["is_active"])

        listing = self.client.get("/api/v1/datasets", **self._auth())
        native = self.client.post(
            "/api/dsg/v1/authorize",
            {"service": "any", "entries": [{"name": "versioned"}]},
            format="json",
            **self._auth(),
        )
        self.assertEqual(listing.status_code, 401)
        self.assertEqual(native.status_code, 401)
