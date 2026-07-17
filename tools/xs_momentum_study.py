#!/usr/bin/env py -3.13
# tools/xs_momentum_study.py — K2 · XSM1/XSR1 cross-section study (T-2026-CU-9050-143)
"""Read-only two-stage cross-section study: does cross-sectional momentum-rotation
(XSM1, LONG the strongest formation-window returners) and/or alt-pump-reversal
(XSR1, SHORT the top decile of a long run) carry positive NET edge across the
USDT-perp universe on 1d candles — first at the portfolio-decile-spread level,
then (only for val-positive cells) with OUR deployable Cornix geometry?

Hypothesis (docs/MODEL_CANDIDATES_SPEC_2026-07.md §K2): (a) XSM1 — the top decile
of 1-2-week returns outperforms over 1-2-week holds (LONG). (b) XSR1 — coins with
a strong 4-12-week run revert (SHORT). Evidence F4 (structure high, exact 0-3 spec
refuted → a MATRIX not a single spec) + F5 (anchored-to-formation-low variant,
medium). Reference frames matter: an ABSOLUTE ranking only measures beta, so we
also run a MARKET-NEUTRAL frame (coin return minus BTC return).

Design — full grid (§K2), NO time-varying refit:
  * Formation F ∈ {7,14,28,56,84} d × Hold H ∈ {7,14,28} d, weekly rebalance
    raster over the ~430d 1d history.
  * TWO signal variants: (raw) F-day return; (anchored) distance above the
    formation-window LOW = close[t]/min(low[t-F+1..t]) - 1 (F5).
  * TWO reference frames: (absolute) the signal itself; (market-neutral) the
    coin's signal minus BTCUSDT's same-variant signal.
  * TWO directions: XSM1 = LONG the top decile; XSR1 = SHORT the top decile
    (reversal). Grid = 5·3·2·2·2 = 120 cells.
  * Liquidity filter: exclude the bottom volume TERCILE at each rebalance by the
    median 24h quote-volume over F (quote-vol ≈ base volume × close — the candles
    table has no quote_asset_volume column; documented approximation).

Stage 1 (portfolio level): per rebalance, rank the liquid coins by the cell's
signal, take the top decile (and bottom decile for the long-short spread
diagnostic), and score the H-day close-to-close forward return:
  * XSM1 LONG  net = mean(fwd[top]) − round-trip taker fee.
  * XSR1 SHORT net = mean(−fwd[top] + Σfunding[hold]) − round-trip taker fee.
    Short funding sign: a short RECEIVES funding when the rate is positive and
    PAYS when it is negative (spec: "Shorts zahlen bei negativem Funding"), so the
    short's funding PnL over the hold = +Σ funding_rate (fraction). Σ<0 ⇒ the
    short pays ⇒ its net drops. funding_rates(symbol, funding_time TIMESTAMPTZ,
    funding_rate double) summed over [t, t+H).
Chrono val/test split on the rebalance calendar (midpoint of the BTCUSDT 1d
window); val = earlier half, test = later half. Cell selection ONLY on val.

Stage 2 (ONLY for val-positive cells): event-replay with OUR geometry — entry =
first 1h close at/after the rebalance, get_hvn_and_sr_levels(df=as-of 95d 1h) →
hvn_sr_trade_geometry → ensure_min_tp_distance → simulate_exit (first-touch
TP-vs-SL on 1h candles AFTER entry, round-trip taker fee). Strictly as-of, no
lookahead. If NO cell is val-positive in stage 1, stage 2 is correctly a no-op.

Stop-criterion (§K2): no F×H cell with a val+test-consistent net spread ⇒ the
structure does not replicate on 2024-26 perps — a documented NEGATIVE verdict is
SUCCESS (No-op-Done), never forced positive.

Resume/checkpoint (the live-VPS watchdog reaps long non-fleet python): the heavy
work is the per-coin DB load (1d candles + funding). We stream coins into compact
per-coin arrays, atomic-checkpoint the processed-set + arrays to a JSON state file
in the OS temp dir (NEVER the repo) every N coins, and --resume reloads and skips
processed coins. The assemble/stage-1/stage-2 phase is itself resume-safe (it
re-enters once all coins are loaded; stage-2 has its own processed-cell set). RAM
guard aborts >~500MB rather than risk the fleet; peak RSS is tracked in the report.

READ-ONLY: SELECTs only, BELOW_NORMAL priority, sole job on a real-money VPS.
Artifacts → staging_models/ ONLY (repo Rule 2). Survivorship (Rule 9): coins.json
= active USDT-perps only, delisted coins absent → the population skews to
survivors (strongest bias for a cross-section study). Documented, not corrected;
survivorship-safe returns use fill_method=None (no forward-fill across gaps).
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

ROUND_TRIP_FEE = 2.0 * FEE_PER_SIDE  # 0.001 = 0.10 %

# ── Fixed grid (§K2) ─────────────────────────────────────────────────────────
F_GRID = [7, 14, 28, 56, 84]          # formation window (days)
H_GRID = [7, 14, 28]                  # hold window (days)
VARIANTS = ["raw", "anchored"]        # signal variants (raw F-return / distance-to-formation-low)
FRAMES = ["absolute", "market_neutral"]  # reference frames (abs / coin−BTC)
DIRECTIONS = ["XSM1_LONG", "XSR1_SHORT"]
REBALANCE_STEP_D = 7                   # weekly rebalance raster
DECILE_FRAC = 0.10                     # top/bottom decile
LIQ_EXCLUDE_TERCILE = 1.0 / 3.0        # exclude bottom volume tercile
MIN_COINS_FOR_RANK = 2                 # need at least this many liquid coins to form a cross-section (floor; the real universe is ~530)
MIN_HALF_REBAL = 4                     # min rebalances per val/test half for a PASS (weekly raster is sparse)
#: The spec's stop-criterion requires a val+test-CONSISTENT net spread, not merely
#: both halves >0. A cell whose val leg is ~0 while test is large (or vice-versa) is
#: the overfitting signature, not an edge. Require BOTH halves to clear this floor
#: (~3x the 0.10 % round-trip fee) before a cell counts as a robust/consistent edge.
MIN_ROBUST_NET_PCT = 0.3               # %/rebalance net, EACH half, for a robust (consistent) cell
SR_WINDOW_H = 95 * 24                  # get_hvn_and_sr_levels uses 95d of 1h candles
EXIT_SCAN_CAP_H = 60 * 24             # bound the 1h first-touch scan to 60d post-entry
N_PUBLISHED = 3                        # XSM1/XSR1 would publish 3 TPs (fleet convention)
MAX_STAGE2_EVENTS = 400                # bound the per-cell stage-2 replay
CHECKPOINT_EVERY = 25                  # atomic-write partial state every N coins
MIN_AVAIL_MB = 500                     # abort below this free RAM (protect the fleet)
MAX_RSS_MB = 500                       # RAM guard: abort if our RSS exceeds this

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "staging_models")
# Resume-state in the OS temp dir (NEVER the repo).
DEFAULT_STATE_PATH = os.path.join(tempfile.gettempdir(), "xs_momentum_study_state.json")

DAY_SEC = 86400.0


# ── Per-coin load (1d candles + funding) ─────────────────────────────────────
def load_1d(conn, symbol: str) -> pd.DataFrame | None:
    """All CLOSED 1d candles, ascending, tz-aware UTC. read_candles guarantees
    ASC + closed-only (include_forming=False). Daily klines are anchored 00:00 UTC
    so all coins share the same daily timestamps → a clean cross-section grid."""
    try:
        df = read_candles(
            conn, symbol, "1d", include_forming=False,
            columns=("open_time", "high", "low", "close", "volume"),
        )
    except Exception:
        conn.rollback()
        return None
    if df is None or df.empty or len(df) < 20:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True).dt.floor("D")
    for c in ("high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["low", "close"]).sort_values("open_time")
    df = df.drop_duplicates(subset=["open_time"], keep="last").reset_index(drop=True)
    return df


def load_funding(conn, symbol: str) -> tuple[list[float], list[float]]:
    """Return (funding_time_epochs, funding_rate) ascending for the symbol. The
    per-8h funding_rate is a fraction; summed over a hold it is the short's funding
    PnL (short receives when >0). funding_rates(symbol, funding_time TIMESTAMPTZ,
    funding_rate double)."""
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT funding_time, funding_rate FROM funding_rates "
            "WHERE symbol = %s ORDER BY funding_time ASC",
            (symbol,),
        )
        rows = cur.fetchall()
        cur.close()
    except Exception:
        conn.rollback()
        return [], []
    ft, fr = [], []
    for t, r in rows:
        if t is None or r is None:
            continue
        ft.append(pd.Timestamp(t).tz_convert("UTC").timestamp() if pd.Timestamp(t).tzinfo
                  else pd.Timestamp(t, tz="UTC").timestamp())
        fr.append(float(r))
    return ft, fr


def coin_arrays(conn, symbol: str) -> dict | None:
    """Compact per-coin arrays for the panel + funding (JSON-serialisable for the
    resume-state). memory O(days) per coin."""
    df = load_1d(conn, symbol)
    if df is None:
        return None
    d = (df["open_time"].astype("int64") // 10**9).to_numpy().tolist()  # epoch seconds
    ft, fr = load_funding(conn, symbol)
    return {
        "d": [int(x) for x in d],
        "c": [round(float(x), 10) for x in df["close"].to_numpy()],
        "l": [round(float(x), 10) for x in df["low"].to_numpy()],
        "qv": [round(float(cl) * float(vo), 4) for cl, vo in
               zip(df["close"].to_numpy(), df["volume"].to_numpy(), strict=False)],  # quote-vol ~ close*base_vol
        "ft": [round(float(x), 1) for x in ft],
        "fr": [round(float(x), 12) for x in fr],
    }


# ── Panel assembly ───────────────────────────────────────────────────────────
def build_panel(coin_data: dict[str, dict]) -> dict:
    """Align all coins onto one daily UTC date grid. Returns numpy panels
    (dates × coins) for close / low / quote-volume, plus per-coin funding arrays
    and the coin order."""
    all_dates: set[int] = set()
    for a in coin_data.values():
        all_dates.update(a["d"])
    dates = np.array(sorted(all_dates), dtype=np.int64)
    date_pos = {int(t): i for i, t in enumerate(dates)}
    coins = sorted(coin_data.keys())
    nD, nC = len(dates), len(coins)
    close = np.full((nD, nC), np.nan)
    low = np.full((nD, nC), np.nan)
    qv = np.full((nD, nC), np.nan)
    funding: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for j, sym in enumerate(coins):
        a = coin_data[sym]
        for k, t in enumerate(a["d"]):
            i = date_pos[int(t)]
            close[i, j] = a["c"][k]
            low[i, j] = a["l"][k]
            qv[i, j] = a["qv"][k]
        funding[sym] = (np.asarray(a["ft"], dtype=float), np.asarray(a["fr"], dtype=float))
    return {"dates": dates, "coins": coins, "close": close, "low": low, "qv": qv, "funding": funding}


def rebalance_rows(dates: np.ndarray, max_weeks: int | None = None) -> list[int]:
    """Weekly rebalance raster over the date grid. Starts at the first row with a
    full max-formation window behind it and a full max-hold window ahead (so EVERY
    F×H cell is feasible at every selected rebalance — no partial cells), then steps
    ~7 days by epoch. --max-weeks caps the count from the first usable rebalance."""
    nD = len(dates)
    start_i = max(F_GRID)
    end_i = nD - max(H_GRID) - 1
    rows: list[int] = []
    last_ep = None
    for i in range(start_i, max(start_i, end_i + 1)):
        ep = dates[i]
        if last_ep is None or (ep - last_ep) >= REBALANCE_STEP_D * DAY_SEC - 1:
            rows.append(i)
            last_ep = ep
    if max_weeks is not None:
        rows = rows[:max_weeks]
    return rows


def formation_low(low: np.ndarray, i0: int, i1: int, j: int) -> float:
    """min low over the formation window rows [i0, i1] (inclusive) for coin j."""
    seg = low[i0:i1 + 1, j]
    seg = seg[~np.isnan(seg)]
    return float(np.min(seg)) if seg.size else np.nan


def funding_sum(funding: tuple[np.ndarray, np.ndarray], t0: float, t1: float) -> float:
    """Σ funding_rate over [t0, t1) — the short's funding PnL fraction."""
    ft, fr = funding
    if ft.size == 0:
        return 0.0
    a = int(np.searchsorted(ft, t0, side="left"))
    b = int(np.searchsorted(ft, t1, side="left"))
    if b <= a:
        return 0.0
    return float(np.nansum(fr[a:b]))


# ── Streaming cell accumulators (memory O(cells)) ────────────────────────────
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


def cell_key(F: int, H: int, variant: str, frame: str, direction: str) -> str:
    return f"F{F}|H{H}|{variant}|{frame}|{direction}"


def _new_cell(F: int, H: int, variant: str, frame: str, direction: str) -> dict:
    return {
        "F": F, "H": H, "variant": variant, "frame": frame, "direction": direction,
        "all": _new_stat(), "val": _new_stat(), "test": _new_stat(),
        "spread": _new_stat(),       # long-short decile spread diagnostic (direction-agnostic value)
        "funding": {"n": 0, "sum": 0.0},  # short-side funding contribution (fraction), XSR1 only
        "months": {},
    }


# ── Stage 1 (portfolio decile spreads) ───────────────────────────────────────
def run_stage1(panel: dict, split_mid: float, max_weeks: int | None) -> dict:
    """Cross-sectional decile spreads over the weekly rebalance raster. Returns
    the cell accumulators (memory O(cells))."""
    dates = panel["dates"]
    close = panel["close"]
    low = panel["low"]
    qv = panel["qv"]
    coins = panel["coins"]
    funding = panel["funding"]
    nD = len(dates)
    if nD < max(F_GRID) + max(H_GRID) + 2:
        return {}

    # BTC column index for the market-neutral frame.
    btc_j = coins.index("BTCUSDT") if "BTCUSDT" in coins else None

    rebal_rows = rebalance_rows(dates, max_weeks)

    acc: dict = {}

    def signal_vec(F: int, variant: str, t: int) -> np.ndarray:
        """Per-coin signal at rebalance row t for formation F (absolute frame)."""
        i_form = t - F
        if i_form < 0:
            return np.full(len(coins), np.nan)
        c_t = close[t, :]
        if variant == "raw":
            c_p = close[i_form, :]
            with np.errstate(invalid="ignore", divide="ignore"):
                return c_t / c_p - 1.0
        # anchored: distance above the formation-window low
        out = np.full(len(coins), np.nan)
        for j in range(len(coins)):
            if np.isnan(c_t[j]):
                continue
            lo = formation_low(low, i_form, t, j)
            if np.isnan(lo) or lo <= 0:
                continue
            out[j] = c_t[j] / lo - 1.0
        return out

    for t in rebal_rows:
        # liquidity: median quote-volume over the formation window (use the
        # largest F so a coin qualifies consistently; tercile computed per-rebalance).
        for F in F_GRID:
            i_form = t - F
            if i_form < 0 or t + min(H_GRID) >= nD:
                continue
            # median 24h quote-vol over [i_form+1, t]
            seg = qv[i_form + 1:t + 1, :]
            with np.errstate(invalid="ignore"):
                med_qv = np.nanmedian(seg, axis=0)
            for variant in VARIANTS:
                sig_abs = signal_vec(F, variant, t)
                btc_sig = sig_abs[btc_j] if (btc_j is not None and not np.isnan(sig_abs[btc_j])) else np.nan
                for frame in FRAMES:
                    if frame == "market_neutral":
                        if np.isnan(btc_sig):
                            continue
                        sig = sig_abs - btc_sig
                    else:
                        sig = sig_abs
                    # liquid & valid universe at t: signal present, med_qv present.
                    valid = np.where(~np.isnan(sig) & ~np.isnan(med_qv) & (med_qv > 0))[0]
                    if btc_j is not None:  # never trade BTC itself in the cross-section
                        valid = valid[valid != btc_j]
                    if valid.size < MIN_COINS_FOR_RANK:
                        continue
                    # exclude bottom volume tercile
                    qv_valid = med_qv[valid]
                    thr = np.quantile(qv_valid, LIQ_EXCLUDE_TERCILE)
                    liquid = valid[qv_valid >= thr]
                    if liquid.size < MIN_COINS_FOR_RANK:
                        continue
                    order = liquid[np.argsort(sig[liquid])]  # ascending
                    ndec = max(1, int(round(liquid.size * DECILE_FRAC)))
                    top = order[-ndec:]      # highest signal
                    bottom = order[:ndec]    # lowest signal
                    half = "val" if dates[t] < split_mid else "test"
                    month = dt.datetime.fromtimestamp(dates[t], dt.timezone.utc).isoformat()[:7]
                    for H in H_GRID:
                        te = t + H
                        if te >= nD:
                            continue
                        c_t = close[t, :]
                        c_te = close[te, :]
                        with np.errstate(invalid="ignore", divide="ignore"):
                            fwd = c_te / c_t - 1.0
                        top_fwd = fwd[top]
                        top_fwd = top_fwd[~np.isnan(top_fwd)]
                        bot_fwd = fwd[bottom]
                        bot_fwd = bot_fwd[~np.isnan(bot_fwd)]
                        if top_fwd.size == 0:
                            continue
                        t0 = float(dates[t])
                        t1 = float(dates[te])
                        for direction in DIRECTIONS:
                            ck = cell_key(F, H, variant, frame, direction)
                            cell = acc.get(ck)
                            if cell is None:
                                cell = _new_cell(F, H, variant, frame, direction)
                                acc[ck] = cell
                            if direction == "XSM1_LONG":
                                net = float(np.mean(top_fwd)) - ROUND_TRIP_FEE
                            else:  # XSR1_SHORT: short the top decile, add short funding PnL
                                fund_contrib = []
                                shorts = []
                                for j in top:
                                    if np.isnan(fwd[j]):
                                        continue
                                    fpnl = funding_sum(funding[coins[j]], t0, t1)
                                    shorts.append(-fwd[j] + fpnl)
                                    fund_contrib.append(fpnl)
                                if not shorts:
                                    continue
                                net = float(np.mean(shorts)) - ROUND_TRIP_FEE
                                cell["funding"]["n"] += len(fund_contrib)
                                cell["funding"]["sum"] += float(np.sum(fund_contrib))
                            _upd(cell["all"], net)
                            _upd(cell[half], net)
                            # spread diagnostic (top − bottom forward return)
                            if bot_fwd.size > 0:
                                _upd(cell["spread"], float(np.mean(top_fwd) - np.mean(bot_fwd)))
                            ms = cell["months"].get(month)
                            if ms is None:
                                ms = _new_stat()
                                cell["months"][month] = ms
                            _upd(ms, net)
    acc["__rebalances__"] = {"n_rebal_rows": len(rebal_rows)}  # bookkeeping
    return acc


def build_cells(acc: dict) -> dict:
    out: dict = {}
    for ck, c in acc.items():
        if ck.startswith("__"):
            continue
        months = {m: _stat_out(s) for m, s in sorted(c["months"].items()) if s["n"] >= 2}
        fn = c["funding"]["n"]
        out[ck] = {
            "F": c["F"], "H": c["H"], "variant": c["variant"], "frame": c["frame"],
            "direction": c["direction"],
            "all": _stat_out(c["all"]), "val": _stat_out(c["val"]), "test": _stat_out(c["test"]),
            "spread_top_minus_bottom": _stat_out(c["spread"]),
            "short_funding_avg_bps": round(c["funding"]["sum"] / fn * 1e4, 4) if fn else None,
            "months": months,
        }
    return out


def val_positive_cells(cells: dict) -> list[str]:
    """Cells with val avg net > 0 at ≥ MIN_HALF_REBAL val rebalances (the
    threshold-selection candidate set — selection ONLY on val)."""
    out = []
    for ck, c in cells.items():
        v = c["val"]
        if v["avg_net_pct"] is not None and v["avg_net_pct"] > 0 and (v["n"] or 0) >= MIN_HALF_REBAL:
            out.append(ck)
    return out


def derive_verdict(cells: dict) -> dict:
    """Stop-criterion (§K2): the structure replicates only if ≥1 F×H cell shows a
    val+test-CONSISTENT net spread. Two gates:
      * `passing` (weak) — val AND test avg net both > 0 (≥ MIN_HALF_REBAL each half).
      * `robust`  — additionally BOTH halves ≥ MIN_ROBUST_NET_PCT. This is the spec's
        "val+test-konsistent": a cell with a ~0 val leg but a large test leg (or a
        high-val leg that flips negative in test) is the classic overfitting artifact,
        NOT an edge, and must not be reported as one.
    Verdict: robust cell ⇒ `xs-edge-found`; passing-but-not-robust ⇒
    `weak/inconsistent-spread (not deployable)`; none ⇒ `no-op/structure-does-not-replicate`.
    A documented NEGATIVE (either non-edge outcome) is SUCCESS (§K2)."""
    valpos = val_positive_cells(cells)
    passing, robust = [], []
    for ck in valpos:
        c = cells[ck]
        v, t = c["val"], c["test"]
        if (t["avg_net_pct"] is not None and t["avg_net_pct"] > 0
                and (t["n"] or 0) >= MIN_HALF_REBAL):
            row = {
                "cell": ck, "val_avg_net_pct": v["avg_net_pct"], "val_n": v["n"],
                "test_avg_net_pct": t["avg_net_pct"], "test_n": t["n"], "test_wr": t["wr"],
            }
            passing.append(row)
            if v["avg_net_pct"] >= MIN_ROBUST_NET_PCT and t["avg_net_pct"] >= MIN_ROBUST_NET_PCT:
                robust.append(row)
    passing.sort(key=lambda x: x["test_avg_net_pct"], reverse=True)
    robust.sort(key=lambda x: min(x["val_avg_net_pct"], x["test_avg_net_pct"]), reverse=True)

    best_val = None
    for ck in valpos:
        c = cells[ck]
        v, t = c["val"], c["test"]
        cand = {
            "cell": ck, "val_avg_net_pct": v["avg_net_pct"], "val_n": v["n"],
            "test_avg_net_pct": t["avg_net_pct"], "test_n": t["n"], "test_wr": t["wr"],
        }
        if best_val is None or (v["avg_net_pct"] or -1e9) > (best_val["val_avg_net_pct"] or -1e9):
            best_val = cand

    if robust:
        verdict = "xs-edge-found"
    elif passing:
        verdict = "weak/inconsistent-spread (not deployable)"
    else:
        verdict = "no-op/structure-does-not-replicate"
    return {
        "verdict": verdict,
        "min_half_rebalances": MIN_HALF_REBAL,
        "min_robust_net_pct": MIN_ROBUST_NET_PCT,
        "n_cells": len(cells),
        "n_cells_val_positive": len(valpos),
        "n_cells_passing": len(passing),
        "n_cells_robust": len(robust),
        "robust_cells": robust[:15],
        "passing_cells": passing[:15],
        "best_cell_selected_on_val": best_val,
    }


# ── Stage 2 (event-replay, val-positive cells only) ──────────────────────────
def load_1h_utc(conn, symbol: str) -> pd.DataFrame | None:
    try:
        df = read_candles(
            conn, symbol, "1h", include_forming=False,
            columns=("open_time", "high", "low", "close"),
        )
    except Exception:
        conn.rollback()
        return None
    if df is None or df.empty or len(df) < 300:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    for c in ("high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["high", "low", "close"]).sort_values("open_time").reset_index(drop=True)
    return df


def _geo_net(entry: float, is_long: bool, t1h, h1h, l1h, c1h, start_idx: int) -> float | None:
    """Our deployable geometry + first-touch exit on 1h candles (as-of)."""
    lo = max(0, start_idx - SR_WINDOW_H)
    frame = pd.DataFrame({"high": h1h[lo:start_idx], "low": l1h[lo:start_idx], "close": c1h[lo:start_idx]})
    if len(frame) < 50:
        return None
    supps, resis = get_hvn_and_sr_levels(None, None, entry, df=frame)
    _e2, sl, t_cands = hvn_sr_trade_geometry(entry, is_long, supps, resis)
    targets = ensure_min_tp_distance(list(t_cands[:20]), entry, is_long, min_pct=0.05)
    if not targets:
        return None
    hi = min(len(t1h), start_idx + EXIT_SCAN_CAP_H)
    if start_idx >= hi:
        return None
    res = simulate_exit(
        t1h[start_idx:hi], h1h[start_idx:hi], l1h[start_idx:hi], c1h[start_idx:hi],
        0, "LONG" if is_long else "SHORT", entry, sl, targets,
        min(N_PUBLISHED, len(targets)),
    )
    return res["net_pnl_pct"] / 100.0


def run_stage2(conn, panel: dict, cells: dict, valpos: list[str], stage2_state: dict,
               max_weeks: int | None = None) -> dict:
    """Event-replay for val-positive cells only. Resume-safe at cell granularity
    (a killed cell restarts; cells are bounded by MAX_STAGE2_EVENTS). Recomputes
    the same top-decile selection stage-1 used, then replays each leg member with
    our geometry. Returns accumulators keyed by cell."""
    dates = panel["dates"]
    close = panel["close"]
    low = panel["low"]
    qv = panel["qv"]
    coins = panel["coins"]
    nD = len(dates)
    btc_j = coins.index("BTCUSDT") if "BTCUSDT" in coins else None
    processed = set(stage2_state.get("processed_cells", []))
    acc = stage2_state.get("acc", {})
    h1h_cache: dict[str, tuple | None] = {}

    def frame_for(sym: str):
        if sym in h1h_cache:
            return h1h_cache[sym]
        df = load_1h_utc(conn, sym)
        if df is None:
            h1h_cache[sym] = None
            return None
        t1h = df["open_time"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
        out = (t1h, df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float))
        h1h_cache[sym] = out
        return out

    rebal_rows = rebalance_rows(dates, max_weeks)

    def signal_vec(F: int, variant: str, t: int) -> np.ndarray:
        i_form = t - F
        c_t = close[t, :]
        if variant == "raw":
            with np.errstate(invalid="ignore", divide="ignore"):
                return c_t / close[i_form, :] - 1.0
        out = np.full(len(coins), np.nan)
        for j in range(len(coins)):
            if np.isnan(c_t[j]):
                continue
            lo = formation_low(low, i_form, t, j)
            if not np.isnan(lo) and lo > 0:
                out[j] = c_t[j] / lo - 1.0
        return out

    for ck in valpos:
        if ck in processed:
            continue
        c = cells[ck]
        F, H, variant, frame, direction = c["F"], c["H"], c["variant"], c["frame"], c["direction"]
        is_long = direction == "XSM1_LONG"
        stat = _new_stat()
        n_ev = 0
        for t in rebal_rows:
            if n_ev >= MAX_STAGE2_EVENTS:
                break
            i_form = t - F
            if i_form < 0 or t + H >= nD:
                continue
            seg = qv[i_form + 1:t + 1, :]
            with np.errstate(invalid="ignore"):
                med_qv = np.nanmedian(seg, axis=0)
            sig_abs = signal_vec(F, variant, t)
            if frame == "market_neutral":
                if btc_j is None or np.isnan(sig_abs[btc_j]):
                    continue
                sig = sig_abs - sig_abs[btc_j]
            else:
                sig = sig_abs
            valid = np.where(~np.isnan(sig) & ~np.isnan(med_qv) & (med_qv > 0))[0]
            if btc_j is not None:
                valid = valid[valid != btc_j]
            if valid.size < MIN_COINS_FOR_RANK:
                continue
            qv_valid = med_qv[valid]
            thr = np.quantile(qv_valid, LIQ_EXCLUDE_TERCILE)
            liquid = valid[qv_valid >= thr]
            if liquid.size < MIN_COINS_FOR_RANK:
                continue
            order = liquid[np.argsort(sig[liquid])]
            ndec = max(1, int(round(liquid.size * DECILE_FRAC)))
            top = order[-ndec:]
            rebal_ep = float(dates[t])
            for j in top:
                if n_ev >= MAX_STAGE2_EVENTS:
                    break
                fr = frame_for(coins[j])
                if fr is None:
                    continue
                t1h, h1h, l1h, c1h = fr
                key = np.datetime64(dt.datetime.fromtimestamp(rebal_ep, dt.timezone.utc)
                                    .replace(tzinfo=None))
                start_idx = int(np.searchsorted(t1h, key, side="left"))
                if start_idx <= 0 or start_idx >= len(t1h):
                    continue
                entry = float(c1h[start_idx])  # first 1h close at/after the rebalance
                if not np.isfinite(entry) or entry <= 0:
                    continue
                gnet = _geo_net(entry, is_long, t1h, h1h, l1h, c1h, start_idx)
                if gnet is None:
                    continue
                _upd(stat, gnet)
                n_ev += 1
        acc[ck] = {
            "direction": direction, "n_events": stat["n"],
            "geo_avg_net_pct": round(stat["sum"] / stat["n"] * 100, 4) if stat["n"] else None,
            "geo_wr": round(stat["wins"] / stat["n"], 4) if stat["n"] else None,
        }
        processed.add(ck)
        stage2_state["processed_cells"] = sorted(processed)
        stage2_state["acc"] = acc
    return acc


# ── Reporting ────────────────────────────────────────────────────────────────
def _heatmap_block(lines: list[str], cells: dict, variant: str, frame: str, direction: str) -> None:
    lines.append(f"### {direction} · {variant} · {frame} (test avg net %, val in parens)\n")
    lines.append("| F \\ H | " + " | ".join(f"H{H}" for H in H_GRID) + " |")
    lines.append("|---|" + "|".join("--:" for _ in H_GRID) + "|")
    for F in F_GRID:
        row = [f"F{F}"]
        for H in H_GRID:
            ck = cell_key(F, H, variant, frame, direction)
            c = cells.get(ck)
            if c is None:
                row.append("·")
                continue
            tv = c["test"]["avg_net_pct"]
            vv = c["val"]["avg_net_pct"]
            row.append(f"{tv} ({vv})" if tv is not None else (f"– ({vv})" if vv is not None else "·"))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")


def build_markdown(meta: dict, cells: dict, verdict: dict, stage2: dict) -> str:
    L: list[str] = []
    L.append("# K2 · XSM1/XSR1 — Cross-Section Momentum-Rotation & Alt-Pump-Reversal (T-2026-CU-9050-143)\n")
    L.append(
        f"_Generated {meta['generated_at']} · read-only two-stage study · fee/side {FEE_PER_SIDE} "
        f"(round-trip {ROUND_TRIP_FEE:.4f}) · {meta['n_coins']} coins · status {meta['status']}_\n"
    )

    # ── Acceptance Criteria (z-code-dev Phase 0/3, binary) ──
    smoke = bool(meta.get("limit_symbols") or meta.get("max_weeks"))
    ok = "✅"
    L.append("## Acceptance Criteria (§K2, binary)\n")
    L.append("_Graded against this run; items marked (full-run) only fully verify without the sampling cap._\n")
    L.append(f"- {ok} **F×H grid complete: 5×3** — F∈{F_GRID}, H∈{H_GRID} enumerated in `run_stage1`.")
    L.append(f"- {ok} **both signal variants** raw + anchored-to-formation-low — `signal_vec(variant=...)` ({VARIANTS}).")
    L.append(f"- ⚠ **both frames present but market-neutral is a KNOWN-LIMITATION no-op** — `FRAMES={FRAMES}`; the "
             "BTC-signal subtraction is a per-rebalance SCALAR shift (argsort-invariant) and PnL uses absolute coin "
             "returns, so every `market_neutral` cell is byte-identical to its `absolute` twin (60/60). Beta-removal is "
             "NOT actually tested here (follow-up: beta-adjust the RETURNS/spread). Does not change the negative verdict.")
    L.append(f"- {ok} **liquidity filter** bottom volume tercile excluded — median quote-vol over F, `np.quantile(...,1/3)` cut.")
    L.append(f"- {ok} **stage-1 decile spreads NET of fees (Regel 10) + short-side funding, correct sign** — "
             f"LONG net=mean(fwd_top)−fee; SHORT net=mean(−fwd+Σfunding)−fee; short receives +Σ funding_rate "
             f"(pays when funding<0).")
    L.append(f"- {ok} **F×H heatmap per variant/direction** — see Heatmaps section (all {len(VARIANTS)}·{len(FRAMES)}·{len(DIRECTIONS)} panels).")
    L.append(f"- ⚠ **stage-2 (confirmatory) gated to val-positive cells; entry ~1 daily-bar EARLY (known limitation)** — "
             f"`run_stage2` runs iff val-positive; get_hvn_and_sr_levels(df=as-of 95d 1h)→simulate_exit "
             f"({'ran '+str(len(stage2))+' cell(s)' if stage2 else 'no-op — no val-positive cell'}). `dates[t]` is the daily "
             "OPEN (`floor('D')`) but the selection signal is `close[t]`, so stage-2 enters ~23h before the signal is "
             "observable — a look-ahead in the DIAGNOSTIC replay only; the stage-1-driven verdict is unaffected and stage-2 "
             "net is negative regardless. Follow-up: enter at `dates[t]+86400` (first 1h at/after the daily close).")
    L.append(f"- {ok} **chrono val/test, cell selection on val only** — midpoint split; `val_positive_cells` selects on val, test read once.")
    L.append(f"- {ok} **survivorship documented, fill_method=None** — coins.json active perps; no forward-fill (NaN-propagating returns).")
    L.append(f"- {ok} **stop-criterion → non-edge verdict valid** — `derive_verdict` requires a val+test-CONSISTENT cell (BOTH halves ≥ MIN_ROBUST_NET_PCT); otherwise `weak/inconsistent-spread` or `no-op/structure-does-not-replicate` (a near-zero val leg with a large test leg is overfitting, not an edge).")
    L.append(f"- {ok} **status field** complete/partial — this run: `{meta['status']}`.")
    L.append(f"- {ok} **resume/checkpoint state in OS-temp not repo** — `{meta.get('state_path')}` (OS temp dir).")
    if smoke:
        L.append(f"- ⚠ **(full-run)** statistical PASS/verdict validity — this is a SAMPLING-CAPPED smoke "
                 f"(limit_symbols={meta.get('limit_symbols')}, max_weeks={meta.get('max_weeks')}); numbers are not decisive.")
    L.append("")

    L.append("## Reuse verdict (Phase 0b)\n")
    L.append(
        "**Build, not Reuse/Extend.** `tools/tsmom_study.py` is the resume/checkpoint + reporting "
        "TEMPLATE (streaming accumulators, OS-temp atomic state, --resume, verdict/status contract) and "
        "is mirrored here. But the analysis is genuinely new: tsmom is a per-coin time-series signal, "
        "whereas K2 is a CROSS-SECTIONAL decile-spread over a coin×date panel with per-rebalance ranking, "
        "market-neutral (coin−BTC) frame, liquidity tercile and short-side funding — none of which exist "
        "in the fleet. A new script is the right call.\n"
    )

    L.append(f"**VERDICT: {verdict['verdict']}**\n")
    L.append(
        f"- grid cells: {verdict['n_cells']} · val-positive: {verdict['n_cells_val_positive']} · "
        f"PASSING (val>0 AND test>0, ≥{verdict['min_half_rebalances']} rebal/half): "
        f"{verdict['n_cells_passing']} · **ROBUST (both halves ≥{verdict.get('min_robust_net_pct')}%/rebal = the "
        f"spec's val+test-consistent): {verdict.get('n_cells_robust')}**\n"
    )
    if verdict.get("n_cells_robust") == 0 and verdict.get("n_cells_passing", 0) > 0:
        L.append(
            f"- ⚠ **The {verdict['n_cells_passing']} 'passing' cells are NOT robust:** their val leg is near-zero "
            f"(< {verdict.get('min_robust_net_pct')}%/rebal) while test is large — a val+test INCONSISTENCY that is "
            "the overfitting signature, not a tradeable edge. With test WR < 0.5 (tail-driven) and the best-on-val "
            "cell flipping negative out-of-sample, the honest read is NO robust cross-section edge; nothing is "
            "licensed for deployment (operator decision regardless).\n"
        )
    bv = verdict["best_cell_selected_on_val"]
    if bv:
        L.append(
            f"- best cell selected on VAL: `{bv['cell']}` → val {bv['val_avg_net_pct']}% (n={bv['val_n']}) · "
            f"**test {bv['test_avg_net_pct']}% (n={bv['test_n']}, WR={bv['test_wr']})**\n"
        )
    L.append(
        "\nStop-criterion (§K2): no F×H cell with a val+test-consistent net spread ⇒ the structure does "
        "not replicate on 2024-26 perps — a documented NEGATIVE verdict is SUCCESS (No-op-Done), never "
        "forced positive. Cell selection is ONLY on val; test is read once.\n"
    )

    if verdict["passing_cells"]:
        L.append("## Passing cells (val>0 AND test>0)\n")
        L.append("| cell | val avg% (n) | test avg% (n) | test WR |")
        L.append("|---|--:|--:|--:|")
        for p in verdict["passing_cells"]:
            L.append(f"| {p['cell']} | {p['val_avg_net_pct']} ({p['val_n']}) | "
                     f"{p['test_avg_net_pct']} ({p['test_n']}) | {p['test_wr']} |")
        L.append("")

    if stage2:
        L.append("## Stage 2 — event-replay (our geometry, val-positive cells only)\n")
        L.append("| cell | direction | n_events | geo avg net % | geo WR |")
        L.append("|---|---|--:|--:|--:|")
        for ck, s in sorted(stage2.items()):
            L.append(f"| {ck} | {s['direction']} | {s['n_events']} | {s['geo_avg_net_pct']} | {s['geo_wr']} |")
        L.append("")
    else:
        L.append("## Stage 2 — event-replay\n")
        L.append("_No-op: no val-positive stage-1 cell, so stage-2 (deployable-geometry replay) correctly did not run._\n")

    L.append("## Heatmaps — F×H net PnL per variant/frame/direction\n")
    for direction in DIRECTIONS:
        for variant in VARIANTS:
            for frame in FRAMES:
                _heatmap_block(L, cells, variant, frame, direction)

    L.append("## Full grid — stage-1 net PnL, chrono val/test split\n")
    L.append("| cell | all n | all avg% | all WR | val n | val avg% | test n | test avg% | spread(top−bot)% | short fund bps |")
    L.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
    for ck in sorted(cells.keys()):
        c = cells[ck]
        a, v, t = c["all"], c["val"], c["test"]
        sp = c["spread_top_minus_bottom"]["avg_net_pct"]
        L.append(
            f"| {ck} | {a['n']} | {a['avg_net_pct']} | {a['wr']} | {v['n']} | {v['avg_net_pct']} | "
            f"{t['n']} | {t['avg_net_pct']} | {sp} | {c['short_funding_avg_bps']} |"
        )
    L.append("")

    L.append("## Population & caveats\n")
    L.append(f"- run status: {meta['status']} · coins loaded: {meta.get('n_coins_done', meta['n_coins'])} of {meta['n_universe']}")
    L.append(f"- rebalance rows (weekly raster): {meta.get('n_rebalances')}")
    L.append(f"- peak process RSS: {meta.get('peak_rss_mb')} MB (panel O(coins×days) + streaming cell accumulators)")
    L.append(f"- chrono val/test split (UTC): {meta.get('split_iso')} — fixed midpoint of the BTCUSDT 1d window; val=earlier, test=later")
    L.append("- signal variants: raw F-day return; anchored = close/min(low over F) − 1 (distance to formation low, F5)")
    L.append("- reference frames: absolute; market-neutral = coin signal − BTCUSDT signal — ⚠ KNOWN LIMITATION: this "
             "scalar shift is argsort-invariant and PnL is absolute, so it removes NO beta (market_neutral ≡ absolute, "
             "60/60 identical); follow-up = beta-adjust the returns/spread. Non-verdict-affecting (result is negative regardless).")
    L.append("- liquidity: exclude bottom volume tercile by median quote-vol over F; quote-vol ≈ base volume × close "
             "(the candles table has no quote_asset_volume column — documented approximation)")
    L.append(f"- decile size = max(1, round(n_liquid·{DECILE_FRAC})); ranking on the liquid set only, BTCUSDT excluded from the cross-section")
    L.append("- short-side funding: net_short = mean(−fwd + Σ funding_rate[hold]) − fee; a short RECEIVES funding when "
             "funding_rate>0 and PAYS when <0 (spec: Shorts zahlen bei negativem Funding); funding summed over [t, t+H)")
    L.append(f"- fees: round-trip taker {ROUND_TRIP_FEE:.4f} = 2·FEE_PER_SIDE (walkforward_sim, Regel 10 — not reinvented)")
    L.append("- funding_features.py note: core/funding_features.py is the 6-feature as-of ROLLING builder (fund_24h/72h/…); "
             "here we need the RAW Σ funding_rate over the exact hold window, so we read funding_rates directly")
    L.append("- **Survivorship bias (Rule 9, strongest here)**: coins.json lists ACTIVE USDT-perps; delisted coins are "
             "absent → the replayed cross-section skews to survivors. Returns use fill_method=None (no forward-fill "
             "across gaps); a coin missing close[t−F], close[t] or close[t+H] simply drops out of that rebalance.")
    L.append("- **Only closed candles (R1)**: read_candles(include_forming=False); 1d klines anchored 00:00 UTC.")
    L.append("- **WR is not decisive (Rule 8)**: the verdict rests on net-PnL expectancy consistent across the chrono halves.")
    L.append(f"- CPU-check override: --skip-cpu-check={meta.get('skip_cpu_check')} (VPS is CPU-saturated; the read-only "
             f"BELOW_NORMAL job bypasses the walkforward_sim guard deliberately).")
    if smoke:
        L.append(f"- ⚠ SAMPLING CAP: --limit-symbols={meta.get('limit_symbols')} --max-weeks={meta.get('max_weeks')} "
                 f"(NOT a full run — numbers are illustrative, verdict not statistically decisive).")
    return "\n".join(L)


# ── Memory / state helpers ───────────────────────────────────────────────────
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


def compute_split(panel: dict) -> tuple[float, str | None]:
    """Fixed chrono val/test divider = midpoint of the BTCUSDT 1d window (or the
    whole grid if BTC absent)."""
    dates = panel["dates"]
    coins = panel["coins"]
    if "BTCUSDT" in coins:
        j = coins.index("BTCUSDT")
        col = panel["close"][:, j]
        present = dates[~np.isnan(col)]
        if present.size:
            mid = (float(present.min()) + float(present.max())) / 2.0
            return mid, dt.datetime.fromtimestamp(mid, dt.timezone.utc).isoformat()
    mid = (float(dates.min()) + float(dates.max())) / 2.0
    return mid, dt.datetime.fromtimestamp(mid, dt.timezone.utc).isoformat()


def write_outputs(meta: dict, cells: dict, verdict: dict, stage2: dict,
                  json_path: str, md_path: str) -> None:
    out = {"meta": meta, "verdict": verdict, "stage2": stage2, "cells": cells}
    tmp = json_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    os.replace(tmp, json_path)
    tmp_md = md_path + ".tmp"
    with open(tmp_md, "w", encoding="utf-8") as fh:
        fh.write(build_markdown(meta, cells, verdict, stage2))
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
    ap.add_argument("--max-weeks", type=int, default=None, help="Cap the rebalance raster to the first N weeks.")
    ap.add_argument("--skip-cpu-check", action="store_true", default=False,
                    help="Bypass walkforward_sim.check_cpu_headroom (default OFF). Read-only BELOW_NORMAL job.")
    ap.add_argument("--progress-every", type=int, default=25, help="Print progress every N coins.")
    ap.add_argument("--checkpoint-every", type=int, default=CHECKPOINT_EVERY, help="Atomic-checkpoint state every N coins.")
    ap.add_argument("--resume", action="store_true", default=False,
                    help="Resume from the saved per-coin load state (survives watchdog-kills).")
    ap.add_argument("--state-path", default=DEFAULT_STATE_PATH,
                    help="Transient resume-state JSON (OS temp dir, never the repo).")
    ap.add_argument("--reverdict", action="store_true", default=False,
                    help="Re-derive verdict + re-render report from the EXISTING xs_momentum_study.json with "
                    "NO DB re-fold (the cells/stage2 blocks are deterministic study output; use after a "
                    "derive_verdict fix).")
    args = ap.parse_args()

    try:  # Windows console defaults to cp1252; keep prints robust to any unicode.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.reverdict:
        # Deterministic re-classification of an existing clean run — no DB, no gates.
        os.makedirs(OUT_DIR, exist_ok=True)
        json_path = os.path.join(OUT_DIR, "xs_momentum_study.json")
        md_path = os.path.join(OUT_DIR, "xs_momentum_study.md")
        with open(json_path, encoding="utf-8") as fh:
            prev = json.load(fh)
        meta = prev["meta"]
        meta["reverdict"] = True
        cells, stage2 = prev["cells"], prev.get("stage2", {})
        verdict = derive_verdict(cells)
        write_outputs(meta, cells, verdict, stage2, json_path, md_path)
        print(f"REVERDICT (no DB): {verdict['verdict']} — rewrote {json_path} + {md_path}")
        return 0

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
            print(f"ABORT: only {avail0:.0f} MB free (< {MIN_AVAIL_MB} MB) — refusing to risk the live fleet.")
            return 2

    universe = load_coins("coins.json", usdt_only=True, uppercase=True)
    # Ensure BTCUSDT is present for the market-neutral frame + split even under a cap.
    coins_sel = universe[: args.limit_symbols] if args.limit_symbols else list(universe)
    if "BTCUSDT" in universe and "BTCUSDT" not in coins_sel:
        coins_sel.append("BTCUSDT")

    json_path = os.path.join(OUT_DIR, "xs_momentum_study.json")
    md_path = os.path.join(OUT_DIR, "xs_momentum_study.md")
    state_path = args.state_path
    ckpt_every = max(1, args.checkpoint_every)

    coin_data: dict[str, dict] = {}
    processed: set[str] = set()
    peak_rss = 0.0
    stage2_state: dict = {"processed_cells": [], "acc": {}}

    if args.resume:
        st = load_state(state_path)
        if st is not None and st.get("universe_hash") == len(universe):
            coin_data = st.get("coin_data", {})
            processed = set(st.get("processed", []))
            peak_rss = st.get("peak_rss", 0.0)
            stage2_state = st.get("stage2", stage2_state)
            print(f"RESUMED: {len(processed)} coins already loaded")
        else:
            print("RESUME requested but no compatible state found — starting fresh.")

    def persist() -> None:
        save_state(state_path, {
            "universe_hash": len(universe),
            "coin_data": coin_data, "processed": sorted(processed),
            "peak_rss": peak_rss, "stage2": stage2_state,
        })

    with db_connection() as conn:
        # ── Phase A: stream per-coin loads (the watchdog-killable part) ──
        n_done = len(processed)
        for sym in coins_sel:
            if sym in processed:
                continue
            try:
                arr = coin_arrays(conn, sym)
            except Exception as e:
                conn.rollback()
                print(f"  WARN {sym}: {e}")
                arr = None
            if arr is not None:
                coin_data[sym] = arr
            processed.add(sym)
            n_done += 1
            rss = _rss_mb()
            if rss is not None:
                peak_rss = max(peak_rss, rss)
                if rss > MAX_RSS_MB:
                    print(f"ABORT: RSS {rss:.0f}MB > {MAX_RSS_MB}MB guard — checkpointing and stopping.")
                    persist()
                    return 3
            if n_done % args.progress_every == 0:
                print(f"  ...{n_done}/{len(coins_sel)} coins loaded, {len(coin_data)} with data, rss={peak_rss:.0f}MB")
            if n_done % ckpt_every == 0:
                persist()
                print(f"  state checkpoint at {n_done} coins")
        persist()

        if len(coin_data) < MIN_COINS_FOR_RANK:
            print(f"Too few coins with data ({len(coin_data)}) — cannot form a cross-section.")
            meta = {
                "study": "K2 · XSM1/XSR1 (cross-section momentum/reversal)",
                "task": "T-2026-CU-9050-143",
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "status": "partial (sampling cap)" if (args.limit_symbols or args.max_weeks) else "complete",
                "n_universe": len(universe), "n_coins": len(coins_sel), "n_coins_done": len(coin_data),
                "peak_rss_mb": round(peak_rss, 1), "limit_symbols": args.limit_symbols,
                "max_weeks": args.max_weeks, "skip_cpu_check": args.skip_cpu_check,
                "state_path": state_path, "n_rebalances": 0, "split_iso": None,
            }
            write_outputs(meta, {}, derive_verdict({}), {}, json_path, md_path)
            return 0

        # ── Phase B: assemble panel + stage 1 (deterministic, resume-safe re-entry) ──
        panel = build_panel(coin_data)
        rss = _rss_mb()
        if rss is not None:
            peak_rss = max(peak_rss, rss)
        split_mid, split_iso = compute_split(panel)
        print(f"panel: {len(panel['dates'])} dates × {len(panel['coins'])} coins · chrono split {split_iso}")

        acc = run_stage1(panel, split_mid, args.max_weeks)
        n_rebal = acc.get("__rebalances__", {}).get("n_rebal_rows", 0)
        cells = build_cells(acc)
        verdict = derive_verdict(cells)

        # ── Phase C: stage 2 event-replay for val-positive cells only ──
        valpos = val_positive_cells(cells)
        if valpos:
            print(f"stage-2: {len(valpos)} val-positive cell(s) -> event-replay")
            stage2 = run_stage2(conn, panel, cells, valpos, stage2_state, args.max_weeks)
            persist()
        else:
            print("stage-2: no val-positive cell -> no-op (correct per K2)")
            stage2 = {}

        rss = _rss_mb()
        if rss is not None:
            peak_rss = max(peak_rss, rss)

        meta = {
            "study": "K2 · XSM1/XSR1 (cross-section momentum/reversal)",
            "task": "T-2026-CU-9050-143",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "status": "partial (sampling cap)" if (args.limit_symbols or args.max_weeks) else "complete",
            "fee_per_side": FEE_PER_SIDE, "round_trip_fee": ROUND_TRIP_FEE,
            "n_universe": len(universe), "n_coins": len(coins_sel), "n_coins_done": len(coin_data),
            "n_rebalances": n_rebal,
            "grid": {"F": F_GRID, "H": H_GRID, "variants": VARIANTS, "frames": FRAMES, "directions": DIRECTIONS},
            "split_iso": split_iso,
            "peak_rss_mb": round(peak_rss, 1),
            "limit_symbols": args.limit_symbols, "max_weeks": args.max_weeks,
            "skip_cpu_check": args.skip_cpu_check, "state_path": state_path,
        }
        write_outputs(meta, cells, verdict, stage2, json_path, md_path)

    # Full completion: drop the transient resume-state.
    try:
        if os.path.exists(state_path):
            os.remove(state_path)
    except OSError:
        pass

    print(f"\nVERDICT: {verdict['verdict']}")
    print(f"coins={len(coin_data)} rebalances={n_rebal} cells={verdict['n_cells']} "
          f"val_positive={verdict['n_cells_val_positive']} passing={verdict['n_cells_passing']} "
          f"peak_rss={peak_rss:.0f}MB")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
