"""Kontoabruf über PSD2 (Enable Banking): connect a bank, fetch booked transactions, disconnect.

The session ID is stored sealed (core.crypto). Transactions land in the same table as file imports;
fetched windows overlap on purpose, the import key keeps every line unique. After a fetch the
unambiguous incoming payments are matched (``auto_match``, only if the company switched it on).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets
from collections import Counter

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from skontio.audit.services import record
from skontio.banking.enablebanking import Bank, Client, EnableBankingError  # noqa: F401 – re-exported
from skontio.banking.models import BankAccount, BankConnection, BankTransaction
from skontio.banking.services import BankingError, auto_match
from skontio.core.crypto import SealError, seal, unseal

log = logging.getLogger("skontio.banking")

SEAL_PURPOSE = "bank-session"
SOURCE = "psd2"
FIRST_FETCH_DAYS = 90
OVERLAP_DAYS = 10
REMIND_DAYS = 14
CALLBACK_PATH = "/bank/verbinden/rueckkehr/"


def configured() -> bool:
    return bool(settings.ENABLE_BANKING)


def client(**kwargs) -> Client:
    if not settings.ENABLE_BANKING:
        raise BankingError("Der Kontoabruf ist nicht eingerichtet (SKONTIO_ENABLEBANKING_APP_ID).")
    try:
        return Client(settings.ENABLE_BANKING, **kwargs)
    except EnableBankingError as exc:
        raise BankingError(str(exc)) from exc


def callback_url() -> str:
    return settings.PUBLIC_URL + CALLBACK_PATH


def start_connection(company, *, bank_name: str, country: str, business: bool, user, api: Client) -> str:
    """Create a pending connection and return the bank's login URL."""
    bank_name, country = " ".join(bank_name.split())[:200], country.strip().upper()[:2]
    if not bank_name or len(country) != 2 or not country.isalpha():
        raise BankingError("Bitte eine Bank auswählen.")
    valid_until = timezone.now() + dt.timedelta(days=settings.ENABLE_BANKING["days"])
    state = secrets.token_urlsafe(32)
    connection = BankConnection.objects.create(
        company=company, bank_name=bank_name, bank_country=country, state=state, created_by=user
    )
    try:
        url = api.start(Bank(bank_name, country), state=state, redirect_url=callback_url(),
                        valid_until=valid_until, business=business)  # fmt: skip
    except EnableBankingError as exc:
        connection.status, connection.last_error = BankConnection.Status.FAILED, str(exc)[:500]
        connection.save(update_fields=["status", "last_error"])
        raise BankingError(str(exc)) from exc
    record("bank.connect_start", actor=user, company=company, entity="bank_connection", entity_id=connection.pk,
           data={"bank": bank_name, "country": country})  # fmt: skip
    return url


def finish_connection(company, *, state: str, code: str, user, api: Client) -> BankConnection:
    """The bank sent the user back: open the session, link the accounts, fetch for the first time."""
    connection = BankConnection.objects.filter(
        company=company, state=state, status=BankConnection.Status.PENDING
    ).first()
    if connection is None or not code:
        raise BankingError("Diese Rückkehr von der Bank passt zu keiner offenen Verbindung.")
    try:
        session = api.session(code)
    except EnableBankingError as exc:
        connection.status, connection.last_error = BankConnection.Status.FAILED, str(exc)[:500]
        connection.save(update_fields=["status", "last_error"])
        raise BankingError(str(exc)) from exc
    with transaction.atomic():
        connection.session = seal(session.session_id, purpose=SEAL_PURPOSE)
        connection.status = BankConnection.Status.ACTIVE
        connection.valid_until = session.valid_until or (
            timezone.now() + dt.timedelta(days=settings.ENABLE_BANKING["days"])
        )
        connection.save(update_fields=["session", "status", "valid_until"])
        for item in session.accounts:
            account = (
                BankAccount.objects.filter(company=company, iban=item.iban).first() if item.iban else None
            ) or BankAccount(company=company, iban=item.iban, name=f"{connection.bank_name} {item.name}"[:100])
            account.connection, account.provider_uid, account.active = connection, item.uid, True
            account.save()
        record("bank.connect", actor=user, company=company, entity="bank_connection", entity_id=connection.pk,
               data={"bank": connection.bank_name, "accounts": len(session.accounts)})  # fmt: skip
    try:
        sync_connection(connection, api=api)
    except BankingError:
        log.warning("Erster Abruf für %s fehlgeschlagen", connection, exc_info=True)
    return connection


def _session_id(connection: BankConnection) -> str:
    try:
        return unseal(connection.session, purpose=SEAL_PURPOSE)
    except SealError as exc:
        raise BankingError("Die Bankverbindung lässt sich nicht entschlüsseln – bitte neu verbinden.") from exc


def _key(account: BankAccount, tx, occurrence: int) -> str:
    if tx.reference:
        raw = f"psd2|{account.provider_uid}|{tx.reference}"
    else:
        parts = (account.provider_uid, tx.booked_on, tx.amount, tx.counterparty, tx.iban, tx.purpose, occurrence)
        raw = "psd2|" + "|".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def sync_connection(connection: BankConnection, *, api: Client, now: dt.datetime | None = None) -> int:
    """Fetch booked transactions of every account of an active connection; returns how many are new."""
    now = now or timezone.now()
    if connection.status != BankConnection.Status.ACTIVE:
        return 0
    if connection.valid_until and connection.valid_until <= now:
        connection.status = BankConnection.Status.EXPIRED
        connection.save(update_fields=["status"])
        return 0
    _session_id(connection)  # fails early if the secret key changed
    company, created = connection.company, 0
    try:
        for account in connection.accounts.filter(active=True).exclude(provider_uid=""):
            last = account.transactions.filter(source=SOURCE).aggregate(m=Max("booked_on"))["m"]
            today = timezone.localtime(now).date()
            date_from = last - dt.timedelta(days=OVERLAP_DAYS) if last else today - dt.timedelta(days=FIRST_FETCH_DAYS)
            fetched = api.transactions(account.provider_uid, date_from=date_from)
            seen: Counter = Counter()
            # lines already imported from a statement file: same account, day and amount count as known
            from_files = Counter(
                account.transactions.filter(booked_on__gte=date_from).exclude(source=SOURCE)
                .values_list("booked_on", "amount")
            )  # fmt: skip
            matched_files: Counter = Counter()
            with transaction.atomic():
                existing = set(BankTransaction.objects.filter(company=company).values_list("import_key", flat=True))
                for tx in fetched:
                    identity = (tx.booked_on, tx.amount, tx.counterparty, tx.iban, tx.purpose)
                    seen[identity] += 1
                    key = _key(account, tx, seen[identity])
                    if key in existing:
                        continue
                    day_amount = (tx.booked_on, tx.amount)
                    if matched_files[day_amount] < from_files[day_amount]:
                        matched_files[day_amount] += 1
                        continue
                    BankTransaction.objects.create(
                        company=company, account=account, booked_on=tx.booked_on, amount=tx.amount,
                        currency=tx.currency, counterparty=tx.counterparty, iban=tx.iban, purpose=tx.purpose,
                        import_key=key, source=SOURCE,
                    )  # fmt: skip
                    existing.add(key)
                    created += 1
    except EnableBankingError as exc:
        connection.last_error = str(exc)[:500]
        connection.save(update_fields=["last_error"])
        raise BankingError(str(exc)) from exc
    connection.last_sync_at, connection.last_error = now, ""
    connection.save(update_fields=["last_sync_at", "last_error"])
    if created:
        record("bank.fetch", company=company, entity="bank_connection", entity_id=connection.pk,
               data={"new": created})  # fmt: skip
    auto_match(company)
    return created


def sync_all(*, api: Client | None = None) -> str:
    """Worker: fetch every active connection. One failing bank does not stop the others."""
    connections = list(BankConnection.objects.filter(status=BankConnection.Status.ACTIVE).select_related("company"))
    if not connections:
        return "keine aktive Bankverbindung"
    api = api or client()
    created, failed = 0, []
    for connection in connections:
        try:
            created += sync_connection(connection, api=api)
        except BankingError as exc:
            failed.append(f"{connection.bank_name}: {exc}")
    summary = f"{created} neue Umsätze aus {len(connections)} Verbindung(en)"
    if failed:
        raise BankingError(summary + "; Fehler: " + "; ".join(failed))
    return summary


def disconnect(connection: BankConnection, *, user, api: Client | None) -> None:
    """End the consent at the provider (best effort) and forget the session. Fetched data stays."""
    if connection.session and api is not None:
        try:
            api.end(_session_id(connection))
        except (EnableBankingError, BankingError):
            log.warning("Sitzung bei Enable Banking ließ sich nicht beenden", exc_info=True)
    connection.status, connection.session = BankConnection.Status.REVOKED, ""
    connection.save(update_fields=["status", "session"])
    record("bank.disconnect", actor=user, company=connection.company, entity="bank_connection",
           entity_id=connection.pk)  # fmt: skip


def expiring(company, *, now: dt.datetime | None = None) -> list[BankConnection]:
    """Active connections whose consent ends within ``REMIND_DAYS`` – shown as a reminder."""
    now = now or timezone.now()
    return list(
        BankConnection.objects.filter(
            company=company, status=BankConnection.Status.ACTIVE,
            valid_until__lte=now + dt.timedelta(days=REMIND_DAYS),
        )
    )  # fmt: skip
