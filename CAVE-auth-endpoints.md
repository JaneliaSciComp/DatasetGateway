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

Tokens reach CAVE services via three mechanisms (checked in this order by `middle_auth_client`):

1. HTTP cookie: `middle_auth_token`
2. Authorization header: `Bearer {token}`
3. Query parameter: `?middle_auth_token=...`

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
  "name": "username",
  "email": "user@example.org",
  "admin": false,
  "groups": ["group1", "group2"],
  "datasets_admin": ["fish2", "fanc"],
  "permissions_v2": {
    "fish2": ["view", "edit"],
    "fanc": ["view"]
  },
  "permissions_v2_ignore_tos": {
    "fish2": ["view", "edit"]
  }
}
```

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

Google OAuth callback. Exchanges code for token, creates/updates user, sets `middle_auth_token` cookie (7-day TTL), redirects to original URL.

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

Implement the ~10 endpoints above with the same request/response contract. The `middle_auth_client` decorators don't care what's behind the URLs. This is the least disruptive path — no CAVE service code changes required, only deployment config (`AUTH_URL` env var).

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
