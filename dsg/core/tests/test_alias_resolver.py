"""Tests for native dataset alias resolution."""

import pytest
from django.test import TestCase

from core.authz import ResolveStatus, resolve_dataset_reference
from core.models import Dataset, DatasetAlias, DatasetVersion, Service


@pytest.mark.django_db
class TestAliasResolver(TestCase):
    def setUp(self):
        self.service = Service.objects.create(name="clio")
        self.dataset = Dataset.objects.create(name="canonical-ds")
        self.version = DatasetVersion.objects.create(
            dataset=self.dataset, version="v1", branch="main", ordinal=10
        )
        self.other_dataset = Dataset.objects.create(name="other-ds")
        self.other_version = DatasetVersion.objects.create(
            dataset=self.other_dataset, version="v2", branch="main", ordinal=20
        )

    def test_exact_alias_beats_name_level_alias(self):
        DatasetAlias.objects.create(
            service=self.service,
            client_name="client-ds",
            dataset=self.other_dataset,
        )
        DatasetAlias.objects.create(
            service=self.service,
            client_name="client-ds",
            client_version="release",
            dataset=self.dataset,
            dataset_version=self.version,
        )

        result = resolve_dataset_reference(self.service, "client-ds", "release")

        self.assertEqual(result.status, ResolveStatus.FOUND)
        self.assertEqual(result.target.dataset, self.dataset)
        self.assertEqual(result.target.dataset_version, self.version)
        self.assertEqual(result.target.ordinal, 10)

    def test_name_level_alias_interprets_version_canonically(self):
        DatasetAlias.objects.create(
            service=self.service,
            client_name="client-ds",
            dataset=self.other_dataset,
        )

        result = resolve_dataset_reference(self.service, "client-ds", "v2")

        self.assertTrue(result.found)
        self.assertEqual(result.target.dataset, self.other_dataset)
        self.assertEqual(result.target.dataset_version, self.other_version)

    def test_canonical_dataset_and_version_pass_through(self):
        result = resolve_dataset_reference(self.service, "canonical-ds", "v1")

        self.assertTrue(result.found)
        self.assertEqual(result.target.dataset, self.dataset)
        self.assertEqual(result.target.dataset_version, self.version)

    def test_canonical_dataset_grain_passes_through(self):
        result = resolve_dataset_reference(self.service, "canonical-ds")

        self.assertTrue(result.found)
        self.assertEqual(result.target.dataset, self.dataset)
        self.assertTrue(result.target.is_dataset_grain)

    def test_digits_self_describe_branch_and_ordinal_without_registered_anchor(self):
        result = resolve_dataset_reference(self.service, "canonical-ds", "42", branch="branch-a")

        self.assertTrue(result.found)
        self.assertEqual(result.target.dataset, self.dataset)
        self.assertEqual(result.target.branch, "branch-a")
        self.assertEqual(result.target.ordinal, 42)
        self.assertIsNone(result.target.dataset_version)

    def test_unknown_alias_is_typed_miss_not_exception(self):
        result = resolve_dataset_reference(self.service, "client-ds", "missing")

        self.assertEqual(result.status, ResolveStatus.NOT_FOUND)
        self.assertFalse(result.found)
        self.assertIsNone(result.target)
