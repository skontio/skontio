# Sicherheitskonzept: Container und Betrieb

Angriffswege und ihre Prüfung: `docs/sicherheit-angriffe.md`.

Stand 2026-11-02. Ziel: Auch wenn Skontio einen Fehler hat, der Angreifern Code-Ausführung erlaubt, bekommen sie
**keine Shell, kein root und keinen Weg aus dem Container** – und der Schaden bleibt auf das begrenzt, was die
Anwendung selbst ohnehin darf.

## 1. Bedrohungsmodell

| Angreifer | Weg | Schutz |
|---|---|---|
| Internet, ohne Konto | öffentliche Routen (Login, OAuth, `/mcp`, Metadaten) | festgeschriebene Liste öffentlicher Routen (`tests/test_public_surface.py`), Login nur über Pocket ID mit Passkey, Notfall-Login nur im LAN mit 2FA und Sperre, `/mcp` nur für Anthropic-IPs und das LAN (Caddy) |
| Internet, mit gestohlenem Token | `/mcp`, OAuth | Audience exakt `/mcp` (RFC 8707), Scopes, Rate-Limit, Widerruf in den Einstellungen |
| bösartige Datei | Beleg-Postfach, Upload, E-Rechnung, Kontoauszug | Typprüfung am Inhalt, Größenlimits, XML ohne Entities, Fuzzing-Tests (`test_adversarial`) |
| Code-Ausführung in Skontio | Fehler in Skontio oder einer Bibliothek | **dieses Dokument**: kein root, keine Shell, kein setuid, schreibgeschützt, keine Capabilities |
| Admin des NAS | `docker exec -u 0` | nicht abwehrbar und nicht Ziel: wer den Docker-Host verwaltet, verwaltet alles. TrueNAS-Zugang selbst schützen (2FA, nur LAN) |

## 2. Umgesetzt

**Image** (`Dockerfile`)
- läuft als UID/GID 568 (`apps`), nie als root – auch `tini` (PID 1) und `docker exec` ohne `-u`
- **keine Shell** (`sh`, `bash`, `dash` entfernt), **keine Paketverwaltung** (`apt`, `dpkg`), kein Perl: Exploits,
  die `/bin/sh -c …` starten, laufen ins Leere; `docker exec … sh` geht nicht
- **keine setuid/setgid-Programme** (`su`, `passwd`, `mount` …): niemand wird im Container root
- Einstieg in Python (`skontio-entrypoint`) statt Shell-Skript; Healthcheck in Exec-Form
- die Paketliste von dpkg bleibt erhalten, damit Image-Scanner die Systempakete weiter erkennen

**Laufzeit** (`deploy/truenas/compose.yaml`, ebenso `compose.dev.yaml`)
- `read_only: true` – das Dateisystem des Images ist unveränderlich; schreibbar sind nur `/data`, `/archive`,
  `/backups` und `/tmp`
- `/tmp` als `tmpfs` mit `noexec,nosuid,nodev` und 256 MB: dort abgelegte Dateien lassen sich nicht ausführen
- `cap_drop: [ALL]` und `no-new-privileges` – keine Linux-Capabilities, kein Rechtegewinn über Programme
- `pids_limit: 256`, `mem_limit: 1g` – eine Fork-Bombe oder ein Speicherleck legt nicht das NAS lahm
- Port nur an die LAN-Adresse gebunden, davor Caddy mit TLS; Geheimnisse als Dateien, schreibgeschützt eingehängt
- Absturz: Granian startet abgestürzte Worker neu, Docker den Container; `DEBUG` ist aus, Fehlerseiten zeigen keine
  Tracebacks (`tests/test_public_surface.py`)

**Anwendung**
- kein Django-Admin (seine Passwort-Anmeldung hätte LAN-Sperre, Fehlversuchssperre und 2FA umgangen)
- OAuth nur mit den Endpunkten, die MCP-Clients brauchen (keine Device-Flow-, Introspection- oder
  Client-Verwaltungs-Routen)
- Abhängigkeiten werden in der CI bei jedem Push und **wöchentlich** auf bekannte Lücken geprüft (`pip-audit`)

**Nachweis**: `tools/smoke.sh` (läuft in der CI gegen das gebaute Image) prüft, dass es keine Shell gibt, kein
Prozess als root läuft, keine setuid-Dateien existieren, keine Capabilities gesetzt sind und `/app`, `/usr`,
`/etc` nicht beschreibbar sind.

## 3. Was bleibt – ehrlich

Python ist selbst ein Interpreter. Wer Code in Skontio ausführen kann, kann weiter Python-Code ausführen – als
UID 568, im schreibgeschützten Container, ohne Capabilities. Damit erreicht er, was Skontio erreicht: die
Datenbank, das Archiv, die Backups und das Netz. Die Härtung verhindert root, Werkzeuge und Ausbruch, nicht den
Zugriff auf die eigenen Daten. Deshalb zählen die Schutzschichten davor (Login, Tests, Updates) genauso.

## 4. „Distroless“ – Bewertung

| Variante | Bewertung |
|---|---|
| Google `distroless/python3` | bringt Debians Python (nicht 3.13) und keine Pango/HarfBuzz-Bibliotheken für die PDF-Erzeugung mit; nachrüsten heißt Bibliotheken von Hand kopieren – fehleranfällig |
| Chainguard/Wolfi | technisch passend (aktuelles Python, Pango verfügbar), feste Versionen aber nur im Abo; Wechsel des Basissystems |
| **Debian slim, entkernt (umgesetzt)** | gleiche Wirkung wie distroless in den entscheidenden Punkten – keine Shell, kein Paketmanager, kein setuid, kein root – bei bekanntem, gut gepflegtem Basissystem |

Empfehlung: bei der entkernten Variante bleiben. Ein Wechsel auf Wolfi lohnt sich erst, wenn Scanner dauerhaft
viele unbehobene Debian-Meldungen zeigen.

## 5. Nächste Schritte (empfohlen, noch offen)

1. **Image-Scan in der CI** (z. B. Trivy) auf hohe und kritische Lücken in Systempaketen, Abbruch nur bei
   behebbaren Meldungen.
2. **Basis-Image per Digest festnageln**; automatische Update-PRs (Dependabot) für Actions, Python-Pakete und das
   Basis-Image sind eingerichtet, GitHub Actions sind bereits auf Commit-SHAs festgenagelt.
3. **Signatur und SBOM** für veröffentlichte Images (cosign, `docker buildx --sbom`); TrueNAS zieht nur signierte
   Tags.
4. **Ausgehende Verbindungen begrenzen**: Skontio braucht nur Pocket ID, den SMTP-/IMAP-Server, Enable Banking und
   ggf. den KI-Anbieter. Eine Firewall-Regel am NAS (oder ein eigenes Docker-Netz mit Egress-Proxy) verhindert,
   dass eine Code-Ausführung Daten an beliebige Ziele schickt.
5. **Postgres-Rolle ohne Sonderrechte**: der Skontio-Benutzer besitzt nur die eigene Datenbank, kein `SUPERUSER`,
   kein `CREATEROLE`; Backups außer Haus verschlüsselt.
6. **Nie den Docker-Socket einhängen** und Seccomp/AppArmor nicht abschalten (TrueNAS-Standard beibehalten).
