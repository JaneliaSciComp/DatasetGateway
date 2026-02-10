"""SCIM filter expression → Django Q objects.

Port of CAVE's scim/filter.py, adapted from SQLAlchemy to Django ORM.
Uses scim2-filter-parser library for RFC 7644 filter parsing.
"""

import logging

from django.db.models import Q

logger = logging.getLogger(__name__)


class SCIMFilterError(Exception):
    pass


# SCIM operator → Django lookup mapping
_OPERATOR_MAP = {
    "eq": "",         # exact match (default __exact)
    "ne": "",         # negated eq
    "co": "contains",
    "sw": "startswith",
    "ew": "endswith",
    "gt": "gt",
    "ge": "gte",
    "lt": "lt",
    "le": "lte",
}

_CASE_INSENSITIVE_MAP = {
    "eq": "iexact",
    "co": "icontains",
    "sw": "istartswith",
    "ew": "iendswith",
}


def _build_q(field_name, operator, value, case_insensitive=False):
    """Build a Django Q object for a single comparison."""
    if operator == "pr":
        # "present" — field is not null and not empty
        return ~Q(**{f"{field_name}__isnull": True}) & ~Q(**{field_name: ""})

    if operator == "ne":
        # Negated equality
        if case_insensitive and isinstance(value, str):
            return ~Q(**{f"{field_name}__iexact": value})
        return ~Q(**{field_name: value})

    if operator == "eq":
        if case_insensitive and isinstance(value, str):
            return Q(**{f"{field_name}__iexact": value})
        return Q(**{field_name: value})

    lookup = _OPERATOR_MAP.get(operator)
    if lookup is None:
        raise SCIMFilterError(f"Unsupported operator: {operator}")

    if case_insensitive and operator in _CASE_INSENSITIVE_MAP:
        lookup = _CASE_INSENSITIVE_MAP[operator]

    return Q(**{f"{field_name}__{lookup}": value})


def _ast_to_q(expr, attr_map):
    """Recursively convert a scim2-filter-parser AST node to a Django Q object."""
    if expr is None:
        return Q()

    # Handle negation wrapper
    is_negated = getattr(expr, "negated", False)

    # Check for AttrExpr (attribute comparison)
    if hasattr(expr, "attr_path") and hasattr(expr, "comp_value"):
        attr_path = getattr(expr, "attr_path", None)
        if attr_path is None:
            return Q()

        # Get attribute name from path
        attr_name = str(attr_path)
        case_insensitive = getattr(attr_path, "case_insensitive", False)

        operator = expr.value if hasattr(expr, "value") else "eq"

        # Extract comparison value
        comp_value_obj = getattr(expr, "comp_value", None)
        if comp_value_obj is not None:
            value = getattr(comp_value_obj, "value", str(comp_value_obj))
        else:
            value = None

        # Map SCIM attribute to Django field
        if attr_name not in attr_map or attr_map[attr_name] is None:
            return Q()

        django_field = attr_map[attr_name]

        # Handle boolean strings
        if isinstance(value, str):
            if value.lower() == "true":
                value = True
            elif value.lower() == "false":
                value = False

        q = _build_q(django_field, operator, value, case_insensitive)
        return ~q if is_negated else q

    # Check for LogExpr (logical: and/or)
    if hasattr(expr, "op") and hasattr(expr, "expr1") and hasattr(expr, "expr2"):
        left = _ast_to_q(expr.expr1, attr_map)
        right = _ast_to_q(expr.expr2, attr_map)

        if expr.op == "and":
            result = left & right
        elif expr.op == "or":
            result = left | right
        else:
            result = Q()

        return ~result if is_negated else result

    # Nested filter
    if hasattr(expr, "expr"):
        result = _ast_to_q(expr.expr, attr_map)
        return ~result if is_negated else result

    return Q()


def apply_scim_filter(queryset, filter_expr, attr_map):
    """Apply a SCIM filter expression to a Django queryset.

    Args:
        queryset: Django queryset
        filter_expr: SCIM filter string (e.g., 'userName eq "user@example.org"')
        attr_map: Dict mapping SCIM attribute names to Django field names

    Returns:
        Filtered queryset
    """
    if not filter_expr or not filter_expr.strip():
        return queryset

    try:
        from scim2_filter_parser.lexer import SCIMLexer
        from scim2_filter_parser.parser import SCIMParser

        lexer = SCIMLexer()
        parser = SCIMParser()
        token_stream = lexer.tokenize(filter_expr)
        ast = parser.parse(token_stream)

        q = _ast_to_q(ast, attr_map)
        return queryset.filter(q)
    except Exception as e:
        logger.warning(f"Invalid SCIM filter: {filter_expr}, error: {e}")
        raise SCIMFilterError(f"Invalid filter expression: {e}") from e
