# ADR 0001 – Ergebnisse der Phase-0-Spikes

Status: angenommen · Datum: 2026-10-11

## 1. Nummernkreise und Festschreibung

**Entscheidung:** Die Nummer wird beim Festschreiben in derselben Transaktion vergeben.
- **SQLite:** `transaction_mode=IMMEDIATE` (Django ≥ 5.1) plus WAL und `busy_timeout`.
- **Postgres:** `SELECT … FOR UPDATE` auf die Zeile des Nummernkreises.

Unveränderbarkeit wird doppelt erzwungen: im ORM (`DocumentLocked`) und durch **Datenbank-Trigger ohne Umgehungsweg** (Migration `documents/0002`).

**Belege:**
- 50 parallele Vergaben ergeben 1…50 ohne Lücken und ohne Duplikate (SQLite und Postgres).
- 20 parallele Festschreibungen ergeben 1…20 (Postgres).
- Direktes SQL `UPDATE`, `DELETE` und `INSERT` auf festgeschriebene Dokumente und Positionen schlägt fehl.

**Befunde:**
- Die In-Memory-Testdatenbank von SQLite (Shared Cache) nutzt Tabellen-Locks, die `busy_timeout` ignorieren. Die Testdatenbank liegt deshalb in einer Datei, wie im Betrieb.
- Djangos Test-Teardown leert SQLite per `DELETE`, und das verweigern die Trigger. Der parallele Festschreibe-Test läuft daher nur unter Postgres (dort TRUNCATE). Die Parallelität unter SQLite deckt der Nummernkreis-Test ab.
- Rückwirkende Nummernvergabe in ein bereits begonnenes neues Jahr (bei jährlichem Zurücksetzen) wird abgelehnt.

**Entschieden (2026-10-11): Nach Ablauf der Aufbewahrungsfrist wird nicht gelöscht, sondern geschwärzt.**
Festgeschriebene Dokumente und Positionen werden nie physisch gelöscht; die Trigger bleiben ohne Ausnahme.
Nach Fristablauf (8 Jahre für Belege, 10 Jahre für Aufzeichnungen, jeweils ab Ende des Kalenderjahres)
werden personenbezogene Daten (DSGVO Art. 17) entfernt:
- Archivdateien (PDF/XML) und Anhänge liegen außerhalb der gesperrten Tabellen und werden gelöscht
- Kundendaten in den Stammdaten werden anonymisiert
- Snapshot-Felder im Dokument bleiben gesperrt

Daher sollen personenbezogene Snapshot-Daten ab Phase 1a in einer **separaten, nicht gesperrten
Tabelle** liegen. Das Dokument verweist nur über einen Hash darauf, sodass Schwärzen ohne Änderung
gesperrter Zeilen möglich ist. Die Hash-Kette des Audit-Logs belegt die Integrität des Originals.
Abgelehnte Alternativen: (a) Trigger temporär entfernen; (c) Freigabe in der Datenbank (vom
Auto-Mode-Classifier als Abschwächung der Festschreibung abgelehnt).

## 2. E-Rechnung (EN 16931)

**Entscheidung:**
- XML mit **drafthorse** (CII)
- PDF mit **WeasyPrint** (`pdf_variant="pdf/a-3b"`)
- Einbetten mit **factur-x** (`generate_from_binary`, AFRelationship `Data`, XMP-Erweiterungsschema)
- Prüfung in der CI mit **KoSIT 1.6.3 + XRechnung-Konfiguration 3.0.2 (2026-08-31)** und **Mustang 2.26.0** (enthält veraPDF)

**Belege:**
- XRechnung (CII) für einen Kleinunternehmer: KoSIT **accept**
- ZUGFeRD EN 16931: Mustang **valid**, veraPDF **PDF/A-3b konform** (613–644 Prüfungen), 75 XML-Regeln, 0 Fehler
- Auch das im Container erzeugte PDF (andere Schriften) ist PDF/A-3b-konform

**Fachliche Befunde aus den Validatoren:**
- **BR-CO-26:** Der Verkäufer braucht eine USt-IdNr. (BT-31), eine Registernummer (BT-30) oder eine **Verkäuferkennung (BT-29)**. Die Steuernummer (BT-32) reicht **nicht**. Für Kleinunternehmer ohne USt-IdNr. ist BT-29 nötig. → Empfehlung im Einrichtungsassistenten: USt-IdNr. beim BZSt beantragen (auch für Kleinunternehmer möglich); sonst BT-29 belegen.
- **FX-SCH-A-000570:** Beim Käufer ist nur eine USt-IdNr. (`VA`) erlaubt, keine Steuernummer.
- **PEPPOL-EN16931-R008:** Ein leeres Lieferelement erzeugt eine Warnung. Das Leistungsdatum ist ohnehin Pflicht (§ 14 Abs. 4 Nr. 6 UStG) und wird jetzt erzwungen.
- `factur-x` kann XML selbst erzeugen, braucht für Schematron aber einen Saxon-Server; drafthorse bleibt daher der Writer.

## 3. OAuth für MCP

**Entscheidung:** **django-oauth-toolkit 3.4** als Autorisierungsserver. Es bringt mit:
- RFC 8707
- RFC 9728 (`/.well-known/oauth-protected-resource`)
- RFC 8414
- PKCE als Pflicht
- rotierende Refresh-Tokens
- Speicherung von Tokens nur als Hash (RFC 9700)
- Client ID Metadata Documents mit SSRF-sicherem Fetcher

Authlib als Ausweichlösung wird nicht gebraucht.

**Wichtiger Befund:** Das Toolkit behandelt Tokens **ohne `resource`** als uneingeschränkt. `skontio.mcp.auth` lehnt solche Tokens ab und verlangt exakte Übereinstimmung mit `https://<host>/mcp` (eigener Validator statt URL-Präfix). Session-Cookies authentisieren `/mcp` nie. Alles ist durch Tests abgedeckt.

## 4. OIDC zu Pocket ID

`skontio doctor oidc` prüft Discovery, Issuer, PKCE S256, JWKS, `groups`-Claim und Zeitabweichung.
Gegen `https://id.example.org` (vom Entwicklungsrechner aus, 2026-10-11): **6/6 OK**.

Pocket ID bietet auch `plain`-PKCE an; Skontio verwendet ausschließlich S256. Die Prüfung vom TrueNAS aus (NAT-Loopback) folgt beim Deployment.

## 5. Container

Python 3.13-slim, Granian (`asginl`), UID/GID 568, read-only Dateisystem mit tmpfs für `/tmp`, `cap_drop: ALL`, `no-new-privileges`; Healthcheck `/healthz`. Image-Größe ca. 354 MB.

Fontconfig braucht bei read-only `XDG_CACHE_HOME=/tmp`.
