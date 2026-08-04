# tools/oi_event_study.py — K9 harvest: the three seeded OI model ideas, falsified cheaply.
#
# T-2026-KYT-9050-096. MODEL_CANDIDATES_SPEC_2026-07.md K9 seeded three model
# ideas on top of the oi_5m collector; this study runs all three as event
# studies over the accumulated history (~53d at first run) BEFORE any live
# code exists (Batch-E discipline):
#
#   1. DIVERGENCE  — price moves but OI falls: the move is position-closing,
#      not new money => fade it (SHORT a rally with falling OI, LONG a dump
#      with falling OI).
#   2. SPIKE-FADE  — sudden OI build-up = overheated fresh positioning =>
#      fade the price move that accompanied the build-up.
#   3. OI x FUNDING — squeeze susceptibility: OI near its own high while
#      funding is extreme => position for the squeeze (crowded shorts pay
#      negative funding => LONG; crowded longs pay positive => SHORT).
#
# READ-ONLY: SELECTs against oi_5m and funding_rates only. Runs in a VPS
# session from the repo root. Verdict: staging_models/replay/oi_event_study_t096.md
#
# Methodology notes (the traps this design avoids):
#   * The collector's effective cadence degraded from 5m to 10-30m since
#     mid-July (measured 2026-08-04). All features therefore live on an
#     HOURLY as-of grid (merge_asof backward, staleness-capped) — nothing
#     assumes a dense 5m series.
#   * Price is the implied mark oi_value_usdt / open_interest — same source
#     as the OI itself, no candle-table join across ~530 per-coin tables.
#   * Strictly causal: every feature at grid time t uses only points <= t
#     (as-of); forward returns use only points >= t+h. A stale as-of point
#     (> MAX_STALE) voids the row instead of pretending freshness (P0.12).
#   * Funding is as-of the last funding_time <= t (8h grid) — mirrors the
#     as-of contract of core/funding_features.py.
#   * Per-symbol, per-mechanic cooldown of 24h so one persistent condition
#     does not stack into dozens of pseudo-independent events.
#   * Baseline = forward return of the FULL grid (same universe, same
#     period) — an "edge" must beat holding the tape, not zero.
#   * Fees: FEE_RT (round trip, taker both sides) reported next to gross.

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

OI_SINCE = "2026-06-12"  # first oi_5m point (30d backfill horizon)
MAX_STALE_MIN = 45  # as-of point older than this voids the grid row
FEE_RT = 0.10  # % round trip (fleet study convention, e.g. trailing_book_health)
MIN_OI_USDT = 3_000_000  # universe floor: median OI value, drops microcap noise
COOLDOWN_H = 24
FWD_HOURS = [1, 4, 24]

# Mechanic thresholds — deliberately simple, pre-registered, no per-coin tuning.
DIV_PX_PCT = 2.0  # |4h price move| that counts as "a move"
DIV_OI_PCT = -2.0  # 4h OI change below this = "positions leaving"
SPIKE_OI_PCT = 5.0  # 1h OI build-up that counts as a spike
SPIKE_PX_MIN = 0.5  # spike must ride a visible 1h move to define a fade side
SQUEEZE_OI_PCTL = 0.90  # OI value >= own 30d percentile
SQUEEZE_FUND = 0.0005  # |funding| beyond 5 bps per 8h = extreme (~55%/yr)


def load(conn) -> tuple[pd.DataFrame, pd.DataFrame]:
    oi = pd.read_sql(
        "SELECT ts, symbol, open_interest, oi_value_usdt FROM oi_5m WHERE ts >= %(t0)s ORDER BY ts",
        conn,
        params={"t0": OI_SINCE},
    )
    oi["ts"] = pd.to_datetime(oi["ts"], utc=True).astype("datetime64[ns, UTC]")
    oi["px"] = oi["oi_value_usdt"] / oi["open_interest"].replace(0, np.nan)
    fr = pd.read_sql(
        "SELECT symbol, funding_time, funding_rate FROM funding_rates "
        "WHERE funding_time >= %(t0)s ORDER BY funding_time",
        conn,
        params={"t0": OI_SINCE},
    )
    fr["funding_time"] = pd.to_datetime(fr["funding_time"], utc=True).astype("datetime64[ns, UTC]")
    return oi, fr


def hourly_grid(oi: pd.DataFrame) -> pd.DataFrame:
    """As-of snapshot of every symbol at every top-of-hour, staleness-capped."""
    t0 = oi["ts"].min().ceil("h")
    t1 = oi["ts"].max().floor("h")
    hours = pd.date_range(t0, t1, freq="1h", tz="UTC")
    symbols = oi["symbol"].unique()
    grid = pd.MultiIndex.from_product([hours, symbols], names=["ts", "symbol"]).to_frame(index=False)
    grid = pd.merge_asof(
        grid.sort_values("ts"),
        oi[["ts", "symbol", "open_interest", "oi_value_usdt", "px"]].rename(columns={"ts": "ots"}),
        left_on="ts",
        right_on="ots",
        by="symbol",
        direction="backward",
        tolerance=pd.Timedelta(minutes=MAX_STALE_MIN),
    )
    grid = grid.dropna(subset=["px"])
    # Universe floor on the SYMBOL's median book size, not per-row (a row-level
    # floor would select on the very OI moves the study measures).
    med = grid.groupby("symbol")["oi_value_usdt"].median()
    keep = set(med[med >= MIN_OI_USDT].index)
    return grid[grid["symbol"].isin(keep)].copy()


def add_features(grid: pd.DataFrame, fr: pd.DataFrame) -> pd.DataFrame:
    g = grid.sort_values(["symbol", "ts"]).set_index("ts")
    parts = []
    for _sym, s in g.groupby("symbol", sort=False):
        s = s.copy()
        for h in [1, 4]:
            past = s[["px", "open_interest"]].shift(freq=pd.Timedelta(hours=h))
            past = past[~past.index.duplicated()]
            s[f"px_{h}h_ago"] = past["px"].reindex(s.index)
            s[f"oi_{h}h_ago"] = past["open_interest"].reindex(s.index)
        for h in FWD_HOURS:
            fut = s[["px"]].shift(freq=pd.Timedelta(hours=-h))
            fut = fut[~fut.index.duplicated()]
            s[f"px_{h}h_fwd"] = fut["px"].reindex(s.index)
        # 30d rolling percentile of OI value (own history only, causal)
        s["oi_pctl_30d"] = s["oi_value_usdt"].rolling("30d", min_periods=24 * 7).rank(pct=True)
        parts.append(s.reset_index())
    g = pd.concat(parts, ignore_index=True)
    for h in [1, 4]:
        g[f"dpx_{h}h"] = (g["px"] / g[f"px_{h}h_ago"] - 1) * 100
        g[f"doi_{h}h"] = (g["open_interest"] / g[f"oi_{h}h_ago"] - 1) * 100
    for h in FWD_HOURS:
        g[f"fwd_{h}h"] = (g[f"px_{h}h_fwd"] / g["px"] - 1) * 100
    # As-of funding (8h grid): last funding_time <= ts.
    g = pd.merge_asof(
        g.sort_values("ts"),
        fr.rename(columns={"funding_time": "fts"}).sort_values("fts"),
        left_on="ts",
        right_on="fts",
        by="symbol",
        direction="backward",
        tolerance=pd.Timedelta(hours=9),
    )
    return g


def dedupe(ev: pd.DataFrame) -> pd.DataFrame:
    """One event per symbol per COOLDOWN_H (first wins) — causal, order-safe."""
    ev = ev.sort_values(["symbol", "ts"])
    keep = []
    last: dict[str, pd.Timestamp] = {}
    for row in ev.itertuples():
        prev = last.get(row.symbol)
        if prev is None or (row.ts - prev) >= pd.Timedelta(hours=COOLDOWN_H):
            keep.append(row.Index)
            last[row.symbol] = row.ts
    return ev.loc[keep]


def signed_fwd(ev: pd.DataFrame) -> pd.DataFrame:
    for h in FWD_HOURS:
        ev[f"ret_{h}h"] = ev[f"fwd_{h}h"] * np.where(ev["side"] == "LONG", 1.0, -1.0)
    return ev


def report(name: str, ev: pd.DataFrame, base: pd.DataFrame) -> dict:
    out = {"mechanic": name, "n": len(ev)}
    print(f"\n=== {name}: n={len(ev)} (symbols={ev['symbol'].nunique()}) ===")
    if len(ev) < 30:
        print("too few events — NOT CONCLUDABLE at current history depth")
        out["verdict"] = "not-concludable"
        return out
    for h in FWD_HOURS:
        r = ev[f"ret_{h}h"].dropna()
        b = base[f"fwd_{h}h"].dropna()
        net = r.mean() - FEE_RT
        wr = (r > 0).mean()
        tstat = r.mean() / (r.std() / np.sqrt(len(r))) if len(r) > 1 else np.nan
        print(
            f"  fwd {h:2d}h: gross avg {r.mean():+.3f}% (median {r.median():+.3f}%, WR {wr:.0%}, "
            f"t={tstat:.2f}) | net-fee {net:+.3f}% | tape baseline |avg| {abs(b.mean()):.3f}%"
        )
        out[f"net_{h}h"] = round(net, 3)
    wk = ev.set_index("ts").groupby([pd.Grouper(freq="W"), "side"])["ret_4h"].agg(["size", "mean"])
    print("  weekly stability (4h horizon):")
    print(wk.round(3).to_string().replace("\n", "\n  "))
    return out


def main() -> None:
    sys.path.insert(0, ".")
    from core.database import get_db_connection

    conn = get_db_connection()
    try:
        oi, fr = load(conn)
    finally:
        conn.close()
    print(f"oi rows: {len(oi)}, funding rows: {len(fr)}")

    grid = hourly_grid(oi)
    print(f"hourly grid rows: {len(grid)}, symbols in universe: {grid['symbol'].nunique()}")
    g = add_features(grid, fr)

    results = []

    def run_mechanic(name: str, ev: pd.DataFrame) -> None:
        ev = signed_fwd(dedupe(ev))
        results.append(report(name, ev, g))
        # The fleet-wide realized audit found the edge to be DIRECTIONAL, not
        # regime-bound — every mechanic is therefore also read per side.
        for side in ("LONG", "SHORT"):
            sub = ev[ev["side"] == side]
            if len(sub) >= 30:
                results.append(report(f"{name} · {side} only", sub, g))

    # 1. DIVERGENCE — a real move whose OI fell: fade it.
    div = g[(g["dpx_4h"].abs() >= DIV_PX_PCT) & (g["doi_4h"] <= DIV_OI_PCT)].copy()
    div["side"] = np.where(div["dpx_4h"] > 0, "SHORT", "LONG")
    run_mechanic("DIVERGENCE (fade move with falling OI)", div)

    # 2. SPIKE-FADE — fresh OI build-up riding a move: fade the move.
    spk = g[(g["doi_1h"] >= SPIKE_OI_PCT) & (g["dpx_1h"].abs() >= SPIKE_PX_MIN)].copy()
    spk["side"] = np.where(spk["dpx_1h"] > 0, "SHORT", "LONG")
    run_mechanic("SPIKE-FADE (fade move behind OI spike)", spk)

    # 3. OI x FUNDING — crowded book at extreme funding: position for the squeeze.
    sq = g[(g["oi_pctl_30d"] >= SQUEEZE_OI_PCTL) & (g["funding_rate"].abs() >= SQUEEZE_FUND)].copy()
    sq["side"] = np.where(sq["funding_rate"] < 0, "LONG", "SHORT")
    run_mechanic("OIxFUNDING (squeeze susceptibility)", sq)

    print("\n=== summary (net of fees, %-points per event) ===")
    for r in results:
        line = " ".join(f"{k}={v}" for k, v in r.items() if k.startswith("net_"))
        print(f"{r['mechanic']:45s} n={r['n']:5d} {line}")


if __name__ == "__main__":
    main()
