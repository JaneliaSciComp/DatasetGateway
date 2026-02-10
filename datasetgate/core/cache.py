"""Permission cache builder — faithful port of CAVE's User.create_cache().

Produces the exact JSON structure that middle_auth_client expects.
"""

from django.db.models import Q


def build_permission_cache(user):
    """Build the permission cache dict for a user.

    Returns a dict matching CAVE's create_cache() output exactly:
    {
        "id", "parent_id", "service_account", "name", "email", "admin",
        "pi", "affiliations", "groups", "groups_admin", "permissions",
        "permissions_v2", "permissions_v2_ignore_tos", "missing_tos",
        "datasets_admin"
    }
    """
    permissions = _get_permissions(user, ignore_tos=False)
    permissions_ignore_tos = _get_permissions(user, ignore_tos=True)

    def permission_to_level(p):
        return {"none": 0, "view": 1, "edit": 2}.get(p, 0)

    groups_list = _get_groups(user)
    groups_admin_list = _get_groups_admin(user)

    return {
        "id": user.pk,
        "parent_id": user.parent_id,
        "service_account": user.parent_id is not None,
        "name": user.public_name,
        "email": user.email,
        "admin": user.admin,
        "pi": user.pi,
        "affiliations": [],  # Affiliations not ported yet; empty list for compat
        "groups": groups_list,
        "groups_admin": groups_admin_list,
        "permissions": {
            entry["name"]: max(permission_to_level(p) for p in entry["permissions"])
            for entry in permissions
        },
        "permissions_v2": {
            entry["name"]: entry["permissions"] for entry in permissions
        },
        "permissions_v2_ignore_tos": {
            entry["name"]: entry["permissions"] for entry in permissions_ignore_tos
        },
        "missing_tos": _datasets_missing_tos(user),
        "datasets_admin": _get_datasets_adminning(user),
    }


def _get_groups(user):
    """Return list of group names the user belongs to."""
    from .models import UserGroup

    return list(
        UserGroup.objects.filter(user=user)
        .select_related("group")
        .values_list("group__name", flat=True)
    )


def _get_groups_admin(user):
    """Return list of group names the user is admin of."""
    from .models import UserGroup

    return list(
        UserGroup.objects.filter(user=user, is_admin=True)
        .select_related("group")
        .values_list("group__name", flat=True)
    )


def _get_datasets_adminning(user):
    """Return list of dataset names the user is admin of."""
    from .models import DatasetAdmin

    return list(
        DatasetAdmin.objects.filter(user=user)
        .select_related("dataset")
        .values_list("dataset__name", flat=True)
    )


def _get_permissions(user, ignore_tos=False):
    """Port of User._get_permissions().

    Returns list of dicts: [{"id": ..., "name": ..., "permissions": [...]}, ...]
    """
    from .models import GroupDatasetPermission, TOSAcceptance

    tos_user_id = user.parent_id if user.is_service_account else user.pk

    qs = (
        GroupDatasetPermission.objects.filter(group__user_groups__user=user)
        .select_related("dataset", "permission")
    )

    if not ignore_tos:
        # Include rows where dataset has no TOS, or user has accepted the TOS
        accepted_tos_ids = TOSAcceptance.objects.filter(
            user_id=tos_user_id
        ).values_list("tos_document_id", flat=True)

        qs = qs.filter(
            Q(dataset__tos__isnull=True) | Q(dataset__tos_id__in=accepted_tos_ids)
        )

    if user.read_only:
        qs = qs.exclude(permission__name="edit")

    # Aggregate into {dataset_id: {"id": ..., "name": ..., "permissions": [...]}}
    temp = {}
    for gdp in qs:
        did = gdp.dataset_id
        if did not in temp:
            temp[did] = {
                "id": did,
                "name": gdp.dataset.name,
                "permissions": [],
            }
        pname = gdp.permission.name
        if pname not in temp[did]["permissions"]:
            temp[did]["permissions"].append(pname)

    return list(temp.values())


def _datasets_missing_tos(user):
    """Port of User.datasets_missing_tos().

    Returns list of dicts for datasets where user has permissions but
    hasn't accepted the required TOS.
    """
    from .models import GroupDatasetPermission, TOSAcceptance, TOSDocument

    tos_user_id = user.parent_id if user.is_service_account else user.pk

    # Get all datasets user has permissions on that have a TOS requirement
    datasets_with_tos = (
        GroupDatasetPermission.objects.filter(
            group__user_groups__user=user,
            dataset__tos__isnull=False,
        )
        .select_related("dataset", "dataset__tos")
        .values_list("dataset_id", "dataset__name", "dataset__tos_id", "dataset__tos__name")
        .distinct()
    )

    # Find which TOS the user has already accepted
    accepted_tos_ids = set(
        TOSAcceptance.objects.filter(user_id=tos_user_id).values_list(
            "tos_document_id", flat=True
        )
    )

    return [
        {
            "dataset_id": dataset_id,
            "dataset_name": dataset_name,
            "tos_id": tos_id,
            "tos_name": tos_name,
        }
        for dataset_id, dataset_name, tos_id, tos_name in datasets_with_tos
        if tos_id not in accepted_tos_ids
    ]
