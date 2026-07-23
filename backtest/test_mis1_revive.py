"""DB-freier Test für den MIS1-Revive (T-2026-KYT-9050-034).

Pinnt die "exakte Restauration": Bot 11 lädt die 8 MIS1-Legacy-Artefakte
(pump_model_*_final.pkl + threshold_*_final.pkl) wieder, ihre 67 Features sind
vollständig vom include_legacy-Builder abgedeckt, und die Geometrie verzweigt
generations-korrekt (MIS1 = calculate_smart_targets beide Richtungen; MIS2-SHORT
= DUMP_RULES-Bracket).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_bot():
    """Lädt 11_ai_mis_bot.py mit den schweren core.*-Deps gemockt; core.mis_features
    bleibt echt (Feature-Namen), core.trade_utils wird gemockt (calculate_smart_targets)."""
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
    os.chdir(REPO)  # load_mis1_models liest die Artefakte relativ zum Repo-Root
    try:
        yield _load_bot()
    finally:
        os.chdir(cwd)


HORIZON_KEYS = ("8h_pump", "24h_pump", "72h_pump", "168h_pump", "8h_dump", "24h_dump", "72h_dump", "168h_dump")


def test_all_eight_mis1_models_load_with_thresholds(bot):
    bot.load_mis1_models()
    for key in HORIZON_KEYS:
        cfg = bot.MIS1_MODELS[key]
        assert cfg["loaded"], f"MIS1 {key} nicht geladen (pump_model_{key}_final.pkl fehlt?)"
        assert cfg["model"] is not None
        # Threshold kommt aus dem separaten threshold_*_final.pkl, muss ein echter
        # Operating-Point sein (0 < t < 1), kein Default-Platzhalter-Leak.
        assert 0.0 < cfg["threshold"] < 1.0, f"{key}: Threshold {cfg['threshold']} unplausibel"
        # 67-Feature-Legacy-Modelle (feature_names_in_).
        assert len(cfg["features"]) == 67, f"{key}: {len(cfg['features'])} Features statt 67"


def test_mis1_features_fully_covered_by_include_legacy_builder(bot):
    """Der entscheidende Selfcheck-Invariant: der include_legacy=True-Builder
    liefert JEDES der 67 MIS1-Features — sonst würde der Startup-Selfcheck das
    Modell entladen (P0.12) und der Revive wäre still tot."""
    from core.mis_features import FEATURE_COLS, LEGACY_ONLY_COLS

    available = set(FEATURE_COLS) | set(LEGACY_ONLY_COLS)  # == add_advanced_features(include_legacy=True)
    bot.load_mis1_models()
    for key in HORIZON_KEYS:
        feats = bot.MIS1_MODELS[key]["features"]
        missing = [f for f in feats if f not in available]
        assert not missing, f"MIS1 {key}: include_legacy-Builder fehlen {missing}"


def test_geometry_branches_by_generation(bot):
    """MIS1 = calculate_smart_targets für BEIDE Richtungen (immediate CMP entry:
    entry_filled=True, expiry=None). MIS2-SHORT = DUMP_RULES-Bracket (Limit-Entry:
    entry_filled=False, expiry=Horizont-Stunden)."""
    bot.calculate_smart_targets = mock.MagicMock(
        return_value={"entry1": 100.0, "entry2": 95.0, "sl": 90.0, "targets": [110.0, 120.0]}
    )
    conn = mock.MagicMock()

    # MIS1 SHORT → Smart-Targets, sofort gefüllt, kein Verfall.
    e1, e2, sl, targets, entry_filled, expiry = bot._mis_geometry(conn, "MIS1", "COINUSDT", "SHORT", "8H", 100.0)
    assert (e1, e2, sl, targets) == (100.0, 95.0, 90.0, [110.0, 120.0])
    assert entry_filled is True and expiry is None
    bot.calculate_smart_targets.assert_called_with(conn, "COINUSDT", "SHORT", 100.0)

    # MIS1 LONG → ebenfalls Smart-Targets.
    *_, entry_filled_l, expiry_l = bot._mis_geometry(conn, "MIS1", "COINUSDT", "LONG", "24H", 100.0)
    assert entry_filled_l is True and expiry_l is None

    # MIS2 SHORT → DUMP_RULES-Bracket (KEIN Smart-Targets), Limit-Entry.
    rules = bot.DUMP_RULES["24H"]
    e1s, e2s, sls, tgts, ef, exp = bot._mis_geometry(conn, "MIS2", "COINUSDT", "SHORT", "24H", 100.0)
    assert e1s == pytest.approx(100.0 * (1 + rules["bounce_pct"] / 100.0))
    assert e2s == e1s  # Einzel-Entry
    assert sls == pytest.approx(e1s * (1 + rules["sl_pct"] / 100.0))
    assert tgts == [pytest.approx(100.0 * (1 - rules["tp_pct"] / 100.0))]
    assert ef is False and exp == 24

    # MIS2 LONG → Smart-Targets (Pump-Seite), sofort gefüllt.
    *_, ef_ml, exp_ml = bot._mis_geometry(conn, "MIS2", "COINUSDT", "LONG", "72H", 100.0)
    assert ef_ml is True and exp_ml is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
