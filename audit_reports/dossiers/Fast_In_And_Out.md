# Dossier: Fast In And Out (FIFO)

> Momentum-Scalper ohne Edge-Hypothese — **Note F** (Report 16) · größter Verlustbringer der gesamten Flotte (Σ **−25.843** Preis-% netto). Kernverdikt: „Nicht rettbar — es gibt keine Selektion, die man durch Bugfixes freilegen könnte. Abschalten" — einzige Alternative: S11-Filter-Modell davor.

## 1. Steckbrief
- **Modul:** `strategies/strat_fast_in_out.py`, Runner `3_detectors.py`, Monitoring `5_trade_monitor.py`.
- **Signal-Logik:** Drei Bedingungen auf 30m — RSI_9 zwischen 55–75, EMA9>EMA21, 5% „Luft" bis zur Resistance — ein TP bei +1,25%. Faktisch die Definition von „gerade steigt es"; trifft in jedem Aufwärtsdrift auf hunderte Coins zu (111.387 Trades).
- **Channel:** eigener Cornix-Trading-Channel via `telegram_outbox` (Whitelist-Rohname „Fast In And Out").
- **Cooldowns:** kein Per-Coin-Cooldown; nur globaler Win-Count-Circuit-Breaker (400/500 Wins in 3–4h über ALLE Coins — praktisch toter Guard, drosselt perverserweise nach Gewinnen, nie nach Verlusten), zusätzlich TZ-fehlerhaft (P2.1: 3h-Fenster deckt in CEST nur 1–2h).

## 2. Live-Bilanz (Report 14, dedupliziert, `closed_trades_master`)
- **n = 111.387** · WR **60,6%** · ø **−0,13%**/Trade · Median **+1,25%** · Σ netto **−25.843** Preis-%.
- Muster: Median positiv, ø negativ → seltene, aber riesige Verlust-Tails; die abs>50%-Ausreißer der Classic-Familie konzentrieren sich hier („Pennies vor der Dampfwalze"). Richtungssplit nicht separat berichtet. Monatstrend nicht separat ausgewiesen.
- **Wichtig:** E6 (Report 15) — ein Loss-Cap bei −3% verbessert den ø nur um 0,02pp → das Problem ist **Selektion, nicht Ausreißer-Tails**.
- **Scoring-Vorbehalt (Report 17):** Monitor-Scoring stimmt bei FIFO nur zu **73%** mit dem First-Touch-Replay überein (Flotte gesamt 63,4%; 17,8% verpasste TP1, 18,8% TP1 trotz SL-zuerst) — per-Trade-Wahrheit eingeschränkt zuverlässig; zusätzlich **212 verworfene Outbox-Messages im FIFO-Trading-Channel** (verlorene Signale/SL-Updates ohne Alarm).

## 3. Befunde
| ID | Schweregrad | Einzeiler | Status |
|---|---|---|---|
| P1.14 | Hoch | SHORT-„Headroom"-Check ist vorzeichenverdrehter No-op (`close > support*0.95` statt `*1.05`) → SHORT ohne Guard | ~ (Code, [DB] offen) |
| P1.15 | Hoch | Ein schlechter Coin killt den ganzen Detector-Prozess (Strategie-Calls unprotected) | ~ |
| P2.1 | Mittel | Cooldown-Circuit-Breaker vergleicht naive Lokalzeit gegen UTC-`posted` → Fenster schrumpft | ~ ([DB]) |
| P2.44 | Mittel | 538 serielle Binance-HTTP-Calls pro Detector-Zyklus (vor jeder Prüfung) | ~ |
| R1/05 | Hoch | Bewertet die noch laufende 30m-Kerze (Forming Candle); Engine stempelt bei :02 UND :32 | ✔ (Step 2) |
| 05 | Kontext | Globaler Win-Cooldown 400/500 quasi tot; kein Per-Coin-Cooldown | ~ |
| 16b | Konzept | Keine Edge-Hypothese; Payoff strukturell negativ | ✔ (Live-Zahlen) |

## 4. Abhängigkeiten & Querschnitts-Risiken
- **R1 Forming Candle** (Step 2 bewiesen): Signale auf ~2-min-alten Partial-Kerzen, bei :32 auf einer 32 min offenen 1h-Kerze.
- **R3 TZ-Mix** (Session-TZ Europe/Bucharest, naive Spalten gemischt UTC/lokal) → P2.1 live-relevant.
- **Monitor-Bugs P1.2/P2.7:** Trailing-SL zieht nie nach; nur jüngste 5m-Kerze geprüft → alle FIFO-KPIs monitor-verzerrt (Replay-Agree nur 73%).
- **Outbox-Verluste (N2):** 212 der 800 still verworfenen Messages betrafen den FIFO-Channel; zudem md5-identische Messages 2–3× binnen 60 min (Detector-Refire, Step 2 P0.1).
- **Stale Whitelist (P0.4/P2.25):** Orchestrator gated „Fast In And Out" auf seit 19.04. eingefrorenen Raw-Namen-Statistiken.

## 5. Sanierungsplan
- **Sofort:** Abschalten (Portfolio-Empfehlung Report 16, Abschnitt 8: „Stoppen: … Fast In And Out"). −25,8k Σ netto bei fehlender Edge-Hypothese rechtfertigt keinen Weiterbetrieb.
- **Strukturell (falls Weiterbetrieb gewünscht):** **S11 „FIFO-Filter-Modell"** (Report 15) — Meta-Klassifier vor dem Posten auf Basis der 111k gelabelten Trades (größter Datensatz im Haus); schon +0,3pp ø-Verbesserung dreht die Strategie von −25,8k auf positiv. Voraussetzung: Monitor-Rewrite (Report 17) für saubere Labels + V1–V3 (R1-Fix, Dedup, First-Touch-Simulator). Dazu Exit-Redesign S13 (TP/SL-Geometrie fee-positiv setzen, sonst abschalten) und P1.14/P2.1-Fixes.

## 6. Belege
- `AUDIT_TODO.md`: P1.14, P1.15, P2.1, P2.44, R1, R3
- `audit_reports/05_classic_strats.md`: Forming Candle, Headroom-No-op, Cooldown-Anatomie (Cross-cutting #2)
- `audit_reports/14_bot_performance_db.md` §C: Zahlenzeile FIFO
- `audit_reports/16_strategy_concept_evaluation.md` §3: Note F, Verdikt
- `audit_reports/15_strategy_proposals.md`: E6, S11, S13
- `audit_reports/17_monitor_replay_and_gaps.md` §1–2: agree 73%, 212 Outbox-Verluste
