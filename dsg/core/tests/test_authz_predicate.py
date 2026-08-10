"""Tests for the native authorization containment predicate."""

import pytest
from django.test import TestCase

from core.authz import (
    BucketAuthorizationStatus,
    ContainmentStatus,
    ResolvedTarget,
    evaluate_bucket_authorization,
    evaluate_containment,
    expand_permission,
)
from core.models import (
    Dataset,
    DatasetBucket,
    DatasetVersion,
    Grant,
    Permission,
    Service,
    ServiceAccount,
    ServiceAccountGrant,
    TOSDocument,
    User,
)


@pytest.mark.django_db
class TestAuthzContainmentPredicate(TestCase):
    def setUp(self):
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.edit_perm, _ = Permission.objects.get_or_create(name="edit")
        self.manage_perm, _ = Permission.objects.get_or_create(name="manage")
        self.admin_perm, _ = Permission.objects.get_or_create(name="admin")
        self.named_perm, _ = Permission.objects.get_or_create(name="annotation_editor")

        self.linear_service = Service.objects.create(name="linear", version_eval_mode="linear")
        self.dag_service = Service.objects.create(name="dag", version_eval_mode="dag")
        self.other_service = Service.objects.create(name="other", version_eval_mode="linear")
        self.dataset = Dataset.objects.create(name="ds1")
        self.v1 = DatasetVersion.objects.create(
            dataset=self.dataset, version="v1", branch="main", ordinal=1
        )
        self.v2 = DatasetVersion.objects.create(
            dataset=self.dataset, version="v2", branch="main", ordinal=2
        )
        self.alt = DatasetVersion.objects.create(
            dataset=self.dataset, version="alt1", branch="alt", ordinal=1
        )
        self.unranked = DatasetVersion.objects.create(
            dataset=self.dataset, version="unranked", branch="main"
        )
        self.user = User.objects.create(email="user@example.org")

    def _target(self, version):
        return ResolvedTarget(
            dataset=version.dataset,
            branch=version.branch,
            ordinal=version.ordinal,
            dataset_version=version,
        )

    def test_dataset_grain_grant_covers_version_query(self):
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)

        decision = evaluate_containment(
            self.user, self.linear_service, self._target(self.v2), "view"
        )

        self.assertEqual(decision.status, ContainmentStatus.COVERED)


@pytest.mark.django_db
class TestBucketAuthorizationPredicate(TestCase):
    def setUp(self):
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.service = Service.objects.create(name="clio")
        self.user = User.objects.create(email="bucket-user@example.org")
        self.dataset = Dataset.objects.create(name="bucket-ds")
        self.bucket_a = DatasetBucket.objects.create(
            dataset=self.dataset, name="bucket-a",
        )
        self.bucket_b = DatasetBucket.objects.create(
            dataset=self.dataset, name="bucket-b",
        )
        self.v1 = DatasetVersion.objects.create(
            dataset=self.dataset,
            version="v1",
            branch="main",
            ordinal=1,
        )
        self.v2 = DatasetVersion.objects.create(
            dataset=self.dataset,
            version="v2",
            branch="main",
            ordinal=2,
        )

    def _decision(self, bucket_name):
        return evaluate_bucket_authorization(self.user, bucket_name)

    def test_bucket_constraints_restrict_nonempty_and_leave_empty_unrestricted(self):
        grant = Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            permission=self.view_perm,
        )

        self.assertEqual(
            self._decision("bucket-b").status,
            BucketAuthorizationStatus.AUTHORIZED,
        )

        grant.buckets.add(self.bucket_a)

        self.assertEqual(
            self._decision("bucket-a").status,
            BucketAuthorizationStatus.AUTHORIZED,
        )
        self.assertEqual(
            self._decision("bucket-b").status,
            BucketAuthorizationStatus.DENIED,
        )

    def test_version_reach_allows_ancestor_bucket_and_denies_descendant_bucket(self):
        self.v1.buckets.add(self.bucket_a)
        v3 = DatasetVersion.objects.create(
            dataset=self.dataset,
            version="v3",
            branch="main",
            ordinal=3,
        )
        v3.buckets.add(self.bucket_b)
        Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            dataset_version=self.v2,
            permission=self.view_perm,
        )

        self.assertEqual(
            self._decision("bucket-a").status,
            BucketAuthorizationStatus.AUTHORIZED,
        )
        self.assertEqual(
            self._decision("bucket-b").status,
            BucketAuthorizationStatus.DENIED,
        )

    def test_service_scoped_grant_denied_and_unscoped_grant_allowed(self):
        Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            service=self.service,
            permission=self.view_perm,
        )

        self.assertEqual(
            self._decision("bucket-a").status,
            BucketAuthorizationStatus.DENIED,
        )

        Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            permission=self.view_perm,
        )
        self.assertEqual(
            self._decision("bucket-a").status,
            BucketAuthorizationStatus.AUTHORIZED,
        )

    def test_shared_bucket_uses_union_without_granting_other_bucket(self):
        other_dataset = Dataset.objects.create(name="other-ds")
        DatasetBucket.objects.create(dataset=other_dataset, name="bucket-a")
        DatasetBucket.objects.create(dataset=other_dataset, name="other-only")
        primary_grant = Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            permission=self.view_perm,
        )

        self.assertEqual(
            self._decision("bucket-a").status,
            BucketAuthorizationStatus.AUTHORIZED,
        )
        self.assertEqual(
            self._decision("other-only").status,
            BucketAuthorizationStatus.DENIED,
        )

        primary_grant.delete()
        Grant.objects.create(
            user=self.user,
            dataset=other_dataset,
            permission=self.view_perm,
        )

        self.assertEqual(
            self._decision("bucket-a").status,
            BucketAuthorizationStatus.AUTHORIZED,
        )
        self.assertEqual(
            self._decision("bucket-b").status,
            BucketAuthorizationStatus.DENIED,
        )

    def test_admin_has_no_implicit_bucket_bypass(self):
        self.user.admin = True
        self.user.save(update_fields=["admin"])

        self.assertEqual(
            self._decision("bucket-a").status,
            BucketAuthorizationStatus.DENIED,
        )

    def test_cross_dataset_grant_version_is_ignored(self):
        foreign_dataset = Dataset.objects.create(name="foreign-version-ds")
        foreign_version = DatasetVersion.objects.create(
            dataset=foreign_dataset,
            version="foreign-v1",
            branch="main",
            ordinal=1,
        )
        self.v1.buckets.add(self.bucket_a)
        Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            dataset_version=foreign_version,
            permission=self.view_perm,
        )

        self.assertEqual(
            self._decision("bucket-a").status,
            BucketAuthorizationStatus.DENIED,
        )

    def test_cross_dataset_grant_bucket_constraint_is_ignored(self):
        foreign_dataset = Dataset.objects.create(name="foreign-bucket-ds")
        foreign_bucket = DatasetBucket.objects.create(
            dataset=foreign_dataset,
            name="foreign-bucket",
        )
        grant = Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            permission=self.view_perm,
        )
        grant.buckets.add(foreign_bucket)

        self.assertEqual(
            self._decision("bucket-a").status,
            BucketAuthorizationStatus.DENIED,
        )

    def test_cross_dataset_version_bucket_edge_is_ignored(self):
        foreign_dataset = Dataset.objects.create(name="foreign-edge-ds")
        foreign_version = DatasetVersion.objects.create(
            dataset=foreign_dataset,
            version="foreign-v1",
            branch="main",
            ordinal=1,
        )
        foreign_version.buckets.add(self.bucket_a)
        Grant.objects.create(
            user=self.user,
            dataset=foreign_dataset,
            dataset_version=foreign_version,
            permission=self.view_perm,
        )

        self.assertEqual(
            self._decision("bucket-a").status,
            BucketAuthorizationStatus.DENIED,
        )

    def test_mixed_version_bucket_requires_coverage_and_tos_at_same_anchor(self):
        self.v1.buckets.add(self.bucket_a)
        self.v2.buckets.add(self.bucket_a)
        Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            dataset_version=self.v1,
            permission=self.view_perm,
        )
        TOSDocument.objects.create(
            name="v1 terms",
            text="Terms",
            dataset_version=self.v1,
        )

        decision = self._decision("bucket-a")

        self.assertEqual(decision.status, BucketAuthorizationStatus.TOS_REQUIRED)
        self.assertEqual(decision.anchor.dataset_version, self.v1)


@pytest.mark.django_db
class TestAuthzContainmentPredicateAdditional(TestCase):
    def setUp(self):
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.edit_perm, _ = Permission.objects.get_or_create(name="edit")
        self.manage_perm, _ = Permission.objects.get_or_create(name="manage")
        self.admin_perm, _ = Permission.objects.get_or_create(name="admin")
        self.named_perm, _ = Permission.objects.get_or_create(
            name="annotation_editor",
        )

        self.linear_service = Service.objects.create(
            name="linear", version_eval_mode="linear",
        )
        self.dag_service = Service.objects.create(
            name="dag", version_eval_mode="dag",
        )
        self.other_service = Service.objects.create(
            name="other", version_eval_mode="linear",
        )
        self.dataset = Dataset.objects.create(name="ds1")
        self.v1 = DatasetVersion.objects.create(
            dataset=self.dataset, version="v1", branch="main", ordinal=1,
        )
        self.v2 = DatasetVersion.objects.create(
            dataset=self.dataset, version="v2", branch="main", ordinal=2,
        )
        self.alt = DatasetVersion.objects.create(
            dataset=self.dataset, version="alt1", branch="alt", ordinal=1,
        )
        self.unranked = DatasetVersion.objects.create(
            dataset=self.dataset, version="unranked", branch="main",
        )
        self.user = User.objects.create(email="user@example.org")

    def _target(self, version):
        return ResolvedTarget(
            dataset=version.dataset,
            branch=version.branch,
            ordinal=version.ordinal,
            dataset_version=version,
        )

    def test_version_grant_does_not_leak_to_dataset_grain(self):
        Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            dataset_version=self.v2,
            permission=self.view_perm,
        )

        decision = evaluate_containment(
            self.user, self.linear_service, ResolvedTarget(dataset=self.dataset), "view"
        )

        self.assertEqual(decision.status, ContainmentStatus.NOT_COVERED)

    def test_same_branch_ordinal_at_or_below_anchor_covers(self):
        Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            dataset_version=self.v2,
            permission=self.view_perm,
        )

        covered = evaluate_containment(
            self.user, self.linear_service, self._target(self.v1), "view"
        )
        not_covered = evaluate_containment(
            self.user,
            self.linear_service,
            ResolvedTarget(dataset=self.dataset, branch="main", ordinal=3),
            "view",
        )

        self.assertEqual(covered.status, ContainmentStatus.COVERED)
        self.assertEqual(not_covered.status, ContainmentStatus.NOT_COVERED)

    def test_ordinal_less_anchor_covers_only_exact_anchor(self):
        Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            dataset_version=self.unranked,
            permission=self.view_perm,
        )

        exact = evaluate_containment(
            self.user, self.linear_service, self._target(self.unranked), "view"
        )
        sibling = evaluate_containment(
            self.user, self.linear_service, self._target(self.v1), "view"
        )

        self.assertEqual(exact.status, ContainmentStatus.COVERED)
        self.assertEqual(sibling.status, ContainmentStatus.NOT_COVERED)

    def test_cross_branch_is_indeterminate_for_dag_and_not_covered_for_linear(self):
        Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            dataset_version=self.v2,
            permission=self.view_perm,
        )

        linear = evaluate_containment(
            self.user, self.linear_service, self._target(self.alt), "view"
        )
        dag = evaluate_containment(self.user, self.dag_service, self._target(self.alt), "view")

        self.assertEqual(linear.status, ContainmentStatus.NOT_COVERED)
        self.assertEqual(dag.status, ContainmentStatus.INDETERMINATE)

    def test_service_scoped_grant_matches_only_its_service(self):
        Grant.objects.create(
            user=self.user,
            dataset=self.dataset,
            service=self.linear_service,
            permission=self.view_perm,
        )

        matching = evaluate_containment(
            self.user, self.linear_service, self._target(self.v1), "view"
        )
        other = evaluate_containment(self.user, self.other_service, self._target(self.v1), "view")

        self.assertEqual(matching.status, ContainmentStatus.COVERED)
        self.assertEqual(other.status, ContainmentStatus.NOT_COVERED)

    def test_null_service_grant_matches_all_services(self):
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.view_perm)

        decision = evaluate_containment(
            self.user, self.other_service, self._target(self.v1), "view"
        )

        self.assertEqual(decision.status, ContainmentStatus.COVERED)

    def test_named_role_matches_exactly_not_via_hierarchy(self):
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.named_perm)

        exact = evaluate_containment(
            self.user, self.linear_service, self._target(self.v1), "annotation_editor"
        )
        view = evaluate_containment(self.user, self.linear_service, self._target(self.v1), "view")

        self.assertEqual(exact.status, ContainmentStatus.COVERED)
        self.assertEqual(view.status, ContainmentStatus.NOT_COVERED)

    def test_view_permissions_expand_downward(self):
        self.assertEqual(expand_permission("admin"), {"admin", "manage", "edit", "view"})
        self.assertEqual(expand_permission("manage"), {"manage", "edit", "view"})
        self.assertEqual(expand_permission("edit"), {"edit", "view"})
        self.assertEqual(expand_permission("view"), {"view"})

    def test_read_only_principal_never_covers_edit(self):
        self.user.read_only = True
        self.user.save()
        Grant.objects.create(user=self.user, dataset=self.dataset, permission=self.admin_perm)

        decision = evaluate_containment(
            self.user, self.linear_service, self._target(self.v1), "edit"
        )

        self.assertEqual(decision.status, ContainmentStatus.NOT_COVERED)
        self.assertNotIn("edit", decision.effective_permissions)

    def test_service_account_grants_are_sources(self):
        service_account = ServiceAccount.objects.create(name="pipeline")
        ServiceAccountGrant.objects.create(
            service_account=service_account,
            dataset=self.dataset,
            dataset_version=self.v2,
            permission=self.view_perm,
        )

        decision = evaluate_containment(
            service_account, self.linear_service, self._target(self.v1), "view"
        )

        self.assertEqual(decision.status, ContainmentStatus.COVERED)
