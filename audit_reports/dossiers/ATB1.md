# Dossier: ATB1 — Trendline Break/Bounce (Bot 14)

> ML-gescorte Trendlinien-Events auf 538 Coins. **Note D (Report 16).** Kernverdikt: „Das Modell sah nie das Event, das es scored" — Trainer labelt ein anderes mathematisches Objekt als der Bot handelt; live netto negativ (−172). Neuaufbau oder parken.

## 1. Steckbrief

| Feld | Inhalt |
|---|---|
| Bot-Datei | `14_ai_atb_bot.py` (stündlicher Scan über 538 Coins, Minute 3) |
| Artefakte | `long_trend_prediction_model.joblib` / `short_trend_prediction_model.joblib` — XGBClassifier `binary:logistic`, 19 Features (Pickle-verifiziert: `feature_names` == `features_dict` des Bots) |
| Trainer | `legacy_trainers/BT1-Datagrepper-for-ml.py` (Daten/Labels) + `BT1-ML-Trainer_Optimized.py` (Training) + `BT1-Thresholdoptimizing_V2.py` (Schwellen). `BT1-ML-Trainer.py` ist tot (20 Features, würde crashen) — Provenienz in Report 13 geklärt |
| Trainingsdatum | nicht dokumentiert (keine meta.json im Artefakt) |
| Datenquelle | 1h-Kerzen/Indikatoren der Live-DB; Coin-Universum = heutige `coins.json` (Survivorship-Bias, Report 13) |
| Label-Definition | +10%-**Touch** binnen 72h, **ohne SL-Pfad** — Events sind Kreuzungen der **90d-Close-Regressionsgeraden**, nicht die live gehandelten Trendlinien |
| Live-Event | Pivot-Chart-Trendlinien (`find_peaks`, R≥0,2 — extrem lax) mit 4-Event-State-Machine (Break/Bounce up/down); Bounce-Events haben **kein Trainings-Gegenstück**; Break und Bounce sind für das Modell ununterscheidbar (Event ist kein Feature) |
| Thresholds | 0.80 / 0.75 — aus `BT1-Thresholdoptimizing_V2.py:48,96-103` auf dem (rekonstruierten) **Test-Set maximiert** (Maximum-Statistik-Artefakt, X-R2) |
| Channel | in den Quellen nicht dokumentiert |

## 2. Live-Bilanz (Stand 2026-07-03, aktive Ära 24.02.–03.07., dedupliziert)

- **n = 306 · WR 65,7% · ø −0,46%/Trade · Median 0,00 · Σ netto −172 Preis-%** (Report 14: „negativ, passt zu Report-13-Verdikt")
- Richtungssplit: für ATB1 nicht ausgewiesen (Report 14 nennt Asymmetrien nur für EPD1/RUB1/BR1H)
- Kalibrierung: in der Step-2-Messung ohne Wert („—") — kein Beleg, dass die Confidence Information trägt; Report 16: „Das ML-Gate ist faktisch ein Zufallsfilter"
- 65,7% „WR" bei negativem Netto = Paradebeispiel des Querschnittsbefunds „Win (TP1-Touch) ≠ Profit"
- Portfolio-Einordnung: Report 14 D.3 „Stoppen/parken: … ATB1"; Report 16 §8 „Parken"[^1]

[^1]: **Monitor-Vorbehalt (Report 17):** Alle Live-Zahlen sind monitor-generiert. Das First-Touch-Replay (Classic-Sample, n=388) stimmt nur zu **63,4%** mit dem Monitor-Scoring überein (17,8% verpasste TP1, 18,8% TP1 trotz SL-zuerst); für die AI-Flotte ist ein Replay rückwirkend unmöglich (N4: `ai_signals` löscht SL/Targets beim Close). Dazu P1.2 (Trailing-SL zieht nie nach), P2.7, P2.31, P1.9 — per-Trade-Wahrheit unzuverlässig, Netto-Bias moderat.

## 3. Befunde (konsolidiert)

Status: ✔ = bewiesen/bestätigt (Step 2/3) · ✘ = widerlegt · ~ = Code-Befund, offen/teilbestätigt

| ID | Ebene | Schweregrad | Einzeiler | Status |
|---|---|---|---|---|
| R13-ATB1-1 (X-R1/P0.10) | Trainer | P0 | Event-Mismatch: Trainer labelt Kreuzungen der 90d-Close-Regressionsgeraden, Live handelt Pivot-Trendlinien — Modell scored eine Event-Population, die es nie sah | ✔ (Step 3) |
| R13-ATB1-2 | Bot+Trainer | P0 | `vol_ratio`-Skew hergeleitet: live ≈1/19 der Trainingsskala (3-min-Forming-Candle ÷ rolling-20) — deckt die Audit-Beobachtung ~1/20; keine Trainingsdaten für den Live-Wertebereich | ✔ (Step 3) |
| P1.22 | Bot | P1 | ML-Features auf 3-min-alter Forming-Candle (`row=df.iloc[-1]` nicht gesliced, anders als ABR1); RSI/MACD/TSI/BB/DC auf Partial-Close | ~ (R1 live bewiesen, Bot-Fix offen) |
| P1.23 | Bot | P1 | Aborted transaction vergiftet den Rest des 538-Coin-Scans (kein rollback im per-Coin-except, nicht autocommit) | ~ |
| R13-ATB1-3 (X-R3) | Trainer | P1 | Random `train_test_split` über 72h-**überlappende** Fenster (`BT1-ML-Trainer_Optimized.py:46`) → Zwillings-Leakage | ✔ (Step 3) |
| R13-ATB1-4 (X-R2) | Trainer | P1 | Live-Thresholds 0.80/0.75 auf dem Test-Set maximiert (`BT1-Thresholdoptimizing_V2.py`) | ✔ (Step 3) |
| R13-ATB1-5 (X-R1) | Trainer | P1 | Label +10%-Touch/72h **ohne SL** vs. Live-SL bis −8,8% — Confidence schätzt eine nie gehandelte Größe | ✔ (Step 3) |
| P2.36 | Bot | P2 | „unknown"-State-Break-Trigger bewusst reaktiviert (Kommentar: „BUG AUS DEINEM ALTEN BOT WIEDER AKTIV") → State-Loss = Massen-Event-Flood, stale Breaks >0.80 posten echte Signale | ~ |
| P2.37 | Bot | P2 | Main-Loop fängt nur KeyboardInterrupt → jede Scan-Exception killt Prozess + leakt Conn; dazu naiver `last_alert`-TypeError (TZ, R3) | ~ |
| R13-ATB1-6 | Trainer | P2 | `make_scorer(roc_auc_score)` auf harten Labels (GridSearch optimiert das Falsche); Survivorship über heutige coins.json; Live-`fillna(0)` erzeugt nie gesehene Werte (X-R5) | ✔ (Step 3) |
| R07-ATB1-a | Bot | LOW | Live-pandas_ta-Recompute + `fillna(0)` als dokumentiertes Train/Serve-Drift-Risiko (Parität nicht falsifizierbar, Namen matchen) | ~ |
| R07-ATB1-b | Bot | LOW | „loaded successfully" wird geloggt, auch wenn kein Modell-File existiert → stiller Info-only-Degrade | ~ |
| R07-ATB1-c | Bot | LOW | Stündliches N+1: 538×95d-Reads + 150dpi-22×15in-Chart pro Event; CREATE TABLE im Event-Pfad | ~ |

## 4. Abhängigkeiten & Querschnitts-Risiken

- **R1 (Forming Candle, Step 2 bewiesen):** ATB1 ist einer der zwei von drei AI-Bots (14/15) **ohne** Forming-Candle-Verteidigung — Kontamination architektonisch, Backtests auf denselben Tabellen sehen finale Kerzen → Live/Backtest-Divergenz eingebaut.
- **R3 (TZ-Mix, Step 2 bewiesen):** naiver `last_alert`-Vergleich (P2.37-Umfeld).
- **X-R1…X-R6 (Report 13):** ATB1 verletzt alle sechs — Label≠Geometrie, Test-Set-Threshold, Split-Leakage, unkalibrierte „Confidence %", Silent-Default (`fillna(0)`), Forming-Candle-Serving.
- **Silent-Feature-Death-Muster (Report 07):** gemeinsam mit ABR1/AIM1; ein geteilter „assert kein Feature konstant"-Helper fängt alle drei.
- **Modell-Staleness:** Load einmal beim Start, kein Hot-Reload (Report 07, Querschnitt #4); `ml_predictions_master.trade_id` immer 0 (tote Verknüpfung); Chart-Lifecycle-Risiko (Housekeeping löscht Charts >2h, Outbox referenziert sie).
- **Whitelist/Orchestrator:** Gating-Statistiken basieren auf der irreführenden WR-Metrik und monitor-verzerrten Outcomes (Report 16 §7, Report 17) — auch ATB1-Bewertungen im Gate erben das.

## 5. Sanierungsplan

**(a) Sofort ohne Retrain:** **Parken** (Report 14 D.3, Report 16 §8 — das Modell ist ein Zufallsfilter mit negativem Netto). Falls Weiterbetrieb erzwungen: Confidence nicht mehr als „%" kommunizieren (Report 13, Maßnahme 4), `rollback` im per-Coin-except (P1.23), „unknown"-State auf observe-only (P2.36), breites except+Backoff im Main-Loop (P2.37).

**(b) Retrain-Anforderungen:** Kein Fix, sondern **Neuaufbau von null** (Report 16): Event-Definition fixieren und auf **Live-Events** labeln (Bounce-Events inklusive, Event-Typ als Feature). Voraussetzungen: R1-Fix zuerst, gemeinsamer Walk-Forward-First-Touch-Simulator (P0.10/V3) als Label-Quelle, zeitlicher 3-Wege-Split mit Embargo + Episoden-Dedup, Threshold auf Validation, Isotonic-Kalibrierung out-of-time, meta.json (Features/Threshold/Zeitraum/Hash) im Artefakt, Startup-Assertion (Report 13, Neutraining-Gerüst). Reihenfolge-Empfehlung Report 13: ATB1 nach MIS1/AIM1/ABR1.

**(c) Offene Fragen:** Channel + Trainingszeitraum undokumentiert; Richtungssplit nie ausgewertet; `trendmeet_rawdata`-Event-Bursts um Restarts (Report 07, DB-Frage 8) ungeprüft; Monitor-Rewrite (Report 17) muss vor jedem neuen Label-Lauf stehen.

## 6. Belege

- `AUDIT_TODO.md` → P1.22/P1.23/P2.36/P2.37 (Bot-Findings), R1/R3/R4-Kontext, P0.10-Muster
- `audit_reports/07_ai_bots_b.md` → Forming-Candle-/vol_ratio-Detail, Robustheits-/LOW-Findings, Pickle-Verifikation, Querschnitte
- `audit_reports/13_x_ml_trainers.md` → Trainer-Provenienz (BT1-Kette), Verdikt „nicht vertrauenswürdig", Event-Mismatch, X-R1..R6
- `audit_reports/14_bot_performance_db.md` → n=306, WR 65,7%, ø −0,46%, Σ −172; Empfehlung stoppen/parken
- `audit_reports/STEP2_DB_VERIFICATION.md` → R1/R3 live bewiesen; ATB1 ohne Kalibrierungswert
- `audit_reports/16_strategy_concept_evaluation.md` → Note D, Konzeptkritik („Zufallsfilter", Neuaufbau oder parken)
- `audit_reports/17_monitor_replay_and_gaps.md` → Monitor-Vorbehalt (63,4% Übereinstimmung, N4)
