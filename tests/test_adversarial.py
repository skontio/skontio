"""Negativtests: Müll, Grenzwerte und Angriffe dürfen weder abstürzen noch Daten verfälschen.

Jede URL wird ohne Login und mit Login mit unsinnigen Eingaben aufgerufen. Danach müssen
festgeschriebene Rechnungen unverändert und die Audit-Kette intakt sein.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import socket
import time
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import URLResolver, get_resolver
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from lxml import etree

from skontio.accounts import totp, twofactor
from skontio.accounts.roles import DRAFTS_ONLY_GROUP
from skontio.archive.services import load
from skontio.attachments.models import Attachment
from skontio.audit.services import verify_chain
from skontio.banking.parsers import StatementError, parse_statement
from skontio.documents.models import Document, DocumentLine, Rendition
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.einvoice.cii import Profile, build_cii
from skontio.einvoice.mapping import invoice_from_document
from skontio.einvoice.reader import EInvoiceParseError, parse_einvoice
from skontio.payments.models import Payment
from skontio.payments.services import record_payment
from skontio.tools.registry import ToolError, call_tool

# Routes that may answer without a login; everything else redirects to the login or refuses.
PUBLIC = re.compile(r"^(\.well-known/|oauth/|auth/|mcp$|manifest\.webmanifest$|sw\.js$)")
NASTY = [
    "NaN",
    "Infinity",
    "-Infinity",
    "1e999",
    "1" * 40,
    "-1",
    "0,00000001",
    "2026-02-30",
    "<script>alert(1)</script>",
    "'; DROP TABLE documents_document; --",
    "../../../../etc/passwd",
    "A" * 20_000,
    "‮﻿Ä😀",
]
FIELDS = (
    "name street postcode city country email iban bic tax_number vat_id customer item doc_type issue_date "
    "service_date service_period_start service_period_end due_date valid_until document_discount_percent "
    "intro_text closing_text language template text quantity unit_code unit_price discount_percent "
    "tax_treatment amount received_on method note to cc subject body reason percent net_amount kind sku "
    "description unit net_price title due_on regime regime_since since interval start_date code "
    "partial base_url model api_key provider level days fee confirm next"
).split()
PASSWORD = "richtig-und-lang-genug"


def _routes() -> list[str]:
    def walk(resolver, prefix=""):
        for pattern in resolver.url_patterns:
            if isinstance(pattern, URLResolver):
                yield from walk(pattern, prefix + str(pattern.pattern))
            else:
                yield prefix + str(pattern.pattern)

    return [route for route in walk(get_resolver()) if not route.startswith("admin/")]


def _fill(route: str, ids: list[int]) -> list[str]:
    """Concrete URLs for a route pattern, once per candidate id; the last one also tries a traversal."""
    if "<" not in route:
        return ["/" + route]
    urls = []
    for value in ids:
        url = re.sub(r"<int:[a-z_]+>", str(value), route)
        url = url.replace("<str:kind>", "..%2F..%2Fetc%2Fpasswd" if value == ids[-1] else "pdf")
        url = re.sub(r"<(slug|str|path):[a-z_]+>", "x", url)
        urls.append("/" + url)
    return urls


@pytest.fixture
def no_network(monkeypatch):
    """Outgoing connections fail like an unreachable host; views must handle that, not crash."""

    def refuse(*args, **kwargs):
        raise OSError("Netzwerk im Test gesperrt")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture
def world(shop):
    """A finalized, partly paid invoice next to a draft – the sweep must not touch the former."""
    shop.user.is_superuser = True
    shop.user.set_password(PASSWORD)
    shop.user.save()
    today = dt.date.today()
    invoice = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=today, service_date=today)
    add_line_from_item(invoice, shop.item, quantity=Decimal("2"))
    invoice = finalize(invoice)
    record_payment(invoice, Decimal("10"), today)
    draft = create_draft(shop.company, Document.Type.INVOICE, shop.person, issue_date=today, service_date=today)
    line = add_line_from_item(draft, shop.item, quantity=Decimal("1"))
    shop.invoice, shop.draft, shop.line = invoice, draft, line
    return shop


def _handled(url: str, status: int) -> bool:
    """No server error – except the deliberate 503 when the identity provider is unreachable."""
    return status < 500 or (status == 503 and url.startswith("/auth/login"))


def _snapshot(document: Document) -> tuple:
    document.refresh_from_db()
    lines = tuple(document.lines.order_by("pk").values_list("text", "quantity", "unit_price", "discount_percent"))
    payments = tuple(document.payments.order_by("pk").values_list("amount", "received_on"))
    return document.number, document.status, document.locked_at, document.issue_date, lines, payments


def _assert_sane_amounts():
    for quantity, price, discount in DocumentLine.objects.values_list("quantity", "unit_price", "discount_percent"):
        for value in (quantity, price):
            assert value is None or (value.is_finite() and abs(value) < Decimal("1e12")), value
        assert discount is None or Decimal(0) <= discount <= Decimal(100), discount


class TestWithoutLogin:
    def test_no_private_page_answers_without_login(self, client, world, no_network):
        ids = [world.invoice.pk, world.draft.pk, 999_999]
        leaks = []
        for route in _routes():
            if PUBLIC.match(route):
                continue
            for url in _fill(route, ids):
                for method in ("get", "post"):
                    response = getattr(client, method)(url)
                    status = response.status_code
                    location = response.get("Location", "")
                    if status in (301, 302) and location.startswith(("/auth/", "/einrichtung/")):
                        continue
                    if status in (401, 403, 404, 405):
                        continue
                    leaks.append(f"{method.upper()} {url} → {status} {location}")
        assert leaks == [], "\n".join(leaks)

    def test_public_routes_do_not_crash_on_garbage(self, client, world, no_network):
        for route in _routes():
            if not PUBLIC.match(route):
                continue
            for url in _fill(route, [1, 999_999]):
                for response in (
                    client.get(url, {"next": "https://evil.example/", "code": "x", "state": "x"}),
                    client.post(url, dict.fromkeys(FIELDS, NASTY[8])),
                ):
                    assert _handled(url, response.status_code), (url, response.status_code)

    def test_no_open_redirect_via_next(self, client, world):
        response = client.post("/auth/local/?next=https://evil.example/", {"username": "x", "password": "y"})
        assert "evil.example" not in response.get("Location", "")
        response = client.get("/auth/login", {"next": "//evil.example/"})
        assert not response.get("Location", "").startswith("//evil.example")


class TestGarbageSweep:
    """Every URL, logged in, with absurd input: no 500, no change to finalized documents."""

    def test_every_route_survives_garbage(self, client, world, no_network, mailoutbox):
        client.force_login(world.user)
        before = _snapshot(world.invoice)
        ids = [world.invoice.pk, world.draft.pk, world.line.pk, 999_999]
        payloads = [dict.fromkeys(FIELDS, value) for value in NASTY] + [{}]
        crashes = []
        for route in _routes():
            for url in _fill(route, ids):
                for method, payload in [("get", {})] + [("post", p) for p in payloads]:
                    try:
                        status = getattr(client, method)(url, payload).status_code
                    except Exception as exc:  # noqa: BLE001 – collect every crash, then fail once
                        crashes.append(
                            f"{method.upper()} {url} [{str(payload)[:40]}]: {type(exc).__name__}: {exc}"[:300]
                        )
                        continue
                    if not _handled(url, status):
                        crashes.append(f"{method.upper()} {url} [{str(payload)[:40]}] → {status}")
        assert crashes == [], "\n".join(sorted(set(crashes)))
        assert _snapshot(world.invoice) == before
        assert verify_chain() == []
        _assert_sane_amounts()

    def test_role_drafts_only_cannot_finalize_book_or_send_through_any_route(
        self, client, world, no_network, mailoutbox, django_user_model
    ):
        """Every route, with garbage and with plausible input, as the restricted role."""
        user = django_user_model.objects.create_user("mitarbeit")
        user.groups.add(Group.objects.get_or_create(name=DRAFTS_ONLY_GROUP)[0])
        client.force_login(user)
        finalized = set(Document.objects.filter(status=Document.Status.FINALIZED).values_list("pk", flat=True))
        payments = Payment.objects.count()
        plausible = {
            "to": "a@b.example", "subject": "x", "body": "y", "reason": "weil", "amount": "10",
            "received_on": dt.date.today().isoformat(), "method": "transfer", "invoice": world.invoice.pk,
            "jahr": dt.date.today().year, "zeitraum": "01", "name": "x", "interval": "1",
            "start_date": dt.date.today().isoformat(),
        }  # fmt: skip
        ids = [world.invoice.pk, world.draft.pk, world.line.pk]
        for route in _routes():
            for url in _fill(route, ids):
                for payload in (plausible, dict.fromkeys(FIELDS, NASTY[0]), {}):
                    assert _handled(url, client.post(url, payload).status_code), url
        assert set(Document.objects.filter(status=Document.Status.FINALIZED).values_list("pk", flat=True)) == finalized
        assert Payment.objects.count() == payments
        assert mailoutbox == []
        assert verify_chain() == []

    def test_body_over_the_limit_is_rejected(self, client, world, settings):
        client.force_login(world.user)
        settings.DATA_UPLOAD_MAX_MEMORY_SIZE = 1024
        assert client.post("/kunden/neu/", {"name": "x" * 4096}).status_code == 400

    def test_too_many_fields_are_rejected(self, client, world):
        client.force_login(world.user)
        assert client.post("/kunden/neu/", {f"f{i}": "1" for i in range(5000)}).status_code == 400


class TestNumbers:
    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "sNaN", "1e30", "1" * 30, "-5", "1,2,3"])
    def test_absurd_line_values_never_reach_the_database(self, client, world, value):
        client.force_login(world.user)
        response = client.post(
            f"/dokumente/{world.draft.pk}/positionen/",
            {"text": "X", "quantity": value, "unit_price": value, "tax_treatment": "standard"},
        )
        assert response.status_code < 500
        _assert_sane_amounts()

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "1e30", "-1", "0"])
    def test_absurd_payments_are_refused(self, client, world, value):
        client.force_login(world.user)
        before = world.invoice.payments.count()
        response = client.post(
            f"/dokumente/{world.invoice.pk}/zahlung/",
            {"amount": value, "received_on": dt.date.today().isoformat(), "method": "transfer"},
        )
        assert response.status_code < 500
        assert world.invoice.payments.count() == before

    @pytest.mark.parametrize("value", ["250", "-50", "100.00001"])
    def test_discounts_stay_between_0_and_100_percent(self, client, world, value):
        client.force_login(world.user)
        client.post(
            f"/dokumente/{world.draft.pk}/positionen/{world.line.pk}/",
            {"text": "X", "quantity": "1", "unit_price": "100", "discount_percent": value},
        )
        _assert_sane_amounts()


class TestInjection:
    EVIL = '<script>alert("x")</script><img src=x onerror=alert(1)>'

    def test_markup_in_names_is_escaped_in_html(self, client, world):
        client.force_login(world.user)
        world.org.name = self.EVIL
        world.org.save()
        world.item.name = self.EVIL
        world.item.save()
        for url in ("/kunden/", f"/kunden/{world.org.pk}/", "/leistungen/", "/dokumente/", "/"):
            page = client.get(url).content.decode()
            assert "<script>alert" not in page and "<img src=x" not in page, url

    def test_xml_specials_survive_the_e_invoice_unchanged(self, world):
        evil = 'A&B <Firma> "Ü" ]]> ‮'
        draft = create_draft(
            world.company, Document.Type.INVOICE, world.org, issue_date=dt.date.today(), service_date=dt.date.today()
        )
        add_line_from_item(draft, world.item, quantity=Decimal("1"))
        draft.lines.update(text=evil)
        invoice = finalize(draft)
        xml = build_cii(invoice_from_document(invoice), Profile.EN16931)
        root = etree.fromstring(xml)
        assert evil in [element.text for element in root.iter() if str(element.tag).endswith("}Name")]
        assert parse_einvoice(xml).lines[0].name == evil
        zugferd = Rendition.objects.get(document=invoice, kind=Rendition.Kind.ZUGFERD_PDF)
        assert parse_einvoice(load(zugferd.blob)).lines[0].name == evil

    @pytest.mark.parametrize(
        "value", ["NaN", "Infinity", "-1", "1e30", "10000001", "0.000001", "' OR 1=1 --", None, [], {"x": 1}]
    )
    def test_ai_tools_refuse_absurd_numbers(self, ctx, value):
        line = {"item_id": None, "text": "Workshop", "quantity": value, "unit": "DAY", "unit_price": value}
        args = {
            "customer_number": ctx.company.customers.first().number,
            "idempotency_key": "adv-0001-abcdef",
            "issue_date": dt.date.today().isoformat(),
            "service_date": dt.date.today().isoformat(),
            "lines": [line],
        }
        with pytest.raises(ToolError):
            call_tool(ctx, "invoice_draft_create", args)
        assert not Document.objects.exists()


class TestParsersUnderFuzzing:
    @hyp_settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(st.binary(max_size=4096))
    def test_bank_import_only_raises_its_own_error(self, data):
        try:
            parse_statement(data)
        except StatementError:
            pass

    @hyp_settings(max_examples=200, deadline=None)
    @given(
        st.lists(
            st.tuples(
                st.text(max_size=12), st.text(max_size=12), st.text(alphabet="0123456789,.-+eEnaNIfity ", max_size=20)
            ),
            max_size=20,
        )
    )
    def test_bank_csv_amounts_are_always_finite(self, rows):
        body = "Buchungstag;Verwendungszweck;Betrag\n" + "\n".join(";".join(r) for r in rows)
        try:
            _kind, parsed = parse_statement(body.encode())
        except StatementError:
            return
        for row in parsed:
            assert row.amount.is_finite() and abs(row.amount) < Decimal("1e12")

    @hyp_settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(st.binary(max_size=4096))
    def test_e_invoice_reader_only_raises_its_own_error(self, data):
        try:
            parse_einvoice(data)
        except EInvoiceParseError:
            pass

    @pytest.mark.parametrize(
        "payload",
        [
            b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
            b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;"><!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">]><l>&d;</l>',
            b"<" * 100_000,
            b"<a>" * 50_000,
            b"%PDF-1.7\n" + b"\x00" * 10_000,
            "Ü;1,00\n".encode("utf-16"),
            b'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt><Ntry>'
            b'<Amt Ccy="EUR">NaN</Amt><BookgDt><Dt>2026-01-01</Dt></BookgDt></Ntry></Stmt></BkToCstmrStmt></Document>',
            b'<Document><Stmt><Ntry><Amt Ccy="EUR">1e999</Amt>'
            b"<BookgDt><Dt>2026-13-45</Dt></BookgDt></Ntry></Stmt></Document>",
        ],
        ids=["billion-laughs", "angle-brackets", "deep-nesting", "fake-pdf", "utf16", "camt-nan", "camt-overflow"],
    )
    def test_hostile_documents_are_rejected_cleanly(self, payload):
        for parse, error in ((parse_einvoice, EInvoiceParseError), (parse_statement, StatementError)):
            try:
                parse(payload)
            except error:
                pass


class TestBruteForce:
    def test_password_guessing_is_throttled(self, client, world):
        for _ in range(20):
            client.post("/auth/local/", {"username": world.user.username, "password": "falsch"})
        response = client.post("/auth/local/", {"username": world.user.username, "password": PASSWORD})
        assert response.status_code == 429

    def test_second_factor_guessing_is_throttled_across_sessions(self, client, world):
        secret = twofactor.start_setup(world.user)
        twofactor.confirm_setup(world.user, totp.hotp(secret, totp.current_step(time.time())))
        for _ in range(10):  # a fresh session every time – a per-session counter alone is not enough
            client.cookies.clear()
            client.post("/auth/local/", {"username": world.user.username, "password": PASSWORD})
            for code in ("000000", "111111", "222222"):
                client.post("/auth/local/2fa/", {"code": code})
        client.cookies.clear()
        response = client.post("/auth/local/", {"username": world.user.username, "password": PASSWORD})
        assert response.status_code == 429


class TestUploads:
    @pytest.mark.parametrize(
        ("name", "data"),
        [("rechnung.pdf", b"MZ\x90\x00"), ("leer.pdf", b""), ("archiv.docx", b"PK\x03\x04kaputt")],
        ids=["exe-as-pdf", "empty", "broken-zip"],
    )
    def test_dangerous_files_are_refused(self, client, world, name, data):
        client.force_login(world.user)
        upload = io.BytesIO(data)
        upload.name = name
        response = client.post("/dateien/hochladen/", {"document": world.draft.pk, "file": upload})
        assert response.status_code < 500
        assert not Attachment.objects.exists()

    @pytest.mark.parametrize(
        ("name", "data"),
        [
            ("bild.svg", b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'),
            ("seite.html", b"<html><script>alert(1)</script></html>"),
            ("rechnung.pdf", b"<script>alert(1)</script>"),
        ],
        ids=["svg-script", "html", "html-as-pdf"],
    )
    def test_markup_files_are_only_served_as_inert_downloads(self, client, world, name, data):
        """Text is allowed (notes, XML), but never rendered by the browser: no script can run."""
        client.force_login(world.user)
        upload = io.BytesIO(data)
        upload.name = name
        client.post("/dateien/hochladen/", {"document": world.draft.pk, "file": upload})
        attachment = Attachment.objects.get()
        response = client.get(f"/dateien/{attachment.pk}/")
        assert response["Content-Type"].startswith(("text/plain", "application/xml"))
        assert response["Content-Disposition"].startswith("attachment")
        assert response["X-Content-Type-Options"] == "nosniff"

    def test_traversal_in_filename_is_neutralised(self, client, world):
        client.force_login(world.user)
        upload = io.BytesIO(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF")
        upload.name = "../../../etc/passwd.pdf"
        client.post("/dateien/hochladen/", {"document": world.draft.pk, "file": upload})
        for attachment in Attachment.objects.all():
            assert "/" not in attachment.filename and ".." not in attachment.filename
