# DatasetGate User Manual

## Concepts

DatasetGate has three levels of admin privilege. Understanding the
difference is important before setting up the system.

### Django superuser vs DatasetGate admin vs dataset admin

| Role | How to assign | What it grants |
|------|--------------|----------------|
| **Django superuser** | `python manage.py createsuperuser` | Access to the Django admin panel at `/admin/`. This is a Django built-in concept — it uses Django's own `auth_user` table, completely separate from DatasetGate's `User` model. The superuser logs in with a username/password, not Google OAuth. |
| **DatasetGate admin** | `python manage.py make_admin user@example.com` | Full access to all datasets. Can manage grants and public roots for any dataset via the web UI. Checked by the `admin` field on DatasetGate's `User` model. The user must have logged in via Google OAuth first. |
| **Dataset admin** | Created via Django admin panel (DatasetAdmin record) | Can manage grants and public roots for specific datasets they are assigned to via the web UI at `/web/grants/<dataset>`. |

A typical setup uses a Django superuser for initial configuration (creating
datasets, groups, permissions) and DatasetGate admins for day-to-day
management (granting user access).

### How services use DatasetGate

DatasetGate is the central auth layer for multiple platforms. Each
platform authenticates users through DatasetGate but in slightly
different ways:

**CAVE services** — CAVE is a set of microservices for connectomics
annotation (MaterializationEngine, AnnotationEngine, PyChunkedGraph,
etc.). Each service has an `AUTH_URL` environment variable pointing at
DatasetGate. On every authenticated request, the service calls
`GET /api/v1/user/cache` with the user's token to validate identity and
check permissions. DatasetGate is a drop-in replacement for CAVE's
original auth server (`middle_auth`) — no CAVE service code changes are
needed, only the `AUTH_URL` config.

**Neuroglancer** — Neuroglancer is a web-based 3D viewer for
neuroscience data stored in Google Cloud Storage (GCS). It uses the
"ngauth" protocol: when a user opens a protected dataset, Neuroglancer
shows a login popup pointing at DatasetGate's `/auth/login`. After
Google OAuth, the user gets a `dsg_token` cookie. Neuroglancer then
calls `POST /token` (server-side, so it can read the cookie) to get a
short-lived token, and exchanges that for a time-limited GCS read
credential via `POST /gcs_token`. This lets the browser load data
directly from cloud storage without exposing long-lived credentials.

**neuPrint, celltyping-light, Clio** — These services validate users
by calling DatasetGate's `/api/v1/user/cache` with the user's token,
the same pattern as CAVE. When deployed on sibling subdomains with
`AUTH_COOKIE_DOMAIN` configured (e.g., `.janelia.org`), the `dsg_token`
cookie is shared automatically — users log in once and are authenticated
across all services.

### Authorization model

Access to datasets is controlled through two mechanisms:

- **Group permissions** — A group is granted a permission (view or edit)
  on a dataset. All users in that group inherit the permission. Managed
  via the Django admin panel.

- **Direct grants** — A specific user is granted a permission on a
  dataset, optionally scoped to a specific version. Managed via the web
  UI by dataset admins.

If a dataset has an associated **Terms of Service (TOS)** document, users
must accept the TOS before their permissions take effect. Permissions
exist in the database but are hidden from API responses until TOS is
accepted.

---

## Initial Setup

### 1. Install and migrate

```bash
cd datasetgate
pip install -e ".[dev]"
python manage.py migrate
python manage.py seed_permissions
python manage.py seed_groups
```

`migrate` is a built-in Django command that creates the database tables.
`seed_permissions` and `seed_groups` are custom commands that populate the
`Permission` table with `view` and `edit`, and the `Group` table with
`admin`, `sc`, `lab_head`, and `user`.

### 2. Configure Google OAuth

See the [README](../README.md#google-oauth-setup) for full instructions.
Either place a `client_credentials.json` file in `datasetgate/secrets/`
or set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` environment
variables.

### 3. Create a Django superuser

```bash
python manage.py createsuperuser
```

This is a built-in Django command. It prompts for a username, email, and
password. This account is used only to access the Django admin panel at
`/admin/` — it is not the same as a DatasetGate user and does not
appear in DatasetGate's `User` table.

### 4. Start the server

```bash
python manage.py runserver
```

The server starts at `http://localhost:8000`.

### 5. Create the first DatasetGate admin

The first real user must log in via Google OAuth to create their record
in the DatasetGate `User` table:

1. Visit `http://localhost:8000` and click "Log in"
2. Complete the Google OAuth flow
3. The user record is now in the database

Then promote them to admin from the command line:

```bash
python manage.py make_admin user@example.com
```

This is a custom command. It sets the `admin` field on the DatasetGate
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
3. On the same page, add **Dataset versions** inline:
   - **Version** — version string (e.g., `v1`, `2026-01`)
   - **Gcs bucket** — the GCS bucket for this version's data
   - **Prefix** — optional path prefix within the bucket
   - **Is public** — whether this version is publicly accessible
4. Optionally add **Dataset admins** inline — users who can manage grants
   for this dataset via the web UI
5. Optionally add **Service tables** inline — maps CAVE service/table
   names to this dataset (needed for CAVE API compatibility)
6. Save

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

**From the DatasetGate web UI or Neuroglancer:**
1. Visit the DatasetGate server or open a Neuroglancer login popup
2. Click "Log in" → redirects to `/auth/login` → Google OAuth
3. After authenticating, a `dsg_token` cookie is set
4. You are redirected back (to the web UI, or Neuroglancer closes the popup)

**From a CAVE client or browser app:**
1. The app redirects you to `/api/v1/authorize` → Google OAuth
2. After authenticating, a `dsg_token` cookie is set
3. You are redirected back to the app

Both flows do the same thing: authenticate with Google, create a
DatasetGate API key, and set the `dsg_token` cookie. The cookie is
shared across subdomains when `AUTH_COOKIE_DOMAIN` is configured
(e.g., `.janelia.org`), so you only need to log in once to access
all services.

### Browsing datasets

After logging in, visit `/web/datasets` to see all datasets in the
system. Each dataset shows its versions and whether you are an admin
of that dataset.

### Accepting Terms of Service

If a dataset requires TOS acceptance:

1. Go to `/web/datasets` and find the dataset
2. Click the TOS link to view the terms
3. Click "Accept" to record your acceptance

Until you accept, your permissions for that dataset will not appear in
API responses (the `/api/v1/user/cache` endpoint filters them out).

### Viewing your access

Visit `/web/my-datasets` to see:
- Which TOS documents you have accepted (and when)
- Which grants you have been given (dataset, permission, version scope)

### Using the API

All API requests authenticate via one of:
- `dsg_token` cookie (set automatically by the browser after login)
- `Authorization: Bearer {token}` header (for programmatic access)
- `?dsg_token={token}` query parameter

Key endpoints:
- `GET /api/v1/user/cache` — your identity, groups, permissions
- `GET /api/v1/whoami` — your identity and roles
- `POST /api/v1/create_token` — generate a new API token for
  programmatic use

### Generating an API token

For programmatic access (scripts, CLI tools), generate a token:

```bash
curl -X POST http://localhost:8000/api/v1/create_token \
  -H "Authorization: Bearer YOUR_EXISTING_TOKEN"
```

Or use the CAVE OAuth flow at `/api/v1/authorize` to get a token
via browser redirect.

---

## Dataset Admin Workflows

Dataset admins can manage who has access to their datasets through the
web UI. You are a dataset admin if either:
- Your `admin` flag is set (global admin), or
- You have a `DatasetAdmin` record for the specific dataset

### Managing grants

1. Go to `/web/datasets`
2. Click "Manage Grants" next to your dataset
3. To **grant access**: enter the user's email, select a permission
   (view or edit), optionally select a specific version, and submit
4. To **revoke access**: click "Revoke" next to the grant

Grants are immediate — no cache delay for the grant itself, though the
user's permission cache may take up to 5 minutes to refresh.

### Managing public roots

For CAVE service tables that need public root IDs:

1. Go to `/web/datasets`
2. Click "Manage Public Roots" next to your dataset
3. Select a service table and enter a root ID to add
4. Or click "Remove" next to an existing public root

---

## Global Admin Workflows

Global admins have all the capabilities of dataset admins for every
dataset. Additionally, global admins should use the Django admin panel
at `/admin/` for operations not available in the web UI:

- Creating and editing datasets, versions, and TOS documents
- Managing groups and group memberships
- Creating group-dataset permission assignments
- Viewing the audit log
- Managing service tables
- Viewing and managing API keys

---

## Management Commands Reference

All commands are run from the `datasetgate/` directory.

| Command | Source | Purpose |
|---------|--------|---------|
| `python manage.py migrate` | Django built-in | Create/update database tables |
| `python manage.py createsuperuser` | Django built-in | Create a Django admin panel login |
| `python manage.py runserver` | Django built-in | Start the development server |
| `python manage.py collectstatic` | Django built-in | Collect static files for production |
| `python manage.py seed_permissions` | Custom | Create `view` and `edit` permission types |
| `python manage.py seed_groups` | Custom | Create default groups (`admin`, `sc`, `lab_head`, `user`) |
| `python manage.py make_admin EMAIL` | Custom | Promote a user to DatasetGate admin (user must exist) |
