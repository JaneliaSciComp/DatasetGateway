"""Tests for the /api/v1/groups/<name>/members endpoint."""

import pytest
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import APIKey, Group, User, UserGroup


@pytest.mark.django_db
class TestGroupMembersView(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.admin = User.objects.create(email="admin@example.org", name="admin", admin=True)
        self.api_key = APIKey.objects.create(user=self.admin, key="group-test-token")

    def _auth_header(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def test_unauthenticated_returns_401(self):
        resp = self.client.get("/api/v1/groups/team/members")
        self.assertEqual(resp.status_code, 401)

    def test_nonexistent_group_returns_404(self):
        resp = self.client.get("/api/v1/groups/nonexistent/members", **self._auth_header())
        self.assertEqual(resp.status_code, 404)

    def test_empty_group_returns_empty_list(self):
        Group.objects.create(name="empty-group")
        resp = self.client.get("/api/v1/groups/empty-group/members", **self._auth_header())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_group_with_members(self):
        group = Group.objects.create(name="teamA")
        alice = User.objects.create(email="alice@example.org", name="alice")
        bob = User.objects.create(email="bob@example.org", name="bob")
        UserGroup.objects.create(user=alice, group=group)
        UserGroup.objects.create(user=bob, group=group)

        resp = self.client.get("/api/v1/groups/teamA/members", **self._auth_header())
        self.assertEqual(resp.status_code, 200)
        emails = resp.json()
        self.assertEqual(sorted(emails), ["alice@example.org", "bob@example.org"])

    def test_only_members_of_requested_group(self):
        group_a = Group.objects.create(name="groupA")
        group_b = Group.objects.create(name="groupB")
        alice = User.objects.create(email="alice@example.org")
        bob = User.objects.create(email="bob@example.org")
        UserGroup.objects.create(user=alice, group=group_a)
        UserGroup.objects.create(user=bob, group=group_b)

        resp = self.client.get("/api/v1/groups/groupA/members", **self._auth_header())
        emails = resp.json()
        self.assertEqual(emails, ["alice@example.org"])
