"""Disabled users' live dsg_token cookies are rejected on ngauth endpoints.

/token is the cookie-side gate for /gcs_token: without a freshly minted
temporary token, a disabled user cannot reach GCS credential minting.
"""

import json

import pytest
from django.conf import settings
from django.test import TestCase

from core.models import APIKey, User


@pytest.mark.django_db
class TestDisabledUserNgauth(TestCase):
    def setUp(self):
        self.user = User.objects.create(email="user@example.org", name="User")
        self.key = APIKey.objects.create(user=self.user, key="tok-ng-user")
        self.client.cookies[settings.AUTH_COOKIE_NAME] = self.key.key

    def _post_activate(self):
        return self.client.post(
            "/activate", data=json.dumps({}), content_type="application/json",
        )

    def test_disabled_user_activate_rejected(self):
        self.user.is_active = False
        self.user.save()
        resp = self._post_activate()
        self.assertEqual(resp.status_code, 401)

    def test_disabled_user_token_rejected(self):
        resp = self.client.post("/token")
        self.assertEqual(resp.status_code, 200)

        self.user.is_active = False
        self.user.save()
        resp = self.client.post("/token")
        self.assertEqual(resp.status_code, 401)

    def test_disabled_parent_sa_token_rejected(self):
        sa_user = User.objects.create(email="robot@example.org", parent=self.user)
        sa_key = APIKey.objects.create(user=sa_user, key="tok-ng-robot")
        self.client.cookies[settings.AUTH_COOKIE_NAME] = sa_key.key
        resp = self.client.post("/token")
        self.assertEqual(resp.status_code, 200)

        self.user.is_active = False
        self.user.save()
        resp = self.client.post("/token")
        self.assertEqual(resp.status_code, 401)

    def test_reenabled_user_token_works_again(self):
        self.user.is_active = False
        self.user.save()
        resp = self.client.post("/token")
        self.assertEqual(resp.status_code, 401)

        self.user.is_active = True
        self.user.save()
        resp = self.client.post("/token")
        self.assertEqual(resp.status_code, 200)

    def test_disabled_user_login_status_logged_out(self):
        resp = self.client.get("/login")
        self.assertTrue(resp.context["logged_in"])

        self.user.is_active = False
        self.user.save()
        resp = self.client.get("/login")
        self.assertFalse(resp.context["logged_in"])
