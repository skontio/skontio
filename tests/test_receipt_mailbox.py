"""Beleg-Postfach: Anhänge landen im Belegeingang oder bei den Eingangsrechnungen, nie doppelt, nie Müll."""

from __future__ import annotations

import datetime as dt
import imaplib
from email.message import EmailMessage

import pytest
from django.utils import timezone

from skontio.core.doctor import check_imap
from skontio.core.models import JobRun
from skontio.einvoice.cii import Profile, build_cii
from skontio.expenses import mailbox
from skontio.expenses.models import PendingReceipt, ReceiptMail
from skontio.inbox.models import IncomingInvoice
from skontio.ops import scheduler
from tests.test_inbox import supplier_invoice

PDF = b"%PDF-1.4\n" + b"x" * 2000 + b"\n%%EOF"
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 80_000
LOGO = b"\x89PNG\r\n\x1a\n" + b"p" * 3000
CONFIG = {
    "host": "imap.example.org", "port": 993, "security": "ssl", "user": "belege@example.org", "password": "x",
    "folder": "INBOX", "done_folder": "", "company": None, "allowed_senders": [], "every_minutes": 15,
}  # fmt: skip


def mail(*attachments, sender="lieferant@example.org", subject="Rechnung 4711", message_id="<1@example.org>",
         inline=()) -> bytes:  # fmt: skip
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, "belege@example.org", subject
    if message_id:
        message["Message-ID"] = message_id
    message.set_content("Anbei die Rechnung.")
    message.add_alternative("<p>Anbei die Rechnung.</p>", subtype="html")
    for data, maintype, subtype, filename in inline:
        html = message.get_payload()[1]
        html.add_related(data, maintype, subtype, cid="<logo>", filename=filename, disposition="inline")
    for data, maintype, subtype, filename in attachments:
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes()


class FakeImap:
    """Just enough IMAP for ``poll``: UIDs map to raw mails; records flags and moves."""

    def __init__(self, mails: dict[bytes, bytes], *, move=True):
        self.mails, self.flags, self.moved, self.logged_out = mails, {}, [], False
        self.capabilities = ("IMAP4REV1", "MOVE") if move else ("IMAP4REV1",)

    def select(self, folder, readonly=False):
        return ("OK", [b"1"]) if folder in ('"INBOX"', '"Verarbeitet"') else ("NO", [b"no such folder"])

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b" ".join(uid for uid in self.mails if "\\Seen" not in self.flags.get(uid, ""))]
        if command == "FETCH":
            uid, what = args
            raw = self.mails[uid]
            if what == "(RFC822.SIZE)":
                return "OK", [f"1 (UID {uid.decode()} RFC822.SIZE {len(raw)})".encode()]
            return "OK", [(b"1 (UID 1 BODY[] {%d}" % len(raw), raw), b")"]
        if command == "STORE":
            self.flags[args[0]] = self.flags.get(args[0], "") + args[2]
            return "OK", [b""]
        if command in ("MOVE", "COPY"):
            self.moved.append((args[0], args[1]))
            return "OK", [b""]
        raise AssertionError(command)

    def logout(self):
        self.logged_out = True


def test_parse_skips_signature_logos_and_follows_forwarded_mails():
    forwarded = EmailMessage()
    forwarded["From"], forwarded["Subject"] = "shop@example.org", "Ihre Rechnung"
    forwarded.set_content("Original")
    forwarded.add_attachment(PDF, maintype="application", subtype="pdf", filename="original.pdf")
    raw = mail(
        (b"PK\x03\x04zip", "application", "zip", "x.zip"),
        (JPEG, "image", "jpeg", "kassenbon.jpg"),
        inline=[(LOGO, "image", "png", "logo.png")],
    )
    outer = EmailMessage()
    outer["From"], outer["Message-ID"] = "ich@example.org", "<fwd@x>"
    outer.set_content("siehe Anhang")
    outer.add_attachment(forwarded)
    assert [name for name, _ in mailbox.parse(raw).files] == ["kassenbon.jpg"]
    assert [name for name, _ in mailbox.parse(outer.as_bytes()).files] == ["original.pdf"]


def test_pdf_and_photo_land_in_the_receipt_inbox_with_a_note(shop):
    record = mailbox.import_mail(
        shop.company, mail((PDF, "application", "pdf", "rechnung.pdf"), (JPEG, "image", "jpeg", "bon.jpg"))
    )
    assert record.result == "2 Beleg(e), 0 E-Rechnung(en)"
    pending = PendingReceipt.objects.filter(company=shop.company)
    assert sorted(p.filename for p in pending) == ["bon.jpg", "rechnung.pdf"]
    assert pending[0].note == "Per E-Mail von lieferant@example.org: Rechnung 4711"


def test_e_invoices_become_incoming_invoices_and_other_xml_is_ignored(shop):
    xml = build_cii(supplier_invoice(), Profile.XRECHNUNG)
    raw = mail((xml, "application", "xml", "xrechnung.xml"), (b"<?xml version='1.0'?><a/>", "text", "xml", "x.xml"))
    record = mailbox.import_mail(shop.company, raw)
    assert record.result == "0 Beleg(e), 1 E-Rechnung(en), 1 übersprungen"
    assert IncomingInvoice.objects.filter(company=shop.company).count() == 1
    assert not PendingReceipt.objects.exists()


def test_the_same_mail_is_imported_once(shop):
    raw = mail((PDF, "application", "pdf", "r.pdf"))
    assert mailbox.import_mail(shop.company, raw) is not None
    assert mailbox.import_mail(shop.company, raw) is None
    without_id = mail((PDF, "application", "pdf", "r.pdf"), message_id="")
    assert mailbox.import_mail(shop.company, without_id) is not None
    assert mailbox.import_mail(shop.company, without_id) is None
    assert PendingReceipt.objects.count() == 2


def test_sender_allowlist(shop):
    assert mailbox.sender_allowed("a@firma.de", ["firma.de"]) and mailbox.sender_allowed("x@y.de", [])
    assert not mailbox.sender_allowed("a@evilfirma.de", ["firma.de"])
    assert not mailbox.sender_allowed("b@firma.de", ["a@firma.de"])
    record = mailbox.import_mail(shop.company, mail((PDF, "application", "pdf", "r.pdf")), allowed_senders=["ich.de"])
    assert "nicht zugelassen" in record.result and not PendingReceipt.objects.exists()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x00\xff" * 500,
        b"From: =?utf-8?q?=FF?=\r\nSubject: =?x?b?###?=\r\n\r\nhi",
        b"Content-Type: multipart/mixed\r\n\r\n",
    ],
)
def test_garbage_mails_do_not_crash(shop, raw):
    record = mailbox.import_mail(shop.company, raw)
    assert record is not None and not PendingReceipt.objects.exists()


def test_poll_marks_read_moves_and_flags_broken_mails(shop, monkeypatch):
    good = mail((PDF, "application", "pdf", "r.pdf"))
    client = FakeImap({b"1": good, b"2": b"broken", b"3": good})
    original = mailbox.import_mail

    def import_or_fail(company, raw, **kwargs):
        if raw == b"broken":
            raise RuntimeError("kaputt")
        return original(company, raw, **kwargs)

    monkeypatch.setattr(mailbox, "import_mail", import_or_fail)
    summary = mailbox.poll({**CONFIG, "done_folder": "Verarbeitet"}, client=client)
    assert summary == "1 Mail(s) übernommen, 1 schon bekannt, 1 fehlgeschlagen (im Postfach markiert)"
    assert client.flags == {b"1": "(\\Seen)", b"2": "(\\Seen \\Flagged)", b"3": "(\\Seen)"}
    assert client.moved == [(b"1", '"Verarbeitet"'), (b"3", '"Verarbeitet"')] and client.logged_out


def test_poll_skips_huge_mails_and_needs_a_clear_company(shop, monkeypatch):
    monkeypatch.setattr(mailbox, "MAX_MAIL_BYTES", 100)
    client = FakeImap({b"7": mail((PDF, "application", "pdf", "r.pdf"))})
    assert "1 fehlgeschlagen" in mailbox.poll(CONFIG, client=client)
    assert not PendingReceipt.objects.exists()
    with pytest.raises(mailbox.MailboxError):
        mailbox.poll({**CONFIG, "company": 999_999}, client=FakeImap({}))


def test_worker_polls_every_few_minutes_and_reports(shop, settings, monkeypatch):
    settings.RECEIPT_MAILBOX = CONFIG
    calls = []
    monkeypatch.setattr(mailbox, "poll", lambda config: calls.append(config) or "0 Mail(s) übernommen")
    now = timezone.now()
    assert scheduler.poll_mailbox(now).status == JobRun.Status.OK
    assert scheduler.poll_mailbox(now + dt.timedelta(minutes=5)) is None
    assert scheduler.poll_mailbox(now + dt.timedelta(minutes=16)) is not None
    assert len(calls) == 2
    assert any(job.name == "receipt_mail" for job, _ in scheduler.status())

    def unreachable(config):
        raise OSError("Verbindung abgelehnt")

    monkeypatch.setattr(mailbox, "poll", unreachable)
    row = scheduler.poll_mailbox(now + dt.timedelta(minutes=40))
    assert row.status == JobRun.Status.FAILED and "Verbindung abgelehnt" in row.message


def test_worker_without_mailbox_does_nothing(settings):
    settings.RECEIPT_MAILBOX = None
    assert scheduler.poll_mailbox() is None


def test_doctor_imap():
    assert not check_imap(None, connect=None)[0].ok

    def refused(config):
        raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    assert check_imap(CONFIG, connect=refused)[0].name == "login"
    checks = check_imap({**CONFIG, "done_folder": "Fehlt"}, connect=lambda config: FakeImap({}))
    assert [(c.name, c.ok) for c in checks] == [("login", True), ("folder", True), ("done_folder", False)]


def test_receipt_inbox_shows_address_and_received_mails(client, shop, settings):
    settings.RECEIPT_MAILBOX_ADDRESS = "belege@example.org"
    mailbox.import_mail(shop.company, mail((PDF, "application", "pdf", "r.pdf"), subject="<b>Juni</b>"))
    client.force_login(shop.user)
    page = client.get("/ausgaben/belege/").content.decode()
    assert "belege@example.org" in page and "&lt;b&gt;Juni&lt;/b&gt;" in page and "1 Beleg(e)" in page
    assert ReceiptMail.objects.count() == 1
