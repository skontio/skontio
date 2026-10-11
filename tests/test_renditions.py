"""Festschreiben erzeugt PDF/E-Rechnung und legt sie revisionssicher im Archiv ab."""

from __future__ import annotations

import datetime as dt
import io
import os
import stat
from decimal import Decimal

import facturx
import pytest
from lxml import etree
from pypdf import PdfReader

from skontio.archive.services import ArchiveCorrupted, load, store
from skontio.catalog.services import create_item
from skontio.core.models import Company
from skontio.crm.services import create_customer
from skontio.documents.models import Document, Rendition
from skontio.documents.services import add_line_from_item, cancel_invoice, create_draft, finalize
from skontio.einvoice.mapping import invoice_from_document
from skontio.rendering.girocode import epc_payload
from skontio.tax.engine import Regime
from skontio.taxstatus.services import start_regime

D = Decimal


@pytest.fixture(autouse=True)
def archive_dir(tmp_path, settings):
    settings.ARCHIVE_DIR = tmp_path / "archive"
    return settings.ARCHIVE_DIR


@pytest.fixture
def company(db):
    company = Company.objects.create(
        name="Muster Selbstständig",
        street="Musterweg 1",
        postcode="12345",
        city="Musterstadt",
        tax_number="123/456/78901",
        seller_identifier="123/456/78901",
        email="rechnung@example.org",
        phone="+49 30 1234567",
        contact_name="Max Muster",
        iban="DE02120300000000202051",
        bic="BYLADEM1001",
        account_holder="Max Muster",
    )
    start_regime(company, Regime.SMALL_BUSINESS, dt.date(2024, 1, 1))
    return company


@pytest.fixture
def customer(company):
    return create_customer(
        company,
        name="Kunde GmbH",
        street="Kundenstraße 5",
        postcode="54321",
        city="Köln",
        email="einkauf@kunde.example",
        buyer_reference="Bestellung 4711",
    )


@pytest.fixture
def invoice(company, customer):
    doc = create_draft(
        company, Document.Type.INVOICE, customer, issue_date=dt.date(2026, 10, 11), service_date=dt.date(2026, 10, 9)
    )
    add_line_from_item(doc, create_item(company, name="Beratung", unit="HUR", net_price=D("80")), quantity=D("3"))
    return finalize(doc)


def local(root, path: str) -> str:
    return root.xpath(f"string({path})")


@pytest.mark.django_db
class TestArchive:
    def test_store_is_content_addressed_and_write_once(self, archive_dir):
        first = store(b"%PDF-1.7 demo", mime="application/pdf")
        again = store(b"%PDF-1.7 demo", mime="application/pdf")
        assert first.pk == again.pk
        path = archive_dir / first.storage_key
        assert path.read_bytes() == b"%PDF-1.7 demo"
        assert not os.stat(path).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        assert load(first) == b"%PDF-1.7 demo"

    def test_tampering_is_detected(self, archive_dir):
        blob = store(b"original", mime="text/plain")
        path = archive_dir / blob.storage_key
        os.chmod(path, 0o600)
        path.write_bytes(b"manipulated")
        with pytest.raises(ArchiveCorrupted):
            load(blob)

    def test_missing_file_is_detected(self, archive_dir):
        blob = store(b"gone", mime="text/plain")
        (archive_dir / blob.storage_key).unlink()
        with pytest.raises(ArchiveCorrupted):
            load(blob)


@pytest.mark.django_db
class TestFinalizeRenditions:
    def test_invoice_gets_zugferd_pdf_and_xrechnung(self, invoice):
        kinds = set(Rendition.objects.filter(document=invoice).values_list("kind", flat=True))
        assert kinds == {Rendition.Kind.ZUGFERD_PDF, Rendition.Kind.XRECHNUNG_XML}

    def test_embedded_xml_matches_document(self, invoice):
        pdf = load(Rendition.objects.get(document=invoice, kind=Rendition.Kind.ZUGFERD_PDF).blob)
        _, xml = facturx.get_xml_from_pdf(pdf, check_xsd=False)
        root = etree.fromstring(xml)
        assert local(root, "//*[local-name()='GrandTotalAmount']") == str(invoice.gross_total)
        assert local(root, "/*/*[local-name()='ExchangedDocument']/*[local-name()='ID']") == invoice.number

    def test_pdf_shows_mandatory_texts(self, invoice):
        pdf = load(Rendition.objects.get(document=invoice, kind=Rendition.Kind.ZUGFERD_PDF).blob)
        text = "".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
        for expected in ("RE-2026-0001", "Kunde GmbH", "§ 19 UStG", "123/456/78901", "240,00", "09.10.2026"):
            assert expected in text, expected

    def test_xrechnung_is_skipped_when_buyer_data_is_incomplete(self, company):
        bare = create_customer(company, name="Privat", street="Weg 1", postcode="1", city="Ort")
        doc = create_draft(
            company, Document.Type.INVOICE, bare, issue_date=dt.date(2026, 10, 11), service_date=dt.date(2026, 10, 9)
        )
        add_line_from_item(doc, create_item(company, name="Hilfe", unit="HUR", net_price=D("50")), quantity=D("1"))
        doc = finalize(doc)
        kinds = set(Rendition.objects.filter(document=doc).values_list("kind", flat=True))
        assert kinds == {Rendition.Kind.ZUGFERD_PDF}

    def test_quote_gets_plain_pdf(self, company, customer):
        quote = create_draft(company, Document.Type.QUOTE, customer, issue_date=dt.date(2026, 10, 11))
        workshop = create_item(company, name="Workshop", unit="DAY", net_price=D("900"))
        add_line_from_item(quote, workshop, quantity=D("1"))
        quote = finalize(quote)
        assert set(Rendition.objects.filter(document=quote).values_list("kind", flat=True)) == {Rendition.Kind.PDF}

    def test_cancellation_gets_credit_note_xml(self, invoice):
        storno = cancel_invoice(invoice, issue_date=dt.date(2026, 10, 12), reason="Doppelt")
        pdf = load(Rendition.objects.get(document=storno, kind=Rendition.Kind.ZUGFERD_PDF).blob)
        _, xml = facturx.get_xml_from_pdf(pdf, check_xsd=False)
        assert local(etree.fromstring(xml), "//*[local-name()='ExchangedDocument']/*[local-name()='TypeCode']") == "381"

    def test_blobs_are_kept_for_retention(self, invoice):
        blob = Rendition.objects.get(document=invoice, kind=Rendition.Kind.ZUGFERD_PDF).blob
        assert blob.retention_until == dt.date(2034, 12, 31)  # 8 years after end of 2026 (§ 147 Abs. 3 AO)


@pytest.mark.einvoice
@pytest.mark.django_db
def test_finalized_renditions_pass_official_validators(invoice, tmp_path):
    from skontio.einvoice.validation import ValidatorUnavailable, validate_with_kosit, validate_with_mustang

    pdf = load(Rendition.objects.get(document=invoice, kind=Rendition.Kind.ZUGFERD_PDF).blob)
    xml = load(Rendition.objects.get(document=invoice, kind=Rendition.Kind.XRECHNUNG_XML).blob)
    try:
        mustang = validate_with_mustang(pdf, workdir=tmp_path)
        kosit = validate_with_kosit(xml, workdir=tmp_path)
    except ValidatorUnavailable as exc:
        pytest.skip(str(exc))
    assert mustang.valid, mustang.summary
    assert b"isCompliant=true" in mustang.report
    assert kosit.valid, kosit.summary


@pytest.mark.django_db
def test_mapping_uses_snapshot_not_current_master_data(invoice, customer):
    customer.name = "Umbenannt AG"
    customer.save()
    assert invoice_from_document(invoice).buyer.name == "Kunde GmbH"


def test_girocode_payload():
    payload = epc_payload(
        name="Max Muster",
        iban="DE02 1203 0000 0000 2020 51",
        bic="BYLADEM1001",
        amount=D("240.00"),
        reference="RE-2026-0001",
    )
    lines = payload.split("\n")
    assert lines[:3] == ["BCD", "002", "1"]
    assert lines[3:8] == ["SCT", "BYLADEM1001", "Max Muster", "DE02120300000000202051", "EUR240.00"]
    assert lines[10] == "RE-2026-0001"
    assert len(payload.encode()) <= 331


def test_girocode_rejects_invalid_amounts():
    with pytest.raises(ValueError):
        epc_payload(name="X", iban="DE02120300000000202051", amount=D("0"), reference="R")
    with pytest.raises(ValueError):
        epc_payload(name="X", iban="DE02120300000000202051", amount=D("1000000000"), reference="R")
