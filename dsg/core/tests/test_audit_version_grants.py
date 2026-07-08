"""Tests for the native-authz rollout preflight audit command."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import TestCase

from core.models import (
    Dataset,
    DatasetVersion,
    Grant,
    Permission,
    TOSDocument,
    User,
)


@pytest.mark.django_db
class TestAuditVersionGrantsCommand(TestCase):
    def test_lists_version_grants_and_referenced_versions_without_writes(self):
        permission, _ = Permission.objects.get_or_create(name="view")
        dataset = Dataset.objects.create(name="ds1")
        user = User.objects.create(email="user@example.org")
        granted = DatasetVersion.objects.create(dataset=dataset, version="v1")
        public = DatasetVersion.objects.create(
            dataset=dataset, version="v2", branch="main", ordinal=2, is_public=True
        )
        tos_version = DatasetVersion.objects.create(dataset=dataset, version="v3")
        Grant.objects.create(
            user=user,
            dataset=dataset,
            dataset_version=granted,
            permission=permission,
        )
        TOSDocument.objects.create(name="TOS", text="Terms", dataset_version=tos_version)
        before = {
            "versions": DatasetVersion.objects.count(),
            "grants": Grant.objects.count(),
            "tos": TOSDocument.objects.count(),
        }
        out = StringIO()

        call_command("audit_version_grants", stdout=out)

        text = out.getvalue()
        self.assertIn("Version-scoped grants", text)
        self.assertIn("user=user@example.org dataset=ds1 version=v1", text)
        self.assertIn("ordinal=MISSING", text)
        self.assertIn("dataset=ds1 version=v2 branch=main ordinal=2 reasons=public", text)
        self.assertIn("dataset=ds1 version=v3 branch=main ordinal=MISSING reasons=tos", text)
        self.assertIn("convert grant to dataset-grain", text)
        self.assertIn("attach explicit buckets to the grant", text)
        self.assertIn("set branch and ordinal on the DatasetVersion anchor", text)
        self.assertIn("accept the narrowed reach", text)
        after = {
            "versions": DatasetVersion.objects.count(),
            "grants": Grant.objects.count(),
            "tos": TOSDocument.objects.count(),
        }
        self.assertEqual(after, before)
