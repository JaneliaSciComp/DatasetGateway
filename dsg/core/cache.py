"""Permission cache builder — faithful port of CAVE's User.create_cache().

Produces the exact JSON structure that middle_auth_client expects.
"""

from django.db.models import Q
from django.utils import timezone


def build_permission_cache(user, service=None):
    """Build the permission cache dict for a user.

    Parameters
    ----------
    user : User
    service : str or None
        If given, the service slug (e.g. "celltyping"). Service-specific TOS
        requirements are enforced in addition to general dataset TOS.

    Returns a dict matching CAVE's create_cache() output exactly:
    {
        "id", "parent_id", "service_account", "name", "email", "admin",
        "pi", "affiliations", "groups", "groups_admin", "permissions",
        "permissions_v2", "permissions_v2_ignore_tos", "missing_tos",
        "datasets_admin"
    }
    """
    permissions = _get_permissions(user, ignore_tos=False, service=service)
    permissions_ignore_tos = _get_permissions(user, ignore_tos=True)

    def permission_to_level(p):
        return {"none": 0, "view": 1, "edit": 2, "manage": 3, "admin": 4}.get(p, 0)

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
        "picture_url": user.picture_url,
        "affiliations": list(user.affiliations.values_list("name", flat=True)),
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
        "missing_tos": _datasets_missing_tos(user, service=service),
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
    from .models import Grant

    return list(
        Grant.objects.filter(user=user, permission__name="admin")
        .select_related("dataset")
        .values_list("dataset__name", flat=True)
    )


def _get_permissions(user, ignore_tos=False, service=None):
    """Port of User._get_permissions().

    Returns list of dicts: [{"id": ..., "name": ..., "permissions": [...]}, ...]
    Merges both group-based permissions (GroupDatasetPermission) and
    direct user grants (Grant).

    When *service* is given and *ignore_tos* is False, datasets that have an
    active service-specific TOS the user hasn't accepted are also excluded.
    """
    from .models import Grant, GroupDatasetPermission, TOSAcceptance, TOSDocument

    tos_user_id = user.parent_id if user.is_service_account else user.pk

    # --- Group-based permissions (existing) ---
    group_qs = (
        GroupDatasetPermission.objects.filter(group__user_groups__user=user)
        .select_related("dataset", "permission")
    )

    # --- Direct grants ---
    grant_qs = (
        Grant.objects.filter(user=user)
        .select_related("dataset", "permission")
    )

    accepted_tos_ids = None
    if not ignore_tos:
        # Include rows where dataset has no TOS, or user has accepted the TOS
        accepted_tos_ids = set(
            TOSAcceptance.objects.filter(
                user_id=tos_user_id
            ).values_list("tos_document_id", flat=True)
        )

        tos_filter = Q(dataset__tos__isnull=True) | Q(dataset__tos_id__in=accepted_tos_ids)
        group_qs = group_qs.filter(tos_filter)
        grant_qs = grant_qs.filter(tos_filter)

    # Aggregate into {dataset_id: {"id": ..., "name": ..., "permissions": [...]}}
    temp = {}
    for gdp in group_qs:
        did = gdp.dataset_id
        if did not in temp:
            temp[did] = {
                "id": did,
                "name": gdp.dataset.name,
                "permissions": set(),
            }
        temp[did]["permissions"].add(gdp.permission.name)

    for grant in grant_qs:
        did = grant.dataset_id
        if did not in temp:
            temp[did] = {
                "id": did,
                "name": grant.dataset.name,
                "permissions": set(),
            }
        temp[did]["permissions"].add(grant.permission.name)

    # Service-specific TOS: additionally exclude datasets with unaccepted
    # active TOS documents scoped to the requested service.
    if service and not ignore_tos and temp:
        now = timezone.now()
        service_tos_dataset_ids = set(
            TOSDocument.objects.filter(
                service__name=service,
                dataset_id__in=temp.keys(),
                effective_date__lte=now,
            )
            .filter(Q(retired_date__isnull=True) | Q(retired_date__gt=now))
            .exclude(pk__in=accepted_tos_ids)
            .values_list("dataset_id", flat=True)
        )
        if service_tos_dataset_ids:
            temp = {did: entry for did, entry in temp.items()
                    if did not in service_tos_dataset_ids}

    # Expand permission hierarchy: each level implies all levels below
    hierarchy = {"admin": {"manage", "edit", "view"}, "manage": {"edit", "view"}, "edit": {"view"}}
    for entry in temp.values():
        expanded = set(entry["permissions"])
        for p in list(entry["permissions"]):
            expanded |= hierarchy.get(p, set())
        # For read_only users, strip edit but keep manage/admin/view
        if user.read_only:
            expanded.discard("edit")
        entry["permissions"] = sorted(expanded)

    return list(temp.values())


def _datasets_missing_tos(user, service=None):
    """Port of User.datasets_missing_tos().

    Returns list of dicts for datasets where user has permissions (via
    groups or direct grants) — or any public dataset — but hasn't accepted
    the required TOS. Public datasets are included even without a grant so
    clients know to redirect users to DSG to accept the TOS.

    When *service* is given, also includes service-specific TOS documents
    the user hasn't accepted.
    """
    from .models import Dataset, Grant, GroupDatasetPermission, TOSAcceptance, TOSDocument

    tos_user_id = user.parent_id if user.is_service_account else user.pk

    # Get datasets from group-based permissions that have a TOS requirement
    group_datasets = set(
        GroupDatasetPermission.objects.filter(
            group__user_groups__user=user,
            dataset__tos__isnull=False,
        )
        .values_list("dataset_id", "dataset__name", "dataset__tos_id", "dataset__tos__name")
        .distinct()
    )

    # Get datasets from direct grants that have a TOS requirement
    grant_datasets = set(
        Grant.objects.filter(
            user=user,
            dataset__tos__isnull=False,
        )
        .values_list("dataset_id", "dataset__name", "dataset__tos_id", "dataset__tos__name")
        .distinct()
    )

    # All public datasets that have a TOS — applies to every user, since
    # public datasets are self-service and a TOS may be required to access.
    public_datasets = set(
        Dataset.objects.filter(
            access_mode=Dataset.ACCESS_PUBLIC,
            tos__isnull=False,
        )
        .values_list("id", "name", "tos_id", "tos__name")
    )

    datasets_with_tos = group_datasets | grant_datasets | public_datasets

    # Find which TOS the user has already accepted
    accepted_tos_ids = set(
        TOSAcceptance.objects.filter(user_id=tos_user_id).values_list(
            "tos_document_id", flat=True
        )
    )

    result = [
        {
            "dataset_id": dataset_id,
            "dataset_name": dataset_name,
            "tos_id": tos_id,
            "tos_name": tos_name,
        }
        for dataset_id, dataset_name, tos_id, tos_name in datasets_with_tos
        if tos_id not in accepted_tos_ids
    ]

    # Service-specific TOS: find active service TOS docs the user hasn't accepted
    if service:
        now = timezone.now()
        # Datasets the user has any permission on (ignoring TOS), plus all
        # public datasets — service-specific TOS on public datasets must be
        # surfaced too, even without a pre-existing grant.
        user_dataset_ids = set(
            GroupDatasetPermission.objects.filter(
                group__user_groups__user=user,
            ).values_list("dataset_id", flat=True)
        ) | set(
            Grant.objects.filter(user=user).values_list("dataset_id", flat=True)
        ) | set(
            Dataset.objects.filter(access_mode=Dataset.ACCESS_PUBLIC)
            .values_list("id", flat=True)
        )

        service_tos_docs = (
            TOSDocument.objects.filter(
                service__name=service,
                dataset_id__in=user_dataset_ids,
                effective_date__lte=now,
            )
            .filter(Q(retired_date__isnull=True) | Q(retired_date__gt=now))
            .exclude(pk__in=accepted_tos_ids)
            .select_related("dataset")
        )
        for tos in service_tos_docs:
            result.append({
                "dataset_id": tos.dataset_id,
                "dataset_name": tos.dataset.name if tos.dataset else None,
                "tos_id": tos.pk,
                "tos_name": tos.name,
                "service": service,
            })

    return result
