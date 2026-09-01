"""Native DSG authorization resolution and containment helpers."""

from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlencode

from django.urls import reverse
from django.db.models import Q
from django.utils import timezone

from core.models import (
    Dataset,
    DatasetBucket,
    DatasetTranslation,
    DatasetVersion,
    Grant,
    GroupDatasetPermission,
    Service,
    ServiceAccount,
    ServiceAccountGrant,
    TOSAcceptance,
    TOSDocument,
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


class BucketAuthorizationStatus(str, Enum):
    AUTHORIZED = "authorized"
    TOS_REQUIRED = "tos_required"
    DENIED = "denied"


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


@dataclass(frozen=True)
class BucketAuthorizationDecision:
    status: BucketAuthorizationStatus
    reason: str
    dataset: Dataset | None = None
    anchor: ResolvedTarget | None = None
    pending_documents: tuple = ()

    @property
    def authorized(self):
        return self.status == BucketAuthorizationStatus.AUTHORIZED


@dataclass(frozen=True)
class PublicVersionCoverage:
    covered: bool = False
    service_eval_versions: tuple[DatasetVersion, ...] = ()


@dataclass(frozen=True)
class AnonymousAuthorizationDecision:
    decision: str = "deny"
    reason: str = "not-public"
    pending_documents: tuple = ()


def resolve_dataset_reference(service, client_name, client_version=None, branch=None):
    """Resolve a service/client dataset reference to a canonical DSG target."""
    branch = branch or "main"
    service_obj = _coerce_service(service)

    if service_obj is not None and client_version is not None:
        translation = (
            DatasetTranslation.objects.filter(
                service=service_obj,
                client_name=client_name,
                client_version=client_version,
            )
            .select_related("dataset", "dataset_version")
            .first()
        )
        if translation is not None:
            return ResolveResult(
                ResolveStatus.FOUND, _target_from_translation(translation)
            )

    if service_obj is not None:
        translation = (
            DatasetTranslation.objects.filter(
                service=service_obj,
                client_name=client_name,
                client_version__isnull=True,
            )
            .select_related("dataset", "dataset_version")
            .first()
        )
        if translation is not None:
            target = _interpret_canonical_version(
                translation.dataset, client_version, branch
            )
            if target is not None:
                return ResolveResult(ResolveStatus.FOUND, target)
            return ResolveResult(
                ResolveStatus.NOT_FOUND, reason="unknown_translation_version"
            )

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


def public_version_coverage(
    principal,
    target,
    requested_permission="view",
    service=None,
):
    """Evaluate public-version view coverage for an enabled principal.

    A public version covers itself and same-branch ancestors at or below its
    ordinal.  Without an ordinal it covers only its exact registered version.
    For DAG services, non-covering public versions with ordinals are returned
    as anchors for service-side ancestry evaluation.

    ServiceAccount principals participate like users: SAs exist to support
    curated services, so public data is visible to them without a grant and
    TOS never gates them (``pending_tos`` returns nothing for an SA).
    """
    if (
        not bool(getattr(principal, "is_enabled", False))
        or requested_permission != "view"
        or target.is_dataset_grain
    ):
        return PublicVersionCoverage()

    public_versions = tuple(
        DatasetVersion.objects.filter(
            dataset=target.dataset,
            is_public=True,
        ).order_by("pk")
    )
    if any(_public_version_covers_target(version, target) for version in public_versions):
        return PublicVersionCoverage(covered=True)

    service_obj = _coerce_service(service)
    if service_obj is None or service_obj.version_eval_mode != Service.VERSION_EVAL_DAG:
        return PublicVersionCoverage()

    return PublicVersionCoverage(
        service_eval_versions=tuple(
            version for version in public_versions if version.ordinal is not None
        )
    )


def evaluate_anonymous_authorization(
    service,
    target,
    requested_permission="view",
):
    """Evaluate public read access without constructing or accepting a principal.

    Anonymous authorization is deliberately narrower than principal-based
    authorization: it grants only exact ``view`` requests, never evaluates
    grants, and never returns DAG ``service_eval`` anchors.
    """
    if requested_permission != "view":
        return AnonymousAuthorizationDecision(reason="non-view")

    if target.dataset.access_mode == Dataset.ACCESS_PUBLIC:
        reason = "public"
    elif not target.is_dataset_grain and _public_version_covers_target_query(target):
        reason = "public-version"
    else:
        return AnonymousAuthorizationDecision()

    pending = tuple(
        _pending_tos_documents(
            target.dataset,
            service_name=getattr(service, "name", None),
            anchor=target,
            accepted_ids=set(),
        )
    )
    if pending:
        return AnonymousAuthorizationDecision(
            decision="tos_required",
            reason="missing-tos",
            pending_documents=pending,
        )
    return AnonymousAuthorizationDecision(decision="allow", reason=reason)


def evaluate_bucket_authorization(principal, bucket_name):
    """Evaluate DSG-model view access for one physical GCS bucket name.

    Bucket names are not unique in DSG.  Every matching ``DatasetBucket`` row
    contributes an authorization domain, and access is the union across those
    domains.  Within a domain, containment and TOS must both succeed at the
    same dataset/version anchor.
    """
    bucket_rows = list(
        DatasetBucket.objects.filter(name=bucket_name)
        .select_related("dataset")
        .prefetch_related("versions")
        .order_by("pk")
    )
    if not bucket_rows:
        return BucketAuthorizationDecision(
            BucketAuthorizationStatus.DENIED,
            reason="unknown_bucket",
        )

    first_tos_block = None
    for bucket_row in bucket_rows:
        for anchor in _bucket_anchors(bucket_row):
            containment = evaluate_containment(
                principal,
                service=None,
                target=anchor,
                requested_permission="view",
            )
            covering_rows = tuple(
                row
                for row in containment.covering_rows
                if _row_is_valid_for_bucket(row, anchor, bucket_row)
            )
            dataset_public_coverage = (
                bool(getattr(principal, "is_enabled", False))
                and anchor.dataset.access_mode == Dataset.ACCESS_PUBLIC
            )
            version_public_coverage = public_version_coverage(
                principal,
                anchor,
                requested_permission="view",
                service=None,
            ).covered
            if (
                not covering_rows
                and not dataset_public_coverage
                and not version_public_coverage
            ):
                continue

            pending = tuple(
                pending_tos(
                    principal,
                    anchor.dataset,
                    service_name=None,
                    anchor=anchor,
                )
            )
            if not pending:
                if covering_rows:
                    reason = "covered"
                elif dataset_public_coverage:
                    reason = "public"
                else:
                    reason = "public-version"
                return BucketAuthorizationDecision(
                    BucketAuthorizationStatus.AUTHORIZED,
                    reason=reason,
                    dataset=anchor.dataset,
                    anchor=anchor,
                )

            if first_tos_block is None:
                first_tos_block = BucketAuthorizationDecision(
                    BucketAuthorizationStatus.TOS_REQUIRED,
                    reason="missing_tos",
                    dataset=anchor.dataset,
                    anchor=anchor,
                    pending_documents=pending,
                )

    if first_tos_block is not None:
        return first_tos_block
    return BucketAuthorizationDecision(
        BucketAuthorizationStatus.DENIED,
        reason="no_coverage",
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


def pending_tos(user, dataset, service_name=None, anchor=None):
    """Return active TOS documents the principal still needs to accept.

    Dedicated ServiceAccount principals do not participate in TOS; they reach
    this function from native authorization and return early here.
    """
    if isinstance(user, ServiceAccount):
        return []

    accepted_ids = set(
        TOSAcceptance.objects.filter(user_id=user.pk).values_list(
            "tos_document_id", flat=True
        )
    )
    return _pending_tos_documents(dataset, service_name, anchor, accepted_ids)


def _pending_tos_documents(dataset, service_name, anchor, accepted_ids):
    """Return active governing TOS documents not present in ``accepted_ids``."""
    now = timezone.now()
    docs = []
    seen = set()

    def add_doc(doc):
        if doc is None or doc.pk in seen or doc.pk in accepted_ids or not doc.is_active:
            return
        seen.add(doc.pk)
        docs.append(doc)

    if dataset.tos_id:
        add_doc(dataset.tos)

    active_filter = Q(effective_date__lte=now) & (
        Q(retired_date__isnull=True) | Q(retired_date__gt=now)
    )

    if service_name:
        service_docs = (
            TOSDocument.objects.filter(
                active_filter,
                service__name=service_name,
                dataset=dataset,
                dataset_version__isnull=True,
            )
            .exclude(pk__in=accepted_ids)
            .select_related("dataset", "dataset_version", "service")
            .order_by("pk")
        )
        for doc in service_docs:
            add_doc(doc)

    anchor_version = getattr(anchor, "dataset_version", None)
    if anchor_version is not None:
        version_filter = Q(service__isnull=True)
        if service_name:
            version_filter |= Q(service__name=service_name)
        version_docs = (
            TOSDocument.objects.filter(
                active_filter,
                version_filter,
                dataset_version=anchor_version,
            )
            .exclude(pk__in=accepted_ids)
            .select_related("dataset", "dataset_version", "service")
            .order_by("pk")
        )
        for doc in version_docs:
            add_doc(doc)

    return docs


def build_tos_url(request, service_name, dataset, anchor, return_url, pending_documents=None):
    """Build an opaque absolute service-check URL for a pending TOS decision."""
    params = {"dataset": dataset.name}
    if return_url:
        params["next"] = return_url
    if service_name:
        params["service"] = service_name

    docs = list(pending_documents or [])
    has_version_tos = any(doc.dataset_version_id for doc in docs)
    if not docs and getattr(anchor, "dataset_version", None) is not None:
        now = timezone.now()
        version_filter = Q(service__isnull=True)
        if service_name:
            version_filter |= Q(service__name=service_name)
        has_version_tos = TOSDocument.objects.filter(
            Q(effective_date__lte=now)
            & (Q(retired_date__isnull=True) | Q(retired_date__gt=now)),
            version_filter,
            dataset_version=anchor.dataset_version,
        ).exists()

    if has_version_tos and getattr(anchor, "dataset_version", None) is not None:
        params["version"] = anchor.dataset_version.version

    path = reverse("web-tos-service-check")
    return request.build_absolute_uri(f"{path}?{urlencode(params)}")


def user_is_authorized_for_dataset(user, dataset):
    """Return whether a web user has direct or group-derived dataset access."""
    if user.admin:
        return True
    if Grant.objects.filter(user=user, dataset=dataset).exists():
        return True
    group_ids = UserGroup.objects.filter(user=user).values_list("group_id", flat=True)
    return GroupDatasetPermission.objects.filter(
        group_id__in=group_ids,
        dataset=dataset,
    ).exists()


def _coerce_service(service):
    if service is None or isinstance(service, Service):
        return service
    return Service.objects.filter(name=service).first()


def _target_from_translation(translation):
    if translation.dataset_version_id:
        return _target_from_dataset_version(translation.dataset_version)
    return ResolvedTarget(dataset=translation.dataset)


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
        .prefetch_related("buckets")
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


def _public_version_covers_target(public_version, target):
    if public_version.ordinal is None:
        return (
            target.dataset_version is not None
            and target.dataset_version.pk == public_version.pk
        )

    return (
        target.ordinal is not None
        and target.branch == public_version.branch
        and target.ordinal <= public_version.ordinal
    )


def _public_version_covers_target_query(target):
    return any(
        _public_version_covers_target(version, target)
        for version in DatasetVersion.objects.filter(
            dataset=target.dataset,
            is_public=True,
        ).order_by("pk")
    )


def _bucket_anchors(bucket_row):
    versions = sorted(
        (
            version
            for version in bucket_row.versions.all()
            if version.dataset_id == bucket_row.dataset_id
        ),
        key=lambda version: version.pk,
    )
    if not versions:
        return (ResolvedTarget(dataset=bucket_row.dataset),)
    return tuple(_target_from_dataset_version(version) for version in versions)


def _row_is_valid_for_bucket(row, target, bucket_row):
    """Apply bucket constraints and reject representable cross-dataset edges."""
    if getattr(row, "dataset_id", None) != target.dataset.pk:
        return False

    row_version = getattr(row, "dataset_version", None)
    if row_version is not None and row_version.dataset_id != target.dataset.pk:
        return False

    if not isinstance(row, Grant):
        return True

    constrained_buckets = list(row.buckets.all())
    if not constrained_buckets:
        return True
    return any(
        constrained.pk == bucket_row.pk
        and constrained.dataset_id == target.dataset.pk
        for constrained in constrained_buckets
    )


def _row_is_cross_branch_indeterminate(row, target, service):
    if target.is_dataset_grain:
        return False
    if service is None or service.version_eval_mode != Service.VERSION_EVAL_DAG:
        return False
    row_version = getattr(row, "dataset_version", None)
    return row_version is not None and row_version.branch != target.branch
