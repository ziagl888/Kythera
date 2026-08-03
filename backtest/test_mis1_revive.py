"""DB-free test for the MIS1 revive (T-2026-KYT-9050-034).

Pins "exact restoration": bot 11 reloads the 8 MIS1 legacy artefacts
(pump_model_*_final.pkl + threshold_*_final.pkl), their 67 features are
fully covered by the include_legacy builder, and the geometry branches
generation-correctly (MIS1 = calculate_smart_targets both directions; MIS2 SHORT
= DUMP_RULES bracket).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_bot():
    """Loads 11_ai_mis_bot.py with heavy core.* deps mocked; core.mis_features
    stays real (feature names), core.trade_utils is mocked (calculate_smart_targets)."""
    spec = importlib.util.spec_from_file_location("ai_mis_bot_revive", os.path.join(REPO, "11_ai_mis_bot.py"))
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        "sys.modules",
        {
            "core.config": mock.MagicMock(),
            "core.candles": mock.MagicMock(),
            "core.charting": mock.MagicMock(),
            "core.database": mock.MagicMock(),
            "core.live_price": mock.MagicMock(),
            "core.market_utils": mock.MagicMock(),
            "core.trade_utils": mock.MagicMock(),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bot():
    cwd = os.getcwd()
    os.chdir(REPO)  # load_mis1_models reads artefacts relative to repo root
    try:
        yield _load_bot()
    finally:
        os.chdir(cwd)


HORIZON_KEYS = ("8h_pump", "24h_pump", "72h_pump", "168h_pump", "8h_dump", "24h_dump", "72h_dump", "168h_dump")


def test_all_eight_mis1_models_load_with_thresholds(bot):
    bot.load_mis1_models()
    for key in HORIZON_KEYS:
        cfg = bot.MIS1_MODELS[key]
        assert cfg["loaded"], f"MIS1 {key} not loaded (pump_model_{key}_final.pkl missing?)"
        assert cfg["model"] is not None
        # Threshold comes from the separate threshold_*_final.pkl, must be a real
        # operating point (0 < t < 1), not a default placeholder leak.
        assert 0.0 < cfg["threshold"] < 1.0, f"{key}: threshold {cfg['threshold']} implausible"
        # 67-feature legacy models (feature_names_in_).
        assert len(cfg["features"]) == 67, f"{key}: {len(cfg['features'])} features instead of 67"


def test_mis1_features_fully_covered_by_include_legacy_builder(bot):
    """The critical self-check invariant: the include_legacy=True builder
    provides EVERY one of the 67 MIS1 features — otherwise the startup self-check would
    unload the model (P0.12) and the revive would be silently dead."""
    from core.mis_features import FEATURE_COLS, LEGACY_ONLY_COLS

    available = set(FEATURE_COLS) | set(LEGACY_ONLY_COLS)  # == add_advanced_features(include_legacy=True)
    bot.load_mis1_models()
    for key in HORIZON_KEYS:
        feats = bot.MIS1_MODELS[key]["features"]
        missing = [f for f in feats if f not in available]
        assert not missing, f"MIS1 {key}: include_legacy-Builder fehlen {missing}"


def test_geometry_branches_by_generation(bot):
    """MIS1 = calculate_smart_targets for BOTH directions (immediate CMP entry:
    entry_filled=True, expiry=None). MIS2 SHORT = DUMP_RULES bracket (limit entry:
    entry_filled=False, expiry=horizon hours)."""
    bot.calculate_smart_targets = mock.MagicMock(
        return_value={"entry1": 100.0, "entry2": 95.0, "sl": 90.0, "targets": [110.0, 120.0]}
    )
    conn = mock.MagicMock()

    # MIS1 SHORT → smart targets, fills immediately, no expiry.
    e1, e2, sl, targets, entry_filled, expiry = bot._mis_geometry(conn, "MIS1", "COINUSDT", "SHORT", "8H", 100.0)
    assert (e1, e2, sl, targets) == (100.0, 95.0, 90.0, [110.0, 120.0])
    assert entry_filled is True and expiry is None
    bot.calculate_smart_targets.assert_called_with(conn, "COINUSDT", "SHORT", 100.0)

    # MIS1 LONG → also smart targets.
    *_, entry_filled_l, expiry_l = bot._mis_geometry(conn, "MIS1", "COINUSDT", "LONG", "24H", 100.0)
    assert entry_filled_l is True and expiry_l is None

    # MIS2 SHORT → DUMP_RULES bracket (NO smart targets), limit entry.
    rules = bot.DUMP_RULES["24H"]
    e1s, e2s, sls, tgts, ef, exp = bot._mis_geometry(conn, "MIS2", "COINUSDT", "SHORT", "24H", 100.0)
    assert e1s == pytest.approx(100.0 * (1 + rules["bounce_pct"] / 100.0))
    assert e2s == e1s  # single entry
    assert sls == pytest.approx(e1s * (1 + rules["sl_pct"] / 100.0))
    assert tgts == [pytest.approx(100.0 * (1 - rules["tp_pct"] / 100.0))]
    assert ef is False and exp == 24

    # MIS2 LONG → smart targets (pump side), fills immediately.
    *_, ef_ml, exp_ml = bot._mis_geometry(conn, "MIS2", "COINUSDT", "LONG", "72H", 100.0)
    assert ef_ml is True and exp_ml is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
