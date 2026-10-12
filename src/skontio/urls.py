from django.contrib import admin
from django.db import connection
from django.http import JsonResponse
from django.urls import include, path
from oauth2_provider import urls as oauth2_urls

from skontio.accounts import views as accounts
from skontio.mcp import views as mcp_views
from skontio.mcp.auth import mcp_endpoint
from skontio.web import security_views as security


def healthz(request):
    """Liveness and DB check for Docker/TrueNAS health checks."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    # RFC 8414 / RFC 9728 documents must live at the origin root
    path("", include((oauth2_urls.metadata_urlpatterns, "oauth2_metadata"))),
    # own consent screen (admins only, German, privacy notes) in front of the toolkit's view
    path("oauth/authorize/", mcp_views.ConsentView.as_view(), name="oauth_authorize"),
    path("oauth/", include((oauth2_urls.base_urlpatterns, "oauth2_provider"))),
    path("einstellungen/ki-zugriff/", mcp_views.ai_access, name="ai_access"),
    path("einstellungen/ki-zugriff/<int:pk>/widerrufen/", mcp_views.ai_access_revoke, name="ai_access_revoke"),
    path("mcp", mcp_endpoint, name="mcp"),
    path("auth/login", accounts.oidc_login, name="login"),
    path("auth/callback", accounts.oidc_callback, name="oidc_callback"),
    path("auth/logout", accounts.oidc_logout, name="logout"),
    path("auth/local/", accounts.LocalLoginView.as_view(), name="local_login"),
    path("auth/local/2fa/", accounts.local_second_factor, name="local_login_2fa"),
    path("einstellungen/sicherheit/", security.security, name="security"),
    path("einstellungen/sicherheit/einrichten/", security.security_start, name="security_start"),
    path("einstellungen/sicherheit/bestaetigen/", security.security_confirm, name="security_confirm"),
    path("einstellungen/sicherheit/aus/", security.security_disable, name="security_disable"),
    path("", include("skontio.web.urls")),
]
