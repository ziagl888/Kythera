# Dossier: ROM1 / Regime-Orchestrator (Meta-Ebene)

> **Regime-gesteuertes Signal-Gate + eigener Trading-Bot in einem:** Detector klassifiziert das BTC-Regime, Analyzer baut daraus eine Bot×Regime-Whitelist, der Orchestrator gated alle Fleet-Signale und re-postet die Gewinner als eigene ROM1-Trades.
> **Note (Report 16b): C+** — Konzept B / Implementierung D+ / Live-Wirkung positiv.
> **Kernverdikt:** ROM1 liefert messbaren Mehrwert (+8pp WR über Fleet, +2.184 netto, positiver Median) — aber **trotz**, nicht **wegen** seiner Whitelist: Das 4D-Gate ist zu 89% default-open, gated teils auf 2,5 Monate eingefrorenen Daten, und der Mehrwert stammt plausibel aus Cooldown, Opposite-Block, grober Negativselektion und der eigenen S/R-Trade-Konstruktion. Die spezifische 4D-Hypothese („Bot X funktioniert in Regime Y") ist **ungetestet**.

## 1. Steckbrief

| | |
|---|---|
| **Komponenten** | `26_regime_detector.py` (BTC-Regime alle 5 min, 15m-Returns/ATR, adaptive P75/P40-Schwellen, 2-Check-Debounce) · `27_bot_regime_analyzer.py` (Outcome-Attribution → Whitelist) · `28_signal_orchestrator.py` (Gate + ROM1-Forwarding + Regime-Auto-Close) · `core/regime_logic.py` (Klassifikations-/Debounce-Logik) |
| **„Modell"** | Kein ML-Artefakt, sondern die **`bot_regime_whitelist`-Statistik**: 4D-Matrix Bot × Regime × Alt-Context × Direction × 3 Zeitfenster = 4.056 Zellen; Gate-Regel `wr_bot ≥ wr_overall` (Punktschätzer, kein Signifikanztest/Shrinkage) |
| **Datenfluss** | `regime_history` (raw) → debounce → `regime_current` · Analyzer → `bot_regime_performance` → `bot_regime_whitelist` · Orchestrator scannt `telegram_outbox` → `identify_bot` → Whitelist-Gate → Forward als ROM1 (**eigene Geometrie:** CMP-Entry, S/R-SL, bis 20 Targets) → `orchestrator_open_trades`; Suppressions → `orchestrator_suppressed_signals`; bei Regime-Wechsel Auto-Close aller offenen Trades auf Coin+Richtung |
| **Tabellen** | `regime_history`, `regime_current`, `bot_regime_performance`, `bot_regime_whitelist`, `orchestrator_open_trades`, `orchestrator_suppressed_signals`, `telegram_outbox` |
| **Channel** | Regime-Trading-Channel `CH_REGIME_TRADING` (−1003963430969) |

## 2. Live-Bilanz (Step 2 + Report 14/16, dedupliziert, netto −0,10% Fee, ungehebelt)

- **ROM1: n=2.677, WR 69,2% vs. 61,1% Fleet (+8pp), ø +0,92%/Trade, Median +1,00% (fast einziges Modell mit positivem Median), Σ +2.184 netto.**
- **Lifecycle dicht:** 0 OPEN-Trades älter 7 Tage; 4.339 Closes seit 18.04.; Gate greift real: **Gate-Rate 44,7% (Apr) → 63,5% (Jun)** — die Juni-Rate basiert für MIS-Familie + Channel-Bots aber auf April-Statistik (P0.4).
- **Whitelist hohl:** **89% Default-Open** — frische Rows: 747× `insufficient_data`, nur 41× `wr_above_overall` + 52× `wr_below_overall` datenbasiert (11% echte Entscheidungen); **Median 7 Trades/Zelle**, 68% der Zellen <30. Historisch dennoch 3.043 `wr_below_overall`-Suppressions (auf teils stale Daten).
- **Detector:** nur **7 TREND-Episoden in 5,5 Monaten** (5× TREND_DOWN, 2× TREND_UP, alle <1h) — Verteilung **TRANSITION 44,5% / HIGH_VOLA 29,7% / CHOP 25,8%**; **52% aller Raw-Episoden sind <1h-Flaps** (654/1.257), Median-Dauer CHOP/TRANSITION 0,9h; Confidence median 0,54, p10 0,40; 2,9 Raw-Wechsel/Tag, 17,2% der 2h-Fenster mit ≥2 Regimes.
- **Auto-Close schneidet blind:** 3.653 REGIME_CHANGE-Closes, **median PnL 0,00%, 49,3% im Gewinn geschnitten**; 35% aller ROM1-Trades (1.411/4.339) enden per Regime-Close statt TP/SL; AIM1 wird im Schnitt bei **+9,5%** gekappt.
- **ROM1-Risiko:** SL-Distanz median 7,9%, **p90=17,9%, max 65,3%**; 20/133 Signale >15% → bei 20x jenseits Liquidation (P2.27).
- **Interpretations-Vorbehalt (16b):** Die +8pp sind optimistisch verzerrt (P1.9-Zensur, WR-Metrik, fremde Outcomes via P1.8) — echter Mehrwert wahrscheinlich positiv (Netto-PnL + Median stützen das unabhängig), aber kleiner als die Schlagzeile.

## 3. Befunde

Status: ✔ = live/DB bewiesen · ~ = Code-Befund, live nicht (voll) quantifiziert · ✘ = live entschärft/nicht beobachtet.

| ID | Komponente | Schweregrad | Einzeiler | Status |
|---|---|---|---|---|
| P0.3 / B7 | 28 | CRITICAL | Orchestrator konsumiert eigene ROM1-Posts (Self-Echo): **109 Rows** aus dem eigenen Channel; nur der 4h-Cooldown (erst NACH Send committed) verhindert Doppel-Trades — Crash-Fenster bleibt | ✔ |
| P0.4 / B5 / P2.25 | 28↔27 | CRITICAL | Bot-Name-Mismatch (pretty_name fehlt im Orchestrator): Raw-Namen-Rows **eingefroren seit 19.04.** → MIS-Familie + Channel-Bots gaten auf 2,5 Monate alten Stats | ✔ |
| B1 | 27/Whitelist | HIGH | Whitelist zu **89% Default-Open** (747× `insufficient_data` vs. 93 datenbasierte Entscheidungen) | ✔ |
| B2 | 27/Whitelist | HIGH | 4D-Matrix statistisch unterbesetzt: 4.056 Zellen, **Median 7 Trades/Zelle**, 68% <30 | ✔ |
| B3 | 26 | HIGH | Detector kennt de facto nur 3 Regimes — TREND-Klassen tot (7 Episoden/5,5 Monate, alle <1h); Strukturfehler: Trend erfordert *niedrige* Vola, Mid-Vola-Band fällt immer in TRANSITION | ✔ |
| B4 | 26 | HIGH | 52% der Raw-Episoden sind <1h-Flaps; Confidence p10=0,40 — Detector rät oft | ✔ |
| B6 | 28/Auto-Close | HIGH | Auto-Close schneidet blind: median 0,00% PnL, 49,3% im Gewinn gekappt, 35% aller ROM1-Trades enden so → Churn, Fees, zensierte Statistik | ✔ |
| B8 | 28 | MEDIUM | Forwards ohne `wl_reason` geloggt → nicht messbar, welcher Gate-Pfad Geld verdient | ✔ |
| B9 | 27↔28 | HIGH | **Zirkularität:** Analyzer lernt aus Outcomes, die der Orchestrator selbst zensiert (B6/P1.9) → Whitelist-WRs systematisch geschönt | ~ |
| B10 | 28/identify_bot | MEDIUM | 841 `bot_unidentified`-Suppressions (drittgrößter Grund) — Patterns decken den Signalstrom nicht ab | ✔ |
| P1.6 | 28 | HIGH | `sent=FALSE`-Filter racet gegen Dispatcher → Signale still nie gegated, kein Log | ~ |
| P1.7 | 28 | HIGH | Forward-Pipeline nicht atomar, Batch-Cursor am Pass-Ende → fired-but-untracked / Batch-Replay | ~ |
| P1.8 | 28 | HIGH | `sync_closed_trades` matcht fremde Trades (kein model-Filter, 720h-Fenster) → falsche ROM1-Outcomes, Opposite-Schutz fällt vorzeitig | ~ |
| P1.9 | 28 | HIGH | Regime-Close löscht offene Trades **aller** Bots auf Coin+Richtung → fremde Verluste als neutral zensiert, Whitelist-WR nach oben gebiast | ~ |
| P1.10 | Doku↔28 | HIGH | Spec-Drift: Doku sagt „reiner Signal-Router", Code baut eigene Trades → Gating-Statistik ≠ Ausführungs-Statistik | ✔ |
| P2.21 | 28/market_utils | MEDIUM | TZ-Mix: 4h-Cooldown effektiv 6h, 60s-Fenster wird 2h (R3 live bewiesen: DB-TZ Europe/Bucharest) | ~ |
| P2.22 | 27 | MEDIUM | Training/Serving-Skew: Attribution auf RAW `regime_history`, Gating auf debounced `regime_current` + Backfill-Look-ahead | ~ |
| P2.23 | 28 | MEDIUM | „Unreliable"-Heuristik zählt RAW-Flaps → Overall-Fallback dominiert (TRANSITION 44,5%, 256 `regime_is_transition`-Suppressions) | ✔ |
| P2.24 | 28 | MEDIUM | Regime-Wechsel während Downtime nie nachgeholt (In-Memory-State) | ~ |
| P2.26 | 28 | MEDIUM | Kein Same-Direction-Open-Check (Stacking nach Cooldown möglich) — live aktuell **keine** gestapelten Duplikate beobachtet | ✘ |
| P2.27 | 28/ROM1 | MEDIUM | ROM1-SL ohne Distanz-Cap: p90=17,9%, max 65,3%, 20/133 >15% → bei 20x jenseits Liquidation (R4) | ✔ |
| P2.28 | 28 | MEDIUM | 60s-Fenster + start_delay=175 → jeder Restart wirft ≥3 min Signalstrom kommentarlos weg | ~ |

## 4. Abhängigkeiten & Querschnitts-Risiken

- **Zirkularität (B9):** Whitelist-Statistik ← Monitor-Outcomes ← Orchestrator-Auto-Close. Drei gleichgerichtete Aufwärts-Biases auf genau der Zahl, an der das Geld-Gate hängt: Open-Trade-Zensur, Regime-Change-Closes als „neutral" entfernt statt realisiert, Fremd-Trade-Zensur (P1.9).
- **Monitor-Label-Vorbehalt (Report 17):** Monitor-Scoring stimmt nur zu **63,4%** mit dem First-Touch-Replay überein (je ~18% verpasste und fälschlich vergebene TP1s) → die per-Trade-Wahrheit, auf der Analyzer und Whitelist rechnen, ist unzuverlässig. Der Monitor-Rewrite steht damit VOR jeder Whitelist-Härtung.
- **Falsche Gate-Metrik:** Das Gate optimiert WR (TP1-Touch), von der Report 14 beweist, dass sie irreführend ist (67% WR kann netto negativ sein); avg_pnl/median/sharpe_like stehen bereits in der Tabelle und werden ignoriert.
- **Geometrie-Bruch (P1.10):** Whitelist wird auf Trades mit Original-Parametern erhoben, ROM1 exekutiert eigene Geometrie — die statistische Kette ist gerissen.
- **Erwartung nach Fixes (16b):** P0.4-Fix bringt moderaten Zusatzgewinn (TRANSITION-Fallback bleibt); der P1.9-Fix wird die gemessenen WRs **senken** — gewollt, vorab kommunizieren.

## 5. Sanierungsplan (4 Stufen aus Report 16, komprimiert)

1. **Stufe 1 — Reparieren (Tage):** P0.4 (`pretty_name()` nach `identify_bot()`, April-Rows purgen, `computed_at`-Staleness-Gate >48h, Default-Open-Alarm) · P0.3 (Channel-Filter im Scan-SELECT, ROM1-Hard-Reject, Cooldown VOR Send committen; P1.7: Txn zuerst, Outbox zuletzt) · P1.9 (Auto-Close nur `model='ROM1'`; P1.8: sync mit Model-Filter ±60s; P1.6: id-Cursor) · B10 (identify_bot-Patterns gegen die 841 nachziehen) · B8 (`wl_reason`-Spalte).
2. **Stufe 2 — Statistik ehrlich machen (Wochen):** 4D-Matrix durch **hierarchisches Shrinkage** (Empirical-Bayes, untere Wilson-Grenze > Break-even) ersetzen · Zensur-Korrektur: Regime-Closes mit PnL-zum-Close in die Statistik (B9) · **Suppressed-Counterfactual-Scorer** ✔(Tooling T-2026-CU-9050-047: `tools/rom1_counterfactual.py`, scored beide Seiten pro Gate-Pfad via First-Touch-Replay; Lauf braucht VPS) — macht den Gate-Wert erstmals laufend messbar (heute unbekannt).
3. **Stufe 3 — Detector & Gating weiterentwickeln:** TREND-Features (EMA-Slope-Persistenz, ADX), adaptive Hysterese gegen 52% Flaps, `UNKNOWN` statt Raten · TRANSITION aufspalten bzw. **Transition-Resolution-Modell S10** (TRANSITION-Trades sind mit 63–64% WR gut — das Regime ist handelbar, der Fallback zu grob) · **Regime-Richtungs-Matrix S2** als grobe zweite Gate-Ebene (CHOP→nur SHORT, Longs dort −3,69%/Trade; HIGH_VOLA→LONGs droppen) · Auto-Close differenzieren (Gewinner trailen statt kappen — 49% im Gewinn, AIM1 bei +9,5%) · ROM1-Geometrie: Original durchreichen oder SL-Cap + `cap_leverage_to_sl` (P2.27/R4).
4. **Stufe 4 — Betriebsfestigkeit:** Startup-Reconcile offener Trades gegen Whitelist (P2.24), Detection-Fenster 5–10 min + `stale_signal`-Log (P2.28), Fallback-/Default-Open-Rate als Gesundheitsmetrik im Status-Post.

**Priorität (Report 16):** Stufe 1 komplett → Counterfactual-Scorer (Nr. 8) ✔Tooling(T-2026-CU-9050-047) → Shrinkage + S2-Matrix → Stufe 3 nach Datenlage. Größter Hebel sind drei **Konzeptänderungen**, keine Bugfixes: TRANSITION aufspalten, Gate-Metrik WR→Netto-Expectancy, ROM1 als eigenen Bot mit eigener Evidenzschicht führen (16b). Ziel-Note nach P0/P1-Fixes + Konzeptänderungen: **B**.

## 6. Belege

- `audit_reports/16_regime_orchestrator_analysis.md` — Hauptquelle: B1–B10, 4-Stufen-Plan, Ziel-Bild (Step 6)
- `audit_reports/04_orchestrator_regime.md` — Code-Findings 26/27/28/regime_logic + Cross-cutting
- `audit_reports/STEP2_DB_VERIFICATION.md` — P0.3 (109 Self-Echos), P0.4 (eingefroren 19.04.), P2.23 (44,5% TRANSITION), P2.27 (SL-Distanzen), ROM1 69,2% vs. 61,1%
- `audit_reports/14_bot_performance_db.md` — n=2.677, +2.184 netto, Median +1,00
- `audit_reports/16_strategy_concept_evaluation.md` — Note C+, Abschnitt 7 (drei Lesarten der +8pp)
- `audit_reports/15_strategy_proposals.md` — E2/E8, S2 Regime-Richtungs-Matrix, S10 Transition-Modell
- `audit_reports/17_monitor_replay_and_gaps.md` — Monitor-Label-Vorbehalt (63,4%)
- `AUDIT_TODO.md` — P0.2–P0.4, P1.6–P1.10, P2.21–P2.28 mit Step-2-Annotationen
