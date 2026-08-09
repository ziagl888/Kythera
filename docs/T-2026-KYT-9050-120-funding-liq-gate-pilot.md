# T-2026-KYT-9050-120 — Funding × forced-liquidation entry-gate pilot (fleet-wide, snapshot-driven)

_Status: built 2026-08-09 · conclusive run blocked on liq_events coverage until ~2026-08-24 ·
complements T-2026-KYT-9050-095 (bot-40 liquidation arm)_

## Question (Michi, 2026-08-09)

We found an edge with volatility (T-111: rolling-q80 vol gate holds out-of-sample, live as FIF2)
and probed OI without one. Is there an edge in the **interplay of funding rates and forced
liquidations** — ideally as an entry gate on the existing bots' trades that lifts the win rate
in BOTH directions (LONG and SHORT)?

## What is already answered (do not re-litigate)

| Prior study | Verdict | Consequence here |
|---|---|---|
| T-134/K3 funding-risk layer | direction-confirmed, **magnitude-weak and attenuating** (Spearman collapses in the test half) | funding ALONE does not license a fleet-wide gate — it enters only as an interaction term |
| T-094 OI/liq gate (bot 40) | OI = **NO EDGE** (AUC 0.455–0.498); liq **not concludable** (1 day of data) | OI stays out of this pilot entirely |
| T-096 K9 OI event study | spike-fade and OI×funding **refuted** | ditto |
| T-111 FIF2 vol gate | q80 gate **holds OOS** | proof that gates CAN work here; the eval discipline below mirrors it |

The genuinely open cell is **liquidation features and the funding × liquidation interaction**
(squeeze/flush signatures). Nobody has tested it because `liq_events` (collector 41,
`!forceOrder@arr`) only started writing on **2026-08-03**.

## Hypotheses

- **H1 (crowded-side flush/squeeze veto, symmetric):** entries taken while the trade's OWN side is
  crowded (funding extreme in the trade direction) AND that crowded side is actively being
  force-closed (liquidation cascade against the trade direction) are entering into a flush/squeeze
  and underperform. Veto them. Note this is the genuinely NEW cell: T-134 found funding-alone
  extreme-positive to be weakly GOOD for LONGs — the hypothesis is that it flips conditional on a
  simultaneous cascade.
  - LONG variant: fund_24h > +3 bps (crowded longs) + long-liquidation cascade (side=SELL, pushes
    price down = against LONG).
  - SHORT variant: fund_24h < −3 bps (crowded shorts) + short-liquidation cascade (side=BUY, pushes
    price up = against SHORT) — the classic short squeeze.
- **H2 (cascade-against veto, funding-free):** any liquidation cascade against the trade direction
  in the last 15/60 min predicts adverse continuation → veto.
- **H3 (market-wide cascade veto):** ≥ K distinct symbols printing liquidations within 15 min =
  market-wide deleveraging; new entries in either direction underperform → veto.
  _Amended 2026-08-09 (before any conclusive evidence):_ the original K=5 was degenerate — the
  market always has liquidations printing (median 78 distinct symbols/15 min over the first
  6 collector days), so K=5 skipped 100% of entries in the smoke run. New K=140 = q90 of the
  observed feature marginal (~10% skip). Derived from the feature DISTRIBUTION only, never from
  outcomes — the T-116 pre-registration discipline holds.

All three are entry-time-only (no forming data, as-of backward joins) and are evaluated per
direction — Michi's target metric is a per-direction WR lift, but per repo Rule 8 the verdict
hangs on WR **and** raw net-PnL expectancy moving together.

## Architecture (extraction-first, per Michi 2026-08-09)

Millions of per-trade queries against live Postgres are off the table. One read-only VPS export
pulls everything into a single DuckDB snapshot file; the study runs DB-free on that file and can
be iterated arbitrarily often (SRV02 or VPS session) without touching Postgres again.

```
VPS session:      python tools/gate_snapshot_export.py            (read-only SELECTs → .local/gate_snapshots/*.duckdb)
any session:      python tools/funding_liq_gate_study.py --snapshot <file>   (DB-free)
build machine CI: python -m pytest backtest/test_funding_liq_gate_study.py   (synthetic fixtures, DB-free)
```

Snapshot contents: deduped `closed_ai_signals` slice (fleet log), `trailing_positions` realized
book (bot-40 arm, pre-builds T-095), full `funding_rates`, full `liq_events`.

## Feature discipline (non-negotiable)

- `liq_events` is a **SAMPLE** — Binance throttles `!forceOrder@arr` to 1 order/s/symbol. Counts,
  cluster/recency and side-imbalance features only; notional sums are computed but labeled
  secondary and never carry a verdict alone.
- As-of joins strictly backward (`searchsorted` left / `merge_asof` backward), `datetime64[ns]`
  (T-073 epoch trap). An event at exactly the entry timestamp is EXCLUDED (no simultaneity).
- `closed_ai_signals.open_time` is naive Europe/Bucharest → DST-aware localization
  (`core.time.LEGACY_WRITER_TZ`), never a fixed offset.
- Funding features come from the SHARED builder `core.funding_features.funding_features_asof`
  (Rule 7 — no reinvention); fee = `tools.walkforward_sim.FEE_PER_SIDE` (no invented fee).
- No outcome conditioning (T-106) and no deriving thresholds from outcomes under the same gate
  (T-116): gates are pre-registered above, thresholds are round numbers fixed in this doc BEFORE
  the conclusive run (cascade ≥ 3 same-symbol events/60 min, ≥ 2/15 min, market cascade ≥ 140
  distinct symbols/15 min — amended from the degenerate 5, see H3 — extreme funding ±3 bps = the
  T-134 cuts).
- Evaluation: chronological val/test halves must AGREE (sign) per direction; paired gate-on/off on
  identical trade populations; a gate is a candidate only if kept-WR AND kept-raw-mean improve in
  BOTH halves with a non-trivial skip count.

## Guard rails

- `MIN_LIQ_DAYS = 21`: below 21 days of `liq_events` coverage the study refuses a verdict
  (same discipline as T-094/T-095). `--smoke` runs the full plumbing and stamps the report
  NOT CONCLUDABLE — use it now; the conclusive run unlocks ~2026-08-24.
- Expected sample size at first conclusive run is SMALL (~3 weeks of fleet trades). A negative is
  cheap and safe; a positive is a *candidate* for a longer confirmation window, never a
  straight-to-live gate.
- Go-live of any surviving gate is a SEPARATE task: feature builder promotion into `core/`
  (trainer == serving == replay), shadow A/B first, and a Michi gate (escalation rule —
  money-affecting).

## Deliverables (this task)

- `tools/gate_snapshot.py` — snapshot table specs + DuckDB write/read helpers (pure, testable).
- `tools/gate_snapshot_export.py` — one-shot read-only VPS export.
- `tools/funding_liq_gate_study.py` — snapshot-driven study (features, cells, gate eval, report).
- `backtest/test_funding_liq_gate_study.py` — DB-free tests on synthetic fixtures.

## Run plan

1. **Now (VPS session, 5 min):** export snapshot, run `--smoke` → validates plumbing end-to-end on
   the ~6 days of liq data; no verdict.
2. **~2026-08-24 (VPS session):** fresh export, conclusive run (guard lifts automatically),
   verdict doc to `staging_models/`, KB task update. Coordinates with T-095 (bot-40 arm reuses the
   same snapshot and the trailing section of this study).
