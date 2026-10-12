"""Self-diagnosis for deployments (``skontio doctor``).

The OIDC check runs server-to-server from the Skontio container, which is exactly where
NAT loopback (hairpin) problems and clock drift between NAS and identity provider show up.
"""

from __future__ import annotations

import datetime as dt
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

MAX_CLOCK_SKEW = dt.timedelta(seconds=60)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""


def check_oidc(issuer: str, *, http_client: httpx.Client, now: dt.datetime | None = None) -> list[Check]:
    now = now or dt.datetime.now(dt.UTC)
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        response = http_client.get(url, timeout=10)
        response.raise_for_status()
        discovery = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return [
            Check(
                "discovery",
                False,
                f"{url} nicht abrufbar: {exc}",
                "Vom Skontio-Container aus muss die öffentliche Issuer-URL erreichbar sein. Kann der Router kein "
                "NAT-Loopback, im lokalen DNS einen Split-DNS-Eintrag auf die interne IP des Providers anlegen.",
            )
        ]

    checks = [Check("discovery", True, url)]
    checks.append(
        Check(
            "issuer",
            discovery.get("issuer") == issuer,
            f"erwartet {issuer!r}, Provider meldet {discovery.get('issuer')!r}",
            "SKONTIO_OIDC_ISSUER exakt so setzen, wie der Provider ihn meldet (auch ohne/mit Schrägstrich).",
        )
    )
    methods = discovery.get("code_challenge_methods_supported") or []
    checks.append(
        Check(
            "pkce",
            "S256" in methods,
            f"unterstützt: {', '.join(methods) or 'nichts angegeben'} – Skontio nutzt ausschließlich S256",
        )
    )

    jwks_uri = discovery.get("jwks_uri")
    try:
        keys = http_client.get(jwks_uri, timeout=10).json().get("keys", []) if jwks_uri else []
        checks.append(Check("jwks", bool(keys), f"{len(keys)} Schlüssel unter {jwks_uri}"))
    except (httpx.HTTPError, ValueError) as exc:
        checks.append(Check("jwks", False, f"{jwks_uri} nicht abrufbar: {exc}"))

    has_groups = "groups" in (discovery.get("scopes_supported") or []) or "groups" in (
        discovery.get("claims_supported") or []
    )
    checks.append(
        Check(
            "groups",
            has_groups,
            "groups-Claim verfügbar" if has_groups else "kein groups-Claim angeboten",
            "Gruppen skontio / skontio-admin werden für Zugriff und Rollen benötigt.",
        )
    )

    server_date = response.headers.get("Date")
    if server_date:
        skew = parsedate_to_datetime(server_date) - now
        checks.append(
            Check(
                "clock",
                abs(skew) <= MAX_CLOCK_SKEW,
                f"Zeitabweichung zum Provider: {skew.total_seconds():+.0f} s",
                "NTP auf NAS und Provider-Host aktivieren; Tokens werden sonst als ungültig abgelehnt.",
            )
        )
    return checks


def check_smtp(mailer, *, host: str) -> list[Check]:
    """Connect and log in with the configured mailer, without sending anything."""
    if not host:
        return [
            Check("server", False, "SKONTIO_SMTP_HOST ist nicht gesetzt", "SMTP-Daten in der Compose-Datei eintragen.")
        ]
    try:
        mailer.open()
    except smtplib.SMTPAuthenticationError as exc:
        return [
            Check(
                "login", False, f"Anmeldung abgelehnt: {exc.smtp_code}", "SKONTIO_SMTP_USER und smtp_password prüfen."
            )
        ]
    except ssl.SSLError as exc:
        return [
            Check(
                "tls",
                False,
                f"TLS fehlgeschlagen: {exc}",
                "Port 587 nutzt meist starttls, Port 465 ssl (SKONTIO_SMTP_SECURITY).",
            )
        ]
    except (OSError, smtplib.SMTPException) as exc:
        return [
            Check(
                "server",
                False,
                f"{host} nicht erreichbar: {exc}",
                "Host, Port und ausgehende Firewall-Regeln des NAS prüfen.",
            )
        ]
    else:
        mailer.close()
    return [Check("server", True, f"{host} erreichbar, Anmeldung erfolgreich")]


def check_backup(backup_dir: Path, *, vendor: str, pg_versions=None) -> list[Check]:
    """``pg_versions`` returns (pg_dump major, server major) and raises on problems (Postgres only)."""
    writable = Path(backup_dir).is_dir() and os.access(backup_dir, os.W_OK)
    checks = [
        Check(
            "verzeichnis",
            writable,
            f"{backup_dir} {'beschreibbar' if writable else 'fehlt oder ist nicht beschreibbar'}",
            "Dataset für Backups anlegen und UID 568 Schreibrechte geben.",
        )
    ]
    if vendor == "postgresql":
        try:
            client, server = pg_versions()
        except Exception as exc:  # noqa: BLE001 - report any failure as a check result
            checks.append(Check("pg_dump", False, str(exc), "SKONTIO_PG_BIN_DIR und Datenbankverbindung prüfen."))
        else:
            checks.append(Check("pg_dump", True, f"pg_dump {client} für Postgres-Server {server}"))
    return checks
