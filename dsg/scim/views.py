"""SCIM 2.0 views — User, Group, Dataset CRUD + discovery endpoints."""

from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import log_audit
from core.models import Dataset, Group, ServiceTable, User, UserGroup

from .authentication import SCIMAuthentication
from .filters import SCIMFilterError, apply_scim_filter
from .pagination import SCIMPaginator
from .parsers import SCIMParser
from .renderers import SCIMRenderer
from .serializers import (
    DATASET_SCHEMA,
    GROUP_SCHEMA,
    USER_EXTENSION,
    USER_SCHEMA,
    DatasetSCIMSerializer,
    GroupSCIMSerializer,
    UserSCIMSerializer,
)
from .utils import generate_scim_id, scim_error


class SCIMBaseView(APIView):
    """Base class for SCIM views with common settings."""

    authentication_classes = [SCIMAuthentication]
    permission_classes = [IsAuthenticated]
    renderer_classes = [SCIMRenderer]
    parser_classes = [SCIMParser, JSONParser]


# =============================================================================
# Discovery Endpoints
# =============================================================================


class ServiceProviderConfigView(SCIMBaseView):
    """GET /auth/scim/v2/ServiceProviderConfig"""

    def get(self, request):
        return Response({
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "documentationUri": "",
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 1000},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "OAuth Bearer Token",
                    "description": "Bearer token with admin privileges",
                }
            ],
        })


class ResourceTypesView(SCIMBaseView):
    """GET /auth/scim/v2/ResourceTypes"""

    def get(self, request):
        return Response([
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                "id": "User",
                "name": "User",
                "endpoint": "/Users",
                "schema": USER_SCHEMA,
                "schemaExtensions": [
                    {"schema": USER_EXTENSION, "required": False}
                ],
            },
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                "id": "Group",
                "name": "Group",
                "endpoint": "/Groups",
                "schema": GROUP_SCHEMA,
            },
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                "id": "Dataset",
                "name": "Dataset",
                "endpoint": "/Datasets",
                "schema": DATASET_SCHEMA,
            },
        ])


class SchemasView(SCIMBaseView):
    """GET /auth/scim/v2/Schemas"""

    def get(self, request):
        return Response([
            {
                "id": USER_SCHEMA,
                "name": "User",
                "description": "Core User schema",
                "attributes": [
                    {"name": "userName", "type": "string", "required": True},
                    {"name": "displayName", "type": "string"},
                    {"name": "active", "type": "boolean"},
                ],
            },
            {
                "id": USER_EXTENSION,
                "name": "Neuroglancer User Extension",
                "description": "Neuroglancer-specific user attributes",
                "attributes": [
                    {"name": "admin", "type": "boolean"},
                    {"name": "pi", "type": "string"},
                    {"name": "gdprConsent", "type": "boolean"},
                    {"name": "serviceAccount", "type": "boolean", "mutability": "readOnly"},
                ],
            },
            {
                "id": GROUP_SCHEMA,
                "name": "Group",
                "description": "Core Group schema",
                "attributes": [
                    {"name": "displayName", "type": "string", "required": True},
                    {"name": "members", "type": "complex", "multiValued": True},
                ],
            },
            {
                "id": DATASET_SCHEMA,
                "name": "Dataset",
                "description": "Neuroglancer Dataset schema",
                "attributes": [
                    {"name": "name", "type": "string", "required": True},
                    {"name": "description", "type": "string"},
                    {"name": "tosId", "type": "integer"},
                    {"name": "serviceTables", "type": "complex", "multiValued": True},
                ],
            },
        ])


# =============================================================================
# User CRUD
# =============================================================================


class UserListView(SCIMBaseView):
    """GET /auth/scim/v2/Users — List users
    POST /auth/scim/v2/Users — Create user
    """

    USER_ATTR_MAP = {
        "userName": "email",
        "emails.value": "email",
        "displayName": "display_name",
        "name.givenName": "name",
        "name.familyName": "name",
        "active": "is_active",
    }

    def get(self, request):
        qs = User.objects.all()

        filter_expr = request.query_params.get("filter")
        if filter_expr:
            try:
                qs = apply_scim_filter(qs, filter_expr, self.USER_ATTR_MAP)
            except SCIMFilterError as e:
                return scim_error(400, detail=str(e), scim_type="invalidFilter")

        paginator = SCIMPaginator(request)
        items, total = paginator.paginate_queryset(qs.order_by("pk"))
        resources = [UserSCIMSerializer.to_scim(u) for u in items]
        return Response(paginator.get_response_data(resources, total))

    def post(self, request):
        data = request.data
        fields = UserSCIMSerializer.from_scim(data)

        if "email" not in fields:
            return scim_error(400, detail="userName is required", scim_type="invalidValue")

        if User.objects.filter(email=fields["email"]).exists():
            return scim_error(409, detail="User already exists", scim_type="uniqueness")

        user = User.objects.create(**fields)
        user.scim_id = generate_scim_id(user.pk, "User")
        user.save(update_fields=["scim_id"])

        log_audit(request.user, "user_created", "User", user.pk, after_state={
            "email": user.email, "name": user.name, "admin": user.admin,
        })

        return Response(
            UserSCIMSerializer.to_scim(user),
            status=201,
        )


class UserDetailView(SCIMBaseView):
    """GET/PUT/PATCH/DELETE /auth/scim/v2/Users/{scim_id}"""

    def _get_user(self, scim_id):
        try:
            return User.objects.get(scim_id=scim_id)
        except User.DoesNotExist:
            return None

    def get(self, request, scim_id):
        user = self._get_user(scim_id)
        if not user:
            return scim_error(404, detail="User not found")
        return Response(UserSCIMSerializer.to_scim(user))

    def put(self, request, scim_id):
        user = self._get_user(scim_id)
        if not user:
            return scim_error(404, detail="User not found")

        fields = UserSCIMSerializer.from_scim(request.data)
        before = {k: getattr(user, k) for k in fields}
        for k, v in fields.items():
            setattr(user, k, v)
        user.save()
        after = {k: getattr(user, k) for k in fields}
        log_audit(request.user, "user_updated", "User", user.pk,
                  before_state=before, after_state=after)
        return Response(UserSCIMSerializer.to_scim(user))

    def patch(self, request, scim_id):
        user = self._get_user(scim_id)
        if not user:
            return scim_error(404, detail="User not found")

        before = {"email": user.email, "name": user.name, "admin": user.admin,
                  "is_active": user.is_active}
        operations = request.data.get("Operations", [])
        for op in operations:
            op_type = op.get("op", "").lower()
            path = op.get("path", "")
            value = op.get("value")

            if op_type == "replace":
                if isinstance(value, dict):
                    fields = UserSCIMSerializer.from_scim(value)
                elif path:
                    fields = UserSCIMSerializer.from_scim({path: value})
                else:
                    fields = {}
                for k, v in fields.items():
                    setattr(user, k, v)

        user.save()
        after = {"email": user.email, "name": user.name, "admin": user.admin,
                 "is_active": user.is_active}
        if before != after:
            log_audit(request.user, "user_updated", "User", user.pk,
                      before_state=before, after_state=after)
        return Response(UserSCIMSerializer.to_scim(user))

    def delete(self, request, scim_id):
        user = self._get_user(scim_id)
        if not user:
            return scim_error(404, detail="User not found")

        log_audit(request.user, "user_deactivated", "User", user.pk, before_state={
            "email": user.email, "is_active": True,
        })
        # Deactivate rather than delete (preserves audit trail)
        user.is_active = False
        user.save(update_fields=["is_active"])
        return Response(status=204)


# =============================================================================
# Group CRUD
# =============================================================================


class GroupListView(SCIMBaseView):
    """GET /auth/scim/v2/Groups — List groups
    POST /auth/scim/v2/Groups — Create group
    """

    GROUP_ATTR_MAP = {
        "displayName": "name",
    }

    def get(self, request):
        qs = Group.objects.all()

        filter_expr = request.query_params.get("filter")
        if filter_expr:
            try:
                qs = apply_scim_filter(qs, filter_expr, self.GROUP_ATTR_MAP)
            except SCIMFilterError as e:
                return scim_error(400, detail=str(e), scim_type="invalidFilter")

        paginator = SCIMPaginator(request)
        items, total = paginator.paginate_queryset(qs.order_by("pk"))
        resources = [GroupSCIMSerializer.to_scim(g) for g in items]
        return Response(paginator.get_response_data(resources, total))

    def post(self, request):
        data = request.data
        fields = GroupSCIMSerializer.from_scim(data)

        if "name" not in fields:
            return scim_error(400, detail="displayName is required", scim_type="invalidValue")

        if Group.objects.filter(name=fields["name"]).exists():
            return scim_error(409, detail="Group already exists", scim_type="uniqueness")

        group = Group.objects.create(**fields)
        group.scim_id = generate_scim_id(group.pk, "Group")
        group.save(update_fields=["scim_id"])

        log_audit(request.user, "group_created", "Group", group.pk, after_state={
            "name": group.name,
        })

        # Handle members if provided
        self._sync_members(request.user, group, data.get("members", []))

        return Response(
            GroupSCIMSerializer.to_scim(group),
            status=201,
        )

    def _sync_members(self, actor, group, members):
        """Add members to a group from SCIM member list."""
        for member in members:
            user_scim_id = member.get("value")
            if user_scim_id:
                try:
                    user = User.objects.get(scim_id=user_scim_id)
                    _, created = UserGroup.objects.get_or_create(user=user, group=group)
                    if created:
                        log_audit(actor, "member_added", "UserGroup", f"{user.pk}:{group.pk}",
                                  after_state={"user": user.email, "group": group.name})
                except User.DoesNotExist:
                    pass


class GroupDetailView(SCIMBaseView):
    """GET/PUT/PATCH/DELETE /auth/scim/v2/Groups/{scim_id}"""

    def _get_group(self, scim_id):
        try:
            return Group.objects.get(scim_id=scim_id)
        except Group.DoesNotExist:
            return None

    def get(self, request, scim_id):
        group = self._get_group(scim_id)
        if not group:
            return scim_error(404, detail="Group not found")
        return Response(GroupSCIMSerializer.to_scim(group))

    def put(self, request, scim_id):
        group = self._get_group(scim_id)
        if not group:
            return scim_error(404, detail="Group not found")

        fields = GroupSCIMSerializer.from_scim(request.data)
        before = {k: getattr(group, k) for k in fields}
        for k, v in fields.items():
            setattr(group, k, v)
        group.save()
        after = {k: getattr(group, k) for k in fields}
        if before != after:
            log_audit(request.user, "group_updated", "Group", group.pk,
                      before_state=before, after_state=after)

        # Replace members
        if "members" in request.data:
            old_members = set(
                UserGroup.objects.filter(group=group).values_list("user__email", flat=True)
            )
            UserGroup.objects.filter(group=group).delete()
            for email in old_members:
                log_audit(request.user, "member_removed", "UserGroup",
                          f"{email}:{group.pk}",
                          before_state={"user": email, "group": group.name})
            GroupListView._sync_members(None, request.user, group, request.data["members"])

        return Response(GroupSCIMSerializer.to_scim(group))

    def patch(self, request, scim_id):
        group = self._get_group(scim_id)
        if not group:
            return scim_error(404, detail="Group not found")

        operations = request.data.get("Operations", [])
        for op in operations:
            op_type = op.get("op", "").lower()
            path = op.get("path", "")
            value = op.get("value", [])

            if op_type == "add" and path == "members":
                for member in (value if isinstance(value, list) else [value]):
                    user_scim_id = member.get("value") if isinstance(member, dict) else member
                    try:
                        user = User.objects.get(scim_id=user_scim_id)
                        _, created = UserGroup.objects.get_or_create(user=user, group=group)
                        if created:
                            log_audit(request.user, "member_added", "UserGroup",
                                      f"{user.pk}:{group.pk}",
                                      after_state={"user": user.email, "group": group.name})
                    except User.DoesNotExist:
                        pass

            elif op_type == "remove" and path.startswith("members"):
                # SCIM remove: path like members[value eq "..."]
                import re
                match = re.search(r'value\s+eq\s+"([^"]+)"', path)
                if match:
                    user_scim_id = match.group(1)
                    ug = UserGroup.objects.filter(
                        group=group, user__scim_id=user_scim_id
                    ).select_related("user").first()
                    if ug:
                        log_audit(request.user, "member_removed", "UserGroup",
                                  f"{ug.user.pk}:{group.pk}",
                                  before_state={"user": ug.user.email, "group": group.name})
                        ug.delete()

            elif op_type == "replace":
                if isinstance(value, dict):
                    fields = GroupSCIMSerializer.from_scim(value)
                elif path:
                    fields = GroupSCIMSerializer.from_scim({path: value})
                else:
                    fields = {}
                for k, v in fields.items():
                    setattr(group, k, v)
                group.save()

        return Response(GroupSCIMSerializer.to_scim(group))

    def delete(self, request, scim_id):
        group = self._get_group(scim_id)
        if not group:
            return scim_error(404, detail="Group not found")
        members = list(
            UserGroup.objects.filter(group=group).values_list("user__email", flat=True)
        )
        log_audit(request.user, "group_deleted", "Group", group.pk, before_state={
            "name": group.name, "members": members,
        })
        group.delete()
        return Response(status=204)


# =============================================================================
# Dataset CRUD
# =============================================================================


class DatasetListView(SCIMBaseView):
    """GET /auth/scim/v2/Datasets — List datasets
    POST /auth/scim/v2/Datasets — Create dataset
    """

    DATASET_ATTR_MAP = {
        "name": "name",
        "tosId": "tos_id",
    }

    def get(self, request):
        qs = Dataset.objects.all()

        filter_expr = request.query_params.get("filter")
        if filter_expr:
            try:
                qs = apply_scim_filter(qs, filter_expr, self.DATASET_ATTR_MAP)
            except SCIMFilterError as e:
                return scim_error(400, detail=str(e), scim_type="invalidFilter")

        paginator = SCIMPaginator(request)
        items, total = paginator.paginate_queryset(qs.order_by("pk"))
        resources = [DatasetSCIMSerializer.to_scim(d) for d in items]
        return Response(paginator.get_response_data(resources, total))

    def post(self, request):
        data = request.data
        fields = DatasetSCIMSerializer.from_scim(data)

        if "name" not in fields:
            return scim_error(400, detail="name is required", scim_type="invalidValue")

        if Dataset.objects.filter(name=fields["name"]).exists():
            return scim_error(409, detail="Dataset already exists", scim_type="uniqueness")

        dataset = Dataset.objects.create(**fields)
        dataset.scim_id = generate_scim_id(dataset.pk, "Dataset")
        dataset.save(update_fields=["scim_id"])

        # Handle serviceTables if provided
        self._sync_service_tables(dataset, data.get("serviceTables", []))

        log_audit(request.user, "dataset_created", "Dataset", dataset.pk, after_state={
            "name": dataset.name,
        })

        return Response(
            DatasetSCIMSerializer.to_scim(dataset),
            status=201,
        )

    @staticmethod
    def _sync_service_tables(dataset, service_tables):
        """Create or update service tables for a dataset."""
        for st_data in service_tables:
            service_name = st_data.get("serviceName", "")
            table_name = st_data.get("tableName", "")
            if service_name and table_name:
                ServiceTable.objects.update_or_create(
                    service_name=service_name,
                    table_name=table_name,
                    defaults={"dataset": dataset},
                )


class DatasetDetailView(SCIMBaseView):
    """GET/PUT/PATCH/DELETE /auth/scim/v2/Datasets/{scim_id}"""

    def _get_dataset(self, scim_id):
        try:
            return Dataset.objects.get(scim_id=scim_id)
        except Dataset.DoesNotExist:
            return None

    def get(self, request, scim_id):
        dataset = self._get_dataset(scim_id)
        if not dataset:
            return scim_error(404, detail="Dataset not found")
        return Response(DatasetSCIMSerializer.to_scim(dataset))

    def put(self, request, scim_id):
        dataset = self._get_dataset(scim_id)
        if not dataset:
            return scim_error(404, detail="Dataset not found")

        fields = DatasetSCIMSerializer.from_scim(request.data)
        for k, v in fields.items():
            setattr(dataset, k, v)
        dataset.save()

        # Replace service tables
        if "serviceTables" in request.data:
            ServiceTable.objects.filter(dataset=dataset).delete()
            DatasetListView._sync_service_tables(dataset, request.data["serviceTables"])

        return Response(DatasetSCIMSerializer.to_scim(dataset))

    def patch(self, request, scim_id):
        dataset = self._get_dataset(scim_id)
        if not dataset:
            return scim_error(404, detail="Dataset not found")

        operations = request.data.get("Operations", [])
        for op in operations:
            op_type = op.get("op", "").lower()
            value = op.get("value")

            if op_type == "replace" and isinstance(value, dict):
                fields = DatasetSCIMSerializer.from_scim(value)
                for k, v in fields.items():
                    setattr(dataset, k, v)

        dataset.save()
        return Response(DatasetSCIMSerializer.to_scim(dataset))

    def delete(self, request, scim_id):
        dataset = self._get_dataset(scim_id)
        if not dataset:
            return scim_error(404, detail="Dataset not found")
        log_audit(request.user, "dataset_deleted", "Dataset", dataset.pk, before_state={
            "name": dataset.name,
        })
        dataset.delete()
        return Response(status=204)
