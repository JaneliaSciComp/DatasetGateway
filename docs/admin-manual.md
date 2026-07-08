---
doc_status: living
sync_policy: Update with setup, admin workflow, environment variable, and management command changes.
last_reviewed: 2026-06-01
---

# DatasetGateway Admin Manual

This manual covers initial system setup and day-to-day administration
through the Django admin console (`/admin/`). For end-user workflows
(logging in, browsing datasets, accepting TOS) see the
[User Manual](user-manual.md). For programmatic non-human identities
(CI jobs, backend services), see [Service Accounts](service-accounts.md).

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

This works for both local and Docker deployments — it automatically
detects a running container. It creates the user if they don't exist, sets `admin=True`, and
prompts for a password (needed to log into the Django admin console at
`/admin/`). If the user already exists (e.g., from an import or OAuth
login), it promotes them and adds a password. Use `--no-password` to skip
the password prompt, or `--remove` to revoke admin status.

### 3. (Optional) Import Clio auth data

```bash
bash scripts/manage.sh import_clio_auth path/to/clio_export_auth.json
```

> **Docker deployment:** The file must be accessible inside the container.
> Copy it in first:
> ```bash
> docker compose cp path/to/clio_export_auth.json dsg:/tmp/
> bash scripts/manage.sh import_clio_auth /tmp/clio_export_auth.json
> ```

This imports users, datasets, grants, groups, and dataset-admin
assignments from a Clio export. It is idempotent — running it again
will skip records that already exist.

### 4. Start the server

**Local development:**
```bash
pixi run serve
```

Use `pixi run serve-bg` to run detached (survives logout); logs are
appended to `dsg/serve.log`, PID stored in `dsg/serve.pid`. Stop with
`pixi run stop-serve`.

**Docker deployment:**
```bash
pixi run deploy
```

If `.env` doesn't exist yet, the setup wizard runs automatically.
The admin console is at `/admin/`.

### Full database reset

To start completely fresh:

**Local development:**
```bash
cd dsg
rm db.sqlite3
pixi run setup                        # re-runs migrations and seeds
pixi run make-admin user@example.com
```

**Docker deployment:**
```bash
cd dsg
docker compose down -v                # removes containers and database volume
pixi run deploy                       # rebuilds, runs migrations and seeds
pixi run make-admin user@example.com
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
| Promote dataset admins | Web UI (`/web/dataset-admins/<dataset>`) — SC/admin only |
| Manage team members and group grants | Web UI (`/web/group/<group>/`) — group admins/team leads |
| Manage public roots | Web UI (`/web/public-roots/<dataset>`) |
| Create / manage service accounts and their tokens | Web UI (`/web/service-accounts`) — admin only. See [Service Accounts](service-accounts.md). |

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
| **Parent** | Legacy field for an early User-as-service-account design. Not used by the current service-account feature — see [Service Accounts](service-accounts.md). Existing parent-linked rows still inherit TOS acceptance from their parent. |

**Inline sections on the User detail page:**

- **User groups** — which groups this user belongs to. Add rows here to
  put a user in a group.
- **API keys** — authentication tokens for this user. Each OAuth login
  creates one. You can see when tokens were created and last used. You
  generally don't need to edit these, but you can delete old ones.

**When to edit Users here:** To toggle the `admin`, `is_active`, or
`read_only` flags, or to add/remove group memberships. For bulk user
creation, use `import_csv`, `import_clio_auth`, or `import_neuprint_auth` as appropriate.

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
  Used for IAM provisioning and Neuroglancer token issuance. Adding,
  renaming, or deleting a bucket here immediately syncs bucket IAM for
  the dataset's users (see [Bucket IAM Synchronization](#bucket-iam-synchronization)).
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

A record of administrative actions. The web UI, Django admin custom save
hooks, SCIM views, and import commands write entries through `log_audit()`.
Use this table to inspect grant changes, TOS acceptances, service-account
mutations, SCIM changes, and bulk imports.

### API keys

Authentication tokens. Each row links a token string to a user. Created
automatically on OAuth login. Usually viewed inline on the User detail
page. You can delete old/unused keys here if needed.

### Service accounts, Service account tokens, Service account grants

Non-human identities for programmatic access (CI jobs, backend services,
shared scripting credentials). Distinct from users — no Google login,
no TOS, no group membership. The admin console exposes the three models
for back-office visibility, but day-to-day management (create, mint
tokens, assign dataset grants, disable, delete) happens in the web UI
at `/web/service-accounts` and is admin-only.

See the dedicated [Service Accounts](service-accounts.md) doc for the
full model, capabilities, limitations, and architectural decisions.

---

## SITES Section

### Sites

Required by django-allauth. There should be exactly one record with
`id=1`. Allauth uses this domain to construct OAuth callback URLs
(e.g., `http://<domain>/accounts/google/login/callback/`).

The historical migration sets this to `localhost:8000`, while the current
development server defaults to port `8200`. If you configure allauth through
Django Site/SocialApp records instead of the settings-based Google provider,
update the Site domain to match your local or production origin:

```bash
bash scripts/manage.sh shell -c "
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

## Bucket IAM Synchronization

DSG grants and revokes per-user GCS bucket IAM (`core/iam.py`) at
`(user, bucket)` grain. A user is provisioned on a bucket only when the
bucket is in the union of that user's qualifying DSG permission sources for
the dataset, after TOS gates are applied:

```
user enabled (active, and an active parent for user-type service accounts)
AND dataset TOS accepted, if any
AND bucket reached by a qualifying Grant or Group dataset permission
AND no unaccepted active version TOS blocks that bucket
```

Global admins are skipped (they use service-account auth, not per-user
bucket IAM). `ServiceAccount`-model accounts (organization robots) are
never added to bucket IAM; user-type service accounts (`User` rows with a
parent) carry their own bucket IAM and follow their parent's enabled state.

Only DSG permissions that GCS can safely express are qualifying bucket-IAM
sources:

- A direct `Grant` with explicitly attached buckets provisions exactly
  those buckets. This is the escape hatch for rare service-scoped or
  role-scoped cases that really need bucket access.
- Otherwise, a direct `Grant` qualifies only when its permission is one of
  `view`, `edit`, `manage`, or `admin` and its `service` is blank. A
  dataset-grain grant reaches all dataset buckets. A version-scoped grant
  reaches registered anchors on the same branch with `ordinal <=` the
  grant anchor's ordinal; if the grant anchor has no ordinal, it reaches
  only that anchor's own buckets.
- A `Group dataset permission` qualifies only when its permission is one of
  `view`, `edit`, `manage`, or `admin` and its `service` is blank. Group
  permissions are dataset-grain and reach all dataset buckets.
- Service-scoped grants without explicit buckets and named-role grants such
  as `annotation_editor` provision no bucket IAM. They are capability
  statements for the native decision API, not GCS policy inputs.

Version-scoped TOS documents gate bucket IAM per bucket. An active
version-scoped, non-service TOS blocks an anchor until the user (or parent,
for user-type service accounts) accepts it. A bucket attached to anchors is
excluded only when all of its anchor attachments are blocked; a bucket with
no anchor attachment is never blocked by version TOS. Service-scoped TOS
documents do not affect bucket IAM because GCS cannot express the service
scope.

**Every mutation surface syncs inline.** Web-UI grant/TOS/group flows,
SCIM provisioning, and the Django admin console all converge IAM as part
of the mutation: editing Grants, Group dataset permissions, TOS
acceptances, user↔group memberships, Dataset buckets, DatasetVersion bucket
attachments, a dataset's TOS, grant bucket attachments, or moving/editing a
TOS document between datasets or versions triggers the appropriate
add/remove calls, including bulk "delete selected" actions, retargeted rows
(the old user/dataset pair is deprovisioned), and bucket renames/moves (the
old bucket name is deprovisioned first). Flipping a user's **Active** or
**Admin** flag (Django admin or SCIM `active`) resyncs every dataset where
they hold a permission source — disabling a user removes their bucket IAM
everywhere, including their user-type service accounts', and also cuts off
their tokens, web login, and ngauth endpoints; re-enabling re-adds IAM
wherever the full rule passes. GCS
calls are synchronous best-effort: failures are logged, never raised, so
a large fan-out (e.g. adding a bucket to a dataset with many users) may
take a moment but cannot block the save.

**DSG removes only bindings it created.** The `BucketIAMBinding` admin table
is a provenance ledger for GCS `roles/storage.objectViewer` bucket IAM
bindings that DSG actually created. The invariant is:

```
removals <= BucketIAMBinding rows <= bindings DSG created
```

When DSG provisions a user, it first asks GCS to add the member. A
confirmed create writes a ledger row. If GCS reports that the member was
already present, DSG treats the access as foreign and records no row. That
means a hand-added bucket binding is not claimed and will not be removed
later if the DSG rule says the user should not have access. Failed adds
write no row.

All removal paths check the ledger first. If no row exists, DSG makes no
GCS remove call. If a row exists, DSG removes the member from the bucket
and deletes the row only after confirmed success; failures keep the row so
the reconcile can retry. Deleting a ledger row manually in the admin is a
deliberate "disown" operation: DSG will stop removing that binding.
Adding a ledger row manually is a deliberate "claim" operation: DSG may
remove that bucket/user pair during a later sync.

**Required preflight before enabling this migration in production:** run the
version-grant audit and choose a remediation for every row it reports:

```bash
pixi run python manage.py audit_version_grants
```

The command lists version-scoped `Grant` rows and `DatasetVersion` anchors
that are public or referenced by grants/TOS documents. For each row, choose
one of four remediations before rollout: convert the grant to dataset-grain,
attach explicit buckets to the grant, set the anchor's `branch` and
`ordinal`, or accept the narrowed reach. This is migration-visible because
old version-scoped grants used to provision every dataset bucket; after this
change, they provision only the anchor reach described above.

DSG only ever adds users it enumerates from its own tables, and it removes
only ledger-owned rows. Bucket IAM is treated as a **superset** of DSG
state, so members it cannot derive from its own tables or did not create
(e.g. hand-added collaborators) are never touched. No sync path scans a
bucket policy to discover users or decide removals.

**A scheduled reconcile is the backstop for DSG-visible rows.** Because
inline calls are best-effort, run the reconcile command periodically:

```bash
pixi run iamsync                                    # all datasets
bash scripts/manage.sh sync_bucket_iam --dry-run    # preview only
bash scripts/manage.sh sync_bucket_iam --dataset DS # one dataset
```

For production, install the bundled systemd templates
`scripts/datasetgateway-iamsync.{service,timer}` (daily at 03:45; see
the unit headers for install steps, mirroring the backup units).

The reconcile command walks users currently enumerable from DSG tables
(direct Grants or memberships in groups with dataset permissions) and
probes each derived `(user, bucket)` pair. It does not enumerate bucket IAM
members. Its report categories are:

- `ADD` — DSG created a missing binding and recorded a ledger row.
- `REASSERT` — a ledger-owned binding was missing and DSG recreated it.
- `SATISFIED (foreign)` — access already exists but DSG has no ledger row,
  so the binding is left unclaimed.
- `REMOVE` — DSG removed a ledger-owned binding.
- `SKIP (not DSG-owned)` — access exists but DSG has no ledger row, so no
  removal is attempted.
- `PRUNE ledger` — DSG had a row, but a definitive probe showed the
  binding is gone; the row is removed without a GCS write.
- `ORPHAN REMOVE` — a ledger row is no longer reachable from the current
  DSG graph (for example, the user was deleted or a bucket was renamed);
  DSG removes the owned binding and deletes the row.
- `PROBE-FAIL` — DSG could not read the pair's state. This counts as a
  failure in both real and `--dry-run` mode; a dry-run with probe failures
  exits non-zero because it was not a reliable preview.

`--dry-run` performs no GCS writes and no ledger writes or deletes. It
still reports planned adds/removes/prunes/orphans and exits non-zero on
probe failures.

If a binding should be removed but is not ledger-owned, remove it manually
with the Google Cloud console or:

```bash
gcloud storage buckets remove-iam-policy-binding gs://BUCKET \
  --member=user:EMAIL --role=roles/storage.objectViewer
```

Alternatively, add a `BucketIAMBinding` row to deliberately claim the pair
and then delete it through a hooked DSG surface (web UI, Django admin, or
SCIM) so the normal remove path runs.

**Phase A production deploy checklist:**

1. Apply migrations through `0011_bucketiambinding`.
2. Pause the scheduled reconcile timer.
3. Run `audit_version_grants` and complete the chosen remediation for
   every reported row.
4. Run `sync_bucket_iam --dry-run` and review `SKIP (not DSG-owned)`,
   `SATISFIED (foreign)`, `PRUNE ledger`, `ORPHAN REMOVE`, and failure
   lines.
5. Run the real reconcile.
6. Record the audit and reconcile results in the Phase A rollout notes.

The ledger must land before the first production reconcile. It starts empty
and correct only while DSG has not yet created production bucket IAM
bindings.

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
| `CLIENT_CREDENTIALS_PATH` | `secrets/client_credentials.json` | Alternative path to OAuth credentials file. In Docker, mount this file or use `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`. |
| `AUTH_COOKIE_DOMAIN` | (empty) | Set to `.example.org` to share the `dsg_token` cookie across subdomains. |
| `NGAUTH_ALLOWED_ORIGINS` | `^https?://.*\.neuroglancer\.org$` | Regex for allowed CORS origins on ngauth endpoints. |
| `DSG_ORIGIN` | (empty) | Public origin for CSRF trusted origins (e.g., `https://dataset-gateway.mydomain.org`). |
| `DSG_PORT` | `8200` | Port for the development server. |
| `SECURE_SSL_REDIRECT` | `True` (when not DEBUG) | Whether to redirect HTTP to HTTPS. |

---

## Management Commands Reference

All commands are run from the `dsg/` directory. Use `bash scripts/manage.sh`
instead of `python manage.py` to auto-detect whether to run locally or
inside a Docker container.

| Command | Purpose |
|---------|---------|
| `pixi run make-admin EMAIL` | Create or promote a user to admin (works for both local and Docker). |
| `pixi run make-admin EMAIL --remove` | Revoke admin status from a user. |
| `bash scripts/manage.sh migrate` | Create/update database tables. |
| `bash scripts/manage.sh changepassword EMAIL` | Reset a user's admin console password. |
| `bash scripts/manage.sh seed_permissions` | Create `view`, `edit`, `manage`, and `admin` permission types. |
| `bash scripts/manage.sh seed_groups` | Create default groups (`admin`, `sc`, `team_lead`, `user`). |
| `bash scripts/manage.sh import_csv FILE --dataset DS` | Import users from CSV and grant `view` on one dataset. |
| `bash scripts/manage.sh import_clio_auth FILE` | Import users, datasets, and grants from a Clio export JSON. Note: `--dry-run` is currently not a no-write preview. |
| `bash scripts/manage.sh import_neuprint_auth FILE --datasets DS [DS ...]` | Import neuPrint `authorized.json`. |
| `bash scripts/manage.sh sync_bucket_iam [--dataset DS] [--dry-run]` | Reconcile GCS bucket IAM bindings (also `pixi run iamsync`; schedule via `scripts/datasetgateway-iamsync.{service,timer}`). |
| `pixi run setup` | Interactive setup wizard — generates `.env`. |
| `pixi run serve` | Start the development server (runs setup if `.env` is missing). |
| `pixi run serve-bg` | Start the dev server detached; logs to `dsg/serve.log`, PID in `dsg/serve.pid`. |
| `pixi run deploy` | Build and deploy with Docker. |
| `pixi run stop-deploy` | Stop the Docker deployment. |
| `pixi run stop-serve` | Stop the detached development server (kills `serve.pid`, cleans up). |
