"""Integration tests for DatasetGateway authorization API."""

import pytest
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import (
    APIKey,
    Dataset,
    DatasetBucket,
    DatasetVersion,
    Grant,
    Group,
    GroupDatasetPermission,
    Permission,
    Service,
    TOSAcceptance,
    TOSDocument,
    User,
    UserGroup,
)


@pytest.mark.django_db
class TestWhoAmIView(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create(email="alice@example.org", name="alice")
        self.api_key = APIKey.objects.create(user=self.user, key="tok-whoami")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_returns_identity(self):
        resp = self.client.get("/api/v1/whoami", **self._auth())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["email"], "alice@example.org")
        self.assertEqual(data["id"], self.user.pk)
        self.assertFalse(data["admin"])

    def test_unauthenticated(self):
        resp = self.client.get("/api/v1/whoami")
        self.assertEqual(resp.status_code, 401)


@pytest.mark.django_db
class TestDatasetsListView(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create(email="alice@example.org", name="alice")
        self.api_key = APIKey.objects.create(user=self.user, key="tok-ds")
        self.perm = Permission.objects.create(name="view")
        self.ds1 = Dataset.objects.create(name="fish2")
        self.ds2 = Dataset.objects.create(name="fanc")
        self.ds3 = Dataset.objects.create(name="secret-data")

        # Give user access to ds1 via group
        g = Group.objects.create(name="team")
        UserGroup.objects.create(user=self.user, group=g)
        GroupDatasetPermission.objects.create(
            group=g, dataset=self.ds1, permission=self.perm
        )
        # Give user access to ds2 via direct grant
        Grant.objects.create(
            user=self.user, dataset=self.ds2, permission=self.perm
        )

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_non_admin_sees_only_accessible_datasets(self):
        resp = self.client.get("/api/v1/datasets", **self._auth())
        self.assertEqual(resp.status_code, 200)
        names = {d["name"] for d in resp.json()}
        self.assertIn("fish2", names)
        self.assertIn("fanc", names)
        self.assertNotIn("secret-data", names)

    def test_admin_sees_all_datasets(self):
        self.user.admin = True
        self.user.save()
        resp = self.client.get("/api/v1/datasets", **self._auth())
        self.assertEqual(resp.status_code, 200)
        names = {d["name"] for d in resp.json()}
        self.assertIn("secret-data", names)

    def test_dataset_admin_sees_their_dataset(self):
        admin_perm, _ = Permission.objects.get_or_create(name="admin")
        Grant.objects.create(user=self.user, dataset=self.ds3, permission=admin_perm)
        cache.clear()
        resp = self.client.get("/api/v1/datasets", **self._auth())
        names = {d["name"] for d in resp.json()}
        self.assertIn("secret-data", names)


@pytest.mark.django_db
class TestDatasetVersionsView(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create(email="alice@example.org", name="alice")
        self.api_key = APIKey.objects.create(user=self.user, key="tok-ver")
        self.ds = Dataset.objects.create(name="fish2")
        self.bucket_v1 = DatasetBucket.objects.create(dataset=self.ds, name="gs://fish2-v1")
        self.bucket_v2 = DatasetBucket.objects.create(dataset=self.ds, name="gs://fish2-v2")
        dv1 = DatasetVersion.objects.create(
            dataset=self.ds, version="v1", is_public=True
        )
        dv1.buckets.add(self.bucket_v1)
        dv2 = DatasetVersion.objects.create(
            dataset=self.ds, version="v2", is_public=False
        )
        dv2.buckets.add(self.bucket_v2)

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_list_versions(self):
        resp = self.client.get("/api/v1/datasets/fish2/versions", **self._auth())
        self.assertEqual(resp.status_code, 200)
        versions = resp.json()
        self.assertEqual(len(versions), 2)
        names = {v["version"] for v in versions}
        self.assertIn("v1", names)
        self.assertIn("v2", names)

    def test_dataset_not_found(self):
        resp = self.client.get("/api/v1/datasets/nope/versions", **self._auth())
        self.assertEqual(resp.status_code, 404)


@pytest.mark.django_db
class TestAuthorizeDecisionView(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create(email="alice@example.org", name="alice")
        self.admin = User.objects.create(
            email="admin@example.org", name="admin", admin=True
        )
        self.api_key = APIKey.objects.create(user=self.user, key="tok-authz")
        self.admin_key = APIKey.objects.create(user=self.admin, key="tok-admin")
        self.perm = Permission.objects.create(name="view")
        self.ds = Dataset.objects.create(name="fish2")
        g = Group.objects.create(name="team")
        UserGroup.objects.create(user=self.user, group=g)
        GroupDatasetPermission.objects.create(
            group=g, dataset=self.ds, permission=self.perm
        )

    def _auth(self, key=None):
        return {"HTTP_AUTHORIZATION": f"Bearer {key or self.api_key.key}"}

    def test_admin_always_allowed(self):
        resp = self.client.post(
            "/api/v1/check-access",
            {"dataset": "fish2", "permission": "view"},
            format="json",
            **self._auth(self.admin_key.key),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["allowed"])
        self.assertEqual(data["reason"], "admin")

    def test_group_permission_allowed(self):
        resp = self.client.post(
            "/api/v1/check-access",
            {"dataset": "fish2", "permission": "view"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["allowed"])

    def test_no_permission_denied(self):
        ds2 = Dataset.objects.create(name="secret")
        resp = self.client.post(
            "/api/v1/check-access",
            {"dataset": "secret", "permission": "view"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["allowed"])
        self.assertEqual(data["reason"], "no_permission")

    def test_tos_required(self):
        tos = TOSDocument.objects.create(name="Fish TOS", text="Accept these terms.")
        self.ds.tos = tos
        self.ds.save()
        cache.clear()

        resp = self.client.post(
            "/api/v1/check-access",
            {"dataset": "fish2", "permission": "view"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["allowed"])
        self.assertEqual(data["reason"], "tos_required")

    def test_tos_accepted_then_allowed(self):
        tos = TOSDocument.objects.create(name="Fish TOS", text="Accept these terms.")
        self.ds.tos = tos
        self.ds.save()
        TOSAcceptance.objects.create(user=self.user, tos_document=tos)
        cache.clear()

        resp = self.client.post(
            "/api/v1/check-access",
            {"dataset": "fish2", "permission": "view"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["allowed"])

    def test_dataset_admin_allowed(self):
        ds2 = Dataset.objects.create(name="private")
        admin_perm, _ = Permission.objects.get_or_create(name="admin")
        Grant.objects.create(user=self.user, dataset=ds2, permission=admin_perm)
        cache.clear()

        resp = self.client.post(
            "/api/v1/check-access",
            {"dataset": "private", "permission": "edit"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["allowed"])
        self.assertEqual(data["reason"], "dataset_admin")

    def test_direct_grant_allowed(self):
        ds2 = Dataset.objects.create(name="granted")
        Grant.objects.create(user=self.user, dataset=ds2, permission=self.perm)
        cache.clear()

        resp = self.client.post(
            "/api/v1/check-access",
            {"dataset": "granted", "permission": "view"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["allowed"])

    def test_missing_dataset_field(self):
        resp = self.client.post(
            "/api/v1/check-access",
            {"permission": "view"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_dataset_not_found(self):
        resp = self.client.post(
            "/api/v1/check-access",
            {"dataset": "nonexistent"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 404)

    def test_version_specific_grant(self):
        ds2 = Dataset.objects.create(name="versioned")
        dv = DatasetVersion.objects.create(
            dataset=ds2, version="v1",
        )
        Grant.objects.create(
            user=self.user, dataset=ds2, dataset_version=dv, permission=self.perm
        )
        cache.clear()

        resp = self.client.post(
            "/api/v1/check-access",
            {"dataset": "versioned", "version": "v1", "permission": "view"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["allowed"])

    def test_service_tos_required(self):
        """Service-specific TOS blocks access via check-access."""
        svc = Service.objects.create(name="celltyping")
        svc_tos = TOSDocument.objects.create(
            name="CT TOS", text="CT terms.", dataset=self.ds, service=svc,
        )
        cache.clear()

        resp = self.client.post(
            "/api/v1/check-access",
            {"dataset": "fish2", "permission": "view", "service": "celltyping"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["allowed"])
        self.assertEqual(data["reason"], "tos_required")
        self.assertEqual(data["tos_id"], svc_tos.pk)
        self.assertEqual(data["service"], "celltyping")

    def test_service_tos_accepted(self):
        """After accepting service TOS, check-access allows."""
        svc = Service.objects.create(name="celltyping")
        svc_tos = TOSDocument.objects.create(
            name="CT TOS", text="CT terms.", dataset=self.ds, service=svc,
        )
        TOSAcceptance.objects.create(user=self.user, tos_document=svc_tos)
        cache.clear()

        resp = self.client.post(
            "/api/v1/check-access",
            {"dataset": "fish2", "permission": "view", "service": "celltyping"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["allowed"])

    def test_service_tos_without_service_param_ignored(self):
        """Without service param, service-specific TOS is not checked."""
        svc = Service.objects.create(name="celltyping")
        TOSDocument.objects.create(
            name="CT TOS", text="CT terms.", dataset=self.ds, service=svc,
        )
        cache.clear()

        resp = self.client.post(
            "/api/v1/check-access",
            {"dataset": "fish2", "permission": "view"},
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["allowed"])
