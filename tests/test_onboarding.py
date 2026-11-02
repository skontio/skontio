"""Einrichtungsassistent: Schritte, Pflichtangaben je Rechtsform, Fortschritt – und austauschbares Aussehen."""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from skontio.core import onboarding
from skontio.core.models import Company
from skontio.documents.layouts import default_template
from skontio.documents.models import Document
from skontio.documents.renditions import render_document_html
from skontio.documents.services import add_line_from_item, create_draft, finalize

SETUP = {
    "name": "Muster GmbH", "legal_form": "gmbh", "register_court": "Amtsgericht Musterstadt",
    "register_number": "HRB 12345", "managing_directors": "Erika Muster", "street": "Weg 1", "postcode": "12345",
    "city": "Stadt", "country": "DE", "activity": "trade", "tax_number": "12/345/67890", "email": "info@muster.example",
    "regime": "standard", "regime_since": "2026-01-01", "default_payment_terms_days": "14",
}  # fmt: skip


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), "#336699").save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def admin(client, django_user_model):
    user = django_user_model.objects.create_user("inhaberin", is_staff=True, email="inhaberin@example.org")
    client.force_login(user)
    return user


def test_whole_wizard_from_scratch(client, admin):
    response = client.post("/einrichtung/", SETUP)
    assert response.status_code == 302 and response["Location"] == "/einrichtung/assistent/aussehen/"
    company = Company.objects.get()
    assert company.legal_form == "gmbh" and onboarding.progress(company) == (1, 5)

    logo = SimpleUploadedFile("logo.png", png(), content_type="image/png")
    response = client.post("/einrichtung/assistent/aussehen/", {"logo": logo, "accent_color": "#aa3300",
                                                                "layout": "modern"})  # fmt: skip
    assert response["Location"] == "/einrichtung/assistent/bank/"
    assert default_template(company).logo_id and default_template(company).accent_color == "#aa3300"

    bank = {"iban": "DE02120300000000202051", "bic": "", "account_holder": "Muster GmbH", "auto_match_payments": "on"}
    response = client.post("/einrichtung/assistent/bank/", bank)
    company.refresh_from_db()
    assert response["Location"] == "/einrichtung/assistent/nummern/" and company.auto_match_payments
    year = dt.date.today().year
    assert f"RE-{year}-0001" in client.get("/einrichtung/assistent/nummern/").content.decode()
    client.post("/einrichtung/assistent/nummern/")
    shipping = {"email": "rechnung@muster.example", "default_payment_terms_days": "30", "payment_link": ""}
    response = client.post("/einrichtung/assistent/versand/", shipping)
    company.refresh_from_db()
    assert response["Location"] == "/einrichtung/assistent/" and onboarding.complete(company)
    assert company.default_payment_terms_days == 30
    assert "Alles eingerichtet" in client.get("/einrichtung/assistent/").content.decode()
    assert "Schritten erledigt" not in client.get("/").content.decode()


def test_gmbh_needs_register_and_directors(client, admin):
    incomplete = {**SETUP, "register_number": "", "managing_directors": " "}
    response = client.post("/einrichtung/", incomplete)
    assert response.status_code == 200 and not Company.objects.exists()
    page = response.content.decode()
    assert "Registernummer auf jede Rechnung" in page and "Geschäftsführer auf jede Rechnung" in page
    sole = {**SETUP, "legal_form": "sole", "register_court": "", "register_number": "", "managing_directors": ""}
    assert client.post("/einrichtung/", sole).status_code == 302


def test_mandatory_details_appear_on_invoices(shop):
    company = shop.company
    company.legal_form, company.register_court, company.register_number = "gmbh", "AG Musterstadt", "HRB 999"
    company.managing_directors = "Erika Muster, Max Muster"
    company.save()
    doc = create_draft(company, Document.Type.INVOICE, shop.org, issue_date=dt.date(2026, 3, 2),
                       service_date=dt.date(2026, 3, 2))  # fmt: skip
    add_line_from_item(doc, shop.item, quantity=Decimal("1"))
    html = render_document_html(finalize(doc))
    assert "AG Musterstadt HRB 999" in html and "Geschäftsführung: Erika Muster, Max Muster" in html


def test_skip_and_progress_on_dashboard(client, shop):
    client.force_login(shop.user)
    assert "Schritten erledigt" in client.get("/").content.decode()
    response = client.post("/einrichtung/assistent/aussehen/ueberspringen/")
    assert response.status_code == 302
    shop.company.refresh_from_db()
    assert shop.company.onboarding["aussehen"] == "skipped"
    assert client.post("/einrichtung/assistent/erfunden/ueberspringen/").status_code == 404


def test_test_mail(client, shop):
    client.force_login(shop.user)
    client.post("/einrichtung/assistent/versand/", {"aktion": "testmail"})
    assert len(mail.outbox) == 1 and mail.outbox[0].to == [shop.company.email]


def test_bad_logo_is_refused(client, shop):
    client.force_login(shop.user)
    bad = SimpleUploadedFile("logo.png", b"<svg onload=alert(1)>", content_type="image/png")
    response = client.post("/einrichtung/assistent/aussehen/", {"logo": bad, "accent_color": "#000000",
                                                                "layout": "classic"})  # fmt: skip
    assert response.status_code == 200 and "PNG oder JPEG" in response.content.decode()
    assert "aussehen" not in (Company.objects.get().onboarding or {})


def test_only_admins(client, shop, django_user_model):
    client.force_login(django_user_model.objects.create_user("mitarbeit"))
    for url in ("/einrichtung/assistent/", "/einrichtung/assistent/bank/", "/einrichtung/assistent/versand/"):
        assert client.get(url).status_code == 403


def test_theme_file(client, shop, settings, tmp_path):
    client.force_login(shop.user)
    assert client.get("/theme.css").status_code == 404
    assert 'href="/theme.css?v=' not in client.get("/").content.decode()
    (tmp_path / "theme.css").write_text(":root { --blue: #0b7a75; }")
    settings.THEME_DIR = tmp_path
    page = client.get("/").content.decode()
    assert "web/theme.css" in page and 'href="/theme.css?v=' in page
    response = client.get("/theme.css")
    assert response.status_code == 200 and b"--blue: #0b7a75" in response.content
    assert response["Content-Type"].startswith("text/css")
    client.logout()
    assert client.get("/theme.css").status_code == 200  # the login page needs it too


def test_app_css_uses_only_theme_variables_for_colours():
    """The split keeps working: colour values live in theme.css, app.css refers to variables."""
    from pathlib import Path

    import skontio.web

    static = Path(skontio.web.__file__).parent / "static" / "web"
    assert ":root" not in (static / "app.css").read_text() and "@font-face" not in (static / "app.css").read_text()
    assert "--blue:" in (static / "theme.css").read_text()
