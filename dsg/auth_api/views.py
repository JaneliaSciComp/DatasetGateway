"""DatasetGateway authorization API views."""

from django.db.models import Q
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import (
    Dataset,
    DatasetVersion,
    Grant,
    GroupDatasetPermission,
    Permission,
    TOSAcceptance,
    TOSDocument,
)


class WhoAmIView(APIView):
    """GET /api/v1/whoami — User identity + roles."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        cache = getattr(request, "permission_cache", None)

        return Response({
            "id": user.pk,
            "email": user.email,
            "name": user.public_name,
            "admin": user.admin,
            "is_active": user.is_active,
            "groups": cache["groups"] if cache else [],
            "datasets_admin": cache["datasets_admin"] if cache else [],
        })


class DatasetsListView(APIView):
    """GET /api/v1/datasets — List accessible datasets."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        if user.admin:
            datasets = Dataset.objects.all()
        else:
            # Datasets where user has group permissions or direct grants
            group_dataset_ids = GroupDatasetPermission.objects.filter(
                group__user_groups__user=user
            ).values_list("dataset_id", flat=True)

            grant_dataset_ids = Grant.objects.filter(user=user).values_list(
                "dataset_id", flat=True
            )

            admin_dataset_ids = Grant.objects.filter(
                user=user, permission__name="admin"
            ).values_list("dataset_id", flat=True)

            datasets = Dataset.objects.filter(
                Q(pk__in=group_dataset_ids)
                | Q(pk__in=grant_dataset_ids)
                | Q(pk__in=admin_dataset_ids)
            ).distinct()

        return Response([
            {
                "id": d.pk,
                "name": d.name,
                "description": d.description,
                "has_tos": d.tos_id is not None,
            }
            for d in datasets
        ])


class DatasetVersionsView(APIView):
    """GET /api/v1/datasets/<slug>/versions — List accessible versions."""

    permission_classes = [IsAuthenticated]

    def get(self, request, slug):
        try:
            dataset = Dataset.objects.get(name=slug)
        except Dataset.DoesNotExist:
            return Response({"error": "Dataset not found"}, status=404)

        versions = DatasetVersion.objects.filter(dataset=dataset).prefetch_related("buckets")

        return Response([
            {
                "id": v.pk,
                "version": v.version,
                "buckets": [b.name for b in v.buckets.all()],
                "is_public": v.is_public,
            }
            for v in versions
        ])


class AuthorizeDecisionView(APIView):
    """POST /api/v1/check-access — Evaluate access (returns allow/deny + reason).

    Request body:
    {
        "dataset": "fish2",
        "version": "v1",   // optional
        "permission": "view",
        "service": "celltyping"  // optional — checks service-specific TOS
    }
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        dataset_name = request.data.get("dataset")
        version = request.data.get("version")
        permission_name = request.data.get("permission", "view")
        service_name = request.data.get("service")

        if not dataset_name:
            return Response(
                {"allowed": False, "reason": "dataset is required"}, status=400
            )

        try:
            dataset = Dataset.objects.get(name=dataset_name)
        except Dataset.DoesNotExist:
            return Response(
                {"allowed": False, "reason": "Dataset not found"}, status=404
            )

        # Admins always have access
        if user.admin:
            return Response({"allowed": True, "reason": "admin"})

        # Dataset admins have access (users with admin grant)
        if Grant.objects.filter(user=user, dataset=dataset, permission__name="admin").exists():
            return Response({"allowed": True, "reason": "dataset_admin"})

        # Check general TOS requirement
        tos_user_id = user.parent_id if user.is_service_account else user.pk
        if dataset.tos_id:
            if not TOSAcceptance.objects.filter(
                user_id=tos_user_id, tos_document_id=dataset.tos_id
            ).exists():
                return Response({
                    "allowed": False,
                    "reason": "tos_required",
                    "tos_id": dataset.tos_id,
                    "tos_name": dataset.tos.name if dataset.tos else "",
                })

        # Check service-specific TOS requirement
        if service_name:
            now = timezone.now()
            unaccepted_service_tos = (
                TOSDocument.objects.filter(
                    service__name=service_name,
                    dataset=dataset,
                    effective_date__lte=now,
                )
                .filter(Q(retired_date__isnull=True) | Q(retired_date__gt=now))
                .exclude(
                    pk__in=TOSAcceptance.objects.filter(
                        user_id=tos_user_id,
                    ).values_list("tos_document_id", flat=True)
                )
                .first()
            )
            if unaccepted_service_tos:
                return Response({
                    "allowed": False,
                    "reason": "tos_required",
                    "tos_id": unaccepted_service_tos.pk,
                    "tos_name": unaccepted_service_tos.name,
                    "service": service_name,
                })

        # Check group-based permissions
        has_group_perm = GroupDatasetPermission.objects.filter(
            group__user_groups__user=user,
            dataset=dataset,
            permission__name=permission_name,
        ).exists()

        if has_group_perm:
            return Response({"allowed": True, "reason": "group_permission"})

        # Check direct grants
        grant_filter = {"user": user, "dataset": dataset, "permission__name": permission_name}
        if version:
            try:
                dv = DatasetVersion.objects.get(dataset=dataset, version=version)
                # Match version-specific or dataset-wide grants (version=None)
                has_grant = Grant.objects.filter(
                    Q(dataset_version=dv) | Q(dataset_version__isnull=True),
                    **grant_filter,
                ).exists()
            except DatasetVersion.DoesNotExist:
                has_grant = False
        else:
            has_grant = Grant.objects.filter(**grant_filter).exists()

        if has_grant:
            return Response({"allowed": True, "reason": "direct_grant"})

        return Response({"allowed": False, "reason": "no_permission"})
