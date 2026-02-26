"""SCIM utility functions — UUID5 ID generation, error responses."""

import uuid
from datetime import datetime

from rest_framework.response import Response

# UUID namespace for deterministic ID conversion (same as CAVE's scim/utils.py)
SCIM_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def generate_scim_id(internal_id, resource_type):
    """Convert internal integer ID to SCIM-compliant UUID string.

    Uses UUID5 for deterministic mapping — same internal ID + resource type
    always maps to the same UUID.
    """
    return str(uuid.uuid5(SCIM_NAMESPACE, f"{resource_type}:{internal_id}"))


def scim_error(status, detail=None, scim_type=None):
    """Build a SCIM error response."""
    body = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
        "status": str(status),
    }
    if scim_type:
        body["scimType"] = scim_type
    if detail:
        body["detail"] = detail
    return Response(body, status=status, content_type="application/scim+json")


def format_datetime(dt):
    """Format datetime to SCIM ISO 8601 format."""
    if dt is None:
        return None
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.isoformat() + "Z"
    return dt.isoformat()
