"""View helpers: login, current company (one company per installation until Phase 2)."""

from __future__ import annotations

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render

from skontio.core.models import Company


def current_company() -> Company | None:
    return Company.objects.order_by("pk").first()


def company_view(view):
    """Require login and an existing company; expose it as ``request.company``."""

    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        company = current_company()
        if company is None:
            if request.user.is_staff:
                return redirect("/einrichtung/")
            return HttpResponse(
                "Skontio ist noch nicht eingerichtet. Bitte die Inhaberin/den Inhaber fragen.", status=503
            )
        request.company = company
        return view(request, *args, **kwargs)

    return wrapper


def admin_required(view):
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_staff:
            return HttpResponseForbidden("Nur für Administratoren.")
        return view(request, *args, **kwargs)

    return wrapper


def is_htmx(request) -> bool:
    return request.headers.get("HX-Request") == "true"


def render_page_or_fragment(request, page: str, fragment: str, context: dict):
    return render(request, fragment if is_htmx(request) else page, context)
