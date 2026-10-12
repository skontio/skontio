"""Verfahrensdokumentation: aus der Konfiguration erzeugt, versioniert und 10 Jahre archiviert."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from skontio.archive.services import load
from skontio.audit.models import AuditEvent
from skontio.core.models import ProcedureDocument
from skontio.core.procedure import ProcedureError, generate, render_procedure_html
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.mcp.tokens import create_token


@pytest.fixture
def busy_shop(shop, settings, tmp_path):
    settings.BACKUP_DIR = tmp_path / "backups"
    settings.BACKUP_DIR.mkdir()
    (settings.BACKUP_DIR / "skontio-20261011-020000.tar").write_bytes(b"x")
    doc = create_draft(
        shop.company,
        Document.Type.INVOICE,
        shop.org,
        issue_date=dt.date(2026, 10, 12),
        service_date=dt.date(2026, 10, 9),
    )
    add_line_from_item(doc, shop.item, quantity=Decimal("1"))
    finalize(doc)
    create_token(shop.user, name="Claude Desktop", scopes=["drafts:read"])
    return shop


def test_content_reflects_the_configuration(busy_shop):
    html = render_procedure_html(busy_shop.company, version=1, note="Ersterstellung")
    for expected in (
        "Muster Selbstständig",
        "Regelbesteuerung",  # tax status period
        "RE-{YYYY}-{####}",  # number range in use
        "inhaber",  # users
        "Claude Desktop",  # active personal access token
        "skontio-20261011-020000.tar",  # latest backup
        "Prüfung ohne Befund",  # audit chain verified
        "Version 1",
    ):
        assert expected in html, expected


def test_missing_backup_and_audit_findings_are_flagged(shop, settings, tmp_path, monkeypatch):
    settings.BACKUP_DIR = tmp_path / "none"
    monkeypatch.setattr("skontio.core.procedure.verify_chain", lambda: ["#3: Inhalt wurde verändert"])
    html = render_procedure_html(shop.company, version=1, note="x")
    assert "Keine Sicherung gefunden" in html
    assert "Prüfung meldet Probleme" in html and "#3: Inhalt wurde verändert" in html


def test_versions_are_archived_for_ten_years(busy_shop):
    first = generate(busy_shop.company, note="Ersterstellung", user=busy_shop.user)
    second = generate(busy_shop.company, note="Neuer Zugangstoken", user=busy_shop.user)
    assert (first.version, second.version) == (1, 2)
    assert load(second.blob).startswith(b"%PDF-")
    assert second.blob.retention_until == dt.date(timezone.localdate().year + 10, 12, 31)
    assert AuditEvent.objects.filter(action="procedure.generate").count() == 2
    html = render_procedure_html(busy_shop.company, version=3, note="x")
    assert html.index("Ersterstellung") < html.index("Neuer Zugangstoken")  # history in order


def test_reason_is_required(shop):
    with pytest.raises(ProcedureError, match="Anlass"):
        generate(shop.company, note="  ")


def test_web_generate_list_download(client, busy_shop):
    client.force_login(busy_shop.user)
    response = client.post("/einstellungen/verfahrensdokumentation/neu/", {"note": "Ersterstellung"})
    assert response.status_code == 302
    document = ProcedureDocument.objects.get()
    page = client.get("/einstellungen/verfahrensdokumentation/").content.decode()
    assert "v1 (PDF)" in page and "Ersterstellung" in page
    download = client.get(f"/einstellungen/verfahrensdokumentation/{document.pk}/")
    assert b"".join(download.streaming_content).startswith(b"%PDF-")
    assert "verfahrensdokumentation-v1.pdf" in download["Content-Disposition"]


def test_only_admins(client, shop, django_user_model):
    client.force_login(django_user_model.objects.create_user("mitarbeiter"))
    assert client.get("/einstellungen/verfahrensdokumentation/").status_code == 403
