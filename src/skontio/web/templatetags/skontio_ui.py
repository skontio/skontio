"""Small template helpers for the app shell and status stamps."""

import hashlib
from functools import cache

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

# label, CSS class – the CSS draws them as stamps (one bold element of the design)
STAMPS = {
    "draft": ("Entwurf", "draft"),
    "final": ("Festgeschrieben", "final"),
    "open": ("Offen", "open"),
    "partial": ("Teilweise bezahlt", "partial"),
    "paid": ("Bezahlt", "paid"),
    "overdue": ("Überfällig", "overdue"),
    "cancelled": ("Storniert", "cancelled"),
}


@register.simple_tag(takes_context=True)
def nav_current(context, prefix: str, *exclude: str) -> str:
    """``aria-current="page"`` when the request path starts with ``prefix`` (``/`` only matches exactly).

    ``prefix`` may list several alternatives separated by ``|``.
    """
    request = context.get("request")
    path = request.path if request else ""
    if prefix == "/":
        hit = path == "/"
    else:
        hit = path.startswith(tuple(prefix.split("|"))) and not any(path.startswith(e) for e in exclude)
    return mark_safe('aria-current="page"') if hit else ""  # noqa: S308 – constant markup


@register.simple_tag
def stamp(state: str, size: str = "") -> str:
    label, css = STAMPS.get(state, (state, ""))
    return format_html('<span class="stamp {} {}">{}</span>', css, size, label)


@cache
def _content_hash(path: str) -> str:
    found = finders.find(path)
    if not found:
        return ""
    with open(found, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:10]


@register.simple_tag
def asset(path: str) -> str:
    """Static URL with a content hash, so browsers fetch the new file after an update.

    Granian serves /static/ with a one-day cache lifetime and the file names never change.
    """
    url = static(path)
    digest = _content_hash(path)
    return f"{url}?v={digest}" if digest else url


@register.simple_tag
def custom_theme() -> str:
    """Link to the own theme.css from SKONTIO_THEME_DIR (loaded after the bundled styles), if there is one."""
    from skontio.web.theme import custom_css

    data = custom_css()
    if data is None:
        return ""
    digest = hashlib.sha256(data).hexdigest()[:12]
    return format_html('\n<link rel="stylesheet" href="/theme.css?v={}">', digest)
