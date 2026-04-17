"""CAVE-compatible API views."""

from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Group, PublicRoot, ServiceTable, User


class UserCacheView(APIView):
    """GET /api/v1/user/cache

    The single most important CAVE endpoint. Every @auth_required and
    @auth_requires_permission decorator in middle_auth_client calls this
    to validate the token and retrieve the user's permissions.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        service = request.query_params.get("service")
        cache = getattr(request, "permission_cache", None)
        if cache is None or service:
            from core.cache import build_permission_cache

            cache = build_permission_cache(request.user, service=service)
        return Response(cache)


class TableDatasetView(APIView):
    """GET /api/v1/service/{namespace}/table/{table_id}/dataset

    Maps a service table to a dataset name. Called by
    @auth_requires_permission to resolve which dataset a table belongs to.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, namespace, table_id):
        try:
            st = ServiceTable.objects.select_related("dataset").get(
                service_name=namespace, table_name=table_id
            )
        except ServiceTable.DoesNotExist:
            return Response(
                {"error": "Table not found"}, status=status.HTTP_404_NOT_FOUND
            )
        return Response(st.dataset.name)


class UserPermissionsView(APIView):
    """GET /api/v1/user/{user_id}/permissions

    Returns user object with group membership. Called by
    users_share_common_group() in AnnotationEngine/MaterializationEngine.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, user_id):
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return Response(
                {"error": "User not found"}, status=status.HTTP_404_NOT_FOUND
            )

        from core.cache import build_permission_cache

        return Response(build_permission_cache(user))


class UsernameView(APIView):
    """GET /api/v1/username?id={id1},{id2},...

    Returns display names for a list of user IDs.
    Called by PyChunkedGraph's get_username_dict().
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        ids_str = request.query_params.get("id", "")
        if not ids_str:
            return Response([])

        try:
            user_ids = [int(x.strip()) for x in ids_str.split(",") if x.strip()]
        except ValueError:
            return Response(
                {"error": "Invalid user IDs"}, status=status.HTTP_400_BAD_REQUEST
            )

        users = User.objects.filter(pk__in=user_ids)
        return Response([{"id": u.pk, "name": u.public_name} for u in users])


class UserListView(APIView):
    """GET /api/v1/user?id={id1},{id2},...

    Returns full user info for a list of user IDs.
    Called by PyChunkedGraph's get_userinfo_dict().
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        ids_str = request.query_params.get("id", "")
        if not ids_str:
            return Response([])

        try:
            user_ids = [int(x.strip()) for x in ids_str.split(",") if x.strip()]
        except ValueError:
            return Response(
                {"error": "Invalid user IDs"}, status=status.HTTP_400_BAD_REQUEST
            )

        users = User.objects.filter(pk__in=user_ids)
        return Response(
            [
                {
                    "id": u.pk,
                    "name": u.public_name,
                    "email": u.email,
                    "admin": u.admin,
                    "pi": u.pi,
                    "picture_url": u.picture_url,
                }
                for u in users
            ]
        )


class GroupMembersView(APIView):
    """GET /api/v1/groups/<group_name>/members

    Returns list of email addresses for members of the given group.
    Used by clio-store to scope annotation visibility.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, group_name):
        try:
            group = Group.objects.get(name=group_name)
        except Group.DoesNotExist:
            return Response(
                {"error": "Group not found"}, status=status.HTTP_404_NOT_FOUND
            )
        emails = list(
            group.user_groups.select_related("user")
            .values_list("user__email", flat=True)
        )
        return Response(emails)


class TableHasPublicView(APIView):
    """GET /api/v1/table/{table_id}/has_public

    Check if a table has any public entries.
    """

    permission_classes = [AllowAny]

    def get(self, request, table_id):
        has_public = PublicRoot.objects.filter(
            service_table__table_name=table_id
        ).exists()
        return Response(has_public)


class RootIsPublicView(APIView):
    """GET /api/v1/table/{table_id}/root/{root_id}/is_public

    Check if a specific root is public.
    """

    permission_classes = [AllowAny]

    def get(self, request, table_id, root_id):
        is_public = PublicRoot.objects.filter(
            service_table__table_name=table_id, root_id=root_id
        ).exists()
        return Response(is_public)


class RootAllPublicView(APIView):
    """POST /api/v1/table/{table_id}/root_all_public

    Batch check which roots are public. Accepts JSON array of root IDs.
    """

    permission_classes = [AllowAny]

    def post(self, request, table_id):
        root_ids = request.data
        if not isinstance(root_ids, list):
            return Response(
                {"error": "Expected JSON array of root IDs"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        public_root_ids = set(
            PublicRoot.objects.filter(
                service_table__table_name=table_id, root_id__in=root_ids
            ).values_list("root_id", flat=True)
        )

        return Response([rid in public_root_ids for rid in root_ids])
