"""Standalone (DB-free) guard for the confidence posting floors (T-2026-CU-9050-171).

The realized-trade analysis (closed_ai_signals ⨝ ml_predictions_master, 32.4k
trades 2026-03..07) showed zero-EV segments below a confidence floor for AIM2
(p<0.70, ~72 % of volume) and the BB sniper legs (p<0.50, ~95 % of volume),
plus a net-negative 0.65-0.70 band for SRA1. Two env floors were added (AIM2,
BB) and one legacy fallback constant bumped (SRA1 — an artifact WITH meta
still brings its own operating point); TD was deliberately left alone
(confidence not selective on realized trades there).

Pinned here:
  * core.prob_floor.load_prob_floor parsing semantics (env override, garbage
    fallback, clamping) — pure, no DB.
  * The static wiring: the floors only ever TIGHTEN a gate
    (max(artifact_threshold, floor)), never replace the artifact's operating
    point, and the TD leg carries no floor.

Run: python backtest/test_prob_floor.py   (or: pytest backtest/test_prob_floor.py)
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.prob_floor import load_prob_floor  # noqa: E402

MASTER_SRC = (ROOT / "15_ai_master_bot.py").read_text(encoding="utf-8")
SNIPER_SRC = (ROOT / "25_smc_ml_sniper.py").read_text(encoding="utf-8")
SRA_SRC = (ROOT / "9_ai_sr_bot.py").read_text(encoding="utf-8")

_ENV_KEYS = ("AIM2_MIN_PROB", "BB_MIN_PROB")


def _with_env(**kv):
    old = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
    os.environ.update(kv)
    try:
        return {k: load_prob_floor(k, 0.5) for k in kv}
    finally:
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
            if old[k] is not None:
                os.environ[k] = old[k]


# ------------------------------------------------------------ parsing semantics


def test_unset_env_returns_default():
    old = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
    try:
        assert load_prob_floor("AIM2_MIN_PROB", 0.70) == 0.70
        assert load_prob_floor("BB_MIN_PROB", 0.50) == 0.50
    finally:
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v


def test_env_overrides_default():
    assert _with_env(AIM2_MIN_PROB="0.75")["AIM2_MIN_PROB"] == 0.75


def test_empty_and_garbage_fall_back_to_default():
    assert _with_env(AIM2_MIN_PROB="")["AIM2_MIN_PROB"] == 0.5
    assert _with_env(AIM2_MIN_PROB="  ")["AIM2_MIN_PROB"] == 0.5
    assert _with_env(AIM2_MIN_PROB="nonsense")["AIM2_MIN_PROB"] == 0.5


def test_values_clamp_into_unit_interval():
    assert _with_env(BB_MIN_PROB="1.5")["BB_MIN_PROB"] == 1.0
    assert _with_env(BB_MIN_PROB="-0.3")["BB_MIN_PROB"] == 0.0


def test_non_finite_values_fall_back_to_default():
    # nan would collapse to 0.0 in the clamp and silently disable the floor.
    assert _with_env(BB_MIN_PROB="nan")["BB_MIN_PROB"] == 0.5
    assert _with_env(BB_MIN_PROB="inf")["BB_MIN_PROB"] == 0.5
    assert _with_env(BB_MIN_PROB="-inf")["BB_MIN_PROB"] == 0.5


# ------------------------------------------------- static wiring in bot 15 (AIM2)


def test_aim2_floor_default_is_070():
    assert re.search(r'^MIN_PROB = load_prob_floor\("AIM2_MIN_PROB", 0\.70\)', MASTER_SRC, re.MULTILINE), (
        "the AIM2 posting floor (default 0.70) is gone or renamed"
    )


def test_aim2_floor_only_tightens_the_artifact_gate():
    # The floor must combine via max() — never replace the artifact threshold.
    assert 'wants_post = prob >= max(ARTIFACT["threshold"], MIN_PROB) and trusted' in MASTER_SRC, (
        "the AIM2 live gate no longer applies max(artifact threshold, floor)"
    )


def test_aim2_shadow_floor_untouched():
    # Data collection must keep running below the posting floor.
    assert "SHADOW_FLOOR = 0.25" in MASTER_SRC, "the AIM2 shadow floor changed — shadow coverage would shrink"


# ------------------------------------------------ static wiring in bot 25 (sniper)


def test_bb_floor_default_is_050_and_td_has_none():
    # Anchored to the MIN_PROB_FLOORS block so a 'td: 0.0' elsewhere can't satisfy it.
    assert re.search(
        r"MIN_PROB_FLOORS = \{\s*'bb': load_prob_floor\(\"BB_MIN_PROB\", 0\.50\),\s*'td': 0\.0,",
        SNIPER_SRC,
    ), (
        "the BB posting floor (default 0.50) is gone/renamed, or TD grew a floor — "
        "TD confidence is not selective on realized trades; leave TD at its artifact operating point"
    )


def test_sniper_shadow_log_floor_untouched():
    # Data collection below the raised posting gate must keep running.
    assert "if prob >= 0.25:" in SNIPER_SRC, "the sniper shadow-log floor changed — shadow coverage would shrink"


def test_sniper_floor_only_tightens_the_loaded_threshold():
    assert "max(base_threshold, MIN_PROB_FLOORS[strategy])" in SNIPER_SRC, (
        "the sniper no longer applies max(artifact/hardcode threshold, floor)"
    )


# --------------------------------------------------- static wiring in bot 9 (SRA)


def test_sra_legacy_threshold_is_070():
    assert re.search(r"^SRA_LEGACY_THRESHOLD = 0\.70", SRA_SRC, re.MULTILINE), (
        "SRA_LEGACY_THRESHOLD moved off 0.70 — the 0.65-0.70 band was net negative on realized trades"
    )


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
