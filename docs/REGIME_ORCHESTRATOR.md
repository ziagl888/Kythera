# Regime-Orchestrator — Technische Dokumentation

**Version**: 5.0 (zweidimensionale Klassifikation BTC-Regime × Alt-Context)  
**Stand**: April 2026  
**Autor**: Automatisch generiert

---

## Overview

Der Regime-Orchestrator ist ein Metasystem das über den bestehenden 25 Trading-Bots liegt. Er:

1. **Erkennt das Markt-Regime** alle 5 Minuten zweidimensional
2. **Filtert Bot-Signale** nach historischer Regime-Performance
3. **Leitet passende Signale** in einen dedizierten Cornix-Channel weiter
4. **Schließt automatisch Trades** bei Regime-Wechseln

Das System tradet **nicht selbst**. Es ist ein reiner Filter und Signal-Router.

### Warum zweidimensional?

```
Achse 1: BTC-Regime      Achse 2: Alt-Context
─────────────────────    ─────────────────────
TREND_UP                 ALT_STRONG (BTCDOM fällt)
TREND_DOWN               ALT_NEUTRAL
CHOP                     ALT_WEAK (BTCDOM steigt)
HIGH_VOLA
TRANSITION
```

Ohne Alt-Context-Achse wären zwei grundlegend verschiedene Szenarien identisch klassifiziert:

| BTC-Regime | Alt-Context | Szenario | Empfehlung |
|---|---|---|---|
| TREND_UP | ALT_STRONG | **Altseason** — Alts pumpen stärker als BTC | Alt-LONGs ideal |
| TREND_UP | ALT_WEAK | **BTC-Only-Pump** — Alts hinken hinterher | Alt-LONGs trügerisch |

---

## Architektur

```
26_regime_detector.py          (alle 5 Min)
  ↓ schreibt regime_history
  ↓ debounced → regime_current

27_bot_regime_analyzer.py      (stündlich)
  ↓ liest regime_history + closed trades
  ↓ schreibt bot_regime_performance
  ↓ schreibt bot_regime_whitelist

28_signal_orchestrator.py      (alle 500ms)
  ↓ liest telegram_outbox (neue Bot-Signale)
  ↓ prüft bot_regime_whitelist
  ↓ leitet durch → REGIME_TRADING_CHANNEL_ID
  ↓ trackt als ROM1 in ai_signals
  ↓ erkennt Regime-Wechsel → Close-Commands
```

---

## Prozesse

### `26_regime_detector.py`

**Was**: Klassifiziert alle 5 Minuten das BTC-Regime und den Alt-Context.  
**Wie**: Lädt BTCUSDT_15m + BTCDOMUSDT_15m, berechnet ATR/Returns, klassifiziert regelbasiert.  
**Output**:
- `regime_history` — jeder Check als line
- `regime_current` — debounced aktuelles Regime (Singleton)

**Wichtigste Konstanten** (am Datei-Anfang):
```python
CHECK_INTERVAL_SECONDS = 300          # alle 5 Minuten
TREND_RETURN_THRESHOLD_4H_PCT = 1.5   # > ±1.5% in 4h = Trend
CHOP_RETURN_THRESHOLD_4H_PCT = 0.5    # < ±0.5% in 4h = Chop
VOLA_HIGH_PERCENTILE = 75             # ATR > P75 = HIGH_VOLA
VOLA_LOW_PERCENTILE = 40              # ATR < P40 = Trend-/Chop-Zone
ALT_CONTEXT_THRESHOLD_PCT = 1.5       # |BTCDOM 24h| > 1.5% = Rotation
REGIME_DEBOUNCE_COUNT = 2             # 2 Checks = 10 Min Bestätigung
```

**Stündlicher Status-Post** (XX:00:50) in `REGIME_STATUS_CHANNEL_ID`:
```
🌡️ REGIME STATUS — 2026-04-18 14:00 UTC

BTC-Regime: CHOP (conf 85%)
Seit: 2026-04-18 11:25 UTC (2h 35min)
Alt-Context: ALT_NEUTRAL
...
```

### `27_bot_regime_analyzer.py`

**Was**: Berechnet für jeden Bot die historische Win-Rate in jedem (Regime × Alt-Context × Direction)-Kombination.  
**Wann**: Stündlich zu XX:05:00.  
**Output**:
- `bot_regime_performance` — Win-Rate, PnL-Stats pro (Bot, Regime, Alt, Direction, Window)
- `bot_regime_whitelist` — Boolean ob Bot in dieser 4D-Kombination durchgelassen wird

**Whitelist-Logik (zweistufig)**:

```
n < 30 Trades:
    → WHITELISTED (insufficient_data)

TREND_UP + SHORT oder TREND_DOWN + LONG (Counter-Trend):
    wr_bot ≥ 60% UND wr_bot ≥ overall + 10pp
    → WHITELISTED (counter_trend_specialist)
    sonst: GEBLOCKT (counter_trend_insufficient)

Alle anderen (Standard):
    wr_bot ≥ wr_overall
    → WHITELISTED (wr_above_overall)
    sonst: GEBLOCKT (wr_below_overall)
```

**Täglicher Cross-Table-Post** (07:00 UTC) in `REGIME_STATUS_CHANNEL_ID`:
```
📊 BOT × ALT-CONTEXT PERFORMANCE — TREND_UP (30d)

Bot          LONG                          SHORT
             ALT_W    ALT_N    ALT_S       ALT_W    ALT_N    ALT_S
MIS1-8h      45%↓     62%      71%↑        42%      47%      38%↓
...
```

### `28_signal_orchestrator.py`

**Was**: Liest `telegram_outbox`, filtert Bot-Signale, reicht passende durch.  
**Wann**: Alle 500ms.  
**Output**:
- Weitergeleitete Signale in `REGIME_TRADING_CHANNEL_ID`
- ROM1-Einträge in `ai_signals`
- Tracking in `orchestrator_open_trades`
- Unterdrückte Signale in `orchestrator_suppressed_signals`

**Overall-Fallback** (wenn Detektor unzuverlässig):
- `no_regime`: regime_current leer → Fallback auf ≥50% Overall-WR
- `regime_is_transition`: explizites TRANSITION → Fallback
- `regime_unstable`: ≥3 verschiedene Regimes in 2h → Fallback

---

## Datenbank-Tabellen

| Tabelle | Beschreibung | Schreiber |
|---|---|---|
| `regime_history` | Jeder 5-Min-Check | `26_regime_detector` |
| `regime_current` | Debounced aktuelles Regime (1 line) | `26_regime_detector` |
| `bot_regime_performance` | Win-Rate pro Bot/Regime/Alt/Direction/Window | `27_bot_regime_analyzer` |
| `bot_regime_whitelist` | Whitelist-Status pro Bot/Regime/Alt/Direction | `27_bot_regime_analyzer` |
| `orchestrator_open_trades` | Durchgereichte offene Trades | `28_signal_orchestrator` |
| `orchestrator_suppressed_signals` | Unterdrückte Signale (Log) | `28_signal_orchestrator` |

---

## Parameter-Tuning

### Wann ist ein Regime-Wechsel zu häufig?

Wenn die Fallback-Rate im Status-Post dauerhaft über 30% steigt (`regime_unstable`), sind die ATR-Schwellwerte zu sensitiv. Optionen:

1. `VOLA_HIGH_PERCENTILE` erhöhen (e.g. 80 statt 75) → HIGH_VOLA seltener
2. `REGIME_DEBOUNCE_COUNT` erhöhen (e.g. 3 statt 2) → 15 Min Bestätigung
3. `TREND_RETURN_THRESHOLD_4H_PCT` erhöhen (e.g. 2.0 statt 1.5) → strengere Trend-Erkennung

### Wann ist die Whitelist zu restriktiv?

Wenn viele Signale gefiltert werden und die ROM1-Performance nicht besser ist als die durchschnittliche Bot-Performance:

1. `COUNTER_TREND_MIN_WR_PCT` reduzieren (e.g. 55 statt 60)
2. `COUNTER_TREND_MIN_ADVANTAGE_PP` reduzieren (e.g. 7 statt 10)
3. `MIN_TRADES_FOR_DECISION` erhöhen (e.g. 50 statt 30) → mehr Bots bleiben in insufficient_data

### Alt-Context zu sensitiv?

Wenn ALT_STRONG/ALT_WEAK zu häufig ausgelöst wird:

1. `ALT_CONTEXT_THRESHOLD_PCT` erhöhen (e.g. 2.0 statt 1.5) → nur stärkere Rotationen triggern

---

## Troubleshooting

### `regime_history` füllt sich nicht

1. Prüfen ob `26_regime_detector.py` läuft: `ps aux | grep regime`
2. Log checken: `tail -f logs/REGIME_DETECTOR.log`
3. Prüfen ob `BTCUSDT_15m` Daten hat: `SELECT COUNT(*) FROM "BTCUSDT_15m"`
4. Prüfen ob `MIN_DATA_POINTS_15M` (480 Kerzen = 5 Tage) erfüllt ist

### `regime_current` wird nicht initialisiert

`regime_current` wird erst nach dem **zweiten** Check (DEBOUNCE_COUNT=2) gesetzt (10 Min). Das ist normal.

### Keine Signale im Trading-Channel

1. Ist `REGIME_TRADING_CHANNEL_ID` korrekt? Bot muss Admin im Channel sein.
2. Hat `bot_regime_whitelist` Einträge? → `27_bot_regime_analyzer --initial-run` ausführen
3. Ist das aktuelle Regime ein Fallback-Regime (TRANSITION)?
4. Log prüfen: `tail -f logs/SIGNAL_ORCHESTRATOR.log`
5. Unterdrückte Signale prüfen: `SELECT * FROM orchestrator_suppressed_signals ORDER BY ts DESC LIMIT 10`

### ROM1 erscheint nicht in Per-Bot-Performance

`8_ai_trade_monitor.py` übernimmt das Lifecycle-Tracking für ROM1. Erst nach dem ersten geschlossenen ROM1-Trade erscheint er in `closed_ai_signals` und damit in der Performance-Tabelle.

### Cornix reagiert nicht auf Signale

Cornix muss so konfiguriert sein, dass es **ausschließlich** `<CH_REGIME_TRADING>` als Signal-Quelle überwacht. Alle alten Bot-Channels müssen aus der Cornix-Config removed werden.

---

## Betrieb

### Neue Bots hinzufügen

Der Orchestrator erkennt Bots automatisch über:
1. Regex-Patterns im Signaltext (e.g. `MIS1`, `QM_BULL`)
2. Channel-ID-Mapping (`CHANNEL_TO_BOT_FALLBACK` in `28_signal_orchestrator.py`)

Nach dem Deployment eines neuen Bots: Nächster stündlicher Analyzer-Lauf berechnet automatisch seine Whitelist-Einträge.

### Manueller Regime-Override (Testing)

```sql
UPDATE regime_current SET regime = 'TREND_UP', alt_context = 'ALT_STRONG'
WHERE id = 1;
```

Der Orchestrator erkennt den Wechsel beim nächsten Loop (500ms) und führt Close-Commands aus.

### AUTO_CLOSE deaktivieren

```python
# In 28_signal_orchestrator.py:
AUTO_CLOSE_ON_REGIME_CHANGE = False
```

Nach Neustart des Prozesses: Regime-Wechsel werden noch erkannt und geloggt, aber keine Close-Commands gepostet.
