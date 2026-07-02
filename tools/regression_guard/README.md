# Indicator regression guard

**Phase 1 of the Kythera Staged-C refactor** (KB task `T-2026-CU-9050-009`,
premortem `T-2026-CU-9050-007` point 3 — *"enforcement over discipline"*).

## What it does

Freezes a **golden snapshot** of the live indicator engine's output for a fixed
set of inputs, then **re-checks it on every commit** via a pre-commit hook. If a
code change silently alters what the indicators compute, the commit is
**blocked**. This protects the live trading signal path while the refactor
mutates code in place.

It guards exactly one seam — the *deterministic* one:
`2_indicator_engine.calculate_indicators_optimized(df, timeframe)`. That
function turns an OHLCV frame into ~108 indicator columns using only
numpy/pandas, **no RNG, no clock, no I/O** — so its output is reproducible bit
for bit given the same input. Everything downstream (the strategies and ~27
bots) depends on live prices, `datetime.now`, DB state and Telegram, so it
cannot be snapshotted deterministically here — that is a later, separate step.

This is **not** the migration parity oracle (that compares a future TimescaleDB
rebuild against today's output). It is the in-place-mutation guard.

## The pieces

```
tools/regression_guard/
  guard.py          entrypoint / CLI (verify | refresh | status | extract | smoke)
  rgcore.py         serialization, comparison, fixtures, golden compute, manifest
  tolerances.json   per-feature tolerance bands (default is tight: atol=1e-9)
  fixtures/*.npz    frozen OHLCV INPUT (immutable, committed)  ← the reference
  golden/*.npz      frozen indicator OUTPUT snapshot (immutable, committed)
  golden/manifest.json   sha256 + provenance of every fixture/golden file
```

Serialization is `np.savez` (numpy is already a hard dependency) — chosen because
it round-trips float64 **exactly**, unlike CSV, and because we do **not** install
new packages (e.g. pyarrow) into the live bot's Python environment.

## Determinism

`guard.py` re-execs itself once with BLAS threads pinned
(`OMP/OPENBLAS/MKL_NUM_THREADS=1`) and `PYTHONHASHSEED=0`, so float
reduction order (dot products, `std`, `linregress`) is stable across machines.
The tolerance band in `tolerances.json` is a thin safety net on top of that, not
the primary mechanism — the compute is deterministic by construction.

## Usage

```bash
# See state (armed = a golden snapshot exists)
python tools/regression_guard/guard.py status

# Run the guard manually (the pre-commit hook runs this as `verify`)
python tools/regression_guard/guard.py verify

# Prove the machinery end-to-end on synthetic data (no DB)
python tools/regression_guard/guard.py smoke
```

### Arming it (one-time, needs the live DB)

The guard ships **not armed**: `fixtures/` and `golden/` are empty, so `verify`
passes as a no-op and never blocks a commit. To arm it, on a host where
`core/.env` carries real DB credentials:

```bash
# 1) freeze the OHLCV fixtures from the live DB (default: 4 coins x 6 timeframes x 600 bars)
python tools/regression_guard/guard.py extract
#    (override breadth: --coins BTCUSDT,ETHUSDT --timeframes 1h,4h --bars 800)

# 2) freeze the golden indicator snapshot from those fixtures
KYTHERA_GOLDEN_REFRESH=1 python tools/regression_guard/guard.py refresh

# 3) commit fixtures/ + golden/ + manifest.json — the guard is now armed
```

The reference **decays** — capture it sooner rather than later, before more
drift accumulates in the live signal path.

## The immutability contract

The golden snapshot is **immutable by default**. `verify` never writes it; only
`refresh` does, and `refresh` is gated behind `KYTHERA_GOLDEN_REFRESH=1` (or
`--force`). So refreshing the baseline is always a **deliberate act** that shows
up in the diff and demands a justification in the commit message.

When `verify` blocks a commit, there are two honest paths:

1. **The change was unintended** → the guard just caught a regression. Fix it.
2. **The change was intentional** (you meant to change what the indicators
   compute) → refresh the snapshot on purpose:
   ```bash
   KYTHERA_GOLDEN_REFRESH=1 python tools/regression_guard/guard.py refresh
   ```
   then commit the changed `golden/` explaining *why* the output moved.

Never silently regenerate the snapshot to make red go away — that bakes the
skew into the baseline and blinds the guard to the very leak it exists to catch.
