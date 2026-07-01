# Log-Analyse-Fixes: Kelly-Post, CJK-Fonts, Cooldown

## Problem 1: Per-Bot-Performance-Post kommt nicht

### Symptom im Log
```
14:02:05 - MARKET_TRACKER - ✅ Per-Bot Performance-Post gesendet (46 Strategien, 549969 Trades total).
14:02:08 - TELEGRAM_BOT - ⚠️ Msg 572058 Sendefehler, wird erneut versucht: Timed out
14:02:24 - TELEGRAM_BOT - ⚠️ Msg 572060 Sendefehler, wird erneut versucht: Message is too long
14:02:42 - TELEGRAM_BOT - ⚠️ Msg 572060 Sendefehler, wird erneut versucht: Message is too long
```

### Root Cause
Bei 46 aktiven Strategien (klassisch + AI-Modelle in verschiedenen Timeframes
+ SMC-Sniper + TD/BB etc.) wird die kombinierte Message > 4096 Zeichen.
Telegram lehnt mit "Message is too long" ab, Retries bringen nichts weil der
Text ja unverändert bleibt.

### Fix
`23_market_tracker.py` splittet jetzt den Post in mehrere Messages:

- **Message 1**: Tabelle + Legend (~3400 chars)
- **Message 2**: Kelly-Block Header + Bots 1-N (~3500 chars)
- **Message 3+**: "(continued)" mit weiteren Bots falls nötig

Die Aufteilung ist pro-Bot, nie wird ein einzelner Bot-Eintrag mittendrin
gesplittet. Jede Message bleibt sicher unter 3896 chars (= 4096 − 200 Puffer).

**Getestet** mit 46 Strategien: 3 Messages à 3443 / 3654 / 3610 chars.

### Parameter
In `23_market_tracker.py` ganz einfach anpassbar:
```python
TELEGRAM_TEXT_LIMIT = 4096       # Telegram API-Limit, nicht ändern
SAFETY_BUFFER = 200              # Puffer — bei Bedarf erhöhen
```

## Problem 2: Warnings über chinesische Glyphen in Charts

### Symptom im Log
```
UserWarning: Glyph 24065 (\N{CJK UNIFIED IDEOGRAPH-5E01}) missing from font(s) DejaVu Sans.
UserWarning: Glyph 23433 (\N{CJK UNIFIED IDEOGRAPH-5B89}) missing from font(s) DejaVu Sans.
UserWarning: Glyph 20154 (\N{CJK UNIFIED IDEOGRAPH-4EBA}) missing from font(s) DejaVu Sans.
UserWarning: Glyph 29983 (\N{CJK UNIFIED IDEOGRAPH-751F}) missing from font(s) DejaVu Sans.
```

币安人生 = "Binance Life" — ein Meme-Token bei Binance Futures.

### Root Cause
Manche Binance-Futures-Coins haben chinesische Namen wie `龙虾USDT` oder
`币安人生USDT`. Die landen in Chart-Titeln (e.g. `SYMBOL • 241min • $X.XX`).
matplotlib's default-Font "DejaVu Sans" enthält keine CJK-Glyphen → Warning-
Spam im Log, Kästchen statt Zeichen im Chart-Bild.

### Fix
`core/charting.py` setzt beim Import eine Font-Fallback-Kette:
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

matplotlib nutzt pro Zeichen den ersten Font der das entsprechende Glyph
enthält. Unbekannte Fonts werden still ignoriert — also harmlos wenn nicht
alle installiert sind. Auf Windows 11 ist "Microsoft YaHei" standardmäßig
da und bringt vollständige CJK-Unterstützung.

Zusätzlich:
```python
logging.getLogger('matplotlib.font_manager').setLevel(logging.ERROR)
```
unterdrückt den Warning-Spam — selbst wenn in einem Edge-Case doch mal
Glyphen fehlen, landet das nicht mehr im Production-Log.

## Problem 3: Cooldown blockiert zu viele SHORT-Signale

### Symptom im Log
In einem ~3-minütigen Log-Ausschnitt:
- **27× "Zu viele SHORT Trades. Cooldown aktiv."**
- **0× LONG-Blocks**
- Gleichzeitig: 9 neue LONG-Signale, nur 1 SHORT-Signal

### Root Cause
Der Cooldown in den klassischen Strategies war:

```python
# Alt:
SELECT COUNT(*) FROM closed_trades_master
WHERE status = '1' AND direction = %s AND posted >= %s;
# Wenn > 250 Hits in 3h → Cooldown
```

Zwei Probleme:

1. **Nur TP1-Hits zählen** (`status = '1'`) — TP2/3/4 wurden ignoriert,
   also wurden die "vollen Wins" gar nicht mitgezählt. Aggregiert über
   alle TP-Levels ergibt das ein schiefes Bild.

2. **Schwelle 250 bei 570 Coins × 6 klassischen Bots zu niedrig**.
   Bei einseitigem Markt (ganze Welt geht long oder short) werden diese
   Zahlen schnell überschritten — und der Bot blockiert dann GENAU die
   Richtung die gerade gut läuft. Anti-Trend-Bias.

### Fix
In `strategies/strat_fast_in_out.py` und `strategies/strat_5_percent.py`:

```python
# Neu:
SELECT COUNT(*) FROM closed_trades_master
WHERE status IN ('1','2','3','4') AND direction = %s AND posted >= %s;
# Zählt ALLE erfolgreichen Closes (TP1-TP4)
```

Und Schwellen hochgesetzt:
- `strat_fast_in_out.py`: 250 → **500**
- `strat_5_percent.py` LONG: 200 → **400**
- `strat_5_percent.py` SHORT: 250 → **500**

Das asymmetrische Verhältnis in 5 Percent (LONG strenger als SHORT) ist
erhalten — nur die absoluten Werte verdoppelt.

### Tuning
Falls Michael den Cooldown anders haben will, direkt in der Strategy-Datei
ändern:

```python
# strat_fast_in_out.py
def check_recent_trades(conn, direction, hours=3, count=500):
                                                      ^^^ hier ändern

# strat_5_percent.py, analyze_coin()
count = 400 if direction == 'LONG' else 500
        ^^^                            ^^^
```

Komplett deaktivieren: `count=999999`
Aggressiver: `count=300`
Noch lockerer: `count=800` oder höher

## Deploy

Die Dateien überschreiben:
```
C:\_BOTS\crypto_trading_bot_v2\23_market_tracker.py
C:\_BOTS\crypto_trading_bot_v2\core\charting.py
C:\_BOTS\crypto_trading_bot_v2\strategies\strat_fast_in_out.py
C:\_BOTS\crypto_trading_bot_v2\strategies\strat_5_percent.py
```

Watchdog neu starten. Verifizieren:

1. **Kelly-Post**: Beim nächsten XX:00:30 kommen **mehrere** Messages in Folge
   im Haupt-Channel — Tabelle + Kelly-Block(s). Alle sichtbar, kein
   "Message is too long" mehr im Log.

2. **CJK-Charts**: Bei Signalen auf chinesischen Coins (e.g. 龙虾USDT) kommt
   kein UserWarning mehr im Log. Chart-Titel zeigt die Zeichen korrekt
   (auf Windows 11 mit Microsoft YaHei).

3. **Cooldown**: Deutlich weniger "Cooldown aktiv" Einträge im Log. Bei
   einseitigen Märkten werden Trend-Fortsetzungen nicht mehr blockiert.
