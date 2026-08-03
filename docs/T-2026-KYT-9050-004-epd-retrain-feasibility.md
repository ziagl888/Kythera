# T-2026-KYT-9050-004 — EPD detector retrain on the new feature definitions

**Status:** 2026-08-01 · **Task:** `pump_dump_model.pkl` (bot 10) was fitted on the OLD
feature definition; P1.39 + the T-035 rate normalisation changed four of the ten
model inputs (`vol_ratio`, `p_chg_60s`, `buy_pres`, `volat`). Retrain on the new
definition, cut after the cut point, artifact only into `staging_models/`, new tag.

---

## Verdict

**The retrain is not executable today. No artifact produced — and none that should be
produced.** The blocker is the calendar, not the data quality: the
post-cut history is 22.0 days long, a leak-free chronological 70/15/15 split
with the 7-day purge gap needs ~50 days, an operating point with reserve ~122 days.

**Secondary finding, which lowers the urgency:** the shift that this task
exists for is, on the population that passes the alert gate,
**not distinguishable from ordinary market drift** — and the deployed model keeps
discriminating on post-cut data out-of-sample at its own level. There is no sign
of a serving model broken by the definition change.

**Recommendation:** revisit **2026-11-09**, leave bot 10 running unchanged.
No deploy, no gate flip, no promote — none of that is up for debate here.

---

## 1. Cut point: evidenced, not just taken over

The brief names the bot-10 restart 2026-07-10 17:08:29Z. Counter-check against the
data: hourly `pump_dump_events` count on 2026-07-10 (UTC; the detector is the sole
writer of the table).

```
00–16 Uhr:  124 170  68  72  75  98 135 127 103  91  91  62  93  97 126  56 148
17–23 Uhr:   24  33  20  10  20  21  29
```

The break sits exactly on the hour of the restart. It is **not a feature effect but
a gate effect**: `f485d09` gave the hourly warm-up a coverage and sample floor
(previously a single surviving bucket could set the whole baseline), and that
made the class of junk events that arose from a one-sample denominator vanish.
The event rate dropped by ~5×.

The restart switched on three changes together — P1.39 (index → timestamp),
the T-035 rate normalisation of `p_chg_60s` and the revived volume gate. The
cut point is therefore one, not three.

## 2. The dataset is good — it's just too short

`tools/epd2_build_dataset.py --since '2026-07-10 17:00:00'` (2032 s, job lock held):

| | |
|---|---|
| Events after gates + 900s dedup | 4712 (2582 pump/LONG, 2130 dump/SHORT), 403 symbols |
| written | 4698 |
| losses | `no_candles` 11, `no_ticker` 3, `no_window`/`stale_join`/`geometry_fail` 0 |
| labelled | 4327 (7.9% still open at the 7-day horizon) |
| base rate TP1 | LONG 58.9%, SHORT 69.5% |
| span | 22.0 days |

0.3% loss. The pipeline works cleanly on the new definition.

### Why the split still comes up empty

`chrono_split` gives val and test each the 15% quantile band of the signal times; the
purge gap (7 d = the builder's label horizon) cuts 7 days off the front of that. At
22 days of span, that band is 3.3 days — **shorter than the gap**. Both slices are
empty, regardless of row count. Real trainer run
(`--strategy epd --model-id EPD4`):

```
epd2 LONG:  2378 Events | split 1664/0/0 | Basisrate TP1 58.9%
epd2 LONG:  degenerierter Split — übersprungen. Spanne 21.8d, 15%-Band 3.3d < Purge-Gap 7d
            (Dichte 109 Zeilen/Tag) ⇒ Val/Test leer. Für ≥50 Zeilen je Slice braucht es
            ~50d Spanne (~28d mehr Datensammlung).
epd2 SHORT: 1949 Events | split 1364/0/0 | Basisrate TP1 69.5%
epd2 SHORT: … ~50d Spanne (~28d mehr Datensammlung).
```

Shrinking the purge gap would be the obvious shortcut, and it's the wrong one: it
is by construction equal to the label horizon, and a label window from the train
slice reaching into the val slice is exactly the twin leakage the gap guards
against. A 4-day horizon (covers p95 of the real EPD3 holding time: p50 8.1h,
p90 62.4h, p95 97.4h) would shift the date by three weeks and make the model
incomparable with EPD2/EPD3. Not done.

### No way out via more history

`tools/epd2_build_dataset.py` takes the entry since T-2026-CU-9050-035 from
`ticker_10s` and refuses an earlier `--since` — the old estimator
`close×(1+p_chg_60s/100)` is simply wrong since the rate normalisation.
**`ticker_10s` starts on 2026-07-07 11:19 UTC**, three days BEFORE the cut point.
The Feb–Jul dataset (85 031 events) on which EPD2/EPD3 were fitted is no longer
reproducible with today's builder.

That's the real limit: **the trainable history is ticker-bound, not
cut-point-bound.** The cut demanded by the brief at the cut point costs three
days. The hypertable's retention stands at 365 days (`core/ticker_10s.RETAIN_FOR`) —
the window is growing, it is not capped.

## 3. The shift is smaller than market drift

Two-sample KS per feature, 14 d before vs. 14 d after the cut, measured against a
null band from 15 neighbouring 14-day window pairs of the pre-cut history (i.e.
against what ordinary regime changes produce anyway):

| Feature | KS at cut | Null-band median | Null-band max | above null band? |
|---|---|---|---|---|
| `volume_ratio`  | 0,0361 | 0,0624 | 0,4342 | no |
| `\|p_chg_60s\|` | 0,0796 | 0,0580 | 0,3355 | no |
| `buy_pressure`  | 0,1737 | 0,0798 | 0,2039 | no |
| `volatility`    | 0,0363 | 0,0627 | 0,3536 | no |

n_pre = 20 084, n_post = 4682. The smaller post window drives the KS statistic
up, not down — the finding is thus conservative if anything. No feature leaves
the band. Visible is a granularity effect on `buy_pressure` (p90 0.8333 →
1.0000): the share of rising diffs gets coarser over shorter windows, which the
code already carries at `10_pump_dump_detector.py:1070-1074` as a deliberate
cadence dependency.

**Limitation:** marginal distributions only. A joint shift with unchanged
margins is not ruled out by this.

## 4. The deployed model holds up on post-cut data

`epd3_model_{LONG,SHORT}.pkl` (repo root, fitted on PRE-cut data) scored on the
post-cut events — strictly out-of-sample for this model:

**LONG** (n=2378, live threshold 0.76) — AUC(TP1) 0.586, corr(prob, netPnL) +0.070

| Prob bucket | n | TP1 | Avg net |
|---|---|---|---|
| 0,0–0,3 | 115 | 38,3 % | −5,33 % |
| 0,3–0,4 | 165 | 45,5 % | −1,68 % |
| 0,4–0,5 | 241 | 51,0 % | −0,76 % |
| 0,5–0,6 | 534 | 57,9 % | **+0,17 %** |
| 0,6–0,7 | 915 | 63,6 % | **+0,25 %** |
| 0,7–0,8 | 384 | 65,4 % | −0,32 % |
| 0,8–1,0 | 24 | 66,7 % | +0,36 % |

**SHORT** (n=1949, live threshold 0.6737) — AUC(TP1) 0.537, corr +0.041; at the
threshold n=756 (38.8%), WR 72.6%, avg +0.065%/trade.

The LONG calibration is monotonic in the TP1 rate across the whole range — that's
the behaviour of an intact model, not one being queried out-of-distribution. So the
task's premise ("serving is running against a shifted distribution") is formally
correct but its effect is not demonstrable.

**Side finding, not part of the task:** the operator-set LONG threshold 0.76
(volume cap, explicitly not an edge filter, T-2026-KYT-9050-037) takes only 81
trades (3.4%) on this population at avg −0.760%, while the 0.5–0.7 bands are
positive. n=81 is thin and the replay geometry isn't the live geometry — that's a
pointer for a dedicated measurement, not a verdict.

## 5. Training vs. serving population (open)

The builder dedups events at 900s per symbol and thereby mirrors bot 10's alert
throttle. But the throttle timer is reset only in the **live-trade branch**; for
a leg that doesn't post live, it's inert, and throttling happens only via
`has_open_ai_signal`. Measured (`closed_ai_signals`, tag EPD3, from 2026-07-11,
shadow and live combined):

| | Training rows/day | Live emissions/day | Factor |
|---|---|---|---|
| LONG  | 108,9 | 295,9 | 2,7× |
| SHORT | 88,6  | 478,6 | 5,4× |

The serving population is considerably denser than the one used for training and
choosing the threshold. That's the same class of issue as the OOD error the
`vol_ratio ≥ 5` gate fixed in EPD2, just one level deeper. **Not verified in this
task** whether a threshold chosen on the deduplicated population hits the live
rate — this should be clarified before an EPD4 go-live.

## 6. Tag: EPD4 (reserved, not yet registered)

Taken are **EPD1, EPD2, EPD3** — checked against `tools/bot_variants/index.legacy_artifact_slots()`,
`core/shadow_gate.SHADOW_ARTIFACTS`, `_LIFECYCLE`, `_RETIRED_TAGS` as well as the
DB history (`ai_signals.model`, `closed_ai_signals.model`, `ml_predictions_master.model_name`).
**EPD4 is free everywhere**, and `epd4_model_{LONG,SHORT}.pkl` doesn't claim a
foreign loader slot (`tools/promotion_guard.check_staging_filename` → PASS).

EPD4 is **not** registered — without an artifact, an entry in `core/shadow_gate`
would be dead configuration. What's pinned instead is the claim itself
(`backtest/test_retrain_model_id.py::test_epd4_is_free_in_every_code_registry`), including
the trap: the gate default is **LIVE**, so an unregistered tag posts live. Before the
first EPD4 emission, the `_LIFECYCLE` row must be in place.

### P1.45 wiring — why deliberately no rewire here

The brief demands that `meta.model_id` be wired into the posting path, or a
precise reason given why not. The finding:

1. For the **artifact path** it's long since wired: `module_tag = best_art["tag"]`
   comes from `core.model_artifacts.load_artifact` and thus from `meta.model_id`
   (pinned in `backtest/test_epd_tag.py`).
2. For the **challenger/shadow path** (`_emit_epd3_shadow`) the tag is a constant —
   and that must stay that way. Measured against the artifacts themselves:

   | File | `meta.model_id` |
   |---|---|
   | `epd3_model_LONG.pkl` (root, **live**) | `EPD2` |
   | `epd3_model_SHORT.pkl` (root, live) | `EPD2` |
   | `staging_models/epd3_model_SHORT.pkl` | `EPD3` (re-tagged, T-2026-KYT-9050-057) |
   | `staging_models/rub2_model_LONG.pkl` (= RUB3's artifact) | `RUB2` |

   If `load_shadow_artifact` pulled the tag from the meta, the **live-running**
   EPD3-LONG leg would post under `EPD2` from that moment on — it would merge with
   the parked legacy leg, and `has_open_ai_signal(symbol, dir, "EPD3")` would no
   longer find its own open trades. The hardcoded tag is currently the only thing
   keeping the generations apart there.

   The LONG tag defect is known and deliberately left open:
   `tools/retag_artifact.py` refuses the re-dump because the artifact was
   pickled under sklearn 1.9.0 and the fleet serves 1.7.1 (the round trip would
   degrade the isotonic calibrator) — see
   `backtest/test_epd3_artifact_model_id.py`.

The wiring at this point is therefore **not a missing feature but blocked by an
open artifact defect**. The right moment is the EPD4 run: its artifact carries
`model_id = EPD4` from birth (below), and then the shadow path can check the
register tag against the meta tag without renaming a live leg.

## 7. What this task changed in the code

- `tools/retrain_from_replay.py` — `run_epd(model_id=…)` + CLI `--model-id`. The tag
  sets `meta.model_id` **and** the filename prefix together (`artifact_slot`,
  identical to `promotion_guard.tag_prefix`); letting them drift apart is exactly
  the slot-hijack bug from 2026-07-21. Default `EPD2` ⇒ unchanged run.
- `tools/retrain_from_replay.py` — the degenerate split now reports the arithmetic
  instead of just "skipped" (`split_shortfall`), and the finding lands
  machine-readable in `retrain_<slot>_stats.json`. That's the message the run in
  November will see.
- `tools/retrain_pump.py` — `--model-id` passed through.
- `backtest/test_retrain_model_id.py` — new, 14 tests.

No artifact, no registration, no bot code. The retrain command for later stands
in `retrain_pump.py`:

```
python tools/retrain_pump.py --since 2026-07-11 --model-id EPD4
```

## 8. Revisit — **T-2026-KYT-9050-067**

`(0,15 · Spanne − 7 d) · Dichte ≥ Zielzeilen`, density held constant at 108.9
(LONG) / 88.6 (SHORT) labelled rows/day:

| Date | Val/test per direction | Assessment |
|---|---|---|
| 2026-08-30 | ~50 | split no longer degenerate, statistically worthless |
| 2026-09-17 | ~300 | `pick_threshold_safe` (min_n=200) carries only at the very bottom |
| **2026-11-09** | **~1000** | first operating point with reserve up to ~p80 · **recommendation** |

The density is an assumption — it depends on market activity. The actual value
sits after every run in `staging_models/retrain_epd4_stats.json`
(`missing_days`); a run costs ~35 min build time and is thus the cheapest way to
sharpen the date.

**Open points for the later run** (also in `epd4_feasibility.json`):

1. `core/shadow_gate`: EPD4 into `_LIFECYCLE` (SHADOW) + `SHADOW_ARTIFACTS`,
   **before** bot 10 emits — default is LIVE.
2. `10_pump_dump_detector`: emission branch for EPD4 (pattern `_emit_epd3_shadow`).
3. `tools/verify_staging_artifacts.build_registry()`: the `epd` family only globs
   `epd2_model_*.pkl`; an EPD4 artifact would be silently skipped.
4. Training vs. serving population (§5) to be clarified before a threshold goes
   live.

---

**Raw data:** `staging_models/replay/epd4_feasibility.json` · `staging_models/retrain_epd4_stats.json`
**Not committed:** the 3.4MB event dataset (`epd4_events.jsonl`) — reproducible
via `tools/epd2_build_dataset.py --since '2026-07-10 17:00:00'`.
