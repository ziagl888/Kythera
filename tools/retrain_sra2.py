"""
tools/retrain_sra2.py — SRA2: Retraining of the S/R meta-filter (task #1, 2026-07-06).

Basis: legacy_trainers/X9-SR-ANALYZER-step1.py (proven SRA1 trainer,
meta-labeling via closed_trades3, separate LONG/SHORT models, chronological
split). Four improvements per operator decision (docs/MODEL_INTENT.md §5):

  1. RAW PRICE COLUMNS OUT (scale leakage): the 15 absolute price levels +
     atr_14/macd in price scale are removed; retained are the scale-free
     counterparts (pct_*, *_atr) plus new scale-free replacement columns
     (macd_dif_pct, macd_dea_pct, atr_pct).
  2. LOOK-AHEAD FIX (audit 13-P2b): indicator join only to the last
     CLOSED 1h candle (open_time <= signal_time - 1h) — the old join
     hit the forming candle (up to +1h future).
  3. NaN LIVE-CONSISTENT: no global fillna(median) anymore (train/live
     gap) — XGBoost processes NaN natively, exactly like the bot (P1.20).
  4. Isotonic calibration on validation + threshold via pick_threshold_safe
     (avg-PnL/trade, min-n) instead of implicit 0.65 hardcode.

Label (operator confirmed 2026-07-06 + code evidence 13-updatesupportresistance):
  status in ('SL1','SL2','SL3','4') = WIN (SL after TP1/2/3 = trailing win),
  'SL0' = LOSS. PnL approximation for threshold economics: 25% tranches per
  target reached, rest to trailing-SL level (SL1→entry, SL2→T1, SL3→T2).

Artefacts ONLY to staging (P1.35): sra2_model_{LONG,SHORT}.json (native
XGBoost JSON as the bot loads it) + _meta.json (contract: features, threshold,
model_id='SRA2') + _calib.pkl. Deploy is Michi's decision.
"""

from __future__ import annotations

import json
import os
import sys

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.isotonic import IsotonicRegression

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.candles import read_indicators  # noqa: E402
from core.database import get_db_connection  # noqa: E402
from core.sra_features import SRA2_FEATURES, build_sra2_features  # noqa: E402
from core.time import legacy_naive_to_utc  # noqa: E402
from tools.retrain_from_replay import STAGING_DIR, pick_threshold_safe  # noqa: E402

# Feature contract + builder live in core/sra_features.py — ONE builder for
# trainer and serving (X-R1). 9_ai_sr_bot imports the same module.

# Raw indicator columns that build_sra2_features needs as input (SQL projection).
INDICATOR_COLS = (
    "open_time, close, rsi_9, rsi_14, rsi_24, macd_dif_fast_9_21_9, "
    "macd_dea_fast_9_21_9, tsi_fast_12_7_7, tsi_fast_12_7_7_signal, atr_14, "
    "r_squared, boll_upper_20, boll_mid_20, boll_lower_20, donchian_upper_20, "
    "donchian_lower_20, donchian_mid_20, support_price, resistance_price, "
    "ema_9, ema_21, wma_9, wma_21, kama_9, kama_21, trend_direction"
)
# Same columns as projection for read_indicators (open_time first — the read
# sorts by that). One source of truth, parsed from INDICATOR_COLS.
INDICATOR_COL_LIST = tuple(c.strip() for c in INDICATOR_COLS.split(","))


def approx_pnl_pct(row) -> float:
    """PnL approximation per status for threshold economics: 25% tranches per
    target reached, rest to trailing-SL (SL1→entry, SL2→T1, SL3→T2),
    SL0 = full SL, '4' = full ladder. Fees 0.10% RT."""
    try:
        entry = float(row["entry"])
        if entry <= 0:
            return 0.0
        is_long = str(row["direction"]).upper() == "LONG"
        sign = 1.0 if is_long else -1.0
        tgts = [row.get(f"target{i}") for i in range(1, 5)]
        tgts = [float(t) if t is not None else None for t in tgts]
        sl = float(row["sl"]) if row.get("sl") is not None else None

        def leg(price):
            return sign * (float(price) - entry) / entry * 100.0

        status = str(row["status"]).strip()
        if status == "4":
            legs = [leg(t) for t in tgts if t is not None]
        elif status in ("SL1", "SL2", "SL3"):
            n_hit = int(status[2])
            legs = [leg(tgts[i]) for i in range(n_hit) if tgts[i] is not None]
            trail_exit = entry if n_hit == 1 else tgts[n_hit - 2]
            legs += [leg(trail_exit)] * (4 - n_hit) if trail_exit is not None else []
        else:  # SL0 / unknown = full loss to SL
            legs = [leg(sl)] * 4 if sl is not None else [-5.0] * 4
        return float(np.mean(legs)) - 0.10 if legs else 0.0
    except (TypeError, ValueError, KeyError):
        return 0.0


def load_dataset(conn) -> pd.DataFrame:
    trades = pd.read_sql_query(
        """SELECT lfd, time, coin, direction, entry, sl,
                  target1, target2, target3, target4, status
           FROM closed_trades3 ORDER BY time ASC""",
        conn,
    )
    trades["coin"] = trades["coin"].str.replace("USDC", "USDT", regex=False)
    print(f"closed_trades3: {len(trades)} Trades, {trades['coin'].nunique()} Coins")

    rows = []
    cur = conn.cursor()
    ind_cache: dict[str, pd.DataFrame | None] = {}
    for _, tr in trades.iterrows():
        coin = tr["coin"]
        if coin not in ind_cache:
            try:
                # Via core.candles: CLOSED 1h indicator rows, ascending. The
                # per-signal mask below (ot <= t_sig-1h) selects only closed
                # candles anyway; include_forming=False keeps the forming row out
                # of the cache (R1).
                ind_cache[coin] = read_indicators(
                    conn, coin, "1h", include_forming=False, columns=INDICATOR_COL_LIST
                )
            except Exception:
                conn.rollback()
                ind_cache[coin] = None
        dfi = ind_cache[coin]
        if dfi is None or dfi.empty:
            continue
        # LOOK-AHEAD FIX (improvement 2): last CLOSED 1h candle —
        # open_time + 1h <= signal time. TZ contract since the R3 flip
        # (T-2026-KYT-9050-005): the fixed Bucharest localization is out, history
        # reading is central in core.time (docs/UTC_POLICY.md §6).
        # closed_trades3 is a pure legacy source (last row 2026-02-23,
        # 8,245 rows, read-only measured 2026-08-01) — it lies completely before
        # the flip and thus belongs in the backfill scope or under each
        # cutover constant. Candles are UTC.
        t_sig = pd.Timestamp(legacy_naive_to_utc(pd.Timestamp(tr["time"]).to_pydatetime())).tz_localize("UTC")
        ot = pd.to_datetime(dfi["open_time"], utc=True)
        mask = ot <= (t_sig - pd.Timedelta(hours=1))
        if not mask.any():
            continue
        ind = dfi[mask].iloc[-1].to_dict()

        f = build_sra2_features(ind)
        f["signal_time"] = t_sig
        f["direction"] = str(tr["direction"]).upper()
        f["outcome"] = 1 if str(tr["status"]).strip() in ("SL1", "SL2", "SL3", "4") else 0
        f["net_pnl_pct"] = approx_pnl_pct(tr)
        rows.append(f)
    cur.close()

    df = pd.DataFrame(rows)
    if not df.empty:
        df["signal_time"] = pd.to_datetime(df["signal_time"], utc=True).dt.tz_localize(None)
        df = df.sort_values("signal_time").reset_index(drop=True)
        # Times are naive UTC from here on — consistent for split quantiles.
    return df


def chrono_split_gap(df: pd.DataFrame, gap_days: float = 7.0):
    t_train = df["signal_time"].quantile(0.70)
    t_val = df["signal_time"].quantile(0.85)
    gap = pd.Timedelta(days=gap_days)
    return (
        df[df["signal_time"] <= t_train],
        df[(df["signal_time"] > t_train + gap) & (df["signal_time"] <= t_val)],
        df[df["signal_time"] > t_val + gap],
    )


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    conn = get_db_connection()
    df = load_dataset(conn)
    conn.close()
    if df.empty or len(df) < 300:
        raise SystemExit(f"Too few events ({len(df)}) — abort (guard like step1)")
    print(f"Dataset: {len(df)} Events, {df['signal_time'].min()} → {df['signal_time'].max()}")

    results: dict = {"strategy": "sra2", "features": SRA2_FEATURES}
    for direction in ("LONG", "SHORT"):
        d = df[df["direction"] == direction].reset_index(drop=True)
        if len(d) < 300:
            print(f"SRA2 {direction}: only {len(d)} events — skipped")
            continue
        train, val, test = chrono_split_gap(d)
        base = d["outcome"].mean() * 100
        print(f"SRA2 {direction}: {len(d)} events | split {len(train)}/{len(val)}/{len(test)} | "
              f"base rate WIN {base:.1f}%")

        # Hyperparameters like the proven step1 trainer
        model = xgb.XGBClassifier(
            objective="binary:logistic", eval_metric="auc", n_estimators=400,
            max_depth=4, learning_rate=0.025, subsample=0.82,
            colsample_bytree=0.75, reg_lambda=1.3, reg_alpha=0.1,
            tree_method="hist", random_state=42, early_stopping_rounds=50,
        )
        model.fit(train[SRA2_FEATURES], train["outcome"].astype(int),
                  eval_set=[(val[SRA2_FEATURES], val["outcome"].astype(int))], verbose=False)

        p_val = model.predict_proba(val[SRA2_FEATURES])[:, 1]
        p_test = model.predict_proba(test[SRA2_FEATURES])[:, 1]

        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(p_val, val["outcome"].astype(int))

        thresh, val_stats = pick_threshold_safe(val.reset_index(drop=True), p_val, min_n=100)
        m = p_test >= thresh if thresh is not None else np.zeros(len(p_test), dtype=bool)
        test_stats = {
            "n_taken": int(m.sum()),
            "wr": round(float(test.loc[m, "outcome"].mean()) * 100, 1) if m.sum() else None,
            "avg_net_pnl_pct": round(float(test.loc[m, "net_pnl_pct"].mean()), 3) if m.sum() else None,
            "sum_net_pnl_pct": round(float(test.loc[m, "net_pnl_pct"].sum()), 1) if m.sum() else None,
            "base_rate_test": round(float(test["outcome"].mean()) * 100, 1),
            "n_test_total": int(len(test)),
        }
        print(f"  Threshold {thresh} | TEST: {json.dumps(test_stats)}")

        meta = {
            "trainer": "tools/retrain_sra2.py", "strategy": "sra2",
            "model_id": "SRA2", "direction": direction,
            "model_type": "binary (1 = TP1 erreicht: status SL1/SL2/SL3/4)",
            "success_proba": "predict_proba[:, 1]",
            "features": SRA2_FEATURES,
            "optimal_threshold": thresh,
            "label_source": "closed_trades3 (Meta-Labeling; Semantik Operator+Code-verifiziert 2026-07-06)",
            "changes_vs_sra1": "Preis-Rohspalten raus (22 skalenfreie Features), "
                               "Look-ahead-Fix (nur geschlossene 1h-Kerze), NaN nativ "
                               "statt Median-Imputation, Isotonic + pick_threshold_safe",
            "split": "chronological 70/15/15 + 7d purge gap",
            "xgboost_version": xgb.__version__,
            "n_train": len(train), "n_val": len(val), "n_test": len(test),
            "val_stats": val_stats, "test_stats": test_stats,
        }
        out = os.path.join(STAGING_DIR, f"sra2_model_{direction}.json")
        model.save_model(out)  # native XGBoost JSON — format as bot loads it
        joblib.dump(iso, os.path.join(STAGING_DIR, f"sra2_model_{direction}_calib.pkl"))
        with open(os.path.join(STAGING_DIR, f"sra2_model_{direction}_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, default=str)
        print(f"  💾 {out}")
        results[direction] = {"n_events": len(d), "base_rate": round(base, 1),
                              "threshold": thresh, "val_stats": val_stats, "test_stats": test_stats}

    with open(os.path.join(STAGING_DIR, "retrain_sra2_stats.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, default=str)
    print(f"\nStats: {os.path.join(STAGING_DIR, 'retrain_sra2_stats.json')}")


if __name__ == "__main__":
    main()
