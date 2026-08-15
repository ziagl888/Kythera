# tools/pump_oi_study.py — pump-conditioned OI study (T-2026-KYT-9050-144).
#
# Michi's observation (2026-08-15): the manual OI+volatility combination works
# "really well" only on coins that pumped >=25% within the last 24h (ACE/APR/BR).
# T-096 refuted the UNCONDITIONED spike-fade; this study re-asks the narrower,
# pre-registered question: does the pump filter flip the sign?
#
# Pre-registration (frozen BEFORE the first run, incl. the candidate rule):
#   staging_models/replay/pump_oi_study_t144_prereg.md
#
# READ-ONLY: SELECTs against oi_5m only. Runs in a VPS session from the repo
# root (or a worktree inside it — load_dotenv walks up to the checkout's .env).
# Verdict: staging_models/replay/pump_oi_study_t144.md
#
# Method is deliberately byte-similar to tools/oi_event_study.py (T-096) so the
# two studies stay comparable: hourly as-of grid (45-min staleness), implied
# price oi_value_usdt/open_interest, universe floor median OI >= $3M, strictly
# causal features, 24h per-symbol per-mechanic cooldown, fees 0.10%/RT.
# Differences (all pre-registered): 24h-lookback pump filter, 4h OI window
# ("OI auf 4h Zeitframe", operator ask), rolling-24h OI peak for the rollover
# mechanic, forward horizons 4/8/24h, and BOTH sides are always reported
# (SHORT = fade the pump, LONG = ride the continuation).

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

OI_SINCE = "2026-06-12"  # first oi_5m point
MAX_STALE_MIN = 45  # as-of point older than this voids the grid row
FEE_RT = 0.10  # % round trip (fleet study convention)
MIN_OI_USDT = 3_000_000  # universe floor: median OI value
COOLDOWN_H = 24
FWD_HOURS = [4, 8, 24, 48, 72]  # 48/72h added by the registered extension

# Pre-registered thresholds — no per-coin tuning, no post-hoc search.
PUMP_PCTS = [25.0, 50.0]  # 24h price gain that counts as "a big pump"
SPIKE_OI_4H = 10.0  # M1: 4h OI build-up >= this = fresh money still entering
ROLL_BUILD_24H = 25.0  # M2: 24h OI build (peak vs 24h ago) >= this
ROLL_OFFPEAK = -5.0  # M2: OI off its 24h peak by <= this = money leaving
STALL_OI_4H = 0.0  # M3: 4h OI change <= this = pump no longer fed
# M4 POST-COLLAPSE (registered extension, the BLUAI question): the trailing
# 48h price peak was itself a pump, and price now sits deep below it.
COLLAPSE_PUMP_48H = 25.0  # peak_48h vs px_48h_ago >= this = "there WAS a pump"
COLLAPSE_RETRACE = -40.0  # px vs peak_48h <= this = "and it collapsed"


def load(conn) -> pd.DataFrame:
    oi = pd.read_sql(
        "SELECT ts, symbol, open_interest, oi_value_usdt FROM oi_5m WHERE ts >= %(t0)s ORDER BY ts",
        conn,
        params={"t0": OI_SINCE},
    )
    oi["ts"] = pd.to_datetime(oi["ts"], utc=True).astype("datetime64[ns, UTC]")
    oi["px"] = oi["oi_value_usdt"] / oi["open_interest"].replace(0, np.nan)
    return oi


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


def add_features(grid: pd.DataFrame) -> pd.DataFrame:
    g = grid.sort_values(["symbol", "ts"]).set_index("ts")
    parts = []
    for _sym, s in g.groupby("symbol", sort=False):
        s = s.copy()
        for h in [4, 24, 48]:
            past = s[["px", "open_interest"]].shift(freq=pd.Timedelta(hours=h))
            past = past[~past.index.duplicated()]
            s[f"px_{h}h_ago"] = past["px"].reindex(s.index)
            s[f"oi_{h}h_ago"] = past["open_interest"].reindex(s.index)
        for h in FWD_HOURS:
            fut = s[["px"]].shift(freq=pd.Timedelta(hours=-h))
            fut = fut[~fut.index.duplicated()]
            s[f"px_{h}h_fwd"] = fut["px"].reindex(s.index)
        # Trailing peaks (causal: the rolling window ends at the current row).
        s["oi_peak_24h"] = s["open_interest"].rolling("24h", min_periods=1).max()
        s["px_peak_48h"] = s["px"].rolling("48h", min_periods=1).max()
        parts.append(s.reset_index())
    g = pd.concat(parts, ignore_index=True)
    g["dpx_24h"] = (g["px"] / g["px_24h_ago"] - 1) * 100
    g["doi_4h"] = (g["open_interest"] / g["oi_4h_ago"] - 1) * 100
    g["oi_build_24h"] = (g["oi_peak_24h"] / g["oi_24h_ago"] - 1) * 100
    g["oi_offpeak"] = (g["open_interest"] / g["oi_peak_24h"] - 1) * 100
    # M4 POST-COLLAPSE: the 48h peak was itself a pump, price sits far below it.
    g["px_pump_48h"] = (g["px_peak_48h"] / g["px_48h_ago"] - 1) * 100
    g["px_retrace"] = (g["px"] / g["px_peak_48h"] - 1) * 100
    for h in FWD_HOURS:
        g[f"fwd_{h}h"] = (g[f"px_{h}h_fwd"] / g["px"] - 1) * 100
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


def report_side(name: str, side: str, ev: pd.DataFrame, control: pd.DataFrame | None) -> dict:
    """Signed stats for one side. SHORT fades the pump (ret = -fwd)."""
    sign = -1.0 if side == "SHORT" else 1.0
    out: dict = {"mechanic": name, "side": side, "n": len(ev)}
    print(f"\n=== {name} · {side}: n={len(ev)} (symbols={ev['symbol'].nunique()}) ===")
    if len(ev) < 30:
        print("  too few events — NOT CONCLUDABLE at current history depth")
        out["verdict"] = "not-concludable"
        return out
    for h in FWD_HOURS:
        if f"fwd_{h}h" not in ev:
            continue
        r = (ev[f"fwd_{h}h"] * sign).dropna()
        if r.empty:
            continue
        net = r.mean() - FEE_RT
        tstat = r.mean() / (r.std() / np.sqrt(len(r))) if len(r) > 1 else np.nan
        line = (
            f"  fwd {h:2d}h: gross {r.mean():+.3f}% (median {r.median():+.3f}%, "
            f"WR {(r > 0).mean():.0%}, t={tstat:.2f}) | net-fee {net:+.3f}%"
        )
        if control is not None:
            c = (control[f"fwd_{h}h"] * sign).dropna()
            edge_vs_ctrl = net - (c.mean() - FEE_RT)
            line += f" | vs pump-only {edge_vs_ctrl:+.3f}pp"
            out[f"vsctrl_{h}h"] = round(edge_vs_ctrl, 3)
        print(line)
        out[f"net_{h}h"] = round(net, 3)
        out[f"t_{h}h"] = round(tstat, 2)
    # Weekly stability, 4h horizon (pre-registered rule 3).
    wk = ev.set_index("ts")["fwd_4h"].mul(sign).groupby(pd.Grouper(freq="W")).mean().dropna()
    if len(wk):
        pos = (wk > 0).mean()
        out["weeks_pos"] = round(pos, 2)
        print(f"  weekly stability (4h): {len(wk)} weeks, {pos:.0%} positive")
    return out


def main() -> None:
    sys.path.insert(0, ".")
    from core.database import get_db_connection

    conn = get_db_connection()
    try:
        oi = load(conn)
    finally:
        conn.close()
    print(f"oi rows: {len(oi)}")

    g = add_features(hourly_grid(oi))
    print(f"grid rows: {len(g)}, symbols in universe: {g['symbol'].nunique()}")

    results = []
    for pump_pct in PUMP_PCTS:
        pump = g[g["dpx_24h"] >= pump_pct]
        print(f"\n######## PUMP FILTER: dpx_24h >= {pump_pct:.0f}% — raw grid rows {len(pump)} ########")

        mechanics = {
            f"M0 PUMP-ONLY >= {pump_pct:.0f}%": pump,
            f"M1 +OI-SPIKE-4h (doi_4h >= {SPIKE_OI_4H:.0f}%)": pump[pump["doi_4h"] >= SPIKE_OI_4H],
            f"M2 +OI-ROLLOVER (build >= {ROLL_BUILD_24H:.0f}%, offpeak <= {ROLL_OFFPEAK:.0f}%)": pump[
                (pump["oi_build_24h"] >= ROLL_BUILD_24H) & (pump["oi_offpeak"] <= ROLL_OFFPEAK)
            ],
            f"M3 +OI-STALL-4h (doi_4h <= {STALL_OI_4H:.0f}%)": pump[pump["doi_4h"] <= STALL_OI_4H],
        }
        control: pd.DataFrame | None = None
        for name, ev in mechanics.items():
            ev = dedupe(ev.copy())
            if name.startswith("M0"):
                control = ev  # the pump-only baseline every OI mechanic must beat
                ctrl_for = None
            else:
                ctrl_for = control
            for side in ("SHORT", "LONG"):
                results.append(report_side(name, side, ev, ctrl_for))

    # M4 POST-COLLAPSE (registered extension) — independent of the pump filter
    # matrix: the event population is coins whose pump already collapsed. LONG
    # = bounce-back, SHORT = continuation. No pump-only control applies (rule 4
    # waived for M4 per pre-registration).
    m4 = dedupe(g[(g["px_pump_48h"] >= COLLAPSE_PUMP_48H) & (g["px_retrace"] <= COLLAPSE_RETRACE)].copy())
    for side in ("SHORT", "LONG"):
        results.append(
            report_side(
                f"M4 POST-COLLAPSE (48h-pump >= {COLLAPSE_PUMP_48H:.0f}%, retrace <= {COLLAPSE_RETRACE:.0f}%)",
                side,
                m4,
                None,
            )
        )

    print("\n=== summary (net of fees, %-points per event; vsctrl = net edge over pump-only) ===")
    for r in results:
        if r.get("verdict") == "not-concludable":
            print(f"{r['mechanic']:55s} {r['side']:5s} n={r['n']:5d} NOT CONCLUDABLE")
            continue
        nets = " ".join(f"net_{h}h={r.get(f'net_{h}h', float('nan')):+.3f}(t={r.get(f't_{h}h', float('nan'))})" for h in FWD_HOURS)
        vs = " ".join(f"vs{h}h={r[f'vsctrl_{h}h']:+.3f}" for h in FWD_HOURS if f"vsctrl_{h}h" in r)
        print(f"{r['mechanic']:55s} {r['side']:5s} n={r['n']:5d} wk+={r.get('weeks_pos', '—')} {nets} {vs}")


if __name__ == "__main__":
    main()
