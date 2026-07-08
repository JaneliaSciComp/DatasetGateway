"""Native DSG authorization resolution and containment helpers."""

from dataclasses import dataclass, field
from enum import Enum

from django.db.models import Q

from core.models import (
    Dataset,
    DatasetAlias,
    DatasetVersion,
    Grant,
    GroupDatasetPermission,
    Service,
    ServiceAccount,
    ServiceAccountGrant,
    UserGroup,
)


VIEW_HIERARCHY = {
    "admin": {"manage", "edit", "view"},
    "manage": {"edit", "view"},
    "edit": {"view"},
}
VIEW_PERMISSIONS = frozenset({"admin", "manage", "edit", "view"})


class ResolveStatus(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"


class ContainmentStatus(str, Enum):
    COVERED = "covered"
    NOT_COVERED = "not_covered"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class ResolvedTarget:
    dataset: Dataset
    branch: str = "main"
    ordinal: int | None = None
    dataset_version: DatasetVersion | None = None

    @property
    def is_dataset_grain(self):
        return self.dataset_version is None and self.ordinal is None


@dataclass(frozen=True)
class ResolveResult:
    status: ResolveStatus
    target: ResolvedTarget | None = None
    reason: str = ""

    @property
    def found(self):
        return self.status == ResolveStatus.FOUND


@dataclass(frozen=True)
class ContainmentDecision:
    status: ContainmentStatus
    effective_permissions: frozenset[str] = field(default_factory=frozenset)
    matching_rows: tuple = ()
    covering_rows: tuple = ()
    indeterminate_rows: tuple = ()

    @property
    def covered(self):
        return self.status == ContainmentStatus.COVERED


def resolve_dataset_reference(service, client_name, client_version=None, branch=None):
    """Resolve a service/client dataset reference to a canonical DSG target."""
    branch = branch or "main"
    service_obj = _coerce_service(service)

    if service_obj is not None and client_version is not None:
        alias = (
            DatasetAlias.objects.filter(
                service=service_obj,
                client_name=client_name,
                client_version=client_version,
            )
            .select_related("dataset", "dataset_version")
            .first()
        )
        if alias is not None:
            return ResolveResult(ResolveStatus.FOUND, _target_from_alias(alias))

    if service_obj is not None:
        alias = (
            DatasetAlias.objects.filter(
                service=service_obj,
                client_name=client_name,
                client_version__isnull=True,
            )
            .select_related("dataset", "dataset_version")
            .first()
        )
        if alias is not None:
            target = _interpret_canonical_version(alias.dataset, client_version, branch)
            if target is not None:
                return ResolveResult(ResolveStatus.FOUND, target)
            return ResolveResult(ResolveStatus.NOT_FOUND, reason="unknown_alias_version")

    dataset = Dataset.objects.filter(name=client_name).first()
    if dataset is None:
        return ResolveResult(ResolveStatus.NOT_FOUND, reason="unknown_dataset")

    target = _interpret_canonical_version(dataset, client_version, branch)
    if target is None:
        return ResolveResult(ResolveStatus.NOT_FOUND, reason="unknown_version")
    return ResolveResult(ResolveStatus.FOUND, target)


def evaluate_containment(principal, service, target, requested_permission="view"):
    """Evaluate whether a principal has a permission covering the target."""
    service_obj = _coerce_service(service)
    read_only = bool(getattr(principal, "read_only", False))
    effective_permissions = set()
    matching_rows = []
    covering_rows = []
    indeterminate_rows = []

    for row in _candidate_rows(principal, target.dataset, service_obj):
        expanded = expand_permission(row.permission.name, read_only=read_only)
        effective_permissions |= expanded
        if requested_permission not in expanded:
            continue
        matching_rows.append(row)

        if _row_covers_target(row, target):
            covering_rows.append(row)
            continue
        if _row_is_cross_branch_indeterminate(row, target, service_obj):
            indeterminate_rows.append(row)

    if covering_rows:
        status = ContainmentStatus.COVERED
    elif indeterminate_rows:
        status = ContainmentStatus.INDETERMINATE
    else:
        status = ContainmentStatus.NOT_COVERED

    return ContainmentDecision(
        status=status,
        effective_permissions=frozenset(effective_permissions),
        matching_rows=tuple(matching_rows),
        covering_rows=tuple(covering_rows),
        indeterminate_rows=tuple(indeterminate_rows),
    )


def expand_permission(permission_name, read_only=False):
    """Return a permission's exact or downward-expanded effective role set."""
    if permission_name in VIEW_PERMISSIONS:
        permissions = {permission_name} | VIEW_HIERARCHY.get(permission_name, set())
    else:
        permissions = {permission_name}
    if read_only:
        permissions.discard("edit")
    return permissions


def _coerce_service(service):
    if service is None or isinstance(service, Service):
        return service
    return Service.objects.filter(name=service).first()


def _target_from_alias(alias):
    if alias.dataset_version_id:
        return _target_from_dataset_version(alias.dataset_version)
    return ResolvedTarget(dataset=alias.dataset)


def _target_from_dataset_version(dataset_version):
    return ResolvedTarget(
        dataset=dataset_version.dataset,
        branch=dataset_version.branch,
        ordinal=dataset_version.ordinal,
        dataset_version=dataset_version,
    )


def _interpret_canonical_version(dataset, client_version, branch):
    if client_version is None:
        return ResolvedTarget(dataset=dataset)

    dataset_version = DatasetVersion.objects.filter(
        dataset=dataset, version=client_version
    ).first()
    if dataset_version is not None:
        return _target_from_dataset_version(dataset_version)

    if client_version.isdigit():
        return ResolvedTarget(dataset=dataset, branch=branch, ordinal=int(client_version))

    return None


def _candidate_rows(principal, dataset, service):
    service_filter = Q(service__isnull=True)
    if service is not None:
        service_filter |= Q(service=service)

    if isinstance(principal, ServiceAccount):
        return list(
            ServiceAccountGrant.objects.filter(service_filter, service_account=principal, dataset=dataset)
            .select_related("permission", "dataset_version", "service")
        )

    group_ids = UserGroup.objects.filter(user=principal).values_list("group_id", flat=True)
    grants = list(
        Grant.objects.filter(service_filter, user=principal, dataset=dataset)
        .select_related("permission", "dataset_version", "service")
    )
    group_permissions = list(
        GroupDatasetPermission.objects.filter(
            service_filter,
            group_id__in=group_ids,
            dataset=dataset,
        ).select_related("permission", "service")
    )
    return grants + group_permissions


def _row_covers_target(row, target):
    row_version = getattr(row, "dataset_version", None)
    if target.is_dataset_grain:
        return row_version is None

    if row_version is None:
        return True

    if row_version.ordinal is None:
        return (
            target.dataset_version is not None
            and target.dataset_version.pk == row_version.pk
        )

    return target.ordinal is not None and target.branch == row_version.branch and target.ordinal <= row_version.ordinal


def _row_is_cross_branch_indeterminate(row, target, service):
    if target.is_dataset_grain:
        return False
    if service is None or service.version_eval_mode != Service.VERSION_EVAL_DAG:
        return False
    row_version = getattr(row, "dataset_version", None)
    return row_version is not None and row_version.branch != target.branch
