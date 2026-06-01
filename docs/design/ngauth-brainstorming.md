---
doc_status: brainstorm
sync_policy: Pre-decision brainstorm under review by the maintainer and Codex. NOT synchronized with code — nothing described here is implemented. Once a direction is chosen, supersede this doc or split the decisions into the relevant living docs.
last_reviewed: 2026-06-01
---

# ngauth in DatasetGateway — Brainstorming & Design Options

> **Status for reviewers (Bill + Codex):** This is a *pre-decision* brainstorm.
> It synthesizes the current state of DSG's ngauth support, how it compares to
> the `tos-ngauth` prototype, and the open design problem of supporting
> ngauth across **multiple datasets, multiple buckets, and multiple GCP
> projects**. No code has been written against any of this. The goal is to give
> us (and Codex) a single reviewable artifact before we commit to a course of
> action. File/line references point at `dsg/` as of this writing; verify
> against the code and tests, which are the source of truth.

---

## 0. Purpose & how to read this

DSG is intended to also act as an **ngauth server** for Neuroglancer — issuing
short-lived, downscoped GCS access tokens after gating on TOS + grants — in the
same spirit as the single-dataset `tos-ngauth` prototype
(`github.com/JaneliaSciComp/tos-ngauth`, cloned locally at `~/tos-ngauth`).

This doc has three analysis sections and three decision/action sections:

1. **What is actually implemented in DSG today** (§2)
2. **Operational parity with `tos-ngauth`** (§3) — access tracking, GCP API
   enablement, service-account provisioning, deployment model
3. **The multi-project / multi-bucket / multi-credential problem** (§4)
4. **The immediate test** against `gs://fibsem-ngauth` (§5)
5. **Open decisions** (§6)
6. **Candidate courses of action** (§7)

---

## 1. Background: the two codebases

| | `tos-ngauth` (prototype) | DatasetGateway (DSG) |
|---|---|---|
| Framework | FastAPI + Uvicorn | Django + allauth + DRF |
| Scope | **One dataset, one bucket** per deployment | **Many datasets, many buckets** (buckets can span GCP projects) |
| Config | `config.yaml` (TOML/YAML) + `secrets/` | Django DB + `.env`; datasets/buckets managed in Django admin & web UI |
| Persistence | Firestore (activation records) | Relational DB (**sqlite** by default), full audit log |
| Deploy target | Cloud Run (stateless) + Firestore | Long-running Docker container (`docker compose`) + sqlite volume |
| Runtime identity | One service account, attached on Cloud Run | One ambient ADC identity (see §4.1) |

The single-dataset vs. multi-dataset difference is the crux of everything in §4:
`tos-ngauth` can hardcode one bucket + one credential in config; DSG cannot.

---

## 2. What is actually implemented in DSG today

### 2.1 Endpoints & routing — ported, mounted at root

The ngauth app exists at `dsg/ngauth/` and is mounted at the **URL root**
(`dsg/dsg/urls.py:12` → `path("", include("ngauth.urls"))`), which is what the
`gs+ngauth+https://SERVER/...` scheme expects. Routes (`dsg/ngauth/urls.py`):
`/`, `/health`, `/auth/login`, `/login`, `/logout`, `/activate`, `/success`,
`/token`, `/gcs_token`.

### 2.2 Server-side token issuance — correct

`POST /gcs_token` (`dsg/ngauth/views.py:228`) is a faithful port:
decode the HMAC user token → `check_storage_permission` (direct bucket IAM read)
→ `generate_bounded_access_token` (STS downscope to `objectViewer` on the
bucket) → return token. The HMAC encode/decode (`dsg/ngauth/tokens.py`) matches
the prototype's `auth.py` format and has unit tests (`test_tokens.py`).

Notable improvement over the prototype: DSG does a **direct bucket IAM read**
(`bucket.get_iam_policy`, `dsg/ngauth/gcs.py:20`) rather than the Policy
Troubleshooter API. The STS form-encoding (`gcs.py:40`) is also cleaner
(single-encode vs. the prototype's double-encode trick).

### 2.3 The cross-origin login handshake — **MISSING (blocker for live Neuroglancer)**

The prototype delivers the user token to Neuroglancer via a **popup
`postMessage` handshake**. DSG has none of it — a repo-wide grep finds no
`postMessage` / `window.opener` anywhere. Three concrete gaps:

1. **`/login` doesn't do popup token delivery.** Prototype `routes.py:279`
   accepts `?origin=` and, when logged in, returns HTML that runs
   `window.opener.postMessage({token}, origin); window.close()`. DSG's
   `LoginStatusView` (`views.py:90`) renders a static "Logged in / Not logged
   in" page (`templates/ngauth/login_status.html`) — it ignores `origin` and
   never posts a token.
2. **No `state=origin` OAuth round-trip / no postMessage callback.** Prototype
   threads `origin` through OAuth `state` and postMessages from `/auth/callback`.
   DSG's `AuthLoginView` stores `oauth_next` and hands off to allauth, whose
   callback just redirects to `/web/datasets` (`allauth_adapter.py:34`).
3. **Auth cookie is `SameSite=Lax`, so the cross-site `/token` fallback also
   fails.** `cookie_middleware.py:34` hardcodes `samesite="Lax"`, so the
   `dsg_token` cookie is not sent on Neuroglancer's cross-site `POST /token`
   with credentials → 401. The prototype uses `SameSite=None` on https for
   exactly this reason.

Net: there is currently no working path for a different-origin Neuroglancer to
obtain the user token, which is the prerequisite for `/gcs_token`.

### 2.4 Config gotchas

- **Allowed-origins default won't match common clients.**
  `NGAUTH_ALLOWED_ORIGINS` defaults to `^https?://.*\.neuroglancer\.org$`
  (`settings.py:185`) — this does **not** match `neuroglancer-demo.appspot.com`
  or `localhost`. The prototype default matched localhost + the demo. Must set
  the env var to whatever origin we test from.
- **Auth cookie** is `SameSite=Lax`, `Secure = not DEBUG` (`settings.py:201`).

### 2.5 Test coverage

Only HMAC unit tests exist (`dsg/ngauth/tests/test_tokens.py`). No integration
test exercises `/login → /token → /gcs_token`, origin/CORS, or postMessage.

---

## 3. Operational parity with `tos-ngauth`

### 3.1 Access tracking — ✅ integrated into the Django backend (richer than Firestore)

The prototype's only persistent record is a Firestore doc per user
(`storage.py:record_activation` → `activations/{email}` = email, activated_at,
tos_version, dataset_id).

DSG replaces this with the relational DB. `ActivateView`
(`dsg/ngauth/views.py:144`) calls
`TOSAcceptance.objects.get_or_create(user, tos_document, ip_address)`, and
`TOSAcceptance` (`core/models.py:352`) carries `accepted_at` (auto timestamp) +
the `TOSDocument → Dataset` FK chain — so email, timestamp, dataset, and TOS
version are all captured, normalized rather than denormalized. On top of that,
DSG has a general `AuditLog` + `log_audit()` (`core/audit.py`) used across admin
mutations (grants, users, SCIM) with no prototype equivalent.

**Honest caveats:**
- Granularity matches the prototype, not finer. `/activate` records acceptance;
  `/gcs_token` (the actual token-issuance / data-access event) persists nothing
  — just `logger.info`, same as the prototype. And `/activate` itself does not
  call `log_audit()` (only creates the `TOSAcceptance` row).
- Backend is **sqlite** (`settings.py:86`, `DATABASE_PATH`-overridable) under a
  **long-running container** deploy (`scripts/deploy.py` → `docker compose up`),
  not Cloud Run. The prototype used Firestore *because* Cloud Run is stateless.
  So this is a different deployment model, not a like-for-like storage swap.

### 3.2 GCP API enablement — ❌ not ported

Repo-wide grep of `dsg/`: **no `gcloud services enable`, no `google.cloud`
imports outside `ngauth/gcs.py`, no Firestore, no Policy Troubleshooter.** DSG's
`scripts/setup.py` only collects settings → writes `.env` → checks for
`client_credentials.json` (manual OAuth) → runs `migrate`/`seed_*`. `deploy.py`
only builds + starts the container. There is **no GCP provisioning, no API
enablement, and no `verify-gcp` preflight** (the prototype has all of these in
`scripts/setup.py` and `scripts/verify_gcp_setup.py`).

DSG needs a *smaller* API surface than the prototype (direct IAM read instead of
Policy Troubleshooter → no `policytroubleshooter.googleapis.com`, no
`iam.securityReviewer`), but none of it is automated.

### 3.3 GCP service-account provisioning — ❌ not ported + terminology trap

The prototype's `setup.py` creates a GCP SA `access-gateway@PROJECT...`, grants
project roles (`datastore.user`, `serviceUsageConsumer`), grants bucket roles
(`storage.admin`, `iam.securityReviewer`), and downloads a key. DSG does **none**
of this.

⚠️ **Terminology trap:** DSG *has* a `ServiceAccount` model
(`core/models.py:438`), but it is a **DSG-internal non-human identity** — "no
Google OAuth" — holding long-lived bearer tokens for programmatic access to
DSG's *own* API. It is **not** a GCP IAM service account. Despite the matching
name, there is zero GCP-SA automation.

### 3.4 Comparison table

| Operation | `tos-ngauth` | DSG |
|---|---|---|
| Persist access/activation record | ✅ Firestore | ✅ relational (`TOSAcceptance` + `AuditLog`) |
| Enable GCP APIs | ✅ auto (8 APIs) | ❌ none |
| Create runtime GCP service account | ✅ auto | ❌ none (model name is unrelated) |
| Grant SA bucket roles | ✅ auto | ❌ none |
| Download / wire SA credentials | ✅ auto | ❌ manual/undocumented |
| Pre-deploy verification | ✅ `verify_gcp_setup.py` | ❌ none |
| OAuth client (manual in Console) | guided | guided (`setup.py`) |
| Multi-dataset / grants / groups / TOS / audit / admin UI / SCIM | ❌ | ✅ |

**Bottom line:** for *multi-dataset management*, DSG is far ahead. For the narrow
goal of *"get ngauth working against one bucket"*, DSG is currently **less
turnkey** than `tos-ngauth`, because exactly the GCP-provisioning steps the
prototype automated were not ported.

---

## 4. The multi-project / multi-bucket / multi-credential problem

This is the part that needs careful design, because DSG's multi-dataset nature
breaks an assumption the prototype could get away with.

### 4.1 The seam: `gcs.py` resolves exactly one identity

Every GCS operation goes through one of two unscoped calls:
- `storage.Client()` — `gcs.py:25, 108, 128` (for `get_iam_policy` /
  `set_iam_policy`)
- `google.auth.default()` — `gcs.py:47` (the subject token for STS downscoping)

Both resolve **one** ADC identity and **one** quota/billing project. And
`DatasetBucket` (`core/models.py:189`) stores only `dataset` + `name` — **no
project, no credential reference, no ngauth flag.** `sync_user_dataset_iam`
(`core/iam.py`) loops over *every* bucket on a dataset and calls these functions
blindly. So today there is nowhere to express "this bucket is in project X, use
credential Y, and it does/doesn't need ngauth."

### 4.2 Two non-obvious facts about cross-project

1. **One SA *can* work cross-project.** GCS bucket ops are addressed by global
   bucket name, and the downscope boundary already uses the project wildcard
   `//storage.googleapis.com/projects/_/buckets/{bucket}` (`gcs.py:58`). A single
   gateway SA granted `storage.admin` on a bucket in *any* project can read its
   IAM, add/remove users, and downscope tokens for it. You don't strictly need
   per-project credentials — you need the chosen identity *granted on each
   bucket*.
2. **Downscoping inherits.** The issued token is the gateway SA's own token,
   narrowed. Whatever credential DSG uses as the subject for a bucket must
   *itself* be able to read that bucket — downscoping narrows, never grants. So
   "which credential for which bucket" and "is that credential granted on the
   bucket" are the same question.

### 4.3 Credential strategies

**Option A — one cross-project gateway SA.** Keep `gcs.py` as-is; operationally
grant the single SA `storage.admin` on every bucket regardless of project.
*Pros:* simplest, zero new schema to function. *Cons:* one SA accumulates admin
across many projects (blast radius); many orgs forbid cross-project SA grants by
policy (domain-restricted sharing / VPC-SC), which would block this outright.

**Option B — per-project credential registry.** New `GCPProject(project_id,
credential_source)` table, `DatasetBucket.project → FK`, and `gcs.py` refactored
so every function resolves `(client, credentials)` from the bucket's project.
*Pros:* general, respects org boundaries, smaller blast radius. *Cons:* more
moving parts + a real "where do keys live" decision (Secret Manager refs /
mounted key files / per-project workload identity).

**Recommendation — build B's seam, default to A's behavior.** Refactor `gcs.py`
so there is exactly one `_resolve(bucket) -> (client, credentials)` helper that
every function calls; have it look up the bucket's project → credential and
**fall back to ADC when none is configured**. This makes the single-SA path work
today, isolates the multi-credential change to one function, and lets a
per-project key drop in later without re-plumbing. The cost of the seam now is
small; retrofitting it after 20 buckets across 5 projects is not.

### 4.4 Why "provision in the Django admin at bucket-creation" needs reframing

The instinct (from the maintainer) is right on two counts: provisioning **must
be decoupled from initial setup** (buckets are added later — the current
situation: a `DatasetBucket` was just added via Django admin against a running
DSG), and a **per-bucket ngauth flag belongs in the model**.

But there is an **identity bootstrap problem** with literally provisioning from
the admin save: the Django admin runs *inside the web container, as the gateway
runtime identity*. Granting that SA `storage.admin` on a bucket in a **new**
project requires `setIamPolicy` on that bucket — which the gateway SA, by
definition, does not yet have (that's the bootstrap). The only identity that can
grant it is the **operator's** credentials. So the web process cannot
self-provision a brand-new project; at best it can *detect that it can't* and
emit the exact command to run.

### 4.5 Proposed split: verify+instruct (in-app) vs. privileged-provision (out-of-band)

- **At bucket-add (admin hook / model `save`): verify + instruct, never
  mutate.** Detect the bucket's project and test whether the gateway identity
  can `get_iam_policy` (and read an object) on it. If yes → mark ready. If no →
  store status and surface the exact `gsutil iam ch …` / credential-wiring
  command. Safe to run as the gateway identity; fails fast.
- **Privileged provisioning: a management command run with the operator's own
  gcloud creds** — e.g. `python manage.py provision_bucket <name>` (or a
  `setup.py --gcp` flow). This creates/locates the SA and grants bucket roles,
  because only operator creds can.

### 4.6 Chicken-and-egg payoff

To auto-detect a bucket's project you already need `storage.buckets.get` on it —
so the **detection step *is* the preflight.** A bucket whose project DSG can't
read is exactly a bucket whose credentials aren't wired. Auto-detect on save
doubles as the readiness check.

### 4.7 Minimal schema additions (cheap, future-proof)

- `DatasetBucket.supports_ngauth` (bool) — so `sync_user_dataset_iam` and any
  source listing only touch ngauth buckets; public/otherwise-served buckets are
  skipped. (Today `sync_user_dataset_iam` touches all buckets blindly.)
- `DatasetBucket.gcp_project` (char, nullable, auto-detected on save) — needed to
  group credentials and to verify.
- `DatasetBucket.ngauth_status` + `last_checked` (readonly in admin) — surfaces
  "ready / needs grant / no creds" with the fix command. This is the UX the
  maintainer is reaching for.
- (Option B only) a `GCPProject` table mapping project → credential source;
  `DatasetBucket.project` → FK. Normalizes credentials per project instead of
  duplicating config per bucket.

### 4.8 Runtime identity permissions (whatever strategy)

For a bucket the gateway must serve via ngauth, the chosen identity needs, on
that bucket:
- `storage.buckets.getIamPolicy` — for `check_storage_permission`
- `storage.buckets.getIamPolicy` + `setIamPolicy` — for `add/remove_user_to_bucket`
- **its own object read** (`storage.objects.get`/`list`) — so the downscoped
  token can actually read (see §4.2.2)

`roles/storage.admin` on the bucket covers all three. (A custom role with just
those four permissions also works and is tighter.)

---

## 5. The immediate test: `gs://fibsem-ngauth` (large Zarr v2 image)

Independent of the multi-project design, a live Neuroglancer test needs:

1. **The login handshake gaps (§2.3) fixed** — otherwise Neuroglancer can't get
   a user token at all. *(Open: do we fix this before or after the test? As-is,
   the cross-origin flow won't complete.)*
2. **`gs://fibsem-ngauth` registered as data:** a `Dataset` with a
   `DatasetBucket(name="fibsem-ngauth")`, the test user holding a `Grant` (or
   group perm), TOS accepted, and IAM synced so the user lands in the bucket's
   `objectViewer` binding.
3. **The gateway runtime identity granted on the bucket** (§4.8) — *and this is
   where the same-vs-different-project question bites:*
   - **Same project as DSG's ADC/OAuth** → just grant the gateway identity on
     the bucket; multi-project problem doesn't apply to this test.
   - **Different project** → this test already exercises the cross-project
     credential issue; needs explicit provisioning first.
4. **`NGAUTH_ALLOWED_ORIGINS`** set to the Neuroglancer origin we test from
   (default won't match — §2.4).
5. **Bucket CORS** allowing the Neuroglancer origin (browser reads chunks
   directly from `storage.googleapis.com`).
6. **Zarr v2 specifics:** format is orthogonal to auth — point Neuroglancer at
   `zarr://gs+ngauth+https://SERVER/fibsem-ngauth/<path>` (or `zarr2://`). The
   only wrinkle is volume: Zarr issues many small chunk GETs, each using the
   downscoped token (~1h lifetime); CORS and token refresh matter more than for
   one big request. IAM propagation can take up to ~7 min.

---

## 6. Open decisions (for review by Bill + Codex)

1. **Credential model (§4.3):**
   (a) build the per-bucket resolution seam now, default to one SA / ADC;
   (b) one cross-project SA only;
   (c) full per-project registry now.
   *Constraint to confirm: does the GCP org forbid cross-project SA grants?*
2. **Where per-project keys live** (Option B): Secret Manager refs / mounted key
   files / per-project workload identity. (Docker-compose deploy makes mounted
   files or a per-project env map most realistic; WIF needs GKE/Cloud Run.)
3. **Provisioning execution model (§4.5):** management command with operator
   creds (recommended) vs. in-admin save hook (limited to verify+instruct) vs.
   external script/Terraform.
4. **`fibsem-ngauth` project (§5):** same project as DSG's runtime identity, or
   different? Determines whether the imminent test hits the multi-project path.
5. **Does the running DSG have any GCP credentials mounted right now,** or is it
   running with no ADC (in which case `/gcs_token` can't talk to GCS at all)?
6. **Sequencing:** fix the login handshake (§2.3) before or after the bucket
   provisioning work? They are independent but both required for an end-to-end
   Neuroglancer demo.

---

## 7. Candidate courses of action (phased, for discussion — not yet chosen)

These are options to react to, not a committed plan.

- **Phase 0 — unblock the test (smallest):** grant the gateway identity
  `storage.admin` on `gs://fibsem-ngauth` by hand; register the dataset/bucket +
  grant + TOS; set `NGAUTH_ALLOWED_ORIGINS` + bucket CORS. Verify `/gcs_token`
  returns a token via curl (bypasses the broken login UI). Confirms the GCS half
  works before investing in UI/provisioning.
- **Phase 1 — login handshake (§2.3):** origin-aware `/login` + `/auth/callback`
  postMessage, `SameSite=None` auth cookie, sane `NGAUTH_ALLOWED_ORIGINS`
  default, plus an integration test driving `/login → /token → /gcs_token`.
- **Phase 2 — credential seam (§4.3 rec.):** refactor `gcs.py` to
  `_resolve(bucket)`; add `DatasetBucket.supports_ngauth` + `gcp_project` +
  `ngauth_status`; auto-detect project on save (doubles as preflight, §4.6).
- **Phase 3 — provisioning UX (§4.5):** `manage.py provision_bucket` (operator
  creds) for privileged grants; admin "Verify ngauth access" action / readonly
  status for verify+instruct; optional `GCPProject` registry for Option B.
- **Phase 4 — parity polish (§3.2/3.3):** fold GCP API-enablement + SA
  provisioning into `setup.py`/a `verify-gcp` command so DSG-as-ngauth is as
  turnkey as `tos-ngauth`, scoped to what the direct-IAM approach needs.

---

## 8. Reference: key files & lines (as of writing)

DSG (`dsg/`):
- `dsg/urls.py:12` — ngauth mounted at root
- `ngauth/urls.py` — route list
- `ngauth/views.py:90` — `LoginStatusView` (static; no origin/postMessage)
- `ngauth/views.py:144` — `/activate` writes `TOSAcceptance`
- `ngauth/views.py:228` — `/gcs_token`
- `ngauth/gcs.py:25,108,128` — `storage.Client()`; `:47` — `google.auth.default()`; `:58` — downscope resource wildcard
- `ngauth/tokens.py` — HMAC token (matches prototype `auth.py`)
- `core/models.py:189` — `DatasetBucket` (only `dataset` + `name`)
- `core/models.py:352` — `TOSAcceptance`
- `core/models.py:438` — `ServiceAccount` (DSG-internal, NOT a GCP SA)
- `core/iam.py` — `sync_user_dataset_iam` (iterates all buckets)
- `core/cookie_middleware.py:34` — `samesite="Lax"`
- `dsg/settings.py:86` — sqlite; `:185` — `NGAUTH_ALLOWED_ORIGINS` default; `:201` — `AUTH_COOKIE_SECURE`
- `core/allauth_adapter.py:34` — login redirect to `/web/datasets`
- `scripts/setup.py`, `scripts/deploy.py` — no GCP provisioning

`tos-ngauth` (`~/tos-ngauth`):
- `src/access_gateway/routes.py:279` — `/login` postMessage; `:138` — `/auth/callback` postMessage
- `src/access_gateway/storage.py` — Firestore `record_activation`
- `src/access_gateway/ngauth.py` — token issuance (Policy Troubleshooter variant)
- `scripts/setup.py` — enables 8 APIs, creates SA, grants project + bucket roles, downloads key
- `scripts/verify_gcp_setup.py` — 7-check preflight
- `GCP-SETUP.md`, `Specifications.md` — automated-vs-manual matrix, endpoint spec
