"""Regression tests for native authorization schema constraints."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from core.models import (
    Dataset,
    DatasetAlias,
    DatasetVersion,
    Group,
    GroupDatasetPermission,
    Permission,
    Service,
    ServiceAccount,
    ServiceAccountGrant,
)


@pytest.mark.django_db
class TestNativeAuthzSchema(TestCase):
    def setUp(self):
        self.dataset = Dataset.objects.create(name="ds1")
        self.other_dataset = Dataset.objects.create(name="ds2")
        self.version = DatasetVersion.objects.create(
            dataset=self.dataset, version="v1", branch="main", ordinal=1
        )
        self.other_version = DatasetVersion.objects.create(
            dataset=self.other_dataset, version="v1", branch="main", ordinal=1
        )
        self.service = Service.objects.create(name="clio")
        self.other_service = Service.objects.create(name="dvid")
        self.permission, _ = Permission.objects.get_or_create(name="view")

    def test_dataset_version_branch_ordinal_unique_when_ordinal_set(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            DatasetVersion.objects.create(
                dataset=self.dataset, version="v1-copy", branch="main", ordinal=1
            )

        DatasetVersion.objects.create(
            dataset=self.dataset, version="v1-other-branch", branch="dev", ordinal=1
        )
        DatasetVersion.objects.create(dataset=self.dataset, version="unranked-a")
        DatasetVersion.objects.create(dataset=self.dataset, version="unranked-b")

    def test_dataset_alias_uniqueness_and_validation(self):
        DatasetAlias.objects.create(
            service=self.service,
            client_name="client-ds",
            client_version="1.0",
            dataset=self.dataset,
            dataset_version=self.version,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            DatasetAlias.objects.create(
                service=self.service,
                client_name="client-ds",
                client_version="1.0",
                dataset=self.dataset,
                dataset_version=self.version,
            )

        DatasetAlias.objects.create(
            service=self.service, client_name="name-only", dataset=self.dataset
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            DatasetAlias.objects.create(
                service=self.service, client_name="name-only", dataset=self.dataset
            )

        empty_version = DatasetAlias(
            service=self.service,
            client_name="empty",
            client_version="",
            dataset=self.dataset,
        )
        with self.assertRaises(ValidationError):
            empty_version.full_clean()

        wrong_dataset = DatasetAlias(
            service=self.service,
            client_name="wrong-ds",
            client_version="1.0",
            dataset=self.dataset,
            dataset_version=self.other_version,
        )
        with self.assertRaises(ValidationError):
            wrong_dataset.full_clean()

    def test_group_dataset_permission_service_constraints(self):
        group = Group.objects.create(name="lab")
        GroupDatasetPermission.objects.create(
            group=group, dataset=self.dataset, permission=self.permission
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            GroupDatasetPermission.objects.create(
                group=group, dataset=self.dataset, permission=self.permission
            )

        GroupDatasetPermission.objects.create(
            group=group, dataset=self.dataset, service=self.service, permission=self.permission
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            GroupDatasetPermission.objects.create(
                group=group,
                dataset=self.dataset,
                service=self.service,
                permission=self.permission,
            )
        GroupDatasetPermission.objects.create(
            group=group,
            dataset=self.dataset,
            service=self.other_service,
            permission=self.permission,
        )

    def test_service_account_grant_constraints_cover_null_quadrants(self):
        service_account = ServiceAccount.objects.create(name="pipeline")

        self._assert_sa_duplicate_rejected(service_account)
        self._assert_sa_duplicate_rejected(service_account, dataset_version=self.version)
        self._assert_sa_duplicate_rejected(service_account, service=self.service)
        self._assert_sa_duplicate_rejected(
            service_account, service=self.service, dataset_version=self.version
        )

    def _assert_sa_duplicate_rejected(self, service_account, service=None, dataset_version=None):
        kwargs = {
            "service_account": service_account,
            "dataset": self.dataset,
            "service": service,
            "dataset_version": dataset_version,
            "permission": self.permission,
        }
        ServiceAccountGrant.objects.create(**kwargs)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ServiceAccountGrant.objects.create(**kwargs)
