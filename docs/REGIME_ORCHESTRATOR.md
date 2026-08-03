# Regime orchestrator — technical documentation

**Version**: 5.0 (two-dimensional classification BTC regime × alt context)  
**As of**: April 2026  
**Author**: Automatically generated

---

## Overview

The regime orchestrator is a meta-system that sits above the existing 25 trading bots. It:

1. **Detects the market regime** two-dimensionally every 5 minutes
2. **Filters bot signals** by historical regime performance
3. **Posts its own trade** (module `ROM1`) into a dedicated Cornix channel as soon as a signal passes the gate
4. **Automatically closes trades** on regime changes

The system **trades on its own** — it's not a pure signal router. A bot signal that gets through is only the *trigger*: `compute_rom1_trade_params()` (`28_signal_orchestrator.py`) discards the original signal's entry/SL/targets and computes its own **ROM1 geometry** from the current price and real S/R zones, which is posted as its own Cornix message and tracked as `model='ROM1'` in `ai_signals`.

**Consequence (P1.10):** gating statistics ≠ execution statistics. The whitelist decides based on the *trigger bot's* performance, but ROM1 geometry is what's traded. A bot can be profitable in its regime and the ROM1 trade derived from it can still lose (and vice versa) — keep this in mind when reading the regime-performance tables.

### Why two-dimensional?

```
Achse 1: BTC-Regime      Achse 2: Alt-Context
─────────────────────    ─────────────────────
TREND_UP                 ALT_STRONG (BTCDOM fällt)
TREND_DOWN               ALT_NEUTRAL
CHOP                     ALT_WEAK (BTCDOM steigt)
HIGH_VOLA
TRANSITION
```

Without the alt-context axis, two fundamentally different scenarios would be classified identically:

| BTC regime | Alt context | Scenario | Recommendation |
|---|---|---|---|
| TREND_UP | ALT_STRONG | **Altseason** — alts pump harder than BTC | alt LONGs ideal |
| TREND_UP | ALT_WEAK | **BTC-only pump** — alts lag behind | alt LONGs deceptive |

---

## Architecture

```
26_regime_detector.py          (alle 5 Min)
  ↓ schreibt regime_history
  ↓ debounced → regime_current

27_bot_regime_analyzer.py      (stündlich)
  ↓ liest regime_history + closed trades
  ↓ schreibt bot_regime_performance
  ↓ schreibt bot_regime_whitelist

28_signal_orchestrator.py      (alle 500ms)
  ↓ liest telegram_outbox (neue Bot-Signale)
  ↓ prüft bot_regime_whitelist
  ↓ leitet durch → REGIME_TRADING_CHANNEL_ID
  ↓ trackt als ROM1 in ai_signals
  ↓ erkennt Regime-Wechsel → Close-Commands
```

---

## Processes

### `26_regime_detector.py`

**What**: classifies the BTC regime and the alt context every 5 minutes.  
**How**: loads BTCUSDT_15m + BTCDOMUSDT_15m, computes ATR/returns, classifies rule-based.  
**Output**:
- `regime_history` — every check as a line
- `regime_current` — debounced current regime (singleton)

**Most important constants** (at the top of the file):
```python
CHECK_INTERVAL_SECONDS = 300          # alle 5 Minuten
TREND_RETURN_THRESHOLD_4H_PCT = 1.5   # > ±1.5% in 4h = Trend
CHOP_RETURN_THRESHOLD_4H_PCT = 0.5    # < ±0.5% in 4h = Chop
VOLA_HIGH_PERCENTILE = 75             # ATR > P75 = HIGH_VOLA
VOLA_LOW_PERCENTILE = 40              # ATR < P40 = Trend-/Chop-Zone
ALT_CONTEXT_THRESHOLD_PCT = 1.5       # |BTCDOM 24h| > 1.5% = Rotation
REGIME_DEBOUNCE_COUNT = 2             # 2 Checks = 10 Min Bestätigung
```

**Hourly status post** (XX:00:50) in `REGIME_STATUS_CHANNEL_ID`:
```
🌡️ REGIME STATUS — 2026-04-18 14:00 UTC

BTC-Regime: CHOP (conf 85%)
Seit: 2026-04-18 11:25 UTC (2h 35min)
Alt-Context: ALT_NEUTRAL
...
```

### `27_bot_regime_analyzer.py`

**What**: computes the historical win rate for every bot in every (regime × alt context × direction) combination.  
**When**: hourly at XX:05:00.  
**Output**:
- `bot_regime_performance` — win rate, PnL stats per (bot, regime, alt, direction, window)
- `bot_regime_whitelist` — boolean whether the bot is let through in this 4D combination

**Whitelist logic (two-stage)**:

```
n < 30 Trades:
    → WHITELISTED (insufficient_data)

TREND_UP + SHORT oder TREND_DOWN + LONG (Counter-Trend):
    wr_bot ≥ 60% UND wr_bot ≥ overall + 10pp
    → WHITELISTED (counter_trend_specialist)
    sonst: GEBLOCKT (counter_trend_insufficient)

Alle anderen (Standard):
    wr_bot ≥ wr_overall
    → WHITELISTED (wr_above_overall)
    sonst: GEBLOCKT (wr_below_overall)
```

**Daily cross-table post** (07:00 UTC) in `REGIME_STATUS_CHANNEL_ID`:
```
📊 BOT × ALT-CONTEXT PERFORMANCE — TREND_UP (30d)

Bot          LONG                          SHORT
             ALT_W    ALT_N    ALT_S       ALT_W    ALT_N    ALT_S
MIS1-8h      45%↓     62%      71%↑        42%      47%      38%↓
...
```
> **Spec drift (P3.10):** the per-cell `↑`/`↓` markers in this example are
> **not implemented**. `_cell()` in `27_bot_regime_analyzer.py` only outputs
> `"{wr}%"` or `"---"`; the `↑`/`↓` legend below it in the status post is
> orphaned (documented at the legend code). Cells therefore never carry an
> arrow.

### `28_signal_orchestrator.py`

**What**: reads `telegram_outbox`, filters bot signals, passes matching ones through.  
**When**: every 500ms.  
**Output**:
- Forwarded signals in `REGIME_TRADING_CHANNEL_ID`
- ROM1 entries in `ai_signals`
- Tracking in `orchestrator_open_trades`
- Suppressed signals in `orchestrator_suppressed_signals`

**Overall fallback** (when the detector is unreliable):
- `no_regime`: regime_current empty → fallback to ≥50% overall WR
- `regime_is_transition`: explicit TRANSITION → fallback
- `regime_unstable`: ≥3 different regimes in 2h → fallback

---

## Database tables

| Table | Description | Writer |
|---|---|---|
| `regime_history` | Every 5-min check | `26_regime_detector` |
| `regime_current` | Debounced current regime (1 line) | `26_regime_detector` |
| `bot_regime_performance` | Win rate per bot/regime/alt/direction/window | `27_bot_regime_analyzer` |
| `bot_regime_whitelist` | Whitelist status per bot/regime/alt/direction | `27_bot_regime_analyzer` |
| `orchestrator_open_trades` | Passed-through open trades | `28_signal_orchestrator` |
| `orchestrator_suppressed_signals` | Suppressed signals (log) | `28_signal_orchestrator` |

---

## Parameter tuning

### When is a regime change too frequent?

If the fallback rate in the status post rises persistently above 30%, the ATR thresholds are too sensitive. Options:

> **Clarification (P3.10):** the `fallback %` in the hourly status post
> (`26_regime_detector.py`) is a **gate-path aggregate number** over *all*
> fallback reasons (`no_regime`, `regime_is_transition`, `regime_unstable`,
> `whitelist_stale`) from `orchestrator_open_trades.wl_reason` — **not** an
> isolated `regime_unstable` rate, and not computed from
> `regime_history`/ATR. A high value can therefore also come from
> TRANSITION/whitelist staleness; check the fallback reasons individually
> before tuning the ATR.

1. increase `VOLA_HIGH_PERCENTILE` (e.g. 80 instead of 75) → HIGH_VOLA rarer
2. increase `REGIME_DEBOUNCE_COUNT` (e.g. 3 instead of 2) → 15-min confirmation
3. increase `TREND_RETURN_THRESHOLD_4H_PCT` (e.g. 2.0 instead of 1.5) → stricter trend detection

### When is the whitelist too restrictive?

If many signals are filtered out and ROM1 performance isn't better than the average bot performance:

1. reduce `COUNTER_TREND_MIN_WR_PCT` (e.g. 55 instead of 60)
2. reduce `COUNTER_TREND_MIN_ADVANTAGE_PP` (e.g. 7 instead of 10)
3. increase `MIN_TRADES_FOR_DECISION` (e.g. 50 instead of 30) → more bots stay in insufficient_data

### Alt context too sensitive?

If ALT_STRONG/ALT_WEAK triggers too often:

1. increase `ALT_CONTEXT_THRESHOLD_PCT` (e.g. 2.0 instead of 1.5) → only stronger rotations trigger

---

## Troubleshooting

### `regime_history` isn't filling up

1. Check whether `26_regime_detector.py` is running: `ps aux | grep regime`
2. Check the log: `tail -f logs/REGIME_DETECTOR.log`
3. Check whether `BTCUSDT_15m` has data: `SELECT COUNT(*) FROM "BTCUSDT_15m"`
4. Check whether `MIN_DATA_POINTS_15M` (480 candles = 5 days) is satisfied

### `regime_current` isn't being initialized

Correction (P3.10, spec drift against the code): `regime_current` is
written immediately on the **first** successful check (cold start) — not
only after the second. `DEBOUNCE_COUNT=2` only delays subsequent regime
**changes**, not the initial population (`core/regime_logic.py`, the
cold-start INSERT runs before the debounce logic). If the row stays
empty, the detector hasn't passed a single check yet (missing data /
process not running), not "waiting for check 2".

### No signals in the trading channel

1. Is `REGIME_TRADING_CHANNEL_ID` correct? The bot must be admin in the channel.
2. Does `bot_regime_whitelist` have entries? → run `27_bot_regime_analyzer --initial-run`
3. Is the current regime a fallback regime (TRANSITION)?
4. Check the log: `tail -f logs/SIGNAL_ORCHESTRATOR.log`
5. Check suppressed signals: `SELECT * FROM orchestrator_suppressed_signals ORDER BY ts DESC LIMIT 10`

### ROM1 doesn't appear in per-bot performance

`8_ai_trade_monitor.py` takes over lifecycle tracking for ROM1. Only after the first closed ROM1 trade does it appear in `closed_ai_signals` and thus in the performance table.

### Cornix doesn't react to signals

Cornix must be configured to watch **exclusively** `<CH_REGIME_TRADING>` as the signal source. All old bot channels must be removed from the Cornix config.

---

## Operation

### Adding new bots

The orchestrator detects bots automatically via:
1. Regex patterns in the signal text (e.g. `MIS1`, `QM_BULL`)
2. Channel-ID mapping (`CHANNEL_TO_BOT_FALLBACK` in `28_signal_orchestrator.py`)

After deploying a new bot: the next hourly analyzer run automatically computes its whitelist entries.

### Manual regime override (testing)

```sql
UPDATE regime_current SET regime = 'TREND_UP', alt_context = 'ALT_STRONG'
WHERE id = 1;
```

The orchestrator detects the change on the next loop (500ms) and executes close commands.

### Disabling AUTO_CLOSE

```python
# In 28_signal_orchestrator.py:
AUTO_CLOSE_ON_REGIME_CHANGE = False
```

After restarting the process: regime changes are still detected and logged, but no close commands are posted.

### Differentiated auto-close: trail winners instead of closing them (A/B, T-2026-CU-9050-049)

The blind auto-close cut short ~49% of regime closes while in profit (report from T-2026-CU-9050-031). The close can optionally be differentiated: a trade that is **in profit** at a regime change is **not** market-closed, but instead its stop-loss is moved via a Cornix **SL update message** (`SL <SYMBOL> <preis>`, symbol-addressed like `Close`) to **break-even** or the **last TP level reached** — the trade keeps running. Losers keep getting closed.

```python
# In 28_signal_orchestrator.py bzw. per .env (Operator-Entscheid):
TRAIL_WINNERS_ON_REGIME_CHANGE  # env KYTHERA_REGIME_TRAIL_WINNERS=1, Default 0 (OFF)
```

**Default OFF** — this changes live-money behaviour and starts an A/B experiment; arming it is an operator decision (OPUS-HANDOFF §6). The SL update message is **not** a second Cornix-parsable signal message (hard rule 4).

**A/B evaluation** via `orchestrator_open_trades.regime_close_action`:

- `REGIME_CHANGE_CLOSED` — losers closed immediately; outcome = real PnL at close time (row lands in `closed_ai_signals` with `status='CLOSED_REGIME_CHANGE'`).
- `REGIME_CHANGE_TRAILED` — winners trailed, keep running; the real outcome comes later from the monitor/lifecycle sync (`closed_ai_signals.status` = `CLOSED_TP`/`CLOSED_SL`). The tag survives the final close, `regime_action_at` holds the timestamp.

Comparison of the two cohorts (net PnL/WR over 4–6 weeks) via a join `orchestrator_open_trades` → `closed_ai_signals` (coin+direction+`open_time`≈`opened_at`, like `sync_closed_trades`).
