"""Tests for the import_csv management command."""

import csv
import tempfile

import pytest
from django.core.management import call_command
from django.test import TestCase

from core.models import Affiliation, Dataset, Grant, Group, Permission, User, UserGroup


def _write_csv(rows, fieldnames=("email", "name", "affiliation", "notes")):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    f.close()
    return f.name


SAMPLE_ROWS = [
    {"email": "alice@example.org", "name": "Alice A", "affiliation": "HHMI", "notes": "PI lead"},
    {"email": "bob@example.org", "name": "Bob B", "affiliation": "MIT", "notes": ""},
    {"email": "carol@example.org", "name": "", "affiliation": "", "notes": "visiting"},
]


@pytest.mark.django_db
class TestImportCSV(TestCase):
    def test_basic_import(self):
        path = _write_csv(SAMPLE_ROWS)
        call_command("import_csv", path, "--dataset", "fish2")

        assert User.objects.filter(email="alice@example.org").exists()
        assert User.objects.filter(email="bob@example.org").exists()
        assert User.objects.filter(email="carol@example.org").exists()

        alice = User.objects.get(email="alice@example.org")
        assert alice.name == "Alice A"
        assert alice.notes == "PI lead"

        assert Dataset.objects.filter(name="fish2").exists()

        view_perm = Permission.objects.get(name="view")
        assert Grant.objects.filter(
            user=alice, dataset__name="fish2", permission=view_perm
        ).exists()

        user_group = Group.objects.get(name="user")
        assert UserGroup.objects.filter(user=alice, group=user_group).exists()

    def test_affiliations_created(self):
        path = _write_csv(SAMPLE_ROWS)
        call_command("import_csv", path, "--dataset", "fish2")

        alice = User.objects.get(email="alice@example.org")
        assert Affiliation.objects.filter(user=alice, name="HHMI").exists()

        bob = User.objects.get(email="bob@example.org")
        assert Affiliation.objects.filter(user=bob, name="MIT").exists()

        carol = User.objects.get(email="carol@example.org")
        assert Affiliation.objects.filter(user=carol).count() == 0

    def test_idempotency(self):
        path = _write_csv(SAMPLE_ROWS)
        call_command("import_csv", path, "--dataset", "fish2")
        call_command("import_csv", path, "--dataset", "fish2")

        assert User.objects.count() == 3
        alice = User.objects.get(email="alice@example.org")
        view_perm = Permission.objects.get(name="view")
        assert Grant.objects.filter(
            user=alice, dataset__name="fish2", permission=view_perm
        ).count() == 1
        assert Affiliation.objects.filter(user=alice, name="HHMI").count() == 1

    def test_union_with_existing_users(self):
        """CSV import unions with users created by other importers."""
        User.objects.create(email="alice@example.org", name="Alice Existing")

        path = _write_csv([
            {"email": "alice@example.org", "name": "Alice New", "affiliation": "HHMI", "notes": ""},
            {"email": "dave@example.org", "name": "Dave D", "affiliation": "Stanford", "notes": ""},
        ])
        call_command("import_csv", path, "--dataset", "fish2")

        assert User.objects.count() == 2
        alice = User.objects.get(email="alice@example.org")
        assert alice.name == "Alice Existing"
        assert Affiliation.objects.filter(user=alice, name="HHMI").exists()

        dave = User.objects.get(email="dave@example.org")
        assert dave.name == "Dave D"

    def test_affiliation_union_across_imports(self):
        """Multiple imports add to affiliation set, not replace."""
        path1 = _write_csv([
            {"email": "alice@example.org", "name": "Alice", "affiliation": "HHMI", "notes": ""},
        ])
        call_command("import_csv", path1, "--dataset", "fish2")

        path2 = _write_csv([
            {"email": "alice@example.org", "name": "Alice", "affiliation": "MIT", "notes": ""},
        ])
        call_command("import_csv", path2, "--dataset", "fish2")

        alice = User.objects.get(email="alice@example.org")
        affiliations = set(alice.affiliations.values_list("name", flat=True))
        assert affiliations == {"HHMI", "MIT"}

    def test_notes_appended(self):
        """Importing a user with notes twice appends."""
        path1 = _write_csv([
            {"email": "alice@example.org", "name": "Alice", "affiliation": "", "notes": "first note"},
        ])
        call_command("import_csv", path1, "--dataset", "fish2")

        path2 = _write_csv([
            {"email": "alice@example.org", "name": "Alice", "affiliation": "", "notes": "second note"},
        ])
        call_command("import_csv", path2, "--dataset", "fish2")

        alice = User.objects.get(email="alice@example.org")
        assert "first note" in alice.notes
        assert "second note" in alice.notes

    def test_dry_run_creates_nothing(self):
        path = _write_csv(SAMPLE_ROWS)
        call_command("import_csv", path, "--dataset", "fish2", "--dry-run")

        assert User.objects.count() == 0
        assert Dataset.objects.count() == 0
        assert Grant.objects.count() == 0
        assert Affiliation.objects.count() == 0

    def test_name_not_overwritten(self):
        """Existing name is not overwritten by CSV import."""
        User.objects.create(email="bob@example.org", name="Bob Original")

        path = _write_csv([
            {"email": "bob@example.org", "name": "Bob New", "affiliation": "", "notes": ""},
        ])
        call_command("import_csv", path, "--dataset", "fish2")

        bob = User.objects.get(email="bob@example.org")
        assert bob.name == "Bob Original"

    def test_name_filled_if_blank(self):
        """If existing user has no name, CSV name is applied."""
        User.objects.create(email="bob@example.org", name="")

        path = _write_csv([
            {"email": "bob@example.org", "name": "Bob B", "affiliation": "", "notes": ""},
        ])
        call_command("import_csv", path, "--dataset", "fish2")

        bob = User.objects.get(email="bob@example.org")
        assert bob.name == "Bob B"

    def test_multiple_datasets(self):
        """Separate imports for different datasets create grants on each."""
        path = _write_csv([
            {"email": "alice@example.org", "name": "Alice", "affiliation": "", "notes": ""},
        ])
        call_command("import_csv", path, "--dataset", "fish2")
        call_command("import_csv", path, "--dataset", "manc")

        alice = User.objects.get(email="alice@example.org")
        view_perm = Permission.objects.get(name="view")
        assert Grant.objects.filter(user=alice, permission=view_perm).count() == 2

    def test_empty_rows_skipped(self):
        """Rows with blank email are skipped."""
        path = _write_csv([
            {"email": "alice@example.org", "name": "Alice", "affiliation": "", "notes": ""},
            {"email": "", "name": "Nobody", "affiliation": "", "notes": ""},
        ])
        call_command("import_csv", path, "--dataset", "fish2")

        assert User.objects.count() == 1

    def test_case_insensitive_headers(self):
        """CSV headers are matched case-insensitively."""
        path = _write_csv(
            [{"Email": "alice@example.org", "Name": "Alice", "Affiliation": "HHMI", "Notes": ""}],
            fieldnames=("Email", "Name", "Affiliation", "Notes"),
        )
        call_command("import_csv", path, "--dataset", "fish2")

        assert User.objects.filter(email="alice@example.org").exists()
        assert Affiliation.objects.filter(name="HHMI").exists()
