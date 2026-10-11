# Skontio

**Open-Source-Rechnungen und CRM für Selbstständige** – selbst hostbar, E-Rechnung inklusive,
alle Funktionen frei (auch eigenes Logo, mehrere Vorlagen und Hosting für Dritte).

> ⚠️ **Frühe Entwicklungsphase (Phase 0).** Noch nicht für den Echtbetrieb geeignet.
> Skontio ist **keine Steuer- oder Rechtsberatung**. Die Software unterstützt die
> Einhaltung von GoBD, UStG und AO; die Verantwortung bleibt bei dir.

## Ziele

- Kunden, Angebote, Rechnungen, Zahlungseingänge – für Kleinunternehmer (§ 19 UStG) und Regelbesteuerung
- E-Rechnung nach EN 16931: **ZUGFeRD/Factur-X** (PDF/A-3) und **XRechnung**, erzeugen und einlesen
- Festschreibung, lückenlose Nummernkreise, Änderungsprotokoll, revisionssichere Ablage
- OIDC-Login (z. B. Pocket ID, Authentik, Keycloak), MCP-Server für KI-Assistenten, PWA für Android
- Ein Container, SQLite oder Postgres – läuft auf einem NAS

Der vollständige Plan steht in [`docs/plan.md`](docs/plan.md), Architekturentscheidungen in [`docs/adr/`](docs/adr/).

## Stand Phase 0

| Baustein | Status |
|---|---|
| Nummernkreise (lückenlos, parallel sicher auf SQLite und Postgres) | ✅ getestet |
| Festschreibung inkl. Datenbank-Triggern | ✅ getestet |
| XRechnung (CII) gegen **KoSIT-Validator** | ✅ akzeptiert |
| ZUGFeRD EN 16931 + **PDF/A-3b** gegen **Mustang/veraPDF** | ✅ gültig |
| OAuth 2.1 für MCP (PKCE, RFC 8707, RFC 9728) | ✅ getestet |
| `skontio doctor oidc` | ✅ |

## Entwickeln

```bash
uv sync
uv run pytest                      # Tests (SQLite)
tools/validators/fetch.sh          # offizielle E-Rechnungs-Validatoren laden (Java nötig)
uv run pytest -m einvoice          # E-Rechnung gegen KoSIT und Mustang prüfen
SKONTIO_DATABASE_URL=postgres://user:pw@localhost:5432/skontio uv run pytest   # Postgres
```

## Betrieb

Docker-Image `ghcr.io/skontio/skontio`. Vorlagen für TrueNAS und Caddy liegen in [`deploy/`](deploy/).

```bash
skontio doctor oidc                # prüft die Verbindung zum Identity Provider
```

## Lizenz

[AGPL-3.0-or-later](LICENSE). Beiträge nach dem [Developer Certificate of Origin](CONTRIBUTING.md).
