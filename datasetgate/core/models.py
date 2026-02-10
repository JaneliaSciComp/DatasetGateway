"""Core models for DatasetGate authorization service.

Ported from CAVE's SQLAlchemy models with extensions from Architecture.md.
"""

import secrets

from django.db import models
from django.utils import timezone


class User(models.Model):
    """User identity, linked to Google OAuth."""

    google_sub = models.CharField(max_length=255, unique=True, blank=True, null=True)
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255, blank=True, default="")
    display_name = models.CharField(max_length=255, blank=True, default="")
    admin = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    gdpr_consent = models.BooleanField(default=False)
    pi = models.CharField(max_length=255, blank=True, default="")
    read_only = models.BooleanField(default=False)

    # Service account support — parent is the owning human user
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True, related_name="service_accounts"
    )

    # SCIM 2.0 fields
    scim_id = models.CharField(max_length=36, unique=True, null=True, blank=True, db_index=True)
    external_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True, db_index=True
    )

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    # M:M to Group through UserGroup
    groups = models.ManyToManyField("Group", through="UserGroup", related_name="users")

    class Meta:
        db_table = "user"

    def __str__(self):
        return self.email

    @property
    def is_authenticated(self):
        """Required by DRF's IsAuthenticated permission."""
        return True

    @property
    def is_service_account(self):
        return self.parent_id is not None

    @property
    def public_name(self):
        return self.display_name or self.name or self.email.split("@")[0]


class Group(models.Model):
    """Authorization group."""

    name = models.CharField(max_length=255, unique=True)

    # SCIM 2.0 fields
    scim_id = models.CharField(max_length=36, unique=True, null=True, blank=True, db_index=True)
    external_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True, db_index=True
    )

    class Meta:
        db_table = "group"

    def __str__(self):
        return self.name


class UserGroup(models.Model):
    """M:M through table for User-Group membership."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="user_groups")
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="user_groups")
    is_admin = models.BooleanField(default=False)

    class Meta:
        db_table = "user_group"
        unique_together = [("user", "group")]

    def __str__(self):
        role = " (admin)" if self.is_admin else ""
        return f"{self.user} -> {self.group}{role}"


class Permission(models.Model):
    """Abstract permission type (e.g. view, edit)."""

    name = models.CharField(max_length=80, unique=True)

    class Meta:
        db_table = "permission"

    def __str__(self):
        return self.name


class Dataset(models.Model):
    """A neuroscience dataset."""

    name = models.SlugField(max_length=255, unique=True)
    description = models.TextField(blank=True, default="")
    tos = models.ForeignKey(
        "TOSDocument", on_delete=models.SET_NULL, null=True, blank=True, related_name="datasets"
    )

    # SCIM 2.0 fields
    scim_id = models.CharField(max_length=36, unique=True, null=True, blank=True, db_index=True)
    external_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True, db_index=True
    )

    class Meta:
        db_table = "dataset"

    def __str__(self):
        return self.name


class DatasetVersion(models.Model):
    """A versioned release of a dataset, mapped to a GCS bucket."""

    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="versions")
    version = models.CharField(max_length=255)
    gcs_bucket = models.CharField(max_length=255, blank=True, default="")
    prefix = models.CharField(max_length=512, blank=True, default="")
    is_public = models.BooleanField(default=False)

    class Meta:
        db_table = "dataset_version"
        unique_together = [("dataset", "version")]

    def __str__(self):
        return f"{self.dataset.name}:{self.version}"


class DatasetAdmin(models.Model):
    """Admin role for a specific dataset."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="admin_datasets")
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="admins")

    class Meta:
        db_table = "dataset_admin"
        unique_together = [("user", "dataset")]

    def __str__(self):
        return f"{self.user} admin of {self.dataset}"


class GroupDatasetPermission(models.Model):
    """Grants a permission on a dataset to a group."""

    group = models.ForeignKey(
        Group, on_delete=models.CASCADE, related_name="dataset_permissions"
    )
    dataset = models.ForeignKey(
        Dataset, on_delete=models.CASCADE, related_name="group_permissions"
    )
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE)

    class Meta:
        db_table = "group_dataset_permission"
        unique_together = [("group", "dataset", "permission")]

    def __str__(self):
        return f"{self.group} -> {self.dataset}: {self.permission}"


class Grant(models.Model):
    """Direct user grant on a dataset (optionally scoped to a version)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="grants")
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="grants")
    dataset_version = models.ForeignKey(
        DatasetVersion, on_delete=models.CASCADE, null=True, blank=True, related_name="grants"
    )
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE)
    granted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="grants_given"
    )
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "grant"

    def __str__(self):
        scope = f":{self.dataset_version.version}" if self.dataset_version else ""
        return f"{self.user} -> {self.dataset}{scope}: {self.permission}"


class ServiceTable(models.Model):
    """Maps a CAVE service table to a dataset."""

    service_name = models.CharField(max_length=255)
    table_name = models.CharField(max_length=255)
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="service_tables")

    class Meta:
        db_table = "service_table"
        unique_together = [("service_name", "table_name")]

    def __str__(self):
        return f"{self.service_name}/{self.table_name} -> {self.dataset}"


class TOSDocument(models.Model):
    """Terms of Service document, optionally scoped to dataset/version."""

    name = models.CharField(max_length=255)
    text = models.TextField()
    dataset = models.ForeignKey(
        Dataset,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tos_documents",
    )
    dataset_version = models.ForeignKey(
        DatasetVersion,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tos_documents",
    )
    effective_date = models.DateTimeField(default=timezone.now)
    retired_date = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "tos_document"

    def __str__(self):
        return self.name

    @property
    def is_active(self):
        now = timezone.now()
        if self.retired_date and self.retired_date <= now:
            return False
        return self.effective_date <= now


class TOSAcceptance(models.Model):
    """Record of a user accepting a TOS document."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="tos_acceptances")
    tos_document = models.ForeignKey(
        TOSDocument, on_delete=models.CASCADE, related_name="acceptances"
    )
    accepted_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        db_table = "tos_acceptance"
        unique_together = [("user", "tos_document")]

    def __str__(self):
        return f"{self.user} accepted {self.tos_document}"


def _generate_token():
    return secrets.token_hex(32)


class APIKey(models.Model):
    """API token for authenticating requests."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_keys")
    key = models.CharField(max_length=128, unique=True, default=_generate_token, db_index=True)
    description = models.CharField(max_length=255, blank=True, default="")
    created = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "api_key"

    def __str__(self):
        return f"APIKey({self.user}, {self.description!r})"


class PublicRoot(models.Model):
    """A root ID that is publicly accessible for a service table."""

    service_table = models.ForeignKey(
        ServiceTable, on_delete=models.CASCADE, related_name="public_roots"
    )
    root_id = models.BigIntegerField()

    class Meta:
        db_table = "public_root"
        unique_together = [("service_table", "root_id")]

    def __str__(self):
        return f"{self.service_table}: root {self.root_id}"


class AuditLog(models.Model):
    """Audit trail for administrative actions."""

    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    action = models.CharField(max_length=255)
    target_type = models.CharField(max_length=100)
    target_id = models.CharField(max_length=255)
    before_state = models.JSONField(null=True, blank=True)
    after_state = models.JSONField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_log"
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.actor} {self.action} {self.target_type}:{self.target_id}"
