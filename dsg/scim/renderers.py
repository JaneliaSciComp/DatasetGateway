"""SCIM 2.0 JSON renderer — application/scim+json content type."""

from rest_framework.renderers import JSONRenderer


class SCIMRenderer(JSONRenderer):
    media_type = "application/scim+json"
    format = "scim"
