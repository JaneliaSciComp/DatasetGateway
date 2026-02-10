"""SCIM 2.0 pagination — 1-based startIndex/count per RFC 7644."""


class SCIMPaginator:
    """Parse and apply SCIM pagination parameters."""

    DEFAULT_COUNT = 100
    MAX_COUNT = 1000

    def __init__(self, request):
        self.start_index = self._parse_int(
            request.query_params.get("startIndex"), default=1, minimum=1
        )
        self.count = self._parse_int(
            request.query_params.get("count"),
            default=self.DEFAULT_COUNT,
            minimum=0,
            maximum=self.MAX_COUNT,
        )

    def _parse_int(self, value, default, minimum=None, maximum=None):
        if value is None:
            return default
        try:
            v = int(value)
        except ValueError:
            return default
        if minimum is not None:
            v = max(v, minimum)
        if maximum is not None:
            v = min(v, maximum)
        return v

    def paginate_queryset(self, queryset):
        """Apply pagination to a queryset. Returns (page_items, total_count)."""
        total = queryset.count()
        # SCIM startIndex is 1-based
        offset = self.start_index - 1
        return list(queryset[offset : offset + self.count]), total

    def get_response_data(self, resources, total_results):
        """Build SCIM ListResponse envelope."""
        return {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
            "totalResults": total_results,
            "itemsPerPage": len(resources),
            "startIndex": self.start_index,
            "Resources": resources,
        }
