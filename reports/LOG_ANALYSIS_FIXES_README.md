# Log Analysis Fixes: Kelly Post, CJK Fonts, Cooldown

## Problem 1: Per-bot performance post doesn't arrive

### Symptom in the log
```
14:02:05 - MARKET_TRACKER - ✅ Per-Bot Performance-Post gesendet (46 Strategien, 549969 Trades total).
14:02:08 - TELEGRAM_BOT - ⚠️ Msg 572058 Sendefehler, wird erneut versucht: Timed out
14:02:24 - TELEGRAM_BOT - ⚠️ Msg 572060 Sendefehler, wird erneut versucht: Message is too long
14:02:42 - TELEGRAM_BOT - ⚠️ Msg 572060 Sendefehler, wird erneut versucht: Message is too long
```

### Root Cause
With 46 active strategies (classic + AI models across various timeframes
+ SMC sniper + TD/BB etc.) the combined message exceeds 4096 characters.
Telegram rejects it with "Message is too long"; retries don't help because
the text stays unchanged.

### Fix
`23_market_tracker.py` now splits the post into multiple messages:

- **Message 1**: table + legend (~3400 chars)
- **Message 2**: Kelly block header + bots 1-N (~3500 chars)
- **Message 3+**: "(continued)" with further bots if needed

The split is per-bot — a single bot entry is never split in the middle.
Every message stays safely under 3896 chars (= 4096 − 200 buffer).

**Tested** with 46 strategies: 3 messages of 3443 / 3654 / 3610 chars.

### Parameter
Easily adjustable in `23_market_tracker.py`:
```python
TELEGRAM_TEXT_LIMIT = 4096       # Telegram API-Limit, nicht ändern
SAFETY_BUFFER = 200              # Puffer — bei Bedarf erhöhen
```

## Problem 2: Warnings about Chinese glyphs in charts

### Symptom in the log
```
UserWarning: Glyph 24065 (\N{CJK UNIFIED IDEOGRAPH-5E01}) missing from font(s) DejaVu Sans.
UserWarning: Glyph 23433 (\N{CJK UNIFIED IDEOGRAPH-5B89}) missing from font(s) DejaVu Sans.
UserWarning: Glyph 20154 (\N{CJK UNIFIED IDEOGRAPH-4EBA}) missing from font(s) DejaVu Sans.
UserWarning: Glyph 29983 (\N{CJK UNIFIED IDEOGRAPH-751F}) missing from font(s) DejaVu Sans.
```

币安人生 = "Binance Life" — a meme token on Binance Futures.

### Root Cause
Some Binance Futures coins have Chinese names like `龙虾USDT` or
`币安人生USDT`. These end up in chart titles (e.g. `SYMBOL • 241min • $X.XX`).
matplotlib's default font "DejaVu Sans" contains no CJK glyphs → warning
spam in the log, boxes instead of characters in the chart image.

### Fix
`core/charting.py` sets a font fallback chain on import:
```python
plt.rcParams['font.sans-serif'] = [
    'DejaVu Sans',           # Latin (default)
    'Microsoft YaHei',       # Win10/11 CJK
    'SimHei',                # Windows CJK fallback
    'Noto Sans CJK SC',      # Linux
    'Arial Unicode MS',      # macOS
    'sans-serif'
]
```

matplotlib uses, per character, the first font that contains the
corresponding glyph. Unknown fonts are silently ignored — harmless if not
all of them are installed. On Windows 11, "Microsoft YaHei" is present by
default and provides full CJK support.

Additionally:
```python
logging.getLogger('matplotlib.font_manager').setLevel(logging.ERROR)
```
suppresses the warning spam — even if glyphs are occasionally missing in
an edge case, it no longer ends up in the production log.

## Problem 3: Cooldown blocks too many SHORT signals

### Symptom in the log
In a ~3-minute log excerpt:
- **27× "Too many SHORT trades. Cooldown active."**
- **0× LONG blocks**
- At the same time: 9 new LONG signals, only 1 SHORT signal

### Root Cause
The cooldown in the classic strategies was:

```python
# Alt:
SELECT COUNT(*) FROM closed_trades_master
WHERE status = '1' AND direction = %s AND posted >= %s;
# Wenn > 250 Hits in 3h → Cooldown
```

Two problems:

1. **Only TP1 hits count** (`status = '1'`) — TP2/3/4 were ignored, so
   the "full wins" weren't counted at all. Aggregated across all TP
   levels, this gives a skewed picture.

2. **Threshold of 250 too low for 570 coins × 6 classic bots.** In a
   one-sided market (the whole world goes long or short) these numbers
   are quickly exceeded — and the bot then blocks EXACTLY the direction
   that's currently working. Anti-trend bias.

### Fix
In `strategies/strat_fast_in_out.py` and `strategies/strat_5_percent.py`:

```python
# Neu:
SELECT COUNT(*) FROM closed_trades_master
WHERE status IN ('1','2','3','4') AND direction = %s AND posted >= %s;
# Zählt ALLE erfolgreichen Closes (TP1-TP4)
```

And thresholds raised:
- `strat_fast_in_out.py`: 250 → **500**
- `strat_5_percent.py` LONG: 200 → **400**
- `strat_5_percent.py` SHORT: 250 → **500**

The asymmetric ratio in 5 Percent (LONG stricter than SHORT) is
preserved — only the absolute values were doubled.

### Tuning
If Michael wants the cooldown different, change it directly in the
strategy file:

```python
# strat_fast_in_out.py
def check_recent_trades(conn, direction, hours=3, count=500):
                                                      ^^^ hier ändern

# strat_5_percent.py, analyze_coin()
count = 400 if direction == 'LONG' else 500
        ^^^                            ^^^
```

Fully disable: `count=999999`
More aggressive: `count=300`
Even looser: `count=800` or higher

## Deploy

Overwrite these files:
```
C:\_BOTS\crypto_trading_bot_v2\23_market_tracker.py
C:\_BOTS\crypto_trading_bot_v2\core\charting.py
C:\_BOTS\crypto_trading_bot_v2\strategies\strat_fast_in_out.py
C:\_BOTS\crypto_trading_bot_v2\strategies\strat_5_percent.py
```

Restart the watchdog. Verify:

1. **Kelly post**: at the next XX:00:30, **multiple** messages arrive in
   sequence in the main channel — table + Kelly block(s). All visible,
   no more "Message is too long" in the log.

2. **CJK charts**: on signals for Chinese coins (e.g. 龙虾USDT) no more
   UserWarning appears in the log. Chart title shows the characters
   correctly (on Windows 11 with Microsoft YaHei).

3. **Cooldown**: significantly fewer "Cooldown active" entries in the
   log. In one-sided markets, trend continuations are no longer blocked.
