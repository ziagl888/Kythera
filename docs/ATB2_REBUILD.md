# ATB2 — Converging-Channel-Breakout: Neuaufbau (T-2026-CU-9050-104)

Neuaufbau des geparkten Trendlinien-Bots (Bot 14, ATB1) von null gemäß
**`docs/MODEL_INTENT.md` §11** (Michi, 2026-07-07). ATB1 ist tot: Audit-Note D,
live Σ −172 netto bei 65,7 % „WR", Kernverdikt Report 16 „Das Modell sah nie das
Event, das es scored" (Trainer labelte Kreuzungen der 90d-Close-Regressionsgerade,
live gehandelt wurden Pivot-Trendlinien). Dossier: `audit_reports/dossiers/ATB1.md`.

## 1. Was ATB2 anders macht (behobene X-R-Findings)

| Alt (ATB1, tot) | Neu (ATB2) | behebt |
|---|---|---|
| Event = Kreuzung der 90d-Close-Regressionsgerade | **Konvergierender Kanal** (Wedge/Triangle/Pennant) aus bestätigten Swing-Pivots, geschlossener Ausbruch | X-R1 Event-Mismatch |
| Label = +10 %-Touch/72 h **ohne SL** | First-Touch **TP1-vor-SL** der Measured-Move-Geometrie via `simulate_exit`, Fees inkl. | X-R1/X-R5 |
| Random `train_test_split` über 72 h-überlappende Fenster | Chronologischer 3-Wege-Split + 3d-Purge-Embargo | X-R3 Zwillings-Leakage |
| Threshold auf dem Test-Set maximiert | Threshold via `pick_threshold_safe` **auf Validation** (None = nicht deploybar) | X-R2 |
| „Confidence %" unkalibriert | Isotonic-Kalibrierung out-of-time | Report 16 |
| kein meta.json, Silent-Feature-Death | Artefakt-Meta (`model_id=ATB2`, Feature-Liste, Threshold) + `assert_features_alive` | X-R6 |

Der 5-Faktor-WillyAlgoTrader-Score (Penetrationstiefe/ATR, Body-Ratio,
Body-Commitment, Volumen-Spike, RSI-Momentum) geht **nicht als handgewichteter
Score** ein, sondern als 5 XGB-Setup-Features neben der Kanalgeometrie — analog
`18_ai_abr1_bot.GEOMETRY_FEATURES`.

## 2. Code (in diesem PR, DB-frei gebaut + getestet)

- **`core/atb2_features.py`** — geteilte Quelle für Bot + Simulator + Trainer
  (X-R1-Regel). Bestätigte Pivots (No-Repaint), Kanal-Fit (§11-Kriterien:
  ≥3 Berührungen je Kante, Konvergenz ≥2 %, Breite 0,5…120×ATR,
  Volumen-Kontraktion <85 %), geschlossener Ausbruch, `ATB2_FEATURES`-Vertrag,
  Measured-Move-Targets, `assert_features_alive`. Indikatoren (ATR/RSI/EMA)
  deterministisch aus OHLCV — keine `pandas_ta`-Versionsdrift (P0.12).
- **`tools/walkforward_sim.py`** — Adapter `run_atb2` (`--strategy atb2`).
  Label-Geometrie = Measured-Move; zusätzlich Smart-Targets derselben Kerze als
  Vergleich (`smart_*`-Felder, §11 Measured-Move GEGEN Smart-Targets).
- **`tools/retrain_from_replay.py`** — Runner `run_atb` (`--strategy atb2`):
  je Richtung, chronologischer Split + 3d-Purge, Isotonic, Threshold auf Val,
  Artefakt + `_meta.json` nach `staging_models/` mit `model_id=ATB2`.
- **`backtest/test_atb2_features.py`** — 9 DB-freie Tests (Detektion beide
  Richtungen, No-Repaint, Feature-Vertrag, Kein-Kanal-auf-Trend,
  Alive-Assertion, Measured-Move-Geometrie, End-to-End-Adapter).

## 3. VPS-Run-Book (Phase B — NICHT auf der Build-Maschine)

DB-gebunden → nur in einer VPS-Session, Trainer in `Documents\_X`. **Sequential-
Jobs-Regel:** strikt HINTER der laufenden T-061-Retrain-Queue
(`_X\t061_full_rerun_runner.ps1`) einreihen — genau ein Train-/Sim-Job zur Zeit.
Live-Tabellen read-only, CPU-gedrosselt (der Simulator setzt BELOW_NORMAL und
prüft CPU-Headroom selbst).

```powershell
# 1) Labeling: Walk-Forward-Replay über coins.json (365d), schreibt
#    staging_models/replay/atb2_replay_365d.jsonl
py -3.13 tools\walkforward_sim.py --strategy atb2 --days 365 --resume

# 2) Training: je Richtung, Artefakt + Meta nach staging_models/
py -3.13 tools\retrain_from_replay.py --strategy atb2
#    -> staging_models\atb2_model_LONG.pkl  + _meta.json
#    -> staging_models\atb2_model_SHORT.pkl + _meta.json
#    -> staging_models\retrain_atb2_stats.json
```

Kontext-Vorteile jetzt (auf aktueller DB labeln): Indikator-Historie
P1.13-bereinigt (T-061), RSI eindomänig Wilder (T-097).

## 4. Deploy-Verdikt (was „deploybar" heißt)

Pro Richtung deploybar nur, wenn **`optimal_threshold` ≠ None** (d. h.
`pick_threshold_safe` fand eine Validierungs-Wahrscheinlichkeit mit
Ø-Netto-PnL > 0 bei ≥ min_n Trades) **und** die out-of-time-Test-Stats
(`test_stats`) das Val-Verdikt bestätigen (positives Σ-Netto-PnL, plausible
Kalibrierung). Ein `threshold=None` ist ein **gültiges „nicht deployen"** —
wie RUB2-LONG/EPD2. Measured-Move vs. Smart-Targets über die `smart_*`-Felder
im Replay vergleichen, bevor eine Geometrie festgeschrieben wird.

## 5. Follow-up (gated, C-Gate Michi)

Erst NACH einem deploybaren ATB2-Verdikt:

1. **Bot-Serving-Rewire** (`14_ai_atb_bot.py`): alten Einzel-Trendlinien-
   Detektor (`detect_trend`/`classify_trendline_event`/`get_ml_prediction`)
   durch `core.atb2_features.find_channel_breakout` ersetzen; Modell-Load über
   `core.model_artifacts.load_artifact` (Idle-Mode, `expected_features=ATB2_FEATURES`);
   Measured-Move-Geometrie posten; **P1.45**: Tag aus `contract["tag"]`
   (`meta.model_id`) statt hartem `MODEL_ID='ATB1'` (Muster: `18_ai_abr1_bot.py:520`).
   Ohne Artefakt läuft der Bot dann sauber im Idle-Mode.
   **Paritäts-Kontrakt:** der Serving-Pfad MUSS je Coin ≥ `atb2_features.MIN_HISTORY_CANDLES`
   (1500) geschlossene 1h-Kerzen vor der Entscheidungskerze laden, sonst driften
   EMA200-abhängige Features (dist_ema200) gegenüber dem Replay (X-R1).
2. **Entparken** (`control/parked/14_ai_atb_bot.py` entfernen) — Operator-Entscheid.

Bis dahin bleibt ATB1 geparkt (kein Live-Effekt, kein Artefakt im Live-Pfad).
