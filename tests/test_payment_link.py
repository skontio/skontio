"""Zahlungslink: nur gültige https-Links, Platzhalter, auf der Rechnung und in der Mail, nie bei Angeboten."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from skontio.documents.models import Document
from skontio.documents.renditions import render_document_html
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.mail.services import default_message
from skontio.payments.link import build, validate_template
from skontio.payments.services import record_payment
from skontio.web.forms import CompanyForm

DAY = dt.date(2026, 3, 2)
LINK = "https://paypal.me/muster/{betrag}EUR"


def make(shop, doc_type=Document.Type.INVOICE):
    draft = create_draft(shop.company, doc_type, shop.org, issue_date=DAY, service_date=DAY)
    add_line_from_item(draft, shop.item, quantity=Decimal("1"))
    return finalize(draft)


@pytest.mark.parametrize(
    "template",
    [LINK, "https://pay.example.org/r?nr={nummer}&a={betrag}", "https://paypal.me/muster", ""],
)
def test_valid_templates(template):
    validate_template(template)


@pytest.mark.parametrize(
    "template",
    [
        "javascript:alert(1)",
        "http://paypal.me/muster",
        "data:text/html,<script>",
        "https://",
        "https://localhost/{betrag}",
        "https://user:pass@paypal.me/x",
        'https://paypal.me/x"><script>',
        "https://paypal.me/x {betrag}",
        "https://paypal.me/{amount}",
        "https://paypal.me/{betrag",
        "https://paypal.me/\x00",
        "https://paypal.me/" + "a" * 300,
        "https://[::1/",
    ],
)
def test_invalid_templates_are_refused(template):
    with pytest.raises(ValidationError):
        validate_template(template)


def test_build_fills_amount_and_encodes_number():
    assert build(LINK, amount=Decimal("119"), number="RE-1") == "https://paypal.me/muster/119.00EUR"
    url = build("https://x.example/?n={nummer}", amount=Decimal("1"), number="RE 1/&x")
    assert url == "https://x.example/?n=RE%201%2F%26x"
    assert build("javascript:alert(1)", amount=Decimal("1"), number="x") == ""


def test_invoice_pdf_and_mail_carry_the_link(shop):
    shop.company.payment_link = LINK
    shop.company.save()
    invoice = make(shop)
    expected = f"https://paypal.me/muster/{invoice.gross_total:.2f}EUR"
    assert f'href="{expected}"' in render_document_html(invoice)
    assert f"Online bezahlen: {expected}" in default_message(invoice)[1]


def test_no_link_without_setting_for_quotes_or_when_paid(shop):
    assert "Online bezahlen" not in render_document_html(make(shop))
    shop.company.payment_link = LINK
    shop.company.save()
    assert "paypal.me" not in render_document_html(make(shop, Document.Type.QUOTE))
    invoice = make(shop)
    record_payment(invoice, invoice.gross_total, DAY)
    assert "paypal.me" not in default_message(invoice)[1]


def test_english_invoice_uses_english_label(shop):
    shop.company.payment_link = LINK
    shop.company.save()
    shop.org.language = "en"
    shop.org.save()
    assert "Pay online" in render_document_html(make(shop))


def test_company_form_rejects_hostile_links(shop):
    data = {f: getattr(shop.company, f) for f in CompanyForm.Meta.fields}
    data["payment_link"] = "javascript:alert(document.cookie)"
    form = CompanyForm(data, instance=shop.company)
    assert not form.is_valid() and "payment_link" in form.errors
