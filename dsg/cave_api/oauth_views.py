"""CAVE OAuth flow and token management views."""

import logging
import secrets
import unicodedata
from contextlib import nullcontext
from datetime import timedelta
from threading import Lock
from time import sleep
from urllib.parse import urlsplit

from django.conf import settings
from django.db import DatabaseError, OperationalError, connection, transaction
from django.http import HttpResponseRedirect, JsonResponse
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import APIKey, User
from core.permissions import IsHumanUser

DEFAULT_LONG_LIVED_TOKEN_DESCRIPTION = "Default long-lived API token"
LOGIN_TOKEN_DESCRIPTION = "OAuth login token"
LOGIN_TOKEN_LIMIT = 10
REDIRECT_ALLOWED_DOMAIN = "janelia.org"

logger = logging.getLogger(__name__)
_SQLITE_LOGIN_LOCK = Lock()


def validate_redirect_url(redirect_url):
    """Return an allowed absolute redirect URL, or the safe root fallback."""
    if not isinstance(redirect_url, str):
        return "/"
    if any(unicodedata.category(char) == "Cc" for char in redirect_url):
        return "/"
    if redirect_url.startswith("//"):
        return "/"

    try:
        parsed = urlsplit(redirect_url)
        hostname = parsed.hostname
        has_credentials = parsed.username is not None or parsed.password is not None
    except (TypeError, ValueError):
        return "/"

    if parsed.scheme not in {"http", "https"} or not hostname or has_credentials:
        return "/"
    if hostname != REDIRECT_ALLOWED_DOMAIN and not hostname.endswith(
        f".{REDIRECT_ALLOWED_DOMAIN}"
    ):
        return "/"
    return redirect_url


def _upsert_user_and_create_login_token_atomic(email, defaults, presented_token):
    with transaction.atomic():
        user, _ = User.objects.update_or_create(email=email, defaults=defaults)
        locked_user = User.objects.select_for_update().get(pk=user.pk)
        login_tokens = APIKey.objects.filter(
            user=locked_user,
            description=LOGIN_TOKEN_DESCRIPTION,
        )

        if presented_token:
            login_tokens.filter(key=presented_token).delete()

        now = timezone.now()
        login_tokens.filter(expires_at__lte=now).delete()

        eviction_count = max(0, login_tokens.count() - (LOGIN_TOKEN_LIMIT - 1))
        if eviction_count:
            oldest_ids = list(
                login_tokens.order_by("created", "pk").values_list(
                    "pk", flat=True
                )[:eviction_count]
            )
            APIKey.objects.filter(pk__in=oldest_ids).delete()

        api_key = APIKey.objects.create(
            user=locked_user,
            description=LOGIN_TOKEN_DESCRIPTION,
            expires_at=now + timedelta(seconds=settings.AUTH_COOKIE_AGE),
        )
        return locked_user, api_key


def upsert_user_and_create_login_token(email, defaults, presented_token=None):
    """Update a user and atomically issue a bounded per-device session."""
    # SQLite has no per-user row locks. Serialize callbacks within this process;
    # the retry below handles a writer in another process.
    lock = _SQLITE_LOGIN_LOCK if connection.vendor == "sqlite" else nullcontext()
    with lock:
        for attempt in range(5):
            try:
                return _upsert_user_and_create_login_token_atomic(
                    email,
                    defaults,
                    presented_token,
                )
            except OperationalError as error:
                sqlite_lock_error = (
                    connection.vendor == "sqlite"
                    and "locked" in str(error).lower()
                )
                if not sqlite_lock_error or attempt == 4:
                    raise
                # Retry the whole rolled-back transaction.
                sleep(0.01 * (attempt + 1))


def get_or_create_default_long_lived_token(user):
    """Return the user's stable long-lived API token, creating it if missing.

    The token is identified by a reserved description and a NULL expires_at.
    Idempotent: subsequent calls return the same token row.
    """
    api_key = (
        APIKey.objects.filter(
            user=user,
            description=DEFAULT_LONG_LIVED_TOKEN_DESCRIPTION,
            expires_at__isnull=True,
        )
        .order_by("created")
        .first()
    )
    if api_key is None:
        api_key = APIKey.objects.create(
            user=user,
            description=DEFAULT_LONG_LIVED_TOKEN_DESCRIPTION,
            expires_at=None,
        )
    return api_key


class AuthorizeView(APIView):
    """GET/POST /api/v1/authorize

    Initiates Google OAuth flow. Returns authorization URL for
    programmatic clients (when X-Requested-With header is set) or
    redirects the browser.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return self._initiate_oauth(request)

    def post(self, request):
        return self._initiate_oauth(request)

    def _initiate_oauth(self, request):
        from urllib.parse import urlencode

        redirect_url = request.query_params.get("redirect")
        if redirect_url is None:
            redirect_url = request.data.get("redirect", "/")
        redirect_url = validate_redirect_url(redirect_url)

        # Store redirect URL and optional tos_id in session
        request.session["oauth_redirect"] = redirect_url
        tos_id = request.query_params.get("tos_id")
        if tos_id:
            request.session["oauth_tos_id"] = tos_id

        # Store optional service + dataset for post-login TOS interception
        service = request.query_params.get("service")
        dataset = request.query_params.get("dataset")
        if service:
            request.session["oauth_service"] = service
        if dataset:
            request.session["oauth_dataset"] = dataset

        callback_url = request.build_absolute_uri("/api/v1/oauth2callback")

        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": callback_url,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "offline",
            "state": secrets.token_urlsafe(32),
        }
        request.session["oauth_state"] = params["state"]

        # If user already has a valid dsg_token, pass login_hint to skip
        # Google's account selector on re-auth (e.g., TOS acceptance flows).
        token = request.COOKIES.get(settings.AUTH_COOKIE_NAME)
        if token:
            try:
                api_key = APIKey.objects.select_related("user").get(key=token)
                params["login_hint"] = api_key.user.email
            except APIKey.DoesNotExist:
                pass

        auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"

        # Programmatic clients get the URL in the response
        if request.META.get("HTTP_X_REQUESTED_WITH"):
            return Response({"authorization_url": auth_url})

        return HttpResponseRedirect(auth_url)


class OAuth2CallbackView(APIView):
    """GET /api/v1/oauth2callback

    Google OAuth callback. Exchanges code for token, creates/updates
    user, sets dsg_token cookie, redirects to original URL.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        error = request.query_params.get("error")

        if error:
            return Response({"error": error}, status=400)

        if not code:
            return Response({"error": "Missing authorization code"}, status=400)

        # Verify state
        expected_state = request.session.get("oauth_state")
        if state != expected_state:
            return Response({"error": "Invalid state parameter"}, status=400)

        # Exchange code for tokens
        callback_url = request.build_absolute_uri("/api/v1/oauth2callback")
        token_data = self._exchange_code(code, callback_url)
        if token_data is None:
            return Response({"error": "Token exchange failed"}, status=500)

        # Verify ID token and get user info
        user_info = self._verify_id_token(token_data.get("id_token", ""))
        if user_info is None:
            return Response({"error": "ID token verification failed"}, status=500)

        email = user_info.get("email", "")
        google_sub = user_info.get("sub", "")
        name = user_info.get("name", email.split("@")[0])
        picture = user_info.get("picture", "")

        if not email:
            return Response({"error": "No email in token"}, status=400)

        defaults = {
            "google_sub": google_sub,
            "name": name,
            "display_name": name,
        }
        if picture:
            defaults["picture_url"] = picture
        user, api_key = upsert_user_and_create_login_token(
            email,
            defaults,
            presented_token=request.COOKIES.get(settings.AUTH_COOKIE_NAME),
        )

        # Sync the Django session to match the freshly-authenticated user.
        # Without this, a stale session from an earlier Allauth login as a
        # different Google account would be returned by web views' _get_web_user
        # while DRF endpoints (which read dsg_token) would see this user —
        # causing /web/tos/service-check to act on the wrong account.
        request.session["user_email"] = user.email

        # Check for pending TOS before redirecting
        redirect_url = validate_redirect_url(
            request.session.pop("oauth_redirect", "/")
        )
        service_name = request.session.pop("oauth_service", None)
        dataset_name = request.session.pop("oauth_dataset", None)

        if service_name and dataset_name:
            redirect_url = self._maybe_intercept_for_tos(
                request, user, redirect_url, service_name, dataset_name,
            )

        response = HttpResponseRedirect(redirect_url)
        cookie_kwargs = {
            "max_age": settings.AUTH_COOKIE_AGE,
            "path": "/",
            "httponly": True,
            "samesite": "Lax",
            "secure": settings.AUTH_COOKIE_SECURE,
        }
        cookie_domain = getattr(settings, "AUTH_COOKIE_DOMAIN", "")
        if cookie_domain:
            cookie_kwargs["domain"] = cookie_domain
        response.set_cookie(
            settings.AUTH_COOKIE_NAME,
            api_key.key,
            **cookie_kwargs,
        )

        return response

    def _maybe_intercept_for_tos(self, request, user, redirect_url, service_name, dataset_name):
        """If the user has pending TOS for this service+dataset, redirect to TOS page instead."""
        from urllib.parse import urlencode

        from django.db.models import Q

        from core.models import Dataset, TOSAcceptance, TOSDocument

        try:
            dataset = Dataset.objects.get(name=dataset_name)
        except Dataset.DoesNotExist:
            return redirect_url

        tos_user_id = user.pk
        accepted_tos_ids = set(
            TOSAcceptance.objects.filter(user_id=tos_user_id).values_list(
                "tos_document_id", flat=True
            )
        )

        pending = []
        # Check general dataset TOS
        if dataset.tos_id and dataset.tos_id not in accepted_tos_ids:
            pending.append(dataset.tos_id)

        # Check service-specific TOS
        now = timezone.now()
        service_tos_ids = list(
            TOSDocument.objects.filter(
                service__name=service_name,
                dataset=dataset,
                effective_date__lte=now,
            )
            .filter(Q(retired_date__isnull=True) | Q(retired_date__gt=now))
            .exclude(pk__in=accepted_tos_ids)
            .values_list("pk", flat=True)
        )
        pending.extend(service_tos_ids)

        if not pending:
            return redirect_url

        # Store context in session for the TOS service-check view.
        # The redirect_url is the service's own URL (potentially with
        # query params like ?dataset=hemibrain:v1.2) and must be
        # preserved exactly as received.
        request.session["tos_check_ids"] = pending
        request.session["tos_check_next"] = redirect_url
        return f"/web/tos/service-check/"

    def _exchange_code(self, code, redirect_uri):
        """Exchange authorization code for tokens."""
        import urllib.request
        import json
        from urllib.parse import urlencode

        data = urlencode({
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }).encode()

        try:
            req = urllib.request.Request(
                "https://oauth2.googleapis.com/token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception:
            return None

    def _verify_id_token(self, id_token_str):
        """Verify Google ID token and extract claims."""
        try:
            from google.oauth2 import id_token
            from google.auth.transport import requests as google_requests

            return id_token.verify_oauth2_token(
                id_token_str,
                google_requests.Request(),
                settings.GOOGLE_CLIENT_ID,
            )
        except Exception:
            return None


class LogoutView(APIView):
    """GET/POST /api/v1/logout

    Invalidates token and clears cookie.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return self._logout(request)

    def post(self, request):
        return self._logout(request)

    def _logout(self, request):
        token = request.COOKIES.get(settings.AUTH_COOKIE_NAME)
        if token:
            try:
                APIKey.objects.filter(
                    key=token,
                    description=LOGIN_TOKEN_DESCRIPTION,
                ).delete()
            except DatabaseError:
                logger.warning("Could not revoke OAuth login token", exc_info=True)

        if "redirect" in request.query_params:
            response = HttpResponseRedirect(
                validate_redirect_url(request.query_params.get("redirect"))
            )
        else:
            response = Response({"status": "logged out"})

        delete_kwargs = {"path": "/"}
        cookie_domain = getattr(settings, "AUTH_COOKIE_DOMAIN", "")
        if cookie_domain:
            delete_kwargs["domain"] = cookie_domain
        response.delete_cookie(settings.AUTH_COOKIE_NAME, **delete_kwargs)
        return response


class CreateTokenView(APIView):
    """POST /api/v1/create_token

    Generate a new API token for the authenticated user.
    """

    permission_classes = [IsAuthenticated, IsHumanUser]

    def post(self, request):
        api_key = APIKey.objects.create(
            user=request.user,
            description=request.data.get("description", "API token"),
            expires_at=None,
        )
        return Response(api_key.key)


class LongLivedTokenView(APIView):
    """GET /api/v1/long_lived_token

    Return the authenticated user's stable long-lived API token. Creates the
    token on first call and returns the same token thereafter. Use this for
    integrated frontends that display a token for users to paste into scripts
    and clients (neuprint-python, clio scripts, curl). Use
    POST /api/v1/create_token for explicit token-management workflows.
    """

    permission_classes = [IsAuthenticated, IsHumanUser]

    def get(self, request):
        api_key = get_or_create_default_long_lived_token(request.user)
        return Response({"token": api_key.key})


class UserTokensView(APIView):
    """GET /api/v1/user/token

    List all tokens for the current user.
    """

    permission_classes = [IsAuthenticated, IsHumanUser]

    def get(self, request):
        tokens = APIKey.objects.filter(user=request.user).values(
            "id", "description", "created", "last_used"
        )
        return Response(list(tokens))


class RefreshTokenView(APIView):
    """GET /api/v1/refresh_token

    Deprecated but still referenced in CAVEclient.
    """

    permission_classes = [IsAuthenticated, IsHumanUser]

    def get(self, request):
        return Response({"status": "deprecated", "message": "Use /api/v1/create_token instead"})
