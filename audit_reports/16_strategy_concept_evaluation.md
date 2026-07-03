# 16 — Strategie- & ML-Modell-Konzeptbewertung (Step 8, Zusatzanalyse)

**Stand:** 2026-07-03 · **Methode:** 5 parallele Konzept-Reviews (Classic-Strats, Pump/Dump-ML-Familie, AI-Bots SRA1/ATB1/AIM1/ABR1, SMC/Pattern-Familie + UFI1, Meta-Ebene Regime/Orchestrator/Intelligence). Jede Strategie wurde auf drei Ebenen bewertet: **Konzept** (ist die Edge-Hypothese plausibel?), **Trainings-/Implementierungsvalidität** (Belege aus Reports 01–13) und **Live-Evidenz** (realisierte Zahlen aus Report 14, dedupliziert, netto nach −0,10% Round-Trip-Fee, ohne Leverage).

**Notenskala:** A = klare, belegte Edge · B = plausible Edge mit positivem Live-Beleg · C = tragfähiges Konzept, Beleg fehlt/dünn · D = Konzept oder Umsetzung strukturell fragwürdig · F = konzeptionell tot bzw. verlässlich schädlich.

---

## 1. Gesamtranking (alle Strategien/Modelle)

| # | Strategie / Modell | Quelle | n (live) | WR | Σ netto | Note | Verdikt |
|---|---|---|---|---|---|---|---|
| 1 | **MIS1-72H** | 11 | 11.822 | 63,9% | **+15.868** | **B−** | Arbeitspferd; Retrain-Priorität #1 (keine Provenienz!) |
| 2 | **Three-Drive TD_1H/4H** | 25 | 2.794 | 57,3% | +2.387 | **B−** | einzige gut kalibrierte ML-Familie; behalten + sauber neutrainieren |
| 3 | **SRA1** | 9 | 396 | 69,9% | +134 | **B−** | konzeptionell sauberstes ML-Setup (Meta-Labeling); klein aber gesund |
| 4 | **Support Resistance** | strategies/ | 1.917 | 63,5% | +596 | **B−** | einzige netto-positive Classic; SHORT trägt alles |
| 5 | **ROM1 / Orchestrator** | 28 | 2.677 | 69,2% | +2.184 | **C+*** | +8pp WR-Mehrwert trotz degradiertem Gate; Architektur trägt |
| 6 | **EPD1** | 10 | 4.392 | 72,8% | +14.222 | **C+** | bestes Edge-Narrativ, aber OOD-Serving + regimeabhängig (Juli negativ) |
| 7 | **ATS1** | 12 | 1.768 | 65,8% | +1.622 | **C+** | architektonische Blaupause (Event-Gate); OBV-Skew invertiert Confidence |
| 8 | **ABR1** | 18 | 110 | 63,6% | +335 | **C−** | solides Konzept, aber real nur 7/18 Features; n zu klein |
| 9 | **MIS1-168H** | 11 | 7.167 | 58,5% | +6.928 | **C−** | driftet seit Mai (WR 48/49/35); nur mit Retrain + Monitoring |
| 10 | **Breaker Block BB_4H** | 25 | 2.162 | 61,2% | +565 | **C−** | Konzept am besten abgesichert (S/R-Flip), Feature-Skew fixen |
| 11 | **Main Channel** | strategies/ | 202 | 67,3% | −77 | **C−** | Duplikat von Support Resistance; mergen statt Doppelbetrieb |
| 12 | **RUB1** | 13 | 2.496 | 57,6% | +3.675 | **D+** | ML-Layer ist Rauschen (MACD-Bruch); Gewinn = Vorfilter + SHORT-Tails |
| 13 | **QM_1H** | 24 | 3.139 | 67,5% | −139 | **D+** | 67% WR und trotzdem ≈0 — Exit-Geometrie gibt alles zurück |
| 14 | **Volume Indicator** | strategies/ | 51.440 | 64,1% | −705 | **D+** | kleiner echter Kern, degenerierte HVN-Mechanik, Fee-Generator |
| 15 | **MIS1-8H/24H** | 11 | 1.003 | ~52% | +1.261 | **D** | Horizont/Feature-Mismatch; im Retrain eher streichen |
| 16 | **ATB1** | 14 | 306 | 65,7% | −172 | **D** | Modell sah nie das Event, das es scored; Neuaufbau oder parken |
| 17 | **BR-Familie (BR1H/2H/4H)** | 7 | 11.756 | 58–60% | **−4.106** | **D** | Break&Retest ohne ML-Gate; BR1H-SHORT sofort zu |
| 18 | **BB_1H** | 25 | 3.909 | 55,7% | −1.089 | **D** | 1h-Edge überlebt Fees+Rauschen nicht; parken bis Retrain |
| 19 | **5 Percent** | strategies/ | 19.385 | 71,1% | −5.766 | **D** | Schein-Konfluenz (26 redundante Filter), später Einstieg |
| 20 | **Mayank** | 17 | untracked | ? | ? | **D** | unmessbar; „FVG fully closed" als Entry konzeptionell wackelig |
| 21 | **BTC SMC 100x** | 21 | untracked | ? | ? | **D (F as-is)** | bestes SMC-Setup-Design, aber 100x-Design = Liquidations-Generator |
| 22 | **SMC Forex/Metals** | 16 | untracked | ? | ? | **D−** | unvalidiert + Repaint + kein Tracking; Abschalt-Kandidat |
| 23 | **QM_4H** | 24 | 556 | 54,9% | −277 | **F** | stoppen |
| 24 | **Fast In And Out** | strategies/ | 111.387 | 60,6% | **−25.843** | **F** | keine Edge-Hypothese; größter Verlustbringer der Flotte |
| 25 | **AIM1** | 15 | 3.047 | 50,8% | **−3.399** | **F** | verlässlich invertiert (conf>0,9 → 9,3% WR); sofort pausieren |
| 26 | **UFI1** | 29 | 35 | 25,7% | −280 | **F** | Backtest-Claim durch Look-ahead entwertet; konzeptionell tot |

\* ROM1-Note ist Meta-Ebene gesamt: Konzept B / Implementierung D+ / Live-Wirkung positiv — Details Abschnitt 7.

---

## 2. Querschnittsbefunde (gelten über fast alle Strategien)

1. **Win-Rate ist als KPI wertlos — und das System optimiert genau darauf.** Alle Classic-Strats liegen >60% WR und verlieren in Summe −13.360; 5 Percent hat 71% WR bei −5.766, QM_1H 67,5% bei −139. „Win" = TP1-Touch, danach gibt Trailing/SL alles zurück, Fees fressen den Rest. Die Summen der Gewinner entstehen in den Tails (p95), nicht in der WR. Konsequenz: Dashboard-Hauptmetrik und Whitelist-Gate müssen auf **Netto-Expectancy/Median** umgestellt werden (siehe Report 14 D.7 und Abschnitt 7).

2. **Kein einziges ML-Modell hat aktuell belegbaren ML-Skill.** Jede Familie bricht mindestens einmal den Vertrag zwischen Trainingslabel und gehandelter Order-Geometrie (X-R1 aus Report 13: „backtest the detector, trade something else"): idealisierte Fills (Pivot-Close, Limit am Level, fixe 1%/2%-Brackets) im Training vs. CMP-Entry + `calculate_smart_targets`-Geometrie live. Die positiven Live-Summen entstehen plausibel aus **Regel-Vorfiltern + S/R-basierter TP/SL-Konstruktion + günstigem Marktregime** — nicht aus den Modellen. Einzige Teilausnahme: TD_1H ist empirisch gut kalibriert (Step 2), d.h. dort trägt das Feature-Signal trotz Label-Bias.

3. **Die S/R-basierte Trade-Konstruktion ist der heimliche Star.** Die vier Strategien mit strukturbasierten Targets (Support Resistance, SRA1, ROM1, MIS1-Familie via `calculate_smart_targets`) sind die relativen Gewinner; ROM1 hat als fast einziges Modell einen positiven Median (+1,00%). Das legt nahe: Ein erheblicher Teil des Flotten-Alphas steckt in der Zonen-/Target-Logik, nicht in der Signalauswahl.

4. **Richtungs-Asymmetrien sind der billigste ungehobene Hebel.** EPD1 SHORT 76,5% vs LONG 50,2% WR; RUB1 SHORT 63,9% vs LONG 48,7%; BR1H LONG 65,5% vs SHORT 49,5%; Support Resistance: SHORT trägt den gesamten Gewinn. Per-Modell-Direction-Gates sind sofort umsetzbar und brauchen kein Retrain.

5. **Leverage vs. SL wird nirgends abgeglichen (R4).** Zwei Bots sind mathematisch nicht existenzfähig: BTC SMC (100x, Liquidation ~−0,9% vor jedem SL) und UFI1 (20x, Liquidation ~+5% bei ~34% SL-Distanz); ROM1-SLs erreichen p90=17,9% Distanz bei 20x. Ein zentrales `cap_leverage_to_sl()` schließt die Klasse.

6. **Drei Bots (16, 17, 21) sind komplett unvermessen** — sie schreiben kein `ai_signals`, tauchen in keiner Performance-Statistik auf und haben nie einen validen Backtest gesehen. Was unmessbar ist, hat in einer Bot-Flotte keinen Ertragsanspruch: instrumentieren oder abschalten.

7. **Der Intelligence-Layer ist ein Anzeige-Layer.** Whale- und Funding-Daten — genau die Datenklassen, die ein Regime-Gate veredeln könnten — werden von keiner einzigen Entscheidungslogik konsumiert (und der Whale-Logger ist seit 18.04. tot). Die einzige maschinelle Feedback-Schleife läuft über Preis/ATR und die eigene Trade-Historie.

---

## 3. Classic-Strategien (strategies/, Runner `3_detectors.py`)

Alle 5 teilen systemische Defekte: Bewertung der **noch laufenden Kerze** (R1), praktisch toter globaler Win-Cooldown (drosselt nach Gewinnen, nie nach Verlusten), und die Empty-Zone-Target-Interpolation, die LONG-TPs unter Entry erzeugen kann (P0.7).

### Support Resistance — **B−** (Σ +596, einzige netto-positive Classic)
**Konzept:** Wiederholter Test eines S/R-Levels + RSI-Divergenz zwischen erstem und aktuellem Hit + OBV-Bestätigung → Umkehr-Einstieg, Targets aus echten Struktur-Zonen. Theoretisch plausibel und die einzige Classic-Idee mit echter Selektionslogik — die niedrige Signalfrequenz (1.917 vs. 111k bei FIFO) zeigt, dass die Filter tatsächlich filtern.
**Schwächen:** OBV-Baustein statistisch nahezu wirkungslos (dekorativ); kein Regime-Bewusstsein; fixer 2,5%-SL ignoriert Coin-Volatilität; SHORT-Seite (+0,66% ø) trägt den gesamten Gewinn.
**Verdikt:** Beste Rettungskandidatin. Target-Interpolation fixen, Closed-Candle, ATR-SL von Main Channel übernehmen, OBV ersetzen/streichen, Direction-Gate erwägen.

### Main Channel — **C−** (Σ −77, n=202)
Logisch **identisch mit Support Resistance** (gleiche Hit-/Divergenz-/OBV-Logik), nur auf 38-Coin-Whitelist und mit ATR- statt Fix-SL. Ein Event erzeugt zwei nahezu identische Hebel-Signale in zwei Cornix-Channels — verdeckte Doppel-Exposure statt Diversifikation. **Verdikt:** in Support Resistance mergen (ATR-SL als Verbesserung mitnehmen), nicht separat betreiben.

### Volume Indicator — **D+** (Σ −705 netto bei brutto +4.439 — Fees fressen alles)
**Konzept:** Preis an 90d-High-Volume-Node + 3σ-Volumen-Spike in den letzten 5 Tagen bestimmt die Richtung. Volume-Profile-Level sind legitim, aber: ein bis zu **5 Tage alter** Spike als Richtungssignal für einen 30m-Entry ist hoffnungslos veraltet, und ohne Cooldown feuert ein historisches Event tagelang alle 30 min neu (51.440 Trades — Signalinflation per Konstruktion).
**Implementierung degeneriert:** HVN-Erkennung summiert Volumen pro exaktem Float-Close → das Gate misst Tick-Size statt Volumenstruktur; Spike-Logik nimmt den *ältesten* Spike, Index-0-Spike ist immer „Sell".
**Verdikt:** Dass trotzdem brutto ein Plus bleibt, deutet auf einen echten kleinen Kern (Volumenzonen wirken). Rettbar nur mit Umbau: gebinnte HVNs, Frische-Anforderung, Per-Coin-Cooldown, Struktur-Targets.

### 5 Percent — **D** (Σ −5.766 bei 71,1% WR — Paradebeispiel Win≠Profit)
**Konzept:** ~26 AND-Bedingungen (RSI-Band, TSI, komplettes EMA/WMA/KAMA-Alignment, MACD, Donchian/Boll-Mid). Die Konfluenz ist Schein: fast alle Bedingungen sind Glättungen desselben Close-Preises und kollabieren auf einen Filter „etablierter, steiler Trend" → systematisch **später** Einstieg in ausgereizte Bewegungen. Fixe %-Targets, kein Zeit-Exit, kein Regime-Bewusstsein. Dazu SHORT-Headroom-No-op (P1.14) und EMA-Typo (P2.43).
**Verdikt:** Mit Fixes evtl. Break-even (LONG-Seite 76% WR bei n=1.087 prüfenswert), aber ohne Redesign von Entry-Timing und Exits keine positive Erwartung begründbar.

### Fast In And Out — **F** (Σ −25.843 — größter Verlustbringer der gesamten Flotte)
**Konzept:** Drei Bedingungen (RSI_9 55–75, EMA9>EMA21, 5% Luft) auf 30m, ein TP bei +1,25%. Das ist keine Edge-Hypothese, sondern die Definition von „gerade steigt es" — trifft in jedem Aufwärtsdrift auf hunderte Coins zu (111.387 Trades). Payoff strukturell negativ: Median +1,25% (Scalps „funktionieren" mechanisch), aber die seltenen Verlierer sind riesig — die abs>50%-Ausreißer der Classic-Familie konzentrieren sich hier. Lehrbuch „Pennies vor der Dampfwalze".
**Verdikt:** Nicht rettbar — es gibt keine Selektion, die man durch Bugfixes freilegen könnte. Abschalten.

---

## 4. Pump/Dump-ML-Familie (MIS1, ATS1, RUB1, EPD1)

**Familienbefund:** Keines der vier Modelle hat belegbaren ML-Skill; die positiven Summen kommen aus Regel-Gates + S/R-TP/SL + Marktregime. Jede Familie bricht X-R1 (Label ≠ gehandelte Geometrie) und liefert unkalibrierte „Confidence"-Werte.

### MIS1 (8 Modelle: {8h,24h,72h,168h}×{pump,dump}) — Familie **C+**, getragen von 72H
**Konzept:** Batterie binärer XGBoost-Klassifikatoren, stündlich pro Coin: Wahrscheinlichkeit eines Pumps/Dumps im jeweiligen Horizont, aus 67 1h-Indikator-Features. Bester Score gewinnt (Cross-Horizon-Argmax), TP/SL aus `calculate_smart_targets`. Die Horizont-Differenzierung ist der interessanteste Teil — 1h-Features haben ihren Sweet Spot empirisch genau bei 72h.
**Konzeptmängel:** Label-Definition **unbekannt** (kein Trainer existiert — null Provenienz, null Reproduzierbarkeit, einzige Familie ohne Trainer auf der Maschine); Argmax vergleicht rohe Wahrscheinlichkeiten verschieden kalibrierter Modelle (P2.33); globales Modell über 538 Coins mit bewiesenem **Ticker-/Preisklassen-Leakage** (`pct_distance`-Unfall-Features in Coin-Preisskala, beim 168h_pump sogar Top-Feature mit 10,4% Importance — das Modell hat teilweise „welcher Coin ist das" gelernt). Dazu P1.17: Prediction auf der laufenden Kerze mit ~1/6-Partialvolumen.
- **MIS1-72H: B−** — +15.868 netto, in jedem Monat positiv, stärkstes Arbeitspferd der Flotte. Abzug: nicht reproduzierbares Black-Box-Artefakt; niemand kann sagen, warum es funktioniert, und niemand könnte es neu bauen. **Dringendster Retrain-Kandidat** (versionierter Trainer, First-Touch-Label der echten Geometrie, Leakage-Features raus).
- **MIS1-168H: C−** — +6.928 kumuliert, aber seit Mai WR 48/49/35%: der Horizont ist zu lang für stationäre 1h-Features, das Modell driftet mit dem Regime. Nur mit Retrain + Drift-Monitoring behalten.
- **MIS1-8H/24H: D** — ~52% WR, Median negativ, rein tail-getrieben. Kurzer Horizont + langsame Features = konzeptionell dünnste Kombination. Im Retrain-Programm eher streichen.

### ATS1 (TSI-Sniper) — **C+** (Σ +1.622)
**Konzept:** Event-getriebener Filter — nur bei TSI-Fast-Crossover auf der letzten *geschlossenen* Kerze wird das Richtungsmodell befragt. Architektonisch das sauberste Design der Familie: das Regel-Gate garantiert, dass live nur die trainierte Event-Population gescored wird; einziger Bot der Familie mit korrekter Kerzen-Disziplin.
**Mängel:** OBV-Train/Serve-Skew (Training kumuliert ~300 Tage, live 500-Kerzen-Fenster mit anderer Normalisierung) → erklärt die gemessene Kalibrierungs-**Inversion** (Bucket 0,6–0,7 → 71% WR, 0,8–0,9 → 57%); Trainingslabel (2,5%/1,5%-Bracket) ≠ Live-Geometrie; Trainingsdaten 6,5 Monate stale.
**Verdikt:** Günstig rettbar: skalenfreie OBV-Features + Retrain auf echter Geometrie; Sofortmaßnahme quasi kostenlos (Operating Point auf den empirisch besten 0,6–0,7-Bucket legen). Konzeptionell die Blaupause für alle anderen Familien.

### RUB1 (Rubberband Mean Reversion) — **D+** (Σ +3.675, aber tail-/SHORT-getrieben)
**Konzept:** 4-fach-Extrem-Vorfilter (≥8% unter/über 90d-Regression + RSI-Extrem + TSI-Extrem + Donchian-Touch), dann 9-Feature-ML als Snap-Back-Filter. Grundmuster richtig gedacht, aber: das Label ignoriert den **SL-Pfad** — beim Messer-Fangen ist der Drawdown-Pfad die eigentliche Risikogröße, das Modell kann per Konstruktion nicht lernen, wofür es eingesetzt wird.
**Schwerste Findings der Familie:** MACD-Semantikbruch (trainiert auf 9/21, live mit 12/26-Spalten unter demselben Namen gefüttert — für die Validierung unsichtbar); Random-Split über Persistenz-Episoden → Test-AUC ist Memorization; Vorfilter feuert live in anderer Population (DB-RSI ≠ Wilder, Δ≈4,8).
**Verdikt:** Der Live-Gewinn **kann nicht vom ML stammen** (falscher MACD, falscher RSI) — er kommt aus Vorfilter + S/R-Konstruktion, Median −0,06, p95 +33%, SHORT 63,9% vs LONG 48,7% WR. Sofort: LONG-Gate zu. Rettung nur per Komplett-Retrain mit gemeinsamem Feature-Builder.

### EPD1 (Echtzeit-Pump/Dump-Detektor) — **C+** (Σ +14.222, bester ø der Flotte — aber mit Sternchen)
**Konzept:** 10s-Tick-Detektor: Volumen-Anomalie + Mikro-Momentum aus dem 24h-Ticker, 3-Klassen-Modell, 15-min-Cooldown. Das Edge-Narrativ ist das gesundeste der Familie — Volumen-Ignition ist eine der wenigen echten Kurzfrist-Edges in Alt-Perps, und die SHORT-Asymmetrie (76,5% vs 50,2% WR) bestätigt das „Pump-Faden"-Muster.
**Kernproblem (P0-Klasse):** Der Trainer sampelte nur `volume_ratio ≥ 5`-Events, der Live-Bot scored **jeden Tick ohne Gate** → fast alle Live-Queries sind out-of-distribution, Kalibrierung flach (corr≈0). Die 72,8% WR stammen plausibel aus der S/R-Konstruktion. Dazu: Trainingscode auskommentiert (stales Artefakt), Timestamp-Fix unvollständig, Shadow-Flut in `ml_predictions_master`.
**Verdikt:** **Billigster Fix der ganzen Flotte** — das `vol_ratio ≥ 5`-Gate vor `predict` ist eine Zeile und bringt das Modell erstmals in seine Trainingsverteilung. Vorsicht: fast der gesamte Gewinn stammt aus Mai/Juni (Alt-Pump-Phase), Juli negativ (−345) → Regime-Abhängigkeit, Drift-Watch Pflicht. Nach Gate-Fix + Retrain Potenzial Richtung B.

---

## 5. AI-Bots SRA1 / ABR1 / ATB1 / AIM1

### SRA1 — **B−** (Σ +134, n=396, einziger der vier mit positivem Median)
**Konzept:** Kein Signalgeber, sondern ML-Qualitätsfilter über der klassischen Support-Resistance-Strategie — de facto **Meta-Labeling nach Lopez de Prado**: wohldefinierte Event-Population, Features zum Event-Zeitpunkt, Label = echtes Trade-Ergebnis derselben Strategie. Dadurch strukturell kleinste Train/Live-Lücke der Flotte. Trainerlage die gesündeste (chronologischer Split, Provenienz bewiesen).
**Offene Punkte:** Label-Semantik `SL1/SL2/SL3/4` unverifiziert (falls `SL1` „SL vor TP1" heißt, wäre das Label teilinvertiert — klären!); rohe Preisspalten als Features (Skalen-Leakage-Geruch); Crash-Loop bei fehlenden ATR-Features (35 statt 38 Spalten → predict wirft, Batch-Rollback).
**Verdikt:** Behalten. Crash-Fix sofort (ohne Retrain möglich), Label verifizieren, beim Retrain Preisfeatures raus + Kalibrierung. Bester Retrain-Kandidat der vier, weil das Fundament stimmt.

### ABR1 (Break & Retest) — **C−** (Σ +335, n=110)
**Konzept:** Continuation vs. Failed-Breakout nach Level-Retest klassifizieren — die richtige ML-Formulierung für ein solides Handelskonzept; konzeptionell zweitbester Ansatz nach SRA1.
**Aber:** Bewiesen fährt das Modell real nur auf **7 von 18 Features** (P0.12, pandas_ta-Namens-Mismatch → 11 Features konstant 0, im Booster-Dump verifiziert); Threshold + Win-Rate vollständig in-sample; ehrlichste Zahl CV-F1(success) = **0,134 ≈ Rauschen**. Der kleine Live-Gewinn stammt plausibel aus dem Setup + S/R-Konstruktion, nicht aus Modell-Skill. Positiv: Verkabelung sauber (Klassenindex 3-fach verifiziert, Closed-Candle korrekt).
**Verdikt:** Gut rettbar mit klarem Pfad: pta-Prefix-Match-Fix (Vorlage in `14:197-211`), Retrain mit allen 18 Features (zeitlicher Split, First-Touch-Label ab Retest-Close, Threshold auf Validation), Startup-Assertion „kein Feature konstant".

### ATB1 (Trendline Break/Bounce) — **D** (Σ −172, n=306)
**Konzept:** Trendlinien-Events (Break/Bounce up/down) ML-gescored. Trendline-Trading ist als diskretionäres Konzept legitim, aber schlecht formalisierbar („die" Trendlinie existiert nicht; R≥0,2 ist extrem lax). Vier semantisch verschiedene Events werden von zwei Modellen gescored, die das Event **nicht als Feature kennen** — Break und Bounce sind für das Modell ununterscheidbar. Die bewusst reaktivierte „unknown"-State-Logik (Kommentar im Code: „HIER IST DER BUG AUS DEINEM ALTEN BOT WIEDER AKTIV!") macht die Event-Definition vollends beliebig.
**Killer:** Der Trainer labelt ein **anderes mathematisches Objekt** (Kreuzungen der Close-Regressionsgeraden statt Pivot-Trendlinien; Bounces haben gar kein Trainings-Gegenstück); `vol_ratio` live ~1/19 der Trainingsskala; Label +10%/72h ohne SL vs. Live-SL bis −8,8%.
**Verdikt:** Das ML-Gate ist faktisch ein Zufallsfilter. Parken; Rettung wäre ein Neuaufbau von null (Event-Definition fixieren, auf Live-Events labeln), kein Fix.

### AIM1 (Meta-Modell) — **F** (Σ −3.399, größter AI-Verlustbringer; conf>0,9 → 9,3% WR)
**Konzept:** Der ambitionierteste Ansatz — Stacking über alle Bot-Signale: Marktkontext × Signal-Schwarm-Verhalten × Quell-Identität → Erfolgswahrscheinlichkeit je Kandidat. Meta-Learning über Basis-Signale ist grundsätzlich eine gute Idee, aber die Architektur verletzt alle Voraussetzungen: Quell-Identität als One-Hot über frei benannte, sich ändernde Bot-Namen (fragilste denkbare Kodierung); „Confidence" der Classic-Bots ist ein hartkodiertes Fantasie-Mapping (Konstante pro Quelle); Selbst-Feedback-Schleife (eigene Shadow-Ausgaben zählen als Input); ein Label über heterogene Trade-Geometrien.
**Bewiesen (Report 13):** Identitäts-Vokabular tot (nur historische Namen im pkl → alle Identity-Dummies live 0), Join-Lookahead (`round` statt `floor` → Feature-Kerze bis 90 min in der Zukunft), Volatilitäts-Label (+10%/72h vor −7,5%-SL → Top-Features sind ATR → das Modell ist ein Volatilitäts-Detektor, und die volatilsten Kandidaten reißen live zuerst den SL) → **echte, ehrlich gelernte Inversion**.
**Verdikt:** Das Modell ist nicht nutzlos, sondern **verlässlich falsch**. Sofort pausieren. Rettung = Neuprojekt (First-Touch-Label, floor-Join in Trainer und Serving identisch, versioniertes Vokabular, Selbstausschluss, Out-of-Time-Kalibrierung) — Neutraining nur aufs Vokabular würde wieder ein überkonfidentes Volatilitätsmodell erzeugen.

---

## 6. SMC-/Pattern-Familie + UFI1

**Tag-Klärung (im Code verifiziert):** `QM_*` = Quasimodo (24) · `TD_*` = **Three-Drive**-Divergenz (25) · `BB_*` = **Breaker Block** = Break-and-Retest mit ML (25, *nicht* Bollinger) · `BR1H/2H/4H` = Break-and-Retest **ohne** ML aus dem Pattern-Detector (7, nicht aus 25!) · Bots 16/17/21 schreiben kein `ai_signals` → **komplett unvermessen**.

### Three-Drive TD_1H/4H — **B−** (Σ +2.387; TD_1H laut Step 2 das am besten kalibrierte Modell der Flotte)
**Konzept:** Drei höhere Hochs / tiefere Tiefs bei gegenläufigem RSI an den Pivots → Momentum-Erschöpfung → Reversal. Faktisch eine klassische **RSI-Divergenz-Strategie** in Pattern-Verkleidung — und damit die konzeptionell solideste der Familie (Divergenz an Mehrfach-Extrema hat empirische Tradition).
**Trotz formal wertlosem Training** (Trainer-Entry = Pivot-Close mit 10-Kerzen-Hindsight, fixe 2R-Geometrie, Random-Split — P0.10/P1.25/P1.29) ist TD netto klar positiv und kalibriert: die Features tragen offenbar echtes Signal, das den Label-Bias überlebt.
**Verdikt:** Klarer Behalten-Kandidat. Größter Hebel: korrektes Neutraining (Entry bei `p3+PIVOT_WINDOW`, Live-Geometrie, chronologischer Split) — plausibel, dass Kalibrierung und Selektionsschärfe dann weiter steigen. Kein A, weil ø-Edge klein und tail-getrieben.

### Breaker Block BB_4H — **C−** (+565) / BB_1H — **D** (−1.089)
**Konzept:** Break-and-Retest („Support wird Resistance") — die am besten abgesicherte Idee der SMC-Familie. Das Live-Muster passt exakt zur Theorie: auf 4h ist die Basis-Edge groß genug, um ML-Rauschen und Fees zu überleben, auf 1h nicht.
**Kritischer Skew:** Trainer extrahiert Features an der *Breakout*-Kerze (RSI ~65–75), der Bot an der *Retest*-Kerze (RSI ~45–55) → die Wahrscheinlichkeiten sind Rauschen. Dazu `peak_idx[-2]`-Level-Bug (P2.39) und Fees von 8–15% eines R bei 1%-SL-Geometrie.
**Verdikt:** BB_1H parken; BB_4H ist der Grund, die Pipeline zu reparieren (Features an der Retest-Kerze, Level-Logik, Fees ins Labeling) statt zu löschen.

### BR-Familie (Pattern-Detector 7) — **D** (Σ −4.106)
Dieselbe Break-and-Retest-Idee wie BB, aber **ohne ML-Gate** und mit 4-fachem Signalvolumen. Der Vergleich BB_4H (+ML, +565) vs. BR-Familie (ohne ML, −4.106) ist das beste In-vivo-Argument im Repo, dass ein ML-Gate über Break-and-Retest-Rohsignalen Wert stiftet. **Sofort:** BR1H-SHORT-Seite zu (LONG 65,5% vs SHORT 49,5% WR).

### Quasimodo QM_1H — **D+** (−139 bei 67,5% WR) / QM_4H — **F** (−277)
**Konzept:** Liquidity-Sweep + Struktur-Bruch, Retest der Sweep-Zone als Reversal — unter den Pattern-Ideen eine der plausibleren und objektiver definierbar als FVGs. Der ML-Filter als „nimm die besten X%" ist der richtige Ansatz.
**Aber:** Live-Pivots auf der Forming Candle vs. korrekt gegateter Trainer (Training-Serving-Skew); Trainer simuliert Limit-Order am QML, Bot handelt CMP; Fill-Logik löscht garantierte Verlierer + vergibt Same-Candle-TP-Wins (P1.30) → Labels systematisch geschönt; Bot ignoriert den gespeicherten `optimal_threshold`.
**Verdikt:** 67% WR bei ±0 heißt: die Geometrie (TP1 = halbe Strecke, SL jenseits des Extrems) gibt strukturell alles zurück. QM_4H stoppen; QM_1H nur mit Neutraining + Exit-Redesign, sonst parken.

### SMC Forex/Metals (16) — **D−** · Mayank (17) — **D** · BTC SMC 100x (21) — **D (F as-is)**
Alle drei unvermessen (kein Tracking, kein valider Backtest). 16: Retail-SMC-Folklore + Repaint-Entries + SL ohne Sanity-Check — Abschalt-Kandidat. 17: konsistenter implementiert (Closed-Candle), aber „FVG fully closed" als Entry ist in der SMC-Lehre selbst ein *entwertetes* Level — Knife-Catch an altem Gap-Boden; als Info-Kanal harmlos, als Strategie unbewertbar. 21: handwerklich das **beste** Setup-Design der SMC-Familie (Age-Caps, Trendfilter, R:R-Check) — aber das 100x-Design ist mathematisch defekt (Liquidation ~−0,9% vor jedem SL, P0.5) und die Parameter stammen aus In-Sample-Grid-Search. Mit Hebel-Fix + ehrlichem Walk-Forward der prüfenswerteste regelbasierte SMC-Bot.

### UFI1 (29) — **F** (25,7% WR, −7,90% ø/Trade — schlechtestes Modell der Flotte pro Trade)
**Konzept:** Dead-Cat-Bounce gedumpter Coins shorten (Retracement-Rejection auf Daily). Die Idee ist nicht absurd, aber das Risiko-Design widerspricht ihr fundamental: SL 25–40% über Entry bei genau den Assets mit 50%-Squeeze-Kerzen, und mit 20x ist die Strategie mathematisch nicht existenzfähig (Liquidation ~+5%, der SL wird nie erreicht — P0.6).
**Der „+278R"-Backtest-Claim zerfällt dreifach** (P0.11): Entry-Wahl mit dem zukünftigen Fenster-Tief (Look-ahead), 5-Target-Trailing-Ladder im Backtest vs. Single-TP1 live, wochenalte Confirmation-Kerzen mit CMP-Entry.
**Verdikt:** Hier ist nicht nur die Implementierung, sondern die validierende Evidenz selbst durch Look-ahead entwertet — kein Grund zu glauben, dass eine korrekte Version positiv wäre. Sofort stoppen (deckt sich mit Report 14). Dass UFI1 exakt so implodiert ist, wie die Look-ahead-Analyse vorhersagt, validiert umgekehrt die Audit-Methodik.

---

## 7. Meta-Ebene: Regime-Detection, Whitelist, Orchestrator/ROM1 — **C+** (Konzept B / Implementierung D+ / Live-Wirkung positiv)

### Regime-Detection (26 + core/regime_logic.py)
Zweiachsig (BTC-Regime aus 15m-Returns/ATR mit adaptiven P75/P40-Perzentil-Schwellen; Alt-Context aus BTCDOM), sauberes 2-Check-Debouncing. Konzeptionell solide Grundidee mit einem **strukturellen Definitionsfehler**: Trend erfordert *niedrige* Vola (ATR<P40) — das Mid-Vola-Band (P40–P75, ~35% der Zeit) hat keine Klassifikationsregel und fällt immer in die Restklasse TRANSITION. Live-Beleg (Step 2): TRANSITION 44,5%, HIGH_VOLA 29,7%, CHOP 25,8% — **TREND_UP/DOWN kommen faktisch nicht vor**. Da TRANSITION gleichzeitig der „Detektor unzuverlässig"-Trigger ist, ist das 4D-Gating fast die Hälfte der Zeit deaktiviert. Im Ist-Zustand ist die Taxonomie ein Vola-Klassifikator mit Trend-Etikett.

### Bot-Regime-Analyzer (27)
Die Idee (Bot × Regime × Alt-Context × Direction → Whitelist) ist State-of-the-Practice; die PnL-basierte Outcome-Klassifikation (Delisting/Cleanup/Ausreißer als „neutral") und die asymmetrisch strenge Counter-Trend-Regel sind durchdacht. **Statistisch aber zu naiv:** 30 Zellen pro Bot bei 30-Tage-Fenster → meist n<30 → Default-Open; und selbst bei n=30 ist `wr_bot ≥ wr_overall` als Punktschätzer-Vergleich bedeutungslos (95%-KI ±17pp) — kein Signifikanztest, kein Shrinkage. Das Gate flippt auf Rauschen. Gravierender: **Es optimiert die falsche Metrik** — WR (TP1-Touch), von der Report 14 beweist, dass sie irreführend ist; avg_pnl/median/sharpe_like stehen bereits in der Tabelle und werden ignoriert. Dazu drei gleichgerichtete Aufwärts-Biases durch Zensur (P1.9-Fremd-Close, Open-Trade-Zensur, Delisting-Neutralisierung) auf genau der Zahl, an der das Geld-Gate hängt.

### Orchestrator/ROM1 (28)
Die Doku sagt „reiner Signal-Router", der Code ist ein **26. Trading-Bot**, der die anderen 25 als Screening-Schicht benutzt: ROM1 verwirft Original-Entry/SL/Targets und baut eigene (CMP-Entry, S/R-SL ohne Distanz-Cap — p90 17,9% bei 20x!, bis 20 Targets). Das ist konzeptionell sogar vertretbar (normiert heterogene Risikoprofile), **zerreißt aber die statistische Kette**: Die Whitelist wird auf Trades mit Original-Parametern erhoben, ROM1 exekutiert etwas anderes. Dazu Self-Echo (P0.3, 109 Fälle), nicht-atomare Pipeline, `sync_closed_trades` schreibt fremde Outcomes auf ROM1.

### Der zentrale Befund — ROM1 +8pp WR trotz degradiertem Gate
ROM1: n=2.677, WR 69,2% (Fleet 61,1%), +2.184 netto, als fast einziges Modell **positiver Median** (+1,00%). Drei Lesarten:
1. **Das Gate war nie völlig inert, nur degradiert** (3.043 echte Suppressions, aber auf teils 2,5 Monate alten Stats + Overall-Fallback im dominanten TRANSITION-Modus). Bei einer Flotte mit dieser Qualitätsspreizung ist schon **primitive Negativselektion werthaltig** — die schlimmsten Quellen (AIM1, UFI1, negative SHORT-Seiten) auszusortieren erzeugt +8pp fast zwangsläufig.
2. **Ein erheblicher Teil des Mehrwerts stammt vermutlich nicht vom Regime-Konzept**, sondern von den Nebenwirkungen: 4h-Cooldown (Anti-Overtrading), Opposite-Block, und v.a. der eigenen Trade-Konstruktion (der positive Median deutet stark darauf). Die spezifische 4D-Hypothese („Bot X funktioniert in Regime Y") ist durch die +8pp **nicht validiert — sie ist ungetestet**.
3. **Die +8pp sind optimistisch verzerrt** (fremde Outcomes, P1.9-Zensur, WR-Metrik) — echter Mehrwert wahrscheinlich positiv (Netto-PnL und Median stützen das unabhängig), aber kleiner als die Schlagzeile.

### Intelligence-Layer (19/20/23/7/22)
Whale-Logger (seit 18.04. tot, vorher 49/529 Symbole) und Funding-Logger werden **von keiner Entscheidungslogik konsumiert** — totes Datensammeln bzw. Human-Info. Market-Tracker ist sinnvolles Reporting mit eigenen Bugs. Pattern-Detector 7 ist kein Intelligence-, sondern ein (netto negativer) Signal-Layer.

### Erwartung nach den Fixes
- P0.4-Fix (pretty_name + Staleness-Gate): moderater Zusatzgewinn; kein Sprung, weil der TRANSITION-Fallback unberührt bleibt.
- P1.9-Fix wird die gemessenen WRs **senken** — gewollt (Ehrlichkeit), vorab kommunizieren.
- **Größter Hebel sind drei Konzeptänderungen, keine Bugfixes:** (a) TRANSITION-Restklasse aufspalten (Mid-Vola-Trend als eigenes Regime), (b) Gate-Metrik von WR auf Netto-Expectancy mit Konfidenzintervall/Shrinkage, (c) ROM1 als eigenen Bot dokumentieren und seine eigene Historie als zweite Evidenzschicht ins Gate.

Mit P0/P1-Fixes plus diesen drei Änderungen ist eine B-Note realistisch; ohne TRANSITION- und Metrik-Korrektur bleibt es ein guter Repost-Filter mit Regime-Etikett.

---

## 8. Portfolio-Empfehlungen (konsolidiert)

**Sofort (kein Retrain nötig):**
1. **Stoppen:** AIM1 (verlässlich invertiert), UFI1, QM_4H, Fast In And Out. **Parken:** ATB1, BB_1H, BR-Familie prüfen.
2. **Direction-Gates:** EPD1 LONG zu, RUB1 LONG zu, BR1H SHORT zu.
3. **EPD1-Gate-Fix** (`vol_ratio ≥ 5` vor predict — eine Zeile) und **ATS1-Operating-Point** auf den 0,6–0,7-Bucket.
4. **Leverage-Cap zentral** (`cap_leverage_to_sl()`): schließt P0.5 (BTC SMC 100x), P0.6 (UFI1) und ROM1-SL-Distanzen (P2.27).
5. Bots 16/17/21: instrumentieren (`ai_signals`) oder abschalten — unvermessene Strategien haben keinen Ertragsanspruch.

**Retrain-Programm (Priorität nach erwartetem Wert):**
1. **MIS1-72H** — größter Ertragsträger ohne jede Provenienz; versionierter Trainer, First-Touch-Label der echten Geometrie, Leakage-Features raus.
2. **TD** — beste Kalibrierung + positiver Ertrag; korrektes Labeling dürfte die Selektionsschärfe weiter heben.
3. **SRA1** — gesündestes Fundament; Label-Semantik verifizieren, dann Retrain.
4. **ABR1** — pta-Fix + Retrain mit allen 18 Features.
5. **EPD1 / ATS1 / RUB1** — gemeinsamer Feature-Builder Bot↔Trainer (X-R2), Episoden-Dedup, Label mit SL-Pfad.
Voraussetzung für alle: **R1 (Forming-Candle) zuerst fixen** — sonst trainiert man erneut auf Daten, die es live nicht gibt; und den gemeinsamen Walk-Forward-Simulator aus P0.10 bauen, der die bot-eigenen Setup-Funktionen bar-für-bar abspielt.

**Strukturell:**
- KPI-Umstellung von WR auf Netto-Expectancy/Median überall (Dashboard, Whitelist-Gate, Reports) — die aktuelle WR-Anzeige belohnt exakt das falsche Verhalten.
- Classic-Familie: Support Resistance als einzige ausbauen (+ Main-Channel-Merge), Exits überarbeiten; 5 Percent nur als Experiment auf der LONG-Seite weiter.
- Regime-Taxonomie reparieren (TRANSITION aufspalten) — davor ist jede Aussage „Bot X funktioniert in Regime Y" ungetestet.
- Whale-/Funding-Daten entweder als Features in Regime/Gate einspeisen oder die Logger abschalten — der aktuelle Zustand (sammeln ohne Konsument, Logger tot) ist reiner Betriebsaufwand.

---

### Einordnung zum Gesamtaudit
Dieser Bericht ergänzt die Bug-zentrierten Reports 01–13 um die Konzept-Perspektive. Die zentrale Erkenntnis beider Blickwinkel deckt sich: **Das System verdient sein Geld derzeit nicht mit ML-Skill, sondern mit S/R-basierter Trade-Konstruktion, groben Negativfiltern und Marktregime** — und es verliert Geld durch Signalinflation (FIFO), invertierte Modelle (AIM1) und ungeprüfte Hebel-Geometrien (UFI1, BTC SMC). Die Reihenfolge „R1 → Sofortmaßnahmen (Abschnitt 8) → Retrain-Programm" maximiert den Erwartungswert der Sanierung.
