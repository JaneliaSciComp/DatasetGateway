"""Neuroglancer ngauth endpoint views.

Implements the ngauth protocol for Neuroglancer protected sources,
plus TOS gating and GCS token issuance.
"""

import json
import re
import secrets
import time

from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from core.models import Dataset, DatasetVersion, TOSAcceptance, TOSDocument, User

from . import gcs, tokens


def _get_session_key():
    """Get the HMAC session key for ngauth tokens."""
    return settings.SECRET_KEY.encode()[:32]


def _get_user_from_cookie(request):
    """Extract user email from ngauth cookie."""
    cookie_value = request.COOKIES.get(settings.NGAUTH_COOKIE_NAME)
    if not cookie_value:
        return None
    token = tokens.decode_user_token(_get_session_key(), cookie_value)
    if token:
        return token.user_id
    return None


def _is_origin_allowed(origin):
    """Check if origin matches allowed pattern."""
    if not origin:
        return False
    pattern = getattr(settings, "NGAUTH_ALLOWED_ORIGINS", r"^https?://.*\.neuroglancer\.org$")
    return re.match(pattern, origin) is not None


def _cors_headers(request):
    """Build CORS response headers if origin is allowed."""
    origin = request.META.get("HTTP_ORIGIN")
    if origin and _is_origin_allowed(origin):
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Vary": "origin",
        }
    return {}


class IndexView(View):
    """GET / — Landing page with TOS."""

    def get(self, request):
        user_email = _get_user_from_cookie(request)
        tos_documents = TOSDocument.objects.filter(retired_date__isnull=True)
        return render(request, "ngauth/index.html", {
            "user_email": user_email,
            "tos_documents": tos_documents,
        })


class HealthView(View):
    """GET /health — Health check."""

    def get(self, request):
        return JsonResponse({"status": "ok"})


class AuthLoginView(View):
    """GET /auth/login — Initiate OAuth."""

    def get(self, request):
        from urllib.parse import urlencode

        # Store post-login redirect target (default: /login for Neuroglancer popup flow)
        next_url = request.GET.get("next", "")
        if next_url:
            request.session["oauth_next"] = next_url

        callback_url = request.build_absolute_uri("/auth/callback")
        state = secrets.token_urlsafe(32)
        request.session["oauth_state"] = state

        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": callback_url,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
        }

        return HttpResponseRedirect(
            f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
        )


class AuthCallbackView(View):
    """GET /auth/callback — OAuth callback."""

    def get(self, request):
        code = request.GET.get("code")
        error = request.GET.get("error")

        if error or not code:
            return JsonResponse({"error": error or "Missing code"}, status=400)

        # Exchange code for tokens
        callback_url = request.build_absolute_uri("/auth/callback")
        user_info = self._exchange_and_verify(code, callback_url)
        if user_info is None:
            return JsonResponse({"error": "Authentication failed"}, status=500)

        email = user_info.get("email", "")
        if not email:
            return JsonResponse({"error": "No email in token"}, status=400)

        # Ensure user exists
        User.objects.get_or_create(
            email=email,
            defaults={
                "google_sub": user_info.get("sub", ""),
                "name": user_info.get("name", email.split("@")[0]),
                "display_name": user_info.get("name", ""),
            },
        )

        # Set ngauth cookie and redirect
        cookie_value = tokens.create_login_token(_get_session_key(), email)
        next_url = request.session.pop("oauth_next", "/login")
        response = HttpResponseRedirect(next_url)
        cookie_kwargs = {
            "max_age": tokens.MAX_COOKIE_LIFETIME_SECONDS,
            "httponly": True,
            "samesite": "Lax",
        }
        cookie_domain = getattr(settings, "AUTH_COOKIE_DOMAIN", "")
        if cookie_domain:
            cookie_kwargs["domain"] = cookie_domain
        response.set_cookie(
            settings.NGAUTH_COOKIE_NAME,
            cookie_value,
            **cookie_kwargs,
        )
        return response

    def _exchange_and_verify(self, code, redirect_uri):
        """Exchange code and verify ID token."""
        import urllib.request
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
                token_data = json.loads(resp.read())
        except Exception:
            return None

        try:
            from google.oauth2 import id_token
            from google.auth.transport import requests as google_requests

            return id_token.verify_oauth2_token(
                token_data.get("id_token", ""),
                google_requests.Request(),
                settings.GOOGLE_CLIENT_ID,
            )
        except Exception:
            return None


class LoginStatusView(View):
    """GET /login — Login status / ngauth popup flow."""

    def get(self, request):
        user_email = _get_user_from_cookie(request)
        return render(request, "ngauth/login_status.html", {
            "user_email": user_email,
            "logged_in": user_email is not None,
        })


@method_decorator(csrf_exempt, name="dispatch")
class LogoutView(View):
    """POST /logout — Clear ngauth cookie."""

    def post(self, request):
        response = JsonResponse({"status": "logged out"})
        response.delete_cookie(settings.NGAUTH_COOKIE_NAME)
        return response


@method_decorator(csrf_exempt, name="dispatch")
class ActivateView(View):
    """POST /activate — TOS acceptance + bucket IAM provisioning."""

    def post(self, request):
        user_email = _get_user_from_cookie(request)
        if not user_email:
            return JsonResponse({"error": "Not logged in"}, status=401)

        try:
            user = User.objects.get(email=user_email)
        except User.DoesNotExist:
            return JsonResponse({"error": "User not found"}, status=404)

        # Get TOS document from request
        try:
            body = json.loads(request.body) if request.body else {}
        except json.JSONDecodeError:
            body = {}

        tos_id = body.get("tos_id") or request.POST.get("tos_id")
        bucket = body.get("bucket") or request.POST.get("bucket")

        # Accept TOS if provided
        if tos_id:
            try:
                tos_doc = TOSDocument.objects.get(pk=tos_id)
                TOSAcceptance.objects.get_or_create(
                    user=user,
                    tos_document=tos_doc,
                    defaults={"ip_address": request.META.get("REMOTE_ADDR")},
                )
            except TOSDocument.DoesNotExist:
                return JsonResponse({"error": "TOS document not found"}, status=404)

        # Add user to bucket IAM if bucket specified
        if bucket:
            success = gcs.add_user_to_bucket(bucket, user_email)
            if not success:
                return JsonResponse(
                    {"error": "Failed to provision bucket access"}, status=500
                )

        return JsonResponse({"status": "activated"})


class SuccessView(View):
    """GET /success — Success page."""

    def get(self, request):
        return render(request, "ngauth/success.html")


@method_decorator(csrf_exempt, name="dispatch")
class TokenView(View):
    """POST /token — Cross-origin user token (ngauth protocol)."""

    def post(self, request):
        headers = {"X-Frame-Options": "deny"}
        origin = request.META.get("HTTP_ORIGIN")

        if origin:
            if not re.match(r"^https?://[a-zA-Z0-9\-.]+(:\d+)?$", origin):
                return JsonResponse({"error": "Invalid Origin"}, status=400, headers=headers)

            if _is_origin_allowed(origin):
                headers["Access-Control-Allow-Origin"] = origin
                headers["Access-Control-Allow-Credentials"] = "true"
                headers["Vary"] = "origin"
            else:
                return JsonResponse({"error": "Origin not allowed"}, status=403, headers=headers)

        user_email = _get_user_from_cookie(request)
        if not user_email:
            return JsonResponse({"error": "Not logged in"}, status=401, headers=headers)

        # Create temporary cross-origin token
        key = _get_session_key()
        user_token = tokens.UserToken(
            user_id=user_email,
            expires=int(time.time()) + tokens.MAX_COOKIE_LIFETIME_SECONDS,
        )
        temp_token = tokens.make_temporary_token(user_token)
        encoded = tokens.encode_user_token(key, temp_token)

        return HttpResponse(encoded, content_type="text/plain", headers=headers)

    def options(self, request):
        """CORS preflight."""
        headers = {}
        origin = request.META.get("HTTP_ORIGIN")
        if origin and _is_origin_allowed(origin):
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            headers["Access-Control-Allow-Headers"] = "Content-Type"
            headers["Access-Control-Allow-Credentials"] = "true"
            headers["Access-Control-Max-Age"] = "86400"
        return HttpResponse("", headers=headers)


@method_decorator(csrf_exempt, name="dispatch")
class GCSTokenView(View):
    """POST /gcs_token — Downscoped GCS access token (ngauth protocol)."""

    def post(self, request):
        headers = {}
        origin = request.META.get("HTTP_ORIGIN")
        if origin:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "origin"

        try:
            body = json.loads(request.body)
            user_token_str = body.get("token", "")
            bucket = body.get("bucket", "")
        except (json.JSONDecodeError, AttributeError):
            return JsonResponse(
                {"error": "Invalid request body"}, status=400, headers=headers
            )

        if not user_token_str or not bucket:
            return JsonResponse(
                {"error": "Missing token or bucket"}, status=400, headers=headers
            )

        # Decode and validate user token
        key = _get_session_key()
        user_token = tokens.decode_user_token(key, user_token_str)
        if not user_token:
            return JsonResponse(
                {"error": "Invalid authentication token"}, status=401, headers=headers
            )

        # Get GCS token
        gcs_token = gcs.get_gcs_token_for_user(user_token.user_id, bucket)
        if not gcs_token:
            return JsonResponse(
                {"error": "Access denied"}, status=403, headers=headers
            )

        return JsonResponse({"token": gcs_token}, headers=headers)

    def options(self, request):
        """CORS preflight."""
        headers = {}
        origin = request.META.get("HTTP_ORIGIN")
        if origin and _is_origin_allowed(origin):
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            headers["Access-Control-Allow-Headers"] = "Content-Type"
            headers["Access-Control-Allow-Credentials"] = "true"
            headers["Access-Control-Max-Age"] = "86400"
        return HttpResponse("", headers=headers)
