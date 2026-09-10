"""SCIM authentication — Bearer token with admin privileges."""

from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from core.models import APIKey


class SCIMAuthentication(BaseAuthentication):
    """Authenticates SCIM requests. Requires admin user."""

    def authenticate_header(self, request):
        return "Bearer"

    def authenticate(self, request):
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return None

        token = auth_header[7:].strip()
        if not token:
            return None

        try:
            api_key = APIKey.objects.select_related("user").get(key=token)
        except APIKey.DoesNotExist:
            raise AuthenticationFailed("Invalid token.")

        if api_key.is_expired:
            raise AuthenticationFailed("Token has expired.")

        if api_key.delegated_client_id is not None:
            raise AuthenticationFailed("Delegated keys cannot access SCIM.")

        user = api_key.user
        if not user.is_enabled:
            raise AuthenticationFailed("User account is disabled.")

        if not user.admin:
            raise AuthenticationFailed("SCIM access requires admin privileges.")

        return (user, token)
