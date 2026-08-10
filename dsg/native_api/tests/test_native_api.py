"""Tests for the DSG-native /api/dsg/v1 surface."""

from urllib.parse import urlsplit
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import (
    APIKey,
    AuditLog,
    BucketIAMBinding,
    Dataset,
    DatasetAlias,
    DatasetBucket,
    DatasetVersion,
    Group,
    Grant,
    Permission,
    Service,
    ServiceAccount,
    ServiceAccountGrant,
    ServiceAccountToken,
    TOSAcceptance,
    TOSDocument,
    User,
    UserGroup,
)


@pytest.mark.django_db
class TestNativeUser(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def _auth(self, key):
        return {"HTTP_AUTHORIZATION": f"Bearer {key}"}

    def test_human_user_shape(self):
        user = User.objects.create(
            email="user@example.org",
            name="User Example",
            picture_url="https://example.org/avatar.png",
        )
        token = APIKey.objects.create(user=user, key="tok-human")
        group = Group.objects.create(name="researchers")
        UserGroup.objects.create(user=user, group=group)

        resp = self.client.get("/api/dsg/v1/user", **self._auth(token.key))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {
            "id": user.pk,
            "email": "user@example.org",
            "name": "User Example",
            "picture_url": "https://example.org/avatar.png",
            "admin": False,
            "service_account": False,
            "groups": ["researchers"],
        })


    def test_dedicated_service_account_token_shape(self):
        service_account = ServiceAccount.objects.create(name="pipeline")
        token = ServiceAccountToken.objects.create(
            service_account=service_account,
            key="tok-dedicated-sa",
            description="native user",
        )

        resp = self.client.get("/api/dsg/v1/user", **self._auth(token.key))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {
            "id": service_account.pk,
            "email": None,
            "name": "pipeline",
            "picture_url": None,
            "admin": False,
            "service_account": True,
            "groups": [],
        })

    def test_admin_flag(self):
        user = User.objects.create(email="admin@example.org", name="Admin", admin=True)
        token = APIKey.objects.create(user=user, key="tok-admin-user")

        resp = self.client.get("/api/dsg/v1/user", **self._auth(token.key))

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["admin"])

    def test_unauthenticated_returns_401(self):
        resp = self.client.get("/api/dsg/v1/user")

        self.assertEqual(resp.status_code, 401)


@pytest.mark.django_db
class TestNativeGroupMembers(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create(email="user@example.org")
        self.token = APIKey.objects.create(user=self.user, key="tok-native-groups")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.token.key}"}

    def test_member_emails(self):
        group = Group.objects.create(name="annotation-team")
        alice = User.objects.create(email="alice@example.org")
        bob = User.objects.create(email="bob@example.org")
        UserGroup.objects.create(user=alice, group=group)
        UserGroup.objects.create(user=bob, group=group)

        resp = self.client.get(
            "/api/dsg/v1/groups/annotation-team/members", **self._auth()
        )

        self.assertEqual(resp.status_code, 200)
        self.assertCountEqual(resp.json(), ["alice@example.org", "bob@example.org"])

    def test_unknown_group_returns_404(self):
        resp = self.client.get("/api/dsg/v1/groups/missing/members", **self._auth())

        self.assertEqual(resp.status_code, 404)

    def test_unauthenticated_returns_401(self):
        resp = self.client.get("/api/dsg/v1/groups/annotation-team/members")

        self.assertEqual(resp.status_code, 401)


@pytest.mark.django_db
class TestNativeAuthorize(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.edit_perm, _ = Permission.objects.get_or_create(name="edit")
        self.admin_perm, _ = Permission.objects.get_or_create(name="admin")
        self.named_perm, _ = Permission.objects.get_or_create(name="annotation_editor")

        self.linear = Service.objects.create(name="linear", version_eval_mode="linear")
        self.dag = Service.objects.create(name="dag", version_eval_mode="dag")
        self.other = Service.objects.create(name="other", version_eval_mode="linear")

        self.user = User.objects.create(email="user@example.org", name="User")
        self.api_key = APIKey.objects.create(user=self.user, key="tok-native-user")

        self.dataset = Dataset.objects.create(name="canonical")
        self.v1 = DatasetVersion.objects.create(
            dataset=self.dataset, version="v1", branch="main", ordinal=1
        )
        self.v2 = DatasetVersion.objects.create(
            dataset=self.dataset, version="v2", branch="main", ordinal=2
        )
        self.alt = DatasetVersion.objects.create(
            dataset=self.dataset, version="alt1", branch="alt", ordinal=1
        )
        DatasetAlias.objects.create(
            service=self.linear,
            client_name="client-ds",
            dataset=self.dataset,
        )
        DatasetAlias.objects.create(
            service=self.linear,
            client_name="client-ds",
            client_version="client-v1",
            dataset=self.dataset,
            dataset_version=self.v1,
        )

    def _auth(self, key=None):
        return {"HTTP_AUTHORIZATION": f"Bearer {key or self.api_key.key}"}

    def _post(self, entries, service="linear", key=None):
        payload = {
            "service": service,
            "return_url": "https://service.example.org/return?x=1",
            "entries": entries,
        }
        return self.client.post(
            "/api/dsg/v1/authorize", payload, format="json", **self._auth(key)
        )

    def test_batch_echoes_correlation_keys_and_never_returns_canonical_fields(self):
        Grant.objects.create(
            user=self.user, dataset=self.dataset, dataset_version=self.v1,
            permission=self.view_perm,
        )

        resp = self._post([
            {"name": "client-ds", "version": "client-v1"},
            {"name": "client-ds", "version": "client-v1"},
            {"name": "missing"},
        ])

        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertEqual([entry["decision"] for entry in entries], ["allow", "allow", "deny"])
        self.assertEqual(entries[0]["name"], "client-ds")
        self.assertEqual(entries[0]["version"], "client-v1")
        self.assertEqual(entries[1]["name"], "client-ds")
        for entry in entries:
            self.assertFalse({
                "dataset", "dataset_id", "canonical_dataset",
                "canonical_version", "canonical_branch",
            } & set(entry))

    def test_service_scoped_roles_null_service_grants_and_read_only_stripping(self):
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)
        Grant.objects.create(
            user=self.user, dataset=self.dataset, service=self.linear,
            permission=self.named_perm,
        )

        null_service = self._post([{"name": "canonical"}], service="other").json()["entries"][0]
        self.assertEqual(null_service["decision"], "allow")
        self.assertEqual(null_service["roles"], ["view"])

        named_match = self._post([
            {"name": "canonical", "permission": "annotation_editor"}
        ]).json()["entries"][0]
        self.assertEqual(named_match["decision"], "allow")
        self.assertEqual(named_match["roles"], ["annotation_editor"])

        named_other = self._post([
            {"name": "canonical", "permission": "annotation_editor"}
        ], service="other").json()["entries"][0]
        self.assertEqual(named_other["decision"], "deny")

        self.user.read_only = True
        self.user.save()
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.admin_perm)
        read_only = self._post([{"name": "canonical"}]).json()["entries"][0]
        self.assertEqual(read_only["decision"], "allow")
        self.assertIn("admin", read_only["roles"])
        self.assertNotIn("edit", read_only["roles"])

    def test_anchor_to_root_and_service_eval_modes(self):
        Grant.objects.create(
            user=self.user, dataset=self.dataset, dataset_version=self.v2,
            permission=self.view_perm,
        )

        ancestor = self._post([{"name": "canonical", "version": "v1"}]).json()["entries"][0]
        self.assertEqual(ancestor["decision"], "allow")

        descendant = self._post([
            {"name": "canonical", "version": "3", "branch": "main"}
        ]).json()["entries"][0]
        self.assertEqual(descendant["decision"], "deny")
        self.assertEqual(descendant["branch"], "main")

        dag = self._post([
            {"name": "canonical", "version": "alt1"}
        ], service="dag").json()["entries"][0]
        self.assertEqual(dag["decision"], "service_eval")
        self.assertEqual(dag["anchors"], [
            {"branch": "main", "version": 2, "roles": ["view"]}
        ])

        linear = self._post([
            {"name": "canonical", "version": "alt1"}
        ], service="linear").json()["entries"][0]
        self.assertEqual(linear["decision"], "deny")

        no_grant_user = User.objects.create(email="nogrant@example.org")
        no_grant_key = APIKey.objects.create(user=no_grant_user, key="tok-no-grant")
        no_grant = self._post([
            {"name": "canonical", "version": "alt1"}
        ], service="dag", key=no_grant_key.key).json()["entries"][0]
        self.assertEqual(no_grant["decision"], "deny")
        self.assertNotIn("anchors", no_grant)

    def test_public_self_service_tos_round_trip_and_public_version_metadata_only(self):
        public = Dataset.objects.create(name="public-ds", access_mode=Dataset.ACCESS_PUBLIC)
        tos = TOSDocument.objects.create(name="Public TOS", text="Terms.", dataset=public)
        public.tos = tos
        public.save()

        first = self._post([{"name": "public-ds"}]).json()["entries"][0]
        self.assertEqual(first["decision"], "tos_required")
        self.assertIn("/web/tos/service-check/", first["tos_url"])

        self.client.cookies[settings.AUTH_COOKIE_NAME] = self.api_key.key
        path = urlsplit(first["tos_url"]).path + "?" + urlsplit(first["tos_url"]).query
        self.client.get(path)
        with patch("ngauth.gcs.add_user_to_bucket"), patch("ngauth.gcs.remove_user_from_bucket"):
            resp = self.client.post("/web/tos/service-check/", {
                "next": "https://service.example.org/return?x=1",
            })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(TOSAcceptance.objects.filter(user=self.user, tos_document=tos).exists())
        self.assertTrue(Grant.objects.filter(
            user=self.user, dataset=public, source=Grant.SOURCE_SELF_SERVICE,
        ).exists())
        self.assertEqual(AuditLog.objects.filter(action="grant_created").count(), 1)

        second = self._post([{"name": "public-ds"}]).json()["entries"][0]
        self.assertEqual(second["decision"], "allow")
        self.assertEqual(second["roles"], ["view"])

        no_tos = Dataset.objects.create(name="open-ds", access_mode=Dataset.ACCESS_PUBLIC)
        open_decision = self._post([{"name": no_tos.name}]).json()["entries"][0]
        self.assertEqual(open_decision["decision"], "allow")
        self.assertEqual(open_decision["roles"], ["view"])

        closed = Dataset.objects.create(name="closed-public-version")
        DatasetVersion.objects.create(dataset=closed, version="v1", is_public=True)
        closed_decision = self._post([
            {"name": closed.name, "version": "v1"}
        ]).json()["entries"][0]
        self.assertEqual(closed_decision["decision"], "deny")

    def test_version_tos_round_trip_provisions_anchor_bucket(self):
        bucket = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-v1")
        self.v1.buckets.add(bucket)
        Grant.objects.create(
            user=self.user, dataset=self.dataset, dataset_version=self.v1,
            permission=self.view_perm,
        )
        version_tos = TOSDocument.objects.create(
            name="Version TOS", text="Version terms.", dataset_version=self.v1,
        )

        first = self._post([
            {"name": "canonical", "version": "v1"}
        ]).json()["entries"][0]
        self.assertEqual(first["decision"], "tos_required")
        self.assertIn("version=v1", first["tos_url"])

        self.client.cookies[settings.AUTH_COOKIE_NAME] = self.api_key.key
        path = urlsplit(first["tos_url"]).path + "?" + urlsplit(first["tos_url"]).query
        get_resp = self.client.get(path)
        self.assertContains(get_resp, "Version TOS")

        with patch("ngauth.gcs.add_user_to_bucket") as mock_add, \
             patch("ngauth.gcs.remove_user_from_bucket"):
            mock_add.return_value = "created"
            resp = self.client.post("/web/tos/service-check/", {
                "next": "https://service.example.org/return?x=1",
            })

        self.assertEqual(resp.status_code, 302)
        mock_add.assert_called_with("bucket-v1", "user@example.org")
        self.assertTrue(BucketIAMBinding.objects.filter(
            bucket_name="bucket-v1", email="user@example.org",
        ).exists())
        self.assertTrue(TOSAcceptance.objects.filter(
            user=self.user, tos_document=version_tos,
        ).exists())
        second = self._post([
            {"name": "canonical", "version": "v1"}
        ]).json()["entries"][0]
        self.assertEqual(second["decision"], "allow")

    def test_service_account_uses_sa_grants_and_skips_tos(self):
        tos = TOSDocument.objects.create(name="Dataset TOS", text="Terms.", dataset=self.dataset)
        self.dataset.tos = tos
        self.dataset.save()
        sa = ServiceAccount.objects.create(name="pipeline")
        token = ServiceAccountToken.objects.create(
            service_account=sa, key="tok-native-sa", description="native",
        )
        ServiceAccountGrant.objects.create(
            service_account=sa, dataset=self.dataset, permission=self.view_perm,
        )

        allowed = self._post([
            {"name": "canonical"}
        ], key=token.key).json()["entries"][0]
        self.assertEqual(allowed["decision"], "allow")

        denied = self._post([
            {"name": "missing"}
        ], key=token.key).json()["entries"][0]
        self.assertEqual(denied["decision"], "deny")

    def test_logging_gate(self):
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)
        with patch("native_api.views.logger.debug") as mock_debug:
            self._post([{"name": "canonical"}])
        mock_debug.assert_not_called()

        with override_settings(DSG_LOG_LEVEL="DEBUG"):
            with patch("native_api.views.logger.debug") as mock_debug:
                self._post([{"name": "canonical"}, {"name": "missing"}])
        self.assertEqual(mock_debug.call_count, 2)
        reasons = [call.args[-1] for call in mock_debug.call_args_list]
        self.assertEqual(reasons, ["covered", "unknown-alias"])


@pytest.mark.django_db
class TestNativeMetadata(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.user = User.objects.create(email="metadata@example.org")
        self.api_key = APIKey.objects.create(user=self.user, key="tok-native-metadata")
        self.service = Service.objects.create(name="linear")

        self.granted = Dataset.objects.create(name="canonical")
        self.v1 = DatasetVersion.objects.create(
            dataset=self.granted, version="v1", branch="main", ordinal=1,
        )
        self.v2 = DatasetVersion.objects.create(
            dataset=self.granted, version="v2", branch="main", ordinal=2,
            is_public=True,
        )
        DatasetAlias.objects.create(
            service=self.service, client_name="client-ds", dataset=self.granted,
        )
        DatasetAlias.objects.create(
            service=self.service, client_name="client-ds", client_version="client-v1",
            dataset=self.granted, dataset_version=self.v1,
        )
        Grant.objects.create(user=self.user, dataset=self.granted, permission=self.view_perm)

        self.public = Dataset.objects.create(
            name="public-ds", access_mode=Dataset.ACCESS_PUBLIC,
        )
        self.public_version_only = Dataset.objects.create(name="version-public")
        DatasetVersion.objects.create(
            dataset=self.public_version_only, version="pub", is_public=True,
        )
        self.hidden = Dataset.objects.create(name="hidden")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_datasets_list_uses_service_vocabulary_and_visibility_rule(self):
        resp = self.client.get("/api/dsg/v1/datasets?service=linear", **self._auth())
        self.assertEqual(resp.status_code, 200)
        rows = {row["name"]: row for row in resp.json()}
        self.assertIn("client-ds", rows)
        self.assertIn("public-ds", rows)
        self.assertIn("version-public", rows)
        self.assertNotIn("hidden", rows)
        self.assertEqual(rows["client-ds"]["access_mode"], Dataset.ACCESS_CLOSED)
        self.assertTrue(rows["client-ds"]["has_public_versions"])

    def test_versions_endpoint_returns_registered_anchors_and_hides_closed_dataset(self):
        resp = self.client.get(
            "/api/dsg/v1/datasets/client-ds/versions?service=linear",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()
        by_version = {row["version"]: row for row in rows}
        self.assertEqual(by_version["client-v1"]["branch"], "main")
        self.assertEqual(by_version["client-v1"]["ordinal"], 1)
        self.assertFalse(by_version["client-v1"]["is_public"])
        self.assertTrue(by_version["v2"]["is_public"])

        hidden = self.client.get(
            "/api/dsg/v1/datasets/hidden/versions?service=linear",
            **self._auth(),
        )
        self.assertEqual(hidden.status_code, 404)
