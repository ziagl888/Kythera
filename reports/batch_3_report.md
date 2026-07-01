# Batch 3 Report — Cooldown-Konsolidierung & ATB/Master/Forex

**Target files:** `14_ai_atb_bot.py`, `15_ai_master_bot.py`, `16_smc_forex_metals_bot.py`, `core/market_utils.py`

## Completed

### #33 — SMC Forex Cooldown-Check hatte Seiteneffekt (16_smc_forex_metals_bot.py)
Die alte `is_cooled_down`-Funktion hat den `INSERT ON CONFLICT`-Update-Befehl direkt beim Check ausgeführt — i.e. selbst wenn der nachfolgende `send_signal` gecrashed wäre, hätte die Tabelle bereits den Cooldown-Eintrag enthalten (Trade "gesendet" obwohl nie posted). Jetzt getrennt: `check_cooldown` (nur Lese-Query) vor dem Send, `update_cooldown` erst nach erfolgreichem Send.

### #34 — SMC Forex Cooldown-Keys ohne TF-Suffix (16_smc_forex_metals_bot.py)
Previously `module = f"SMC_{tf.upper()}_BOS"` → Dasselbe Coin/Direction konnte gleichzeitig auf `1h` und `4h` feuern (Dual-Signal). Jetzt nur `SMC_BOS` und `SMC_FVG` → TF-übergreifender Cooldown von 12h.

### #51 — Cooldown-Konsolidierung
Zwei eigene Cooldown-Implementierungen removed und durch `core.market_utils.check_cooldown` + `update_cooldown` ersetzt:
- `14_ai_atb_bot.py`: `is_cooled_down()` + `set_cooldown()` → `check_cooldown()` + `update_cooldown()`
- `16_smc_forex_metals_bot.py`: `is_cooled_down()` (vermischter Check+Update) → getrennte `check_cooldown`/`update_cooldown`-Calls

This means existieren jetzt zentral nur noch die market_utils-Helper. Alle Bots nutzen die gleiche Timezone-bewusste Logik, die gleichen DB-Tabellen, und die gleiche Fehlerbehandlung.

### #28 — Master Bot symbol-cleanup robuster (15_ai_master_bot.py)
An zwei Stellen: `str.replace('_.*', '', regex=True).str.replace('USDT', '', regex=False) + 'USDT'` durch `str.replace(r'_\d+[mhdwM]$', '', regex=True)` ersetzt. Die alte Logik war "selbstheilend" für Standard-Coins (removed USDT und hängt es wieder an), aber fragil bei hypothetischen Edge-Cases wie Coins mit `USDT` im Namensinnern. Die neue Regex matcht **nur** den Timeframe-Suffix am Ende (e.g. `_1h`, `_4h`, `_30m`, `_1d`, `_1w`, `_1M`) und lässt den eigentlichen Coin-Namen unberührt. Verifiziert gegen 12 Testfälle.

## Bereits erledigt

### #42 — Mayank asset cooldown
Der Mayank Bot wurde in **Batch 1** (Fix #35) bereits auf `module_tag = f"MAYANK_{symbol_name}_{tf.upper()}"` migrated. Das ist bereits Cooldown per Asset + TF + Direction. Keine zusätzliche Änderung erforderlich.

## Verification
- Alle 4 geänderten Dateien parse cleanly
- Projektweit keine verbleibenden Calls auf die alten `is_cooled_down`/`set_cooldown`-Funktionen (verifiziert via grep)
- Master-Bot-Regex gegen 12 Testfälle verifiziert (Standard-Coins + Edge-Cases)

## Recommendations für späteren Review

- Die ATB-Bot-Datei hat noch eine tote Kommentar-Stelle bei `set_cooldown`-Entfernung. Nicht kritisch, aber bei einem späteren Cleanup-Durchgang aufräumen.
- Die SMC-Forex-Cooldown-Dauer (12h) ist jetzt TF-übergreifend. Falls das zu restriktiv sein sollte (weniger Signale als zuvor), kann die Dauer reduziert werden (`check_cooldown(conn, cd_key, display_name, 'LONG', 12)` → `8` oder `6`).
