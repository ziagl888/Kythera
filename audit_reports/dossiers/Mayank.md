# Dossier: Mayank

> Regelbasierter FVG-Bot („FVG fully closed" als Entry). **Note (16): D.** Kernverdikt: konsistenter implementiert als Bot 16 (Closed-Candle-Disziplin), aber das Entry-Konzept ist in der SMC-Lehre selbst ein *entwertetes* Level — Knife-Catch am alten Gap-Boden; komplett unvermessen, als Info-Kanal harmlos, als Strategie unbewertbar.

## 1. Steckbrief

| | |
|---|---|
| Bot | `17_mayank_bot.py` — regelbasiert, kein ML, kein Trainer |
| Signal-Logik | FVG-Retest: „FVG fully closed" triggert Entry; keine Altersgrenze für Gaps |
| Channel | CH_MAYANK |
| Leverage | in den Quellen nicht beziffert; kein R4-Befund gegen 17 |
| Tracking | **keins** — schreibt kein `ai_signals`, taucht in keiner Performance-Statistik auf |

## 2. Live-Bilanz

**Keine.** Report 16 (Ranking #20): n = untracked, WR = ?, Σ = ? — „unmessbar". Einer der drei unvermessenen Bots (16/17/21): kein Tracking, kein valider Backtest. Konsequenz aus Report 16, Querschnittsbefund 6 / Empfehlung 8.5: instrumentieren oder abschalten.

## 3. Befunde

| ID | Ebene | Schweregrad | Einzeiler | Status |
|---|---|---|---|---|
| P2.45a | Bot | MEDIUM | Static-Data-Refire nach Cooldown-Ablauf (Wochenenden) — wie Bot 16: dasselbe alte Signal re-postet, sobald der Cooldown abläuft | ✔ (Code) |
| P2.45b | Bot | MEDIUM | Kein FVG-Altersllimit — monatealte Gaps erzeugen „Retest"-Signale; Oldest-First-Break (Bot 21 cappt MAX_FVG_AGE=48) | ✔ (Code) |
| 08-LOW | Bot | LOW | Drei separate Pool-Connections pro Signal | ✔ (Code) |
| 08-LOW | Bot | LOW | 2h/4h-Resample in Exchange-Lokalzeit vor UTC-Konvertierung (geteilt mit 16) | ✔ (Code) |
| 16-Konzept | Konzept | — | „FVG fully closed" als Entry konzeptionell wackelig: ein vollständig gefülltes Gap gilt in der SMC-Lehre als entwertet | ✔ (Konzept-Review) |
| P3.8 | Bot | LOW | matplotlib ohne `Agg`-Backend → headless-Crash-Risiko (17/24/25 betroffen) | ✔ (Code) |

Positiv (08, Cross-Cutting): 17 wird als „konsistenter implementiert (Closed-Candle)" eingestuft — der R1-Repaint-Befund der Nachbarn 16/24/25 trifft ihn so nicht.

## 4. Abhängigkeiten & Querschnitts-Risiken

- **R1:** DB-Kerzen-Vertrag wird flottenweit inkonsistent interpretiert; 17 gehört zu den saubereren Konsumenten, profitiert aber ebenfalls von einem gemeinsamen `fetch_closed_candles()`.
- **R4:** kein spezifischer Befund, aber die Familie hat nirgends einen Leverage-vs-SL-Abgleich — zentrales `cap_leverage_to_sl()` sollte auch 17 einbinden.
- Kein Tracking → keine Monitor-Verzerrung (Report 17), aber auch keinerlei Evidenz; Whitelist/Analyzer kennen den Bot nicht.

## 5. Sanierungsplan

**Sofort:** Entscheidung instrumentieren vs. abschalten (Report 16: unvermessene Strategien haben keinen Ertragsanspruch). Als reiner Info-Kanal ohne Cornix-Ausführung wäre er laut Report 16 „harmlos".

**Regel-Fixes (falls behalten):** MAX_FVG_AGE=48-Fenster von Bot 21 übernehmen, Newest-First statt Oldest-First (P2.45b); Freshness-Gate bzw. Trigger-Candle-Timestamp in den Cooldown-Key (P2.45a); eine Pool-Connection pro Signal; `Agg`-Backend (P3.8); Resample nach UTC.

**Offene Fragen:** Wochenend-Timestamps im CH_MAYANK (08, DB-Frage 8) nie ausgewertet; reale Signalqualität mangels Tracking unbekannt.

## 6. Belege

- `AUDIT_TODO.md` P2.45, P3.8
- `audit_reports/08_smc_bots.md` (Abschnitt 17_mayank_bot.py + Cross-Cutting)
- `audit_reports/16_strategy_concept_evaluation.md` (Abschnitt 6, Ranking #20; Querschnittsbefund 6, Empfehlung 8.5)
