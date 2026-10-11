from django.contrib import admin
from django.db import connection
from django.http import JsonResponse
from django.urls import include, path
from oauth2_provider import urls as oauth2_urls

from skontio.mcp.auth import mcp_endpoint


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
    path("oauth/", include((oauth2_urls.base_urlpatterns, "oauth2_provider"))),
    path("mcp", mcp_endpoint, name="mcp"),
]
