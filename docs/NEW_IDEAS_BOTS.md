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

## Trainings-Ergebnisse (2026-07-07, alle staging — kein Deploy ohne Operator)

Alle vier Datensätze gebaut (nach DST-Fix f95f092) und trainiert
(`tools/new_models_train.py`, Chrono-Split + Purge, Gate auf roher Prob):

| Modell | Events | AUC val/test | Val-OP | Test-Gate-Uplift | Verdict |
|---|---|---|---|---|---|
| PEX1 | 28.855 (26.271 gelabelt) | 0,545 / 0,565 | thr 0,65 degeneriert (99 % Pass) | −0,560 → −0,555 %/Trade (nichts) | ❌ kein Selektionswert; best_iteration=2 |
| FMR1 | 11.503 (10.481) | **0,498** / 0,544 | −2,24 %/Trade (n=65) | −1,05 → −0,06 %/Trade (n=144) | ❌ Val = Zufall, OP negativ — kein Fundament |
| TRM1 | 1.594 (Klassen 0/5/1.589!) | — nicht trainiert — | — | — | ⛔ upstream blockiert: Detector hält TREND nie (Step-6-Befund) → Klassen existieren nicht. Wiedervorlage nach Detector-Rework/TRANSITION-Split |
| FIF1 | 120.102 (120.072) | 0,533 / 0,561 | **+0,044 %/Trade** (thr 0,67, n=541) | **−0,082 → +0,331 %/Trade, WR 75,3 %, n=893/18.011 (5 % Pass)** | ⚠ einziger Kandidat: Val UND Test positiv, aber Val-Edge hauchdünn |
| (EPD2) | 78.351 | siehe MODEL_INTENT §7 | Safe-Picker verweigert / Val-Test-Bruch | LONG alle Buckets negativ; SHORT Test-WR == Basisrate | ❌ beide Richtungen |

Einordnung: konsistent mit der Batch-E-Kernthese — Event-Ranking-Gates liefern
fast nie robuste Out-of-Time-Expectancy. FIF1 ist die Ausnahme mit dünnem, aber
in Val und Test gleichgerichtetem Signal (vergleichbar MIS1-8h_pump).

**FIF1 DEPLOYED (Operator-Entscheid 2026-07-07 ~11:49):** `fif1_model.pkl`
(thr 0,67, 21 Features) ins Repo-Root kopiert, Bot 33 per Restart-Marker
recycelt, Artefakt-Load verifiziert. Läuft mit `NEW_IDEAS_LIVE_POSTING=1`
LIVE (kein Shadow — Operator-Muster wie AIM2: Cornix-Tracking der geposteten
Signale ist die Validierung). Review nach 4–6 Wochen gegen `ai_signals`.
PEX1/FMR1/TRM1: kein Deploy, Bots 30–32 bleiben idle.

## Design-Notizen je Bot

### PEX1 — Pump-Exhaustion-Short (S6)
Konsumiert die Events des `10_pump_dump_detector` (nur Pumps: `price_change_60s
≥ +1,5`), Gate `volume_ratio ≥ 5` exakt wie im Training (Report 13 EPD1-P0:
sonst out-of-distribution). Events älter als 30 min werden verworfen (Catch-up
nach Downtime darf keine verfallenen Exhaustion-Thesen posten); die
Feature-Kerze wird relativ zur EVENT-Zeit gewählt (floor-1 wie im Training).
Geometrie: `calculate_smart_targets` SHORT. **Label-Geometrie (Review-Fix
2026-07-06):** Trainings-Entry ist die Spike-Preis-Schätzung
`close[idx] × (1 + 60s-Move)` — nicht der Pre-Pump-Close (der hätte
pump-korreliert deflationierte Labels erzeugt); das Replay startet konservativ
NACH der Event-Kerze (deren High enthält den Run-up vor dem Entry). Cooldown
4h je Coin auf JEDEM gescorten Event — exakter Spiegel des
Trainings-Dedups.

### FMR1 — Funding-Extreme Mean-Reversion (S8)
Cross-Section über ALLE Coins aus einem `premiumIndex`-Request; Kandidaten sind
die Perzentil-Extreme, das Modell gated auf TP1-vor-SL. Settlement-Historie je
Kandidat kommt live per REST (`/fapi/v1/fundingRate`) — der Bot hängt damit
NICHT am Backfill-Zustand der `funding_rates`-Tabelle. **Bekannter Rest-Skew:**
live wird die *laufende* Rate bewertet, im Training die *gesettelte* (gleiche
Quelle, ein Settlement Versatz). Cooldown 24h je Coin/Richtung.
**Bewusste Abweichung von der S8-Exit-Idee:** Report 15 skizziert „Halten bis
Funding-Normalisierung oder Time-Stop" — implementiert ist die
Flotten-Standard-Geometrie (Smart-Target-TP/SL, Trainings-Horizont 7 Tage),
weil First-Touch-Simulator und AI-Trade-Monitor genau diese Geometrie labeln
bzw. tracken; ein Funding-Normalisierungs-Exit bräuchte einen eigenen
Monitor-Pfad. V2-Kandidat, falls die Shadow-Zahlen die Idee tragen.

### TRM1 — Transition-Resolution (S10)
Läuft nur, wenn das DEBOUNCED Regime (`regime_current`) TRANSITION ist.
3-Klassen-Modell (0=OTHER, 1=TREND_UP, 2=TREND_DOWN — Vertrag in
`core/research_features.py`); Gate = max(P(up), P(down)). Bei Gate-Pass postet
der Bot einen BTCUSDT-Trade in der prognostizierten Richtung (Smart-Targets).
**Bekannter Skew:** Trainings-Events sind Roh-Checks aus `regime_history`,
live gated das debounced Regime. Cooldown 12h je Richtung.

### FIF1 — FIFO-Filter (S11)
Standalone-A/B: Der Live-FIFO-Pfad (`3_detectors.py`) bleibt unangetastet.
FIF1 pollt `Fast In And Out`-Zeilen der letzten 10 Minuten aus BEIDEN
Master-Tabellen (Review-Fixes 2026-07-06: die closed-UNION fängt
Fast-Resolver, die der Monitor binnen 60s aus active löscht; das Zeitfenster
verhindert, dass nach Idle-/Ausfall-Phasen ein Backlog tage-alter Signale mit
verfallener Geometrie gepostet wird). Dedupe über einen Content-Key
(Coin/Richtung/Zeit/Entry); benötigte `(strategy, time)`-Indizes legt der Bot
beim Start an. Gate-Passer posten unter Tag FIF1 mit der ORIGINAL-Geometrie
(Entry/TP1/SL unverändert), damit die Selektion der einzige Unterschied ist.
JEDER Kandidat landet in `ml_predictions_master` (posted true/false) — das ist
die A/B-Auswertungsbasis.

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
- `pump_dump_events.spike_time` trägt keine TZ-Garantie — der Bot misst den
  Offset gegen die Wanduhr (±12h-Clip); der Builder konvertiert bei Offset
  2/3h DST-aware über Europe/Bucharest (Review-Fix: ein konstanter Offset über
  Monate wäre über die DST-Grenze 1h falsch gewesen).
- TRM1-`minutes_in_transition` ist live die debounced Episodendauer, im
  Training die Roh-Episodendauer — akzeptierte Näherung, im Doc vermerkt.
- TRM1 nutzt von den „confidence-Verläufen" (S10) nur die aktuellen
  `confidence_btc/alt`-Werte; Fenster-Verläufe existieren für Returns/ATR/
  Regime-Fraktionen. Confidence-Deltas sind ein V2-Feature-Kandidat.
- FIF1 lässt zwei S11-genannte Feature-Familien bewusst weg: den
  Modell-übergreifenden Konfluenz-Zähler (E3 — bräuchte den vollen
  Multi-Quellen-Event-Strom im Live-Pfad; implementiert sind FIFO-interne
  Burst-Zähler) und die Coin-Liquiditätsklasse (kein sauberer Live-Proxy
  ohne neue Datenpflege; `vol_ratio_sma20` deckt einen Teil ab). Beides
  V2-Kandidaten nach der ersten Shadow-Auswertung.
- TRM1 postet nie gegen eine offene Gegenposition (kein Self-Hedge auf
  BTCUSDT) — kippt die Prognose, wird nur Shadow geloggt.
- Betriebsnotiz: +4 Prozesse ≈ +8 dauerhafte PG-Connections
  (KYTHERA_DB_POOL_MIN=2 je Prozess) — beim Rollout gegen `max_connections`
  prüfen (P1.34).
