"""Nummernkreise in der Oberfläche: Startnummer setzen ja, Duplikate und Rückschritte nie."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from skontio.audit.models import AuditEvent
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.numbering.models import NumberSequence
from skontio.numbering.services import NumberingError, change_problems, pattern_regex, update_sequence

TODAY = dt.date.today()
YEAR = TODAY.year


def invoice(shop):
    draft = create_draft(shop.company, Document.Type.INVOICE, shop.org, issue_date=TODAY, service_date=TODAY)
    add_line_from_item(draft, shop.item, quantity=Decimal("1"))
    return finalize(draft)


def save(client, kind, pattern, next_value, reset="yearly"):
    return client.post(
        "/einstellungen/nummernkreise/", {"kind": kind, "pattern": pattern, "next_value": next_value, "reset": reset}
    )


@pytest.fixture
def admin(client, shop):
    shop.user.is_staff = True
    shop.user.save()
    client.force_login(shop.user)
    return client


class TestStartNumber:
    def test_start_number_before_the_first_invoice_is_kept(self, admin, shop):
        save(admin, "invoice", "RE-{YYYY}-{####}", 1001)
        assert invoice(shop).number == f"RE-{YEAR}-1001"
        assert invoice(shop).number == f"RE-{YEAR}-1002"

    def test_start_number_may_still_be_lowered_before_anything_was_issued(self, admin, shop):
        save(admin, "invoice", "RE-{YYYY}-{####}", 1001)
        save(admin, "invoice", "RE-{YYYY}-{####}", 500)
        assert invoice(shop).number == f"RE-{YEAR}-0500"

    def test_own_pattern_with_month(self, admin, shop):
        save(admin, "invoice", "{YY}{MM}-{###}", 1)
        assert invoice(shop).number == f"{YEAR % 100:02d}{TODAY.month:02d}-001"

    def test_page_shows_preview_and_every_kind(self, admin, shop):
        page = admin.get("/einstellungen/nummernkreise/").content.decode()
        for label in ("Rechnungen", "Stornorechnungen", "Angebote", "Kundennummern", "Ausgaben", "Mahnungen"):
            assert label in page
        assert f"RE-{YEAR}-0001" in page

    def test_changes_are_audited(self, admin, shop):
        save(admin, "invoice", "RE-{YYYY}-{####}", 50)
        event = AuditEvent.objects.get(action="numbering.changed")
        assert event.data["after"]["next_value"] == 50 and event.data["before"]["next_value"] == 1


class TestNoDuplicatesNoGoingBack:
    def test_next_number_cannot_go_back_once_numbers_were_issued(self, admin, shop):
        invoice(shop), invoice(shop)
        response = save(admin, "invoice", "RE-{YYYY}-{####}", 1)
        assert "nicht unter 3 sinken" in response.content.decode()
        assert invoice(shop).number == f"RE-{YEAR}-0003"

    def test_pattern_change_that_would_repeat_an_existing_number_is_refused(self, admin, shop):
        invoice(shop)  # RE-YYYY-0001
        save(admin, "invoice", "R{YYYY}-{##}", 1)  # different pattern: no collision possible
        assert invoice(shop).number == f"R{YEAR}-01"
        response = save(admin, "invoice", "RE-{YYYY}-{####}", 1)  # back to the old pattern, counter 1 again
        assert "gibt es schon" in response.content.decode()

    def test_yearly_reset_needs_a_year_in_the_pattern(self, admin, shop):
        response = save(admin, "invoice", "RE-{####}", 1, reset="yearly")
        assert "{YYYY} oder {YY}" in response.content.decode()
        assert not NumberSequence.objects.filter(doc_type="invoice", pattern="RE-{####}").exists()

    def test_without_year_and_without_reset_is_fine(self, admin, shop):
        save(admin, "invoice", "RE-{######}", 7, reset="never")
        assert invoice(shop).number == "RE-000007"

    @pytest.mark.parametrize(
        ("pattern", "next_value", "message"),
        [
            ("RE-{YYYY}", "1", "genau einen Zähler"),
            ("RE-{#}-{#}", "1", "genau einen Zähler"),
            ("RE-{JAHR}-{###}", "1", "unbekannte Platzhalter"),
            ("RE-{YYYY}-{###}", "0", "zwischen 1 und"),
            ("RE-{YYYY}-{###}", "-5", "zwischen 1 und"),
            ("RE-{YYYY}-{###}", "99999999999", "zwischen 1 und"),
            ("RE-{YYYY}-{###}", "1e3", "ganze Zahl"),
            ("RE-{YYYY}-{###}", "NaN", "ganze Zahl"),
            ("K-{YYYY}-" + "X" * 40 + "{#}", "1", "höchstens"),
        ],
    )
    def test_invalid_input_is_refused_with_a_reason(self, admin, shop, pattern, next_value, message):
        kind = "customer" if pattern.startswith("K-") else "invoice"
        response = save(admin, kind, pattern, next_value)
        assert response.status_code == 200 and message in response.content.decode()
        assert not AuditEvent.objects.filter(action="numbering.changed").exists()

    def test_unknown_kind_and_markup_are_ignored(self, admin, shop):
        response = save(admin, "<script>", "<script>{#}", 1)
        assert response.status_code == 200 and "<script>{#}" not in response.content.decode()
        assert not NumberSequence.objects.filter(pattern__contains="script").exists()

    def test_only_admins(self, client, shop, django_user_model):
        client.force_login(django_user_model.objects.create_user("mitarbeit"))
        assert save(client, "invoice", "X-{YYYY}-{#}", 1).status_code == 403
        assert not NumberSequence.objects.filter(pattern="X-{YYYY}-{#}").exists()


class TestServiceRules:
    def test_regex_matches_what_the_pattern_produces(self):
        regex = pattern_regex("RE-{YYYY}-{MM}-{####}")
        match = regex.match("RE-2026-03-0042")
        assert match and match.group("year") == "2026" and int(match.group("n")) == 42
        assert not regex.match("RE-2026-03-42")  # fewer digits than the pattern ever produces
        assert not regex.match("XRE-2026-03-0042")

    def test_numbers_of_an_earlier_year_do_not_block_a_yearly_range(self, shop):
        seq = NumberSequence(company=shop.company, doc_type="invoice", pattern="RE-{YYYY}-{####}")
        existing = [f"RE-{YEAR - 1}-0005"]
        assert (
            change_problems(seq, pattern=seq.pattern, reset="yearly", next_value=1, existing=existing, today=TODAY)
            == []
        )

    def test_never_resetting_ranges_check_all_years(self, shop):
        seq = NumberSequence(company=shop.company, doc_type="customer", pattern="K-{#####}", reset="never")
        problems = change_problems(
            seq, pattern="K-{#####}", reset="never", next_value=3, existing=["K-00005"], today=TODAY
        )
        assert problems and "K-00005" in problems[0]

    def test_service_refuses_without_saving(self, shop):
        seq = NumberSequence.objects.create(company=shop.company, doc_type="invoice", pattern="RE-{YYYY}-{####}")
        with pytest.raises(NumberingError):
            update_sequence(seq, pattern="RE-{####}", reset="yearly", next_value=1, existing=[], today=TODAY)
        seq.refresh_from_db()
        assert seq.pattern == "RE-{YYYY}-{####}"
