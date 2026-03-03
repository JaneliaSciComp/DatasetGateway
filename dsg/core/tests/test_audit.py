"""Tests for the core audit logging helper."""

import pytest
from django.test import TestCase

from core.audit import log_audit
from core.models import AuditLog, User


@pytest.mark.django_db
class TestLogAudit(TestCase):
    def setUp(self):
        self.actor = User.objects.create(email="actor@example.org", name="Actor")

    def test_creates_entry(self):
        log_audit(self.actor, "test_action", "Widget", "42")
        entry = AuditLog.objects.get()
        assert entry.actor == self.actor
        assert entry.action == "test_action"
        assert entry.target_type == "Widget"
        assert entry.target_id == "42"
        assert entry.before_state is None
        assert entry.after_state is None

    def test_with_before_and_after_state(self):
        before = {"name": "old"}
        after = {"name": "new"}
        log_audit(self.actor, "updated", "Widget", "1",
                  before_state=before, after_state=after)
        entry = AuditLog.objects.get()
        assert entry.before_state == {"name": "old"}
        assert entry.after_state == {"name": "new"}

    def test_none_actor(self):
        """Management commands may pass actor=None."""
        log_audit(None, "bulk_import", "Command", "import_foo")
        entry = AuditLog.objects.get()
        assert entry.actor is None
        assert entry.action == "bulk_import"

    def test_target_id_coerced_to_string(self):
        log_audit(self.actor, "created", "Grant", 99)
        entry = AuditLog.objects.get()
        assert entry.target_id == "99"

    def test_timestamp_auto_set(self):
        log_audit(self.actor, "created", "Grant", "1")
        entry = AuditLog.objects.get()
        assert entry.timestamp is not None

    def test_multiple_entries(self):
        log_audit(self.actor, "a", "X", "1")
        log_audit(self.actor, "b", "Y", "2")
        assert AuditLog.objects.count() == 2
