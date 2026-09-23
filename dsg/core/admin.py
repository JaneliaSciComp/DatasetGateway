from django.contrib import admin
from django.contrib.auth.models import Group as AuthGroup

# Unregister Django's built-in Group — we use core.Group instead
admin.site.unregister(AuthGroup)

from .audit import log_audit
from .models import (
    Affiliation,
    APIKey,
    AuditLog,
    ClientConsent,
    Dataset,
    DatasetBucket,
    DatasetTranslation,
    DatasetVersion,
    Grant,
    Group,
    GroupDatasetPermission,
    Permission,
    PublicRoot,
    RegisteredClient,
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


class ClientConsentInline(admin.TabularInline):
    model = ClientConsent
    extra = 0
    readonly_fields = ("client", "created")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(RegisteredClient)
class RegisteredClientAdmin(admin.ModelAdmin):
    list_display = ("name", "origin", "owner", "enabled")
    readonly_fields = ("allowed_services", "created")


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("id", "email", "name", "admin", "is_active", "created")
    list_filter = ("admin", "is_active", "read_only")
    search_fields = ("email", "name", "display_name")
    readonly_fields = ("password",)
    exclude = ("password",)
    inlines = [AffiliationInline, UserGroupInline, APIKeyInline, ClientConsentInline]

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


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ("id", "name")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            fields.append("name")
        return fields


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


@admin.register(DatasetBucket)
class DatasetBucketAdmin(admin.ModelAdmin):
    list_display = ("id", "dataset", "name")
    search_fields = ("dataset__name", "name")


@admin.register(DatasetVersion)
class DatasetVersionAdmin(admin.ModelAdmin):
    list_display = ("id", "dataset", "version", "branch", "ordinal", "is_public")
    list_filter = ("is_public",)
    search_fields = ("dataset__name", "version")
    filter_horizontal = ("buckets",)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "is_public":
            kwargs["help_text"] = (
                "Covers this version and, when ordinals are set, its "
                "same-branch ancestors."
            )
        return super().formfield_for_dbfield(db_field, request, **kwargs)


@admin.register(DatasetTranslation)
class DatasetTranslationAdmin(admin.ModelAdmin):
    list_display = (
        "id", "service", "client_name", "client_version", "dataset", "dataset_version",
    )
    list_filter = ("service",)
    search_fields = (
        "client_name", "client_version", "dataset__name", "dataset_version__version",
    )
    autocomplete_fields = ("service", "dataset", "dataset_version")
    list_select_related = ("service", "dataset", "dataset_version")


@admin.register(GroupDatasetPermission)
class GroupDatasetPermissionAdmin(admin.ModelAdmin):
    list_display = ("id", "group", "dataset", "service", "permission")
    list_filter = ("permission", "service")

    def save_model(self, request, obj, form, change):
        if change:
            before = {f: str(form.initial.get(f, "")) for f in form.changed_data}
            after = {f: str(form.cleaned_data.get(f, "")) for f in form.changed_data}
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

    def delete_model(self, request, obj):
        before = {
            "group": str(obj.group), "dataset": str(obj.dataset),
            "permission": str(obj.permission),
        }
        log_audit(request.user, "group_permission_deleted", "GroupDatasetPermission",
                  obj.pk, before_state=before)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        # Bulk "delete selected" never calls delete_model; loop the per-object path
        for obj in queryset:
            self.delete_model(request, obj)


@admin.register(Grant)
class GrantAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "dataset", "dataset_version", "service", "permission", "granted_by", "source")
    list_filter = ("permission", "service", "source")
    search_fields = ("user__email", "dataset__name")
    filter_horizontal = ("buckets",)

    def save_model(self, request, obj, form, change):
        if change:
            before = {f: str(form.initial.get(f, "")) for f in form.changed_data}
            after = {f: str(form.cleaned_data.get(f, "")) for f in form.changed_data}
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

    def delete_model(self, request, obj):
        before = {
            "user": str(obj.user), "dataset": str(obj.dataset),
            "permission": str(obj.permission), "source": obj.source,
        }
        log_audit(request.user, "grant_deleted", "Grant", obj.pk, before_state=before)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        # Bulk "delete selected" never calls delete_model; loop the per-object path
        for obj in queryset:
            self.delete_model(request, obj)


class PublicRootInline(admin.TabularInline):
    model = PublicRoot
    extra = 0


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "display_name", "base_url", "version_eval_mode")
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
            # service scope) moves the TOS gate off the old dataset.
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
        if obj.dataset_id and not obj.service_id and obj.dataset.tos_id != obj.pk:
            obj.dataset.tos = obj
            obj.dataset.save(update_fields=["tos"])
        if (
            retargeted
            and old_dataset is not None
            and old_dataset.tos_id == obj.pk
        ):
            old_dataset.tos = None
            old_dataset.save(update_fields=["tos"])


@admin.register(TOSAcceptance)
class TOSAcceptanceAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "tos_document", "accepted_at")
    list_filter = ("tos_document",)
    search_fields = ("user__email",)


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

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            fields.append("name")
        return fields


@admin.register(ServiceAccountToken)
class ServiceAccountTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "service_account", "description", "created", "last_used")
    list_filter = ("service_account",)
    readonly_fields = ("key", "created", "last_used")
    search_fields = ("service_account__name", "description")


@admin.register(ServiceAccountGrant)
class ServiceAccountGrantAdmin(admin.ModelAdmin):
    list_display = ("id", "service_account", "dataset", "dataset_version", "service", "permission", "granted_by")
    list_filter = ("permission", "service")
    search_fields = ("service_account__name", "dataset__name")
