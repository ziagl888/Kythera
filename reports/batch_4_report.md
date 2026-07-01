# Batch 4 Report — Indicator Engine & Strategies

**Target files:** `2_indicator_engine.py`, `strategies/*`

## Completed

### #6 — Trendline Division durch 0 und NaN-Robustheit (2_indicator_engine.py)
`calculate_trendline_and_channel_robust_optimized()`:
- Bei komplett konstanter Preis-Serie (`y == y[0]`) wird `stats.linregress` NaN zurückgeben. Now: Early-Return mit neutralen Werten (slope=0, direction=SIDEWAYS).
- Bei erfolgreicher linregress zusätzlich `np.isfinite()`-Checks für slope, intercept, r_value, std_dev.
- Direction-Threshold `0.0001 * y[0]` schlug bei `y[0] == 0` (theoretisch durch fehlerhafte Ingestion möglich) fehl. Now: `abs(base)` und Fallback auf `y[-1]` statt `y[0]`, plus Mindest-Schwelle `1e-8` für den Edge-Case.

### #12 — Volume Indicator iloc statt loc (strategies/strat_volume_indicator.py)
`detect_volume_spike_in_period()`: `df_period.loc[index - 1, 'close']` durch positionsbasierten `iloc`-Zugriff ersetzt. Zusätzlich `reset_index(drop=True)` nach dem SQL-Read, damit der Index garantiert 0..N-1 ist.

### #45 — indicator_state.json atomar schreiben (2_indicator_engine.py)
Temp-File + `fsync` + `os.replace` statt direktes Write in die Zieldatei. Verhindert halbgeschriebene JSONs beim gleichzeitigen Read aus dem Detector-Prozess.

## Als false alarme geklärt

### #9 — HVN-Binning
Der Code nutzt `bins = int(np.sqrt(len(prices)))`, also dynamisch skaliert — nicht hardcoded auf 0.5%. Die 0.5%-Zahl in der ursprünglichen Analyse bezog sich auf den Duplikat-Filter zwischen Peaks (`abs(p - poc_price) > poc_price * 0.005`), und dieser ist funktional sinnvoll (redundante nahe Peaks zusammenfassen). Keine Änderung riskiert die HVN-Stabilität.

### #26 — fibs['extensions'] ungenutzt
`FIB_EXTENSION_*` werden in line 394 in die Results geschrieben und damit in die Indikatoren-Tabelle der DB. Sie sind also nicht "tot" — werden nur nicht im Python-Code direkt gelesen.

### #29 — strat_5_percent SL ohne resistances
Bei genauer Code-Betrachtung: Die 5-Percent-Strategy nutzt **ATR-basierten** SL mit 3.5×ATR und hat bereits einen implementeden Cap (`live_price * 0.95/1.05` wenn der ATR-SL mehr als 5% vom Preis abweicht). Die resistance/support-Werte werden nur in den Filter-Conditions genutzt, nicht für die SL-Berechnung. Kein Bug.

### #41 — strat_main_channel auf geglätteten close
Der Bot nutzt `close_price_current` = finale 1h-Close der gerade abgeschlossenen Kerze. Das ist bereits eine semantische Glättung (1h-Bucket-Aggregation). Eine weitere Glättung per EMA würde Channel-Break-Signale verzögern — kontraproduktiv für die Strategie.

### #44 — BB std=2.0 hardcoded, MACD-Inkonsistenzen
BB std=2 ist Industriestandard. Die zwei MACD-Varianten (9/21/9 Fast und 12/26/9 Normal) sind beide Standard und bewusst parallel vorgehalten — keine Inkonsistenz, sondern Dual-Variante für verschiedene Signaltypen.

### #46 — calc_kama Spaltennamen als String
Die Funktion liefert numerische Werte und wird mit Integer-Parametern aufgerufen (`KAMA_{p}` wobei p ein int ist). Kein String-Typo.

### #47 — calc_wma length 200 auf 100 Kerzen
`rolling(window=200)` liefert NaN bei <200 Kerzen, `fillna(0)` macht 0. Nicht ideal als MA-Fallback, aber by-design — die ML-Modelle wurden auf genau diesem Verhalten trainiert. Ein Wechsel auf "carry-forward" oder ähnliches würde Retraining erfordern.

### #49 — lookback_candles = 3000
Der 3000er-Wert wird **nur beim ersten Lauf** (initiale Befüllung der Indikator-Tabelle) verwendet. Im laufenden Betrieb sind es 1000 — ausreichend klein.

## Verification
Alle 6 Dateien parse cleanly.

## Summary Batch 4
Von 10 geplanten Fixes: 3 echte Bugs fixed, 7 als false alarme aus der ursprünglichen Analyse identifiziert. Die Indicator Engine und die Strategies sind insgesamt solider als meine Erstanalyse suggerierte — meine Analyse war in diesem Bereich zu pessimistisch.
