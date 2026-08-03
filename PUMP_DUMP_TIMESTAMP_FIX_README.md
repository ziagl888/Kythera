# Pump-Dump Detector: Fix for Incorrect Spike Timestamps After Restart

## Symptom

After restarts, the pump-dump detector posted messages like:

```
💥 DUMP DETECTED
SPACE/USDT
→ −6.37% in 2m 0s
→ Spike: 02:22:43 → 05:40:19 UTC
```

That's impossible: the percentage line says "in 2 minutes," but the spike range
in the label shows **3 hours 17 minutes**.

## Root Cause

In `process_coin_logics`, the pump/dump detection accessed the bucket list
**index-based**:

```python
chg_pct = (current_price / prices[-lookback] - 1) * 100
spike_window = data[-lookback:]
```

The code assumed: "bucket index -12 = 120 seconds ago."

**This only holds in steady state.** After a restart, the deque is loaded from
`1minute.json` with up to **1440 old entries** (up to 4 hours of old data).
When fresh buckets then come in, old and new mix:

```
[bucket@01:45, bucket@01:45:10, ..., bucket@03:00,          ← 450 alte Einträge
 bucket@05:40, bucket@05:40:10, bucket@05:40:20]             ← 3 frische Einträge
                                               ↑ data[-1]
                                     ↑ data[-12]  ← zeigt bucket@03:00 !
```

- `data[-1]` is fresh (05:40:20)
- `data[-12]` points to **03:00** — a bucket that is **almost 3 hours old**
- The `chg_pct` calculation compares a fresh price against a 3h-old price → wrong percentages
- `spike_window = data[-12:]` contains the gap between 03:00 and 05:40
- `spike_prices.index(min(...))` finds the lowest value from the old range
- Timestamp 02:22 ends up in the label even though "2m 0s" is displayed

## Fix

**All lookbacks were refactored from index-based to timestamp-based.**

New helper functions in `10_pump_dump_detector.py`:

```python
def _parse_bucket_ts(entry): ...
def _find_bucket_before(data, now, seconds_ago, tolerance=20): ...
def _find_bucket_range(data, now, seconds_ago, tolerance=20): ...
```

Instead of `prices[-12]`, the code now uses `_find_bucket_before(data, now, 120, tolerance=20)`.
If no bucket exists in the time window `[120-20, 120+20]` seconds
(= data gap), the lookback is **skipped** and the next stage
is tried. This prevents false alerts after restarts.

Additionally: a **stale-data check at the start**:

```python
if latest_age_sec > 60:
    logger.debug(f"{symbol}: stale data ({latest_age_sec:.0f}s alt), überspringe")
    return
```

If the newest bucket is older than 60 seconds (= the process just started
or a WS outage), the entire cycle is skipped. On the next tick
(10 seconds later), the newest bucket is fresh again.

Sanity check for the spike start:

```python
if spike_start_dt is not None:
    age_sec = (now - spike_start_dt).total_seconds()
    if age_sec > seconds_back * 2 or age_sec < 0:
        logger.warning(f"{symbol}: spike_start inkonsistent...")
        spike_start_dt = None
        spike_time_label = None
```

If an inconsistent timestamp still slips through somehow, the spike label
is suppressed instead of posting a wrong value.

## What Does NOT Happen Anymore

The old symptoms are now excluded:

- ❌ "−6.37% in 2m 0s" with a spike range over 3h → the spike start is now only
  ever taken from the actual time window
- ❌ Wrong percentage calculation against 4h-old prices → the bucket isn't
  found, lookback is skipped
- ❌ Wild post flood after a restart → the stale-data check prevents alerts
  as long as there's no fresh data

## Tested

Three scenarios were tested end to end:

1. **Normal operation**: works unchanged — the bucket from 120s ago is
   reliably found
2. **After restart with old cache data**: `find_before(120s)` correctly
   returns `None`, alert is skipped
3. **Stale-data check**: with 4h-old data, `process_coin_logics` returns
   immediately without an alert

## Deploy

Only one file needs to be overwritten:
```
C:\_BOTS\crypto_trading_bot_v2\10_pump_dump_detector.py
```

Restart the watchdog. On the next restart test:

1. Stop the detector
2. Pause the system for ~30 minutes (until `1minute.json` contains old data)
3. Start the detector
4. Watch the logs: should show "stale data" debug entries, no
   false "DUMP DETECTED" with a 4h spike range

## What Changes for Users

In normal operation: **nothing**. Alerts come exactly as before, just with
correct spike timestamps.

After restarts there is a brief phase (~30-120 seconds) during which no
pump/dump alerts are posted — until enough fresh buckets have been collected.
That's a feature, not a bug.
