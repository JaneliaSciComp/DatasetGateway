"""CAVE OAuth flow and token management views."""

import secrets

from django.conf import settings
from django.http import HttpResponseRedirect, JsonResponse
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import APIKey, User


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

        redirect_url = request.query_params.get(
            "redirect", request.data.get("redirect", "/")
        )

        # Store redirect URL and optional tos_id in session
        request.session["oauth_redirect"] = redirect_url
        tos_id = request.query_params.get("tos_id")
        if tos_id:
            request.session["oauth_tos_id"] = tos_id

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

        if not email:
            return Response({"error": "No email in token"}, status=400)

        # Create or update user
        user, created = User.objects.update_or_create(
            email=email,
            defaults={
                "google_sub": google_sub,
                "name": name,
                "display_name": name,
            },
        )

        # Create an API key token for the user
        api_key = APIKey.objects.create(user=user, description="OAuth login token")

        # Redirect with cookie
        redirect_url = request.session.pop("oauth_redirect", "/")
        response = HttpResponseRedirect(redirect_url)
        cookie_kwargs = {
            "max_age": settings.AUTH_COOKIE_AGE,
            "httponly": True,
            "samesite": "Lax",
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

    def get(self, request):
        return self._logout(request)

    def post(self, request):
        return self._logout(request)

    def _logout(self, request):
        # Delete the API key if authenticated
        if request.user and hasattr(request.user, "pk"):
            token = request.COOKIES.get(settings.AUTH_COOKIE_NAME)
            if token:
                APIKey.objects.filter(key=token).delete()

        response = Response({"status": "logged out"})
        delete_kwargs = {}
        cookie_domain = getattr(settings, "AUTH_COOKIE_DOMAIN", "")
        if cookie_domain:
            delete_kwargs["domain"] = cookie_domain
        response.delete_cookie(settings.AUTH_COOKIE_NAME, **delete_kwargs)
        return response


class CreateTokenView(APIView):
    """POST /api/v1/create_token

    Generate a new API token for the authenticated user.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        api_key = APIKey.objects.create(
            user=request.user,
            description=request.data.get("description", "API token"),
        )
        return Response(api_key.key)


class UserTokensView(APIView):
    """GET /api/v1/user/token

    List all tokens for the current user.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tokens = APIKey.objects.filter(user=request.user).values(
            "id", "description", "created", "last_used"
        )
        return Response(list(tokens))


class RefreshTokenView(APIView):
    """GET /api/v1/refresh_token

    Deprecated but still referenced in CAVEclient.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"status": "deprecated", "message": "Use /api/v1/create_token instead"})
