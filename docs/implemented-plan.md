# DatasetGateway Implementation Record

> **Note:** This document was originally written as the build plan before
> implementation began. It has been updated to reflect what was actually
> built. Sections marked *"Implementation note (retrospective)"* record
> deviations, bugs, and fixes encountered during the build.

## Context

DatasetGateway is a greenfield Django authorization service for neuroscience datasets. It unifies authorization across CAVE, Neuroglancer, WebKnossos, neuprint, and Clio behind a single service. The project currently contains only documentation (Architecture.md, CAVE-auth-endpoints.md). This plan covers two deliverables: (1) updating CAVE-auth-endpoints.md to document SCIM 2.0 endpoints from CAVE's `scim` branch, and (2) implementing the entire Django authorization service.

---

## Deliverable 1: Update CAVE-auth-endpoints.md with SCIM 2.0

Add a new section documenting the SCIM 2.0 provisioning endpoints derived from the `scim` branch of middle_auth.

**Content to add (after the "Token Management" section):**

- Overview: SCIM 2.0 (RFC 7643/7644) provisioning for machine-to-machine management
- Base URL: `/{URL_PREFIX}/scim/v2` (default `/auth/scim/v2`)
- Authentication: Bearer token with super admin privileges (`scim_auth_required`)
- Discovery endpoints: `ServiceProviderConfig`, `ResourceTypes`, `Schemas`
- User CRUD: `GET/POST/PUT/PATCH/DELETE /auth/scim/v2/Users[/{scim_id}]`
- Group CRUD: `GET/POST/PUT/PATCH/DELETE /auth/scim/v2/Groups[/{scim_id}]`
- Dataset CRUD (custom resource): `GET/POST/PUT/PATCH/DELETE /auth/scim/v2/Datasets[/{scim_id}]`
- Schema URNs (core + neuroglancer extensions)
- ID mapping: UUID5 deterministic from internal IDs + `externalId` field
- Filtering: RFC 7644 filter expressions
- Pagination: 1-based `startIndex`/`count`
- SCIM error response format
- New model fields: `scim_id`, `external_id` on User, Group, Dataset
- Update "Replacement Strategies" section to note SCIM adds ~18 more endpoints

**File:** `CAVE-auth-endpoints.md`

---

## Deliverable 2: Django Authorization Service

### Architecture Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| API framework | Django REST Framework (DRF) | Serializers, ViewSets, auth classes, pagination, content negotiation |
| Multi-dataset routing | `DatasetContextMiddleware` strips `/{dataset}/{service_type}/` prefix | Matches Architecture.md recommendation; no CAVE service code changes |
| Token auth | Custom DRF `TokenAuthentication` class | Checks `dsg_token` cookie → `Bearer` header → `?dsg_token=` query param |
| SCIM endpoints | Separate Django app with DRF ViewSets | Custom renderer/parser for `application/scim+json` |
| Token storage | Database `APIKey` model + Django cache | Replaces CAVE's Redis; Django cache swappable to Redis later |
| ngauth tokens | HMAC-SHA256 encoded tokens (port from tos-ngauth) | Internal plumbing for Neuroglancer cross-origin `/token` flow only |
| TOS scope | Both dataset-wide and per-version (admin chooses) | Admin sets per TOSDocument whether it applies to entire dataset or specific version |
| GCS auth | Both bucket IAM + downscoped tokens | `/activate` adds user to bucket IAM; `/gcs_token` issues downscoped tokens for Neuroglancer |
| Dependency management | `pyproject.toml` | Modern Python packaging standard |

### Project Structure

```
dsg/                          # Project root
    manage.py
    pyproject.toml
    dsg/                      # Django project package
        __init__.py
        settings.py
        urls.py
        wsgi.py
        asgi.py
        middleware.py                 # DatasetContextMiddleware
    core/                             # Models, auth, shared utilities
        apps.py, models.py, admin.py
        authentication.py             # TokenAuthentication
        permissions.py                # DRF permission classes
        cache.py                      # build_permission_cache() — port of User.create_cache()
        management/commands/          # seed_permissions.py, seed_groups.py
        migrations/
        tests/
    cave_api/                         # CAVE compatibility (~10 critical endpoints)
        apps.py, urls.py, views.py
        oauth_views.py                # OAuth flow and token management
        tests/
    auth_api/                         # DatasetGateway authorization API
        apps.py, urls.py, views.py
        tests/
    ngauth/                           # Neuroglancer ngauth endpoints
        apps.py, urls.py, views.py
        tokens.py                     # HMAC token encode/decode (port from tos-ngauth auth.py)
        gcs.py                        # Downscoped token + IAM check (port from tos-ngauth)
        templates/ngauth/             # index.html, login_status.html, success.html
        tests/
    scim/                             # SCIM 2.0 provisioning
        apps.py, urls.py, views.py, serializers.py
        filters.py                    # SCIM filter → Django Q objects
        renderers.py, parsers.py      # application/scim+json
        pagination.py                 # 1-based SCIM pagination
        authentication.py             # Admin-only Bearer check
        utils.py                      # UUID5 ID generation
        tests/
    web/                              # User-facing web UI
        apps.py, urls.py, views.py, forms.py
        templates/web/                # base.html, datasets.html, tos_accept.html, etc.
        tests/
    conftest.py                       # Shared pytest fixtures
```

### Models (`core/models.py`)

Port from CAVE's SQLAlchemy models + Architecture.md extensions:

- **User** — `google_sub`, `email`, `display_name`, `is_active`, `admin`, `pi`, `gdpr_consent`, `parent` (FK self, for service accounts), `read_only`, `scim_id`, `external_id`
- **Group** — `name`, `scim_id`, `external_id`
- **UserGroup** — M:M through (`user`, `group`, `is_admin`)
- **Permission** — `name` (view, edit)
- **Dataset** — `name` (slug), `description`, `tos` (FK), `scim_id`, `external_id`
- **DatasetVersion** — `dataset` (FK), `version`, `gcs_bucket`, `prefix`, `is_public`
- **DatasetAdmin** — `user` (FK), `dataset` (FK)
- **GroupDatasetPermission** — `group`, `dataset`, `permission`
- **Grant** — `user`, `dataset`, `dataset_version` (nullable=all versions), `permission`, `granted_by`
- **ServiceTable** — `service_name`, `table_name`, `dataset` (FK)
- **TOSDocument** — `name`, `text`, `dataset` (FK nullable), `dataset_version` (FK nullable), `effective_date`, `retired_date`
- **TOSAcceptance** — `user`, `tos_document`, `accepted_at`, `ip_address`
- **APIKey** — `user`, `key`, `description`, `last_used`
- **PublicRoot** — `service_table` (FK), `root_id` (BigInteger) — for CAVE public data endpoints; managed via Django Admin and a dedicated web UI page where dataset admins can add/remove public root IDs per service table
- **AuditLog** — `actor`, `action`, `target_type`, `target_id`, `before_state` (JSON), `after_state` (JSON)

### Authentication Flow

**`core/authentication.py` — `TokenAuthentication`:**
1. Check `dsg_token` cookie
2. Check `Authorization: Bearer {token}` header
3. Check `?dsg_token=` query param
4. Look up token in `APIKey` table → get user → build permission cache
5. Cache result for 300s via `django.core.cache`

**`core/cache.py` — `build_permission_cache(user)`:**
Port of `User.create_cache()` from middle_auth. Must produce identical JSON:
```json
{
  "id": 42, "parent_id": null, "service_account": false,
  "name": "username", "email": "user@example.org", "admin": false,
  "pi": "", "affiliations": [...],
  "groups": ["group1"], "groups_admin": [],
  "permissions": {"fish2": 2},
  "permissions_v2": {"fish2": ["view", "edit"]},
  "permissions_v2_ignore_tos": {"fish2": ["view", "edit"]},
  "missing_tos": [{"dataset_id": 1, "dataset_name": "x", "tos_id": 1, "tos_name": "y"}],
  "datasets_admin": ["fish2"]
}
```

### Implementation Steps (one commit each)

**Step 1: Update CAVE-auth-endpoints.md with SCIM documentation**

**Step 2: Initialize Django project**
- Create `manage.py`, `pyproject.toml`, `dsg/settings.py`, `wsgi.py`, `asgi.py`
- Dependencies: Django, djangorestframework, google-auth, google-auth-oauthlib, google-cloud-storage, gunicorn, scim2-filter-parser, markdown, pytest-django
- Settings: SQLite, installed apps, DRF config, session/cache config

**Step 3: Define core models and migrations**
- All models in `core/models.py`
- Django Admin registration in `core/admin.py`
- Management commands: `seed_permissions` (view, edit), `seed_groups`
- Generate initial migration

**Step 4: Implement authentication system**
- `core/authentication.py`: `TokenAuthentication`
- `core/cache.py`: `build_permission_cache()` — faithful port of `User.create_cache()`
- `core/permissions.py`: `IsAdmin`, `IsDatasetAdmin`
- `dsg/middleware.py`: `DatasetContextMiddleware`

**Step 5: Implement `GET /api/v1/user/cache` (most critical CAVE endpoint)**
- `cave_api/views.py`: `UserCacheView`
- `cave_api/urls.py`
- Tests: valid token, invalid token, admin, permissions, TOS filtering

> *Implementation note (retrospective):* Three DRF compatibility issues
> surfaced while writing the Step 5 tests, all related to the custom `User`
> model not being a Django `AbstractBaseUser`:
>
> 1. **Missing `is_authenticated` property.** DRF's `IsAuthenticated`
>    permission checks `request.user.is_authenticated`. Our custom User model
>    didn't inherit from Django's auth user, so this attribute was absent.
>    Fixed by adding `@property is_authenticated` returning `True` to
>    `core/models.py:User`.
>
> 2. **401 vs 403 for unauthenticated requests.** DRF returns 403 (not 401)
>    when no authentication class claims the request, unless at least one
>    class defines `authenticate_header()`. Added
>    `authenticate_header(self, request)` returning `"Bearer"` to
>    `TokenAuthentication` (originally named `CaveTokenAuthentication`).
>
> 3. **Stale permission cache across tests.** Django's `locmem` cache
>    persists across test methods within a `TestCase`. A test that modified a
>    user's permissions would still see the old cached value. Fixed by
>    calling `cache.clear()` in `setUp()` of every test class, and also
>    before re-checking permissions within a single test (e.g., the TOS
>    filtering test that accepts TOS mid-test and re-queries).

**Step 6: Implement remaining CAVE API endpoints**
- `TableDatasetView`: `GET /api/v1/service/{ns}/table/{id}/dataset`
- `UserPermissionsView`: `GET /api/v1/user/{id}/permissions`
- `UsernameView`: `GET /api/v1/username?id=...`
- `UserListView`: `GET /api/v1/user?id=...`
- Public data: `TableHasPublicView`, `RootIsPublicView`, `RootAllPublicView` — backed by `PublicRoot` model with full DB queries

**Step 7: Implement CAVE OAuth flow and token management**

All views in `cave_api/oauth_views.py`:
- `AuthorizeView`: `GET/POST /api/v1/authorize` — Google OAuth redirect
- `OAuth2CallbackView`: `GET /api/v1/oauth2callback` — exchange code, create/update user, set `dsg_token` cookie
- `LogoutView`: `GET/POST /api/v1/logout`
- `CreateTokenView`: `POST /api/v1/create_token`
- `UserTokensView`: `GET /api/v1/user/token`
- `RefreshTokenView`: `GET /api/v1/refresh_token` (deprecated stub)

**Step 8: Implement ngauth endpoints**
- `ngauth/tokens.py`: Port HMAC token encode/decode from tos-ngauth `auth.py`
- `ngauth/gcs.py`: Port `check_storage_permission()`, `generate_bounded_access_token()`, `get_gcs_token_for_user()` from tos-ngauth
- `ngauth/views.py`:
  - `GET /` — Landing page with TOS
  - `GET /health` — Health check
  - `GET /auth/login`, `GET /auth/callback` — OAuth flow
  - `GET /login` — Login status / ngauth popup flow
  - `POST /logout`
  - `POST /activate` — TOS acceptance + bucket IAM provisioning
  - `GET /success`
  - `POST /token` — Cross-origin user token (CORS handling)
  - `POST /gcs_token` — Downscoped GCS access token
  - CORS preflight for `/token` and `/gcs_token`
- Templates: `index.html`, `login_status.html`, `success.html`

**Step 9: Implement SCIM infrastructure**
- `scim/renderers.py`, `scim/parsers.py`: `application/scim+json`
- `scim/pagination.py`: 1-based SCIM pagination
- `scim/authentication.py`: Bearer + admin check
- `scim/utils.py`: UUID5 ID generation
- `scim/filters.py`: Port `SCIMFilterParser` to produce Django `Q` objects

> *Implementation note (retrospective):* The `SCIMParser` was originally
> implemented to accept only `application/scim+json` (per the SCIM RFC).
> During Step 14 integration testing, this caused 415 Unsupported Media Type
> errors because DRF's test client and many real-world SCIM clients send
> `application/json`. Fixed by adding DRF's `JSONParser` as a fallback in
> `SCIMBaseView.parser_classes`, so SCIM views accept both content types.

**Step 10: Implement SCIM User and Group CRUD + discovery**
- `scim/serializers.py`: `UserSCIMSerializer`, `GroupSCIMSerializer`
- `scim/views.py`: `UserViewSet`, `GroupViewSet`
- Discovery: `ServiceProviderConfigView`, `ResourceTypesView`, `SchemasView`

**Step 11: Implement SCIM Dataset CRUD**
- `scim/serializers.py`: `DatasetSCIMSerializer` (with `serviceTables` management)
- `scim/views.py`: `DatasetViewSet`

**Step 12: Implement DatasetGateway authorization API**
- `auth_api/views.py`:
  - `GET /api/v1/whoami` — user identity + roles
  - `GET /api/v1/datasets` — list accessible datasets
  - `GET /api/v1/datasets/<slug>/versions` — list accessible versions
  - `POST /api/v1/authorize` — evaluate access (returns allow/deny + reason)

> *Implementation note (retrospective):* The plan specified
> `POST /api/v1/authorize` for the access-decision endpoint, but CAVE's
> OAuth endpoint (Step 7) already registers `GET/POST /api/v1/authorize`.
> Since both `cave_api` and `auth_api` are mounted at `api/v1/` in the main
> `urls.py`, Django's first-match-wins resolution meant the CAVE OAuth
> redirect always won, returning 302 instead of the expected JSON response.
>
> Fixed by renaming the DatasetGateway endpoint to `POST /api/v1/check-access`.
> The CAVE OAuth path is immovable (existing clients depend on it); the
> DatasetGateway path has no existing consumers yet and is free to change.

**Step 13: Implement web UI**
- `web/views.py`: Dataset browsing, TOS acceptance flow, "My datasets" history
- `web/views.py`: Team lead grant management
- `web/views.py`: Public root management page — dataset admins can view service tables for their dataset and add/remove public root IDs
- Django Admin customization (filters, search, inline editing, PublicRoot inline on ServiceTable)
- Templates

**Step 14: Deployment and final integration**
- Dockerfile, gunicorn config
- Environment variable documentation
- End-to-end integration tests

> *Implementation note (retrospective):* Integration tests (68 total) were
> the forcing function that uncovered the Step 9 SCIM content-type and
> Step 12 URL-conflict bugs described above. Both were fixed in the same
> commit as the tests themselves.

### URL Routing Summary

```python
# dsg/urls.py
urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/', include('cave_api.urls')),        # CAVE compat
    path('api/v1/', include('auth_api.urls')),         # DatasetGateway API
    path('auth/scim/v2/', include('scim.urls')),       # SCIM 2.0
    path('', include('ngauth.urls')),                  # ngauth (/, /token, etc.)
    path('web/', include('web.urls')),                 # Web UI
]
```

Multi-dataset prefix (`/{dataset}/{service_type}/`) stripped by `DatasetContextMiddleware` before URL resolution.

### Verification Plan

1. **Unit tests**: Models, `build_permission_cache()`, authentication, SCIM filters
2. **API compatibility tests**: Verify `/api/v1/user/cache` response matches exact JSON structure that `middle_auth_client` parses (fields: `id`, `name`, `email`, `admin`, `groups`, `permissions_v2`, `permissions_v2_ignore_tos`, `datasets_admin`, `missing_tos`, `service_account`, `parent_id`, `pi`, `affiliations`, `groups_admin`, `permissions`)
3. **SCIM tests**: CRUD operations, filter expressions, pagination, error responses
4. **ngauth tests**: HMAC token round-trip, CORS headers, GCS token flow mocking
5. **Integration**: `python manage.py test` across all apps
6. **Manual**: Run `python manage.py runserver`, test OAuth flow with Google credentials
