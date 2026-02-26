# CAVE Authorization System: Drop-In Replacement Guide

This document describes the HTTP API surface of the CAVE authorization system (middle_auth) and what a replacement service must implement to be a drop-in substitute for CAVE services that depend on it.

## Architecture Overview

CAVE authorization is cleanly separated into two components:

1. **middle_auth** — The Flask server providing OAuth login, token management, user/group/permission CRUD, and token validation endpoints.
2. **middle_auth_client** — A Python library installed in every CAVE service, providing Flask decorators (`@auth_required`, `@auth_requires_permission`, etc.) that make HTTP callbacks to the middle_auth server on every request.

Every CAVE service delegates auth entirely through `middle_auth_client`. No service implements its own token validation or permission logic. The decorators call back to middle_auth, cache the result (TTL 300s by default), and gate access based on the response.

### Service Discovery

Services locate middle_auth via environment variables set in Kubernetes deployment config:

- **`AUTH_URL`** — Base URL of the auth server (e.g., `cave.example.org/auth`). Used by `middle_auth_client` decorators and by PyChunkedGraph's direct HTTP calls.
- **`STICKY_AUTH_URL`** — URL for OAuth browser redirects (may differ for sticky session routing). Used by AnnotationFrameworkInfoService.

Pointing CAVE services at a replacement is a deployment config change — update these env vars.

### Token Delivery

Tokens reach CAVE services via three mechanisms (checked in this order by DatasetGateway's `TokenAuthentication`):

1. HTTP cookie: `dsg_token`
2. Authorization header: `Bearer {token}`
3. Query parameter: `?dsg_token=...`

> **Note:** The original CAVE `middle_auth_client` uses `middle_auth_token` as the cookie and query param name. DatasetGateway unifies to `dsg_token`. CAVE services using `middle_auth_client` with `Bearer` header auth are unaffected; those relying on cookie or query param names need the updated client or config.

---

## Endpoints Required for CAVE Compatibility

A replacement must implement approximately 10 endpoints that CAVE services actually call. The remaining ~40 management endpoints in middle_auth are only used by its own admin UI and can use whatever API design fits the new platform.

### Critical Path: Token Validation and Permission Checking

Called on every authenticated request across all CAVE services via `middle_auth_client` decorators.

#### `GET /api/v1/user/cache`

The single most important endpoint. Every `@auth_required` and `@auth_requires_permission` decorator calls this to validate the token and retrieve the user's permissions.

**Request:**
- Header: `Authorization: Bearer {token}`

**Response (200):**
```json
{
  "id": 42,
  "parent_id": null,
  "service_account": false,
  "name": "username",
  "email": "user@example.org",
  "admin": false,
  "pi": "",
  "affiliations": [],
  "groups": ["group1", "group2"],
  "groups_admin": [],
  "permissions": {
    "fish2": 2,
    "fanc": 1
  },
  "permissions_v2": {
    "fish2": ["view", "edit"],
    "fanc": ["view"]
  },
  "permissions_v2_ignore_tos": {
    "fish2": ["view", "edit"]
  },
  "missing_tos": [],
  "datasets_admin": ["fish2"]
}
```

Field notes:
- `parent_id` / `service_account` — non-null when the token belongs to a service account (child of a human user)
- `permissions` — legacy v1 format mapping dataset name to a numeric level (0=none, 1=view, 2=edit); the max level across all permissions for that dataset
- `permissions_v2` — permissions filtered by TOS acceptance
- `permissions_v2_ignore_tos` — permissions regardless of TOS acceptance
- `missing_tos` — list of `{dataset_id, dataset_name, tos_id, tos_name}` for datasets where the user has permissions but hasn't accepted the required TOS
- `groups_admin` — groups where the user has the admin role
- `affiliations` — currently always `[]` (not yet implemented)

**Response (401):** Token invalid or expired.

**Caching:** `middle_auth_client` caches responses client-side for 300 seconds (configurable via `TOKEN_CACHE_TTL` env var, LRU size via `TOKEN_CACHE_MAXSIZE`, default 1024).

**Consumers:** Every CAVE service with auth decorators — AnnotationEngine, MaterializationEngine, PyChunkedGraph, SkeletonService, PCGL2Cache, NeuroglancerJsonServer, AnnotationFrameworkInfoService, ProofreadingProgress, guidebook, dash_on_flask.

#### `GET /api/v1/service/{namespace}/table/{table_id}/dataset`

Maps a service table to a dataset name. Called by `@auth_requires_permission` when a service needs to resolve which dataset a table belongs to for permission checking.

**Request:**
- Header: `Authorization: Bearer {token}`
- Path params: `namespace` (e.g., `"aligned_volume"`, `"datastack"`), `table_id` (string)

**Response (200):**
```json
"fish2"
```

**Consumers:** Services using `@auth_requires_permission` with `resource_namespace` — MaterializationEngine (~30 endpoints), AnnotationEngine, PyChunkedGraph, SkeletonService, PCGL2Cache, AnnotationFrameworkInfoService, guidebook, dash_on_flask.

---

### Utility Functions Called by Specific Services

#### `GET /api/v1/user/{user_id}/permissions`

Called by `users_share_common_group()` to check whether two users share a common group.

**Request:**
- Header: `Authorization: Bearer {service_token}`
- Path param: `user_id` (integer)

**Response (200):** User object with group membership.

**Consumers:** AnnotationEngine and MaterializationEngine only.

#### `GET /api/v1/username?id={id1},{id2},...`

Returns display names for a list of user IDs.

**Request:**
- Header: `Authorization: Bearer {token}`
- Query param: comma-separated user IDs

**Response (200):**
```json
[
  {"id": 42, "name": "alice"},
  {"id": 43, "name": "bob"}
]
```

**Consumers:** PyChunkedGraph only (direct HTTP call in `get_username_dict()`).

#### `GET /api/v1/user?id={id1},{id2},...`

Returns full user info for a list of user IDs.

**Request:**
- Header: `Authorization: Bearer {token}`
- Query param: comma-separated user IDs

**Response (200):** Array of user objects.

**Consumers:** PyChunkedGraph only (direct HTTP call in `get_userinfo_dict()`).

---

### Public Data Access

Called when checking whether unauthenticated users can access specific data.

#### `GET /api/v1/table/{table_id}/has_public`

Check if a table has any public entries.

**Request:** Header: `Authorization: Bearer {token}`

**Response (200):** Boolean.

**Cached:** 300 seconds in `middle_auth_client`.

#### `GET /api/v1/table/{table_id}/root/{root_id}/is_public`

Check if a specific root is public.

**Request:** Header: `Authorization: Bearer {token}`

**Response (200):** Boolean.

**Cached:** 300 seconds in `middle_auth_client`.

#### `POST /api/v1/table/{table_id}/root_all_public`

Batch check which roots are public.

**Request:**
- Header: `Authorization: Bearer {token}`, `Content-Type: application/json`
- Body: JSON array of root IDs

**Response (200):** Boolean or list of booleans.

**Cached:** 300 seconds in `middle_auth_client`.

---

### OAuth Flow (Browser-Facing)

Required for user login via browser.

#### `GET/POST /api/v1/authorize`

Initiates Google OAuth flow. Returns authorization URL (for programmatic clients via `X-Requested-With` header) or redirects browser.

**Query params:** `redirect` (return URL), `tos_id` (optional ToS to accept).

#### `GET /api/v1/oauth2callback`

Google OAuth callback. Exchanges code for token, creates/updates user, sets `dsg_token` cookie (7-day TTL), redirects to original URL.

#### `GET/POST /api/v1/logout`

Invalidates token and clears cookie.

**Consumers:** AnnotationFrameworkInfoService redirects users to `STICKY_AUTH_URL + /api/v1/logout`.

---

### Token Management (CAVEclient User-Facing)

Called by the CAVEclient Python library for programmatic token management.

#### `POST /api/v1/create_token`

Generate a new API token for the authenticated user.

**Request:** Header: `Authorization: Bearer {token}`

**Response (200):** New token string.

#### `GET /api/v1/user/token`

List all tokens for the current user.

#### `GET /api/v1/refresh_token`

Deprecated but still referenced in CAVEclient.

---

## SCIM 2.0 Provisioning Endpoints

The `scim` branch of middle_auth implements [SCIM 2.0](https://tools.ietf.org/html/rfc7643) (System for Cross-domain Identity Management) endpoints for machine-to-machine provisioning of users, groups, and datasets.

### Overview

SCIM 2.0 (RFC 7643 / RFC 7644) provides a standardized REST API for identity provisioning. These endpoints enable external identity providers (e.g., Okta, Azure AD) to automatically manage users, groups, and dataset assignments without using the admin UI.

### Base URL

```
/{URL_PREFIX}/scim/v2
```

Default: `/auth/scim/v2`

### Authentication

All SCIM endpoints require a Bearer token with super admin privileges, enforced by `scim_auth_required`:

```
Authorization: Bearer {admin_token}
```

### Discovery Endpoints

#### `GET /auth/scim/v2/ServiceProviderConfig`

Returns the SCIM service provider configuration, including supported features (patch, bulk, filter, sort, changePassword, etag).

#### `GET /auth/scim/v2/ResourceTypes`

Returns the list of supported resource types: `User`, `Group`, and `Dataset` (custom).

#### `GET /auth/scim/v2/Schemas`

Returns full JSON schemas for all supported resource types.

### User CRUD

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/auth/scim/v2/Users` | List users (with filtering and pagination) |
| `GET` | `/auth/scim/v2/Users/{scim_id}` | Get a single user |
| `POST` | `/auth/scim/v2/Users` | Create a new user |
| `PUT` | `/auth/scim/v2/Users/{scim_id}` | Replace a user (full update) |
| `PATCH` | `/auth/scim/v2/Users/{scim_id}` | Partially update a user |
| `DELETE` | `/auth/scim/v2/Users/{scim_id}` | Deactivate a user |

**Schema URNs:**
- `urn:ietf:params:scim:schemas:core:2.0:User`
- `urn:ietf:params:scim:schemas:extension:neuroglancer:2.0:User` (custom extension with `admin`, `pi`, `gdprConsent`, `serviceAccount` fields)

**Key field mappings:**
- `userName` → `email`
- `displayName` / `name.formatted` → `name`
- `active` → `is_active` (deactivation only; no hard delete)
- `externalId` → `external_id`
- `id` → `scim_id` (UUID5 deterministic from internal ID)

### Group CRUD

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/auth/scim/v2/Groups` | List groups (with filtering and pagination) |
| `GET` | `/auth/scim/v2/Groups/{scim_id}` | Get a single group (includes members) |
| `POST` | `/auth/scim/v2/Groups` | Create a new group |
| `PUT` | `/auth/scim/v2/Groups/{scim_id}` | Replace a group (full update) |
| `PATCH` | `/auth/scim/v2/Groups/{scim_id}` | Partially update a group (add/remove members) |
| `DELETE` | `/auth/scim/v2/Groups/{scim_id}` | Delete a group |

**Schema URN:** `urn:ietf:params:scim:schemas:core:2.0:Group`

**Key field mappings:**
- `displayName` → `name`
- `members` → `UserGroup` M:M relationship (each member has `value` = user SCIM ID and `display` = user name)

### Dataset CRUD (Custom Resource)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/auth/scim/v2/Datasets` | List datasets (with filtering and pagination) |
| `GET` | `/auth/scim/v2/Datasets/{scim_id}` | Get a single dataset |
| `POST` | `/auth/scim/v2/Datasets` | Create a new dataset |
| `PUT` | `/auth/scim/v2/Datasets/{scim_id}` | Replace a dataset (full update) |
| `PATCH` | `/auth/scim/v2/Datasets/{scim_id}` | Partially update a dataset |
| `DELETE` | `/auth/scim/v2/Datasets/{scim_id}` | Delete a dataset |

**Schema URN:** `urn:ietf:params:scim:schemas:neuroglancer:2.0:Dataset` (custom)

**Key field mappings:**
- `name` → `name` (dataset slug)
- `tosId` → `tos_id` (FK to TOS document)
- `serviceTables` → associated `ServiceTable` records (service name + table name pairs)

### ID Mapping

SCIM IDs are deterministic UUID5 values generated from internal integer IDs:

```python
uuid5(SCIM_NAMESPACE, f"{resource_type}:{internal_id}")
```

Where `SCIM_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")`.

Each resource also supports an `externalId` field for mapping to the identity provider's internal ID. Lookup priority: `externalId` first, then `scim_id`.

### Filtering

Supports RFC 7644 filter expressions on list endpoints via the `filter` query parameter:

```
GET /auth/scim/v2/Users?filter=userName eq "user@example.org"
GET /auth/scim/v2/Groups?filter=displayName co "admin"
```

Supported operators: `eq`, `ne`, `co` (contains), `sw` (starts with), `ew` (ends with), `pr` (present), `gt`, `ge`, `lt`, `le`. Logical operators `and`, `or`, and `not` are supported.

### Pagination

SCIM uses 1-based pagination with `startIndex` and `count` parameters:

```
GET /auth/scim/v2/Users?startIndex=1&count=50
```

- `startIndex` defaults to 1 (1-based index)
- `count` defaults to 100, max 1000
- `count=0` returns only `totalResults` (per RFC 7644 §3.4.2.4)

**Response format:**
```json
{
  "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
  "totalResults": 150,
  "itemsPerPage": 50,
  "startIndex": 1,
  "Resources": [...]
}
```

### Error Response Format

SCIM errors use `application/scim+json` content type:

```json
{
  "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
  "status": "404",
  "scimType": "invalidValue",
  "detail": "Resource not found"
}
```

### New Model Fields

The SCIM implementation adds the following fields to existing models:

| Model | Field | Type | Description |
|-------|-------|------|-------------|
| User | `scim_id` | String(36), unique, indexed | UUID5 SCIM identifier |
| User | `external_id` | String(255), unique, indexed | External system identifier |
| Group | `scim_id` | String(36), unique, indexed | UUID5 SCIM identifier |
| Group | `external_id` | String(255), unique, indexed | External system identifier |
| Dataset | `scim_id` | String(36), unique, indexed | UUID5 SCIM identifier |
| Dataset | `external_id` | String(255), unique, indexed | External system identifier |

---

## Endpoints NOT Required for CAVE Service Compatibility

The following endpoint groups are only called by middle_auth's own admin UI. A replacement can implement equivalent functionality with any API design:

- **Group CRUD** — `POST/GET/PUT/DELETE /api/v1/group/...`
- **Dataset CRUD** — `POST/GET/PUT/DELETE /api/v1/dataset/...`
- **Permission CRUD** — `POST/GET/PUT /api/v1/permission/...`
- **Service account management** — `POST/GET/PUT/DELETE /api/v1/service_account/...`
- **Terms of Service management** — `POST/GET/PUT /api/v1/tos/...`
- **Service-table-dataset mapping management** — `POST/DELETE /api/v1/service/{service}/table/{table}/dataset/{dataset}`
- **Redis debugging** — `GET /api/v1/redis/...`
- **User admin operations** — `POST/PUT/DELETE /api/v1/user/...` (the admin CRUD, not `/user/cache` which is critical)

---

## Decorator Reference

These are the `middle_auth_client` decorators used by CAVE services. A replacement that keeps the `middle_auth_client` library (or a fork) needs to satisfy the HTTP calls these decorators make.

| Decorator | HTTP Calls | Used By |
|-----------|-----------|---------|
| `@auth_required` | GET `/api/v1/user/cache` | All 10 services |
| `@auth_requires_permission(perm, table_arg, resource_namespace)` | GET `/api/v1/user/cache` + GET `/api/v1/service/{ns}/table/{id}/dataset` | MaterializationEngine (30+), PyChunkedGraph, SkeletonService, AnnotationEngine, PCGL2Cache, AnnotationFrameworkInfoService, guidebook, dash_on_flask |
| `@auth_requires_admin` | GET `/api/v1/user/cache` (checks `admin` field) | MaterializationEngine, AnnotationEngine, AnnotationFrameworkInfoService, NeuroglancerJsonServer |
| `@auth_requires_dataset_admin` | GET `/api/v1/user/cache` + GET `/api/v1/service/{ns}/table/{id}/dataset` (checks `datasets_admin`) | MaterializationEngine (2 endpoints) |
| `@auth_requires_group(group)` | GET `/api/v1/user/cache` (checks `groups` field) | Not currently used by any service |

---

## Replacement Strategies

### Strategy A: Replace the server, keep middle_auth_client

Implement the ~10 endpoints above with the same request/response contract. The `middle_auth_client` decorators don't care what's behind the URLs. This is the least disruptive path — no CAVE service code changes required, only deployment config (`AUTH_URL` env var). Note: the SCIM 2.0 endpoints add ~18 additional provisioning endpoints (3 discovery + 5 per resource type × 3 resource types) for machine-to-machine management.

### Strategy B: Replace both server and client

If you want to change the auth model (e.g., local JWT validation instead of per-request HTTP callback to `/user/cache`), fork `middle_auth_client` to do local token validation. This eliminates the HTTP round-trip on every request but requires updating the dependency in every CAVE service.

---

## Multi-Dataset / Multi-Platform Routing

A central auth service handling multiple CAVE deployments (and non-CAVE services) needs to distinguish which dataset a request belongs to without endpoint conflicts.

### Recommended: Path prefix routing

Add the dataset and service type as a path prefix. The original `/api/v1/...` paths remain unchanged beneath it:

```
basedomain.org/fish2/cave/api/v1/user/cache
basedomain.org/fanc/cave/api/v1/user/cache
basedomain.org/other-service/custom/api/v1/...
```

Each CAVE deployment sets its `AUTH_URL` to include the prefix:

```
AUTH_URL=basedomain.org/fish2/cave
```

The auth service parses the prefix to determine dataset context. Non-CAVE services get their own prefixes with no conflict.

**Advantages:**
- Single domain, single TLS certificate
- Standard reverse proxy routing
- `AUTH_URL` already supports path prefixes — no code changes needed
- Fork of `middle_auth_client` not required if proxy strips the prefix before forwarding

### Alternative: Reverse proxy with prefix stripping

Keep paths identical (`/api/v1/...`) and use separate `AUTH_URL` values per deployment. A reverse proxy routes based on prefix and injects the dataset context as a header:

```
AUTH_URL=basedomain.org/fish2  → proxy strips /fish2, adds X-Dataset: fish2 header
AUTH_URL=basedomain.org/fanc   → proxy strips /fanc, adds X-Dataset: fanc header
```

The auth service receives unmodified `/api/v1/...` paths with dataset context in a header. No CAVE code changes or `middle_auth_client` fork required.

### Not recommended: Subdomain encoding

Encoding the dataset in the subdomain (`fish2-cave.basedomain.org`) works but requires wildcard DNS records, wildcard TLS certificates, and subdomain parsing logic. The path-based approaches are simpler operationally.

---

## Source Code References

- Auth server: [middle_auth](https://github.com/CAVEconnectome/middle_auth)
- Client library: [middle_auth_client](https://github.com/CAVEconnectome/middle_auth_client)
- Token validation and caching: `middle_auth_client/decorators.py` — `user_cache_http()`, `@auth_required`
- Permission checking: `middle_auth_client/decorators.py` — `@auth_requires_permission`, `dataset_from_table_id()`
- PyChunkedGraph direct calls: `pychunkedgraph/app/app_utils.py` — `get_username_dict()`, `get_userinfo_dict()`
- CAVEclient endpoint definitions: `caveclient/endpoints.py` — `auth_endpoints_v1`
- Environment variable configuration: Set via Kubernetes deployment config (see [CAVEdeployment](https://github.com/CAVEconnectome/CAVEdeployment))
