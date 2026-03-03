"""Tests that import management commands create AuditLog entries."""

import json
import tempfile

import pytest
from django.core.management import call_command
from django.test import TestCase

from core.models import AuditLog


def _write_auth_json(data):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return f.name


@pytest.mark.django_db
class TestImportNeurprintAudit(TestCase):
    def test_bulk_import_audit_entry(self):
        path = _write_auth_json({"alice@example.org": "readonly"})
        call_command("import_neuprint_auth", path, "--datasets", "hemibrain")
        entry = AuditLog.objects.get(action="bulk_import", target_id="import_neuprint_auth")
        assert entry.actor is None
        assert entry.after_state["datasets"] == ["hemibrain"]
        assert entry.after_state["users"] == 1

    def test_dry_run_no_audit(self):
        path = _write_auth_json({"alice@example.org": "readonly"})
        call_command("import_neuprint_auth", path, "--datasets", "hemibrain", "--dry-run")
        assert not AuditLog.objects.filter(action="bulk_import").exists()


@pytest.mark.django_db
class TestImportClioAudit(TestCase):
    def test_bulk_import_audit_entry(self):
        data = {
            "datasets": {"ds1": {"public": False}},
            "users": {
                "bob@example.org": {
                    "name": "Bob",
                    "global_roles": [],
                    "datasets": {"ds1": ["clio_read"]},
                    "groups": [],
                }
            },
        }
        path = _write_auth_json(data)
        call_command("import_clio_auth", path)
        entry = AuditLog.objects.get(action="bulk_import", target_id="import_clio_auth")
        assert entry.actor is None
        assert entry.after_state["users"] == 1
        assert entry.after_state["datasets"] == 1
