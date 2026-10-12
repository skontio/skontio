from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.views import LoginView
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from skontio.accounts import oidc
from skontio.core.net import client_ip, ip_in

log = logging.getLogger(__name__)
SESSION_PENDING = "oidc_pending"
SESSION_KEY_FOR_ID_TOKEN_HINT = "oidc_id_token"  # noqa: S105 - session key name, not a secret
BACKEND = "django.contrib.auth.backends.ModelBackend"


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
    """Break-glass password login, only reachable from the local network (plan §10)."""

    template_name = "accounts/local_login.html"

    def dispatch(self, request, *args, **kwargs):
        if not ip_in(client_ip(request), settings.LOCAL_LOGIN_NETWORKS):
            return HttpResponseForbidden("Der lokale Login ist nur aus dem lokalen Netz erlaubt.")
        return super().dispatch(request, *args, **kwargs)
