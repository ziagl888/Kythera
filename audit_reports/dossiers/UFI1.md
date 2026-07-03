# Dossier: UFI1 (Fibonacci-Dead-Cat-Bounce-Short)

> Regelbasierter Daily-Fibonacci-Short auf gedumpte Coins (kein ML) — **Note F** (Report 16, Rang 26/26): schlechtestes Modell der Flotte pro Trade (25,7% WR, −7,90% ø), Backtest-Claim „+278R" durch Look-ahead entwertet, 20x-Hebel bei ~34% SL-Distanz mathematisch nicht existenzfähig. Kernverdikt: **konzeptionell tot — stoppen.**

## 1. Steckbrief

| Feld | Inhalt |
|---|---|
| Bot | `29_ufi1_bot.py` |
| ML-Modell | **keins** — reines Fibonacci-Regelwerk |
| Regelwerk | Dump-Erkennung → Fib-Retracement des Dumps → „Kerze schließt unter Fib-Level" als Daily-Confirmation → SHORT. Entry = CMP (~0,77·swing_high), `sl = swing_high·1.03` (~34% Distanz), Single-TP1. Hinweis: das „0.382"-Level im Code ist real das **61,8%-Retracement** des Dumps (Labeling-Konvention, `29:109-111,48`); „Rejection" wird akzeptiert, ohne dass das Level je berührt wurde (±2% reicht); 48h-Cooldown → Refire gealteter Setups, Confirmation-Kerze kann ~2 Wochen alt sein |
| Leverage | **20x** — bei ~34% SL-Distanz liquidiert isoliert ~+5%, lange vor dem SL (P0.6, R4) |
| „Backtest" | `fib_backtest.py` — Claim 54,2% WR / +278R / +0,83R ø. Entwertet durch: (1) Entry-Wahl über das **zukünftige globale Fenster-Tief** (argmin über volles 30-Bar-Fenster = Look-ahead), (2) 5-Target-Trailing-Ladder im Backtest vs. Single-TP1 live, (3) Live-Entries bis Wochen stale bei CMP |
| Channel | Cornix-Channel; postet Plain-Cornix-Block UND zweite HTML-Message mit identischem Block (Double-Parse-Risiko P3.9); Logging-Stil positiv (ERROR+exc_info+rollback — Vorbild laut Report 08) |

## 2. Live-Bilanz (aktive Ära, dedupliziert; Report 14/Step 2)¹

- **n = 35 · WR 25,7% · ø −7,90%/Trade · Median −3,22% · Σ netto −280 Preis-%** — „katastrophal (bestätigt P0.11)"; vs. beworbene 54,2% WR.
- Richtungssplit: entfällt (Short-only-Strategie). Kalibrierung: entfällt (kein Modell). Monatstrend: bei n=35 nicht ausgewiesen.
- **Leverage nicht eingerechnet:** −7,9% ø bei 20x wäre Liquidation (Report 14) — die realen Kontoverluste sind ein Vielfaches der Preis-%.
- Report 16: „Dass UFI1 exakt so implodiert ist, wie die Look-ahead-Analyse vorhersagt, validiert umgekehrt die Audit-Methodik."

¹ *Monitor-Vorbehalt (Report 17): Zahlen monitor-generiert (Replay-Übereinstimmung gesamt nur 63,4%); AI-Trades rückwirkend nicht replaybar (N4). Am F-Verdikt ändert das nichts — beide Fehlklassen (verpasste/fälschliche TP1) treten je ~18% auf, der Abstand zu 54,2% ist um Größenordnungen größer.*

## 3. Befunde (konsolidiert)

| ID | Ebene | Schweregrad | Einzeiler | Status |
|---|---|---|---|---|
| P0.6 | Bot/Risiko | P0 | 20x Leverage mit ~34% SL (`29:194,244`) → isolierte Liquidation ~+5% **vor** dem SL; die „+0,83R" überleben 20x nicht | ✔ (mathematisch; R4) |
| P0.11 | Backtest | P0 | „+278R"-Claim nicht auf den Live-Bot übertragbar: Look-ahead-Entry (zukünftiges Fenster-Tief), Trailing-Ladder vs. Single-TP1, stale CMP-Entries | ✔✔ (Step 2: live 25,7% WR, n=35) |
| 11-H1 | Backtest | Hoch | `fib_backtest.py:252-262,327-336`: argmin über volles Fenster wählt Entries mit Zukunftswissen → Live-Bot sieht andere Trade-Population, flachere Fib-Anker | ✔ (code-belegt) |
| 11-H2 | Bot | Hoch | Kein Recency-Check auf der Confirmation-Kerze → wochenalte Setups feuern bei CMP irgendwo in [tp1·1.02, sl) → WR/R beliebig; Refire nach 48h + ai_signals-Clear = Trade-Count-Inflation (`29:177-226,241,363`) | ✔ (code-belegt) |
| 08-H | Bot | Hoch | „Candle closes below Fib" kann auf der **noch laufenden Tageskerze** evaluiert werden (j erreicht n−1) — Intraday-Dip zählt als bestätigte Daily-Rejection (`29:177-193,66-88`) | ✔ (R1 live bewiesen) |
| 08-M1 | Bot | Mittel | Fib-Level-Mislabeling (0.382 = 61,8%-Retracement) — intern konsistent NUR falls Backtest dieselbe Formel nutzt; dokumentieren/bestätigen | ~ (offen) |
| 08-M2 | Bot | Mittel | Gealterte Setups refeuern alle 48h; stale Korridor breit; ai_signals blockt nur solange offen (Monitor löscht Rows) | ✔ (code-belegt) |
| 08-L | Bot | Niedrig | „Rejection" ohne je berührtes Level akzeptiert (Close binnen ±2% reicht) | ✔ |
| P3.9 | Telegram | P3 | Plain-Cornix-Block + HTML-Duplikat in denselben Channel → Double-Parse-/Doppelausführungs-Risiko | ~ (offen, [DB]) |

## 4. Abhängigkeiten & Querschnitts-Risiken

- **R4 (Leverage vs. SL nirgends abgeglichen) — der Killer:** UFI1 ist neben BTC SMC (100x/1,2%-SL) der Hauptfall; **Liquidation kommt lange vor dem SL**, der SL wird real nie erreicht. Zentraler Fix `cap_leverage_to_sl(sl_pct)` in `core/trade_utils` schließt die ganze Klasse (auch ROM1-SL-Distanzen p90=17,9%).
- **R1 (Forming Candle):** Confirmation auf der laufenden Tageskerze; Bot 29 „mixt" den Kerzen-Vertrag (Report 08).
- **P0.10-Muster („backtest the detector, trade something else"):** fib_backtest ist einer der drei Belegfälle des dominanten Musters aus Report 11 — keine der publizierten WR/R-Zahlen beschreibt das System, das handelt.
- Zielpopulation = frisch gedumpte Alt-Coins mit 50%-Squeeze-Kerzen — genau die Assets, bei denen ein 25–40%-SL + 20x maximal toxisch ist (Report 16).

## 5. Sanierungsplan

**a) Sofort:**
1. **Stoppen** — einhellige Empfehlung aus Report 14 (D.3), Report 16 (Note F, „Sofort stoppen", Abschnitt 8.1). Alternativ, falls Weiterbetrieb erzwungen: **Leverage-Cap** (Hebel aus SL-Distanz ableiten, ~1–2x, hart ≤3x; P0.6/R4) — ohne den ist jeder einzelne Trade ein Liquidations-Kandidat.
2. Falls weiterbetrieben zusätzlich: `j ≤ n−2` (Confirmation nur auf geschlossenen Tageskerzen), Frische-Gate (Confirmation in den letzten 1–2 Daily-Kerzen), Setup-keyed Cooldown.

**b) Retrain/Umbau:**
- Kein Retrain (kein Modell). Ein Neu-Backtest wäre nur als **Walk-Forward mit `find_ufi1_setup`** valide (Replay der bot-eigenen Setup-Funktion bar-für-bar, Exit-Modell = Single-TP1, Fees) — Report 16: „kein Grund zu glauben, dass eine korrekte Version positiv wäre"; die validierende Evidenz selbst ist entwertet. Umbau-Aufwand ist daher nicht gerechtfertigt.

**c) Offene Fragen:**
- Fib-Konvention (0.382↔61,8%) gegen `fib_backtest.py` bestätigen — nur relevant, falls je ein ehrlicher Re-Backtest gefahren wird.
- P3.9: Cornix-Double-Parse in den Outbox-Daten prüfen (betrifft auch Bots 24/25).

## 6. Belege

- `AUDIT_TODO.md` P0.6, P0.11 (+Step-2-Annotationen), R4, P3.9 · `audit_reports/08_smc_bots.md` (29_ufi1-Findings) · `audit_reports/11_ml_backtest.md` (fib_backtest-Kritik, Cross-Cutting 1) · `audit_reports/14_bot_performance_db.md` (n=35, 25,7% WR, −7,90% ø, −280 netto) · `audit_reports/STEP2_DB_VERIFICATION.md` (P0.11 ✔) · `audit_reports/16_strategy_concept_evaluation.md` (Note F, Abschnitt 6) · `audit_reports/17_monitor_replay_and_gaps.md` (Monitor-Vorbehalt, N4).
