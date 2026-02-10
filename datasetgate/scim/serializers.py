"""SCIM 2.0 serializers for User, Group, Dataset resources."""

from core.models import (
    Dataset,
    Group,
    GroupDatasetPermission,
    ServiceTable,
    User,
    UserGroup,
)

from .utils import format_datetime, generate_scim_id

# Schema URNs
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
USER_EXTENSION = "urn:ietf:params:scim:schemas:extension:neuroglancer:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
DATASET_SCHEMA = "urn:ietf:params:scim:schemas:neuroglancer:2.0:Dataset"


class UserSCIMSerializer:
    """Convert between User model and SCIM User resource."""

    @staticmethod
    def to_scim(user, base_url=""):
        scim_id = user.scim_id or generate_scim_id(user.pk, "User")

        groups = list(
            UserGroup.objects.filter(user=user)
            .select_related("group")
            .values_list("group__name", flat=True)
        )

        resource = {
            "schemas": [USER_SCHEMA, USER_EXTENSION],
            "id": scim_id,
            "userName": user.email,
            "name": {
                "formatted": user.public_name,
                "givenName": user.name,
                "familyName": "",
            },
            "displayName": user.public_name,
            "emails": [
                {"value": user.email, "type": "work", "primary": True}
            ],
            "active": user.is_active,
            "groups": [{"display": g, "value": g} for g in groups],
            USER_EXTENSION: {
                "admin": user.admin,
                "pi": user.pi,
                "gdprConsent": user.gdpr_consent,
                "serviceAccount": user.is_service_account,
            },
            "meta": {
                "resourceType": "User",
                "created": format_datetime(user.created),
                "lastModified": format_datetime(user.updated),
                "location": f"{base_url}/v2/Users/{scim_id}" if base_url else "",
            },
        }
        if user.external_id:
            resource["externalId"] = user.external_id
        return resource

    @staticmethod
    def from_scim(data, user=None):
        """Extract model fields from SCIM User data. Returns dict of fields."""
        fields = {}

        if "userName" in data:
            fields["email"] = data["userName"]

        name_data = data.get("name", {})
        if "givenName" in name_data:
            fields["name"] = name_data["givenName"]
        elif "formatted" in name_data:
            fields["name"] = name_data["formatted"]

        if "displayName" in data:
            fields["display_name"] = data["displayName"]

        if "active" in data:
            fields["is_active"] = data["active"]

        if "externalId" in data:
            fields["external_id"] = data["externalId"]

        # Extension fields
        ext = data.get(USER_EXTENSION, {})
        if "admin" in ext:
            fields["admin"] = ext["admin"]
        if "pi" in ext:
            fields["pi"] = ext["pi"]
        if "gdprConsent" in ext:
            fields["gdpr_consent"] = ext["gdprConsent"]

        return fields


class GroupSCIMSerializer:
    """Convert between Group model and SCIM Group resource."""

    @staticmethod
    def to_scim(group, base_url="", include_members=True):
        scim_id = group.scim_id or generate_scim_id(group.pk, "Group")

        resource = {
            "schemas": [GROUP_SCHEMA],
            "id": scim_id,
            "displayName": group.name,
            "meta": {
                "resourceType": "Group",
                "location": f"{base_url}/v2/Groups/{scim_id}" if base_url else "",
            },
        }

        if group.external_id:
            resource["externalId"] = group.external_id

        if include_members:
            members = []
            for ug in UserGroup.objects.filter(group=group).select_related("user"):
                member_scim_id = ug.user.scim_id or generate_scim_id(ug.user.pk, "User")
                members.append({
                    "value": member_scim_id,
                    "display": ug.user.public_name,
                    "$ref": f"{base_url}/v2/Users/{member_scim_id}" if base_url else "",
                })
            resource["members"] = members

        return resource

    @staticmethod
    def from_scim(data, group=None):
        """Extract model fields from SCIM Group data."""
        fields = {}

        if "displayName" in data:
            fields["name"] = data["displayName"]

        if "externalId" in data:
            fields["external_id"] = data["externalId"]

        return fields


class DatasetSCIMSerializer:
    """Convert between Dataset model and SCIM Dataset resource."""

    @staticmethod
    def to_scim(dataset, base_url=""):
        scim_id = dataset.scim_id or generate_scim_id(dataset.pk, "Dataset")

        service_tables = [
            {"serviceName": st.service_name, "tableName": st.table_name}
            for st in ServiceTable.objects.filter(dataset=dataset)
        ]

        resource = {
            "schemas": [DATASET_SCHEMA],
            "id": scim_id,
            "name": dataset.name,
            "description": dataset.description,
            "serviceTables": service_tables,
            "meta": {
                "resourceType": "Dataset",
                "location": f"{base_url}/v2/Datasets/{scim_id}" if base_url else "",
            },
        }

        if dataset.external_id:
            resource["externalId"] = dataset.external_id

        if dataset.tos_id:
            resource["tosId"] = dataset.tos_id

        return resource

    @staticmethod
    def from_scim(data, dataset=None):
        """Extract model fields from SCIM Dataset data."""
        fields = {}

        if "name" in data:
            fields["name"] = data["name"]

        if "description" in data:
            fields["description"] = data["description"]

        if "externalId" in data:
            fields["external_id"] = data["externalId"]

        if "tosId" in data:
            fields["tos_id"] = data["tosId"]

        return fields
