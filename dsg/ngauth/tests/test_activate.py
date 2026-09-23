"""Tests for POST /activate TOS acceptance audit logging."""

import json

import pytest
from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from core.models import (
    APIKey,
    AuditLog,
    Dataset,
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

    def test_acceptance_writes_audit_row_once(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "activated"})
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

    def _assert_bucket_only_rejected(self, data, content_type=None):
        if content_type:
            resp = self.client.post(
                "/activate", data=data, content_type=content_type,
            )
        else:
            resp = self.client.post("/activate", data=data)

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"error": "Missing tos_id"})
        self.assertFalse(TOSAcceptance.objects.exists())

    def test_legacy_bucket_json_payload_is_rejected_without_side_effects(self):
        self._assert_bucket_only_rejected(
            json.dumps({"bucket": "legacy-bucket"}),
            content_type="application/json",
        )

    def test_legacy_bucket_form_payload_is_rejected_without_side_effects(self):
        self._assert_bucket_only_rejected({"bucket": "legacy-bucket"})

    def test_blank_tos_with_legacy_bucket_is_rejected_without_side_effects(self):
        self._assert_bucket_only_rejected(
            json.dumps({"tos_id": "", "bucket": "legacy-bucket"}),
            content_type="application/json",
        )

    def test_legacy_bucket_with_valid_tos_is_ignored_and_logged(self):
        with self.assertLogs("ngauth.views", level="INFO") as logs:
            resp = self.client.post(
                "/activate",
                data=json.dumps({"tos_id": self.tos.pk, "bucket": "legacy-bucket"}),
                content_type="application/json",
            )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(TOSAcceptance.objects.filter(
            user=self.user, tos_document=self.tos,
        ).exists())
        record = next(
            record for record in logs.records
            if getattr(record, "decision", None) == "legacy_bucket_ignored"
        )
        self.assertEqual(record.bucket, "legacy-bucket")

    def test_datasetless_tos_with_legacy_bucket_is_accepted(self):
        datasetless_tos = TOSDocument.objects.create(name="General", text="Terms")

        resp = self.client.post(
            "/activate",
            data=json.dumps({
                "tos_id": datasetless_tos.pk,
                "bucket": "legacy-bucket",
            }),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(TOSAcceptance.objects.filter(
            user=self.user, tos_document=datasetless_tos,
        ).exists())

    def test_expired_api_key_is_rejected_without_acceptance(self):
        self.key.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        self.key.save(update_fields=["expires_at"])

        resp = self._post()

        self.assertEqual(resp.status_code, 401)
        self.assertFalse(TOSAcceptance.objects.exists())

    def test_nonexpiring_api_key_can_activate(self):
        self.key.expires_at = None
        self.key.save(update_fields=["expires_at"])

        resp = self._post()

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(TOSAcceptance.objects.filter(
            user=self.user, tos_document=self.tos,
        ).exists())
