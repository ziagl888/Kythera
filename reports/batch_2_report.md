# Batch 2 Report — AI-Bot Signal-Quality

**Target files:** `10_pump_dump_detector.py`, `11_ai_mis_bot.py`, `12_ai_ats_bot.py`, `13_ai_rub_bot.py`, `14_ai_atb_bot.py`, `18_ai_abr1_bot.py`

## Completed

### #17 — RUB Bot Cooldown vor ML-Prediction (13_ai_rub_bot.py)
`check_cooldown()` is now vor `predict_proba()` aufgerufen. Bei 500 Coins × mehreren Event-Typen spart das deutlich CPU, wenn die meisten Coins im Cooldown stehen. Der Shadow-Log für abgelehnte Trades (prob < threshold) läuft weiterhin nach der Prediction.

### #20 — ATB NaN/Inf-Absicherung (14_ai_atb_bot.py)
Zusätzliches `replace([inf, -inf], nan).fillna(0)` nach dem Feature-Bau. Das eigentliche Refactoring (Indikatoren aus DB statt pandas_ta) habe ich NICHT gemacht — dokumentiert mit Begründung: Da das ML-Modell bereits auf pandas_ta-Werten trainiert wurde, würde ein Wechsel zu DB-Werten die Feature-Semantik ändern und erfordert Re-Training.

### #24 — RUB get_f robuster (13_ai_rub_bot.py)
`get_f()` prüfte vorher nur auf `None`. Now auch auf NaN/Inf geprüft, defensive `float()`-Konvertierung mit Fallback. Verhindert Crashes bei frischen Coins mit Warmup-Phase.

### #25 — ABR1 defensive Features (18_ai_abr1_bot.py)
`X_event` wird vor `predict_proba()` via `replace([inf, -inf], nan).fillna(0)` bereinigt.

### #27 — MIS1 Threshold-Loading loggen (11_ai_mis_bot.py)
Beim Laden der Modelle is now explizit geloggt welche Thresholds aus den separaten pkl-Files übernommen wurden. This means fällt Drift zwischen Modell-Version und Threshold-Datei sofort auf. Keine Code-Änderung an der Lade-Logik selbst — sie ist bereits korrekt, nur besser sichtbar.

### #74 — ABR1 SUCCESS_CLASS_IDX dokumentieren (18_ai_abr1_bot.py)
Der Wert `SUCCESS_CLASS_IDX=0` wurde mit einem ausführlichen Kommentar versehen: Standard-Konvention wäre `1`, hier ist aber `0` aus historischen Gründen. **Die tatsächliche Korrektheit muss gegen das Training-Notebook verifiziert werden** — wenn das Modell auf `y=1=success` trainiert wurde, MUSS hier `1` stehen. Ich kann das ohne Zugriff aufs Training nicht entscheiden.

### #75 — ABR1 asymmetrische Thresholds dokumentieren (18_ai_abr1_bot.py)
Kommentar zur Begründung der LONG=0.60/SHORT=0.80-Asymmetrie (historisch mehr False Positives bei SHORT-Setups in Bull-Phasen).

### #76 — ABR1 minute-Filter removed (18_ai_abr1_bot.py)
Der Filter `retest_candle['open_time'].minute != 0` war wirkungslos weil 1h-Kerzen IMMER `minute == 0` haben. Die aktuelle (laufende) Kerze wird bereits in line 219 via `df = df[df['open_time'] < current_hour_utc]` abgeschnitten, der Filter war also redundant. Entfernt + Kommentar zur Klarstellung.

## Kein Bug / als false alarm eingeordnet

### #39 — Pump/Dump Volumen-Bestätigung
Nach genauerem Review: Der preisbasierte Alert in Block A) des Detectors ist bewusst ein **Market-Notification-Alert** (e.g. für News-Events und schnelle Bewegungen), nicht ein Trade-Signal. Eine zusätzliche Volumen-Bestätigung würde die Sensitivität reduzieren und den Use-Case verändern. Der ML-Teil in Block B) hat bereits Volumen-Features im Modell integriert.

### #40 — MIS1 nutzt nur 1h
false alarm meiner ursprünglichen Analyse: MIS1 verarbeitet 1h-OHLCV-Daten, aber testet **alle 8 Horizon-Modelle** (8h/24h/72h/168h × pump/dump) gegen diese Daten und wählt das beste (siehe `for horizon, cfg in PUMP_MODELS.items()` + `candidates.sort`). Genau das ist das designte Verhalten.

## Deferred

### #28 — Master Bot symbol_cleanup-Regex
Gehört zu `15_ai_master_bot.py` → wird in Batch 3 behandelt.

## Verification
Alle 6 Dateien parse cleanly.

## Recommendations für späteren Review

- **ABR1 SUCCESS_CLASS_IDX**: Bitte **manuell gegen das Training-Notebook verifizieren**. Wenn dort `y=1` für gewinnende Trades steht, muss hier auf `1` geändert werden. Der aktuelle Wert `0` ist nur sicher wenn explizit `y=0` als "success" trainiert wurde.
- **ATB Indikatoren aus DB**: Mittelfristig sinnvoll, aber nur bei einem Retraining des Modells. Als eigenständiger Fix zu riskant.
