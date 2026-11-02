"""Umzug aus Papierkram – mit einem erfundenen Export im Originalformat (keine echten Daten)."""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from skontio.banking.models import BankTransaction
from skontio.core.models import Company
from skontio.crm.models import Customer
from skontio.documents.models import Document
from skontio.imports.papierkram import PapierkramError, import_papierkram, lines_for
from skontio.mail.services import MailError, send_document
from skontio.numbering.models import NumberSequence
from skontio.payments.services import open_amount
from skontio.tax.engine import Regime, TaxTreatment
from skontio.taxstatus.services import start_regime

D = Decimal
FILES = {
    "Kontakte.csv": (
        "Name;Kontaktart;Kunden-/Lieferantennr.;Straße (Post);PLZ (Post);Stadt (Post);Land (Post);E-Mail;Telefon;"
        "UStID\n"
        "Musterverein e.V.;Kunde;K-00001;Hauptstr. 1;12345;Musterstadt;Deutschland;info@verein.example;;\n"
        "Erika Beispiel;Kunde;K-00002;Weg 2;12345;Musterstadt;Österreich;erika@example.org;;ungültig\n"
    ),
    "Waren_oder_Dienstleistungen.csv": (
        "Name;Artikelnummer;Beschreibung;Typ;Netto-Preis;Brutto-Preis;Einheitbezeichnung Einzel;"
        "Einheitbezeichnung Mehrfach;USt.-Satz\n"
        "Webpflege;AN-00001;Updates;Dienstleistung;100,00;100,00;Stunde;Stunden;0.0\n"
    ),
    "Rechnungen.csv": (
        "Rechnungsnr.;Betreff;Kunde;Land;Projekt;Kundennummer;Datum;Fälligkeitsdatum;Status;Typ;Netto;USt.;Brutto;"
        "Skonto;Bezahlt;letztes Bezahldatum;Notiz;Mahnungsdatum;Waren & Dienstleistungen\n"
        'R-00001;Webpflege;Musterverein e.V.;Deutschland;"";K-00001;02.02.2026;16.02.2026;Bezahlt;Rechnung;'
        '1.200,00;0,00;1.200,00;0,00;1.200,00;2026-02-05;"";"";AN-00001\n'
        'R-00002;"";Erika Beispiel;Deutschland;"";K-00002;09.03.2026;23.03.2026;Offen;Rechnung;50,00;0,00;50,00;'
        '0,00;0,00;"";"";"";""\n'
    ),
    "Angebote.csv": (
        "Angebots-Nr.;Betreff;Kunde;Kundennummer;Datum;Sendungsdatum;Status;Typ;Netto;USt.;Brutto;Notiz\n"
        'A-00001;Relaunch;Musterverein e.V.;K-00001;01.01.2026;02.01.2026;Angenommen;Angebot;900,00;0,00;900,00;""\n'
        'Entwurf;Hosting;Erika Beispiel;K-00002;03.04.2026;"";Entwurf;Angebot;15,00;0,00;15,00;""\n'
    ),
    "Zahlungseingänge.csv": "Buchung;Kundennummer;Rechnungsnummer;Belegnummer;Datum;Betrag\n"
    'Zahlung zu R-00001;K-00001;R-00001;"";2026-02-05;1.200,00\n',
    "Bankverbindungen.csv": "Name;Kontonummer;BLZ;IBAN;BIC;Hausbank;Benutzerkennung;Kundenkennung\n"
    "Giro;;;DE02120300000000202051;;ja;;\n",
    "Kontoumsätze.csv": (
        "Status;Zahlungswert;Zahlungswährung;Storniert;Kunden-Referenz;Instref;GV-Code;Kategorie;Verwendungszweck;"
        "Primanota;SEPA?;Absender-IBAN;Absender-BIC;Absender-Kontonummer;Absender-BLZ;Absender-Währung;Absender-Name;"
        "Saldo-Wert;Saldo-Währung;Saldo-Zeitpunkt;Bankverbindung-ID;Valuta;Buchungsdatum;Importierungsdatum\n"
        "cleared;1.200,00;EUR;nein;;;;;R-00001;;;DE89370400440532013000;;;;;Musterverein e.V.;;;;1;;05.02.2026;\n"
        "cleared;-11,90;EUR;nein;;;;;Server;;;;;;;;Hosting AG;;;;1;;06.02.2026;\n"
    ),
    "Projekte.csv": "Name;Kunde;Mitglieder;Startdatum;Enddatum;Budget\nWebsite;Musterverein e.V.;;;;\n",
}


def export(files=FILES) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(f"CSV/{name}", text)
    return buffer.getvalue()


@pytest.fixture
def company(db):
    company = Company.objects.create(name="Neu", street="W", postcode="1", city="S", tax_number="1/2/3",
                                     iban="DE02120300000000202051")  # fmt: skip
    start_regime(company, Regime.SMALL_BUSINESS, dt.date(2026, 1, 1))
    return company


def test_dry_run_saves_nothing(company):
    report = import_papierkram(company, export(), dry_run=True)
    assert report.ok and report.created["Rechnungen"] == 2 and report.not_imported == {"Projekte": 1}
    assert not Customer.objects.exists() and not Document.objects.exists()


def test_import_keeps_numbers_totals_payments_and_continues_numbering(company):
    report = import_papierkram(company, export())
    assert report.ok, report.errors
    assert dict(report.created) == {
        "Kontakte": 2, "Leistungen": 1, "Rechnungen": 2, "Angebote": 1, "Angebote (Entwurf)": 1, "Zahlungen": 1,
        "Bankkonten": 1, "Kontoumsätze": 2,
    }  # fmt: skip
    r1 = Document.objects.get(number="R-00001")
    assert r1.imported_from == "Papierkram" and r1.status == "finalized" and r1.gross_total == D("1200.00")
    assert not r1.renditions.exists() and open_amount(r1) == 0
    assert open_amount(Document.objects.get(number="R-00002")) == D("50.00")
    erika = Customer.objects.get(number="K-00002")
    assert erika.country == "AT" and erika.kind == "person"
    assert any("ungültig" in w for w in report.warnings)  # bad VAT id dropped, reported
    assert BankTransaction.objects.get(amount=D("1200.00")).payment.document == r1
    invoices = NumberSequence.objects.get(company=company, doc_type="invoice")
    assert (invoices.pattern, invoices.next_value) == ("R-{#####}", 3)
    assert Document.objects.get(doc_type="quote", status="draft").lines.get().unit_price == D("15.00")


def test_second_import_adds_nothing(company):
    import_papierkram(company, export())
    again = import_papierkram(company, export())
    assert again.ok and not again.created and again.skipped["Rechnungen"] == 2
    assert Document.objects.count() == 4 and BankTransaction.objects.count() == 2


def test_any_problem_aborts_everything(company):
    broken = dict(FILES)
    broken["Rechnungen.csv"] = FILES["Rechnungen.csv"].replace("1.200,00;0,00;1.200,00", "1.200,00;0,00;9.999,00")
    report = import_papierkram(company, export(broken))
    assert not report.ok and "Brutto" in report.errors[0] and not Customer.objects.exists()


def test_vat_splits_and_unknown_rates():
    assert lines_for(D("100"), D("19"), Regime.STANDARD) == [(D("100"), TaxTreatment.STANDARD)]
    split = lines_for(D("200"), D("26"), Regime.STANDARD)
    assert split == [(D("100.00"), TaxTreatment.STANDARD), (D("100.00"), TaxTreatment.REDUCED)]
    assert lines_for(D("100"), D("5"), Regime.STANDARD) is None


@pytest.mark.parametrize("data", [b"kein zip", export({"Fremd.csv": "a;b\n1;2\n"})])
def test_garbage_is_refused(company, data):
    with pytest.raises(PapierkramError):
        import_papierkram(company, data)


def test_imported_invoices_cannot_be_sent(company, settings):
    settings.MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    import_papierkram(company, export())
    with pytest.raises(MailError, match="Altbelege"):
        send_document(Document.objects.get(number="R-00001"), to=["a@example.org"], subject="x", body="y")


def test_view_checks_then_imports(client, django_user_model, company):
    client.force_login(django_user_model.objects.create_user("admin", is_staff=True))
    url = "/einstellungen/umzug/papierkram/"
    upload = SimpleUploadedFile("export.zip", export(), content_type="application/zip")
    page = client.post(url, {"export": upload, "aktion": "pruefen", "nummern": "1"})
    assert "Noch nichts gespeichert" in page.content.decode() and not Document.objects.exists()
    upload = SimpleUploadedFile("export.zip", export(), content_type="application/zip")
    page = client.post(url, {"export": upload, "aktion": "importieren", "nummern": "1"})
    assert "Übernommen" in page.content.decode() and Document.objects.filter(imported_from="Papierkram").count() == 3
    client.force_login(django_user_model.objects.create_user("mitarbeit"))
    assert client.get(url).status_code == 403
