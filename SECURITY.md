# Sicherheit

Skontio verarbeitet Rechnungs-, Kunden- und künftig Bankdaten. Sicherheitsmeldungen haben Vorrang.

## Melden

Bitte melde Schwachstellen **privat** über GitHub:
*Security → Report a vulnerability* im Repository `skontio/skontio`.
Keine öffentlichen Issues, Pull Requests oder Diskussionen zu ungepatchten Lücken.

Hilfreich sind: betroffene Version, Schritte zur Reproduktion, Auswirkung, ggf. Vorschlag zur Behebung.
Wir bestätigen den Eingang in der Regel innerhalb von 7 Tagen.

## Unterstützte Versionen

Bis zur Version 1.0 wird nur der jeweils aktuelle Stand von `main` mit Sicherheitsupdates versorgt.

## Grundsätze

- Festgeschriebene Dokumente sind auf Datenbankebene unveränderbar.
- MCP-Zugriff nur mit Tokens, die Skontio selbst für `/mcp` ausgestellt hat (RFC 8707); nur Entwürfe, nie Festschreiben oder Versenden.
- Keine Bank-PINs; Zugangsdaten verschlüsselt (Envelope Encryption), Master-Key getrennt von Backups.
- Container läuft unprivilegiert (UID 568), read-only, ohne Capabilities.
