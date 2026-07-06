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
- [x] **Retrain eingeplant** (Warteschlange nach den MIS1-Move-Retrains):
      Preisspalten-Features raus, Kalibrierung dazu, gleiches Gerüst.
      ATR-Crash-Fix (35/38-Spalten-Loop) sofort davor, geht ohne Retrain.

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
- [x] **LONG-Gate WIEDER ÖFFNEN** (Operator-Entscheid, revidiert den Audit-Batch:
      Idee ist symmetrisch, LONG-Schwäche womöglich Artefakt des kaputten ML).
      → Code-Änderung + Bot-Neustart nötig.

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

## 11. ATB1 — Trendlinien-Break/Bounce 🔨 NEUAUFBAU eingeplant (2026-07-06)

**Soll (von Michi definiert, 2026-07-06):** „Die" Trendlinie = **Linie durch
bestätigte Swing-Pivots mit ≥3 Berührungen** (1h/4h, objektiv reproduzierbar —
analog find_pivot_levels). Events: Break und Bounce dieser Linien, ML scored.

**Plan Neuaufbau (eigener Task, nach der aktuellen Retrain-Queue):** Detektor auf
die Pivot-Definition bauen (Event-Typ als Feature!), Walkforward-Adapter, Labels =
gepostete Geometrie via Replay, dann Retrain nach Standard-Gerüst. Der alte Trainer
(Close-Regressionsgeraden) wird verworfen. Bot bleibt geparkt bis der Neuaufbau
validiert ist.

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

## 22. Regime-Detection ✅ Fix beauftragt (2026-07-06)

**Entschieden (Michi):** **TRANSITION-Restklasse aufspalten** — Mid-Vola-Band
(P40–P75) bekommt eine eigene Trend-Regel, damit TREND_UP/DOWN überhaupt vorkommen
und das 4D-Gating nicht die halbe Zeit deaktiviert ist. Eigener Task.

## 23. Bot-Regime-Analyzer / Whitelist ✅ Umbau beauftragt (2026-07-06)

**Entschieden (Michi):** Gate-Metrik von WR auf **Netto-Expectancy/Median** umstellen,
plus **Konfidenzintervall/Shrinkage + Mindest-n** (kein Default-Open, kein Flippen
auf Rauschen mehr). Eigener Task.

## 24. ROM1 / Orchestrator ✅ Rollen-Klärung (2026-07-06)

**Entschieden (Michi):** ROM1 wird als **eigenständiger Trading-Bot anerkannt**
(kein „reiner Router" mehr): eigene Trade-Historie fließt als Evidenz ins Gate,
SL-Distanzen werden gedeckelt (15 %-Cap aus dem Audit-Batch verifizieren/schärfen).
Eigener Task.

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
