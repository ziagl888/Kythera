# -*- coding: utf-8 -*-
"""Inspect split thresholds of pathological features in the boosters (read-only)."""
import sys, io, re, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
os.chdir(r"C:\Users\Michael\PycharmProjects\crypto_trading_bot_v2")
import warnings; warnings.filterwarnings("ignore")
import joblib
import numpy as np

SUS = ["boll_upper_dist_atr_dist_pct", "boll_lower_dist_atr_dist_pct",
       "ema_200_dist_atr_dist_pct", "ema_9_cross_above_21_dist_pct"]
KEYS = ["8h_pump", "8h_dump", "24h_pump", "24h_dump", "72h_pump", "72h_dump", "168h_pump", "168h_dump"]

for k in KEYS:
    m = joblib.load(f"pump_model_{k}_final.pkl")
    dump = m.get_booster().get_dump(with_stats=False)
    text = "\n".join(dump)
    print(f"\n--- {k} ---")
    for feat in SUS:
        # splits look like  [feat<123.456]
        vals = [float(v) for v in re.findall(rf"\[{re.escape(feat)}<([-0-9.e+]+)\]", text)]
        if not vals:
            print(f"  {feat}: no splits")
            continue
        vals = np.array(vals)
        print(f"  {feat}: {len(vals)} splits | min={vals.min():.4g} max={vals.max():.4g} "
              f"median={np.median(vals):.4g} | thresholds>1000 abs: {np.sum(np.abs(vals)>1000)}")
print("\nDONE")
