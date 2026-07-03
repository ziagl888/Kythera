# Dossier: BTC SMC (100x)

> Regelbasierte BTC-SMC-Strategie mit dem handwerklich besten Setup-Design der SMC-Familie — und einem mathematisch defekten 100x-Hebel-Design. **Note (16): D (F as-is).** Kernverdikt: **P0.5** — bei 100x isoliert liegt die Liquidation bei ~−0,9%, *vor* jedem 0,4–1,2%-SL; jeder Stop = −100% Margin. Der Bot ist im Ist-Zustand ein Liquidations-Generator; mit Hebel-Fix + ehrlichem Walk-Forward der prüfenswerteste regelbasierte SMC-Bot.

## 1. Steckbrief

| | |
|---|---|
| Bot | `21_btc_smc_strategy.py` — regelbasiert, kein ML, kein Trainer |
| Markt/TF | BTC; 1h-Signale (Doppelsignal-Fenster „1h auseinander", P2.46) |
| Leverage | **`DESIRED_LEVERAGE = 100x`** (21:31-35) bei 0,4–1,2%-SL (21:199, 238) — **P0** |
| Setup-Qualität | positiv hervorgehoben (Report 16): FVG-Age-Caps (MAX_FVG_AGE=48), Trendfilter, R:R-Check, SL-Validierung; einziger Bot der Familie, der die letzte (Forming-)Kerze korrekt droppt (`iloc[:-1]`) |
| Parameter-Herkunft | In-Sample-Grid-Search (nie out-of-sample validiert) |
| Tracking | **keins** — schreibt kein `ai_signals`, taucht in keiner Performance-Statistik auf |

## 2. Live-Bilanz

**Keine.** Report 16 (Ranking #21): untracked — einer der drei unvermessenen Bots (16/17/21), kein valider Backtest. Die einzige „Bilanz" ist rechnerisch: bei 100x isoliert wird jede Position bei ~−0,9% liquidiert, bevor der SL (0,4–1,2%) greift; selbst der 0,4%-Floor bedeutet −40% Margin pro Stop. Der R:R-Check des Bots ignoriert den Hebel.

## 3. Befunde

| ID | Ebene | Schweregrad | Einzeiler | Status |
|---|---|---|---|---|
| **P0.5** | Bot | **P0** | 100x Leverage mit 0,4–1,2% SL → Liquidation ~−0,9% *vor* dem SL; jeder Stop = −100% Margin; R:R-Check ignoriert Leverage | ✔ (Code, `21:31-35,199,238`) |
| P2.46 | Bot | MEDIUM | Kein Cooldown/Dedupe im ganzen File: unconditional `iloc[:-1]` + DB-Write-Lag → dieselbe Trigger-Kerze signalisiert zweimal, 1h auseinander | ✔ (Code, `21:121-123,264`) |
| 16-Konzept | Konzept | MEDIUM | Parameter aus In-Sample-Grid-Search; nie ehrlicher Walk-Forward | ✔ (Konzept-Review) |
| 16-Meta | Prozess | MEDIUM | Komplett unvermessen (kein `ai_signals`, keine Performance-Statistik) | ✔ |
| 08-Positiv | Bot | — | Validiert SL-Seite (im Gegensatz zu 16), droppt letzte Kerze korrekt, Age-Caps/Trendfilter/R:R-Check vorhanden — „bestes Setup-Design der SMC-Familie" | ✔ |

## 4. Abhängigkeiten & Querschnitts-Risiken

- **R4 (Leverage vs. SL, Kern-Root-Cause):** 21 ist neben UFI1 (P0.6) der Namensgeber des R4-Befunds „Leverage-vs-SL nirgends abgeglichen". Fix-Klasse: zentrales `core/trade_utils.py: cap_leverage_to_sl(sl_pct)` (z.B. `lev ≤ 0.5/sl_pct`), von allen signal-emittierenden Bots genutzt — schließt P0.5, P0.6 und die ROM1-SL-Distanzen (P2.27) in einem Zug (Report 16, Empfehlung 8.4).
- **R1:** nicht betroffen — 21 ist die Referenz-Implementierung für den Closed-Candle-Umgang in der Familie.
- Kein Tracking → Monitor-Vorbehalt (Report 17) gegenstandslos; aber auch keine Evidenz für irgendeine Edge.

## 5. Sanierungsplan

**Sofort (P0, vor allem anderen):** Leverage cappen — `lev ≤ 0.5/sl_pct` bzw. `DESIRED_LEVERAGE ≤ 25` (Fix aus P0.5/R4). Solange das nicht deployt ist, darf der Bot kein Kapital anfassen.

**Regel-Fixes:** Standard-`check_cooldown/update_cooldown` oder Dedupe auf Trigger-`open_time` (P2.46); Instrumentierung (`ai_signals`) nachrüsten.

**Validierung statt Retrain:** ehrlicher Walk-Forward (V3-Simulator aus Report 15) statt In-Sample-Grid-Search — laut Report 16 ist 21 *nach* Hebel-Fix „der prüfenswerteste regelbasierte SMC-Bot".

**Offene Fragen:** historische Doppelsignale ~1h auseinander im Channel (08, DB-Frage 8) nie ausgewertet; reale Signalfrequenz unbekannt.

## 6. Belege

- `AUDIT_TODO.md` P0.5, P2.46, R4
- `audit_reports/08_smc_bots.md` (Abschnitt 21_btc_smc_strategy.py + Cross-Cutting 1/5)
- `audit_reports/16_strategy_concept_evaluation.md` (Abschnitt 6, Ranking #21; Querschnittsbefunde 5+6; Empfehlung 8.4/8.5)
