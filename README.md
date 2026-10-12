# Skontio

**Open-Source-Rechnungen und CRM für Selbstständige** – selbst hostbar, E-Rechnung inklusive,
alle Funktionen frei (auch eigenes Logo, mehrere Vorlagen und Hosting für Dritte).

> ⚠️ **Frühe Entwicklungsphase (Phase 1a).** Vor dem Echtbetrieb bitte mit der eigenen Steuerberatung prüfen.
> Skontio ist **keine Steuer- oder Rechtsberatung**. Die Software unterstützt die
> Einhaltung von GoBD, UStG und AO; die Verantwortung bleibt bei dir.

## Ziele

- Kunden, Angebote, Rechnungen, Zahlungseingänge – für Kleinunternehmer (§ 19 UStG) und Regelbesteuerung
- E-Rechnung nach EN 16931: **ZUGFeRD/Factur-X** (PDF/A-3) und **XRechnung**, erzeugen und einlesen
- Festschreibung, lückenlose Nummernkreise, Änderungsprotokoll, revisionssichere Ablage
- OIDC-Login (z. B. Pocket ID, Authentik, Keycloak), MCP-Server für KI-Assistenten, PWA für Android
- Ein Container, SQLite oder Postgres – läuft auf einem NAS

Der vollständige Plan steht in [`docs/plan.md`](docs/plan.md), Architekturentscheidungen in [`docs/adr/`](docs/adr/).

## Stand Phase 1a

| Baustein | Status |
|---|---|
| Steuer-Engine DE (19/7/0 %, § 19, § 13b, ig. Lieferung, Ausfuhr), Summen nach EN 16931 | ✅ |
| Steuerstatus Kleinunternehmer ↔ Regelbesteuerung mit Stichtag, Grenzwert-Überwachung | ✅ |
| Kunden, Leistungskatalog, Angebot → Rechnung, Storno, lückenlose Nummernkreise | ✅ |
| Dokumentenablage je Kunde/Dokument (Typprüfung am Inhalt, Aufbewahrung 6 Jahre optional) | ✅ 1b |
| CRM: Aktivitäten, Wiedervorlagen (Übersicht, Angebot nachfassen), Versand automatisch protokolliert | ✅ 1b |
| Abschlags- und Schlussrechnungen (§ 14 Abs. 5 UStG, BT-113/BT-115), geprüft mit KoSIT/Mustang | ✅ 1b |
| Festschreibung (ORM + Datenbank-Trigger), Schwärzen statt Löschen nach Fristablauf | ✅ |
| ZUGFeRD/XRechnung erzeugen – geprüft mit **KoSIT** und **Mustang/veraPDF** | ✅ |
| E-Rechnungs-Eingang (CII, UBL, ZUGFeRD-PDF), revisionssicheres Archiv | ✅ |
| Zahlungen, offene Posten, GiroCode, Versand per SMTP mit Protokoll | ✅ |
| Login über OIDC (Pocket ID), Notfall-Login nur aus dem LAN mit TOTP-2FA, Argon2id | ✅ |
| Oberfläche (Mobile-first), installierbare PWA, PDF teilen (Web Share API) | ✅ |
| Backup/Restore (SQLite und Postgres) mit Prüfsummen, atomar, Sicherung vor Updates | ✅ |
| MCP-Server (2026-07-28 + Legacy), 19 Tools nur für Entwürfe und Auswertungen, Pseudonyme, hashverkettetes Audit-Log | ✅ 1b |
| Zugangstokens und stdio-Bridge `skontio-mcp` für Claude Desktop und Claude Code | ✅ 1b |
| Auftragsbestätigung, Abschlags- und Schlussrechnung (§ 14 Abs. 5 UStG) | ✅ 1b |
| CRM: Aktivitäten, Wiedervorlagen, Dateiablage mit Aufbewahrungsfrist | ✅ 1b |
| Dokumente auf Deutsch oder Englisch (PDF, E-Rechnung, Mailtext) | ✅ 1b |
| Vorlagen (Logo, Farbe, zwei Layouts, Fußzeile), Textbausteine, PDF-Vorschau für Entwürfe | ✅ 1b |
| Steuerstatus-Wechsel mit Bestätigung, Korrektur-Assistent bei rückwirkend überschrittener Grenze | ✅ 1b |
| Verfahrensdokumentation (GoBD) aus der Konfiguration, versioniert und archiviert | ✅ 1b |
| Ausgaben mit Belegfoto, Belegeingang und „Teilen an Skontio“ (Web Share Target), EÜR (Zufluss/Abfluss, Wechsel im Jahr, § 13b-Hinweis), CSV-Journal | ✅ 2 |
| Mahnwesen: einstellbare Stufen, Mahnschreiben als PDF/A mit GiroCode, Versand mit Rechnung | ✅ 2 |
| Wiederkehrende Rechnungen als Entwürfe (Nachholen ohne Doppelte, Cron-Befehl) | ✅ 2 |
| Bankumsätze aus CAMT.053/CSV, Vorschläge und Zuordnung zu Rechnungen und Ausgaben | ✅ 2 |
| USt-Voranmeldung (Ist): Kennzahlen mit Kopier-Knöpfen für Mein ELSTER, Frist, § 13b, Übermittlung vermerken | ✅ 2 |
| Assistent, DATEV, Web Push, mehrere Firmen, PSD2/FinTS | Phase 2–3 |

Betrieb auf TrueNAS: [`docs/betrieb.md`](docs/betrieb.md).

## Entwickeln

```bash
uv sync
uv run pytest                      # Tests (SQLite)
tools/validators/fetch.sh          # offizielle E-Rechnungs-Validatoren laden (Java nötig)
uv run pytest -m einvoice          # E-Rechnung gegen KoSIT und Mustang prüfen
SKONTIO_DATABASE_URL=postgres://user:pw@localhost:5432/skontio uv run pytest   # Postgres (Backup-Tests brauchen postgresql-client ≥ Server)
```

## Betrieb

Docker-Image `ghcr.io/skontio/skontio`. Vorlagen für TrueNAS und Caddy liegen in [`deploy/`](deploy/).

```bash
skontio doctor oidc                # prüft die Verbindung zum Identity Provider
skontio doctor smtp                # prüft die SMTP-Anmeldung
skontio doctor backup              # prüft Backup-Verzeichnis und pg_dump
```

## Lizenz

[AGPL-3.0-or-later](LICENSE). Beiträge nach dem [Developer Certificate of Origin](CONTRIBUTING.md).
