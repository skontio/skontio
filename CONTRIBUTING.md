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

## Sicherheitslücken

Bitte **nicht** öffentlich melden – siehe [SECURITY.md](SECURITY.md).
