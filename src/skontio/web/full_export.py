"""Gesamtexport: all business data as CSV plus the archived files in one ZIP.

Meant for people (Steuerberatung, a move to other software), not for restoring – that is the
backup's job. Archive files are checked against their hash; a missing or altered file is listed
in FEHLER.txt instead of breaking the export.
"""

from __future__ import annotations

import csv
import io
import re
import tempfile
import zipfile

from django.utils import timezone

from skontio.archive.services import ArchiveCorrupted, load
from skontio.attachments.models import Attachment
from skontio.banking.models import BankTransaction
from skontio.documents.models import Document, DocumentLine, Rendition
from skontio.dunning.models import DunningNotice
from skontio.expenses.models import Expense
from skontio.payments.models import Payment
from skontio.web import transfer
from skontio.web.csvexport import cell

SUFFIX = {
    Rendition.Kind.PDF: ".pdf",
    Rendition.Kind.ZUGFERD_PDF: "-zugferd.pdf",
    Rendition.Kind.XRECHNUNG_XML: "-xrechnung.xml",
}
README = """Skontio – Gesamtexport vom {date}

CSV-Dateien: Semikolon, UTF-8, Beträge mit Dezimalkomma, Datum als JJJJ-MM-TT.
Texte, die mit = + - @ beginnen, haben ein vorangestelltes ' (Schutz vor Formeln in Tabellenprogrammen).

kontakte.csv, leistungen.csv  – lassen sich in Skontio wieder importieren
dokumente.csv, positionen.csv – Rechnungen, Angebote, Stornos usw. mit ihren Positionen
zahlungen.csv                 – Zahlungseingänge (negative Beträge = Rückbuchung)
ausgaben.csv, bankumsaetze.csv, mahnungen.csv
dokumente/, belege/, mahnungen/, dateien/ – die archivierten Originale (PDF/A, E-Rechnung, Belege)

Das ist keine Datensicherung: Zum Wiederherstellen dient das nächtliche Backup.
"""


def _safe(name: str) -> str:
    """A flat, harmless file name inside the ZIP (no folders, no traversal, no control characters)."""
    return re.sub(r"[^\w.\-]+", "_", name).strip("._")[:120] or "datei"


def _csv(header: list[str], rows) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([cell(v) for v in row])
    return ("﻿" + buffer.getvalue()).encode("utf-8")


class _Zip:
    def __init__(self, zf: zipfile.ZipFile):
        self.zf, self.used, self.problems, self.files = zf, set(), [], 0

    def blob(self, folder: str, name: str, blob) -> None:
        stem, dot, ext = _safe(name).rpartition(".")
        if not dot:
            stem, ext = ext, ""
        path, n = f"{folder}/{stem}{dot}{ext}", 1
        while path in self.used:
            n += 1
            path = f"{folder}/{stem}-{n}{dot}{ext}"
        self.used.add(path)
        try:
            self.zf.writestr(path, load(blob))
            self.files += 1
        except ArchiveCorrupted as exc:
            self.problems.append(f"{path}: {exc}")


def build(company, out) -> dict:
    """Write the ZIP to the binary file ``out``; returns counts for the audit log."""
    documents = Document.objects.filter(company=company).select_related("customer", "parent")
    expenses = Expense.objects.filter(company=company).select_related("receipt").order_by("receipt_date", "pk")
    notices = DunningNotice.objects.filter(company=company).select_related("invoice", "blob").order_by("pk")
    lines = DocumentLine.objects.filter(document__company=company).select_related("document")
    payments = Payment.objects.filter(company=company).select_related("document").order_by("received_on", "pk")
    bank = BankTransaction.objects.filter(company=company).select_related("payment__document", "expense")
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        z = _Zip(zf)
        zf.writestr("LIESMICH.txt", README.format(date=timezone.localdate().strftime("%d.%m.%Y")))
        zf.writestr("kontakte.csv", _csv(transfer.CUSTOMER_COLUMNS, transfer.customer_rows(company)))
        zf.writestr("leistungen.csv", _csv(transfer.ITEM_COLUMNS, transfer.item_rows(company)))
        zf.writestr("dokumente.csv", _csv(
            ["Nummer", "Art", "Status", "Kunde", "Kundennummer", "Datum", "Leistungsdatum", "Fällig", "Netto", "USt",
             "Brutto", "Bezug", "Sprache"],
            ([d.number, d.get_doc_type_display(), d.get_status_display(), d.customer.name if d.customer else "",
              d.customer.number if d.customer else "", d.issue_date, d.service_date, d.due_date, d.net_total,
              d.tax_total, d.gross_total, d.parent.number if d.parent else "", d.language]
             for d in documents.order_by("issue_date", "pk").iterator()),
        ))  # fmt: skip
        zf.writestr("positionen.csv", _csv(
            ["Dokument", "Pos.", "Text", "Beschreibung", "Menge", "Einheit", "Einzelpreis", "Rabatt %", "USt %",
             "Netto"],
            ([ln.document.number or f"Entwurf {ln.document_id}", ln.position, ln.text, ln.description, ln.quantity,
              ln.unit_code, ln.unit_price, ln.discount_percent, ln.tax_rate, ln.net_amount]
             for ln in lines.order_by("document_id", "position").iterator()),
        ))  # fmt: skip
        zf.writestr("zahlungen.csv", _csv(
            ["Datum", "Rechnung", "Betrag", "Zahlart", "Notiz"],
            ([p.received_on, p.document.number, p.amount, p.get_method_display(), p.note]
             for p in payments.iterator()),
        ))  # fmt: skip
        zf.writestr("ausgaben.csv", _csv(
            ["Beleg", "Belegdatum", "Bezahlt am", "Lieferant", "Land", "Beschreibung", "Kategorie", "Brutto", "USt %",
             "Zahlart", "Storniert", "Belegdatei"],
            ([e.number, e.receipt_date, e.paid_on, e.supplier, e.supplier_country, e.description,
              e.get_category_display(), e.gross_amount, e.vat_rate, e.get_method_display(),
              e.cancelled_at.date() if e.cancelled_at else "", e.receipt_filename]
             for e in expenses.iterator()),
        ))  # fmt: skip
        zf.writestr("bankumsaetze.csv", _csv(
            ["Buchungstag", "Betrag", "Währung", "Gegenpartei", "IBAN", "Verwendungszweck", "Rechnung", "Ausgabe",
             "Ignoriert"],
            ([t.booked_on, t.amount, t.currency, t.counterparty, t.iban, t.purpose,
              t.payment.document.number if t.payment else "", t.expense.number if t.expense else "",
              "ja" if t.ignored else ""]
             for t in bank.order_by("booked_on", "pk").iterator()),
        ))  # fmt: skip
        zf.writestr("mahnungen.csv", _csv(
            ["Nummer", "Rechnung", "Stufe", "Titel", "Datum", "Zahlbar bis", "Offen", "Gebühr", "Versendet"],
            ([n.number, n.invoice.number, n.level, n.title, n.issued_on, n.pay_by, n.open_amount, n.fee,
              n.sent_at.date() if n.sent_at else ""] for n in notices),
        ))  # fmt: skip
        for r in Rendition.objects.filter(document__company=company).select_related("document", "blob"):
            z.blob("dokumente", (r.document.number or f"dokument-{r.document_id}") + SUFFIX.get(r.kind, ".bin"), r.blob)
        for e in expenses.exclude(receipt=None):
            z.blob("belege", f"{e.number}-{e.receipt_filename or 'beleg'}", e.receipt)
        for n in notices:
            z.blob("mahnungen", f"{n.number}.pdf", n.blob)
        for a in Attachment.objects.filter(company=company).select_related("blob"):
            z.blob("dateien", f"{a.pk}-{a.filename}", a.blob)
        if z.problems:
            zf.writestr("FEHLER.txt", "Diese Archivdateien fehlen oder wurden verändert:\n" + "\n".join(z.problems))
    return {"files": z.files, "problems": len(z.problems)}


def build_tempfile(company):
    """The ZIP in a temporary file (rewound), so large archives never sit in memory."""
    out = tempfile.TemporaryFile()
    counts = build(company, out)
    out.seek(0)
    return out, counts
