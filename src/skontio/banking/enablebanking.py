"""Client for the Enable Banking API (PSD2 account information).

Every request carries a short-lived JWT signed with the application's private key (RS256).
Docs: https://enablebanking.com/docs/api/reference/ – only the calls Skontio needs.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from joserfc import jwt
from joserfc.jwk import RSAKey

TIMEOUT = 30
MAX_PAGES = 50


class EnableBankingError(RuntimeError):
    pass


@dataclass(frozen=True)
class Bank:
    name: str
    country: str
    logo: str = ""


@dataclass(frozen=True)
class Account:
    uid: str
    iban: str
    name: str
    currency: str


@dataclass(frozen=True)
class Session:
    session_id: str
    accounts: list[Account]
    valid_until: dt.datetime | None


@dataclass(frozen=True)
class Transaction:
    reference: str
    booked_on: dt.date
    amount: Decimal  # signed: + incoming
    currency: str
    counterparty: str
    iban: str
    purpose: str


class Client:
    def __init__(self, config: dict, *, http: httpx.Client | None = None, key_pem: str | None = None):
        self.config = config
        self.http = http or httpx.Client(timeout=TIMEOUT)
        if key_pem is None:
            path = Path(config.get("key_file") or "")
            if not path.is_file():
                raise EnableBankingError("SKONTIO_ENABLEBANKING_KEY_FILE fehlt oder ist nicht lesbar")
            key_pem = path.read_text(encoding="utf-8")
        try:
            self.key = RSAKey.import_key(key_pem)
        except Exception as exc:  # noqa: BLE001 – joserfc raises various types for a broken PEM
            raise EnableBankingError("Der private Schlüssel für Enable Banking ist ungültig") from exc

    def _token(self) -> str:
        now = int(time.time())
        header = {"typ": "JWT", "alg": "RS256", "kid": self.config["app_id"]}
        claims = {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + 3600}
        return jwt.encode(header, claims, self.key)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = self.config["api"] + path
        try:
            response = self.http.request(method, url, headers={"Authorization": f"Bearer {self._token()}"}, **kwargs)
        except httpx.HTTPError as exc:
            raise EnableBankingError(f"Enable Banking nicht erreichbar: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text[:300]
            raise EnableBankingError(f"Enable Banking: {response.status_code} {detail}")
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise EnableBankingError("Enable Banking: unerwartete Antwort") from exc
        if not isinstance(data, dict):
            raise EnableBankingError("Enable Banking: unerwartete Antwort")
        return data

    def banks(self, country: str = "DE") -> list[Bank]:
        data = self._request("GET", "/aspsps", params={"country": country, "psu_type": "personal"})
        return [
            Bank(str(b.get("name", ""))[:200], str(b.get("country", country))[:2], str(b.get("logo", ""))[:500])
            for b in data.get("aspsps", [])
            if isinstance(b, dict) and b.get("name")
        ]

    def start(self, bank: Bank, *, state: str, redirect_url: str, valid_until: dt.datetime, business: bool) -> str:
        """Returns the URL of the bank login."""
        body = {
            "access": {"valid_until": valid_until.isoformat()},
            "aspsp": {"name": bank.name, "country": bank.country},
            "state": state,
            "redirect_url": redirect_url,
            "psu_type": "business" if business else "personal",
        }
        url = self._request("POST", "/auth", json=body).get("url", "")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise EnableBankingError("Enable Banking hat keine gültige Login-Adresse geliefert")
        return url

    def session(self, code: str) -> Session:
        data = self._request("POST", "/sessions", json={"code": code})
        accounts = []
        for item in data.get("accounts", []):
            if not isinstance(item, dict) or not item.get("uid"):
                continue
            account_id = item.get("account_id") if isinstance(item.get("account_id"), dict) else {}
            accounts.append(
                Account(
                    uid=str(item["uid"])[:100],
                    iban=str(account_id.get("iban") or "").replace(" ", "").upper()[:34],
                    name=str(item.get("name") or item.get("product") or "Konto")[:100],
                    currency=str(item.get("currency") or "EUR")[:3],
                )
            )
        access = data.get("access") if isinstance(data.get("access"), dict) else {}
        valid_until = _datetime(access.get("valid_until"))
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise EnableBankingError("Enable Banking hat keine Sitzung geliefert")
        return Session(session_id, accounts, valid_until)

    def transactions(self, account_uid: str, *, date_from: dt.date) -> list[Transaction]:
        result, key = [], None
        for _ in range(MAX_PAGES):
            params = {"date_from": date_from.isoformat()}
            if key:
                params["continuation_key"] = key
            data = self._request("GET", f"/accounts/{account_uid}/transactions", params=params)
            for item in data.get("transactions", []):
                tx = _transaction(item)
                if tx is not None:
                    result.append(tx)
            key = data.get("continuation_key")
            if not key:
                break
        return result

    def end(self, session_id: str) -> None:
        self._request("DELETE", f"/sessions/{session_id}")


def _datetime(value) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _party(item: dict, key: str) -> dict:
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def _transaction(item) -> Transaction | None:
    """Booked transactions only; anything malformed is skipped instead of guessed."""
    if not isinstance(item, dict) or item.get("status", "BOOK") != "BOOK":
        return None
    amount_data = _party(item, "transaction_amount")
    try:
        amount = Decimal(str(amount_data.get("amount")))
        booked_on = dt.date.fromisoformat(str(item.get("booking_date") or item.get("value_date")))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or abs(amount) >= Decimal("1e12"):
        return None
    incoming = item.get("credit_debit_indicator") == "CRDT"
    amount = abs(amount) if incoming else -abs(amount)
    other = "debtor" if incoming else "creditor"
    info = item.get("remittance_information")
    purpose = " ".join(str(x) for x in info) if isinstance(info, list) else str(info or "")
    return Transaction(
        reference=str(item.get("entry_reference") or item.get("transaction_id") or "")[:100],
        booked_on=booked_on,
        amount=amount.quantize(Decimal("0.01")),
        currency=str(amount_data.get("currency") or "EUR")[:3],
        counterparty=" ".join(str(_party(item, other).get("name") or "").split())[:200],
        iban=str(_party(item, f"{other}_account").get("iban") or "").replace(" ", "").upper()[:34],
        purpose=" ".join(purpose.split())[:500],
    )
