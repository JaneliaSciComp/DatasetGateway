from django.contrib import admin
from django.contrib.auth.models import Group as AuthGroup

# Unregister Django's built-in Group — we use core.Group instead
admin.site.unregister(AuthGroup)

from .models import (
    APIKey,
    AuditLog,
    Dataset,
    DatasetVersion,
    Grant,
    Group,
    GroupDatasetPermission,
    Permission,
    PublicRoot,
    ServiceTable,
    TOSAcceptance,
    TOSDocument,
    User,
    UserGroup,
)


class UserGroupInline(admin.TabularInline):
    model = UserGroup
    extra = 0


class APIKeyInline(admin.TabularInline):
    model = APIKey
    extra = 0
    readonly_fields = ("key", "created", "last_used")


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("id", "email", "name", "admin", "is_active", "created")
    list_filter = ("admin", "is_active", "read_only")
    search_fields = ("email", "name", "display_name")
    readonly_fields = ("password",)
    exclude = ("password",)
    inlines = [UserGroupInline, APIKeyInline]


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)
    inlines = [UserGroupInline]


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ("id", "name")


class ServiceTableInline(admin.TabularInline):
    model = ServiceTable
    extra = 0


class DatasetVersionInline(admin.TabularInline):
    model = DatasetVersion
    extra = 0


@admin.register(Dataset)
class DatasetModelAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "tos", "access_mode")
    list_filter = ("access_mode",)
    search_fields = ("name",)
    inlines = [DatasetVersionInline, ServiceTableInline]


@admin.register(DatasetVersion)
class DatasetVersionAdmin(admin.ModelAdmin):
    list_display = ("id", "dataset", "version", "gcs_bucket", "is_public")
    list_filter = ("is_public",)
    search_fields = ("dataset__name", "version")


@admin.register(GroupDatasetPermission)
class GroupDatasetPermissionAdmin(admin.ModelAdmin):
    list_display = ("id", "group", "dataset", "permission")
    list_filter = ("permission",)


@admin.register(Grant)
class GrantAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "dataset", "dataset_version", "permission", "granted_by", "source")
    list_filter = ("permission", "source")
    search_fields = ("user__email", "dataset__name")


class PublicRootInline(admin.TabularInline):
    model = PublicRoot
    extra = 0


@admin.register(ServiceTable)
class ServiceTableAdmin(admin.ModelAdmin):
    list_display = ("id", "service_name", "table_name", "dataset")
    search_fields = ("service_name", "table_name", "dataset__name")
    inlines = [PublicRootInline]


@admin.register(TOSDocument)
class TOSDocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "dataset", "dataset_version", "effective_date", "retired_date")
    list_filter = ("effective_date",)


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
