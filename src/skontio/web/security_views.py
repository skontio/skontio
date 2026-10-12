"""Einstellungen → Sicherheit: second factor for the local break-glass login."""

from __future__ import annotations

import base64
import io

import segno
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from skontio.accounts import totp, twofactor


def _qr_data_uri(uri: str) -> str:
    buffer = io.BytesIO()
    segno.make(uri, error="m").save(buffer, kind="svg", scale=4, border=2)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode()


@login_required
def security(request):
    user = request.user
    context = {
        "enabled": twofactor.is_enabled(user),
        "remaining": twofactor.remaining_recovery_codes(user),
        "recovery_codes": request.session.pop("new_recovery_codes", None),
    }
    secret = None if context["enabled"] else twofactor.pending_secret(user)
    if secret:
        uri = totp.provisioning_uri(secret, account=user.get_username())
        context |= {"secret": secret, "qr": _qr_data_uri(uri)}
    return render(request, "web/security.html", context)


@login_required
@require_POST
def security_start(request):
    try:
        twofactor.start_setup(request.user)
    except twofactor.TwoFactorError as exc:
        messages.error(request, str(exc))
    return redirect("security")


@login_required
@require_POST
def security_confirm(request):
    try:
        request.session["new_recovery_codes"] = twofactor.confirm_setup(request.user, request.POST.get("code", ""))
        messages.success(request, "Zweiter Faktor ist aktiv.")
    except twofactor.TwoFactorError as exc:
        messages.error(request, str(exc))
    return redirect("security")


@login_required
@require_POST
def security_disable(request):
    if not twofactor.verify(request.user, request.POST.get("code", "")):
        messages.error(request, "Zum Ausschalten bitte einen gültigen Code eingeben.")
    else:
        twofactor.disable(request.user)
        messages.success(request, "Zweiter Faktor ausgeschaltet. Der Notfall-Login ist damit gesperrt.")
    return redirect("security")
