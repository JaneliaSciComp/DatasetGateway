"""Tests for web access-control user stories — organized by role."""

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase

from core.models import (
    APIKey,
    Dataset,
    DatasetBucket,
    DatasetVersion,
    Grant,
    Group,
    GroupDatasetPermission,
    Permission,
    PublicRoot,
    Service,
    ServiceTable,
    TOSAcceptance,
    TOSDocument,
    User,
    UserGroup,
)


def _auth_cookies(api_key):
    """Return a dict suitable for client.cookies to authenticate via dsg_token."""
    return {settings.AUTH_COOKIE_NAME: api_key.key}


class _WebTestBase(TestCase):
    """Shared setUp for web story tests."""

    def setUp(self):
        cache.clear()

        # Permissions
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.edit_perm, _ = Permission.objects.get_or_create(name="edit")
        self.manage_perm, _ = Permission.objects.get_or_create(name="manage")
        self.admin_perm, _ = Permission.objects.get_or_create(name="admin")

        # Users
        self.global_admin = User.objects.create(email="admin@example.org", name="Admin", admin=True)
        self.global_admin_key = APIKey.objects.create(user=self.global_admin, key="tok-admin")

        self.sc_user = User.objects.create(email="sc@example.org", name="SC Member")
        self.sc_key = APIKey.objects.create(user=self.sc_user, key="tok-sc")

        self.group_admin_a = User.objects.create(email="lead-a@example.org", name="Group Admin A")
        self.group_admin_a_key = APIKey.objects.create(user=self.group_admin_a, key="tok-lead-a")

        self.group_admin_b = User.objects.create(email="lead-b@example.org", name="Group Admin B")
        self.group_admin_b_key = APIKey.objects.create(user=self.group_admin_b, key="tok-lead-b")

        self.regular_user = User.objects.create(email="regular@example.org", name="Regular")
        self.regular_key = APIKey.objects.create(user=self.regular_user, key="tok-regular")

        # Groups
        self.sc_group = Group.objects.create(name="sc")
        UserGroup.objects.create(user=self.sc_user, group=self.sc_group)

        self.group_a = Group.objects.create(name="alpha-lab")
        UserGroup.objects.create(user=self.group_admin_a, group=self.group_a, is_admin=True)
        UserGroup.objects.create(user=self.regular_user, group=self.group_a)

        self.group_b = Group.objects.create(name="beta-lab")
        UserGroup.objects.create(user=self.group_admin_b, group=self.group_b, is_admin=True)

        # Dataset
        self.dataset = Dataset.objects.create(name="test-dataset")

        # SC user has admin grant on dataset
        Grant.objects.create(
            user=self.sc_user, dataset=self.dataset,
            permission=self.admin_perm, source=Grant.SOURCE_MANUAL,
        )

        # Group admin A has manage grant on dataset (scoped to group_a)
        Grant.objects.create(
            user=self.group_admin_a, dataset=self.dataset,
            permission=self.manage_perm, group=self.group_a,
            source=Grant.SOURCE_MANUAL,
        )

    def _login(self, api_key):
        self.client.cookies[settings.AUTH_COOKIE_NAME] = api_key.key


# ──────────────────────────────────────────────────────────────
# Permission hierarchy tests
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestPermissionHierarchy(_WebTestBase):
    """Test that permission hierarchy expansion works correctly in cache."""

    def _get_cache(self, user):
        from core.cache import build_permission_cache
        return build_permission_cache(user)

    def test_admin_grant_expands_to_all_permissions(self):
        perm_cache = self._get_cache(self.sc_user)
        perms = perm_cache["permissions_v2"]["test-dataset"]
        self.assertIn("admin", perms)
        self.assertIn("manage", perms)
        self.assertIn("edit", perms)
        self.assertIn("view", perms)

    def test_manage_grant_expands_to_manage_edit_view(self):
        perm_cache = self._get_cache(self.group_admin_a)
        perms = perm_cache["permissions_v2"]["test-dataset"]
        self.assertIn("manage", perms)
        self.assertIn("edit", perms)
        self.assertIn("view", perms)
        self.assertNotIn("admin", perms)

    def test_edit_grant_expands_to_edit_view(self):
        user = User.objects.create(email="editor@example.org")
        Grant.objects.create(user=user, dataset=self.dataset, permission=self.edit_perm)
        perm_cache = self._get_cache(user)
        perms = perm_cache["permissions_v2"]["test-dataset"]
        self.assertIn("edit", perms)
        self.assertIn("view", perms)
        self.assertNotIn("manage", perms)

    def test_view_grant_only_view(self):
        user = User.objects.create(email="viewer@example.org")
        Grant.objects.create(user=user, dataset=self.dataset, permission=self.view_perm)
        perm_cache = self._get_cache(user)
        perms = perm_cache["permissions_v2"]["test-dataset"]
        self.assertEqual(perms, ["view"])

    def test_read_only_manage_keeps_manage_view_loses_edit(self):
        self.group_admin_a.read_only = True
        self.group_admin_a.save()
        perm_cache = self._get_cache(self.group_admin_a)
        perms = perm_cache["permissions_v2"]["test-dataset"]
        self.assertIn("manage", perms)
        self.assertIn("view", perms)
        self.assertNotIn("edit", perms)

    def test_admin_numeric_level(self):
        perm_cache = self._get_cache(self.sc_user)
        level = perm_cache["permissions"]["test-dataset"]
        self.assertEqual(level, 4)  # admin = 4

    def test_manage_numeric_level(self):
        perm_cache = self._get_cache(self.group_admin_a)
        level = perm_cache["permissions"]["test-dataset"]
        self.assertEqual(level, 3)  # manage = 3


# ──────────────────────────────────────────────────────────────
# Grant group scoping tests
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestGrantGroupScoping(_WebTestBase):
    """Test that group-scoped grants are independent."""

    def test_two_groups_create_distinct_grants(self):
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, group=self.group_a,
        )
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, group=self.group_b,
        )
        count = Grant.objects.filter(
            user=self.regular_user, dataset=self.dataset, permission=self.view_perm
        ).count()
        self.assertEqual(count, 2)

    def test_revoking_one_group_grant_preserves_other(self):
        g1 = Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, group=self.group_a,
        )
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, group=self.group_b,
        )
        g1.delete()
        self.assertTrue(Grant.objects.filter(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, group=self.group_b,
        ).exists())

    def test_cache_unions_both_group_grants(self):
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, group=self.group_a,
        )
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.edit_perm, group=self.group_b,
        )
        from core.cache import build_permission_cache
        perm_cache = build_permission_cache(self.regular_user)
        perms = perm_cache["permissions_v2"]["test-dataset"]
        self.assertIn("view", perms)
        self.assertIn("edit", perms)


# ──────────────────────────────────────────────────────────────
# Group dashboard tests
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestGroupDashboard(_WebTestBase):
    """Group admin manages group members and grants via group dashboard."""

    def test_group_admin_can_view_dashboard(self):
        self._login(self.group_admin_a_key)
        resp = self.client.get(f"/web/group/{self.group_a.name}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "alpha-lab")

    def test_group_admin_sees_group_members(self):
        self._login(self.group_admin_a_key)
        resp = self.client.get(f"/web/group/{self.group_a.name}/")
        self.assertContains(resp, "regular@example.org")

    def test_group_admin_can_grant_view_to_member(self):
        self._login(self.group_admin_a_key)
        resp = self.client.post(f"/web/group/{self.group_a.name}/", {
            "action": "grant",
            "email": "regular@example.org",
            "dataset": "test-dataset",
            "permission": "view",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Grant.objects.filter(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, group=self.group_a,
        ).exists())

    def test_group_admin_can_grant_edit_to_member(self):
        self._login(self.group_admin_a_key)
        self.client.post(f"/web/group/{self.group_a.name}/", {
            "action": "grant",
            "email": "regular@example.org",
            "dataset": "test-dataset",
            "permission": "edit",
        })
        self.assertTrue(Grant.objects.filter(
            user=self.regular_user, dataset=self.dataset,
            permission=self.edit_perm, group=self.group_a,
        ).exists())

    def test_group_admin_can_grant_manage_sub_lead(self):
        self._login(self.group_admin_a_key)
        self.client.post(f"/web/group/{self.group_a.name}/", {
            "action": "grant",
            "email": "regular@example.org",
            "dataset": "test-dataset",
            "permission": "manage",
        })
        self.assertTrue(Grant.objects.filter(
            user=self.regular_user, dataset=self.dataset,
            permission=self.manage_perm, group=self.group_a,
        ).exists())

    def test_group_admin_cannot_grant_admin(self):
        self._login(self.group_admin_a_key)
        resp = self.client.post(f"/web/group/{self.group_a.name}/", {
            "action": "grant",
            "email": "regular@example.org",
            "dataset": "test-dataset",
            "permission": "admin",
        }, follow=True)
        self.assertContains(resp, "Cannot grant a permission level higher than your own")
        self.assertFalse(Grant.objects.filter(
            user=self.regular_user, dataset=self.dataset,
            permission=self.admin_perm,
        ).exists())

    def test_group_admin_cannot_see_other_groups_dashboard(self):
        self._login(self.group_admin_a_key)
        resp = self.client.get(f"/web/group/{self.group_b.name}/")
        self.assertContains(resp, "Access Denied")

    def test_group_admin_cannot_manage_unmanaged_dataset(self):
        other_ds = Dataset.objects.create(name="other-dataset")
        self._login(self.group_admin_a_key)
        resp = self.client.post(f"/web/group/{self.group_a.name}/", {
            "action": "grant",
            "email": "regular@example.org",
            "dataset": "other-dataset",
            "permission": "view",
        }, follow=True)
        self.assertContains(resp, "You do not have manage permission")

    def test_group_admin_can_add_member(self):
        self._login(self.group_admin_a_key)
        new_user = User.objects.create(email="newmember@example.org")
        resp = self.client.post(f"/web/group/{self.group_a.name}/", {
            "action": "add_member",
            "email": "newmember@example.org",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(UserGroup.objects.filter(
            user=new_user, group=self.group_a
        ).exists())

    def test_group_admin_can_add_new_email_creates_user(self):
        self._login(self.group_admin_a_key)
        self.client.post(f"/web/group/{self.group_a.name}/", {
            "action": "add_member",
            "email": "brand-new@example.org",
        })
        new_user = User.objects.get(email="brand-new@example.org")
        self.assertFalse(new_user.has_usable_password())
        self.assertTrue(UserGroup.objects.filter(
            user=new_user, group=self.group_a
        ).exists())

    def test_group_admin_can_remove_member_cascades_grants(self):
        # Give regular_user a group-scoped grant
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, group=self.group_a,
        )
        ug = UserGroup.objects.get(user=self.regular_user, group=self.group_a)
        self._login(self.group_admin_a_key)
        self.client.post(f"/web/group/{self.group_a.name}/", {
            "action": "remove_member",
            "member_id": ug.pk,
        })
        # Member removed
        self.assertFalse(UserGroup.objects.filter(
            user=self.regular_user, group=self.group_a
        ).exists())
        # Group-scoped grants deleted
        self.assertFalse(Grant.objects.filter(
            user=self.regular_user, group=self.group_a
        ).exists())

    def test_regular_user_denied_group_dashboard(self):
        self._login(self.regular_key)
        resp = self.client.get(f"/web/group/{self.group_a.name}/")
        self.assertContains(resp, "Access Denied")


# ──────────────────────────────────────────────────────────────
# SC/Dataset admin tests
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestSCDatasetAdmin(_WebTestBase):
    """SC member (admin grant on dataset) manages all grants."""

    def test_sc_sees_all_grants_across_groups(self):
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, group=self.group_a,
        )
        Grant.objects.create(
            user=self.group_admin_b, dataset=self.dataset,
            permission=self.view_perm, group=self.group_b,
        )
        self._login(self.sc_key)
        resp = self.client.get(f"/web/grants/{self.dataset.name}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "regular@example.org")
        self.assertContains(resp, "lead-b@example.org")
        self.assertContains(resp, "alpha-lab")
        self.assertContains(resp, "beta-lab")

    def test_sc_can_grant_any_permission(self):
        self._login(self.sc_key)
        resp = self.client.post(f"/web/grants/{self.dataset.name}", {
            "action": "grant",
            "email": "regular@example.org",
            "permission": self.edit_perm.pk,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Grant.objects.filter(
            user=self.regular_user, dataset=self.dataset,
            permission=self.edit_perm,
        ).exists())

    def test_sc_can_revoke_any_grant(self):
        g = Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, group=self.group_a,
        )
        self._login(self.sc_key)
        self.client.post(f"/web/grants/{self.dataset.name}", {
            "action": "revoke", "grant_id": g.pk,
        })
        self.assertFalse(Grant.objects.filter(pk=g.pk).exists())

    def test_sc_can_assign_dataset_admins(self):
        self._login(self.sc_key)
        resp = self.client.post(f"/web/grants/{self.dataset.name}", {
            "action": "grant",
            "email": "lead-b@example.org",
            "permission": self.manage_perm.pk,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Grant.objects.filter(
            user=self.group_admin_b, dataset=self.dataset,
            permission=self.manage_perm,
        ).exists())

    def test_sc_cannot_see_grants_on_unassigned_dataset(self):
        other_ds = Dataset.objects.create(name="other-dataset")
        self._login(self.sc_key)
        resp = self.client.get(f"/web/grants/{other_ds.name}")
        self.assertContains(resp, "Access Denied")

    def test_sc_sees_group_attribution(self):
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, group=self.group_a,
        )
        self._login(self.sc_key)
        resp = self.client.get(f"/web/grants/{self.dataset.name}")
        self.assertContains(resp, "alpha-lab")


# ──────────────────────────────────────────────────────────────
# Global admin tests
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestGlobalAdmin(_WebTestBase):
    """Global admin can access any management page."""

    def test_global_admin_can_access_any_dataset_members(self):
        self._login(self.global_admin_key)
        resp = self.client.get(f"/web/grants/{self.dataset.name}")
        self.assertEqual(resp.status_code, 200)

    def test_global_admin_can_access_any_group_dashboard(self):
        self._login(self.global_admin_key)
        resp = self.client.get(f"/web/group/{self.group_a.name}/")
        self.assertEqual(resp.status_code, 200)

    def test_global_admin_sees_all_datasets_in_group_dashboard(self):
        self._login(self.global_admin_key)
        resp = self.client.get(f"/web/group/{self.group_a.name}/")
        # Global admin should see all datasets in the grant dropdown
        self.assertContains(resp, self.dataset.name)

    def test_global_admin_can_grant_via_group_dashboard(self):
        self._login(self.global_admin_key)
        resp = self.client.post(f"/web/group/{self.group_a.name}/", {
            "action": "grant",
            "email": "regular@example.org",
            "dataset": self.dataset.name,
            "permission": "view",
        }, follow=True)
        self.assertTrue(Grant.objects.filter(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, group=self.group_a,
        ).exists())

    def test_global_admin_can_access_unassigned_dataset(self):
        other_ds = Dataset.objects.create(name="other-dataset")
        self._login(self.global_admin_key)
        resp = self.client.get(f"/web/grants/{other_ds.name}")
        self.assertEqual(resp.status_code, 200)


# ──────────────────────────────────────────────────────────────
# Manage-user grant page tests
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestManageUserGrantPage(_WebTestBase):
    """User with manage permission can access /web/grants/<dataset>."""

    def test_manage_user_can_view_grants_page(self):
        self._login(self.group_admin_a_key)
        resp = self.client.get(f"/web/grants/{self.dataset.name}")
        self.assertEqual(resp.status_code, 200)

    def test_manage_user_can_grant_view_permission(self):
        self._login(self.group_admin_a_key)
        resp = self.client.post(f"/web/grants/{self.dataset.name}", {
            "action": "grant",
            "email": "newuser@example.org",
            "permission": self.view_perm.pk,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Grant.objects.filter(
            user__email="newuser@example.org", dataset=self.dataset,
            permission=self.view_perm,
        ).exists())

    def test_manage_user_can_grant_manage_permission(self):
        self._login(self.group_admin_a_key)
        resp = self.client.post(f"/web/grants/{self.dataset.name}", {
            "action": "grant",
            "email": "newuser@example.org",
            "permission": self.manage_perm.pk,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Grant.objects.filter(
            user__email="newuser@example.org", dataset=self.dataset,
            permission=self.manage_perm,
        ).exists())

    def test_manage_user_cannot_grant_admin_permission(self):
        self._login(self.group_admin_a_key)
        resp = self.client.post(f"/web/grants/{self.dataset.name}", {
            "action": "grant",
            "email": "newuser@example.org",
            "permission": self.admin_perm.pk,
        }, follow=True)
        self.assertContains(resp, "You cannot grant admin permission")
        self.assertFalse(Grant.objects.filter(
            user__email="newuser@example.org", dataset=self.dataset,
            permission=self.admin_perm,
        ).exists())

    def test_manage_user_can_revoke_non_admin_grant(self):
        g = Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, group=self.group_a,
        )
        self._login(self.group_admin_a_key)
        self.client.post(f"/web/grants/{self.dataset.name}", {
            "action": "revoke", "grant_id": g.pk,
        })
        self.assertFalse(Grant.objects.filter(pk=g.pk).exists())

    def test_manage_user_cannot_revoke_admin_grant(self):
        admin_grant = Grant.objects.get(
            user=self.sc_user, dataset=self.dataset, permission=self.admin_perm,
        )
        self._login(self.group_admin_a_key)
        self.client.post(f"/web/grants/{self.dataset.name}", {
            "action": "revoke", "grant_id": admin_grant.pk,
        })
        self.assertTrue(Grant.objects.filter(pk=admin_grant.pk).exists())

    def test_manage_user_does_not_see_admin_in_dropdown(self):
        self._login(self.group_admin_a_key)
        resp = self.client.get(f"/web/grants/{self.dataset.name}")
        self.assertContains(resp, "view")
        self.assertContains(resp, "manage")
        # admin should not appear as an option in the permission dropdown
        self.assertNotContains(resp, '<option value="%s">admin</option>' % self.admin_perm.pk)

    def test_no_revoke_button_for_own_grants(self):
        self._login(self.group_admin_a_key)
        own_grant = Grant.objects.get(
            user=self.group_admin_a, dataset=self.dataset, permission=self.manage_perm,
        )
        resp = self.client.get(f"/web/grants/{self.dataset.name}")
        # The revoke form (with grant_id) should not appear for the user's own grant
        self.assertNotContains(resp, f'name="grant_id" value="{own_grant.pk}"')

    def test_user_cannot_revoke_own_grant(self):
        self._login(self.group_admin_a_key)
        own_grant = Grant.objects.get(
            user=self.group_admin_a, dataset=self.dataset, permission=self.manage_perm,
        )
        resp = self.client.post(f"/web/grants/{self.dataset.name}", {
            "action": "revoke", "grant_id": own_grant.pk,
        }, follow=True)
        self.assertTrue(Grant.objects.filter(pk=own_grant.pk).exists())
        self.assertContains(resp, "You cannot revoke your own grants")

    def test_regular_user_denied_grants_page(self):
        self._login(self.regular_key)
        resp = self.client.get(f"/web/grants/{self.dataset.name}")
        self.assertContains(resp, "Access Denied")


# ──────────────────────────────────────────────────────────────
# Dataset admin promotion tests (SC manages dataset admins)
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestDatasetAdminPromotion(_WebTestBase):
    """SC/admin can add/remove dataset admins via the Manage Dataset Admins page."""

    def test_sc_can_add_dataset_admin(self):
        new_user = User.objects.create(email="newlead@example.org", name="New Lead")
        self._login(self.sc_key)
        resp = self.client.post(f"/web/dataset-admins/{self.dataset.name}", {
            "action": "add", "email": "newlead@example.org",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Grant.objects.filter(
            user=new_user, dataset=self.dataset, permission__name="admin"
        ).exists())

    def test_sc_can_remove_dataset_admin(self):
        grant = Grant.objects.get(
            user=self.sc_user, dataset=self.dataset, permission=self.admin_perm,
        )
        self._login(self.global_admin_key)  # Global admin can also do this
        self.client.post(f"/web/dataset-admins/{self.dataset.name}", {
            "action": "remove", "grant_id": grant.pk,
        })
        self.assertFalse(Grant.objects.filter(pk=grant.pk).exists())

    def test_dataset_admin_manage_only_cannot_access(self):
        """User with only manage (not SC) cannot manage dataset admins."""
        self._login(self.group_admin_a_key)
        resp = self.client.get(f"/web/dataset-admins/{self.dataset.name}")
        self.assertContains(resp, "Access Denied")

    def test_regular_user_denied(self):
        self._login(self.regular_key)
        resp = self.client.get(f"/web/dataset-admins/{self.dataset.name}")
        self.assertContains(resp, "Access Denied")

    def test_datasets_page_shows_manage_dataset_admins_for_sc(self):
        self._login(self.sc_key)
        resp = self.client.get("/web/datasets")
        self.assertContains(resp, "Manage Dataset Admins")

    def test_datasets_page_hides_manage_dataset_admins_for_regular(self):
        self._login(self.regular_key)
        resp = self.client.get("/web/datasets")
        self.assertNotContains(resp, "Manage Dataset Admins")

    def test_unauthenticated_redirects(self):
        resp = self.client.get(f"/web/dataset-admins/{self.dataset.name}")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/login", resp.url)


# ──────────────────────────────────────────────────────────────
# My Account dashboard tests
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestMyAccountDashboard(_WebTestBase):
    def test_shows_direct_grants(self):
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, source=Grant.SOURCE_MANUAL,
        )
        self._login(self.regular_key)
        resp = self.client.get("/web/my-account")
        self.assertContains(resp, "test-dataset")
        self.assertContains(resp, "view")

    def test_shows_group_permissions(self):
        GroupDatasetPermission.objects.create(
            group=self.group_a, dataset=self.dataset, permission=self.view_perm,
        )
        self._login(self.regular_key)
        resp = self.client.get("/web/my-account")
        self.assertContains(resp, "alpha-lab")

    def test_shows_datasets(self):
        self._login(self.sc_key)
        resp = self.client.get("/web/my-account")
        self.assertContains(resp, "Datasets")
        self.assertContains(resp, "test-dataset")

    def test_shows_groups_you_administer(self):
        self._login(self.group_admin_a_key)
        resp = self.client.get("/web/my-account")
        self.assertContains(resp, "My Groups")
        self.assertContains(resp, "alpha-lab")
        self.assertContains(resp, "Manage Group")

    def test_shows_groups(self):
        self._login(self.sc_key)
        resp = self.client.get("/web/my-account")
        self.assertContains(resp, "sc")

    def test_global_admin_sees_all_groups_with_manage_links(self):
        self._login(self.global_admin_key)
        resp = self.client.get("/web/my-account")
        # Global admin should see all groups, even those they're not a member of
        self.assertContains(resp, "alpha-lab")
        self.assertContains(resp, "beta-lab")
        self.assertContains(resp, "sc")
        # And should have Manage Group links for all of them
        self.assertContains(resp, '/web/group/alpha-lab/')
        self.assertContains(resp, '/web/group/beta-lab/')
        self.assertContains(resp, '/web/group/sc/')

    def test_shows_missing_tos_with_invite_token(self):
        tos = TOSDocument.objects.create(
            name="Test TOS", text="Terms here", dataset=self.dataset,
            invite_token="test-invite-token-abc",
        )
        self.dataset.tos = tos
        self.dataset.save()
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm,
        )
        self._login(self.regular_key)
        resp = self.client.get("/web/my-account")
        self.assertContains(resp, "Action Required")
        self.assertContains(resp, "test-invite-token-abc")

    def test_unauthenticated_redirects(self):
        resp = self.client.get("/web/my-account")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/login", resp.url)

    def test_lists_long_lived_tokens_and_hides_login_tokens(self):
        from django.utils import timezone

        long_lived = APIKey.objects.create(
            user=self.regular_user, description="my laptop", expires_at=None,
        )
        APIKey.objects.create(
            user=self.regular_user, description="OAuth login token",
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )
        self._login(self.regular_key)
        resp = self.client.get("/web/my-account")
        self.assertContains(resp, "API Tokens")
        self.assertContains(resp, "my laptop")
        self.assertContains(resp, long_lived.key)
        # The expiring login token must not appear
        self.assertNotContains(resp, "OAuth login token")

    def test_create_token(self):
        self._login(self.regular_key)
        resp = self.client.post("/web/my-account", {
            "action": "create_token", "description": "CAVEclient",
        })
        self.assertEqual(resp.status_code, 302)
        token = APIKey.objects.get(
            user=self.regular_user, description="CAVEclient",
        )
        self.assertIsNone(token.expires_at)

    def test_create_token_requires_description(self):
        self._login(self.regular_key)
        before = APIKey.objects.filter(user=self.regular_user).count()
        resp = self.client.post("/web/my-account", {
            "action": "create_token", "description": "   ",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            APIKey.objects.filter(user=self.regular_user).count(), before,
        )

    def test_revoke_token(self):
        token = APIKey.objects.create(
            user=self.regular_user, description="rotate me", expires_at=None,
        )
        self._login(self.regular_key)
        resp = self.client.post("/web/my-account", {
            "action": "revoke_token", "token_id": str(token.pk),
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(APIKey.objects.filter(pk=token.pk).exists())

    def test_cannot_revoke_other_users_token(self):
        from django.utils import timezone

        victim_token = APIKey.objects.create(
            user=self.global_admin, description="not yours", expires_at=None,
        )
        self._login(self.regular_key)
        resp = self.client.post("/web/my-account", {
            "action": "revoke_token", "token_id": str(victim_token.pk),
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(APIKey.objects.filter(pk=victim_token.pk).exists())

    def test_cannot_revoke_login_token_via_ui(self):
        # The login token (with expires_at set) should not be revocable via
        # this UI even if its ID is submitted.
        resp_id = self.regular_key.pk
        self._login(self.regular_key)
        resp = self.client.post("/web/my-account", {
            "action": "revoke_token", "token_id": str(resp_id),
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(APIKey.objects.filter(pk=resp_id).exists())


# ──────────────────────────────────────────────────────────────
# TOS landing page tests
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestTOSLandingClosed(_WebTestBase):
    """TOS landing page for closed (invite-only) datasets."""

    def setUp(self):
        super().setUp()
        self.dataset.access_mode = Dataset.ACCESS_CLOSED
        self.dataset.save()
        self.tos = TOSDocument.objects.create(
            name="Closed TOS", text="Accept these terms.",
            dataset=self.dataset, invite_token="closed-tok-123",
        )
        self.dataset.tos = self.tos
        self.dataset.save()
        # Give regular_user a grant so they're "pre-added"
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, source=Grant.SOURCE_MANUAL,
        )

    def test_pre_added_user_can_view(self):
        self._login(self.regular_key)
        resp = self.client.get("/web/tos/closed-tok-123/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Closed TOS")
        self.assertContains(resp, "I Accept")

    def test_pre_added_user_can_accept(self):
        self._login(self.regular_key)
        resp = self.client.post("/web/tos/closed-tok-123/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            TOSAcceptance.objects.filter(user=self.regular_user, tos_document=self.tos).exists()
        )

    def test_admin_grant_user_can_accept(self):
        """User with admin grant can accept TOS on closed dataset."""
        self._login(self.sc_key)
        resp = self.client.post("/web/tos/closed-tok-123/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            TOSAcceptance.objects.filter(user=self.sc_user, tos_document=self.tos).exists()
        )

    def test_global_admin_can_accept(self):
        self._login(self.global_admin_key)
        resp = self.client.post("/web/tos/closed-tok-123/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            TOSAcceptance.objects.filter(user=self.global_admin, tos_document=self.tos).exists()
        )

    def test_user_without_grant_is_denied(self):
        self._login(self.group_admin_b_key)
        resp = self.client.get("/web/tos/closed-tok-123/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Access Restricted")

    def test_denied_page_mentions_dataset_administrator(self):
        self._login(self.group_admin_b_key)
        resp = self.client.get("/web/tos/closed-tok-123/")
        self.assertContains(resp, "dataset administrator")

    def test_unauthenticated_sees_login_link(self):
        resp = self.client.get("/web/tos/closed-tok-123/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Login to accept")
        self.assertContains(resp, "?next=/web/tos/closed-tok-123/")


@pytest.mark.django_db
class TestTOSLandingPublic(_WebTestBase):
    """TOS landing page for public (self-service) datasets."""

    def setUp(self):
        super().setUp()
        self.dataset.access_mode = Dataset.ACCESS_PUBLIC
        self.dataset.save()
        self.tos = TOSDocument.objects.create(
            name="Public TOS", text="Public terms.",
            dataset=self.dataset, invite_token="public-tok-456",
        )
        self.dataset.tos = self.tos
        self.dataset.save()

    def test_any_user_can_view(self):
        self._login(self.regular_key)
        resp = self.client.get("/web/tos/public-tok-456/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Public TOS")
        self.assertContains(resp, "Public")

    def test_acceptance_creates_self_service_grant(self):
        self._login(self.regular_key)
        resp = self.client.post("/web/tos/public-tok-456/")
        self.assertEqual(resp.status_code, 302)
        grant = Grant.objects.get(user=self.regular_user, dataset=self.dataset, permission=self.view_perm)
        self.assertEqual(grant.source, Grant.SOURCE_SELF_SERVICE)

    def test_acceptance_is_idempotent(self):
        self._login(self.regular_key)
        self.client.post("/web/tos/public-tok-456/")
        self.client.post("/web/tos/public-tok-456/")
        self.assertEqual(
            Grant.objects.filter(
                user=self.regular_user, dataset=self.dataset, permission=self.view_perm
            ).count(), 1
        )
        self.assertEqual(
            TOSAcceptance.objects.filter(user=self.regular_user, tos_document=self.tos).count(), 1
        )


@pytest.mark.django_db
class TestTOSLandingBucketIAM(_WebTestBase):
    """Bucket IAM provisioning on TOS acceptance via sync_user_dataset_iam."""

    def setUp(self):
        super().setUp()
        self.dataset.access_mode = Dataset.ACCESS_PUBLIC
        self.dataset.save()
        self.tos = TOSDocument.objects.create(
            name="Bucket TOS", text="Terms.",
            dataset=self.dataset, invite_token="bucket-tok-789",
        )
        self.dataset.tos = self.tos
        self.dataset.save()
        self.bucket_a = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        self.bucket_b = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-b")
        self.dv1 = DatasetVersion.objects.create(
            dataset=self.dataset, version="v1",
        )
        self.dv1.buckets.add(self.bucket_a)
        self.dv2 = DatasetVersion.objects.create(
            dataset=self.dataset, version="v2",
        )
        self.dv2.buckets.add(self.bucket_b)
        self.dv_empty = DatasetVersion.objects.create(
            dataset=self.dataset, version="v3",
        )

    def test_bucket_iam_called_per_version(self):
        from unittest.mock import patch

        self._login(self.regular_key)
        with patch("ngauth.gcs.add_user_to_bucket") as mock_add, \
             patch("ngauth.gcs.remove_user_from_bucket"):
            mock_add.return_value = True
            self.client.post("/web/tos/bucket-tok-789/")

        called_buckets = sorted(c.args[0] for c in mock_add.call_args_list)
        self.assertEqual(called_buckets, ["bucket-a", "bucket-b"])
        for call in mock_add.call_args_list:
            self.assertEqual(call.args[1], "regular@example.org")

    def test_empty_bucket_skipped(self):
        from unittest.mock import patch

        self._login(self.regular_key)
        with patch("ngauth.gcs.add_user_to_bucket") as mock_add, \
             patch("ngauth.gcs.remove_user_from_bucket"):
            mock_add.return_value = True
            self.client.post("/web/tos/bucket-tok-789/")

        called_buckets = [c.args[0] for c in mock_add.call_args_list]
        self.assertNotIn("", called_buckets)


@pytest.mark.django_db
class TestGrantIAMSync(_WebTestBase):
    """Grant create/revoke triggers IAM sync."""

    def setUp(self):
        super().setUp()
        DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        DatasetVersion.objects.create(
            dataset=self.dataset, version="v1",
        )

    def test_grant_create_triggers_iam_sync(self):
        from unittest.mock import patch

        self._login(self.sc_key)
        with patch("ngauth.gcs.add_user_to_bucket") as mock_add, \
             patch("ngauth.gcs.remove_user_from_bucket"):
            mock_add.return_value = True
            self.client.post(f"/web/grants/{self.dataset.name}", {
                "action": "grant",
                "email": "regular@example.org",
                "permission": self.view_perm.pk,
            })
        mock_add.assert_called_with("bucket-a", "regular@example.org")

    def test_grant_revoke_triggers_iam_sync(self):
        from unittest.mock import patch

        g = Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm,
        )
        self._login(self.sc_key)
        with patch("ngauth.gcs.add_user_to_bucket"), \
             patch("ngauth.gcs.remove_user_from_bucket") as mock_remove:
            mock_remove.return_value = True
            self.client.post(f"/web/grants/{self.dataset.name}", {
                "action": "revoke", "grant_id": g.pk,
            })
        mock_remove.assert_called_with("bucket-a", "regular@example.org")


@pytest.mark.django_db
class TestGroupMembershipIAMSync(_WebTestBase):
    """Group membership changes trigger IAM sync for group dataset permissions."""

    def setUp(self):
        super().setUp()
        DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        DatasetVersion.objects.create(
            dataset=self.dataset, version="v1",
        )
        GroupDatasetPermission.objects.create(
            group=self.group_a, dataset=self.dataset, permission=self.view_perm,
        )

    def test_add_member_triggers_iam_sync(self):
        from unittest.mock import patch

        new_user = User.objects.create(email="new@example.org")
        self._login(self.group_admin_a_key)
        with patch("ngauth.gcs.add_user_to_bucket") as mock_add, \
             patch("ngauth.gcs.remove_user_from_bucket"):
            mock_add.return_value = True
            self.client.post(f"/web/group/{self.group_a.name}/", {
                "action": "add_member",
                "email": "new@example.org",
            })
        mock_add.assert_called_with("bucket-a", "new@example.org")

    def test_remove_member_triggers_iam_sync(self):
        from unittest.mock import patch

        ug = UserGroup.objects.get(user=self.regular_user, group=self.group_a)
        self._login(self.group_admin_a_key)
        with patch("ngauth.gcs.add_user_to_bucket"), \
             patch("ngauth.gcs.remove_user_from_bucket") as mock_remove:
            mock_remove.return_value = True
            self.client.post(f"/web/group/{self.group_a.name}/", {
                "action": "remove_member",
                "member_id": ug.pk,
            })
        mock_remove.assert_called_with("bucket-a", "regular@example.org")


@pytest.mark.django_db
class TestTOSLandingGeneral(_WebTestBase):
    """General TOS landing tests."""

    def test_invalid_token_returns_404(self):
        self._login(self.regular_key)
        resp = self.client.get("/web/tos/nonexistent-token/")
        self.assertEqual(resp.status_code, 404)

    def test_already_accepted_shows_message(self):
        self.dataset.access_mode = Dataset.ACCESS_PUBLIC
        self.dataset.save()
        tos = TOSDocument.objects.create(
            name="Already TOS", text="Terms.",
            dataset=self.dataset, invite_token="already-tok",
        )
        self.dataset.tos = tos
        self.dataset.save()
        TOSAcceptance.objects.create(user=self.regular_user, tos_document=tos)
        self._login(self.regular_key)
        resp = self.client.get("/web/tos/already-tok/")
        self.assertContains(resp, "already accepted")

    def test_datasets_page_uses_invite_token_links(self):
        tos = TOSDocument.objects.create(
            name="Link TOS", text="Terms.",
            dataset=self.dataset, invite_token="link-tok",
        )
        self.dataset.tos = tos
        self.dataset.save()
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm,
        )
        self._login(self.regular_key)
        resp = self.client.get("/web/datasets")
        self.assertContains(resp, "/web/tos/link-tok/")
        self.assertNotContains(resp, f"/web/tos/{tos.pk}/accept")

    def test_datasets_page_shows_all_tos_docs(self):
        """Both general and service-specific TOS appear on the datasets page."""
        general_tos = TOSDocument.objects.create(
            name="General TOS", text="General.", dataset=self.dataset,
            invite_token="gen-tok",
        )
        self.dataset.tos = general_tos
        self.dataset.save()

        svc = Service.objects.create(name="celltyping", display_name="Cell Typing")
        svc_tos = TOSDocument.objects.create(
            name="CT TOS", text="CT terms.", dataset=self.dataset,
            service=svc, invite_token="ct-tok",
        )

        self._login(self.global_admin_key)
        resp = self.client.get("/web/datasets")
        self.assertContains(resp, "/web/tos/gen-tok/")
        self.assertContains(resp, "/web/tos/ct-tok/")
        self.assertContains(resp, "Cell Typing")


# ──────────────────────────────────────────────────────────────
# Service Table Management
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestServiceTableManagement(_WebTestBase):
    """Service table CRUD on the public-roots page."""

    def _login(self, api_key):
        self.client.cookies[settings.AUTH_COOKIE_NAME] = api_key.key

    def test_admin_can_add_service_table(self):
        self._login(self.sc_key)
        resp = self.client.post(f"/web/public-roots/{self.dataset.name}", {
            "action": "add_service_table",
            "service_name": "pychunkedgraph",
            "table_name": "fly_v31",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ServiceTable.objects.filter(
            service_name="pychunkedgraph", table_name="fly_v31", dataset=self.dataset,
        ).exists())

    def test_admin_can_remove_service_table(self):
        st = ServiceTable.objects.create(
            service_name="pcg", table_name="tbl", dataset=self.dataset,
        )
        PublicRoot.objects.create(service_table=st, root_id=42)
        self._login(self.sc_key)
        resp = self.client.post(f"/web/public-roots/{self.dataset.name}", {
            "action": "remove_service_table",
            "service_table_id": st.pk,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(ServiceTable.objects.filter(pk=st.pk).exists())
        self.assertFalse(PublicRoot.objects.filter(service_table=st).exists())

    def test_manage_user_cannot_add_service_table(self):
        self._login(self.group_admin_a_key)
        self.client.post(f"/web/public-roots/{self.dataset.name}", {
            "action": "add_service_table",
            "service_name": "pcg",
            "table_name": "tbl",
        })
        self.assertFalse(ServiceTable.objects.filter(
            service_name="pcg", table_name="tbl",
        ).exists())

    def test_manage_user_cannot_remove_service_table(self):
        st = ServiceTable.objects.create(
            service_name="pcg", table_name="tbl", dataset=self.dataset,
        )
        self._login(self.group_admin_a_key)
        self.client.post(f"/web/public-roots/{self.dataset.name}", {
            "action": "remove_service_table",
            "service_table_id": st.pk,
        })
        self.assertTrue(ServiceTable.objects.filter(pk=st.pk).exists())

    def test_add_root_form_hidden_when_no_service_tables(self):
        self._login(self.sc_key)
        resp = self.client.get(f"/web/public-roots/{self.dataset.name}")
        self.assertNotContains(resp, "Add Public Root")

    def test_add_root_form_shown_when_service_tables_exist(self):
        ServiceTable.objects.create(
            service_name="pcg", table_name="tbl", dataset=self.dataset,
        )
        self._login(self.sc_key)
        resp = self.client.get(f"/web/public-roots/{self.dataset.name}")
        self.assertContains(resp, "Add Public Root")

    def test_duplicate_service_table_shows_error(self):
        ServiceTable.objects.create(
            service_name="pcg", table_name="tbl", dataset=self.dataset,
        )
        self._login(self.sc_key)
        resp = self.client.post(f"/web/public-roots/{self.dataset.name}", {
            "action": "add_service_table",
            "service_name": "pcg",
            "table_name": "tbl",
        }, follow=True)
        self.assertContains(resp, "already exists")

    def test_datasets_page_shows_service_tables_button(self):
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.manage_perm,
        )
        self._login(self.regular_key)
        resp = self.client.get("/web/datasets")
        self.assertContains(resp, "Service Tables & Roots")


# ──────────────────────────────────────────────────────────────
# TOS Service Check
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestTOSServiceCheck(_WebTestBase):
    """Tests for the /web/tos/service-check/ flow."""

    def setUp(self):
        super().setUp()
        self.svc = Service.objects.create(name="celltyping", display_name="Cell Typing")
        self.general_tos = TOSDocument.objects.create(
            name="General TOS", text="General terms.", dataset=self.dataset,
        )
        self.dataset.tos = self.general_tos
        self.dataset.save()
        self.svc_tos = TOSDocument.objects.create(
            name="CT TOS", text="Celltyping terms.",
            dataset=self.dataset, service=self.svc,
        )
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm,
        )

    def test_unauthenticated_redirects_to_login(self):
        resp = self.client.get("/web/tos/service-check/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/login", resp.url)

    def test_no_pending_tos_redirects_to_next(self):
        TOSAcceptance.objects.create(user=self.regular_user, tos_document=self.general_tos)
        TOSAcceptance.objects.create(user=self.regular_user, tos_document=self.svc_tos)
        self._login(self.regular_key)

        session = self.client.session
        session["tos_check_ids"] = [self.general_tos.pk, self.svc_tos.pk]
        session["tos_check_next"] = "/some-service/"
        session.save()

        resp = self.client.get("/web/tos/service-check/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/some-service/")

    def test_shows_pending_tos_documents(self):
        self._login(self.regular_key)

        session = self.client.session
        session["tos_check_ids"] = [self.general_tos.pk, self.svc_tos.pk]
        session["tos_check_next"] = "/return/"
        session.save()

        resp = self.client.get("/web/tos/service-check/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "General TOS")
        self.assertContains(resp, "CT TOS")
        self.assertContains(resp, "Cell Typing")

    def test_post_accepts_all_and_redirects(self):
        from unittest.mock import patch

        self._login(self.regular_key)

        session = self.client.session
        session["tos_check_ids"] = [self.general_tos.pk, self.svc_tos.pk]
        session["tos_check_next"] = "/return/"
        session.save()

        with patch("ngauth.gcs.add_user_to_bucket"), \
             patch("ngauth.gcs.remove_user_from_bucket"):
            resp = self.client.post("/web/tos/service-check/")

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/return/")

        self.assertTrue(TOSAcceptance.objects.filter(
            user=self.regular_user, tos_document=self.general_tos
        ).exists())
        self.assertTrue(TOSAcceptance.objects.filter(
            user=self.regular_user, tos_document=self.svc_tos
        ).exists())

    def test_query_param_mode(self):
        """Already-authenticated user can be directed with query params."""
        self._login(self.regular_key)
        resp = self.client.get(
            f"/web/tos/service-check/?service=celltyping&dataset={self.dataset.name}&next=/ct/"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "General TOS")
        self.assertContains(resp, "CT TOS")

    def test_query_param_post_redirects_to_next(self):
        """POST after query-param GET redirects to the next URL, not /."""
        from unittest.mock import patch

        self._login(self.regular_key)
        # GET loads the TOS page with query params (no session)
        self.client.get(
            f"/web/tos/service-check/?service=celltyping&dataset={self.dataset.name}"
            "&next=https://celltyping.example.com/graph/fish"
        )

        with patch("ngauth.gcs.add_user_to_bucket"), \
             patch("ngauth.gcs.remove_user_from_bucket"):
            resp = self.client.post(
                "/web/tos/service-check/",
                {"next": "https://celltyping.example.com/graph/fish"},
            )

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "https://celltyping.example.com/graph/fish")

        # Verify TOS were actually accepted
        self.assertTrue(TOSAcceptance.objects.filter(
            user=self.regular_user, tos_document=self.general_tos
        ).exists())
        self.assertTrue(TOSAcceptance.objects.filter(
            user=self.regular_user, tos_document=self.svc_tos
        ).exists())

    def test_redirect_preserves_query_string(self):
        """Redirect URL with query params (e.g. ?dataset=hemibrain) survives the full TOS flow.

        Services pass their own dataset identifiers in the redirect URL.
        DSG must preserve the URL exactly — including query string — so the
        user returns to the correct dataset after accepting TOS.
        """
        from html import escape
        from unittest.mock import patch

        self._login(self.regular_key)

        redirect_url = "https://neuprint-test.example.com/?dataset=hemibrain%3Av1.2.1&tab=graph"

        session = self.client.session
        session["tos_check_ids"] = [self.general_tos.pk, self.svc_tos.pk]
        session["tos_check_next"] = redirect_url
        session.save()

        # GET should render the hidden field with the full redirect URL
        # (& is HTML-escaped to &amp; in the template, which browsers
        # decode back to & when submitting the form)
        resp = self.client.get("/web/tos/service-check/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, escape(redirect_url))

        # POST (via session) should redirect to the exact URL
        with patch("ngauth.gcs.add_user_to_bucket"), \
             patch("ngauth.gcs.remove_user_from_bucket"):
            resp = self.client.post("/web/tos/service-check/")

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, redirect_url)

    def test_redirect_preserves_query_string_via_form(self):
        """Same as above but simulates the browser POST with the hidden field value."""
        from unittest.mock import patch

        self._login(self.regular_key)

        redirect_url = "https://neuprint-test.example.com/?dataset=hemibrain%3Av1.2.1&tab=graph"

        session = self.client.session
        session["tos_check_ids"] = [self.general_tos.pk, self.svc_tos.pk]
        session["tos_check_next"] = redirect_url
        session.save()

        # Simulate browser form submission with the hidden 'next' field
        with patch("ngauth.gcs.add_user_to_bucket"), \
             patch("ngauth.gcs.remove_user_from_bucket"):
            resp = self.client.post(
                "/web/tos/service-check/",
                {"next": redirect_url},
            )

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, redirect_url)
