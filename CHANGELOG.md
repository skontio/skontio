# Änderungen

## 0.3.0 – 2026-11-02

Das bisher größte Update: Bank, Belege, Einrichtung, Briefvorlagen und eine gründliche Sicherheitsrunde.

### Vor dem Update lesen

- **Backup**: Skontio legt vor den Migrationen automatisch `skontio-<datum>-vor-update-0.3.0.tar` an; es wird
  nicht mehr automatisch gelöscht. Weg zurück: alten Image-Tag setzen und dieses Backup einspielen.
- **Änderungsprotokoll**: wird beim ersten Start einmalig auf eine Hash-Kette mit Schlüssel umgestellt (aus
  `secret_key`). Danach `skontio verify_audit` ausführen. Der `secret_key` muss erhalten bleiben – wie bisher.
- **Kein Django-Admin mehr** unter `/admin/`; alles Nötige liegt unter *Einstellungen*.
- **Sitzungen** laufen nach 12 Stunden ab (`SKONTIO_SESSION_HOURS`).
- **Compose-Härtung** übernehmen (Web und Worker), siehe `deploy/truenas/compose.yaml`:
  `tmpfs: /tmp:rw,noexec,nosuid,nodev,size=256m`, `pids_limit: 256`, `mem_limit: 1g`.
- Neue Funktionen sind **aus, bis du sie einschaltest**: Kontoabruf, Beleg-Postfach, automatische Zuordnung,
  Belegerkennung. Einrichtung in `docs/betrieb.md`.

### Neu

**Rechnungen und Angebote**
- Einrichtungsassistent: Unternehmen mit Rechtsform, Logo und Design, Bank, Nummern, Versand mit Testmail
- vier Briefbogen-Designs nach DIN 5008 (Klassisch, Farbband, Schlicht, Seitenleiste) mit Falzmarken,
  Folgeseitenkopf und Seitenzahlen – für alle Dokumente einschließlich Mahnung
- Pflichtangaben je Rechtsform (Registergericht, Registernummer, Geschäftsführung) in der Fußzeile
- Bruttopreise je Position, Positionsgruppen mit Zwischensumme, Gesamtrabatt, Lieferschein, Arbeitskosten (§ 35a)
- Standardanhänge (AGB, Datenschutz) beim Versand, weitere Dateien anhängen, Zahlungslink, Postausgang
- Nummernkreise in den Einstellungen, Zahlungsziel als Firmenstandard

**Geld und Belege**
- Kontoabruf über PSD2 (Enable Banking), MT940-Import, mehrere Konten, Suche und Filter
- automatische Zuordnung eindeutiger Zahlungseingänge – auch Daueraufträge über die gelernte IBAN –, jederzeit
  zurücknehmbar; Bank-Regeln als Vorschlag
- Beleg-Postfach (IMAP), Belegerkennung über den KI-Anbieter (nur als geprüfter Vorschlag)
- sonstige Einnahmen, wiederkehrende Ausgaben, Lieferanten als Kontakte

**Steuern und Export**
- Zusammenfassende Meldung, DATEV-Export (SKR03/SKR04), frei wählbare Zeiträume, USt-Jahresübersicht
- Gesamtexport als ZIP, CSV-Import und -Export für Kontakte und Leistungen

**Bedienung**
- neues Erscheinungsbild, Positionen direkt in der Tabelle bearbeiten, Schnellsuche, Filter in Listen
- Rolle „nur Entwürfe“ für Mitarbeitende
- eigenes Theme über `SKONTIO_THEME_DIR` (Farben, Schriften, Templates)

### Sicherheit

- Django-Admin entfernt (umging LAN-Sperre, Fehlversuchssperre und zweiten Faktor des Notfall-Logins)
- Änderungsprotokoll als HMAC-Kette; Kettenende in jedem Backup, `verify_audit --backup` deckt Umschreiben auf
- Warnmail an alle Admins, wenn IBAN, Zahlungslink oder Absenderadresse geändert werden
- Container ohne Shell, Paketverwaltung, pip und setuid-Programme; Basis-Images per Digest; signierte Images mit
  SBOM und Herkunftsnachweis (`gh attestation verify`)
- Abhängigkeiten aktualisiert (u. a. authlib, joserfc, pillow, pypdf); `pip-audit` und Image-Scan in der CI
- feste Liste öffentlicher Routen, Angriffstests, `skontio doctor db`
- Analyse und Konzept: `docs/sicherheit-angriffe.md`, `docs/sicherheit-container.md`

### Qualität

- Update-Test in der CI: letzte Release-Version mit Daten → neuer Build, für SQLite und Postgres
- Negativtests mit Müll-Eingaben für jede Route, Fuzzing der Datei-Importe

## 0.2.1 – 2026-10-16

Erste veröffentlichte Version: Rechnungen, E-Rechnung, Festschreibung, Ausgaben, EÜR, UStVA-Kennzahlen,
Mahnwesen, wiederkehrende Rechnungen, Bank-CSV, MCP-Zugang und KI-Assistent.
