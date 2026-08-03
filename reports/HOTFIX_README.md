# HOTFIX: Telegram Bot Connection Leak

## Problem

In the last combined_fixes.zip, the new `4_telegram_bot.py` introduced a bug:

```
TELEGRAM_BOT - Error returning connection to pool: trying to put unkeyed connection
TELEGRAM_BOT - ⚠️ Loop-Fehler: connection already closed
```

In the log: **14 errors in 53 seconds**. The bot tries to fetch a DB connection per loop iteration, closes it twice, and the ConnectionPool slowly becomes unusable.

## Root Cause

In my reorder fix, I had implemented idle handling like this:

```python
with conn.cursor() as cur:
    ...
    if not unsent_messages:
        conn.close()        # ← BUG: schließt Connection
        await asyncio.sleep(IDLE_SLEEP_SEC)
        continue            # ← verlässt den with-Block, der dann versucht
                            #    den Cursor auf der geschlossenen Connection
                            #    zu schließen → Exception
```

The cursor's `with` block tries to close the cursor on exit, but the connection is already back in the pool. That raises an error, which the outer `except Exception` catches, but the `finally` then calls `conn.close()` again — this time on an already-closed connection.

## The Fix

No more `conn.close()` in the middle of the `with cursor()` block. Instead, on an empty batch only a flag is set (`batch_was_empty = True`), the `with` block is exited, the `finally` does the `conn.close()` cleanly, and the idle sleep happens **afterwards**.

```python
batch_was_empty = False
try:
    conn = get_db_connection()
    with conn.cursor() as cur:
        ...
        if not unsent_messages:
            batch_was_empty = True
        else:
            # send-loop
            ...
except Exception:
    ...
finally:
    if conn:
        conn.close()

# Idle-Sleep AUSSERHALB des try
if batch_was_empty:
    await asyncio.sleep(IDLE_SLEEP_SEC)
else:
    await asyncio.sleep(0.1)
```

## Apply

Overwrite a single file:

```
C:\_BOTS\crypto_trading_bot_v2\4_telegram_bot.py
```

Restart the watchdog:
```
Ctrl+C
py main_watchdog.py
```

## Verification after deploy

The following two errors no longer appear in the log:
- `Error returning connection to pool: trying to put unkeyed connection`
- `⚠️ Loop-Fehler: connection already closed`

## Git commit

```
git add 4_telegram_bot.py
git commit -m "hotfix: telegram-bot connection-leak at idle

BUG: conn.close() was called inside the 'with conn.cursor()' block when
the batch was empty, leaving the cursor's __exit__ trying to close a
cursor on a pool-returned connection. Multiple Loop-Fehler per minute.

FIX: Track batch_was_empty as a flag, exit the with-block normally, let
the outer finally do conn.close() exactly once. Idle-sleep happens
outside the try-block."
git push
```

## All other fixes from combined_fixes.zip are OK

- `6_housekeeping.py` (gap filler) ✓
- `10_pump_dump_detector.py` (Dead-Cat-Bounce) ✓
- `core/charting.py` (spike marker) ✓
- `23_market_tracker.py` (Per-Bot Performance + Kelly) ✓

Only the Telegram bot had the bug.
