"""DSG-native authorization and metadata API."""

import logging

from django.conf import settings
from django.db.models import Q
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.authz import (
    ContainmentStatus,
    build_tos_url,
    evaluate_containment,
    expand_permission,
    pending_tos,
    public_version_coverage,
    resolve_dataset_reference,
)
from core.models import (
    Dataset,
    DatasetTranslation,
    DatasetVersion,
    Group,
    Grant,
    GroupDatasetPermission,
    Service,
    ServiceAccount,
    ServiceAccountGrant,
    UserGroup,
)


logger = logging.getLogger("dsg.authz")

ROLE_ORDER = ("view", "edit", "manage", "admin")


class AuthorizeView(APIView):
    """POST /api/dsg/v1/authorize — batch DSG-native authorization decision."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        entries = request.data.get("entries")
        if not isinstance(entries, list):
            return Response({"error": "entries must be a list"}, status=400)

        service_name = request.data.get("service") or None
        service = _service_for_name(service_name)
        return_url = request.data.get("return_url")

        return Response({
            "entries": [
                self._evaluate_entry(request, entry, service_name, service, return_url)
                for entry in entries
            ]
        })

    def _evaluate_entry(self, request, entry, service_name, service, return_url):
        base = _echo_entry(entry)
        if not isinstance(entry, dict):
            return _finish_decision(request.user, service_name, entry, base, "deny", "malformed-entry")

        name = entry.get("name")
        version = entry.get("version")
        branch = entry.get("branch")
        requested_permission = entry.get("permission", "view")
        if (
            not isinstance(name, str)
            or (version is not None and not isinstance(version, str))
            or (branch is not None and not isinstance(branch, str))
            or not isinstance(requested_permission, str)
            or not requested_permission
        ):
            return _finish_decision(
                request.user, service_name, entry, base, "deny", "malformed-entry"
            )

        resolved = resolve_dataset_reference(
            service, name, client_version=version, branch=branch
        )
        if not resolved.found:
            return _finish_decision(
                request.user,
                service_name,
                entry,
                base,
                "deny",
                "unknown-translation",
            )

        target = resolved.target
        principal = request.user

        if getattr(principal, "admin", False):
            return _finish_decision(
                principal,
                service_name,
                entry,
                base,
                "allow",
                "admin",
                roles=_ordered_roles({"view", "edit", "manage", "admin"}),
            )

        containment = evaluate_containment(
            principal, service, target, requested_permission
        )

        if containment.status == ContainmentStatus.COVERED:
            roles = _roles_from_rows(containment.covering_rows, principal)
            pending = pending_tos(principal, target.dataset, service_name, target)
            if pending:
                return _finish_decision(
                    principal,
                    service_name,
                    entry,
                    base,
                    "tos_required",
                    "missing-tos",
                    roles=roles,
                    tos_url=build_tos_url(
                        request, service_name, target.dataset, target, return_url, pending
                    ),
                )
            return _finish_decision(
                principal, service_name, entry, base, "allow", "covered", roles=roles
            )

        public_coverage = public_version_coverage(
            principal,
            target,
            requested_permission=requested_permission,
            service=service,
        )

        if (
            containment.status == ContainmentStatus.NOT_COVERED
            and not isinstance(principal, ServiceAccount)
            and requested_permission == "view"
            and target.dataset.access_mode == Dataset.ACCESS_PUBLIC
        ):
            pending = pending_tos(principal, target.dataset, service_name, target)
            if pending:
                return _finish_decision(
                    principal,
                    service_name,
                    entry,
                    base,
                    "tos_required",
                    "missing-tos",
                    roles=["view"],
                    tos_url=build_tos_url(
                        request, service_name, target.dataset, target, return_url, pending
                    ),
                )
            return _finish_decision(
                principal, service_name, entry, base, "allow", "public", roles=["view"]
            )

        if (
            containment.status == ContainmentStatus.NOT_COVERED
            and public_coverage.covered
        ):
            pending = pending_tos(principal, target.dataset, service_name, target)
            if pending:
                return _finish_decision(
                    principal,
                    service_name,
                    entry,
                    base,
                    "tos_required",
                    "missing-tos",
                    roles=["view"],
                    tos_url=build_tos_url(
                        request, service_name, target.dataset, target, return_url, pending
                    ),
                )
            return _finish_decision(
                principal,
                service_name,
                entry,
                base,
                "allow",
                "public-version",
                roles=["view"],
            )

        public_anchors = _anchors_from_public_versions(
            public_coverage.service_eval_versions
        )
        if containment.status == ContainmentStatus.INDETERMINATE or public_anchors:
            grant_roles = _roles_from_rows(
                containment.indeterminate_rows,
                principal,
            )
            service_eval_roles = set(grant_roles)
            if public_anchors:
                service_eval_roles.add("view")

            pending = pending_tos(principal, target.dataset, service_name, target)
            if pending:
                return _finish_decision(
                    principal,
                    service_name,
                    entry,
                    base,
                    "tos_required",
                    "missing-tos",
                    roles=_ordered_roles(service_eval_roles),
                    tos_url=build_tos_url(
                        request, service_name, target.dataset, target, return_url, pending
                    ),
                )

            anchors = (
                _anchors_from_rows(containment.indeterminate_rows, principal)
                + public_anchors
            )
            if anchors:
                roles = _ordered_roles({
                    role for anchor in anchors for role in anchor["roles"]
                })
                return _finish_decision(
                    principal,
                    service_name,
                    entry,
                    base,
                    "service_eval",
                    "service-eval",
                    roles=roles,
                    anchors=anchors,
                )

        return _finish_decision(
            principal, service_name, entry, base, "deny", "no-grant"
        )


class UserView(APIView):
    """GET /api/dsg/v1/user — authenticated principal identity."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        principal = request.user
        is_dedicated_service_account = isinstance(principal, ServiceAccount)
        groups = []
        if not is_dedicated_service_account:
            groups = list(
                UserGroup.objects.filter(user=principal)
                .select_related("group")
                .values_list("group__name", flat=True)
            )
        return Response({
            "id": principal.pk,
            "email": principal.email,
            "name": getattr(principal, "name", ""),
            "picture_url": (
                None
                if is_dedicated_service_account
                else getattr(principal, "picture_url", "")
            ),
            "admin": bool(getattr(principal, "admin", False)),
            "service_account": bool(getattr(principal, "is_service_account", False)),
            "groups": groups,
        })


class GroupMembersView(APIView):
    """GET /api/dsg/v1/groups/<name>/members — member email addresses."""

    permission_classes = [IsAuthenticated]

    def get(self, request, name):
        try:
            group = Group.objects.get(name=name)
        except Group.DoesNotExist:
            return Response({"error": "Group not found"}, status=404)
        emails = list(
            group.user_groups.select_related("user").values_list("user__email", flat=True)
        )
        return Response(emails)


class DatasetsView(APIView):
    """GET /api/dsg/v1/datasets — list datasets visible to the caller."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        service = _service_for_name(request.query_params.get("service"))
        datasets = _visible_datasets(request.user).order_by("name")
        return Response([
            {
                "name": _dataset_name_for_service(dataset, service),
                "access_mode": dataset.access_mode,
                "has_public_versions": dataset.versions.filter(is_public=True).exists(),
            }
            for dataset in datasets
        ])


class DatasetVersionsView(APIView):
    """GET /api/dsg/v1/datasets/<name>/versions — list registered anchors."""

    permission_classes = [IsAuthenticated]

    def get(self, request, name):
        service = _service_for_name(request.query_params.get("service"))
        resolved = resolve_dataset_reference(service, name)
        if not resolved.found or not _dataset_visible(request.user, resolved.target.dataset):
            return Response({"error": "Dataset not found"}, status=404)

        versions = (
            DatasetVersion.objects.filter(dataset=resolved.target.dataset)
            .order_by("branch", "ordinal", "version")
        )
        return Response([
            {
                "version": _version_name_for_service(version, service, name),
                "branch": version.branch,
                "ordinal": version.ordinal,
                "is_public": version.is_public,
            }
            for version in versions
        ])


def _service_for_name(service_name):
    if not service_name:
        return None
    return Service.objects.filter(name=service_name).first()


def _echo_entry(entry):
    if not isinstance(entry, dict):
        return {"roles": []}
    result = {}
    for key in ("name", "version", "branch"):
        if key in entry:
            result[key] = entry[key]
    result["roles"] = []
    return result


def _finish_decision(
    principal, service_name, entry, base, decision, reason, roles=None, tos_url=None, anchors=None
):
    result = dict(base)
    result["decision"] = decision
    result["roles"] = roles or []
    if tos_url:
        result["tos_url"] = tos_url
    if anchors is not None:
        result["anchors"] = anchors
    _log_decision(principal, service_name, entry, decision, reason)
    return result


def _roles_from_rows(rows, principal):
    roles = set()
    read_only = bool(getattr(principal, "read_only", False))
    for row in rows:
        roles |= expand_permission(row.permission.name, read_only=read_only)
    return _ordered_roles(roles)


def _anchors_from_rows(rows, principal):
    anchors = []
    for row in rows:
        version = getattr(row, "dataset_version", None)
        if version is None or version.ordinal is None:
            continue
        anchors.append({
            "branch": version.branch,
            "version": version.ordinal,
            "roles": _roles_from_rows([row], principal),
        })
    return anchors


def _anchors_from_public_versions(versions):
    return [
        {
            "branch": version.branch,
            "version": version.ordinal,
            "roles": ["view"],
        }
        for version in versions
        if version.ordinal is not None
    ]


def _ordered_roles(roles):
    known = [role for role in ROLE_ORDER if role in roles]
    extra = sorted(role for role in roles if role not in ROLE_ORDER)
    return known + extra


def _visible_datasets(principal):
    if isinstance(principal, ServiceAccount):
        granted_ids = ServiceAccountGrant.objects.filter(
            service_account=principal
        ).values_list("dataset_id", flat=True)
        return Dataset.objects.filter(
            Q(pk__in=granted_ids)
            | Q(access_mode=Dataset.ACCESS_PUBLIC)
            | Q(versions__is_public=True)
        ).distinct()

    if getattr(principal, "admin", False):
        return Dataset.objects.all().distinct()

    grant_ids = Grant.objects.filter(user=principal).values_list("dataset_id", flat=True)
    group_ids = GroupDatasetPermission.objects.filter(
        group__user_groups__user=principal
    ).values_list("dataset_id", flat=True)
    return Dataset.objects.filter(
        Q(pk__in=grant_ids)
        | Q(pk__in=group_ids)
        | Q(access_mode=Dataset.ACCESS_PUBLIC)
        | Q(versions__is_public=True)
    ).distinct()


def _dataset_visible(principal, dataset):
    return _visible_datasets(principal).filter(pk=dataset.pk).exists()


def _dataset_name_for_service(dataset, service):
    if service is None:
        return dataset.name
    translation = (
        DatasetTranslation.objects.filter(
            service=service, dataset=dataset, client_version__isnull=True
        )
        .order_by("client_name")
        .first()
    )
    return translation.client_name if translation else dataset.name


def _version_name_for_service(version, service, client_name):
    if service is None:
        return version.version
    translation_qs = DatasetTranslation.objects.filter(
        service=service, dataset_version=version
    )
    translation = (
        translation_qs.filter(client_name=client_name)
        .order_by("client_version")
        .first()
    )
    if translation is None:
        translation = translation_qs.order_by("client_name", "client_version").first()
    return translation.client_version if translation else version.version


def _log_decision(principal, service_name, entry, decision, reason):
    if getattr(settings, "DSG_LOG_LEVEL", "WARNING").upper() != "DEBUG":
        return
    logger.debug(
        "principal=%s service=%s entry=%r decision=%s reason=%s",
        getattr(principal, "email", str(principal)),
        service_name or "",
        entry,
        decision,
        reason,
    )
