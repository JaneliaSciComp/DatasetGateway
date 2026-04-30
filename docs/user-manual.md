# DatasetGateway User Manual

## Roles

DatasetGateway uses a hierarchical role model. Each role has a specific
scope of access and management capability.

| Role | How to assign | Dataset scope | User scope |
|------|--------------|---------------|------------|
| **Global admin** | `python manage.py make_admin user@example.com` (sets `user.admin=True`) | All datasets | All users |
| **Dataset admin (SC)** | Grant with `admin` permission on a dataset | Assigned datasets only | All users on that dataset |
| **Team lead** | Grant with `manage` permission on a dataset + `UserGroup.is_admin=True` | Datasets where they have `manage` | Only users in their group |
| **Regular user** | No special role | N/A | N/A |

A **Django superuser** (created via `python manage.py createsuperuser`) is
a special case: it has `admin=True` plus a usable password for the Django
admin console at `/admin/`. The password is only used for the admin console
— all other login flows use Google OAuth.

### Permission hierarchy

Permissions follow a strict linear hierarchy: `admin` > `manage` > `edit` > `view`.
Each level implies all levels below it. For example, a user with `manage`
permission on a dataset automatically has `edit` and `view` as well.

### Group-scoped grants

Grants can optionally be scoped to a group. When a team lead creates a grant,
it is associated with their group. This means:
- Team lead A cannot see or revoke grants created by team lead B (different group)
- Dataset admins (SC) can see all grants across all groups on their datasets
- Global admins can see everything

A typical setup uses a global admin for initial configuration (creating
datasets, groups, permissions), dataset admins (SC) for dataset-level
management, and team leads for day-to-day user access within their groups.

### How services use DatasetGateway

DatasetGateway is the central auth layer for multiple platforms. Each
platform authenticates users through DatasetGateway but in slightly
different ways:

**CAVE services** — CAVE is a set of microservices for connectomics
annotation (MaterializationEngine, AnnotationEngine, PyChunkedGraph,
etc.). Each service has an `AUTH_URL` environment variable pointing at
DatasetGateway. On every authenticated request, the service calls
`GET /api/v1/user/cache` with the user's token to validate identity and
check permissions. DatasetGateway is a drop-in replacement for CAVE's
original auth server (`middle_auth`) — no CAVE service code changes are
needed, only the `AUTH_URL` config.

**Neuroglancer** — Neuroglancer is a web-based 3D viewer for
neuroscience data stored in Google Cloud Storage (GCS). It uses the
"ngauth" protocol: when a user opens a protected dataset, Neuroglancer
shows a login popup pointing at DatasetGateway's `/auth/login`. After
Google OAuth, the user gets a `dsg_token` cookie. Neuroglancer then
calls `POST /token` (server-side, so it can read the cookie) to get a
short-lived token, and exchanges that for a time-limited GCS read
credential via `POST /gcs_token`. This lets the browser load data
directly from cloud storage without exposing long-lived credentials.

**neuPrint, celltyping-light, Clio** — These services validate users
by calling DatasetGateway's `/api/v1/user/cache` with the user's token,
the same pattern as CAVE. When deployed on sibling subdomains with
`AUTH_COOKIE_DOMAIN` configured (e.g., `.janelia.org`), the `dsg_token`
cookie is shared automatically — users log in once and are authenticated
across all services.

### Authorization model

Access to datasets is controlled through two mechanisms:

- **Group permissions** — A group is granted a permission (view, edit,
  manage, or admin) on a dataset. All users in that group inherit the
  permission. Managed via the Django admin panel.

- **Direct grants** — A specific user is granted a permission on a
  dataset, optionally scoped to a specific version and/or group. Managed
  via the web UI by dataset admins or team leads.

If a dataset has an associated **Terms of Service (TOS)** document, users
must accept the TOS before their permissions take effect. Permissions
exist in the database but are hidden from API responses until TOS is
accepted.

---

## Initial Setup

### 1. Install and configure

```bash
cd dsg
pixi install
pixi run setup
```

The setup wizard prompts for settings, checks for Google OAuth
credentials, runs migrations, and seeds the database. See the
[README](../README.md#google-oauth-setup) for manual OAuth setup
if you prefer.

### 2. Create a Django superuser

```bash
python manage.py createsuperuser
```

This is a built-in Django command. It prompts for an email and password.
The account is created in DatasetGateway's `User` table with `admin=True`
and a usable password. The password is only used to access the Django
admin panel at `/admin/` — all other login flows use Google OAuth.

### 3. Start the server

```bash
pixi run serve
```

If `.env` doesn't exist yet, the setup wizard runs automatically.

Use `pixi run serve-bg` to run detached (survives logout); stdout/stderr
are appended to `dsg/serve.log` and the PID is written to `dsg/serve.pid`.
Stop the detached server with `pixi run stop-serve`.

### 4. Create the first DatasetGateway admin

The first real user must log in via Google OAuth to create their record
in the DatasetGateway `User` table:

1. Visit `http://localhost:8000` and click "Log in"
2. Complete the Google OAuth flow
3. The user record is now in the database

Then promote them to admin from the command line:

```bash
python manage.py make_admin user@example.com
```

This is a custom command. It sets the `admin` field on the DatasetGateway
`User` model. The user must have logged in first — the command will
error if the email isn't found.

---

## Creating a Dataset

Datasets are created through the Django admin panel. There is currently
no web UI for dataset creation.

### 1. Log into the Django admin panel

Go to `/admin/` and log in with the Django superuser credentials
(from step 3 above).

### 2. Create a TOS document (optional)

If the dataset requires users to accept terms before accessing data:

1. Go to **Tos documents > Add**
2. Fill in:
   - **Name** — display name (e.g., "Fish2 Terms of Use")
   - **Text** — the full terms text
   - **Effective date** — when the TOS becomes active
3. Save

### 3. Create the dataset

1. Go to **Datasets > Add**
2. Fill in:
   - **Name** — a slug identifier (e.g., `fish2`). This is used in API
     responses and URL paths. Use lowercase, no spaces.
   - **Description** — optional human-readable description
   - **Tos** — select the TOS document if one is required
3. On the same page, add **Dataset buckets** inline:
   - **Name** — the GCS bucket name for this dataset's data
4. Add **Dataset versions** inline:
   - **Version** — version string (e.g., `v1`, `2026-01`)
   - **Buckets** — select which of the dataset's buckets this version uses
   - **Prefix** — optional path prefix within the bucket
   - **Is public** — whether this version is publicly accessible
5. Optionally add **Dataset admins** inline — users who can manage grants
   for this dataset via the web UI
6. Optionally add **Service tables** inline — maps CAVE service/table
   names to this dataset (needed for CAVE API compatibility)
7. Save

### 4. Set up group access

To grant a group of users access to the dataset:

1. Go to **Group dataset permissions > Add**
2. Select:
   - **Group** — which group gets access
   - **Dataset** — which dataset
   - **Permission** — `view` or `edit`
3. Save

Then add users to the group:

1. Go to **Groups** and select the group
2. In the **User groups** inline at the bottom, add users
3. Save

Any user in the group now has the selected permission on the dataset.
If the dataset has a TOS, the permission won't appear in API responses
until the user accepts it.

---

## User Workflows

### Logging in

There are two ways to log in, depending on which service you're using:

**From the DatasetGateway web UI or Neuroglancer:**
1. Visit the DatasetGateway server or open a Neuroglancer login popup
2. Click "Log in" → redirects to `/auth/login` → Google OAuth
3. After authenticating, a `dsg_token` cookie is set
4. You are redirected back (to the web UI, or Neuroglancer closes the popup)

**From a CAVE client or browser app:**
1. The app redirects you to `/api/v1/authorize` → Google OAuth
2. After authenticating, a `dsg_token` cookie is set
3. You are redirected back to the app

Both flows do the same thing: authenticate with Google, create a
DatasetGateway API key, and set the `dsg_token` cookie. The cookie is
shared across subdomains when `AUTH_COOKIE_DOMAIN` is configured
(e.g., `.janelia.org`), so you only need to log in once to access
all services.

### Browsing datasets

After logging in, visit `/web/datasets` to see all datasets in the
system. Each dataset shows its versions and whether you can manage
that dataset.

### Accepting Terms of Service

If a dataset requires TOS acceptance:

1. Go to `/web/datasets` and find the dataset
2. Click the TOS link to view the terms
3. Click "Accept" to record your acceptance

Until you accept, your permissions for that dataset will not appear in
API responses (the `/api/v1/user/cache` endpoint filters them out).

### Viewing your access

Visit `/web/my-datasets` to see:
- Your group memberships
- Datasets you lead (have `admin` permission on)
- Teams you lead (groups where you are an admin)
- Action-required items (TOS needing acceptance)
- Your direct grants (dataset, permission, version scope)
- Group-based permissions
- TOS acceptances

### Using the API

All API requests authenticate via one of:
- `dsg_token` cookie (set automatically by the browser after login)
- `Authorization: Bearer {token}` header (for programmatic access)
- `?dsg_token={token}` query parameter

Key endpoints:
- `GET /api/v1/user/cache` — your identity, groups, permissions
- `GET /api/v1/whoami` — your identity and roles
- `GET /api/v1/long_lived_token` — fetch your stable long-lived API
  token (creates it on first call; idempotent thereafter)
- `POST /api/v1/create_token` — generate a *new* no-expiry API token
  on every call (CAVEclient / middle_auth compatibility)

### Generating an API token

For programmatic access (scripts, CLI tools), fetch your stable
long-lived token:

```bash
curl http://localhost:8000/api/v1/long_lived_token \
  -H "Authorization: Bearer YOUR_EXISTING_TOKEN"
```

This returns the same token on every call, so it is safe to paste into
scripts and configuration files. Use `POST /api/v1/create_token` only
when you want a fresh, separately-revocable token.

Or use the CAVE OAuth flow at `/api/v1/authorize` to get a token
via browser redirect.

---

## Team Lead Workflows

Team leads manage users within their group. You are a team lead if you
have `manage` permission on a dataset and are a group admin
(`UserGroup.is_admin=True`).

### Managing your team

1. Go to `/web/my-datasets`
2. Click "Manage Team" next to your group name
3. The team dashboard shows:
   - Current group members
   - Grants associated with your group

### Adding team members

1. On the team dashboard, enter an email in the "Add Member" form
2. If the user doesn't exist, they are auto-created with an unusable password
3. The user is added to your group

### Granting dataset access to team members

1. On the team dashboard, use the "Grant Dataset Access" form
2. Select the user's email, a dataset (from datasets you can manage),
   and a permission level (view, edit, or manage)
3. The grant is created and scoped to your group

You can grant up to your own permission level. If you have `manage`,
you can grant `view`, `edit`, or `manage` (creating a sub-team-lead).
You cannot grant `admin`.

### Removing team members

Click "Remove" next to a member. This also revokes all grants associated
with your group for that user.

---

## Dataset Admin (SC) Workflows

Dataset admins can manage all access to their datasets. You are a dataset
admin if you have an `admin` grant on a specific dataset.

### Managing dataset members

1. Go to `/web/datasets`
2. Click "Manage Grants" next to your dataset
3. The members page shows ALL grants across all groups
4. To **grant access**: enter the user's email, select a permission
   (view, edit, manage, or admin), optionally select a specific version
5. To **revoke access**: click "Revoke" next to the grant
6. Use the group filter to view grants by group

### Assigning team leads

1. Go to `/web/datasets`
2. Click "Manage Team Leads" next to your dataset
3. Enter a user's email and click "Add" to grant them `manage` permission
4. The user must also be a group admin (`UserGroup.is_admin=True`) to
   use the team dashboard

### Managing public roots

For CAVE service tables that need public root IDs:

1. Go to `/web/datasets`
2. Click "Manage Public Roots" next to your dataset
3. Select a service table and enter a root ID to add
4. Or click "Remove" next to an existing public root

---

## Global Admin Workflows

Global admins (`user.admin=True`) have all the capabilities of dataset
admins for every dataset and can access any team dashboard. Additionally,
global admins should use the Django admin panel at `/admin/` for
operations not available in the web UI:

- Creating and editing datasets, versions, and TOS documents
- Managing groups and group memberships
- Creating group-dataset permission assignments
- Viewing the audit log
- Managing service tables
- Viewing and managing API keys

---

## Service-Specific Notes

DatasetGateway integrates with multiple services, each with its own
authentication pattern and considerations. This section provides a
quick summary; see the linked documents for full details.

### CAVE

CAVE services (MaterializationEngine, AnnotationEngine, PyChunkedGraph,
etc.) set `AUTH_URL` to DatasetGateway's base URL. On every authenticated
request, the service calls `GET /api/v1/user/cache` with the user's
token. DatasetGateway is a drop-in replacement for CAVE's original
`middle_auth` server -- no CAVE code changes are needed.

### Clio (clio-store)

clio-store delegates auth to DatasetGateway when the `DSG_URL` environment
variable is set. A migration command (`import_clio_auth`) imports users,
roles, and groups from Firestore into DatasetGateway.

Key consideration: clio-store's `public` flag on datasets lives in
Firestore (not DatasetGateway) and grants implicit read access to all
authenticated users. This means dataset access is determined by two
sources -- DatasetGateway permissions AND the Firestore `public` flag.

See [Clio integration details](clio-support.md) for permission mapping,
the `public` flag behavior, migration steps, and configuration.

### Neuroglancer

Neuroglancer uses the ngauth protocol. After Google OAuth login,
DatasetGateway issues short-lived tokens that Neuroglancer exchanges for
time-limited GCS read credentials. See the
[architecture document](architecture.md) for the token flow.

### neuPrint

neuPrint validates users by calling `GET /api/v1/user/cache` with the
user's token, the same pattern as CAVE. When deployed on sibling
subdomains with `AUTH_COOKIE_DOMAIN` configured, the `dsg_token` cookie
is shared automatically.

To import existing neuPrint users from `authorized.json`, use the
`import_neuprint_auth` management command:

```bash
python manage.py import_neuprint_auth authorized.json --datasets hemibrain manc
```

Permission mapping: `"readonly"` → view, `"readwrite"` → edit,
`"admin"` → global admin. Use `--dry-run` to preview without writing.
See [`docs/auth-integration.md`](../neuprintHTTP/docs/auth-integration.md)
in the neuprintHTTP repo for the full integration plan.

---

## Management Commands Reference

All commands are run from the `dsg/` directory.

| Command | Source | Purpose |
|---------|--------|---------|
| `python manage.py migrate` | Django built-in | Create/update database tables |
| `python manage.py createsuperuser` | Django built-in | Create a Django admin panel login |
| `pixi run setup` | Pixi task | Interactive setup wizard — generates `.env` |
| `pixi run serve` | Pixi task | Start the development server (runs setup if `.env` is missing) |
| `pixi run serve-bg` | Pixi task | Start the dev server detached; logs to `dsg/serve.log`, PID in `dsg/serve.pid` |
| `pixi run stop-serve` | Pixi task | Stop the detached development server (kills `serve.pid`, cleans up) |
| `pixi run deploy` | Pixi task | Build and deploy with Docker |
| `pixi run stop-deploy` | Pixi task | Stop the Docker deployment |
| `python manage.py collectstatic` | Django built-in | Collect static files for production |
| `python manage.py seed_permissions` | Custom | Create `view`, `edit`, `manage`, and `admin` permission types |
| `python manage.py seed_groups` | Custom | Create default groups (`admin`, `sc`, `team_lead`, `user`) |
| `python manage.py make_admin EMAIL` | Custom | Promote a user to DatasetGateway admin (user must exist) |
| `python manage.py import_clio_auth FILE` | Custom | Import clio-store auth data from exported JSON (see [Clio integration](clio-support.md)) |
| `python manage.py import_neuprint_auth FILE --datasets DS [DS ...]` | Custom | Import neuPrint authorized.json (see [neuPrint integration](#neuprint)) |
