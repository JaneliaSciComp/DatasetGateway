"""CAVE-compatible API views."""

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


class UserCacheView(APIView):
    """GET /api/v1/user/cache

    The single most important CAVE endpoint. Every @auth_required and
    @auth_requires_permission decorator in middle_auth_client calls this
    to validate the token and retrieve the user's permissions.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        cache = getattr(request, "permission_cache", None)
        if cache is None:
            from core.cache import build_permission_cache

            cache = build_permission_cache(request.user)
        return Response(cache)
