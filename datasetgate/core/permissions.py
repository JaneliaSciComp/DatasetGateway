"""DRF permission classes for DatasetGate."""

from rest_framework.permissions import BasePermission


class IsAdmin(BasePermission):
    """Allows access only to admin users."""

    def has_permission(self, request, view):
        if request.user is None:
            return False
        return getattr(request.user, "admin", False)


class IsDatasetAdmin(BasePermission):
    """Allows access if user is admin of the dataset in context.

    Checks request.dataset_name (set by DatasetContextMiddleware) or
    a 'dataset' kwarg on the view.
    """

    def has_permission(self, request, view):
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

        from .models import DatasetAdmin

        return DatasetAdmin.objects.filter(
            user=request.user, dataset__name=dataset_name
        ).exists()
