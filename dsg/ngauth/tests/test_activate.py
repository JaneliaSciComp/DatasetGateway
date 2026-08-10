"""Tests for POST /activate TOS acceptance audit logging."""

import json
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from core.models import (
    APIKey,
    AuditLog,
    BucketIAMBinding,
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
        mock_add.return_value = "created"

        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        entry = AuditLog.objects.get(action="tos_accepted", target_type="TOSAcceptance")
        self.assertEqual(entry.actor, self.user)
        self.assertEqual(entry.after_state["dataset"], "ds1")
        self.assertEqual(
            TOSAcceptance.objects.filter(user=self.user, tos_document=self.tos).count(), 1,
        )
        self.assertTrue(BucketIAMBinding.objects.filter(
            bucket_name="bucket-a", email="user@example.org",
        ).exists())

        # Re-POST: acceptance already exists → no second audit row
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AuditLog.objects.filter(action="tos_accepted").count(), 1)

    def _assert_bucket_only_rejected(self, data, content_type=None):
        with (
            patch("core.iam.provision_binding") as mock_provision,
            patch("ngauth.gcs.add_user_to_bucket") as mock_iam,
        ):
            if content_type:
                resp = self.client.post(
                    "/activate", data=data, content_type=content_type,
                )
            else:
                resp = self.client.post("/activate", data=data)

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"error": "Missing tos_id"})
        mock_provision.assert_not_called()
        mock_iam.assert_not_called()
        self.assertFalse(BucketIAMBinding.objects.exists())

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

    @patch("core.iam.provision_binding")
    @patch("core.iam.sync_user_dataset_iam")
    def test_legacy_bucket_with_valid_tos_is_ignored_and_logged(
        self, mock_sync, mock_provision,
    ):
        with self.assertLogs("ngauth.views", level="INFO") as logs:
            resp = self.client.post(
                "/activate",
                data=json.dumps({"tos_id": self.tos.pk, "bucket": "legacy-bucket"}),
                content_type="application/json",
            )

        self.assertEqual(resp.status_code, 200)
        mock_sync.assert_called_once_with(self.user, self.dataset)
        mock_provision.assert_not_called()
        self.assertTrue(TOSAcceptance.objects.filter(
            user=self.user, tos_document=self.tos,
        ).exists())
        self.assertFalse(BucketIAMBinding.objects.exists())
        record = next(
            record for record in logs.records
            if getattr(record, "decision", None) == "legacy_bucket_ignored"
        )
        self.assertEqual(record.bucket, "legacy-bucket")

    @patch("ngauth.gcs.add_user_to_bucket")
    @patch("core.iam.provision_binding")
    def test_datasetless_tos_with_legacy_bucket_never_provisions(
        self, mock_provision, mock_iam,
    ):
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
        mock_provision.assert_not_called()
        mock_iam.assert_not_called()
        self.assertTrue(TOSAcceptance.objects.filter(
            user=self.user, tos_document=datasetless_tos,
        ).exists())
        self.assertFalse(BucketIAMBinding.objects.exists())

    def test_expired_api_key_is_rejected_without_acceptance_or_iam(self):
        self.key.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        self.key.save(update_fields=["expires_at"])

        with patch("ngauth.gcs.add_user_to_bucket") as mock_iam:
            resp = self._post()

        self.assertEqual(resp.status_code, 401)
        mock_iam.assert_not_called()
        self.assertFalse(TOSAcceptance.objects.exists())
        self.assertFalse(BucketIAMBinding.objects.exists())

    def test_nonexpiring_api_key_can_activate(self):
        self.key.expires_at = None
        self.key.save(update_fields=["expires_at"])

        with (
            patch("ngauth.gcs.add_user_to_bucket", return_value="already_present"),
            patch("ngauth.gcs.remove_user_from_bucket"),
        ):
            resp = self._post()

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(TOSAcceptance.objects.filter(
            user=self.user, tos_document=self.tos,
        ).exists())
