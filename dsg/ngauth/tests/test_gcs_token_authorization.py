"""DSG-authoritative authorization tests for POST /gcs_token."""

import json
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import TestCase

from core.models import (
    APIKey,
    BucketIAMBinding,
    Dataset,
    DatasetBucket,
    DatasetVersion,
    Grant,
    Group,
    GroupDatasetPermission,
    Permission,
    TOSAcceptance,
    TOSDocument,
    User,
    UserGroup,
)
from ngauth import gcs


@pytest.mark.django_db
class TestGCSTokenAuthorization(TestCase):
    def setUp(self):
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.user = User.objects.create(
            email="authorized@example.org",
            name="Authorized User",
        )
        self.api_key = APIKey.objects.create(
            user=self.user,
            key="gcs-token-cookie",
        )
        self.client.cookies[settings.AUTH_COOKIE_NAME] = self.api_key.key
        self.dataset = Dataset.objects.create(name="private-dataset")
        self.bucket = DatasetBucket.objects.create(
            dataset=self.dataset,
            name="private-bucket",
        )
        self.grant = Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            permission=self.view_perm,
        )

    def _mint_user_token(self):
        response = self.client.post("/token")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _post(self, bucket_name=None, user_token=None):
        return self.client.post(
            "/gcs_token",
            data=json.dumps({
                "token": user_token or self._mint_user_token(),
                "bucket": bucket_name or self.bucket.name,
            }),
            content_type="application/json",
        )

    @patch("ngauth.gcs.probe_storage_permission")
    @patch("ngauth.gcs.get_gcs_token_for_user", return_value="bounded-token")
    def test_grant_and_tos_issue_without_bucket_iam_binding(
        self, mock_get_token, mock_probe,
    ):
        tos = TOSDocument.objects.create(
            name="Private terms",
            text="Terms",
            dataset=self.dataset,
        )
        self.dataset.tos = tos
        self.dataset.save(update_fields=["tos"])
        TOSAcceptance.objects.create(user=self.user, tos_document=tos)
        self.assertFalse(BucketIAMBinding.objects.filter(
            bucket_name=self.bucket.name,
            email=self.user.email,
        ).exists())

        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"token": "bounded-token"})
        mock_get_token.assert_called_once_with(self.user.email, self.bucket.name)
        mock_probe.assert_not_called()

    @patch("ngauth.gcs.get_gcs_token_for_user")
    def test_unknown_bucket_is_denied_before_any_gcs_call(self, mock_get_token):
        response = self._post(bucket_name="unknown-bucket")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"error": "Access denied"})
        mock_get_token.assert_not_called()

    @patch("ngauth.gcs.get_gcs_token_for_user", return_value="bounded-token")
    def test_grant_and_tos_revocation_affect_the_next_request(self, mock_get_token):
        user_token = self._mint_user_token()
        self.assertEqual(self._post(user_token=user_token).status_code, 200)

        self.grant.delete()
        grant_denied = self._post(user_token=user_token)
        self.assertEqual(grant_denied.status_code, 403)
        self.assertEqual(grant_denied.json(), {"error": "Access denied"})

        self.grant = Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            permission=self.view_perm,
        )
        tos = TOSDocument.objects.create(
            name="Revocable terms",
            text="Terms",
            dataset=self.dataset,
        )
        self.dataset.tos = tos
        self.dataset.save(update_fields=["tos"])
        acceptance = TOSAcceptance.objects.create(
            user=self.user,
            tos_document=tos,
        )
        self.assertEqual(self._post(user_token=user_token).status_code, 200)

        acceptance.delete()
        tos_denied = self._post(user_token=user_token)
        self.assertEqual(tos_denied.status_code, 403)
        self.assertEqual(tos_denied.json()["error"], "tos_required")
        self.assertEqual(mock_get_token.call_count, 2)

    @patch("ngauth.gcs.get_gcs_token_for_user", return_value="bounded-token")
    def test_group_derived_grant_authorizes(self, mock_get_token):
        self.grant.delete()
        group = Group.objects.create(name="readers")
        UserGroup.objects.create(user=self.user, group=group)
        GroupDatasetPermission.objects.create(
            group=group,
            dataset=self.dataset,
            permission=self.view_perm,
        )

        response = self._post()

        self.assertEqual(response.status_code, 200)
        mock_get_token.assert_called_once()

    @patch("ngauth.gcs.get_gcs_token_for_user")
    def test_tos_pointer_emission_rule_three_ways(self, mock_get_token):
        covered_tos = TOSDocument.objects.create(
            name="Covered terms",
            text="Terms",
            dataset=self.dataset,
        )
        self.dataset.tos = covered_tos
        self.dataset.save(update_fields=["tos"])

        covered = self._post()
        self.assertEqual(covered.status_code, 403)
        self.assertEqual(covered.json()["error"], "tos_required")
        self.assertIn(self.dataset.name, covered.json()["message"])
        self.assertIn(covered.json()["tos_url"], covered.json()["message"])

        public_dataset = Dataset.objects.create(
            name="public-pending",
            access_mode=Dataset.ACCESS_PUBLIC,
        )
        public_bucket = DatasetBucket.objects.create(
            dataset=public_dataset,
            name="public-pending-bucket",
        )
        public_tos = TOSDocument.objects.create(
            name="Public terms",
            text="Terms",
            dataset=public_dataset,
        )
        public_dataset.tos = public_tos
        public_dataset.save(update_fields=["tos"])

        public = self._post(bucket_name=public_bucket.name)
        self.assertEqual(public.status_code, 403)
        self.assertEqual(public.json()["error"], "tos_required")

        closed_dataset = Dataset.objects.create(name="secret-dataset")
        closed_bucket = DatasetBucket.objects.create(
            dataset=closed_dataset,
            name="secret-bucket",
        )
        closed_tos = TOSDocument.objects.create(
            name="Secret terms",
            text="Terms",
            dataset=closed_dataset,
        )
        closed_dataset.tos = closed_tos
        closed_dataset.save(update_fields=["tos"])

        closed = self._post(bucket_name=closed_bucket.name)
        self.assertEqual(closed.status_code, 403)
        self.assertEqual(closed.json(), {"error": "Access denied"})
        self.assertNotIn(closed_dataset.name, closed.content.decode())
        self.assertNotIn("tos_url", closed.json())
        mock_get_token.assert_not_called()

    @patch("ngauth.gcs.get_gcs_token_for_user", return_value="bounded-token")
    def test_public_dataset_zero_grant_funnel_and_public_version_nonrule(
        self, mock_get_token,
    ):
        self.grant.delete()
        public_dataset = Dataset.objects.create(
            name="public-funnel",
            access_mode=Dataset.ACCESS_PUBLIC,
        )
        public_bucket = DatasetBucket.objects.create(
            dataset=public_dataset,
            name="public-funnel-bucket",
        )

        no_tos = self._post(bucket_name=public_bucket.name)
        self.assertEqual(no_tos.status_code, 200)
        self.assertFalse(Grant.objects.filter(
            user=self.user,
            dataset=public_dataset,
        ).exists())

        tos = TOSDocument.objects.create(
            name="Funnel terms",
            text="Terms",
            dataset=public_dataset,
        )
        public_dataset.tos = tos
        public_dataset.save(update_fields=["tos"])
        pending = self._post(bucket_name=public_bucket.name)
        self.assertEqual(pending.status_code, 403)
        self.assertEqual(pending.json()["error"], "tos_required")

        with (
            patch("ngauth.gcs.add_user_to_bucket"),
            patch("ngauth.gcs.remove_user_from_bucket"),
        ):
            activation = self.client.post(
                "/activate",
                data=json.dumps({"tos_id": tos.pk}),
                content_type="application/json",
            )
        self.assertEqual(activation.status_code, 200)
        self.assertTrue(TOSAcceptance.objects.filter(
            user=self.user,
            tos_document=tos,
        ).exists())
        accepted = self._post(bucket_name=public_bucket.name)
        self.assertEqual(accepted.status_code, 200)
        self.assertFalse(Grant.objects.filter(
            user=self.user,
            dataset=public_dataset,
        ).exists())

        closed_dataset = Dataset.objects.create(name="closed-public-version")
        closed_bucket = DatasetBucket.objects.create(
            dataset=closed_dataset,
            name="closed-public-version-bucket",
        )
        public_version = DatasetVersion.objects.create(
            dataset=closed_dataset,
            version="v1",
            ordinal=1,
            is_public=True,
        )
        public_version.buckets.add(closed_bucket)

        closed = self._post(bucket_name=closed_bucket.name)
        self.assertEqual(closed.status_code, 403)
        self.assertEqual(closed.json(), {"error": "Access denied"})
        self.assertEqual(mock_get_token.call_count, 2)

    @patch("ngauth.gcs.get_gcs_token_for_user")
    def test_version_tos_pointer_identifies_anchor_without_next(self, mock_get_token):
        version = DatasetVersion.objects.create(
            dataset=self.dataset,
            version="v1",
            ordinal=1,
        )
        version.buckets.add(self.bucket)
        self.grant.dataset_version = version
        self.grant.save(update_fields=["dataset_version"])
        TOSDocument.objects.create(
            name="Version terms",
            text="Terms",
            dataset_version=version,
        )

        response = self._post()

        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["error"], "tos_required")
        query = parse_qs(urlsplit(body["tos_url"]).query)
        self.assertEqual(query["dataset"], [self.dataset.name])
        self.assertEqual(query["version"], [version.version])
        self.assertNotIn("next", query)
        mock_get_token.assert_not_called()

    def test_mint_failure_status_mapping_uses_sanitized_reasons(self):
        cases = [
            (gcs.ADCUnavailableError(), 503, "Credential service unavailable"),
            (gcs.STSResponseError(), 502, "Credential exchange failed"),
            (gcs.STSUnavailableError(), 503, "Credential service unavailable"),
        ]

        for error, expected_status, expected_message in cases:
            with self.subTest(reason=error.reason_code):
                with (
                    patch(
                        "ngauth.gcs.get_gcs_token_for_user",
                        side_effect=error,
                    ),
                    self.assertLogs("ngauth.views", level="WARNING") as logs,
                ):
                    response = self._post()

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.json(), {"error": expected_message})
                self.assertEqual(logs.records[-1].reason, error.reason_code)
                self.assertNotIn("token", response.content.decode().lower())

    @patch("ngauth.gcs.get_gcs_token_for_user", return_value=None)
    def test_unusable_sts_response_maps_to_502(self, mock_get_token):
        response = self._post()

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"error": "Credential exchange failed"})

    def test_model_denial_and_issuance_are_logged_without_bearer_token(self):
        self.grant.delete()
        with self.assertLogs("ngauth.views", level="INFO") as denied_logs:
            denied = self._post()

        self.assertEqual(denied.status_code, 403)
        denied_record = denied_logs.records[-1]
        self.assertEqual(denied_record.user, self.user.email)
        self.assertEqual(denied_record.bucket, self.bucket.name)
        self.assertEqual(denied_record.decision, "denied")

        Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            permission=self.view_perm,
        )
        marker = "bearer-credential-marker"
        with (
            patch("ngauth.gcs.get_gcs_token_for_user", return_value=marker),
            self.assertLogs("ngauth.views", level="INFO") as issued_logs,
        ):
            issued = self._post()

        self.assertEqual(issued.status_code, 200)
        issued_record = issued_logs.records[-1]
        self.assertEqual(issued_record.user, self.user.email)
        self.assertEqual(issued_record.bucket, self.bucket.name)
        self.assertEqual(issued_record.decision, "issued")
        self.assertNotIn(marker, "\n".join(issued_logs.output))
