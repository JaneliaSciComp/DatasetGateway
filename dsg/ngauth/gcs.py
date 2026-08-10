"""GCS token operations — port from tos-ngauth ngauth.py + iam.py.

Provides:
- generate_bounded_access_token(): Downscoped GCS token via STS
- get_gcs_token_for_user(): Mint after the caller authorizes in DSG
- add_user_to_bucket(): IAM provisioning for model-derived synchronization
"""

import json
import logging
import socket
import urllib.error
import urllib.request
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

OBJECT_VIEWER_ROLE = "roles/storage.objectViewer"
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


def probe_storage_permission(user_email, bucket):
    """Probe direct bucket IAM access.

    Returns True when the member is present, False when definitively absent,
    and None when the policy could not be read.
    """
    try:
        from google.cloud import storage

        client = storage.Client()
        bucket_obj = client.bucket(bucket)
        policy = bucket_obj.get_iam_policy(requested_policy_version=3)

        member = f"user:{user_email}"
        for binding in policy.bindings:
            if binding["role"] == OBJECT_VIEWER_ROLE:
                if member in binding.get("members", set()):
                    return True
        return False
    except Exception as e:
        logger.error(f"Error checking bucket IAM: {e}", extra={"user": user_email, "bucket": bucket})
        return None


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


def add_user_to_bucket(bucket_name, user_email):
    """Add a user to a bucket's IAM policy with objectViewer role.

    Returns "created", "already_present", or "failed".
    """
    try:
        from google.cloud import storage

        client = storage.Client()
        bucket = client.bucket(bucket_name)
        policy = bucket.get_iam_policy(requested_policy_version=3)

        member = f"user:{user_email}"
        for binding in policy.bindings:
            if binding["role"] == OBJECT_VIEWER_ROLE:
                if member in binding.get("members", set()):
                    logger.info(
                        "User already has bucket IAM",
                        extra={"email": user_email, "bucket": bucket_name},
                    )
                    return "already_present"

        policy.bindings.append({"role": OBJECT_VIEWER_ROLE, "members": {member}})
        bucket.set_iam_policy(policy)

        logger.info("Added user to bucket IAM", extra={"email": user_email, "bucket": bucket_name})
        return "created"
    except Exception as e:
        logger.error(f"Failed to add user to bucket IAM: {e}", extra={"email": user_email, "bucket": bucket_name})
        return "failed"


def remove_user_from_bucket(bucket_name, user_email):
    """Remove a user from a bucket's IAM policy objectViewer role."""
    try:
        from google.cloud import storage

        client = storage.Client()
        bucket = client.bucket(bucket_name)
        policy = bucket.get_iam_policy(requested_policy_version=3)

        member = f"user:{user_email}"
        new_bindings = []
        for binding in policy.bindings:
            if binding["role"] == OBJECT_VIEWER_ROLE:
                members = binding.get("members", set())
                members.discard(member)
                if members:
                    binding["members"] = members
                    new_bindings.append(binding)
                # Drop empty binding
            else:
                new_bindings.append(binding)
        policy.bindings = new_bindings
        bucket.set_iam_policy(policy)

        logger.info("Removed user from bucket IAM", extra={"email": user_email, "bucket": bucket_name})
        return True
    except Exception as e:
        logger.error(f"Failed to remove user from bucket IAM: {e}", extra={"email": user_email, "bucket": bucket_name})
        return False
