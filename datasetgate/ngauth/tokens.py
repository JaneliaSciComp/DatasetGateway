"""HMAC-SHA256 token encode/decode — port from tos-ngauth auth.py.

Token format: base64(hmac_sha256(json) + json)
Cookie name: ngauth_login
"""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Optional


# Cookie lifetime: 1 year
MAX_COOKIE_LIFETIME_SECONDS = 60 * 60 * 24 * 365

# Cross-origin token lifetime: 1 hour
MAX_CROSS_ORIGIN_LIFETIME_SECONDS = 60 * 60

# HMAC length in bytes
MAC_LENGTH = 32


@dataclass
class UserToken:
    """ngauth user token."""

    user_id: str  # User's email address
    expires: int  # Unix timestamp

    def to_dict(self):
        return {"u": self.user_id, "e": self.expires}

    @classmethod
    def from_dict(cls, data):
        return cls(user_id=data["u"], expires=data["e"])


def compute_mac(key: bytes, data: bytes) -> bytes:
    """Compute HMAC-SHA256."""
    return hmac.new(key, data, hashlib.sha256).digest()


def encode_user_token(key: bytes, token: UserToken) -> str:
    """Encode user token with HMAC authentication.

    Format: base64(hmac_sha256(json) + json)
    """
    encoded_json = json.dumps(token.to_dict()).encode("utf-8")
    mac = compute_mac(key, encoded_json)
    return base64.b64encode(mac + encoded_json).decode("utf-8")


def decode_user_token(key: bytes, encoded_token: str) -> Optional[UserToken]:
    """Decode and verify user token. Returns None if invalid or expired."""
    try:
        raw = base64.b64decode(encoded_token)
    except Exception:
        return None

    if len(raw) < MAC_LENGTH:
        return None

    stored_mac = raw[:MAC_LENGTH]
    encoded_json = raw[MAC_LENGTH:]

    expected_mac = compute_mac(key, encoded_json)
    if not hmac.compare_digest(stored_mac, expected_mac):
        return None

    try:
        data = json.loads(encoded_json.decode("utf-8"))
        token = UserToken.from_dict(data)
    except Exception:
        return None

    if token.expires < int(time.time()):
        return None

    return token


def make_temporary_token(token: UserToken) -> UserToken:
    """Create a short-lived token for cross-origin use."""
    new_expires = int(time.time()) + MAX_CROSS_ORIGIN_LIFETIME_SECONDS
    if new_expires < token.expires:
        return UserToken(user_id=token.user_id, expires=new_expires)
    return token


def create_login_token(key: bytes, email: str) -> str:
    """Create a new login token for a user."""
    token = UserToken(
        user_id=email,
        expires=int(time.time()) + MAX_COOKIE_LIFETIME_SECONDS,
    )
    return encode_user_token(key, token)
