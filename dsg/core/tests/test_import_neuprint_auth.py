"""Tests for the import_neuprint_auth management command."""

import json
import tempfile

import pytest
from django.core.management import call_command
from django.test import TestCase

from core.models import Dataset, Grant, Group, Permission, User, UserGroup


def _write_auth_json(data):
    """Write a dict to a temp JSON file and return the path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return f.name


SAMPLE_AUTH = {
    "alice@example.org": "admin",
    "bob@example.org": "readwrite",
    "carol@example.org": "readonly",
}


@pytest.mark.django_db
class TestImportNeurprintAuth(TestCase):
    def test_basic_import(self):
        """Import creates users, grants, and sets admin flag."""
        path = _write_auth_json(SAMPLE_AUTH)
        call_command("import_neuprint_auth", path, "--datasets", "hemibrain", "manc")

        # Users created
        assert User.objects.filter(email="alice@example.org").exists()
        assert User.objects.filter(email="bob@example.org").exists()
        assert User.objects.filter(email="carol@example.org").exists()

        # Admin flag
        alice = User.objects.get(email="alice@example.org")
        assert alice.admin is True

        bob = User.objects.get(email="bob@example.org")
        assert bob.admin is False

        # Datasets created
        assert Dataset.objects.filter(name="hemibrain").exists()
        assert Dataset.objects.filter(name="manc").exists()

        # Grants: bob has edit on both datasets
        edit_perm = Permission.objects.get(name="edit")
        assert Grant.objects.filter(
            user=bob, dataset__name="hemibrain", permission=edit_perm
        ).exists()
        assert Grant.objects.filter(
            user=bob, dataset__name="manc", permission=edit_perm
        ).exists()

        # Grants: carol has view on both datasets
        view_perm = Permission.objects.get(name="view")
        assert Grant.objects.filter(
            user__email="carol@example.org",
            dataset__name="hemibrain",
            permission=view_perm,
        ).exists()
        assert Grant.objects.filter(
            user__email="carol@example.org",
            dataset__name="manc",
            permission=view_perm,
        ).exists()

        # Admin users don't get per-dataset grants (they get global admin)
        assert not Grant.objects.filter(user=alice).exists()

        # All users in "user" group
        user_group = Group.objects.get(name="user")
        assert UserGroup.objects.filter(user=alice, group=user_group).exists()
        assert UserGroup.objects.filter(user=bob, group=user_group).exists()

    def test_idempotency(self):
        """Running import twice does not create duplicate entries."""
        path = _write_auth_json(SAMPLE_AUTH)
        call_command("import_neuprint_auth", path, "--datasets", "hemibrain")
        call_command("import_neuprint_auth", path, "--datasets", "hemibrain")

        # Still only 3 users
        assert User.objects.count() == 3

        # Bob still has exactly 1 edit grant on hemibrain
        bob = User.objects.get(email="bob@example.org")
        edit_perm = Permission.objects.get(name="edit")
        assert (
            Grant.objects.filter(
                user=bob, dataset__name="hemibrain", permission=edit_perm
            ).count()
            == 1
        )

    def test_admin_not_downgraded(self):
        """If a user is already admin, re-importing as non-admin keeps admin."""
        # First: import alice as admin
        path1 = _write_auth_json({"alice@example.org": "admin"})
        call_command("import_neuprint_auth", path1, "--datasets", "hemibrain")
        assert User.objects.get(email="alice@example.org").admin is True

        # Second: import alice as readonly (shouldn't downgrade)
        path2 = _write_auth_json({"alice@example.org": "readonly"})
        call_command("import_neuprint_auth", path2, "--datasets", "hemibrain")
        assert User.objects.get(email="alice@example.org").admin is True

    def test_dry_run_creates_nothing(self):
        """--dry-run prints preview but creates no database records."""
        path = _write_auth_json(SAMPLE_AUTH)
        call_command(
            "import_neuprint_auth", path, "--datasets", "hemibrain", "--dry-run"
        )

        assert User.objects.count() == 0
        assert Dataset.objects.count() == 0
        assert Grant.objects.count() == 0

    def test_unknown_level_skipped(self):
        """Unknown permission levels are skipped with a warning."""
        auth = {"alice@example.org": "superuser"}
        path = _write_auth_json(auth)
        call_command("import_neuprint_auth", path, "--datasets", "hemibrain")

        assert User.objects.count() == 0
        assert Grant.objects.count() == 0

    def test_existing_user_gets_grant(self):
        """If a user already exists in DSG, import adds grants without error."""
        # Pre-create user
        User.objects.create(email="bob@example.org", name="Bob")

        path = _write_auth_json({"bob@example.org": "readwrite"})
        call_command("import_neuprint_auth", path, "--datasets", "hemibrain")

        bob = User.objects.get(email="bob@example.org")
        edit_perm = Permission.objects.get(name="edit")
        assert Grant.objects.filter(
            user=bob, dataset__name="hemibrain", permission=edit_perm
        ).exists()

    def test_multiple_datasets(self):
        """Grants are created for every specified dataset."""
        path = _write_auth_json({"carol@example.org": "readonly"})
        call_command(
            "import_neuprint_auth", path, "--datasets", "ds1", "ds2", "ds3"
        )

        view_perm = Permission.objects.get(name="view")
        carol = User.objects.get(email="carol@example.org")
        assert Grant.objects.filter(user=carol, permission=view_perm).count() == 3

    def test_summary_output(self):
        """Command output includes per-dataset added/existed summary."""
        path = _write_auth_json({"bob@example.org": "readwrite"})

        # First import — bob is added
        out = self._call_and_capture(path, "--datasets", "hemibrain")
        assert "bob@example.org (edit)" in out
        assert "Added" in out

        # Second import — bob already exists
        out = self._call_and_capture(path, "--datasets", "hemibrain")
        assert "Already existed" in out

    def _call_and_capture(self, *args):
        from io import StringIO

        out = StringIO()
        call_command("import_neuprint_auth", *args, stdout=out)
        return out.getvalue()
