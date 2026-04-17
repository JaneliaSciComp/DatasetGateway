"""Unified token authentication for DRF.

Checks token in this order:
1. dsg_token cookie
2. Authorization: Bearer {token} header
3. ?dsg_token= query parameter

Looks up token in the APIKey table, returns the associated User.
"""

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .cache import build_permission_cache


class TokenAuthentication(BaseAuthentication):
    """DRF authentication class for DatasetGateway API tokens."""

    COOKIE_NAME = settings.AUTH_COOKIE_NAME
    QUERY_PARAM = "dsg_token"
    CACHE_PREFIX = "dsg_auth_"

    def authenticate_header(self, request):
        """Return a string to be used as the WWW-Authenticate header.

        Returning a value here causes DRF to return 401 (instead of 403)
        when authentication fails or is not provided.
        """
        return "Bearer"

    def authenticate(self, request):
        token = self._extract_token(request)
        if token is None:
            return None

        user = self._get_user_for_token(token)
        if user is None:
            raise AuthenticationFailed("Invalid or expired token.")

        if not user.is_active:
            raise AuthenticationFailed("User account is disabled.")

        # Attach cached permissions to the request for downstream views
        cache_key = f"{self.CACHE_PREFIX}{user.pk}"
        permission_cache = cache.get(cache_key)
        if permission_cache is None:
            permission_cache = build_permission_cache(user)
            cache.set(
                cache_key,
                permission_cache,
                getattr(settings, "PERMISSION_CACHE_TTL", 300),
            )
        request.permission_cache = permission_cache

        return (user, token)

    def _extract_token(self, request):
        """Extract token from cookie, header, or query param."""
        # 1. Cookie
        token = request.COOKIES.get(self.COOKIE_NAME)
        if token:
            return token

        # 2. Authorization header (takes priority in middle_auth_client)
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip()

        # 3. Query parameter
        token = request.query_params.get(self.QUERY_PARAM)
        if token:
            return token

        return None

    def _get_user_for_token(self, token):
        """Look up user by API key token."""
        from .models import APIKey

        try:
            api_key = APIKey.objects.select_related("user").get(key=token)
        except APIKey.DoesNotExist:
            return None

        if api_key.is_expired:
            return None

        # Update last_used timestamp
        APIKey.objects.filter(pk=api_key.pk).update(last_used=timezone.now())

        return api_key.user
