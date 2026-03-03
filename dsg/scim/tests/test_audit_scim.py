"""Tests that SCIM CRUD operations create correct AuditLog entries."""

import pytest
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import APIKey, AuditLog, Dataset, Group, User, UserGroup
from scim.utils import generate_scim_id


@pytest.mark.django_db
class TestSCIMUserAudit(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.admin = User.objects.create(
            email="admin@example.org", name="admin", admin=True,
        )
        self.api_key = APIKey.objects.create(user=self.admin, key="scim-tok")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_create_user_audit(self):
        self.client.post(
            "/auth/scim/v2/Users",
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": "new@example.org",
                "displayName": "New",
            },
            format="json",
            **self._auth(),
        )
        entry = AuditLog.objects.get(action="user_created")
        assert entry.actor == self.admin
        assert entry.target_type == "User"
        assert entry.after_state["email"] == "new@example.org"

    def test_put_user_audit(self):
        user = User.objects.create(email="put@example.org", name="put")
        user.scim_id = generate_scim_id(user.pk, "User")
        user.save(update_fields=["scim_id"])

        self.client.put(
            f"/auth/scim/v2/Users/{user.scim_id}",
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": "put@example.org",
                "displayName": "Updated",
            },
            format="json",
            **self._auth(),
        )
        entry = AuditLog.objects.get(action="user_updated")
        assert entry.actor == self.admin
        assert entry.target_type == "User"

    def test_patch_user_audit(self):
        user = User.objects.create(email="patch@example.org", name="patch")
        user.scim_id = generate_scim_id(user.pk, "User")
        user.save(update_fields=["scim_id"])

        self.client.patch(
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
        entry = AuditLog.objects.get(action="user_updated")
        assert entry.before_state is not None
        assert entry.after_state is not None

    def test_patch_noop_no_audit(self):
        """A PATCH that changes nothing should not create an audit entry."""
        user = User.objects.create(
            email="noop@example.org", name="noop", display_name="Same",
        )
        user.scim_id = generate_scim_id(user.pk, "User")
        user.save(update_fields=["scim_id"])

        self.client.patch(
            f"/auth/scim/v2/Users/{user.scim_id}",
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {"op": "replace", "path": "displayName", "value": "Same"}
                ],
            },
            format="json",
            **self._auth(),
        )
        assert not AuditLog.objects.filter(action="user_updated").exists()

    def test_delete_user_audit(self):
        user = User.objects.create(email="del@example.org", name="del")
        user.scim_id = generate_scim_id(user.pk, "User")
        user.save(update_fields=["scim_id"])

        self.client.delete(
            f"/auth/scim/v2/Users/{user.scim_id}", **self._auth(),
        )
        entry = AuditLog.objects.get(action="user_deactivated")
        assert entry.before_state["email"] == "del@example.org"
        assert entry.before_state["is_active"] is True


@pytest.mark.django_db
class TestSCIMGroupAudit(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.admin = User.objects.create(
            email="admin@example.org", name="admin", admin=True,
        )
        self.api_key = APIKey.objects.create(user=self.admin, key="scim-tok")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_create_group_audit(self):
        self.client.post(
            "/auth/scim/v2/Groups",
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "researchers",
            },
            format="json",
            **self._auth(),
        )
        entry = AuditLog.objects.get(action="group_created")
        assert entry.after_state["name"] == "researchers"

    def test_create_group_with_members_audit(self):
        member = User.objects.create(email="member@example.org", name="member")
        member.scim_id = generate_scim_id(member.pk, "User")
        member.save(update_fields=["scim_id"])

        self.client.post(
            "/auth/scim/v2/Groups",
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "team",
                "members": [{"value": member.scim_id}],
            },
            format="json",
            **self._auth(),
        )
        assert AuditLog.objects.filter(action="group_created").count() == 1
        member_entry = AuditLog.objects.get(action="member_added")
        assert member_entry.after_state["user"] == "member@example.org"

    def test_patch_add_member_audit(self):
        group = Group.objects.create(name="grp")
        group.scim_id = generate_scim_id(group.pk, "Group")
        group.save(update_fields=["scim_id"])

        member = User.objects.create(email="m@example.org", name="m")
        member.scim_id = generate_scim_id(member.pk, "User")
        member.save(update_fields=["scim_id"])

        self.client.patch(
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
        entry = AuditLog.objects.get(action="member_added")
        assert entry.after_state["user"] == "m@example.org"
        assert entry.after_state["group"] == "grp"

    def test_patch_remove_member_audit(self):
        group = Group.objects.create(name="grp")
        group.scim_id = generate_scim_id(group.pk, "Group")
        group.save(update_fields=["scim_id"])

        member = User.objects.create(email="m@example.org", name="m")
        member.scim_id = generate_scim_id(member.pk, "User")
        member.save(update_fields=["scim_id"])
        UserGroup.objects.create(user=member, group=group)

        self.client.patch(
            f"/auth/scim/v2/Groups/{group.scim_id}",
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "remove",
                        "path": f'members[value eq "{member.scim_id}"]',
                    }
                ],
            },
            format="json",
            **self._auth(),
        )
        entry = AuditLog.objects.get(action="member_removed")
        assert entry.before_state["user"] == "m@example.org"
        assert entry.before_state["group"] == "grp"

    def test_delete_group_audit(self):
        group = Group.objects.create(name="doomed")
        group.scim_id = generate_scim_id(group.pk, "Group")
        group.save(update_fields=["scim_id"])

        member = User.objects.create(email="m@example.org")
        UserGroup.objects.create(user=member, group=group)

        self.client.delete(
            f"/auth/scim/v2/Groups/{group.scim_id}", **self._auth(),
        )
        entry = AuditLog.objects.get(action="group_deleted")
        assert entry.before_state["name"] == "doomed"
        assert "m@example.org" in entry.before_state["members"]


@pytest.mark.django_db
class TestSCIMDatasetAudit(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.admin = User.objects.create(
            email="admin@example.org", name="admin", admin=True,
        )
        self.api_key = APIKey.objects.create(user=self.admin, key="scim-tok")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_create_dataset_audit(self):
        self.client.post(
            "/auth/scim/v2/Datasets",
            {
                "schemas": [
                    "urn:ietf:params:scim:schemas:neuroglancer:1.0:Dataset"
                ],
                "name": "fish",
                "description": "Fish dataset",
            },
            format="json",
            **self._auth(),
        )
        entry = AuditLog.objects.get(action="dataset_created")
        assert entry.after_state["name"] == "fish"

    def test_delete_dataset_audit(self):
        ds = Dataset.objects.create(name="doomed-ds")
        ds.scim_id = generate_scim_id(ds.pk, "Dataset")
        ds.save(update_fields=["scim_id"])

        self.client.delete(
            f"/auth/scim/v2/Datasets/{ds.scim_id}", **self._auth(),
        )
        entry = AuditLog.objects.get(action="dataset_deleted")
        assert entry.before_state["name"] == "doomed-ds"
