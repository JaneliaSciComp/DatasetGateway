# Architecture.md

## Overview

DatasetGateway provides a unified authorization layer that enforces terms of service and controls access to datasets across multiple services and tools. 

The system thinks of authorization in terms of permissions and grants. A `permission` is an abstract capability that describes what can be done (e.g., `dataset.read`) and lives in code/config. A `grant` is an assignment of permissions to a subject, scoped to an object (e.g., user Alice is granted `dataset.read` on dataset `fish2:v9`) and lives in a database.

DatasetGateway provides:

1. **Web app**
   - Google-login gated user experience
   - Dataset/version-specific **Terms of Service (TOS)** presentation + acceptance tracking
   - Team-lead flows to manage membership/grants within the team-lead grants
   - Admin console to manage all users/datasets/versions/grants/TOS

2. **Authorization API**
   - HTTP endpoints used by downstream systems (e.g., CAVE, WebKnossos, neuprint, Clio) to query authorization decisions and (optionally) obtain scoped credentials

3. **Neuroglancer ngauth service**
   - Implements Neuroglancer’s **ngauth server** behavior (token issuance for protected sources)
   - Uses **tos-ngauth-style** mechanics: Google auth, TOS gating, and GCS access based on policy
   - Supports multiple dataset “buckets” and dataset versions under a single service deployment

The implementation is a **single Django app** using **SQLite** initially (with an easy path to Postgres if needed), and integrates the proven ideas from `github.com/JaneliaSciComp/tos-ngauth`:
- TOS gating
- “activate” flow
- mapping dataset to a GCS bucket
- bucket permission management and/or token downscoping
- ngauth endpoints like `/token` and related helper endpoints

---

## Goals

- **Single source of truth** for dataset/version authorization, TOS acceptances, and user roles.
- **Multi-system integration** via stable HTTP APIs.
- **Neuroglancer compatibility** with minimal friction.
- **Minimal operational complexity** for initial deployment (SQLite, single service).
- Preserve the **working proof-of-concept behavior** from `tos-ngauth`, but extend it to handle:
  - multiple datasets
  - multiple versions per dataset
  - per-version grants and “all versions” grants
  - admin / team-lead control plane

---

## Non-Goals (for initial phase)

- Supporting non-Google identity providers.
- Running a full OAuth provider for other services (we provide policy + optionally scoped tokens).
- Fine-grained object-level ACL inside a dataset beyond dataset/version scope (can be added later if needed).

---

## High-Level System Design

### Control Plane + Data Plane in one Django service

This service contains both:
- **Control plane**: admin + management UI, DB models, audit trails, grant editing
- **Data plane**: low-latency authorization endpoints and ngauth endpoints used at request time

The design keeps the **ngauth token path stable** (e.g. `/token`) while making the service **dataset-aware** by deriving dataset context from the **bucket/source** being requested.

---

## Authentication & Identity

### Identity Provider
- All users authenticate via **Google** (OpenID Connect / OAuth2).
- The system stores the stable Google subject identifier (`google_sub`) and primary email.
- Email domain allow-listing can be enabled if desired (optional).

### Login Flow

The Neuroglancer flow (`/auth/login`) delegates to **django-allauth** for
OAuth. The CAVE flow (`/api/v1/authorize`) uses its own OAuth callback but
follows the same pattern. Both produce the same `dsg_token` cookie.

1. **Redirect to Google** — The user visits `/auth/login` (Neuroglancer
   flow) or `/api/v1/authorize` (CAVE flow). The server redirects to
   allauth's Google login (`/accounts/google/login/`) or directly to
   Google's authorization endpoint with `openid email profile` scopes.

2. **Google callback** — After consent, Google redirects back to the
   callback URL (`/accounts/google/login/callback/` for allauth, or
   `/api/v1/oauth2callback` for CAVE).

3. **User upsert** — allauth creates or matches the `User` record by
   email. The `SocialAccountAdapter.populate_user()` sets the user's
   name and display_name from Google profile data.

4. **API key creation** — The `AccountAdapter.login()` creates a new
   `APIKey` record and stashes the key value in the Django session.

5. **Cookie set** — The `DSGTokenCookieMiddleware` picks up the token
   from the session and sets the `dsg_token` cookie (HttpOnly,
   SameSite=Lax, 7-day TTL). The browser is redirected to the original
   destination.

Both flows produce the same `dsg_token` cookie backed by the same
`APIKey` table.

### Unified `dsg_token` Cookie

All authentication flows converge on a single cookie: **`dsg_token`**.

On login (via either the ngauth OAuth flow or the CAVE OAuth flow), the
server creates a DB-stored `APIKey` and sets `dsg_token` to its value.
All consumers — CAVE services, neuPrint, celltyping-light, clio-store,
and the web UI — read the same cookie. The DRF `TokenAuthentication`
class checks for the token in this order:

1. `dsg_token` cookie
2. `Authorization: Bearer {token}` header
3. `?dsg_token=` query parameter

It looks up the token in the `APIKey` table, returns the associated user,
and attaches the permission cache to the request.

### Cross-Origin HMAC Tokens (Neuroglancer Only)

Neuroglancer's cross-origin `/token` endpoint cannot use the `dsg_token`
cookie directly (different origin). Instead, `POST /token` reads the
`dsg_token` cookie, looks up the user via `APIKey`, then creates a
short-lived HMAC-signed token that Neuroglancer can pass to
`POST /gcs_token` for downscoped GCS access. This HMAC mechanism is
internal plumbing — no external consumer needs to know about it.

### Session Model
- Browser UI uses standard Django **session cookies** (secure, HttpOnly, same-site).
- API calls from other systems use either:
  - a service-to-service credential (recommended for server-side callers), or
  - a user token/session (if those systems are browser-facing and integrate with the same login)

(Exact choice depends on each downstream system; the service supports both patterns.)

---

## Authorization Model

Authorization is **dataset-version scoped** with optional wildcards.

### Core authorization concepts
- A **Dataset** has one or more **DatasetVersions**.
- A user can be granted permissions:
  - on a specific version, or
  - on a dataset wildcard (“all versions”)
- Special roles like “dataset creator” can implicitly map to “all versions”.

### Role categories (org roles)
Users can be any combination of:
- `admin`
- `sc` (steering committee)
- `team_lead`
- `user`

These roles govern who can **manage grants**, not necessarily who can **access data** (which is dataset/version permission based).

### Dataset permissions (data access)

Permissions follow a strict linear hierarchy: `admin` > `manage` > `edit` > `view`.
Each level implies all levels below it.

- `view` — read/access dataset
- `edit` — write/modify dataset
- `manage` — can manage grants within a group (team lead capability)
- `admin` — full dataset administration

These are seeded by `python manage.py seed_permissions`.

---

## Terms of Service (TOS)

TOS is tracked at the dataset/version level.

### Requirements
- Before access is granted, user must accept the applicable TOS.
- Acceptance is recorded with:
  - dataset + version (or dataset-wide, depending on policy)
  - TOS document/version identifier
  - timestamp
  - user identity

### Serving TOS
- The service hosts TOS pages similar to `tos-ngauth`:
  - dataset/version landing page
  - TOS content page(s)
  - accept/decline flow
- TOS documents can be stored as Markdown/HTML in the DB or referenced by URL/versioned assets.

---

## Storage & Database

### Initial DB: SQLite
- SQLite for the first deployment.
- Django migrations manage schema evolution.
- Plan to migrate to Postgres if concurrency/availability needs increase.

### Database tables

Items marked *(not yet implemented)* are design goals that don't exist
in the current codebase.

**User** (extends `AbstractBaseUser`)
- id
- google_sub (unique, nullable)
- email (unique) — `USERNAME_FIELD`
- name
- display_name
- admin (boolean) — maps to `is_staff` / `is_superuser`
- is_active (boolean)
- password (inherited from AbstractBaseUser, always unusable)
- last_login (inherited from AbstractBaseUser)
- gdpr_consent (boolean)
- pi (string)
- read_only (boolean)
- parent_id (FK to self, nullable — for service accounts)
- scim_id, external_id (SCIM 2.0 identifiers)
- created / updated
- db_table: `dsg_user`

**Group**
- id
- name (unique, e.g., admin, sc, team_lead, user)
- scim_id, external_id (SCIM 2.0 identifiers)

**UserGroup** (M:M through table)
- user (FK to User)
- group (FK to Group)
- is_admin (boolean)
- unique constraint on (user, group)

**Permission**
- id
- name (unique — `view`, `edit`, `manage`, `admin`)

**Dataset**
- id
- name (unique slug, e.g., "fish2")
- description
- tos (FK to TOSDocument, nullable)
- access_mode (`closed` or `public` — controls invite-only vs self-service TOS)
- scim_id, external_id (SCIM 2.0 identifiers)
- owner/creator user or group *(not yet implemented)*

**DatasetVersion**
- id
- dataset (FK to Dataset)
- version (string, e.g., "v1", "2026-01")
- description *(not yet implemented)*
- gcs_bucket (string)
- prefix (string, optional path within bucket)
- is_public (boolean)
- unique constraint on (dataset, version)

**Grant**
- id
- user (FK to User)
- dataset (FK to Dataset)
- dataset_version (FK to DatasetVersion, nullable — null means all versions)
- permission (FK to Permission, e.g., view, edit, manage, admin)
- group (FK to Group, nullable — if set, grant is scoped to this group)
- granted_by (FK to User, nullable)
- source (`manual` or `self_service` — tracks grant provenance)
- created

Both `Grant` and `GroupDatasetPermission` records are merged into the
permission cache returned by `/api/v1/user/cache`. This allows per-user
permissions without creating per-user groups — used by the clio-store
migration and the web UI's grant management.

**GroupDatasetPermission**
- group (FK to Group)
- dataset (FK to Dataset)
- permission (FK to Permission)
- unique constraint on (group, dataset, permission)

**ServiceTable**
- service_name (string)
- table_name (string)
- dataset (FK to Dataset)
- unique constraint on (service_name, table_name)

Maps a CAVE service/table pair to a dataset, used by
`GET /api/v1/service/{namespace}/table/{table_id}/dataset`.

**PublicRoot**
- service_table (FK to ServiceTable)
- root_id (bigint)
- unique constraint on (service_table, root_id)

**APIKey**
- user (FK to User)
- key (unique, indexed — random 64-char hex token)
- description (string)
- created
- last_used (nullable)

**TOSDocument**
- id
- name
- text (full terms text)
- dataset (FK to Dataset, nullable — null means global)
- dataset_version (FK to DatasetVersion, nullable — null means dataset-wide)
- invite_token (unique, auto-generated — unguessable token for TOS page URLs on closed datasets)
- tos_version string/hash *(not yet implemented)*
- content / url *(not yet implemented — currently text only)*
- effective_date
- retired_date (nullable)

**TOSAcceptance**
- id
- user (FK to User)
- tos_document (FK to TOSDocument)
- accepted_at
- ip_address (optional)
- user_agent *(not yet implemented)*
- unique constraint on (user, tos_document)

**AuditLog**
- actor (FK to User, nullable)
- action (string)
- target_type (string)
- target_id (string)
- before_state / after_state (JSON, nullable)
- timestamp

---

## GCS Authorization Strategy

This system preserves the operational behavior of `tos-ngauth` while
generalizing it. Dataset data is stored in Google Cloud Storage (GCS)
buckets. DatasetGateway controls who can read that data through two
mechanisms that can coexist.

### Mode 1: Bucket IAM membership ("activate" flow)

When a user accepts the TOS and calls `POST /activate` with a bucket
name, DatasetGateway adds the user's Google email to the bucket's IAM
policy with the `roles/storage.objectViewer` role. After IAM propagation
(which can take minutes), the user can access the bucket directly with
their own Google credentials.

- Pros: simple for tools that check bucket IAM directly
- Cons: IAM propagation delay; the user gets read access to the entire
  bucket, not just a specific prefix

### Mode 2: Downscoped tokens (Neuroglancer flow)

When Neuroglancer calls `POST /gcs_token`, DatasetGateway generates a
short-lived GCS access token that is restricted to a single bucket.
This uses Google's [Credential Access Boundaries](https://cloud.google.com/iam/docs/downscoping-short-lived-credentials)
(Security Token Service API):

1. DatasetGateway first verifies the user has `objectViewer` access on the
   bucket via a direct IAM policy check.
2. It then takes its own service account credential and exchanges it
   with Google's STS endpoint (`sts.googleapis.com/v1/token`) for a new
   token whose permissions are narrowed to `roles/storage.objectViewer`
   on just that one bucket.
3. The resulting token is returned to the browser. Neuroglancer uses it
   to read data directly from GCS. The token expires after a short
   period (set by Google, typically 1 hour).

This means the browser never receives a credential that can access
anything beyond the specific bucket, and the credential expires
automatically.

- Pros: tight scope, instant (no IAM propagation delay), short-lived
- Cons: requires the client to request new tokens periodically

### Dataset mapping

Each dataset version maps to a GCS bucket (and optionally a path
prefix). This mapping determines which bucket a user gets access to
when they are granted permission on a dataset version.

---

## Neuroglancer ngauth Integration

### Routing constraint
Neuroglancer typically expects a fixed token endpoint like:
- `https://auth.example.org/token`

We do **not** require `https://auth.example.org/<dataset>/token`.

### How dataset context is determined
The dataset context is derived from the Neuroglancer source URL scheme used in `tos-ngauth`, which includes the bucket:
- `precomputed://gs+ngauth+https://AUTH_SERVER/BUCKET/path/to/data`

The request arriving at `/token` includes enough context (bucket / resource) to map to:
- dataset version → required permissions → TOS requirement → token issuance policy

## API Endpoints

Endpoints marked with **ngauth** are required by the [ngauth protocol](https://github.com/google/neuroglancer/tree/master/ngauth_server). Others support the TOS/access provisioning flow.

| Method | Path | Description | Source |
|--------|------|-------------|--------|
| `GET` | `/` | Landing page with TOS | ngauth* |
| `GET` | `/health` | Health check | — |
| `GET` | `/auth/login` | Initiate OAuth (redirects to allauth) | — |
| `GET` | `/accounts/google/login/callback/` | OAuth callback (allauth) | — |
| `POST` | `/activate` | Provision access | — |
| `GET` | `/success` | Success page | — |
| `GET` | `/login` | Login status page | ngauth |
| `POST` | `/logout` | Logout | ngauth |
| `POST` | `/token` | Get user token (cross-origin) | ngauth |
| `POST` | `/gcs_token` | Get GCS access token | ngauth |

*ngauth's `/` just shows login status; ours extends it with TOS display.

## Authorization API for Other Systems

See the API endpoints necessary to be a [drop-in replacement for CAVE auth API endpoints](cave-auth-endpoints.md).

Downstream systems (CAVE/WebKnossos/neuprint/Clio) can use:

### Identity endpoints
- `GET /api/v1/whoami` → returns user identity + roles + org metadata

### Policy endpoints
- `GET /api/v1/datasets` → list datasets user can see (filtered)
- `GET /api/v1/datasets/<dataset>/versions` → list versions user can access
- `POST /api/v1/check-access` → evaluate access for a user to dataset/version with requested permission(s)
  - returns allow/deny + reason (e.g., "requires TOS acceptance", "grant missing", etc.)

Note: `/api/v1/authorize` is the CAVE OAuth flow endpoint, not the
policy check.

### Group endpoints
- `GET /api/v1/groups/<group_name>/members` → list email addresses of
  group members. Used by clio-store to scope annotation visibility
  (users see annotations from people in their groups).

### Token endpoints (optional for non-Neuroglancer systems)
- `POST /api/v1/token/gcs` → returns a downscoped token for dataset/version if allowed

---

## Web UI

### User UI
- Login via Google
- Browse datasets/versions available
- View TOS for dataset/version
- Accept TOS → triggers grant activation as configured
- View “My datasets” and acceptance history

### Team lead UI (team_lead / sc)
- Manage members within their scope:
  - grant/revoke access to dataset versions
  - grant “all versions” access
- View roster and current access

### Admin UI
Use Django Admin as the first implementation:
- Users, roles, datasets, versions, grants
- TOS documents and acceptances
- Audit log browsing
- Manual override tools (grant, revoke, deactivate)

---

## Deployment

### Initial deployment
- Single Django service (Gunicorn/uvicorn as appropriate)
- SQLite file storage (backed by persistent volume if needed)
- Config via environment variables + Django settings:
  - Google OAuth client id/secret
  - allowed domains (optional)
  - GCP project/service account for bucket IAM modifications
  - token signing keys and session secrets

### Scaling path
- Move SQLite → Postgres
- Add caching for authorization lookups if needed
- Separate the ngauth/token issuance endpoints behind a lightweight worker tier if they become hot-path

---

## Security Considerations

- Enforce HTTPS everywhere.
- Secure cookies (HttpOnly, Secure, SameSite=Lax/Strict).
- CSRF protection for browser flows.
- Strict validation of dataset/version identifiers and bucket mappings.
- Minimize GCP privileges for the service account:
  - only what is needed for IAM changes and/or token issuance.
- Audit log for all grant changes and admin actions.
- Rate limit token endpoints to reduce abuse.

---

## Operational Notes

- If using bucket IAM membership, expect propagation delays; communicate this in UI and API responses.
- Provide clear “deny reasons”:
  - not logged in
  - no grant
  - TOS required/not accepted
  - dataset/version retired
  - account disabled

---

## Open Questions / Decisions to Finalize

- Whether TOS acceptance is per dataset-version or dataset-wide (architecture supports both).
- Preferred integration patterns for each downstream system:
  - pure oracle calls vs token issuance
  - service-to-service auth method
- Whether to enforce dataset isolation via:
  - bucket-per-version (simple), or
  - prefix constraints (more complex, can be added later)
- Whether to keep “activate adds user to bucket IAM” as default or use downscoped tokens primarily.

---