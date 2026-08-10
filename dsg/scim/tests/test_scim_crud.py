"""Integration tests for SCIM 2.0 CRUD endpoints."""

from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import (
    APIKey,
    BucketIAMBinding,
    Dataset,
    DatasetBucket,
    Grant,
    Group,
    Permission,
    ServiceTable,
    User,
    UserGroup,
)


@pytest.mark.django_db
class TestSCIMDiscovery(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.admin = User.objects.create(
            email="admin@example.org", name="admin", admin=True
        )
        self.api_key = APIKey.objects.create(user=self.admin, key="scim-tok")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_service_provider_config(self):
        resp = self.client.get("/auth/scim/v2/ServiceProviderConfig", **self._auth())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["filter"]["supported"])
        self.assertTrue(data["patch"]["supported"])
        self.assertTrue(data["etag"]["supported"])

    def test_resource_types(self):
        resp = self.client.get("/auth/scim/v2/ResourceTypes", **self._auth())
        self.assertEqual(resp.status_code, 200)
        names = {r["name"] for r in resp.json()}
        self.assertEqual(names, {"User", "Group", "Dataset"})

    def test_schemas(self):
        resp = self.client.get("/auth/scim/v2/Schemas", **self._auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 4)  # User, Extension, Group, Dataset

    def test_etag_in_meta(self):
        from scim.utils import generate_scim_id

        # User meta should have version
        user = User.objects.create(email="etag@example.org", name="etag")
        user.scim_id = generate_scim_id(user.pk, "User")
        user.save(update_fields=["scim_id"])
        resp = self.client.get(f"/auth/scim/v2/Users/{user.scim_id}", **self._auth())
        self.assertIn("version", resp.json()["meta"])
        self.assertTrue(resp.json()["meta"]["version"].startswith('W/"'))

        # Group meta should have version
        group = Group.objects.create(name="etag-grp")
        group.scim_id = generate_scim_id(group.pk, "Group")
        group.save(update_fields=["scim_id"])
        resp = self.client.get(f"/auth/scim/v2/Groups/{group.scim_id}", **self._auth())
        self.assertIn("version", resp.json()["meta"])

        # Dataset meta should have version
        ds = Dataset.objects.create(name="etag-ds")
        ds.scim_id = generate_scim_id(ds.pk, "Dataset")
        ds.save(update_fields=["scim_id"])
        resp = self.client.get(f"/auth/scim/v2/Datasets/{ds.scim_id}", **self._auth())
        self.assertIn("version", resp.json()["meta"])


@pytest.mark.django_db
class TestSCIMNonAdmin(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create(
            email="user@example.org", name="user", admin=False
        )
        self.api_key = APIKey.objects.create(user=self.user, key="non-admin-tok")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_non_admin_rejected(self):
        resp = self.client.get("/auth/scim/v2/Users", **self._auth())
        self.assertEqual(resp.status_code, 401)


@pytest.mark.django_db
class TestSCIMAdminEnabledGate(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.admin_user = User.objects.create(
            email="admin-gate@example.org", name="admin-gate", admin=True,
        )
        self.api_key = APIKey.objects.create(user=self.admin_user, key="admin-gate-tok")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def _get_config(self):
        return self.client.get("/auth/scim/v2/ServiceProviderConfig", **self._auth())

    def test_admin_requires_enabled_account(self):
        resp = self._get_config()
        self.assertEqual(resp.status_code, 200)

        self.admin_user.is_active = False
        self.admin_user.save()
        resp = self._get_config()
        self.assertEqual(resp.status_code, 401)

        self.admin_user.is_active = True
        self.admin_user.save()
        resp = self._get_config()
        self.assertEqual(resp.status_code, 200)


@pytest.mark.django_db
class TestSCIMUserCRUD(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.admin = User.objects.create(
            email="admin@example.org", name="admin", admin=True
        )
        self.api_key = APIKey.objects.create(user=self.admin, key="scim-tok")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_create_and_get_user(self):
        # Create
        resp = self.client.post(
            "/auth/scim/v2/Users",
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": "newuser@example.org",
                "displayName": "New User",
                "active": True,
            },
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 201)
        scim_id = resp.json()["id"]
        self.assertTrue(scim_id)

        # Get
        resp = self.client.get(f"/auth/scim/v2/Users/{scim_id}", **self._auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["userName"], "newuser@example.org")

    def test_create_duplicate_user(self):
        User.objects.create(email="dup@example.org", name="dup")
        resp = self.client.post(
            "/auth/scim/v2/Users",
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": "dup@example.org",
            },
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 409)

    def test_list_users(self):
        resp = self.client.get("/auth/scim/v2/Users", **self._auth())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("totalResults", data)
        self.assertIn("Resources", data)

    def test_put_user(self):
        # Create a user first
        user = User.objects.create(email="put@example.org", name="put")
        from scim.utils import generate_scim_id
        user.scim_id = generate_scim_id(user.pk, "User")
        user.save(update_fields=["scim_id"])

        resp = self.client.put(
            f"/auth/scim/v2/Users/{user.scim_id}",
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": "put@example.org",
                "displayName": "Updated Name",
            },
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["displayName"], "Updated Name")

    def test_patch_user(self):
        user = User.objects.create(email="patch@example.org", name="patch")
        from scim.utils import generate_scim_id
        user.scim_id = generate_scim_id(user.pk, "User")
        user.save(update_fields=["scim_id"])

        resp = self.client.patch(
            f"/auth/scim/v2/Users/{user.scim_id}",
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {"op": "replace", "path": "displayName", "value": "Patched"}
                ],
            },
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["displayName"], "Patched")

    def test_patch_add_group(self):
        user = User.objects.create(email="grpmem@example.org", name="grpmem")
        from scim.utils import generate_scim_id
        user.scim_id = generate_scim_id(user.pk, "User")
        user.save(update_fields=["scim_id"])

        group = Group.objects.create(name="scientists")
        group.scim_id = generate_scim_id(group.pk, "Group")
        group.save(update_fields=["scim_id"])

        resp = self.client.patch(
            f"/auth/scim/v2/Users/{user.scim_id}",
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "add",
                        "path": "groups",
                        "value": [{"value": group.scim_id}],
                    }
                ],
            },
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(UserGroup.objects.filter(user=user, group=group).exists())

    def test_patch_remove_group(self):
        user = User.objects.create(email="rmgrp@example.org", name="rmgrp")
        from scim.utils import generate_scim_id
        user.scim_id = generate_scim_id(user.pk, "User")
        user.save(update_fields=["scim_id"])

        group = Group.objects.create(name="toremove")
        group.scim_id = generate_scim_id(group.pk, "Group")
        group.save(update_fields=["scim_id"])

        UserGroup.objects.create(user=user, group=group)

        resp = self.client.patch(
            f"/auth/scim/v2/Users/{user.scim_id}",
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "remove",
                        "path": f'groups[value eq "{group.scim_id}"]',
                    }
                ],
            },
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(UserGroup.objects.filter(user=user, group=group).exists())

    @patch("ngauth.gcs.remove_user_from_bucket", return_value=True)
    def test_delete_user_deprovisions_owned_bindings(self, mock_remove):
        user = User.objects.create(email="del@example.org", name="del")
        BucketIAMBinding.objects.create(bucket_name="bucket-a", email=user.email)
        from scim.utils import generate_scim_id
        user.scim_id = generate_scim_id(user.pk, "User")
        user.save(update_fields=["scim_id"])

        resp = self.client.delete(
            f"/auth/scim/v2/Users/{user.scim_id}", **self._auth()
        )
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(
            BucketIAMBinding.objects.filter(email="del@example.org").exists()
        )
        self.assertEqual(
            {(c.args[0], c.args[1]) for c in mock_remove.call_args_list},
            {("bucket-a", "del@example.org")},
        )

    def test_get_nonexistent_user(self):
        resp = self.client.get(
            "/auth/scim/v2/Users/nonexistent-id", **self._auth()
        )
        self.assertEqual(resp.status_code, 404)


@pytest.mark.django_db
class TestSCIMUserFlagIAM(TestCase):
    """SCIM is_active/admin flips fan out per-user bucket IAM inline,
    including the user's user-type service accounts."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.admin = User.objects.create(
            email="admin@example.org", name="admin", admin=True
        )
        self.api_key = APIKey.objects.create(user=self.admin, key="scim-tok")

        from scim.utils import generate_scim_id
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.ds_a = Dataset.objects.create(name="ds-a")
        DatasetBucket.objects.create(dataset=self.ds_a, name="bucket-a")
        self.user = User.objects.create(email="user@example.org", name="user")
        self.user.scim_id = generate_scim_id(self.user.pk, "User")
        self.user.save(update_fields=["scim_id"])
        Grant.objects.create(user=self.user, dataset=self.ds_a, permission=self.view_perm)
        BucketIAMBinding.objects.create(bucket_name="bucket-a", email="user@example.org")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def _patch_active(self, value):
        return self.client.patch(
            f"/auth/scim/v2/Users/{self.user.scim_id}",
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {"op": "replace", "path": "active", "value": value}
                ],
            },
            format="json",
            **self._auth(),
        )

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_patch_active_false_removes_user_iam(self, mock_remove, mock_add):
        mock_remove.return_value = True
        resp = self._patch_active(False)
        self.assertEqual(resp.status_code, 200)
        removed = {(c.args[0], c.args[1]) for c in mock_remove.call_args_list}
        self.assertEqual(removed, {("bucket-a", "user@example.org")})
        mock_add.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_patch_active_true_readds_user_iam(self, mock_remove, mock_add):
        mock_add.return_value = "created"
        self.user.is_active = False
        self.user.save()
        resp = self._patch_active(True)
        self.assertEqual(resp.status_code, 200)
        added = {(c.args[0], c.args[1]) for c in mock_add.call_args_list}
        self.assertEqual(added, {("bucket-a", "user@example.org")})
        mock_remove.assert_not_called()

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_put_admin_flip_resyncs(self, mock_remove, mock_add):
        from scim.serializers import USER_EXTENSION
        mock_add.return_value = "created"
        mock_remove.return_value = True
        resp = self.client.put(
            f"/auth/scim/v2/Users/{self.user.scim_id}",
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User", USER_EXTENSION],
                "userName": self.user.email,
                USER_EXTENSION: {"admin": True},
            },
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        # Promoted admins are never provisioned per-user
        removed = {(c.args[0], c.args[1]) for c in mock_remove.call_args_list}
        self.assertEqual(removed, {("bucket-a", "user@example.org")})

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_patch_unrelated_change_does_not_sync(self, mock_remove, mock_add):
        resp = self.client.patch(
            f"/auth/scim/v2/Users/{self.user.scim_id}",
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {"op": "replace", "path": "displayName", "value": "Renamed"}
                ],
            },
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        mock_add.assert_not_called()
        mock_remove.assert_not_called()


@pytest.mark.django_db
class TestSCIMGroupCRUD(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.admin = User.objects.create(
            email="admin@example.org", name="admin", admin=True
        )
        self.api_key = APIKey.objects.create(user=self.admin, key="scim-tok")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_create_and_get_group(self):
        resp = self.client.post(
            "/auth/scim/v2/Groups",
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "researchers",
            },
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 201)
        scim_id = resp.json()["id"]

        resp = self.client.get(f"/auth/scim/v2/Groups/{scim_id}", **self._auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["displayName"], "researchers")

    def test_create_group_with_members(self):
        member = User.objects.create(email="member@example.org", name="member")
        from scim.utils import generate_scim_id
        member.scim_id = generate_scim_id(member.pk, "User")
        member.save(update_fields=["scim_id"])

        resp = self.client.post(
            "/auth/scim/v2/Groups",
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "team",
                "members": [{"value": member.scim_id}],
            },
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 201)
        members = resp.json().get("members", [])
        self.assertEqual(len(members), 1)

    def test_patch_add_member(self):
        group = Group.objects.create(name="grp")
        from scim.utils import generate_scim_id
        group.scim_id = generate_scim_id(group.pk, "Group")
        group.save(update_fields=["scim_id"])

        member = User.objects.create(email="m@example.org", name="m")
        member.scim_id = generate_scim_id(member.pk, "User")
        member.save(update_fields=["scim_id"])

        resp = self.client.patch(
            f"/auth/scim/v2/Groups/{group.scim_id}",
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "add",
                        "path": "members",
                        "value": [{"value": member.scim_id}],
                    }
                ],
            },
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            UserGroup.objects.filter(user=member, group=group).exists()
        )

    def test_delete_group(self):
        group = Group.objects.create(name="to-delete")
        from scim.utils import generate_scim_id
        group.scim_id = generate_scim_id(group.pk, "Group")
        group.save(update_fields=["scim_id"])

        resp = self.client.delete(
            f"/auth/scim/v2/Groups/{group.scim_id}", **self._auth()
        )
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Group.objects.filter(pk=group.pk).exists())


@pytest.mark.django_db
class TestSCIMDatasetCRUD(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.admin = User.objects.create(
            email="admin@example.org", name="admin", admin=True
        )
        self.api_key = APIKey.objects.create(user=self.admin, key="scim-tok")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_create_dataset_with_service_tables(self):
        resp = self.client.post(
            "/auth/scim/v2/Datasets",
            {
                "schemas": [
                    "urn:ietf:params:scim:schemas:neuroglancer:1.0:Dataset"
                ],
                "name": "fish2",
                "description": "Fish dataset",
                "serviceTables": [
                    {"serviceName": "pychunkedgraph", "tableName": "fish2_v1"},
                ],
            },
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["name"], "fish2")
        self.assertTrue(
            ServiceTable.objects.filter(
                service_name="pychunkedgraph", table_name="fish2_v1"
            ).exists()
        )

    def test_list_datasets(self):
        Dataset.objects.create(name="ds1")
        Dataset.objects.create(name="ds2")
        resp = self.client.get("/auth/scim/v2/Datasets", **self._auth())
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(resp.json()["totalResults"], 2)

    def test_patch_add_service_table(self):
        ds = Dataset.objects.create(name="patch-st")
        from scim.utils import generate_scim_id
        ds.scim_id = generate_scim_id(ds.pk, "Dataset")
        ds.save(update_fields=["scim_id"])

        resp = self.client.patch(
            f"/auth/scim/v2/Datasets/{ds.scim_id}",
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "add",
                        "path": "serviceTables",
                        "value": [
                            {"serviceName": "annotation", "tableName": "patch_tbl"},
                        ],
                    }
                ],
            },
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            ServiceTable.objects.filter(
                dataset=ds, service_name="annotation", table_name="patch_tbl"
            ).exists()
        )

    def test_patch_remove_service_table(self):
        ds = Dataset.objects.create(name="rm-st")
        from scim.utils import generate_scim_id
        ds.scim_id = generate_scim_id(ds.pk, "Dataset")
        ds.save(update_fields=["scim_id"])

        ServiceTable.objects.create(
            dataset=ds, service_name="pychunkedgraph", table_name="rm_tbl"
        )

        resp = self.client.patch(
            f"/auth/scim/v2/Datasets/{ds.scim_id}",
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "remove",
                        "path": 'serviceTables[serviceName eq "pychunkedgraph"]',
                    }
                ],
            },
            format="json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            ServiceTable.objects.filter(
                dataset=ds, service_name="pychunkedgraph"
            ).exists()
        )

    def test_delete_dataset(self):
        ds = Dataset.objects.create(name="to-delete")
        from scim.utils import generate_scim_id
        ds.scim_id = generate_scim_id(ds.pk, "Dataset")
        ds.save(update_fields=["scim_id"])

        resp = self.client.delete(
            f"/auth/scim/v2/Datasets/{ds.scim_id}", **self._auth()
        )
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Dataset.objects.filter(pk=ds.pk).exists())
