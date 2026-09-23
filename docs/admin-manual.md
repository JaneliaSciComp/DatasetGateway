---
doc_status: living
sync_policy: Update with setup, admin workflow, environment variable, and management command changes.
last_reviewed: 2026-09-23
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

Migrations also create `dsg_cache_table`, the shared permission-cache table.
If an older or manually provisioned deployment does not have it, the fallback
is safe to run repeatedly:

```bash
pixi run python manage.py createcachetable dsg_cache_table
```

The Docker deploy script migrates with a one-off container before starting the
new application container, so the cache table is available on its first
request. The database cache makes invalidation visible across workers, but
leave the production one-worker default in place until a two-worker
warm/mutate/read smoke check shows no SQLite lock errors.

### 2. Create an admin user

```bash
pixi run make-admin user@example.com
```

This bootstraps the first admin from a shell, before anyone can reach the
admin console. It works for both local and Docker deployments — it automatically
detects a running container. It creates the user if they don't exist, sets `admin=True`, and
prompts for a password. If the user already exists (e.g., from an import or OAuth
login), it promotes them and adds a password. Use `--no-password` to skip
the password prompt, or `--remove` to revoke admin status.

After that, promote admins in the admin console: open **Core › Users**, find
the person, check **Admin**, and save (uncheck it to revoke). The person needs a
user row first, so have them sign in with Google once, or import them. Prefer
the checkbox day to day: it needs no shell on the server and records who made
the change in the admin history. From a shell, `make-admin EMAIL --no-password`
does the same promotion and also works for someone who has not signed in yet
(their first Google sign-in attaches to the row by email).

Admins sign in with Google: `/admin/` redirects to a login page whose
**Sign in with Google** button returns them to the admin console. The email +
password form below that button only works for accounts given a Django password
by `make-admin`; keep one such account (typically the bootstrap admin) as a
break-glass door in case Google sign-in is unavailable.

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
The admin console is at `/admin/` (sign in with the Google button).

### serve.log rotation (detached dev server, interim)

`pixi run serve-bg` appends everything Django logs — request lines,
`dsg.authz` decisions and the `ngauth.views` token-issuance records — to
`dsg/serve.log` and never rotates it. Until serving moves to gunicorn under
systemd (where journald owns retention), rotate it with `logrotate` and ship
the rotated files to the same nearline directory that holds the SQLite
backups. Three files under `dsg/scripts/` do this:

| File | Role |
|---|---|
| `datasetgateway-logrotate.conf` | logrotate stanza: `copytruncate`, `size 50M`, plaintext, rotated copies in `dsg/logs/serve.log-YYYYmmddTHHMMSS` |
| `datasetgateway-logrotate.service` / `.timer` | oneshot + daily timer: run logrotate, then the shipper |
| `ship-rotated-logs.sh` | copy every `dsg/logs/serve.log-*` to `$DSG_BACKUP_DIR/logs/`, verify sha256 on the target, then keep only the two newest locally |

**Why `copytruncate`.** `serve.sh --detach` starts the server with
`nohup … >> serve.log 2>&1 &`, so the process holds one `O_APPEND` descriptor
on `serve.log` for its whole life and never reopens it. logrotate therefore
*copies* the content out and truncates the original in place; the next write
lands at the new end of file, with no restart, no signal and no sparse gap.
Rename-and-recreate rotation would leave the server writing to the renamed
file forever. The only loss is whatever the server writes between the copy
and the truncate (typically nothing; at most a line or two under load).

**Retention.** Rotated files are uncompressed and unencrypted, both locally
and on nearline (the nearline directory carries the same owner and group as
`serve.log`). The shipper keeps the two newest rotated files in `dsg/logs/`
for quick reading and deletes older ones *only after* their nearline copy has
been verified; nearline keeps everything. The stanza's `rotate 60` is just a
cap on unshipped backlog if nearline is unreachable for weeks. `size 50M` is
a threshold checked once a day, not a ceiling.

**Install (root, one session).** Requires `DSG_BACKUP_DIR` in `dsg/.env`
(see [Backups](backups.md)) and that directory to exist — the shipper refuses
to create the nearline root, so an unmounted share fails loudly instead of
filling the local disk. Placeholders are the same as in
`datasetgateway-backup.service`.

```bash
cd /path/to/DatasetGateway/dsg
sed -e 's#<path-to>#/path/to#g' -e 's#<dsg-user>#USER#g' -e 's#<dsg-group>#GROUP#g' \
    scripts/datasetgateway-logrotate.conf > /etc/dsg/datasetgateway-logrotate.conf
cp scripts/datasetgateway-logrotate.service scripts/datasetgateway-logrotate.timer /etc/systemd/system/
# edit User=, Group=, WorkingDirectory=, EnvironmentFile= and the ExecStart/ExecStartPost paths
systemctl daemon-reload
systemctl enable --now datasetgateway-logrotate.timer
systemctl list-timers datasetgateway-logrotate.timer
```

`systemctl start datasetgateway-logrotate.service` runs a check by hand; it
rotates only if `serve.log` is above 50 MB and then ships. Failures are in
`journalctl -u datasetgateway-logrotate` and `systemctl status`.

**Bootstrap an oversized existing `serve.log`.** If the file is already far
above the threshold, move its history to nearline *before* enabling the
timer, running as the service user (not root, so the state file and
`dsg/logs/` are owned correctly):

```bash
cd /path/to/DatasetGateway/dsg
logrotate --force --state "$PWD/logrotate.state" /etc/dsg/datasetgateway-logrotate.conf
ls -l /proc/$(cat serve.pid)/fd | grep serve.log     # still the live file
set -a; . ./.env; set +a
scripts/ship-rotated-logs.sh                          # copies logs/serve.log-<stamp> to $DSG_BACKUP_DIR/logs/
sha256sum logs/serve.log-* "$DSG_BACKUP_DIR"/logs/serve.log-*   # pairs must match
```

Do not use `gzip … && truncate` or `> serve.log` shortcuts: they leave the
history outside `dsg/logs/`, where the shipper never looks.

**Remove at the gunicorn cutover.** When `datasetgateway.service` replaces
`serve-bg`, run `scripts/ship-rotated-logs.sh` one last time (with
`DSG_LOG_KEEP_LOCAL=0` to flush the local copies), then
`systemctl disable --now datasetgateway-logrotate.timer`, delete the two
units and `/etc/dsg/datasetgateway-logrotate.conf`, and `daemon-reload`.
journald owns local retention from then on; whether journal exports keep
going to nearline is decided at that cutover.

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
| **Access mode** | `Closed` (invite-only — users need a Grant or admin role) or `Public` (anonymous and authenticated callers receive `view` access, subject to TOS). Public never grants write access. |

Public access is read-only. Mutations still require an explicit grant carrying
the required write role; publishing a dataset or version does not create a
grant and does not make its base data writable.

**Inline sections on the Dataset detail page:**

- **Dataset buckets** — the GCS buckets associated with this dataset.
  These mappings are the authoritative entry point for Neuroglancer token
  authorization; the same bucket name on several datasets declares union
  semantics. For the end-to-end checklist that makes a bucket readable through
  Neuroglancer, see [Neuroglancer (ngauth) Bucket Setup](#neuroglancer-ngauth-bucket-setup).
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
| **Version** | Version string exactly as services send it (e.g., `v0.7`, `2026-01`). Unique within the dataset. |
| **Branch** | Release line; `main` unless versions fork. Ordinals compare only within a branch. |
| **Ordinal** | Optional integer order within the branch; when set, grants and **Is public** on this version also cover lower-ordinal versions of the same branch. Unique within the dataset and branch. |
| **Buckets** | The GCS buckets linked to this version (selected from the dataset's bucket list). Attaching a bucket to any version makes `/gcs_token` check access against those versions instead of the whole dataset. |
| **Prefix** | Optional path prefix within the bucket. Informational: tokens cover the whole bucket. |
| **Is public** | Grants anonymous callers and enabled human principals `view` access to this version and, when ordinals are set, its same-branch ancestors. It does not make the dataset-grain target public or grant write access. |

Terms are evaluated at the version a user requests. A version-grain TOS on a
public version does not follow `is_public` ancestry to that version's ancestors.
Use a dataset-grain TOS for a gated public release that must cover its ancestry
(or a service-and-dataset-scoped TOS when the gate is service-specific).

See [Setting Up Dataset Versions](#setting-up-dataset-versions) for the
procedure.

### Dataset translations

Map a service's own dataset names (and optionally versions) to DSG datasets
and versions. Needed only when a service's vocabulary differs from DSG's, such
as DVID node UUIDs or a neuPrint dataset named differently from DSG's.

| Field | Meaning |
|-------|---------|
| **Service** | The service whose requests are translated. |
| **Client name** | The dataset name that service sends. |
| **Client version** | The version string it sends. Blank for a name-level translation, which maps only the name; the version is then looked up among the target dataset's versions. |
| **Dataset** | The DSG dataset. |
| **Dataset version** | Required when **Client version** is set, blank otherwise; must belong to **Dataset**. |

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

## Setting Up Dataset Versions

A **dataset version** is a named, static release of a dataset, such as
`fish2` `v0.7`. Register one whenever a service asks DSG about a specific
version (neuPrint's `fish2:v0.7`, Clio's `dataset:version`) so that grants,
the public flag and TOS can be scoped to it. Field reference:
[Dataset versions](#dataset-versions) and
[Dataset translations](#dataset-translations).

### How a service's dataset reference resolves

Every authorization request names a service, a dataset name, and optionally a
version. neuPrintHTTP, for example, sends `fish2:v0.7` as name `fish2` plus
version `v0.7`, and plain `fish2` with no version. DSG resolves the pair in
this order (`resolve_dataset_reference` in `core/authz.py`):

1. A [Dataset translation](#dataset-translations) for that service with the
   same name and version: its dataset version.
2. A name-level translation for that service (no version): its dataset, then
   the version lookup in step 4 against that dataset.
3. Otherwise, the Dataset whose **Name** equals the requested name.
4. The version: the Dataset version whose **Version** equals the requested
   string exactly. Failing that, an all-digit string is read as an ordinal
   on the requested branch (default `main`). Anything else does not resolve.

A request without a version targets the dataset as a whole (dataset grain).

**An unregistered version is denied to everyone, admins included.** When the
version does not resolve, `POST /api/dsg/v1/authorize` answers `deny` before
looking at any grant or public flag. The response looks like any other
denial; only DSG's decision log records the reason (`unknown-translation`).
Register the version before pointing a service at it.

### Add a static version

Open **Core › Datasets › (dataset)** and add a row under **Dataset versions**
(or use **Core › Dataset versions › Add**):

| Field | What to enter |
|---|---|
| **Version** | Exactly the string the service sends, e.g. `v0.7`. Case and punctuation matter. Unique within the dataset. |
| **Branch** | `main` for a linear series of releases. |
| **Ordinal** | Blank unless grants or the public flag on this version should also reach earlier versions (see below). Unique within the dataset and branch. |
| **Buckets** | Usually none; see [Buckets](#buckets-and-neuroglancer-tokens). |
| **Prefix** | Optional note of where the version lives in its bucket. Not enforced: tokens always cover the whole bucket. |
| **Is public** | Leave unticked until the release is announced (see below). |

No grants are needed for existing users when their grants have a blank
**Dataset version**: a dataset-wide grant covers every version, including
ones added later. The import commands (`import_neuprint_auth`, `import_csv`)
create dataset-wide grants.

### Ordinals: how far a grant or the public flag reaches

An ordinal places versions of one branch in order (for example `6` for
`v0.6`, `7` for `v0.7`). A grant scoped to a version, or that version's
**Is public** flag, covers:

| The version's Ordinal | Covers |
|---|---|
| Blank | Only that exact version. |
| Set | That version and every same-branch version with a lower or equal ordinal. |

Neither ever covers the dataset as a whole: a request for plain `fish2` needs
a dataset-wide grant, or the dataset's **Access mode** set to `Public`. A
version without an ordinal is never covered by another version's reach.

Leave Ordinal blank by default. Set ordinals when a later release should
carry its audience back to earlier ones, for example so that making `v0.7`
public also publishes `v0.6`. Services whose **Version eval mode** is `DAG`
(intended for DVID) can receive `service_eval` decisions with ordinal anchors
for cross-branch questions; linear services (neuPrint, Clio) always get a
final answer.

### Make a version public

Tick **Is public** on the version. Anonymous callers, enabled users and
service accounts then get `view`, never write, on that version (and, with an
ordinal, its ancestors), subject to TOS. The dataset can stay `Closed`: its
other versions and plain dataset-grain requests remain invite-only.

### Terms of service

| Gate | Set up as | Applies to |
|---|---|---|
| Dataset-wide | TOS document with **Dataset** set, **Dataset version** and **Service** blank. Saving it makes it the dataset's **Tos**. | Every request for the dataset, any version |
| One version | TOS document with **Dataset version** set and **Dataset** blank | Requests that resolve to exactly that version |
| One service | TOS document with **Dataset** and **Service** set | That service's requests for the dataset |

Leave **Dataset** blank on a version TOS: a document with **Dataset** set and
no **Service** is saved as the dataset's **Tos**, which gates every version.

A version TOS does not follow ordinal reach: publishing `v0.7` with an
ordinal opens `v0.6` without `v0.7`'s version TOS. Use a dataset-wide TOS for
a gated release that must cover its ancestors.

### When a service needs a Dataset translation

Only when the service's names differ from DSG's. neuPrint and Clio usually
send the canonical dataset name and the version string, so most of their
datasets need none; an exception such as neuPrint's `manc` for DSG's `MANC`
needs a name-level translation. DVID sends node UUIDs, so fish2's DVID service
has translations mapping its UUIDs to `fish2` and to `fish2` `v0.6`. A
name-level translation (blank
**Client version**) renames the dataset but still needs a Dataset version
matching whatever version string the service sends.

### Buckets and Neuroglancer tokens

`/gcs_token` decides by bucket, not by the version a viewer is looking at. For
each Dataset bucket row with the requested name:

- **Attached to no version:** access is checked against the dataset as a
  whole. Dataset-wide grants and a `Public` dataset qualify; grants scoped to
  a version and public versions do not.
- **Attached to one or more versions:** access is checked against each
  attached version in turn, with the ordinal reach above. A grant on `v0.7`
  with ordinals reaches a bucket attached to `v0.6`; a grant on `v0.6` does
  not reach a bucket attached to `v0.7`.

Tokens cover the whole bucket, so keep one audience per bucket (see
[How the token path works](#how-the-token-path-works)). When a new version's
data goes into the same bucket, decide whether the bucket should stay
attached to the old version, move to the new one, or be attached to no
version. Detaching the last version switches that bucket back to
dataset-wide checks.

### Replace and retire a version (example: fish2 `v0.6` → `v0.7`)

1. Add the `v0.7` row (Version `v0.7`, Branch `main`, Ordinal blank). Do this
   before the service starts sending `fish2:v0.7`.
2. Dataset-wide grants carry over. Grants scoped to `v0.6`, and a TOS
   scoped to `v0.6`, do not: re-create them for `v0.7` if still wanted.
3. Review bucket attachments as described above.
4. Retire `v0.6` by unticking **Is public** (and setting **Retired date** on
   a version TOS, if any). Keep the row: services or old links may still ask
   for `fish2:v0.6`, and the row costs nothing.

**Deleting a version row is permanent and cascades** to its version-scoped
grants, service-account grants, Dataset translations and TOS documents, and
through those TOS documents to users' TOS acceptance records. The admin's
delete confirmation page lists everything that will go; read it before
confirming.

### Verify

Anonymous (no token): only public versions return `allow`.

```bash
curl -s -X POST https://dsg.janelia.org/api/dsg/v1/authorize \
  -H 'Content-Type: application/json' \
  -d '{"service":"neuprint-fish2","entries":[{"name":"fish2","version":"v0.7"}]}'
```

`deny` here means "not public" or "not registered"; the two look the same.
With a signed-in token (a user's or a service account's that can see the
dataset), list the registered rows:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://dsg.janelia.org/api/dsg/v1/datasets/fish2/versions?service=neuprint-fish2"
```

Each entry shows `version`, `branch`, `ordinal` and `is_public`. A user's own
decision for a version is the authorize call above with their token.

---

## Neuroglancer (ngauth) Bucket Setup

DatasetGateway is an [ngauth](https://github.com/google/neuroglancer/tree/master/src/datasource/ngauth)
server: Neuroglancer can open a private GCS bucket through it, with DSG
deciding who may read and Google Cloud doing the actual serving. Getting a
bucket to work takes a one-time deployment step plus four per-bucket steps
(the CORS step is usually optional), two of which are on the GCP side and
cannot be done from the DSG admin console. This section is the checklist. Placeholders: `GATEWAY_PROJECT` is
the GCP project that holds DSG's own identity, `BUCKET_PROJECT` is the project
that owns the bucket (often a different one), `BUCKET` is the bare bucket
name, and `https://viewer.example.org` is an origin that embeds Neuroglancer.

### Browser sign-in for registered web clients

Static web clients such as CODA can request a browser sign-in key through
`https://dsg.janelia.org/login?origin=https%3A%2F%2Fnavis-org.github.io&token=api`.
The user signs in to DSG, reviews the requesting site's name, owner, origin,
and the grant's expiry, then chooses **Allow** or **Cancel**. Allow is a
CSRF-protected POST; loading the page never creates a browser grant. Allow
posts `{token: …}` to the opener at the exact registered origin and closes
the popup. Cancel closes it without sending a message. The key is never
placed in a redirect URL.

To register a client, open **CORE → Registered clients → Add** in the
Django admin and set:

- **Name:** the name shown in the consent page, for example `CODA`.
- **Owner:** the maintainer's contact text, for example `Philipp Schlegel`.
- **Origin:** the exact HTTP(S) origin, for example
  `https://navis-org.github.io`, with no path, trailing slash, query, or
  fragment. A local development site can use `http://localhost:<port>`.
- **Enabled:** checked to permit issuance and remembered-key delivery.

Matching is exact, not a regular expression. An empty registered-client
table disables API-mode sign-in, and a missing or disabled client gets the
`"badorigin"` popup message. The registration list is independent of
`NGAUTH_ALLOWED_ORIGINS`; that setting continues to govern Neuroglancer's
GCS-only temporary-token handshake without `token=api`. No environment
setting is needed for browser API sign-in. **Allowed services** is stored
as a JSON list but is read-only and **reserved — not yet enforced**.

Web origins have no path component. In particular,
`https://navis-org.github.io` covers **every GitHub Pages site under the
navis-org organisation**, not just `/coda/`. Register an origin only when
that entire origin is trusted to receive delegated credentials.

A delegated key expires after `AUTH_COOKIE_AGE` (7 days by default) and can
sign requests to neuPrint and other DSG-protected services using the user's
service permissions. It does not inherit global administrator authority:
identity and permission-cache responses report `admin: false`, and the
native and legacy authorization endpoints do not take the global-admin
shortcut. DSG refuses it at the four token-management endpoints, SCIM,
and web-cookie authentication. It also cannot be used as a cookie to
create or retrieve another browser grant. Per-service restriction is not
implemented; the key can reach all DSG services the user can access.

**Don't ask again for this site** stores a consent for that user and
client. Later opens deliver the most recent live key for that client,
updating its last-used time; they never mint another key. Once no live key
remains, the consent page returns with the checkbox pre-ticked. Issuance
purges expired keys for that user/client and keeps at most 10 live keys,
evicting the oldest by creation time and ID. Other clients and the user's
normal login and programmatic tokens are unaffected.

On **My Account**, **Sites you have signed in from** lists browser grants
with their client, origin, creation time, expiry, and a **Revoke** action.
**Remembered sites → Forget this site** restores the consent prompt while
leaving issued keys valid. Revoking a key likewise leaves remembered
consent intact. Users can revoke or forget only their own entries.

To stop new issuance and remembered delivery, untick the client's
**Enabled** field; this takes effect on the next request. It does **not**
revoke issued keys. Revoke those separately from the user's account page,
or delete the client's API key rows in the admin. Disable rather than
delete a registration while keys remain: the foreign key uses `SET_NULL`,
so deleting the registration would remove their delegated-client marker.
Delete its keys before deleting the registration itself.

DSG rejects revoked keys immediately. neuPrintHTTP can continue accepting
a previously validated identity until its cache expires (300 seconds by
default), so allow that interval for revocation to reach cached requests.
Consent, delivery, cancel, badorigin, and login pages in API mode carry
`Cache-Control: no-store, private`, `Referrer-Policy: same-origin`,
`X-Frame-Options: DENY`, and `Cross-Origin-Opener-Policy: unsafe-none`.
The referrer policy is deliberately `same-origin` rather than `no-referrer`:
browsers send `Origin: null` on a form POST from a `no-referrer` document,
and Django's CSRF origin check would then refuse the consent form.

### How the token path works

A Neuroglancer layer source such as

```text
precomputed://gs+ngauth+https://dataset-gateway.mydomain.org/BUCKET/path/to/volume
zarr3://gs+ngauth+https://dataset-gateway.mydomain.org/BUCKET/
```

names the DSG host and the bucket. The client then:

1. Opens `/login?origin=<viewer origin>` in a popup. The user signs in with
   Google and the popup posts a short-lived user token back to the viewer.
   (`POST /token` is the cookie-based fallback for same-site callers.)
2. Posts `{token, bucket}` to `/gcs_token`. DSG resolves the bucket name to
   its **Dataset bucket** rows, authorizes the user from the dataset / grant /
   TOS model (see [Datasets](#datasets); the check runs with no service scope,
   so service-scoped grants do not count), and, if authorized, mints a GCS
   access token **from DSG's own GCP identity**, downscoped via the Security
   Token Service to `roles/storage.objectViewer` on that one bucket for about
   an hour.
3. Fetches objects directly from `storage.googleapis.com` with that token.
   DSG is not in the data path.

DSG never adds users to bucket IAM. (Earlier releases also synced per-user
`roles/storage.objectViewer` bindings; that subsystem was retired in
September 2026 and migration `0017` drops its `bucket_iam_binding` ledger.
Any user binding still present on a bucket was placed by hand or by that old
code and is not removed automatically.) Earlier releases still query that
table, so apply `0017` only while no older server process is running
(`git pull`, then `pixi run stop-serve && pixi run python manage.py migrate &&
pixi run serve-bg`), and before rolling back to an earlier release run
`pixi run python manage.py migrate core 0016` from this one; it recreates the
empty table.

Three consequences drive the steps below:

- **DSG's runtime identity must itself be able to read the bucket.** A
  downscoped token can never exceed what the identity holds, so if the
  identity has no binding on the bucket, DSG happily issues a token that GCS
  then rejects. The DSG log shows `decision=issued`; the viewer shows an
  authentication or permission error from `storage.googleapis.com`.
- **Issuance is whole-bucket.** The token reads every object in the bucket,
  so a bucket has exactly one audience: everyone authorized for any dataset
  the bucket is attached to. Attaching one bucket to several datasets means
  the union of their audiences. Never mix public and restricted data in one
  bucket; use a separate bucket instead.
- **The browser talks to GCS cross-origin.** Neuroglancer fetches through
  the GCS JSON API, which answers CORS for any origin, so bucket CORS
  configuration is not needed for Neuroglancer itself. It matters only if
  some other browser client reads the bucket through XML-API URLs.

### One-time: DSG's runtime GCP identity

DSG uses Google Application Default Credentials (ADC) for its only GCP call,
token minting. This identity is unrelated to the OAuth *client* used for login;
both may live in `secrets/` but they do different jobs.

1. Create a dedicated service account in the gateway project. Grant it no
   project-level roles; it gets per-bucket bindings only.

   ```bash
   gcloud iam service-accounts create dsg-ngauth --project=GATEWAY_PROJECT \
     --display-name="DatasetGateway ngauth runtime"
   ```

2. Make it DSG's ADC. On a plain host, download a JSON key into `secrets/`
   (mode 0600; the directory is git-ignored) and point `.env` at it:

   ```bash
   gcloud iam service-accounts keys create secrets/dsg-ngauth-key.json \
     --iam-account=dsg-ngauth@GATEWAY_PROJECT.iam.gserviceaccount.com
   chmod 600 secrets/dsg-ngauth-key.json
   echo 'GOOGLE_APPLICATION_CREDENTIALS=secrets/dsg-ngauth-key.json' >> .env
   ```

   Restart DSG to pick up the variable. On GCE/GKE, attach the service
   account to the workload instead of shipping a key. Without a usable ADC,
   `/gcs_token` answers `503 Credential service unavailable`.

3. Allow the viewer origins. `NGAUTH_ALLOWED_ORIGINS` (see
   [Environment Variables Reference](#environment-variables-reference)) is a
   regex that must *fully* match each embedding origin, e.g.
   `^https://(viewer|viewer-dev)\.example\.org$`. A reverse proxy in front of
   DSG must not add its own `Access-Control-Allow-Origin` header on the ngauth
   endpoints; DSG emits the correct one itself.

DSG serves `Cross-Origin-Opener-Policy: unsafe-none` on `/login`, `/auth/login`,
and every response under `/accounts/`, including redirects and error pages,
so the login popup keeps its opener through the whole Google round trip.
Everything else keeps `same-origin`. A reverse proxy must not add or replace
`Cross-Origin-Opener-Policy` on those paths.

### Per bucket, step 1: approve the contents

Before the runtime identity is granted on a bucket, confirm that
**everything** in it is meant for that bucket's whole (union) audience.
Listing the bucket and recording the approval in your rollout notes is
enough. Also confirm the bucket does not have Requester Pays enabled: DSG
sends no `userProject`, so a Requester Pays bucket cannot be served.

### Per bucket, step 2: register it in DatasetGateway

In the admin console, open (or create) the Dataset and add a **Dataset
bucket** whose name is the bare bucket name (`BUCKET`, no `gs://`). If access
is version-scoped, attach the bucket to the relevant Dataset versions too.
Then make sure the intended users are covered:

- a `Grant` or `Group dataset permission` with a blank **Service** field, or
  the dataset's **Access mode** set to `Public` (or a version marked
  **Is public**); and
- the dataset's TOS document, if any, accepted by each user. `/gcs_token`
  returns `tos_required` with a `tos_url` the viewer can open when acceptance
  is missing.

Registering first is safe: until step 4 the runtime identity cannot read the
bucket, so no data is reachable yet.

### Per bucket, step 3: bucket CORS (XML-API clients only)

Neuroglancer's `gs+ngauth` sources fetch through the JSON API
(`storage.googleapis.com/storage/v1/b/BUCKET/o/OBJECT?alt=media`), and GCS
answers CORS for any origin on that endpoint regardless of bucket
configuration. Verified: a bucket whose CORS listed only two origins still
served a third origin's JSON-API preflight and GET with a matching
`Access-Control-Allow-Origin`. Bucket CORS configuration applies to the XML
API (`storage.googleapis.com/BUCKET/OBJECT`), so set it only when some
browser client reads the bucket that way. Skip this step for a
Neuroglancer-only bucket.

```bash
cat > cors.json <<'JSON'
[{"origin": ["https://viewer.example.org"],
  "method": ["GET", "HEAD"],
  "responseHeader": ["Content-Type", "Range", "Authorization"],
  "maxAgeSeconds": 3600}]
JSON
gcloud storage buckets update gs://BUCKET --cors-file=cors.json
gcloud storage buckets describe gs://BUCKET --format="json(cors_config)"
```

List every origin that reads the bucket through the XML API. `Range` matters
because chunked formats issue range reads.

### Per bucket, step 4: grant the runtime identity (in the bucket's project)

Custom roles are project-scoped, so create the role once per bucket project
and reuse it for later buckets there. The role is read-only and deliberately
omits object create/delete **and** `storage.buckets.setIamPolicy`: the token
path never writes IAM, and an identity that can rewrite bucket policy is a
much larger blast radius than a read-serving gateway needs.

```bash
gcloud iam roles create dsgNgauthBucketManager --project=BUCKET_PROJECT \
  --title="DSG ngauth bucket manager" \
  --description="DatasetGateway ngauth runtime: read bucket metadata and objects. No create, delete, or setIamPolicy." \
  --permissions=storage.buckets.get,storage.objects.get,storage.objects.list \
  --stage=GA

gcloud storage buckets add-iam-policy-binding gs://BUCKET \
  --member=serviceAccount:dsg-ngauth@GATEWAY_PROJECT.iam.gserviceaccount.com \
  --role=projects/BUCKET_PROJECT/roles/dsgNgauthBucketManager
```

Notes:

- A freshly created custom role can take a minute to propagate. If the
  binding fails with *"Role … does not exist in the resource's hierarchy"*,
  retry unchanged.
- The predefined `roles/storage.objectViewer` also works for the token path.
  Roles created before September 2026 also carry
  `storage.buckets.getIamPolicy`, which only the retired per-user IAM
  reconcile used; it can be dropped with
  `gcloud iam roles update dsgNgauthBucketManager --project=BUCKET_PROJECT --remove-permissions=storage.buckets.getIamPolicy`.
- The binding is compatible with uniform bucket-level access and enforced
  public access prevention; a bucket-level grant to a service account is not
  public access.
- If the bucket's organization restricts sharing to specific domains, the
  gateway project must be inside an allowed organization. A successful
  `add-iam-policy-binding` confirms this.

### Verify

Every `/gcs_token` decision is logged at INFO regardless of `DSG_LOG_LEVEL`
(`dsg/serve.log` for `pixi run serve-bg`, otherwise the service journal):

```text
GCS token decision user=EMAIL bucket=BUCKET decision=issued reason=covered
```

`reason` is `covered` (a grant or group permission), `public`, or
`public-version`. Then open the layer in the viewer: first load should
prompt the ngauth login popup, after which chunks render.

| Symptom | Log `reason` / response | Cause and fix |
|---------|------------------------|---------------|
| Popup shows `badorigin`; `/token` or `/gcs_token` returns `403 Origin not allowed` | — | Viewer origin does not fully match `NGAUTH_ALLOWED_ORIGINS`. Fix the regex and restart. |
| `403 Access denied` | `unknown_bucket` | No Dataset bucket row has this exact name. Register it (step 2). |
| `403 Access denied` | `no_coverage` | User has no qualifying grant, group permission, or public coverage. Check the grant's **Service** field is blank. |
| `403 tos_required` | `missing_tos` | User has not accepted the dataset's TOS. Send them the returned `tos_url`. |
| `503 Credential service unavailable` | `adc_unavailable` | `GOOGLE_APPLICATION_CREDENTIALS` unset, unreadable, or not a service-account key. |
| `502 Credential exchange failed` | `sts_response_error` | STS rejected the exchange. Check the key is current and the service account is not disabled. |
| `decision=issued`, but the viewer reports an authentication or permission error from `storage.googleapis.com` | `issued` | The runtime identity is not granted on the bucket. Do step 4 and check `gcloud storage buckets get-iam-policy gs://BUCKET`. |
| `decision=issued`, browser console shows a CORS error on `storage.googleapis.com` | `issued` | Only possible for clients using XML-API URLs (`storage.googleapis.com/BUCKET/…`); Neuroglancer's JSON-API fetches are exempt from bucket CORS. Add the origin and the `Range`/`Authorization` response headers in step 3. |
| `decision=issued`, layer shows `…/info not found … HTTP error 404` (or `…/zarr.json … 404`) | `issued` | Auth is working: GCS answers 404 only to a caller that can read the bucket. The layer scheme does not match the data format. Use `precomputed://` for a volume that has an `info` file and `zarr3://` (or `zarr://`, which auto-detects) for a Zarr volume that has `zarr.json`; confirm the path with `gcloud storage ls gs://BUCKET/PATH`. |

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
| `GOOGLE_APPLICATION_CREDENTIALS` | (empty; Google ADC default chain) | Path to the service-account key DSG uses as its own GCP identity for ngauth token minting. See [Neuroglancer (ngauth) Bucket Setup](#neuroglancer-ngauth-bucket-setup). |
| `TOS_RETURN_ALLOWED_ORIGINS` | (empty) | Comma-separated exact HTTP(S) origins allowed as `/web/tos/service-check/` return targets. Origins accepted by `NGAUTH_ALLOWED_ORIGINS` are also valid returns; adding an origin here does not grant ngauth CORS access. |
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
| `bash scripts/manage.sh createcachetable dsg_cache_table` | Manually create the shared permission-cache table if migration fallback is needed. |
| `bash scripts/manage.sh changepassword EMAIL` | Reset a user's admin console password. |
| `bash scripts/manage.sh seed_permissions` | Create `view`, `edit`, `manage`, and `admin` permission types. |
| `bash scripts/manage.sh seed_groups` | Create default groups (`admin`, `sc`, `team_lead`, `user`). |
| `bash scripts/manage.sh import_csv FILE --dataset DS` | Import users from CSV and grant `view` on one dataset. |
| `bash scripts/manage.sh import_clio_auth FILE` | Import users, datasets, and grants from a Clio export JSON. Note: `--dry-run` is currently not a no-write preview. |
| `bash scripts/manage.sh import_neuprint_auth FILE --datasets DS [DS ...]` | Import neuPrint `authorized.json`. |
| `pixi run setup` | Interactive setup wizard — generates `.env`. |
| `pixi run serve` | Start the development server (runs setup if `.env` is missing). |
| `pixi run serve-bg` | Start the dev server detached; logs to `dsg/serve.log`, PID in `dsg/serve.pid`. |
| `pixi run deploy` | Build and deploy with Docker. |
| `pixi run stop-deploy` | Stop the Docker deployment. |
| `pixi run stop-serve` | Stop the detached development server (kills `serve.pid`, cleans up). |
