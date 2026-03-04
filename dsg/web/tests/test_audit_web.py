"""Tests that web view mutations create correct AuditLog entries."""

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase

from core.models import (
    APIKey,
    AuditLog,
    Dataset,
    DatasetVersion,
    Grant,
    Group,
    GroupDatasetPermission,
    Permission,
    PublicRoot,
    ServiceTable,
    TOSAcceptance,
    TOSDocument,
    User,
    UserGroup,
)


class _AuditWebBase(TestCase):
    """Shared setUp for web audit tests."""

    def setUp(self):
        cache.clear()

        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.edit_perm, _ = Permission.objects.get_or_create(name="edit")
        self.manage_perm, _ = Permission.objects.get_or_create(name="manage")
        self.admin_perm, _ = Permission.objects.get_or_create(name="admin")

        self.admin_user = User.objects.create(
            email="admin@example.org", name="Admin", admin=True,
        )
        self.admin_key = APIKey.objects.create(user=self.admin_user, key="tok-admin")

        self.sc_user = User.objects.create(email="sc@example.org", name="SC")
        self.sc_key = APIKey.objects.create(user=self.sc_user, key="tok-sc")
        self.sc_group = Group.objects.create(name="sc")
        UserGroup.objects.create(user=self.sc_user, group=self.sc_group)

        self.group_admin = User.objects.create(email="lead@example.org", name="Lead")
        self.group_admin_key = APIKey.objects.create(user=self.group_admin, key="tok-lead")

        self.regular = User.objects.create(email="regular@example.org", name="Regular")
        self.regular_key = APIKey.objects.create(user=self.regular, key="tok-regular")

        self.group = Group.objects.create(name="lab")
        UserGroup.objects.create(user=self.group_admin, group=self.group, is_admin=True)
        UserGroup.objects.create(user=self.regular, group=self.group)

        self.dataset = Dataset.objects.create(name="ds")

        # SC has admin on dataset
        Grant.objects.create(
            user=self.sc_user, dataset=self.dataset,
            permission=self.admin_perm, source=Grant.SOURCE_MANUAL,
        )

        # Group admin has manage on dataset (scoped to group)
        Grant.objects.create(
            user=self.group_admin, dataset=self.dataset,
            permission=self.manage_perm, group=self.group,
            source=Grant.SOURCE_MANUAL,
        )

    def _login(self, api_key):
        self.client.cookies[settings.AUTH_COOKIE_NAME] = api_key.key


# ──────────────────────────────────────────────────────────────
# GrantManageView audit tests
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestGrantManageAudit(_AuditWebBase):
    def test_grant_creates_audit_entry(self):
        self._login(self.sc_key)
        self.client.post(f"/web/grants/{self.dataset.name}", {
            "action": "grant",
            "email": "regular@example.org",
            "permission": self.view_perm.pk,
        })
        entry = AuditLog.objects.get(action="grant_created", target_type="Grant")
        assert entry.actor == self.sc_user
        assert entry.after_state["user"] == "regular@example.org"
        assert entry.after_state["dataset"] == "ds"
        assert entry.after_state["permission"] == "view"
        assert entry.after_state["source"] == Grant.SOURCE_MANUAL

    def test_grant_with_version_records_version(self):
        dv = DatasetVersion.objects.create(
            dataset=self.dataset, version="v1",
        )
        self._login(self.sc_key)
        self.client.post(f"/web/grants/{self.dataset.name}", {
            "action": "grant",
            "email": "regular@example.org",
            "permission": self.view_perm.pk,
            "version": dv.pk,
        })
        entry = AuditLog.objects.get(action="grant_created")
        assert entry.after_state["version"] == "v1"

    def test_duplicate_grant_no_audit(self):
        Grant.objects.create(
            user=self.regular, dataset=self.dataset,
            permission=self.view_perm,
        )
        self._login(self.sc_key)
        self.client.post(f"/web/grants/{self.dataset.name}", {
            "action": "grant",
            "email": "regular@example.org",
            "permission": self.view_perm.pk,
        })
        assert not AuditLog.objects.filter(action="grant_created").exists()

    def test_revoke_creates_audit_entry(self):
        grant = Grant.objects.create(
            user=self.regular, dataset=self.dataset,
            permission=self.view_perm, source=Grant.SOURCE_MANUAL,
        )
        self._login(self.sc_key)
        self.client.post(f"/web/grants/{self.dataset.name}", {
            "action": "revoke",
            "grant_id": grant.pk,
        })
        entry = AuditLog.objects.get(action="grant_revoked")
        assert entry.actor == self.sc_user
        assert entry.before_state["user"] == "regular@example.org"
        assert entry.before_state["permission"] == "view"
        assert entry.target_id == str(grant.pk)


# ──────────────────────────────────────────────────────────────
# DatasetAdminManageView audit tests
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestDatasetAdminManageAudit(_AuditWebBase):
    def test_add_dataset_admin_audit(self):
        new_lead = User.objects.create(email="newlead@example.org", name="New Lead")
        self._login(self.sc_key)
        self.client.post(f"/web/dataset-admins/{self.dataset.name}", {
            "action": "add",
            "email": "newlead@example.org",
        })
        entry = AuditLog.objects.get(action="dataset_admin_added")
        assert entry.actor == self.sc_user
        assert entry.after_state["user"] == "newlead@example.org"
        assert entry.after_state["permission"] == "admin"

    def test_remove_dataset_admin_audit(self):
        admin_grant = Grant.objects.get(
            user=self.sc_user, dataset=self.dataset, permission=self.admin_perm,
        )
        self._login(self.admin_key)
        self.client.post(f"/web/dataset-admins/{self.dataset.name}", {
            "action": "remove",
            "grant_id": admin_grant.pk,
        })
        entry = AuditLog.objects.get(action="dataset_admin_removed")
        assert entry.actor == self.admin_user
        assert entry.before_state["user"] == "sc@example.org"
        assert entry.before_state["permission"] == "admin"


# ──────────────────────────────────────────────────────────────
# GroupDashboardView audit tests
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestGroupDashboardAudit(_AuditWebBase):
    def test_group_grant_audit(self):
        self._login(self.group_admin_key)
        self.client.post(f"/web/group/{self.group.name}/", {
            "action": "grant",
            "email": "regular@example.org",
            "dataset": "ds",
            "permission": "view",
        })
        entry = AuditLog.objects.get(action="grant_created")
        assert entry.after_state["group"] == "lab"
        assert entry.after_state["user"] == "regular@example.org"

    def test_group_revoke_audit(self):
        grant = Grant.objects.create(
            user=self.regular, dataset=self.dataset,
            permission=self.view_perm, group=self.group,
        )
        self._login(self.group_admin_key)
        self.client.post(f"/web/group/{self.group.name}/", {
            "action": "revoke",
            "grant_id": grant.pk,
        })
        entry = AuditLog.objects.get(action="grant_revoked")
        assert entry.before_state["group"] == "lab"

    def test_add_member_audit(self):
        new_user = User.objects.create(email="new@example.org")
        self._login(self.group_admin_key)
        self.client.post(f"/web/group/{self.group.name}/", {
            "action": "add_member",
            "email": "new@example.org",
        })
        entry = AuditLog.objects.get(action="member_added")
        assert entry.after_state["user"] == "new@example.org"
        assert entry.after_state["group"] == "lab"

    def test_remove_member_audit_with_cascade(self):
        """Removing a member also logs cascade-deleted grants."""
        Grant.objects.create(
            user=self.regular, dataset=self.dataset,
            permission=self.view_perm, group=self.group,
        )
        ug = UserGroup.objects.get(user=self.regular, group=self.group)
        self._login(self.group_admin_key)
        self.client.post(f"/web/group/{self.group.name}/", {
            "action": "remove_member",
            "member_id": ug.pk,
        })
        # Should have both member_removed and grant_revoked entries
        assert AuditLog.objects.filter(action="member_removed").count() == 1
        revoke = AuditLog.objects.get(action="grant_revoked")
        assert revoke.before_state["reason"] == "member_removed"
        assert revoke.before_state["user"] == "regular@example.org"

        member_entry = AuditLog.objects.get(action="member_removed")
        assert member_entry.before_state["user"] == "regular@example.org"


# ──────────────────────────────────────────────────────────────
# TOSLandingView audit tests
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestTOSLandingAudit(_AuditWebBase):
    def setUp(self):
        super().setUp()
        self.dataset.access_mode = Dataset.ACCESS_PUBLIC
        self.dataset.save()
        self.tos = TOSDocument.objects.create(
            name="Test TOS", text="Terms.",
            dataset=self.dataset, invite_token="tos-tok",
        )
        self.dataset.tos = self.tos
        self.dataset.save()

    def test_tos_acceptance_audit(self):
        self._login(self.regular_key)
        self.client.post("/web/tos/tos-tok/")
        entry = AuditLog.objects.get(action="tos_accepted")
        assert entry.actor == self.regular
        assert entry.after_state["tos_document"] == "Test TOS"
        assert entry.after_state["dataset"] == "ds"

    def test_public_self_service_grant_audit(self):
        self._login(self.regular_key)
        self.client.post("/web/tos/tos-tok/")
        entry = AuditLog.objects.get(action="grant_created")
        assert entry.after_state["source"] == Grant.SOURCE_SELF_SERVICE
        assert entry.after_state["permission"] == "view"

    def test_idempotent_acceptance_no_duplicate_audit(self):
        self._login(self.regular_key)
        self.client.post("/web/tos/tos-tok/")
        self.client.post("/web/tos/tos-tok/")
        assert AuditLog.objects.filter(action="tos_accepted").count() == 1
        assert AuditLog.objects.filter(action="grant_created").count() == 1


# ──────────────────────────────────────────────────────────────
# Service Table & Public Root audit tests
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestServiceTableAudit(_AuditWebBase):
    def test_add_service_table_audit(self):
        self._login(self.sc_key)
        self.client.post(f"/web/public-roots/{self.dataset.name}", {
            "action": "add_service_table",
            "service_name": "pcg",
            "table_name": "tbl",
        })
        entry = AuditLog.objects.get(action="service_table_added")
        assert entry.actor == self.sc_user
        assert entry.after_state["service_name"] == "pcg"
        assert entry.after_state["table_name"] == "tbl"
        assert entry.after_state["dataset"] == "ds"

    def test_remove_service_table_audit(self):
        st = ServiceTable.objects.create(
            service_name="pcg", table_name="tbl", dataset=self.dataset,
        )
        self._login(self.sc_key)
        self.client.post(f"/web/public-roots/{self.dataset.name}", {
            "action": "remove_service_table",
            "service_table_id": st.pk,
        })
        entry = AuditLog.objects.get(action="service_table_removed")
        assert entry.actor == self.sc_user
        assert entry.before_state["service_name"] == "pcg"
        assert entry.before_state["table_name"] == "tbl"

    def test_add_public_root_audit(self):
        st = ServiceTable.objects.create(
            service_name="pcg", table_name="tbl", dataset=self.dataset,
        )
        self._login(self.sc_key)
        self.client.post(f"/web/public-roots/{self.dataset.name}", {
            "action": "add",
            "service_table": st.pk,
            "root_id": "999",
        })
        entry = AuditLog.objects.get(action="public_root_added")
        assert entry.actor == self.sc_user
        assert entry.after_state["root_id"] == 999
        assert entry.after_state["dataset"] == "ds"

    def test_remove_public_root_audit(self):
        st = ServiceTable.objects.create(
            service_name="pcg", table_name="tbl", dataset=self.dataset,
        )
        pr = PublicRoot.objects.create(service_table=st, root_id=42)
        self._login(self.sc_key)
        self.client.post(f"/web/public-roots/{self.dataset.name}", {
            "action": "remove",
            "public_root_id": pr.pk,
        })
        entry = AuditLog.objects.get(action="public_root_removed")
        assert entry.actor == self.sc_user
        assert entry.before_state["root_id"] == 42
