# Research-Bots 30–33 — PEX1 / FMR1 / TRM1 / FIF1

**Stand:** 2026-07-06 · **Quelle:** `audit_reports/15_strategy_proposals.md` (S6, S8, S10, S11)
· **Task:** T-2026-CU-9050-019

Vier neue ML-Bots als Kohorte im gemeinsamen Telegram-Channel **`CH_NEW_IDEAS`**
(Operator-Entscheid 2026-07-06: der Channel ist die Testumgebung, Signale gehen
direkt live; `NEW_IDEAS_LIVE_POSTING=0` schaltet alle vier auf Shadow-only).
Attribution je Bot läuft über den Modell-Tag in `ai_signals` /
`ml_predictions_master` — der Channel ist nur der Transportweg.

## Überblick

| Bot | Tag | Idee (Report 15) | Events | Richtung | Takt |
|---|---|---|---|---|---|
| `30_ai_pex1_bot.py` | PEX1 | S6 Pump-Exhaustion-Short | `pump_dump_events` (vol_ratio ≥ 5, +1,5%/60s) | nur SHORT | 60s-Poll |
| `31_ai_fmr1_bot.py` | FMR1 | S8 Funding-Extreme Mean-Reversion | Funding-Cross-Section (≥95. Pctl SHORT / ≤5. Pctl LONG) | beide | stündlich (Min. 19) |
| `32_ai_trm1_bot.py` | TRM1 | S10 Transition-Resolution | `regime_current` = TRANSITION | BTCUSDT LONG/SHORT | alle 5 min (Min. %5==4) |
| `33_ai_fif1_bot.py` | FIF1 | S11 FIFO-Filter | neue `active_trades_master`-Zeilen (Fast In And Out) | wie Quelle | 60s-Poll |

Gemeinsame Bausteine (eine Quelle für Bot, Builder und Trainer — X-R1-Regel):

- `core/research_features.py` — Feature-Verträge + Builder (alles skalenfrei)
- `core/model_artifacts.py` — Artefakt-Loader (`<strat>_model.pkl` im Repo-Root;
  fehlt das Artefakt, läuft der Bot im Idle-Modus)
- `core/signal_post.py` — Outbox + `ai_signals` + Shadow-Log (atomar, kein
  Cornix-Block in der Info-Nachricht)

Alle vier folgen den Flotten-Konventionen: Closed-Candle-Features (R1),
Startup-Feature-Selbsttest (P0.12), Gate auf roher Probability (Threshold aus
dem Val-Operating-Point), kalibrierte Confidence nur für Anzeige, Cooldowns via
`trade_cooldowns`, Tracking durch `8_ai_trade_monitor`.

## Design-Notizen je Bot

### PEX1 — Pump-Exhaustion-Short (S6)
Konsumiert die Events des `10_pump_dump_detector` (nur Pumps: `price_change_60s
≥ +1,5`), Gate `volume_ratio ≥ 5` exakt wie im Training (Report 13 EPD1-P0:
sonst out-of-distribution). Events älter als 30 min werden verworfen (Catch-up
nach Downtime darf keine verfallenen Exhaustion-Thesen posten). Geometrie:
`calculate_smart_targets` SHORT — identisch zur Label-Geometrie. Cooldown 4h je
Coin, im Training als Event-Dedup gespiegelt.

### FMR1 — Funding-Extreme Mean-Reversion (S8)
Cross-Section über ALLE Coins aus einem `premiumIndex`-Request; Kandidaten sind
die Perzentil-Extreme, das Modell gated auf TP1-vor-SL. Settlement-Historie je
Kandidat kommt live per REST (`/fapi/v1/fundingRate`) — der Bot hängt damit
NICHT am Backfill-Zustand der `funding_rates`-Tabelle. **Bekannter Rest-Skew:**
live wird die *laufende* Rate bewertet, im Training die *gesettelte* (gleiche
Quelle, ein Settlement Versatz). Cooldown 24h je Coin/Richtung.

### TRM1 — Transition-Resolution (S10)
Läuft nur, wenn das DEBOUNCED Regime (`regime_current`) TRANSITION ist.
3-Klassen-Modell (0=OTHER, 1=TREND_UP, 2=TREND_DOWN — Vertrag in
`core/research_features.py`); Gate = max(P(up), P(down)). Bei Gate-Pass postet
der Bot einen BTCUSDT-Trade in der prognostizierten Richtung (Smart-Targets).
**Bekannter Skew:** Trainings-Events sind Roh-Checks aus `regime_history`,
live gated das debounced Regime. Cooldown 12h je Richtung.

### FIF1 — FIFO-Filter (S11)
Standalone-A/B: Der Live-FIFO-Pfad (`3_detectors.py`) bleibt unangetastet.
FIF1 pollt neue `Fast In And Out`-Zeilen (id-Watermark, beim Start = MAX(id)),
scored jede mit dem Meta-Klassifier und postet die Gate-Passer unter Tag FIF1 —
mit der ORIGINAL-Geometrie (Entry/TP1/SL unverändert), damit die Selektion der
einzige Unterschied ist. JEDER Kandidat landet in `ml_predictions_master`
(posted true/false) — das ist die A/B-Auswertungsbasis.

## Step 2 — Training auf dem VPS

Reihenfolge je Strategie: Dataset-Builder → Trainer → Report prüfen → Deploy.
Alle Builder laufen mit BELOW_NORMAL-Priorität und read-only gegen die DB.
Artefakte landen NUR in `%KYTHERA_STAGING_DIR%` (P1.35) — Deploy ins Repo-Root
ist eine bewusste Operator-Entscheidung.

```bash
# 0. Voraussetzung nur für FMR1: Funding-Historie backfillen (resumierbar)
python tools/backfill_funding_rates.py

# 1. Datasets bauen (je ~Minuten bis Stunden; --limit-symbols N für Smoke-Tests)
python tools/pex1_build_dataset.py
python tools/fmr1_build_dataset.py
python tools/trm1_build_dataset.py
python tools/fif1_build_dataset.py            # 111k Events; ggf. --sample-pct 50

# 2. Trainieren (Artefakt + _report.json nach staging_models)
python tools/new_models_train.py --strategy pex1
python tools/new_models_train.py --strategy fmr1
python tools/new_models_train.py --strategy trm1 --min-val-trades 20
python tools/new_models_train.py --strategy fif1

# 3. Report prüfen (GATE-UPLIFT test > 0? Reliability plausibel?) und dann
#    bewusst deployen:
copy %KYTHERA_STAGING_DIR%\pex1_model.pkl C:\_BOT\Kythera\pex1_model.pkl
#    … analog fmr1/trm1/fif1. Bots laden neue Artefakte automatisch (täglicher
#    Reload; im Idle-Modus alle 30 min).
```

**Deploy-Gate (Empfehlung, analog AIM2 Rollout-Gates):** nur deployen, wenn der
Test-Report `gate_avg_pnl > 0` UND `n_pass` groß genug für eine ehrliche
Aussage ist (≥ 50 für PEX1/FIF1, ≥ 20 für FMR1/TRM1). Ein negatives Resultat
ist ein gültiges Ergebnis (Action-Bias-Korrektur) — dann bleibt der Bot im
Idle-Modus und die Idee wird mit Befund geparkt.

## VPS-Setup-Checkliste

1. `.env` ergänzen: `CH_NEW_IDEAS=<Channel-ID>` und `NEW_IDEAS_LIVE_POSTING=1`.
2. Cornix auf den neuen Channel lauschen lassen (falls die Signale ausgeführt
   werden sollen — sonst bleibt es ein Beobachtungs-Channel).
3. Fleet-Restart oder `touch control/restart/main_watchdog.py`-Äquivalent —
   die vier Bots sind in `PROCESSES_TO_RUN` registriert (start_delay 191–215).
4. Ohne deployte Artefakte laufen die Bots im Idle-Modus (Log:
   "Artefakt fehlt … Idle-Modus") — das ist der erwartete Zustand bis Step 2.

## Offene Punkte / bewusste Vereinfachungen

- PEX1 nutzt die 4 Event-Messwerte + 1h-Kontext; Microstructure-Features aus
  einem 10s-Ticker (Report 15) existieren als Live-Tabelle nicht — bewusst
  verschoben, bis eine 10s-Persistenz existiert.
- `pump_dump_events.spike_time` trägt keine TZ-Garantie — Bot und Builder
  messen den Offset gegen die Wanduhr (±12h-Clip) statt zu raten.
- TRM1-`minutes_in_transition` ist live die debounced Episodendauer, im
  Training die Roh-Episodendauer — akzeptierte Näherung, im Doc vermerkt.
