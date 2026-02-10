"""Stub authentication — will be implemented in Step 4."""

from rest_framework.authentication import BaseAuthentication


class CaveTokenAuthentication(BaseAuthentication):
    def authenticate(self, request):
        return None
