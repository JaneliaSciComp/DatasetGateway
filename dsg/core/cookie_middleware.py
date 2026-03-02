"""Middleware to manage the dsg_token cookie.

Sets the cookie after allauth login (AccountAdapter.login() stashes the
APIKey value in the session) and clears it on logout (AccountAdapter.logout()
sets request._dsg_logout).
"""

from django.conf import settings


class DSGTokenCookieMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def _cookie_domain_kwargs(self):
        kwargs = {}
        cookie_domain = getattr(settings, "AUTH_COOKIE_DOMAIN", "")
        if cookie_domain:
            kwargs["domain"] = cookie_domain
        return kwargs

    def __call__(self, request):
        response = self.get_response(request)

        # Set cookie on login
        token_value = request.session.pop("dsg_token_value", None)
        if token_value:
            response.set_cookie(
                settings.AUTH_COOKIE_NAME,
                token_value,
                max_age=settings.AUTH_COOKIE_AGE,
                httponly=True,
                samesite="Lax",
                secure=settings.AUTH_COOKIE_SECURE,
                **self._cookie_domain_kwargs(),
            )

        # Clear cookie on logout (flagged by AccountAdapter.logout)
        if getattr(request, "_dsg_logout", False):
            response.delete_cookie(
                settings.AUTH_COOKIE_NAME,
                **self._cookie_domain_kwargs(),
            )

        return response
