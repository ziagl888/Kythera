# Dossier: SRA1 (Support/Resistance-AI-Bot)

> ML-Qualitätsfilter (Meta-Labeling) über der klassischen Support-Resistance-Strategie — **Note B−** (Report 16, Rang 3): konzeptionell sauberstes ML-Setup der Flotte, klein aber gesund und positiv kalibriert; Kernverdikt (Report 13): „funktional, mit offener Label-Frage".

## 1. Steckbrief

| Feld | Inhalt |
|---|---|
| Bot | `9_ai_sr_bot.py` |
| Artefakte | `trade_success_xgb_LONG_v2.json` / `trade_success_xgb_SHORT_v2.json` — **bewiesen: v2 = reine Formatkonvertierung von v1** via `core/update_model.py` (Booster-Vergleich: alle 100 Bäume bit-identisch, 38 Features, LONG+SHORT; Report 13) |
| Trainer | `legacy_trainers/X9-SR-ANALYZER-Schritt1.py` (v1) + `core/update_model.py` (Konvertierung). Status: Provenienz **geklärt/bewiesen**, aber Konvertierung/Training nicht versioniert (P1); Alt-Trainer `X9-SR-ANALYZER.py` (random Split) als deprecated markieren. Achtung: `core/update_model.py:35` überschreibt `.pkl`/`.joblib` in-place (P1.35) |
| Label | `success = status in ['SL1','SL2','SL3','4']` (Schritt1:157) — vermutlich „Trailing-SL nach TPn = Win"; **falls `SL1` in `closed_trades3` „SL vor TP1" bedeutet, ist das Label teilinvertiert → klären!** (Report 13, P2) |
| Features | 38, Parität Bot↔Modell exakt (JSON-verifiziert). Mängel: rohe Preisspalten als Features (Skalen-Leakage-Geruch), 1h-Look-ahead im Trainings-Join (Schritt1:56-61), Median-Imputation über Gesamtdatensatz vs. live rohe NaN |
| Thresholds | Shadow-Log-Inkonsistenz: Kommentar 0.45 vs. Code 0.35 (`9:285-299`); Minimal-Insert schreibt NULL time/direction/entry |
| Channel/Exits | Publiziert TP1–3 (Cornix), Monitor scored aber bis 10–20 Targets → Live-Statistik ≠ Cornix-Realität (P2.31 ✔, targets_hit bis 21) |
| Konzept | Kein Signalgeber, sondern Meta-Labeling nach Lopez de Prado: wohldefinierte Event-Population, Features zum Event-Zeitpunkt, Label = echtes Trade-Ergebnis derselben Strategie → strukturell kleinste Train/Live-Lücke der Flotte; Schritt1-Split chronologisch korrekt |

## 2. Live-Bilanz (aktive Ära 24.02.–03.07., dedupliziert; Report 14/Step 2)¹

- **n = 396 · WR 69,9% · ø +0,44%/Trade · Median +1,12% · Σ netto +134 Preis-%** — „gesund, klein"; einziger der vier AI-Bots (SRA1/ABR1/ATB1/AIM1) mit positivem Median.
- **Kalibrierung: positiv ✓** (Step 2, conf→win) — SRA1 gehört mit TD_1H, MIS1-8H und QM zu den wenigen echt kalibrierten Modellen der Flotte.
- Richtungssplit/Monatstrend: in den Reports nicht separat ausgewiesen (n klein).

¹ *Monitor-Vorbehalt (Report 17): Alle Zahlen sind monitor-generiert; das Monitor-Scoring stimmt nur zu 63,4% mit einem First-Touch-Replay überein (17,8% verpasste TP1, 18,8% TP1 trotz SL-zuerst). AI-Trades sind zudem rückwirkend nicht replaybar, weil `ai_signals`-Rows beim Close gelöscht werden (N4).*

## 3. Befunde (konsolidiert)

| ID | Ebene | Schweregrad | Einzeiler | Status |
|---|---|---|---|---|
| P1.20 | Bot | P1 | Bedingt fehlende ATR-Features → 35 statt 38 Spalten → predict wirft → ganze Iteration bricht, Rollback verwirft Shadow-Inserts; Crash-Loop alle 300s für 60 min (`9:135-143,268-305`) | ✔ (code-belegt, Report 13) |
| P2.30 | Bot/Daten | P2 | Loggt `posted=True` auch wenn Cooldown den Post unterdrückt hat → Phantom-Posts in der Performance-Auswertung (`9:163-164,278-283`) | ~ (offen, [DB]) |
| P2.29 | Core | P2 | `get_hvn_and_sr_levels` liest 95d **ohne ORDER BY** (von SRA1 für SL/TP genutzt!) → Phantom-Extrema als SL/TP-Preise; Fix = 1 Zeile | ~ (offen, [DB]) |
| 06-M | Bot | Mittel | Forming-Candle-Indikator-Zeile + bis 60 min stale Entry als „CMP Entry" gepostet (`9:54-74,154-188,244-253`) | ✔ (R1 live bewiesen) |
| 13-P2a | Trainer | P2 | Label-Semantik `SL1/SL2/SL3/4` unverifiziert — Inversionsrisiko | ~ (klären!) |
| 13-P2b | Trainer | P2 | 1h-Look-ahead im Trainings-Join (open_time-keyed Kerze enthält Zukunft bis +1h) | ✔ (code-belegt) |
| 13-P1 | Trainer | P1 | Training/Konvertierung nicht versioniert (3-Zeilen-Skript + meta.json fehlen) | ✔ |
| P2.31 | Monitor | P2 | Subscriber sehen TP1–3, Monitor scored bis 10–20 Targets | ✔ (Step 2: targets_hit bis 21) |
| 06-L | Bot | Niedrig | Shadow-Threshold Kommentar 0.45 vs. Code 0.35; Minimal-Insert mit NULL-Feldern | ✔ |

## 4. Abhängigkeiten & Querschnitts-Risiken

- **R1 (Forming Candle, live bewiesen):** SRA1 ist explizit als betroffen gelistet — Features/Entry auf der laufenden Kerze; jedes Retrain vor dem R1-Fix trainiert erneut auf Daten, die es live nicht gibt.
- **R3 (TZ-Mix):** Session-TZ Europe/Bucharest, naive Spalten gemischt UTC/lokal — betrifft Cooldown-/Statistik-Fenster.
- **X-R1 (Label ≠ Live-Geometrie):** bei SRA1 am schwächsten ausgeprägt (Label = echtes Strategie-Outcome), aber Label-Semantik offen; **X-R4:** Confidence unkalibriert kommuniziert (SRA1 immerhin empirisch positiv kalibriert); **X-R6:** Serving auf Forming Candle.
- Abhängig von der Classic-Strategie „Support Resistance" (B−, +596, SHORT trägt alles) als Event-Quelle und von `core/trade_utils` (P2.29) für die SL/TP-Konstruktion — Report 16: die S/R-Trade-Konstruktion ist der „heimliche Star" und plausibel die eigentliche Ertragsquelle.

## 5. Sanierungsplan

**a) Sofort (ohne Retrain):**
1. **ATR-Emit-Fix:** ATR-Features immer emittieren (als NaN — XGB kann NaN) + reindex-Guard + per-Trade try/except → killt den 300s-Crash-Loop (P1.20; Report 13 Sofortmaßnahme 3).
2. `ORDER BY open_time ASC` in `get_hvn_and_sr_levels` (P2.29, eine Zeile).
3. `posted`-Rückgabe als bool fixen (P2.30); exakt die publizierten Targets speichern (P2.31).

**b) Retrain/Umbau (Report 13/16: bester Retrain-Kandidat der vier, weil Fundament stimmt — aber in der Reihenfolge zuletzt, da funktional am gesündesten):**
- Erst Label verifizieren, dann Retrain nach dem gemeinsamen Gerüst: versionierter Trainer + meta.json, First-Touch-Label der echten Geometrie, nur geschlossene Kerzen (R1-Fix zuerst), rohe Preisfeatures raus, Isotonic-Kalibrierung, Startup-Assertion „kein Feature konstant".

**c) Offene Fragen:**
- **SL1/SL2/SL3-Label-Semantik!** Gegen `closed_trades3`-Statuscodes verifizieren — falls `SL1` = „SL vor TP1", ist das Trainingslabel teilinvertiert (wichtigste offene Frage der Familie).
- Phantom-Post-Quote (P2.30) und SL/TP-Anomalien aus P2.29 per DB-Join quantifizieren.

## 6. Belege

- `AUDIT_TODO.md` P1.20, P2.29–P2.31 · `audit_reports/06_ai_bots_a.md` (SRA1-Bot-Findings) · `audit_reports/13_x_ml_trainers.md` (SRA1-Abschnitt: v2=v1-Beweis, Label-Frage, Maßnahmen) · `audit_reports/14_bot_performance_db.md` (n=396, +134) · `audit_reports/STEP2_DB_VERIFICATION.md` (Kalibrierung positiv, targets_hit bis 21, R1-Beweis) · `audit_reports/16_strategy_concept_evaluation.md` (Note B−, Abschnitt 5) · `audit_reports/17_monitor_replay_and_gaps.md` (Monitor-Vorbehalt, N4).
