# Mitwirken an Skontio

Danke für dein Interesse! Skontio ist AGPL-3.0-or-later lizenziert.

## Developer Certificate of Origin (DCO)

Wir verwenden statt einer CLA das [Developer Certificate of Origin 1.1](https://developercertificate.org/).
Jeder Commit braucht eine Sign-off-Zeile, mit der du bestätigst, dass du den Beitrag einbringen darfst:

```bash
git commit -s -m "Nummernkreise: Jahreswechsel testen"
# → Signed-off-by: Vorname Nachname <mail@example.org>
```

## Arbeitsweise

- **Test-first** für fachliche Logik: Nummernkreise, Festschreibung, Steuerberechnung, E-Rechnung.
- E-Rechnungen müssen die offiziellen Validatoren bestehen: `tools/validators/fetch.sh`, dann `uv run pytest -m einvoice`.
- Tests laufen auf **SQLite und Postgres** (siehe README).
- Vor dem Commit: `uv run ruff check . && uv run ruff format . && uv run pytest`.
- Rechtliche Annahmen nur mit Quelle (Gesetz, BMF-Schreiben) – Nachweise in `docs/legal/sources.md`.
- Keine echten Kundendaten in Tests, Issues oder Screenshots.

## Keine privaten Daten in Commits

Passwörter, Tokens, Schlüssel, eigene Hostnamen, IP-Adressen und private E-Mail-Adressen gehören nicht ins Repo.
Zwei Schranken:

- **CI** (`secrets`-Job, Regeln in `.gitleaks.toml`): [gitleaks](https://github.com/gitleaks/gitleaks) prüft bei
  jedem PR, auf `main` und wöchentlich die gesamte Historie. Ohne grünen Lauf wird kein Image veröffentlicht.
- **Lokal** (`tools/install-hooks.sh`, einmal ausführen): ein `pre-push`-Hook prüft neue Commits gegen deine
  eigenen Muster in `.git/private-patterns` (eine Regex pro Zeile: private Adresse, Hostnamen, IPs – diese Datei
  bleibt lokal) und, wenn installiert, gegen gitleaks.

Zusätzlich in den GitHub-Einstellungen des Repos *Secret scanning* mit *Push protection* einschalten und im
eigenen Profil *Keep my email addresses private* (sonst steht beim Squash-Merge die Konto-Adresse im Commit).
Das gepinnte gitleaks-Image im Workflow gelegentlich von Hand aktualisieren; Dependabot sieht es dort nicht.

## Sicherheitslücken

Bitte **nicht** öffentlich melden – siehe [SECURITY.md](SECURITY.md).
