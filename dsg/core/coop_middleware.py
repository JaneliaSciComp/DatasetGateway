"""Relax Cross-Origin-Opener-Policy on the ngauth login popup chain.

Neuroglancer's ngauth client opens ``/login?origin=<viewer origin>`` in a popup
and waits for that window to ``postMessage`` a token to ``window.opener``.
``SecurityMiddleware`` gives every response Django's default
``Cross-Origin-Opener-Policy: same-origin``; for a popup opened by a
cross-origin viewer that policy moves the popup into a new browsing-context
group and nulls ``window.opener``, so the handshake never reaches the viewer.

This middleware sets ``unsafe-none`` on every response the popup can receive
along ``/login`` -> ``/auth/login`` -> ``/accounts/google/login/`` -> Google ->
``/accounts/google/login/callback/`` -> ``/login``. Redirects and error pages
are included deliberately: browsers enforce COOP on each response of a redirect
chain, and once a browsing-context-group switch is required it is never undone
within that navigation. Everything outside these paths keeps the global
``same-origin`` policy (``SECURE_CROSS_ORIGIN_OPENER_POLICY`` is unchanged).
Listed directly after ``WhiteNoiseMiddleware``; see the MIDDLEWARE comment in
``dsg/settings.py`` for the ordering rationale.
"""

POPUP_CHAIN_EXACT = ("/login", "/auth/login")
POPUP_CHAIN_PREFIXES = ("/accounts/",)


class PopupOpenerPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if request.path in POPUP_CHAIN_EXACT or request.path.startswith(
            POPUP_CHAIN_PREFIXES
        ):
            response["Cross-Origin-Opener-Policy"] = "unsafe-none"

        return response
