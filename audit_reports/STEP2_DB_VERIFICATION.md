# Step 2 — Live-DB-Verifikation (VPS)

**Stand:** 2026-07-03 · **Umgebung:** Live-VPS, PostgreSQL 17 (+TimescaleDB) `cryptodata@localhost` · Fleet war zum Prüfzeitpunkt **gestoppt** (sauberer Watchdog-Shutdown 11:23 lokal).

**Vorab — Code-Stand-Abgleich (Diff Kythera ↔ Live):**
AST-Vergleich aller 75 gemeinsamen `.py`-Dateien: Die Live-Version `PycharmProjects\crypto_trading_bot_v2` ist **identisch mit dem Kythera-Import-Commit `b6735d9`** („live state 2026-07-01"), einziger Unterschied: Live hat die Telegram-Channel-IDs hardcoded, Kythera liest sie aus Env-Vars (Redaction). **Alle Kythera-Commits seit dem Import sind NICHT deployt** (ruff-Fixes, `_apply_keepalive`-Fix, mplfinance-RAM-Leak-Fix, Watchdog-Lifecycle-Fix, Dashboard-Tri-State, `core/process_control.py`, Regression-Guard). Nur live existiert: `99_smc_paper_bot.py` (nicht auditiert).

---

## A. Fundament

| # | Check | Ergebnis |
|---|---|---|
| 1 | `SHOW timezone` | **`Europe/Bucharest` (UTC+3)** → alle TZ-Findings sind live-relevant |
| 2 | Schemas | `trade_cooldowns.last_posted_at` = **timestamptz** (P2.2: die WITH-TZ-Variante hat den Bootstrap gewonnen); `active_trades_master.time/posted` naiv + Preise `REAL` (P3.12); `telegram_outbox` **hat** `image_path` (breite DDL gewann); `ai_signals.current_target_hit` = **INTEGER → P1.5 entschärft** |
| 3 | `max_connections` | **200** (nicht 100) — P1.34 abgemildert, bei 27 Prozessen × maxconn 8 = 216 potenziell trotzdem eng |
| 4 | Forming Candle | **R1 BEWIESEN**, siehe unten |

**R1/P1.11 — Forming-Candle-Beweis (empirisch):**
Letzte gespeicherte `BTCUSDT_1h`-Kerze (02:00 UTC): `V=1618.9, low=61485.3, close=61537.9`. Binance real: `V=3999.4, low=61271, close=61411.8` → eine **~40%-Partial-Kerze liegt als „fertig" in der DB** und wird nie korrigiert (P1.11). Die Tageskerze vom 3.7. steht mit `V=9668` in der DB (real >37.976, Vortage ~236k–264k). `BTCUSDT_1h_indicators` hat eine Zeile **genau auf dieser Partial-Kerze** → Indikatoren auf Forming-Candles bestätigt.

**TZ-Mix direkt bewiesen (R3):** Fleet-Shutdown 11:23 lokal = 08:23 UTC. `ml_predictions_master.created_at` (naiv): max **11:23** → Lokalzeit. `regime_history.ts` (naiv): max **08:20** → UTC. **Zwei naive Spalten, zwei verschiedene Semantiken.** `closed_ai_signals.close_time` max 06:00 → gemischte Writer (P2.4 bestätigt).

---

## B. Neue operative Funde (nicht im Step-1-Katalog)

1. **🔴 Data-Ingestion-Wedge, 6 Stunden unbemerkt (P2.47 live belegt).** Ingestion lief seit 2.7. 16:46 ohne Restart, aber **alle** Symbole enden 05:00–05:25 lokal (02:00–02:25 UTC). Der WS-Stream war ~6h tot, der Watchdog hielt den Prozess für gesund, und die restliche Fleet hat bis 11:23 **auf 6h alten Indikatoren weiter Signale gepostet** (Outbox-Einträge bis 11:23). Genau das im Audit beschriebene „wedged bot bleibt grün"-Szenario. → Hang-Detection/Heartbeat ist Pflicht, nicht Kür. Die entstandene 6h-Lücke muss der 12h-REST-Catch-up beim nächsten Start füllen (prüfen!).
2. **🔴 Whale-Logger seit 18. April tot.** Letzte `whale_data/whale_trades_*.json` = 2026-04-18. Zusätzlich P1.42 bestätigt: die letzten 3 Files enthalten nur **49 von 529** Symbolen.
3. **Whitelist-Doppel-Vokabular** (Detail zu P0.4, siehe C).
4. Müll-Tabellen von kaputtem Symbol-Parsing: `BTCUSD1_*`, `BTCU_*`, `ETHU_*` (Second-Order-Folge von P3.3).

---

## C. Beweise je Finding

### P0 — bestätigt
- **P0.3 Self-Echo ✔:** **109** Rows in `orchestrator_suppressed_signals`, deren `original_outbox_id` auf den **eigenen Regime-Trading-Channel** (-1003963430969) zeigt. 0 davon wurden re-geopent (Cooldown fing sie bisher) — der Loop existiert, das Crash-Fenster bleibt.
- **P0.4 Whitelist-Mismatch ✔ (präzisiert):** `bot_regime_whitelist` enthält **beide** Namensvarianten. Pretty-Namen (`MIS1-8h`, `FastInOut`, `5Percent`, `SR`, `VolIndic`, …): `computed_at` = **heute 08:06** (Analyzer schreibt sie aktuell). Raw-Namen, die der Orchestrator abfragt (`MIS1-8H`, `Fast In And Out`, `5 Percent`, `Support Resistance`, `Volume Indicator`): `computed_at` = **eingefroren 2026-04-19**. → Das Gate „funktioniert" (3.043× `wr_below_overall`-Suppressions), aber **für die MIS-Familie + alle 5 Channel-Fallback-Bots auf 2,5 Monate alten Regime-Statistiken** (P2.25 in geld-relevanter Form). Fix bleibt wie in Step 1: `pretty_name()` im Orchestrator + Stale-Row-Cleanup + `computed_at`-Staleness-Gate.
- **P0.7 ✔:** 5 aktive + 79 geschlossene Trades mit LONG-`target1 <= entry`.
- **P0.9 ✔ (strukturell):** PK der Candle-Tabellen ist `(symbol, open_time)`, Live-Code `6_housekeeping.py:660` nutzt `ON CONFLICT (open_time)` → jeder Gap-Insert wirft, Exception wird verschluckt. Aktuell **0 interne 1h-Lücken über alle 529 Coins/30d** — der 12h-REST-Catch-up der Ingestion trägt das System; das nächtliche Safety-Net existiert trotzdem nicht.
- **P0.11 ✔:** UFI1 realisiert **25,7% WR (n=35)** vs. beworbene 54,2%/+278R.
- **P0.13 ✔✔ (drastisch):** Master-pkl-Dummies: `ai_model_*` matcht von 22 Live-Modellnamen **nur `ATS1`+`EPD1`** (Rest `MSI1-*`-Typos), `conv_bot_*`-Overlap = **0**. Kalibrierung: **corr(confidence, win) = −0.304**; Bucket 0.8–0.9 → 31,1% WR, Bucket **0.9–1.0 → 9,3% WR** (n=19.561). Das Meta-Modell ist bei seiner höchsten Confidence **invers prädiktiv** — es postet fast nur conf>0.85 → AIM1-Channel ist aktiv schädlich. Sofort pausieren/neu trainieren.
- **P0.1 (teilweise):** `sent_after_retry = 0` → der Crash/Retry-Doppel-Send ist bisher **nicht** eingetreten. Aber: identische Messages (md5-gleich) mehrfach binnen 60 min in Trading-Channels (FastInOut, VolumeIndicator, PatternDetector, je 2-3×) → Upstream-Doppel-Generierung (Detector-Refire). Architektur-Risiko bleibt.

### P1/P2 — bestätigt
- **P1.42 ✔:** 49/529 Symbole in Whale-Files (Cap ~200 Streams/Conn) + Logger seit 18.4. tot.
- **P2.12 ✔:** gespeicherter `rsi_14` == `ewm(span=14)`-Variante exakt (Δ=0.000), Abstand zu echtem Wilder-RSI ø **4,84 Punkte**.
- **P2.23/#11 ✔:** Regime-Verteilung 30d: **TRANSITION 44,5%**, HIGH_VOLA 29,7%, CHOP 25,8%; 2,9 Raw-Wechsel/Tag; 17,2% der 2h-Fenster mit ≥2 Regimes → Fallback-Pfad dominiert häufig; 256 Suppressions via `regime_is_transition`-Fallback.
- **P2.27 ✔:** ROM1-SL-Distanz: median 7,9%, **p90=17,9%, max 65,3%**; 20/133 Signale >15% → bei 20x jenseits Liquidation.
- **P2.31 ✔:** `targets_hit` bis **21** (EPD1: 215 Rows mit 20 Targets; ROM1/RUB1 zweistellig) — Monitor scored weit jenseits der publizierten TP1-5.
- **P1.12 ✔ (für Level-Werte):** alte Rows (>30d, n=5000): `poc` nur 149 distinct, `support_price` 236 distinct (broadcastet), `trendline_price` 4997 (per-row ok).
- **P1.40/41 (Größenordnung):** `ml_predictions_master` Shadow-Flut: EPD1 31k + AIM1 25k Rows/7d (~72k/Woche ungeposted). `pump_dump_events` existiert (schmales Schema, `spike_time`).
- **P2.9 (historisch):** aktive Trades sauber (`sl>0` überall); `closed_trades_master` enthält 162.194 Alt-Rows mit `sl<=0/NULL`.

### Widerlegt / entschärft
- **P1.5 ✘:** `current_target_hit` ist INTEGER → kein `int>str`-TypeError möglich.
- **P1.26 ✘:** SMC-FVG-Cooldowns existieren (SMC_1H/2H/4H/1D_FVG = 83 Rows) → FVG-Pfad feuert. Dead-Code-These falsch (oder galt für ältere Codeversion).
- **P1.31/P1.13 ✘ (aktuell):** 0/529 Coins ohne `_1h`/`_1h_indicators`/`_4h_indicators`-Tabellen; 0 `ma_200=0`-Rows (BTC); keine internen 1h-Lücken 30d.
- **P2.45 (Teilaspekt):** XAU/XAG/XAUT/PAXG-Tabellen existieren vollständig.
- **P2.26 (aktuell):** keine gestapelten OPEN-Duplikate auf coin+direction.
- **P2.38 ✔ entwarnt:** ABR1 LONG 67,2% / SHORT 59,2% WR (n=110) — keine Klassen-Inversion, `SUCCESS_CLASS_IDX=0` konsistent (deckt sich mit Commit d19a68d).

---

## D. Strategie-Herz-und-Nieren (Katalog #12–14)

**Realized WR aus `closed_ai_signals`** (win = ≥TP1; ohne 352k LEGACY-Rows, die separat ~49,6% WR zeigen):

| Modell | n | WR | Kalibrierung (conf→win) |
|---|---|---|---|
| MIS1-72H | 11.822 | 63,9% | **negativ** (72%@conf<0.4 → 65%@0.5-0.6) — Schwellen bedeutungslos (stützt P1.17) |
| MIS1-168H | 7.167 | 58,5% | flach |
| BR1H/2H/4H/1D | 12.034 | 57–60% | — |
| EPD1 | 4.392 | 72,8% | flach (aber hohes Grundniveau) |
| **ROM1** | 2.677 | **69,2%** | — |
| QM_1H | 3.139 | 67,5% | leicht positiv |
| AIM1 | 3.125 | 50,3% | **invertiert −0.30** (s. P0.13) |
| TD_1H | 2.202 | 57,2% | **positiv** (78,5%@conf>0.9) ✓ |
| ATS1 | 1.768 | 65,8% | leicht negativ |
| SRA1 | 396 | 69,9% | positiv ✓ |
| MIS1-8H | 569 | 52,9% | positiv (91%@0.7-0.8, kleine n) |
| ABR1 | 110 | 63,6% | — |
| **UFI1** | 35 | **25,7%** | → P0.11 |

**Overall 61,1% — ROM1 69,2%**: Der Orchestrator-KPI (#13) ist **positiv** (+8pp über Fleet-Schnitt), trotz Stale-Whitelist. Achtung Interpretations-Vorbehalte: WR ohne Fees/R-Gewichtung, Regime-Close zensiert fremde Trades als neutral (P1.9), Monitor-Targets ≠ Cornix-Targets (P2.31) — die absoluten Zahlen sind optimistisch verzerrt.

**Kalibrierungs-Fazit (#12):** TD_1H, SRA1, MIS1-8H, QM sind echt kalibriert. MIS1-72H/168H, EPD1, BB flach bis negativ → Forming-Candle/Feature-Skew-Findings (P1.17-25) empirisch gestützt. AIM1 invers → P0.13.

---

## E. Regression-Guard (P2.50) — scharf geschaltet ✔

`extract` gegen die Live-DB (24 Fixtures: BTC/ETH/SOL/DOGE × 30m/1h/2h/4h/1d/1w) + `refresh` (24 Goldens, 111 Spalten) + `verify` grün. Fixtures/Golden/Manifest committen (dieser Commit).
Hinweis: `python-dotenv` wurde in die Live-venv installiert (fehlte; von Kythera-`core/config.py` benötigt).

## F. Offene Step-2-Restpunkte

- 6h-Datenlücke von heute (02:25–08:23 UTC): nach Fleet-Neustart prüfen, ob der REST-Catch-up sie füllt (falls nicht: P0.9-Fix zuerst).
- Watchdog-Doppel-Fleet-Beweis (#8): Log zeigt sauberen Stop heute; historische Doppel-Starts nicht systematisch ausgewertet.
- Fee-adjustierte PnL (#14) und Gap-Census für 5m/30m-TFs: nicht gerechnet.
- `bot_unidentified` = 841 Suppressions (größter Einzel-Reason nach wr_below_overall) — Pattern-Lücken in `identify_bot()` ansehen.
