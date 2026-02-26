"""Integration tests for SCIM 2.0 CRUD endpoints."""

import pytest
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import APIKey, Dataset, Group, ServiceTable, User, UserGroup


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

    def test_resource_types(self):
        resp = self.client.get("/auth/scim/v2/ResourceTypes", **self._auth())
        self.assertEqual(resp.status_code, 200)
        names = {r["name"] for r in resp.json()}
        self.assertEqual(names, {"User", "Group", "Dataset"})

    def test_schemas(self):
        resp = self.client.get("/auth/scim/v2/Schemas", **self._auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 4)  # User, Extension, Group, Dataset


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

    def test_delete_user_deactivates(self):
        user = User.objects.create(email="del@example.org", name="del")
        from scim.utils import generate_scim_id
        user.scim_id = generate_scim_id(user.pk, "User")
        user.save(update_fields=["scim_id"])

        resp = self.client.delete(
            f"/auth/scim/v2/Users/{user.scim_id}", **self._auth()
        )
        self.assertEqual(resp.status_code, 204)

        # User should be deactivated, not deleted
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_get_nonexistent_user(self):
        resp = self.client.get(
            "/auth/scim/v2/Users/nonexistent-id", **self._auth()
        )
        self.assertEqual(resp.status_code, 404)


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
