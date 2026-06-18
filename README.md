# DatasetGateway

Unified authorization service for neuroscience datasets.

DatasetGateway is a single Django service that centralizes dataset access control
across multiple platforms:

- **Neuroglancer** — implements the ngauth protocol for GCS token-based access
- **Clio and neuprint** — provides authorization APIs these services call to
  check user permissions
- **CAVE** — preliminary middle_auth-compatible endpoints are implemented;
  full support is planned pending CAVE deployment testing, token migration
  validation, and review
- **WebKnossos** — planned; will require coordination with ScalableMinds

## Quick start

### Prerequisites

- [pixi](https://pixi.sh)
- Docker (for production deployment only)
- A Google OAuth 2.0 client (for login — the setup wizard walks you through it)

### One-time setup

```bash
cd dsg
pixi install
pixi run setup              # interactive wizard — generates .env, runs migrations
```

### Option A: Local development

```bash
pixi run serve
```

Starts the Django dev server. If `.env` doesn't exist yet, the setup wizard
runs automatically.

To run detached (survives logout, logs to `dsg/serve.log`, PID in
`dsg/serve.pid`):

```bash
pixi run serve-bg
pixi run stop-serve   # to stop
```

### Option B: Docker production

```bash
pixi run deploy
```

Builds the Docker image, starts the container, runs migrations and seed
commands. Put a reverse proxy (nginx/caddy) in front for TLS.

### Option C: gunicorn behind an existing nginx (no Docker)

For a host that already has nginx terminating TLS (e.g. an emdata server),
run gunicorn directly under systemd instead of the dev `runserver`:

```bash
pixi run serve-prod   # gunicorn, DEBUG=False, WhiteNoise static; binds 127.0.0.1:8200
```

For a supervised process that restarts on crash/reboot, install the systemd
unit template at `scripts/datasetgateway.service` (see the header comments).
Point your nginx `location /` at `http://127.0.0.1:8200` and forward
`X-Forwarded-Proto $scheme` so Django's `SECURE_PROXY_SSL_HEADER` sees the
original HTTPS scheme. WhiteNoise serves `/static/` from within gunicorn, so
nginx needs no `location /static/` block.

The Django admin is at `/admin/`.

### Google OAuth setup

Login requires a Google OAuth 2.0 client. Without one the server runs but
all login/authorize links will fail with a `client_id` error. The setup
wizard (`pixi run setup`) will walk you through creating one if
`secrets/client_credentials.json` is missing.

Alternatively, you can set it up manually:

1. Go to the [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
   and create an OAuth 2.0 Client ID (type: Web application).
2. Add `http://localhost:8200/accounts/google/login/callback/` as an
   authorized redirect URI (and your production URI if known).
3. Download the JSON credentials and save them:

```bash
mkdir -p dsg/secrets
cp ~/Downloads/client_secret_*.json dsg/secrets/client_credentials.json
```

The `secrets/` directory is gitignored. For Docker production, prefer
`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` in `.env`, or mount a credentials
file and set `CLIENT_CREDENTIALS_PATH`; `dsg/.dockerignore` excludes local
`secrets/` from the image. Alternatively, you can set environment variables
instead of using the JSON file:

```bash
export GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com"
export GOOGLE_CLIENT_SECRET="your-client-secret"
```

## Authentication

All users authenticate via **Google OpenID Connect**. On successful login,
the server creates a DB-stored API key and sets it as the `dsg_token`
cookie. This single cookie is shared by all services in the ecosystem.

API requests are authenticated by checking for the token in this order:

1. `dsg_token` cookie
2. `Authorization: Bearer {token}` header
3. `?dsg_token=` query parameter

### How each platform authenticates

**CAVE services** (MaterializationEngine, AnnotationEngine, etc.) call
DatasetGateway's `/api/v1/user/cache` endpoint on every request to validate
the user's token and retrieve their permissions. DatasetGateway has a
preliminary implementation of the middle_auth-compatible endpoints, but it
is not yet declared supported until tested with a real CAVE deployment. For
a fresh deployment or planned migration where clients obtain DSG-minted
Bearer tokens and CAVE services point `AUTH_URL` / `STICKY_AUTH_URL` at
DatasetGateway, existing middle_auth_client Bearer-token flows should not
require service code changes. Existing cookie/query-token flows that depend
on `middle_auth_token` need a DSG login/token transition or compatibility
configuration because DatasetGateway uses `dsg_token`.

**Neuroglancer** uses the [ngauth protocol](https://github.com/google/neuroglancer/tree/master/ngauth_server).
Users log in via a popup that hits `/auth/login` → Google OAuth →
`dsg_token` cookie. Because Neuroglancer runs on a different origin
(e.g., `neuroglancer.org`), it cannot read the cookie directly. Instead
it calls `POST /token`, which reads the cookie server-side and returns a
short-lived token. Neuroglancer then exchanges that token for a
time-limited GCS access credential via `POST /gcs_token`, which grants
read access to the specific cloud storage bucket holding the dataset.

**Other services** (neuPrint, celltyping-light, Clio) validate users by
calling `/api/v1/user/cache` with the `dsg_token` value, the same way
CAVE services do. When all services share a cookie domain (configured
via `AUTH_COOKIE_DOMAIN`), users log in once and are authenticated
everywhere.

## Running tests

```bash
cd dsg
pixi run -e dev python -m pytest
```

## Production deployment

DatasetGateway is designed for a single-server Docker deployment behind a
reverse proxy that handles TLS.

```bash
cd dsg
pixi run setup    # generates .env interactively (set DJANGO_DEBUG=False for production)
pixi run deploy   # builds Docker image, starts container, runs migrations + seeds
```

Then create an admin user:

```bash
docker compose -f docker-compose.yml exec dsg python manage.py make_admin user@example.com
```

Put a reverse proxy (nginx or Caddy) in front for TLS, pointed at
`localhost:8080`. The setup wizard defaults `SECURE_SSL_REDIRECT=False`
since most deployments terminate TLS at the proxy.

The SQLite database and static files are stored in Docker volumes
(`dsg-data` and `dsg-static`) so they survive container
restarts. If you need PostgreSQL or Redis, swap the `DATABASES` / `CACHES`
settings and add services to `docker-compose.yml`.

### Without Docker (gunicorn + systemd)

On a host with its own nginx, skip Docker and run gunicorn under systemd
(see Option C above and `scripts/datasetgateway.service`). Production
prerequisites are the same either way:

- `DJANGO_DEBUG=False` — enables Secure cookies, HSTS, and generic error
  pages; Django then refuses to start unless `DJANGO_SECRET_KEY` and
  `DJANGO_ALLOWED_HOSTS` are set.
- `DJANGO_SECRET_KEY` — a strong random secret used to sign sessions, CSRF
  tokens, and password-reset/signed values. Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(64))"` and keep it
  out of source control.
- `collectstatic` runs automatically in `serve-prod.sh`; WhiteNoise serves the
  result, so the admin UI is styled without `runserver`'s DEBUG-only static.
- Keep `GUNICORN_WORKERS=1` while `CACHES` is the per-process `LocMemCache`
  (the permission cache is not shared across workers); raise it only after
  moving to a shared cache backend.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `DJANGO_SECRET_KEY` | insecure dev key | Secret key for sessions and CSRF. **Set in production.** |
| `DJANGO_DEBUG` | `True` | Set to `False` in production. |
| `DJANGO_ALLOWED_HOSTS` | `*` | Comma-separated list of allowed hostnames. |
| `DATABASE_PATH` | `db.sqlite3` | Path to SQLite database file. |
| `SECURE_SSL_REDIRECT` | `True` (prod) | Set to `False` if reverse proxy handles TLS. |
| `DSG_ORIGIN` | *(empty)* | Public origin for CSRF trusted origins (e.g., `https://dataset-gateway.mydomain.org`). |
| `DSG_PORT` | `8200` | Port for the development server. |
| `GOOGLE_CLIENT_ID` | *(empty)* | Google OAuth 2.0 client ID (overrides `client_credentials.json`). |
| `GOOGLE_CLIENT_SECRET` | *(empty)* | Google OAuth 2.0 client secret (overrides `client_credentials.json`). |
| `CLIENT_CREDENTIALS_PATH` | `secrets/client_credentials.json` | Alternative OAuth client credentials path. Useful when mounting credentials into Docker. |
| `NGAUTH_ALLOWED_ORIGINS` | `^https?://.*\.neuroglancer\.org$` | Regex for allowed CORS origins. |
| `AUTH_COOKIE_DOMAIN` | *(empty)* | Cookie domain for cross-subdomain auth (e.g., `.example.org`). |
| `PORT` | `8080` | Port for gunicorn (Docker). |
| `GUNICORN_WORKERS` | `2` | Number of gunicorn worker processes. |
| `LOG_LEVEL` | `info` | Gunicorn log level. |

## Documentation

- [Documentation index](docs/README.md) — status markers for living reference
  docs vs historical design records.
- [User manual](docs/user-manual.md) — setup, admin workflows, user
  workflows, management commands
- [CAVE auth endpoints](docs/cave-auth-endpoints.md) — CAVE API compatibility
  reference and SCIM 2.0 provisioning
- [Admin manual](docs/admin-manual.md) — administration and operational
  reference
- [Service accounts](docs/service-accounts.md) — non-human identity and token
  workflows
- [Design archive](docs/design/) — historical architecture and implementation
  records, not automatically synchronized with code changes
