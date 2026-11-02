"""Bank-Regeln: schlagen nur vor, passen genau, leere Regeln verboten, Anwenden nur für „ignorieren“."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth.models import Group

from skontio.accounts.roles import DRAFTS_ONLY_GROUP
from skontio.banking.models import BankRule, BankTransaction
from skontio.banking.services import BankingError, apply_ignore_rules, create_rule, rule_for
from skontio.core.models import Company


def tx(company, amount, counterparty="", purpose="", key=None):
    return BankTransaction.objects.create(
        company=company, booked_on="2026-10-01", amount=Decimal(amount), counterparty=counterparty,
        purpose=purpose, import_key=key or f"{counterparty}{purpose}{amount}", source="csv",
    )  # fmt: skip


def test_matching_by_text_direction_and_case(shop):
    hosting = create_rule(shop.company, counterparty_contains="hosting", direction="out", action="expense",
                          category="software")  # fmt: skip
    private = create_rule(shop.company, purpose_contains="Umbuchung", direction="any", action="ignore")
    rules = [hosting, private]
    assert rule_for(tx(shop.company, "-9.99", "HOSTING AG"), rules) == hosting
    assert rule_for(tx(shop.company, "9.99", "Hosting AG"), rules) is None  # incoming, rule is for outgoing
    assert rule_for(tx(shop.company, "500", purpose="Umbuchung Tagesgeld"), rules) == private
    assert rule_for(tx(shop.company, "-1", "Bäcker"), rules) is None


def test_both_texts_must_match_when_both_are_set(shop):
    rule = create_rule(shop.company, counterparty_contains="Telekom", purpose_contains="Mobil", action="expense",
                       category="telecom")  # fmt: skip
    assert rule.matches(tx(shop.company, "-40", "Telekom", "Mobilfunk Oktober"))
    assert not rule.matches(tx(shop.company, "-40", "Telekom", "Festnetz"))


@pytest.mark.parametrize(
    "fields",
    [
        {"counterparty_contains": "", "purpose_contains": "  ", "action": "ignore"},  # would match everything
        {"counterparty_contains": "X", "action": "expense", "category": ""},
        {"counterparty_contains": "X", "action": "expense", "category": "casino"},
        {"counterparty_contains": "X", "action": "expense", "category": "software", "direction": "in"},
        {"counterparty_contains": "X", "action": "delete_everything"},
        {"counterparty_contains": "X", "action": "ignore", "direction": "sideways"},
        {"counterparty_contains": "X", "company": None},
    ],
)
def test_bad_rules_are_refused(shop, fields):
    with pytest.raises((BankingError, TypeError)):
        create_rule(shop.company, **fields)
    assert not BankRule.objects.exists()


def test_apply_only_ignores_and_only_open_transactions(shop):
    create_rule(shop.company, purpose_contains="Privatentnahme", direction="any", action="ignore")
    create_rule(shop.company, counterparty_contains="Hosting", action="expense", category="software")
    private = tx(shop.company, "-100", purpose="Privatentnahme")
    hosting = tx(shop.company, "-9.99", "Hosting AG")
    assert apply_ignore_rules(shop.company) == 1
    private.refresh_from_db()
    hosting.refresh_from_db()
    assert private.ignored and not hosting.ignored and hosting.expense_id is None
    assert apply_ignore_rules(shop.company) == 0  # already done, nothing flips back


def test_rules_never_act_on_another_company(shop):
    other = Company.objects.create(name="Fremd", street="X", postcode="1", city="Y")
    create_rule(other, purpose_contains="Privat", direction="any", action="ignore")
    mine = tx(shop.company, "-5", purpose="Privat")
    assert apply_ignore_rules(other) == 0
    mine.refresh_from_db()
    assert not mine.ignored


# ---- Oberfläche --------------------------------------------------------------------------------


def test_suggestion_and_prefilled_expense(client, shop):
    rule = create_rule(shop.company, counterparty_contains="Hosting", action="expense", category="software",
                       supplier="Hosting AG (Rechnungsname)")  # fmt: skip
    line = tx(shop.company, "-9.99", "HOSTING AG MUENCHEN", "Abo")
    client.force_login(shop.user)
    page = client.get("/bank/").content.decode()
    assert f"regel={rule.pk}" in page and "Software, Lizenzen, Hosting" in page
    form = client.get("/ausgaben/neu/", {"bank": line.pk, "regel": rule.pk}).content.decode()
    assert 'value="software" selected' in form and "Hosting AG (Rechnungsname)" in form


def test_rule_that_does_not_match_does_not_prefill(client, shop):
    rule = create_rule(shop.company, counterparty_contains="Telekom", action="expense", category="telecom")
    line = tx(shop.company, "-9.99", "Hosting AG")
    client.force_login(shop.user)
    form = client.get("/ausgaben/neu/", {"bank": line.pk, "regel": rule.pk}).content.decode()
    assert 'value="telecom" selected' not in form


def test_create_apply_and_delete_through_the_ui(client, shop):
    client.force_login(shop.user)
    client.post("/bank/regeln/", {"purpose_contains": "Umbuchung", "direction": "any", "action": "ignore"})
    client.post("/bank/regeln/", {"purpose_contains": "", "counterparty_contains": "", "direction": "any",
                                  "action": "ignore"})  # fmt: skip
    rule = BankRule.objects.get()
    line = tx(shop.company, "200", purpose="Umbuchung")
    client.post("/bank/regeln/anwenden/")
    line.refresh_from_db()
    assert line.ignored
    client.post(f"/bank/regeln/{rule.pk}/loeschen/")
    assert not BankRule.objects.exists()


@pytest.mark.parametrize("regel", ["abc", "-1", "99999999999999999999", ""])
def test_garbage_rule_parameter_is_ignored(client, shop, regel):
    line = tx(shop.company, "-1", "X")
    client.force_login(shop.user)
    assert client.get("/ausgaben/neu/", {"bank": line.pk, "regel": regel}).status_code == 200


def test_foreign_rule_cannot_be_deleted_or_used(client, shop):
    other = Company.objects.create(name="Fremd", street="X", postcode="1", city="Y")
    foreign = create_rule(other, counterparty_contains="X", action="expense", category="rent")
    line = tx(shop.company, "-1", "X")
    client.force_login(shop.user)
    assert client.post(f"/bank/regeln/{foreign.pk}/loeschen/").status_code == 404
    form = client.get("/ausgaben/neu/", {"bank": line.pk, "regel": foreign.pk}).content.decode()
    assert 'value="rent" selected' not in form and BankRule.objects.filter(pk=foreign.pk).exists()


def test_drafts_only_role_cannot_manage_rules(client, shop, django_user_model):
    helper = django_user_model.objects.create_user("hilfe")
    helper.groups.add(Group.objects.get_or_create(name=DRAFTS_ONLY_GROUP)[0])
    client.force_login(helper)
    data = {"purpose_contains": "X", "direction": "any", "action": "ignore"}
    assert client.post("/bank/regeln/", data).status_code == 403
    assert client.post("/bank/regeln/anwenden/").status_code == 403
    assert not BankRule.objects.exists()
