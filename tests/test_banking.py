"""Bankumsätze: CAMT.053 und CSV einlesen, ohne Duplikate, Vorschläge und Zuordnung mit einem Klick."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from skontio.banking.models import BankTransaction
from skontio.banking.parsers import StatementError, parse_amount, parse_camt053, parse_csv
from skontio.banking.services import BankingError, book_payment, import_statement, link_expense, suggestions
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.expenses.models import Expense
from skontio.expenses.services import create_expense
from skontio.payments.services import paid_amount

D = Decimal

CAMT = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"><BkToCstmrStmt><Stmt>
  <Ntry><Amt Ccy="EUR">95.20</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-10-01</Dt></BookgDt>
    <NtryDtls><TxDtls><RltdPties><Dbtr><Pty><Nm>Kunde GmbH</Nm></Pty></Dbtr>
      <DbtrAcct><Id><IBAN>DE89370400440532013000</IBAN></Id></DbtrAcct></RltdPties>
      <RmtInf><Ustrd>Rechnung {number}</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
  <Ntry><Amt Ccy="EUR">11.90</Amt><CdtDbtInd>DBIT</CdtDbtInd><BookgDt><Dt>2026-10-02</Dt></BookgDt>
    <NtryDtls><TxDtls><RltdPties><Cdtr><Pty><Nm>Hosting AG</Nm></Pty></Cdtr></RltdPties>
      <RmtInf><Ustrd>Server Oktober</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
</Stmt></BkToCstmrStmt></Document>"""

N26 = (
    '"Date","Payee","Account number","Transaction type","Payment reference","Amount (EUR)"\n'
    '"2026-10-03","Kunde GmbH","DE89370400440532013000","Credit Transfer","Danke","95.20"\n'
    '"2026-10-04","Cafe","","MasterCard Payment","","-4.50"\n'
)
SPARKASSE = (
    "Umsatzanzeige;Konto DE02120300000000202051\n"
    "\n"
    "Buchungstag;Valutadatum;Beguenstigter/Zahlungspflichtiger;Verwendungszweck;Soll;Haben;Waehrung\n"
    "05.10.2026;05.10.2026;Bürobedarf GmbH;Papier;23,80;;EUR\n"
    "06.10.2026;06.10.2026;Kunde GmbH;Zahlung;;1.234,56;EUR\n"
)


def camt(number: str = "x") -> bytes:
    return CAMT.replace(b"{number}", number.encode())


def invoice(shop):
    doc = create_draft(
        shop.company,
        Document.Type.INVOICE,
        shop.org,
        issue_date=dt.date(2026, 9, 20),
        service_date=dt.date(2026, 9, 20),
    )
    add_line_from_item(doc, shop.item, quantity=D("1"))
    return finalize(doc)  # 80 € + 19 % = 95,20 €


class TestParsers:
    def test_camt053(self):
        rows = parse_camt053(camt("RE-2026-0001"))
        assert [(r.booked_on, r.amount, r.counterparty) for r in rows] == [
            (dt.date(2026, 10, 1), D("95.20"), "Kunde GmbH"),
            (dt.date(2026, 10, 2), D("-11.90"), "Hosting AG"),
        ]
        assert rows[0].iban == "DE89370400440532013000" and rows[0].purpose == "Rechnung RE-2026-0001"

    def test_external_entities_are_not_resolved(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("geheim")
        doctype = f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file://{secret}">]>'.encode()
        evil = CAMT.replace(b'<?xml version="1.0" encoding="UTF-8"?>', doctype).replace(b"{number}", b"&x;")
        rows = parse_camt053(evil)
        assert "geheim" not in rows[0].purpose

    def test_n26_csv(self):
        rows = parse_csv(N26.encode())
        assert [(r.amount, r.counterparty) for r in rows] == [(D("95.20"), "Kunde GmbH"), (D("-4.50"), "Cafe")]

    def test_sparkasse_csv_with_preamble_and_debit_credit_columns(self):
        rows = parse_csv(SPARKASSE.encode("cp1252"))
        assert [(r.booked_on, r.amount, r.counterparty) for r in rows] == [
            (dt.date(2026, 10, 5), D("-23.80"), "Bürobedarf GmbH"),
            (dt.date(2026, 10, 6), D("1234.56"), "Kunde GmbH"),
        ]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("1.234,56", "1234.56"), ("-1.234,56", "-1234.56"), ("1,234.56", "1234.56"), ("12,50-", "-12.50"), ("", "0")],
    )
    def test_amounts(self, text, expected):
        assert parse_amount(text) == D(expected)

    def test_unknown_csv(self):
        with pytest.raises(StatementError, match="Spalten"):
            parse_csv(b"a;b;c\n1;2;3\n")


class TestImport:
    def test_reimport_skips_known_lines_but_keeps_identical_twins(self, shop):
        twins = N26 + '"2026-10-04","Cafe","","MasterCard Payment","","-4.50"\n'
        assert import_statement(shop.company, twins.encode()) == (3, 0)
        assert import_statement(shop.company, twins.encode()) == (0, 3)
        assert BankTransaction.objects.filter(amount=D("-4.50")).count() == 2

    def test_errors(self, shop):
        with pytest.raises(BankingError, match="Spalten"):
            import_statement(shop.company, b"nonsense")


class TestMatching:
    def test_invoice_number_in_purpose(self, shop):
        doc = invoice(shop)
        import_statement(shop.company, camt(doc.number))
        tx = BankTransaction.objects.get(amount=D("95.20"))
        (best,) = suggestions(tx)
        assert best.target == doc and "Rechnungsnummer" in best.reason
        book_payment(tx, doc, user=shop.user)
        assert paid_amount(doc) == D("95.20") and tx.payment.received_on == dt.date(2026, 10, 1)
        assert suggestions(tx) == []
        with pytest.raises(BankingError, match="bereits"):
            book_payment(tx, doc)

    def test_amount_only_and_overpayment_refused(self, shop):
        doc = invoice(shop)
        import_statement(shop.company, N26.encode())
        tx = BankTransaction.objects.get(amount=D("95.20"))
        assert [s.target for s in suggestions(tx)] == [doc]
        big = BankTransaction.objects.create(
            company=shop.company, booked_on=dt.date(2026, 10, 9), amount=D("500"), import_key="x", source="csv"
        )
        assert suggestions(big) == []

    def test_outgoing_matches_unpaid_expense(self, shop):
        expense = create_expense(
            shop.company,
            receipt_date=dt.date(2026, 10, 1),
            supplier="Hosting AG",
            gross_amount=D("11.90"),
            vat_rate=D("19"),
        )
        import_statement(shop.company, camt())
        tx = BankTransaction.objects.get(amount=D("-11.90"))
        (best,) = suggestions(tx)
        assert best.target == expense
        link_expense(tx, expense, user=shop.user)
        expense.refresh_from_db()
        assert expense.paid_on == dt.date(2026, 10, 2)


class TestWeb:
    def test_import_match_and_ignore(self, client, shop):
        doc = invoice(shop)
        client.force_login(shop.user)
        client.post("/bank/import/", {"statement": SimpleUploadedFile("auszug.xml", camt(doc.number))})
        page = client.get("/bank/").content.decode()
        assert "2 Umsätze ohne Zuordnung" in page and doc.number in page and "Ausgabe anlegen" in page
        tx = BankTransaction.objects.get(amount=D("95.20"))
        client.post(f"/bank/{tx.pk}/zuordnen/", {"invoice": doc.pk})
        assert paid_amount(doc) == D("95.20")
        out = BankTransaction.objects.get(amount=D("-11.90"))
        client.post(f"/bank/{out.pk}/ignorieren/")
        out.refresh_from_db()
        assert out.ignored and "Keine offenen Umsätze" in client.get("/bank/").content.decode()

    def test_create_expense_from_transaction(self, client, shop):
        import_statement(shop.company, camt())
        tx = BankTransaction.objects.get(amount=D("-11.90"))
        client.force_login(shop.user)
        form = client.get(f"/ausgaben/neu/?bank={tx.pk}").content.decode()
        assert 'value="Hosting AG"' in form and "11,90" in form
        client.post(
            "/ausgaben/neu/",
            {
                "bank": tx.pk,
                "receipt_date": "2026-10-02",
                "supplier": "Hosting AG",
                "gross_amount": "11,90",
                "vat_rate": "19",
                "category": "software",
                "paid_on": "2026-10-02",
                "method": "bank",
                "supplier_country": "DE",
            },
        )
        tx.refresh_from_db()
        assert tx.expense == Expense.objects.get()

    def test_other_company_is_hidden(self, client, shop):
        from skontio.core.models import Company

        other = Company.objects.create(name="Fremd")
        tx = BankTransaction.objects.create(
            company=other, booked_on=dt.date(2026, 10, 1), amount=D("1"), import_key="y", source="csv"
        )
        client.force_login(shop.user)
        assert client.post(f"/bank/{tx.pk}/ignorieren/").status_code == 404
