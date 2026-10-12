"""Consent screen for MCP clients and the "KI-Zugriff" page (connected clients, revoke, AI audit trail)."""

from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Max, Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from oauth2_provider.models import AccessToken, Application, RefreshToken
from oauth2_provider.views import AuthorizationView

from skontio.audit.models import AuditEvent
from skontio.audit.services import record
from skontio.web.support import admin_required


class ConsentView(AuthorizationView):
    """Only administrators may grant an AI client access to the company's data."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.is_staff:
            return HttpResponseForbidden("Nur Administratoren dürfen KI-Clients freigeben.")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["redirect_host"] = urlparse(context.get("redirect_uri") or "").hostname or "?"
        context["pseudonymised"] = settings.MCP_PSEUDONYMISE_PERSONS
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        if form.cleaned_data.get("allow"):
            application = Application.objects.filter(client_id=form.cleaned_data.get("client_id")).first()
            record(
                "mcp.consent",
                actor=self.request.user,
                entity="oauth_application",
                entity_id=application.pk if application else "",
                data={"scopes": sorted(form.cleaned_data.get("scope", "").split())},
            )
        return response


@admin_required
def ai_access(request):
    now = timezone.now()
    clients = (
        Application.objects.annotate(
            active=Count("accesstoken", filter=Q(accesstoken__expires__gt=now), distinct=True),
            refreshable=Count("refreshtoken", filter=Q(refreshtoken__revoked__isnull=True), distinct=True),
            last_used=Max("accesstoken__created"),
        )
        .filter(Q(active__gt=0) | Q(refreshable__gt=0))
        .order_by("name")
    )
    events = AuditEvent.objects.filter(ai_initiated=True).select_related("actor")[:50]
    context = {
        "clients": clients,
        "events": events,
        "pseudonymised": settings.MCP_PSEUDONYMISE_PERSONS,
        "mcp_url": f"{settings.PUBLIC_URL}/mcp",
    }
    return render(request, "web/ai_access.html", context)


@admin_required
@require_POST
def ai_access_revoke(request, pk: int):
    application = get_object_or_404(Application, pk=pk)
    with transaction.atomic():
        for refresh in RefreshToken.objects.filter(application=application, revoked__isnull=True):
            refresh.revoke()  # also removes the linked access token
        AccessToken.objects.filter(application=application).delete()
        record("mcp.revoke", actor=request.user, entity="oauth_application", entity_id=application.pk)
    messages.success(request, f"Zugriff für {application.name} widerrufen.")
    return redirect("ai_access")
