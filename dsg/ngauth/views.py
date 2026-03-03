"""Neuroglancer ngauth endpoint views.

Implements the ngauth protocol for Neuroglancer protected sources,
plus TOS gating and GCS token issuance.
"""

import json
import re
import time

from django.conf import settings
from django.contrib.auth import logout as auth_logout
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from core.models import APIKey, TOSAcceptance, TOSDocument, User

from . import gcs, tokens


def _get_session_key():
    """Get the HMAC session key for ngauth tokens."""
    return settings.SECRET_KEY.encode()[:32]


def _get_user_from_cookie(request):
    """Extract user email from dsg_token cookie (APIKey lookup)."""
    cookie_value = request.COOKIES.get(settings.AUTH_COOKIE_NAME)
    if not cookie_value:
        return None
    try:
        api_key = APIKey.objects.select_related("user").get(key=cookie_value)
        return api_key.user.email
    except APIKey.DoesNotExist:
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
        return render(request, "ngauth/index.html", {
            "user_email": user_email,
        })


class HealthView(View):
    """GET /health — Health check."""

    def get(self, request):
        return JsonResponse({"status": "ok"})


class AuthLoginView(View):
    """GET /auth/login — Initiate OAuth via allauth."""

    def get(self, request):
        # Store post-login redirect target (default: /login for Neuroglancer popup flow)
        next_url = request.GET.get("next", "")
        if next_url:
            request.session["oauth_next"] = next_url

        return HttpResponseRedirect("/accounts/google/login/")


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
    """POST /logout — Clear ngauth cookie and allauth session."""

    def post(self, request):
        auth_logout(request)
        response = JsonResponse({"status": "logged out"})
        delete_kwargs = {}
        cookie_domain = getattr(settings, "AUTH_COOKIE_DOMAIN", "")
        if cookie_domain:
            delete_kwargs["domain"] = cookie_domain
        response.delete_cookie(settings.AUTH_COOKIE_NAME, **delete_kwargs)
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

        # Look up user from dsg_token cookie (APIKey)
        cookie_value = request.COOKIES.get(settings.AUTH_COOKIE_NAME)
        if not cookie_value:
            return JsonResponse({"error": "Not logged in"}, status=401, headers=headers)
        try:
            api_key = APIKey.objects.select_related("user").get(key=cookie_value)
        except APIKey.DoesNotExist:
            return JsonResponse({"error": "Not logged in"}, status=401, headers=headers)

        user_email = api_key.user.email

        # Create temporary cross-origin HMAC token for Neuroglancer
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
