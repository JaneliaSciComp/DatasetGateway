"""Shared validation helpers for browser-supplied web origins."""

import re
from urllib.parse import urlparse


# One DNS label: alphanumeric, internal hyphens, 63 chars max.
_HOSTNAME_LABEL_RE = re.compile(
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
)


def is_origin_syntax_valid(origin):
    """Return whether *origin* is exactly a serialized HTTP(S) origin.

    Valid values have the form ``scheme://host[:port]``. Credentials, paths,
    queries, fragments, whitespace, malformed hostnames, and invalid ports are
    rejected before an origin is considered by any allowlist.
    """
    if not origin or not isinstance(origin, str):
        return False
    if origin != origin.strip() or any(c.isspace() for c in origin):
        return False
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.path or parsed.params or parsed.query or parsed.fragment:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    hostname = parsed.hostname
    if not hostname or hostname.endswith("."):
        return False
    labels = hostname.split(".")
    return all(_HOSTNAME_LABEL_RE.fullmatch(label) for label in labels)
