"""Tests for web access-control user stories."""

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase

from core.models import (
    APIKey,
    Dataset,
    DatasetVersion,
    Grant,
    Group,
    GroupDatasetPermission,
    Permission,
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

        # Users
        self.sc_user = User.objects.create(email="sc@example.org", name="SC Member")
        self.sc_key = APIKey.objects.create(user=self.sc_user, key="tok-sc")

        self.admin_user = User.objects.create(email="admin@example.org", name="Admin", admin=True)
        self.admin_key = APIKey.objects.create(user=self.admin_user, key="tok-admin")

        self.lab_head = User.objects.create(email="labhead@example.org", name="Lab Head")
        self.lab_head_key = APIKey.objects.create(user=self.lab_head, key="tok-labhead")

        self.regular_user = User.objects.create(email="regular@example.org", name="Regular")
        self.regular_key = APIKey.objects.create(user=self.regular_user, key="tok-regular")

        # Groups
        self.sc_group = Group.objects.create(name="sc")
        UserGroup.objects.create(user=self.sc_user, group=self.sc_group)

        # Dataset
        self.dataset = Dataset.objects.create(name="test-dataset")

        # Permissions
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.edit_perm, _ = Permission.objects.get_or_create(name="edit")
        self.admin_perm, _ = Permission.objects.get_or_create(name="admin")

        # Lab head has admin grant on dataset (but NOT in sc group)
        Grant.objects.create(
            user=self.lab_head, dataset=self.dataset,
            permission=self.admin_perm, source=Grant.SOURCE_MANUAL,
        )

    def _login(self, api_key):
        self.client.cookies[settings.AUTH_COOKIE_NAME] = api_key.key


# ──────────────────────────────────────────────────────────────
# Team lead management (SC promotes team leads)
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestTeamLeadManage(_WebTestBase):
    def test_sc_member_can_view(self):
        self._login(self.sc_key)
        resp = self.client.get(f"/web/team-leads/{self.dataset.name}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "labhead@example.org")

    def test_global_admin_can_view(self):
        self._login(self.admin_key)
        resp = self.client.get(f"/web/team-leads/{self.dataset.name}")
        self.assertEqual(resp.status_code, 200)

    def test_lab_head_denied(self):
        """Team lead (admin grant only, not SC) cannot manage team leads."""
        self._login(self.lab_head_key)
        resp = self.client.get(f"/web/team-leads/{self.dataset.name}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Access Denied")

    def test_regular_user_denied(self):
        self._login(self.regular_key)
        resp = self.client.get(f"/web/team-leads/{self.dataset.name}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Access Denied")

    def test_unauthenticated_redirects(self):
        resp = self.client.get(f"/web/team-leads/{self.dataset.name}")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/login", resp.url)

    def test_sc_can_add_team_lead(self):
        self._login(self.sc_key)
        new_user = User.objects.create(email="newlead@example.org", name="New Lead")
        resp = self.client.post(
            f"/web/team-leads/{self.dataset.name}",
            {"action": "add", "email": "newlead@example.org"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            Grant.objects.filter(
                user=new_user, dataset=self.dataset, permission__name="admin"
            ).exists()
        )

    def test_add_nonexistent_email_shows_error(self):
        self._login(self.sc_key)
        resp = self.client.post(
            f"/web/team-leads/{self.dataset.name}",
            {"action": "add", "email": "nobody@example.org"},
            follow=True,
        )
        self.assertContains(resp, "User not found")

    def test_sc_can_remove_team_lead(self):
        self._login(self.sc_key)
        grant = Grant.objects.get(
            user=self.lab_head, dataset=self.dataset, permission=self.admin_perm
        )
        resp = self.client.post(
            f"/web/team-leads/{self.dataset.name}",
            {"action": "remove", "grant_id": grant.pk},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(
            Grant.objects.filter(
                user=self.lab_head, dataset=self.dataset, permission__name="admin"
            ).exists()
        )

    def test_datasets_page_shows_manage_team_leads_for_sc(self):
        self._login(self.sc_key)
        resp = self.client.get("/web/datasets")
        self.assertContains(resp, "Manage Team Leads")

    def test_datasets_page_hides_manage_team_leads_for_regular(self):
        self._login(self.regular_key)
        resp = self.client.get("/web/datasets")
        self.assertNotContains(resp, "Manage Team Leads")


# ──────────────────────────────────────────────────────────────
# Commit 2: Lab heads add users (enhance GrantManageView)
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestGrantManageEnhanced(_WebTestBase):
    def test_lab_head_grants_existing_user(self):
        self._login(self.lab_head_key)
        resp = self.client.post(
            f"/web/grants/{self.dataset.name}",
            {"action": "grant", "email": "regular@example.org", "permission": self.view_perm.pk},
        )
        self.assertEqual(resp.status_code, 302)
        grant = Grant.objects.get(user=self.regular_user, dataset=self.dataset)
        self.assertEqual(grant.source, Grant.SOURCE_MANUAL)
        self.assertEqual(grant.granted_by, self.lab_head)

    def test_lab_head_grants_new_email_creates_user(self):
        self._login(self.lab_head_key)
        resp = self.client.post(
            f"/web/grants/{self.dataset.name}",
            {"action": "grant", "email": "newbie@example.org", "permission": self.view_perm.pk},
        )
        self.assertEqual(resp.status_code, 302)
        new_user = User.objects.get(email="newbie@example.org")
        self.assertEqual(new_user.name, "newbie")
        self.assertFalse(new_user.has_usable_password())
        grant = Grant.objects.get(user=new_user, dataset=self.dataset)
        self.assertEqual(grant.source, Grant.SOURCE_MANUAL)

    def test_grant_success_message_for_new_user(self):
        self._login(self.lab_head_key)
        resp = self.client.post(
            f"/web/grants/{self.dataset.name}",
            {"action": "grant", "email": "newbie@example.org", "permission": self.view_perm.pk},
            follow=True,
        )
        self.assertContains(resp, "Created user and granted")

    def test_global_admin_can_manage_grants(self):
        self._login(self.admin_key)
        resp = self.client.get(f"/web/grants/{self.dataset.name}")
        self.assertEqual(resp.status_code, 200)

    def test_regular_user_denied(self):
        self._login(self.regular_key)
        resp = self.client.get(f"/web/grants/{self.dataset.name}")
        self.assertContains(resp, "Access Denied")

    def test_unauthenticated_redirects(self):
        resp = self.client.get(f"/web/grants/{self.dataset.name}")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/login", resp.url)

    def test_grant_manage_shows_source_column(self):
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, granted_by=self.lab_head,
            source=Grant.SOURCE_MANUAL,
        )
        self._login(self.lab_head_key)
        resp = self.client.get(f"/web/grants/{self.dataset.name}")
        self.assertContains(resp, "Added by admin or team lead")


# ──────────────────────────────────────────────────────────────
# Commit 3: Enhanced user dashboard
# ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestMyDatasetsDashboard(_WebTestBase):
    def test_shows_direct_grants(self):
        Grant.objects.create(
            user=self.regular_user, dataset=self.dataset,
            permission=self.view_perm, source=Grant.SOURCE_MANUAL,
        )
        self._login(self.regular_key)
        resp = self.client.get("/web/my-datasets")
        self.assertContains(resp, "test-dataset")
        self.assertContains(resp, "view")

    def test_shows_group_permissions(self):
        group = Group.objects.create(name="researchers")
        UserGroup.objects.create(user=self.regular_user, group=group)
        GroupDatasetPermission.objects.create(
            group=group, dataset=self.dataset, permission=self.view_perm,
        )
        self._login(self.regular_key)
        resp = self.client.get("/web/my-datasets")
        self.assertContains(resp, "researchers")
        self.assertContains(resp, "Group-Based Permissions")

    def test_shows_admin_datasets(self):
        self._login(self.lab_head_key)
        resp = self.client.get("/web/my-datasets")
        self.assertContains(resp, "Datasets You Lead")
        self.assertContains(resp, "test-dataset")
        self.assertContains(resp, "Manage Grants")

    def test_shows_groups(self):
        self._login(self.sc_key)
        resp = self.client.get("/web/my-datasets")
        self.assertContains(resp, "sc")

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
        resp = self.client.get("/web/my-datasets")
        self.assertContains(resp, "Action Required")
        self.assertContains(resp, "test-invite-token-abc")

    def test_unauthenticated_redirects(self):
        resp = self.client.get("/web/my-datasets")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/login", resp.url)


# ──────────────────────────────────────────────────────────────
# Commit 4: TOS landing pages (closed + public flows)
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

    def test_dataset_admin_can_accept(self):
        self._login(self.lab_head_key)
        resp = self.client.post("/web/tos/closed-tok-123/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            TOSAcceptance.objects.filter(user=self.lab_head, tos_document=self.tos).exists()
        )

    def test_global_admin_can_accept(self):
        self._login(self.admin_key)
        resp = self.client.post("/web/tos/closed-tok-123/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            TOSAcceptance.objects.filter(user=self.admin_user, tos_document=self.tos).exists()
        )

    def test_user_without_grant_is_denied(self):
        """SC user without grant/admin on closed dataset is denied."""
        self._login(self.sc_key)
        resp = self.client.get("/web/tos/closed-tok-123/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Access Restricted")

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
        grant = Grant.objects.get(user=self.regular_user, dataset=self.dataset)
        self.assertEqual(grant.source, Grant.SOURCE_SELF_SERVICE)
        self.assertEqual(grant.permission.name, "view")

    def test_acceptance_is_idempotent(self):
        self._login(self.regular_key)
        self.client.post("/web/tos/public-tok-456/")
        self.client.post("/web/tos/public-tok-456/")
        self.assertEqual(
            Grant.objects.filter(user=self.regular_user, dataset=self.dataset).count(), 1
        )
        self.assertEqual(
            TOSAcceptance.objects.filter(user=self.regular_user, tos_document=self.tos).count(), 1
        )


@pytest.mark.django_db
class TestTOSLandingBucketIAM(_WebTestBase):
    """Bucket IAM provisioning on TOS acceptance."""

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
        self.dv1 = DatasetVersion.objects.create(
            dataset=self.dataset, version="v1", gcs_bucket="bucket-a",
        )
        self.dv2 = DatasetVersion.objects.create(
            dataset=self.dataset, version="v2", gcs_bucket="bucket-b",
        )
        self.dv_empty = DatasetVersion.objects.create(
            dataset=self.dataset, version="v3", gcs_bucket="",
        )

    def test_bucket_iam_called_per_version(self):
        from unittest.mock import patch

        self._login(self.regular_key)
        with patch("ngauth.gcs.add_user_to_bucket") as mock_add:
            mock_add.return_value = True
            self.client.post("/web/tos/bucket-tok-789/")

        called_buckets = sorted(c.args[0] for c in mock_add.call_args_list)
        self.assertEqual(called_buckets, ["bucket-a", "bucket-b"])
        for call in mock_add.call_args_list:
            self.assertEqual(call.args[1], "regular@example.org")

    def test_empty_bucket_skipped(self):
        from unittest.mock import patch

        self._login(self.regular_key)
        with patch("ngauth.gcs.add_user_to_bucket") as mock_add:
            mock_add.return_value = True
            self.client.post("/web/tos/bucket-tok-789/")

        called_buckets = [c.args[0] for c in mock_add.call_args_list]
        self.assertNotIn("", called_buckets)


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
        self._login(self.regular_key)
        resp = self.client.get("/web/datasets")
        self.assertContains(resp, "/web/tos/link-tok/")
        self.assertNotContains(resp, f"/web/tos/{tos.pk}/accept")
