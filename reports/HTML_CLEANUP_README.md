# Systemweiter HTML-Cleanup: Telegram-API-Konformität

## Was wurde gefixt

Alle Bot-Dateien die HTML-Nachrichten an Telegram senden hatten `style="..."`-Attribute in ihren Tags. Diese sind laut Telegram Bot API **offiziell nicht erlaubt** und wurden nur durch Nachsichtigkeit der Parser toleriert. Bei komplexeren Messages mit vielen verschachtelten Tags triggerten sie stille Parse-Fehler — die Messages wurden dann als `failed` markiert und erschienen nie.

**Akut gefixtes Problem**: Der stündliche Per-Bot-Performance-Post mit Kelly-Sizing wurde nicht gerendert.

**Präventiv gefixt**: Alle anderen Bot-Posts die das gleiche Pattern nutzten. Aktuell funktionieren sie zwar, aber jeder Telegram-Client-Update könnte sie brechen.

## Telegram-API-HTML-Regel (aus Bot API Doku)

Erlaubte Tags:
```
<b>, <strong>, <i>, <em>, <u>, <ins>, <s>, <strike>, <del>,
<code>, <pre>, <a>, <span>, <tg-spoiler>, <blockquote>
```

Erlaubte Attribute:
- `href="..."` bei `<a>` (Pflicht)
- `class="tg-spoiler"` bei `<span>` (nur dieser eine Wert)
- `class="language-xxx"` bei `<code>` innerhalb `<pre>`

**Alles andere ist verboten** — besonders:
- `style="..."` (e.g. `style="color:red; font-size:16px"`)
- `class="..."` mit anderen Werten
- `font-family`, `background`, `border-left`, `padding-left` etc. als Attribute

## Was genau geändert wurde

Alle `style="..."` und `style='...'`-Attribute wurden aus allen Tags removed. Die Tags selbst bleiben identisch, nur die Attribute verschwinden.

**Beispiel vor dem Fix** (aus `11_ai_mis_bot.py`):
```html
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; 
font-family:'Courier New'; font-size:15px; border-left:6px solid #00ff00;">
<b style="color:#00ffff; font-size:18px;">💎 AI MIS TRADE</b>
<b style="color:#ffd700;">BTC/USDT</b>
<b>→ Direction: <b style="color:#00ff00;">LONG</b></b>
</pre>
```

**Nach dem Fix**:
```html
<pre>
<b>💎 AI MIS TRADE</b>
<b>BTC/USDT</b>
<b>→ Direction: <b>LONG</b></b>
</pre>
```

Die **Formatierung im Chat** (fett, preformatted) bleibt identisch — Telegram rendert `<b>` und `<pre>` nativ. **Weg sind nur die Farben** — die hätten sowieso nie angezeigt werden dürfen (Telegram-App ignoriert sie).

## Welche Dateien wurden geändert

| Datei | Style-Tags removed |
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
| **Gesamt** | **135** |

Nicht in diesem ZIP, aber ebenfalls betroffen (und sollte später auch gesäubert werden):
- `7_pattern_detector.py` (geringere Priorität, nur 1 Tag)
- `22_ip_pattern_bot.py` (derzeit deaktiviert im Watchdog)
- `dashboard.py` (nicht Telegram-relevant — Web-Dashboard)
- `core/charting.py` (nicht Telegram-relevant — matplotlib-Farben)

## Wie verifiziert wurde

- Alle 14 Dateien wurden durch einen strengen Auditor gejagt der **jedes** Tag-Attribut gegen die API-Whitelist prüft
- Result: **0 verbotene Attribute** übrig
- Python-Syntax aller Dateien ist weiterhin valide
- Funktions-Signaturen und -Logik sind unverändert — nur die HTML-Strings wurden gestutzt

## Deploy

Alle Dateien aus dem ZIP nach `C:\_BOTS\crypto_trading_bot_v2\` überschreiben:

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

Dann Watchdog neu starten. Alle Bot-Messages werden weiterhin identisch formatiert erscheinen (fett, preformatted), nur die Per-Bot-Performance-Post rendert jetzt zuverlässig.

## Git-Commit

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

## Falls nach Deploy etwas nicht mehr aussieht wie erwartet

Unwahrscheinlich aber möglich: Ein HTML-String hätte durch den Regex-Replace einen
doppelten Leerzeichen bekommen könnten. Falls du irgendwo komische Formatierung
siehst (doppelte Leerzeichen in Tag-Attributen), sag Bescheid und ich korrigiere
den konkreten Fall.
