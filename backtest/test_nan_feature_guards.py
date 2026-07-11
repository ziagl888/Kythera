"""Standalone (DB-free) guard for the NaN-imputation contracts on ML serving paths.

Background (T-2026-CU-9050-060, follow-up to P1.13/T-054): since the indicator
engine lets warmup rows flow as NaN instead of fabricating 0/50, three serving
paths can see non-finite features. All three models are XGBClassifiers, and
XGBoost does NOT raise on NaN — it routes NaN down untrained default branches
and produces an out-of-contract prediction (verified against the production
pickle; the original review's "sklearn raises, the broad except suppresses"
premise was falsified). The fix is per-path train/serve parity imputation:

- 10_pump_dump_detector.py (legacy EPD): the legacy trainer imputed
  NULL -> rsi=50, everything else 0 (legacy_trainers/zzz.py:7609-7617), so
  serving imputes non-finite values the same way. rsi=0 would read "extreme
  oversold" — out-of-distribution for this model.
- 24_quasimodo_bot.py / 25_smc_ml_sniper.py: their trainers fit AND score on
  .fillna(0) frames (qm_ml_trainer.py:321/353/378, smc_ml_trainer.py:328/344/365),
  so serving imputes non-finite values to 0.0.

The static checks pin the contracts in the bot sources (runtime asserts would
be swallowed by the fleet-wide broad except blocks); the behavioral checks pin
the imputation semantics and — when the artifact and xgboost are available —
the XGBoost-scores-NaN premise itself.

Run: py -3.13 backtest/test_nan_feature_guards.py
"""

import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EPD_SRC = (ROOT / "10_pump_dump_detector.py").read_text(encoding="utf-8")
QM_SRC = (ROOT / "24_quasimodo_bot.py").read_text(encoding="utf-8")
SMC_SRC = (ROOT / "25_smc_ml_sniper.py").read_text(encoding="utf-8")

IMPUTE_RE = r"float\(v\) if np\.isfinite\(v\) else 0\.0"


def test_epd_legacy_path_imputes_per_trainer_null_contract():
    assert re.search(r"50\.0 if c == \"rsi\" else 0\.0", EPD_SRC), (
        "the legacy EPD path no longer imputes non-finite features per the legacy "
        "trainer's NULL contract (rsi -> 50, everything else -> 0, zzz.py:7609-7617) — "
        "XGBoost would silently score NaN via untrained default branches"
    )
    # The imputation must feed the positional array, not sit next to it.
    assert re.search(r"features_array = np\.array\(\[\[imputed\[c\] for c in EPD_BASE_FEATURES\]\]\)", EPD_SRC), (
        "the legacy feature array no longer reads from the imputed dict"
    )


def test_epd2_branch_keeps_its_own_zero_contract():
    """The EPD2 branch's fillna(0) mirrors train_binary. (Presence pin only —
    an upstream mutation of the feats dict would not be caught here.)"""
    assert re.search(r"reindex\(columns=art\[\"features\"\]\)\.fillna\(0\)", EPD_SRC), (
        "the EPD2 branch lost its trainer-parity fillna(0)"
    )


def test_bots_24_25_impute_nonfinite_to_zero():
    for name, src in (("24_quasimodo_bot.py", QM_SRC), ("25_smc_ml_sniper.py", SMC_SRC)):
        assert re.search(IMPUTE_RE, src), (
            f"{name} lost the non-finite feature imputation (inf/NaN -> 0) that mirrors "
            f"its trainer's fillna(0) — XGBoost would silently score NaN via untrained "
            f"default branches (train/serve skew)"
        )


def test_imputation_semantics():
    """The exact expressions used in the bots, on representative values."""
    nan, inf = float("nan"), float("inf")

    # Bot 24/25 comprehension: non-finite -> 0.0, finite (incl. int flags) preserved as float.
    features = {"a": nan, "b": inf, "c": -inf, "trend_UP": 1, "rsi_14": 37.2, "zero": 0}
    out = {k: (float(v) if math.isfinite(v) else 0.0) for k, v in features.items()}
    assert out == {"a": 0.0, "b": 0.0, "c": 0.0, "trend_UP": 1.0, "rsi_14": 37.2, "zero": 0.0}

    # Legacy EPD comprehension: rsi -> 50, everything else -> 0, finite untouched.
    base = {"rsi": nan, "tsi": nan, "macd": inf, "vol_ratio": 6.0}
    imputed = {c: (v if math.isfinite(v) else (50.0 if c == "rsi" else 0.0)) for c, v in base.items()}
    assert imputed == {"rsi": 50.0, "tsi": 0.0, "macd": 0.0, "vol_ratio": 6.0}


def test_xgboost_scores_nan_premise():
    """Pin the falsified premise: the production legacy pkl predicts through NaN
    without raising. Skips when the artifact or xgboost is unavailable (CI has
    neither) — the static contracts above are the load-bearing net."""
    pkl = ROOT / "pump_dump_model.pkl"
    if not pkl.exists():
        print("  (skip: pump_dump_model.pkl not present)")
        return
    try:
        import joblib
        import numpy as np
        import xgboost  # noqa: F401
    except ImportError:
        print("  (skip: joblib/xgboost not installed)")
        return
    model = joblib.load(pkl)
    row = np.array([[6.0, 2.5, 0.7, 0.01, 1.0, np.nan, 0.0, 0.0, 0.5, 0.8]])
    prob = model.predict_proba(row)  # must NOT raise
    assert prob.shape[1] == 3 and np.isfinite(prob).all(), (
        "legacy model no longer scores NaN input — if this raises or changes, "
        "re-evaluate the imputation rationale in 10_pump_dump_detector.py"
    )


if __name__ == "__main__":
    test_epd_legacy_path_imputes_per_trainer_null_contract()
    test_epd2_branch_keeps_its_own_zero_contract()
    test_bots_24_25_impute_nonfinite_to_zero()
    test_imputation_semantics()
    test_xgboost_scores_nan_premise()
    print("OK — NaN feature-guard contracts hold")
