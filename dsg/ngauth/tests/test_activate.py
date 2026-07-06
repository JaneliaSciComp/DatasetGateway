"""Tests for POST /activate TOS acceptance audit logging."""

import json
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import TestCase

from core.models import (
    APIKey,
    AuditLog,
    Dataset,
    DatasetBucket,
    Grant,
    Permission,
    TOSAcceptance,
    TOSDocument,
    User,
)


@pytest.mark.django_db
class TestActivateAudit(TestCase):
    def setUp(self):
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.user = User.objects.create(email="user@example.org", name="User")
        self.key = APIKey.objects.create(user=self.user, key="tok-activate")
        self.dataset = Dataset.objects.create(name="ds1")
        DatasetBucket.objects.create(dataset=self.dataset, name="bucket-a")
        self.tos = TOSDocument.objects.create(name="TOS", text="Terms", dataset=self.dataset)
        self.dataset.tos = self.tos
        self.dataset.save()
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)
        self.client.cookies[settings.AUTH_COOKIE_NAME] = self.key.key

    def _post(self):
        return self.client.post(
            "/activate",
            data=json.dumps({"tos_id": self.tos.pk}),
            content_type="application/json",
        )

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("ngauth.gcs.remove_user_from_bucket")
    def test_acceptance_writes_audit_row_once(self, mock_remove, mock_add):
        mock_add.return_value = True

        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        entry = AuditLog.objects.get(action="tos_accepted", target_type="TOSAcceptance")
        self.assertEqual(entry.actor, self.user)
        self.assertEqual(entry.after_state["dataset"], "ds1")
        self.assertEqual(
            TOSAcceptance.objects.filter(user=self.user, tos_document=self.tos).count(), 1,
        )

        # Re-POST: acceptance already exists → no second audit row
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AuditLog.objects.filter(action="tos_accepted").count(), 1)
