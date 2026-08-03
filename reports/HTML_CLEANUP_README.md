# System-wide HTML cleanup: Telegram API compliance

## What was fixed

All bot files that send HTML messages to Telegram had `style="..."` attributes on their tags. These are **officially not allowed** per the Telegram Bot API and were only tolerated by lenient parsers. On more complex messages with many nested tags they triggered silent parse failures — the messages were then marked as `failed` and never appeared.

**Acute problem fixed**: The hourly per-bot performance post with Kelly sizing was not rendering.

**Preventively fixed**: All other bot posts that used the same pattern. They currently work, but any Telegram client update could break them.

## Telegram API HTML rule (from Bot API docs)

Allowed tags:
```
<b>, <strong>, <i>, <em>, <u>, <ins>, <s>, <strike>, <del>,
<code>, <pre>, <a>, <span>, <tg-spoiler>, <blockquote>
```

Allowed attributes:
- `href="..."` on `<a>` (required)
- `class="tg-spoiler"` on `<span>` (only this one value)
- `class="language-xxx"` on `<code>` inside `<pre>`

**Everything else is forbidden** — in particular:
- `style="..."` (e.g. `style="color:red; font-size:16px"`)
- `class="..."` with other values
- `font-family`, `background`, `border-left`, `padding-left` etc. as attributes

## What exactly was changed

All `style="..."` and `style='...'` attributes were removed from all tags. The tags themselves stay identical, only the attributes disappear.

**Example before the fix** (from `11_ai_mis_bot.py`):
```html
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; 
font-family:'Courier New'; font-size:15px; border-left:6px solid #00ff00;">
<b style="color:#00ffff; font-size:18px;">💎 AI MIS TRADE</b>
<b style="color:#ffd700;">BTC/USDT</b>
<b>→ Direction: <b style="color:#00ff00;">LONG</b></b>
</pre>
```

**After the fix**:
```html
<pre>
<b>💎 AI MIS TRADE</b>
<b>BTC/USDT</b>
<b>→ Direction: <b>LONG</b></b>
</pre>
```

The **formatting in chat** (bold, preformatted) stays identical — Telegram renders `<b>` and `<pre>` natively. **Only the colours are gone** — they should never have been displayed anyway (the Telegram app ignores them).

## Which files were changed

| File | Style tags removed |
|---|---|
| `23_market_tracker.py` | 28 |
| `11_ai_mis_bot.py` | 21 |
| `25_smc_ml_sniper.py` | 21 |
| `10_pump_dump_detector.py` | 23 |
| `17_mayank_bot.py` | 8 |
| `14_ai_atb_bot.py` | 8 |
| `13_ai_rub_bot.py` | 5 |
| `15_ai_master_bot.py` | 5 |
| `12_ai_ats_bot.py` | 4 |
| `16_smc_forex_metals_bot.py` | 4 |
| `18_ai_abr1_bot.py` | 4 |
| `20_funding_logger_bot.py` | 2 |
| `19_whale_logger_bot.py` | 1 |
| `24_quasimodo_bot.py` | 1 |
| **Total** | **135** |

Not in this ZIP, but also affected (and should be cleaned up later too):
- `7_pattern_detector.py` (lower priority, only 1 tag)
- `22_ip_pattern_bot.py` (currently disabled in the watchdog)
- `dashboard.py` (not Telegram-relevant — web dashboard)
- `core/charting.py` (not Telegram-relevant — matplotlib colours)

## How it was verified

- All 14 files were run through a strict auditor that checks **every** tag attribute against the API whitelist
- Result: **0 forbidden attributes** left
- Python syntax of all files is still valid
- Function signatures and logic are unchanged — only the HTML strings were trimmed

## Deploy

Overwrite all files from the ZIP into `C:\_BOTS\crypto_trading_bot_v2\`:

```
C:\_BOTS\crypto_trading_bot_v2\10_pump_dump_detector.py
C:\_BOTS\crypto_trading_bot_v2\11_ai_mis_bot.py
C:\_BOTS\crypto_trading_bot_v2\12_ai_ats_bot.py
C:\_BOTS\crypto_trading_bot_v2\13_ai_rub_bot.py
C:\_BOTS\crypto_trading_bot_v2\14_ai_atb_bot.py
C:\_BOTS\crypto_trading_bot_v2\15_ai_master_bot.py
C:\_BOTS\crypto_trading_bot_v2\16_smc_forex_metals_bot.py
C:\_BOTS\crypto_trading_bot_v2\17_mayank_bot.py
C:\_BOTS\crypto_trading_bot_v2\18_ai_abr1_bot.py
C:\_BOTS\crypto_trading_bot_v2\19_whale_logger_bot.py
C:\_BOTS\crypto_trading_bot_v2\20_funding_logger_bot.py
C:\_BOTS\crypto_trading_bot_v2\23_market_tracker.py
C:\_BOTS\crypto_trading_bot_v2\24_quasimodo_bot.py
C:\_BOTS\crypto_trading_bot_v2\25_smc_ml_sniper.py
```

Then restart the watchdog. All bot messages will keep appearing formatted identically (bold, preformatted); only the per-bot performance post now renders reliably.

## Git commit

```bash
cd <projekt>
git add 10_pump_dump_detector.py 11_ai_mis_bot.py 12_ai_ats_bot.py \
        13_ai_rub_bot.py 14_ai_atb_bot.py 15_ai_master_bot.py \
        16_smc_forex_metals_bot.py 17_mayank_bot.py 18_ai_abr1_bot.py \
        19_whale_logger_bot.py 20_funding_logger_bot.py 23_market_tracker.py \
        24_quasimodo_bot.py 25_smc_ml_sniper.py
git commit -m "chore: remove non-API-compliant style attributes from all Telegram HTML

Telegram Bot API does not permit style= attributes on HTML tags. They
were silently tolerated by lenient parsers but caused parse failures
on complex messages with many nested tags (Per-Bot-Performance-Post).

Removes 135 style attributes across 14 bot files. No visible formatting
change for users - Telegram never rendered these style values anyway."
git push
```

## If something doesn't look as expected after deploy

Unlikely but possible: an HTML string could have picked up a double space through the regex replace. If you see odd formatting anywhere (double spaces in tag attributes), let me know and I'll fix the specific case.
