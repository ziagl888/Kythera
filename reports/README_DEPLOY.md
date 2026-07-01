# Regime-Bot Kelly/WR Fix + Follow-ups

## What is being fixed

### (1) Kelly/WR-Fix — main issue

Drei Bugs in der Performance-Erfassung des Regime-Orchestrators, die dazu
caused Bots mit echter 57% Win-Rate als 0.28% angezeigt wurden:

1. **LEGACY TARGET HIT schreibt targets_hit=0** — der AI-Trade-Monitor setzt
   bei Legacy-Trades mit +2.5% PnL `close_reason="LEGACY TARGET HIT"`, aber
   leaves `targets_hit` at 0. Die alte `targets_hit >= 1`-Logik klassifiziert
   diese Wins incorrectly as losses.

2. **DELISTED / CLEANUP zählt als Loss** — bei Symbol-Delisting wird der
   Trade zwangsgeschlossen mit `targets_hit=0`. DELISTED ist weder Win
   noch Loss.

3. **Extreme PnL-Ausreißer** — vereinzelte Trades mit PnL > 100% oder
   < -100% deuten auf Daten-Fehler hin und verzerren avg_win/avg_loss massiv.

**Fix**: PnL-basierte Klassifikation jedes Trades in **win**, **loss**
oder **neutral**. Neutrale Trades are excluded from performance stats.

### (2) SQL-Crash bei 'SL1'-Status

`closed_trades_master.status` enthält teilweise Non-Integer-Werte (e.g.
`"SL1"`, vermutlich aus Legacy-Bots oder manuellen DB-Edits). Die alte
Query `t.status::int > 0` crashte damit. Konsequenz: **keine klassischen
Trades wurden geladen**, die ganze Performance-Analyse blieb leer.

**Fix**: `is_win` wird nicht mehr aus SQL geladen (wurde sowieso von
der Python-Klassifikation überschrieben). Stattdessen liefern die Queries
einen Platzhalter `0 AS is_win`. Der `status`-String wird direkt als
`close_reason` durchgereicht — die Python-Klassifikation entscheidet dann
robust anhand von PnL + Reason-Keywords, egal welcher String drin steht.

### (3) Pandas UserWarning

`pandas.read_sql_query` warns about psycopg2 connections mit einer
UserWarning. Der Code läuft trotzdem korrekt, die Warning ist kosmetisch.
**Fix**: Warning per `warnings.filterwarnings` am Dateianfang
unterdrückt, konsistent mit `10_pump_dump_detector.py`.

### (4) Vertikale Linien im Pump-Dump-Chart removed

Die Spike-Region-Markierung (zwei vertikale Linien + schattierte Fläche)
wurde ursprünglich implemented, um visuell zu verifizieren dass die Bucket-
Timestamp-Logik nach einem früheren Bug korrekt arbeitet. Der Bug ist
inzwischen gefixt und die Logik validiert — die visuelle Bestätigung ist
nicht mehr nötig.

**Fix**: Spike-Rendering in `core/charting.py` deaktiviert. Die
Funktionssignatur (`spike_start`/`spike_end`/`spike_time` Parameter)
bleibt erhalten für Backwards-Kompatibilität. Der Aufruf aus
`10_pump_dump_detector.py` muss nicht geändert werden.

## Geänderte Dateien

| Datei | Änderung |
|---|---|
| `27_bot_regime_analyzer.py` | Outcome-Klassifikation, SQL robust gegen Non-Int status, UserWarning-Suppression |
| `28_signal_orchestrator.py` | PnL-basierter Lifecycle-Sync mit `CLOSED_NEUTRAL`-Status |
| `core/charting.py` | Spike-Marker (Linien+Region) deaktiviert |
| `backtest/test_bot_regime_analyzer.py` | 10 neue Outcome-Tests + EPD1-E2E |
| `backtest/test_signal_orchestrator.py` | 9 neue Klassifikations-Tests |

**Keine Schema-Migration nötig.** `orchestrator_open_trades.status` ist
TEXT ohne CHECK-Constraint, akzeptiert `CLOSED_NEUTRAL` direkt.

## Deploy

```bash
cd C:\Users\Michael\PycharmProjects\crypto_trading_bot_v2

# Backup (empfohlen)
copy 27_bot_regime_analyzer.py 27_bot_regime_analyzer.py.bak
copy 28_signal_orchestrator.py 28_signal_orchestrator.py.bak
copy core\charting.py core\charting.py.bak

# Neue Dateien einspielen, dann Analyzer neu berechnen:
python 27_bot_regime_analyzer.py --initial-run
```

Der `--initial-run` berechnet `bot_regime_performance` komplett neu.
Ohne diesen Schritt bleiben die alten (falschen) Zahlen bis der stündliche
Lauf sie überschreibt (bis zu 24h).

## Kontrolle

Nach dem Deploy per SQL prüfen:

```sql
-- WRs sollten jetzt realistisch sein (nicht mehr ~0%)
SELECT bot_name, direction, n_trades, win_rate
FROM bot_regime_performance
WHERE regime = 'ALL' AND alt_context = 'ALL'
ORDER BY bot_name, direction
LIMIT 30;
```

Erwartung EPD1: `win_rate` ≈ 57-58% (statt vorher ≈ 0%).

## Validation (vor Auslieferung durchgeführt)

- **14/14** Analyzer-Klassifikations-Tests grün
- **14/14** Orchestrator-Klassifikations-Tests grün
- **EPD1-Simulation**: 70.303 Input → 65.668 decisive → **WR 57.84%**
- SQL-Crash fixed: `'SL1'` und andere Non-Int-Strings landen
  jetzt in `close_reason` und werden PnL-basiert klassifiziert

## Offen (nicht in diesem Paket)

- **`8_ai_trade_monitor.py` Fix** — würde LEGACY TARGET HIT `targets_hit=1`
  setzen (statt 0) und `close_reason` normalisieren. Behebt den Bug an
  der Quelle, aber der aktuelle Fix kommt ohne diese Änderung aus.

- **Altdaten-Migration** — SQL-UPDATEs um `targets_hit` in alten Einträgen
  rückwirkend zu korrigieren. Nicht nötig, weil die neue PnL-basierte
  Logik die Altdaten korrekt interpretiert.
