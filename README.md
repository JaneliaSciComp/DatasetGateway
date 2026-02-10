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
3. Export the credentials before starting the server:

```bash
export GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com"
export GOOGLE_CLIENT_SECRET="your-client-secret"
python manage.py runserver
```

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
| `NGAUTH_SESSION_KEY` | *(empty)* | HMAC key for ngauth token signing (bytes). |
| `NGAUTH_ALLOWED_ORIGINS` | `^https?://.*\.neuroglancer\.org$` | Regex for allowed CORS origins. |
| `PORT` | `8080` | Port for gunicorn (Docker). |
| `GUNICORN_WORKERS` | `2` | Number of gunicorn worker processes. |
| `LOG_LEVEL` | `info` | Gunicorn log level. |

## Documentation

- [Architecture](docs/architecture.md) — system design, authorization model,
  deployment strategy
- [CAVE auth endpoints](docs/cave-auth-endpoints.md) — CAVE API compatibility
  reference and SCIM 2.0 provisioning
- [Implementation plan](docs/implementation-plan.md) — build plan with
  retrospective notes on what changed during implementation
