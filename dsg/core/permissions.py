"""DRF permission classes for DatasetGateway."""

from rest_framework.permissions import BasePermission


class IsAdmin(BasePermission):
    """Allows access only to admin users."""

    def has_permission(self, request, view):
        if request.user is None:
            return False
        return getattr(request.user, "admin", False)


class IsHumanUser(BasePermission):
    """Rejects ServiceAccount principals — endpoint is for real users only.

    Use alongside IsAuthenticated for endpoints that mint or list per-user
    APIKey rows, or otherwise embed assumptions that the principal is a
    User row in the database.
    """

    def has_permission(self, request, view):
        from .models import ServiceAccount

        if request.user is None:
            return False
        return not isinstance(request.user, ServiceAccount)


class IsDatasetAdmin(BasePermission):
    """Allows access if user is admin of the dataset in context.

    Checks request.dataset_name (set by DatasetContextMiddleware) or
    a 'dataset' kwarg on the view.
    """

    def has_permission(self, request, view):
        from .models import ServiceAccount

        if request.user is None:
            return False
        if getattr(request.user, "admin", False):
            return True

        dataset_name = getattr(request, "dataset_name", None)
        if dataset_name is None:
            dataset_name = view.kwargs.get("dataset")
        if dataset_name is None:
            return False

        cache = getattr(request, "permission_cache", None)
        if cache:
            return dataset_name in cache.get("datasets_admin", [])

        # Service accounts never have admin grants; only the User path falls
        # back to a direct Grant query.
        if isinstance(request.user, ServiceAccount):
            return False

        from .models import Grant

        return Grant.objects.filter(
            user=request.user, dataset__name=dataset_name, permission__name="admin"
        ).exists()
