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

        principal = self._get_principal_for_token(token)
        if principal is None:
            raise AuthenticationFailed("Invalid or expired token.")

        from .models import ServiceAccount

        if not principal.is_active:
            if isinstance(principal, ServiceAccount):
                raise AuthenticationFailed("Service account is disabled.")
            raise AuthenticationFailed("User account is disabled.")

        # Attach cached permissions to the request for downstream views.
        # SA pks could collide with User pks, so namespace the cache key.
        if isinstance(principal, ServiceAccount):
            cache_key = f"{self.CACHE_PREFIX}sa_{principal.pk}"
        else:
            cache_key = f"{self.CACHE_PREFIX}{principal.pk}"
        permission_cache = cache.get(cache_key)
        if permission_cache is None:
            permission_cache = build_permission_cache(principal)
            cache.set(
                cache_key,
                permission_cache,
                getattr(settings, "PERMISSION_CACHE_TTL", 300),
            )
        request.permission_cache = permission_cache

        return (principal, token)

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

    def _get_principal_for_token(self, token):
        """Look up the principal (User or ServiceAccount) for a bearer token.

        Tries APIKey first, then ServiceAccountToken. Returns None if neither
        matches or the APIKey is expired.
        """
        from .models import APIKey, ServiceAccountToken

        try:
            api_key = APIKey.objects.select_related("user").get(key=token)
        except APIKey.DoesNotExist:
            api_key = None

        if api_key is not None:
            if api_key.is_expired:
                return None
            APIKey.objects.filter(pk=api_key.pk).update(last_used=timezone.now())
            return api_key.user

        try:
            sa_token = ServiceAccountToken.objects.select_related(
                "service_account"
            ).get(key=token)
        except ServiceAccountToken.DoesNotExist:
            return None

        ServiceAccountToken.objects.filter(pk=sa_token.pk).update(
            last_used=timezone.now()
        )
        return sa_token.service_account
