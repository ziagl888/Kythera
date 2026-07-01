# HOTFIX: Telegram-Bot Connection-Leak

## Problem

Im letzten combined_fixes.zip hat der neue `4_telegram_bot.py` einen Bug eingeschleppt:

```
TELEGRAM_BOT - Error returning connection to pool: trying to put unkeyed connection
TELEGRAM_BOT - ⚠️ Loop-Fehler: connection already closed
```

Im Log: **14 Fehler in 53 Sekunden**. Der Bot versucht pro Loop-Iteration eine DB-Connection zu holen, schließt sie zweimal, und der ConnectionPool wird langsam unbrauchbar.

## Root Cause

In meinem Reorder-Fix hatte ich die Idle-Behandlung so implemented:

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

Der Cursor-`with`-Block versucht beim Exit den Cursor zu schließen, aber die Connection ist schon zurück im Pool. Das wirft einen Fehler, der outer `except Exception` fängt ihn, aber das `finally` macht dann nochmal `conn.close()` — diesmal auf einer bereits-geschlossenen Connection.

## Der Fix

Kein `conn.close()` mehr mitten im `with cursor()`-Block. Stattdessen wird bei leerem Batch nur ein Flag gesetzt (`batch_was_empty = True`), der `with`-Block verlassen, das `finally` macht den `conn.close()` sauber, und **danach** erfolgt der Idle-Sleep.

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

## Anwenden

Eine einzige Datei überschreiben:

```
C:\_BOTS\crypto_trading_bot_v2\4_telegram_bot.py
```

Watchdog neu starten:
```
Ctrl+C
py main_watchdog.py
```

## Verification nach Deploy

Im Log erscheinen die beiden Fehler nicht mehr:
- `Error returning connection to pool: trying to put unkeyed connection`
- `⚠️ Loop-Fehler: connection already closed`

## Git-Commit

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

## Alle anderen Fixes aus combined_fixes.zip sind OK

- `6_housekeeping.py` (Gap-Filler) ✓
- `10_pump_dump_detector.py` (Dead-Cat-Bounce) ✓
- `core/charting.py` (Spike-Marker) ✓
- `23_market_tracker.py` (Per-Bot Performance + Kelly) ✓

Nur der Telegram-Bot hatte den Bug.
