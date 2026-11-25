# Änderungen

## 0.6.0 – 2026-11-25

Skontio hat jetzt wahlweise eine neue Oberfläche, und Angebote trennen einmalige von laufenden Positionen. Update
wie gewohnt per Image-Tag `0.6`; eine neue Migration (`core` 0010), das automatische Backup vor dem Update greift.
Das bisherige Aussehen bleibt die Voreinstellung.

### Neu

- **Angebote: einmalige und laufende Positionen getrennt**: Enthält ein Angebot Positionen mit der Einheit Monat
  oder Jahr, weist das PDF die Summen getrennt nach „Einmalig“, „Monatlich (pro Monat)“ und „Jährlich (pro Jahr)“
  aus. Die Einheit „Jahr“ wird in den Dokumenttexten übersetzt (deutsch und englisch).
- **Oberfläche „Modern“**: Unter Einstellungen → Oberfläche lässt sich das Aussehen von Skontio zwischen „Standard“
  (wie bisher, Voreinstellung) und „Modern“ umschalten. „Modern“ ist eine eigene Bedienoberfläche: dunkle Navigation
  mit Icons (auf schmalen Fenstern eine Leiste, die sich beim Darüberfahren öffnet), Übersicht als Kachelraster mit
  Begrüßung und offenem Betrag, Listenzeilen als schwebende Karten, Filter als Segment-Schalter, Status als Tag mit
  Punkt, eine Befehlspalette (Strg/⌘ + K oder „/“) zum Suchen und Springen, eine schwebende Tab-Leiste am Handy und
  sanfte Einblend-Bewegung (entfällt bei „Bewegung reduzieren“). Das gilt für alle Nutzer der Installation; PDFs
  bleiben unverändert. Eine eigene `theme.css` aus `SKONTIO_THEME_DIR` greift in beiden Varianten.

## 0.5.1 – 2026-11-24

Kleine Korrektur an der Oberfläche. Update wie gewohnt per Image-Tag `0.5`; keine Migration.

### Behoben

- **Position hinzufügen**: Wählt man eine Leistung aus dem Katalog, zeigt das Feld „Einheit“ jetzt deren Einheit
  (z. B. „Monat“) statt immer „Stück (Standard)“. Gespeichert wurde die Einheit der Leistung schon vorher richtig;
  die Anzeige führte nur in die Irre. Bei freien Positionen bleibt das Feld frei wählbar.

## 0.5.0 – 2026-11-23

Wiederkehrende Rechnungen lassen sich wie eine Vorlage bearbeiten und löschen, bei manuellem Versand erinnert eine
Mail ans Festschreiben, und die Version steht in der Oberfläche. Update wie gewohnt per Image-Tag `0.5`; keine
Migration.

### Neu

- **Version in der Oberfläche**: Die laufende Version steht unten auf jeder Seite (nur für angemeldete Nutzer).
- **Wiederkehrende Rechnungen bearbeiten und löschen**: Eine Serie verhält sich wie eine Vorlage. Bezeichnung,
  Rhythmus, Beginn, Ende, Texte und Positionen lassen sich ändern (Positionen entfernen, ändern, ergänzen); die
  Änderung gilt ab der nächsten Rechnung. Rhythmus und Beginn sind gesperrt, sobald Rechnungen entstanden sind.
  Löschen geht, sobald keine Entwürfe der Serie mehr offen sind; die bisherigen Rechnungen bleiben unverändert.
- **Hinweis bei manuellem Versand**: Legt der Worker für eine Serie ohne automatischen Versand einen Entwurf an,
  bekommen die Admins eine E-Mail mit Link zur Rechnung, damit sie geprüft und festgeschrieben wird.

## 0.4.1 – 2026-11-23

Wartungsrelease ohne Änderung am Programm: Das Image wird mit aktualisierten Build-Werkzeugen gebaut, dazu eine
Anleitung für die externe Sicherung mit Backrest. Update wie gewohnt per Image-Tag `0.4`; keine Migration.

### Intern

- GitHub Actions aktualisiert: `docker/login-action` 4.7.0, `docker/metadata-action` 6.2.0,
  `docker/build-push-action` 7.4.0 (je einzeln getestet, Veröffentlichungslauf grün).
- Dokumentation: Abschnitt „Externe Sicherung mit Backrest“ in `docs/betrieb.md`.

## 0.4.0 – 2026-11-22

Wiederkehrende Rechnungen lassen sich automatisch versenden, Fehler im Hintergrund melden sich per Mail, und die
Claude-Anbindung funktioniert wieder.

### Vor dem Update lesen

- **Image-Tag** in der Compose-Datei von `0.3` auf `0.4` ändern. Die Datenbank wird beim Start automatisch
  migriert (zwei kleine Felder an wiederkehrenden Rechnungen).
- **Claude-Konnektor**: Ging die Verbindung mit „Invalid client_id“ nicht mehr, nach dem Update den Konnektor in
  Claude entfernen und neu hinzufügen.
- **Admin-Mails**: Für die neuen Fehlermeldungen brauchen die Admins eine E-Mail-Adresse und SMTP muss eingerichtet
  sein.

### Neu

- **Wiederkehrende Rechnungen automatisch versenden**: optional schreibt der Worker neue Rechnungen sofort fest und
  mailt sie mit den Standardanhängen an den Kunden. Standard bleibt: nur Entwurf. Scheitert der Versand, bleibt die
  Rechnung liegen, wird 7 Tage lang täglich erneut versucht, die Admins werden benachrichtigt und die Liste
  *Wiederkehrend* zeigt „Versand fehlgeschlagen“. Schalter beim Anlegen und in der Liste („Versand“).
- **Fehlermeldungen per Mail an die Admins** bei Backup, wiederkehrenden Rechnungen, Beleg-Postfach und Kontoabruf
  (einmal je Fehlerserie, nicht bei jedem Versuch).

### Behoben

- **Versandstatus an der Rechnung**: Nach dem Senden bestätigt Skontio „E-Mail an … gesendet“, und oben auf der Seite steht
  „Per E-Mail versendet am … um … Uhr an …“. Ein fehlgeschlagener Versand erscheint dort mit Fehlertext, eine nie
  versendete Rechnung mit „Noch nicht per E-Mail versendet“. Vorher stand das nur im eingeklappten Versandprotokoll.
- **MCP-Verbindung mit Claude** ließ sich nicht mehr herstellen: Claudes Client-Dokument nennt jetzt zusätzlich den
  Grant-Typ `jwt-bearer`, was zu „Invalid client_id“ führte. Skontio ignoriert Grant-Typen, die es nicht anbietet.

### Sicherheit

- `oauthlib` 4.0.1 (CVE-2026-49272); Debian-Sicherheitsupdates (`apt`, `zlib`) werden beim Bau des Images eingespielt.

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

**Umzug und KI**
- Umzug aus Papierkram: CSV-Export (ZIP) übernehmen – Kontakte, Leistungen, Rechnungen und Angebote als
  Altbelege mit Originalnummer, Zahlungen, Bankkonten und -umsätze; Probelauf, alles oder nichts, Nummernkreise
  laufen weiter (*Einstellungen → Umzug aus Papierkram* oder `skontio import_papierkram`)
- MCP: Kunden ändern (`customer_update`), Entwürfe auflisten und ungeprüfte KI-Entwürfe verwerfen, offene
  Bankumsätze mit Zuordnungsvorschlägen lesen – Festschreiben, Versenden und Buchen bleiben bei dir

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
