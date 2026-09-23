"""GCS token operations — port from tos-ngauth ngauth.py.

Provides:
- generate_bounded_access_token(): Downscoped GCS token via STS
- get_gcs_token_for_user(): Mint after the caller authorizes in DSG
"""

import json
import logging
import socket
import urllib.error
import urllib.request
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"


class GCSTokenMintError(RuntimeError):
    reason_code = "gcs_token_mint_error"

    def __init__(self):
        super().__init__(self.reason_code)


class ADCUnavailableError(GCSTokenMintError):
    reason_code = "adc_unavailable"


class STSResponseError(GCSTokenMintError):
    reason_code = "sts_response_error"


class STSUnavailableError(GCSTokenMintError):
    reason_code = "sts_unavailable"


def generate_bounded_access_token(bucket):
    """Generate a downscoped access token for a bucket."""
    try:
        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        request = google.auth.transport.requests.Request()
        credentials.refresh(request)
        access_token = credentials.token
        if not access_token:
            raise ValueError("ADC refresh returned no access token")
    except Exception as exc:
        raise ADCUnavailableError() from exc

    boundary = {
        "accessBoundary": {
            "accessBoundaryRules": [
                {
                    "availableResource": f"//storage.googleapis.com/projects/_/buckets/{bucket}",
                    "availablePermissions": ["inRole:roles/storage.objectViewer"],
                }
            ]
        }
    }

    data = urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "options": json.dumps(boundary),
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "subject_token": access_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        }
    ).encode()

    req = urllib.request.Request(
        STS_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        raise STSResponseError() from exc
    except (
        urllib.error.URLError,
        TimeoutError,
        socket.timeout,
        ConnectionError,
        OSError,
    ) as exc:
        raise STSUnavailableError() from exc

    try:
        result = json.loads(payload)
        token = result.get("access_token")
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise STSResponseError() from exc
    if not isinstance(token, str) or not token:
        raise STSResponseError()
    return token


def get_gcs_token_for_user(user_email, bucket):
    """Mint after the caller has authorized ``user_email`` against DSG's model."""
    return generate_bounded_access_token(bucket)
