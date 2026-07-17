#!/usr/bin/env py -3.13
# tools/wick_reversal_study.py — K11 · WSH1 (Wick-Reversal Stop-Hunt) study (T-2026-CU-9050-145)
"""Read-only event study + replay: do 15m candles with EXTREME wick geometry +
a VOLUME climax (a liquidation-cascade proxy — we have no liquidation feed) mark
short-term reversal points that carry positive NET edge under OUR deployable
geometry (smart-targets + fixed SL, first-touch)?

Hypothesis (docs/MODEL_CANDIDATES_SPEC_2026-07.md §K11): a candle with a long
LOWER wick + volume climax + close-recovery marks a LONG bounce (mirror: long
UPPER wick → SHORT). Evidence external = mechanics only (TradingView "Liquidation
Cascade Detector"; performance claims ignored, F11/F12); internal = the PEX1
lesson. Only CLOSED candles (Rule 5); the study lives entirely on 15m — 5m has
only ~1 month retention, 15m has ~1 year (spec data-table).

PEX1 lesson (spec §K11, honored in code): the information sits in the INTRADAY
window AROUND the event. We do NOT fall back to 1h context features — event
detection, the as-of S/R frame AND the first-touch exit scan are all on 15m. If
15m proves too coarse, the honest answer is waiting for ticker_10s / PEX2
maturity, NOT reaching for 1h context.

Event definition (parametrized grid, only CLOSED candles — entry = the event
candle's CLOSE):
  * LONG (lower wick):  lower_wick ≥ k·ATR14  AND  volume ≥ m·vol_sma20
                        AND close recovered ≥ 50% of the candle range.
  * SHORT (upper wick): upper_wick ≥ k·ATR14  AND  volume ≥ m·vol_sma20
                        AND close recovered ≥ 50% of the candle range (mirror).
  * lower_wick = min(open,close) − low ; upper_wick = high − max(open,close).
  * Recovery is operationalized on the candle RANGE (unambiguous, no-lookahead):
    LONG  (close−low)/(high−low) ≥ 0.50 (close in the upper half → the down-spike
    was rejected); SHORT (high−close)/(high−low) ≥ 0.50. NOTE: "≥50% of the wick"
    cannot mean the lower shadow itself — close ≥ min(open,close) makes that
    trivially 100%; the range-half rule is the faithful operationalization and is
    documented as such.
  * ATR14 / vol_sma20 are TRAILING and EXCLUDE the event candle (…rolling.mean()
    .shift(1)) so the event candle's own extreme range/volume never self-inflates
    its threshold. TR uses the exact get_atr() formula (fmax of the three ranges);
    the current-excluded window is a deliberate deviation from get_atr's inclusive
    tail (rationale: baseline = the "normal" volatility the wick is judged against).
  * Grid: k ∈ {1.5, 2, 3} × m ∈ {3, 5}. Per (k,m,direction) a re-entry-after-exit
    dedup (the prior trade's geometry exit closes the position) so trades within a
    cell never overlap — the 4h-cooldown convention, ported to 15m.

TWO populations (spec §K11.2):
  (a) ALL deduped events.
  (b) CASCADE: the SUBSET of (a) whose entry falls ≤ 60 min AFTER a
      pump_dump_events row FOR THE SAME COIN. pump_dump_events.spike_time is
      TIMESTAMPTZ/UTC (verified docs/schema.sql) — we compare in UTC epoch
      seconds, window = spike_time ∈ [entry_time − 60min, entry_time]. (b) ⊆ (a):
      dedup runs ONCE on the all-stream, cascade is a label on it — so cascade n
      ≤ all n and the two populations are directly comparable ("any wick" vs
      "wick after a cascade").

Labels (spec §K11.3 — wired EXACTLY as tools/tsmom_study.py):
  get_hvn_and_sr_levels(df = as-of trailing 15m frame) → hvn_sr_trade_geometry →
  ensure_min_tp_distance → simulate_exit (first-touch TP-vs-SL, round-trip taker
  fee). Entry = event-candle close; the exit scan starts at the NEXT 15m candle
  (strictly after entry). Strictly as-of — the S/R frame ends at the event candle,
  the exit scan is forward-only, NO live lookups, NO lookahead.

Verdict / stop-criterion (§K11.4): a cell PASSES if val AND test avg net PnL are
BOTH > 0 at n_test ≥ MIN_TEST_N. Threshold/cell selection is made on VAL only;
TEST is read once. NO passing cell ⇒ WSH1 falsified for our stack — a NEGATIVE
result is SUCCESS, documented and parked (do NOT force a positive). WR alone is
not decisive (Rule 8) — the verdict rests on net-PnL expectancy consistent across
the chrono val/test halves.

READ-ONLY: SELECTs only, BELOW_NORMAL priority, sole job on a real-money VPS.
Artifacts → staging_models/ ONLY (Rule 2). Survivorship (Rule 9): coins.json =
active USDT-perps only, delisted coins absent → population skews to survivors,
documented not corrected. Fees per Rule 10 = walkforward_sim.FEE_PER_SIDE
(round-trip inside simulate_exit).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.candles import read_candles  # noqa: E402
from core.database import db_connection  # noqa: E402
from core.market_utils import load_coins  # noqa: E402
from core.trade_utils import (  # noqa: E402
    ensure_min_tp_distance,
    get_hvn_and_sr_levels,
    hvn_sr_trade_geometry,
)
from tools.walkforward_sim import (  # noqa: E402
    FEE_PER_SIDE,
    check_cpu_headroom,
    set_low_priority,
    simulate_exit,
)

ROUND_TRIP_FEE = 2.0 * FEE_PER_SIDE  # 0.001 = 0.10 % (reported; simulate_exit nets it)

# ── Fixed grid (§K11) ────────────────────────────────────────────────────────
K_GRID = [1.5, 2.0, 3.0]              # lower/upper wick ≥ k · ATR14 (trailing, excl. current)
M_GRID = [3.0, 5.0]                  # volume ≥ m · vol_sma20 (trailing, excl. current)
RECOVERY_MIN = 0.50                   # close recovered ≥ 50% of the candle range
ATR_PERIOD = 14
VOL_SMA_PERIOD = 20
CASCADE_WINDOW_S = 60 * 60            # cascade population: event ≤ 60 min after a pump row
TF = "15m"
BAR_SECONDS = 15 * 60                 # 15m candle → close instant = open_time + 900s
N_PUBLISHED = 3                       # WSH1 would publish 3 TPs (abr1/rub/ats/atb reversal convention)
SR_WINDOW_BARS = 30 * 96             # as-of S/R frame = trailing 30 days of 15m candles
EXIT_SCAN_CAP_BARS = 14 * 96         # bound the first-touch scan to 14d post-entry (reversals resolve fast)
MIN_HISTORY_BARS = 500                # skip coins with < this many closed 15m candles
MIN_SR_BARS = 50                      # get_hvn_and_sr_levels floor (needs ≥50 rows)
MIN_TEST_N = 50                       # stop-criterion trade floor (TEST half) — 15m events are rarer than K1's
DEFAULT_CHECKPOINT_EVERY = 25         # atomic-write partial aggregate + resume-state every N coins
MIN_AVAIL_MB = 500                    # abort (rather than risk the live fleet) below this free RAM

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "staging_models")
# Resume-state lives in the OS temp dir (NEVER the repo) — a transient JSON of the
# streaming accumulators so a watchdog-kill mid-run can be resumed, not restarted.
DEFAULT_STATE_PATH = os.path.join(tempfile.gettempdir(), "wick_reversal_study_state.json")


# ── Encoding-safe printing (cp1252 stdout on the Windows VPS bites on non-ASCII
#    coin symbols — reconfigure to utf-8 AND ascii-fold every dynamic print) ────
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


def _safe(s: object) -> str:
    return str(s).encode("ascii", "replace").decode()


# ── Load / features ──────────────────────────────────────────────────────────
def load_15m(conn, symbol: str) -> pd.DataFrame | None:
    """All CLOSED 15m candles, ascending, tz-aware UTC open_time + numeric OHLCV.
    read_candles returns TIMESTAMPTZ → we normalize to UTC explicitly so cascade
    windows and the chrono split anchor on UTC, never the DB session's offset."""
    try:
        df = read_candles(
            conn, symbol, TF, include_forming=False,
            columns=("open_time", "open", "high", "low", "close", "volume"),
        )
    except Exception:
        conn.rollback()
        return None
    if df is None or df.empty or len(df) < MIN_HISTORY_BARS:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["high", "low", "close"]).sort_values("open_time").reset_index(drop=True)
    return df if len(df) >= MIN_HISTORY_BARS else None


def trailing_atr14(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:  # noqa: E741
    """ATR14[t] = mean(TR over the 14 candles PRECEDING t) — trailing, EXCLUDES
    the current candle (…rolling(14).mean().shift(1)). TR is get_atr()'s exact
    formula: fmax(h−l, |h−prev_c|, |l−prev_c|); first TR = h−l."""
    prev_c = np.empty_like(c)
    prev_c[0] = np.nan
    prev_c[1:] = c[:-1]
    tr = np.fmax(h - l, np.fmax(np.abs(h - prev_c), np.abs(l - prev_c)))
    tr[0] = h[0] - l[0]  # first candle: only high−low (get_atr's fmax-with-NaN semantics)
    return pd.Series(tr).rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean().shift(1).to_numpy()


def trailing_vol_sma20(v: np.ndarray) -> np.ndarray:
    """vol_sma20[t] = mean(volume over the 20 candles PRECEDING t) — trailing,
    excludes the current candle so a volume climax stands out against baseline."""
    return pd.Series(v).rolling(VOL_SMA_PERIOD, min_periods=VOL_SMA_PERIOD).mean().shift(1).to_numpy()


def load_spike_epochs(conn, symbol: str) -> np.ndarray:
    """Sorted UTC epoch-seconds of this coin's pump_dump_events (cascade context).
    spike_time is TIMESTAMPTZ/UTC (docs/schema.sql). SELECT-only."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT spike_time FROM pump_dump_events WHERE symbol = %s ORDER BY spike_time",
                (symbol,),
            )
            rows = cur.fetchall()
    except Exception:
        conn.rollback()
        return np.array([], dtype=float)
    out = []
    for (ts,) in rows:
        if ts is None:
            continue
        t = pd.Timestamp(ts)
        t = t.tz_convert("UTC") if t.tzinfo is not None else t.tz_localize("UTC")
        out.append(t.timestamp())
    return np.array(sorted(out), dtype=float)


def is_cascade(spikes: np.ndarray, entry_epoch: float) -> bool:
    """True if any spike falls in [entry_epoch − 60min, entry_epoch] (event ≤ 60
    min AFTER a cascade)."""
    if spikes.size == 0:
        return False
    lo = entry_epoch - CASCADE_WINDOW_S
    i = int(np.searchsorted(spikes, lo, side="left"))
    return i < spikes.size and spikes[i] <= entry_epoch


# ── Per-coin replay ──────────────────────────────────────────────────────────
def replay_coin(conn, symbol: str) -> list[dict]:
    """All deduped wick-reversal events for one coin across the full k×m grid and
    both directions; each event carries geo net PnL + a cascade flag. Geometry
    labels are cached per (candle-index, direction) and reused across all cells."""
    df = load_15m(conn, symbol)
    if df is None:
        return []
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)  # noqa: E741
    c = df["close"].to_numpy(float)
    v = df["volume"].to_numpy(float)
    n = len(c)
    # naive-UTC open_time array for the first-touch exit scan; close-epoch per candle.
    t_naive = df["open_time"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
    open_epoch = df["open_time"].astype("int64").to_numpy() / 1e9
    close_epoch = open_epoch + BAR_SECONDS

    atr = trailing_atr14(h, l, c)
    vsma = trailing_vol_sma20(v)
    rng = h - l
    minoc = np.minimum(o, c)
    maxoc = np.maximum(o, c)
    lower_wick = minoc - l
    upper_wick = h - maxoc
    with np.errstate(divide="ignore", invalid="ignore"):
        rec_long = (c - l) / rng     # close in upper half → down-spike rejected
        rec_short = (h - c) / rng    # close in lower half → up-spike rejected

    spikes = load_spike_epochs(conn, symbol)

    geo_cache: dict[tuple[int, bool], dict | None] = {}

    def geo_for(i: int, is_long: bool) -> dict | None:
        key = (i, is_long)
        if key in geo_cache:
            return geo_cache[key]
        out = None
        if i >= MIN_SR_BARS and i + 1 < n:
            entry = float(c[i])
            if np.isfinite(entry) and entry > 0:
                lo = max(0, i - SR_WINDOW_BARS + 1)
                frame = pd.DataFrame({"high": h[lo:i + 1], "low": l[lo:i + 1], "close": c[lo:i + 1]})
                supps, resis = get_hvn_and_sr_levels(None, None, entry, df=frame)
                del frame  # release the as-of window immediately (memory O(cells))
                _e2, sl, t_cands = hvn_sr_trade_geometry(entry, is_long, supps, resis)
                targets = ensure_min_tp_distance(list(t_cands[:20]), entry, is_long, min_pct=0.05)
                if targets:
                    start = i + 1
                    hi = min(n, start + EXIT_SCAN_CAP_BARS)
                    if start < hi:
                        res = simulate_exit(
                            t_naive[start:hi], h[start:hi], l[start:hi], c[start:hi],
                            0, "LONG" if is_long else "SHORT", entry, sl, targets,
                            min(N_PUBLISHED, len(targets)),
                        )
                        exit_ep = None
                        if res.get("exit_time"):
                            exit_ep = pd.Timestamp(res["exit_time"]).tz_localize("UTC").timestamp()
                        out = {"net": res["net_pnl_pct"] / 100.0, "exit_epoch": exit_ep}
        geo_cache[key] = out
        return out

    events: list[dict] = []
    for direction in ("LONG", "SHORT"):
        is_long = direction == "LONG"
        wick = lower_wick if is_long else upper_wick
        rec = rec_long if is_long else rec_short
        # candidate candles: recovery + finite baselines + positive range (grid-independent)
        base = (rec >= RECOVERY_MIN) & (rng > 0) & np.isfinite(atr) & (atr > 0) & np.isfinite(vsma) & (vsma > 0)
        cand_idx = np.nonzero(base)[0]
        for k in K_GRID:
            for m in M_GRID:
                last_exit_ep: float | None = None
                for i in cand_idx:
                    i = int(i)
                    if not (wick[i] >= k * atr[i] and v[i] >= m * vsma[i]):
                        continue
                    ep = float(close_epoch[i])
                    if last_exit_ep is not None and ep < last_exit_ep:
                        continue  # position still open (re-entry only after prior geometry exit)
                    geo = geo_for(i, is_long)
                    if geo is None:
                        continue
                    last_exit_ep = geo["exit_epoch"] if geo["exit_epoch"] is not None else ep
                    events.append({
                        "k": k, "m": m, "dir": direction,
                        "entry_epoch": ep,
                        "entry_iso": dt.datetime.fromtimestamp(ep, dt.timezone.utc).isoformat(),
                        "net": geo["net"],
                        "cascade": is_cascade(spikes, ep),
                    })
    return events


# ── Streaming accumulators (memory O(cells), NOT O(events)) ──────────────────
def _new_stat() -> dict:
    return {"n": 0, "wins": 0, "sum": 0.0}


def _upd(s: dict, x: float) -> None:
    s["n"] += 1
    if x > 0:
        s["wins"] += 1
    s["sum"] += x


def _stat_out(s: dict) -> dict:
    n = s["n"]
    if n == 0:
        return {"n": 0, "wr": None, "avg_net_pct": None}
    return {"n": n, "wr": round(s["wins"] / n, 4), "avg_net_pct": round(s["sum"] / n * 100, 4)}


def _new_cell(pop: str, k: float, m: float, direction: str) -> dict:
    return {
        "pop": pop, "k": k, "m": m, "dir": direction,
        "geo": {"all": _new_stat(), "val": _new_stat(), "test": _new_stat()},
        "months": {},
    }


def _fold_one(acc: dict, pop: str, ev: dict, split_epoch: float) -> None:
    ck = f"{pop}|k{ev['k']}|m{ev['m']}|{ev['dir']}"
    cell = acc.get(ck)
    if cell is None:
        cell = _new_cell(pop, ev["k"], ev["m"], ev["dir"])
        acc[ck] = cell
    half = "val" if ev["entry_epoch"] < split_epoch else "test"
    g = ev["net"]
    _upd(cell["geo"]["all"], g)
    _upd(cell["geo"][half], g)
    mo = ev["entry_iso"][:7]
    ms = cell["months"].get(mo)
    if ms is None:
        ms = _new_stat()
        cell["months"][mo] = ms
    _upd(ms, g)


def fold_event(acc: dict, counters: dict, ev: dict, split_epoch: float) -> None:
    """Fold into the ALL population always; into CASCADE iff the event followed a
    pump within 60 min (cascade ⊆ all)."""
    _fold_one(acc, "all", ev, split_epoch)
    counters["n_all"] += 1
    if ev["cascade"]:
        _fold_one(acc, "cascade", ev, split_epoch)
        counters["n_cascade"] += 1


def build_cells(acc: dict) -> dict:
    out: dict = {}
    for ck, c in acc.items():
        months = {mo: _stat_out(s) for mo, s in sorted(c["months"].items()) if s["n"] >= 10}
        out[ck] = {
            "pop": c["pop"], "k": c["k"], "m": c["m"], "dir": c["dir"],
            "geometry": {
                "all": _stat_out(c["geo"]["all"]),
                "val": _stat_out(c["geo"]["val"]),
                "test": _stat_out(c["geo"]["test"]),
            },
            "months_geo": months,
        }
    return out


def derive_verdict(analysis: dict) -> dict:
    """Stop-criterion (§K11): a cell PASSES if val AND test avg net PnL are both
    > 0 at n_test ≥ MIN_TEST_N. Threshold picked on VAL, test read once. No passing
    cell ⇒ WSH1 falsified for our stack (a NEGATIVE result is SUCCESS)."""
    passing = []
    val_positive = []
    for ck, c in analysis.items():
        g = c["geometry"]
        val, test = g["val"], g["test"]
        if val["avg_net_pct"] is not None and val["avg_net_pct"] > 0:
            val_positive.append(ck)
        if (
            val["avg_net_pct"] is not None and test["avg_net_pct"] is not None
            and val["avg_net_pct"] > 0 and test["avg_net_pct"] > 0 and (test["n"] or 0) >= MIN_TEST_N
        ):
            passing.append({
                "cell": ck, "val_avg_net_pct": val["avg_net_pct"], "val_n": val["n"],
                "test_avg_net_pct": test["avg_net_pct"], "test_n": test["n"], "test_wr": test["wr"],
            })
    passing.sort(key=lambda x: x["test_avg_net_pct"], reverse=True)

    # Best cell BY VAL among those with a testable val sample — the honest pick.
    best_val = None
    for ck, c in analysis.items():
        val, test = c["geometry"]["val"], c["geometry"]["test"]
        if val["avg_net_pct"] is None or (val["n"] or 0) < MIN_TEST_N:
            continue
        cand = {
            "cell": ck, "val_avg_net_pct": val["avg_net_pct"], "val_n": val["n"],
            "test_avg_net_pct": test["avg_net_pct"], "test_n": test["n"], "test_wr": test["wr"],
        }
        if best_val is None or (val["avg_net_pct"] or -1e9) > (best_val["val_avg_net_pct"] or -1e9):
            best_val = cand

    verdict = "wick-reversal-edge-found" if passing else "no-op/WSH1-falsified"
    return {
        "verdict": verdict,
        "min_test_n": MIN_TEST_N,
        "n_cells": len(analysis),
        "n_cells_val_positive": len(val_positive),
        "n_cells_passing": len(passing),
        "passing_cells": passing[:10],
        "best_cell_selected_on_val": best_val,
    }


# ── Reporting ────────────────────────────────────────────────────────────────
def build_markdown(meta: dict, analysis: dict, verdict: dict) -> str:
    capped = bool(meta.get("limit_symbols"))
    L: list[str] = []
    L.append("# K11 · WSH1 — Wick-Reversal Stop-Hunt event study (T-2026-CU-9050-145)\n")
    L.append(
        f"_Generated {meta['generated_at']} · read-only 15m event study · fee/side {FEE_PER_SIDE} "
        f"(round-trip {ROUND_TRIP_FEE:.4f}) · {meta['n_coins']} coins · "
        f"{meta['n_events_all']:,} all-events / {meta['n_events_cascade']:,} cascade-events_\n"
    )
    L.append(f"**VERDICT: {verdict['verdict']}** · status: `{meta['status']}`\n")
    L.append(
        f"- grid cells: {verdict['n_cells']} (2 populations × k{K_GRID} × m{M_GRID} × 2 dir) · "
        f"val-positive: {verdict['n_cells_val_positive']} · PASSING (val>0 AND test>0 at "
        f"n_test≥{verdict['min_test_n']}): **{verdict['n_cells_passing']}**\n"
    )
    bv = verdict["best_cell_selected_on_val"]
    if bv:
        L.append(
            f"- best cell selected on VAL: `{bv['cell']}` → val {bv['val_avg_net_pct']}% "
            f"(n={bv['val_n']}) · **test {bv['test_avg_net_pct']}% (n={bv['test_n']}, WR={bv['test_wr']})**\n"
        )

    # ── Acceptance Criteria (§K11, binary) ──
    L.append("## Acceptance Criteria (§K11, binary)\n")
    ac = [
        ("Event grid `lower_wick ≥ k·ATR14` k∈{1.5,2,3} × `volume ≥ m·vol_sma20` m∈{3,5} × recovery ≥ 50%",
         "K_GRID/M_GRID/RECOVERY_MIN; trailing_atr14/trailing_vol_sma20 (current-excluded); replay_coin trigger"),
        ("Mirror upper wick → SHORT; entry = CLOSE of the CLOSED event candle; direction WITH the bounce",
         "replay_coin loops LONG+SHORT; entry=c[i]; include_forming=False (Rule 5)"),
        ("TWO populations: (a) all events, (b) events ≤60min after pump_dump_events (cascade ⊆ all)",
         "fold_event → 'all' always, 'cascade' iff is_cascade(); spike_time TIMESTAMPTZ/UTC window [entry−60m, entry]"),
        ("Labels = get_hvn_and_sr_levels(df=as-of) → geometry → simulate_exit, strictly as-of / no lookahead",
         "geo_for(): as-of 15m frame ends at event candle, exit scan starts at i+1, forward-only"),
        ("Report Rule-8 standard: per-cell n / WR / avg net PnL incl. fees; chrono val/test; selection on val ONLY",
         "_stat_out (n/wr/avg_net_pct); simulate_exit nets round-trip fee; compute_split; derive_verdict picks on val"),
        ("Stop-criterion: no val+test-positive cell ⇒ falsified (No-op-Done, not forced positive)",
         "derive_verdict → 'no-op/WSH1-falsified' when passing==[]"),
        ("PEX1 lesson stated: info is intraday; NO 1h fallback",
         "this report §PEX1 + module docstring; all stages on 15m"),
        ("Survivorship (Rule 9) + closed candles (Rule 5) + 15m sort order documented",
         "§Population & caveats; load_15m sort_values ASC; include_forming=False"),
        ("Resume/checkpoint machinery: streaming O(cells) accumulators, atomic temp+rename state in OS temp, "
         "--resume/--state-path/--checkpoint-every/--progress-every/--skip-cpu-check, RAM guard, peak-RSS, encoding-safe",
         "save_state/load_state (os.replace); DEFAULT_STATE_PATH=tempdir; argparse; _avail_mb guard; _safe()"),
    ]
    done = "partial (sampling cap)" not in meta["status"]
    mark = "✅" if done else "☐"
    for crit, how in ac:
        L.append(f"- {mark} {crit}  \n  _verify: {how}_")
    if capped:
        L.append("\n_Note: this artifact is a SMOKE (sampling-capped); functional criteria are exercised "
                 "end-to-end but the VERDICT is not universe-final until the full run._")
    L.append("")

    L.append("**Reuse-vs-Build:** REUSE the tsmom_study.py label+resume machinery wholesale "
             "(get_hvn_and_sr_levels→hvn_sr_trade_geometry→ensure_min_tp_distance→simulate_exit, "
             "streaming O(cells) accumulators, atomic checkpoint/--resume); BUILD only the 15m "
             "wick-geometry + volume-climax event detector and the two-population (all vs cascade) fold.\n")

    if verdict["passing_cells"]:
        L.append("## Passing cells (val>0 AND test>0, n_test≥%d)\n" % verdict["min_test_n"])
        L.append("| cell | val avg% (n) | test avg% (n) | test WR |")
        L.append("|---|--:|--:|--:|")
        for p in verdict["passing_cells"]:
            L.append(f"| {p['cell']} | {p['val_avg_net_pct']} ({p['val_n']}) | "
                     f"{p['test_avg_net_pct']} ({p['test_n']}) | {p['test_wr']} |")
        L.append("")

    L.append("## Full grid — geometry net PnL, chrono val/test split\n")
    L.append("| cell | all n | all avg% | all WR | val n | val avg% | test n | test avg% | test WR |")
    L.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    for ck in sorted(analysis.keys()):
        g = analysis[ck]["geometry"]
        a, val, test = g["all"], g["val"], g["test"]
        L.append(f"| {ck} | {a['n']} | {a['avg_net_pct']} | {a['wr']} | {val['n']} | {val['avg_net_pct']} "
                 f"| {test['n']} | {test['avg_net_pct']} | {test['wr']} |")
    L.append("")

    L.append("## PEX1 lesson (§K11.5)\n")
    L.append(
        "The information sits in the INTRADAY window around the event. Event detection, the as-of S/R "
        "frame AND the first-touch exit are ALL on 15m — we deliberately do NOT fall back to 1h context "
        "features (the falsified PEX1 path). If 15m proves too coarse here, the answer is waiting for "
        "ticker_10s / PEX2 maturity, NOT 1h.\n"
    )

    L.append("## Population & caveats\n")
    L.append(f"- run status: {meta['status']} · coins done: {meta.get('n_coins_done')} of {meta['n_coins']} "
             f"(universe {meta['n_universe']})")
    L.append(f"- events: {meta['n_events_all']:,} all · {meta['n_events_cascade']:,} cascade (≤60min after a pump_dump_events row)")
    L.append(f"- peak process RSS: {meta.get('peak_rss_mb')} MB (streaming accumulators, memory O(cells), not O(events))")
    L.append(f"- chrono val/test split epoch (UTC): {meta.get('split_iso')} — FIXED calendar midpoint of the "
             "BTCUSDT 15m window (longest-history proxy); val=earlier half, test=later half; selection on VAL only")
    L.append(f"- geometry exit: first-touch TP-vs-SL on 15m candles, {N_PUBLISHED} published TPs, scan capped "
             f"{EXIT_SCAN_CAP_BARS // 96}d; as-of S/R frame = trailing {SR_WINDOW_BARS // 96}d of 15m")
    L.append(f"- ATR14 & vol_sma20 are TRAILING and EXCLUDE the event candle (rolling.mean().shift(1)); "
             "TR = get_atr()'s fmax formula; recovery = (close−low)/(high−low)≥0.5 (range-half operationalization)")
    L.append(f"- fees (Rule 10): FEE_PER_SIDE={FEE_PER_SIDE} netted inside simulate_exit (round-trip {ROUND_TRIP_FEE})")
    L.append("- **Survivorship bias (Rule 9)**: coins.json lists ACTIVE USDT-perps; delisted coins are absent → "
             "the population skews to survivors. Documented, not corrected.")
    L.append("- **Only closed candles (Rule 5)**: read_candles(include_forming=False); ATR/vol baselines are "
             "trailing/as-of (no lookahead); the exit scan starts strictly AFTER the entry candle.")
    L.append("- **15m sort order**: load_15m sorts ASC by open_time before array-izing; the exit scan and "
             "searchsorted assume ascending time — indexing was NOT 'simplified' without checking direction.")
    L.append("- **WR is not decisive (Rule 8)**: the verdict rests on net-PnL expectancy consistent across the "
             "chrono val/test halves.")
    L.append(f"- CPU-check override: --skip-cpu-check={meta['skip_cpu_check']} (VPS is CPU-saturated; the "
             "walkforward_sim guard would abort this read-only BELOW_NORMAL job).")
    if capped:
        L.append(f"- ⚠ SAMPLING CAP: --limit-symbols={meta['limit_symbols']} (NOT a full run; VERDICT not universe-final).")
    return "\n".join(L)


# ── Memory / checkpoint / split helpers ──────────────────────────────────────
def _avail_mb() -> float | None:
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 * 1024)
    except Exception:
        return None


def _rss_mb() -> float | None:
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        return None


def compute_split(conn) -> tuple[float, str | None]:
    """Fixed chrono val/test divider = calendar midpoint of the BTCUSDT 15m data
    window (longest-history proxy). Decided up front so the streaming fold assigns
    each event to val/test without retaining any."""
    df = load_15m(conn, "BTCUSDT")
    if df is None or df.empty:
        return 0.0, None
    tmin = df["open_time"].min().timestamp()
    tmax = df["open_time"].max().timestamp()
    mid = (tmin + tmax) / 2.0
    return mid, dt.datetime.fromtimestamp(mid, dt.timezone.utc).isoformat()


def write_outputs(meta: dict, cells: dict, verdict: dict, json_path: str, md_path: str) -> None:
    """Atomic write (temp + os.replace) so a mid-run kill leaves a valid file."""
    out = {"meta": meta, "verdict": verdict, "cells": cells}
    tmp = json_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    os.replace(tmp, json_path)
    tmp_md = md_path + ".tmp"
    with open(tmp_md, "w", encoding="utf-8") as fh:
        fh.write(build_markdown(meta, cells, verdict))
    os.replace(tmp_md, md_path)


def save_state(state_path: str, state: dict) -> None:
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, state_path)


def load_state(state_path: str) -> dict | None:
    if not os.path.exists(state_path):
        return None
    try:
        with open(state_path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-symbols", type=int, default=None, help="Smoke pass: first N coins only.")
    ap.add_argument("--skip-cpu-check", action="store_true", default=False,
                    help="Bypass walkforward_sim.check_cpu_headroom (default OFF). Needed on the "
                         "CPU-saturated VPS; this job is read-only + BELOW_NORMAL priority.")
    ap.add_argument("--progress-every", type=int, default=25, help="Print progress every N coins.")
    ap.add_argument("--checkpoint-every", type=int, default=DEFAULT_CHECKPOINT_EVERY,
                    help="Atomic-write the partial aggregate + resume-state every N coins.")
    ap.add_argument("--resume", action="store_true", default=False,
                    help="Resume from the saved accumulator state (survives watchdog-kills). "
                         "The relaunch wrapper passes this on every segment; exit 0 = fully complete.")
    ap.add_argument("--state-path", default=DEFAULT_STATE_PATH,
                    help="Path to the transient resume-state JSON (OS temp dir, never the repo).")
    args = ap.parse_args()

    set_low_priority()
    if not args.skip_cpu_check:
        check_cpu_headroom()
    else:
        print("CPU-check SKIPPED (--skip-cpu-check): read-only BELOW_NORMAL job on saturated VPS.")
    os.makedirs(OUT_DIR, exist_ok=True)

    avail0 = _avail_mb()
    if avail0 is not None:
        print(f"RAM available at start: {avail0:.0f} MB")
        if avail0 < MIN_AVAIL_MB:
            print(f"ABORT: only {avail0:.0f} MB free (< {MIN_AVAIL_MB} MB) — refusing to risk the "
                  f"live fleet. Report back rather than run.")
            return 2

    universe = load_coins("coins.json", usdt_only=True, uppercase=True)
    coins = universe[: args.limit_symbols] if args.limit_symbols else universe

    json_path = os.path.join(OUT_DIR, "wick_reversal_study.json")
    md_path = os.path.join(OUT_DIR, "wick_reversal_study.md")
    state_path = args.state_path

    acc: dict = {}
    counters = {"n_all": 0, "n_cascade": 0}
    n_done = 0
    peak_rss = 0.0
    processed: set[str] = set()
    resumed_split_mid = None
    resumed_split_iso = None

    if args.resume:
        st = load_state(state_path)
        if st is not None and st.get("universe_hash") == len(universe):
            acc = st["acc"]
            counters = st["counters"]
            n_done = st["n_done"]
            peak_rss = st.get("peak_rss", 0.0)
            processed = set(st.get("processed", []))
            resumed_split_mid = st.get("split_mid")
            resumed_split_iso = st.get("split_iso")
            print(f"RESUMED: {n_done} coins folded ({counters['n_all']:,} all-events), "
                  f"{len(processed)} in processed-set")
        else:
            print("RESUME requested but no compatible state found — starting fresh.")

    with db_connection() as conn:
        if resumed_split_mid is not None:
            split_mid, split_iso_val = resumed_split_mid, resumed_split_iso
        else:
            split_mid, split_iso_val = compute_split(conn)
        print(f"chrono split (UTC): {split_iso_val}")

        def persist_state() -> None:
            save_state(state_path, {
                "universe_hash": len(universe),
                "acc": acc, "counters": counters, "n_done": n_done,
                "peak_rss": peak_rss, "processed": sorted(processed),
                "split_mid": split_mid, "split_iso": split_iso_val,
            })

        def snapshot(final: bool) -> dict:
            cells = build_cells(acc)
            verdict = derive_verdict(cells)
            capped = bool(args.limit_symbols)
            if final:
                status = "partial (sampling cap)" if capped else "complete"
            else:
                status = "partial (checkpoint)"
            meta = {
                "study": "K11 · WSH1 (Wick-Reversal Stop-Hunt event study, 15m)",
                "task": "T-2026-CU-9050-145",
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "status": status,
                "fee_per_side": FEE_PER_SIDE,
                "round_trip_fee": ROUND_TRIP_FEE,
                "n_universe": len(universe),
                "n_coins": len(coins),
                "n_coins_done": n_done,
                "n_events_all": counters["n_all"],
                "n_events_cascade": counters["n_cascade"],
                "grid": {"k": K_GRID, "m": M_GRID, "recovery_min": RECOVERY_MIN,
                         "atr_period": ATR_PERIOD, "vol_sma_period": VOL_SMA_PERIOD,
                         "cascade_window_min": CASCADE_WINDOW_S // 60, "tf": TF},
                "split_iso": split_iso_val,
                "peak_rss_mb": round(peak_rss, 1),
                "limit_symbols": args.limit_symbols,
                "skip_cpu_check": args.skip_cpu_check,
            }
            write_outputs(meta, cells, verdict, json_path, md_path)
            return verdict

        for sym in coins:
            if sym in processed:
                continue
            try:
                evs = replay_coin(conn, sym)
            except Exception as e:  # one bad coin must not kill the run
                conn.rollback()
                print(f"  WARN {_safe(sym)}: {_safe(e)}")
                evs = []
            for e in evs:  # fold then discard — memory O(cells), not O(events)
                fold_event(acc, counters, e, split_mid)
            del evs
            processed.add(sym)
            n_done += 1
            rss = _rss_mb()
            if rss is not None:
                peak_rss = max(peak_rss, rss)
            if n_done % max(1, args.progress_every) == 0:
                av = _avail_mb()
                msg = (f"  ...{n_done}/{len(coins)} coins, {counters['n_all']:,} all-events, "
                       f"{counters['n_cascade']:,} cascade, rss={peak_rss:.0f}MB")
                print(f"{msg} avail={av:.0f}MB" if av is not None else msg)
            if n_done % max(1, args.checkpoint_every) == 0:
                snapshot(final=False)
                persist_state()
                print(f"  checkpoint+state written at {n_done} coins ({counters['n_all']:,} all-events)")

        verdict = snapshot(final=True)

    # Full completion (not a sampling cap): drop the transient resume-state.
    if not args.limit_symbols:
        try:
            if os.path.exists(state_path):
                os.remove(state_path)
        except OSError:
            pass

    print(f"\nVERDICT: {verdict['verdict']}")
    print(f"coins={n_done} all_events={counters['n_all']:,} cascade_events={counters['n_cascade']:,} "
          f"cells={verdict['n_cells']} val_positive={verdict['n_cells_val_positive']} "
          f"passing={verdict['n_cells_passing']} peak_rss={peak_rss:.0f}MB")
    if verdict["best_cell_selected_on_val"]:
        bv = verdict["best_cell_selected_on_val"]
        print(f"best-on-val: {bv['cell']} val={bv['val_avg_net_pct']}% (n={bv['val_n']}) "
              f"test={bv['test_avg_net_pct']}% (n={bv['test_n']})")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
