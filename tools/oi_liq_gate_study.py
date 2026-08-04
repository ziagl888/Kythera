# tools/oi_liq_gate_study.py — can OI or forced liquidations gate bot 40 entries?
#
# T-2026-KYT-9050-094. Operator observation: bot 40 (trailing arm) SL hits go
# 100-200% into the red (levered) while trail wins are capped at 20-40%. Can an
# entry gate on open interest (oi_5m, K9) or forced liquidations (liq_events,
# LQE1) keep the deep-SL trades out of the book?
#
# READ-ONLY: only SELECTs against trailing_positions / oi_5m / liq_events.
# Runs in a VPS session (needs DB credentials). Verdict + numbers:
# staging_models/replay/oi_liq_gate_verdict_t094.md
#
# Population: posted + filled + closed mirrors with a mark, i.e. the realized
# live book of the trailing arm. ENTRY_NOT_FILLED and shadow rows are excluded
# (no position ever existed at Cornix). Marks are unlevered %-points — the
# levered view Michi sees in the channel is ~20x these numbers.
#
# Cohort split: TIME_STOP went live 2026-07-28T14:00Z with a grandfather cutoff
# (40_trailing_close_bot.TIME_STOP_SINCE). The pre-cutoff cohort rode to
# catastrophic SL on explicit operator risk — pooling it with today's config
# would blame the current bot for a decision that is already paid for.
#
# OI features are computed from oi_5m alone: open_interest (contracts) for the
# OI deltas, and oi_value_usdt / open_interest as the implied mark price for
# the price deltas — no candle tables needed, and the 5m grid is dense enough
# for 1h/4h/24h lookbacks. merge_asof needs datetime64[ns]; the DB returns us
# precision (T-073 epoch trap), hence the explicit astype.

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

TIME_STOP_SINCE = "2026-07-28T14:00:00+00:00"
DEEP_LOSS_PCT = -4.0  # "deep loss" label: worse than any trail giveback can be
MIN_LIQ_DAYS = 21  # below this, the liq part of the study refuses to conclude

TRADES_SQL = """
    SELECT id, symbol, model, direction, entry, sl, opened_at, closed_at,
           close_reason, close_mark_pct,
           opened_at >= %(cutoff)s AS post_timestop
    FROM trailing_positions
    WHERE posted AND filled_at IS NOT NULL AND closed_at IS NOT NULL
      AND close_reason IN ('TRAIL','SL_HIT','TIME_STOP','SOURCE_CLOSED')
      AND close_mark_pct IS NOT NULL
"""


def load_trades(conn) -> pd.DataFrame:
    df = pd.read_sql(TRADES_SQL, conn, params={"cutoff": TIME_STOP_SINCE})
    df["opened_at"] = pd.to_datetime(df["opened_at"], utc=True).astype("datetime64[ns, UTC]")
    df["is_sl"] = df["close_reason"].eq("SL_HIT")
    df["is_deep"] = df["close_mark_pct"] <= DEEP_LOSS_PCT
    return df


def book_stats(df: pd.DataFrame) -> None:
    print("=== book by cohort × reason (unlevered %-points) ===")
    g = df.groupby(["post_timestop", "close_reason"])["close_mark_pct"].agg(["size", "mean", "sum"])
    print(g.round(2).to_string())
    print("\n=== cohort totals ===")
    print(df.groupby("post_timestop")["close_mark_pct"].agg(["size", "sum"]).round(1).to_string())


def add_oi_features(conn, df: pd.DataFrame) -> pd.DataFrame:
    syms = tuple(sorted(df["symbol"].unique()))
    t0 = df["opened_at"].min() - pd.Timedelta(hours=25)
    t1 = df["opened_at"].max()
    oi = pd.read_sql(
        "SELECT ts, symbol, open_interest, oi_value_usdt FROM oi_5m "
        "WHERE symbol IN %(syms)s AND ts BETWEEN %(t0)s AND %(t1)s ORDER BY ts",
        conn,
        params={"syms": syms, "t0": t0, "t1": t1},
    )
    oi["ts"] = pd.to_datetime(oi["ts"], utc=True).astype("datetime64[ns, UTC]")
    oi["px"] = oi["oi_value_usdt"] / oi["open_interest"].replace(0, np.nan)
    oi = oi.sort_values("ts")
    df = df.sort_values("opened_at").copy()
    for label, lag in [("0", 0), ("1h", 1), ("4h", 4), ("24h", 24)]:
        probe = df[["id", "symbol", "opened_at"]].copy()
        probe["t"] = probe["opened_at"] - pd.Timedelta(hours=lag)
        m = pd.merge_asof(
            probe.sort_values("t"),
            oi[["ts", "symbol", "open_interest", "px"]],
            left_on="t",
            right_on="ts",
            by="symbol",
            tolerance=pd.Timedelta(minutes=20),
            direction="backward",
        )
        df = df.merge(
            m[["id", "open_interest", "px"]].rename(columns={"open_interest": f"oi_{label}", "px": f"px_{label}"}),
            on="id",
        )
    sgn = np.where(df["direction"] == "LONG", 1.0, -1.0)
    for w in ["1h", "4h", "24h"]:
        df[f"doi_{w}"] = (df["oi_0"] / df[f"oi_{w}"] - 1) * 100
        df[f"dpx_{w}"] = (df["px_0"] / df[f"px_{w}"] - 1) * 100
        df[f"dpx_{w}_signed"] = df[f"dpx_{w}"] * sgn
    return df


def oi_separation(df: pd.DataFrame) -> None:
    from sklearn.metrics import roc_auc_score

    cols = ["doi_1h", "doi_4h", "doi_24h", "dpx_1h_signed", "dpx_4h_signed"]
    print("\n=== OI/price feature medians by outcome ===")
    print(df.groupby("close_reason")[cols].median().round(3).to_string())
    print("\n=== AUC feature -> deep loss (0.5 = no information) ===")
    for c in cols:
        ok = df[c].notna()
        if df.loc[ok, "is_deep"].nunique() == 2:
            print(f"{c:16s} AUC={roc_auc_score(df.loc[ok, 'is_deep'], df.loc[ok, c]):.3f} (n={ok.sum()})")


def gate_sweep(df: pd.DataFrame) -> None:
    base_sum = df["close_mark_pct"].sum()
    print(f"\n=== gate sweep (baseline n={len(df)} sum={base_sum:.1f} SL={df['is_sl'].sum()}) ===")

    def show(name: str, skip: pd.Series) -> None:
        kept, sk = df[~skip], df[skip]
        print(
            f"{name:34s} skip={skip.sum():4d} ({skip.mean():5.1%}) "
            f"skipped_sum={sk['close_mark_pct'].sum():7.1f} | "
            f"kept sum={kept['close_mark_pct'].sum():7.1f} SL kept={kept['is_sl'].sum()}"
        )

    for thr in [2, 3, 5]:
        show(f"|dOI_1h| > {thr}%", df["doi_1h"].abs() > thr)
        show(f"dOI_1h < -{thr}% (flush)", df["doi_1h"] < -thr)
        show(f"dOI_1h > +{thr}% (spike)", df["doi_1h"] > thr)
    for thr in [5, 8, 12]:
        show(f"|dOI_4h| > {thr}%", df["doi_4h"].abs() > thr)
    for thr in [1, 2, 3]:
        show(f"chase: dpx_1h_signed > +{thr}%", df["dpx_1h_signed"] > thr)
        show(f"fade:  dpx_1h_signed < -{thr}%", df["dpx_1h_signed"] < -thr)


def sl_cap_sweep(df: pd.DataFrame) -> None:
    # The deterministic alternative: the realized SL depth IS the entry->SL
    # distance (sl_exit_mark books fill-at-stop), so it is known at admission.
    d = df[df["post_timestop"]].copy()
    d["sl_dist"] = (d["entry"] - d["sl"]).abs() / d["entry"] * 100
    print(
        f"\n=== SL-distance admission cap (post-timestop cohort, n={len(d)}, sum={d['close_mark_pct'].sum():.1f}) ==="
    )
    print("sl_dist median by outcome:")
    print(d.groupby("close_reason")["sl_dist"].median().round(2).to_string())
    for cap in [4, 5, 6, 8, 10]:
        skip = d["sl_dist"] > cap
        kept, sk = d[~skip], d[skip]
        print(
            f"cap {cap:2d}%: skip={skip.sum():3d} ({skip.mean():5.1%}) "
            f"skipped_sum={sk['close_mark_pct'].sum():7.1f} "
            f"(SL avoided={(sk['close_reason'] == 'SL_HIT').sum():2d}, "
            f"wins lost={(sk['close_reason'] == 'TRAIL').sum():3d}) | "
            f"kept sum={kept['close_mark_pct'].sum():7.1f} worst={kept['close_mark_pct'].min():.1f}"
        )


def liq_coverage(conn) -> None:
    cov = pd.read_sql("SELECT MIN(ts) AS lo, MAX(ts) AS hi, COUNT(*) AS n FROM liq_events", conn)
    lo, hi, n = cov.iloc[0]
    days = 0.0 if pd.isna(lo) else (hi - lo).total_seconds() / 86400
    print(f"\n=== liq_events coverage: {days:.1f} days, {n} events ===")
    if days < MIN_LIQ_DAYS:
        print(
            f"NOT CONCLUDABLE: a liquidation gate needs >= {MIN_LIQ_DAYS} days of overlap "
            "with the live book before the same study can run on liq features. "
            "Collector 41 started 2026-08-03 — re-run then."
        )


def main() -> None:
    sys.path.insert(0, ".")
    from core.database import get_db_connection

    conn = get_db_connection()
    try:
        df = load_trades(conn)
        print(f"trades: {len(df)} (posted+filled+closed, with mark)")
        book_stats(df)
        df = add_oi_features(conn, df)
        print(f"\nOI feature coverage: {df['doi_1h'].notna().mean():.1%}")
        oi_separation(df)
        gate_sweep(df)
        sl_cap_sweep(df)
        liq_coverage(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
