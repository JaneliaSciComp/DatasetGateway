# Celltyping-Light Auth Integration — Plan

Reference document for adding authorization to
[celltyping-light](https://github.com/), a FastAPI dashboard for fish2 cell
type clustering. Implementation happens after DatasetGate main is stable.

## Goal

Gate access to the celltyping-light dashboard so only users with `view`
permission on the `fish2` dataset can use it. Two approaches are available
depending on whether the team wants to depend on DatasetGate.

---

## Approach 1: DatasetGate / CAVE Integration (Recommended)

Add an ASGI middleware (~80 lines) that validates the user's auth cookie
against DatasetGate's CAVE-compatible API.

### How It Works

1. Middleware reads the `middle_auth_token` cookie from the request.
2. If missing → 302 redirect to `{AUTH_URL}/api/v1/authorize?redirect={current_url}`.
3. If present → GET `{AUTH_URL}/api/v1/user/cache` with
   `Authorization: Bearer {token}`.
4. Check `permissions_v2[DATASET_NAME]` includes `"view"`.
5. Cache the validation result in-memory (keyed by token, TTL ~5 min).
6. Attach user info to `request.state.user`.
7. If user lacks permission → 403.
8. Static files (`/static/*`) and health checks are exempted.
9. WebSocket upgrade requests check the cookie before accepting.

### Environment Variables

| Variable       | Default | Purpose                                      |
|----------------|---------|----------------------------------------------|
| `AUTH_URL`     | (empty) | DatasetGate base URL. Empty = auth disabled. |
| `AUTH_DATASET` | `fish2` | Dataset name to check permissions for.       |

### Feature Toggle

Auth is off by default. Set `AUTH_URL` to enable. This keeps the app usable
in development without a running auth server.

### App Wiring

In `main.py` `_create_app_impl()`:

```python
auth_url = os.environ.get("AUTH_URL", "")
if auth_url:
    from celltyping_light.dashboard.backend.auth import CaveAuthMiddleware
    app.add_middleware(CaveAuthMiddleware, auth_url=auth_url, dataset="fish2")
```

CLI argument `--auth-url` can also be added to `cli.py`.

### Portability

Because DatasetGate exposes CAVE-compatible endpoints
(`/api/v1/user/cache`, `/api/v1/authorize`), this middleware works
unchanged with:
- DatasetGate at Janelia
- Native middle_auth at any other CAVE-using institute

No code changes needed to switch between the two.

---

## Approach 2: Standalone Auth (No External Dependencies)

If the team prefers not to depend on DatasetGate, auth can be added
directly to the FastAPI app.

### How It Works

1. Add Google OAuth login directly to the FastAPI app using
   `authlib` or `httpx-oauth`.
2. Store sessions in signed cookies (FastAPI `SessionMiddleware`).
3. Maintain an `allowed_emails` list in config (env var or JSON file).
4. On each request, check if the session email is in the allowlist.

### Scope

~150 lines of new Python code. No external service dependencies.

### Limitations

- Manual email allowlist management (no self-service TOS acceptance).
- No integration with CAVE's permission model.
- Each app manages its own auth — no single sign-on across tools.

---

## Trade-offs

| Dimension                  | Approach 1: DatasetGate       | Approach 2: Standalone           |
|----------------------------|-------------------------------|----------------------------------|
| **New code**               | ~80 lines (middleware only)   | ~150 lines (OAuth + sessions)    |
| **External dependency**    | DatasetGate must be running   | None                             |
| **User management**        | Central (DatasetGate web UI)  | Manual (email allowlist)         |
| **Permission model**       | Dataset + version scoped      | Binary allow/deny                |
| **TOS gating**             | Built-in (DatasetGate)        | Not included                     |
| **Single sign-on**         | Yes (shared cookie)           | No                               |
| **Works at other CAVE sites** | Yes (CAVE-compatible API)  | No                               |
| **Dev setup complexity**   | Needs running DatasetGate     | Self-contained                   |
| **Feature toggle**         | `AUTH_URL` env var            | `REQUIRE_AUTH` env var           |

---

## Implementation Plan (Approach 1)

Happens on a `datasetgate-auth` branch of the celltyping-light fork, after
DatasetGate main is stable.

1. Create `celltyping_light/dashboard/backend/auth.py` — ASGI middleware.
2. Wire into `main.py` with env var toggle.
3. Add `--auth-url` CLI argument.
4. Test end-to-end with DatasetGate running locally.
5. Document setup in celltyping-light README.

### Prerequisites from DatasetGate

- `next` parameter on OAuth flow (so redirect back works).
- `AUTH_COOKIE_DOMAIN` for cross-subdomain cookie sharing (if deployed on
  sibling subdomains).
- A `fish2` dataset with TOS and permissions configured.
