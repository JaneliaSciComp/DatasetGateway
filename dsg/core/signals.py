"""Permission-cache invalidation signals.

The authentication layer caches each principal's permission blob for
PERMISSION_CACHE_TTL seconds (see TokenAuthentication). SA blobs embed
public-dataset view coverage, so a Dataset change (e.g. an access_mode
flip public→private) or an SA grant change must bust the affected entries
instead of serving stale allows until TTL. Dataset saves are rare admin
actions and the SA population is small, so blanket SA invalidation on any
Dataset change is cheap and simpler than field-level change tracking.

Invalidation runs after the database transaction commits, closing the
delete-before-commit repopulation race. This is not linearizable: another
worker can consume the old cache row between commit and the callback, and
a process crash or cache-backend error after commit can leave it in place
until the 300-second TTL expires. That bounded residual window is accepted.

QuerySet.update(), bulk_create(), and other bulk operations bypass model
signals and are outside this invalidation contract.

Downstream middle_auth_client keeps its own per-token cache DSG cannot
reach; it self-heals on stale-deny (re-fetches on permission failure) but
holds stale allows up to its own TTL. That bound is documented in
docs/service-accounts.md.
"""

from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from .models import Dataset, ServiceAccount, ServiceAccountGrant


def _sa_cache_key(sa_pk):
    from .authentication import TokenAuthentication

    return f"{TokenAuthentication.CACHE_PREFIX}sa_{sa_pk}"


def _invalidate_all_service_account_caches(using):
    keys = [
        _sa_cache_key(pk)
        for pk in ServiceAccount.objects.using(using).values_list("pk", flat=True)
    ]
    if keys:
        cache.delete_many(keys)


@receiver(post_save, sender=Dataset, dispatch_uid="core.dataset_saved_sa_cache")
@receiver(post_delete, sender=Dataset, dispatch_uid="core.dataset_deleted_sa_cache")
def _dataset_changed(sender, instance, using, **kwargs):
    transaction.on_commit(
        lambda: _invalidate_all_service_account_caches(using), using=using
    )


@receiver(
    pre_save, sender=ServiceAccountGrant, dispatch_uid="core.sa_grant_capture_owner"
)
def _capture_service_account_grant_owner(sender, instance, using, **kwargs):
    old_owner_id = None
    if instance.pk is not None:
        old_owner_id = (
            ServiceAccountGrant.objects.using(using)
            .filter(pk=instance.pk)
            .values_list("service_account_id", flat=True)
            .first()
        )
    instance._dsg_old_service_account_id = old_owner_id


@receiver(
    post_save, sender=ServiceAccountGrant, dispatch_uid="core.sa_grant_saved_cache"
)
def _service_account_grant_saved(sender, instance, using, **kwargs):
    old_owner_id = getattr(instance, "_dsg_old_service_account_id", None)
    grant_pk = instance.pk

    def invalidate():
        new_owner_id = (
            ServiceAccountGrant.objects.using(using)
            .filter(pk=grant_pk)
            .values_list("service_account_id", flat=True)
            .first()
        )
        keys = [
            _sa_cache_key(owner_id)
            for owner_id in {old_owner_id, new_owner_id}
            if owner_id is not None
        ]
        if keys:
            cache.delete_many(keys)

    transaction.on_commit(invalidate, using=using)


@receiver(
    post_delete, sender=ServiceAccountGrant, dispatch_uid="core.sa_grant_deleted_cache"
)
def _service_account_grant_deleted(sender, instance, using, **kwargs):
    owner_id = instance.service_account_id

    def invalidate():
        cache.delete(_sa_cache_key(owner_id))

    transaction.on_commit(invalidate, using=using)
