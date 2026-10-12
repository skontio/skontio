from __future__ import annotations

import logging
import time

from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.views import LoginView
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from skontio.accounts import oidc, twofactor
from skontio.audit.services import record
from skontio.core.net import client_ip, ip_in

log = logging.getLogger(__name__)
SESSION_PENDING = "oidc_pending"
SESSION_KEY_FOR_ID_TOKEN_HINT = "oidc_id_token"  # noqa: S105 - session key name, not a secret
BACKEND = "django.contrib.auth.backends.ModelBackend"
SESSION_2FA = "local_2fa_pending"
SESSION_2FA_NEXT = "local_2fa_next"
SECOND_FACTOR_SECONDS = 300
MAX_SECOND_FACTOR_ATTEMPTS = 5


def _callback_uri() -> str:
    return f"{settings.PUBLIC_URL}/auth/callback"


def _safe_next(request: HttpRequest, candidate: str | None) -> str:
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return "/"


@require_GET
def oidc_login(request: HttpRequest) -> HttpResponse:
    try:
        auth = oidc.authorization_request(_callback_uri())
    except oidc.OidcError:
        log.exception("OIDC login could not be started")
        return HttpResponse("Anmeldung derzeit nicht möglich – bitte `skontio doctor oidc` ausführen.", status=503)
    request.session[SESSION_PENDING] = {
        "state": auth.state,
        "nonce": auth.nonce,
        "code_verifier": auth.code_verifier,
        "next": _safe_next(request, request.GET.get("next")),
    }
    return redirect(auth.url)


@require_GET
def oidc_callback(request: HttpRequest) -> HttpResponse:
    pending = request.session.pop(SESSION_PENDING, None)  # single use
    if not pending or not oidc.states_match(pending["state"], request.GET.get("state", "")):
        return HttpResponseBadRequest("Ungültige oder abgelaufene Anmeldung. Bitte erneut anmelden.")
    if "error" in request.GET or "code" not in request.GET:
        return HttpResponseBadRequest("Die Anmeldung wurde beim Identity Provider abgebrochen.")
    try:
        tokens = oidc.exchange_code(
            request.GET["code"], code_verifier=pending["code_verifier"], redirect_uri=_callback_uri()
        )
        claims = oidc.validate_id_token(tokens["id_token"], nonce=pending["nonce"])
        user = oidc.map_identity(claims)
    except oidc.OidcForbidden as exc:
        log.warning("OIDC login forbidden: %s", exc)
        return HttpResponseForbidden("Kein Zugriff auf Skontio. Bitte die Gruppenmitgliedschaft prüfen.")
    except oidc.OidcError:
        log.exception("OIDC login failed")
        return HttpResponseBadRequest("Anmeldung fehlgeschlagen.")
    login(request, user, backend=BACKEND)  # rotates the session key
    request.session[SESSION_KEY_FOR_ID_TOKEN_HINT] = tokens["id_token"]
    return redirect(pending["next"])


@require_POST
def oidc_logout(request: HttpRequest) -> HttpResponse:
    id_token = request.session.get(SESSION_KEY_FOR_ID_TOKEN_HINT)
    logout(request)
    if id_token:
        target = oidc.end_session_url(id_token, post_logout_redirect_uri=f"{settings.PUBLIC_URL}/")
        if target:
            return redirect(target)
    return redirect("/")


class LocalLoginView(LoginView):
    """Break-glass password login, only reachable from the local network (plan §10).

    With a second factor the password step only marks the session as pending; the login itself
    happens in ``LocalSecondFactorView``.
    """

    template_name = "accounts/local_login.html"

    def dispatch(self, request, *args, **kwargs):
        if not _from_lan(request):
            return HttpResponseForbidden("Der lokale Login ist nur aus dem lokalen Netz erlaubt.")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.get_user()
        if twofactor.is_enabled(user):
            self.request.session[SESSION_2FA] = {"user": user.pk, "since": time.time(), "attempts": 0}
            self.request.session[SESSION_2FA_NEXT] = self.get_success_url()
            return redirect("local_login_2fa")
        if settings.LOCAL_LOGIN_REQUIRE_2FA:
            form.add_error(
                None,
                "Für den Notfall-Login ist ein zweiter Faktor Pflicht. Einrichten unter Einstellungen → Sicherheit "
                "(nach Anmeldung über Pocket ID) oder per Kommandozeile: skontio totp setup <Benutzername>",
            )
            return self.form_invalid(form)
        record("auth.local_login", actor=user, entity="user", entity_id=user.pk, data={"second_factor": False})
        return super().form_valid(form)


def _from_lan(request) -> bool:
    return ip_in(client_ip(request), settings.LOCAL_LOGIN_NETWORKS)


def local_second_factor(request: HttpRequest) -> HttpResponse:
    if not _from_lan(request):
        return HttpResponseForbidden("Der lokale Login ist nur aus dem lokalen Netz erlaubt.")
    pending = request.session.get(SESSION_2FA)
    if not pending or time.time() - pending["since"] > SECOND_FACTOR_SECONDS:
        request.session.pop(SESSION_2FA, None)
        return redirect("local_login")
    error = ""
    if request.method == "POST":
        user = get_user_model().objects.filter(pk=pending["user"], is_active=True).first()
        if user is not None and twofactor.verify(user, request.POST.get("code", "")):
            request.session.pop(SESSION_2FA, None)
            target = request.session.pop(SESSION_2FA_NEXT, "/")
            login(request, user, backend=BACKEND)
            record("auth.local_login", actor=user, entity="user", entity_id=user.pk, data={"second_factor": True})
            return redirect(_safe_next(request, target))
        pending["attempts"] += 1
        if pending["attempts"] >= MAX_SECOND_FACTOR_ATTEMPTS:
            request.session.pop(SESSION_2FA, None)
            return redirect("local_login")
        request.session[SESSION_2FA] = pending
        error = "Der Code stimmt nicht."
    return render(request, "accounts/local_2fa.html", {"error": error})
