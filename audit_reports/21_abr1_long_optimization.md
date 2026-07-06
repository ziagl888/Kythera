# Report 21 — ABR1 LONG: Optimierungs-Studie (negatives Resultat)

**Datum:** 2026-07-06 · **Datenbasis:** Replay `detector_fix/abr1_replay_365d.jsonl`
(neuer Detektor nach Rework, 100 Coins / 365 d, 27.705 LONG-Events) + 1h-OHLCV read-only aus der Live-DB.
**Anlass:** Nach dem Detektor-Rework (CHANGELOG 2026-07-05) wurde nur das SHORT-Binärmodell
deployt; LONG blieb auf dem Legacy-Blocker. Frage des Operators: lässt sich LONG retten —
„Break & Retest funktioniert doch auch long"?

**Kurzantwort: Nein — nicht in diesem Marktjahr, nicht mit diesen Hebeln.** Alle drei
Optimierungsklassen (Trade-Management, ML-Entry-Selektion, Regime-Filter) wurden auf echten
Kursdaten durchsimuliert; keine dreht LONG ins Plus. LONG bleibt zu.

---

## 1. Diagnose: warum LONG verliert

| | LONG | SHORT (Referenz) |
|---|---|---|
| TP1-First-Touch-WR | 55,5 % | 58,0 % |
| avg Win / avg Loss | +3,01 % / −5,09 % | +4,18 % / −5,14 % |
| Break-even-WR (aus Payoff) | **≈ 62,8 %** | ≈ 55,2 % |
| avg PnL/Trade | **−0,59 %** | +0,28 % |

Das Problem ist die **Payoff-Asymmetrie, nicht die Trefferquote**: LONG-Wins zahlen ~28 %
weniger als SHORT-Wins bei gleicher Loss-Größe. Selbst die besten Monate (2025-09: 64 % WR,
2026-04: 63 %) erreichen den Break-even nur haarscharf. Monats-WR schwankt 43–64 % → stark
regimegetrieben. Kein einzelnes der 23 Features trennt nennenswert (bestes Quartil −0,16 %/Trade).

Strukturursachen im Code: `calculate_smart_targets` setzt den SL ≥ 3×ATR unter Entry
(∅ Risk 4,96 %) — generische Swing-Geometrie statt Setup-Invalidierung am Level; das
Ladder-Management (1/n, Trailing erst ab TP2) gibt bei `sl_after_tp1` 2/3 der Position am
vollen SL ab (3.746 Trades).

## 2. Getestete Hebel

### 2a. Trade-Management (Exit-Resimulation, 27.559 Trades, Baseline-Replikation 99,7 %)

| Variante | WR | avg PnL/Trade | avg R | Summe |
|---|---|---|---|---|
| V0 Original (SL 3×ATR, Trailing ab TP2) | 55,4 % | −0,60 % | −0,10 | −16.566 % |
| V1 + Breakeven-SL nach TP1 | 55,4 % | −0,50 % | −0,09 | −13.742 % |
| V2 Setup-SL 1,0 % unter Level | 32,6 % | −0,25 % | −0,13 | −6.845 % |
| V3 Setup-SL 1,5 % unter Level | 38,2 % | −0,30 % | −0,12 | −8.201 % |
| V4 = V2 + BE nach TP1 | 32,6 % | **−0,24 %** | −0,13 | −6.696 % |

Der enge Setup-SL halbiert den nominalen Verlust, ist aber **risikoadjustiert schlechter**
(−0,13 R vs. −0,10 R): die 1h-Wicks reißen den engen Stop zu oft. BE-nach-TP1 hilft
(+0,10 pp), reicht allein nicht. Kein Monat außer dem Randmonat 2026-07 wird stabil positiv.

### 2b. ML-Entry-Selektion (XGB, Label `net_pnl > 0`, chrono 70/15/15 + 7d-Purge, 23+3 Features)

| | Val (q0.95-Slice) | Test (gleicher Threshold) |
|---|---|---|
| unter V0-Management | **+3,25 %**/Trade | **−2,17 %**/Trade |
| unter V4-Management | +0,74 %/Trade | −1,07 %/Trade |

Jede Test-Scheibe negativ, und **je höher der Threshold, desto schlechter** — das Modell
lernt Val-Regime-Muster, die out-of-sample invertieren. Identische Signatur wie das
Batch-Retrain (Report 19 / Deploy 2026-07-06: Test-WR 51,8 % == Basisrate, Top-Bucket
invertiert). Das ist kein Trainings-Bug, sondern fehlendes Signal in den Features.

### 2c. BTC-Regime-Filter (EMA200(1d) / 30d-Momentum, Vortages-Shift)

| Regime | n | V0 avg | V4 avg |
|---|---|---|---|
| BTC > EMA200 | 6.508 | **−1,08 %** | −0,22 % |
| BTC < EMA200 | 21.197 | −0,46 % | −0,25 % |

Sogar invertiert: Alt-Resistance-Breaks nach oben werden im BTC-Aufwärtsregime *stärker*
verkauft. Als Gate unbrauchbar.

## 3. Einordnung & Empfehlung

Die Asymmetrie ist marktlogisch konsistent: Aufwärtsbreaks in Alts werden gefadet —
die Edge der Strategie liegt auf der SHORT-Seite (gescheiterte/überdehnte Moves), was das
deployte SHORT-Gate (Test-WR 68 % vs. 63,7 %, +1,5 %/Trade) bestätigt.

1. **LONG bleibt aus** (Status quo: Legacy-3-Klassen-Modell ohne meta.json wirkt als
   De-facto-Sperre @ Threshold 0,60). Kein Code-Change nötig.
2. **Nicht weiter an Exit-Geometrie/Threshold drehen** — der Suchraum ist hier abgegrast,
   weitere Iterationen wären Overfitting auf dieselben 365 Tage.
3. Reaktivierung nur über **neue Informationsquellen** (Orderflow/Funding/Whale-Daten aus
   Bot 19/20, BTC-Dominanz, Level-Konfluenz über Timeframes) — eigenes Forschungsprojekt,
   kein Tuning — **oder** über einen Regimewechsel: Replay quartalsweise neu laufen lassen;
   dreht die ungefilterte LONG-Basisrate nachhaltig über ~63 % (Break-even), neu bewerten.
4. V1 (BE-nach-TP1) wäre als *generelle* Management-Verbesserung auch für SHORT prüfenswert
   (+0,10 pp bei LONG ohne WR-Verlust) — separates Ticket, betrifft `8_ai_trade_monitor`.

**Artefakte:** Diagnose-/Resim-/Modell-Skripte im Session-Scratchpad; Resim-Rohdaten
`resim_results.pkl`. Replay + Stats: `_X\staging_models\replay\detector_fix\`,
`_X\staging_models\retrain_abr1_stats.json`.

---

## Addendum (2026-07-06 abends): Target-Seite ebenfalls getestet — negativ

Auf Operator-Wunsch wurde der letzte ungetestete strukturelle Hebel geprüft:
**R-basierte Targets** (TP1/2/3 = Entry + 1R/2R/3R) statt Level-Cluster-Targets —
die Hypothese, dass die nahen Cluster-Targets über dem Entry die Payoff-Asymmetrie
verursachen.

| Variante | WR (TP1) | avg PnL/Trade | avg R | BE-WR (Payoff) | pnl>0-Quote |
|---|---|---|---|---|---|
| G0 Original | 55,3 % | −0,61 % | −0,10 | 48,4 % | 41,7 % |
| G1 Smart-SL + R-Targets | 46,7 % | −0,84 % | −0,14 | 36,2 % | 29,2 % |
| G2 Setup-SL + R-Targets | 47,6 % | **−0,21 %** | −0,11 | 36,7 % | 31,8 % |
| G3 = G2 + BE nach TP1 | 47,6 % | −0,21 % | −0,11 | 53,5 % | 47,6 % |
| G4 = G1 + BE nach TP1 | 46,7 % | −0,69 % | −0,12 | 53,9 % | 46,7 % |

Zentrale Beobachtung: Die Geometrie verschiebt nur, WIE verloren wird — der
risikoadjustierte Erwartungswert bleibt über den GESAMTEN Geometrie-Raum bei
**−0,10 bis −0,14 R** (Fees erklären davon nur ~0,05 R). Symmetrischer Payoff
(G1: BE-WR 36 %) wird exakt durch die einbrechende Gewinnquote (29 %) bezahlt.
Kein Monat außer dem Randmonat 2026-07 stabil positiv. **Damit sind Entry-Gate,
SL-Seite, Target-Seite, Management, ML-Selektion und BTC-Regime alle
falsifiziert — die LONG-Seite hat in diesem Marktjahr keine Edge, Punkt.**

Nächster (letzter) Kandidat gem. §3: neue Informationsquellen. Funding-Rate-
Historie wird backfillt (`tools/backfill_funding_rates.py`, Tabelle
`funding_rates`) — Funding ist im Gegensatz zu Whale-Daten (WS erst seit
04.07. wieder live) vollständig historisch verfügbar.
