## [2026-04-18] Regime-Orchestrator (v1.0)

### Added
- `26_regime_detector.py` — Klassifiziert alle 5 Min das Markt-Regime zweidimensional:
  BTC-Regime (TREND_UP/DOWN/CHOP/HIGH_VOLA/TRANSITION) + Alt-Context (ALT_STRONG/NEUTRAL/WEAK)
  basierend auf BTCUSDT + BTCDOMUSDT. Debounce (2 Checks = 10 Min) auf beiden Achsen unabhängig.
  Stündlicher Status-Post + Regime-Change-Alerts in REGIME_STATUS_CHANNEL_ID.

- `27_bot_regime_analyzer.py` — Stündliche Bot×Regime-Performance-Analyse.
  Berechnet Win-Rate, PnL-Stats, Sharpe für jede (Bot, BTC-Regime, Alt-Context, Direction,
  Window)-Kombination. Zweistufige Whitelist-Logik: Standard-Regel (WR ≥ Overall) und
  strengere Counter-Trend-Regel (≥60% UND ≥Overall+10pp).
  Täglicher Cross-Table-Post um 07:00 UTC.

- `28_signal_orchestrator.py` — Signal-Gating + Auto-Close bei Regime-Wechsel.
  Liest telegram_outbox alle 500ms, identifiziert Bot-Signale, prüft 4D-Whitelist,
  leitet whitelisted Signale in REGIME_TRADING_CHANNEL_ID durch.
  Trackt als ROM1 in ai_signals (automatisch vom 8_ai_trade_monitor übernommen).
  Overall-Fallback bei Detektor-Ausfall (TRANSITION, Instabilität, Cold-Start).
  A3-Cooldown (4h, gleich wie AI-Bots).

- `core/regime_logic.py` — Geteilte Klassifikations-Logik (importierbar von 26_ und backfill).
  `compute_features()`, `classify_btc_regime()`, `classify_alt_context()`,
  `classify_regime()`, `apply_debounce()`.

- `backtest/backfill_regime_history.py` — Einmaliger Historien-Backfill (90 Tage, 5-Min-Schritte).
  Idempotent via ON CONFLICT DO NOTHING.

- `backtest/test_regime_detector.py` — Unit-Tests für Classifier + Debounce
- `backtest/test_bot_regime_analyzer.py` — Unit-Tests für Performance-Stats + Whitelist-Logik
- `backtest/test_signal_orchestrator.py` — Unit-Tests für Parsing + Gating + Cooldown + ROM1

- 6 neue DB-Tabellen (idempotent via `ensure_regime_schema()`):
  `regime_history`, `regime_current`, `bot_regime_performance`,
  `bot_regime_whitelist`, `orchestrator_open_trades`, `orchestrator_suppressed_signals`

- `docs/REGIME_ORCHESTRATOR.md` — Technische Dokumentation
- `INSTALL_REGIME_ORCHESTRATOR.md` — Installations-Anleitung

### Changed
- `core/config.py` — Zwei neue Channel-Konstanten:
  `REGIME_TRADING_CHANNEL_ID = <CH_REGIME_TRADING>`
  `REGIME_STATUS_CHANNEL_ID = <CH_MARKET_DATA>`

- `main_watchdog.py` — Drei neue Prozess-Einträge (start_delay 160/167/175):
  Regime Detector, Bot Regime Analyzer, Signal Orchestrator

- `23_market_tracker.py` — Neue line im Per-Bot-Kelly-Post:
  `Regime Fit: CHOP 58% (n=145), Overall 59% → NEUTRAL`
  Mit Graceful Degradation (zeigt `---` wenn Orchestrator nicht deployt)

### Architecture Notes
- Kein ML — komplett regelbasiert und deterministisch
- ROM1 erscheint automatisch als eigener "Bot" in der Performance-Tabelle
- Alle bestehenden Bots unverändert (außer 23_market_tracker.py, minimal)
- Cornix muss auf ausschließlich <CH_REGIME_TRADING> migrated werden
