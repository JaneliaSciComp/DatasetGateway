"""List/PUT contracts preserve omitted fields and reject unauthorized writes."""

import pytest

from core.models import (
    APIKey, AuditLog, Dataset, Group, RegisteredClient, ServiceTable,
    TOSDocument, User, UserGroup,
)
from scim.serializers import DATASET_SCHEMA, GROUP_SCHEMA

pytestmark = pytest.mark.django_db
BASE = "/auth/scim/v2"


@pytest.fixture
def admin_headers(api_client):
    admin = User.objects.create(email="scim-admin@example.org", admin=True)
    key = APIKey.objects.create(user=admin)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {key.key}")
    # Keep the generated key inside the client, out of pytest's fixture repr.
    return {}


@pytest.fixture
def group():
    return Group.objects.create(name="original", external_id="external-group", scim_id="group-id")


@pytest.fixture
def dataset():
    return Dataset.objects.create(
        name="original", description="Original description", external_id="external-dataset", scim_id="dataset-id",
    )


def state():
    # Compare persisted rows, including memberships, mapping IDs and audit payloads.
    return [list(model.objects.order_by("pk").values()) for model in (
        Group, Dataset, UserGroup, ServiceTable, AuditLog,
    )]


def test_group_list_empty(api_client, admin_headers):
    response = api_client.get(f"{BASE}/Groups", **admin_headers)
    assert response.status_code == 200
    assert response.json() == {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": 0, "itemsPerPage": 0, "startIndex": 1, "Resources": [],
    }


@pytest.mark.parametrize("query, names, total, start", [
    ({}, ["zeta", "alpha", "middle"], 3, 1),
    ({"startIndex": 2, "count": 1}, ["alpha"], 3, 2),
    ({"startIndex": 9, "count": 2}, [], 3, 9),
    ({"count": 0}, [], 3, 1),
    ({"filter": 'displayName eq "alpha"'}, ["alpha"], 1, 1),
])
def test_group_list_order_filter_and_pagination(api_client, admin_headers, query, names, total, start):
    for index, name in enumerate(["zeta", "alpha", "middle"]):
        Group.objects.create(name=name, scim_id=f"group-{index}")
    before = state()
    response = api_client.get(f"{BASE}/Groups", query, **admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    assert body["totalResults"] == total
    assert body["itemsPerPage"] == len(names)
    assert body["startIndex"] == start
    assert [resource["displayName"] for resource in body["Resources"]] == names
    for resource in body["Resources"]:
        assert resource["schemas"] == [GROUP_SCHEMA]
        assert resource["id"] == Group.objects.get(name=resource["displayName"]).scim_id
        assert resource["members"] == []
    assert state() == before


@pytest.mark.parametrize("expression", ['displayName eq'])
def test_group_list_invalid_filter(api_client, admin_headers, group, expression):
    before = state()
    response = api_client.get(f"{BASE}/Groups", {"filter": expression}, **admin_headers)
    assert response.status_code == 400
    assert response.json()["scimType"] == "invalidFilter"
    assert response.json()["status"] == "400"
    assert state() == before


@pytest.mark.parametrize("payload, expected_name, expected_external", [
    ({"displayName": "renamed"}, "renamed", "external-group"),
    ({"externalId": "changed"}, "original", "changed"),
    ({"displayName": "renamed", "externalId": "changed"}, "renamed", "changed"),
    ({"displayName": "original"}, "original", "external-group"),
    ({}, "original", "external-group"),
])
def test_group_put_scalar_fields_and_audit(api_client, admin_headers, group, payload,
                                           expected_name, expected_external):
    member = User.objects.create(email="retained@example.org")
    UserGroup.objects.create(user=member, group=group, is_admin=True)
    before_members = list(UserGroup.objects.values())
    response = api_client.put(f"{BASE}/Groups/{group.scim_id}", payload, format="json", **admin_headers)
    assert response.status_code == 200
    group.refresh_from_db()
    assert (group.name, group.external_id) == (expected_name, expected_external)
    assert response.json()["displayName"] == expected_name
    assert response.json()["externalId"] == expected_external
    assert response.json()["schemas"] == [GROUP_SCHEMA]
    assert list(UserGroup.objects.values()) == before_members
    changed = (expected_name, expected_external) != ("original", "external-group")
    assert AuditLog.objects.filter(action="group_updated").count() == int(changed)
    assert not AuditLog.objects.filter(action__in=["member_added", "member_removed"]).exists()
    if changed:
        audit = AuditLog.objects.get(action="group_updated")
        field_map = {"displayName": "name", "externalId": "external_id"}
        original = {"name": "original", "external_id": "external-group"}
        assert audit.before_state == {field_map[key]: original[field_map[key]] for key in payload}
        assert audit.after_state == {field_map[key]: value for key, value in payload.items()}
        assert audit.actor.email == "scim-admin@example.org"
        assert audit.target_type == "Group"
        assert audit.target_id == str(group.pk)


@pytest.mark.parametrize("member_names", [["retained", "added"], []])
def test_group_put_replaces_members(api_client, admin_headers, group, member_names):
    users = {name: User.objects.create(email=f"{name}@example.org", scim_id=f"user-{name}")
             for name in ("removed", "retained", "added")}
    for name in ("removed", "retained"):
        UserGroup.objects.create(user=users[name], group=group)
    response = api_client.put(f"{BASE}/Groups/{group.scim_id}", {
        "members": [{"value": users[name].scim_id} for name in member_names],
    }, format="json", **admin_headers)
    assert response.status_code == 200
    assert set(UserGroup.objects.filter(group=group).values_list("user__email", flat=True)) == {
        users[name].email for name in member_names
    }
    assert {member["value"] for member in response.json()["members"]} == {
        users[name].scim_id for name in member_names
    }
    removed = list(AuditLog.objects.filter(action="member_removed"))
    added = list(AuditLog.objects.filter(action="member_added"))
    assert len(removed) == 2
    assert {entry.before_state["user"] for entry in removed} == {users[name].email for name in ("removed", "retained")}
    assert len(added) == len(member_names)
    assert {entry.after_state["user"] for entry in added} == {users[name].email for name in member_names}
    assert all(entry.actor.email == "scim-admin@example.org" for entry in removed + added)
    assert not AuditLog.objects.filter(action="group_updated").exists()


@pytest.mark.parametrize("field, value", [
    ("name", "renamed"), ("description", "Replacement description"), ("externalId", "replacement-id"),
    ("tosId", "new_terms"), ("tosId", None),
])
def test_dataset_put_replaces_only_supplied_scalars(api_client, admin_headers, dataset, field, value):
    old_terms = TOSDocument.objects.create(name="Old", text="Old terms")
    new_terms = TOSDocument.objects.create(name="New", text="New terms")
    dataset.tos = old_terms
    dataset.save()
    ServiceTable.objects.create(dataset=dataset, service_name="svc", table_name="original")
    before_tables = list(ServiceTable.objects.values())
    value = new_terms.pk if value == "new_terms" else value
    expected = {"name": dataset.name, "description": dataset.description,
                "external_id": dataset.external_id, "tos_id": old_terms.pk}
    expected[{"externalId": "external_id", "tosId": "tos_id"}.get(field, field)] = value
    response = api_client.put(f"{BASE}/Datasets/{dataset.scim_id}", {field: value}, format="json", **admin_headers)
    assert response.status_code == 200
    dataset.refresh_from_db()
    assert {key: getattr(dataset, key) for key in expected} == expected
    body = response.json()
    assert body["schemas"] == [DATASET_SCHEMA]
    assert body["id"] == dataset.scim_id
    assert body["name"] == expected["name"]
    assert body["description"] == expected["description"]
    assert body["externalId"] == expected["external_id"]
    assert body.get("tosId") == expected["tos_id"]
    assert body["serviceTables"] == [{"serviceName": "svc", "tableName": "original"}]
    assert list(ServiceTable.objects.values()) == before_tables


@pytest.mark.parametrize("tables", [[], [
    {"serviceName": "neuprint", "tableName": "fixture:v1"},
    {"serviceName": "cave", "tableName": "fixture"},
]])
def test_dataset_put_replaces_service_table_resolution(api_client, admin_headers, dataset, tables):
    other = Dataset.objects.create(name="other")
    ServiceTable.objects.create(dataset=dataset, service_name="old", table_name="removed")
    moved = ServiceTable.objects.create(dataset=other, service_name="neuprint", table_name="fixture:v1")
    untouched = ServiceTable.objects.create(dataset=other, service_name="unrelated", table_name="keep")
    response = api_client.put(f"{BASE}/Datasets/{dataset.scim_id}", {"serviceTables": tables}, format="json", **admin_headers)
    assert response.status_code == 200
    assert not ServiceTable.objects.filter(service_name="old", table_name="removed").exists()
    assert {(row.service_name, row.table_name) for row in ServiceTable.objects.filter(dataset=dataset)} == {
        (entry["serviceName"], entry["tableName"]) for entry in tables
    }
    assert sorted(response.json()["serviceTables"], key=lambda row: row["serviceName"]) == sorted(tables, key=lambda row: row["serviceName"])
    for entry in tables:
        assert ServiceTable.objects.get(service_name=entry["serviceName"], table_name=entry["tableName"]).dataset == dataset
    moved.refresh_from_db()
    assert moved.dataset == (dataset if tables else other)
    untouched.refresh_from_db()
    assert untouched.dataset == other
    dataset.refresh_from_db()
    assert (dataset.name, dataset.description, dataset.external_id) == ("original", "Original description", "external-dataset")


@pytest.mark.parametrize("resource", ["Groups", "Datasets"])
def test_put_missing_resource_has_no_effect(api_client, admin_headers, group, dataset, resource):
    before = state()
    response = api_client.put(f"{BASE}/{resource}/missing", {}, format="json", **admin_headers)
    assert response.status_code == 404
    assert response.json()["status"] == "404"
    assert response.json()["detail"] == ("Group not found" if resource == "Groups" else "Dataset not found")
    assert state() == before


@pytest.mark.parametrize("credential", ["anonymous", "non_admin", "delegated"])
@pytest.mark.parametrize("operation", ["list_groups", "put_group", "put_dataset"])
def test_scim_access_rejection_does_not_mutate(api_client, group, dataset, credential, operation):
    user = User.objects.create(email="caller@example.org", admin=(credential == "delegated"))
    UserGroup.objects.create(user=user, group=group)
    ServiceTable.objects.create(dataset=dataset, service_name="keep", table_name="mapping")
    headers = {}
    if credential != "anonymous":
        registered = RegisteredClient.objects.create(name="Fixture client", origin="https://client.example.org", owner="Fixture")
        key = APIKey.objects.create(user=user, delegated_client=registered if credential == "delegated" else None)
        headers["HTTP_AUTHORIZATION"] = f"Bearer {key.key}"
    before = state()
    if operation == "list_groups":
        response = api_client.get(f"{BASE}/Groups", **headers)
    elif operation == "put_group":
        response = api_client.put(f"{BASE}/Groups/{group.scim_id}", {"displayName": "forbidden", "members": []}, format="json", **headers)
    else:
        response = api_client.put(f"{BASE}/Datasets/{dataset.scim_id}", {"name": "forbidden", "serviceTables": []}, format="json", **headers)
    assert response.status_code == 401
    assert response["WWW-Authenticate"] == "Bearer"
    assert set(response.json()) == {"detail"}
    assert state() == before
