"""Gesamtexport als ZIP: Inhalt, Archiv geprüft, fremde Firma fehlt, sichere Dateinamen, nur Admins."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import zipfile
from decimal import Decimal

import pytest

from skontio.archive.services import _root
from skontio.attachments.services import add_attachment
from skontio.audit.models import AuditEvent
from skontio.catalog.services import create_item
from skontio.core.models import Company
from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.payments.services import record_payment
from skontio.tax.engine import Regime
from skontio.taxstatus.services import start_regime
from skontio.web.full_export import build
from skontio.web.transfer import import_customers

DAY = dt.date(2026, 3, 2)
CSVS = ("kontakte.csv", "leistungen.csv", "dokumente.csv", "positionen.csv", "zahlungen.csv", "ausgaben.csv",
        "bankumsaetze.csv", "mahnungen.csv", "einnahmen.csv", "LIESMICH.txt")  # fmt: skip


def invoice(company, customer, item):
    draft = create_draft(company, Document.Type.INVOICE, customer, issue_date=DAY, service_date=DAY)
    add_line_from_item(draft, item, quantity=Decimal("2"))
    return finalize(draft)


def zip_of(company) -> zipfile.ZipFile:
    out = io.BytesIO()
    build(company, out)
    return zipfile.ZipFile(io.BytesIO(out.getvalue()))


def text(zf, name) -> str:
    return zf.read(name).decode("utf-8-sig")


@pytest.fixture
def doc(shop):
    return invoice(shop.company, shop.org, shop.item)


def test_contains_csv_and_verified_originals(shop, doc):
    record_payment(doc, doc.gross_total, DAY)
    zf = zip_of(shop.company)
    names = set(zf.namelist())
    assert set(CSVS) <= names
    assert doc.number in text(zf, "dokumente.csv") and "Beratung" in text(zf, "positionen.csv")
    assert doc.number in text(zf, "zahlungen.csv")
    files = [n for n in names if n.startswith(f"dokumente/{doc.number}")]
    for rendition in doc.renditions.all():
        assert any(hashlib.sha256(zf.read(n)).hexdigest() == rendition.blob.sha256 for n in files)
    assert "FEHLER.txt" not in names


def test_contacts_in_the_export_import_again(shop, doc):
    result = import_customers(shop.company, zip_of(shop.company).read("kontakte.csv"))
    assert result.errors == [] and result.created == 0 and len(result.skipped) == 2


def test_altered_archive_file_is_reported_not_exported(shop, doc):
    blob = doc.renditions.first().blob
    path = _root() / blob.storage_key
    path.chmod(0o600)  # archive files are read-only on purpose
    path.write_bytes(b"manipuliert")
    zf = zip_of(shop.company)
    assert "FEHLER.txt" in zf.namelist() and "verändert" in text(zf, "FEHLER.txt")
    assert b"manipuliert" not in b"".join(zf.read(n) for n in zf.namelist())


def test_other_company_is_not_exported(shop, doc):
    other = Company.objects.create(
        name="Fremd", street="X", postcode="1", city="Y", tax_number="9/9/9", iban="DE02120300000000202051"
    )
    start_regime(other, Regime.STANDARD, dt.date(2024, 1, 1))
    invoice(
        other,
        create_customer(other, name="Geheimkunde", street="X", postcode="1", city="Y"),
        create_item(other, name="Geheimleistung", unit="LS", net_price=Decimal("1")),
    )
    zf = zip_of(shop.company)
    assert "Geheim" not in "".join(text(zf, n) for n in zf.namelist() if n.endswith(".csv"))
    assert len([n for n in zf.namelist() if n.startswith("dokumente/")]) == doc.renditions.count()


def test_hostile_file_names_stay_inside_their_folder(shop, doc):
    for name in ("../../etc/passwd.pdf", "..\\..\\boot.ini", "/abs.pdf", "a\x00b.pdf", "same.pdf", "same.pdf"):
        add_attachment(shop.company, b"%PDF-1.4 x" + name.encode(), filename=name, document=doc)
    names = zip_of(shop.company).namelist()
    for name in names:
        assert not name.startswith("/") and ".." not in name.split("/") and "\\" not in name and "\x00" not in name
    attached = [n for n in names if n.startswith("dateien/")]
    assert len(attached) == len(set(attached)) == 6


def test_csv_in_the_export_is_formula_safe(shop):
    evil = create_customer(shop.company, name="=cmd|' /C calc'!A0", street="W", postcode="1", city="K")
    invoice(shop.company, evil, shop.item)
    zf = zip_of(shop.company)
    for name in ("kontakte.csv", "dokumente.csv"):
        assert "'=cmd" in text(zf, name) and ";=cmd" not in text(zf, name)


def test_download_is_post_only_admin_only_and_audited(client, shop, doc, django_user_model):
    client.force_login(shop.user)
    page = client.get("/einstellungen/gesamtexport/")
    assert page.status_code == 200 and page["Content-Type"].startswith("text/html")
    response = client.post("/einstellungen/gesamtexport/")
    assert response.status_code == 200 and response["Content-Type"] == "application/zip"
    assert 'filename="skontio-gesamtexport-' in response["Content-Disposition"]
    assert zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content))).testzip() is None
    assert AuditEvent.objects.filter(action="export.full").exists()

    client.force_login(django_user_model.objects.create_user("mitarbeit"))
    assert client.post("/einstellungen/gesamtexport/").status_code == 403
    client.logout()
    assert client.post("/einstellungen/gesamtexport/").status_code == 302
