# Regime Bot Kelly/WR Fix + Follow-ups

## What is being fixed

### (1) Kelly/WR fix — main issue

Three bugs in the regime orchestrator's performance tracking caused bots
with a real 57% win rate to be displayed as 0.28%:

1. **LEGACY TARGET HIT writes targets_hit=0** — the AI trade monitor sets
   `close_reason="LEGACY TARGET HIT"` on legacy trades with +2.5% PnL, but
   leaves `targets_hit` at 0. The old `targets_hit >= 1` logic incorrectly
   classifies these wins as losses.

2. **DELISTED / CLEANUP counts as a loss** — on symbol delisting, the
   trade is force-closed with `targets_hit=0`. DELISTED is neither a win
   nor a loss.

3. **Extreme PnL outliers** — isolated trades with PnL > 100% or
   < -100% point to data errors and massively skew avg_win/avg_loss.

**Fix**: PnL-based classification of every trade into **win**, **loss**
or **neutral**. Neutral trades are excluded from performance stats.

### (2) SQL crash on 'SL1' status

`closed_trades_master.status` partly contains non-integer values (e.g.
`"SL1"`, presumably from legacy bots or manual DB edits). The old query
`t.status::int > 0` crashed on these. Consequence: **no classic trades
were loaded**, the entire performance analysis stayed empty.

**Fix**: `is_win` is no longer loaded from SQL (it was overwritten by
the Python classification anyway). Instead the queries return a
placeholder `0 AS is_win`. The `status` string is passed straight
through as `close_reason` — the Python classification then decides
robustly based on PnL + reason keywords, regardless of which string is
in there.

### (3) Pandas UserWarning

`pandas.read_sql_query` warns about psycopg2 connections with a
UserWarning. The code still runs correctly, the warning is cosmetic.
**Fix**: the warning is suppressed via `warnings.filterwarnings` at the
top of the file, consistent with `10_pump_dump_detector.py`.

### (4) Vertical lines in the pump-dump chart removed

The spike-region marker (two vertical lines + shaded area) was
originally implemented to visually verify that the bucket-timestamp
logic works correctly after an earlier bug. The bug has since been
fixed and the logic validated — the visual confirmation is no longer
needed.

**Fix**: spike rendering disabled in `core/charting.py`. The function
signature (`spike_start`/`spike_end`/`spike_time` parameters) is kept
for backwards compatibility. The call from `10_pump_dump_detector.py`
doesn't need to change.

## Changed files

| File | Change |
|---|---|
| `27_bot_regime_analyzer.py` | Outcome classification, SQL robust against non-int status, UserWarning suppression |
| `28_signal_orchestrator.py` | PnL-based lifecycle sync with `CLOSED_NEUTRAL` status |
| `core/charting.py` | Spike marker (lines+region) disabled |
| `backtest/test_bot_regime_analyzer.py` | 10 new outcome tests + EPD1 E2E |
| `backtest/test_signal_orchestrator.py` | 9 new classification tests |

**No schema migration needed.** `orchestrator_open_trades.status` is
TEXT without a CHECK constraint, it accepts `CLOSED_NEUTRAL` directly.

## Deploy

```bash
cd C:\Users\Michael\PycharmProjects\crypto_trading_bot_v2

# Backup (empfohlen)
copy 27_bot_regime_analyzer.py 27_bot_regime_analyzer.py.bak
copy 28_signal_orchestrator.py 28_signal_orchestrator.py.bak
copy core\charting.py core\charting.py.bak

# Neue Dateien einspielen, dann Analyzer neu berechnen:
python 27_bot_regime_analyzer.py --initial-run
```

The `--initial-run` recomputes `bot_regime_performance` completely.
Without this step, the old (wrong) numbers remain until the hourly run
overwrites them (up to 24h).

## Verification

Check via SQL after the deploy:

```sql
-- WRs sollten jetzt realistisch sein (nicht mehr ~0%)
SELECT bot_name, direction, n_trades, win_rate
FROM bot_regime_performance
WHERE regime = 'ALL' AND alt_context = 'ALL'
ORDER BY bot_name, direction
LIMIT 30;
```

Expectation for EPD1: `win_rate` ≈ 57-58% (instead of the previous ≈ 0%).

## Validation (performed before delivery)

- **14/14** analyzer classification tests green
- **14/14** orchestrator classification tests green
- **EPD1 simulation**: 70.303 input → 65.668 decisive → **WR 57.84%**
- SQL crash fixed: `'SL1'` and other non-int strings now land in
  `close_reason` and are classified based on PnL

## Open (not in this package)

- **`8_ai_trade_monitor.py` fix** — would set `targets_hit=1` (instead
  of 0) for LEGACY TARGET HIT and normalise `close_reason`. Fixes the
  bug at the source, but the current fix works without this change.

- **Legacy-data migration** — SQL UPDATEs to retroactively correct
  `targets_hit` in old entries. Not needed, because the new PnL-based
  logic correctly interprets the legacy data.
