# EPD4 — Machbarkeit des Detektor-Retrains (T-2026-KYT-9050-004)

Messung 2026-08-01, read-only gegen die Live-DB. Rohdaten: `epd4_feasibility.json`,
Split-Zahlen aus dem echten Trainerlauf: `../retrain_epd4_stats.json`.
Ausführlicher Bericht: `docs/T-2026-KYT-9050-004-epd-retrain-feasibility.md`.

## Verdikt

**Der Retrain ist heute nicht ausführbar — Kalender, nicht Datenqualität. Kein Artefakt erzeugt.**

Der Datensatz auf der neuen Feature-Definition ist sauber (4698 von 4712 Events
geschrieben, 0,3 % Verlust, 7,9 % am Horizont noch offen). Er ist nur **22,0 Tage**
lang. `chrono_split` gibt Val und Test je das 15 %-Quantilsband — 3,3 Tage —, und
davon schneidet der 7-Tage-Purge-Gap (= Label-Horizont) alles weg:

| Richtung | Events | train/val/test | Spanne | 15 %-Band | Dichte | benötigt |
|---|---|---|---|---|---|---|
| LONG  | 2378 | 1664 / **0** / **0** | 21,8 d | 3,3 d | 109 Z/Tag | ~50 d (+28 d) |
| SHORT | 1949 | 1364 / **0** / **0** | 22,0 d | 3,3 d | 89 Z/Tag  | ~50 d (+28 d) |

## Cut-Point belegt, nicht angenommen

Stündliche `pump_dump_events`-Zählung am 2026-07-10 (UTC): 56–170 Events/h bis
16:00, ab 17:00 nur noch 10–33/h. Der Bruch liegt auf der Stunde des
Bot-10-Restarts (17:08:29Z), der P1.39, die T-035-Ratennormierung und den
wiederbelebten Stunden-Warmup gemeinsam scharf schaltete. Die **Ereignisrate** fiel
um ~5×.

## Die Verschiebung selbst ist klein

Zwei-Stichproben-KS je Feature (14 d vor vs. 14 d nach dem Cut) gegen ein Nullband
aus 15 benachbarten 14-d-Fensterpaaren der Vor-Cut-Historie:

| Feature | KS am Cut | Nullband-Median | Nullband-Max | über Nullband? |
|---|---|---|---|---|
| volume_ratio    | 0,0361 | 0,0624 | 0,4342 | nein |
| \|p_chg_60s\|   | 0,0796 | 0,0580 | 0,3355 | nein |
| buy_pressure    | 0,1737 | 0,0798 | 0,2039 | nein |
| volatility      | 0,0363 | 0,0627 | 0,3536 | nein |

Kein Feature verlässt das Band gewöhnlicher Marktdrift. Nur Randverteilungen —
gemeinsame Verschiebungen sind damit nicht ausgeschlossen.

## Das deployte Modell hält out-of-sample

`epd3_model_{LONG,SHORT}.pkl` (auf VOR-Cut-Daten gefittet) auf den Post-Cut-Events:

| | AUC(TP1) | Kalibrierung | am Live-Threshold |
|---|---|---|---|
| LONG (thr 0,76)   | 0,586 | monoton 38,3 → 66,7 % TP1 | n=81 (3,4 %), WR 60,5 %, Ø −0,760 % |
| SHORT (thr 0,6737)| 0,537 | 50,0 → 73,8 % TP1, nicht monoton | n=756 (38,8 %), WR 72,6 %, Ø +0,065 % |

Kein Hinweis auf ein durch die Verschiebung kaputtes Modell. Der LONG-Threshold
0,76 (Operator-Volumenkappe, kein Edge-Filter) liegt allerdings im schlechtesten
PnL-Bereich der Kurve — die Bänder 0,5–0,7 sind positiv (+0,17 / +0,25 %).
n=81 ist dünn; das ist ein Hinweis, kein Verdikt.

## Frühestens

Formel `(0,15·Spanne − 7 d)·Dichte ≥ Zielzeilen`, Dichte konstant angenommen:

- **2026-08-30** — Split nicht mehr degeneriert (≥50 Zeilen/Slice), statistisch wertlos
- **2026-09-17** — ~300 Zeilen je Slice, Threshold-Scan ohne Rückhalt
- **2026-11-09** — ~1000 Zeilen je Slice, erster Operating-Point mit `min_n=200`-Rückhalt · **Empfehlung**

Der Ist-Wert steht nach jedem Lauf in `retrain_epd4_stats.json` (`missing_days`).
