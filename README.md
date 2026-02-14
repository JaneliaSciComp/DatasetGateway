# DatasetGate

Unified authorization service for neuroscience datasets.

DatasetGate is a single Django service that centralizes dataset access control
across multiple platforms:

- **CAVE** — drop-in replacement for middle_auth with compatible API endpoints
- **Neuroglancer** — implements the ngauth protocol for GCS token-based access
- **Clio and neuprint** — provides authorization APIs these services call to
  check user permissions
- **WebKnossos** — planned; will require building compatible APIs based on
  their open source code, similar to the CAVE integration approach

## Quick start

Prerequisites: Python 3.11+

```bash
cd datasetgate
pip install -e ".[dev]"
python manage.py migrate
python manage.py seed_permissions
python manage.py seed_groups
python manage.py runserver
```

The server starts at http://localhost:8000. The Django admin is at `/admin/`.

### Google OAuth setup

Login requires a Google OAuth 2.0 client. Without one the server runs but
all login/authorize links will fail with a `client_id` error.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
   and create an OAuth 2.0 Client ID (type: Web application).
2. Add `http://localhost:8000/api/v1/oauth2callback` and
   `http://localhost:8000/auth/callback` as authorized redirect URIs.
3. Download the JSON credentials and drop the file into the project:

```bash
mkdir -p datasetgate/secrets
cp ~/Downloads/client_secret_*.json datasetgate/secrets/client_credentials.json
python manage.py runserver
```

The `secrets/` directory is gitignored. Alternatively, you can set environment
variables instead of using the JSON file:

```bash
export GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com"
export GOOGLE_CLIENT_SECRET="your-client-secret"
```

## Authentication

All users authenticate via **Google OpenID Connect**. There are two OAuth
entry points that both produce the same result:

- `/auth/login` — used by Neuroglancer's popup login flow
- `/api/v1/authorize` — used by CAVE clients and browser-based apps

On successful login, the server exchanges the Google authorization code
for an ID token, verifies it, creates (or updates) the user record, and
generates a DB-stored API key. The key is set as the `dsg_token` cookie.

API requests are authenticated by the `TokenAuthentication` class, which
checks for the token in this order:

1. `dsg_token` cookie
2. `Authorization: Bearer {token}` header
3. `?dsg_token=` query parameter

For Neuroglancer's cross-origin use case, `POST /token` reads the
`dsg_token` cookie and returns a short-lived HMAC-signed token that
Neuroglancer passes to `POST /gcs_token` for downscoped GCS access.

## Running tests

```bash
cd datasetgate
python -m pytest
```

## Docker

```bash
docker build -t datasetgate datasetgate/
docker run -p 8080:8080 datasetgate
```

The container runs migrations automatically on startup.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `DJANGO_SECRET_KEY` | insecure dev key | Secret key for sessions and CSRF. **Set in production.** |
| `DJANGO_DEBUG` | `True` | Set to `False` in production. |
| `DJANGO_ALLOWED_HOSTS` | `*` | Comma-separated list of allowed hostnames. |
| `GOOGLE_CLIENT_ID` | *(empty)* | Google OAuth 2.0 client ID. |
| `GOOGLE_CLIENT_SECRET` | *(empty)* | Google OAuth 2.0 client secret. |
| `NGAUTH_ALLOWED_ORIGINS` | `^https?://.*\.neuroglancer\.org$` | Regex for allowed CORS origins. |
| `AUTH_COOKIE_DOMAIN` | *(empty)* | Cookie domain for cross-subdomain auth (e.g., `.example.org`). |
| `PORT` | `8080` | Port for gunicorn (Docker). |
| `GUNICORN_WORKERS` | `2` | Number of gunicorn worker processes. |
| `LOG_LEVEL` | `info` | Gunicorn log level. |

## Documentation

- [Architecture](docs/architecture.md) — system design, authorization model,
  deployment strategy
- [CAVE auth endpoints](docs/cave-auth-endpoints.md) — CAVE API compatibility
  reference and SCIM 2.0 provisioning
- [Implementation record](docs/implemented-plan.md) — what was built,
  with retrospective notes on deviations from the original plan
