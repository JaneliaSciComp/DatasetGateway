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
    public_version_coverage,
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
class TestPublicVersionCoverage(TestCase):
    def setUp(self):
        self.linear_service = Service.objects.create(
            name="public-linear",
            version_eval_mode=Service.VERSION_EVAL_LINEAR,
        )
        self.dag_service = Service.objects.create(
            name="public-dag",
            version_eval_mode=Service.VERSION_EVAL_DAG,
        )
        self.dataset = Dataset.objects.create(name="public-coverage")
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
            is_public=True,
        )
        self.v3 = DatasetVersion.objects.create(
            dataset=self.dataset,
            version="v3",
            branch="main",
            ordinal=3,
        )
        self.alt = DatasetVersion.objects.create(
            dataset=self.dataset,
            version="alt",
            branch="alt",
            ordinal=1,
        )
        self.unranked = DatasetVersion.objects.create(
            dataset=self.dataset,
            version="unranked",
            branch="main",
            is_public=True,
        )
        self.user = User.objects.create(email="public-coverage@example.org")

    def _target(self, version):
        return ResolvedTarget(
            dataset=version.dataset,
            branch=version.branch,
            ordinal=version.ordinal,
            dataset_version=version,
        )

    def _coverage(self, target, permission="view", service=None, principal=None):
        return public_version_coverage(
            principal or self.user,
            target,
            requested_permission=permission,
            service=service or self.linear_service,
        )

    def test_ordinal_public_version_covers_exact_and_same_branch_ancestor_only(self):
        self.assertTrue(self._coverage(self._target(self.v2)).covered)
        self.assertTrue(self._coverage(self._target(self.v1)).covered)
        self.assertFalse(self._coverage(self._target(self.v3)).covered)
        self.assertFalse(self._coverage(self._target(self.alt)).covered)

    def test_null_ordinal_public_version_covers_only_itself(self):
        self.assertTrue(self._coverage(self._target(self.unranked)).covered)

        sibling = DatasetVersion.objects.create(
            dataset=self.dataset,
            version="unranked-sibling",
            branch="main",
        )
        self.assertFalse(self._coverage(self._target(sibling)).covered)

    def test_dag_returns_ordinal_anchors_for_noncovering_target_and_skips_null(self):
        coverage = self._coverage(self._target(self.alt), service=self.dag_service)

        self.assertFalse(coverage.covered)
        self.assertEqual(coverage.service_eval_versions, (self.v2,))

    def test_dataset_grain_non_view_and_disabled_are_not_covered(self):
        dataset_grain = self._coverage(ResolvedTarget(dataset=self.dataset))
        non_view = self._coverage(self._target(self.v2), permission="edit")
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        disabled = self._coverage(self._target(self.v2))
        disabled_sa = ServiceAccount.objects.create(
            name="disabled-coverage-sa", is_active=False,
        )
        disabled_sa_result = self._coverage(
            self._target(self.v2),
            principal=disabled_sa,
        )

        for result in (dataset_grain, non_view, disabled, disabled_sa_result):
            self.assertFalse(result.covered)
            self.assertEqual(result.service_eval_versions, ())

    def test_service_account_gets_public_version_coverage_like_a_user(self):
        service_account = ServiceAccount.objects.create(name="public-coverage-sa")

        covered = self._coverage(self._target(self.v2), principal=service_account)
        ancestor = self._coverage(self._target(self.v1), principal=service_account)
        uncovered = self._coverage(self._target(self.v3), principal=service_account)

        self.assertTrue(covered.covered)
        self.assertTrue(ancestor.covered)
        self.assertFalse(uncovered.covered)


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

    def test_public_version_covers_exact_and_ancestor_buckets_only(self):
        descendant_bucket = DatasetBucket.objects.create(
            dataset=self.dataset,
            name="bucket-descendant",
        )
        other_branch_bucket = DatasetBucket.objects.create(
            dataset=self.dataset,
            name="bucket-other-branch",
        )
        descendant = DatasetVersion.objects.create(
            dataset=self.dataset,
            version="v3",
            branch="main",
            ordinal=3,
        )
        other_branch = DatasetVersion.objects.create(
            dataset=self.dataset,
            version="alt",
            branch="alt",
            ordinal=1,
        )
        self.v1.buckets.add(self.bucket_a)
        self.v2.buckets.add(self.bucket_b)
        descendant.buckets.add(descendant_bucket)
        other_branch.buckets.add(other_branch_bucket)
        self.v2.is_public = True
        self.v2.save(update_fields=["is_public"])

        exact = self._decision(self.bucket_b.name)
        ancestor = self._decision(self.bucket_a.name)
        descendant_result = self._decision(descendant_bucket.name)
        other_branch_result = self._decision(other_branch_bucket.name)

        self.assertEqual(exact.status, BucketAuthorizationStatus.AUTHORIZED)
        self.assertEqual(exact.reason, "public-version")
        self.assertEqual(ancestor.status, BucketAuthorizationStatus.AUTHORIZED)
        self.assertEqual(ancestor.reason, "public-version")
        self.assertEqual(descendant_result.status, BucketAuthorizationStatus.DENIED)
        self.assertEqual(other_branch_result.status, BucketAuthorizationStatus.DENIED)

    def test_null_ordinal_public_version_covers_only_its_bucket(self):
        unranked = DatasetVersion.objects.create(
            dataset=self.dataset,
            version="unranked",
            is_public=True,
        )
        unranked.buckets.add(self.bucket_a)
        self.v1.buckets.add(self.bucket_b)

        exact = self._decision(self.bucket_a.name)
        sibling = self._decision(self.bucket_b.name)

        self.assertEqual(exact.status, BucketAuthorizationStatus.AUTHORIZED)
        self.assertEqual(exact.reason, "public-version")
        self.assertEqual(sibling.status, BucketAuthorizationStatus.DENIED)

    def test_public_version_does_not_cover_dataset_grain_or_disabled_principals(self):
        self.v2.is_public = True
        self.v2.save(update_fields=["is_public"])
        self.v2.buckets.add(self.bucket_b)

        dataset_grain = self._decision(self.bucket_a.name)
        service_account = ServiceAccount.objects.create(name="public-bucket-sa")
        service_account_result = evaluate_bucket_authorization(
            service_account,
            self.bucket_b.name,
        )
        disabled_sa = ServiceAccount.objects.create(
            name="disabled-bucket-sa", is_active=False,
        )
        disabled_sa_result = evaluate_bucket_authorization(
            disabled_sa,
            self.bucket_b.name,
        )
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        disabled = self._decision(self.bucket_b.name)

        self.assertEqual(dataset_grain.status, BucketAuthorizationStatus.DENIED)
        self.assertEqual(
            service_account_result.status, BucketAuthorizationStatus.AUTHORIZED
        )
        self.assertEqual(service_account_result.reason, "public-version")
        self.assertEqual(disabled_sa_result.status, BucketAuthorizationStatus.DENIED)
        self.assertEqual(disabled.status, BucketAuthorizationStatus.DENIED)

    def test_public_version_tos_is_live_and_evaluated_at_target(self):
        self.v1.buckets.add(self.bucket_a)
        self.v2.is_public = True
        self.v2.save(update_fields=["is_public"])
        TOSDocument.objects.create(
            name="Public release terms",
            text="Terms",
            dataset_version=self.v2,
        )

        before_target_tos = self._decision(self.bucket_a.name)
        target_tos = TOSDocument.objects.create(
            name="Ancestor terms",
            text="Terms",
            dataset_version=self.v1,
        )
        after_target_tos = self._decision(self.bucket_a.name)

        self.assertEqual(
            before_target_tos.status,
            BucketAuthorizationStatus.AUTHORIZED,
        )
        self.assertEqual(after_target_tos.status, BucketAuthorizationStatus.TOS_REQUIRED)
        self.assertEqual(after_target_tos.pending_documents, (target_tos,))

    def test_dataset_public_reason_remains_distinct(self):
        self.dataset.access_mode = Dataset.ACCESS_PUBLIC
        self.dataset.save(update_fields=["access_mode"])

        decision = self._decision(self.bucket_a.name)

        self.assertEqual(decision.status, BucketAuthorizationStatus.AUTHORIZED)
        self.assertEqual(decision.reason, "public")


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
