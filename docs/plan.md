# Skontio – Gesamtplan

Stand: 2026-10-11 · zusammengeführt aus den Planfassungen v1–v6 · bestätigt am 2026-10-11

Skontio ist ein Open-Source-Rechnungs- und CRM-Programm für Selbstständige. Es ist selbst hostbar und
eine Alternative zu Papierkram und sevdesk. Alle Funktionen sind frei, auch eigenes Logo, mehrere
Vorlagen und Hosting für Dritte.

> **Keine Steuer- oder Rechtsberatung.** Skontio unterstützt die Einhaltung von GoBD, UStG und AO.
> Die Verantwortung bleibt bei den Nutzerinnen und Nutzern. Rechtliche Aussagen in diesem Plan sind
> Arbeitsannahmen mit Quellenangabe. Was als „zu verifizieren“ markiert ist, wird in Phase 0
> geprüft und in `docs/legal/sources.md` dokumentiert.

---

## 1. Entscheidungen

| Thema | Entscheidung |
|---|---|
| Name | **Skontio** (Repo `skontio/skontio`, Images `ghcr.io/skontio/skontio` und `docker.io/skontio/skontio`, PyPI `skontio` und `skontio-mcp`, beide am 2026-10-11 frei) |
| Markt | Deutschland zuerst; das Datenmodell ist EU-fähig (Länderpakete) |
| Lizenz | **AGPL-3.0-or-later**, Beiträge nach dem Developer Certificate of Origin (DCO), Vorlagen und Doku unter CC BY 4.0 |
| Stack | **Python 3.13 und Django 6**, Django Ninja (Pydantic), HTMX, Alpine.js, Tailwind, Granian (ASGI) |
| Datenbank | SQLite (Standard) oder Postgres |
| Zielnutzer | Einzelunternehmer mit **Nebengewerbe**, derzeit **Kleinunternehmer (§ 19 UStG)**; der Wechsel zur Regelbesteuerung ist eingebaut |
| Besteuerungsart | Ist- und Sollversteuerung, **Standard: Istversteuerung** |
| Identity Provider | **Pocket ID** unter `https://id.example.org` (auf einer Synology), Login per Passkey |
| Betrieb | Docker auf **TrueNAS** (Mini-PC mit Intel N150), **Caddy als TrueNAS-App**, öffentlich unter `https://skontio.example.org` |
| MCP | Streamable HTTP, öffentlich; Skontio ist selbst OAuth-Autorisierungsserver, der Login läuft über Pocket ID |
| Claude-Tarif | Pro, also ohne Auftragsverarbeitungsvertrag → minimale Feldfreigabe, **Pseudonym-Modus standardmäßig an**, Notizen standardmäßig aus |
| LLM | Schnittstelle jetzt bauen (`none`, `ollama`, `openai_compatible`, `anthropic`); Ollama wird später angebunden |
| Mobil | **PWA**, auf Android installierbar |
| ERiC | Später, optional als Sidecar |
| Hosting für Dritte | Später; vorher ZAG und Auftragsverarbeitung prüfen |

---

## 2. Geprüfte Fakten (Stand 2026-10)

| Thema | Ergebnis | Quelle |
|---|---|---|
| Invoice Ninja v5 | Elastic License 2.0, nicht von der OSI anerkannt, kein Hosting für Dritte erlaubt | LICENSE im Repo `invoiceninja/invoiceninja` |
| Andere Programme | Akaunting: BSL · InvoiceShelf: AGPL-3.0 · Dolibarr: GPL-3.0 · ERPNext: GPL-3.0 · InvoicePlane: MIT mit Markenvorbehalt | GitHub-API, LICENSE-Dateien |
| N26 | Kein FinTS/HBCI; Zugang nur über PSD2-Aggregatoren | Outbank-Hilfe, DATEV-Community |
| PSD2-Einwilligung | Seit 25.07.2023 alle **180 Tage** statt 90 (Delegierte Verordnung (EU) 2022/2360) | EBA |
| GoCardless Bank Account Data | Keine Neukunden seit Juli 2025 → **Enable Banking** als Referenz prüfen | Actual-Budget-Doku, Invoice-Ninja-Forum |
| Aufbewahrung | § 147 Abs. 3 AO: Bücher **10 Jahre**, Buchungsbelege bzw. Rechnungen **8 Jahre** (Viertes Bürokratieentlastungsgesetz, seit 01.01.2025), Geschäftsbriefe **6 Jahre** | gesetze-im-internet.de |
| E-Rechnung für Kleinunternehmer | § 34a UStDV: Rechnungen dürfen **stets als sonstige Rechnung** (Papier oder PDF) übermittelt werden; die **Empfangspflicht gilt trotzdem** | gesetze-im-internet.de |
| Ausstellungspflicht | Ab 01.01.2027 bei mehr als 800.000 € Vorjahresumsatz, ab 01.01.2028 für alle; ZUGFeRD MINIMUM und BASIC-WL zählen nicht | BMF-Schreiben vom 15.10.2025 (laut IHK) – **zu verifizieren** |
| Kleinunternehmergrenzen | Vorjahr ≤ 25.000 €, laufendes Jahr ≤ 100.000 €, gerechnet nach vereinnahmten Entgelten; der Verzicht bindet 5 Jahre | § 19 UStG |
| Überschreiten im laufenden Jahr | Steuerpflicht ab dem Umsatz, mit dem die Grenze überschritten wird (BMF vom 18.03.2025) | **zu verifizieren** |
| ERiC | Proprietär, Lizenzvertrag mit dem Freistaat Bayern, Registrierung als Entwickler nötig → **nicht im AGPL-Image ausliefern** | elster.de |
| MCP-Autorisierung | Spezifikation 2026-07-28: OAuth 2.1 mit PKCE, Pflicht für RFC 9728 und RFC 8707, Audience-Prüfung, keine Token-Weiterreichung, Client ID Metadata Documents empfohlen, Dynamic Client Registration veraltet | modelcontextprotocol.io |
| Claude-Connectors | Verbinden sich **aus Anthropics Cloud**; der Server muss öffentlich erreichbar sein (Allowlist der Anthropic-IP-Bereiche) | support.claude.com |

---

## 3. Positionierung

Es gibt kein Programm, das alles zugleich bietet: **OSI-Lizenz, leichtgewichtig für Solo-Selbstständige,
Deutschland als Kernmarkt, GoBD-orientierte Festschreibung, EN 16931 beim Erzeugen und Einlesen,
DATEV-Export, OIDC und einen einzigen Container.** Die Positionierung lautet: *„Alles frei, auch fürs
Hosting, und auf deutsches Recht ausgerichtet.“* Verbesserungen an Bibliotheken wie drafthorse und
factur-x fließen an die jeweiligen Projekte zurück.

---

## 4. Architektur

```
┌──────────────────── Image skontio/skontio ────────────────────┐
│ web (Granian ASGI: UI + /api + /mcp + /oauth)   worker (Tasks) │
│ core · auth(oidc, local, mfa, oauth_as) · audit · archive      │
│ crm · catalog · documents · numbering · taxstatus · tax/{engine,de}
│ einvoice · rendering · mail · payments · pwa                    │
│ tools (eine Registry, Pydantic) → mcp · assistant(LLMProvider)  │
│ P2: expenses · dunning · recurring · ledger(EÜR) · vat(UStVA)   │
│     reports · export(datev, csv) · bankcsv · quickentry         │
│ P3: banking/adapters · reconcile · reserves · assets · portal ·  │
│     api · webhooks · projects · timetracking                    │
└─────────────────────────────────────────────────────────────────┘
Optional: skontio-validator (KoSIT, Java) · skontio-elster (ERiC) · skontio-mcp (stdio-Bridge)
```

**Prinzipien:**
- **Hexagonal** an den Außengrenzen: Bank, Speicher, Mail, LLM und Validator sitzen hinter Ports.
- **Mandantenfähigkeit:** `company_id` auf jeder fachlichen Tabelle.
- **Steuer-Engine als Länderpaket:** `TaxRegime` → `TaxCategory` (Codes nach EN 16931: S, Z, E, AE, K, G, O) → `TaxRate` mit Gültigkeitszeitraum. Artikel haben eine **abstrakte Steuerbehandlung**.
- **Festschreibung auf drei Ebenen:**
  1. Status `draft → finalized`
  2. Datenbank-Trigger gegen UPDATE und DELETE
  3. Inhaltsadressierte Ablage (SHA-256) und hashverkettetes Audit-Log
- **Nummernkreise:** Die Nummer wird erst beim Festschreiben vergeben, in derselben Transaktion → lückenlos und eindeutig.
- **Korrekturen** nur über Storno bzw. Rechnungskorrektur, nicht als „Gutschrift“ bezeichnet (§ 14 Abs. 2 S. 5 UStG).
- **Snapshots:** Kunden-, Verkäufer- und Artikeldaten werden beim Festschreiben ins Dokument kopiert.
  Personenbezogene Daten liegen in einer eigenen, nicht gesperrten Snapshot-Tabelle (per Hash referenziert).
- **Nach Ablauf der Aufbewahrungsfrist:** nicht löschen, sondern schwärzen (Archivdateien löschen,
  personenbezogene Snapshot-Daten anonymisieren). Festgeschriebene Zeilen werden nie physisch gelöscht (ADR 0001).

---

## 5. Datenmodell (Kern)

```
Company(id, name, legal_form, activity[trade|freelance], side_business:bool, tax_number, vat_id?,
        small_business_id?, country, iban, bic, vat_filing_period, permanent_extension, cash_accounting)
TaxStatusPeriod(company, regime[small_business|standard], valid_from, valid_to?, reason, option_binding_until?,
                confirmed_by, confirmed_at)
ThresholdRule(code, prev_year_limit, current_year_limit, founding_year_limit?, valid_from, source_url, verified_at)
User · Membership(user, company, role) · OidcIdentity(user, issuer, subject)
AuditEvent(id, company, actor, actor_kind[user|mcp|assistant|api|system], client_id?, tool_name?,
           ai_initiated, action, entity, entity_id, diff_json, request_digest, ts, prev_hash, hash)

Customer(id, company, number, type[person|org], name, vat_id, leitweg_id?, country, lang, payment_terms, tags)
Contact · Address · Activity · FollowUp · Attachment · Tag
ItemCategory(company, name, euer_line_ref?, parent?)
Item(company, kind[service|good], sku?, name, description, unit, net_price, tax_treatment, category, archived_at)
Unit(code UN/ECE Rec 20, label_de, label_en) · TaxRate(regime, category, rate, valid_from, valid_to)

NumberSequence(company, doc_type, pattern, reset[yearly|never], next_value)
Document(id, company, type, status, number?, issue_date, service_date|service_period, due_date,
         valid_until, regime_snapshot, tax_note_text, customer_snapshot, seller_snapshot, currency, lang,
         template_id, texts, parent_id, totals, locked_at, origin[ui|mcp|assistant|api|recurring],
         idempotency_key?, content_fingerprint, ai_review_required)
DocumentLine(document, pos, item_id?, item_snapshot, text, qty, unit, unit_price, discount, tax_treatment,
             resolved_category, resolved_rate, net_amount)
Rendition(document, kind[pdf|cii|ubl|zugferd_pdf], blob, sha256, validator_report)
Blob(id, sha256, size, mime, storage_key, retention_until, legal_hold)
Payment(company, document?, amount, received_on, method, bank_tx?)
MailLog · IncomingEInvoice · Template · Branding · TextTemplate

P2: LedgerEntry · EuerMapping(form_year) · VatReturnPeriod · VatKennzahlMapping(form_year) · Expense ·
    Reminder · PushSubscription · DunningLevel · RecurringInvoice · BankTransaction
P3: ReserveProfile · ReserveComponent(kind, enabled, method, params) · TaxParameterSet(year, source) ·
    Asset · BankConnection · MatchSuggestion · Project · ApiToken · WebhookEndpoint · PortalLink ·
    ElsterCertificate (nur mit Sidecar)
MCP/LLM: McpClient · McpToolPolicy · LlmProviderConfig(kind, base_url, model, api_key_ref, timeout)
```

---

## 6. Fachliche Regeln

### 6.1 Steuerstatus und Wechsel
- Maßgeblich ist das **Leistungsdatum bzw. der Leistungszeitraum**. Liegt ein Zeitraum über dem Stichtag, verlangt Skontio eine Aufteilung oder eine Bestätigung.
- Festgeschriebene Dokumente werden **nie** neu berechnet. Entwürfe werden mit einer Warnung neu berechnet.
- Werden Angebote, die vor dem Wechsel entstanden sind, in Rechnungen umgewandelt, gilt der neue Status; ein Hinweis zeigt die geänderte Bruttosumme.
- Assistent für Rechnungskorrekturen bei rückwirkend erkanntem Überschreiten der Grenze.
- **Schutz vor § 14c UStG:** Solange der Kleinunternehmer-Status gilt, lässt sich kein Steuerausweis festschreiben.
- `ThresholdMonitor`: Zahlungseingänge im Kalenderjahr gegen versionierte Grenzwerte, Warnungen bei 80, 90 und 100 %, Vorschau; der Wechsel wird **nie automatisch** festgelegt.

### 6.2 Leistungskatalog
Eigene Kernentität, abstrakte Steuerbehandlung (bleibt nach dem Wechsel gültig), Übernahme in Dokumente
als Kopie, Archivieren statt Löschen, CSV-Import und -Export.

### 6.3 E-Rechnung
Ausgabe als ZUGFeRD (Profil EN 16931) und XRechnung (CII). Kleinunternehmer erhalten die Steuerkategorie E
mit dem Befreiungsgrund § 19 UStG. Einlesen von CII und UBL mit Viewer und Archiv. In der CI laufen KoSIT,
Mustang und veraPDF.

### 6.4 Steuer-Modul (Phasen 2–3)
- **EÜR:** Journal nach Zufluss- und Abflussprinzip, Gliederung nach der Anlage EÜR (jährlich versionierte Zuordnung), 10-Tage-Regel, beim Kleinunternehmer brutto, beim Wechsel aufgeteilt.
- **Umsatzsteuer-Voranmeldung** (nur bei Regelbesteuerung): Kennzahlen je Steuersatz, Vorsteuer, § 13b, Ist- oder Sollversteuerung, Fristen samt Dauerfristverlängerung, Erinnerungen, Sperre nach Übermittlung.
- **Kleinunternehmer als Empfänger nach § 13b** (z. B. Werbung aus Irland): Skontio erkennt solche Fälle und weist darauf hin.
- **ERiC** optional als Sidecar; Standard ist eine Übersicht mit Kopier-Buttons für Mein ELSTER.
- **Belegarchiv**, **DATEV-Export im Format EXTF Buchungsstapel**, CSV-Export.
- **Rücklagen, frei einstellbar:**

  | Baustein | Methoden |
  |---|---|
  | Einkommensteuer | Fester Prozentsatz · **Mehrsteuer nach Tarif (Hauptjob + Gewinn)** · Vorauszahlungen |
  | Solidaritätszuschlag | Automatisch |
  | Kirchensteuer | 8 oder 9 % |
  | Gewerbesteuer | Hebesatz, Freibetrag, Messzahl, Anrechnung nach § 35 EStG |
  | Kranken- und Pflegeversicherung | Prozentsatz oder Fixbetrag, standardmäßig aus |
  | Altersvorsorge | Prozentsatz oder Fixbetrag |
  | Umsatzsteuer-Zahllast | Automatisch bei Regelbesteuerung |
  | Eigene Bausteine | Frei definierbar |

  Die Parameter werden je Jahr mit Quelle gepflegt. Ausgegeben werden eine monatliche Sparrate und die getroffenen Annahmen, mit dem Hinweis „keine Steuerberatung“.

---

## 7. MCP-Server und KI-Assistent

### 7.1 Betriebsart
- **Streamable HTTP unter `/mcp`.** Skontio ist selbst OAuth-Autorisierungsserver (django-oauth-toolkit mit eigener Unterstützung für RFC 9728 und RFC 8707; Spike in Phase 0). Der Login läuft über Pocket ID.
- Access-Tokens 15 Minuten, rotierende Refresh-Tokens, Audience `https://skontio.example.org/mcp`.
- Zusätzlich eine stdio-Bridge `skontio-mcp` (PyPI, `uvx`) mit einem PAT, das nur MCP-Scopes hat und ein Ablaufdatum trägt.

### 7.2 Tools (nur Entwürfe)

| Tool | Scope |
|---|---|
| `catalog_search`, `catalog_get` | `catalog:read` |
| `catalog_create`, `catalog_update` | `catalog:write` |
| `customer_search`, `customer_get` | `customers:read` |
| `customer_create` | `customers:write` |
| `quote_draft_create`, `quote_draft_update`, `quote_to_invoice_draft`, `invoice_draft_create`, `invoice_draft_update` | `drafts:write` |
| `draft_validate`, `document_preview_link` | `drafts:read` |
| `invoices_list`, `invoice_get`, `open_items` | `documents:read` |
| `revenue_summary`, `euer_summary`, `small_business_status` | `reports:read` |

**Nie über MCP:** Freigeben, Festschreiben, Versenden, Löschen, Stornieren, Zahlungen buchen,
Einstellungen, Steuerstatus ändern.

### 7.3 Regeln
- Steuer, Rundung, Summen und Pflichtangaben berechnet und prüft **der Server**. Vom Modell gelieferte Summen werden ignoriert. Fehlende Angaben führen zu einem strukturierten Fehler mit Hinweis.
- **Dubletten:** Pflichtparameter `idempotency_key` und Inhalts-Fingerabdruck über 10 Minuten.
- **Datenschutz:** Feldfilter als Allowlist, standardmäßig nur Nummer, Name und Land; **Pseudonym-Modus für Privatkunden an**; Notizen nur über den Scope `notes:read` (standardmäßig aus); IBAN nie.
- **Prompt Injection:** Freitexte werden als `untrusted_text` markiert und gekürzt; es gibt keine Tools mit Wirkung nach außen.
- **Audit:** `actor_kind=mcp|assistant`, `ai_initiated=true`, Digest der Anfrage statt Klartext.
- Entwürfe aus MCP sind als „KI-Entwurf, bitte prüfen“ markiert; vor dem Festschreiben ist eine menschliche Prüfbestätigung Pflicht.
- **Auftragsverarbeitung:** Für Claude Pro gibt es keinen Auftragsverarbeitungsvertrag. Ein Hinweis im Einrichtungsdialog nennt die Alternativen: Ollama lokal oder die Anthropic API.

### 7.4 KI-Assistent (Phase 2)
- Dieselbe Tool-Registry wie MCP. Port `LLMProvider` mit den Adaptern `none`, `ollama`, `openai_compatible` und `anthropic` (pydantic-ai).
- Konfiguration über die Oberfläche oder `SKONTIO_LLM_*`, mit Verbindungstest (Modelle, Tool-Fähigkeit, Latenz).
- Anfragen als Hintergrund-Job mit Fortschrittsanzeige (Timeout 120 s wegen des N150); Vorschau vor dem Anlegen eines Entwurfs; Rückfrage bei mehreren Treffern.
- Tests mit einem deterministischen Fake-LLM; Eval-Suite über `skontio eval assistant --model …`.

---

## 8. PWA (Android)

| Funktion | Phase |
|---|---|
| Installierbar (Manifest, maskable Icons, Service Worker nur für das App-Gerüst), Mobile-first-Layouts | 1a |
| PDF teilen (Web Share API) | 1b |
| Belegfoto, „Teilen an Skontio“ (Web Share Target), Offline-Warteschlange, Web Push, **Schnellerfassung** | 2 |
| Sprache über die Tastatur in den Assistenten | 2–3 |
| APK über eine Trusted Web Activity | Später |

**Sicherheit:** Keine personenbezogenen Daten im Cache des Service Workers; beim Logout werden Cache und
IndexedDB geleert; Passkey-Login über Pocket ID. **Spike:** OIDC-Redirect aus der installierten PWA.

---

## 9. Betrieb: TrueNAS, Caddy, Pocket ID

```
Internet ─► Router ─► Caddy (TrueNAS-App) ─► <NAS-LAN-IP>:8085 ─► skontio (TrueNAS Custom App)
Browser/Handy ◄─ Login ─► id.example.org (Synology)
skontio ─ Discovery/JWKS/Token (Server-zu-Server) ─► id.example.org
Anthropic-Cloud ─► /mcp (Caddy: nur Anthropic-IP-Bereiche + LAN)
```

- **Custom App über „Install via YAML“**, Services `skontio` (Web) und `skontio-worker` aus demselben Image, `user: "568:568"`.
- **Host-Path-Datasets:** `data`, `archive`, `backups`; der Master-Key liegt getrennt davon.
- **Proxy:** Port nur auf der LAN-IP. `SKONTIO_TRUSTED_PROXIES` enthält das Docker-Bridge-Netz; `skontio doctor proxy` zeigt, was ankommt.
- **OIDC:** `SKONTIO_OIDC_ISSUER=https://id.example.org`. Fehlt NAT-Loopback, hilft Split-DNS; `skontio doctor oidc`; NTP auf allen Geräten.
- **Caddyfile-Vorlage:**
  - `/mcp*` nur für Anthropics IP-Bereiche und das LAN
  - OAuth, Auth und Oberfläche öffentlich
  - HSTS
  - Rate-Limits in Skontio
- Oberfläche öffentlich: Login nur über den Passkey in Pocket ID; lokaler Notfall-Login nur aus dem LAN; erneute Anmeldung für Admin-Bereiche.
- **Backups:** nächtliches konsistentes App-Backup, ZFS-Snapshots, Replikation nach extern; Warnung, wenn das letzte Backup älter als 48 Stunden ist.
- **Updates:** feste Minor-Tags; vor der Migration sichert Skontio automatisch.
- Auf dem NAS läuft kein KoSIT-Validator, er läuft nur in der CI.

---

## 10. Sicherheitskonzept

- **OIDC:** Authorization Code mit PKCE, Prüfung von state, nonce, iss, aud und exp, Zuordnung über `iss`+`sub`; Gruppen-Claims werden auf Rollen abgebildet (`skontio`, `skontio-admin`).
- **Lokaler Login:** Argon2id, TOTP oder WebAuthn als Pflicht, nur aus dem LAN, Break-Glass-Zugang über die CLI.
- **Sitzungen:** serverseitig, Cookies `Secure`, `HttpOnly`, `SameSite=Lax`, CSRF-Schutz, Content-Security-Policy.
- **OAuth-Autorisierungsserver:** RFC 9728 und RFC 8414 mit `iss` (RFC 9207); `resource` ist Pflicht; nur selbst ausgestellte Tokens werden akzeptiert; Zustimmungsbildschirm; Widerruf je Client; exakter Abgleich der Redirect-URIs; Client ID Metadata Documents und vorab registrierte Clients, Dynamic Client Registration standardmäßig aus.
- **PAT:** gehasht, mit Präfix `skt_`, höchstens 90 Tage gültig, nur MCP-Scopes.
- **Geheimnisse und Bankdaten:** Envelope Encryption mit einem Master-Key aus einer Datei bzw. einem Docker-Secret (AES-256-GCM); keine Bank-PIN; FinTS-PIN wird bei jedem Abruf abgefragt; ELSTER-PIN nie gespeichert.
- **Audit:** hashverkettet, KI-Aktionen markiert.
- **Uploads:** MIME-Allowlist, SSRF-Schutz für SMTP-, Webhook- und LLM-URLs (konfigurierbare Allowlist; das LAN für Ollama ist ausdrücklich erlaubt).

---

## 11. Teststrategie

| Bereich | Methode |
|---|---|
| Steuer-Engine | Tabellengesteuerte Tests und Hypothesis: Rundung nach BR-CO-10 bis 17, gemischte Sätze, Rabatte |
| Steuerstatus | Stichtags-Matrix; Eigenschaft: Kein festgeschriebenes Dokument ändert sich |
| Grenzwerte | Genau auf, knapp unter und knapp über der Grenze; Jahreswechsel; Teilzahlungen |
| Nummernkreise | 50 parallele Festschreibungen unter SQLite und Postgres → keine Lücken, keine Duplikate |
| Festschreibung | Direktes SQL-UPDATE und DELETE schlagen fehl; die Hash-Kette erkennt Manipulation |
| E-Rechnung | KoSIT (XRechnung-Konfiguration), Mustang, veraPDF in der CI; Import-Testsuites |
| EÜR, Voranmeldung, Rücklagen | Golden Files je Jahr; Abgleich mit dem BMF-Rechner; Monotonie |
| MCP und Assistent | Contract-Tests; Negativtests (kein Festschreiben, verbotene Felder); Prompt-Injection-Korpus; Fake-LLM; Eval-Suite |
| Auth | Testcontainer mit Pocket ID und Keycloak; OAuth-Konformität |
| PWA | Lighthouse-Gate, Playwright mit Android-Viewport, kein Cache mit personenbezogenen Daten |
| Betrieb | Backup und Restore, Migrationen SQLite/PG, Upgrade N-1→N, TrueNAS-Compose als 568:568 |

Abdeckung mindestens 80 %; 100 % der Zweige in `tax`, `numbering`, `documents.lock`, `einvoice` und `taxstatus`.

---

## 12. Phasenplan

| Phase | Inhalt | Aufwand (Personenwochen) |
|---|---|---|
| **0 Fundament** | Repo, Lizenz, CI, Docker und TrueNAS-Compose, Caddyfile; **Spikes:** PDF/A-3 + Factur-X (WeasyPrint, factur-x) gegen veraPDF und KoSIT, Festschreibe-Trigger und Nummernkreis unter SQLite/PG, OAuth mit RFC 8707 (django-oauth-toolkit), OIDC zu Pocket ID inklusive `doctor`; rechtliche Verifikationen | 3–4 |
| **1a MVP** | Steuer-Engine mit Steuerstatus und Wechsel, Nummernkreise, Festschreibung, Katalog, CRM-Basis, Angebot → Rechnung, Storno, Branding-PDF, GiroCode, ZUGFeRD/XRechnung, SMTP, E-Rechnungs-Eingang, **Zahlungserfassung und Grenzwertprüfung**, OIDC/lokaler Login, SQLite/PG, Backup, **installierbare PWA**, Deployment | 13–17 |
| **1b** | MCP über das Internet (OAuth-Autorisierungsserver, Feldfilter, Pseudonyme, Audit), stdio-Bridge, Korrektur-Assistent, Auftragsbestätigung, Abschlags- und Schlussrechnung, Vorlagen, Mehrsprachigkeit, UBL-Import, Aktivitäten, Wiedervorlagen, Dokumentenablage, 2FA, Verfahrensdokumentation, PDF teilen | 10–12 |
| **2** | Ausgaben mit Belegen (PWA-Kamera, Share Target, Offline-Warteschlange), § 13b, EÜR, Umsatzsteuer-Voranmeldung, Belegarchiv, DATEV/CSV, Mahnwesen, wiederkehrende Rechnungen, Bank-CSV, Web Push, **Schnellerfassung**, **LLM-Schnittstelle und Assistent**, MCP-Auswertungs-Tools, mehrere Firmen | 17–21 |
| **3** | PSD2 (Enable Banking) und FinTS, Abgleich, **Rücklagen**, Anlagen und AfA, Portal, Projekte, Zeiterfassung, REST-API, Webhooks; ERiC optional | 15–20 |
| Später | Hosting für Dritte, TWA/APK, Peppol/B2G | – |

---

## 13. Risiken

| Risiko | Stufe | Gegenmaßnahme |
|---|---|---|
| Abschlags- und Schlussrechnung in EN 16931 | Hoch | Spike, KoSIT, Abstimmung mit Steuerberatung |
| Fehler im Steuerstatus oder bei den Grenzwerten | Hoch | Kein automatischer Wechsel, Warnungen, Property-Tests |
| Rechts- und Formularänderungen | Hoch | Versionierte Tabellen, Release-Checkliste |
| Umfang (Solo-Projekt) | Hoch | MVP-Schnitt |
| Öffentlicher Endpunkt auf dem NAS | Mittel | Pfadregeln, IP-Allowlist, nur Entwürfe, Rate-Limits |
| Kundendaten bei Claude Pro | Mittel | Pseudonym-Modus, Feldfilter, Ollama als Alternative |
| RFC 8707 im django-oauth-toolkit | Mittel | Spike, Alternative Authlib |
| ERiC-Lizenz | Mittel | Sidecar, Mein-ELSTER-Übersicht |
| Hairpin-NAT, Zeitabweichung | Niedrig | `skontio doctor`, Split-DNS, NTP |
| Marke „Skontio“ | Mittel | TMview/DPMA-Recherche vor dem öffentlichen Launch |

---

## 14. Offene Punkte (blockieren nicht)

- RAM des N150 (wichtig erst für Ollama auf dem NAS)
- Split-DNS oder NAT-Loopback (wird beim Deployment mit `skontio doctor oidc` geklärt)
- Markenrecherche „Skontio“ (TMview, DPMA, WIPO; Klassen 9, 35, 36, 42)
- Lizenzanfrage ERiC beim Bayerischen Landesamt für Steuern (später)
