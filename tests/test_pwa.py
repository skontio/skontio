"""PWA: installierbar auf Android, ohne personenbezogene Daten im Service-Worker-Cache."""

from __future__ import annotations

import json

import pytest
from django.contrib.staticfiles import finders


@pytest.mark.django_db
def test_manifest(client):
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/manifest+json")
    manifest = json.loads(response.content)
    assert manifest["name"] == "Skontio"
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    sizes = {(icon["sizes"], icon.get("purpose", "any")) for icon in manifest["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for icon in manifest["icons"]:
        assert finders.find(icon["src"].removeprefix("/static/")), icon["src"]


@pytest.mark.django_db
def test_service_worker_is_served_from_root_and_never_cached(client):
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/javascript")
    assert "no-cache" in response["Cache-Control"]
    body = response.content.decode()
    assert "/static/" in body
    # only the app shell is cached – never documents, customers or API responses
    for forbidden in ("/dokumente", "/kunden", "/api", "/mcp"):
        assert f"'{forbidden}" not in body and f'"{forbidden}' not in body


@pytest.mark.django_db
def test_pages_link_manifest_and_registration_script(client, django_user_model):
    client.force_login(django_user_model.objects.create_user("x", is_staff=True))
    body = client.get("/einrichtung/").content.decode()
    assert 'rel="manifest" href="/manifest.webmanifest"' in body
    assert "web/app.js" in body
    assert 'name="theme-color"' in body
    assert "<script>" not in body  # CSP: no inline scripts
