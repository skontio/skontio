"""Mehrere Bankkonten, eigene IBAN aus der Datei, Suche und Filter in den Umsätzen."""

from __future__ import annotations

import importlib
from decimal import Decimal

import pytest
from django.apps import apps
from django.contrib.auth.models import Group

from skontio.accounts.roles import DRAFTS_ONLY_GROUP
from skontio.banking.models import BankAccount, BankTransaction
from skontio.banking.parsers import own_account
from skontio.banking.services import BankingError, import_statement
from skontio.core.models import Company

GIRO = "DE02120300000000202051"
SPAR = "DE89370400440532013000"


def camt(iban: str, amount: str = "95.20", who: str = "Kunde GmbH", purpose: str = "Rechnung RE-1") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"><BkToCstmrStmt><Stmt>
  <Acct><Id><IBAN>{iban}</IBAN></Id></Acct>
  <Ntry><Amt Ccy="EUR">{amount}</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-10-01</Dt></BookgDt>
    <NtryDtls><TxDtls><RltdPties><Dbtr><Nm>{who}</Nm></Dbtr></RltdPties>
    <RmtInf><Ustrd>{purpose}</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
</Stmt></BkToCstmrStmt></Document>""".encode()


CSV_PLAIN = "Buchungstag;Betrag;Verwendungszweck;Beguenstigter/Zahlungspflichtiger\n01.10.2026;-12,50;Porto;Post\n"


def test_own_iban_is_read_from_camt_mt940_and_csv():
    assert own_account(camt(GIRO)) == GIRO
    assert own_account(f":20:X\n:25:{SPAR}\n:61:260302C1,00NTRF\n".encode()) == SPAR
    csv = f"Auftragskonto;Buchungstag;Betrag\n{GIRO};01.10.2026;1,00\n".encode()
    assert own_account(csv) == GIRO
    assert own_account(b":20:X\n:25:10020030/1234567890\n:61:260302C1,00NTRF\n") == ""  # BLZ/Konto, no IBAN
    assert own_account(b"\x00\xff garbage") == ""


def test_accounts_are_created_from_the_file_iban(shop):
    import_statement(shop.company, camt(GIRO))
    import_statement(shop.company, camt(SPAR, amount="5.00"))
    accounts = {a.iban: a for a in BankAccount.objects.filter(company=shop.company)}
    assert set(accounts) == {GIRO, SPAR}
    assert BankTransaction.objects.get(amount=Decimal("5.00")).account == accounts[SPAR]


def test_file_without_iban_uses_the_only_account_or_asks(shop):
    import_statement(shop.company, CSV_PLAIN.encode())
    assert BankTransaction.objects.get().account.iban == GIRO  # company IBAN from the fixture
    BankAccount.objects.create(company=shop.company, name="Kreditkarte")
    with pytest.raises(BankingError, match="Konto auswählen"):
        import_statement(shop.company, CSV_PLAIN.replace("Porto", "Briefe").encode())
    card = BankAccount.objects.get(name="Kreditkarte")
    import_statement(shop.company, CSV_PLAIN.replace("Porto", "Briefe").encode(), account=card)
    assert BankTransaction.objects.get(purpose="Briefe").account == card


def test_chosen_account_must_match_the_file(shop):
    giro = BankAccount.objects.create(company=shop.company, name="Giro", iban=GIRO)
    with pytest.raises(BankingError, match="gehört zum Konto"):
        import_statement(shop.company, camt(SPAR), account=giro)
    assert not BankTransaction.objects.exists()


def test_foreign_account_is_refused(shop):
    other = Company.objects.create(name="Fremd", street="X", postcode="1", city="Y")
    foreign = BankAccount.objects.create(company=other, name="Fremd")
    with pytest.raises(BankingError, match="Unbekanntes Konto"):
        import_statement(shop.company, CSV_PLAIN.encode(), account=foreign)


def test_migration_puts_old_transactions_on_one_account(shop):
    BankTransaction.objects.create(company=shop.company, booked_on="2026-01-01", amount=1, import_key="a", source="csv")
    migration = importlib.import_module("skontio.banking.migrations.0002_bank_accounts")
    migration._existing_transactions_to_one_account(apps, None)
    assert BankTransaction.objects.get().account.name == "Girokonto"


# ---- Liste: Suche und Filter -------------------------------------------------------------------


@pytest.fixture
def statement(client, shop):
    import_statement(shop.company, camt(GIRO, who="Kunde GmbH", purpose="Rechnung RE-1"))
    import_statement(shop.company, camt(SPAR, amount="49.90", who="Hosting AG", purpose="Abo Oktober"))
    client.force_login(shop.user)
    return shop


def test_search_by_counterparty_purpose_iban_and_amount(client, statement):
    assert "Hosting AG" in client.get("/bank/", {"q": "abo okt"}).content.decode()
    page = client.get("/bank/", {"q": "kunde"}).content.decode()
    assert "Kunde GmbH" in page and "Hosting AG" not in page
    page = client.get("/bank/", {"q": "49,90"}).content.decode()
    assert "Hosting AG" in page and "Kunde GmbH" not in page


def test_filter_by_account_and_status(client, statement):
    spar = BankAccount.objects.get(iban=SPAR)
    page = client.get("/bank/", {"konto": spar.pk}).content.decode()
    assert "Hosting AG" in page and "Kunde GmbH" not in page
    tx = BankTransaction.objects.get(counterparty="Hosting AG")
    client.post(f"/bank/{tx.pk}/ignorieren/")
    assert "Hosting AG" not in client.get("/bank/").content.decode()
    assert "Hosting AG" in client.get("/bank/", {"status": "erledigt"}).content.decode()
    assert "Hosting AG" in client.get("/bank/", {"alle": "1"}).content.decode()


@pytest.mark.parametrize(
    "params",
    [{"q": "%"}, {"q": "1e999"}, {"q": "NaN"}, {"konto": "99999999999999999999"}, {"konto": "-1"},
     {"status": "<script>"}, {"von": "2026-02-30"}, {"q": "A" * 5000}],
)  # fmt: skip
def test_garbage_filters_never_crash(client, statement, params):
    assert client.get("/bank/", params).status_code == 200


def test_foreign_account_filter_shows_everything_of_mine_only(client, statement):
    other = Company.objects.create(name="Fremd", street="X", postcode="1", city="Y")
    foreign = BankAccount.objects.create(company=other, name="Fremd", iban="DE75512108001245126199")
    page = client.get("/bank/", {"konto": foreign.pk}).content.decode()
    assert "Kunde GmbH" in page and "DE75512108001245126199" not in page


# ---- Konten anlegen ----------------------------------------------------------------------------


def test_create_account_validates_iban_and_duplicates(client, shop):
    client.force_login(shop.user)
    client.post("/bank/konten/", {"name": "Tagesgeld", "iban": "de89 3704 0044 0532 0130 00"})
    assert BankAccount.objects.get(name="Tagesgeld").iban == SPAR
    client.post("/bank/konten/", {"name": "Doppelt", "iban": SPAR})
    client.post("/bank/konten/", {"name": "Kaputt", "iban": "DE00123456781234567890"})
    client.post("/bank/konten/", {"name": "", "iban": ""})
    assert list(BankAccount.objects.values_list("name", flat=True)) == ["Tagesgeld"]


def test_drafts_only_role_cannot_touch_the_bank(client, shop, django_user_model):
    helper = django_user_model.objects.create_user("hilfe")
    helper.groups.add(Group.objects.get_or_create(name=DRAFTS_ONLY_GROUP)[0])
    client.force_login(helper)
    assert client.post("/bank/konten/", {"name": "X"}).status_code == 403
    assert client.get("/bank/").status_code == 403
    assert not BankAccount.objects.exists()
