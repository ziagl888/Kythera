# Agent 4: Orchestrator + Regime (28, 26, 27, core/regime_logic, docs/REGIME_ORCHESTRATOR.md)

### [CRITICAL] [bug] Whitelist gate runs completely into the void for MIS1-* and classic bots (bot name mismatch, default-open)
- 28:134-148, 240-251, 556; 27:322; core/bot_naming.py:36-74. Analyzer normalises with pretty_name ('MIS1-8h', 'FastInOut'); orchestrator NEVER applies pretty_name (import missing) → lookup 'MIS1-8H'/'Fast In And Out' case-sensitive → no row → (True, "no_whitelist_entry") → signal ALWAYS passed through. Fallback + check_regime_change_and_close inert too. Test cements the raw names.
- Fix: pretty_name(bot_name) after identify_bot + on insert; monitor the default-open rate.
- DB phase: reason distribution in suppressed_signals; bot_name in open_trades vs whitelist.

### [CRITICAL] [bug] Orchestrator consumes its own ROM1 posts from the outbox; dedup depends solely on the cooldown → double-trade path
- 28:386-421, 524-537, 597-619. ROM1 message contains "Triggered by: MIS1-8H" → matches BOT_IDENTIFICATION_PATTERNS; no channel filter in the scan SELECT → self-echo through the whole pipeline; only the 4h cooldown protects, and it is only committed AFTER send_telegram. Crash between post and cooldown → restart → second leveraged trade. Normal operation: garbage rows in suppressed_signals.
- Fix: channel_id != REGIME_TRADING_CHANNEL_ID in the SELECT; ROM1 marker as a hard reject; commit cooldown/tracking BEFORE send.

### [HIGH] [bug] sent=FALSE filter races against the Telegram dispatcher → signals silently never gated
- 28:524-537 vs 4:183-194, 263, 338. Dispatcher marks sent=TRUE within ~0.1-0.5s; a signal inserted+sent between two orchestrator passes → drops out of the SELECT → never evaluated, no log. _last_seen_outbox_id makes the sent filter redundant.
- Fix: remove the sent/failed conditions; determine novelty via an id cursor + window.

### [HIGH] [data-integrity] Forward pipeline not atomic; batch cursor only advances at pass end → fired-but-untracked / batch replay
- 28:597-627. 4 steps with their own commits (send → rom1 → open_trade → cooldown); crash after send → trade exists at Cornix without ai_signals + without open_trades (monitor/regime-close never see it). Exception at row 5/10 → rows 1-4 (already posted) fire again on the next pass.
- Fix: DB writes first in ONE txn, outbox insert last; cursor per row; exceptions per row.

### [HIGH] [data-integrity] sync_closed_trades matches foreign trades (no model filter, no ORDER BY, 720h window) → wrong outcomes, premature loss of the opposite-side protection
- 28:879-925. Any coin+direction row from another bot within 30 days "closes" the ROM1 trade → (a) ROM1 statistics become random, (b) opposite check gone → hedge/double exposure, (c) regime close is skipped.
- Fix: only closed_ai_signals WHERE model='ROM1', window ±60s against open_time, ORDER BY.

### [HIGH] [data-integrity] Regime-close closes ALL open trades of all bots on coin+direction — not just orchestrator trades
- 28:672-817. ai_signals/active_trades_master filtered only by coin+direction → paper trades of foreign bots get censored as CLOSED_REGIME_CHANGE, correlated with regime changes (loss phases) → whitelist win rates biased upward (the money gate!).
- Fix: only model='ROM1' or via original_outbox_id.
- DB phase: COUNT by model/strategy WHERE status='CLOSED_REGIME_CHANGE'.

### [HIGH] [spec-drift] Docs describe a pure signal router — code generates independent trades with their own entries/SL/targets
- docs:18,53-59,131-137 vs 28:288-421. ROM1 discards the original parameters, computes its own from 5m close + HVN/SR → the gating statistics (collected with the original parameters) do not apply to ROM1 execution. Undocumented: 60s window, 4h cooldown, opposite block, force-close, default-allow.
- Fix: update docs to v6; document "gating statistics ≠ execution statistics" as a risk.

### [MEDIUM] [bug] TZ mix: update_cooldown NOW() (session TZ) into a naive column, check reads it as UTC; outbox window naive-UTC vs timestamptz
- market_utils:123-135 vs 98-120; 28:521-536; 4:60. DB TZ Vienna → 4h cooldown effectively 6h; 60s window becomes a 2h window (restart replays reach further back).
- Fix: NOW() AT TIME ZONE 'UTC' or aware params.

### [MEDIUM] [bug] Training/serving skew: analyzer attributes on RAW regime_history, gating runs on debounced regime_current
- 27:227-236, 273-282 vs regime_logic:263-422. Systematically wrong regime attribution around transitions (most signals). Backfill look-ahead: the last 15m candle with open_time<=as_of is not yet closed.
- Fix: attribution on the debounced state (effective_regime column); backfill open_time+15min<=as_of.

### [MEDIUM] [robustness] Detector "unreliable" heuristic counts RAW flaps → system can get stuck in the overall fallback permanently
- 28:177-191. COUNT(DISTINCT regime) over the raw history; TRANSITION as a catch-all class → ≥3 distinct is easily reached → 4D gating replaced by a coarse overall-WR filter, potentially most of the time. The fallback rate is (contrary to the docs) not reported in the status post.
- Fix: distinct on debounced changes; fallback rate as a metric.

### [MEDIUM] [robustness] Regime changes during orchestrator downtime are never caught up (in-memory _last_known_regime)
- 28:76-77, 949-952. Baseline init returns without re-evaluating open trades.
- Fix: on start, check all OPEN trades against the current whitelist; persist state vs regime_current.since.

### [MEDIUM] [data-integrity] Stale bot_regime_whitelist rows never cleaned up — cleanup only on the performance table
- 27:747-793 (perf only), 625-643 (whitelist UPSERT without DELETE). Old keys (MIS1-8H uppercase) with frozen decisions; the orchestrator queries with exactly these raw names → gated on month-old data.
- Fix: extend cleanup to the whitelist; computed_at staleness gate (>26h → treated like no_whitelist_entry + warning).

### [MEDIUM] [bug] No same-direction-open check: after the 4h cooldown, ROM1 stacks positions on the same coin+direction
- 28:272-284, 569-577. Exposure per coin is effectively unlimited.
- Fix: is_same_direction_open → suppress (or document+limit stacking).

### [MEDIUM] [bug] ROM1 SL without a distance cap — the next S/R zone can be 30-50% away, past liquidation at 20x
- 28:355-366 vs trade_utils:172-211 (calculate_smart_targets HAS caps 15%/10%; the ROM1 variant does not). Fallback SL ~7.6%.
- Fix: same hard caps; document the risk.
- DB phase: distribution of |sl-entry|/entry across ROM1 rows.

### [MEDIUM] [security] String-interpolated table names from parsed message text ("{coin}_5m")
- 28:312, 660; trade_utils:264. coin from re.search(r"Signal for\s+(\S+)") — \S+ allows arbitrary characters; the outbox is a shared table → second-order injection path with dbfiller privileges.
- Fix: symbol whitelist against coins.json / regex before use.

### [MEDIUM] [robustness] 60s window + start_delay=175 → all signals around every restart are silently discarded
- 28:35, 521-536. Every restart throws away ≥3 min of signal flow, with no log/metric.
- Fix: 5-10 min window + stale_signal logging in suppressed_signals; document it.

### [LOW] [spec-drift] Docs details wrong: regime_current init (cold-start insert on the FIRST check), ↑/↓ markers never implemented (legend only), fallback rate missing from the status post; display threshold n<20 vs decision n<30.
### [LOW] [performance] ensure_regime_schema on EVERY 5-min check (10+ CREATE IF NOT EXISTS); regime_history/suppressed_signals/perf without retention.
### [LOW] [code-quality] Duplicated dead constants in 26 (tuning there has no effect — and the docs point exactly there!); regime_current.confidence shows raw instead of effective confidence.
### [LOW] [robustness] identify_bot gaps: MAYANK/SMC never identifiable (bot_unidentified forever); UFI1 only a fragile footer regex; IGNORECASE produces case variants (aggravates Finding 1). Fix: standardised module_tag field in all Cornix messages.
### [LOW] [data-integrity] Small stuff: entry_price REAL; sync deadline 30d → stays OPEN forever; classic force-close status ignored during sync (CLOSED_REGIME_CHANGE → classified as TP/SL instead of neutral); daily-post default "TREND_UP" when regime_current is empty.

## Cross-cutting observations
1. The safety net is de facto the 4h cooldown (with the TZ bug, committed as the last step). ONE robust primitive defuses almost all the HIGHs: an atomic claim on the outbox row (UPDATE ... SET orchestrator_processed=TRUE ... RETURNING id) BEFORE every action — persistent, crash-safe, multi-start-safe.
2. Default-open as policy: no_whitelist_entry/fallback/insufficient_data (n<30 per 4D cell across 30 cells) → today's orchestrator is closer to a "repost bot with a cooldown" than a regime filter. Forwards per wl_reason as a metric.
3. Statistics hygiene: three systematic upward biases in the WR pipeline: open-trade censoring, regime-change closes REMOVED as neutral instead of realised, foreign-trade censoring. Relevant for a gate that compares tenths of a percentage point.
4. Positive: debounce correct; PnL sign correct everywhere; _classify_outcome defensive; no div-by-zero; parametrised INSERTs; idempotent UPSERTs.

## Questions for live-DB phase
1. SHOW timezone + NOW() vs NOW() AT TIME ZONE 'UTC'.
2. DISTINCT bot_name in whitelist vs open_trades vs suppressed (proof of mismatch + stale keys, MIN/MAX computed_at).
3. wl_reason distribution over 30 days: what % of forwards go through a real 4D decision?
4. Simulate the fallback rate over regime_history; TRANSITION share.
5. suppressed_signals with original_outbox_id → CH_REGIME_TRADING (self-echo proof); bot_unidentified cluster.
6. open_trades: OPEN >30d; coin+direction duplicates; check lifecycle_sync rows against real matches.
7. Do *_15m/*_5m contain the currently forming candle?
8. ROM1 risk as-is: SL distances; ROM1 WR vs overall (the actual KPI).
9. Watchdog logs: two orchestrator processes running simultaneously at any point?
