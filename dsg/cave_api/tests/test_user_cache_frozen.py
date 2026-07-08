"""Frozen guard for the CAVE-compatible /api/v1/user/cache surface."""

import pytest
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import (
    APIKey,
    Dataset,
    DatasetVersion,
    Grant,
    Group,
    GroupDatasetPermission,
    Permission,
    Service,
    TOSDocument,
    User,
    UserGroup,
)


EXPECTED_USER_CACHE = (
    b'{"id":1,"parent_id":null,"service_account":false,"name":"Frozen",'
    b'"email":"frozen@example.org","admin":false,"pi":"PI","picture_url":"",'
    b'"affiliations":[],"groups":["frozen-group"],'
    b'"groups_admin":["frozen-group"],"permissions":{"frozen":1,"svc-ds":1},'
    b'"permissions_v2":{"frozen":["annotation_editor","view"],"svc-ds":["view"]},'
    b'"permissions_v2_ignore_tos":{"frozen":["annotation_editor","view"],'
    b'"tos-ds":["view"],"svc-ds":["view"]},'
    b'"missing_tos":[{"dataset_id":2,"dataset_name":"tos-ds","tos_id":1,'
    b'"tos_name":"Dataset TOS"}],"datasets_admin":[]}'
)
EXPECTED_SERVICE_USER_CACHE = (
    b'{"id":1,"parent_id":null,"service_account":false,"name":"Frozen",'
    b'"email":"frozen@example.org","admin":false,"pi":"PI","picture_url":"",'
    b'"affiliations":[],"groups":["frozen-group"],'
    b'"groups_admin":["frozen-group"],"permissions":{"frozen":1},'
    b'"permissions_v2":{"frozen":["annotation_editor","view"]},'
    b'"permissions_v2_ignore_tos":{"frozen":["annotation_editor","view"],'
    b'"tos-ds":["view"],"svc-ds":["view"]},'
    b'"missing_tos":[{"dataset_id":2,"dataset_name":"tos-ds","tos_id":1,'
    b'"tos_name":"Dataset TOS"},{"dataset_id":3,"dataset_name":"svc-ds",'
    b'"tos_id":2,"tos_name":"Service TOS","service":"celltyping"}],'
    b'"datasets_admin":[]}'
)


@pytest.mark.django_db
class TestFrozenUserCache(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create(
            email="frozen@example.org",
            name="Frozen User",
            display_name="Frozen",
            pi="PI",
        )
        self.api_key = APIKey.objects.create(user=self.user, key="tok-frozen")
        self.view_perm, _ = Permission.objects.get_or_create(name="view")
        self.named_perm, _ = Permission.objects.get_or_create(name="annotation_editor")
        self.service = Service.objects.create(name="celltyping", display_name="Cell Typing")

    def _auth_header(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.api_key.key}"}

    def _install_byte_golden_fixture(self):
        group = Group.objects.create(name="frozen-group")
        UserGroup.objects.create(user=self.user, group=group, is_admin=True)

        frozen = Dataset.objects.create(name="frozen")
        frozen_v1 = DatasetVersion.objects.create(
            dataset=frozen, version="v1", branch="main", ordinal=1,
        )
        GroupDatasetPermission.objects.create(
            group=group, dataset=frozen, permission=self.view_perm,
        )
        Grant.objects.create(
            user=self.user,
            dataset=frozen,
            dataset_version=frozen_v1,
            service=self.service,
            permission=self.named_perm,
        )

        dataset_tos = TOSDocument.objects.create(name="Dataset TOS", text="Dataset terms.")
        tos_ds = Dataset.objects.create(name="tos-ds", tos=dataset_tos)
        dataset_tos.dataset = tos_ds
        dataset_tos.save()
        Grant.objects.create(user=self.user, dataset=tos_ds, permission=self.view_perm)

        svc_ds = Dataset.objects.create(name="svc-ds")
        Grant.objects.create(user=self.user, dataset=svc_ds, permission=self.view_perm)
        TOSDocument.objects.create(
            name="Service TOS", text="Service terms.", dataset=svc_ds, service=self.service,
        )

    def test_user_cache_bytes_are_stable_with_and_without_service(self):
        self._install_byte_golden_fixture()

        first = self.client.get("/api/v1/user/cache", **self._auth_header())
        second = self.client.get("/api/v1/user/cache", **self._auth_header())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, second.content)
        self.assertEqual(first.content, EXPECTED_USER_CACHE)

        cache.clear()
        first_service = self.client.get(
            "/api/v1/user/cache?service=celltyping", **self._auth_header()
        )
        cache.clear()
        second_service = self.client.get(
            "/api/v1/user/cache?service=celltyping", **self._auth_header()
        )
        self.assertEqual(first_service.status_code, 200)
        self.assertEqual(first_service.content, second_service.content)
        self.assertEqual(first_service.content, EXPECTED_SERVICE_USER_CACHE)

    def test_multi_row_missing_tos_is_asserted_semantically(self):
        for name in ["alpha", "beta"]:
            tos = TOSDocument.objects.create(name=f"{name} TOS", text="Terms.")
            dataset = Dataset.objects.create(
                name=name, tos=tos, access_mode=Dataset.ACCESS_PUBLIC,
            )
            tos.dataset = dataset
            tos.save()

        first = self.client.get("/api/v1/user/cache", **self._auth_header())
        second = self.client.get("/api/v1/user/cache", **self._auth_header())
        self.assertEqual(first.content, second.content)
        missing_names = {item["dataset_name"] for item in first.json()["missing_tos"]}
        self.assertEqual(missing_names, {"alpha", "beta"})
