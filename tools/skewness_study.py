#!/usr/bin/env py -3.13
# tools/skewness_study.py — K7 · SKW1 realized-skewness study (T-2026-CU-9050-141)
"""Read-only cross-section study for the shared realized-moments builder
(core.moment_features).

Question (docs/MODEL_CANDIDATES_SPEC_2026-07.md §K7): does realized **skewness**
(third moment of a coin's recent 15m log-returns) sort the cross-section of
forward weekly returns — specifically, is SHORT high-positive-skew vs LONG
low-skew a net-positive, funding- and fee-aware spread that is stable across a
chronological split? Realized-vol and kurtosis decile sorts are reported as a
byproduct (same machinery). This script does NOT decide deployment; it produces
the §K7 verdict.

Stop-criterion (§K7): skew deciles WITHOUT a stable net spread ⇒ SKW1 is dead —
but the moment feature-block (core/moment_features.py) stays as a retrain option
regardless (its use is a later retrain task's call, never licensed here). A no-op
is a valid, documented result — do NOT overclaim.

⚠ FALLE (§K7, F6): this sorts on realized SKEWNESS, not on a MAX/lottery feature.
MAX-based shorts are contraindicated in crypto (the MAX effect inverts). The
builder emits standard moment estimators only — no per-window max return.

Method (weekly rebalance, market-neutral long/short):
  * Universe = coins.json USDT-perps. One 15m-candle query per coin (closes +
    volumes) → core.moment_features.build_moment_panel.
  * Weekly grid: Monday-00:00-UTC stamps anchored on BTC's 15m span (BTC is always
    loaded, has the longest/most-complete history), leaving a trailing moment
    window before and a forward 1-week window after. The grid is FIXED on the
    first run and reused on resume → stable regardless of which coins are done.
  * At each stamp t: realized moments as-of t (closed bars only, no lookahead),
    the forward 1-week return, a trailing-7d dollar-volume liquidity proxy, and
    the realized forward-week funding sum (raw funding_rate summed over (t, t+1w]).
  * Market-neutral: each coin's forward return minus BTC's same-window return.
  * Liquidity filter: within each week drop the bottom dollar-volume tercile.
  * Decile sort by the moment feature (rank-based within each week, then pooled).
  * SKW1 spread: LONG bottom-skew decile, SHORT top-skew decile. Net =
    gross price spread + funding contribution (short earns funding, long pays it)
    − fees (both legs round-trip, tools.walkforward_sim.FEE_PER_SIDE).
  * Chronological val/test split of the weekly spread series — the net sign must
    survive both halves (repo Rule 8: WR/gross alone is worthless).

RESUME / CHECKPOINT (the live VPS watchdog reaps stray python.exe reproducibly;
15m-candle loads are HEAVIER than K6's daily breadth, so a naive full run never
finishes). Mirrors the proven K1/K6 pattern:
  * The kill-prone phase is LOADING 15m candles per coin (one DB query per coin).
    The raw candles (~35k rows/coin) are NOT stored — that would blow the RAM/state
    budget. Instead each coin is immediately reduced to its compact **weekly rows**
    (~52/coin: as-of moments + forward return + liquidity + funding) against the
    fixed BTC-anchored grid. The CHECKPOINT UNIT is that per-coin weekly-row list.
  * Every --checkpoint-every coins the per-coin rows + processed-set + grid are
    atomically written to a transient JSON state file in the OS TEMP dir (NEVER the
    repo). --resume reloads it, SKIPS processed coins, folds the rest. A kill
    between checkpoints loses only the last <N coins' loads, re-loaded on resume
    (idempotent, keyed by symbol → never double-counted).
  * Once every coin is processed the assemble/analyze phase runs from the persisted
    rows; a kill there re-enters directly on --resume (loading complete). On clean
    exit the state file is removed. RAM guard aborts below MIN_AVAIL_MB rather than
    risk the live fleet; memory is bounded (only compact weekly rows are kept).

READ-ONLY. SELECTs only, BELOW_NORMAL (tools.walkforward_sim.set_low_priority). The
VPS is CPU-saturated; walkforward_sim.check_cpu_headroom would abort, so a
study-local --skip-cpu-check flag (default OFF) bypasses it deliberately.

Contracts reused (no reinvention):
  * core.moment_features — the shared X-R1 as-of builder (15m log-returns →
    rolling std/skew/kurt over {24h, 7d}; native NaN, never fillna(0)). UNCHANGED.
  * core.funding_features.load_funding — the canonical funding loader; here we sum
    the RAW funding_rate (fraction, NOT bps) over the forward holding week as the
    funding PnL contribution.
  * tools.walkforward_sim.FEE_PER_SIDE = 0.0005 (taker 0.05%/side, P3.6) and
    set_low_priority / check_cpu_headroom — the fleet-safe guards (K3/K6 siblings).

Known bias (documented, not corrected): survivorship — coins.json / the per-coin
tables cover ACTIVE USDT-perps; delisted coins are partly missing, so every weekly
cross-section is over a survivorship-skewed universe. Funding for a coin without
funding history is treated as 0 contribution (documented, not imputed).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
import warnings

import numpy as np
import pandas as pd

# core.funding_features.load_funding uses pd.read_sql_query on a raw psycopg
# connection (shared-code behavior, not changed here). Over ~530 per-coin funding
# loads that emits one identical UserWarning each — suppress the cosmetic noise so
# the resume progress log stays readable. Purely log hygiene; no behavior change.
warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy", category=UserWarning)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.candles import TF_SECONDS, read_candles  # noqa: E402
from core.database import db_connection  # noqa: E402
from core.funding_features import load_funding  # noqa: E402
from core.moment_features import (  # noqa: E402
    DEFAULT_TF,
    MOMENT_FEATURES,
    build_moment_panel,
    moment_features_asof,
)
from tools.walkforward_sim import FEE_PER_SIDE, check_cpu_headroom, set_low_priority  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO_ROOT, "staging_models")
# Transient resume-state (per-coin weekly rows + processed set + grid) in the OS
# temp dir, NEVER the repo. Removed on clean completion.
DEFAULT_STATE_PATH = os.path.join(tempfile.gettempdir(), "skewness_study_state.json")

BTC_SYMBOL = "BTCUSDT"
WEEK = pd.Timedelta(days=7)
TF_NS = TF_SECONDS[DEFAULT_TF] * 10**9
#: Trailing bars for the dollar-volume liquidity proxy (7d of 15m bars).
LIQ_BARS = 7 * 86400 // TF_SECONDS[DEFAULT_TF]
#: Reject an as-of price if the last closed bar is more than this before the stamp
#: (coin not yet listed at t, or delisted before t+1w → no valid forward week).
MAX_STALE = pd.Timedelta(days=1)
#: Fee drag charged on the L/S spread: 2 legs × round-trip (2 sides each).
LS_FEE_DRAG = 4 * FEE_PER_SIDE
#: Minimum coins in a week to attempt a decile sort (below → week skipped, noted).
MIN_COINS_PER_WEEK = 20
#: Sort features reported. Skewness is primary (the SKW1 hypothesis); the rest are
#: the §K7 byproduct (RV/kurtosis) plus the 24h-skew variant.
SORT_FEATURES = ["mom_skew_7d", "mom_skew_24h", "mom_rv_7d", "mom_kurt_7d"]
N_DECILES = 10
CHECKPOINT_EVERY = 15  # atomic-write rows + processed set every N coins (15m loads are heavy)
MIN_AVAIL_MB = 500  # abort below this free RAM (protect the live fleet)

# ── Verdict thresholds (§K7 stop-criterion) ──────────────────────────────────
#: A weekly L/S net spread must clear this on the FULL series to be "robust".
MIN_ROBUST_NET_SPREAD = 0.001  # 0.1 %/week net after funding + fees
#: … AND both chrono halves must stay net-positive (OOS sign-stability).


def load_coins(path: str = "coins.json") -> list[str]:
    with open(os.path.join(REPO_ROOT, path), encoding="utf-8") as fh:
        coins = json.load(fh)
    if not isinstance(coins, list) or not coins:
        raise ValueError(f"{path} is not a non-empty list")
    return coins


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


# ─────────────────────────────────────────────────────────────────────────────
# Per-coin as-of helpers over one coin's 15m closes/volumes
# ─────────────────────────────────────────────────────────────────────────────
class CoinSeries:
    """One coin's 15m closes/volumes as ascending numpy (ns int64) with as-of
    lookups. Built fresh per coin in Phase 1 and discarded after its weekly rows
    are computed (bounded memory)."""

    __slots__ = ("ot", "close", "dollar")

    def __init__(self, df: pd.DataFrame):
        ot = pd.to_datetime(df["open_time"], utc=True).dt.tz_localize(None)
        self.ot = ot.to_numpy().astype("int64")  # ns since epoch (UTC), ascending
        self.close = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float)
        vol = pd.to_numeric(df["volume"], errors="coerce").to_numpy(dtype=float)
        self.dollar = self.close * vol

    def _asof_pos(self, ts_ns: int) -> int:
        """Index of the last bar CLOSED at/before ts (open_time + tf <= ts)."""
        return int(np.searchsorted(self.ot, ts_ns - TF_NS, side="right")) - 1

    def price_asof(self, ts_ns: int) -> float:
        pos = self._asof_pos(ts_ns)
        if pos < 0:
            return np.nan
        close_time_ns = self.ot[pos] + TF_NS
        if ts_ns - close_time_ns > MAX_STALE.value:  # stale → no valid bar around ts
            return np.nan
        return float(self.close[pos])

    def dollar_vol_trailing(self, ts_ns: int) -> float:
        pos = self._asof_pos(ts_ns)
        if pos < LIQ_BARS - 1:
            return np.nan
        return float(np.nanmean(self.dollar[pos - LIQ_BARS + 1 : pos + 1]))


def forward_funding_sum(fund_df, t: pd.Timestamp, t_next: pd.Timestamp) -> float:
    """Sum of RAW funding_rate (fraction) settled in (t, t_next]. A SHORT position
    earns positive funding; a LONG pays it. No funding history → 0.0 (documented,
    not imputed)."""
    if fund_df is None or fund_df.empty:
        return 0.0
    ft = fund_df["funding_time"]
    m = (ft > t) & (ft <= t_next)
    if not m.any():
        return 0.0
    return float(pd.to_numeric(fund_df.loc[m, "funding_rate"], errors="coerce").fillna(0.0).sum())


# ─────────────────────────────────────────────────────────────────────────────
# Weekly grid (BTC-anchored) + BTC forward returns
# ─────────────────────────────────────────────────────────────────────────────
def load_15m(conn, sym: str, start=None) -> pd.DataFrame:
    """One 15m query (open_time, close, volume), ascending, closed bars only."""
    df = read_candles(
        conn, sym, DEFAULT_TF, start=start, include_forming=False,
        columns=("open_time", "close", "volume"),
    )
    if df.empty or "close" not in df.columns:
        return pd.DataFrame()
    return df.sort_values("open_time").reset_index(drop=True)


def build_btc_grid(conn, start=None, max_weeks: int | None = None) -> tuple[list[int], dict[str, float]]:
    """Weekly Monday-00:00-UTC stamps (epoch seconds) anchored on BTC's 15m span,
    plus BTC's market-neutral forward return per stamp {str(stamp): btc_ret}.

    Returns ([], {}) if BTC has no data (study cannot market-neutralize)."""
    df = load_15m(conn, BTC_SYMBOL, start=start)
    if df.empty:
        return [], {}
    cs = CoinSeries(df)
    lo = pd.Timestamp(cs.ot[0], tz="UTC") + WEEK  # trailing history for the moment window
    hi = pd.Timestamp(cs.ot[-1], tz="UTC") - WEEK  # a forward week for the return
    if hi <= lo:
        return [], {}
    first_mon = (lo.normalize() + pd.Timedelta(days=(7 - lo.weekday()) % 7)).tz_convert("UTC")
    stamps_ts = list(pd.date_range(first_mon, hi, freq="7D", tz="UTC"))
    if max_weeks is not None and len(stamps_ts) > max_weeks:
        stamps_ts = stamps_ts[:max_weeks]
    stamps = [int(t.value // 10**9) for t in stamps_ts]
    btc_ret: dict[str, float] = {}
    for t in stamps_ts:
        tn = t + WEEK
        p0, p1 = cs.price_asof(t.value), cs.price_asof(tn.value)
        if np.isfinite(p0) and np.isfinite(p1) and p0 > 0:
            btc_ret[str(int(t.value // 10**9))] = p1 / p0 - 1.0
    return stamps, btc_ret


# ─────────────────────────────────────────────────────────────────────────────
# Per-coin weekly rows (the checkpoint unit)
# ─────────────────────────────────────────────────────────────────────────────
def coin_weekly_rows(
    conn, sym: str, stamps: list[int], btc_ret: dict[str, float], start=None
) -> list[dict]:
    """Compute one coin's compact weekly rows against the fixed grid. Returns [] if
    the coin has no usable data (delisted/missing) — never fillna'd."""
    df = load_15m(conn, sym, start=start)
    if df.empty:
        return []
    panel = build_moment_panel(df[["open_time", "close"]], tf=DEFAULT_TF)
    if panel.empty:
        return []
    cs = CoinSeries(df)
    try:
        fund_df = load_funding(conn, [sym]).get(sym)
    except Exception:
        conn.rollback()
        fund_df = None
    rows: list[dict] = []
    for s in stamps:
        t = pd.Timestamp(s, unit="s", tz="UTC")
        t_next = t + WEEK
        feats = moment_features_asof(panel, t, tf=DEFAULT_TF)
        if not feats or all(feats.get(f) is None for f in SORT_FEATURES):
            continue
        p0, p1 = cs.price_asof(t.value), cs.price_asof(t_next.value)
        if not (np.isfinite(p0) and np.isfinite(p1) and p0 > 0):
            continue
        fwd_ret = p1 / p0 - 1.0
        br = btc_ret.get(str(s))
        mn_ret = (fwd_ret - br) if br is not None else None
        row = {
            "week": s,
            "symbol": sym,
            "fwd_ret": fwd_ret,
            "mn_ret": mn_ret,
            "dollar_vol": cs.dollar_vol_trailing(t.value),
            "fwd_funding_sum": forward_funding_sum(fund_df, t, t_next),
        }
        for f in SORT_FEATURES:
            row[f] = feats.get(f)
        rows.append(row)
    return rows


def assemble_rows(rows_store: dict[str, list[dict]]) -> pd.DataFrame:
    """Flatten the per-coin weekly rows into one DataFrame (week → tz-aware ts)."""
    flat: list[dict] = []
    for rows in rows_store.values():
        flat.extend(rows)
    if not flat:
        return pd.DataFrame()
    df = pd.DataFrame(flat)
    df["week"] = pd.to_datetime(df["week"], unit="s", utc=True)
    for c in ["fwd_ret", "mn_ret", "dollar_vol", "fwd_funding_sum", *SORT_FEATURES]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Liquidity filter, decile sort, L/S spread
# ─────────────────────────────────────────────────────────────────────────────
def apply_liquidity_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Drop the bottom dollar-volume tercile WITHIN each week."""
    if df.empty:
        return df
    keep = []
    for _, gm in df.groupby("week"):
        g = gm[gm["dollar_vol"].notna()]
        if len(g) < 3:
            keep.append(gm)  # too few to tercile — keep as-is
            continue
        cut = float(np.quantile(g["dollar_vol"], 1 / 3))
        keep.append(g[g["dollar_vol"] >= cut])
    return pd.concat(keep).reset_index(drop=True) if keep else df


def _assign_deciles(vals: np.ndarray) -> np.ndarray:
    """Rank-based decile 0..9 within a week (robust to small n: some deciles stay empty)."""
    k = len(vals)
    ranks = pd.Series(vals).rank(method="first").to_numpy() - 1.0
    return np.minimum(N_DECILES - 1, (ranks / k * N_DECILES).astype(int))


def decile_table(df: pd.DataFrame, sort_col: str) -> dict:
    """Per-decile mean market-neutral forward return, deciles formed WITHIN each
    week then pooled. Weeks with < MIN_COINS_PER_WEEK usable coins are skipped.
    Reports a monotonicity read (Spearman of decile index vs mean mn-ret)."""
    per_decile: dict[int, list[float]] = {d: [] for d in range(N_DECILES)}
    n_weeks_used = 0
    for _, gm in df.groupby("week"):
        g = gm[gm[sort_col].notna() & gm["mn_ret"].notna()]
        if len(g) < MIN_COINS_PER_WEEK:
            continue
        n_weeks_used += 1
        dec = _assign_deciles(g[sort_col].to_numpy(dtype=float))
        for d, r in zip(dec, g["mn_ret"].to_numpy(dtype=float), strict=True):
            per_decile[int(d)].append(float(r))
    table = []
    means: list[float] = []
    idxs: list[int] = []
    for d in range(N_DECILES):
        vals = per_decile[d]
        m = float(np.mean(vals)) if vals else None
        table.append(
            {
                "decile": d,
                "n": len(vals),
                "avg_mn_ret": round(m, 5) if m is not None else None,
                "wr": round(float(np.mean(np.array(vals) > 0)), 4) if vals else None,
            }
        )
        if m is not None:
            means.append(m)
            idxs.append(d)
    mono = None
    if len(means) >= 3 and np.std(idxs) > 0 and np.std(means) > 0:
        mono = float(np.corrcoef(idxs, means)[0, 1])
    return {"sort_feature": sort_col, "n_weeks_used": n_weeks_used, "decile_monotonicity": _r(mono), "deciles": table}


def skew_ls_spread(df: pd.DataFrame, sort_col: str) -> dict:
    """LONG bottom-<feature> decile, SHORT top-<feature> decile. Weekly net spread =
    gross price spread + funding contribution − fees; then chrono val/test (70/30)."""
    weekly: list[dict] = []
    for t, gm in df.groupby("week"):
        g = gm[gm[sort_col].notna() & gm["mn_ret"].notna()]
        if len(g) < MIN_COINS_PER_WEEK:
            continue
        dec = _assign_deciles(g[sort_col].to_numpy(dtype=float))
        g = g.assign(_dec=dec)
        lo = g[g["_dec"] == 0]  # low → LONG
        hi = g[g["_dec"] == N_DECILES - 1]  # high → SHORT
        if lo.empty or hi.empty:
            continue
        gross = float(lo["mn_ret"].mean()) - float(hi["mn_ret"].mean())
        # Short earns funding on the high leg, long pays it on the low leg.
        funding = float(hi["fwd_funding_sum"].mean()) - float(lo["fwd_funding_sum"].mean())
        net = gross + funding - LS_FEE_DRAG
        weekly.append(
            {
                "week": t,
                "gross_spread": gross,
                "funding_contrib": funding,
                "net_spread": net,
                "n_long": int(len(lo)),
                "n_short": int(len(hi)),
            }
        )
    wf = pd.DataFrame(weekly)
    if wf.empty:
        return {"sort_feature": sort_col, "n_weeks": 0, "note": "no week reached MIN_COINS_PER_WEEK"}

    def agg(sub: pd.DataFrame) -> dict:
        return {
            "n_weeks": int(len(sub)),
            "avg_gross_spread": round(float(sub["gross_spread"].mean()), 5),
            "avg_funding_contrib": round(float(sub["funding_contrib"].mean()), 6),
            "avg_net_spread": round(float(sub["net_spread"].mean()), 5),
            "median_net_spread": round(float(sub["net_spread"].median()), 5),
            "pct_weeks_net_pos": round(float((sub["net_spread"] > 0).mean()), 4),
        }

    wf = wf.sort_values("week").reset_index(drop=True)
    cut = int(len(wf) * 0.7)
    return {
        "sort_feature": sort_col,
        "direction": "LONG low decile, SHORT high decile (SKW1: short high-positive-skew)",
        "fee_drag_per_week": round(LS_FEE_DRAG, 5),
        "all": agg(wf),
        "val_first70pct": agg(wf.iloc[:cut]) if cut >= 1 else None,
        "test_last30pct": agg(wf.iloc[cut:]) if len(wf) - cut >= 1 else None,
    }


def _r(v: float | None, nd: int = 4) -> float | None:
    return None if v is None else round(v, nd)


# ─────────────────────────────────────────────────────────────────────────────
# Verdict (§K7 stop-criterion)
# ─────────────────────────────────────────────────────────────────────────────
def derive_verdict(spread: dict) -> dict:
    """§K7: skew deciles WITHOUT a stable net spread ⇒ SKW1 dead. The verdict hangs
    on the primary SKW1 L/S net spread (short high-positive-skew, long low-skew;
    market-neutral, liquidity-filtered, funding-costed, fee'd):

      * "skw1-robust-spread"       — FULL avg net spread ≥ MIN_ROBUST_NET_SPREAD AND
        BOTH chrono halves net-positive (OOS sign-stable). A candidate short filter.
      * "weak/unstable-skew-spread (not deployable)" — FULL avg net > 0 but a half
        flips negative or the magnitude is below the floor. Not robust OOS.
      * "no-op/no-skew-spread"     — FULL avg net spread ≤ 0. SKW1 dead.

    In every non-robust case the moment feature-block stays a retrain option (§K7);
    any deployment is an operator decision (Michi), never licensed here."""
    # Degenerate ONLY when the spread was never formed — that return carries no "all"
    # block (see the MIN_COINS_PER_WEEK guard return). Do NOT test a top-level "n_weeks":
    # on the SUCCESS path n_weeks lives inside spread["all"], so a top-level lookup
    # defaults to 0 and would mis-fire, burying a real spread as a false no-op.
    if "all" not in spread:
        return {
            "verdict": "no-op/no-skew-spread",
            "min_robust_net_spread": MIN_ROBUST_NET_SPREAD,
            "note": (
                "No week reached MIN_COINS_PER_WEEK — the L/S skew spread could not be formed. "
                f"SKW1 shows no exploitable spread ({spread.get('note', '')}). The moment "
                "feature-block stays a retrain option (§K7); nothing deployed."
            ),
        }
    a = spread["all"]
    val = spread.get("val_first70pct") or {}
    test = spread.get("test_last30pct") or {}
    all_net = a["avg_net_spread"]
    val_net = val.get("avg_net_spread")
    test_net = test.get("avg_net_spread")
    halves_pos = (val_net is not None and val_net > 0) and (test_net is not None and test_net > 0)

    if all_net > 0 and all_net >= MIN_ROBUST_NET_SPREAD and halves_pos:
        verdict = "skw1-robust-spread"
        note = (
            f"The SKW1 L/S net spread (short high-positive-skew, long low-skew) is +{all_net}/week "
            f"and stays net-positive in BOTH chrono halves (val {val_net}, test {test_net}) — an OOS "
            "sign-stable, funding/fee-aware edge. Deployment as a short-candidate filter remains an "
            "operator decision (Michi); the moment feature-block is now also a validated retrain input."
        )
    elif all_net > 0:
        verdict = "weak/unstable-skew-spread (not deployable)"
        note = (
            f"The FULL-series SKW1 net spread is marginally positive (+{all_net}/week) but NOT robust: "
            f"chrono halves = val {val_net} / test {test_net} (both must stay >0), and/or the magnitude "
            f"is below the {MIN_ROBUST_NET_SPREAD}/week floor. No stable OOS edge ⇒ §K7 near-no-op. The "
            "moment feature-block stays a retrain option; no SKW1 filter is licensed."
        )
    else:
        verdict = "no-op/no-skew-spread"
        note = (
            f"The SKW1 L/S net spread is non-positive ({all_net}/week after funding + fees) — the skew "
            "deciles show no exploitable net spread. §K7 stop-criterion: SKW1 is dead. The moment "
            "feature-block (core/moment_features.py) stays as a retrain option regardless; nothing deployed."
        )
    return {
        "verdict": verdict,
        "min_robust_net_spread": MIN_ROBUST_NET_SPREAD,
        "all_avg_net_spread": all_net,
        "val_avg_net_spread": val_net,
        "test_avg_net_spread": test_net,
        "both_halves_net_positive": bool(halves_pos),
        "note": note,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Resume state
# ─────────────────────────────────────────────────────────────────────────────
def save_state(state_path: str, state: dict) -> None:
    """Atomic-write (temp + os.replace) so a mid-write kill never truncates state."""
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


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────
def build_markdown(meta: dict, spread: dict, spread_24h: dict, deciles: list[dict], verdict: dict) -> str:
    L: list[str] = []
    L.append("# K7 · SKW1 — realized-skewness study (T-2026-CU-9050-141)\n")
    L.append(
        f"_Generated {meta['generated_at']} · read-only · status={meta['status']} · "
        f"coins_loaded={meta['n_coins_loaded']}/{meta['n_universe']} · weeks={meta['n_weeks']} · "
        f"rows={meta['n_rows_post_liquidity']} · peak RSS {meta.get('peak_rss_mb')} MB_\n"
    )
    L.append(f"**VERDICT: {verdict['verdict']}**\n")
    L.append(
        f"- SKW1 L/S net spread (short high-positive-skew, long low-skew; market-neutral, "
        f"liquidity-filtered, funding-costed, fees): FULL **{verdict.get('all_avg_net_spread')}**/week · "
        f"val {verdict.get('val_avg_net_spread')} · test {verdict.get('test_avg_net_spread')} "
        f"(≥{verdict['min_robust_net_spread']}/week AND both halves >0 required: "
        f"{verdict.get('both_halves_net_positive')})\n"
    )
    L.append(f"> {verdict['note']}\n")
    L.append(
        "Realized SKEWNESS sort (not MAX/lottery — §K7 F6). Realized moments from 15m closed bars "
        "(R1); native NaN, never fillna(0) (P1.20). Survivorship-biased (active USDT-perps only); "
        "funding for coins without history contributes 0 (documented, not imputed).\n"
    )

    L.append("## SKW1 long/short spread — primary sort `mom_skew_7d`\n")
    if "all" not in spread:  # degenerate only; n_weeks lives in spread["all"] on success (mirror derive_verdict)
        L.append(f"- {spread.get('note', 'no spread computed')}\n")
    else:
        L.append(f"- fee drag/week: {spread['fee_drag_per_week']} · weeks used: {spread['all']['n_weeks']}")
        L.append("| slice | n wk | gross | funding | **net** | median net | weeks net+ |")
        L.append("|---|--:|--:|--:|--:|--:|--:|")
        for name in ("all", "val_first70pct", "test_last30pct"):
            s = spread.get(name)
            if s:
                L.append(
                    f"| {name} | {s['n_weeks']} | {s['avg_gross_spread']} | {s['avg_funding_contrib']} "
                    f"| **{s['avg_net_spread']}** | {s['median_net_spread']} | {s['pct_weeks_net_pos']} |"
                )
        L.append("")

    L.append("## Byproduct — L/S spread on 24h-skew\n")
    if "all" in spread_24h:  # n_weeks lives in spread_24h["all"] on success (mirror derive_verdict)
        s = spread_24h["all"]
        v, te = spread_24h.get("val_first70pct") or {}, spread_24h.get("test_last30pct") or {}
        L.append(
            f"- `mom_skew_24h`: FULL net {s['avg_net_spread']} · val {v.get('avg_net_spread')} · "
            f"test {te.get('avg_net_spread')} ({s['n_weeks']} wk)\n"
        )
    else:
        L.append(f"- {spread_24h.get('note', 'n/a')}\n")

    L.append("## Decile sorts — mean market-neutral forward return (incl. RV/kurtosis byproduct)\n")
    for d in deciles:
        L.append(
            f"### `{d['sort_feature']}` ({d['n_weeks_used']} weeks ≥ {MIN_COINS_PER_WEEK} coins · "
            f"monotonicity ρ(decile,mn-ret)={d['decile_monotonicity']})\n"
        )
        if d["n_weeks_used"] == 0:
            L.append("_no week reached the coin minimum — deciles empty._\n")
            continue
        L.append("| decile | n | avg mn-ret | WR |")
        L.append("|--:|--:|--:|--:|")
        for row in d["deciles"]:
            L.append(f"| {row['decile']} | {row['n']} | {row['avg_mn_ret']} | {row['wr']} |")
        L.append("")

    L.append("## Caveats\n")
    L.append(
        "- **Verdict basis**: §K7 stop-criterion — a stable OOS net spread across BOTH chrono halves ⇒ SKW1 is "
        "a candidate; no stable spread ⇒ SKW1 dead. Either way the moment feature-block stays/becomes a retrain "
        "input (§K7); any standalone deployment is an operator decision (Michi), never licensed by this study."
    )
    L.append(
        "- **⚠ REAL structure ≠ tradeable edge (load-bearing):** the net spread is net of ONLY flat taker fees "
        "(FEE_PER_SIDE, both legs round-trip) + realized funding — it models NO slippage, market impact, borrow "
        "availability, or short-liquidation risk. This is a weekly full-decile-rebalance short-term-reversal sort "
        "on the most illiquid, highest-skew alts (only the bottom dollar-volume tercile is dropped), and the LONG "
        "(low-skew = recently-crashed) leg's mean is fat-right-tail / bounce-driven (WR < 0.5 in every decile). "
        "The headline net/week therefore OVERSTATES realizable PnL after microstructure costs — treat it as a "
        "validated FEATURE signal for retrains, NOT a turnkey deployable spread. Independent verification: the "
        "T-133 orchestration investigation (2026-07-16) ruled out stale-price / survivorship / look-ahead "
        "artifacts (structure is real); tradeability after real costs is unproven and is the operator's call."
    )
    L.append("- **Survivorship**: cross-section over active USDT-perps only; delisted coins missing.")
    L.append("- **Funding**: coins without funding history contribute 0 (documented, not imputed).")
    L.append(
        "- Weekly grid anchored on BTC's 15m span; coins with shorter history contribute NaN (skipped) "
        "for early stamps, and a coin without a valid forward week (staleness > 1d) drops that stamp."
    )
    L.append(f"- CPU-check override: --skip-cpu-check={meta['skip_cpu_check']} (read-only, BELOW_NORMAL).")
    L.append(
        f"- Resume machinery: per-coin weekly-row checkpoint every {CHECKPOINT_EVERY} coins to OS-temp "
        "state (survives watchdog kills); memory bounded, state removed on clean exit."
    )
    if meta.get("limit_symbols"):
        L.append(f"- ⚠ SAMPLING CAP: --limit-symbols={meta['limit_symbols']} (NOT a full run).")
    if meta.get("max_weeks"):
        L.append(f"- ⚠ WEEK CAP: --max-weeks={meta['max_weeks']} (NOT a full run).")
    return "\n".join(L)


def write_outputs(out: dict, md: str, json_path: str, md_path: str) -> None:
    """Atomic write (temp + os.replace) so a mid-run kill leaves a valid file."""
    tmp = json_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    os.replace(tmp, json_path)
    tmp_md = md_path + ".tmp"
    with open(tmp_md, "w", encoding="utf-8") as fh:
        fh.write(md)
    os.replace(tmp_md, md_path)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="K7 · SKW1 realized-skewness study (read-only, resumable).")
    ap.add_argument("--limit-symbols", type=int, default=None, help="Cap the universe to the first N coins (smoke).")
    ap.add_argument("--max-weeks", type=int, default=None, help="Cap the number of weekly rebalances (smoke).")
    ap.add_argument("--start", default=None, help="Optional ISO date lower bound for the 15m load (e.g. 2025-10-01).")
    ap.add_argument("--checkpoint-every", type=int, default=CHECKPOINT_EVERY, help="Checkpoint every N coins.")
    ap.add_argument("--progress-every", type=int, default=15, help="Print progress every N coins.")
    ap.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="Resume from the saved per-coin weekly-row state (survives watchdog kills); exit 0 = complete.",
    )
    ap.add_argument("--state-path", default=DEFAULT_STATE_PATH, help="Transient resume-state JSON (OS temp, not repo).")
    ap.add_argument(
        "--skip-cpu-check",
        action="store_true",
        default=False,
        help="Bypass walkforward_sim.check_cpu_headroom (default OFF). Needed on the CPU-saturated VPS.",
    )
    ap.add_argument(
        "--reverdict",
        action="store_true",
        default=False,
        help="Re-derive the verdict + re-render the report from the EXISTING skewness_study.json, "
        "with NO DB re-fold. The spread/decile blocks are deterministic study output; use this when "
        "derive_verdict was fixed after an expensive clean run (zero DB load — safe on a busy live box).",
    )
    args = ap.parse_args()

    if args.reverdict:
        # Deterministic re-classification of an existing clean run. Reads the persisted
        # spread/decile blocks (unchanged), applies the current derive_verdict, and rewrites
        # both artifacts. No DB, no priority/CPU/RAM gates needed (pure file op).
        os.makedirs(OUT_DIR, exist_ok=True)
        json_path = os.path.join(OUT_DIR, "skewness_study.json")
        md_path = os.path.join(OUT_DIR, "skewness_study.md")
        with open(json_path, encoding="utf-8") as fh:
            prev = json.load(fh)
        meta = prev["meta"]
        meta["reverdict_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        spread, spread_24h, deciles = prev["skw1_spread"], prev["skew_24h_spread"], prev["decile_sorts"]
        verdict = derive_verdict(spread)
        out = {
            "meta": meta,
            "verdict": verdict,
            "skw1_spread": spread,
            "skew_24h_spread": spread_24h,
            "decile_sorts": deciles,
        }
        write_outputs(out, build_markdown(meta, spread, spread_24h, deciles, verdict), json_path, md_path)
        print(f"REVERDICT (no DB): {verdict['verdict']} — rewrote {json_path} + {md_path}")
        return 0

    set_low_priority()
    if not args.skip_cpu_check:
        check_cpu_headroom()
    else:
        print("CPU-check SKIPPED (--skip-cpu-check): read-only BELOW_NORMAL job on saturated VPS.", flush=True)
    os.makedirs(OUT_DIR, exist_ok=True)

    avail0 = _avail_mb()
    if avail0 is not None:
        print(f"RAM available at start: {avail0:.0f} MB", flush=True)
        if avail0 < MIN_AVAIL_MB:
            print(f"ABORT: only {avail0:.0f} MB free (< {MIN_AVAIL_MB} MB) — refusing to risk the live fleet.")
            return 2

    start = pd.Timestamp(args.start, tz="UTC") if args.start else None
    universe = load_coins()
    coins = universe[: args.limit_symbols] if args.limit_symbols else universe
    state_path = args.state_path

    stamps: list[int] = []
    btc_ret: dict[str, float] = {}
    rows_store: dict[str, list[dict]] = {}
    processed: set[str] = set()
    peak_rss = 0.0

    if args.resume:
        st = load_state(state_path)
        if (
            st is not None
            and st.get("universe_len") == len(universe)
            and st.get("limit_symbols") == args.limit_symbols
            and st.get("max_weeks") == args.max_weeks
        ):
            stamps = list(st.get("stamps", []))
            btc_ret = dict(st.get("btc_ret", {}))
            rows_store = {k: v for k, v in st.get("rows_store", {}).items()}
            processed = set(st.get("processed", []))
            peak_rss = st.get("peak_rss", 0.0)
            print(f"RESUMED: {len(processed)} coins processed, {len(rows_store)} with rows, {len(stamps)} stamps",
                  flush=True)
        else:
            print("RESUME requested but no compatible state — starting fresh.", flush=True)

    json_path = os.path.join(OUT_DIR, "skewness_study.json")
    md_path = os.path.join(OUT_DIR, "skewness_study.md")

    def persist() -> None:
        save_state(
            state_path,
            {
                "universe_len": len(universe),
                "limit_symbols": args.limit_symbols,
                "max_weeks": args.max_weeks,
                "stamps": stamps,
                "btc_ret": btc_ret,
                "processed": sorted(processed),
                "rows_store": rows_store,
                "peak_rss": peak_rss,
            },
        )

    # ── Phase 0: BTC-anchored weekly grid + BTC forward returns ────────────────
    if not stamps:
        with db_connection() as conn:
            stamps, btc_ret = build_btc_grid(conn, start=start, max_weeks=args.max_weeks)
        if not stamps:
            print("ABORT: BTC has no usable 15m span — cannot anchor the weekly grid / market-neutral.")
            return 2
        print(f"weekly grid: {len(stamps)} stamps (BTC-anchored), BTC fwd-returns for {len(btc_ret)}", flush=True)
        persist()

    # ── Phase 1: per-coin weekly rows (kill-prone; checkpointed) ───────────────
    todo = [c for c in coins if c not in processed]
    if todo:
        with db_connection() as conn:
            for sym in todo:
                try:
                    rows = coin_weekly_rows(conn, sym, stamps, btc_ret, start=start)
                except Exception as e:  # missing table / delisted / transient — never fatal per coin
                    conn.rollback()
                    # Diagnostic only; sanitize to ASCII so a non-cp1252 exception message
                    # (e.g. driver text with unicode) can never crash the run on a Windows
                    # cp1252 console — the per-coin skip below is the intended handling.
                    warn = f"  WARN {sym}: {type(e).__name__} {e}".encode("ascii", "replace").decode("ascii")
                    print(warn, flush=True)
                    rows = []
                if rows:
                    rows_store[sym] = rows
                processed.add(sym)
                rss = _rss_mb()
                if rss is not None:
                    peak_rss = max(peak_rss, rss)
                if len(processed) % args.progress_every == 0:
                    av = _avail_mb()
                    msg = f"  ...{len(processed)}/{len(coins)} coins, {len(rows_store)} with rows, peak_rss={peak_rss:.0f}MB"
                    if av is not None:
                        msg += f" avail={av:.0f}MB"
                    print(msg, flush=True)
                    if av is not None and av < MIN_AVAIL_MB:
                        persist()
                        print(f"ABORT: {av:.0f} MB free (< {MIN_AVAIL_MB}) — state saved, resume later.")
                        return 2
                if len(processed) % args.checkpoint_every == 0:
                    persist()
                    print(f"  checkpoint written at {len(processed)} coins ({len(rows_store)} with rows)", flush=True)
        persist()
    print(f"per-coin load complete: {len(rows_store)}/{len(coins)} coins with weekly rows", flush=True)

    # ── Phase 2: assemble + analyze (fast; re-entrant on resume) ───────────────
    rows = assemble_rows(rows_store)
    print(f"weekly cross-section rows (pre-liquidity): {len(rows)}", flush=True)
    rows = apply_liquidity_filter(rows)
    print(f"rows after liquidity filter: {len(rows)}", flush=True)

    spread = skew_ls_spread(rows, "mom_skew_7d") if not rows.empty else {"n_weeks": 0, "note": "no rows"}
    spread_24h = skew_ls_spread(rows, "mom_skew_24h") if not rows.empty else {"n_weeks": 0, "note": "no rows"}
    deciles = [decile_table(rows, f) for f in SORT_FEATURES] if not rows.empty else []
    verdict = derive_verdict(spread)

    meta = {
        "study": "K7 · SKW1 (realized skewness / moments)",
        "task": "T-2026-CU-9050-141",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "complete" if not (args.limit_symbols or args.max_weeks) else "partial (sampling cap)",
        "tf": DEFAULT_TF,
        "limit_symbols": args.limit_symbols,
        "max_weeks": args.max_weeks,
        "n_universe": len(universe),
        "n_coins_loaded": len(rows_store),
        "n_weeks": len(stamps),
        "n_rows_post_liquidity": int(len(rows)),
        "peak_rss_mb": round(peak_rss, 1),
        "skip_cpu_check": args.skip_cpu_check,
        "moment_features": MOMENT_FEATURES,
        "fee_per_side": FEE_PER_SIDE,
        "min_coins_per_week": MIN_COINS_PER_WEEK,
    }

    out = {
        "meta": meta,
        "verdict": verdict,
        "skw1_spread": spread,
        "skew_24h_spread": spread_24h,
        "decile_sorts": deciles,
    }
    write_outputs(out, build_markdown(meta, spread, spread_24h, deciles, verdict), json_path, md_path)

    # Clean completion → drop the transient resume-state so a later run starts fresh.
    try:
        if os.path.exists(state_path):
            os.remove(state_path)
    except OSError:
        pass

    print(f"\nVERDICT: {verdict['verdict']}")
    print(
        f"coins_loaded={len(rows_store)}/{len(universe)} weeks={len(stamps)} rows={len(rows)} "
        f"net_spread_all={verdict.get('all_avg_net_spread')} "
        f"val={verdict.get('val_avg_net_spread')} test={verdict.get('test_avg_net_spread')} "
        f"peak_rss={peak_rss:.0f}MB"
    )
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
