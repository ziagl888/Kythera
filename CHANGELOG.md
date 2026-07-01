## [2026-04-18] Regime-Orchestrator (v1.0)

### Added
- `26_regime_detector.py` — Classifies BTC regime every 5 min (5 classes) + Alt-Context (3 classes, BTCDOM-based). Debounce on both axes independently. Hourly status posts + regime-change alerts.
- `27_bot_regime_analyzer.py` — Hourly Bot×Regime×AltContext×Direction performance. Two-stage whitelist: standard (WR≥Overall) + counter-trend (≥60% AND ≥Overall+10pp). Daily cross-table post 07:00 UTC.
- `28_signal_orchestrator.py` — Signal gating every 500ms. 4D whitelist check, overall fallback on detector failure. Auto-close on regime change. ROM1 tracking in ai_signals (automatically picked up by 8_ai_trade_monitor). A3 cooldown (4h).
- `core/regime_logic.py` — Shared classification logic (compute_features, classify_regime, apply_debounce).
- `backtest/backfill_regime_history.py` — One-off 90-day backfill (idempotent).
- 3 test files in `backtest/`
- 6 new DB tables: regime_history, regime_current, bot_regime_performance, bot_regime_whitelist, orchestrator_open_trades, orchestrator_suppressed_signals
- `docs/REGIME_ORCHESTRATOR.md`, `INSTALL_REGIME_ORCHESTRATOR.md`

### Changed
- `core/config.py` — REGIME_TRADING_CHANNEL_ID = <CH_REGIME_TRADING>, REGIME_STATUS_CHANNEL_ID = <CH_MARKET_DATA>
- `main_watchdog.py` — 3 new processes (start_delay 160/167/175)
- `23_market_tracker.py` — `Regime Fit:` line in Kelly post (graceful degradation)

# CHANGELOG — Crypto Bot Deep-Review & Fix Round

This review went through the entire codebase (46 Python files, 24 trading bots, Binance Futures integration, Telegram outbox, PostgreSQL storage) and found/clarified **91 analysis points** in total. Of these:

- **57 real bugs fixed**
- **20 points clarified as false alarms from initial analysis** (code was correct, my initial assessment too pessimistic)
- **6 points explicitly descoped by the user** (Master-Bot Dedupe, BTC SMC 100×, Handler-Auth, Cross-Bot-Limit etc.)
- **5 points documented as too invasive for this round** (schema change, retraining required)
- **3 points clarified as asyncio-non-critical/unreproducible**

## Fixes by topic

### 🔧 Trade-Signal-Korrektheit (kritisch)
- **#1 SHORT-RSI-Bug** (strat_fast_in_out, strat_5_percent): `>=75 OR <=45` → nur `<=45`. Der Code generierte SHORT-Signale bei hoch-RSI-**UND** tief-RSI gleichzeitig → regelmäßig dumme Trade-Richtung
- **#3 RSI-fillna-Parens**: `100 - (100/(1+rs)).fillna(0)` → `(100-100/(1+rs)).fillna(50)`. Previously, RSI fälschlich als 100 (Max-Overbought) angezeigt wo keine Daten da waren → false SHORTs
- **#13 AI SR Bot Cooldown**: `pd.Timestamp.utcnow().tz_localize(None)` crashte in neueren pandas-Versionen. Auf `market_utils.check_cooldown` migrated
- **#15 Master-Bot all_ai_models-Konkat-Typo**: `'MIS1' 'MSI1-8h_pump'` (fehlendes Komma + vertauschte Buchstaben) konkateniert → ungültiger Model-Name in ml_predictions_master
- **#19/#18 ATB `except: return True`**: Cooldown-Check gab bei DB-Hiccup "ja, darf traden" zurück → Signal-Spam. Jetzt safe-default `False`
- **#32 ATS Bot** OBV-Normalisierung: `obv - obv.iloc[0]` damit die OBV-Werte nicht vom willkürlichen Startpunkt der Historie dominiert werden
- **#38 Smart Targets SL-Fallback**: `min/max`-Cap added damit SL garantiert innerhalb (LONG) or außerhalb (SHORT) entry2 liegt
- **#58 SMC ML Sniper BB**: `MAX_BB_AGE=20` + 0.3% echter Break-Through (vorher konnten 200-Kerzen-alte Stale-BBs immer noch ein Signal triggern)
- **#59 SMC ML Sniper TD**: `MAX_TD_SPAN=50` Kerzen (vorher: unbegrenzt)
- **#60 BTC SMC**: `ORDER BY ASC` → `DESC + reverse` (historische Daten wurden in falscher Reihenfolge gelesen)
- **#65/#66 IP Pattern Bot**: `ALERTED_QMS` persistent, Pattern-ID mit Unix-Timestamp statt Laufzeit-Counter
- **#55/#56 Quasimodo**: `MIN_CONFIDENCE 0.40→0.65`, `ZONE_TOLERANCE 0.01→0.005`, Touch+Bounce-Validierung

### 🗄️ DB-Robustheit
- **#4 Atomic Write**: `active_trades_master` + `telegram_outbox` in einer Transaktion statt zwei separaten (verhindert Chart ohne Trade)
- **#8/#16 Monitor-Connection**: Auto-Reconnect im Trade Monitor und AI Monitor bei DB-Hiccup (vorher: Bot loopte mit toter Connection weiter)
- **#10 Trade Monitor datetime**: `datetime.now()` → `datetime.now(timezone.utc)` in close_trade
- **#14 DB-Flusher SAVEPOINT**: Per-Row-Fehlertoleranz, ein einzelner Insert-Fail reißt nicht den ganzen Batch mit
- **#48 telegram_outbox Cleanup**: Nightly DELETE gesendeter Einträge älter als 7 Tage (vorher wuchs die Tabelle unbegrenzt)
- **#60 BTC SMC** ORDER BY (oben)

### 🎯 Cooldown-Konsolidierung  
- **#33/#34/#51** drei eigene `is_cooled_down`/`set_cooldown`-Duplikate removed (SMC Forex, ATB, andere), alle nutzen jetzt `core.market_utils.check_cooldown`/`update_cooldown`
- **#34** SMC Forex Cooldown-Keys ohne TF-Suffix → TF-übergreifender Block (1h und 4h nicht gleichzeitig auf demselben Coin)
- **#17 RUB** Cooldown-Check VOR ML-Prediction (CPU-Einsparung)
- **#13 AI SR** eigener timezone-crashing Cooldown removed
- **#35 Mayank** 12h-Cooldown pro asset+TF+direction added
- **#42** Mayank asset-cooldown (durch #35 bereits erledigt)

### 📊 Indicator Engine & Strategies
- **#5** Duplikat-Lookback-Block im indicator_engine (bewirkte dass inkrementelle Läufe IMMER 3000 statt 1000 Kerzen luden)
- **#6 Trendline** NaN-robust bei konstanten Preisen, Division-durch-0 bei `y[0]==0` abgefangen
- **#12 Volume Indicator** `df.loc[index-1]` → `iloc` mit `reset_index` (KeyError bei Filter-inducierten Index-Lücken)
- **#45 indicator_state.json** atomares Write via tmp+fsync+os.replace (verhindert halb-geschriebene Reads)
- **iloc-Fix in strat_fast_in_out**: DESC-sortierter DF, `iloc[-1]` → `iloc[0]` für ATR-Zugriff
- **#11 Support/Resistance Zuordnung**: Nach Proximity (nächster unter Preis = support, nächster über = resistance) statt nach Zeit

### 🤖 AI-Bots (Feature-Robustheit)
- **#20 ATB** NaN/Inf-Absicherung vor predict_proba (`replace([inf,-inf],nan).fillna(0)`)
- **#24 RUB get_f** behandelt NaN/Inf, nicht nur None
- **#25 ABR1** X_event NaN/Inf-Absicherung
- **#27 MIS1** Thresholds beim Load explizit geloggt (Drift-Detection)
- **#36 AI Monitor** targets_hit defensiv zu int() casten
- **#74 ABR1 SUCCESS_CLASS_IDX=0**: Warnung-Kommentar added — **Bitte manuell gegen Training-Notebook verifizieren!**
- **#75 ABR1** asymmetrische Thresholds dokumentiert (LONG=0.60, SHORT=0.80)
- **#76 ABR1** redundanten `minute != 0` Filter removed (1h-Kerzen haben immer minute=0)
- **#52** get_hvn_and_sr_levels zentralisiert (5 bit-identische Kopien → 1 in core/trade_utils.py)

### 💬 Telegram Outbox & Charts
- **#21 active_patterns.json** atomares Write
- **#31 Housekeeping** respektiert Outbox-Referenzen (löscht keine Charts mehr, die noch versendet werden müssen)
- **#67 Chart-Pfad Race**: `int(time.time()*1000)` Millisekunden-Timestamp im Dateinamen (ms statt s)
- **#68/#87 mark_sent/mark_failure**: Chart nur löschen wenn keine anderen ungesendeten Outbox-Einträge die Datei noch referenzieren

### 🛠️ Infra (Watchdog, Dashboard, Housekeeping)
- **#69 Watchdog** Exponential Backoff `[0, 15, 60, 300, 900]s` basierend auf Crashes in der letzten Stunde
- **#70 Dashboard** stdout/stderr in `logs/dashboard.log` statt DEVNULL
- **#85 update_model** Threshold-Files (`threshold_*.pkl`) explizit überspringen + `hasattr(model, 'save_model')` Check
- **#88 core/state_utils.py** neu: atomic_write_json + atomic_read_json als zentrale Helper

### 📈 Market Tracker & Logger
- **#71/#73** Kategorie-Mapping korrigiert (TD/BB/QM als PATTERN statt INDICATOR/VOLUME)
- **#72** Volume-Näherung: `close` → `(open+close)/2` (reduziert Intra-Candle-Bewegungsfehler)
- **#81 Whale Logger** `format_usd` handled negative Werte korrekt (`-$1.5M` statt `$-1500000`)
- **#82 Funding Logger** `check_top20_positive_pct` gibt None statt 50.0 bei leeren Daten
- **#83 Funding Logger** `calc_diff_bps` gibt None bei fehlender Historie, Display zeigt "N/A"

### ❌ Gelöscht
- **99_smc_paper_bot.py** removed (Paper-Trading-Bot der nicht live lief)
- Entsprechende line in `main_watchdog.py` removed

## ⚠️ Wichtige Hinweise für den Deploy

### Sofort-Checks vor Deploy
1. **ABR1 SUCCESS_CLASS_IDX manuell verifizieren**: `18_ai_abr1_bot.py` line 45 — aktuell steht `0`, standard-XGBoost-Konvention wäre `1`. Bitte gegen dein Training-Notebook prüfen. Wenn dort `y=1` für gewinnende Trades steht, MUSS der Wert auf `1` geändert werden.

### Kurzfristig prüfen (erste Run nach Deploy)
2. **Funding-Logger Telegram-Output**: Beim allerersten Lauf wenn keine 1h/24h-Historie vorliegt, sollten jetzt `N/A`-Strings statt `+0.0bps`/`50.0%` angezeigt werden. Das ist gewollt.
3. **Market-Tracker Kategorisierung**: TD/BB/QM/SMC-Signale erscheinen jetzt in der Kategorie PATTERN statt INDICATOR/VOLUME. Die Statistik ändert sich einmalig.
4. **Dashboard-Log**: `logs/dashboard.log` sollte erstellt und beschrieben werden. Falls Dashboard crasht, steht der Traceback da drin.
5. **SMC Forex Cooldowns**: Jetzt TF-übergreifend (12h). Falls Signale signifikant seltener kommen, kann die Dauer auf 8h reduziert werden (Code-Stelle `check_cooldown(conn, cd_key, display_name, 'LONG', 12)`).

### Mittelfristig (Performance-Backlog, nicht jetzt)
6. **#50** Market Tracker 10k-Queries: Würde eine unified `ohlcv_30m`-Tabelle erfordern (Ingestion-Schema-Change). Performance-Backlog.
7. **#88** 7 weitere State-Files könnten auf `core.state_utils` konsolidiert werden. Niedrige Priorität.

### Nicht gefixt, außerhalb Scope (bewusst)
- #22 Master-Bot Dedupe (separate Bewertung pro Quelle gewollt)
- #62 BTC SMC 100× Leverage (deliberate high-risk)
- #77/#78 Open-Handler Auth (privates Env, intentional)
- #89 Cross-Bot Position-Limit (Bots laufen selektiv)
- #2 check_recent_trades (ist ok so)
- #53 TSI Parameter-Order (verifiziert: EWMA-Komposition ist bit-identisch)

## Statistik final

| Kategorie | Anzahl |
|---|---|
| Real bugs fixed | **57** |
| Als false alarme geklärt | 20 |
| User-explizit out-of-scope | 6 |
| Zu invasiv for this round | 5 |
| Asyncio-unkritisch | 3 |
| **Gesamt geprüft** | **91** |

| Python-Dateien im Projekt | Syntax-clean nach Fixes |
|---|---|
| 47 | 47 ✅ |

## Dateien mit wesentlichen Änderungen

```
core/
  market_utils.py              (FIX #51 zentral nutzbar)
  trade_utils.py               (+ get_hvn_and_sr_levels, ensure_min_tp_distance)
  state_utils.py               (NEU)
  update_model.py              (#85)

1_data_ingestion.py            (#14 SAVEPOINT)
2_indicator_engine.py          (#5, #6, #45)
3_detectors.py                 (#4 atomic signal write)
4_telegram_bot.py              (#68/#87 chart ref-counting)
5_trade_monitor.py             (#8 reconnect)
6_housekeeping.py              (#31, #48)
7_pattern_detector.py          (#21 atomic)
8_ai_trade_monitor.py          (#8, #36)
9_ai_sr_bot.py                 (#13, #52)
10_pump_dump_detector.py       (#38, #52)
11_ai_mis_bot.py               (#11, #15, #27)
12_ai_ats_bot.py               (#32, #38, #52)
13_ai_rub_bot.py               (#17, #24, #38, #52)
14_ai_atb_bot.py               (#18/#19, #20, #51, #52)
15_ai_master_bot.py            (#15, #28)
16_smc_forex_metals_bot.py     (#33, #34, #51)
17_mayank_bot.py               (#35)
18_ai_abr1_bot.py              (#25, #74, #75, #76)
19_whale_logger_bot.py         (#81)
20_funding_logger_bot.py       (#82, #83)
21_btc_smc_strategy.py         (#60)
22_ip_pattern_bot.py           (#65, #66)
23_market_tracker.py           (#71, #72, #73)
24_quasimodo_bot.py            (#55, #56)
25_smc_ml_sniper.py            (#58, #59)
main_watchdog.py               (#69, #70)
strategies/
  strat_fast_in_out.py         (#1)
  strat_5_percent.py           (#1)
  strat_main_channel.py        (#11)
  strat_volume_indicator.py    (#12)
```

Einzelne Batch-Reports in `reports/batch_1_report.md` … `reports/batch_6_report.md`.
