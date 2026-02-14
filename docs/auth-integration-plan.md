# Adding DatasetGate Auth to celltyping-light

Plan for integrating DatasetGate authentication into the celltyping-light
dashboard so that only authorized fish2 users can access it.

## Current State

celltyping-light is a FastAPI application
(`codebases/celltyping-light/celltyping_light/dashboard/backend/main.py`).

- **No authentication** — all endpoints are open to anyone.
- **neuPrint access** uses a shared `NEURON_TOKEN` environment variable
  (`runner/loaders/neuprint.py:_get_client()`). All users share Alice's
  token.
- **One existing middleware** — `NoCacheJSMiddleware`, a pure ASGI
  middleware that disables caching for JS files (`main.py` lines 520–540).
- **App factory** — `create_app()` at `main.py` line 497.

## Target State

Users must authenticate via DatasetGate (Google OAuth) and have `view`
permission on the `fish2` dataset before accessing celltyping-light.
Unauthenticated users are redirected to DatasetGate login. The `dsg_token`
cookie, shared across subdomains via `AUTH_COOKIE_DOMAIN`, carries the
user's identity to all fish2 services.

## Changes to celltyping-light

### 1. Auth middleware

Add a pure ASGI middleware (same pattern as `NoCacheJSMiddleware`) in a
new file `dashboard/backend/auth.py` (~80 lines):

```
Request arrives
  │
  ├── Path is /health or /static/* → pass through (no auth)
  │
  ├── No dsg_token cookie → 302 redirect to {AUTH_URL}/auth/login?next={url}
  │
  └── Has dsg_token cookie
        │
        ├── Check in-memory cache (keyed by token, TTL 5 min)
        │     └── Cache hit → attach user info to ASGI scope, continue
        │
        └── Cache miss → GET {AUTH_URL}/api/v1/user/cache
              with Authorization: Bearer {token}
              │
              ├── 200 + permissions_v2[AUTH_DATASET] includes "view"
              │     → cache result, attach user info, continue
              │
              ├── 200 but no "view" permission → 403 Forbidden
              │
              └── 401 or error → 302 redirect to login
```

The middleware calls DatasetGate's `/api/v1/user/cache` endpoint — the
same endpoint that every CAVE service uses via `middle_auth_client`.
DatasetGate returns the user's permissions, groups, and TOS status in a
single response.

### 2. Wire middleware into the app

In `main.py:create_app()`, add the auth middleware after
`NoCacheJSMiddleware`:

```python
if os.environ.get("AUTH_URL"):
    from .auth import DatasetGateAuthMiddleware
    app.add_middleware(DatasetGateAuthMiddleware)
```

Auth is only active when `AUTH_URL` is set, so local development works
without a running DatasetGate instance.

### 3. Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUTH_URL` | *(empty)* | DatasetGate base URL (e.g., `https://auth.janelia.org`). Empty = auth disabled. |
| `AUTH_DATASET` | `fish2` | Dataset name to check in `permissions_v2`. |

### 4. neuPrint token handling

**For now:** Keep `NEURON_TOKEN` as the server-side credential for
neuPrint calls. This works because celltyping-light's backend makes
neuPrint queries on behalf of the user using a service credential.

**Future (once neuPrint validates via DatasetGate):** Pass the user's
`dsg_token` to neuPrint instead of `NEURON_TOKEN`. This requires:
- The middleware to attach the user's token to `request.state`
- `_get_client()` to accept a per-request token parameter
- neuPrint (neuprintHTTP) to validate tokens against DatasetGate

This is a separate effort and doesn't block the initial integration.

## Cross-Subdomain Cookie Sharing

DatasetGate sets the `dsg_token` cookie with `AUTH_COOKIE_DOMAIN`
(e.g., `.janelia.org`). When celltyping-light runs on a sibling
subdomain (e.g., `celltyping.janelia.org`), the browser sends the cookie
automatically — no special configuration in celltyping-light.

If both services share a single host via path-based routing
(e.g., `host/auth/` and `host/app/`), cookies are shared naturally
without setting `AUTH_COOKIE_DOMAIN`.

## What Stays the Same

- All FastAPI routes and endpoint logic — unchanged
- neuPrint data loading (`runner/loaders/neuprint.py`) — unchanged
- Frontend JavaScript — unchanged (cookie is HttpOnly, handled by browser)
- Docker setup — unchanged (just add `AUTH_URL` env var)
- WebSocket connections — middleware only applies to HTTP requests
- Graph management and PostgreSQL schemas — unchanged
