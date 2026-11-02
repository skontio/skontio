"""Own look without code changes: SKONTIO_THEME_DIR may hold ``theme.css`` (overrides the CSS variables
of the bundled theme) and ``templates/`` (overrides any template by the same path, wired in settings)."""

from __future__ import annotations

from django.conf import settings
from django.http import Http404, HttpResponse
from django.views.decorators.http import require_GET

MAX_BYTES = 512 * 1024


def custom_css() -> bytes | None:
    directory = settings.THEME_DIR
    if directory is None:
        return None
    path = directory / "theme.css"
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


@require_GET
def theme_css(request):
    """Public like the other static files (the login page uses it too)."""
    data = custom_css()
    if data is None:
        raise Http404
    response = HttpResponse(data, content_type="text/css; charset=utf-8")
    response["Cache-Control"] = "public, max-age=86400"
    return response
