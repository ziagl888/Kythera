# Replication Scoring on Hyperliquid — Feasibility Eval

Spike for **T-2026-CU-9050-058**. Question: can polybot's "Replication Scoring"
concept ([ent0n29/polybot](https://github.com/ent0n29/polybot), MIT, Java) be
reproduced for Kythera on **Hyperliquid public fills**, and is it worth building?
Source of the lead: the 2026-07-10 repo audit (KB `mcp-41a50fe33552`).

This is a **feasibility evaluation, not an implementation**. The data-access and
scoring claims below were verified live on 2026-07-10 with a standalone PoC
([`tools/research/hl_replication_poc.py`](../tools/research/hl_replication_poc.py)) —
the numbers quoted are its real output, not estimates.

---

## 1. Verdict

**Technically feasible, cheaply. Strategically optional, and gated on a decision
that hasn't been made.**

- **Data access: solved.** Any trader's fill history is public and keyless on
  Hyperliquid; the leaderboard supplies a 40k-address universe. Verified working
  from the build machine (§3).
- **Signature + score: ports directly, and the perp data is *richer* than
  polybot's Polymarket source.** polybot's exact formula (mean L1 over marginal
  distributions → 0–100) ran unchanged on Hyperliquid fills in the PoC (§4–5).
- **The concept only pays off if Kythera adopts a copy-trade / trader-screening
  use case.** Replication scoring answers "how reproducible is *this trader's*
  strategy" — a copy-trading question. Kythera today is a signal-generation ML
  fleet on **Binance**, not a copy-trading system, and the Hyperliquid venue
  decision is **still open** (audit: "machbar, well-trodden, kein Gegenargument"
  — but no decision recorded). **Recommendation: park as a ready-to-pull spike;
  do not build the pipeline until the venue + copy-trade question is decided.**
- **Secondary (ClickHouse ingestion): reject for now.** Kythera is
  PostgreSQL + TimescaleDB. Public fills are low-volume append-only time-series
  that fit a Timescale hypertable natively; a second datastore isn't justified at
  this scale (§7).

No fleet code was touched. The only artifact is the DB-free research PoC.

---

## 2. What polybot's "Replication Scoring" actually is

Stripped of the Java microservice framing (executor / strategy / ingestor /
analytics over ClickHouse + Redpanda), the research core is three scripts in
`research/`:

| Method | Script | What it compares |
|---|---|---|
| Trade-print distribution match | `replication_score.py` | A candidate's fill-distribution vs a reference trader's |
| Order-stream match | `replication_score_orders.py` | Strategy decision signals vs inferred target distributions |
| Sim-trade strict match | `sim_trade_match_report.py` | Paper-execution prints vs the reference trader in the same window |

The load-bearing one is the first. Its algorithm (read from the source):

1. Reduce a fill history to **four marginal distributions** — market mix, outcome
   mix, execution-type mix, size mix (top-15 sizes).
2. Normalize each to probabilities.
3. **L1 distance** per dimension: `Σ|p_k − q_k|`.
4. Combine: `score = max(0, 100·(1 − avg_L1/2))`, i.e. 0–100 where 100 is an
   identical distributional fingerprint.

It is a **distributional-similarity** score. Note what it is *not*: it does not
model order sequencing, entry/exit timing, or holding period, and it scores
*two* traders' similarity rather than *one* trader's reproducibility. Timing
buckets are computed in the source but deliberately excluded from the score.

---

## 3. Data access on Hyperliquid — verified

Base: `https://api.hyperliquid.xyz/info` (POST) and the public leaderboard blob.
No API key, no auth, no proxy for any of this.

| Need | Endpoint | Result (verified 2026-07-10) |
|---|---|---|
| Address universe | `stats-data.hyperliquid.xyz/Mainnet/leaderboard` | **40,376 rows**, each `{ethAddress, accountValue, windowPerformances, prize, displayName}`; `windowPerformances` carries day/week/month/allTime PnL + ROI. 33 MB blob. |
| A trader's fills | `info` `{"type":"userFills","user":<addr>}` | **2,000 fills** returned for the top address. Public by address. |
| Time-ranged fills | `info` `userFillsByTime` (startTime/endTime) | 2,000 per page; **only the 10,000 most recent fills per address are retrievable** — the deep-history ceiling. |
| Historical orders | `info` `historicalOrders` | ≤2,000 recent orders. |

**Fill schema** (far richer than polybot's Polymarket source, which had only
market-slug / YES-NO / exec-type / size):

```
coin, px, sz, side (A=ask/sell, B=bid/buy), time, startPosition,
dir (Open Long | Close Long | Open Short | Close Short),
closedPnl, hash, oid, crossed (false=maker, true=taker), fee, tid, cloid, feeToken, twapId
```

The `dir`, `crossed`, `closedPnl`, and `fee` fields are the win: direction and
maker/taker are explicit (no inference needed), and realized PnL per close comes
for free — polybot had to reconstruct most of this.

**Cost / limits.** userFills is cheap (one POST per address, ≤2000 fills). The
real constraint is the **10k-fills-per-address history ceiling**: for
high-frequency market-makers, 10k fills can be a few days. For strategy
fingerprinting that's fine (the *distribution* stabilizes fast); for full
back-history reconstruction it is not. Rate limits are IP-weighted; a
leaderboard-screening job should throttle (a few req/s) and cache per address.

---

## 4. Signature extraction — perp-adapted

polybot's four Polymarket features map onto perp fills one-to-one, and the perp
schema lets us drop the inference polybot needed:

| polybot (Polymarket) | Kythera (Hyperliquid perp) | Source field |
|---|---|---|
| market mix (15m-BTC, …) | **coin mix** | `coin` |
| outcome mix (YES/NO) | **direction mix** (Open/Close × Long/Short) | `dir` |
| execution mix | **liquidity mix** (maker/taker) | `crossed` |
| size mix (top-15) | **size mix** (USD-notional buckets) | `px·sz` |

Perp-native dimensions worth adding beyond the polybot four (not in the PoC, but
cheap from the same stream): **holding-time buckets** (match Open→Close by
`coin`+sign, diff `time`), **realized-PnL sign/magnitude mix** (`closedPnl`),
and **fee/notional ratio** (a strong maker-vs-taker-vs-aggression fingerprint).

The PoC's live signatures show this discriminates cleanly:

```
A (0x85ec…2052):  coin ETH 100%  | dir balanced L/S  | maker 62%  → ETH market-maker
B (0xf5d8…ad53):  coin HYPE/BTC/ZEC/ETH | dir Open-Short 42% | taker 55% → directional short-taker
```

---

## 5. Score definition — what to keep, what to fix

polybot's formula ran **unchanged** on the perp signatures (PoC output):

```
A ↔ B similarity      : 53.1        (moderately different fingerprints — matches the eyeball read)
A self-consistency    : 86.4        (temporal replicability — see below)
B self-consistency    : 89.5
```

**Keep:** the L1-over-marginals → 0–100 form. It is simple, bounded, explainable
("tadellos und easy zu erklären"), and behaves sensibly on real data.

**Fix / extend** — the raw score has two gaps that matter for a copy-trade use
case:

1. **Similarity ≠ reproducibility.** The polybot score compares *two* traders.
   The more useful screening question is "is *this one* trader's edge
   reproducible?" The PoC adds a **self-consistency** measure: split a trader's
   fills chronologically, score the two halves against each other. A stable
   strategy scores high on itself (A=86, B=90 above); a one-off run would not.
   This is the actual "replicability" signal for leaderboard screening.
2. **Marginals ignore joint + sequential structure.** Two traders can share coin,
   direction, and size marginals yet trade in opposite sequences. If the score
   ever gates real capital, add the sim-trade strict match (polybot's third
   method: replay against the reference trader in identical windows) and a
   holding-time dimension. **Do not** promote a marginals-only score to a
   capital decision.

---

## 6. Fit with Kythera's existing machinery

The methodology is not foreign to this repo — Kythera already owns most of the
pieces polybot spins up microservices for:

| polybot deep-analysis stage | Kythera equivalent that already exists |
|---|---|
| `02_feature_layer_and_regimes` (feature layer + regime detection) | `core/*_features.py` shared feature builders + the live regime detector (26/27) and `core/regime_logic.py` |
| `01_extract_snapshot` | the walk-forward replay path (`tools/walkforward_sim.py`) |
| `03_model_and_tests` / `04_backtest_and_montecarlo` | `tools/retrain_from_replay.py`, `backtest/`, the regression guard |

If a copy-trade use case is ever green-lit, the natural shape is **not** a
polybot port but a new detector family that (a) ingests leaderboard fills to a
Timescale table, (b) computes replication + self-consistency scores as
*features*, and (c) rides the existing `ai_signals` / regime-orchestrator rails.
That reuses the fleet's contracts instead of standing up a parallel Java stack.

---

## 7. Secondary — ClickHouse ingestion design as reference

The task asked whether polybot's ClickHouse + Redpanda ingestion is a reference
for Kythera market-data persistence. **Verdict: no, keep Timescale.**

- Kythera's market-data layer is PostgreSQL + TimescaleDB (production runs 17.6 +
  TimescaleDB 2.26; the hypertable migration is designed in
  [`TIMESCALE_R1_MIGRATION.md`](TIMESCALE_R1_MIGRATION.md)).
- Public fills are **append-only, timestamped, low-cardinality per address** —
  the exact shape a Timescale hypertable is built for. Ingesting them needs one
  table (`hl_fills`, PK `(address, tid)`, partition on `time`), not a columnar
  OLAP store + an event bus.
- ClickHouse + Redpanda earns its keep at **full-tape CEX scale** (every public
  trade on every symbol, millions of rows/min). That is the *hypothetical*
  TimescaleDB-vs-columnar question already flagged as operator-gated infra (Z0 /
  the 9,300-table collapse), not something a leaderboard-fills spike justifies.
- polybot's genuinely reusable idea here is **schema discipline** (typed fill
  events, snapshot extraction to a frozen file for offline reproducibility) — and
  Kythera already does the frozen-snapshot thing via JSONL replay artifacts.

Keep ClickHouse as a documented "if we ever ingest the full trade tape" note, not
as an action item.

---

## 8. Next steps (only if the venue decision opens it)

Gated on the **open Hyperliquid venue decision** and a **copy-trade / trader-
screening use case** being chosen:

1. Leaderboard screener: pull the 40k board weekly, rank by self-consistency ×
   risk-adjusted `windowPerformances`, shortlist reproducible traders.
2. Timescale `hl_fills` ingestion (one hypertable, per-address incremental pull
   with the 10k ceiling in mind).
3. Promote the PoC's marginal score into `core/` as a feature builder; add
   holding-time + sim-trade-match dimensions before any capital gating.
4. Wire scores as features into a new detector on the existing `ai_signals` /
   regime-orchestrator rails — no parallel stack.

Until then this stays a spike. The PoC is the deliverable; it proves the door is
open without walking through it.

---

## Appendix — reproduce the numbers

```bash
python tools/research/hl_replication_poc.py                    # top-2 leaderboard traders
python tools/research/hl_replication_poc.py 0x85ec…2052 0xf5d8…ad53   # explicit pair
```

DB-free, stdlib-only, no imports from `core`, writes nothing. Talks only to
Hyperliquid's public REST surface.
