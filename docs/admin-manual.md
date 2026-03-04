# DatasetGateway Admin Manual

This manual covers initial system setup and day-to-day administration
through the Django admin console (`/admin/`). For end-user workflows
(logging in, browsing datasets, accepting TOS) see the
[User Manual](user-manual.md).

---

## Initial Setup

### 1. Install and configure

```bash
cd dsg
pixi install
pixi run setup
```

The setup wizard prompts for the public origin, port, secret key, allowed
hosts, and other settings. It also checks for Google OAuth credentials and
prints step-by-step instructions if they are missing. After generating
`.env`, it runs migrations and seeds the database with default permissions
and groups. You can re-run `pixi run setup` at any time to update
settings — existing values are shown as defaults.

### 2. Create an admin user

```bash
pixi run make-admin user@example.com
```

This creates the user if they don't exist, sets `admin=True`, and
prompts for a password (needed to log into the Django admin console at
`/admin/`). If the user already exists (e.g., from an import or OAuth
login), it promotes them and adds a password. Use `--no-password` to skip 
the password prompt, or `--remove` to revoke admin status.

### 3. (Optional) Import Clio auth data

```bash
pixi run python manage.py import_clio_auth path/to/clio_export_auth.json
```

This imports users, datasets, grants, groups, and dataset-admin
assignments from a Clio export. It is idempotent — running it again
will skip records that already exist.

### 4. Start the server

```bash
pixi run serve
```

If `.env` doesn't exist yet, the setup wizard runs automatically.
The admin console is at `/admin/`.

### Full database reset

To start completely fresh:

```bash
cd dsg
rm db.sqlite3
pixi run setup                        # re-runs migrations and seeds
# optionally re-import Clio data:
pixi run python manage.py import_clio_auth ../clio_export_auth.json
pixi run make-admin user@example.com  # create or promote admin (order doesn't matter)
```

---

## Django Admin Console Overview

The admin console at `/admin/` lets you view and edit all data in the
system. It is organized into sections.

### When to use the admin console vs the web UI

| Task | Where |
|------|-------|
| Create/edit datasets, versions, TOS documents | Admin console |
| Manage group memberships and group-dataset permissions | Admin console |
| View audit logs | Admin console |
| Manage API keys | Admin console |
| Grant/revoke user access to a dataset | Web UI (`/web/grants/<dataset>`) — preferred for day-to-day use |
| Promote team leads | Web UI (`/web/team-leads/<dataset>`) — SC/admin only |
| Manage team members and group grants | Web UI (`/web/team/<group>/`) — team leads |
| Manage public roots | Web UI (`/web/public-roots/<dataset>`) |

The web UI enforces authorization rules (only team leads can manage
their groups, only SC/admin can promote team leads). The admin console
bypasses all of that — any superuser can edit anything. Use the web
UI for routine operations and the admin console for initial setup,
bulk changes, or debugging.

---

## CORE Section

This is where all DatasetGateway-specific data lives.

### Users

Each row is a DatasetGateway user. Key fields:

| Field | Meaning |
|-------|---------|
| **Email** | The user's identity. Must be unique. |
| **Name** | Display name (often populated from Google profile). |
| **Admin** | If checked, the user is a global admin — they can manage grants for any dataset and bypass access-mode restrictions. |
| **Is active** | Unchecked = account disabled. Disabled users cannot authenticate. |
| **Read only** | If checked, `edit` permissions are stripped from this user's permission cache. They can only view. |
| **Pi** | Principal Investigator field (informational, from Clio legacy). |
| **Parent** | If set, this is a service account owned by the parent user. Service accounts inherit TOS acceptance from their parent. |

**Inline sections on the User detail page:**

- **User groups** — which groups this user belongs to. Add rows here to
  put a user in a group.
- **API keys** — authentication tokens for this user. Each OAuth login
  creates one. You can see when tokens were created and last used. You
  generally don't need to edit these, but you can delete old ones.

**When to edit Users here:** To toggle the `admin`, `is_active`, or
`read_only` flags, or to add/remove group memberships. For bulk user
creation, use `import_clio_auth` instead.

### Groups

Authorization groups (e.g., `sc`, `team_lead`, `user`). Not to be
confused with Django's built-in `auth.Group`, which is not used and has
been hidden from the admin.

The **User groups** inline on the Group detail page shows all members.
Add rows here to add users to the group. The `is_admin` flag on a
`UserGroup` record designates the user as a team lead for that group.

### Permissions

The abstract permission types. The system ships with four, in a strict
hierarchy (`admin` > `manage` > `edit` > `view`):

- **view** — read access to dataset data
- **edit** — write access to dataset data
- **manage** — can manage grants within a group (team lead capability)
- **admin** — full dataset administration (SC-level)

Each level implies all levels below it. You generally never need to add
or change these.

### Datasets

Each row is a dataset. Key fields:

| Field | Meaning |
|-------|---------|
| **Name** | Slug identifier used in URLs and API responses (e.g., `fish2`). Lowercase, no spaces. |
| **Description** | Human-readable description shown on the web UI. |
| **Tos** | Link to the TOS document users must accept. Leave blank if no TOS is required. |
| **Access mode** | `Closed` (invite-only — users need a Grant or admin role) or `Public` (any authenticated user can self-service accept TOS and get view access). |

**Inline sections on the Dataset detail page:**

- **Dataset buckets** — the GCS buckets associated with this dataset.
  Used for IAM provisioning and Neuroglancer token issuance.
- **Dataset versions** — the versioned releases. Each version can be
  linked to one or more dataset buckets via the Buckets M2M field.
- **Grants** — users with `admin` permission on this dataset can manage
  all grants via the web UI. Team leads (users with `manage` permission)
  can manage grants within their group via the team dashboard.
- **Service tables** — maps CAVE service/table names to this dataset.
  Only needed for CAVE API compatibility.

### Dataset versions

Versioned releases of datasets. Usually edited inline on the Dataset
page, but also available as a standalone list for searching across all
datasets.

| Field | Meaning |
|-------|---------|
| **Version** | Version string (e.g., `v1`, `2026-01`). |
| **Buckets** | The GCS buckets linked to this version (selected from the dataset's bucket list). |
| **Prefix** | Optional path prefix within the bucket. |
| **Is public** | Whether this version's data is publicly readable (no auth needed). |

### Group dataset permissions

Grants a permission to an entire group on a dataset. For example:
"the `user` group gets `view` on dataset `fly-hemibrain`."

All users in the group inherit the permission. This is the primary
mechanism for broad access control. Use this for datasets that should
be accessible to a whole community.

### Grants

Direct per-user permission assignments. Each grant gives one user one
permission on one dataset (optionally scoped to a specific version).

| Field | Meaning |
|-------|---------|
| **User** | The user receiving access. |
| **Dataset** | Which dataset. |
| **Dataset version** | If set, the grant applies to only this version. If blank, it applies to all versions. |
| **Permission** | `view`, `edit`, `manage`, or `admin`. |
| **Group** | If set, the grant is scoped to this group (created by a team lead). If blank, the grant is not group-scoped (created by an admin or via self-service). |
| **Granted by** | The admin or team lead who created this grant. |
| **Source** | `manual` (created by an admin or team lead via the web UI) or `self_service` (user accepted TOS on a public dataset). |

Grants are usually managed through the web UI at
`/web/grants/<dataset>`, which enforces authorization. Editing them
here is useful for bulk fixes or debugging.

### Service tables

Maps CAVE service/table pairs to datasets. Required for the
`GET /api/v1/service/{namespace}/table/{table_id}/dataset` endpoint.
Each service table can also have **Public roots** (inline), which are
root IDs that are publicly accessible without authentication.

### TOS documents

Terms of Service documents that users must accept before their
permissions take effect.

| Field | Meaning |
|-------|---------|
| **Name** | Display name (e.g., "FlyWire Terms of Use"). |
| **Text** | The full terms text (HTML is supported). |
| **Dataset** | The dataset this TOS applies to. |
| **Dataset version** | Optional — scope TOS to a specific version. |
| **Invite token** | Auto-generated unguessable token used in TOS landing page URLs (`/web/tos/<token>/`). You don't need to set this — it's generated automatically. |
| **Effective date** | When the TOS becomes active. |
| **Retired date** | If set, the TOS is no longer active after this date. |

### TOS acceptances

Read-only record of which users accepted which TOS documents and when.
Includes the IP address at the time of acceptance. You generally don't
edit these — they're created automatically when users accept TOS via
the web UI.

### Audit logs

A record of administrative actions. Currently a placeholder — the
system does not yet populate audit log entries automatically. Future
versions will record grant changes, user promotions, and other admin
actions here.

### API keys

Authentication tokens. Each row links a token string to a user. Created
automatically on OAuth login. Usually viewed inline on the User detail
page. You can delete old/unused keys here if needed.

---

## SITES Section

### Sites

Required by django-allauth. There should be exactly one record with
`id=1`. Allauth uses this domain to construct OAuth callback URLs
(e.g., `http://<domain>/accounts/google/login/callback/`).

The database migration sets this to `localhost:8000` by default so
local development works out of the box. **For production**, update it
to match your deployment domain:

```bash
pixi run python manage.py shell -c "
from django.contrib.sites.models import Site
Site.objects.update_or_create(id=1, defaults={'domain': 'auth.example.org', 'name': 'DatasetGateway'})
"
```

The corresponding redirect URI must also be registered in your Google
Cloud Console OAuth client's **Authorized redirect URIs** (e.g.,
`https://auth.example.org/accounts/google/login/callback/`).

---

## SOCIAL ACCOUNTS Section (allauth)

These are managed automatically by django-allauth during Google OAuth
logins. You generally don't need to touch them.

- **Social applications** — the Google OAuth app configuration. If you
  configured OAuth via environment variables or `client_credentials.json`,
  this may be empty (allauth reads from Django settings instead).
- **Social accounts** — links between DatasetGateway users and their Google
  accounts. Created automatically on first OAuth login.
- **Social application tokens** — OAuth tokens from Google. Managed
  automatically.

---

## ACCOUNT Section (allauth)

- **Email addresses** — email addresses associated with user accounts,
  managed by allauth. You generally don't need to edit these.

---

## Environment Variables Reference

| Variable | Default | Purpose |
|----------|---------|---------|
| `DJANGO_SECRET_KEY` | insecure dev key | Session signing. **Must be set in production.** |
| `DJANGO_DEBUG` | `True` | Debug mode. Set to `False` in production. |
| `DJANGO_ALLOWED_HOSTS` | `*` | Comma-separated allowed hostnames. **Must be set in production.** |
| `DATABASE_PATH` | `db.sqlite3` | Path to the SQLite database file. |
| `GOOGLE_CLIENT_ID` | (empty) | Google OAuth client ID. |
| `GOOGLE_CLIENT_SECRET` | (empty) | Google OAuth client secret. |
| `CLIENT_CREDENTIALS_PATH` | `secrets/client_credentials.json` | Alternative path to OAuth credentials file. |
| `AUTH_COOKIE_DOMAIN` | (empty) | Set to `.example.org` to share the `dsg_token` cookie across subdomains. |
| `NGAUTH_ALLOWED_ORIGINS` | `^https?://.*\.neuroglancer\.org$` | Regex for allowed CORS origins on ngauth endpoints. |
| `DSG_ORIGIN` | (empty) | Public origin for CSRF trusted origins (e.g., `https://dataset-gateway.mydomain.org`). |
| `DSG_PORT` | `8200` | Port for the development server. |
| `SECURE_SSL_REDIRECT` | `True` (when not DEBUG) | Whether to redirect HTTP to HTTPS. |

---

## Management Commands Reference

All commands are run from the `dsg/` directory.

| Command | Purpose |
|---------|---------|
| `python manage.py migrate` | Create/update database tables. |
| `pixi run make-admin EMAIL` | Create or promote a user to admin (replaces `createsuperuser`). |
| `python manage.py make_admin EMAIL --remove` | Revoke admin status from a user. |
| `python manage.py changepassword EMAIL` | Reset a user's admin console password. |
| `python manage.py seed_permissions` | Create `view`, `edit`, `manage`, and `admin` permission types. |
| `python manage.py seed_groups` | Create default groups (`admin`, `sc`, `team_lead`, `user`). |
| `python manage.py import_clio_auth FILE` | Import users, datasets, and grants from a Clio export JSON. |
| `pixi run setup` | Interactive setup wizard — generates `.env`. |
| `pixi run serve` | Start the development server (runs setup if `.env` is missing). |
| `pixi run deploy` | Build and deploy with Docker. |
| `pixi run stop` | Stop the Docker deployment. |
| `python manage.py collectstatic` | Collect static files for production deployment. |
