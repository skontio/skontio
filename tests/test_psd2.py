"""Kontoabruf über Enable Banking: verbinden, abrufen ohne Dubletten, ablaufen, trennen – nie Müll übernehmen."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.utils import timezone
from joserfc import jwt
from joserfc.jwk import RSAKey

from skontio.banking import psd2
from skontio.banking.enablebanking import Client, EnableBankingError
from skontio.banking.models import BankAccount, BankConnection, BankTransaction
from skontio.banking.services import BankingError
from skontio.core.models import JobRun
from skontio.ops import scheduler
from tests.test_banking import invoice

D = Decimal
PEM = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    .decode()
)
CONFIG = {"app_id": "app-123", "key_file": "", "api": "https://api.example.test", "every_hours": 6, "days": 180}


def booked(ref, amount, indicator, purpose, day="2026-10-05", name="Kunde GmbH", status="BOOK"):
    other = "debtor" if indicator == "CRDT" else "creditor"
    return {
        "entry_reference": ref, "transaction_amount": {"amount": amount, "currency": "EUR"},
        "credit_debit_indicator": indicator, "status": status, "booking_date": day,
        other: {"name": name}, f"{other}_account": {"iban": "DE89 3704 0044 0532 0130 00"},
        "remittance_information": [purpose],
    }  # fmt: skip


class FakeBank:
    """Enable Banking as a MockTransport: records requests, serves configurable transactions."""

    def __init__(self, transactions=None):
        self.requests, self.transactions = [], transactions or []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        token = request.headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(token, RSAKey.import_key(PEM)).claims
        assert claims["aud"] == "api.enablebanking.com" and claims["iss"] == "enablebanking.com"
        path = request.url.path
        if path == "/aspsps":
            return httpx.Response(200, json={"aspsps": [{"name": "Sparkasse Musterstadt", "country": "DE"},
                                                         {"name": "N26", "country": "DE"}, {"bad": 1}]})  # fmt: skip
        if path == "/auth":
            body = json.loads(request.content)
            assert body["redirect_url"].endswith("/bank/verbinden/rueckkehr/") and body["state"]
            return httpx.Response(200, json={"url": "https://bank.example.test/login?x=1"})
        if path == "/sessions":
            return httpx.Response(200, json={
                "session_id": "sess-geheim",
                "accounts": [{"uid": "acc-1", "account_id": {"iban": "DE02120300000000202051"}, "name": "Giro"},
                             {"uid": "acc-2", "account_id": {"iban": "DE11520513735120710131"}, "name": "Tagesgeld"}],
                "access": {"valid_until": "2027-04-01T00:00:00+00:00"},
            })  # fmt: skip
        if path.startswith("/accounts/acc-1/transactions"):
            if request.url.params.get("continuation_key") == "seite2":
                return httpx.Response(200, json={"transactions": self.transactions[1:]})
            return httpx.Response(200, json={"transactions": self.transactions[:1], "continuation_key": "seite2"})
        if path.startswith("/accounts/acc-2/transactions"):
            return httpx.Response(200, json={"transactions": []})
        if path.startswith("/sessions/") and request.method == "DELETE":
            return httpx.Response(200, json={})
        return httpx.Response(404, text="unknown")


def api(fake):
    return Client(CONFIG, http=httpx.Client(transport=httpx.MockTransport(fake)), key_pem=PEM)


@pytest.fixture
def eb(settings):
    settings.ENABLE_BANKING = CONFIG
    settings.PUBLIC_URL = "https://skontio.example.org"
    return settings


def connect(shop, fake):
    psd2.start_connection(shop.company, bank_name="Sparkasse Musterstadt", country="DE", business=False,
                          user=shop.user, api=api(fake))  # fmt: skip
    state = BankConnection.objects.get().state
    return psd2.finish_connection(shop.company, state=state, code="code-1", user=shop.user, api=api(fake))


def test_connect_links_accounts_seals_the_session_and_fetches(shop, eb):
    shop.company.auto_match_payments = True
    shop.company.save()
    inv = invoice(shop)
    fake = FakeBank([booked("r1", "95.20", "CRDT", f"Rechnung {inv.number}"), booked("r2", "11.90", "DBIT", "Server")])
    connection = connect(shop, fake)
    assert connection.status == BankConnection.Status.ACTIVE and "sess-geheim" not in connection.session
    giro = BankAccount.objects.get(iban="DE02120300000000202051")
    assert giro.connection == connection and giro.provider_uid == "acc-1"
    assert BankAccount.objects.filter(company=shop.company).count() == 2
    rows = BankTransaction.objects.order_by("amount")
    assert [(r.amount, r.source) for r in rows] == [(D("-11.90"), "psd2"), (D("95.20"), "psd2")]
    assert rows[0].counterparty == "Kunde GmbH" and rows[1].iban == "DE89370400440532013000"
    assert rows[1].auto_matched  # the fetch runs the automatic matching


def test_refetch_adds_nothing_twice_and_skips_pending_and_garbage(shop, eb):
    fake = FakeBank([booked("r1", "10.00", "CRDT", "a"), booked("r2", "5.00", "DBIT", "b")])
    connection = connect(shop, fake)
    fake.transactions += [
        booked("r3", "7.00", "CRDT", "neu"), booked("r4", "1.00", "CRDT", "vorgemerkt", status="PDNG"),
        booked("r5", "NaN", "CRDT", "kaputt"), booked("r6", "1e99", "CRDT", "riesig"),
        booked("r7", "3.00", "CRDT", "falsches Datum", day="gestern"), "kein Objekt",
    ]  # fmt: skip
    assert psd2.sync_connection(connection, api=api(fake)) == 1
    assert BankTransaction.objects.count() == 3


def test_wrong_state_or_reused_code_is_refused(shop, eb):
    fake = FakeBank()
    with pytest.raises(BankingError):
        psd2.finish_connection(shop.company, state="erfunden", code="x", user=shop.user, api=api(fake))
    connection = connect(shop, fake)
    with pytest.raises(BankingError):  # the same redirect a second time
        psd2.finish_connection(shop.company, state=connection.state, code="code-1", user=shop.user, api=api(fake))


def test_expired_consent_stops_fetching_and_reminds(shop, eb):
    connection = connect(shop, FakeBank())
    connection.valid_until = timezone.now() + dt.timedelta(days=3)
    connection.save()
    assert psd2.expiring(shop.company) == [connection]
    assert psd2.sync_connection(connection, api=api(FakeBank()), now=timezone.now() + dt.timedelta(days=4)) == 0
    connection.refresh_from_db()
    assert connection.status == BankConnection.Status.EXPIRED


def test_provider_errors_are_reported_not_swallowed(shop, eb):
    connection = connect(shop, FakeBank())
    broken = api(lambda request: httpx.Response(500, text="kaputt"))
    with pytest.raises(BankingError):
        psd2.sync_connection(connection, api=broken)
    connection.refresh_from_db()
    assert "500" in connection.last_error
    with pytest.raises(EnableBankingError):
        Client(CONFIG, key_pem="kein Schlüssel")
    with pytest.raises(EnableBankingError):
        api(lambda request: httpx.Response(200, json={"url": "javascript:alert(1)"})).start(
            psd2.Bank("X", "DE"), state="s", redirect_url="https://x", valid_until=timezone.now(), business=False
        )


def test_disconnect_forgets_the_session_and_keeps_data(shop, eb):
    fake = FakeBank([booked("r1", "10.00", "CRDT", "a")])
    connection = connect(shop, fake)
    psd2.disconnect(connection, user=shop.user, api=api(fake))
    connection.refresh_from_db()
    assert connection.status == BankConnection.Status.REVOKED and connection.session == ""
    assert BankTransaction.objects.count() == 1 and fake.requests[-1].method == "DELETE"


def test_worker_fetches_every_six_hours(shop, eb, monkeypatch):
    fake = FakeBank([booked("r1", "10.00", "CRDT", "a")])
    connect(shop, fake)
    monkeypatch.setattr(psd2, "client", lambda: api(fake))
    now = timezone.now()
    assert scheduler.fetch_banks(now).status == JobRun.Status.OK
    assert scheduler.fetch_banks(now + dt.timedelta(hours=2)) is None
    assert scheduler.fetch_banks(now + dt.timedelta(hours=7)) is not None


def test_views_connect_flow_and_permissions(client, shop, eb, monkeypatch, django_user_model):
    fake = FakeBank([booked("r1", "10.00", "CRDT", "a")])
    monkeypatch.setattr(psd2, "client", lambda: api(fake))
    client.force_login(shop.user)
    page = client.get("/bank/verbinden/?q=spark").content.decode()
    assert "Sparkasse Musterstadt" in page and "N26" not in page
    page = client.post("/bank/verbinden/", {"bank": "Sparkasse Musterstadt", "land": "DE"}).content.decode()
    assert 'href="https://bank.example.test/login?x=1"' in page
    state = BankConnection.objects.get().state
    response = client.get(f"/bank/verbinden/rueckkehr/?state={state}&code=abc")
    assert response.status_code == 302 and BankConnection.objects.get().status == "active"
    assert "Sparkasse Musterstadt" in client.get("/bank/").content.decode()
    client.force_login(django_user_model.objects.create_user("mitarbeit"))
    assert client.get("/bank/verbinden/").status_code == 403


def test_bank_denies_access(client, shop, eb, monkeypatch):
    monkeypatch.setattr(psd2, "client", lambda: api(FakeBank()))
    client.force_login(shop.user)
    client.post("/bank/verbinden/", {"bank": "N26", "land": "DE"})
    state = BankConnection.objects.get().state
    client.get(f"/bank/verbinden/rueckkehr/?state={state}&error=access_denied")
    assert BankConnection.objects.get().status == "failed"


def test_without_configuration_explains_setup(client, shop, settings):
    settings.ENABLE_BANKING = None
    client.force_login(shop.user)
    assert "noch nicht eingerichtet" in client.get("/bank/verbinden/").content.decode()
    assert scheduler.fetch_banks() is None


def test_lines_from_an_earlier_file_import_are_not_doubled(shop, eb):
    giro = BankAccount.objects.create(company=shop.company, name="Giro", iban="DE02120300000000202051")
    BankTransaction.objects.create(company=shop.company, account=giro, booked_on=dt.date(2026, 10, 5),
                                   amount=D("10.00"), purpose="aus CSV", import_key="csv-1", source="csv")  # fmt: skip
    fake = FakeBank([booked("r1", "10.00", "CRDT", "a"), booked("r2", "10.00", "CRDT", "zweite gleiche")])
    connect(shop, fake)
    assert BankTransaction.objects.filter(account=giro).count() == 2  # the CSV line + the second one
