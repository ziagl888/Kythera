"""
tools/retrain_sra2.py — SRA2: Retrain des S/R-Meta-Filters (Task #1, 2026-07-06).

Basis: legacy_trainers/X9-SR-ANALYZER-Schritt1.py (bewiesener SRA1-Trainer,
Meta-Labeling über closed_trades3, getrennte LONG/SHORT-Modelle, chronologischer
Split). Vier Verbesserungen per Operator-Beschluss (docs/MODEL_INTENT.md §5):

  1. PREIS-ROHSPALTEN RAUS (Skalen-Leakage): die 15 absoluten Preis-Level +
     atr_14/macd in Preisskala fliegen; erhalten bleiben die skalenfreien
     Pendants (pct_*, *_atr) plus neue skalenfreie Ersatzspalten
     (macd_dif_pct, macd_dea_pct, atr_pct).
  2. LOOK-AHEAD-FIX (Audit 13-P2b): Indikator-Join nur auf die letzte
     GESCHLOSSENE 1h-Kerze (open_time <= signal_time - 1h) — der alte Join
     traf die forming candle (bis +1h Zukunft).
  3. NaN LIVE-KONSISTENT: kein globales fillna(median) mehr (Train/Live-
     Lücke) — XGBoost verarbeitet NaN nativ, exakt wie der Bot (P1.20).
  4. Isotonic-Kalibrierung auf Validation + Threshold via pick_threshold_safe
     (Ø-PnL/Trade, Mindest-n) statt implizitem 0,65-Hardcode.

Label (Operator bestätigt 2026-07-06 + Code-Beweis 13-updatesupportresistance):
  status in ('SL1','SL2','SL3','4') = WIN (SL nach TP1/2/3 = Trailing-Gewinn),
  'SL0' = LOSS. PnL-Approximation für die Threshold-Ökonomie: 25 %-Tranchen je
  erreichtem Target, Rest zum Trailing-SL-Level (SL1→Entry, SL2→T1, SL3→T2).

Artefakte NUR nach staging (P1.35): sra2_model_{LONG,SHORT}.json (natives
XGBoost-JSON wie der Bot es lädt) + _meta.json (Vertrag: features, threshold,
model_id='SRA2') + _calib.pkl. Deploy entscheidet Michi.
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

from core.database import get_db_connection  # noqa: E402
from core.sra_features import SRA2_FEATURES, build_sra2_features  # noqa: E402
from tools.retrain_from_replay import STAGING_DIR, pick_threshold_safe  # noqa: E402

# Feature-Vertrag + Builder leben in core/sra_features.py — EIN Builder fuer
# Trainer und Serving (X-R1). 9_ai_sr_bot importiert denselben Modul.

# Roh-Indikatorspalten, die build_sra2_features als Eingabe braucht (SQL-Projektion).
INDICATOR_COLS = (
    "open_time, close, rsi_9, rsi_14, rsi_24, macd_dif_fast_9_21_9, "
    "macd_dea_fast_9_21_9, tsi_fast_12_7_7, tsi_fast_12_7_7_signal, atr_14, "
    "r_squared, boll_upper_20, boll_mid_20, boll_lower_20, donchian_upper_20, "
    "donchian_lower_20, donchian_mid_20, support_price, resistance_price, "
    "ema_9, ema_21, wma_9, wma_21, kama_9, kama_21, trend_direction"
)


def approx_pnl_pct(row) -> float:
    """PnL-Approximation je Status für die Threshold-Ökonomie: 25 %-Tranchen je
    erreichtem Target, Rest zum Trailing-SL (SL1→Entry, SL2→T1, SL3→T2),
    SL0 = voller SL, '4' = volle Ladder. Fees 0,10 % RT."""
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
        else:  # SL0 / unbekannt = voller Verlust zum SL
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
                ind_cache[coin] = pd.read_sql_query(
                    f'SELECT {INDICATOR_COLS} FROM "{coin}_1h_indicators" ORDER BY open_time ASC',
                    conn,
                )
            except Exception:
                conn.rollback()
                ind_cache[coin] = None
        dfi = ind_cache[coin]
        if dfi is None or dfi.empty:
            continue
        # LOOK-AHEAD-FIX (Verbesserung 2): letzte GESCHLOSSENE 1h-Kerze —
        # open_time + 1h <= Signalzeit. TZ-Vertrag (AIM2-Vermessung 2026-07-05):
        # closed_trades3-Writer stempeln PG-LOKALZEIT (Europe/Bucharest) —
        # naive Zeiten deshalb dort lokalisieren, dann UTC; Kerzen sind UTC.
        t_sig = pd.Timestamp(tr["time"])
        if t_sig.tzinfo is None:
            # DST-Kanten deterministisch auflösen (ambiguous: Herbst-Doppelstunde
            # → DST-Variante; nonexistent: Frühjahrs-Lücke → vorwärts schieben).
            # Fehler von ±1h an 2 Stunden/Jahr ist fürs Training immateriell.
            t_sig = t_sig.tz_localize("Europe/Bucharest", ambiguous=True, nonexistent="shift_forward")
        t_sig = t_sig.tz_convert("UTC")
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
        # Zeiten sind ab hier naive UTC — konsistent für Split-Quantile.
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
        raise SystemExit(f"Zu wenig Events ({len(df)}) — Abbruch (Guard wie Schritt1)")
    print(f"Dataset: {len(df)} Events, {df['signal_time'].min()} → {df['signal_time'].max()}")

    results: dict = {"strategy": "sra2", "features": SRA2_FEATURES}
    for direction in ("LONG", "SHORT"):
        d = df[df["direction"] == direction].reset_index(drop=True)
        if len(d) < 300:
            print(f"SRA2 {direction}: nur {len(d)} Events — übersprungen")
            continue
        train, val, test = chrono_split_gap(d)
        base = d["outcome"].mean() * 100
        print(f"SRA2 {direction}: {len(d)} Events | split {len(train)}/{len(val)}/{len(test)} | "
              f"Basisrate WIN {base:.1f}%")

        # Hyperparameter wie der bewiesene Schritt1-Trainer
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
        model.save_model(out)  # natives XGBoost-JSON — Format wie der Bot es lädt
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
