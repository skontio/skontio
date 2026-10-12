# Rechtliche Quellen und Verifikationsstand

Keine Rechtsberatung. Diese Liste dokumentiert, worauf Skontio seine Regeln stützt und was noch geprüft werden muss.
Status: ✅ am Primärtext geprüft · ⏳ offen (Sekundärquelle) · ❗ widersprüchlich

| Thema | Regel in Skontio | Quelle | Status | Geprüft am |
|---|---|---|---|---|
| Kleinunternehmergrenzen | Vorjahr ≤ 25.000 €, laufendes Jahr ≤ 100.000 €, Gesamtumsatz nach vereinnahmten Entgelten; Verzicht bindet 5 Jahre | [§ 19 UStG](https://www.gesetze-im-internet.de/ustg_1980/__19.html) | ✅ | 2026-10-11 |
| Überschreiten der 100.000 € im laufenden Jahr | Steuerpflicht ab dem Umsatz, mit dem die Grenze überschritten wird | BMF-Schreiben vom 18.03.2025, Abschn. 19.1 UStAE | ⏳ | – |
| Gründungsjahr | Grenze 25.000 € im laufenden Jahr | BMF-Schreiben vom 18.03.2025 | ⏳ | – |
| Rechnungen von Kleinunternehmern | Pflichtangaben; dürfen stets als sonstige Rechnung übermittelt werden | [§ 34a UStDV](https://www.gesetze-im-internet.de/ustdv_1980/__34a.html) | ✅ | 2026-10-11 |
| E-Rechnung: Ausstellungspflicht, Übergangsfristen, zulässige Profile | ab 2027 (> 800.000 € Vorjahresumsatz) bzw. 2028; MINIMUM/BASIC-WL keine E-Rechnung | BMF-Schreiben vom 15.10.2025 (laut IHK Köln) | ⏳ | – |
| Zustimmung bei PDF-Rechnungen (sonstige elektronische Rechnung) | Zustimmung des Empfängers nötig, bei E-Rechnung nicht | BMF-Schreiben vom 15.10.2025 (laut Händlerbund) | ⏳ | – |
| Aufbewahrungsfristen | Bücher 10 J., Buchungsbelege/Rechnungen 8 J., Geschäftsbriefe 6 J.; Fristbeginn Ende Kalenderjahr | [§ 147 Abs. 3 AO](https://www.gesetze-im-internet.de/ao_1977/__147.html), § 14b UStG | ✅ (AO) / ⏳ (§ 14b) | 2026-10-11 |
| Rechnungsnummer | fortlaufend, einmalig | § 14 Abs. 4 Nr. 4 UStG | ⏳ | – |
| Leistungsdatum | Pflichtangabe | § 14 Abs. 4 Nr. 6 UStG | ⏳ | – |
| Ausweis von USt durch Kleinunternehmer | wird geschuldet → Skontio blockiert das | § 14c UStG | ⏳ | – |
| Verkäuferkennung in E-Rechnungen | USt-IdNr. oder BT-29/BT-30 Pflicht (BR-CO-26); Steuernummer reicht nicht | EN 16931 / KoSIT-Validator 1.6.3, XRechnung 3.0.2 | ✅ (Validator) | 2026-10-11 |
| PSD2 SCA-Erneuerung | 180 Tage | Delegierte VO (EU) 2022/2360 (laut EBA) | ⏳ | – |
| Kontoinformationsdienst bei Hosting für Dritte | Registrierung nach § 34 ZAG wahrscheinlich nötig | § 34 ZAG | ⏳ (Fachanwalt) | – |
| Gewerbesteuer in der Rücklagenschätzung | Freibetrag 24.500 €, Messzahl 3,5 %, Anrechnung § 35 EStG (Faktor 4,0) | GewStG, EStG | ⏳ | – |
| Kennzahlen der USt-Voranmeldung | Kz 81/86/35/36/41/43/48/21/45, § 13b Kz 46/47/84/85, Vorsteuer 66/67, Zahllast 83; Abgabe am 10. nach Zeitraumende, Dauerfristverlängerung +1 Monat | Formular USt 1 A und Anleitung (jährlich), § 18 UStG, § 18b UStG | ⏳ | – |

Vor jedem Release mit Steuerlogik: offene Punkte mit Primärquelle (BGBl., BMF-Schreiben im Original) schließen.
