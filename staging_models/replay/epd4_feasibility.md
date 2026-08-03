# EPD4 — Feasibility of the detector retrain (T-2026-KYT-9050-004)

Measurement 2026-08-01, read-only against the live DB. Raw data: `epd4_feasibility.json`,
split numbers from the real trainer run: `../retrain_epd4_stats.json`.
Detailed report: `docs/T-2026-KYT-9050-004-epd-retrain-feasibility.md`.

## Verdict

**The retrain cannot be run today — a calendar problem, not a data-quality one. No artifact produced.**

The dataset on the new feature definition is clean (4698 of 4712 events
written, 0.3% loss, 7.9% still open at the horizon). It only spans **22.0 days**.
`chrono_split` gives val and test each the 15% quantile band — 3.3 days —,
and of that the 7-day purge gap (= label horizon) cuts everything away:

| Direction | Events | train/val/test | Span | 15% band | Density | needed |
|---|---|---|---|---|---|---|
| LONG  | 2378 | 1664 / **0** / **0** | 21.8 d | 3.3 d | 109 rows/day | ~50 d (+28 d) |
| SHORT | 1949 | 1364 / **0** / **0** | 22.0 d | 3.3 d | 89 rows/day  | ~50 d (+28 d) |

## Cut point proven, not assumed

Hourly `pump_dump_events` count on 2026-07-10 (UTC): 56–170 events/h up to
16:00, from 17:00 only 10–33/h. The break sits on the hour of the
Bot-10 restart (17:08:29Z), which switched on P1.39, the T-035 rate
normalisation and the revived hourly warmup together, sharply. The **event
rate** fell by ~5×.

## The shift itself is small

Two-sample KS per feature (14d before vs. 14d after the cut) against a null
band from 15 neighbouring 14-day window pairs of the pre-cut history:

| Feature | KS at the cut | Null-band median | Null-band max | above the null band? |
|---|---|---|---|---|
| volume_ratio    | 0.0361 | 0.0624 | 0.4342 | no |
| \|p_chg_60s\|   | 0.0796 | 0.0580 | 0.3355 | no |
| buy_pressure    | 0.1737 | 0.0798 | 0.2039 | no |
| volatility      | 0.0363 | 0.0627 | 0.3536 | no |

No feature leaves the band of ordinary market drift. Only marginal
distributions — joint shifts are therefore not ruled out.

## The deployed model holds up out-of-sample

`epd3_model_{LONG,SHORT}.pkl` (fitted on pre-cut data) on the post-cut events:

| | AUC(TP1) | Calibration | at the live threshold |
|---|---|---|---|
| LONG (thr 0.76)   | 0.586 | monotonic 38.3 → 66.7% TP1 | n=81 (3.4%), WR 60.5%, avg −0.760% |
| SHORT (thr 0.6737)| 0.537 | 50.0 → 73.8% TP1, non-monotonic | n=756 (38.8%), WR 72.6%, avg +0.065% |

No indication that the shift broke the model. The LONG threshold
0.76 (operator volume cap, not an edge filter) does, however, sit in the
worst PnL region of the curve — the 0.5–0.7 bands are positive (+0.17 / +0.25%).
n=81 is thin; that is a hint, not a verdict.

## Earliest

Formula `(0,15·Spanne − 7 d)·Dichte ≥ Zielzeilen`, density assumed constant:

- **2026-08-30** — split no longer degenerate (≥50 rows/slice), statistically worthless
- **2026-09-17** — ~300 rows per slice, threshold scan without backing
- **2026-11-09** — ~1000 rows per slice, first operating point with `min_n=200` backing · **recommendation**

The current value is written to `retrain_epd4_stats.json` (`missing_days`) after every run.
