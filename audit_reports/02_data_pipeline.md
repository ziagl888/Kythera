# Agent 2: Data Pipeline (1_data_ingestion.py, 2_indicator_engine.py, chart_data_service.py, 6_housekeeping.py)

### [CRITICAL] [data-integrity] Nightly gap-filler is a silent no-op — ON CONFLICT target doesn't match PK and symbol column missing
- 6_housekeeping.py:654-661. PK is (symbol, open_time); INSERT uses ON CONFLICT (open_time) → every insert raises (no matching unique constraint) AND omits symbol (NOT NULL PK part). Exception swallowed by except:continue (662-664) → gaps never filled, indicator invalidation also skipped (667-669). Whole nightly safety net nonexistent.
- Fix: INSERT (symbol, open_time, ...) ON CONFLICT (symbol, open_time) DO NOTHING; log per-gap failures.
- Confidence: high | DB-phase: yes (logs never contain "Kerzen gefüllt"; gap census)

### [HIGH] [data-integrity] WS buffer keyed by (symbol, timeframe) drops the final candle update at every candle boundary
- 1_data_ingestion.py:494-502 (flusher 226-249). No open_time in key; next candle's first msg overwrites closed candle's final values unless flusher ran in the sub-second window. k['x'] never inspected. DB holds closed candle at last-flush state (up to ~3s trades missing). 12h REST catch-up repairs later → live signals on wrong finals up to 12h, history changes under fired signals.
- Fix: key by (sym, tf, open_time), or flush-through when x=true.
- Confidence: high | DB-phase: yes (diff recent closed candles vs REST)

### [HIGH] [data-integrity] Still-forming candles written with no finality marker; indicator engine computes on them
- 1_data_ingestion.py:488-502; 2_indicator_engine.py:551-565. No is_closed column; engine loads everything incl. forming candle → last row of every indicator table computed on partial candle (1d: up to 23h58m incomplete; 1w: 6+ days). REST catch-up inserts current partial too. chart_data_service.py:247-249 does it RIGHT (if not k.get("x"): return).
- Fix: only flush x=true candles (plus live-tick table if needed), or is_closed column + engine excludes.
- Confidence: high | DB-phase: yes (MAX(open_time) of 1d table intraday)

### [HIGH] [bug] Whole-window indicators (trendline, POC/HVN, S/R, Fibonacci) broadcast as constants to every row — look-ahead baked into stored history
- 2_indicator_engine.py:435-467 (calc 213-319, write 562-565). Computed once over whole loaded DF (incl. partial candle), assigned to every row. Initial 3000-candle load → all history gets today's POC/FIB/trendline. Values window-length dependent (3000 vs 1000). Historical evaluation invalid; live latest-row-only consumption OK but indistinguishable.
- Fix: compute rolling per-row or persist only for latest row (NULL elsewhere), document as-of-now.
- Confidence: high (broadcast) / medium (impact) | DB-phase: yes (COUNT DISTINCT poc over old rows → constant runs)

### [HIGH] [bug] fillna(0) on warm-up windows writes fabricated indicator values instead of NULL
- 2_indicator_engine.py:325, 384-403, 284-286. MA/WMA/BOLL/DONCHIAN/EMA fillna(0); HVN pads 0, all-zeros on exception; SUPPORT/RESISTANCE fall back 0. Young coins (50-200 candles): MA_200=0 permanently → close > MA_200 trivially true → false signals on illiquid new listings. KAMA correctly leaves NaN (inconsistent).
- Fix: let NaN/NULL flow; consumers treat NULL as not-computable. No 0-sentinels in price domain.
- Confidence: high | DB-phase: yes (count ma_200=0 rows in young-coin tables)

### [MEDIUM] [bug] RSI is not Wilder RSI — ewm(span=period) doubles effective alpha
- 2_indicator_engine.py:336-337. alpha=2/(period+1) vs Wilder 1/period → RSI_14 behaves like Wilder ~7-8; not comparable to chart RSI; thresholds 70/30 fire far more often. ATR (420) correctly uses alpha=1/p — same file inconsistent.
- Fix: ewm(alpha=1/period) — but changes all downstream behavior → deliberate migration decision.
- Confidence: high (math) / medium (in-house convention?) | DB-phase: yes (compare stored RSI vs chart)

### [MEDIUM] [data-integrity] Indicator engine rolls windows across candle gaps with no continuity check
- 2_indicator_engine.py:551-561. Positional windows mix pre/post-gap candles. Only repair was gap-filler (broken, 24h scan only).
- Fix: verify open_time diff max <= 1.5×tf_delta over lookback; skip save + log on violation.
- Confidence: high | DB-phase: yes (gap census per table)

### [MEDIUM] [robustness] fetch_ohlcv_batch can loop forever; 418 ban handling retries into the ban
- 1_data_ingestion.py:94-120. except Exception: sleep(5) uncapped → one stuck symbol blocks ALL future 12h catch-ups (exe.map). 429/418 default wait 12s hammers into ban (same IP as trading API).
- Fix: max-retry counter; 418 → ≥120s exponential backoff.

### [MEDIUM] [robustness] Coin list frozen at process start — newly listed coins get no data until restart
- 1_data_ingestion.py:579-591; chart_data_service.py:356-374. WS lists + catch-up closure fixed at startup. Housekeeping creates empty tables nightly, ingestion never picks up.
- Fix: re-run update_trading_pairs each 12h iteration; diff WS workers on set change.
- DB-phase: yes (tables with 0 rows / late first open_time)

### [MEDIUM] [data-integrity] Two processes write coins.json with different filters, non-atomically
- 1_data_ingestion.py:31-56 (TRADING + not USDC → includes quarterlies BTCUSDT_250926, non-USDT) vs 6_housekeeping.py:24-47 (USDT PERPETUAL). Meaning flip-flops; engine takes verbatim → quarterly indicator tables sprawl. Non-atomic writes race readers → [] on parse error → skipped cycle.
- Fix: single writer (housekeeping filter) via core helper; tmp+os.replace everywhere.
- DB-phase: yes (tables matching %USDT_2%)

### [MEDIUM] [robustness] chart_data_service: no message watchdog, no gap handling — silent staleness, hidden holes
- chart_data_service.py:184-232, 312-323. Bare async for, ping_interval=None; health says ok with stale data. minutes = entry count not time window → invisible holes after reconnects.
- Fix: asyncio.wait_for(recv,120); last_message_age_sec in health; REST-backfill missed 1m or true time window.

### [MEDIUM] [performance] 12MB JSON snapshot serialized synchronously on the event loop every 60s
- chart_data_service.py:102-119, 346-353. Several hundred ms loop blockage; ~17GB/day disk writes.
- Fix: asyncio.to_thread; interval 300s.

### [MEDIUM] [performance] Indicator cycle risks exceeding 30-min budget; overrun silently skips trigger
- 2_indicator_engine.py:585-626, 322-325. 11 WMAs via rolling().apply Python lambda + KAMA pure-Python loops; NUM_WORKERS=3; fresh ProcessPoolExecutor per timeframe (6× spawn/cycle). Cycle >30min → minute in [2,32] misses next trigger → cadence halves silently.
- Fix: vectorize WMA (np.convolve/sliding_window_view, 10-50×); one executor per cycle; WARN if duration >25min.
- DB-phase: yes (log "Kompletter Indikator-Zyklus" durations)

### [MEDIUM] [bug] Delisted-trade cleanup closes trades on any symbol not in coins.json — incl. non-Binance symbols (metals/forex, ETHBTC)
- 6_housekeeping.py:128, 186. XAUUSD in active_trades_master → nightly force-close DELISTED PnL 0. Universe wobbles with dual coins.json writers.
- Fix: restrict delisted check to %USDT / known Binance-perp shape.
- Confidence: medium | DB-phase: yes (DISTINCT coin NOT LIKE '%USDT')

### [MEDIUM] [robustness] Housekeeping REST calls (gap-filler, exchangeInfo, leverage) have no 429/418 handling
- 6_housekeeping.py:508-522, 29-31, 279-291. After CRITICAL fix lands, outage night → burst of klines calls → 429→418 IP ban hitting trading endpoints.
- Fix: mirror ingestion's Retry-After/backoff handling; abort gap run after N consecutive rate-limits.

### [LOW] Shutdown snapshot: second asyncio.run with Lock bound to dead loop → final snapshot never happens (chart_data_service.py:395-404)
### [LOW] Chart-cleanup outbox-reference check string-equality on paths — separator/case mismatch defeats it (6_housekeeping.py:329-354). Fix: normcase+abspath both sides. DB-phase: check image_path format.
### [LOW] Table names f-string from coins.json/API throughout (ingestion 63,80,126,267; engine 169,520+; housekeeping 57,258,586). Fix: validate on load or sql.Identifier.
### [LOW] Naive local timestamps in indicator_state.json; engine minute-trigger naive local (2_indicator_engine.py:195,586). Housekeeping UTC correct.
### [LOW] Indicator columns REAL (float4) vs candles DOUBLE — BTC ~0.01 resolution loss; indicator close differs from candle close last digit.
### [LOW] handle_client crashes without response on non-int minutes; negative returns oldest candles (chart_data_service.py:314,322).
### [LOW] [performance] 12h catch-up re-downloads 7 full days for ~4300 coin×TF combos unconditionally (~7M row rewrite/day, WAL churn); also ~8600 serial to_regclass+MAX queries. Don't remove before boundary-overwrite fix (it's the only repair). Gap-aware fetch cuts ~85%.
### [LOW] Duplicated infra: TIMEFRAMES re-declared in housekeeping; _apply_keepalive copy-pasted ×2; engine logs unrotated to repo root via basicConfig.

## Cross-cutting observations
1. No candle-finality concept in schema = root cause of 3 findings. One is_closed flag or closed-only policy resolves cluster.
2. Connection budget: 27 bots × pool max 8 + engine's ProcessPool children (own pools) > 220 potential PG connections vs default max_connections=100. Measure.
3. Table sprawl ~7500 tables; 3s upsert loop ~1400 updates/s system-wide; catch-up rewrites 7d twice daily → autovacuum/WAL pressure.
4. Silent-failure culture: except+continue/pass/return-0 hid the dead gap-filler completely. Add errors-per-run counters.
5. Positive: atomic state-file writes, SAVEPOINT-per-row flush, Retry-After in ingestion catch-up, housekeeping UTC clock correct.

## Questions for live-DB phase
1. Gap-filler no-op proof: logs never contain "Kerzen gefüllt"; run one INSERT manually for the error text.
2. Gap census per TF last 30/90 days (generate_series anti-join).
3. Boundary-overwrite magnitude: diff ~100 recent closed candles vs Binance REST.
4. Forming candles present: MAX(open_time) of BTCUSDT_1d + _indicators intraday.
5. Fabricated zeros: ma_200=0 rows in young-coin tables.
6. Engine cycle durations from log; any >25min?
7. SHOW max_connections vs peak pg_stat_activity by application.
8. pg_stat_user_tables churn (n_tup_upd, n_dead_tup) on hot 5m table; WAL rate.
9. telegram_outbox: created_at exists? image_path format?
10. DELISTED closes on non-USDT symbols?
11. Quarterly-futures residue tables %USDT_2%?
12. RSI convention: stored RSI_14 vs exchange chart.
