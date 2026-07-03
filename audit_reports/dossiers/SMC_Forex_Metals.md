# Dossier: SMC Forex/Metals

> Regelbasierter SMC-Bot (Structure-Break + FVG) auf Forex-/Metall-Symbolen. **Note (16): D−.** Kernverdikt: „Retail-SMC-Folklore + Repaint-Entries + SL ohne Sanity-Check" — komplett unvermessen (kein `ai_signals`, kein valider Backtest) → Abschalt-Kandidat; wenn behalten, dann erst instrumentieren.

## 1. Steckbrief

| | |
|---|---|
| Bot | `16_smc_forex_metals_bot.py` — regelbasiert, kein ML, kein Trainer |
| Signale/TF | STRUCTURE (BOS) + FVG-Mitigation; Cooldown-Module SMC_1H/2H/4H/1D_FVG belegt; 2h/4h per Resample, dazu 1d/1w |
| Datenquellen | DB **und** yfinance (beide mit Forming-Candle-Problem) |
| Channel | CH_SMC_FOREX; einziger Bot der Familie, der den Cornix-Block in **eine** Message einbettet (kein P3.9-Doppel-Parse-Risiko) |
| Leverage | „20x-10x" wird gepostet, ohne SL-Distanz-Abgleich (SL = letztes Swing-Low, kann 20–30% entfernt oder sogar über Entry liegen) |
| Tracking | **keins** — schreibt kein `ai_signals`, taucht in keiner Performance-Statistik auf |

## 2. Live-Bilanz

**Keine.** Bot 16 gehört zu den drei komplett unvermessenen Bots (16/17/21, Report 16 Querschnittsbefund 6): n, WR, PnL unbekannt; kein valider Backtest existiert. Report 16: „Was unmessbar ist, hat in einer Bot-Flotte keinen Ertragsanspruch: instrumentieren oder abschalten." Einziger Live-Fußabdruck in den Quellen: 83 `SMC_*_FVG`-Cooldown-Rows (Step 2) — der Bot feuert also tatsächlich.

## 3. Befunde

| ID | Ebene | Schweregrad | Einzeiler | Status |
|---|---|---|---|---|
| P1.26 | Bot | — | „FVG-Entry ist unerreichbarer Dead-Code" | **✘ WIDERLEGT** (Step 2: 83 SMC_1H/2H/4H/1D_FVG-Cooldown-Rows — der FVG-Pfad feuert; These falsch oder galt für ältere Codeversion) |
| P1.27 | Bot | HIGH | Entscheidungen auf der Forming Candle in **beiden** Datenquellen (DB droppt laufende Kerze nicht; yfinance-„FIX" behält die In-Progress-Row bewusst); forming 1d/1w hält die Bedingung tagelang → 12h-Cooldown → Refire die ganze Woche | ✔ (Code) |
| P2.45a | Bot | MEDIUM | Weekend-/Static-Data-Refire: Forex schließt Fr 22:00, Bot scannt das ganze Wochenende, dasselbe Freitag-Signal re-postet mit Freitagspreis in den geschlossenen Markt | ✔ (Code) |
| P2.45b | Bot | MEDIUM | Kein SL-Seiten-/RR-Sanity-Check auf BOS: SL kann 20–30% weg oder über dem Entry liegen; „20x-10x" wird trotzdem gepostet (21 validiert, 16 nicht) | ✔ (Code) |
| 08-LOW | Bot | LOW | 2h/4h-Resample in Exchange-Lokalzeit vor UTC-Konvertierung → Buckets gegen Binance verschoben, DST-Shift (auch in 17) | ✔ (Code) |
| P2.45c | Infra | — | Existieren die METALS-Tabellen überhaupt? | ✔ entwarnt (Step 2: XAU/XAG/XAUT/PAXG-Tabellen existieren vollständig) |
| P3.8 | Bot | — | matplotlib-Backend | ✔ ok — 16 ist der **einzige** Bot der Familie, der `Agg` setzt |

## 4. Abhängigkeiten & Querschnitts-Risiken

- **R1 (Forming Candle):** 16 interpretiert den DB-Kerzen-Vertrag falsch (letzte Row = live behandelt) — Teil der Bug-Klasse, die ein gemeinsames `fetch_closed_candles()` schließen würde (08, Cross-Cutting 1).
- **R4 (Leverage vs. SL):** kein Abgleich; gepostete 10–20x gegen 20–30%-SLs sind dieselbe Defekt-Klasse wie P0.5/P0.6 — zentrales `cap_leverage_to_sl()` deckt auch 16 ab.
- Kein Monitor-/DB-Tracking → auch der Monitor-Vorbehalt (Report 17) greift hier nicht: es gibt schlicht **gar keine** Zahlen, weder verzerrte noch ehrliche.

## 5. Sanierungsplan

**Sofort:** Entscheidung treffen — **abschalten** (Empfehlung Report 16: „Abschalt-Kandidat") oder instrumentieren (`ai_signals`-Writes wie die AI-Flotte). Bis dahin keine Kapital-Zuteilung rechtfertigbar.

**Falls behalten (Regel-Fixes, kein Retrain nötig):** `iloc[:-1]` für DB, Partial-Rows/Buckets bei yfinance droppen, Cooldown ≥ Kerzendauer (P1.27); Sa/So-Skip + Freshness-Gate (P2.45a); ATR-/%-Cap + Reject `sl ≥ entry` und Leverage aus SL-Distanz (P2.45b/R4); Resample nach UTC-Konvertierung (LOW).

**Offene Fragen:** Wochenend-Timestamps im CH_SMC_FOREX (08, DB-Frage 8) nie ausgewertet; reale Signalfrequenz/Ergebnis mangels Tracking unbekannt.

## 6. Belege

- `AUDIT_TODO.md` P1.26 (✘), P1.27, P2.45, P3.8
- `audit_reports/08_smc_bots.md` (Abschnitt 16_smc_forex_metals_bot.py + Cross-Cutting)
- `audit_reports/STEP2_DB_VERIFICATION.md` (P1.26 widerlegt; XAU-Tabellen existieren)
- `audit_reports/16_strategy_concept_evaluation.md` (Abschnitt 6, Ranking #22; Querschnittsbefund 6)
