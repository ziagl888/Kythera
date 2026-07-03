# Dossier: 5 Percent

> Schein-Konfluenz aus ~26 redundanten Filtern — **Note D** (Report 16) · Paradebeispiel „Win ≠ Profit": **71,1% WR und trotzdem Σ −5.766** netto. Kernverdikt: ohne Redesign von Entry-Timing und Exits keine positive Erwartung begründbar; allenfalls LONG-Seite als Experiment.

## 1. Steckbrief
- **Modul:** `strategies/strat_5_percent.py`, Runner `3_detectors.py`, Monitoring `5_trade_monitor.py`.
- **Signal-Logik:** ~26 AND-Bedingungen (RSI-Band, TSI, komplettes EMA/WMA/KAMA-Alignment, MACD, Donchian/Boll-Mid). Die Konfluenz ist Schein: fast alle Bedingungen sind Glättungen desselben Close-Preises und kollabieren auf „etablierter, steiler Trend" → systematisch später Einstieg in ausgereizte Bewegungen; fixe %-Targets, kein Zeit-Exit.
- **Channel:** eigener Cornix-Trading-Channel via `telegram_outbox` (Whitelist-Rohname „5 Percent").
- **Cooldowns:** kein Per-Coin-Cooldown; nur globaler Win-Count-Circuit-Breaker (500 Wins über alle Coins — quasi tot, drosselt nach Gewinnen statt Verlusten), TZ-fehlerhaft (P2.1).

## 2. Live-Bilanz (Report 14, dedupliziert, `closed_trades_master`)
- **n = 19.385** · WR **71,1%** · ø **−0,20%**/Trade · Median **−0,05%** · Σ netto **−5.766** Preis-%.
- Höchste „Win-Rate" der Classic-Familie und klar negativ — TP1-Touch zählt als Win, danach gibt Trailing/SL alles zurück, Fees fressen den Rest.
- **Richtungssplit:** LONG-Seite 76% WR bei n=1.087 — prüfenswert, aber n zu klein für Vertrauen (Report 14 D.5). Monatstrend nicht separat ausgewiesen.
- **Scoring-Vorbehalt (Report 17):** Monitor-Scoring stimmt bei 5 Percent nur zu **45%** mit dem First-Touch-Replay überein — das per-Trade-Scoring ist hier **de facto Rauschen**; alle obigen Zahlen (inkl. der 71% WR und des LONG-Splits) sind entsprechend unzuverlässig, bis der Monitor-Rewrite + Re-Score gelaufen ist.

## 3. Befunde
| ID | Schweregrad | Einzeiler | Status |
|---|---|---|---|
| P1.14 | Hoch | SHORT-„Headroom"-Check ist vorzeichenverdrehter No-op (`close > support*0.95`) → SHORT ohne Guard | ~ (Code, [DB] offen) |
| P2.43 | Mittel | SHORT nutzt `ema_12 < ema_55` wo LONG `ema_21 > ema_55` (wahrscheinlich Typo); `REQUIRED_COLUMNS` deckt `ema_200/wma_21/wma_26` nicht ab (latenter Silent-Never-Fire) | ~ |
| P2.1 | Mittel | Cooldown-Circuit-Breaker vergleicht naive Lokalzeit gegen UTC-`posted` | ~ ([DB]) |
| P1.15 | Hoch | Ein schlechter Coin killt den ganzen Detector-Prozess | ~ |
| P2.44 | Mittel | 538 serielle Binance-HTTP-Calls pro Detector-Zyklus | ~ |
| R1/05 | Hoch | Bewertet die noch laufende Kerze; Engine stempelt :02 UND :32 | ✔ (Step 2) |
| 16b | Konzept | Schein-Konfluenz, später Einstieg, fixe Targets, kein Regime-Bewusstsein | ✔ (Live-Zahlen) |

## 4. Abhängigkeiten & Querschnitts-Risiken
- **R1 Forming Candle** (Step 2 bewiesen): 26 Bedingungen werden auf Partial-Kerzen ausgewertet.
- **R3 TZ-Mix:** Session-TZ Europe/Bucharest → P2.1 live-relevant (3h-Fenster real 1–2h).
- **Monitor-Bugs P1.2/P2.7:** Trailing-SL zieht nie nach, nur jüngste 5m-Kerze geprüft — bei 5 Percent mit nur 45% Replay-Übereinstimmung die gravierendste Konsequenz: die Strategie ist aktuell nicht seriös bewertbar.
- **Outbox-Verluste (N2):** 800 Messages still verworfen (kein 5-Percent-spezifischer Wert berichtet); Whitelist-Rohname „5 Percent" seit 19.04. eingefroren (P0.4/P2.25) → Orchestrator-Gating auf 2,5 Monate alten Statistiken.

## 5. Sanierungsplan
- **Sofort:** P1.14-Fix (`close > support*1.05`) + P2.43-Typo/`REQUIRED_COLUMNS`-Fix + P2.1-TZ-Fix; P1.15-per-Coin-try/except im Detector. SHORT-Seite bis zur Neubewertung schließen bzw. Strategie parken.
- **Strukturell:** Erst Monitor-Rewrite (Report 17) + Re-Score, dann Neubewertung — vorher ist jede Entscheidung auf 45%-Rausch-Labels gebaut. Danach: **S1 Direction-Gate** (nur LONG-Seite als Experiment weiterlaufen lassen, Report 16 §8: „5 Percent nur als Experiment auf der LONG-Seite weiter"), Exit-Redesign nach S13 (TP/SL-Geometrie fee-positiv), sonst abschalten. Das S11-Filter-Muster ist prinzipiell übertragbar, FIFO und Volume Indicator haben aber Vorrang (mehr Daten).

## 6. Belege
- `AUDIT_TODO.md`: P1.14, P1.15, P2.1, P2.43, P2.44, R1, R3
- `audit_reports/05_classic_strats.md`: Headroom-No-op, EMA-Typo, REQUIRED_COLUMNS, Cooldown-Anatomie
- `audit_reports/14_bot_performance_db.md` §C + D.5: Zahlenzeile, LONG-Split n=1.087
- `audit_reports/16_strategy_concept_evaluation.md` §3: Note D, Verdikt
- `audit_reports/15_strategy_proposals.md`: S1 Direction-Gates, S13
- `audit_reports/17_monitor_replay_and_gaps.md` §1: agree 45%
