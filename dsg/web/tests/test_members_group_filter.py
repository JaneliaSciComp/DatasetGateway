"""Tests for the members page's group filter (/web/grants/<ds>?group=<name>)."""

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase

from core.models import APIKey, Dataset, Grant, Group, Permission, User


@pytest.mark.django_db
class TestMembersGroupFilter(TestCase):
    def setUp(self):
        cache.clear()
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.manage_perm, _ = Permission.objects.get_or_create(name="manage")

        self.dataset = Dataset.objects.create(name="fish2")
        self.url = f"/web/grants/{self.dataset.name}"

        self.manager = User.objects.create(email="manager@example.org", name="Manager")
        Grant.objects.create(user=self.manager, dataset=self.dataset, permission=self.manage_perm)
        self.client.cookies[settings.AUTH_COOKIE_NAME] = APIKey.objects.create(
            user=self.manager, key="tok-manager",
        ).key

        self.alpha = Group.objects.create(name="alpha-lab")
        self.rnd = Group.objects.create(name="R&D lab")
        self.alpha_grant = Grant.objects.create(
            user=User.objects.create(email="alpha-member@example.org"),
            dataset=self.dataset, permission=self.view_perm, group=self.alpha,
        )
        Grant.objects.create(
            user=User.objects.create(email="rnd-member@example.org"),
            dataset=self.dataset, permission=self.view_perm, group=self.rnd,
        )
        Grant.objects.create(
            user=User.objects.create(email="ungrouped@example.org"),
            dataset=self.dataset, permission=self.view_perm,
        )

    def _get(self, query=None):
        response = self.client.get(self.url, query or {})
        assert response.status_code == 200
        return response.content.decode("utf-8")

    @staticmethod
    def _grants_table(page):
        return page.split("<h2>Current Grants")[1].split("<h2>Service Account Grants")[0]

    @staticmethod
    def _filter_links(page):
        return page.split('id="group-filter"')[1].split("</p>")[0]

    def test_unfiltered_page_lists_every_grant(self):
        table = self._grants_table(self._get())
        for email in ("alpha-member@", "rnd-member@", "ungrouped@", "manager@"):
            assert email in table
        links = self._filter_links(self._get())
        assert "<strong>All</strong>" in links
        assert '<a href="?group=alpha-lab">alpha-lab</a>' in links

    def test_filter_shows_only_that_groups_grants(self):
        page = self._get({"group": "alpha-lab"})
        table = self._grants_table(page)
        assert "alpha-member@example.org" in table
        for email in ("rnd-member@", "ungrouped@", "manager@"):
            assert email not in table
        assert "— group alpha-lab" in page
        assert "it ignores the group filter" in page
        links = self._filter_links(page)
        assert '<a href="?">All</a>' in links
        assert "<strong>alpha-lab</strong>" in links
        # Other groups stay selectable while filtered.
        assert '<a href="?group=R%26D%20lab">R&amp;D lab</a>' in links

    def test_group_names_are_url_encoded_and_round_trip(self):
        table = self._grants_table(self._get({"group": "R&D lab"}))
        assert "rnd-member@example.org" in table
        assert "alpha-member@" not in table

    def test_unknown_group_shows_empty_state_with_way_back(self):
        page = self._get({"group": "nope"})
        assert "No grants through group nope." in page
        assert '<a href="?">All</a>' in self._filter_links(page)

    def test_export_link_ignores_the_filter(self):
        page = self._get({"group": "alpha-lab"})
        assert f'href="/web/grants/{self.dataset.name}/export.csv"' in page

    def test_revoke_from_filtered_page_keeps_the_filter(self):
        response = self.client.post(
            f"{self.url}?group=R%26D%20lab",
            {"action": "revoke", "grant_id": self.alpha_grant.pk},
        )
        assert response.status_code == 302
        assert response["Location"] == f"{self.url}?group=R%26D+lab"
        assert not Grant.objects.filter(pk=self.alpha_grant.pk).exists()

    def test_post_from_unfiltered_page_redirects_without_query(self):
        response = self.client.post(
            self.url, {"action": "revoke", "grant_id": self.alpha_grant.pk},
        )
        assert response["Location"] == self.url
