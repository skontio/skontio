# Angriffsanalyse: Wie käme jemand an die Daten – oder könnte sie verändern?

Stand 2026-11-02. Vorgehen wie ein Angreifer: Ziel wählen, Wege sammeln, jeden Weg am Code und mit Tests prüfen.
Ergänzt `docs/sicherheit-container.md` (was passiert, wenn trotzdem jemand Code ausführt).

## Ziele des Angreifers

1. **Daten lesen**: Kunden, Rechnungen, Bankumsätze, Belege.
2. **Daten ändern**: Rechnungen fälschen, Zahlungen umleiten, Spuren im Änderungsprotokoll verwischen.
3. **Geld umleiten**: die IBAN auf neuen Rechnungen gegen die eigene tauschen (häufigster Betrug bei Rechnungen).

## Ergebnis je Angriffsweg

| # | Weg | Ergebnis | Nachweis |
|---|---|---|---|
| 1 | Notfall-Login aus dem Internet durch gefälschte IP (`X-Forwarded-For`) | hält: nur vertrauenswürdige Proxys zählen, die Kette wird von rechts gelesen; Caddy überschreibt den Header | `core/net.py`, `test_adversarial` |
| 2 | Passwort-Login über `/admin/` | **war offen** (keine LAN-Sperre, keine Sperre nach Fehlversuchen, kein zweiter Faktor) – **entfernt** | `test_public_surface` |
| 3 | Nicht benötigte OAuth-Endpunkte (Device-Flow, Introspection) | **entfernt** | `test_public_surface` |
| 4 | Fehler in Login/OAuth/MCP (state, PKCE, Audience, offene Weiterleitung) | hält | `test_oidc`, `test_mcp_oauth`, `test_adversarial` |
| 5 | XSS über hochgeladene Dateien, Belege, E-Rechnungen, Namen | hält: Ausgabe immer maskiert, Dateien nur als Download oder in einer Sandbox (`CSP: sandbox`), CSP ohne `unsafe-inline` | `test_adversarial`, `test_public_surface` |
| 6 | Präparierte Dateien (XML-Bomben, XXE, kaputte PDFs, ZIPs) über das Beleg-Postfach | hält: Typprüfung am Inhalt, Größenlimits, sichere XML-Parser; Bibliotheken mit bekannten Lücken **aktualisiert**, CI prüft wöchentlich | `test_receipt_mailbox`, `test_adversarial`, `pip-audit` |
| 7 | Anweisungen in Belegen oder Verwendungszwecken für den KI-Assistenten („Prompt Injection“) | hält: KI-Werkzeuge lesen nur oder legen **Entwürfe** an; Festschreiben, Versenden, Buchen gibt es für die KI nicht; KI-Entwürfe brauchen eine Prüfung durch einen Menschen | `tools/registry.py`, `test_mcp_*` |
| 8 | Daten einer anderen Firma per ID raten | hält: jede Abfrage ist auf die Firma beschränkt; ungültige IDs ergeben 404 | `test_adversarial` |
| 9 | Rolle „nur Entwürfe“ umgehen | hält: jede gesperrte Aktion über jede Route probiert | `test_adversarial`, `test_roles` |
| 10 | SQL-Injection | hält: keine selbst gebauten SQL-Abfragen mit Eingaben | Code-Durchsicht |
| 11 | Festgeschriebene Rechnungen in der Datenbank ändern | hält: Trigger in der Datenbank verbieten UPDATE und DELETE; nur ein Datenbank-Superuser könnte sie abschalten → **`skontio doctor db`** warnt davor | `test_document_lock`, `test_tamper` |
| 12 | Änderungsprotokoll umschreiben und die Hash-Kette neu berechnen | **war möglich** (einfaches SHA-256) – **jetzt HMAC** mit Schlüssel aus `secret_key`; alte Einträge sind über eine Markierung festgenagelt | `test_tamper` |
| 13 | Protokoll umschreiben mit dem Schlüssel (Angreifer im Container) | **neu erkennbar**: jedes Backup speichert das Kettenende; `skontio verify_audit --backup <datei>` vergleicht mit Sicherungen außer Haus | `test_tamper` |
| 14 | IBAN tauschen über eine gekaperte Sitzung | **war still möglich** – jetzt protokolliert, **sofortige Warnmail an alle Admins** und die Firmenadresse (IBAN gekürzt) | `test_tamper` |
| 15 | Gestohlenes Session-Cookie lange nutzen | **verkürzt**: Sitzungen gelten 12 Stunden statt 14 Tage (`SKONTIO_SESSION_HOURS`) | `test_tamper` |
| 16 | Lieferkette: manipulierte GitHub-Action veröffentlicht ein Image | **gehärtet**: alle Actions auf Commit-SHAs festgenagelt, Updates nur als geprüfte PRs (Dependabot) | `.github/` |

## Was bleibt

- **Wer den Docker-Host oder `secret_key` und die Datenbank hat, kann alles.** Dagegen helfen nur der Schutz des NAS
  (2FA, nur LAN) und Sicherungen außer Haus, mit denen `verify_audit --backup` eine Fälschung später aufdeckt.
- **Backups sind nicht verschlüsselt.** Sie enthalten alle Daten. Ziel-Dataset verschlüsselt anlegen und beim
  Kopieren außer Haus (Cloud-Sync, USB) verschlüsseln.
- **Eine IBAN-Änderung wird gemeldet, aber nicht verhindert.** Nächster Schritt wäre eine erneute Anmeldung
  (Passkey) direkt vor der Änderung.
- **Sehr große, präparierte PDFs** können den Worker eine Zeit lang auslasten; `mem_limit` und `pids_limit`
  begrenzen den Schaden auf den Container.

## Regelmäßig prüfen

```sh
skontio verify_audit --backup /backups/<letzte-externe-sicherung>.tar   # Protokoll unverändert?
skontio verify_archive                                                  # Archivdateien unverändert?
skontio doctor db                                                       # Datenbankrolle ohne Sonderrechte?
```
