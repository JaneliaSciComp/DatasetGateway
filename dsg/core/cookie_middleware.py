"""Middleware to set the dsg_token cookie after allauth login.

Allauth's login flow ends with a redirect that we don't control. The
AccountAdapter.login() stashes the APIKey value in the session; this
middleware pops it and sets the cookie on the outgoing response.
"""

from django.conf import settings


class DSGTokenCookieMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        token_value = request.session.pop("dsg_token_value", None)
        if token_value:
            cookie_kwargs = {
                "max_age": settings.AUTH_COOKIE_AGE,
                "httponly": True,
                "samesite": "Lax",
                "secure": settings.AUTH_COOKIE_SECURE,
            }
            cookie_domain = getattr(settings, "AUTH_COOKIE_DOMAIN", "")
            if cookie_domain:
                cookie_kwargs["domain"] = cookie_domain
            response.set_cookie(
                settings.AUTH_COOKIE_NAME,
                token_value,
                **cookie_kwargs,
            )

        return response
