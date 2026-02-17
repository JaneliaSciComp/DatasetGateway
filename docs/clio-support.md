# Clio (clio-store) Integration with DatasetGate

Clio (clio-store) is a FastAPI service managing neuroscience annotations,
dataset metadata, saved searches, and body annotations. It stores data in
Firestore, BigQuery, and GCS.

This document describes how clio-store integrates with DatasetGate for
authentication and authorization.

---

## How it works

When the `DSG_URL` environment variable is set, clio-store delegates all
user authentication and authorization to DatasetGate. The flow is:

1. A request arrives at clio-store with a token (via `Authorization:
   Bearer` header, `dsg_token` cookie, or `?dsg_token=` query parameter)
2. clio-store calls `GET {DSG_URL}/api/v1/user/cache` with that token
3. DatasetGate validates the token and returns the user's identity,
   groups, and permissions
4. clio-store maps the DatasetGate response to its internal `User` model
   and proceeds with the request

When `DSG_URL` is not set, clio-store uses its legacy auth path (FlyEM
JWT / Google OAuth2 token validation with Firestore `clio_users`).

---

## Permission mapping

DatasetGate uses `view` and `edit` permissions per dataset. clio-store
uses its own role names internally. The mapping is:

| DatasetGate | clio-store equivalent | Notes |
|---|---|---|
| `admin: true` | `global_roles: {"admin"}` | Full system admin, bypasses all checks |
| `permissions_v2[ds]` includes `"view"` | `datasets[ds]` includes `"clio_general"` | Read + write own annotations |
| `permissions_v2[ds]` includes `"edit"` | `datasets[ds]` includes `"clio_write"` | Write others' annotations |
| `datasets_admin` includes `ds` | `datasets[ds]` includes `"dataset_admin"` | Per-dataset admin (can delete annotations) |
| `groups` list | `groups` set | Used for annotation visibility scoping |

clio-store's legacy `clio_general` as a *global* role (meaning access to
all datasets) has no DatasetGate equivalent. Per-dataset permissions plus
the Firestore `public` flag cover all existing behavior. Admin users
bypass all checks regardless.

---

## The `public` flag

### What it does

Some datasets in clio-store are marked `public: true` in their Firestore
`clio_datasets` document. When a dataset is public, any authenticated
user can read it and write their own annotations, even without explicit
DatasetGate permissions.

This is checked by three methods on clio-store's `User` model:
- `can_read(dataset)` -- returns `True` if the dataset is public
- `can_write_own(dataset)` -- returns `True` if the dataset is public
- `has_role("clio_general", dataset)` -- returns `True` if the dataset
  is public

### Where the flag lives

The `public` flag is part of dataset metadata in Firestore's
`clio_datasets` collection, alongside DVID URLs, neuroglancer config,
layer definitions, and other dataset configuration. It is NOT stored in
DatasetGate.

clio-store's `DatasetCache` reads all dataset metadata (including
`public`) from Firestore on startup and refreshes every 10 minutes.
This continues to work unchanged with DatasetGate auth enabled.

### Two sources of access decisions

With DatasetGate integration, a user's access to a dataset is determined
by two sources:

1. **DatasetGate permissions** -- explicit `view`/`edit` grants or group
   permissions, returned in `permissions_v2`
2. **Firestore `public` flag** -- if `true`, all authenticated users get
   implicit read + write-own access

This means:
- A user with no DatasetGate permissions can still access a public
  dataset
- To fully restrict a dataset, an admin must both remove DatasetGate
  permissions AND set `public: false` in Firestore
- The `public` flag is a property of the dataset metadata, not an auth
  record -- it lives with DVID URLs, layers, and neuroglancer config

### Migration consideration

During migration from Firestore auth to DatasetGate, public datasets
are handled by creating a `GroupDatasetPermission` granting `view` to
the `user` group (which all DatasetGate users belong to). This mirrors
the Firestore `public` behavior in DatasetGate's permission system.
However, the Firestore `public` flag still governs clio-store's
`can_write_own()` behavior independently.

---

## Group members

clio-store uses groups to scope annotation visibility. When a user
queries annotations, they see annotations from users who share at least
one group with them. This is implemented by `annotations_v2.py` and
`annotations_v3.py` calling `group_members(user, groups)`.

With DatasetGate, group membership is fetched from
`GET {DSG_URL}/api/v1/groups/{group_name}/members`, which returns a list
of email addresses. Results are cached for 10 minutes.

---

## User management endpoints

When `DSG_URL` is set, clio-store's user management endpoints
(`GET/POST/DELETE /v2/users`) return HTTP 501 with a message directing
admins to DatasetGate. All user and role management happens through
DatasetGate's web UI or Django admin panel.

---

## Token generation

When `DSG_URL` is set, `POST /v2/server/token` proxies to DatasetGate's
`POST /api/v1/create_token` endpoint, returning a DatasetGate API key
instead of a FlyEM JWT. Existing clients that call this endpoint
continue to work -- they receive a `dsg_token` that works across all
DatasetGate-integrated services.

---

## Configuration

| Environment variable | Required | Description |
|---|---|---|
| `DSG_URL` | Yes | Base URL of the DatasetGate server (e.g., `https://datasetgate.example.org`). When unset, clio-store uses legacy Firestore auth. |
| `AUTH_COOKIE_DOMAIN` | Recommended | Set on DatasetGate to share the `dsg_token` cookie across subdomains (e.g., `.janelia.org`). When configured, users log in once and are authenticated across clio-store, CAVE, Neuroglancer, etc. |

All other clio-store configuration (Firestore collections, DVID URLs,
etc.) remains unchanged.

---

## Migration from Firestore auth

### Steps

1. **Export** Firestore auth data using `scripts/export_auth.py` in the
   clio-store repo:
   ```bash
   python scripts/export_auth.py exported_auth.json
   ```
   This reads `clio_users` and `clio_datasets` from Firestore and writes
   a JSON file.

2. **Import** into DatasetGate using the management command:
   ```bash
   cd datasetgate
   python manage.py import_clio_auth exported_auth.json
   ```
   This creates User, Dataset, Grant, Group, and UserGroup records. Use
   `--dry-run` to preview without writing.

3. **Deploy** clio-store with `DSG_URL` set to the DatasetGate URL.

4. Firestore `clio_users` becomes legacy data. It can be deleted once
   migration is verified.

### What the import creates

For each Firestore user:
- A `User` record (email, name, admin flag, active status)
- `Grant` records mapping clio-store roles to DatasetGate permissions
- `UserGroup` memberships from the user's groups

For each Firestore dataset:
- A `Dataset` record
- If `public: true`, a `GroupDatasetPermission` granting `view` to the
  `user` group

### What stays in Firestore

- All dataset metadata (`clio_datasets`): DVID URLs, layers,
  neuroglancer config, the `public` flag, etc.
- All annotation data, saved searches, key-value stores, etc.
- Only `clio_users` (user auth/roles) moves to DatasetGate
