"""Tests for the admin login page override (web/templates/admin/login.html).

Admins are Google identities, so /admin/login/ leads with a "Sign in with
Google" link to /auth/login carrying the page's own `next`, while the stock
password form stays below as the break-glass door.
"""

import pytest
from django.conf import settings
from django.test import TestCase, override_settings

from core.models import User


def _google_href(next_value):
    return f'href="/auth/login?next={next_value}"'


@pytest.mark.django_db
# Rendering the admin login must not depend on running collectstatic first.
@override_settings(STORAGES={
    **settings.STORAGES,
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class TestAdminLoginGoogleButton(TestCase):
    def test_admin_redirects_anonymous_users_to_login_page_with_google_link(self):
        resp = self.client.get("/admin/", follow=True)
        self.assertEqual(resp.redirect_chain, [("/admin/login/?next=/admin/", 302)])
        self.assertContains(resp, "Sign in with Google")
        self.assertContains(resp, _google_href("/admin/"))

    def test_password_form_is_kept_below_the_google_link(self):
        resp = self.client.get("/admin/login/", {"next": "/admin/"})
        html = resp.content.decode()
        self.assertContains(resp, 'id="login-form"')
        self.assertContains(resp, '<input type="hidden" name="next" value="/admin/">', html=True)
        self.assertLess(html.index("Sign in with Google"), html.index('id="login-form"'))

    def test_login_page_without_next_targets_admin_index(self):
        resp = self.client.get("/admin/login/")
        self.assertContains(resp, _google_href("/admin/"))

    def test_deep_next_is_encoded_and_round_trips_through_auth_login(self):
        target = "/admin/core/user/?q=bergs&admin__exact=1"
        resp = self.client.get("/admin/login/", {"next": target})
        encoded = "/admin/core/user/%3Fq%3Dbergs%26admin__exact%3D1"
        self.assertContains(resp, _google_href(encoded))

        resp = self.client.get(f"/auth/login?next={encoded}")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/accounts/google/login/")
        self.assertEqual(self.client.session["oauth_next"], target)

    def test_offsite_next_falls_back_to_admin_index(self):
        resp = self.client.get("/admin/login/", {"next": "https://evil.example.com/steal"})
        self.assertContains(resp, _google_href("/admin/"))
        # The stock form's action echoes the full path; only our link must not carry it.
        self.assertNotContains(resp, "/auth/login?next=https")

    def test_signed_in_non_admin_sees_google_link_with_not_authorized_note(self):
        self.client.force_login(User.objects.create(email="gmail-user@example.org"))
        resp = self.client.get("/admin/", follow=True)
        self.assertContains(resp, "but are not authorized to")
        self.assertContains(resp, _google_href("/admin/"))

    def test_signed_in_admin_skips_the_login_page(self):
        self.client.force_login(User.objects.create(email="admin@example.org", admin=True))
        resp = self.client.get("/admin/login/")
        self.assertRedirects(resp, "/admin/", fetch_redirect_response=False)

    def test_password_login_still_works(self):
        user = User.objects.create(email="breakglass@example.org", admin=True)
        user.set_password("correct horse")
        user.save()
        resp = self.client.post("/admin/login/?next=/admin/", {
            "username": "breakglass@example.org",
            "password": "correct horse",
            "next": "/admin/",
        })
        self.assertRedirects(resp, "/admin/", fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)
