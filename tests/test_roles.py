"""Rolle „nur Entwürfe“: anlegen ja, festschreiben, versenden, stornieren und Geld buchen nie."""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group

from skontio.accounts.oidc import OidcForbidden, map_identity
from skontio.accounts.roles import DRAFTS_ONLY_GROUP, drafts_only
from skontio.attachments.models import Attachment
from skontio.banking.models import BankTransaction
from skontio.catalog.models import Item
from skontio.crm.models import Customer
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.expenses.models import Expense
from skontio.expenses.services import create_expense
from skontio.mcp.models import PersonalAccessToken
from skontio.payments.models import Payment
from skontio.recurring.models import RecurringInvoice
from skontio.recurring.services import create_from_invoice

TODAY = dt.date.today()


def claims(*groups: str) -> dict:
    return {"iss": "https://id.example.org", "sub": "mitarbeit-1", "groups": list(groups), "email": "m@example.org"}


class TestMapping:
    def test_drafts_group_alone_grants_restricted_access(self, db):
        user = map_identity(claims("skontio-entwurf"))
        assert drafts_only(user) and not user.is_staff

    def test_drafts_group_wins_over_normal_access(self, db):
        assert drafts_only(map_identity(claims("skontio", "skontio-entwurf")))

    def test_admins_are_never_restricted(self, db):
        user = map_identity(claims("skontio", "skontio-admin", "skontio-entwurf"))
        assert user.is_staff and not drafts_only(user)
        assert not user.groups.filter(name=DRAFTS_ONLY_GROUP).exists()

    def test_restriction_is_lifted_when_the_group_is_removed(self, db):
        map_identity(claims("skontio-entwurf"))
        assert not drafts_only(map_identity(claims("skontio")))

    @pytest.mark.parametrize("groups", [("andere-app",), ()], ids=["fremde-gruppe", "keine-gruppe"])
    def test_without_a_skontio_group_there_is_no_access(self, db, groups):
        with pytest.raises(OidcForbidden):
            map_identity(claims(*groups))


@pytest.fixture
def drafter(shop, django_user_model):
    user = django_user_model.objects.create_user("mitarbeit")
    user.groups.add(Group.objects.get_or_create(name=DRAFTS_ONLY_GROUP)[0])
    return user


@pytest.fixture
def docs(shop):
    draft = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=TODAY, service_date=TODAY)
    add_line_from_item(draft, shop.item, quantity=Decimal("1"))
    invoice = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=TODAY, service_date=TODAY)
    add_line_from_item(invoice, shop.item, quantity=Decimal("2"))
    return draft, finalize(invoice)


class TestDrafterMayCreate:
    def test_customers_items_drafts_lines_and_files(self, client, shop, drafter):
        client.force_login(drafter)
        client.post(
            "/kunden/neu/",
            {
                "kind": "org",
                "name": "Neu GmbH",
                "street": "W 1",
                "postcode": "1",
                "city": "K",
                "country": "DE",
                "language": "de",
                "payment_terms_days": "14",
            },
        )
        assert Customer.objects.filter(name="Neu GmbH").exists()
        client.post(
            "/leistungen/neu/",
            {"kind": "service", "name": "Workshop", "unit": "DAY", "net_price": "600", "tax_treatment": "standard"},
        )
        assert Item.objects.filter(name="Workshop").exists()
        client.post(
            "/dokumente/neu/", {"doc_type": "invoice", "customer": shop.org.pk, "issue_date": TODAY.isoformat()}
        )
        draft = Document.objects.get(status=Document.Status.DRAFT)
        client.post(f"/dokumente/{draft.pk}/positionen/", {"item": shop.item.pk, "quantity": "3"})
        assert draft.lines.count() == 1
        upload = io.BytesIO(b"%PDF-1.4\n%%EOF")
        upload.name = "auftrag.pdf"
        client.post("/dateien/hochladen/", {"document": draft.pk, "file": upload})
        assert Attachment.objects.count() == 1

    def test_pages_offer_no_restricted_actions(self, client, shop, drafter, docs):
        client.force_login(drafter)
        draft, invoice = docs
        page = client.get(f"/dokumente/{draft.pk}/").content.decode()
        assert "/festschreiben/" not in page and "übernimmt die Inhaberin" in page
        page = client.get(f"/dokumente/{invoice.pk}/").content.decode()
        for action in ("/zahlung/", "/senden/", "/stornieren/", "/wiederkehrend/aus-rechnung/"):
            assert action not in page, action
        assert 'href="/bank/"' not in client.get("/").content.decode()


class TestDrafterIsBlocked:
    def test_every_restricted_action_is_refused_and_changes_nothing(self, client, shop, drafter, docs, mailoutbox):
        draft, invoice = docs
        recurring = create_from_invoice(invoice, name="Wartung", interval=1, start_date=TODAY)
        tx = BankTransaction.objects.create(
            company=shop.company,
            booked_on=TODAY,
            amount=Decimal("160"),
            purpose=invoice.number,
            import_key="tx-1",
            source="csv",
        )
        expense = create_expense(
            shop.company,
            receipt_date=TODAY,
            paid_on=TODAY,
            supplier="Laden",
            gross_amount=Decimal("11.90"),
            vat_rate=Decimal("19"),
        )
        upload = io.BytesIO(b"%PDF-1.4\n%%EOF")
        upload.name = "x.pdf"
        client.force_login(shop.user)
        client.post("/dateien/hochladen/", {"document": invoice.pk, "file": upload})
        attachment = Attachment.objects.get()
        client.force_login(drafter)

        refused = [
            (f"/dokumente/{draft.pk}/festschreiben/", {}),
            (f"/dokumente/{draft.pk}/pruefen/", {}),
            (f"/dokumente/{invoice.pk}/senden/", {"to": "a@b.example", "subject": "x", "body": "y"}),
            (f"/dokumente/{invoice.pk}/stornieren/", {"reason": "weil"}),
            (
                f"/dokumente/{invoice.pk}/zahlung/",
                {"amount": "10", "received_on": TODAY.isoformat(), "method": "transfer"},
            ),
            (f"/mahnwesen/rechnung/{invoice.pk}/", {}),
            (
                f"/wiederkehrend/aus-rechnung/{invoice.pk}/",
                {"name": "x", "interval": "1", "start_date": TODAY.isoformat()},
            ),
            (f"/wiederkehrend/{recurring.pk}/umschalten/", {}),
            ("/bank/import/", {}),
            (f"/bank/{tx.pk}/zuordnen/", {"invoice": invoice.pk}),
            (f"/bank/{tx.pk}/ignorieren/", {}),
            (f"/ausgaben/{expense.pk}/stornieren/", {"reason": "doppelt"}),
            ("/auswertungen/ustva/uebermittelt/", {"jahr": TODAY.year, "zeitraum": "01"}),
            (f"/dateien/{attachment.pk}/loeschen/", {}),
        ]
        for url, data in refused:
            assert client.post(url, data).status_code == 403, url
        assert client.get("/bank/").status_code == 403

        draft.refresh_from_db()
        assert draft.status == Document.Status.DRAFT and draft.number is None
        assert not Document.objects.filter(doc_type=Document.Type.CANCELLATION).exists()
        assert not Payment.objects.exists()
        assert mailoutbox == []
        tx.refresh_from_db()
        assert not tx.ignored and tx.payment_id is None
        recurring.refresh_from_db()
        assert recurring.active
        assert RecurringInvoice.objects.count() == 1
        assert Expense.objects.get(pk=expense.pk).cancelled_at is None
        assert Attachment.objects.filter(pk=attachment.pk).exists()

    def test_settings_stay_closed(self, client, shop, drafter):
        client.force_login(drafter)
        for url in (
            "/einrichtung/",
            "/einstellungen/vorlagen/",
            "/einstellungen/steuerstatus/",
            "/einstellungen/ki-zugriff/",
        ):
            assert client.get(url).status_code in (302, 403), url

    def test_no_ai_tokens_for_drafters(self, client, shop, drafter):
        client.force_login(drafter)
        response = client.post("/einstellungen/ki-zugriff/tokens/", {"name": "x", "scopes": ["drafts:write"]})
        assert response.status_code in (302, 403)
        assert not PersonalAccessToken.objects.exists()


class TestFullRolesUnchanged:
    @pytest.mark.parametrize("staff", [False, True], ids=["mitarbeit", "admin"])
    def test_normal_users_and_admins_may_finalize(self, client, shop, docs, django_user_model, staff):
        client.force_login(django_user_model.objects.create_user("voll", is_staff=staff))
        draft, _invoice = docs
        client.post(f"/dokumente/{draft.pk}/festschreiben/")
        draft.refresh_from_db()
        assert draft.status == Document.Status.FINALIZED
