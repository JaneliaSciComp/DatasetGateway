"""Core models for DatasetGateway authorization service.

Ported from CAVE's SQLAlchemy models with extensions from Architecture.md.
"""

import secrets

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


SERVICE_ACCOUNT_EMAIL_SUFFIX = "@service-account.dsg.local"


class UserManager(BaseUserManager):
    """Manager for the custom User model (no passwords — Google OAuth + APIKey only)."""

    def create_user(self, email, name="", **extra_fields):
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(email=email, name=name, **extra_fields)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, name="", **extra_fields):
        extra_fields.setdefault("admin", True)
        return self.create_user(email, name, **extra_fields)


class User(AbstractBaseUser):
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
    notes = models.TextField(blank=True, default="")
    picture_url = models.URLField(max_length=512, blank=True, default="")

    # SCIM 2.0 fields
    scim_id = models.CharField(max_length=36, unique=True, null=True, blank=True, db_index=True)
    external_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True, db_index=True
    )

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    # M:M to Group through UserGroup
    groups = models.ManyToManyField("Group", through="UserGroup", related_name="users")

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]
    EMAIL_FIELD = "email"

    class Meta:
        db_table = "dsg_user"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(email__iendswith=SERVICE_ACCOUNT_EMAIL_SUFFIX),
                name="user_email_not_service_account_domain",
            ),
        ]

    def clean(self):
        super().clean()
        if self.email and self.email.lower().endswith(SERVICE_ACCOUNT_EMAIL_SUFFIX):
            raise ValidationError({
                "email": "User email addresses cannot use the service-account domain."
            })

    def __str__(self):
        return self.email

    @property
    def is_staff(self):
        return self.admin

    @property
    def is_superuser(self):
        return self.admin

    def has_perm(self, perm, obj=None):
        return self.admin

    def has_module_perms(self, app_label):
        return self.admin

    # Constant False for duck-type symmetry with ServiceAccount, which sets
    # is_service_account = True. A User is never a service account: the
    # user-type ("parent"-linked robot) mechanism was removed in favor of the
    # dedicated ServiceAccount model.
    is_service_account = False

    @property
    def is_enabled(self):
        """Disabled means disabled. Single home for the rule — consumed by the
        IAM access rule, DRF auth, and both cookie helpers."""
        return self.is_active

    @property
    def public_name(self):
        return self.display_name or self.name or self.email.split("@")[0]


class Affiliation(models.Model):
    """An organizational affiliation for a user (many-to-one)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="affiliations")
    name = models.CharField(max_length=255)

    class Meta:
        db_table = "affiliation"
        unique_together = [("user", "name")]

    def __str__(self):
        return f"{self.user.email}: {self.name}"


class Group(models.Model):
    """Authorization group."""

    name = models.CharField(max_length=255, unique=True)

    # SCIM 2.0 fields
    scim_id = models.CharField(max_length=36, unique=True, null=True, blank=True, db_index=True)
    external_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True, db_index=True
    )

    class Meta:
        db_table = "dsg_group"

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

    ACCESS_CLOSED = "closed"
    ACCESS_PUBLIC = "public"
    ACCESS_MODE_CHOICES = [
        (ACCESS_CLOSED, "Closed — invite only"),
        (ACCESS_PUBLIC, "Public — self-service access"),
    ]

    name = models.SlugField(max_length=255, unique=True)
    description = models.TextField(blank=True, default="")
    tos = models.ForeignKey(
        "TOSDocument", on_delete=models.SET_NULL, null=True, blank=True, related_name="datasets"
    )
    access_mode = models.CharField(
        max_length=10, choices=ACCESS_MODE_CHOICES, default=ACCESS_CLOSED
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


class DatasetBucket(models.Model):
    """A GCS bucket associated with a dataset."""

    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="buckets")
    name = models.CharField(max_length=255)

    class Meta:
        db_table = "dataset_bucket"
        unique_together = [("dataset", "name")]

    def __str__(self):
        return self.name


class BucketIAMBinding(models.Model):
    """DSG-owned GCS bucket IAM binding provenance ledger."""

    bucket_name = models.CharField(max_length=255)
    email = models.EmailField(max_length=254)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "bucket_iam_binding"
        constraints = [
            models.UniqueConstraint(
                fields=["bucket_name", "email"],
                name="uniq_bucket_iam_binding_bucket_email",
            ),
        ]
        indexes = [
            models.Index(fields=["bucket_name"], name="bucket_iam_bucket_idx"),
            models.Index(fields=["email"], name="bucket_iam_email_idx"),
        ]

    def __str__(self):
        return f"{self.email} -> {self.bucket_name}"


class DatasetVersion(models.Model):
    """A versioned release of a dataset."""

    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="versions")
    version = models.CharField(max_length=255)
    branch = models.CharField(max_length=255, default="main")
    ordinal = models.BigIntegerField(null=True, blank=True)
    buckets = models.ManyToManyField("DatasetBucket", blank=True, related_name="versions")
    prefix = models.CharField(max_length=512, blank=True, default="")
    is_public = models.BooleanField(default=False)

    class Meta:
        db_table = "dataset_version"
        unique_together = [("dataset", "version")]
        constraints = [
            models.UniqueConstraint(
                fields=["dataset", "branch", "ordinal"],
                condition=models.Q(ordinal__isnull=False),
                name="uniq_dataset_version_dataset_branch_ordinal",
            ),
        ]

    def __str__(self):
        return f"{self.dataset.name}:{self.version}"


class DatasetTranslation(models.Model):
    """Service-local dataset/version vocabulary translated to canonical DSG anchors."""

    service = models.ForeignKey(
        "Service", on_delete=models.CASCADE, related_name="dataset_translations"
    )
    client_name = models.CharField(max_length=255)
    client_version = models.CharField(max_length=255, null=True, blank=True)
    dataset = models.ForeignKey(
        Dataset, on_delete=models.CASCADE, related_name="translations"
    )
    dataset_version = models.ForeignKey(
        DatasetVersion,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="translations",
    )

    class Meta:
        db_table = "dataset_translation"
        constraints = [
            models.UniqueConstraint(
                fields=["service", "client_name", "client_version"],
                name="uniq_dataset_translation_service_name_version",
            ),
            models.UniqueConstraint(
                fields=["service", "client_name"],
                condition=models.Q(client_version__isnull=True),
                name="uniq_dataset_translation_service_name_null_version",
            ),
        ]

    def clean(self):
        super().clean()
        if self.client_version == "":
            raise ValidationError(
                {"client_version": "Use NULL for name-level translations."}
            )
        if bool(self.client_version) != bool(self.dataset_version_id):
            raise ValidationError(
                "Version translations must set both client_version and dataset_version; "
                "name-level translations must set neither."
            )
        if (
            self.dataset_version_id
            and self.dataset_id
            and self.dataset_version.dataset_id != self.dataset_id
        ):
            raise ValidationError({"dataset_version": "Dataset version must belong to dataset."})

    def __str__(self):
        version = f":{self.client_version}" if self.client_version is not None else ""
        return f"{self.service}:{self.client_name}{version} -> {self.dataset}"



class GroupDatasetPermission(models.Model):
    """Grants a permission on a dataset to a group."""

    group = models.ForeignKey(
        Group, on_delete=models.CASCADE, related_name="dataset_permissions"
    )
    dataset = models.ForeignKey(
        Dataset, on_delete=models.CASCADE, related_name="group_permissions"
    )
    service = models.ForeignKey(
        "Service", on_delete=models.CASCADE, null=True, blank=True, related_name="group_permissions"
    )
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE)

    class Meta:
        db_table = "group_dataset_permission"
        constraints = [
            models.UniqueConstraint(
                fields=["group", "dataset", "permission"],
                condition=models.Q(service__isnull=True),
                name="uniq_group_dataset_permission_null_service",
            ),
            models.UniqueConstraint(
                fields=["group", "dataset", "service", "permission"],
                condition=models.Q(service__isnull=False),
                name="uniq_group_dataset_permission_service",
            ),
        ]

    def __str__(self):
        return f"{self.group} -> {self.dataset}: {self.permission}"


class Grant(models.Model):
    """Direct user grant on a dataset (optionally scoped to a version)."""

    SOURCE_MANUAL = "manual"
    SOURCE_SELF_SERVICE = "self_service"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Added by admin"),
        (SOURCE_SELF_SERVICE, "Self-service TOS acceptance"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="grants")
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="grants")
    dataset_version = models.ForeignKey(
        DatasetVersion, on_delete=models.CASCADE, null=True, blank=True, related_name="grants"
    )
    service = models.ForeignKey(
        "Service", on_delete=models.CASCADE, null=True, blank=True, related_name="grants"
    )
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE)
    group = models.ForeignKey(
        Group, on_delete=models.CASCADE, null=True, blank=True, related_name="grants"
    )
    granted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="grants_given"
    )
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default=SOURCE_MANUAL
    )
    created = models.DateTimeField(auto_now_add=True)
    buckets = models.ManyToManyField(DatasetBucket, blank=True, related_name="grants")

    class Meta:
        db_table = "grant"

    def __str__(self):
        scope = f":{self.dataset_version.version}" if self.dataset_version else ""
        return f"{self.user} -> {self.dataset}{scope}: {self.permission}"


class Service(models.Model):
    """A named service that can have its own TOS requirements per dataset."""

    VERSION_EVAL_LINEAR = "linear"
    VERSION_EVAL_DAG = "dag"
    VERSION_EVAL_MODE_CHOICES = [
        (VERSION_EVAL_LINEAR, "Linear"),
        (VERSION_EVAL_DAG, "DAG"),
    ]

    name = models.SlugField(max_length=255, unique=True)
    display_name = models.CharField(max_length=255, blank=True, default="")
    base_url = models.URLField(blank=True, default="")
    version_eval_mode = models.CharField(
        max_length=10, choices=VERSION_EVAL_MODE_CHOICES, default=VERSION_EVAL_LINEAR
    )

    class Meta:
        db_table = "service"

    def __str__(self):
        return self.display_name or self.name


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
    """Terms of Service document, optionally scoped to dataset/version/service."""

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
    service = models.ForeignKey(
        Service,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tos_documents",
        help_text="If set, this TOS is required only when accessing via this service",
    )
    invite_token = models.CharField(
        max_length=64, unique=True, default=secrets.token_urlsafe,
        help_text="Unguessable token for TOS page URLs on closed datasets",
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


def _default_expiry():
    return timezone.now() + timezone.timedelta(seconds=settings.AUTH_COOKIE_AGE)


class APIKey(models.Model):
    """API token for authenticating requests."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_keys")
    key = models.CharField(max_length=128, unique=True, default=_generate_token, db_index=True)
    description = models.CharField(max_length=255, blank=True, default="")
    created = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, default=_default_expiry)

    class Meta:
        db_table = "api_key"

    def __str__(self):
        return f"APIKey({self.user}, {self.description!r})"

    @property
    def is_expired(self):
        if self.expires_at is None:
            return False
        return timezone.now() >= self.expires_at


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


class ServiceAccount(models.Model):
    """A non-human identity for programmatic access.

    Distinct from User: no Google OAuth, no TOS, no group membership, no
    DSG-service login. Holds long-lived API tokens and dataset privileges.
    Created and managed by global admins only.
    """

    name = models.SlugField(max_length=255, unique=True)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="service_accounts_created",
    )
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    # Duck-typed surface so DRF and downstream code accept this as request.user.
    # Service accounts are never global admins, never have TOS, never have groups.
    is_authenticated = True
    is_anonymous = False
    is_staff = False
    is_superuser = False
    admin = False
    is_service_account = True
    parent_id = None
    pi = ""
    picture_url = ""
    read_only = False
    google_sub = None

    class Meta:
        db_table = "service_account"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            database = kwargs.get("using") or self._state.db or "default"
            saved_name = (
                type(self).objects.using(database)
                .filter(pk=self.pk)
                .values_list("name", flat=True)
                .first()
            )
            if saved_name is not None and saved_name != self.name:
                raise ValidationError({
                    "name": "Service account names cannot be changed after creation."
                })
        return super().save(*args, **kwargs)

    @property
    def is_enabled(self):
        # Mirrors User.is_enabled: the row's own active flag is the whole rule.
        return self.is_active

    @property
    def email(self):
        # Synthetic identifier; never sent to GCS IAM (SAs use bearer-token
        # auth at the gateway, not bucket-level IAM).
        return f"{self.name}{SERVICE_ACCOUNT_EMAIL_SUFFIX}"

    @property
    def public_name(self):
        return self.name

    @property
    def affiliations(self):
        return _EmptyRelatedManager()

    def has_perm(self, perm, obj=None):
        return False

    def has_module_perms(self, app_label):
        return False


class _EmptyRelatedManager:
    """Stand-in for User.affiliations on ServiceAccount — yields nothing."""

    def all(self):
        return []

    def values_list(self, *args, **kwargs):
        return []

    def exists(self):
        return False


class ServiceAccountToken(models.Model):
    """Long-lived API token for a ServiceAccount. No expiry by design."""

    service_account = models.ForeignKey(
        ServiceAccount, on_delete=models.CASCADE, related_name="tokens"
    )
    key = models.CharField(
        max_length=128, unique=True, default=_generate_token, db_index=True
    )
    description = models.CharField(max_length=255, blank=True, default="")
    created = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "service_account_token"
        ordering = ["-created"]

    def __str__(self):
        return f"ServiceAccountToken({self.service_account}, {self.description!r})"


class ServiceAccountGrant(models.Model):
    """Direct service-account grant on a dataset (optionally scoped to a version).

    Mirrors Grant for SAs. Kept separate from Grant so existing user-grant
    queries are untouched and SA grants never trigger per-user GCS bucket
    IAM provisioning (which is human-only).
    """

    service_account = models.ForeignKey(
        ServiceAccount, on_delete=models.CASCADE, related_name="grants"
    )
    dataset = models.ForeignKey(
        Dataset, on_delete=models.CASCADE, related_name="service_account_grants"
    )
    dataset_version = models.ForeignKey(
        DatasetVersion,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="service_account_grants",
    )
    service = models.ForeignKey(
        Service,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="service_account_grants",
    )
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE)
    granted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sa_grants_given",
    )
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "service_account_grant"
        constraints = [
            models.UniqueConstraint(
                fields=["service_account", "dataset", "permission"],
                condition=models.Q(service__isnull=True, dataset_version__isnull=True),
                name="uniq_sa_grant_dataset_null_service_null_version",
            ),
            models.UniqueConstraint(
                fields=["service_account", "dataset", "dataset_version", "permission"],
                condition=models.Q(service__isnull=True, dataset_version__isnull=False),
                name="uniq_sa_grant_dataset_null_service_version",
            ),
            models.UniqueConstraint(
                fields=["service_account", "dataset", "service", "permission"],
                condition=models.Q(service__isnull=False, dataset_version__isnull=True),
                name="uniq_sa_grant_dataset_service_null_version",
            ),
            models.UniqueConstraint(
                fields=["service_account", "dataset", "dataset_version", "service", "permission"],
                condition=models.Q(service__isnull=False, dataset_version__isnull=False),
                name="uniq_sa_grant_dataset_service_version",
            ),
        ]

    def __str__(self):
        scope = f":{self.dataset_version.version}" if self.dataset_version else ""
        return f"{self.service_account} -> {self.dataset}{scope}: {self.permission}"
