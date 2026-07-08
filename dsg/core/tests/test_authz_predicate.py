"""Tests for the native authorization containment predicate."""

import pytest
from django.test import TestCase

from core.authz import (
    ContainmentStatus,
    ResolvedTarget,
    evaluate_containment,
    expand_permission,
)
from core.models import (
    Dataset,
    DatasetVersion,
    Grant,
    Permission,
    Service,
    ServiceAccount,
    ServiceAccountGrant,
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
