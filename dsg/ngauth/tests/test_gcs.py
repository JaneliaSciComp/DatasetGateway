"""Unit tests for downscoped GCS token minting."""

import json
import urllib.error
from urllib.parse import parse_qs
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from ngauth import gcs


class _Credentials:
    def __init__(self, token="adc-access-token", refresh_error=None):
        self.token = token
        self.refresh_error = refresh_error

    def refresh(self, request):
        if self.refresh_error:
            raise self.refresh_error


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.payload


class TestBoundedGCSToken(SimpleTestCase):
    def _mint_with_response(self, payload):
        credentials = _Credentials()
        with (
            patch("google.auth.default", return_value=(credentials, None)),
            patch("google.auth.transport.requests.Request", return_value=Mock()),
            patch("urllib.request.urlopen", return_value=_Response(payload)) as urlopen,
        ):
            token = gcs.generate_bounded_access_token("bucket-a")
        return token, urlopen

    def test_access_boundary_is_exactly_one_whole_bucket_viewer_rule(self):
        token, urlopen = self._mint_with_response(b'{"access_token": "bounded"}')

        self.assertEqual(token, "bounded")
        request = urlopen.call_args.args[0]
        form = parse_qs(request.data.decode())
        boundary = json.loads(form["options"][0])
        self.assertEqual(boundary, {
            "accessBoundary": {
                "accessBoundaryRules": [{
                    "availableResource": (
                        "//storage.googleapis.com/projects/_/buckets/bucket-a"
                    ),
                    "availablePermissions": [
                        "inRole:roles/storage.objectViewer",
                    ],
                }],
            },
        })
        rule = boundary["accessBoundary"]["accessBoundaryRules"][0]
        self.assertNotIn("availabilityCondition", rule)

    def test_missing_adc_raises_typed_unavailable_error(self):
        with patch(
            "google.auth.default",
            side_effect=RuntimeError("credential-material-marker"),
        ):
            with self.assertRaises(gcs.ADCUnavailableError) as raised:
                gcs.generate_bounded_access_token("bucket-a")

        self.assertEqual(str(raised.exception), "adc_unavailable")

    def test_unrefreshable_adc_raises_typed_unavailable_error(self):
        credentials = _Credentials(
            refresh_error=RuntimeError("credential-material-marker"),
        )
        with (
            patch("google.auth.default", return_value=(credentials, None)),
            patch("google.auth.transport.requests.Request", return_value=Mock()),
        ):
            with self.assertRaises(gcs.ADCUnavailableError):
                gcs.generate_bounded_access_token("bucket-a")

    def test_sts_http_error_raises_typed_response_error(self):
        credentials = _Credentials()
        http_error = urllib.error.HTTPError(
            gcs.STS_TOKEN_URL,
            400,
            "credential-material-marker",
            {},
            None,
        )
        with (
            patch("google.auth.default", return_value=(credentials, None)),
            patch("google.auth.transport.requests.Request", return_value=Mock()),
            patch("urllib.request.urlopen", side_effect=http_error),
        ):
            with self.assertRaises(gcs.STSResponseError) as raised:
                gcs.generate_bounded_access_token("bucket-a")

        self.assertEqual(str(raised.exception), "sts_response_error")

    def test_unusable_sts_payload_raises_typed_response_error(self):
        with self.assertRaises(gcs.STSResponseError):
            self._mint_with_response(b'{"expires_in": 3600}')

    def test_unreachable_and_timed_out_sts_raise_typed_unavailable_error(self):
        errors = [
            urllib.error.URLError("credential-material-marker"),
            TimeoutError("credential-material-marker"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                credentials = _Credentials()
                with (
                    patch("google.auth.default", return_value=(credentials, None)),
                    patch(
                        "google.auth.transport.requests.Request",
                        return_value=Mock(),
                    ),
                    patch("urllib.request.urlopen", side_effect=error),
                ):
                    with self.assertRaises(gcs.STSUnavailableError) as raised:
                        gcs.generate_bounded_access_token("bucket-a")

                self.assertEqual(str(raised.exception), "sts_unavailable")

    def test_get_gcs_token_never_probes_per_user_bucket_iam(self):
        with (
            patch(
                "ngauth.gcs.generate_bounded_access_token",
                return_value="bounded",
            ) as mint,
            patch("ngauth.gcs.probe_storage_permission") as probe,
        ):
            token = gcs.get_gcs_token_for_user(
                "user@example.org",
                "bucket-a",
            )

        self.assertEqual(token, "bounded")
        mint.assert_called_once_with("bucket-a")
        probe.assert_not_called()
