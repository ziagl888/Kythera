## [2026-04-18] Regime Orchestrator (v1.0)

### Added
- `26_regime_detector.py` — classifies the market regime two-dimensionally every 5 minutes:
  BTC regime (TREND_UP/DOWN/CHOP/HIGH_VOLA/TRANSITION) + alt context (ALT_STRONG/NEUTRAL/WEAK)
  based on BTCUSDT + BTCDOMUSDT. Debounce (2 checks = 10 min) independently on both axes.
  Hourly status post + regime-change alerts in REGIME_STATUS_CHANNEL_ID.

- `27_bot_regime_analyzer.py` — hourly bot×regime performance analysis.
  Computes win rate, PnL stats, Sharpe for every (bot, BTC regime, alt context, direction,
  window) combination. Two-tier whitelist logic: standard rule (WR ≥ overall) and a
  stricter counter-trend rule (≥60% AND ≥overall+10pp).
  Daily cross-table post at 07:00 UTC.

- `28_signal_orchestrator.py` — signal gating + auto-close on regime change.
  Reads telegram_outbox every 500ms, identifies bot signals, checks the 4D whitelist,
  forwards whitelisted signals to REGIME_TRADING_CHANNEL_ID.
  Tracked as ROM1 in ai_signals (automatically picked up by 8_ai_trade_monitor).
  Overall fallback on detector failure (TRANSITION, instability, cold start).
  A3 cooldown (4h, same as AI bots).

- `core/regime_logic.py` — shared classification logic (importable from 26_ and backfill).
  `compute_features()`, `classify_btc_regime()`, `classify_alt_context()`,
  `classify_regime()`, `apply_debounce()`.

- `backtest/backfill_regime_history.py` — one-time history backfill (90 days, 5-min steps).
  Idempotent via ON CONFLICT DO NOTHING.

- `backtest/test_regime_detector.py` — unit tests for classifier + debounce
- `backtest/test_bot_regime_analyzer.py` — unit tests for performance stats + whitelist logic
- `backtest/test_signal_orchestrator.py` — unit tests for parsing + gating + cooldown + ROM1

- 6 new DB tables (idempotent via `ensure_regime_schema()`):
  `regime_history`, `regime_current`, `bot_regime_performance`,
  `bot_regime_whitelist`, `orchestrator_open_trades`, `orchestrator_suppressed_signals`

- `docs/REGIME_ORCHESTRATOR.md` — technical documentation
- `INSTALL_REGIME_ORCHESTRATOR.md` — installation guide

### Changed
- `core/config.py` — two new channel constants:
  `REGIME_TRADING_CHANNEL_ID = <CH_REGIME_TRADING>`
  `REGIME_STATUS_CHANNEL_ID = <CH_MARKET_DATA>`

- `main_watchdog.py` — three new process entries (start_delay 160/167/175):
  Regime Detector, Bot Regime Analyzer, Signal Orchestrator

- `23_market_tracker.py` — new line in the per-bot Kelly post:
  `Regime Fit: CHOP 58% (n=145), Overall 59% → NEUTRAL`
  With graceful degradation (shows `---` when the orchestrator isn't deployed)

### Architecture Notes
- No ML — fully rule-based and deterministic
- ROM1 appears automatically as its own "bot" in the performance table
- All existing bots unchanged (except 23_market_tracker.py, minimal)
- Cornix must be migrated to exclusively <CH_REGIME_TRADING>
