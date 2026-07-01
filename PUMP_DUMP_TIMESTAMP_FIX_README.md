# Pump-Dump-Detector: Fix für falsche Spike-Zeitstempel nach Restart

## Symptom

Der Pump-Dump-Detector hat nach Restarts Messages gepostet wie:

```
💥 DUMP DETECTED
SPACE/USDT
→ −6.37% in 2m 0s
→ Spike: 02:22:43 → 05:40:19 UTC
```

Das ist unmöglich: Die Prozent-Zeile sagt "in 2 Minuten", aber der Spike-Range
im Label zeigt **3 Stunden 17 Minuten**.

## Root Cause

In `process_coin_logics` wurde für die Pump/Dump-Erkennung **Index-basiert**
auf die Bucket-Liste zugegriffen:

```python
chg_pct = (current_price / prices[-lookback] - 1) * 100
spike_window = data[-lookback:]
```

Der Code nahm an: "Bucket-Index -12 = vor 120 Sekunden".

**Das gilt nur im steady-state**. Nach einem Neustart wird die Deque aus
`1minute.json` mit bis zu **1440 alten Einträgen** geladen (max 4 Stunden alte
Daten). Wenn dann frische Buckets reinkommen, mischen sich alt und neu:

```
[bucket@01:45, bucket@01:45:10, ..., bucket@03:00,          ← 450 alte Einträge
 bucket@05:40, bucket@05:40:10, bucket@05:40:20]             ← 3 frische Einträge
                                               ↑ data[-1]
                                     ↑ data[-12]  ← zeigt bucket@03:00 !
```

- `data[-1]` ist frisch (05:40:20)
- `data[-12]` zeigt auf **03:00** — einen Bucket der **fast 3 Stunden alt ist**
- Die `chg_pct`-Berechnung vergleicht frischen Preis mit 3h-alten Preis → falsche Prozente
- `spike_window = data[-12:]` enthält die Lücke zwischen 03:00 und 05:40
- `spike_prices.index(min(...))` findet den niedrigsten Wert aus dem alten Bereich
- Zeitstempel 02:22 landet im Label obwohl "2m 0s" ausgegeben wird

## Fix

**Alle Lookbacks wurden von index-basiert auf zeitstempel-basiert umgebaut.**

Neue Helper-Funktionen in `10_pump_dump_detector.py`:

```python
def _parse_bucket_ts(entry): ...
def _find_bucket_before(data, now, seconds_ago, tolerance=20): ...
def _find_bucket_range(data, now, seconds_ago, tolerance=20): ...
```

Statt `prices[-12]` nutzt der Code jetzt `_find_bucket_before(data, now, 120, tolerance=20)`.
Wenn kein Bucket im Zeitfenster `[120-20, 120+20]` Sekunden existiert
(= Daten-Lücke), wird der Lookback **übersprungen** und die nächste Stufe
probiert. Das verhindert falsche Alerts nach Neustarts.

Zusätzlich: **Stale-Data-Check am Anfang**:

```python
if latest_age_sec > 60:
    logger.debug(f"{symbol}: stale data ({latest_age_sec:.0f}s alt), überspringe")
    return
```

Wenn der neueste Bucket älter als 60 Sekunden ist (= Prozess gerade gestartet
oder WS-Ausfall), wird der ganze Cycle übersprungen. Beim nächsten Tick
(10 Sekunden später) ist der neueste Bucket dann wieder frisch.

Sanity-Check für den Spike-Start:

```python
if spike_start_dt is not None:
    age_sec = (now - spike_start_dt).total_seconds()
    if age_sec > seconds_back * 2 or age_sec < 0:
        logger.warning(f"{symbol}: spike_start inkonsistent...")
        spike_start_dt = None
        spike_time_label = None
```

Falls doch irgendwie ein inkonsistenter Timestamp durchrutscht, wird das
Spike-Label unterdrückt statt eine falsche Angabe zu posten.

## Was NICHT mehr passiert

Die alten Symptome sind jetzt ausgeschlossen:

- ❌ "−6.37% in 2m 0s" mit Spike-Range über 3h → der Spike-Start wird nur
  noch aus dem tatsächlichen Zeitfenster genommen
- ❌ Falsche Prozent-Berechnung gegen 4h alte Preise → Bucket wird nicht
  gefunden, Lookback übersprungen
- ❌ Wilde Post-Flut nach Neustart → Stale-Data-Check verhindert Alerts
  solange keine frischen Daten da sind

## Getestet

Drei Szenarien durchgetestet:

1. **Normaler Betrieb**: Funktioniert unverändert — Bucket vor 120s wird
   zuverlässig gefunden
2. **Nach Restart mit alten Cache-Daten**: `find_before(120s)` liefert
   korrekt `None`, Alert wird übersprungen
3. **Stale-Data-Check**: Bei 4h alten Daten returned `process_coin_logics`
   sofort ohne Alert

## Deploy

Nur eine Datei überschreiben:
```
C:\_BOTS\crypto_trading_bot_v2\10_pump_dump_detector.py
```

Watchdog neu starten. Beim nächsten Neustart-Test:

1. Detector anhalten
2. System ~30 Minuten pausieren lassen (bis `1minute.json` alte Daten enthält)
3. Detector starten
4. Logs beobachten: sollten "stale data" debug-Einträge zeigen, keine
   falschen "DUMP DETECTED" mit 4h-Spike-Range

## Was sich für Benutzer ändert

Im Normalbetrieb: **nichts**. Die Alerts kommen genauso wie vorher, nur mit
korrekten Spike-Zeitstempeln.

Nach Restarts gibt es eine kurze Phase (~30-120 Sekunden) in der keine
Pump/Dump-Alerts gepostet werden — bis genug frische Buckets gesammelt sind.
Das ist eine feature, kein bug.
