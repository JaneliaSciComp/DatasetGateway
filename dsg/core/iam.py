"""Centralized GCS bucket IAM provisioning and deprovisioning.

Provides a single source of truth for whether a user should have bucket-level
IAM access to a dataset's GCS buckets, and syncs that state.

Access rule:
    provisioned_buckets = eligible principal
                          AND qualifying grants/group permissions
                          AND dataset/version TOS satisfied

Global admins are skipped — they access buckets via service-account auth tokens,
not per-user bucket IAM.
"""

import logging

from django.db.models import Q
from django.utils import timezone

from core.authz import VIEW_PERMISSIONS

logger = logging.getLogger(__name__)


def provision_binding(bucket_name, email):
    """Provision a DSG-owned bucket IAM binding and record provenance."""
    from core.models import BucketIAMBinding
    from ngauth.gcs import add_user_to_bucket

    row_exists = BucketIAMBinding.objects.filter(
        bucket_name=bucket_name, email=email,
    ).exists()
    try:
        outcome = add_user_to_bucket(bucket_name, email)
    except Exception:
        logger.exception(
            "IAM provision failed",
            extra={"email": email, "bucket": bucket_name},
        )
        return "failed"

    if row_exists:
        if outcome == "already_present":
            return "already-owned"
        if outcome == "created":
            return "reasserted"
        logger.error(
            "IAM provision failed",
            extra={"email": email, "bucket": bucket_name, "outcome": outcome},
        )
        return "failed"

    if outcome == "created":
        try:
            BucketIAMBinding.objects.get_or_create(
                bucket_name=bucket_name, email=email,
            )
        except Exception:
            logger.exception(
                "Bucket IAM binding created but ledger write failed",
                extra={"email": email, "bucket": bucket_name},
            )
            return "failed"
        return "added"

    if outcome == "already_present":
        return "foreign"

    logger.error(
        "IAM provision failed",
        extra={"email": email, "bucket": bucket_name, "outcome": outcome},
    )
    return "failed"


def deprovision_binding(bucket_name, email):
    """Remove a bucket IAM binding only when DSG owns it in the ledger."""
    from core.models import BucketIAMBinding
    from ngauth.gcs import remove_user_from_bucket

    row = BucketIAMBinding.objects.filter(bucket_name=bucket_name, email=email).first()
    if row is None:
        return "skipped-not-owned"

    try:
        success = remove_user_from_bucket(bucket_name, email)
    except Exception:
        logger.exception(
            "IAM deprovision failed",
            extra={"email": email, "bucket": bucket_name},
        )
        return "failed"

    if not success:
        logger.error(
            "IAM deprovision failed",
            extra={"email": email, "bucket": bucket_name},
        )
        return "failed"

    try:
        row.delete()
    except Exception:
        logger.exception(
            "Bucket IAM binding removed but ledger delete failed",
            extra={"email": email, "bucket": bucket_name},
        )
        return "failed"
    return "removed"


def sync_user_dataset_iam(user, dataset):
    """Sync a user's bucket IAM for all versions of a dataset.

    Best-effort: logs errors but does not raise.
    """
    buckets = _get_dataset_buckets(dataset)
    if not buckets:
        return

    provisioned = provisioned_buckets(user, dataset)

    for bucket in buckets:
        should_provision = bucket in provisioned
        try:
            if should_provision:
                result = provision_binding(bucket, user.email)
            else:
                result = deprovision_binding(bucket, user.email)
            if result == "failed":
                logger.error(
                    "IAM sync failed",
                    extra={
                        "email": user.email,
                        "bucket": bucket,
                        "provision": should_provision,
                    },
                )
        except Exception:
            logger.exception(
                "IAM sync failed",
                extra={"email": user.email, "bucket": bucket, "provision": should_provision},
            )


def permission_source_users(dataset):
    """Return users holding any permission source on the dataset.

    A permission source is a direct Grant or membership in a group with a
    GroupDatasetPermission. Deliberately a superset of the currently-effective
    users — no TOS gate, no admin exclusion — because deprovisioning must reach
    users whose IAM is stale precisely *because* they are no longer effective;
    the per-user rule in sync_user_dataset_iam decides add vs. remove.
    """
    from core.models import Grant, GroupDatasetPermission, User, UserGroup

    user_ids = set(
        Grant.objects.filter(dataset=dataset).values_list("user_id", flat=True)
    )
    group_ids = GroupDatasetPermission.objects.filter(
        dataset=dataset
    ).values_list("group_id", flat=True)
    user_ids |= set(
        UserGroup.objects.filter(
            group_id__in=group_ids
        ).values_list("user_id", flat=True)
    )
    return User.objects.filter(pk__in=user_ids)


def sync_dataset_iam(dataset, users=None):
    """Sync bucket IAM for every permission-source user of a dataset.

    Pass ``users`` (captured via permission_source_users *before* a mutation
    that shrinks the permission graph, e.g. deleting a GroupDatasetPermission)
    so users who just lost their permission source are still deprovisioned.
    """
    if users is None:
        users = permission_source_users(dataset)
    for user in users:
        sync_user_dataset_iam(user, dataset)


def permission_source_datasets(user):
    """Return datasets where the user holds any permission source.

    Transpose of permission_source_users: a direct Grant or a membership in a
    group with a GroupDatasetPermission. Deliberately ungated by the effective
    rule for the same reason — deprovisioning must reach datasets where the
    user's IAM is stale. Enumerated from DSG's own tables only; live bucket
    policy is never read (bucket membership is a superset of DSG state).
    """
    from core.models import Dataset, Grant, GroupDatasetPermission, UserGroup

    dataset_ids = set(
        Grant.objects.filter(user=user).values_list("dataset_id", flat=True)
    )
    group_ids = UserGroup.objects.filter(user=user).values_list("group_id", flat=True)
    dataset_ids |= set(
        GroupDatasetPermission.objects.filter(
            group_id__in=group_ids
        ).values_list("dataset_id", flat=True)
    )
    return Dataset.objects.filter(pk__in=dataset_ids)


def sync_user_iam(user):
    """Sync bucket IAM for every permission-source dataset of a user.

    Use when a user-level flag (is_active, admin) flips the rule's outcome
    across all their datasets at once.
    """
    for dataset in permission_source_datasets(user):
        sync_user_dataset_iam(user, dataset)


def deprovision_bucket(bucket_name, dataset):
    """Best-effort removal of DSG-owned bindings from a bucket.

    Used when a bucket row is deleted, renamed, or moved to another dataset:
    ``bucket_name`` is the *old* name and ``dataset`` the *old* dataset,
    captured before the mutation.
    """
    from core.models import BucketIAMBinding

    rows = list(
        BucketIAMBinding.objects.filter(bucket_name=bucket_name).values_list(
            "email", flat=True,
        )
    )
    for email in rows:
        try:
            deprovision_binding(bucket_name, email)
        except Exception:
            logger.exception(
                "IAM deprovision failed",
                extra={"email": email, "bucket": bucket_name},
            )
    logger.info(
        "Deprovisioned bucket %s for %d ledger-owned binding(s) of dataset %s",
        bucket_name, len(rows), dataset,
    )


def sync_group_datasets_for_user(user, group):
    """Sync IAM for a user on all datasets the group has GroupDatasetPermission on."""
    from core.models import GroupDatasetPermission

    dataset_ids = GroupDatasetPermission.objects.filter(
        group=group
    ).values_list("dataset_id", flat=True).distinct()

    if not dataset_ids:
        return

    from core.models import Dataset

    for dataset in Dataset.objects.filter(pk__in=dataset_ids):
        sync_user_dataset_iam(user, dataset)


def provisioned_buckets(user, dataset):
    """Return bucket names the user should hold for a dataset."""
    if user.admin or not user.is_enabled:
        return set()
    if not _dataset_tos_accepted(user, dataset):
        return set()

    from core.models import Grant, GroupDatasetPermission, UserGroup

    bucket_names = set()
    grants = (
        Grant.objects.filter(user=user, dataset=dataset)
        .select_related("permission", "dataset_version", "service")
        .prefetch_related("buckets")
    )
    for grant in grants:
        bucket_names |= _grant_bucket_contribution(grant, dataset)

    group_ids = UserGroup.objects.filter(user=user).values_list("group_id", flat=True)
    group_permissions = GroupDatasetPermission.objects.filter(
        group_id__in=group_ids, dataset=dataset
    ).select_related("permission", "service")
    for group_permission in group_permissions:
        bucket_names |= _group_permission_bucket_contribution(group_permission, dataset)

    return bucket_names - _version_tos_blocked_bucket_names(user, dataset)


def _user_has_effective_access(user, dataset):
    """Return True if user should be provisioned on any dataset bucket.

    Private compatibility helper for older callers/tests; the authoritative
    provisioning unit is now ``(user, bucket)`` via ``provisioned_buckets``.
    """
    return bool(provisioned_buckets(user, dataset))


def _get_dataset_buckets(dataset):
    """Return list of bucket names for a dataset."""
    from core.models import DatasetBucket

    return list(
        DatasetBucket.objects.filter(dataset=dataset).values_list("name", flat=True)
    )


def _grant_bucket_contribution(grant, dataset):
    explicit = list(grant.buckets.all())
    if explicit:
        return {bucket.name for bucket in explicit}
    return _qualifying_grant_reach(grant, dataset)


def _group_permission_bucket_contribution(group_permission, dataset):
    return _qualifying_grant_reach(group_permission, dataset)


def _qualifying_grant_reach(row, dataset):
    if row.service_id is not None or row.permission.name not in VIEW_PERMISSIONS:
        return set()

    row_version = getattr(row, "dataset_version", None)
    if row_version is None:
        return set(_get_dataset_buckets(dataset))

    if row_version.ordinal is None:
        return set(row_version.buckets.values_list("name", flat=True))

    from core.models import DatasetVersion

    return set(
        DatasetVersion.objects.filter(
            dataset=dataset,
            branch=row_version.branch,
            ordinal__isnull=False,
            ordinal__lte=row_version.ordinal,
        )
        .filter(buckets__isnull=False)
        .values_list("buckets__name", flat=True)
    )


def _dataset_tos_accepted(user, dataset):
    if not dataset.tos_id:
        return True
    from core.models import TOSAcceptance

    check_user = _tos_check_user(user)
    return TOSAcceptance.objects.filter(user=check_user, tos_document=dataset.tos).exists()


def _version_tos_blocked_bucket_names(user, dataset):
    blocked_version_ids = _blocked_version_tos_ids(user, dataset)
    if not blocked_version_ids:
        return set()

    from core.models import DatasetBucket

    blocked_bucket_names = set()
    buckets = DatasetBucket.objects.filter(dataset=dataset).prefetch_related("versions")
    for bucket in buckets:
        version_ids = {version.pk for version in bucket.versions.all()}
        if version_ids and version_ids <= blocked_version_ids:
            blocked_bucket_names.add(bucket.name)
    return blocked_bucket_names


def _blocked_version_tos_ids(user, dataset):
    from core.models import TOSAcceptance, TOSDocument

    check_user = _tos_check_user(user)
    accepted_ids = TOSAcceptance.objects.filter(user=check_user).values_list(
        "tos_document_id", flat=True
    )
    now = timezone.now()
    blocked = TOSDocument.objects.filter(
        dataset_version__dataset=dataset,
        dataset_version__isnull=False,
        service__isnull=True,
        effective_date__lte=now,
    ).filter(Q(retired_date__isnull=True) | Q(retired_date__gt=now))
    blocked = blocked.exclude(pk__in=accepted_ids)
    return set(blocked.values_list("dataset_version_id", flat=True))


def _tos_check_user(user):
    if getattr(user, "parent_id", None) is not None:
        return user.parent
    return user
