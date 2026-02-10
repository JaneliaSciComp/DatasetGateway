"""DatasetGate middleware for multi-dataset routing."""

import re


class DatasetContextMiddleware:
    """Strip /{dataset}/{service_type}/ prefix from request path.

    Extracts dataset and service_type context from URL prefixes like
    /fish2/cave/api/v1/user/cache and stores them on the request object
    so downstream views can access them via request.dataset_name and
    request.service_type.

    The prefix is stripped before URL resolution so CAVE endpoints see
    their expected paths (e.g., /api/v1/user/cache).
    """

    PREFIX_PATTERN = re.compile(
        r"^/(?P<dataset>[a-zA-Z0-9_-]+)/(?P<service_type>[a-zA-Z0-9_-]+)/"
    )

    # Paths that should never be treated as dataset prefixes
    PASSTHROUGH_PREFIXES = (
        "/admin/",
        "/api/",
        "/auth/",
        "/web/",
        "/static/",
        "/health",
        "/login",
        "/logout",
        "/activate",
        "/success",
        "/token",
        "/gcs_token",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.dataset_name = None
        request.service_type = None

        path = request.path_info

        # Skip paths that are clearly not dataset-prefixed
        if not any(path.startswith(p) for p in self.PASSTHROUGH_PREFIXES):
            match = self.PREFIX_PATTERN.match(path)
            if match:
                request.dataset_name = match.group("dataset")
                request.service_type = match.group("service_type")
                # Strip the prefix so URL resolver sees /api/v1/...
                request.path_info = path[match.end() - 1 :]

        return self.get_response(request)
