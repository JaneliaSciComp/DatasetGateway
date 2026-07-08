"""Inventory version-scoped grants and version anchors before native authz rollout."""

from django.core.management.base import BaseCommand
from django.db.models import Q

from core.models import DatasetVersion, Grant


class Command(BaseCommand):
    help = "List version-scoped grants and DatasetVersion rows needing ordinal review"

    def handle(self, *args, **options):
        self.stdout.write("Version-scoped grants")
        grants = (
            Grant.objects.filter(dataset_version__isnull=False)
            .select_related("user", "dataset", "dataset_version", "permission", "service")
            .prefetch_related("buckets")
            .order_by("dataset__name", "dataset_version__version", "user__email")
        )
        if not grants:
            self.stdout.write("  none")
        for grant in grants:
            service = grant.service.name if grant.service_id else "*"
            buckets = ",".join(grant.buckets.values_list("name", flat=True)) or "-"
            self.stdout.write(
                "  "
                f"user={grant.user.email} dataset={grant.dataset.name} "
                f"version={grant.dataset_version.version} "
                f"branch={grant.dataset_version.branch} "
                f"ordinal={grant.dataset_version.ordinal if grant.dataset_version.ordinal is not None else 'MISSING'} "
                f"permission={grant.permission.name} service={service} buckets={buckets}"
            )

        self.stdout.write("")
        self.stdout.write("DatasetVersion anchors needing rollout review")
        versions = (
            DatasetVersion.objects.filter(
                Q(is_public=True)
                | Q(grants__isnull=False)
                | Q(service_account_grants__isnull=False)
                | Q(tos_documents__isnull=False)
            )
            .select_related("dataset")
            .distinct()
            .order_by("dataset__name", "version")
        )
        if not versions:
            self.stdout.write("  none")
        for version in versions:
            reasons = []
            if version.is_public:
                reasons.append("public")
            if version.grants.exists():
                reasons.append("grant")
            if version.service_account_grants.exists():
                reasons.append("service-account-grant")
            if version.tos_documents.exists():
                reasons.append("tos")
            ordinal = version.ordinal if version.ordinal is not None else "MISSING"
            self.stdout.write(
                "  "
                f"dataset={version.dataset.name} version={version.version} "
                f"branch={version.branch} ordinal={ordinal} reasons={','.join(reasons)}"
            )

        self.stdout.write("")
        self.stdout.write("Remediation options per row")
        self.stdout.write("  convert grant to dataset-grain")
        self.stdout.write("  attach explicit buckets to the grant")
        self.stdout.write("  set branch and ordinal on the DatasetVersion anchor")
        self.stdout.write("  accept the narrowed reach")
