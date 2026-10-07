"""Tests for the "who can get in" members CSV export (/web/grants/<ds>/export.csv)."""

import csv
import io
import sqlite3

import pytest
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from core.models import (
    Affiliation,
    APIKey,
    AuditLog,
    Dataset,
    DatasetBucket,
    DatasetVersion,
    Grant,
    Group,
    GroupDatasetPermission,
    Permission,
    PublicRoot,
    RegisteredClient,
    Service,
    ServiceAccount,
    ServiceAccountGrant,
    ServiceTable,
    TOSDocument,
    User,
    UserGroup,
)
from web.views import _dataset_access_rows

# The contractual column order, spelled out rather than imported, so a change
# to the view's columns fails here.
HEADER = [
    "email", "full_name", "permissions", "versions", "groups",
    "affiliations", "access_via", "grant_details",
]


@pytest.mark.django_db
class _ExportTestBase(TestCase):
    """A dataset with one direct manager; tests add the rows they need."""

    def setUp(self):
        cache.clear()
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.edit_perm, _ = Permission.objects.get_or_create(name="edit")
        self.manage_perm, _ = Permission.objects.get_or_create(name="manage")
        self.admin_perm, _ = Permission.objects.get_or_create(name="admin")

        self.dataset = Dataset.objects.create(name="fish2")
        self.url = f"/web/grants/{self.dataset.name}/export.csv"

        self.manager = User.objects.create(email="manager@example.org", name="Manager")
        self.manager_key = APIKey.objects.create(user=self.manager, key="tok-manager")
        Grant.objects.create(user=self.manager, dataset=self.dataset, permission=self.manage_perm)

    def _login(self, user, key=None):
        key = key or APIKey.objects.create(user=user, key=f"tok-{user.pk}")
        self.client.cookies[settings.AUTH_COOKIE_NAME] = key.key

    def _user(self, email, name="", **extra):
        return User.objects.create(email=email, name=name or email.split("@")[0], **extra)

    def _export(self):
        self._login(self.manager, self.manager_key)
        response = self.client.get(self.url)
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv; charset=utf-8"
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        assert all(len(row) == len(HEADER) for row in rows), rows
        return rows

    def _rows_by_email(self):
        rows = self._export()
        assert rows[0] == HEADER
        return {row[0]: dict(zip(HEADER, row)) for row in rows[1:]}


class TestExportAccess(_ExportTestBase):
    def _assert_no_export(self, response):
        assert "attachment" not in response.get("Content-Disposition", "")
        assert not response.get("Content-Type", "").startswith("text/csv")
        assert not AuditLog.objects.filter(action="grants_exported").exists()

    def test_anonymous_redirects_to_login(self):
        response = self.client.get(self.url)
        assert response.status_code == 302
        assert response["Location"] == f"/auth/login?next=/web/grants/{self.dataset.name}"
        self._assert_no_export(response)

    def test_disabled_requester_redirects_to_login(self):
        self.manager.is_active = False
        self.manager.save()
        self._login(self.manager, self.manager_key)
        response = self.client.get(self.url)
        assert response.status_code == 302
        self._assert_no_export(response)

    def test_delegated_cookie_with_authorized_session_is_logged_out(self):
        registered = RegisteredClient.objects.create(
            origin="https://navis-org.github.io", name="CODA", owner="Philipp",
        )
        delegated = APIKey.objects.create(user=self.manager, delegated_client=registered)
        client = Client()
        client.cookies[settings.AUTH_COOKIE_NAME] = delegated.key
        session = client.session
        session["user_email"] = self.manager.email
        session.save()
        response = client.get(self.url)
        assert response.status_code == 302
        self._assert_no_export(response)

    def test_view_and_edit_grantees_are_denied(self):
        for perm in (self.view_perm, self.edit_perm):
            user = self._user(f"{perm.name}@example.org")
            Grant.objects.create(user=user, dataset=self.dataset, permission=perm)
            self._login(user)
            response = self.client.get(self.url)
            assert b"Access Denied" in response.content
            self._assert_no_export(response)

    def test_group_only_manager_is_denied(self):
        user = self._user("team@example.org")
        group = Group.objects.create(name="leads")
        UserGroup.objects.create(user=user, group=group)
        GroupDatasetPermission.objects.create(group=group, dataset=self.dataset, permission=self.manage_perm)
        self._login(user)
        self._assert_no_export(self.client.get(self.url))

    def test_manager_on_other_dataset_is_denied(self):
        other = Dataset.objects.create(name="other")
        user = self._user("elsewhere@example.org")
        Grant.objects.create(user=user, dataset=other, permission=self.admin_perm)
        self._login(user)
        self._assert_no_export(self.client.get(self.url))

    def test_allowed_requesters_get_a_csv_attachment(self):
        v1 = DatasetVersion.objects.create(dataset=self.dataset, version="v1.0")
        scoped = self._user("scoped@example.org")
        Grant.objects.create(user=scoped, dataset=self.dataset, dataset_version=v1, permission=self.manage_perm)
        ds_admin = self._user("dsadmin@example.org")
        Grant.objects.create(user=ds_admin, dataset=self.dataset, permission=self.admin_perm)
        global_admin = self._user("root@example.org", admin=True)

        for requester in (self.manager, scoped, ds_admin, global_admin):
            key = (
                self.manager_key if requester == self.manager
                else APIKey.objects.create(user=requester, key=f"tok-allowed-{requester.pk}")
            )
            self._login(requester, key)
            response = self.client.get(self.url)
            assert response.status_code == 200, requester.email
            assert response["Content-Type"] == "text/csv; charset=utf-8"
            disposition = response["Content-Disposition"]
            assert disposition.startswith('attachment; filename="fish2-members-')
            assert disposition.endswith('.csv"')
            assert response["Cache-Control"] == "no-store"

    def test_export_is_audited_with_row_count(self):
        viewer = self._user("viewer@example.org")
        Grant.objects.create(user=viewer, dataset=self.dataset, permission=self.view_perm)
        self._export()
        entry = AuditLog.objects.get(action="grants_exported")
        assert entry.actor == self.manager
        assert entry.target_type == "Dataset"
        assert entry.target_id == str(self.dataset.pk)
        assert entry.after_state == {"dataset": "fish2", "rows": 2}


class TestExportRows(_ExportTestBase):
    def test_header_and_rows_sorted_by_email(self):
        for email in ("zed@example.org", "amy@example.org"):
            Grant.objects.create(user=self._user(email), dataset=self.dataset, permission=self.view_perm)
        rows = self._export()
        assert rows[0] == HEADER
        assert [row[0] for row in rows[1:]] == [
            "amy@example.org", "manager@example.org", "zed@example.org",
        ]

    def test_permissions_versions_and_details_for_direct_grants(self):
        v1 = DatasetVersion.objects.create(dataset=self.dataset, version="v1.0", ordinal=1)
        v2 = DatasetVersion.objects.create(dataset=self.dataset, version="v2.0", ordinal=2)
        user = self._user("multi@example.org", name="Multi Person")
        Grant.objects.create(user=user, dataset=self.dataset, permission=self.view_perm)
        Grant.objects.create(user=user, dataset=self.dataset, dataset_version=v1, permission=self.edit_perm)
        Grant.objects.create(user=user, dataset=self.dataset, dataset_version=v2, permission=self.view_perm)

        row = self._rows_by_email()["multi@example.org"]
        assert row["full_name"] == "Multi Person"
        assert row["permissions"] == "view; edit"
        assert row["versions"] == "all; v1.0; v2.0"
        assert row["grant_details"] == "view@all; view@v2.0; edit@v1.0"
        assert row["access_via"] == "direct"
        assert row["groups"] == ""
        assert row["affiliations"] == ""

    def test_version_order_is_branch_then_ordinal_with_nulls_last(self):
        versions = [
            DatasetVersion.objects.create(dataset=self.dataset, version="m-unordered", branch="main"),
            DatasetVersion.objects.create(dataset=self.dataset, version="m2", branch="main", ordinal=2),
            DatasetVersion.objects.create(dataset=self.dataset, version="m10", branch="main", ordinal=10),
            DatasetVersion.objects.create(dataset=self.dataset, version="d1", branch="dev", ordinal=1),
        ]
        user = self._user("versions@example.org")
        for version in versions:
            Grant.objects.create(user=user, dataset=self.dataset, dataset_version=version, permission=self.view_perm)
        row = self._rows_by_email()["versions@example.org"]
        assert row["versions"] == "d1; m2; m10; m-unordered"

    def test_group_wide_member_is_listed(self):
        everyone = Group.objects.create(name="user")
        member = self._user("member@example.org")
        UserGroup.objects.create(user=member, group=everyone)
        GroupDatasetPermission.objects.create(group=everyone, dataset=self.dataset, permission=self.view_perm)

        row = self._rows_by_email()["member@example.org"]
        assert row["permissions"] == "view"
        assert row["versions"] == "all"
        assert row["access_via"] == "group:user"
        assert row["grant_details"] == "view@all via group:user"
        assert row["groups"] == ""

    def test_user_with_both_paths(self):
        everyone = Group.objects.create(name="user")
        UserGroup.objects.create(user=self.manager, group=everyone)
        GroupDatasetPermission.objects.create(group=everyone, dataset=self.dataset, permission=self.view_perm)

        row = self._rows_by_email()["manager@example.org"]
        assert row["access_via"] == "direct; group:user"
        assert row["permissions"] == "view; manage"
        assert row["grant_details"] == "manage@all; view@all via group:user"

    def test_two_groups_with_identical_permission_both_appear(self):
        member = self._user("twice@example.org")
        for name in ("zeta-lab", "alpha-lab"):
            group = Group.objects.create(name=name)
            UserGroup.objects.create(user=member, group=group)
            GroupDatasetPermission.objects.create(group=group, dataset=self.dataset, permission=self.view_perm)
        row = self._rows_by_email()["twice@example.org"]
        assert row["access_via"] == "group:alpha-lab; group:zeta-lab"
        assert row["grant_details"] == "view@all via group:alpha-lab; view@all via group:zeta-lab"

    def test_duplicate_direct_grants_collapse(self):
        user = self._user("dup@example.org")
        Grant.objects.create(user=user, dataset=self.dataset, permission=self.view_perm)
        Grant.objects.create(user=user, dataset=self.dataset, permission=self.view_perm)
        row = self._rows_by_email()["dup@example.org"]
        assert row["grant_details"] == "view@all"

    def test_team_tagged_grant_is_direct_with_group_in_groups(self):
        lab = Group.objects.create(name="lab")
        user = self._user("teamed@example.org")
        UserGroup.objects.create(user=user, group=lab)
        Grant.objects.create(user=user, dataset=self.dataset, permission=self.edit_perm, group=lab)
        row = self._rows_by_email()["teamed@example.org"]
        assert row["groups"] == "lab"
        assert row["access_via"] == "direct"
        assert row["grant_details"] == "edit@all"

    def test_service_scoped_and_bucket_restricted_grants_are_labelled(self):
        clio = Service.objects.create(name="clio")
        bucket_b = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-b")
        bucket_a = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        user = self._user("scoped@example.org")
        Grant.objects.create(user=user, dataset=self.dataset, permission=self.edit_perm, service=clio)
        restricted = Grant.objects.create(user=user, dataset=self.dataset, permission=self.view_perm)
        restricted.buckets.set([bucket_b, bucket_a])
        row = self._rows_by_email()["scoped@example.org"]
        assert row["grant_details"] == "view@all {bucket-a,bucket-b}; edit@all [clio]"

    def test_service_scoped_group_permission_is_labelled(self):
        clio = Service.objects.create(name="clio")
        group = Group.objects.create(name="clio-users")
        member = self._user("cliouser@example.org")
        UserGroup.objects.create(user=member, group=group)
        GroupDatasetPermission.objects.create(
            group=group, dataset=self.dataset, permission=self.view_perm, service=clio,
        )
        row = self._rows_by_email()["cliouser@example.org"]
        assert row["grant_details"] == "view@all [clio] via group:clio-users"

    def test_affiliations_and_team_groups_fill_their_columns(self):
        user = self._user("affiliated@example.org")
        Affiliation.objects.create(user=user, name="Janelia")
        Affiliation.objects.create(user=user, name="Cambridge")
        lab = Group.objects.create(name="lab")
        Grant.objects.create(user=user, dataset=self.dataset, permission=self.view_perm, group=lab)
        row = self._rows_by_email()["affiliated@example.org"]
        assert row["affiliations"] == "Cambridge; Janelia"
        assert row["groups"] == "lab"

    def test_affiliations_for_group_wide_members(self):
        everyone = Group.objects.create(name="user")
        member = self._user("inherited@example.org")
        Affiliation.objects.create(user=member, name="HHMI")
        UserGroup.objects.create(user=member, group=everyone)
        GroupDatasetPermission.objects.create(group=everyone, dataset=self.dataset, permission=self.view_perm)
        assert self._rows_by_email()["inherited@example.org"]["affiliations"] == "HHMI"

    def test_pending_tos_and_read_only_users_are_listed_as_recorded(self):
        TOSDocument.objects.create(name="Fish2 TOS", text="terms", dataset=self.dataset)
        pending = self._user("pending@example.org")
        Grant.objects.create(user=pending, dataset=self.dataset, permission=self.view_perm)
        read_only = self._user("readonly@example.org", read_only=True)
        Grant.objects.create(user=read_only, dataset=self.dataset, permission=self.edit_perm)
        rows = self._rows_by_email()
        assert rows["pending@example.org"]["permissions"] == "view"
        assert rows["readonly@example.org"]["permissions"] == "edit"

    def test_excluded_principals(self):
        everyone = Group.objects.create(name="user")
        GroupDatasetPermission.objects.create(group=everyone, dataset=self.dataset, permission=self.view_perm)
        disabled_direct = self._user("off-direct@example.org", is_active=False)
        Grant.objects.create(user=disabled_direct, dataset=self.dataset, permission=self.view_perm)
        disabled_member = self._user("off-member@example.org", is_active=False)
        UserGroup.objects.create(user=disabled_member, group=everyone)
        self._user("lone-admin@example.org", admin=True)
        member_admin = self._user("member-admin@example.org", admin=True)
        UserGroup.objects.create(user=member_admin, group=everyone)
        other = Dataset.objects.create(name="other")
        Grant.objects.create(user=self._user("other@example.org"), dataset=other, permission=self.view_perm)
        sa = ServiceAccount.objects.create(name="robot")
        ServiceAccountGrant.objects.create(service_account=sa, dataset=self.dataset, permission=self.view_perm)

        emails = set(self._rows_by_email())
        assert emails == {"manager@example.org", "member-admin@example.org"}

    def test_empty_roster_is_header_only(self):
        Grant.objects.filter(user=self.manager).delete()
        root = self._user("root@example.org", admin=True)
        self._login(root)
        response = self.client.get(self.url)
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        assert rows == [HEADER]

    def test_group_only_dataset_lists_members(self):
        bare = Dataset.objects.create(name="bare")
        group = Group.objects.create(name="lab")
        for email in ("b@example.org", "a@example.org"):
            UserGroup.objects.create(user=self._user(email), group=group)
        GroupDatasetPermission.objects.create(group=group, dataset=bare, permission=self.view_perm)
        assert [row[0] for row in _dataset_access_rows(bare)] == ["a@example.org", "b@example.org"]

    def test_query_count_does_not_grow_with_group_size(self):
        everyone = Group.objects.create(name="user")
        GroupDatasetPermission.objects.create(group=everyone, dataset=self.dataset, permission=self.view_perm)

        def add_members(start, count):
            for i in range(start, start + count):
                member = self._user(f"member{i:03d}@example.org")
                UserGroup.objects.create(user=member, group=everyone)
                Affiliation.objects.create(user=member, name=f"Org {i}")

        add_members(0, 3)
        with CaptureQueriesContext(connection) as small:
            assert len(_dataset_access_rows(self.dataset)) == 4
        add_members(3, 27)
        with CaptureQueriesContext(connection) as large:
            assert len(_dataset_access_rows(self.dataset)) == 31
        assert len(large) == len(small)

    def test_parameter_count_stays_bounded(self):
        """Large rosters must not turn into IN lists of ids (SQLite variable limit)."""
        if connection.vendor != "sqlite":
            pytest.skip("variable limit check uses the sqlite3 connection API")
        everyone = Group.objects.create(name="user")
        GroupDatasetPermission.objects.create(group=everyone, dataset=self.dataset, permission=self.view_perm)
        bucket = DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        for i in range(30):
            direct = self._user(f"direct{i:02d}@example.org")
            grant = Grant.objects.create(user=direct, dataset=self.dataset, permission=self.view_perm)
            grant.buckets.add(bucket)
            Affiliation.objects.create(user=direct, name=f"Org {i}")
            member = self._user(f"member{i:02d}@example.org")
            UserGroup.objects.create(user=member, group=everyone)

        connection.ensure_connection()
        raw = connection.connection
        previous = raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 10)
        try:
            rows = _dataset_access_rows(self.dataset)
        finally:
            raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)
        assert len(rows) == 61
        assert "view@all {bucket-a}" in {row[-1] for row in rows}


class TestExportSanitizing(_ExportTestBase):
    TRIGGERS = ["=", "+", "-", "@", "\t", "\r", "\n", "\uff1d", "\uff0b", "\uff0d", "\uff20"]

    def test_every_trigger_gains_a_leading_tab(self):
        for i, trigger in enumerate(self.TRIGGERS):
            user = self._user(f"t{i:02d}@example.org", name=f"{trigger}cmd|calc")
            Grant.objects.create(user=user, dataset=self.dataset, permission=self.view_perm)
        rows = self._rows_by_email()
        for i, trigger in enumerate(self.TRIGGERS):
            assert rows[f"t{i:02d}@example.org"]["full_name"] == f"\t{trigger}cmd|calc"

    def test_plain_values_are_untouched_and_quotes_round_trip(self):
        user = self._user("quote@example.org", name='Ann "Nan" O\'Neil,\nPhD')
        Grant.objects.create(user=user, dataset=self.dataset, permission=self.view_perm)
        assert self._rows_by_email()["quote@example.org"]["full_name"] == 'Ann "Nan" O\'Neil,\nPhD'

    def test_every_field_is_quoted(self):
        user = self._user("plain@example.org", name="Plain Name")
        Grant.objects.create(user=user, dataset=self.dataset, permission=self.view_perm)
        hostile = self._user("hostile@example.org", name="=1+1")
        Grant.objects.create(user=hostile, dataset=self.dataset, permission=self.view_perm)
        self._login(self.manager, self.manager_key)
        lines = self.client.get(self.url).content.decode("utf-8").split("\r\n")
        assert lines[0] == ",".join(f'"{name}"' for name in HEADER)
        assert lines[-1] == ""
        # Ordinary and sanitized data cells are quoted too: the tab prefix
        # must sit inside the quoted field.
        assert '"hostile@example.org","\t=1+1","view","all","","","direct","view@all"' in lines
        assert '"plain@example.org","Plain Name","view","all","","","direct","view@all"' in lines

    def test_formula_leading_email_and_affiliation_are_prefixed(self):
        user = self._user("+tag@example.org")
        Affiliation.objects.create(user=user, name='=HYPERLINK("http://example.org","x")')
        Grant.objects.create(user=user, dataset=self.dataset, permission=self.view_perm)
        rows = self._export()
        row = next(row for row in rows if row[0].endswith("+tag@example.org"))
        assert row[0] == "\t+tag@example.org"
        assert row[HEADER.index("affiliations")] == '\t=HYPERLINK("http://example.org","x")'


class TestMembersPage(_ExportTestBase):
    def _page(self):
        self._login(self.manager, self.manager_key)
        response = self.client.get(f"/web/grants/{self.dataset.name}")
        assert response.status_code == 200
        return response.content.decode("utf-8")

    def test_manager_sees_export_button_and_hint(self):
        page = self._page()
        assert f'href="/web/grants/{self.dataset.name}/export.csv"' in page
        assert "Export CSV" in page
        assert "group-wide permission" in page
        assert 'id="public-audiences"' not in page

    def test_public_note_names_public_audiences(self):
        self.dataset.access_mode = Dataset.ACCESS_PUBLIC
        self.dataset.save()
        DatasetVersion.objects.create(dataset=self.dataset, version="v0.6", is_public=True)
        DatasetVersion.objects.create(dataset=self.dataset, version="v0.7")
        table = ServiceTable.objects.create(service_name="cave", table_name="t1", dataset=self.dataset)
        PublicRoot.objects.create(service_table=table, root_id=1)
        PublicRoot.objects.create(service_table=table, root_id=2)
        page = self._page()
        assert 'id="public-audiences"' in page
        assert "This dataset is public" in page
        assert "Public version v0.6:" in page
        assert "v0.7" not in page.split('id="public-audiences"')[1].split("</div>")[0]
        assert "2 public root IDs" in page

    def test_public_note_for_public_version_only(self):
        DatasetVersion.objects.create(dataset=self.dataset, version="v0.6", is_public=True)
        page = self._page()
        assert "Public version v0.6:" in page
        assert "This dataset is public" not in page
        assert "public root ID" not in page
