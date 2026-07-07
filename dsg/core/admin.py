from django.contrib import admin
from django.contrib.auth.models import Group as AuthGroup

# Unregister Django's built-in Group — we use core.Group instead
admin.site.unregister(AuthGroup)

from .audit import log_audit
from .iam import (
    deprovision_bucket,
    permission_source_users,
    sync_dataset_iam,
    sync_group_datasets_for_user,
    sync_user_dataset_iam,
    sync_user_iam,
)
from .models import (
    Affiliation,
    APIKey,
    AuditLog,
    Dataset,
    DatasetBucket,
    DatasetVersion,
    Grant,
    Group,
    GroupDatasetPermission,
    Permission,
    PublicRoot,
    Service,
    ServiceAccount,
    ServiceAccountGrant,
    ServiceAccountToken,
    ServiceTable,
    TOSAcceptance,
    TOSDocument,
    User,
    UserGroup,
)


class AffiliationInline(admin.TabularInline):
    model = Affiliation
    extra = 0


class UserGroupInline(admin.TabularInline):
    model = UserGroup
    extra = 0


class APIKeyInline(admin.TabularInline):
    model = APIKey
    extra = 0
    readonly_fields = ("key", "created", "last_used", "expires_at")


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("id", "email", "name", "admin", "is_active", "created")
    list_filter = ("admin", "is_active", "read_only")
    search_fields = ("email", "name", "display_name")
    readonly_fields = ("password",)
    exclude = ("password",)
    inlines = [AffiliationInline, UserGroupInline, APIKeyInline]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # is_active/admin flip the access rule's outcome for every dataset the
        # user (and their user-type service accounts) holds a source on.
        if {"is_active", "admin"} & set(form.changed_data):
            sync_user_iam(obj)
            for sa in obj.service_accounts.all():
                sync_user_iam(sa)

    def save_related(self, request, form, formsets, change):
        old_groups = set()
        if change:
            old_groups = set(
                UserGroup.objects.filter(user=form.instance).values_list("group__name", flat=True)
            )
        super().save_related(request, form, formsets, change)
        new_groups = set(
            UserGroup.objects.filter(user=form.instance).values_list("group__name", flat=True)
        )
        for g in new_groups - old_groups:
            log_audit(request.user, "member_added", "UserGroup",
                      f"{form.instance.pk}:{g}",
                      after_state={"user": form.instance.email, "group": g})
        for g in old_groups - new_groups:
            log_audit(request.user, "member_removed", "UserGroup",
                      f"{form.instance.pk}:{g}",
                      before_state={"user": form.instance.email, "group": g})
        # Symmetric difference also covers rows whose group was retargeted
        for group in Group.objects.filter(name__in=old_groups ^ new_groups):
            sync_group_datasets_for_user(form.instance, group)


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)
    inlines = [UserGroupInline]

    def save_related(self, request, form, formsets, change):
        old_members = set()
        if change:
            old_members = set(
                UserGroup.objects.filter(group=form.instance).values_list("user__email", flat=True)
            )
        super().save_related(request, form, formsets, change)
        new_members = set(
            UserGroup.objects.filter(group=form.instance).values_list("user__email", flat=True)
        )
        for email in new_members - old_members:
            log_audit(request.user, "member_added", "UserGroup",
                      f"{email}:{form.instance.pk}",
                      after_state={"user": email, "group": form.instance.name})
        for email in old_members - new_members:
            log_audit(request.user, "member_removed", "UserGroup",
                      f"{email}:{form.instance.pk}",
                      before_state={"user": email, "group": form.instance.name})
        # Symmetric difference also covers rows whose user was retargeted
        for member in User.objects.filter(email__in=old_members ^ new_members):
            sync_group_datasets_for_user(member, form.instance)


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ("id", "name")


class ServiceTableInline(admin.TabularInline):
    model = ServiceTable
    extra = 0


class DatasetBucketInline(admin.TabularInline):
    model = DatasetBucket
    extra = 0


class DatasetVersionInline(admin.TabularInline):
    model = DatasetVersion
    extra = 0


@admin.register(Dataset)
class DatasetModelAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "tos", "access_mode")
    list_filter = ("access_mode",)
    search_fields = ("name",)
    inlines = [DatasetBucketInline, DatasetVersionInline, ServiceTableInline]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Setting/clearing the TOS gate changes should_provision for every user
        if "tos" in form.changed_data:
            sync_dataset_iam(obj)

    def save_formset(self, request, form, formset, change):
        if formset.model is not DatasetBucket:
            return super().save_formset(request, form, formset, change)
        # Capture old names before save (inline rows can rename, not move datasets)
        old_rows = {
            b.pk: (b.name, b.dataset)
            for b in DatasetBucket.objects.filter(
                pk__in=[f.instance.pk for f in formset.forms if f.instance.pk]
            ).select_related("dataset")
        }
        super().save_formset(request, form, formset, change)
        need_sync = bool(formset.new_objects)
        for obj, _changed_fields in formset.changed_objects:
            old_name, old_dataset = old_rows.get(obj.pk, (None, None))
            if old_name is not None and old_name != obj.name:
                deprovision_bucket(old_name, old_dataset)
                need_sync = True
        for obj in formset.deleted_objects:
            old_name, old_dataset = old_rows.get(obj.pk, (obj.name, obj.dataset))
            deprovision_bucket(old_name, old_dataset)
        if need_sync:
            sync_dataset_iam(form.instance)


@admin.register(DatasetBucket)
class DatasetBucketAdmin(admin.ModelAdmin):
    list_display = ("id", "dataset", "name")
    search_fields = ("dataset__name", "name")

    def save_model(self, request, obj, form, change):
        old = None
        if change:
            # Fetch the DB row: the standalone admin can rename the bucket or
            # move it to another dataset; the old (name, dataset) pair must be
            # deprovisioned.
            old = DatasetBucket.objects.filter(pk=obj.pk).select_related("dataset").first()
        super().save_model(request, obj, form, change)
        moved = old and (old.name != obj.name or old.dataset_id != obj.dataset_id)
        if moved:
            deprovision_bucket(old.name, old.dataset)
        if not change or moved:
            sync_dataset_iam(obj.dataset)

    def delete_model(self, request, obj):
        name, dataset = obj.name, obj.dataset
        super().delete_model(request, obj)
        deprovision_bucket(name, dataset)

    def delete_queryset(self, request, queryset):
        # Bulk "delete selected" never calls delete_model; loop the per-object path
        for obj in queryset:
            self.delete_model(request, obj)


@admin.register(DatasetVersion)
class DatasetVersionAdmin(admin.ModelAdmin):
    list_display = ("id", "dataset", "version", "is_public")
    list_filter = ("is_public",)
    search_fields = ("dataset__name", "version")
    filter_horizontal = ("buckets",)


@admin.register(GroupDatasetPermission)
class GroupDatasetPermissionAdmin(admin.ModelAdmin):
    list_display = ("id", "group", "dataset", "permission")
    list_filter = ("permission",)

    def save_model(self, request, obj, form, change):
        old = None
        old_users = None
        if change:
            before = {f: str(form.initial.get(f, "")) for f in form.changed_data}
            after = {f: str(form.cleaned_data.get(f, "")) for f in form.changed_data}
            # Fetch the DB row (form.initial only has scalars): a retargeted
            # row must also converge the old dataset, and its old group's
            # members drop out of the enumeration once the row is saved.
            old = GroupDatasetPermission.objects.filter(
                pk=obj.pk
            ).select_related("dataset").first()
            if old:
                old_users = list(permission_source_users(old.dataset))
        super().save_model(request, obj, form, change)
        action = "group_permission_updated" if change else "group_permission_created"
        state_kwargs = {}
        if change:
            state_kwargs = {"before_state": before, "after_state": after}
        else:
            state_kwargs = {"after_state": {
                "group": str(obj.group), "dataset": str(obj.dataset),
                "permission": str(obj.permission),
            }}
        log_audit(request.user, action, "GroupDatasetPermission", obj.pk, **state_kwargs)
        if old and (old.group_id != obj.group_id or old.dataset_id != obj.dataset_id):
            sync_dataset_iam(old.dataset, users=old_users)
        sync_dataset_iam(obj.dataset)

    def delete_model(self, request, obj):
        before = {
            "group": str(obj.group), "dataset": str(obj.dataset),
            "permission": str(obj.permission),
        }
        log_audit(request.user, "group_permission_deleted", "GroupDatasetPermission",
                  obj.pk, before_state=before)
        dataset = obj.dataset
        # Capture before delete: the group's members drop out of the
        # enumeration once this row is gone.
        users = list(permission_source_users(dataset))
        super().delete_model(request, obj)
        sync_dataset_iam(dataset, users=users)

    def delete_queryset(self, request, queryset):
        # Bulk "delete selected" never calls delete_model; loop the per-object path
        for obj in queryset:
            self.delete_model(request, obj)


@admin.register(Grant)
class GrantAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "dataset", "dataset_version", "permission", "granted_by", "source")
    list_filter = ("permission", "source")
    search_fields = ("user__email", "dataset__name")

    def save_model(self, request, obj, form, change):
        old = None
        if change:
            before = {f: str(form.initial.get(f, "")) for f in form.changed_data}
            after = {f: str(form.cleaned_data.get(f, "")) for f in form.changed_data}
            # Fetch the DB row (form.initial only has scalars): a retargeted
            # row must also converge the old (user, dataset) pair.
            old = Grant.objects.filter(pk=obj.pk).select_related("user", "dataset").first()
        super().save_model(request, obj, form, change)
        action = "grant_updated" if change else "grant_created"
        state_kwargs = {}
        if change:
            state_kwargs = {"before_state": before, "after_state": after}
        else:
            state_kwargs = {"after_state": {
                "user": str(obj.user), "dataset": str(obj.dataset),
                "permission": str(obj.permission), "source": obj.source,
            }}
        log_audit(request.user, action, "Grant", obj.pk, **state_kwargs)
        if old and (old.user_id != obj.user_id or old.dataset_id != obj.dataset_id):
            sync_user_dataset_iam(old.user, old.dataset)
        sync_user_dataset_iam(obj.user, obj.dataset)

    def delete_model(self, request, obj):
        before = {
            "user": str(obj.user), "dataset": str(obj.dataset),
            "permission": str(obj.permission), "source": obj.source,
        }
        log_audit(request.user, "grant_deleted", "Grant", obj.pk, before_state=before)
        user, dataset = obj.user, obj.dataset
        super().delete_model(request, obj)
        sync_user_dataset_iam(user, dataset)

    def delete_queryset(self, request, queryset):
        # Bulk "delete selected" never calls delete_model; loop the per-object path
        for obj in queryset:
            self.delete_model(request, obj)


class PublicRootInline(admin.TabularInline):
    model = PublicRoot
    extra = 0


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "display_name", "base_url")
    search_fields = ("name", "display_name")


@admin.register(ServiceTable)
class ServiceTableAdmin(admin.ModelAdmin):
    list_display = ("id", "service_name", "table_name", "dataset")
    search_fields = ("service_name", "table_name", "dataset__name")
    inlines = [PublicRootInline]


@admin.register(TOSDocument)
class TOSDocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "dataset", "dataset_version", "service", "effective_date", "retired_date")
    list_filter = ("effective_date", "service")

    def save_model(self, request, obj, form, change):
        old_dataset = None
        retargeted = False
        if change:
            # Fetch the DB row: moving the doc between datasets (or flipping
            # service scope) moves the TOS gate; both datasets must resync.
            old = TOSDocument.objects.filter(pk=obj.pk).select_related("dataset").first()
            if old:
                old_dataset = old.dataset
                retargeted = (old.dataset_id != obj.dataset_id
                              or old.service_id != obj.service_id)
        super().save_model(request, obj, form, change)
        action = "tos_document_updated" if change else "tos_document_created"
        log_audit(request.user, action, "TOSDocument", obj.pk, after_state={
            "name": obj.name, "dataset": str(obj.dataset) if obj.dataset else None,
            "service": str(obj.service) if obj.service else None,
        })
        # Auto-set Dataset.tos only for general (non-service-specific) TOS docs
        flipped = False
        if obj.dataset_id and not obj.service_id and obj.dataset.tos_id != obj.pk:
            obj.dataset.tos = obj
            obj.dataset.save(update_fields=["tos"])
            flipped = True
        if (
            retargeted
            and old_dataset is not None
            and old_dataset.tos_id == obj.pk
        ):
            old_dataset.tos = None
            old_dataset.save(update_fields=["tos"])
        # Resync here only — the auto-set bypasses DatasetModelAdmin.save_model,
        # so there is no double-fire.
        if retargeted or flipped:
            targets = {}
            if retargeted and old_dataset is not None:
                targets[old_dataset.pk] = old_dataset
            if obj.dataset_id:
                targets[obj.dataset_id] = obj.dataset
            for ds in targets.values():
                sync_dataset_iam(ds)


@admin.register(TOSAcceptance)
class TOSAcceptanceAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "tos_document", "accepted_at")
    list_filter = ("tos_document",)
    search_fields = ("user__email",)

    def save_model(self, request, obj, form, change):
        old = None
        if change:
            # Fetch the DB row: a retargeted acceptance must also converge the
            # old (user, dataset) pair.
            old = TOSAcceptance.objects.filter(
                pk=obj.pk
            ).select_related("user", "tos_document__dataset").first()
        super().save_model(request, obj, form, change)
        if old and (old.user_id != obj.user_id or old.tos_document_id != obj.tos_document_id):
            if old.tos_document.dataset:
                sync_user_dataset_iam(old.user, old.tos_document.dataset)
        if obj.tos_document.dataset:
            sync_user_dataset_iam(obj.user, obj.tos_document.dataset)

    def delete_model(self, request, obj):
        user, dataset = obj.user, obj.tos_document.dataset
        super().delete_model(request, obj)
        if dataset:
            sync_user_dataset_iam(user, dataset)

    def delete_queryset(self, request, queryset):
        # Bulk "delete selected" never calls delete_model; loop the per-object path
        for obj in queryset:
            self.delete_model(request, obj)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "actor", "action", "target_type", "target_id")
    list_filter = ("action", "target_type")
    readonly_fields = ("actor", "action", "target_type", "target_id", "before_state", "after_state", "timestamp")


class ServiceAccountTokenInline(admin.TabularInline):
    model = ServiceAccountToken
    extra = 0
    readonly_fields = ("key", "created", "last_used")


class ServiceAccountGrantInline(admin.TabularInline):
    model = ServiceAccountGrant
    extra = 0
    readonly_fields = ("created",)


@admin.register(ServiceAccount)
class ServiceAccountAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "is_active", "created_by", "created")
    list_filter = ("is_active",)
    search_fields = ("name", "description")
    readonly_fields = ("created", "updated")
    inlines = [ServiceAccountTokenInline, ServiceAccountGrantInline]


@admin.register(ServiceAccountToken)
class ServiceAccountTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "service_account", "description", "created", "last_used")
    list_filter = ("service_account",)
    readonly_fields = ("key", "created", "last_used")
    search_fields = ("service_account__name", "description")


@admin.register(ServiceAccountGrant)
class ServiceAccountGrantAdmin(admin.ModelAdmin):
    list_display = ("id", "service_account", "dataset", "dataset_version", "permission", "granted_by")
    list_filter = ("permission",)
    search_fields = ("service_account__name", "dataset__name")
