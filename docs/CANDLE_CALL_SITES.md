# Kerzen-Zugriffe: API-Vertrag, Call-Site-Inventar, Migrations-Reihenfolge

**Stand:** 2026-07-09 (Commit 1b140a5) · **Task:** T-2026-CU-9050-034 (C1-Vorbereitung) · **Übergeordnet:** T-2026-CU-9050-018, `docs/TIMESCALE_R1_MIGRATION.md`

Arbeitsgrundlage für **Phase 0/1** der R1-+-TimescaleDB-Migration: die neue Zugriffs-API `core/candles.py`, das vollständige Inventar der Stellen, die heute per-Coin-Kerzen- oder Indikator-Tabellen anfassen, der R1-Blast-Radius und die Umverdrahtungs-Reihenfolge.

> **Nichts davon ist ausgeführt.** Dieser Task legt nur API, Inventar und `tools/candles_parity.py` an. Kein Call-Site umverdrahtet, kein Dual-Write, kein Backfill, kein Cutover, keine Schema-Änderung. Die offenen Operator-Fragen (§5) sind vor dem ersten Umverdrahtungs-Commit zu beantworten.

---

## 1. Die API (`core/candles.py`)

```python
read_candles(conn, symbol, tf, *, limit, start, end, include_forming=False, columns=CANDLE_COLUMNS)
read_indicators(conn, symbol, tf, *, limit, start, end, include_forming=False, columns=None)
read_candles_with_indicators(conn, symbol, tf, *, limit, start, end, include_forming=False, ...)   # LEFT JOIN
latest_open_time(conn, symbol, tf, *, include_forming=True)
upsert_candles(conn, symbol, tf, rows, *, closed)          # Caller committet
upsert_indicators(conn, df, symbol, tf)                     # Caller committet
table_exists(conn, table) · indicator_column_names(conn, symbol, tf)
period_start(tf, now) · last_closed_open_time(tf, now) · timeframe_delta(tf)
set_symbol_whitelist(...) · load_symbol_whitelist(path='coins.json')
```

Vier Verträge, alle tragend:

1. **Reads liefern immer ASC** nach `open_time`. `iloc[-1]` ist überall die *neueste* Kerze. Heute mischen sich ASC- und DESC-Frames (14 Call-Sites lesen `DESC LIMIT n` und drehen anschließend selbst um) — genau die Falle 1 aus `docs/OPUS-HANDOFF.md`.
2. **`include_forming=False` ist Default.** Preis-Checks (Monitore 5/8, `get_live_price`-Fallbacks, Orchestrator-Last-Close, Health-Monitor-Kanarie, Live-Parity-Replik) übergeben explizit `True`. Analytische Leser nicht — das ist R1.
3. **Writes committen nicht.** Der Caller besitzt die Transaktion (harte Regel 8, wie `core/signal_post.py`). Wer `insert_fast()` / `write_indicators_to_db_optimized()` ersetzt, muss ein `conn.commit()` ergänzen — beide committen heute selbst.
4. **Identifier-Hygiene (P3.3).** `symbol`/`tf` validiert (`^[A-Z0-9]{2,24}$`, TF-Whitelist), gequotet über `psycopg2.sql.Identifier`, optional harte `coins.json`-Whitelist. Die in P3.3 geforderte Validierung in `load_coins` bleibt separat offen.

### Der `is_closed`-Ersatz in Phase A

Das Ziel-Schema trägt `is_closed boolean` aus dem Binance-Kline-Flag `k['x']`. Die Alt-Tabellen haben die Spalte nicht. Phase A leitet „geschlossen" aus der Uhr ab:

> Eine Kerze ist geschlossen ⇔ `open_time < period_start(tf, now())`.

Der Cutoff wird **DB-seitig aus `now()`** gerechnet (eine Uhr — die des Writers) und ist zeitzonen-unabhängig: reine Epoch-Arithmetik, für `1w` auf Montag verankert (Epoch 0 ist ein Donnerstag, Binance-Wochenkerzen öffnen Montag 00:00 UTC). `date_trunc()` wäre falsch — es hängt an der Session-`TimeZone` und würde je nach Bot-Prozess anders schneiden (TZ-Minenfeld R3).

Schwäche gegenüber dem echten Flag: eine Kerze, deren Periode gerade abgelaufen ist, kann für Millisekunden noch die Werte des letzten Pre-Close-Ticks tragen. `KYTHERA_CANDLES_CLOSE_GRACE_SEC` verschiebt den Cutoff zurück. Default 0 → Operator-Frage 5.4.

### Was die API bewusst NICHT tut

- **Kein DDL.** `CREATE TABLE`/`CREATE INDEX` bleiben in `1`/`2`/`6`; sie entfallen ersatzlos in Phase C.
- **Kein `KYTHERA_CANDLES_SOURCE=hyper`.** Der Env-Schalter existiert und wirft `CandleSourceError`. Der Hypertable-Pfad ist Phase 4 und wird nicht spekulativ vorgebaut.
- **Kein Commit, kein Retry, kein Pool-Handling.**

---

## 2. Call-Site-Inventar

**≈108 verifizierte Call-Sites in 50 Live-Dateien** (`legacy_trainers/`: 23 Dateien, toter Code, eine Aggregat-Zeile — wird nicht umverdrahtet, sondern beim Cleanup mit den Tabellen entsorgt).

Legende **Forming heute**: `offen` = die neueste Zeile kann die laufende Kerze sein und wird verwendet · `gedroppt` = die Datei entfernt sie selbst · `gebunden` = die Query ist auf einen geschlossenen Zeitstempel begrenzt · `gewollt` = die forming Candle ist der Zweck.
**Ziel**: `F` = `include_forming=False` · `T` = `include_forming=True`.

### Block A — Ingestion, Engine, Housekeeping (DB-Writer, VPS-only)

| Stelle | Funktion | Art | TF | Ordering | Forming heute | Ziel | Commit heute |
|---|---|---|---|---|---|---|---|
| `1_data_ingestion.py:83` | `create_table_if_needed` | DDL | arg | – | – | bleibt inline | ja |
| `1_data_ingestion.py:99,102` | `get_latest_open_time` | `to_regclass` + `MAX(open_time)` | arg | Aggregat | sieht forming | `latest_open_time(include_forming=True)` | – |
| `1_data_ingestion.py:177` | `insert_fast` (REST-Catch-up) | write-candles, `execute_values`, `IS DISTINCT FROM` | arg | – | schreibt Historie + ggf. forming Endzeile | `upsert_candles(closed=…)`, **zwei Calls** (Historie/forming) | **ja** |
| `1_data_ingestion.py:437` | `_flush_to_db` (WS-Flush) | write-candles, **SAVEPOINT pro Zeile** | Buffer | – | schreibt live forming | `upsert_candles(closed=k['x'])` | ja |
| `2_indicator_engine.py:173,180` | `create_indicator_table` | DDL + Index | arg | – | – | bleibt inline | ja |
| `2_indicator_engine.py:553` | `process_coin_task` | `MAX(open_time)` **auf Indikator-Tabelle** | arg | Aggregat | – | **API-Gap** | – |
| `2_indicator_engine.py:574` | `process_coin_task` | read-candles `SELECT *` | arg | ASC, kein LIMIT | **offen — Indikatoren werden über der forming Kerze gerechnet** (bricht harte Regel 5) | **F** | – |
| `2_indicator_engine.py:513` | `write_indicators_to_db_optimized` | write-indicators | arg | – | schreibt Indikator-Zeile der forming Kerze | `upsert_indicators()` | **ja** |
| `6_housekeeping.py:61` | Bootstrap | DDL | 8 TF | – | – | bleibt inline | ja |
| `6_housekeeping.py:259` | `_fetch_last_close_or_entry` | read-candles | 5m | DESC LIMIT 1 | gewollt | **T** | – |
| `6_housekeeping.py:440` | Delisted-Scan | `information_schema` | – | – | – | **API-Gap** | – |
| `6_housekeeping.py:461` | Retention | **DELETE** | 5m–4h | – | – | **API-Gap** | ja |
| `6_housekeeping.py:647` | Gap-Scan | read-candles (`open_time`) | var | ASC | offen | F | – |
| `6_housekeeping.py:720` | Gap-Filler | write-candles, `ON CONFLICT DO NOTHING` | var | – | nur geschlossene Lücken | `upsert_candles(closed=True)` | ja |
| `6_housekeeping.py:747` | Indikator-Invalidierung | **DELETE** | var | – | – | **API-Gap** | ja |

### Block B — Monitore, Orchestrator, Preis-Fallbacks (`include_forming=True`)

| Stelle | Funktion | Art | TF | Ordering | Forming heute | Ziel |
|---|---|---|---|---|---|---|
| `5_trade_monitor.py:194,199` | SL/TP-Scoring | read-candles | 5m | DESC LIMIT 1 / ASC-Fenster | **gewollt** (Wick-Scoring, `:264-270`) | **T** |
| `8_ai_trade_monitor.py:123,128` | AI-SL/TP-Scoring | read-candles | 5m | DESC LIMIT 1 / ASC-Fenster | **gewollt** (`:202-210`) | **T** |
| `28_signal_orchestrator.py:352,787` | `_get_latest_price`, `_get_close_price` | read-candles | 5m | DESC LIMIT 1 | gewollt | **T** |
| `3_detectors.py:45` | `get_live_price` | read-candles | 5m | DESC LIMIT 1 | gewollt | **T** |
| `29_ufi1_bot.py:96` | `get_live_price` | read-candles | 1h | DESC LIMIT 1 | gewollt | **T** |
| `core/health_monitor.py:70` | DATA_STALE-Kanarie | `EXTRACT(EPOCH FROM NOW()-max(open_time))` auf `BTCUSDT_5m` | 5m | Aggregat | **gewollt** | **T** — ohne forming droht false-positive DATA_STALE → Fleet-Restart |
| `tools/audit/live_parity.py:81` | Live-Serving-Replik von Bot 11 | read-joined | 1h | DESC LIMIT 100 → ASC | **gewollt** (`:105-116`) | **T** — sonst bricht die Parität |

### Block C — AI-/Strategie-Bots

| Stelle | Bot | Art | TF | Ordering | Forming heute | Ziel |
|---|---|---|---|---|---|---|
| `9_ai_sr_bot.py:61` | SR | read-indicators `SELECT *` | 1h | `open_time<=%s` DESC LIMIT 1 | gebunden (vergangener Trade-TS) | F (`end=`) |
| `10_pump_dump_detector.py:175` | Pump/Dump | read-indicators | 1h | DESC LIMIT 1 | offen | F |
| `11_ai_mis_bot.py:178` | MIS | read-joined | 1h | DESC LIMIT 100 → ASC | **gewollt, gesplittet**: Features `iloc[-2:-1]`, Live-Preis `iloc[-1]` (FIX P1.17, `:227-233`) | **T** + Index-Rework |
| `12_ai_ats_bot.py:127` | ATS | read-joined | 1h | DESC LIMIT 500 → ASC | **gewollt**: `current_idx=-2`, `prev_idx=-3` (`:148-151`) | **T** + Index-Rework |
| `13_ai_rub_bot.py:110,126` | RUB | read-candles + read-indicators | 1h | `< date_trunc('hour',NOW())` | **gedroppt** (P1.19) | F |
| `14_ai_atb_bot.py:280,285,290` | ATB (geparkt) | Chart-Reads | 1h | ASC | offen (Chart) | T (Darstellung) |
| `14_ai_atb_bot.py:618` | ATB | read-candles | 1h | ASC + `.tail(4)` | **gewollt**: `last_close=iloc[-1]` triggert Break/Bounce | T |
| `15_ai_master_bot.py:224` | Master/AIM | read-joined | 1h | `< floor(ts)` DESC LIMIT 1 | **gedroppt** (`:218-238`) | F (`end=`) |
| `16_smc_forex_metals_bot.py:66` | SMC Metals | read-candles | var | DESC LIMIT 300 → ASC | **gedroppt beim Caller** (`:334`, P1.27) | F — `:334` entfernen |
| `18_ai_abr1_bot.py:308,583` | ABR1 | read-candles | 1h | ASC | `:583` **gedroppt** (`:595`), `:308` (Selftest) offen | F — `:595` wird redundant |
| `21_btc_smc_strategy.py:110` | BTC-SMC | read-candles | var | DESC LIMIT 500 → ASC | **gedroppt** (`:126`) | F — `:126` entfernen |
| `22_ip_pattern_bot.py:196` | IP-Pattern | read-candles | var | DESC LIMIT n → ASC | offen: `current_price=iloc[-1]` (`:210`) | F |
| `24_quasimodo_bot.py:90` | Quasimodo | read-joined | var | DESC LIMIT 100 → ASC | Pivots gedroppt (`:115`), Preis `closes[-1]` offen | F — `:115` entfernen |
| `25_smc_ml_sniper.py:208` | Sniper | read-joined | var | DESC LIMIT 150 → ASC | **kein Drop.** `argrelextrema` + `current_price` auf der forming Kerze | F — **stiller Repaint, höchstes Risiko** |
| `29_ufi1_bot.py:72` | UFI1 (geparkt) | read-candles | 1d | ASC | offen | F |
| `7_pattern_detector.py:272` | Pattern | read-candles | 1h–1d | DESC LIMIT 168 → ASC | **gedroppt**: `iloc[:-4]` (`:282`), `len(df)-2` (`:310`) | F + Offset-Rework |
| `17_mayank_bot.py` | Mayank | **keine DB-Kerzen** (yfinance) | – | – | – | — |
| `99_smc_paper_bot.py:60` | Paper (nicht live) | read-candles | var | – | gedroppt | F |

### Block D — Geteilte Helfer und Strategien (höchster Fan-in)

| Stelle | Funktion | Art | TF | Forming heute | Ziel |
|---|---|---|---|---|---|
| `core/trade_utils.py:304` | `calculate_smart_targets` | read-candles, DESC LIMIT 1000 → ASC | 1h | **offen** — forming speist Swing/HVN/FVG-Level | F |
| `core/trade_utils.py:423` | `get_hvn_and_sr_levels` | read-candles, ASC 95d | 1h | **offen** — forming speist S/R + Fibs | F |
| `core/market_utils.py:187` | `calculate_obv` | read-candles | 1h | gebunden (Caller-Endstempel) | F |
| `core/charting.py:138` | Mini-Chart | read-candles | 5m | offen (kosmetisch) | F |
| `core/regime_logic.py:81,136` | BTC-Regime, Alt-Context (Literale `BTCUSDT_15m`, `BTCDOMUSDT_15m`) | read-candles | 15m | **offen** — forming 15m steuert die Regime-Klassifikation → Orchestrator-Gating | F, Backfill-Pfad braucht `end=` |
| `core/research_features.py:312` | `fetch_context_frame` | read-joined | 1h | **gedroppt** (`searchsorted…-1`, `:339`) | F |
| `strategies/strat_main_channel.py:52` | Signal | read-candles, `<=%s` DESC 480 → ASC | 1h | gebunden | F (`end=`) |
| `strategies/strat_support_resistance.py:40` | Signal | read-candles, `<=%s` DESC 480 → ASC | 1h | gebunden | F (`end=`) |
| `strategies/strat_volume_indicator.py:18,39,45` | Signal | read-candles | 30m | gebunden (strikte `<`-Grenzen) | F |
| `3_detectors.py:202` | `run_detectors_for_timeframe` | read-indicators `SELECT *`, **DESC LIMIT 480** (DESC-Frame geht an die Strategien!) | 30m/1h | offen | F |

`core/aim2_features.py`, `core/mis_features.py`, `core/rub_features.py`, `core/funding_features.py` haben **keinen direkten DB-Zugriff** — sie rechnen auf übergebenen Frames. Ihre SQL-Fragment-Konstanten (`MIS_SQL_INDICATOR_SELECT`, `CONTEXT_SQL_SELECT`) werden bei den Callern ausgeführt und sind dort inventarisiert.

`strategies/strat_5_percent.py`, `strategies/strat_fast_in_out.py`, `handlers/open_handler.py`, `dashboard.py` fassen keine Kerzen-Tabellen an. **`chart_data_service.py` ebenfalls nicht** — es bedient den WS-Ringbuffer und verwirft die forming 1m-Kerze bei `:250`. Das Design-Doc T-018 §2 listet es fälschlich als Call-Site; **aus dem Migrations-Backlog streichen.**

### Block E — Trainer, Backtests, Dataset-Builder, Audit-Tools (offline)

| Stelle | Art | TF | Forming heute | Ziel |
|---|---|---|---|---|
| `tools/walkforward_sim.py:174,204` | read-candles / read-joined | 1d/1h/4h | **offen — forming als geschlossen behandelt = echter Look-ahead im Walk-Forward** | F (hoher Wert) |
| `tools/walkforward_sim.py:635,759` | read-joined (MIS1/RUB) | 1h | gedroppt (`date_trunc`) | F |
| `tools/aim2_build_dataset.py:275` · `epd2_build_dataset.py:113` · `research_dataset_common.py:74` | read-joined | 1h | Event-Floor `searchsorted-1` | F (geringes Delta) |
| `tools/retrain_sra2.py:172` | read-indicators | 1h | Python-Floor-Maske | F |
| `tools/mis1_move_labels.py:65` | read-candles | 1h | gedroppt (`date_trunc`) | F |
| `tools/regime_rules_study.py:63` | read-candles | 15m | offen (mild) | F |
| `tools/regression_guard/rgcore.py:130` | read-candles `SELECT *`, DESC LIMIT 600 → ASC | 30m–1w | offen (forming kann im Golden eingefroren sein) | F |
| `tools/audit/step2_analysis.py:148,158,190` · `step2_part2.py:17,41,73` · `step7_monitor_replay.py:23,92` | Aggregate, `information_schema`, `generate_series`-Gap-Census | 1h/5m | – | **API-Gap**, bleiben roh |
| `tools/audit/step7_monitor_replay.py:32` | read-candles | 5m | historisch | F |
| `qm_ml_trainer.py:86` · `smc_ml_trainer.py:87` · `smc_pattern_backtester.py:51` · `qm_backtest.py:57` | read-joined / read-candles | 1h/4h | offen | F |
| `fib_backtest.py:87,97` | `pg_tables`-Case-Variante + read-candles | 1d | offen | F, **Gap** (probiert `{symbol.lower()}_1d`) |
| `backtest/smc_btc_backtest{,_v2,_v3}.py` · `trainers_x/BT2-Datagrepper-for-ML.py:47` | read-candles | var | offen | F |

Delegierende Builder ohne eigenes SQL (gleiches Profil wie `research_dataset_common:74`): `tools/fif1_build_dataset.py:151`, `fmr1_build_dataset.py:151`, `pex1_build_dataset.py:158`, `trm1_build_dataset.py:127`, `mis2_dump_geometry_study.py`.

`guard.py verify|refresh|smoke` läuft **DB-frei** auf `.npz`-Fixtures; nur `extract` fasst die DB an. Das Phase-1-Gate ist damit fixture-basiert und auf der Build-Maschine lauffähig.

### API-Gaps (gegen die **implementierte** API, nicht die Skizze)

Die Skizze aus T-018 §2 hatte fünf Funktionen; die gebaute API schließt deren größte Lücken bereits (JOIN, `start`/`end`, `limit=None`, `columns=None`, `indicator_column_names`, `table_exists`). Es bleiben:

| Gap | Stellen | Vorschlag |
|---|---|---|
| **Aggregat-SQL** (`SUM`/`MAX`/`MIN`/`CASE` + korrelierter Subselect, `count(DISTINCT)`, `generate_series`-Gap-Census) | `23_market_tracker:100,309,321,372`; `step2_analysis:148,190`; `step2_part2:17,73`; `step7_monitor_replay:92` | Market-Tracker ist live-hot → `window_volume()`/`window_range()` ergänzen **oder** in pandas umschreiben (30m × 7d ≈ 336 Zeilen/Coin). Audit-Tools bleiben roh. |
| **Ältester Satz im Fenster** (`ORDER BY ASC LIMIT 1`) | `23_market_tracker:132` | Die API liefert immer die *neuesten* N → `read_candles(..., first=True)` nötig |
| **`DELETE` nach Alter / ab `open_time`** | `6_housekeeping:461,747` | `delete_candles_before()` / `delete_indicators_from()` |
| **`MAX(open_time)` auf der Indikator-Tabelle** | `2_indicator_engine:553` | `latest_open_time(..., kind='indicators')` |
| **Tabellen-Enumeration** | `6_housekeeping:440`; 3 Audit-Tools; `fib_backtest:87` | `list_coin_tables(conn, tf=None)`; `fib_backtest` braucht zusätzlich Case-Auflösung |
| **DDL** | `1:83`, `2:173,180`, `6:61` | Bewusst außerhalb der API. Entfällt in Phase C |
| **Gemischter Ingestion-Batch** | `1_data_ingestion:177` | `closed=` ist ein Bool pro Call; der REST-Catch-up mischt geschlossene Historie mit einer forming Endzeile → **zwei** Upsert-Calls. Kein fehlendes Feature, eine Verdrahtungs-Frage |

**Zwei Aufräum-Funde außerhalb des Auftrags** (nicht stillschweigend weggelassen): `db_schema_analysis.py` existierte doppelt (Repo-Root + `tools/`); `legacy_trainers/` (23 Dateien) ist toter Code mit eigenen Roh-Tabellen-Reads und einem eigenen `get_live_price`. Beides ist löschbar, beides ist für die Migration nicht nötig.

> **Korrektur 2026-07-10 (T-2026-CU-9050-039).** Die hier ursprünglich behauptete **Byte-Identität der beiden `db_schema_analysis.py` war falsch.** Die Root-Kopie wurde in `052ba4c` (ruff cleanup) modernisiert, die `tools/`-Kopie stammt unverändert aus dem Initial-Import; zudem zeigte deren `sys.path.insert(0, dirname(__file__))` auf `tools/`, wo kein `core/` liegt — sie konnte `core.database` nie importieren. `audit_reports/10_dashboard_tools.md:47` und `AUDIT_TODO.md` P3.1 hatten das bereits korrekt vermerkt. Die stale `tools/`-Kopie ist gelöscht, die Root-Kopie ist kanonisch (die Exclude-Einträge in `pyproject.toml` und `.github/workflows/typecheck.yml` zeigen ohnehin auf sie).

---

## 3. R1-Blast-Radius

**Echte Verhaltensänderung, und genau dafür ist die Migration da:**

- **`25_smc_ml_sniper:208`** — kein Drop, `argrelextrema`-Pivots *und* `current_price` auf der forming Kerze. **Stiller Repaint, höchstes Einzelrisiko.**
- **`2_indicator_engine:574`** — Indikatoren werden fleet-weit über der forming Kerze berechnet. Bricht heute harte Regel 5.
- **`core/trade_utils:304,423`** — höchster Fan-in: die forming Kerze speist den Level-Pool (Swing/HVN/FVG/S-R/Fib) *aller* Bots.
- **`core/regime_logic:81,136`** — die forming 15m-Kerze steuert die Regime-Klassifikation und damit das Orchestrator-Gating.
- **`tools/walkforward_sim:174,204`** — forming als geschlossen behandelt: **Look-ahead im Walk-Forward-Simulator**, also in genau dem Werkzeug, das die Labels des Retrain-Programms erzeugt.
- `22_ip_pattern:196`, `29_ufi1:72`, `14_ai_atb:618`, `23_market_tracker` (%-Change, Volatilität, Volumen-/Range-Aggregate), `core/charting:138` (kosmetisch), `regime_rules_study:63` und `step2_part2:25` (mild).

**Index-gekoppelt — Flip nur zusammen mit Offset-Rework**, sonst wird eine *geschlossene* Kerze zu viel gedroppt: `7_pattern_detector` (`iloc[:-4]`, `len(df)-2`), `11_ai_mis` (`iloc[-2:-1]` / `iloc[-1]`), `12_ai_ats` (`-2`/`-3`), `24_quasimodo` (`[:-1]` + `closes[-1]`), `16_smc_forex_metals` (`:334`), `21_btc_smc` (`:126`), `18_ai_abr1` (`:595`).

Bei 11 und 12 ist die forming Kerze **Teil des Vertrags** (Feature-Zeile = vorletzte, Live-Preis = letzte). Sie bleiben auf `include_forming=True` und bekommen die Trennung sauber, statt sie über negative Indizes zu erraten.

**Muss `include_forming=True` bleiben** — hier würde `False` Geld kosten oder die Fleet neu starten: Monitore `5`/`8`, Orchestrator `28`, `get_live_price` in `3`/`29`, `6_housekeeping:259`, **`core/health_monitor:70`** (sonst false-positive `DATA_STALE`), `tools/audit/live_parity:81` (Parität zur Live-Serving-Semantik).

**Schon forming-sicher, kein Delta:** `9_ai_sr`, `10_pump_dump`, `13_ai_rub` (P1.19), `15_ai_master`, alle drei `strategies/*`, `core/market_utils`, `core/research_features`, `walkforward_sim:635,759`, `mis1_move_labels`, `retrain_sra2`, die `step2`-Aggregate.

**Regression-Guard:** `rgcore` friert `SELECT * … DESC LIMIT 600` ein. Wenn die Goldens mit forming Candle entstanden sind, wird der Guard beim Umstellen rot. **Das ist ein echtes Signal, kein Refresh-Anlass** (harte Regel 9).

---

## 4. Migrations-Reihenfolge

Sechs Blöcke, jeder ein eigener Commit, Regression-Guard davor und danach. Blöcke 1–5 sind reine Code-Umverdrahtung (read-only, von der Build-Maschine aus machbar); Block 6 fasst die DB an und ist **VPS-only** (harte Regel 1).

| # | Block | Dateien | Warum hier | DB-Write |
|---|---|---|---|---|
| 1 | Offline-Tooling | Trainer, Backtests, `*_build_dataset`, `walkforward_sim`, `retrain_sra2`, `rgcore`, Audit-Replays, `core/charting` | Kein Live-Signal-Pfad, sofort rückrollbar. Fördert die fehlenden API-Formen (Aggregate, `first=True`) früh zutage. `walkforward_sim` zuerst — dort sitzt der Look-ahead, der das Retrain-Programm verunreinigt | nein |
| 2 | Strategien + `3_detectors` + geteilte Helfer | `strat_*`, `3_detectors`, `core/trade_utils`, `core/market_utils` | Die Strategien sind schon zeitstempel-gebunden (kleines Delta); die Helfer entblocken die AI-Bots | nein |
| 3 | **Monitore + Orchestrator explizit auf `True`** | `5`, `8`, `28`, `3.get_live_price`, `29:96`, `6:259`, `core/health_monitor` | **Vor** dem ersten `False` im Geld-Pfad: das `True` sichtbar und reviewbar machen. Ein Monitor, der still auf geschlossene Kerzen kippt, scored SL/TP bis zu 5 Minuten zu spät | nein |
| 4 | AI-Bots, **ein Bot pro Commit** | `9,10,13,14,15,18,22,24,25,29` (F) und `11,12` (T + Index-Rework) | R1 wird hier wirksam. Signal-Raten im 24-h-Vergleich dokumentieren. `25` zuerst — dort ist der Repaint | nein |
| 5 | Geteilte Feature-Builder **plus Trainer/Replay im selben Commit** | `core/research_features`, `core/regime_logic` + zugehörige Trainer | Harte Regel 7: Trainer == Serving == Replay. Getrennt umstellen = stille Feature-Drift in Live-Modellen | nein |
| 6 | `2_indicator_engine` (Reads + Writes), `1_data_ingestion`, `6_housekeeping` | Engine-Read `:574`, Upserts, Gap-Filler, DELETE/DDL-Gaps | Höchste R1-Wirkung (Indikatoren über forming Kerze) und die Caller-Commit-Umstellung. Ab hier trägt das Datenmodell das echte `is_closed` | **ja — VPS, C-Gate** |

Danach erst die Phasen 2–5 aus `docs/TIMESCALE_R1_MIGRATION.md` (Dual-Write, Backfill, Paritäts-Beobachtung, Read-Cutover, Cleanup).

---

## 5. Offene Operator-Fragen (Michi)

Diese Fragen blockieren den Start von Phase 1. Keine davon ist in diesem Task entschieden worden.

1. **Retention** (T-018 §5.1): Historie unbegrenzt (komprimiert ~4–6 GB) oder Fenster? Empfehlung des Design-Docs: unbegrenzt.
2. **`REAL` → `double precision`** für die ~120 Indikator-Spalten (P3.12)? Empfehlung: ja, im Zuge des Schema-Neubaus. Konsequenz hier: `tools/candles_parity.py` kanonisiert Floats auf 12 signifikante Stellen, damit der Typwechsel nicht jede Zeile als Drift meldet.
3. **1d/1w weiter per WS** oder nur REST/Catch-up (spart ~1.300 Streams)? Empfehlung: nur REST für 1d/1w.
4. **Close-Grace-Period.** Default `KYTHERA_CANDLES_CLOSE_GRACE_SEC=0`: eine Kerze gilt in der Millisekunde als geschlossen, in der ihre Periode abläuft. Alternative 2–5 s gegen den Pre-Close-Tick-Race. `0` ist ehrlicher, `>0` konservativer. **Vor dem ersten `include_forming=False`-Bot zu entscheiden.**
5. **`11_ai_mis` / `12_ai_ats`:** beide brauchen die forming Kerze als Live-Preis und die vorletzte als Feature-Zeile. Bleiben sie auf `include_forming=True` mit expliziter Trennung (mein Vorschlag), oder sollen sie zwei Calls machen (`read_candles(include_forming=False)` für Features + `latest_price()` für den Preis)? Zweiteres ist sauberer, kostet aber eine zweite Query pro Coin und Zyklus.
6. **Signal-Raten.** R1 **senkt** sie — das ist der Zweck. Klassik-Strategien feuern seltener, MIS/RUB/ATB-Feature-Verteilungen verschieben sich. **Schwellen erst nach dem Retrain neu tunen** (Report 16), nicht während der Umverdrahtung.
7. **Owner + Branch-Modell.** T-018 §4 verlangt „Migration als EIN Branch mit klarem Owner". Bei parallelen Sessions am selben Repo ist das eine Vorbedingung, keine Empfehlung.
8. **Aufräum-Freigabe** (Nebenfunde): ~~`tools/db_schema_analysis.py` als Duplikat löschen?~~ — **entschieden und erledigt** (T-2026-CU-9050-039, 2026-07-10: gelöscht, Root ist kanonisch). Offen bleibt: `legacy_trainers/` löschen?

---

## 6. Verifikation dieses Pakets

| Artefakt | Verifikation | Status |
|---|---|---|
| `core/candles.py` | `ruff check` + `ruff format --check` + `mypy` (= CI) | grün |
| `core/candles.py` | `backtest/test_candles.py` — 29 DB-freie Tests: Cutoff-Arithmetik (inkl. Montags-Anker und TZ-Unabhängigkeit), Identifier-Hygiene, TF-Sync gegen `core.config`, Argument-Validierung, Phase-4-Seam | grün |
| `tools/candles_parity.py` | `python tools/candles_parity.py --self-check` (DB-frei); ohne Credentials sauberer Exit 2 | grün |
| Regression-Guard | `python tools/regression_guard/guard.py smoke` | grün (unberührt) |
| `tools/candles_parity.py` gegen beide Tabellen | DB nötig | **offen — VPS, ab Phase 2** |
| Phase-0-Gate aus T-018: „API-Reads byte-gleich zu Direkt-SQL" | DB nötig | **offen — VPS** |

Die Build-Maschine hat keine DB-Credentials; jede DB-gebundene Verifikation gehört in eine VPS-Session (T-2026-CU-9050-011).
