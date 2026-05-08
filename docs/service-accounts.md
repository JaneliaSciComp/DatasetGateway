# Service Accounts

Service accounts are non-human identities that hold long-lived API tokens
for programmatic access to DatasetGateway-protected datasets. They are
inspired by GCP IAM service accounts: an admin creates a named identity,
mints one or more bearer tokens for it, and grants it dataset privileges
the same way a human user is granted them.

A service account is **not a user**. It does not log in through Google,
does not accept terms of service, does not belong to groups, and does
not appear in any of the DSG-integrated end-user UIs.

---

## What a service account is for

Use a service account when a non-human client needs to call
DatasetGateway-protected endpoints over a long period:

- A CI job that needs to read dataset versions during automated tests.
- A backend service (CAVE microservice, ingest pipeline, scheduled
  exporter) that calls `/api/v1/check-access`, `/api/v1/datasets`, or
  related endpoints on behalf of itself, not on behalf of an end user.
- A shared `curl` or scripting credential that should outlive any
  individual operator.

The token is a bearer credential: anyone who holds it can act as the
service account. Treat it like a password.

---

## What a service account can do

- **Authenticate** to DSG endpoints by sending its token as
  `Authorization: Bearer <token>`, the `dsg_token` cookie, or the
  `?dsg_token=` query parameter. The same `core/authentication.py`
  `TokenAuthentication` class that resolves human `APIKey` tokens
  resolves service-account tokens.
- **Hold dataset privileges** at the same permission levels users do
  (`view`, `edit`, `manage`, `admin`), optionally scoped to a specific
  dataset version.
- **Be granted** by a global admin from either the service-account
  detail page (`/web/service-accounts/<name>`) or from the dataset's
  grants page (`/web/grants/<dataset>`).
- **Hold multiple tokens.** Mint a new token before deploying it,
  revoke the old one after — zero-downtime rotation.
- **Be disabled.** Setting `is_active=False` blocks all of its tokens
  immediately without deleting them, so the account can be re-enabled
  later.

## What a service account cannot do

- **Log in via Google OAuth.** Service accounts have no `google_sub`
  and never go through the OAuth flow.
- **Accept or be gated by TOS.** Direct service-account grants override
  TOS for that service account; if you do not want a service account to
  see a dataset, do not grant it.
- **Belong to groups.** The single description field replaces both
  group membership and organizational affiliation.
- **Mint user `APIKey`s** (`POST /api/v1/create_token`,
  `GET /api/v1/long_lived_token`, etc.). The `IsHumanUser` permission
  class on those endpoints rejects service-account principals so an SA
  cannot create or list tokens belonging to a User row.
- **Receive ngauth GCS tokens.** Browser-mediated bucket access via
  `ngauth` is human-only. Service accounts call DSG endpoints directly
  with their bearer token; they do not get per-user GCS IAM bindings.
- **Be a global admin.** `admin` is hard-coded to `False` on
  ServiceAccount.
- **Be created or managed by anyone except a global admin.** All
  service-account web routes require `User.admin = True`.
- **Have privileges inherited from any human.** A service account's
  access is exactly the union of its `ServiceAccountGrant` rows —
  nothing else.

---

## Managing service accounts

All service-account UIs are at `/web/service-accounts/` and are
admin-only.

| Action | Where | Notes |
|---|---|---|
| Create | `/web/service-accounts` | Name (slug, unique) + description |
| List   | `/web/service-accounts` | Shows token count, grant count, status |
| Edit description | `/web/service-accounts/<name>` | |
| Disable / enable | `/web/service-accounts/<name>` | Toggle `is_active` — tokens stop working when disabled |
| Mint a new token | `/web/service-accounts/<name>` | Plaintext is shown **once** on creation; copy it then |
| Revoke a token | `/web/service-accounts/<name>` | Hard-delete; cannot be recovered |
| Add dataset grant | `/web/service-accounts/<name>` **or** `/web/grants/<dataset>` | Both pages support this |
| Revoke dataset grant | Same | |
| Delete the service account | `/web/service-accounts/<name>` (Danger Zone) | Requires typing the name to confirm; cascades all tokens and grants |

The Django admin (`/admin/`) also exposes the three models for back-office
visibility.

Every mutation is recorded in `AuditLog` with the human admin as
`actor`. The new `target_type` values are `ServiceAccount`,
`ServiceAccountToken`, and `ServiceAccountGrant`.

---

## Using a service-account token

```bash
TOKEN="<the plaintext you copied at mint time>"

# Identify yourself
curl -H "Authorization: Bearer $TOKEN" https://dsg.example.org/api/v1/whoami
# {"id": 7, "email": "ci-bot@service-account.dsg.local", "name": "ci-bot",
#  "admin": false, "service_account": true, ...}

# List datasets the SA can see
curl -H "Authorization: Bearer $TOKEN" https://dsg.example.org/api/v1/datasets

# Ask for an access decision
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"dataset": "fish2", "permission": "view"}' \
     https://dsg.example.org/api/v1/check-access
```

The synthetic email `<name>@service-account.dsg.local` is an internal
identifier only. It is never sent to Google IAM, GCS, or any external
system.

---

## Identifying a service account from a downstream service

Downstream services that delegate auth to DSG (Clio, neuprint, CAVE
microservices via `middle_auth_client`) call `GET /api/v1/user/cache`
or `GET /api/v1/whoami` with the bearer token. The response shape is
identical for users and service accounts — three fields let the
service identify the SA:

| Field | For a user | For a service account |
|---|---|---|
| `service_account` | `false` | `true` |
| `name` | display name or email prefix | the SA slug (e.g. `"ci-bot"`) — exactly `ServiceAccount.name`, immutable in practice |
| `id` | `User.pk` | `ServiceAccount.pk` |
| `email` | real Google email | synthetic `<name>@service-account.dsg.local` |

Recommendations:

- **Primary identifier: `name`.** It's the slug the admin sees in the
  UI, human-readable, and stable.
- **Always disambiguate with `service_account`.** On the wire, `id` is
  a plain integer for both Users and SAs — they share the integer
  namespace. Treating `id` alone as a globally unique principal key
  will collide. Use `(service_account, id)` or just `name` when
  `service_account` is true.
- **`email` is stable too.** The `service-account.dsg.local` suffix is
  a reliable marker, useful if your existing code already keys
  attribution on email.

```python
cache = dsg.get("/api/v1/user/cache", token=token).json()

if cache["service_account"]:
    principal = f"sa:{cache['name']}"        # "sa:ci-bot"
else:
    principal = cache["email"]               # "alice@example.org"

log_attribution(principal)
```

---

## Architectural decisions

### Service accounts are a separate model, not a kind of User

The codebase already had partial infrastructure for "User row with a
`parent` FK" service accounts (`User.parent`, `User.is_service_account`).
We rejected reusing it. The User model is shaped by Google-OAuth identity
(`google_sub`, `email`, `picture_url`), TOS acceptance, group
membership, and DSG-service login state — none of which apply to a
service account. Reusing User would mean nullable fields that "mean N/A"
rather than expressing the actual semantics, and would risk a service
account accidentally going through OAuth, TOS, or group flows.

Three new models live alongside the user-grant models in
`dsg/core/models.py`:

- `ServiceAccount` — name (slug), description, is_active, created_by,
  created, updated.
- `ServiceAccountToken` — FK to SA, key (auto-generated 64-char hex),
  description, created, last_used. **No `expires_at`** — long-lived by
  definition.
- `ServiceAccountGrant` — FK to SA, dataset, optional dataset_version,
  permission, granted_by. Mirrors `Grant` for SAs.

The legacy `User.parent` and `User.is_service_account` fields are kept
for back-compat with any pre-existing parent-linked rows; new service
accounts never use them.

### `request.user` carries the principal; ServiceAccount duck-types as User

`TokenAuthentication.authenticate()` returns `(ServiceAccount, token)`
when an SA token is presented, and DRF stores the `ServiceAccount`
instance as `request.user`. The model defines just enough class-level
attributes (`is_authenticated = True`, `admin = False`,
`is_service_account = True`, `parent_id = None`, `email` property,
`public_name` property, etc.) for downstream code to consume `request.user`
without crashing.

The risk of this approach is that code paths which pass `request.user`
to a Django ORM filter against the `dsg_user` table (`Grant.objects.filter(user=request.user)`)
would either crash or, worse, silently match a `User` row whose pk
happened to equal the SA's pk. We mitigate this in two ways:

- A new `IsHumanUser` permission class in `dsg/core/permissions.py`
  rejects SA principals at endpoints that mint or list per-user
  `APIKey` rows (`/api/v1/create_token`, `/api/v1/long_lived_token`,
  `/api/v1/user/token`, `/api/v1/refresh_token`, `/api/v1/logout`).
- Every endpoint in `dsg/auth_api/views.py` that resolves access for a
  principal explicitly branches on `isinstance(request.user, ServiceAccount)`
  and queries `ServiceAccountGrant` instead of `Grant` /
  `GroupDatasetPermission`.

### The permission cache shape is identical for users and service accounts

`build_permission_cache` in `dsg/core/cache.py` dispatches on principal
type. The SA path queries `ServiceAccountGrant` only, with no group
merge, no TOS gating, and no `datasets_admin` resolution. But it returns
the **same dict shape** as the User path — `service_account: True`,
`parent_id: None`, `admin: False`, `groups: []`, `groups_admin: []`,
`missing_tos: []`, `datasets_admin: []`, `affiliations: []` — so
downstream consumers (especially CAVE's `middle_auth_client`) see no
schema surprise.

Cache keys are namespaced (`dsg_auth_sa_<pk>`) so an SA pk colliding
with a User pk does not poison either cache.

### Auth surfaces that are deliberately no-ops for service accounts

- **`ngauth`** issues GCS tokens by looking up `APIKey` directly, so
  service-account tokens simply do not match. SAs are never given GCS
  bucket IAM bindings.
- **`scim`** requires `request.user.admin == True`, which is hard-coded
  False on `ServiceAccount`.
- **`web`** views resolve the session user via `_get_web_user()` against
  the User table, which a service-account token cannot satisfy. SAs
  cannot reach any web page.
- **`core/iam.py`** (per-user GCS bucket IAM sync) is only called from
  the user-grant create/revoke paths in `web/views.py`, never from the
  service-account-grant paths. Service-account access is enforced at
  the gateway, not at the bucket.

### Lifecycle: disable is reversible, delete cascades

Disable (`is_active=False`) is the soft kill: every token for the SA is
rejected at auth time with the message "Service account is disabled,"
but the rows remain so the SA can be re-enabled later. Delete is the
hard kill: cascade removes all tokens and grants. The web UI requires
typing the SA's name to confirm a delete.

### Audit log

The acting human admin is always recorded as `AuditLog.actor`. The new
`target_type` values are `ServiceAccount`, `ServiceAccountToken`, and
`ServiceAccountGrant`. Service accounts themselves never appear as
`actor` — they are always the target, never the agent of administrative
mutations.
