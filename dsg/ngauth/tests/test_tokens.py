"""Tests for ngauth HMAC token encode/decode."""

import time

from django.test import TestCase

from ngauth.tokens import (
    UserToken,
    decode_user_token,
    encode_user_token,
    make_temporary_token,
)


class TestHMACTokens(TestCase):
    def setUp(self):
        self.key = b"test-key-that-is-at-least-32-bytes-long!!"

    def test_round_trip(self):
        token = UserToken(user_id="alice@example.org", expires=int(time.time()) + 3600)
        encoded = encode_user_token(self.key, token)
        decoded = decode_user_token(self.key, encoded)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.user_id, "alice@example.org")

    def test_wrong_key_fails(self):
        token = UserToken(user_id="alice@example.org", expires=int(time.time()) + 3600)
        encoded = encode_user_token(self.key, token)
        decoded = decode_user_token(b"wrong-key-that-is-at-least-32-bytes!!!!", encoded)
        self.assertIsNone(decoded)

    def test_expired_token_fails(self):
        token = UserToken(user_id="alice@example.org", expires=int(time.time()) - 1)
        encoded = encode_user_token(self.key, token)
        decoded = decode_user_token(self.key, encoded)
        self.assertIsNone(decoded)

    def test_tampered_token_fails(self):
        token = UserToken(user_id="alice@example.org", expires=int(time.time()) + 3600)
        encoded = encode_user_token(self.key, token)
        # Flip a character
        tampered = encoded[:-1] + ("A" if encoded[-1] != "A" else "B")
        decoded = decode_user_token(self.key, tampered)
        self.assertIsNone(decoded)

    def test_make_temporary_token(self):
        token = UserToken(user_id="alice@example.org", expires=int(time.time()) + 86400)
        temp = make_temporary_token(token)
        # Temporary token should expire sooner (within ~1 hour)
        self.assertLess(temp.expires, token.expires)
        self.assertEqual(temp.user_id, token.user_id)
