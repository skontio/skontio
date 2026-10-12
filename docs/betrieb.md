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
- Erlaubte Gruppen: `skontio` (Zugriff) und `skontio-admin` (Einstellungen)

Dich selbst in beide Gruppen aufnehmen. Client-ID in die Compose-Datei, Client-Secret in `oidc_client_secret`.

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
```

Scheitert `discovery`, kann der Router vermutlich kein NAT-Loopback: im lokalen DNS die Domain
`id.example.org` auf die interne IP der Synology zeigen lassen (Split-DNS).

Danach `https://skontio.example.org` öffnen, mit dem Passkey anmelden und die Einrichtung ausfüllen.

**Notfall-Login ohne Pocket ID:** Konto anlegen mit `docker exec -it <container> skontio createsuperuser`,
dann **aus dem Heimnetz** `https://skontio.example.org/auth/local/` öffnen. Von außen ist dieser
Login gesperrt (Skontio erkennt die echte Client-IP über Caddy; `SKONTIO_TRUSTED_PROXIES` muss stimmen).

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

## 6. Backups

TrueNAS: *System → Advanced → Cron Jobs* → täglich 02:30:

```sh
docker exec <skontio-container> skontio backup --reason nightly
```

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

Image-Tag in der Compose-Datei erhöhen (feste Minor-Version, z. B. `0.2`), App aktualisieren.
Migrationen laufen beim Start nach dem automatischen Backup.

---

Skontio unterstützt bei Rechnungen, Fristen und Aufbewahrung, ersetzt aber **keine Steuer- oder Rechtsberatung**.
