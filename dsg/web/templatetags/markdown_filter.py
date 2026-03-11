"""Custom template filter for rendering Markdown to HTML."""

import markdown
from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter(name="markdown")
def render_markdown(value):
    """Render a Markdown string as HTML.

    Enables common extensions: tables, fenced code blocks, and footnotes.
    The output is marked safe for template rendering.
    """
    if not value:
        return ""
    html = markdown.markdown(
        value,
        extensions=["tables", "fenced_code", "footnotes", "nl2br"],
    )
    return mark_safe(html)
