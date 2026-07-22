# SPEC — GARCH vol-targeting research package (T-2026-KYT-9050-021 + -022)

## Intent
Lift the GARCH walk-forward volatility forecaster + vol-targeting sizer from
`milesdeutscher/garchmethod` (MIT, audit verdict **ADAPT**) into a
self-contained, importable Kythera research package, and adapt its `compare.py`
into a fixed-vs-vol-targeted **validation harness** whose verdict gates whether
vol-targeting is worth wiring into the fleet at all. GARCH answers *how much*
(magnitude/sizing), never *which direction* — orthogonal to Kythera's signal
engines, composed as `signal x size_multiplier`.

**Why build (0b verdict = Adapt-External):** no existing Kythera module does
GARCH vol forecasting or vol-targeting; the OSS source is 424 LOC of CLI scripts,
not an importable library. We port the two core functions + the harness, keep
MIT attribution (`LICENSE.upstream`), and add the Kythera adaptations.

## Akzeptanzkriterien (binär testbar)

### T-021 — GARCH module
- [x] AK1: `size_series` / `size_from_vol` return `target/forecast` clipped to
  `[MIN_SIZE, MAX_LEVERAGE]`; NaN/<=0 forecast -> `MIN_SIZE`; scalar and
  vectorized paths agree. — Test: `backtest/test_garch_vol_target.py`
- [x] AK2: `apply_sizing(signal, size)` = `signal * size`, never flips the sign;
  works for scalars and aligned Series. — Test: `test_garch_vol_target.py`
- [x] AK3: `walkforward_garch` is lookahead-free — forecast at row `t` uses only
  returns `<= t`; with an injected deterministic `fit_fn` the output series is
  exactly reproducible and matches a hand-computed recursion. — Test:
  `backtest/test_garch_walkforward.py`
- [x] AK4: rolling-window cap — a fit never sees more than `max_window` returns;
  `max_window=None` reproduces the upstream expanding window. — Test:
  `test_garch_walkforward.py` (records fit-window lengths via the stub `fit_fn`).
- [x] AK5: `GarchSizer` (per-coin param cache + scheduled refit) fed the return
  history incrementally reproduces `walkforward_garch`'s forecast series
  bar-for-bar, and refits only every `refit_every` bars (fit count asserted). —
  Test: `test_garch_walkforward.py`
- [x] AK6: the `arch`/`ccxt` imports are lazy (module import + all AK1–AK5 tests
  run under plain fleet Python with neither installed). — Test: import guard in
  `test_garch_walkforward.py`.
- [x] AK7 (integration, arch+ccxt present): `walkforward_garch` on a real Binance
  daily series produces a finite, positive vol forecast and a plausible regime.
  — verified by `python tools/research/garch/garch_forecast.py --coin BTC/USDT`.

### T-022 — validation harness
- [x] AK8: harness timing discipline is lookahead-free — `next_ret = ret.shift(-1)`,
  signal@close(t) applied to return(t+1), size@t applied to t+1. — Test:
  `backtest/test_garch_compare.py`.
- [x] AK9: `--signals date,signal` CSV loads, forward-fills onto the price dates,
  clips to `[-1,1]`; a `signals.csv` produced elsewhere (e.g. T-024) runs
  through unchanged. — Test: `test_garch_compare.py`.
- [x] AK10: `perf_stats` (Sharpe / Max-DD / CAGR / final equity) + `worst_month`
  computed correctly on a known synthetic return series. — Test:
  `test_garch_compare.py`.
- [x] AK11: `verdict_from_stats` over a coin sample returns a structured
  PULLS / NO-PULL gate on Sharpe delta + Max-DD/worst-month, with reasons. —
  Test: `test_garch_compare.py`.

## Out of Scope
- Live fleet wiring of the sizer into any bot (composition seam + docs only;
  wiring is a separate, operator-gated task).
- Portfolio correlation layer (that is T-2026-KYT-9050-023, backlog).
- The final Kythera-signal verdict run: real Kythera signals are DB-bound
  (hard rule 1) -> the aggregate verdict over real signals runs in a VPS
  session. Here the harness is validated on ccxt prices + demo/proxy signals.
- Adding `arch`/`ccxt` to the fleet `requirements.txt` (self-contained
  `requirements-garch.txt` instead).

## Scope of consent
**Erlaubt:** new files under `tools/research/garch/**` and `backtest/test_garch_*.py`
on branch `feat/t-2026-kyt-9050-021`; a `CHANGELOG.md` entry + `AUDIT_TODO.md`
note in the PR.
**Verboten:** any edit to live bot/`core/` code, the fleet `requirements.txt`,
`.env`/secrets, model artifacts, `staging_models/`, any DB access, `--no-verify`,
force-push, `gh pr merge` (merge-train only).
**Frag zurück:** wiring the sizer into a live bot; promoting anything; adding
`arch`/`ccxt` to the fleet lockfile; any change touching a running process.
(Pattern: z-coding-standards/references/overeager-mitigation.md.)
