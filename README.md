# DatasetGateway

Unified authorization service for neuroscience datasets.

DatasetGateway is a single Django service that centralizes dataset access control
across multiple platforms:

- **CAVE** — drop-in replacement for middle_auth with compatible API endpoints
- **Neuroglancer** — implements the ngauth protocol for GCS token-based access
- **Clio and neuprint** — provides authorization APIs these services call to
  check user permissions
- **WebKnossos** — planned; will require building compatible APIs based on
  their open source code, similar to the CAVE integration approach

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

### Option B: Docker production

```bash
pixi run deploy
```

Builds the Docker image, starts the container, runs migrations and seed
commands. Put a reverse proxy (nginx/caddy) in front for TLS.

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

The `secrets/` directory is gitignored. Alternatively, you can set environment
variables instead of using the JSON file:

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
the user's token and retrieve their permissions. This is a drop-in
replacement for CAVE's original `middle_auth` server — CAVE services
only need their `AUTH_URL` environment variable pointed at DatasetGateway.
Users log in via `/api/v1/authorize`, which redirects through Google
OAuth and sets the `dsg_token` cookie.

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
| `NGAUTH_ALLOWED_ORIGINS` | `^https?://.*\.neuroglancer\.org$` | Regex for allowed CORS origins. |
| `AUTH_COOKIE_DOMAIN` | *(empty)* | Cookie domain for cross-subdomain auth (e.g., `.example.org`). |
| `PORT` | `8080` | Port for gunicorn (Docker). |
| `GUNICORN_WORKERS` | `2` | Number of gunicorn worker processes. |
| `LOG_LEVEL` | `info` | Gunicorn log level. |

## Documentation

- [User manual](docs/user-manual.md) — setup, admin workflows, user
  workflows, management commands
- [Architecture](docs/architecture.md) — system design, authorization model,
  deployment strategy
- [CAVE auth endpoints](docs/cave-auth-endpoints.md) — CAVE API compatibility
  reference and SCIM 2.0 provisioning
- [Implementation record](docs/implemented-plan.md) — what was built,
  with retrospective notes on deviations from the original plan
- [Admin manual](docs/admin-manual.md) — administration and operational
  reference
