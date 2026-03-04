"""Centralized GCS bucket IAM provisioning and deprovisioning.

Provides a single source of truth for whether a user should have bucket-level
IAM access to a dataset's GCS buckets, and syncs that state.

Access rule:
    should_provision = has_permission(grant OR group_perm) AND tos_satisfied(no TOS OR accepted)

Global admins are skipped — they access buckets via service-account auth tokens,
not per-user bucket IAM.
"""

import logging

logger = logging.getLogger(__name__)


def sync_user_dataset_iam(user, dataset):
    """Sync a user's bucket IAM for all versions of a dataset.

    Best-effort: logs errors but does not raise.
    """
    from ngauth.gcs import add_user_to_bucket, remove_user_from_bucket

    buckets = _get_dataset_buckets(dataset)
    if not buckets:
        return

    should_provision = _user_has_effective_access(user, dataset)

    for bucket in buckets:
        try:
            if should_provision:
                add_user_to_bucket(bucket, user.email)
            else:
                remove_user_from_bucket(bucket, user.email)
        except Exception:
            logger.exception(
                "IAM sync failed",
                extra={"email": user.email, "bucket": bucket, "provision": should_provision},
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


def _user_has_effective_access(user, dataset):
    """Return True if user should be provisioned on the dataset's buckets.

    Returns False for global admins (they don't need per-user bucket IAM).
    """
    if user.admin:
        return False

    from core.models import Grant, GroupDatasetPermission, TOSAcceptance, UserGroup

    # Check direct grants
    has_grant = Grant.objects.filter(user=user, dataset=dataset).exists()

    # Check group-based permissions
    has_group_perm = False
    if not has_grant:
        user_group_ids = UserGroup.objects.filter(user=user).values_list("group_id", flat=True)
        has_group_perm = GroupDatasetPermission.objects.filter(
            group_id__in=user_group_ids, dataset=dataset
        ).exists()

    if not has_grant and not has_group_perm:
        return False

    # Check TOS requirement
    tos_doc = dataset.tos
    if not tos_doc:
        return True

    # Check if user (or parent for service accounts) accepted TOS
    check_user = user.parent if user.is_service_account else user
    return TOSAcceptance.objects.filter(user=check_user, tos_document=tos_doc).exists()


def _get_dataset_buckets(dataset):
    """Return list of non-empty gcs_bucket values from dataset versions."""
    from core.models import DatasetVersion

    return list(
        DatasetVersion.objects.filter(dataset=dataset)
        .exclude(gcs_bucket="")
        .values_list("gcs_bucket", flat=True)
    )
