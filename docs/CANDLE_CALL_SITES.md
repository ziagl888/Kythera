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
4. **Identifier-Hygiene (P3.3).** `symbol`/`tf` validiert (`^[A-Z0-9]{2,24}$`, TF-Whitelist), gequotet über `psycopg2.sql.Identifier`, optional harte `coins.json`-Whitelist. Die in P3.3 zusätzlich geforderte Validierung in `load_coins` ist inzwischen erledigt (T-2026-CU-9050-096, 2026-07-11: zentrale `re.fullmatch(r'[A-Z0-9]+')`-Prüfung in `core.market_utils.load_coins`, alle sechs Caller laufen darüber; Test `backtest/test_symbol_validation.py`).

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

**≈108 verifizierte Call-Sites in 50 Live-Dateien** (`legacy_trainers/`: 23 Dateien mit Roh-Tabellen-Reads, eine Aggregat-Zeile — **wird nicht umverdrahtet**, weil kein Prozess sie ausführt; die Skripte sind eingefrorene Provenienz, siehe §2-Nebenfunde. Wenn die per-Coin-Tabellen in Phase C wegfallen, laufen sie ohnehin nie wieder — das ist kein Grund, sie zu löschen).

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
| `25_smc_ml_sniper.py:208` | Sniper | read-joined | var | DESC LIMIT 150 → ASC | Pivots **gedroppt** (`:239`, T-2026-CU-9050-036), Preis `closes[-1]` offen | F — `:239` entfernen |
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
| `tools/walkforward_sim.py:174,204` | read-candles / read-joined | 1d/1h/4h | **umverdrahtet** (T-2026-CU-9050-037): beide Loader gehen über `core.candles` mit `include_forming=False` | ✅ F |
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

**Zwei Aufräum-Funde außerhalb des Auftrags** (nicht stillschweigend weggelassen): `db_schema_analysis.py` existierte doppelt (Repo-Root + `tools/`); `legacy_trainers/` (23 Dateien) trägt eigene Roh-Tabellen-Reads und einen eigenen `get_live_price`. Für die Migration ist beides nicht nötig.

> **Korrektur 2026-07-10 (T-2026-CU-9050-039).** Der Absatz darüber stand ursprünglich so da: *„`db_schema_analysis.py` und `tools/db_schema_analysis.py` sind **byte-identische Duplikate**; `legacy_trainers/` (23 Dateien) ist **toter Code** […]. **Beides ist löschbar.**"* Beide Aussagen halten der Prüfung am Code nicht stand.
>
> **`db_schema_analysis.py` war nicht byte-identisch.** Die Root-Kopie wurde in `052ba4c` (ruff cleanup) modernisiert, die `tools/`-Kopie stammt unverändert aus dem Initial-Import; zudem zeigte deren `sys.path.insert(0, dirname(__file__))` auf `tools/`, wo kein `core/` liegt — sie konnte `core.database` nie importieren. `audit_reports/10_dashboard_tools.md:47` und `AUDIT_TODO.md` P3.1 hatten das bereits korrekt vermerkt. Die stale `tools/`-Kopie ist gelöscht, die Root-Kopie ist kanonisch (die Exclude-Einträge in `pyproject.toml` und `.github/workflows/typecheck.yml` zeigen ohnehin auf sie).
>
> **`legacy_trainers/` ist nicht „toter Code" im Sinne von löschbar.** Kein laufender Prozess importiert die Skripte, und sie sind bewusst nicht lauffähig (Credentials durch `os.getenv(...)`-Platzhalter ersetzt) — aber sie sind die **einzige Reproduktionsgrundlage der acht live geladenen Modell-Artefakte**. `legacy_trainers/README.md` ordnet jeden Trainer seinem Artefakt und Bot zu (MIS1→11, ABR1→18, ATS1→12, RUB1→13, SRA1→9, AIM1→15, EPD1→10, ATB1→14); der Ordner entstand genau dafür (`7b5ec89 feat: preserve the _X ML trainers as frozen provenance`). Ihre dokumentierten Defekte (Label-Geometrie, Split-Leakage, In-Sample-Thresholds, Feature-Skews) sind absichtlich konserviert — sie erklären das Verhalten der Live-Modelle und sind die Referenz, gegen die das Retrain-Programm seine Deltas misst. **Bleibt. Siehe Operator-Frage §5.8.**

---

## 3. R1-Blast-Radius

**Echte Verhaltensänderung, und genau dafür ist die Migration da:**

- ~~**`25_smc_ml_sniper:208`** — kein Drop, `argrelextrema`-Pivots *und* `current_price` auf der forming Kerze. **Stiller Repaint, höchstes Einzelrisiko.**~~ **Pivot-Seite erledigt** (2026-07-10, T-2026-CU-9050-036, P1.46): `argrelextrema` läuft auf `highs[:-1]/lows[:-1]`, der intra-candle Repaint ist weg. `current_price = closes[-1]` bleibt bewusst live (CMP-Entry + BB-Level-Nähe) — die Preis-Seite kippt erst mit Block 4, nach Operator-Frage 4/6.
- **`2_indicator_engine:574`** — Indikatoren werden fleet-weit über der forming Kerze berechnet. Bricht heute harte Regel 5.
- **`core/trade_utils:304,423`** — höchster Fan-in: die forming Kerze speist den Level-Pool (Swing/HVN/FVG/S-R/Fib) *aller* Bots.
- **`core/regime_logic:81,136`** — die forming 15m-Kerze steuert die Regime-Klassifikation und damit das Orchestrator-Gating.
- ~~**`tools/walkforward_sim:174,204`** — forming als geschlossen behandelt: **Look-ahead im Walk-Forward-Simulator**, also in genau dem Werkzeug, das die Labels des Retrain-Programms erzeugt.~~ **Gefixt 2026-07-10 (T-2026-CU-9050-037)** als erster Schritt von Block 1: beide Loader lesen über `core.candles` (`include_forming=False`), Invariante mechanisch geprüft in `backtest/test_feature_lookahead.py`. Offen bleibt die Frage an den Operator, ob bereits ausgerollte Modelle auf den alten Labels trainiert wurden.
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
| 4 | AI-Bots, **ein Bot pro Commit** | `9,10,13,14,15,18,22,24,25,29` (F) und `11,12` (T + Index-Rework) | R1 wird hier wirksam. Signal-Raten im 24-h-Vergleich dokumentieren. Der Pivot-Repaint in `25` ist vorgezogen erledigt (T-2026-CU-9050-036); offen bleibt dort nur die Preis-Seite | nein |
| 5 | Geteilte Feature-Builder **plus Trainer/Replay im selben Commit** | `core/research_features`, `core/regime_logic` + zugehörige Trainer | Harte Regel 7: Trainer == Serving == Replay. Getrennt umstellen = stille Feature-Drift in Live-Modellen | nein |
| 6 | `2_indicator_engine` (Reads + Writes), `1_data_ingestion`, `6_housekeeping` | Engine-Read `:574`, Upserts, Gap-Filler, DELETE/DDL-Gaps | Höchste R1-Wirkung (Indikatoren über forming Kerze) und die Caller-Commit-Umstellung. Ab hier trägt das Datenmodell das echte `is_closed` | **ja — VPS, C-Gate** |

Danach erst die Phasen 2–5 aus `docs/TIMESCALE_R1_MIGRATION.md` (Dual-Write, Backfill, Paritäts-Beobachtung, Read-Cutover, Cleanup).

### Stand Block 1 — erledigt (T-2026-CU-9050-107, 2026-07-13)

Block 1 (Offline-Tooling) ist umverdrahtet. 12 Read-Sites gehen jetzt über `core.candles` mit `include_forming=False`; gegen die Live-VPS-DB read-only verifiziert (ASC-Frames, forming Kerze ausgeschlossen: `newest open_time < period_start`), Regression-Guard `smoke`+`verify` grün, ruff/format grün auf den nicht-exkludierten Root-Dateien.

- **Umverdrahtet:** `core/charting.py`, `tools/mis1_move_labels.py` (+ transitiv `mis2_dump_geometry_study`), `tools/regime_rules_study.py`, `tools/retrain_sra2.py`, `tools/research_dataset_common.py` (+ transitiv fif1/fmr1/pex1/trm1), `tools/aim2_build_dataset.py`, `tools/epd2_build_dataset.py`, `qm_ml_trainer.py`, `smc_ml_trainer.py`, `qm_backtest.py`, `smc_pattern_backtester.py`, `backtest/smc_btc_backtest{,_v2,_v3}.py`, `tools/regression_guard/rgcore.py`. `tools/walkforward_sim.py` war der erste Schritt (T-2026-CU-9050-037).
- **Neuer Helfer `candles_window_start(since, lookback_days)`** in `research_dataset_common` reproduziert das frühere `%s::timestamptz - INTERVAL 'N days'` in Python (Lokalisierung nach LOCAL_TZ, dann Tage abziehen). Ein Ort für die TZ-sensible Fenstergrenze; aim2/epd2 importieren ihn.
- **Bewusst NICHT umverdrahtet (dokumentiert, nicht stillschweigend):**
  - `fib_backtest.py` — der `pg_tables`-Case-Variant-Probe (`{symbol.lower()}_1d`) kollidiert mit der Uppercase-Validierung der API (`^[A-Z0-9]{2,24}$`). Eigener API-Gap (§2, Case-Auflösung), keine reine Umverdrahtung → bleibt roh bis der Gap geschlossen ist.
  - `tools/audit/step7_monitor_replay.py` — flaches TZ-Forensik-Wegwerf-Skript; der `AT TIME ZONE 'UTC' AS ot`-Read ist bewusst TZ-agnostisch (±4h-Fenster, Shift-0/3-Erkennung). Historisches Fenster → forming irrelevant, CI-exkludiert, null Verhaltensnutzen bei echtem Risiko für die filigrane Shift-Logik.
  - `trainers_x/BT2-Datagrepper-for-ML.py` — eingefrorene Provenienz (eigene hardcoded `DB_CONFIG`, hyphenierter nicht-importierbarer Dateiname, importiert `core` nicht), gleiche Klasse wie `legacy_trainers` (§2, §5.8).

### Stand Block 2 — erledigt (T-2026-CU-9050-108, 2026-07-13)

Block 2 (Strategien + `3_detectors` + geteilte Helfer) ist umverdrahtet. Sieben Read-Sites im **Live-Signal-Pfad** lesen jetzt über `core.candles` mit `include_forming=False`. Reiner read-only Code-Umbau; kein DB-Schema. **Live-Verhaltensänderung → nicht autonom gemergt, Freigabe Michi vor dem Enqueue.**

- **Umverdrahtet:** `core/trade_utils.calculate_smart_targets:304` (1000h-Level-Pool, DESC-then-reverse entfällt → API-ASC), `core/trade_utils.get_hvn_and_sr_levels:423` (95d S/R, `start=utc_now()-95d`), `core/market_utils.calculate_obv:231` (`start=`/`end=`, beide inklusiv, `.set_index('open_time')`), `strategies/strat_main_channel:61` + `strat_support_resistance:50` (`end=open_time_hit`, `sort_values` entfällt), `strategies/strat_volume_indicator` (3 Reads, 30m), `3_detectors.run_detectors_for_timeframe:167` (Indikator-Frame der 5 Classic-Strats).
- **DESC→ASC-Falle (Kern-Review-Punkt).** `3_detectors` reicht einen DESC-Frame an fünf Konsumenten, die alle `iloc[0]`=neueste indexieren — auditiert: `strat_main_channel`, `strat_support_resistance`, `strat_5_percent`, `strat_fast_in_out` (alle `data.iloc[0]`), `strat_volume_indicator` (`df_indexed.iloc[0]`); `strat_fast_in_out` trägt sogar den expliziten Kommentar „iloc[-1] war die ÄLTESTE Kerze (df ist DESC)". Gewählte Lösung: Read über die API (ASC + forming-frei), dann `.iloc[::-1].reset_index(drop=True)` → exakt der bisherige DESC-Frame, **null Konsumenten-Reindex**. Einzige Verhaltensänderung: `iloc[0]` = neueste GESCHLOSSENE statt forming Kerze (= R1).
- **Strikte `<`-Grenzen byte-treu.** Der Volume-Indikator hat zwei strikte `open_time < grenze`-Reads (HVN-Baseline, Spike-Hist). Die API-`end` ist inklusiv → `end = grenze − timeframe_delta("30m")` reproduziert `< grenze` exakt (period-alignte open_times: `<= grenze−30m` ⟺ `< grenze`). Der dritte Read (`<= open_time_hit`) mappt direkt auf `end=`.
- **Fan-in.** `calculate_smart_targets`/`get_hvn_and_sr_levels` sind die höchste-Fan-in-Stellen (Live-Caller 7/9/10/11/12/13/14/15/18/25/34 + `open_handler` + Research 30–32); sie liefern die **Geometrie** (SL/TP/Entry-Level), nicht das Signal-Gate — `include_forming=False` verschiebt also die geposteten Level-**Werte**, nicht die Signal-**Rate**. Die Rate ändert der Detector-Read (5 Classic-Strats). Offline-Caller (`walkforward_sim`, `*_build_dataset`) übergeben `df=` → kein DB-Read, unberührt. Orchestrator `28:495` übergibt `df=` → unberührt.
- **Verifikation (VPS, read-only, 150 Coins).** Mechanik 149/149: ASC, forming ausgeschlossen (`newest open_time < period_start`), Detector-Re-Flip = DESC mit `iloc[0]` neueste geschlossene, geschlossener Frame byte-gleich zum Alt-Query. Live-Signal-Raten-A/B **nicht messbar** (Fleet-Ingestion stand ~2,4 h → keine forming Kerze; historische forming-Snapshots beim Close überschrieben). Tip-Kerzen-Sensitivität als Proxy: 5%/Fast-Gates 0/298, S/R-Hit-Vorbedingung 25/149 (~17 %), Level-Pools 69–83 % der Coins (Ø ~4,6 % Shift). Guard `smoke`+`verify` grün, ruff/format/mypy grün auf `core/`+`3_detectors.py`.
- **Bewusst NICHT in Block 2:** `3_detectors:45 get_live_price` (Block 3, Ziel `True`); die AI-Bot-Direktreader (Block 4); Grace-Period/MIS-ATS-Forming (§5.4/5.5) gaten Block 4/6, nicht diesen Block.

### Stand Block 3 — erledigt (T-2026-CU-9050-109, 2026-07-13)

Block 3 (Monitore + Orchestrator + Preis-Fallbacks) ist umverdrahtet. Die sieben verbleibenden Preis-/Scoring-Reader im Geld-Pfad lesen jetzt über `core.candles` mit **explizitem `include_forming=True`**. Reiner read-only Code-Umbau; kein DB-Schema. **Verhaltens-erhaltend** (siehe unten) — trotzdem Geld-Pfad → nicht autonom gemergt, Freigabe Michi vor dem Enqueue.

- **Umverdrahtet:** `5_trade_monitor:194,199` + `8_ai_trade_monitor:123,128` (SL/TP-Scoring, 5m — erster Lauf `limit=1`, sonst `start=Wasserzeichen`), `28_signal_orchestrator._get_latest_price` + `._get_last_close_price`, `3_detectors.get_live_price`-DB-Fallback (`:63`), `29_ufi1_bot.get_live_price` (`:96`, 1h, geparkt), `6_housekeeping._fetch_last_close_or_entry` (`:270`), `core/health_monitor._check_data_staleness` (`:70` → `latest_open_time(include_forming=True)`).
- **Verhaltens-erhaltend, nicht wie Block 2.** `include_forming=True` fügt keinen Forming-Filter hinzu → die gelesenen Zeilen sind byte-gleich zu den bisherigen `ORDER BY open_time DESC LIMIT 1` / `WHERE open_time >= %s`-Queries (die API wrappt nur in ein `SELECT * FROM (… DESC) s ORDER BY open_time ASC`, gleiche Zeilenmenge). Keine Signal-Raten-Änderung. Das ist der Zweck von Block 3: das `True` sichtbar und reviewbar machen, **bevor** Block 4 das erste `False` in den Geld-Pfad bringt.
- **Inventar-Drift korrigiert.** Das Inventar (§2 Block B) notierte die Orchestrator-Sites als `28:352,787`; real lagen sie bei `:449` (`_get_latest_price`) und `:1063` (`_get_last_close_price`). `:1063` war **gar nicht inventarisiert** — beide sind jetzt erfasst und umverdrahtet.
- **Monitore 5/8 — Struktur erhalten.** Die Loops bauen `coin_candles[coin]` als list-of-dicts mit `float()`-Casts + tz-Normalisierung aus rohen Tupeln. Die API liefert einen ASC-DataFrame; via `rows = list(df.itertuples(index=False, name=None))` bleibt die restliche Loop-Logik (`rows[-1][0]`, `float(r[1..3])`, `>=`-Wasserzeichen-Filter downstream) **unverändert**. Der bisher äußere `with c.cursor() as cur:` entfällt (kein Direkt-SQL mehr), der Loop-Body rückt eine Ebene aus. `open_time` ist danach ein `pd.Timestamp` (tz-aware, Subklasse von `datetime`) statt eines `datetime` — für alle Vergleiche/Arithmetik/`.tzinfo`-Checks ein Drop-in; die Werte sind sekunden-aligned, also keine ns-Präzisionsdifferenz.
- **SAVEPOINT-Reads (28:1063, 6:270).** Der SAVEPOINT/`ROLLBACK TO SAVEPOINT`-Rahmen bleibt exakt erhalten; nur das innere `SELECT` wird durch `read_candles` ersetzt. `read_candles` öffnet einen **zweiten** Cursor auf derselben Connection (zulässig) und wirft bei fehlender Tabelle in dieselbe `except`, die den SAVEPOINT zurückrollt — Semantik identisch.
- **health_monitor — Alter-Clock-Quelle.** Der `EXTRACT(EPOCH FROM NOW()-max(open_time))`-Aggregat-Read wird zu `latest_open_time(…, include_forming=True)` + `age = (datetime.now(utc) - latest).total_seconds()`. Das Alter kommt jetzt aus der Prozess-Wall-Clock statt aus DB-`NOW()`; beide teilen auf dem VPS dieselbe Systemuhr (Sub-Sekunden-Delta gegen das Minuten-`STALE_LIMIT_S` irrelevant). Nebeneffekt-Härtung: `latest_open_time` prüft `table_exists` und liefert `None` statt zu werfen, falls `BTCUSDT_5m` je fehlte — der Watchdog crasht dort nicht mehr.
- **Verifikation (Build-Maschine, DB-frei — Fleet-Python 3.13.12).** `py_compile` + Import-Smoke aller 7 Dateien; `ruff check` + `ruff format --check` + `mypy` grün auf `core/` + den berührten Root-Bots; Regression-Guard `smoke` (6 Fixtures froze+verified, Perturbation gefangen) + `verify` (24/24 Goldens) grün. Live-A/B ist per Konstruktion ein No-op (byte-gleiche Reads), daher nicht separat gemessen.
- **Bewusst NICHT in Block 3:** die AI-Bot-Direktreader mit Ziel `False` (Block 4), die `11`/`12`-Index-Reworks (Block 4, §5.5), der Engine-Read/Writer-Umbau (Block 6/C-Gate). Grace-Period §5.4 gatet Block 4/6, nicht diesen Block.

### Stand Block 4 — Tranche 1 erledigt (T-2026-CU-9050-111, 2026-07-13)

Block 4 (AI-Bot-Direktreader) wird nach **Michis Leitprinzip** umgesetzt (§5): **Erkennung auf geschlossenen Kerzen (`include_forming=False`)**, **Live-Preis nur zur Generierung** (via `get_live_price`, nach erkanntem Signal). Wegen des Money-Path-Risikos in **zwei Tranchen** geschnitten. **Tranche 1** deckt die Bots ohne Offset-Rework und ohne Live-CMP-Umbau ab — sechs Direktreader lesen jetzt über `core.candles` mit `include_forming=False`.

- **Umverdrahtet (Tranche 1):**
  - `13_ai_rub_bot` — beide Reads (90d-Trend + Indikator-Kerze) über `read_candles`/`read_indicators`; **No-op**, der bisherige `open_time < date_trunc('hour', NOW())`-Filter (P1.19) ist für 1h identisch zum zentralen Closed-Cutoff.
  - `15_ai_master_bot.load_market_row` — `read_candles_with_indicators`, As-of-Read der letzten geschlossenen Kerze vor `floor(ts)`; **No-op**, `end = floor_utc − timeframe_delta("1h")` reproduziert den strikten `< floor_utc`-Bound byte-genau (stunden-aligned).
  - `9_ai_sr_bot.get_indicators_at_time` — `read_indicators(end=trade_ts, include_forming=False)`; tightening am Rand: feuerte ein Trade mitten in der laufenden Stunde, lieferte `<= ts` sonst die Partial-Indikatoren dieser Stunde.
  - `10_pump_dump_detector.get_indicators_at_time` — `read_indicators(limit=1, include_forming=False)`; **echte R1-Änderung**: der bisherige `DESC LIMIT 1` ohne Bound las die forming Indikatorzeile.
  - `18_ai_abr1_bot` — Selbsttest-Sample + Live-Read über `read_candles`; für 1h ist `include_forming=False` exakt der bisherige `open_time < current_hour_utc`-Schnitt, `limit=LIVE_DATA_HISTORY_HOURS` ersetzt das `.tail()` (der +5h-Overfetch entfällt). `retest_idx = len(df)−1` bleibt die jüngste geschlossene Kerze.
  - `29_ufi1_bot.load_daily_ohlcv` — `read_candles(include_forming=False)`; **echte R1-Änderung** (der bisherige Read ohne Obergrenze zog die forming 1d-Kerze mit). 29 holt den Live-Preis bereits separat via `get_live_price` (Block 3) — genau das Muster.
- **Dict-Reader-Muster:** 9/10/13(ind) bauten bisher `dict(zip(cur.description, row))`; jetzt `df.iloc[-1].to_dict()` (9: inkl. `SELECT *`-Spalten wie bisher; 10/13: `open_time` nur fürs Ordering, danach `.drop("open_time")`).
- **Bewusst in Tranche 2 (T-…-folge):** die Bots mit **Offset-Rework** (`7_pattern_detector` `len−2→len−1`/`:-4→:-3`; `12_ai_ats` `−2/−3 → −1/−2`) und die mit **Live-CMP-Deferral** (`22_ip_pattern`, `24_quasimodo`, `25_smc_ml_sniper`: Pivots/Struktur auf geschlossen, `current_price` für Entry/Targets aus `get_live_price` statt `closes[-1]`) plus `11_ai_mis` (geschlossene Features + `get_live_price`-Entry + Alias-Reproduktion `tsi_fast`/`macd_dif`/`macd_dea`). `14_ai_atb` bleibt ausgeschlossen (geparkt → ATB2-Track T-106).
- **Verifikation (Build-Maschine, DB-frei — Fleet-Python 3.13.12):** `py_compile` aller 6 Dateien; `ruff check` + `ruff format --check` + `mypy` grün; Regression-Guard `smoke` (6 Fixtures) + `verify` (24/24) grün. Die **echten R1-Änderungen (10, 29)** senken bewusst die Signal-Raten — der 24-h-A/B ist eine Post-Merge-VPS-Beobachtung; Schwellen erst nach Retrain tunen (§5, Frage 6).

### Stand Block 4 — Tranche 2 Teilmenge (Offset-Rework 12 + 7) erledigt (T-2026-CU-9050-111, 2026-07-13)

Die zwei **Offset-Rework-Bots** ohne Live-CMP-Deferral sind umverdrahtet — die restlichen vier (`22`/`24`/`25`/`11`) folgen in einem fokussierten Schritt.

- **`12_ai_ats`** — `read_candles_with_indicators(include_forming=False, limit=500)`, DESC-Umkehr entfällt. Die TSI-Crossover-Detektion lief schon auf `iloc[-2]` (geschlossen) → mit ausgeschlossener forming Kerze ist die jüngste geschlossene `iloc[-1]`, also `current_idx −2→−1`, `prev_idx −3→−2` (**dieselbe** Detektions-Kerze). Entry-Preis bleibt aus der geschlossenen Kerze (Operator-Ausnahme). Transitional: der 500er-OBV-Baseline-Start verschiebt sich um genau eine Kerze — bis zum ATS-Retrain vernachlässigbar (§5 q6).
- **`7_pattern_detector`** — `read_candles(include_forming=False, limit=168)`, DESC-Umkehr entfällt. Die Breakout-Kerze lief schon auf `len(df)−2` (geschlossen) → jetzt `len(df)−1`. Der `iloc[:-4]`-Pivot-Confirm-Puffer bleibt unverändert (Index `len−4` ist durch `rolling(9,center)` ohnehin NaN-geflaggt); der Rand-Pivot verliert nur seinen bisherigen Forming-Repaint (korrekte R1-Wirkung).
- **Verifikation (DB-frei, Fleet-Python 3.13.12):** `py_compile` + `ruff check`/`format` + `mypy` grün auf beiden Dateien.
- **Offen (Tranche 2 Rest, Folge-Task):** `22_ip_pattern`/`24_quasimodo`/`25_smc_ml_sniper` (Struktur/Pivots auf geschlossen, `current_price` = Entry/Targets via `get_live_price` **nach** erkanntem Signal statt `closes[-1]`) und `11_ai_mis` (geschlossene Features + `get_live_price`-Entry + Alias-Reproduktion `tsi_fast`/`macd_dif`/`macd_dea`). Klärungspunkt für den Folge-Task: **welche `get_live_price`-Quelle** diese Bots nutzen (`3_detectors.get_live_price` ist in einer numerisch benannten, nicht importierbaren Datei — ggf. Helfer nach `core/` heben oder den bestehenden Batch-Ticker anziehen).

### Stand Block 4 — Tranche 2 komplett (22/24/25/11 + core/live_price.py) erledigt (T-2026-CU-9050-111, 2026-07-13)

Der Tranche-2-Rest ist umverdrahtet — **Block 4 damit code-seitig komplett** (nur `14_ai_atb` bleibt ausgeschlossen → ATB2-Track T-106).

- **Quellentscheid (Michi, 2026-07-13):** Die `get_live_price`-Helfer aus `3_detectors.py` (numerisch benannt, nicht importierbar) sind **1:1 nach `core/live_price.py`** gehoben (`get_live_price` HTTP→DB-5m-Fallback, `get_live_prices_batch` 1 Call/Zyklus); `3_detectors` re-exportiert beide Namen (Batch-Ticker-Test zieht auf das echte `requests`-Modul-Objekt um). **Wichtiger Befund:** bei `22`/`24`/`25` speist `current_price` das **Erkennungs-Gate** (Level-Nähe/Retest), nicht nur den Entry — der Preis muss also **während** des Scans bekannt sein. Deshalb **Batch-Ticker vorab** (`get_live_prices_batch()` einmal pro Scan, `price_map.get(sym) or get_live_price(sym, conn)` je Coin) statt `get_live_price` pro Coin (das wären ~N HTTP-Calls/Zyklus). Der §5-Leitsatz „Preis erst nach Erkennung, kein Scan-Overhead" gilt damit nur eingeschränkt — ein Batch-Call pro Zyklus, kein Per-Coin-Overhead.
- **`22_ip_pattern`** — `read_candles(include_forming=False, limit=300)`, DESC-Umkehr entfällt, Pivots (`argrelextrema`) laufen jetzt repaint-frei auf geschlossenem Frame (kein manueller Drop nötig). Expliziter Float-Cast auf OHLC (`core.candles` liefert rohes NUMERIC/Decimal → sonst `Decimal − float`-Crash im QML-Gate). `current_price` = Batch-Ticker.
- **`24_quasimodo`** — `read_candles_with_indicators(include_forming=False, limit=100)`, `highs[:-1]/lows[:-1]`-Drop entfällt. Offset-Shift durch fehlende forming Kerze: `touched_recently` `k=1..3 → k=0..2`, `feature_idx len−2 → len−1` (dieselbe geschlossene Kerze, Trainer-Geometrie erhalten). `candle_columns` ohne `symbol` (Float-Cast-Loop). `current_price` (Proximity/SL/Zone-Gates + Entry) = Batch-Ticker.
- **`25_smc_ml_sniper`** (schwerster Offset-Rework) — `read_candles_with_indicators(include_forming=False, limit=150)`, `highs[:-1]`-Drop entfällt. Alle end-relativen Offsets +1: `last_closed len−2→len−1`, TD-Frische-Gates `len−p3 <= PIVOT_WINDOW+2 → +1`, `n_closed len−1→len` (Breakout-Suche + Follow-through decken jetzt die letzte geschlossene Kerze ab, `find_breaker_setup`-Docstring nachgezogen), BB-Retest-Anker `extract_ml_features(len−2)→len−1`. Chart-Tupel bleiben `(len−1, …, current_price)` = rechtester geschlossener Balken + Live-Preis. TD-Pivot-Indizes (`p3`) unverändert (adressieren die Vollarrays). `current_price` (BB-Level-Nähe + `calculate_smart_targets`) = Batch-Ticker.
- **`11_ai_mis`** — `read_candles_with_indicators(include_forming=False, limit=100)` in `_fetch_mis_frame`, DESC-Umkehr entfällt. Die API liefert rohe Indikatornamen → **`df.rename` reproduziert die drei `MIS_SQL_INDICATOR_SELECT`-Aliase** (`tsi_fast`/`macd_dif`/`macd_dea`), Frame bleibt byte-gleich zu `tools/walkforward_sim.py`; `indicator_columns` aus dem geteilten Katalog (`RSI_COLS + RAW_LINE_COLS + 3 Rohnamen + atr_14`), **`MIS_SQL_INDICATOR_SELECT` unangetastet** (harte Regel). Feature-Zeile `iloc[-2:-1] → iloc[-1:]` (weiter 1-Zeilen-DataFrame, byte-gleiche Features derselben geschlossenen Kerze). Entry-Preis = Batch-Ticker. Deckt auch `startup_feature_selfcheck` ab (geteiltes `_fetch_mis_frame`).
- **Contract 2 nachgezogen (`core/candles.py`):** `11_ai_mis`/`12_ai_ats` sind **keine Forming-Leser mehr** — die Ausnahme im Vertrag ist entfernt; der Live-Preis kommt über `get_live_price` (bereits als Forming-Leser gelistet) bzw. bei `12` aus der letzten geschlossenen Kerze.
- **Verifikation (DB-frei, Fleet-Python 3.13.12):** `py_compile` + `ruff check`/`format --check` + `mypy` grün auf allen 5 Dateien; `backtest/test_detector_batch_ticker.py` 4/4; Regression-Guard `verify` 24/24 nach jedem Bot. **Live-Verhaltensänderung (22/24/25 = Signal-Geometrie) → Michi-Go vor Enqueue; 24h-A/B ist Post-Merge-VPS; Schwellen erst nach Retrain (§5 q6).** DB-gebundener `startup_feature_selfcheck` (Bot 11) läuft beim VPS-Restart.

### Stand Block 5 — erledigt (T-2026-CU-9050-112, 2026-07-13, PR #102 gemergt)

Die zwei geteilten Feature-Builder lesen über `core.candles` mit `include_forming=False`, je mit ihrem Trainer-/Replay-Aufrufer im selben Commit (harte Regel 7). `core/funding_features.py` gehört NICHT zu Block 5 (liest `funding_rates`, kein Kerzen-Read; `funding_features_asof` cuttet schon strikt `<`).

- **5a `core/research_features.fetch_context_frame`** — DESC-f-String-SQL → `read_candles_with_indicators(include_forming=False, candle_columns=(open_time,close,volume), indicator_columns=CONTEXT_IND_COLS)`, `.iloc[::-1]` **entfernt** (API ASC; INVERSE der Block-2-Falle — bliebe die Umkehr, würde der Frame wieder DESC und `searchsorted` läge daneben). `CONTEXT_IND_COLS` als **eine Quelle** in `core/research_features` (aus `CONTEXT_SQL_SELECT` abgeleitet), importiert von `tools/research_dataset_common.load_candles_ctx` → Live-Frame == Offline-/Trainings-Frame byte-gleich. **Feature-Parität = No-op** (Feature-Idx via `searchsorted` über open_time). **Aber:** Bots 30/31/32 nehmen `live_price = df["close"].iloc[-1]` — der Entry-Anker verschiebt sich forming→letzte geschlossene Kerze (~≤59 min stale); Bot `33_ai_fif1` (deployed) nicht betroffen (`sig["entry"]`). Follow-up **T-2026-CU-9050-113** (→ `get_live_price`, contract 2).
- **5b `core/regime_logic.compute_features`** — `"BTCUSDT_15m"`/`"BTCDOMUSDT_15m"` (Literale) → `read_candles(include_forming=False)`. **Live-Gating-Änderung** (forming→closed 15m → `classify_regime` → `apply_debounce` → `regime_current` → Orchestrator-Whitelist). **Backfill-Boundary-Korrektur:** der `include_forming`-Cutoff ist **DB-`now()`-basiert** → droppt NICHT die bei einem historischen `as_of` laufende Kerze; Live läuft ohne `end`, Backfill mit `end=last_closed_open_time("15m", as_of)` (API-`end` inklusiv → die bei `as_of` forming Kerze fällt raus, kein Look-ahead). Expliziter Float-Cast auf `high/low/close` + BTCDOM `close` (`core.candles` liefert Decimal). `26_regime_detector` (live) + `backtest/backfill_regime_history` (replay) delegieren = **eine** Edit; `tools/regime_rules_study.py` ist eine Block-1-Replik (Drift zu Live damit geschlossen).
- **Verifikation (DB-frei, Fleet-Python 3.13.12):** `ruff`/`format --check`/`mypy` grün auf `core/research_features.py` + `core/regime_logic.py`; `backtest/test_feature_lookahead.py` 20/20 (zwei `fetch_context_frame`-Tests auf Fake-Reader migriert + neuer `compute_features`-Read-Kontrakt-Test: Live-ohne-`end` vs Backfill-`end=last_closed_open_time`); `test_regime_detector` + `test_bot_regime_analyzer` 79/79; Regression-Guard `smoke`+`verify` 24/24. **Reviews:** z-code-reviewer 3/3 PASS (N-Vote) + z-spec-compliance PASS (7/7). **Post-Merge-VPS (offen):** `backfill_regime_history.py` neu → `regime_history` closed-korrekt → TRM1-Retrain (Train + Serve lesen dieselbe Tabelle, Sequential-Jobs); Schwellen erst nach Retrain (§5 q6).

---

## 5. Offene Operator-Fragen (Michi)

Diese Fragen blockieren den Start von Phase 1. Keine davon ist in diesem Task entschieden worden.

> **Update 2026-07-13 (D-2026-CLD-109):** Die C-Gate-Fragen sind entschieden (Michi) — **1. Retention: unbegrenzt** (nur Compression, keine Retention-Policy), **2. REAL → double precision: ja** (alle ~120 Spalten), **3. 1d/1w: nur REST, kein WS**. Plus: Retrain aller Bots der Reihe nach (Sequential-Jobs). Details in `docs/TIMESCALE_R1_MIGRATION.md` §5.
>
> **Update 2026-07-13 (Block 4, T-2026-CU-9050-111):** Die **Block-4-Fragen sind entschieden** (Michi) — **4. Close-Grace-Period: `0`** (`KYTHERA_CANDLES_CLOSE_GRACE_SEC=0` bleibt Default; „ehrlicher", eine Kerze gilt in der Millisekunde als geschlossen, in der ihre Periode abläuft; per Env-Var später ohne Code-Change anhebbar).
> **5. Leitprinzip Erkennung vs. Generierung (überschreibt den ersten §5.5-Zwischenstand „True+Split"):** Die **Signal-Erkennung** (feuert ein Signal? — Pivots, Breakout, TD/QM-Struktur, Level-Nähe, ML-Features) läuft **einheitlich auf geschlossenen Kerzen** → `include_forming=False`; die forming Kerze ist im Analyse-Frame **nicht mehr enthalten**. Der **Live-Preis** wird **nur bei der Signal-Generierung** gebraucht (Entry1/CMP, `calculate_smart_targets`) und **separat via `get_live_price`** geholt (Binance-Ticker, Fallback neuester DB-Close), erst **nachdem** ein Signal erkannt wurde — also kein Query-Overhead pro Scan. Gilt **einheitlich für alle Block-4-Bots inkl. `11_ai_mis`/`12_ai_ats`**: Features aus der letzten geschlossenen Kerze, Entry-Preis via `get_live_price`. Ausnahme-Flag: `12_ai_ats` bezieht seinen Entry-Preis heute schon aus einer geschlossenen Kerze (`iloc[-2]`) — bleibt unverändert (nicht live gedreht), sofern nicht ausdrücklich gewünscht. Damit ist der Live-CMP eine reine Generierungs-Sorge; die im Inventar (§2 Block C) als „F, Drop entfernen" notierten Pattern-Bots (24/25/7/22) sind damit korrekt auflösbar. **Frage 6 (Signal-Raten)** bleibt Betriebsregel: R1 senkt die Raten bewusst, Schwellen erst **nach** dem Retrain neu tunen.

1. **Retention** (T-018 §5.1): Historie unbegrenzt (komprimiert ~4–6 GB) oder Fenster? Empfehlung des Design-Docs: unbegrenzt.
2. **`REAL` → `double precision`** für die ~120 Indikator-Spalten (P3.12)? Empfehlung: ja, im Zuge des Schema-Neubaus. Konsequenz hier: `tools/candles_parity.py` kanonisiert Floats auf 12 signifikante Stellen, damit der Typwechsel nicht jede Zeile als Drift meldet.
3. **1d/1w weiter per WS** oder nur REST/Catch-up (spart ~1.300 Streams)? Empfehlung: nur REST für 1d/1w.
4. **Close-Grace-Period.** Default `KYTHERA_CANDLES_CLOSE_GRACE_SEC=0`: eine Kerze gilt in der Millisekunde als geschlossen, in der ihre Periode abläuft. Alternative 2–5 s gegen den Pre-Close-Tick-Race. `0` ist ehrlicher, `>0` konservativer. **Vor dem ersten `include_forming=False`-Bot zu entscheiden.**
5. **`11_ai_mis` / `12_ai_ats`:** beide brauchen die forming Kerze als Live-Preis und die vorletzte als Feature-Zeile. Bleiben sie auf `include_forming=True` mit expliziter Trennung (mein Vorschlag), oder sollen sie zwei Calls machen (`read_candles(include_forming=False)` für Features + `latest_price()` für den Preis)? Zweiteres ist sauberer, kostet aber eine zweite Query pro Coin und Zyklus.
6. **Signal-Raten.** R1 **senkt** sie — das ist der Zweck. Klassik-Strategien feuern seltener, MIS/RUB/ATB-Feature-Verteilungen verschieben sich. **Schwellen erst nach dem Retrain neu tunen** (Report 16), nicht während der Umverdrahtung.
7. **Owner + Branch-Modell.** T-018 §4 verlangt „Migration als EIN Branch mit klarem Owner". Bei parallelen Sessions am selben Repo ist das eine Vorbedingung, keine Empfehlung.
8. ~~**Aufräum-Freigabe** (Nebenfunde): `tools/db_schema_analysis.py` als Duplikat löschen? `legacy_trainers/` löschen?~~ — **beide entschieden, 2026-07-10 (T-2026-CU-9050-039).** `tools/db_schema_analysis.py` ist **gelöscht** (stale, nie lauffähig; Root ist kanonisch). `legacy_trainers/` **bleibt** — es ist eingefrorene Provenienz der acht live geladenen Artefakte, kein toter Code. Löschen würde die Reproduktionsgrundlage von MIS1/ABR1/ATS1/RUB1/SRA1/AIM1/EPD1/ATB1 vernichten, um Dateien zu entfernen, die niemand ausführt und die aus ruff/mypy ausgeschlossen sind (`docs/OPUS-HANDOFF.md` §4.12: Excludes nicht als Selbstzweck aufräumen). Diese Frage blockiert Phase 1 damit nicht mehr.

---

## 6. Verifikation dieses Pakets

| Artefakt | Verifikation | Status |
|---|---|---|
| `core/candles.py` | `ruff check` + `ruff format --check` + `mypy` (= CI) | grün |
| `core/candles.py` | `backtest/test_candles.py` — 31 DB-freie Tests: Cutoff-Arithmetik (inkl. Montags-Anker und TZ-Unabhängigkeit), Identifier-Hygiene, TF-Sync gegen `core.config`, Argument-Validierung, Phase-4-Seam | grün |
| `tools/candles_parity.py` | `python tools/candles_parity.py --self-check` (DB-frei); ohne Credentials sauberer Exit 2 | grün |
| Regression-Guard | `python tools/regression_guard/guard.py smoke` | grün (unberührt) |
| `tools/candles_parity.py` gegen beide Tabellen | DB nötig | **offen — VPS, ab Phase 2** (Hypertable existiert noch nicht) |
| **Phase-0-Gate aus T-018: „API-Reads byte-gleich zu Direkt-SQL"** | `backtest/test_candles_db_parity.py` (T-2026-CU-9050-018): DB-freier Kanonisierungs-Kern (3 Tests, überall lauffähig) + 7 DB-Tests gegen die ALTEN per-Coin-Tabellen — `read_candles`/`read_indicators` byte-gleich zu Direkt-SQL, `limit` = neueste n + ASC, `include_forming=False` droppt exakt die forming Rows, JOIN-Read lässt die Candle-Seite unverändert, `latest_open_time` = `MAX(open_time)` | **grün — VPS-Lauf 2026-07-12** (DB `cryptodata`, BTCUSDT_1h) |

Die Build-Maschine hat keine DB-Credentials; jede DB-gebundene Verifikation gehört in eine VPS-Session (T-2026-CU-9050-011). `test_candles_db_parity.py` überspringt die DB-Tests dort sauber (`pytest.skip`) und lässt nur den Kanonisierungs-Kern laufen — der Phase-0-Gate-Lauf oben fand in einer dedizierten VPS-Owner-Session statt (T-2026-CU-9050-018, Read-only-SELECTs, keine Writes/DDL).
