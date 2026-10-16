from django.db import connection
from django.http import JsonResponse


class HealthCheckMiddleware:
    """Answer ``/healthz`` before Django validates the Host header.

    The Docker/TrueNAS health check requests ``http://127.0.0.1:8000/healthz``; that host is not in
    ALLOWED_HOSTS, so going through the normal stack would return 400 and mark the container unhealthy.
    Must stay first in MIDDLEWARE. Reveals nothing beyond "the app and its database respond".
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path_info == "/healthz":
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            return JsonResponse({"status": "ok"})
        return self.get_response(request)
