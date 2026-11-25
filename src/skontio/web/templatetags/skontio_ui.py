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


# 24x24 line icons for the "Modern" interface (constants, so they are safe to mark as HTML)
ICONS = {
    "home": '<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v9.5h13V10"/>',
    "doc": '<path d="M6 3.5h8l4 4v13H6z"/><path d="M14 3.5v4h4M9 12h6M9 15.5h6"/>',
    "users": (
        '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-5.5 6-5.5s6 2.2 6 5.5"/>'
        '<path d="M16 5.2a3 3 0 0 1 0 5.6M18 14.8c1.8.7 3 2.4 3 5.2"/>'
    ),
    "tag": '<path d="M3.5 12.2V4.5h7.7l9.3 9.3-7.7 7.7z"/><circle cx="8" cy="9" r="1.2"/>',
    "repeat": '<path d="M4 11V9a3 3 0 0 1 3-3h11l-3-3M20 13v2a3 3 0 0 1-3 3H6l3 3"/>',
    "bell": '<path d="M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
    "send": '<path d="M21 3 10 14"/><path d="M21 3l-7 18-4-7-7-4z"/>',
    "receipt": '<path d="M6 3.5h12v17l-3-2-3 2-3-2-3 2z"/><path d="M9 8.5h6M9 12h6"/>',
    "inbox": '<path d="M3.5 13 6 5h12l2.5 8v6h-17z"/><path d="M3.5 13h5l1 2.5h5l1-2.5h5"/>',
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 7 8.5 6 8.5-6"/>',
    "income": '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
    "bank": '<path d="M3 9.5 12 4l9 5.5"/><path d="M5 10v8M9.5 10v8M14.5 10v8M19 10v8M3 20h18"/>',
    "chart": '<path d="M4 20V10M10 20V4M16 20v-7M2 20h20"/>',
    "percent": '<path d="M19 5 5 19"/><circle cx="7" cy="7" r="2.2"/><circle cx="17" cy="17" r="2.2"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>',
    "download": '<path d="M12 4v11M7.5 10.5 12 15l4.5-4.5M4.5 20h15"/>',
    "shield": '<path d="M12 3 4.5 6v6c0 4.5 3 7.5 7.5 9 4.5-1.5 7.5-4.5 7.5-9V6z"/>',
    "sparkle": '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 17v4M17 19h4"/>',
    "gear": (
        '<circle cx="12" cy="12" r="3"/>'
        '<path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3"/>'
        '<path d="M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M18.7 5.3l-2.1 2.1M7.4 16.6l-2.1 2.1"/>'
    ),
    "logout": '<path d="M9 4H5v16h4M16 8l4 4-4 4M20 12H9"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
}


@register.simple_tag(takes_context=True)
def nav_icon(context, name: str) -> str:
    """Line icon in front of a menu entry – only in the "Modern" interface (the classic one has no icons)."""
    if not context.get("ui_modern"):
        return ""
    return format_html('<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">{}</svg>', mark_safe(ICONS[name]))  # noqa: S308


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
