# -*- coding: utf-8 -*-
"""Read-only inspection of MIS1 model + threshold artifacts."""
import sys, json, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import joblib
import numpy as np
import sklearn, xgboost

BASE = r"C:\Users\Michael\PycharmProjects\crypto_trading_bot_v2"
KEYS = ["8h_pump", "8h_dump", "24h_pump", "24h_dump", "72h_pump", "72h_dump", "168h_pump", "168h_dump"]

print("ENV sklearn:", sklearn.__version__, "| xgboost:", xgboost.__version__)
print("=" * 80)

results = {}
for k in KEYS:
    path = f"{BASE}\\pump_model_{k}_final.pkl"
    m = joblib.load(path)
    info = {}
    info["type"] = type(m).__module__ + "." + type(m).__name__
    # sklearn version pickled in artifact
    info["sklearn_ver_in_artifact"] = getattr(m, "__sklearn_version__", None) or m.__dict__.get("_sklearn_version", None)
    fni = getattr(m, "feature_names_in_", None)
    info["n_features_in_"] = getattr(m, "n_features_in_", None)
    info["feature_names_in_"] = list(fni) if fni is not None else None
    cls = getattr(m, "classes_", None)
    info["classes_"] = cls.tolist() if cls is not None else None
    # xgboost specifics
    try:
        booster = m.get_booster()
        info["booster_feature_names"] = booster.feature_names
        info["booster_num_features"] = booster.num_features()
        # version saved in booster config
        cfg = json.loads(booster.save_config())
        info["xgb_config_version"] = cfg.get("version", None)
        for attr in ["n_estimators", "max_depth", "learning_rate", "subsample", "colsample_bytree",
                     "scale_pos_weight", "eval_metric", "objective", "base_score", "random_state"]:
            info["param_" + attr] = getattr(m, attr, None)
        info["best_iteration"] = getattr(m, "best_iteration", None)
        try:
            info["actual_num_trees"] = booster.num_boosted_rounds()
        except Exception:
            info["actual_num_trees"] = None
    except Exception as e:
        info["booster_error"] = repr(e)
    # feature importances
    try:
        imp = m.feature_importances_
        names = info["feature_names_in_"] or info.get("booster_feature_names") or [f"f{i}" for i in range(len(imp))]
        info["importances"] = dict(zip(names, [float(x) for x in imp]))
    except Exception as e:
        info["importances_error"] = repr(e)
    results[k] = info

# thresholds
thresholds = {}
for k in KEYS:
    t = joblib.load(f"{BASE}\\threshold_{k}_final.pkl")
    thresholds[k] = {"type": type(t).__name__, "value": float(t)}

# ---- report ----
print("\n### THRESHOLDS")
for k, v in thresholds.items():
    print(f"{k:12s} type={v['type']:10s} value={v['value']}")

print("\n### MODEL SUMMARY")
for k, info in results.items():
    print(f"\n--- {k} ---")
    for key in ["type", "sklearn_ver_in_artifact", "xgb_config_version", "n_features_in_",
                "booster_num_features", "classes_", "param_objective", "param_n_estimators",
                "param_max_depth", "param_learning_rate", "param_scale_pos_weight",
                "param_base_score", "param_random_state", "param_eval_metric",
                "best_iteration", "actual_num_trees"]:
        if key in info:
            print(f"  {key}: {info[key]}")

# feature name comparison across models
print("\n### FEATURE NAME COMPARISON")
ref_key = KEYS[0]
ref = results[ref_key]["feature_names_in_"]
print(f"reference = {ref_key}, n={len(ref) if ref else None}")
all_same = True
for k in KEYS[1:]:
    f = results[k]["feature_names_in_"]
    if f is None:
        print(f"{k}: feature_names_in_ = None !")
        all_same = False
        continue
    same_set = set(f) == set(ref)
    same_order = f == ref
    if not same_order:
        all_same = False
        print(f"{k}: SAME_SET={same_set} SAME_ORDER={same_order}")
        if same_set:
            diffs = [(i, a, b) for i, (a, b) in enumerate(zip(ref, f)) if a != b]
            print(f"   order diffs (first 10): {diffs[:10]}")
        else:
            print(f"   only_in_ref: {sorted(set(ref)-set(f))}")
            print(f"   only_in_{k}: {sorted(set(f)-set(ref))}")
    else:
        print(f"{k}: identical (names+order)")
print("ALL 8 IDENTICAL:", all_same)

print("\n### FULL FEATURE LIST (reference model, in order)")
for i, name in enumerate(ref or []):
    print(f"{i:3d}  {name}")

# booster feature names vs sklearn wrapper
print("\n### BOOSTER feature_names vs feature_names_in_")
for k in KEYS:
    bf = results[k].get("booster_feature_names")
    fni = results[k]["feature_names_in_"]
    print(f"{k}: booster_names={'None' if bf is None else ('MATCH' if bf == fni else 'MISMATCH')}")

# zero-importance features per model
print("\n### ZERO-IMPORTANCE FEATURES PER MODEL")
zero_sets = {}
for k in KEYS:
    imp = results[k].get("importances", {})
    zeros = sorted([n for n, v in imp.items() if v == 0.0])
    zero_sets[k] = set(zeros)
    print(f"\n{k}: {len(zeros)} zero-importance features")
    for n in zeros:
        print(f"    {n}")
common = set.intersection(*zero_sets.values()) if zero_sets else set()
print(f"\nZero in ALL 8 models ({len(common)}):")
for n in sorted(common):
    print(f"    {n}")

# top10 importances per model
print("\n### TOP-10 IMPORTANCES PER MODEL")
for k in KEYS:
    imp = results[k].get("importances", {})
    top = sorted(imp.items(), key=lambda x: -x[1])[:10]
    print(f"\n{k}:")
    for n, v in top:
        print(f"    {v:.4f}  {n}")

# dump JSON for downstream comparison with bot feature list
with open(r"C:\Users\Michael\.claude\jobs\1c6ca5da\tmp\model_info.json", "w") as fh:
    json.dump({"models": {k: {kk: vv for kk, vv in v.items() if kk != "importances"} for k, v in results.items()},
               "thresholds": thresholds,
               "ref_features": ref}, fh, indent=1)
print("\nDONE")
