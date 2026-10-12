"""Progressive Web App: manifest and service worker (plan §8).

The service worker only caches the static app shell. Pages and API responses always come
from the network, so no customer or invoice data ends up in the browser cache.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.http import HttpResponse
from django.templatetags.static import static
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

from skontio import __version__

THEME_COLOR = "#1f5f8b"

SHELL = [
    "web/app.css",
    "web/app.js",
    "web/vendor/htmx.min.js",
    "web/icons/icon-192.png",
]

SERVICE_WORKER = """\
// Skontio service worker – caches the static app shell only (never documents or customer data).
const CACHE = "skontio-shell-%(version)s";
const SHELL = %(shell)s;
const OFFLINE = "<!doctype html><html lang=\\"de\\"><meta charset=\\"utf-8\\"><meta name=\\"viewport\\" " +
  "content=\\"width=device-width, initial-scale=1\\"><title>Offline · Skontio</title>" +
  "<body><h1>Keine Verbindung</h1>" +
  "<p>Skontio braucht eine Verbindung zum Server. Bitte Netz oder VPN prüfen.</p></body></html>";

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("%(static_url)s")) {
    event.respondWith(caches.match(request).then((hit) => hit || fetch(request)));
    return;
  }
  if (request.mode === "navigate") {
    // network only; show a data-free offline page when the server is unreachable
    event.respondWith(
      fetch(request).catch(() => new Response(OFFLINE, { headers: { "Content-Type": "text/html; charset=utf-8" } }))
    );
  }
});

self.addEventListener("message", (event) => {
  if (event.data === "logout") {
    event.waitUntil(caches.keys().then((keys) => Promise.all(keys.map((key) => caches.delete(key)))));
  }
});
"""


@require_GET
@cache_control(max_age=3600)
def manifest(request):
    data = {
        "name": "Skontio",
        "short_name": "Skontio",
        "description": "Rechnungen und Kunden für Selbstständige",
        "lang": "de",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#f6f7f9",
        "theme_color": THEME_COLOR,
        "icons": [
            {"src": static("web/icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": static("web/icons/icon-512.png"), "sizes": "512x512", "type": "image/png"},
            {
                "src": static("web/icons/icon-maskable-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
        "share_target": {
            "action": "/ausgaben/teilen/",
            "method": "POST",
            "enctype": "multipart/form-data",
            "params": {
                "title": "title",
                "text": "text",
                "url": "url",
                "files": [{"name": "receipt", "accept": ["image/*", "application/pdf", ".pdf"]}],
            },
        },
        "shortcuts": [
            {"name": "Beleg erfassen", "url": "/ausgaben/neu/"},
            {"name": "Neue Rechnung", "url": "/dokumente/neu/?typ=invoice"},
            {"name": "Kunden", "url": "/kunden/"},
        ],
    }
    return HttpResponse(json.dumps(data, ensure_ascii=False), content_type="application/manifest+json")


@require_GET
@cache_control(no_cache=True, max_age=0)
def service_worker(request):
    body = SERVICE_WORKER % {
        "version": __version__,
        "shell": json.dumps([static(path) for path in SHELL]),
        "static_url": settings.STATIC_URL if settings.STATIC_URL.startswith("/") else f"/{settings.STATIC_URL}",
    }
    response = HttpResponse(body, content_type="text/javascript; charset=utf-8")
    response["Service-Worker-Allowed"] = "/"
    return response
