"""SCIM 2.0 JSON parser — application/scim+json content type."""

from rest_framework.parsers import JSONParser


class SCIMParser(JSONParser):
    media_type = "application/scim+json"
