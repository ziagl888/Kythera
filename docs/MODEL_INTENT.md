# Modell-Intent-Register — die ursprüngliche Idee je Modell

**Zweck:** Bevor weiter trainiert, gefixt oder deployed wird, fixieren wir je Modell
die URSPRÜNGLICHE Idee (Soll), vergleichen sie mit dem, was Pipeline/Bot heute tun
(Ist), und markieren Drift. Regel ab jetzt: **Kein Retrain und kein Deploy, dessen
Label nicht nachweislich die Soll-Frage beantwortet.**

Anlass: Beim MIS1-Retrain (Batch E / 2026-07-05) wurde das Label stillschweigend von
„±X% Move innerhalb T" (Operator-Konzept) auf „TP1-vor-SL der Smart-Targets-Geometrie"
umgestellt — methodisch sauber, aber es beantwortet eine ANDERE Frage. Korrigiert am
2026-07-06 (`tools/mis1_move_labels.py`, `--label-mode move`).

**Status-Legende:** ✅ Intent bestätigt · ✏️ Intent-Formulierung braucht Michis
Bestätigung/Korrektur · ⚠️ Drift zwischen Soll und Ist · ⛔ empirisch widerlegt/aus.

Quellen: Operator-Aussagen (Chat 2026-07-05/06), `audit_reports/16_strategy_concept_evaluation.md`,
`audit_reports/dossiers/*`, Report 19 / `staging_models/REPORT.md`.

---

## 1. MIS1 — Pump/Dump-Frühwarnung aus Indikator-Kombinationen ✅ (Intent vom Operator bestätigt 2026-07-06)

**Soll (O-Ton sinngemäß):** Kombinationen von Indikatorständen (z. B. RSI hoch >60,
Preis weit über EMA/WMA, Volumen fällt) sagen eine bevorstehende Bewegung voraus:
**±5 % innerhalb 8h, ±10 %/24h, ±15 %/72h, ±25 %/168h** — je größer der Horizont,
desto höher die Trefferchance. Threshold/Confidence so, dass wenige, dafür sichere
Trades entstehen (max. PnL bei min. Risiko).

**Ist:**
- Features = 63 bereinigte Indikatorstände (`core/mis_features.py`) → deckt das Soll ab.
- Batch-E-Retrain nutzte das Geometrie-Label (TP1-vor-SL) → beantwortet „verdient der
  gepostete Trade?", NICHT die Soll-Frage. ⚠ behoben: Move-Label-Modus
  (`tools/mis1_move_labels.py` + `retrain_from_replay.py --label-mode move`,
  Schwellen `MOVE_THRESH_PCT`), Threshold-Wahl `pick_threshold_safe`
  (Ø-PnL/Trade, min. 200 Val-Trades, „nicht deploybar" als ehrliches Ergebnis).
- Beide Modell-Sätze bleiben im Staging vergleichbar: `mis1_move_model_*` (Soll-Frage)
  vs. `mis1_model_*` (Trade-Geometrie-Frage).

**Entschieden (Michi, 2026-07-06):**
- [x] Move-Basis: **BEIDE Varianten trainieren** (Close und Wick, `--move-basis`),
      Testergebnis-Vergleich entscheidet. Artefakte `mis1_move_model_*` (Close)
      vs. `mis1_move_wick_model_*`.
- [x] Produkt bleibt **Trade-Signal mit Smart-Targets**: Move-Modell wählt die
      Kandidaten, Ökonomie wird an der Geometrie gemessen.
- [x] Übergang (REVIDIERT 2026-07-06 nachmittags): **MIS1 wird mit dem
      MIS2-Go-Live ABGESCHALTET** — kein Parallelbetrieb. Die Out-of-Time-Tests
      der Move-Modelle (alle 4 Pump-Horizonte positiv) gelten als Beweis.
- [x] **MIS2 deployed 2026-07-06**: nur Pump-Seite (LONG), Basis-Mix Close für
      8h/24h/168h + Wick für 72h, Tags `MIS2-<H>H`, gleiche Horizont-Channels.
      Bot 11 ohne Legacy-Fallback (MIS1-Modelle laden nicht mehr).
- [x] **Dump-Seite überarbeitet und LIVE (2026-07-06 abends):** Geometrie-Studie
      in zwei Runden (`tools/mis2_dump_geometry_study.py`, Ergebnisse V1/V2 in
      `staging_models/mis2_dump_geometry_study*.json`):
      V1 (Market-Entry, SL ≤8 %) — alles negativ, Diagnose: selektierte Coins
      zucken vor dem Dump nach oben und reißen die Stops. V2 auf Operator-Input
      („mehr SL-Abstand") + Struktur-Analogie zu EPD1/RUB1: **Limit-Entry 5 %
      über Signalkurs (in den Bounce verkaufen) + horizontabhängige weite SLs**
      dreht 24h/72h/168h ins Plus.
      **Deployte Regeln (alle: Entry Limit +5 %, Close-Basis-Modelle,
      Operating Point Top-2 %-Val-Quantil):**
      8H TP−5/SL5 (Studie −0,24 %/Trade — Operator will Live-Beweis, Einwand
      dokumentiert) · 24H TP−10/SL16 (+0,49) · 72H TP−15/SL12 (+0,72) ·
      168H TP−16,7/SL12 (+0,27).
      **Operator-Entscheide:** 20x wird gepostet (Cross, kleine Positionen auf
      großes Depot — bewusst KEIN cap_leverage_to_sl trotz SL > Isolated-
      Liq-Distanz); alle 4 Horizonte als Trades (kein Warn-Kanal).
      **Bekannte Folgearbeit:** Der Trade-Monitor kennt keine Limit-Entries —
      MIS2-SHORT-Scoring muss „Entry nie gefüllt" (Preis erreicht +5 % nicht,
      12–22 % der Signale) erkennen, sonst werden Phantom-Trades gescored.

---

## 2. ABR1 — Break & Retest ✅ (Intent bestätigt 2026-07-06)

**Soll (bestätigt):** Nach dem Bruch eines
S/R-Levels hält der ERSTE Retest des Levels → Continuation in Bruchrichtung; scheitert
der Ausbruch (Preis fällt zurück über/unter das Level), ist das die Verlustklasse.
ML filtert Continuation von Failed Breakout.

**Ist:** Detektor-Rework 2026-07-05 hat die Live-Erkennung erstmals AUF diese Idee
ausgerichtet (Richtungs-Kopplung des Retests, Hold-Check, nur Erst-Touch, keine
repaintenden Rand-Pivots, nur jüngste geschlossene Kerze) + 5 Setup-Geometrie-Features.
Walkforward auf dem neuen Detektor läuft. **Kein Konzept-Drift — im Gegenteil, die
alte Implementierung wich von der Idee ab** (Failed Breakouts wurden als Entry
signalisiert). Label = TP1-vor-SL der geposteten Geometrie: für einen Trade-Filter
intent-konform.

**Entschieden (Michi, 2026-07-06):**
- [x] Intent-Satz bestätigt; Label bleibt Trade-Geometrie (TP1-vor-SL) — für einen
      Detektor-Filter die richtige Frage.
- [x] ~~**LONG-Seite bleibt IMMER offen**~~ **REVIDIERT am Abend des 2026-07-06
      (Michi):** Der LONG-Immer-Bypass produzierte ~60 Signale in 3h über das
      657-Coin-Universum; Report 21 (Exit-Resim + ML-Selektion + BTC-Regime auf
      27,7k Events) zeigt: Setup ungefiltert −0,59 %/Trade, Break-even-WR ~63 %,
      kein getesteter Hebel dreht LONG positiv. LONG läuft wieder über den
      Legacy-Blocker (3-Klassen-Modell ohne meta.json, Gate 0,60 ≈ zu).
      Reaktivierung nur mit neuen Datenquellen oder Regimewechsel (Report 21 §3).
- [x] **LONG-Funding-Gate-EXPERIMENT (Michi, 2026-07-06 spätabends):** Nach dem
      Feature-Recheck auf Operator-Hypothese („falsche Indikatoren") wurden 16
      Setup-Mechanik-Features + 6 Funding-Features getestet (Report 21
      Addendum 2). Einziger Out-of-Sample-Überlebender: **fund_24h > +3 bps**
      (Longs zahlen Prämie über Binance-Default) → +1,12 %/Trade, 74 % WR
      (n=119/Jahr auf 100 Coins; Test +0,69 %, n=17 — dünn). LONG öffnet jetzt
      NUR über dieses Gate (live REST, fail-closed, 30-min-Cache), postet als
      ABR2 mit Funding-Wert in der Info-Nachricht. Erwartung ~1–2 Signale/Tag.
      **Review nach 4–6 Wochen** (≥30 Trades): Cornix-Tracking entscheidet.
- [x] **SHORT-Funding-Veto (Michi, 2026-07-06):** Spiegeltest auf 33,5k
      SHORT-Events — `fund_24h > +1,5 bps` ist für SHORTs in Train UND Test
      konsistent verlustig (−1,2 %/Trade; exakt die Zone, in der das LONG-Gate
      öffnet → unabhängige Kreuzvalidierung des Funding-Signals). SHORTs
      brauchen jetzt Modell-Gate ≥0,75 UND fund_24h ≤ +1,5 bps; fail-open
      (Veto ist Sicherheitsnetz, nicht Primär-Gate). Review zusammen mit dem
      LONG-Experiment.
- [ ] Batch-E-Threshold (SHORT 0,75 aus dünner Validation) nach Abschluss der
      laufenden Sim mit `pick_threshold_safe` neu bestimmen.

---

## 3. TD — Three-Drive / RSI-Divergenz ✅ (Intent bestätigt 2026-07-06)

**Soll (bestätigt):** Drei aufeinanderfolgende höhere Hochs (bzw. tiefere Tiefs),
deren RSI an den Pivots fällt (bzw. steigt) = Momentum-Erschöpfung → Reversal-Entry.
Faktisch eine RSI-Divergenz-Strategie an Mehrfach-Extrema. ML filtert die Muster;
Label = Trade-Geometrie (bestätigt).

**Ist:** Detektor unangetastet (Bot-eigene Erkennung wird im Replay abgespielt);
Label = TP1-vor-SL der geposteten Geometrie. Alter Trainer hatte Hindsight-Entry +
fixe 2R-Geometrie — der Replay-Fix ist eine Korrektur HIN zur gehandelten Realität.
Ergebnis Batch E: TD_4H kleiner echter Edge; TD_1H kein lernbarer Edge auf dem
20-Feature-Set.

**Entschieden (Michi, 2026-07-06):**
- [x] Intent-Satz bestätigt.
- [x] **TD_4H: Staging-Modell deployen** (vorher Threshold mit `pick_threshold_safe`
      neu bestimmen; Rollout-Checkliste aus `staging_models/REPORT.md` beachten).
- [x] **TD_1H: ML-Gate NEU DESIGNEN** statt parken — nicht das alte Gate behalten.
      Ansatzpunkte für den Neuentwurf: Pattern-Geometrie-Features (Divergenz-Stärke,
      Drive-Symmetrie, Pivot-Abstände — analog ABR1-Setup-Features), 1h+4h-Pooling
      gegen die dünne Datenlage, ggf. andere Zielgröße. Eigener Task; bis dahin
      läuft TD_1H im Ist-Zustand weiter.

---

## 4. BB — Breaker Block ✅ (Intent bestätigt 2026-07-06)

**Soll (bestätigt):** Gebrochener Support wird Resistance (und umgekehrt);
Retest des gebrochenen Levels → Entry in Bruchrichtung. Am besten abgesicherte
SMC-Idee; auf 4h groß genug für Fees, auf 1h nicht.

**Ist:** Wie TD — Detektor unangetastet, Label jetzt gehandelte Geometrie. Alter
Trainer hatte Features an der falschen Kerze (Breakout statt Retest) — behoben durch
den Replay. BB_4H: echtes Ranking (+5 pp), aber Test-PnL negativ → nur als Filter.

**Entschieden (Michi, 2026-07-06):**
- [x] Intent-Satz bestätigt.
- [x] **BB_4H: Staging-Modell als Filter deployen** (Threshold vorher mit
      `pick_threshold_safe` neu bestimmen; PnL-Hebel bleibt Exit-Geometrie).
- [x] **BB_1H: NEU ÜBERARBEITEN** (eigener Task, analog TD_1H-Gate-Redesign) —
      nicht bloß parken. Arbeitsannahme bis zur Überarbeitung: Parking
      vervollständigen (SHORT-Lücke schließen), damit kein halb-geparkter
      Zustand weiterfeuert — Veto möglich, falls SHORT bewusst offen bleiben soll.

---

## 5. SRA1 — ML-Qualitätsfilter über Support/Resistance ✅ (Intent + Label-Semantik bestätigt 2026-07-06)

**Soll (rekonstruiert):** Kein eigener Signalgeber: Die klassische S/R-Strategie
erzeugt die Kandidaten, das ML sagt nur „diesen nehmen / diesen nicht"
(Meta-Labeling). Label = echtes Trade-Ergebnis derselben Strategie.

**Ist:** Konzeptionell gesündestes Setup der Flotte, kein Batch-E-Retrain.

**Entschieden (Michi, 2026-07-06):**
- [x] Intent-Satz bestätigt (reiner Meta-Filter).
- [x] **Label-Semantik GEKLÄRT:** SL1/SL2/SL3 = SL nach TP1/TP2/TP3 getroffen =
      Trailing-Gewinn-Exits → Label `WIN` ist KORREKT. Die offene Audit-Frage
      (Report 13/16) ist damit vom Operator beantwortet; kein Label-Blocker mehr.
- [x] ATR-Crash: war bereits gefixt (P1.20). Label-Semantik zusätzlich per
      Code-Beweis bestätigt (13-updatesupportresistance zählt erreichte Targets).

**SRA2-Retrain durchgeführt 2026-07-06 nachts — Ergebnis: NICHT deploybar.**
`tools/retrain_sra2.py` (22 skalenfreie Features, Look-ahead-Fix inkl.
TZ-Korrektur Europe/Bucharest→UTC, NaN nativ, Isotonic + Safe-Threshold;
7.967 Events):
- LONG: Test 448 Trades @0,64 → WR 42,0 % (Basis 38,5 %, nur +3,5 pp Uplift),
  Ø **−1,61 %/Trade** — Val-Test-Bruch; Testfenster (Jan–Feb 26) war Bärenphase.
- SHORT: Safe-Picker verweigert ehrlich (kein Operating Point mit positivem
  Ø-PnL bei n≥100).
- **Root-Blocker entdeckt:** Label-Quelle `closed_trades3` ist TOT seit
  23.02.2026 (Writer 13-updatesupportresistance in _X läuft nicht mehr) —
  Trainingsdaten enden vor 4,5 Monaten, S/R-Outcomes seither ungetrackt.
  → Task #5: Label-Pipeline wiederbeleben (bevorzugt Replay-Labels statt
  fragiler Tracker), DANN SRA2 wiederholen. **SRA1 bleibt unverändert live.**

---

## 6. ATS1 — TSI-Crossover-Sniper ✅ (Intent bestätigt 2026-07-06)

**Soll (bestätigt):** Nur beim TSI-Fast-Crossover auf der letzten geschlossenen
Kerze (Event-Gate) wird ein Richtungsmodell befragt. Architektur-Blaupause: live wird
exakt die trainierte Event-Population gescored.

**Ist:** Kein Retrain bisher; bekannte Defekte: OBV-Train/Serve-Skew (invertiert die
Confidence-Ordnung), Label 2,5 %/1,5 %-Bracket ≠ Live-Geometrie, Daten stale.

**Entschieden (Michi, 2026-07-06):**
- [x] Intent-Satz bestätigt.
- [x] Operating-Band [0,60, 0,80): von Michi bestätigt — war bereits umgesetzt
      (12_ai_ats_bot.py:30-35, Audit-Batch 03./04.07.; ≥0,80 geht in Shadow).
- [x] **Retrain eingeplant** (Warteschlange nach SRA1): skalenfreie OBV-Features,
      Label = gepostete Geometrie via Replay, frische Daten, eigener
      Walkforward-Adapter (Event-gated wie live).

**ATS2-Retrain-Infrastruktur gebaut (T-2026-CU-9050-121):** DB-basiert über
`core.candles` (R1-clean, `include_forming=False`; kein CSV — die alten
`X8-TSI-EXPORT/-ML`-Skripte in `_X` sind abgelöst). Der geteilte Feature-Builder
`core/ats_features.py` wird von Bot 12 UND dem Trainer aufgerufen
(`build_ats_features` → Trainer==Serving, bewiesen vom Parity-Test
`backtest/test_ats_features.py`) und behebt so den OBV-Train/Serve-Skew; Label =
First-Touch der geposteten HVN/S-R-Geometrie via `simulate_exit`
(`core.trade_utils.hvn_sr_trade_geometry`, byte-identisch zur Bot-Geometrie)
statt des alten 2,5/1,5 %-Brackets. Event-gated Walkforward-Adapter
`tools/walkforward_sim.py --strategy ats`, Training
`tools/retrain_from_replay.py --strategy ats` (bzw. Ein-Kommando
`tools/retrain_ats.py --days/--since`) → `staging_models/ats2_model_{LONG,SHORT}.pkl`
(`model_id=ATS2`, chronologischer Split + 7d-Purge, `pick_threshold_safe`,
Isotonic-Kalibrierung). **Noch KEIN VPS-Trainingslauf/Deploy** — Artefakt-Erzeugung
+ Rollout-Empfehlung sind Michi-gegated (harte Regel 2).

---

## 7. EPD1 — Echtzeit-Pump-Ignition ✅ (Intent bestätigt 2026-07-06)

**Soll (bestätigt):** 10s-Ticks: plötzliche Volumen-Anomalie + Mikro-Momentum =
Ignition eines Moves → **mitfahren** (Pump → LONG, Dump → SHORT; kein Fade).
Eine der wenigen echten Kurzfrist-Edges in Alt-Perps.

**Ist:** Trainer sampelte nur `vol_ratio ≥ 5`-Events, Live scored ohne Gate (OOD) —
vol_ratio-Gate inzwischen umgesetzt, dazu aber ein „nur LONG"-Richtungs-Gate
(Audit-Batch). Daily-Retrain ist auskommentiert, loggt aber Erfolg. Gewinn stark
regimeabhängig (Alt-Pump-Phasen; Juli negativ → Drift-Watch Pflicht).

**Entschieden (Michi, 2026-07-06):**
- [x] Intent: Momentum-MITFAHREN in beide Richtungen bestätigt.
- [x] **Richtungs-Gate öffnen: BEIDE Seiten laufen** (das „nur LONG" fällt weg;
      vol_ratio ≥ 5-Gate bleibt). → Code-Änderung + Bot-Neustart nötig.
- [x] Falsches Erfolgs-Logging des auskommentierten Daily-Retrains entfernen.
- [x] **Retrain eingeplant** (Label = gepostete Geometrie, nur vol_ratio≥5-Events,
      Drift-Monitoring wegen Regimeabhängigkeit).
- [ ] **Funding-Features in den Retrain aufnehmen** (Operator, 2026-07-06):
      `core/funding_features.py` (geteilter Builder, Report 21 Addendum 2) —
      bei ABR trennt fund_24h Richtungserfolg sauber (LONG-Gate >+3 bps,
      SHORT-Veto >+1,5 bps, kreuzvalidiert auf 33,5k Events); für ein
      Momentum-MITFAHR-Modell plausibel richtungsentscheidend. Historie liegt
      voll in `funding_rates` (430d × 530 Coins).
- [x] **Replay-Adapter GEBAUT (2026-07-06 nachts):** `tools/epd2_build_dataset.py`
      — EPD ist 10s-Tick-basiert, bar-für-bar nicht nachspielbar; die
      Detektor-Logs (`pump_dump_events`) SIND die Events. Spiegelt Bot-10-
      Semantik: Alert-Gate vol_ratio≥5 beidseitig, Richtung = mitfahren,
      900s-Dedup, Post-Spike-Entry, **HVN/SR-Geometrie as-of**
      (`get_hvn_and_sr_levels(df=…)` + `hvn_sr_trade_geometry`), Label via
      `simulate_exit` (Skip-Entry-Hour, 7d-Horizont), 10 Live-Features
      (sample_fill=1.0 als dokumentierte Näherung) + 6 Funding-Features.

**EPD2-Retrain durchgeführt 2026-07-07 — BEIDE Richtungen NICHT deploybar.**
Datensatz nach DST-Fix: 85.031 Events / 639 Symbole (2026-02-25→07-07,
mehr Historie gibt es nicht — Log-Beginn), gelabelt 78.351;
`retrain_from_replay.py --strategy epd` (16 Features = 10 Live + 6 Funding,
Chrono-Split, 7d-Purge, Safe-Threshold):
- LONG (45.760 Events, Basis 52,2 %): Safe-Picker verweigert; bester
  Val-Punkt −0,97 %/Trade. Test-Kalibrierung monoton in der WR
  (43,9→69 %), aber **jedes Bucket im Ø-PnL negativ** — das Modell rankt
  TP1-Wahrscheinlichkeit, die gepostete Geometrie hat trotzdem negatives EV.
- SHORT (32.591 Events, Basis 60,0 %): Val formal deploybar @0,674
  (+0,09 %/Trade, hauchdünn), aber **Val-Test-Bruch**: Test 1.204 Trades,
  WR 68,2 % == Basisrate (null Selektion), −0,90 %/Trade.
- **Monats-Split: kein einziger positiver Monat in KEINER Richtung**
  (LONG Ø −0,05…−3,66; SHORT Ø −0,00…−3,93). Anders als bei RUB-LONG
  (§8) ist hier im verfügbaren Fenster auch kein Bull-Regime-Rettungsanker
  sichtbar — wobei die 4,5 Monate keine starke Alt-Pump-Phase enthalten
  (EPD1s profitable Phasen lagen laut Step-4-Vermessung davor).
- Konsequenz: kein Deploy; Ist-Zustand (Bot 10 mit Alt-Modell, beide
  Richtungen offen per Operator-Entscheid) läuft weiter, Drift-Watch
  bleibt Pflicht. Artefakte liegen in staging (`epd2_model_{LONG,SHORT}.pkl`),
  Stats `retrain_epd2_stats.json`. Wiedervorlage: Retrain erneut, sobald
  eine echte Alt-Pump-Phase in den Logs ist (Regime-Fenster-These §8).

**EPD2-DB-Pfad auditiert (T-2026-CU-9050-121):** bestätigt bereits DB-basiert,
R1-clean und CSV-frei — `tools/epd2_build_dataset.py` liest die Events aus
`pump_dump_events`, den Entry aus `ticker_10s` und Geometrie/Indikatoren über
`core.candles` (`read_candles_with_indicators(include_forming=False)`), schreibt
JSONL (kein CSV) nach staging. **Kein candle-basierter Pump-Trainer möglich** —
die Live-Features sind 10s-Tick-basiert (nicht aus 1h-OHLCV rekonstruierbar,
harte Regel 7); die Event-Log-Route IST der DB-Retrain. Kein Fix nötig; für
symmetrische Ein-Kommando-Bedienung neu: `tools/retrain_pump.py --days/--since`
(kettet Build + `retrain_from_replay --strategy epd`). Wiedervorlage unverändert.

---

## 8. RUB1 — Rubberband Mean Reversion ✅ (Intent bestätigt 2026-07-06)

**Soll (rekonstruiert):** Extreme Dehnung vom „fairen Wert" (≥8 % von der
90d-Regression + RSI-/TSI-Extrem + Donchian-Touch) → Snap-Back handeln.

**Ist:** ML-Layer nachweislich Rauschen (MACD 9/21 trainiert, live 12/26 gefüttert;
Random-Split-Memorization). Live-Gewinn stammt aus Vorfilter + S/R-Targets + SHORT-Tails.

**Entschieden (Michi, 2026-07-06):**
- [x] Intent-Satz bestätigt (Snap-Back nach Mehrfach-Extrem; ML trennt Snap-Back
      von weiterlaufendem Messer).
- [x] Retrain-Label: **Geometrie mit SL-Pfad** (First-Touch TP1-vor-SL — der
      Drawdown-Pfad steckt via SL-Touch automatisch drin). Gleiche Infrastruktur;
      braucht einen RUB1-Adapter im Walkforward (Vorfilter-Events nachspielen).
- [x] **Adapter GEBAUT (2026-07-06 nachts):** `walkforward_sim.py --strategy rub`
      — Vorfilter/Regression/9-Feature-Vertrag nach `core/rub_features.py`
      gehoben (EINE Quelle, Bot 13 refaktoriert und nutzt sie live; X-R1).
      Replay je geschlossener 1h-Kerze: 95d-Regression as-of, 4h-Cooldown je
      Richtung wie live, Geometrie = `get_hvn_and_sr_levels(df=…)` +
      `hvn_sr_trade_geometry` + `ensure_min_tp_distance`, Label via
      `simulate_exit`; Feature-Dict enthält zusätzlich die 6 Funding-Features.
- [x] **LONG-Gate WIEDER ÖFFNEN** (Operator-Entscheid, revidiert den Audit-Batch:
      Idee ist symmetrisch, LONG-Schwäche womöglich Artefakt des kaputten ML).
      → Code-Änderung + Bot-Neustart nötig.
- [x] **Funding-Features in den Retrain aufnehmen** (Operator, 2026-07-06):
      `core/funding_features.py` (geteilter Builder, Report 21 Addendum 2).
      Für Mean-Reversion besonders interessant: extremes Funding = überfüllte
      Seite → Snap-Back-Kandidat vs. weiterlaufendes Messer. Historie voll in
      `funding_rates`. → Umgesetzt: 15-Feature-Vertrag (9 rub + 6 funding).

**RUB2-Retrain durchgeführt 2026-07-07 vormittags — LONG NICHT deploybar,
SHORT deploybar @0,829.** Replay `rub_replay_365d.jsonl` (365d, 530 Coins,
97.641 Events; Lauf war durch den VPS-Ausfall 04:42 unterbrochen und wurde
per `--resume` ab Coin 433 fertiggerechnet), Trainer
`retrain_from_replay.py --strategy rub --days 365` (Chrono-Split + Purge,
Isotonic, Safe-Threshold):
- LONG (52.081 Events, Basis TP1 60,6 %): Val-Kurve auf ALLEN Thresholds
  negativ (Ø −0,9…−1,2 %/Trade), Safe-Picker verweigert (threshold null,
  Test 0 Trades). Damit ist die Operator-Hypothese „LONG-Schwäche =
  Artefakt des kaputten ML" durch den sauberen Retrain NICHT bestätigt —
  auch das saubere Modell findet keinen profitablen LONG-Operating-Point.
  Kalibrierung invertiert im PnL: niedrige Prob-Buckets tragen die besten
  Ø-PnLs (Tail-Snapbacks), d. h. TP1-Wahrscheinlichkeit ≠ Erwartungswert.
- SHORT (45.560 Events, Basis TP1 73,9 %): thr 0,829, Val +0,25 %/Trade
  (WR 81,5 %), Test 680/4.725 Trades, WR 81,9 % vs. Basis 79,1 %,
  Summe +432 %P (**+0,64 %/Trade netto**) — konsistent mit dem bekannten
  SHORT-Tail-Befund. Top-Features: slope_trend, dist_to_trend, dist_ema200;
  fund_7d_cum/fund_72h auf Platz 5/6 (Funding trägt real bei).
- Artefakte: `staging_models/rub2_model_{LONG,SHORT}.pkl` + Stats
  `retrain_rub2_stats.json`.

**Deploy (Operator-Entscheid 2026-07-07): SHORT LIVE in Bot 13.**
`rub2_model_SHORT.pkl` ins Repo-Root kopiert (P1.35); Bot 13 lädt den
Artefakt-Contract (Bot-25-Muster), baut die 6 Funding-Features as-of aus
`funding_rates` (lazy je Event; fehlende Historie ⇒ 0 = `fillna(0)`-Parität)
und gatet auf roher predict_proba @0,829. Fallback Legacy @0,85, falls das
Artefakt fehlt. Freshness-Infra: Scheduled Task „Kythera Funding Backfill"
(stündlich; Tabelle hatte keinen Live-Writer). LONG läuft unverändert auf
dem Legacy-Modell @0,75 (Operator: Gate bleibt offen).

**Regime-Befund zur LONG-Seite (Monats-Split des Replays, 2026-07-07):**
Operator-These „LONG greift im Bull-Market" wird von den Daten gestützt —
ungefilterte LONG-Events: Aug 25 +3,9 %/Trade (n=4.321), Sep 25 +2,4 %,
Apr 26 +3,0 %, aber Okt 25 −3,6 %, Nov 25 −4,8 %, Jan 26 −3,4 %. Die
Schwankung ist ein Regime-Effekt, kein Ranking-Problem des Modells
(das Event-Ranking bleibt auch im Retrain wertlos). Konsequenz: LONG
braucht ein **Regime-Gate** (Bull-Phasen-Schalter) statt eines
Event-Gates — Kandidat für die HMM-Regime-Studie T-2026-CU-9050-020
bzw. Whitelist/ROM1-Integration.

### 8a. MAX1 — High-Conviction-Drossel über RUB2-SHORT 🔨 (T-2026-CU-9050-067, default-off)

**Soll (Operator-Entscheid Michi, 2026-07-11):** RUB2-SHORT ist die stärkste
Short-Kante der Fleet (OOT +0,64 %/Trade netto; live seit 06.07.: 24 Closes,
79 % TP1-WR, +4,2 % Ø — T-2026-CU-9050-044), feuert aber ~9×/Tag. Für den
**Main-Channel** will Michi **1-3 Trades/Tag mit sehr hoher Trefferquote**.
Nicht umgesetzt wird die Drossel *in* RUB2 (T-2026-CU-9050-050 → **wontfix**:
RUB2 bleibt unverändert in seinem Channel). Stattdessen **MAX1**: ein eigener
Bot, der dasselbe Modell fährt, aber nur die stärksten Kandidaten postet.

**Mechanik (`34_ai_max1_bot.py` → `core/max1_gate.py`, reine Selektion):**
- **Klon, kein Refactor:** Detection/Features/Funding-As-of kommen aus den
  geteilten Buildern (`core/rub_features.py`, `core/funding_features.py` —
  importiert, nicht angefasst, X-R1), die Geometrie aus dem geteilten
  `hvn_sr_trade_geometry`. Damit gilt die gemessene RUB2-SHORT-Winrate für genau
  die Trades, die MAX1 stellt. Bot 13 bleibt Byte-für-Byte, wie er ist.
- **Zwei-teilige Drossel:** hohe **Mindest-Probability** (`MAX1_MIN_PROB`, Default
  **0,93**, nie unter dem Artefakt-Threshold 0,829) als eigentlicher Selektor, plus
  eine **harte rollierende 24h-Kappe** (`MAX1_MAX_PER_DAY`, Default **3**) als
  Backstop. Rollierend statt Kalendertag — kein Mitternachts-Burst.
- **Selektion je Scan:** alle Kandidaten über dem Gate sammeln, per Symbol
  deduplizieren (stärkster gewinnt), deterministisch sortieren (prob desc, Symbol),
  auf die freien Slots der 24h-Kappe schneiden.
- **24h-Zähler aus `ml_predictions_master`** — Shadow **und** Live, damit die Kappe
  im Shadow exakt wie live greift (getreue Vorschau). Vertrag des MAX1-Tags in
  dieser Tabelle: **eine Zeile je Selektion, nie je abgelehntem Kandidaten** —
  darunterliegende Predictions persistiert der RUB2-Scan bereits unter seinem Tag.
- **Scan Minute 15** (RUB2: Minute 10) — dieselbe geschlossene 1h-Kerze, nur
  entzerrt gegen den zweiten Voll-Scan auf der DB.
- **Posting** über `core.signal_post.post_ai_signal` (genau EINE Cornix-Message,
  Regel 4). Tag aus `meta.model_id` des Artefakts (Regel 6 / Falle 16).

**Artefakt:** `max1_model_SHORT.pkl` (Repo-Root, **promoted 2026-07-11** per
Operator-Entscheid Michi; auf dem VPS erzeugt mit sklearn 1.7.1, Load-Verify
`True MAX1 0.829 15 True` — T-2026-CU-9050-070) — Kopie des RUB2-SHORT-Modells
mit `meta.model_id=MAX1`, erzeugt von `tools/make_max1_artifact.py` (Modell,
Feature-Vertrag, Kalibrator, Val-Operating-Point verbatim; nur die Identität
wechselt). Neuerzeugung nach jedem RUB2-SHORT-Retrain **auf dem VPS** (die
Library-Versionen des Quell-Artefakts leben dort); die Promotion bleibt je
Generation Michis Entscheid. Ohne Artefakt läuft Bot 34 im Idle-Modus.

**RUB2-Interaktion (by design):** Cooldown-, Dedupe- und Offene-Trade-Räume sind
über den Tag getrennt (`MAX1` vs. `RUB2`) — die beiden Bots **blocken sich nicht
gegenseitig**. Folge: **Doppel-Exposure auf demselben Coin ist möglich** (RUB2
postet in seinen Channel, MAX1 zusätzlich in den Main-Channel; wenn Cornix beide
Channels tradet, läuft die Position doppelt). Das ist die bewusste Konsequenz aus
„RUB2 bleibt unverändert" — die Positionsgröße dieser Überlappung steuert Michi
über die Cornix-Konfiguration der beiden Channels.

**Gates (default-off, Flip = Michis Entscheid):**
- `MAX1_LIVE_POSTING=0` (shadow-first; ohne Flip nur Shadow-Zeilen).
- `CH_MAX1` ungesetzt ⇒ Fallback `CH_MAIN`; beide ungesetzt (0) ⇒ Shadow-only.

**Zwei Lesehinweise für die Shadow-Zahlen** (sie sind die Datenbasis des
Threshold-Entscheids — nicht überlesen):
- Die persistierte `confidence` ist die **rohe** predict_proba — dieselbe Domäne wie
  Gate, 044-Kurve und die RUB2-Zeilen. Der kalibrierte Wert steht nur in der
  Info-Message.
- Die **Shadow-Frequenz ist eine Obergrenze**: live schreibt ein Post eine
  `ai_signals`-Zeile, die weitere Selektionen desselben Coins bis zum Close sperrt;
  im Shadow existiert diese Zeile nicht, dort drosselt nur der 4h-Cooldown. Shadow
  zeigt also eher etwas **mehr** Posts/Tag als live — nie weniger.
- MAX1 scannt das **volle Coin-Universum** aus `coins.json`, nicht die kuratierte
  `MAIN_CHANNEL_COINS`-Liste. Der Main-Channel sieht damit auch Alts, die er heute
  nicht sieht. Eine Einschränkung wäre ein eigener Operator-Entscheid.

**Offen / Bestätigung einholen:**
- [x] **Shadow-Gate-Zahlen** (Operator-Entscheid Michi 2026-07-11, Ziel =
      **maximale Trefferquote**, T-2026-CU-9050-070): `MAX1_MIN_PROB=0,85` +
      `MAX1_MAX_PER_DAY=3` — bewusst NICHT der Default 0,93. Live-Kurve
      (06.–11.07., 44 posted/28 closed): höchste WR im Band 0,829–0,85
      (81–82 %, n=21–28); ab 0,88 **fällt** die WR (60–71 %) und nur der Ø-PnL
      steigt. ≥0,88-Kandidaten clustern zudem in Funding-Episoden (24h-Kappe
      liefert dann ~0,7/Tag). Achtung: die **Replay-Kurve ist für dieses Gate
      unbrauchbar** — Live↔Replay-Prob-Korrelation −0,37 auf gematchten
      Signalen, Feature-Skew-Verdacht Funding (T-2026-CU-9050-071). **Finale**
      Zahlen nach 1–2 Wochen Shadow (dann misst `ml_predictions_master` die
      kappen-gebundene Selektions-WR direkt); wenn die WR-Inversion hält, auch
      Selektionsreihenfolge/Prob-Band statt Floor prüfen.
- [ ] **Scharf-Schalten** (`MAX1_LIVE_POSTING=1` + Cornix-Konfiguration des
      Main-Channels) — nach Shadow-Auswertung, ausschliesslich Michis Entscheidung.

---

## 9. AIM1 → AIM2 — Meta-Gate über alle Signale ⚠ (Konzept bewusst geändert — Bestätigung einholen)

**Soll AIM1 (historisch):** Stacking über alle Bot-Signale: Marktkontext ×
Schwarm-Verhalten × Quell-Identität → Erfolgswahrscheinlichkeit je Kandidat.
**Befund:** Idee gut, Architektur verletzte alle Voraussetzungen; Modell war
verlässlich invertiert (F). Nicht rettbar per Retrain.

**Ist AIM2 (Neubau 2026-07-05, Parallel-Session):** BEWUSSTE Konzeptänderung —
kein eigenständiger Alpha-Generator mehr, sondern Ranker/Gate über gepostete
Quellsignale; Label = First-Touch der rekonstruierten Geometrie; läuft shadow-only
(Posting per `AIM2_LIVE_POSTING=1` freigegeben, Channel wird nicht getradet).

**Entschieden (Michi, 2026-07-06):**
- [x] **Neudefinition als neues Soll abgesegnet**: AIM2 = Ranker/Gate über
      geposteten Quellsignalen. Die AIM1-Idee (eigenständiger Signalgeber) ist
      offiziell Geschichte.
- [x] **Rollout: SOFORT SCHARF** — keine weitere Shadow-Phase; Posting zählt ab
      jetzt (Flag war bereits aktiv, das Traden des Channels konfiguriert Michi
      in Cornix). Drift-/Kalibrierungs-Monitoring läuft trotzdem weiter.

### 9a. AIM2-TOPN — "Top 1-3 des Tages" als High-Conviction-Kanal 🔨 (T-2026-CU-9050-051, default-off)

**Soll (aus T-2026-CU-9050-031, Weg 2):** der strukturelle Weg zu „täglich 1-3
Trades, sehr hohe Winrate". AIM2 rankt bereits die ganze Fleet (OOT-Gate-Uplift
−0,69 → +1,92 %/Trade @34 % Pass). Statt „alles über der Linie" (≈110/Tag)
selektiert AIM2-TOPN **höchstens N (1-3) der stärksten Kandidaten des Tages** und
routet sie in einen **eigenen Kanal/Tag** (`AIM2-TOPN`, Regel 6) — per Konstruktion
wenige, hoch-selektierte Trades, getrennt vom Basis-AIM2-Posting.

**Mechanik (Bot 15 → `core/aim2_topn.py`, geteilte reine Logik):**
- „Top-N des Tages" ist erst ex-post bekannt → approximiert über eine hohe
  **Mindest-Probability** (`AIM2_TOPN_MIN_PROB`, Default 0,95, nie unter dem
  Basis-Gate-Threshold) plus eine **harte rollierende 24h-Kappe** N
  (`AIM2_TOPN_N`, Default 1). Rollierend statt Kalendertag — kein
  Mitternachts-Burst (23:50 + 00:10 = 2·N in 20 min).
- Selektion je Zyklus: nur `trusted` (Parity-Guard bestanden) & `prob ≥ min_prob`,
  Dedupe je (Coin, Richtung, stärkste), deterministischer Tie-Break
  (prob desc, coin, direction), Kappe = `N − posts_last_24h`.
- 24h-Zähler aus `ml_predictions_master` (Shadow **und** Live), damit die Kappe
  im Shadow exakt wie live greift → getreue Vorschau.
- Posting über den auditierten `core.signal_post.post_ai_signal` (genau EINE
  Cornix-Message, Regel 4). Der TOPN-Tag ist aus AIM2s eigenem Kandidaten-/
  Schwarm-Stream ausgeschlossen (F6-Selbst-Feedback).

**Gates (alle default-off, Flip = Michis Entscheid):**
- `AIM2_TOPN_ENABLED=0` (Master-Schalter; aus ⇒ **null** Verhaltensänderung an
  Basis-AIM2).
- `AIM2_TOPN_LIVE_POSTING=0` (shadow-first, analog `AIM2_LIVE_POSTING`).
- `CH_AIM2_TOPN` ungesetzt ⇒ erzwingt Shadow-only (kein Fallback auf den
  AIM2-Kanal).

**Offen / Bestätigung einholen:**
- [ ] **Schwellen-Kalibrierung** aus der VPS-DB via `tools/aim2_topn_calibrate.py`
      (read-only): welcher `min_prob` liefert historisch ~1-3/Tag? Bis dahin
      läuft der konservative Default 0,95.
- [ ] **Scharf-Schalten** (`AIM2_TOPN_ENABLED=1`, dann `AIM2_TOPN_LIVE_POSTING=1`
      + `CH_AIM2_TOPN` setzen + Cornix-Konfiguration des Kanals) — ausschliesslich
      Michis Entscheidung nach einer Shadow-Auswertung.

---

## 10. UFI1 — Dead-Cat-Bounce-Short ⚠ REAKTIVIERT auf Operator-Entscheid (2026-07-06)

**Soll:** Gedumpte Coins beim Retracement-Bounce shorten (Daily).

**Entschieden (Michi, 2026-07-06):** **Wieder aktivieren im IST-Zustand (20x),
bewusst als „Crash-Monat-Lotterieschein" mit kleinen Positionen.** Der Einwand
wurde vorgetragen und überstimmt — dokumentiert: ehrlicher Walk-Forward zeigt
11/12 Monate negativ (~14 % WR), +185R kamen allein aus Oktober 2025, und bei 20x
liegt die Liquidation (~+5 %) VOR dem SL (25–40 %) — 72 % der historischen Trades
wären liquidiert worden. Positionsgröße klein zu halten ist Operator-Sache
(Cornix-Konfiguration). Kein Retrain; Entparken + Neustart als Action-Item.

## 11. ATB1 → ATB2 — Konvergenz-Kanal-Breakout 🔨 NEUAUFBAU, Design verschmolzen (2026-07-07)

**Soll (Michi 2026-07-06, erweitert 2026-07-07):** „Die" Trendlinie = **Linie durch
bestätigte Swing-Pivots mit ≥3 Berührungen** (1h/4h, objektiv reproduzierbar).
Per Operator-Entscheid 2026-07-07 verschmolzen mit der Event-Definition aus dem
TradingView-Script „Breakout Pattern Setup [WillyAlgoTrader]" (Open Source):
**konvergierende Kanäle** (Wedge/Triangle/Pennant) statt Einzellinien —
Boundary-Fit an bestätigte Pivots, Validierung über Konvergenz (≥2 % Verengung),
Kanalbreite 0,5–120× ATR, Touch-Toleranz 0,15× ATR und **Volume-Contraction im
Kanal** (In-Channel-Volumen < 85 % des Vorlaufs — bei uns bisher ungetestet).
Event: Ausbruch mit bestätigtem Kerzenschluss.

**Bewusste Abweichungen vom Script:** Min-Touches 3 statt 2 (Operator-Intent);
der 5-Faktor-Score (Penetrationstiefe/ATR, Body-Ratio, Body-Commitment,
Volumen-Spike, RSI-Momentum) wird NICHT als handgewichteter Score übernommen,
sondern als **5 Setup-Features fürs XGB-Gate** (analog ABR-Geometrie-Features);
Targets = Measured-Move (⅓/⅔/volle Kanalbreite) als Kandidat GEGEN unsere
Smart-Targets im Replay-Vergleich; Break-even-Trailing des Scripts ist verdächtig
(QM-Lektion: gibt Gewinne zurück) → Exit-Varianten simulieren statt glauben.

**Plan (Task #7, nach der aktuellen Retrain-Queue):** Kanal-Detektor bauen
(kein Repaint: nur bestätigte Pivots, Closed-Candle-Break), Walkforward-Adapter,
Labels = First-Touch mit Fees via simulate_exit, Geometrie-Vergleich
Measured-Move vs. Smart-Targets, Retrain nach Standard-Gerüst (Safe-Threshold,
model_id=ATB2). Der alte Trainer (Close-Regressionsgeraden) ist verworfen;
Bot bleibt geparkt, bis ATB2 out-of-time validiert ist. Kein Backtest-Vertrauen
in das Script selbst — dessen „Winrate" ist TP1-Touch (Report-16-Falle).

**Status (T-2026-CU-9050-104, 2026-07-12):** Labeling-/Trainings-Pipeline
DB-frei gebaut + getestet — `core/atb2_features.py` (Kanal-Detektor + 5
Setup-Features + Kanalgeometrie, geteilt Bot/Simulator/Trainer),
`tools/walkforward_sim.py --strategy atb2` (Measured-Move-Label via
`simulate_exit`, Smart-Targets als Vergleich) und
`tools/retrain_from_replay.py --strategy atb2` (je Richtung, 3d-Purge-Split,
Isotonic, `pick_threshold_safe`, Artefakt `model_id=ATB2` → `staging_models/`).
Run-Book + Verdikt-Kriterien: `docs/ATB2_REBUILD.md`. Offen: Label/Train-Lauf
auf dem VPS (hinter T-061, Sequential-Jobs); Bot-Serving-Rewire + P1.45 +
Entparken erst nach deploybarem out-of-time-Verdikt (C-Gate).

---

## 12. Support Resistance (Classic) ✅ (Intent bestätigt 2026-07-06)

**Soll (bestätigt):** Wiederholter Test eines S/R-Levels + RSI-Divergenz zwischen
erstem und aktuellem Hit → Umkehr-Einstieg am Level, Targets aus Struktur-Zonen.

**Entschieden (Michi, 2026-07-06):**
- [x] Intent bestätigt.
- [x] Freigegebene Fixes: **Closed-Candle (R1) + TP-Interpolations-Fix (P0.7) +
      ATR-SL statt fix 2,5 % + OBV-Baustein streichen** (statistisch wirkungslos).
- [x] Kein Direction-Gate: LONG bleibt offen (SHORT trägt zwar den Gewinn,
      aber Michi will beide Seiten).

## 13. Main Channel (Classic) ✅ (2026-07-06)

**Entschieden (Michi):** Bleibt **getrennt** von Support Resistance — die
Doppel-Exposure (gleiche Logik, zwei Channels) ist bewusst und gewollt. Kein Merge.
ATR-SL-Idee wird trotzdem in Support Resistance übernommen (s. o.).

## 14. Volume Indicator (Classic) ✅ (2026-07-06)

**Soll (bestätigt):** Preis an 90d-High-Volume-Node + frischer Volumen-Spike gibt
Richtung → Einstieg an der Volumenzone.

**Entschieden (Michi):** **Umbau freigegeben** — den echten Kern retten: gebinnte
Volumen-Nodes (statt Float-Preis-Summierung), Frische-Pflicht für den Spike
(Stunden statt 5 Tage), Per-Coin-Cooldown, Struktur-Targets. Eigener Task.

## 15. 5 Percent (Classic) 🔨 REDESIGN beauftragt (2026-07-06, nachgeholt)

**Entschieden (Michi):** **Redesign mit früherem Entry** — das Kernproblem angehen
statt Symptome: die ~26 redundanten Bedingungen auf wenige unabhängige Filter
reduzieren (Trend-Etablierung, aber FRÜH statt ausgereizt), Entry-Timing nach
vorn, Zeit-Exit ergänzen; Fixes SHORT-Headroom-No-op (P1.14) + EMA-Typo (P2.43)
nebenbei. Validierung per Walkforward vor Live-Umstellung. Eigener Task in der
Redesign-Queue (nach QM/BB_1H/TD_1H). Bis dahin läuft der Ist-Zustand weiter.

## 16. Fast In And Out (Classic) ⚠ WEITERLAUFEN auf Operator-Entscheid (2026-07-06)

**Entschieden (Michi):** Läuft **unverändert weiter** — bereits im April bewusst
reaktiviert, heute erneut bestätigt (Audit-Einwand −25.843 netto / „Pennies vor
der Dampfwalze" wurde vorgetragen und überstimmt). Keine Zähmungs-Maßnahmen
gewünscht.

## 17. Quasimodo QM_1H/QM_4H 🔨 BEIDE überarbeiten (2026-07-06)

**Soll (bestätigt):** Liquidity Sweep + Strukturbruch; Retest der Sweep-Zone (QML)
als Reversal-Entry; ML nimmt die besten X %.

**Entschieden (Michi):** **Beide TFs überarbeiten** (auch QM_4H — nicht stoppen):
Neutraining nach Standard-Gerüst (Closed-Candle-Pivots, CMP-Entry im Label,
Threshold aus Artefakt respektieren) + **Exit-Redesign** (aktuelle Geometrie gibt
den 67-%-WR-Vorteil strukturell zurück). Eigener Task in der Queue.

## 18. BR-Familie BR1H/2H/4H 🔨 ML-Gate bauen (2026-07-06)

**Soll:** Break & Retest ohne ML (Pattern-Detector 7).

**Entschieden (Michi):** **Beide Richtungen wieder öffnen** (das „nur SHORT"-Gate
aus dem Audit-Batch fällt) und **ein ML-Gate über die BR-Signale bauen** — der
BB_4H-vs-BR-Vergleich (+565 mit ML vs. −4.106 ohne) motiviert genau das.
Plan: BR-Events im Walkforward nachspielen, Geometrie-Labels, Binärmodell je
TF/Richtung nach Standard-Gerüst. → Gate-Revert ist Action-Item; ML-Gate eigener
Task in der Queue.

## 19. Mayank (FVG) ✅ (2026-07-06)

**Entschieden (Michi):** Läuft weiter als **reiner Info-Kanal** — kein Tracking,
kein Ertragsanspruch, keine Arbeit daran.

## 20. BTC SMC 100x ⚠ (2026-07-06)

**Entschieden (Michi):** **100x bleibt bewusst** (Lotterieschein-Charakter;
Audit-Einwand Liquidation ~−0,9 % vor jedem SL wurde vorgetragen und überstimmt).
**Nur instrumentieren** (ai_signals-Tracking), damit der Bot erstmals messbar wird.
→ Instrumentierungs-Task.

## 21. SMC Forex/Metals ⚠ (2026-07-06)

**Entschieden (Michi):** Läuft **unverändert weiter** (Audit-Abschaltempfehlung
überstimmt). Kein Tracking, kein Repaint-Fix beauftragt.

## 22. Regime-Detection ✅ UMGESETZT + LIVE (2026-07-07)

**Entschieden (Michi):** **TRANSITION-Restklasse aufspalten** — Mid-Vola-Band
(P40–P75) bekommt eine eigene Trend-Regel, damit TREND_UP/DOWN überhaupt vorkommen
und das 4D-Gating nicht die halbe Zeit deaktiviert ist.

**Umsetzung (2026-07-07, Operator-Pick nach `tools/regime_rules_study.py`):**
Vol-skalierte Mid-Band-Regel **V2 K=1,5 mit Hysterese** in
`core/regime_logic.py`: |ret_4h| ≥ 1,5×ATR_4h% → TREND_UP/DOWN; bestehender
TREND hält bis |ret_4h| < 1,0×ATR (Hysterese via `prev_regime` =
effektives Regime aus `regime_current`); TREND-Ziele brauchen 3 statt 2
Debounce-Checks. Low-Vola-/HIGH_VOLA-/CHOP-Regeln unverändert.
- Studie (430d, 7 Varianten): Ist-Regel produzierte 3 TREND_UP-Episoden in
  430 Tagen (100 % <1h) — strukturell tot, weil ATR<P40 und |ret|>1,5 %
  einander fast ausschließen.
- Validierung mit finaler Regel (stateful, echte classify-Funktion):
  TREND_UP 9,6 % / TREND_DOWN 9,8 % der Zeit (je ~415 Ep, med 1,5h,
  Flaps 21–25 %), TRANSITION 41 %→20,8 %. **RUB-LONG in TREND_UP
  +1,65 %/Trade (n=1.378), 9/13 Monate positiv** — negativ nur in den
  tiefen Bear-Monaten Okt/Nov 25 + Jan 26 (Bull-Flackern im Bär = Falle)
  → bestätigt die Regime-Gate-These aus §8, ist aber kein Bear-Immunschutz.
- Deploy-Sicherheit: fehlende Whitelist-Zellen der neuen TREND-Zustände
  defaulten auf open (`no_whitelist_entry`) — kein Mass-Auto-Close-Risiko;
  die Zellen sammeln ab jetzt Daten. Tests: backtest/test_regime_detector.py
  (27, inkl. 7 neue für Mid-Band/Hysterese/Debounce-3).
- **Follow-up:** §23-Umbau (Shrinkage statt Default-Open) gehört zeitnah
  dahinter; RUB-LONG-Regime-Gate in Bot 13 erst nach Whitelist-Datenlage
  oder als expliziter TREND_UP-Schalter (Operator-Entscheid).

## 23. Bot-Regime-Analyzer / Whitelist ✅ Umbau beauftragt (2026-07-06)

**Entschieden (Michi):** Gate-Metrik von WR auf **Netto-Expectancy/Median** umstellen,
plus **Konfidenzintervall/Shrinkage + Mindest-n** (kein Default-Open, kein Flippen
auf Rauschen mehr). Eigener Task.

## 24. ROM1 / Orchestrator ✅ Rollen-Klärung (2026-07-06)

**Entschieden (Michi):** ROM1 wird als **eigenständiger Trading-Bot anerkannt**
(kein „reiner Router" mehr): eigene Trade-Historie fließt als Evidenz ins Gate,
SL-Distanzen werden gedeckelt (15 %-Cap aus dem Audit-Batch verifizieren/schärfen).
Eigener Task.

**Offener Punkt (Michi, 2026-07-07 → Task T-2026-CU-9050-020):**
**HMM-Regime-Studie** — Markov-Switching-Modell (3–4 Gauß-Zustände auf
BTC-4h-Features inkl. Funding) als Regime-Schicht, im direkten A/B-Vergleich
mit `26_regime_detector` (§22) und dem ROM1-Gating. Motivation: der gemeinsame
Fehlermodus ALLER Report-21-Fehlschläge war Regime-Nichtstationarität; ein
HMM-Posterior mit Zustandspersistenz ist die prinzipielle Version dessen, was
die Heuristik versucht. Prüfkriterium: hängt die Monats-Performance von
ABR-LONG/RUB out-of-sample an den Zuständen — und schlägt der Posterior die
bestehende Klassifikation als Gate-Feature? Kontext-Schicht, kein
Alpha-Generator; Details im Task.

## 25. Intelligence-Layer (Whale/Funding) ✅ Aufwertung beauftragt (2026-07-06)

**Entschieden (Michi):** Whale-Flows + Funding-Extremes werden **als Features in
Regime/Gate eingespeist** (statt totem Datensammeln). Eigener Task:
Feature-Engineering + Validierung; Whale-Logger läuft seit dem WS-Fix wieder.

## Arbeitsregeln (ab 2026-07-06)

1. Jedes künftige (Re-)Training referenziert diese Datei: Label muss die Soll-Frage
   des Modells beantworten; Abweichung nur mit dokumentierter Operator-Entscheidung.
2. Threshold-Wahl überall nach Operator-Kriterium: wenige, sichere Trades
   (`pick_threshold_safe`; „nicht deploybar" ist ein zulässiges Ergebnis).
3. Zwei Fragen, zwei Metriken: Treffer der Soll-Frage (z. B. Move-WR) UND Ökonomie
   der gehandelten Geometrie (Netto-Expectancy) werden IMMER beide berichtet —
   WR allein ist als KPI wertlos (Report 16, Befund 1).
4. Klassische Regel-Strategien (Support Resistance, 5 Percent, …) und Meta-Ebene
   (Regime/ROM1) sind in Report 16 konzeptbewertet; sie bekommen Einträge hier,
   sobald an ihnen gearbeitet wird.
5. **Immer nur EIN Trainings-/Simulationsjob gleichzeitig** (Operator-Regel
   2026-07-06): Walkforwards, Retrains, Labeler strikt nacheinander — die Maschine
   trägt die Live-Flotte, parallele Jobs treiben die CPU-Last hoch. Neue Jobs
   werden hinter dem laufenden eingereiht.
6. **Versionierte Modell-Tags** (Operator-Regel 2026-07-06, gilt für ALLE
   überarbeiteten Modelle): Jede Retrain-/Rework-Generation postet unter neuem
   Tag — MIS2-8H…, ABR2, TD2_4H, BB2_4H, SRA2, ATS2, EPD2, RUB2, QM2, … Das Tag
   steht als `model_id` in der Artefakt-Meta und wird vom Bot ins `ai_signals.model`
   geschrieben. Tracker (Sentiment-Kanal-Kreuztabellen, Dashboard, Whitelist)
   matchen per Präfix und zeigen Alt vs. Neu getrennt — so wird der Unterschied
   der Generationen direkt sichtbar. Cooldowns bleiben bewusst versionsübergreifend
   (kein Doppel-Posting Alt+Neu auf demselben Symbol).
7. **Eine Cornix-parsebare Nachricht pro Signal**: Info-/Chart-Nachrichten dürfen
   den Cornix-Block nicht wiederholen (Doppel-Post-Bug 2026-07-06 in Bot 18 + 7,
   gefixt — Cornix legte pro Signal zwei Positionen an).
