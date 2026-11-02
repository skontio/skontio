# Betrieb auf TrueNAS

Zielbild: Skontio als Custom App auf TrueNAS, Caddy (TrueNAS-App) als Reverse Proxy,
Login über Pocket ID, erreichbar unter `https://skontio.example.org`.

## 1. Datasets und Geheimnisse

Datasets anlegen (Beispiel-Pool `tank`) und dem Benutzer `apps` (UID/GID 568) Schreibrechte geben:

| Dataset | Inhalt | Rechte |
|---|---|---|
| `tank/apps/skontio/data` | SQLite-Datenbank | 568:568, rw |
| `tank/apps/skontio/archive` | festgeschriebene PDFs/XML, Eingangsrechnungen (8–10 Jahre) | 568:568, rw |
| `tank/apps/skontio/backups` | nächtliche Backups | 568:568, rw |
| `tank/apps/skontio-secrets` | Geheimnisse (Dateien) | 568:568, **nur lesen** |

Im Dataset `skontio-secrets` je eine Datei anlegen (ohne Zeilenumbruch am Ende):

```sh
openssl rand -base64 48 | tr -d '\n' > secret_key        # Django-Schlüssel
printf '%s' 'CLIENT-SECRET-AUS-POCKET-ID' > oidc_client_secret
printf '%s' 'SMTP-PASSWORT' > smtp_password
printf '%s' 'IMAP-PASSWORT' > imap_password            # nur für das Beleg-Postfach (optional)
# cp ~/Downloads/<app-id>.pem enablebanking.pem          # nur für den Kontoabruf (optional)
chmod 0400 *
```

**Eine Kopie dieser Dateien gehört in den Passwort-Manager.** Ohne `secret_key` sind Sitzungen und
künftig verschlüsselte Zugangsdaten nach einer Wiederherstellung nicht mehr nutzbar.

## 2. Pocket ID

In Pocket ID einen OIDC-Client anlegen:

- Name: `Skontio`
- Callback-URL: `https://skontio.example.org/auth/callback`
- Logout-Callback: `https://skontio.example.org/`
- PKCE: aktiv
- Erlaubte Gruppen: `skontio` (Zugriff), `skontio-admin` (Einstellungen) und optional `skontio-entwurf`

Dich selbst in `skontio` und `skontio-admin` aufnehmen.

| Gruppe | Rolle | Darf |
|---|---|---|
| `skontio` | Mitarbeit | alles außer Einstellungen |
| `skontio-entwurf` | Nur Entwürfe | Kunden, Leistungen, Ausgaben und Entwürfe anlegen; **nicht** festschreiben, versenden, stornieren, Zahlungen buchen, Bank |
| `skontio-admin` | Admin | zusätzlich Einstellungen, Steuerstatus, KI-Zugriff; wird nie eingeschränkt |

`skontio-entwurf` schränkt auch dann ein, wenn jemand zusätzlich in `skontio` ist. Die Namen lassen sich mit
`SKONTIO_OIDC_REQUIRED_GROUP`, `SKONTIO_OIDC_ADMIN_GROUP` und `SKONTIO_OIDC_DRAFTS_GROUP` ändern. Lokale
Benutzer bekommen die Rolle über die Gruppe „Nur Entwürfe“ im Django-Admin. Client-ID in die Compose-Datei, Client-Secret in `oidc_client_secret`.

## 3. App installieren

`deploy/truenas/compose.yaml` über *Apps → Discover → Install via YAML* einspielen; `POOL`, `NAS_LAN_IP`,
`SKONTIO_OIDC_CLIENT_ID` und die SMTP-Werte anpassen. Beim ersten Start legt Skontio die Datenbank an.

## 4. Caddy

`deploy/caddy/Caddyfile` in die Caddy-App übernehmen (`NAS_LAN_IP` ersetzen). `/mcp` ist nur für
Anthropics Netz `160.79.104.0/21` und private Netze freigegeben; die IP-Bereiche gelegentlich prüfen:
<https://platform.claude.com/docs/en/api/ip-addresses>.

## 5. Prüfen

```sh
docker exec -it <skontio-container> skontio doctor oidc     # Pocket ID erreichbar, Issuer, PKCE, Uhrzeit
docker exec -it <skontio-container> skontio doctor smtp     # SMTP-Anmeldung (es wird nichts versendet)
docker exec -it <skontio-container> skontio doctor backup   # Backup-Verzeichnis, bei Postgres pg_dump-Version
docker exec -it <skontio-container> skontio doctor worker   # letzte Läufe von Backup und wiederkehrenden Rechnungen
```

Scheitert `discovery`, kann der Router vermutlich kein NAT-Loopback: im lokalen DNS die Domain
`id.example.org` auf die interne IP der Synology zeigen lassen (Split-DNS).

Danach `https://skontio.example.org` öffnen, mit dem Passkey anmelden und die Einrichtung ausfüllen.

**Notfall-Login ohne Pocket ID:** Konto anlegen mit `docker exec -it <container> skontio createsuperuser`,
dann den zweiten Faktor einrichten – entweder nach der Anmeldung über Pocket ID unter *Einstellungen →
Sicherheit* oder per `docker exec -it <container> skontio totp setup <Benutzer>` (zeigt Schlüssel und
Wiederherstellungscodes; beides in den Passwort-Manager). Danach **aus dem Heimnetz**
`https://skontio.example.org/auth/local/` öffnen: Passwort, dann Code aus der App. Von außen ist dieser
Login gesperrt (Skontio erkennt die echte Client-IP über Caddy; `SKONTIO_TRUSTED_PROXIES` muss stimmen).
Das TOTP-Geheimnis liegt mit `secret_key` verschlüsselt in der Datenbank – ohne diese Datei ist es nach
einer Wiederherstellung unbrauchbar (dann `skontio totp disable <Benutzer>` und neu einrichten).

## 5a. Claude über MCP verbinden

MCP-Adresse: `https://skontio.example.org/mcp` (Protokoll 2026-07-28, ältere Clients mit `initialize`
werden ebenfalls bedient). In Claude: *Einstellungen → Konnektoren → Eigenen Konnektor hinzufügen* und die
Adresse eintragen. Claude meldet sich per OAuth an; Skontio zeigt eine Zustimmungsseite (nur für Admins) mit
den angefragten Berechtigungen.

- **Registrierung:** Claude kann sich über ein Client-ID-Metadatendokument selbst ausweisen (erlaubt sind nur
  Adressen unter `claude.ai`/`claude.com`, änderbar mit `SKONTIO_MCP_CIMD_HOSTS`). Klappt das nicht, Client
  vorab anlegen und die ausgegebene Client-ID in Claudes erweiterten Einstellungen eintragen:
  `docker exec -it <container> skontio mcp_client --name Claude --redirect-uri https://claude.ai/api/mcp/auth_callback`
- **Was Claude darf:** Kunden und Leistungen suchen/anlegen, Angebots- und Rechnungs**entwürfe** anlegen und
  ändern, Rechnungen und offene Posten lesen, Umsatz und Kleinunternehmergrenze abfragen. Festschreiben,
  Versenden, Stornieren, Löschen und Zahlungen buchen geht nur in Skontio selbst. KI-Entwürfe brauchen vor dem
  Festschreiben deine Prüfbestätigung.
- **Datenschutz:** Privatkunden werden als „Privatperson K-…“ übermittelt (`SKONTIO_MCP_PSEUDONYMISE_PERSONS`),
  Kontaktdaten, Anschriften und IBAN nie. Für Claude Pro gibt es keinen Auftragsverarbeitungsvertrag.
- **Kontrolle:** *Einstellungen → KI-Zugriff* zeigt verbundene Clients, die letzten KI-Aktionen und erlaubt den
  Widerruf. Jede KI-Aktion steht im hashverketteten Änderungsprotokoll (`skontio verify_audit`).
- Caddy lässt `/mcp` nur aus Anthropics Netz `160.79.104.0/21` und privaten Netzen zu; `/oauth` und
  `/.well-known` sind öffentlich.

**Claude Desktop und Claude Code (persönlicher Zugangstoken):** *Einstellungen → KI-Zugriff → Neuer
Zugangstoken*, Berechtigungen wählen, höchstens 90 Tage. Der Token (`skt_…`) wird genau einmal angezeigt,
zusammen mit fertigen Einträgen:

- Claude Code spricht HTTP direkt:
  `claude mcp add --transport http skontio https://skontio.example.org/mcp --header "Authorization: Bearer skt_…"`
- Claude Desktop startet die Bridge `skontio-mcp.py` (Download auf derselben Seite, nur Python 3 nötig) mit
  `SKONTIO_URL` und `SKONTIO_TOKEN` in `claude_desktop_config.json`. Die Bridge nimmt nur https an
  (http nur für `localhost`, sonst `--allow-http`). Der Token steht in der Umgebung, nie auf der Kommandozeile.
- Weil Caddy `/mcp` auf private Netze beschränkt, funktioniert das nur im Heimnetz oder über VPN.
- Per Kommandozeile: `skontio mcp_token create <Benutzer> --name "Claude Code" --scope drafts:read --scope drafts:write`,
  `skontio mcp_token list`, `skontio mcp_token revoke skt_Ab12Cd34`. Anlegen und Widerruf stehen im Änderungsprotokoll.

## 5b. Verfahrensdokumentation (GoBD)

*Einstellungen → Firmendaten → Verfahrensdokumentation → Neue Version erstellen* (mit Anlass, z. B.
„Ersterstellung“). Skontio beschreibt darin Belegfluss, Nummernkreise, Steuerstatus, Zugänge, KI-Zugriff,
Archiv, Aufbewahrungsfristen und Sicherung – so, wie sie gerade eingestellt sind – und prüft dabei das
Änderungsprotokoll. Jede Version wird als PDF/A 10 Jahre unveränderbar archiviert. Gelb markierte Abschnitte
(Papierbelege, externe Sicherungskopie, Bankabgleich) selbst ergänzen. Eine neue Version erstellen, wenn sich
Wesentliches ändert: Steuerstatus, Benutzer, Sicherung, KI-Zugriff, Skontio-Update.

## 5c. Wiederkehrende Rechnungen und Mahnwesen

Wiederkehrende Rechnungen (Wartung, Hosting, Abos) legst du an einer Rechnung über „Als wiederkehrend anlegen“
an. Fällige Zeiträume werden zu **Entwürfen** – festschreiben und versenden bleibt Handarbeit. Das erledigt der
Container `skontio-worker` täglich um `SKONTIO_RECURRING_AT` (Standard 06:00); von Hand: `skontio recurring`.
Mehrfache oder parallele Läufe erzeugen keinen Zeitraum doppelt.

Das Mahnwesen (*Übersicht → Mahnwesen*) schlägt fällige Zahlungserinnerungen und Mahnungen vor. Stufen, Fristen
und Gebühren unter *Mahnwesen → Mahnstufen*. Mahnschreiben werden als PDF archiviert und zusammen mit der
Rechnung per E-Mail verschickt.

## 5c-1. Beleg-Postfach (Rechnungen per E-Mail)

Eingangsrechnungen einfach weiterleiten: Der Worker liest ein **eigenes** Postfach (z. B. `belege@…`, nicht das
normale Postfach) alle `SKONTIO_RECEIPT_IMAP_EVERY_MINUTES` Minuten (Standard 15). Es werden nur ungelesene
Mails gelesen; danach sind sie gelesen und, falls `SKONTIO_RECEIPT_IMAP_DONE_FOLDER` gesetzt ist, verschoben.

- E-Rechnungen (XRechnung, ZUGFeRD) → *Eingangsrechnungen*
- andere PDFs und Fotos → *Belegeingang*, mit Absender und Betreff als Notiz
- kleine eingebettete Bilder (Logos in Signaturen), ZIPs, Office-Dateien und anderes werden ignoriert
- jede Mail wird nur einmal übernommen (Message-ID); eine Mail, die nicht verarbeitet werden kann, wird im
  Postfach **markiert** (Flagge) und nicht immer wieder versucht

Einrichten: Passwort als Datei `imap_password` im Dataset `skontio-secrets`, dann die Variablen
`SKONTIO_RECEIPT_IMAP_*` am Worker setzen (siehe `compose.yaml`) und `SKONTIO_RECEIPT_IMAP_ADDRESS` am
Web-Container, damit der Belegeingang die Adresse anzeigt. Prüfen mit

```sh
docker exec -it <worker-container> skontio doctor imap     # Anmeldung und Ordner, liest keine Mail
docker exec -it <worker-container> skontio doctor worker   # letzter Abruf
```

Jeder kann an die Adresse schreiben. Mit `SKONTIO_RECEIPT_IMAP_ALLOWED_SENDERS` (Adressen oder Domains,
kommagetrennt) übernimmt Skontio nur Mails dieser Absender; die übrigen werden nur protokolliert.

## 5c-2. Kontoabruf über PSD2 (Enable Banking)

Skontio holt Kontoumsätze selbst ab – ohne Datei-Export. Dafür einmalig:

1. Bei [enablebanking.com](https://enablebanking.com/) ein Konto anlegen und eine **Anwendung** registrieren
   (für eigene Konten kostenlos). Als Redirect-URL `https://<deine-domain>/bank/verbinden/rueckkehr/` eintragen.
2. Den privaten Schlüssel der Anwendung als `enablebanking.pem` in `skontio-secrets` legen (`chmod 0400`).
3. `SKONTIO_ENABLEBANKING_APP_ID` und `SKONTIO_ENABLEBANKING_KEY_FILE` am **Web-Container und am Worker** setzen.
4. In Skontio *Bank → Bank verbinden*: Bank wählen, bei der Bank anmelden, Lesezugriff freigeben.

Der Worker ruft alle 6 Stunden ab (PSD2 erlaubt vier Abrufe ohne dich pro Tag und Konto), sonst *Jetzt abrufen*.
Die Freigabe gilt höchstens 180 Tage; 14 Tage vorher erinnert Skontio auf der Bankseite. Die Sitzung liegt
verschlüsselt in der Datenbank (mit `secret_key`). Ist die automatische Zuordnung eingeschaltet, werden eindeutige
Zahlungseingänge direkt nach dem Abruf gebucht.

Der erste Abruf reicht 90 Tage zurück. Umsätze, die schon per Datei importiert wurden (gleiches Konto, Tag und
Betrag), übernimmt er nicht noch einmal.

## 5c-3. Eigenes Aussehen (Theme)

Farben, Schriften und Abstände stehen als CSS-Variablen in `web/theme.css`; die übrige Oberfläche nutzt nur diese
Variablen. Für ein eigenes Aussehen ohne Code-Änderung einen Ordner einhängen und `SKONTIO_THEME_DIR` setzen:

```
/mnt/POOL/apps/skontio-theme/
  theme.css            # wird nach dem Standard geladen, z. B. :root { --blue: #0b7a75; --font-text: system-ui; }
  templates/web/...    # optional: einzelne Templates mit gleichem Pfad überschreiben
```

```yaml
    volumes:
      - /mnt/POOL/apps/skontio-theme:/theme:ro
    environment:
      SKONTIO_THEME_DIR: /theme
```

Eigene Templates sind ein Eingriff in die Oberfläche: nach Updates prüfen, ob sie noch passen. Fachliche Regeln
(Pflichtangaben, Beträge, Festschreibung) liegen nicht in den Templates und bleiben davon unberührt.

## 5d. KI-Assistent in Skontio

*Einstellungen → Assistent*: Anbieter wählen und **Verbindung testen** (zeigt Modelle, ob das Modell Werkzeuge
aufruft, und die Antwortzeit).

- **Ollama im Heimnetz** (Daten bleiben lokal): Adresse z. B. `http://192.168.1.20:11434/v1`, Modell z. B.
  `qwen3:8b`. Kleine Modelle auf schwacher Hardware brauchen pro Schritt leicht mehrere Minuten; der Assistent
  läuft deshalb im Hintergrund (Timeout je Schritt 300 s, insgesamt 10 Minuten).
- **Anthropic API / OpenAI-kompatibel**: API-Schlüssel eintragen (wird verschlüsselt gespeichert). Privatkunden
  gehen pseudonymisiert hinaus, Kontaktdaten und IBAN nie.
- Alternativ per Umgebung (hat Vorrang): `SKONTIO_LLM_PROVIDER` (`ollama`, `openai_compatible`, `anthropic`),
  `SKONTIO_LLM_BASE_URL`, `SKONTIO_LLM_MODEL`, `SKONTIO_LLM_API_KEY_FILE`, `SKONTIO_LLM_TIMEOUT`.

Der Assistent nutzt dieselben Werkzeuge wie MCP: lesen und **Entwürfe** anlegen. Festschreiben, Versenden und
Zahlungen bleiben bei dir; jeder KI-Entwurf ist markiert und braucht deine Prüfung. Jeder Werkzeugaufruf steht im
Änderungsprotokoll.

## 6. Backups

Der Container `skontio-worker` erstellt jede Nacht um `SKONTIO_BACKUP_AT` (Standard 02:30) ein geprüftes Backup.
War der Worker zu der Zeit aus (Update, Neustart), holt er es beim Start nach; Fehlschläge versucht er stündlich
bis zu dreimal. Kein Cron-Job nötig. `skontio doctor worker` meldet, wenn das letzte Backup älter als einen Tag
ist oder fehlgeschlagen ist. Von Hand: `docker exec <skontio-container> skontio backup --reason manual`.

Behält standardmäßig die letzten 14 Backups (`SKONTIO_BACKUP_KEEP`). Zusätzlich empfohlen:
periodische ZFS-Snapshots von `tank/apps/skontio` und eine Replikation bzw. Cloud-Sync-Aufgabe nach extern.

Vor jedem Update mit Migrationen legt Skontio automatisch ein Backup `vor-update-<version>` an.

## 7. Wiederherstellen

1. App in TrueNAS stoppen.
2. Einmalig einen Container mit denselben Volumes und Umgebungsvariablen starten und ausführen:
   `skontio restore /backups/skontio-YYYYMMDD-HHMMSS.tar --force`
3. App wieder starten (fehlende Migrationen laufen automatisch) und `skontio verify_archive` prüfen.

Der Restore prüft **vor dem Schreiben** alle Prüfsummen und bricht bei Manipulation ab. Ohne `--force`
spielt er nur in eine leere Datenbank ein.

- **SQLite:** Die Datenbankdatei wird erst zum Schluss atomar ersetzt.
- **Postgres:** Das Image enthält `pg_dump`/`pg_restore`/`psql` 18 (sichert Postgres-Server bis Version 18).
  Das Schema wird in **einer einzigen Transaktion** ersetzt – schlägt irgendetwas fehl, bleibt die
  bisherige Datenbank vollständig erhalten. `skontio doctor backup` warnt, wenn der Server neuer ist als `pg_dump`.

## 8. Updates

Images entstehen automatisch auf GitHub (`ghcr.io/skontio/skontio`):

| Tag | Inhalt | Für |
|---|---|---|
| `:0.2` | neueste Version 0.2.x (Fehlerbehebungen) | Produktivbetrieb |
| `:0.2.0` | genau diese Version | festhalten, zurückgehen |
| `:edge` | aktueller Stand von `main` | Testinstanz |

**Produktiv:** Image-Tag in der Compose-Datei erhöhen (z. B. `0.2` → `0.3`), App aktualisieren. Vor den
Migrationen legt Skontio automatisch ein Backup `vor-update-<version>` an. Zurück geht es mit dem alten Tag;
haben Migrationen die Datenbank umgebaut, zusätzlich dieses Backup einspielen (Abschnitt 7).

**Herkunft prüfen, bevor du aktualisierst.** Jedes veröffentlichte Image trägt eine signierte Stückliste (SBOM) und
einen Herkunftsnachweis: gebaut von genau diesem Repository, in genau dieser CI. Prüfen auf einem Rechner mit
GitHub-CLI:

```sh
gh attestation verify oci://ghcr.io/skontio/skontio:0.2 --owner skontio
docker buildx imagetools inspect ghcr.io/skontio/skontio:0.2 --format '{{json .Manifest.Digest}}'
```

Den ausgegebenen Digest in die Compose-Datei schreiben, dann kann niemand unter demselben Tag ein anderes Image
nachschieben:

```yaml
    image: ghcr.io/skontio/skontio:0.2@sha256:…   # Tag zur Lesbarkeit, Digest gilt
```

Die Stückliste zeigt bei einer neuen Sicherheitsmeldung sofort, ob eine Version betroffen ist:
`gh attestation verify … --predicate-type https://spdx.dev/Document --format json`.

## 9. Testinstanz für neue Funktionen

Festgeschriebene Rechnungen lassen sich (GoBD) weder ändern noch löschen, und jede Rechnungsnummer ist vergeben.
Neue Funktionen deshalb in einer **eigenen Instanz** ausprobieren:

1. Eigene Datasets (`tank/apps/skontio-test/{data,archive,backups}`) und eigene Geheimnisse mit neuem `secret_key`.
2. Eigene Subdomain (z. B. `skontio-test.example.org`) in Caddy und ein eigener OIDC-Client in Pocket ID.
3. Compose-Datei kopieren: Image `:edge`, anderer Port (z. B. `8086`), neue Pfade, App-Name `skontio-test`.
4. **Kein echter SMTP-Server** – sonst gehen Test-Rechnungen an echte Kunden. Ein Testpostfach oder SMTP weglassen.

Neue Version testen: In TrueNAS die Test-App aktualisieren bzw. mit erneutem Image-Pull neu ausrollen, damit
das aktuelle `:edge` geladen wird. Die Testinstanz kann jederzeit weggeworfen und neu angelegt werden.

---

Skontio unterstützt bei Rechnungen, Fristen und Aufbewahrung, ersetzt aber **keine Steuer- oder Rechtsberatung**.
