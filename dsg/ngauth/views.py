"""Neuroglancer ngauth endpoint views.

Implements the ngauth protocol for Neuroglancer protected sources,
plus TOS gating and GCS token issuance.
"""

import json
import re
import time
from urllib.parse import urlencode, urlparse

from django.conf import settings
from django.contrib.auth import logout as auth_logout
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import render
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme

from core.audit import log_audit
from core.models import APIKey, TOSAcceptance, TOSDocument, User

from . import gcs, tokens

# One DNS label: alphanumeric, internal hyphens, 63 chars max.
_HOSTNAME_LABEL_RE = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?")


def _get_session_key():
    """Get the HMAC session key for ngauth tokens."""
    return settings.SECRET_KEY.encode()[:32]


def _get_user_from_cookie(request):
    """Extract user email from dsg_token cookie (APIKey lookup).

    A not-enabled user (disabled, or a user-type service account under a
    disabled parent) resolves to None — their live cookie must stop working
    on every ngauth endpoint, including /token → /gcs_token minting.
    """
    cookie_value = request.COOKIES.get(settings.AUTH_COOKIE_NAME)
    if not cookie_value:
        return None
    try:
        api_key = APIKey.objects.select_related("user__parent").get(key=cookie_value)
    except APIKey.DoesNotExist:
        return None
    if not api_key.user.is_enabled:
        return None
    return api_key.user.email


def _is_origin_syntax_valid(origin):
    """Check that a client-supplied origin is a serialized web origin.

    A value that reaches ``postMessage`` as targetOrigin must be exactly
    ``scheme://host[:port]`` — no credentials, path, query or fragment, and a
    port the URL parser accepts. A regex alone lets through things like
    ``:99999`` that make ``postMessage`` throw ``SyntaxError``, which would
    hang the opener instead of failing cleanly.
    """
    if not origin or not isinstance(origin, str):
        return False
    if origin != origin.strip() or any(c.isspace() for c in origin):
        return False
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.path or parsed.params or parsed.query or parsed.fragment:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    hostname = parsed.hostname
    if not hostname or hostname.endswith("."):
        return False
    labels = hostname.split(".")
    return all(_HOSTNAME_LABEL_RE.fullmatch(label) for label in labels)


def _is_origin_allowed(origin):
    """Check if origin matches allowed pattern.

    Uses ``fullmatch``: this pattern is the only thing standing between an
    arbitrary website and a user's GCS buckets, and a natural-looking deployment
    value such as ``https://clio-dev\\.janelia\\.org`` would otherwise also
    admit ``https://clio-dev.janelia.org.attacker.example``.
    """
    if not origin:
        return False
    pattern = getattr(settings, "NGAUTH_ALLOWED_ORIGINS", r"^https?://.*\.neuroglancer\.org$")
    return re.fullmatch(pattern, origin) is not None


def _mint_temporary_user_token(user_email):
    """Mint the short-lived HMAC token Neuroglancer replays to /gcs_token."""
    key = _get_session_key()
    user_token = tokens.UserToken(
        user_id=user_email,
        expires=int(time.time()) + tokens.MAX_COOKIE_LIFETIME_SECONDS,
    )
    return tokens.encode_user_token(key, tokens.make_temporary_token(user_token))


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
        # Store post-login redirect target (default: /login for Neuroglancer popup flow).
        # Only same-site relative targets are accepted — `next` reaches us from
        # query strings we do not control, and the allauth adapter redirects to
        # whatever is stashed here, so an absolute URL would be an open redirect.
        next_url = request.GET.get("next", "")
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts=None):
            request.session["oauth_next"] = next_url
        else:
            # Never let a rejected (or absent) `next` fall through to a value
            # left in the session by an earlier request.
            request.session.pop("oauth_next", None)

        return HttpResponseRedirect("/accounts/google/login/")


class LoginStatusView(View):
    """GET /login — Login status page and the ngauth popup handshake.

    Neuroglancer's ngauth client opens this in a popup as
    ``/login?origin=<client origin>`` and waits for a ``postMessage`` from this
    window carrying either ``{"token": …}`` or the literal string
    ``"badorigin"``; see ``waitForLogin`` in Neuroglancer's
    ``datasource/ngauth/credentials_provider.ts``. Without an ``origin``
    parameter this is just a human-readable status page.
    """

    def get(self, request):
        origin = request.GET.get("origin", "")

        if origin:
            # Settle the origin before touching the cookie or the database, so a
            # hostile origin cannot time the response to infer login state.
            if not _is_origin_syntax_valid(origin):
                return HttpResponseBadRequest("Invalid origin")

            # Signal a disallowed origin rather than hanging the opener. The
            # payload is a constant, so this discloses nothing to that origin.
            if not _is_origin_allowed(origin):
                return render(request, "ngauth/login_popup.html", {
                    "origin": origin,
                    "payload": "badorigin",
                })

            user_email = _get_user_from_cookie(request)
            if user_email:
                return render(request, "ngauth/login_popup.html", {
                    "origin": origin,
                    "payload": {"token": _mint_temporary_user_token(user_email)},
                })

            # Not logged in: come back to this same handshake after OAuth so the
            # popup can deliver the token without a second round trip.
            return render(request, "ngauth/login_status.html", {
                "user_email": None,
                "logged_in": False,
                "login_url": "/auth/login?" + urlencode({
                    "next": "/login?" + urlencode({"origin": origin}),
                }),
            })

        user_email = _get_user_from_cookie(request)
        return render(request, "ngauth/login_status.html", {
            "user_email": user_email,
            "logged_in": user_email is not None,
            "login_url": "/auth/login?" + urlencode({"next": "/login"}),
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
        tos_doc = None
        if tos_id:
            try:
                tos_doc = TOSDocument.objects.select_related("dataset").get(pk=tos_id)
                acceptance, created = TOSAcceptance.objects.get_or_create(
                    user=user,
                    tos_document=tos_doc,
                    defaults={"ip_address": request.META.get("REMOTE_ADDR")},
                )
                if created:
                    log_audit(user, "tos_accepted", "TOSAcceptance", acceptance.pk,
                              after_state={
                                  "user": user.email, "tos_document": tos_doc.name,
                                  "dataset": tos_doc.dataset.name if tos_doc.dataset else None,
                              })
            except TOSDocument.DoesNotExist:
                return JsonResponse({"error": "TOS document not found"}, status=404)

        # Sync IAM for dataset-scoped TOS
        if tos_doc and tos_doc.dataset:
            from core.iam import sync_user_dataset_iam
            sync_user_dataset_iam(user, tos_doc.dataset)
        elif bucket:
            # Legacy fallback: add user to specific bucket
            from core.iam import provision_binding
            result = provision_binding(bucket, user_email)
            if result == "failed":
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
            if not _is_origin_syntax_valid(origin):
                return JsonResponse({"error": "Invalid Origin"}, status=400, headers=headers)

            if _is_origin_allowed(origin):
                headers["Access-Control-Allow-Origin"] = origin
                headers["Access-Control-Allow-Credentials"] = "true"
                headers["Vary"] = "origin"
            else:
                return JsonResponse({"error": "Origin not allowed"}, status=403, headers=headers)

        # Look up user from dsg_token cookie (APIKey); disabled users must
        # not mint the temporary token /gcs_token accepts.
        user_email = _get_user_from_cookie(request)
        if not user_email:
            return JsonResponse({"error": "Not logged in"}, status=401, headers=headers)

        # Create temporary cross-origin HMAC token for Neuroglancer
        encoded = _mint_temporary_user_token(user_email)

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

        # Neuroglancer's request qualifies as a CORS-simple POST, so the browser
        # sends it without a preflight and the OPTIONS allowlist check never
        # runs. Gate the actual POST the same way /token does, or any origin
        # holding a temporary token could read the downscoped GCS credential.
        if origin:
            if not _is_origin_syntax_valid(origin):
                return JsonResponse({"error": "Invalid Origin"}, status=400)
            if not _is_origin_allowed(origin):
                return JsonResponse({"error": "Origin not allowed"}, status=403)
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Allow-Credentials"] = "true"
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

        try:
            user = User.objects.select_related("parent").get(email=user_token.user_id)
        except User.DoesNotExist:
            return JsonResponse(
                {"error": "Invalid authentication token"}, status=401, headers=headers
            )
        if not user.is_enabled:
            if user.is_active:
                error = "Parent user account is disabled"
            else:
                error = "User account is disabled"
            return JsonResponse({"error": error}, status=401, headers=headers)

        # Get GCS token
        gcs_token = gcs.get_gcs_token_for_user(user.email, bucket)
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
