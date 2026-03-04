"""GCS token operations — port from tos-ngauth ngauth.py + iam.py.

Provides:
- check_storage_permission(): Direct bucket IAM check
- generate_bounded_access_token(): Downscoped GCS token via STS
- get_gcs_token_for_user(): Main entry point combining both
- add_user_to_bucket(): IAM provisioning for "activate" flow
"""

import json
import logging
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

OBJECT_VIEWER_ROLE = "roles/storage.objectViewer"
STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"


def check_storage_permission(user_email, bucket):
    """Check if a user has objectViewer access on a bucket via direct IAM check."""
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
        return False


def generate_bounded_access_token(bucket):
    """Generate a downscoped access token for a bucket."""
    try:
        import google.auth
        import google.auth.transport.requests
        import urllib.request

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        request = google.auth.transport.requests.Request()
        credentials.refresh(request)
        access_token = credentials.token

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

        data = urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "options": json.dumps(boundary),
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "subject_token": access_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        }).encode()

        req = urllib.request.Request(
            STS_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result.get("access_token")

    except Exception as e:
        logger.error(f"Error generating bounded token: {e}", extra={"bucket": bucket})
        return None


def get_gcs_token_for_user(user_email, bucket):
    """Get a GCS access token for a user if they have bucket access.

    1. Verify user has access via direct bucket IAM check
    2. Generate bounded access token
    """
    if not check_storage_permission(user_email, bucket):
        logger.info("User does not have bucket access", extra={"user": user_email, "bucket": bucket})
        return None

    token = generate_bounded_access_token(bucket)
    if token:
        logger.info("Generated GCS token for user", extra={"user": user_email, "bucket": bucket})
    return token


def add_user_to_bucket(bucket_name, user_email):
    """Add a user to a bucket's IAM policy with objectViewer role."""
    try:
        from google.cloud import storage

        client = storage.Client()
        bucket = client.bucket(bucket_name)
        policy = bucket.get_iam_policy(requested_policy_version=3)

        member = f"user:{user_email}"
        policy.bindings.append({"role": OBJECT_VIEWER_ROLE, "members": {member}})
        bucket.set_iam_policy(policy)

        logger.info("Added user to bucket IAM", extra={"email": user_email, "bucket": bucket_name})
        return True
    except Exception as e:
        logger.error(f"Failed to add user to bucket IAM: {e}", extra={"email": user_email, "bucket": bucket_name})
        return False


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
