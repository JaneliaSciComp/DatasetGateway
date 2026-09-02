"""Permission-cache invalidation signals.

The authentication layer caches each principal's permission blob for
PERMISSION_CACHE_TTL seconds (see TokenAuthentication). SA blobs embed
public-dataset view coverage, so a Dataset change (e.g. an access_mode
flip public→private) or an SA grant change must bust the affected entries
instead of serving stale allows until TTL. Dataset saves are rare admin
actions and the SA population is small, so blanket SA invalidation on any
Dataset change is cheap and simpler than field-level change tracking.

Downstream middle_auth_client keeps its own per-token cache DSG cannot
reach; it self-heals on stale-deny (re-fetches on permission failure) but
holds stale allows up to its own TTL. That bound is documented in
docs/service-accounts.md.
"""

from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Dataset, ServiceAccount, ServiceAccountGrant


def _sa_cache_key(sa_pk):
    from .authentication import TokenAuthentication

    return f"{TokenAuthentication.CACHE_PREFIX}sa_{sa_pk}"


def _invalidate_all_service_account_caches():
    keys = [
        _sa_cache_key(pk)
        for pk in ServiceAccount.objects.values_list("pk", flat=True)
    ]
    if keys:
        cache.delete_many(keys)


@receiver(post_save, sender=Dataset, dispatch_uid="core.dataset_saved_sa_cache")
@receiver(post_delete, sender=Dataset, dispatch_uid="core.dataset_deleted_sa_cache")
def _dataset_changed(sender, instance, **kwargs):
    _invalidate_all_service_account_caches()


@receiver(
    post_save, sender=ServiceAccountGrant, dispatch_uid="core.sa_grant_saved_cache"
)
@receiver(
    post_delete, sender=ServiceAccountGrant, dispatch_uid="core.sa_grant_deleted_cache"
)
def _service_account_grant_changed(sender, instance, **kwargs):
    cache.delete(_sa_cache_key(instance.service_account_id))
