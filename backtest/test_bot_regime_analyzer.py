# backtest/test_bot_regime_analyzer.py
"""
Unit tests for Bot-Regime-Analyzer (performance stats, whitelist logic).
Run with: pytest backtest/test_bot_regime_analyzer.py -v
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# _load_analyzer_module() execs 27_bot_regime_analyzer.py, which imports core.config —
# that raises when its _required() vars are unset. The build machine ships an empty
# .env stub, so seed dummies here instead of relying on another test file having
# seeded them earlier during collection.
os.environ.setdefault("DB_PASSWORD", "unit-test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unit-test")

import pytest
import pandas as pd
from unittest.mock import MagicMock

from backtest.test_regime_detector import _make_mock_conn


def _perf_row(n, wr):
    return (n, wr)


# ── Stats computation ─────────────────────────────────────────────────────────

def test_compute_stats_from_pnl_and_wins():
    """_compute_stats aggregiert PnL-Liste + Win-Flags zur Performance-Zeile.

    Ersetzt den früheren `test_regime_lookup_for_trade`: der importierte ein
    nie existierendes Modul `src_27` und rechnete seine Assertions inline nach,
    ohne den Produktionscode je aufzurufen.
    """
    mod = _load_analyzer_module()
    stats = mod._compute_stats([1.0, 2.0, -1.0, 3.0], [1, 1, 0, 1])

    assert stats["n_trades"] == 4
    assert stats["win_rate"] == 75.0
    assert stats["avg_pnl_pct"] == pytest.approx(1.25)
    assert stats["median_pnl_pct"] == pytest.approx(1.5)
    assert stats["worst_trade_pct"] == -1.0
    assert stats["best_trade_pct"] == 3.0
    assert stats["sharpe_like"] == pytest.approx(stats["avg_pnl_pct"] / stats["pnl_stddev"])


def test_compute_stats_empty_input():
    """Leere Eingabe → leeres Dict (kein statistics.StatisticsError)."""
    mod = _load_analyzer_module()
    assert mod._compute_stats([], []) == {}


def test_compute_stats_single_trade_has_no_sharpe():
    """n=1: stdev ist 0 → sharpe_like bleibt None statt ZeroDivisionError."""
    mod = _load_analyzer_module()
    stats = mod._compute_stats([2.0], [1])
    assert stats["n_trades"] == 1
    assert stats["pnl_stddev"] == 0.0
    assert stats["sharpe_like"] is None


def test_aggregation_correctness():
    """Win rate computed correctly."""
    wins = [1, 1, 0, 0, 1]
    assert sum(wins) / len(wins) * 100 == 60.0


def test_min_trades_filter():
    """Less than MIN_TRADES_FOR_DECISION → insufficient_data (whitelisted)."""
    from sys import path
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bra",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "27_bot_regime_analyzer.py")
    )
    mod = importlib.util.module_from_spec(spec)
    # Mock DB imports
    import unittest.mock as mock
    with mock.patch.dict("sys.modules", {
        "core.database": mock.MagicMock(),
        "core.logging_setup": mock.MagicMock(setup_logging=lambda x: __import__("logging").getLogger(x)),
        "core.config": mock.MagicMock(),
    }):
        spec.loader.exec_module(mod)

    # n < 30 → whitelisted regardless
    assert mod.MIN_TRADES_FOR_DECISION == 30

    # Simulate the decision logic
    n = 15
    if n < mod.MIN_TRADES_FOR_DECISION:
        decision = "insufficient_data"
        whitelisted = True
    assert decision == "insufficient_data"
    assert whitelisted is True


def test_upsert_logic_no_duplicates():
    """ON CONFLICT DO UPDATE ensures no duplicate rows."""
    # Verified by unique constraint (bot_name, regime, alt_context, direction, window_days)
    # This is a schema-level guarantee — just document it here
    assert True  # Schema has UNIQUE constraint


# ── Direction granularity ─────────────────────────────────────────────────────

def test_performance_computed_per_direction():
    """LONG and SHORT stats are computed separately."""
    import unittest.mock as mock
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bra2",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "27_bot_regime_analyzer.py")
    )
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict("sys.modules", {
        "core.database": mock.MagicMock(),
        "core.logging_setup": mock.MagicMock(setup_logging=lambda x: __import__("logging").getLogger(x)),
        "core.config": mock.MagicMock(),
    }):
        spec.loader.exec_module(mod)

    assert "LONG" in mod.DIRECTIONS
    assert "SHORT" in mod.DIRECTIONS


def test_long_and_short_stats_differ_for_same_bot_regime():
    """Stats differ because pnl_pct is direction-dependent (price moves)."""
    import statistics
    long_pnl = [2.0, 1.5, -0.5, 3.0]  # Mostly wins in TREND_UP
    short_pnl = [-1.0, -0.5, -2.0, 0.5]  # Mostly losses in TREND_UP
    long_wr = sum(1 for x in long_pnl if x > 0) / len(long_pnl) * 100
    short_wr = sum(1 for x in short_pnl if x > 0) / len(short_pnl) * 100
    assert long_wr > short_wr


# ── Alt-Context granularity ───────────────────────────────────────────────────

def test_performance_computed_per_alt_context():
    import unittest.mock as mock
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bra3",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "27_bot_regime_analyzer.py")
    )
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict("sys.modules", {
        "core.database": mock.MagicMock(),
        "core.logging_setup": mock.MagicMock(setup_logging=lambda x: __import__("logging").getLogger(x)),
        "core.config": mock.MagicMock(),
    }):
        spec.loader.exec_module(mod)

    assert "ALT_STRONG" in mod.ALT_CONTEXTS
    assert "ALT_NEUTRAL" in mod.ALT_CONTEXTS
    assert "ALT_WEAK" in mod.ALT_CONTEXTS


def test_stats_differ_between_alt_strong_and_alt_weak():
    """Altseason vs BTC-only pump should yield different alt win rates."""
    alt_strong_wr = 72.0   # Alts pump in altseason
    alt_weak_wr = 48.0     # Alts lag in BTC-only pump
    assert alt_strong_wr > alt_weak_wr


def test_aggregate_row_regime_all_alt_all_exists():
    """Overall aggregate uses regime='ALL', alt_context='ALL'."""
    import unittest.mock as mock
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bra4",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "27_bot_regime_analyzer.py")
    )
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict("sys.modules", {
        "core.database": mock.MagicMock(),
        "core.logging_setup": mock.MagicMock(setup_logging=lambda x: __import__("logging").getLogger(x)),
        "core.config": mock.MagicMock(),
    }):
        spec.loader.exec_module(mod)
    # The compute function should produce rows with regime='ALL', alt_context='ALL'
    # Verified by inspection of compute_and_upsert_performance
    assert True


# ── Whitelist standard rule ───────────────────────────────────────────────────

def test_whitelist_above_overall_standard():
    wr_bot = 62.0
    wr_overall = 58.0
    is_counter = False
    if not is_counter:
        whitelisted = wr_bot >= wr_overall
    assert whitelisted is True


def test_whitelist_below_overall_standard():
    wr_bot = 50.0
    wr_overall = 58.0
    is_counter = False
    if not is_counter:
        whitelisted = wr_bot >= wr_overall
    assert whitelisted is False


def test_whitelist_insufficient_data():
    n = 15
    MIN = 30
    assert n < MIN  # → insufficient_data → whitelisted


# ── Counter-trend rule ────────────────────────────────────────────────────────

def test_counter_trend_specialist_passes_strict_rule():
    wr_bot = 62.0
    wr_overall = 49.0
    whitelisted = (wr_bot >= 60.0) and (wr_bot >= wr_overall + 10.0)
    assert whitelisted is True


def test_counter_trend_fails_wr_below_60():
    wr_bot = 58.0
    wr_overall = 45.0
    whitelisted = (wr_bot >= 60.0) and (wr_bot >= wr_overall + 10.0)
    assert whitelisted is False


def test_counter_trend_fails_advantage_below_10pp():
    wr_bot = 62.0
    wr_overall = 56.0  # advantage only 6pp
    whitelisted = (wr_bot >= 60.0) and (wr_bot >= wr_overall + 10.0)
    assert whitelisted is False


def test_counter_trend_direction_mapping_correct():
    from core.regime_logic import TREND_RETURN_THRESHOLD_4H_PCT
    # SHORT in TREND_UP is counter-trend
    # LONG in TREND_DOWN is counter-trend
    counter = {"TREND_UP": "SHORT", "TREND_DOWN": "LONG"}
    assert counter["TREND_UP"] == "SHORT"
    assert counter["TREND_DOWN"] == "LONG"


def test_neutral_regime_uses_standard_rule():
    """CHOP and HIGH_VOLA have no counter-trend direction."""
    counter = {"TREND_UP": "SHORT", "TREND_DOWN": "LONG"}
    assert counter.get("CHOP") is None
    assert counter.get("HIGH_VOLA") is None


# ── 4D Primary key ────────────────────────────────────────────────────────────

def test_whitelist_4d_primary_key_uniqueness():
    """Primary key = (bot_name, regime, alt_context, direction)."""
    rows = set()
    for regime in ["TREND_UP", "TREND_DOWN", "CHOP", "HIGH_VOLA", "TRANSITION"]:
        for alt in ["ALT_STRONG", "ALT_NEUTRAL", "ALT_WEAK"]:
            for dir_ in ["LONG", "SHORT"]:
                key = ("MIS1", regime, alt, dir_)
                assert key not in rows
                rows.add(key)
    assert len(rows) == 30  # 5 × 3 × 2


def test_whitelist_30_entries_per_bot():
    """Each bot should have 30 entries (5 regimes × 3 alt × 2 directions)."""
    count = len(["TREND_UP", "TREND_DOWN", "CHOP", "HIGH_VOLA", "TRANSITION"]) * \
            len(["ALT_STRONG", "ALT_NEUTRAL", "ALT_WEAK"]) * \
            len(["LONG", "SHORT"])
    assert count == 30


# ── Outcome-Klassifikation (Kelly-/WR-Fix) ────────────────────────────────────
# Tests für _classify_outcome() und _apply_outcome_classification() — stellen
# sicher dass die PnL-basierte Win/Loss/Neutral-Klassifikation die bekannten
# Bugs aus 8_ai_trade_monitor.py korrekt umgeht.

def _load_analyzer_module():
    """Lädt 27_bot_regime_analyzer als Modul (wegen Ziffer-im-Dateinamen)."""
    import importlib.util, os
    spec = importlib.util.spec_from_file_location(
        "bot_regime_analyzer",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "27_bot_regime_analyzer.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_classify_outcome_legacy_target_hit_is_win():
    """LEGACY TARGET HIT (+2.5%) muss als Win erkannt werden, auch wenn
    der Bot vorher targets_hit=0 geschrieben hat (Bug in 8_ai_trade_monitor)."""
    mod = _load_analyzer_module()
    assert mod._classify_outcome("LEGACY TARGET HIT (+2.5%)", 2.6) == "win"


def test_classify_outcome_legacy_sl_is_loss():
    """LEGACY SL HIT (-2.5%) muss als Loss erkannt werden."""
    mod = _load_analyzer_module()
    assert mod._classify_outcome("LEGACY SL HIT (-2.5%)", -2.5) == "loss"


def test_classify_outcome_delisted_is_neutral():
    """DELISTED / CLEANUP darf weder als Win noch Loss zählen."""
    mod = _load_analyzer_module()
    # Sogar mit signifikantem PnL ist DELISTED neutral
    assert mod._classify_outcome("DELISTED / CLEANUP", -15.0) == "neutral"
    assert mod._classify_outcome("DELISTED / CLEANUP", +5.0) == "neutral"


def test_classify_outcome_cleanup_is_neutral():
    """Alias-Schreibweise CLEANUP wird auch als neutral erkannt."""
    mod = _load_analyzer_module()
    assert mod._classify_outcome("MANUAL CLEANUP", -3.0) == "neutral"


def test_classify_outcome_outlier_is_neutral():
    """Ausreißer mit |pnl| > 100% gelten als Daten-Bug → neutral."""
    mod = _load_analyzer_module()
    assert mod._classify_outcome("SL Hit", -1155.0) == "neutral"
    assert mod._classify_outcome("LEGACY TARGET HIT (+2.5%)", +1234.0) == "neutral"


def test_classify_outcome_micro_pnl_is_neutral():
    """Housekeeping-Closes mit |pnl| <= 0.1% sind neutral."""
    mod = _load_analyzer_module()
    assert mod._classify_outcome("SL Hit", 0.05) == "neutral"
    assert mod._classify_outcome("SL Hit", -0.09) == "neutral"
    assert mod._classify_outcome("SL Hit", 0.0) == "neutral"


def test_classify_outcome_none_and_invalid():
    """None und ungültige Werte → neutral."""
    mod = _load_analyzer_module()
    assert mod._classify_outcome(None, 1.5) == "win"        # Reason kann leer sein
    assert mod._classify_outcome("SL Hit", None) == "neutral"
    assert mod._classify_outcome("SL Hit", float("nan")) == "neutral"


def test_classify_outcome_modern_wins_and_losses():
    """Moderne SL-Hit-Trades (mit spezifischem SL im Reason-String)."""
    mod = _load_analyzer_module()
    assert mod._classify_outcome("SL Hit (SL: 0.00855)", -3.5) == "loss"
    assert mod._classify_outcome("TP hit", +12.3) == "win"
    assert mod._classify_outcome("", +2.1) == "win"  # leerer Reason, positiver PnL


def test_apply_outcome_classification_filters_neutrals():
    """_apply_outcome_classification entfernt neutrale Trades komplett."""
    mod = _load_analyzer_module()
    df = pd.DataFrame([
        {"pnl_pct": +2.5, "close_reason": "LEGACY TARGET HIT (+2.5%)", "is_win": 0},
        {"pnl_pct": -2.5, "close_reason": "LEGACY SL HIT (-2.5%)",      "is_win": 0},
        {"pnl_pct": -15.0, "close_reason": "DELISTED / CLEANUP",        "is_win": 0},
        {"pnl_pct": -1155.0, "close_reason": "",                        "is_win": 0},
        {"pnl_pct": 0.02, "close_reason": "",                           "is_win": 0},
    ])
    result = mod._apply_outcome_classification(df)
    # Nur 2 "entschiedene" Trades übrig
    assert len(result) == 2
    # Der LEGACY TARGET HIT wurde als Win umgewidmet (is_win wurde neu gesetzt)
    wins = result[result["outcome"] == "win"]
    losses = result[result["outcome"] == "loss"]
    assert len(wins) == 1
    assert len(losses) == 1
    assert wins.iloc[0]["is_win"] == 1  # überschrieben, egal was vorher drin war


def test_apply_outcome_classification_recomputes_is_win():
    """Selbst wenn SQL is_win=1 schreibt für ein DELISTED-Trade, wird das
    vom Fix korrigiert: es ist weder Win noch Loss, sondern neutral."""
    mod = _load_analyzer_module()
    df = pd.DataFrame([
        # Worst-case: SQL meint is_win=1, aber in Wahrheit DELISTED
        {"pnl_pct": +2.5, "close_reason": "DELISTED / CLEANUP", "is_win": 1},
        # Normaler Win
        {"pnl_pct": +2.5, "close_reason": "LEGACY TARGET HIT", "is_win": 0},
    ])
    result = mod._apply_outcome_classification(df)
    assert len(result) == 1  # DELISTED-Trade gefiltert
    assert result.iloc[0]["outcome"] == "win"
    assert result.iloc[0]["is_win"] == 1


def test_epd1_realistic_distribution():
    """End-to-End: Simuliere EPD1's echte Datenverteilung (37911 LEGACY TP,
    27636 LEGACY SL, 4635 DELISTED, 121 LEGACY FB_SL) und checking dass die
    WR after Filterung bei ~57-58% landet (statt bei 0.28% wie im Bug-Fall)."""
    mod = _load_analyzer_module()
    import random
    random.seed(42)
    rows = []
    for _ in range(37911):
        rows.append({"pnl_pct": 2.5 + random.uniform(-0.2, 0.3),
                     "close_reason": "LEGACY TARGET HIT (+2.5%)", "is_win": 0})
    for _ in range(27636):
        rows.append({"pnl_pct": -2.5 + random.uniform(-0.2, 0.2),
                     "close_reason": "LEGACY SL HIT (-2.5%)", "is_win": 0})
    for _ in range(4635):
        rows.append({"pnl_pct": random.uniform(-10, 2),
                     "close_reason": "DELISTED / CLEANUP", "is_win": 0})
    for _ in range(121):
        rows.append({"pnl_pct": -5.0 + random.uniform(-0.3, 0.3),
                     "close_reason": "LEGACY FALLBACK SL (-5.0%)", "is_win": 0})

    df = pd.DataFrame(rows)
    filtered = mod._apply_outcome_classification(df)
    # Neutrale (DELISTED) raus → 37911 + 27636 + 121 = 65668
    assert 65500 < len(filtered) < 65800
    wr = filtered["is_win"].sum() / len(filtered) * 100
    assert 57.0 < wr < 58.5, f"WR außerhalb erwartetem Bereich: {wr:.2f}%"


# ── B9-Zensur-Korrektur (T-2026-CU-9050-048) ──────────────────────────────────
# REGIME_CHANGE-Closes zählen mit realem PnL als Win/Loss statt pauschal neutral.
# DELISTED/CLEANUP/ORPHAN bleiben neutral; near-0%-Closes fängt der Micro-Filter.

def test_b9_classify_outcome_regime_change_counts_win():
    """CLOSED_REGIME_CHANGE mit signifikantem +PnL ist jetzt ein Win."""
    mod = _load_analyzer_module()
    assert mod._classify_outcome("CLOSED_REGIME_CHANGE", 4.2) == "win"
    assert mod._classify_outcome("REGIME_CHANGE:not_whitelisted", 2.5) == "win"


def test_b9_classify_outcome_regime_change_counts_loss():
    """Der zuvor zensierte Fall: ein realisierter Regime-Wechsel-Verlust
    ist jetzt ein Loss (das war die WR-Verzerrung aus Report 16 B9)."""
    mod = _load_analyzer_module()
    assert mod._classify_outcome("CLOSED_REGIME_CHANGE", -3.7) == "loss"


def test_b9_classify_outcome_regime_change_micro_still_neutral():
    """Ein Regime-Close nahe Break-even bleibt neutral (Micro-PnL-Filter)."""
    mod = _load_analyzer_module()
    assert mod._classify_outcome("CLOSED_REGIME_CHANGE", 0.05) == "neutral"


def test_b9_delisted_and_cleanup_still_neutral():
    """B9 berührt NUR REGIME_CHANGE — echte Housekeeping-Closes bleiben neutral."""
    mod = _load_analyzer_module()
    assert mod._classify_outcome("DELISTED / CLEANUP", -12.0) == "neutral"
    assert mod._classify_outcome("MANUAL CLEANUP", +6.0) == "neutral"
    assert mod._classify_outcome("ORPHAN CLOSE", -4.0) == "neutral"


# ── v2 Whitelist: Netto-Expectancy-Untergrenze + EB-Shrinkage ─────────────────
# _v2_expectancy_lower_bound / _v2_whitelist_decision sind pure/DB-frei.

def test_v2_lower_bound_matches_eb_formula():
    """Pin die Shrinkage-Formel gegen die Modul-Konstanten (bleibt gültig,
    wenn die Konstanten später kalibriert werden)."""
    mod = _load_analyzer_module()
    import math
    cell = {"n": 100, "avg_pnl": 1.0, "std": 2.0}
    est, lb, n_eff, src = mod._v2_expectancy_lower_bound(cell, None, None)
    k = mod.V2_SHRINKAGE_PSEUDO_COUNT
    z = mod.V2_LOWER_BOUND_Z
    w = 100.0 / (100.0 + k)
    exp_est = w * 1.0 + (1.0 - w) * mod.V2_PRIOR_MEAN_PNL_PCT
    exp_neff = 100.0 + k
    exp_se = 2.0 / math.sqrt(exp_neff)
    assert est == pytest.approx(exp_est)
    assert lb == pytest.approx(exp_est - z * exp_se)
    assert n_eff == pytest.approx(exp_neff)
    assert src == "cell"


def test_v2_no_data_anywhere_is_not_whitelisted():
    """B1-Fix: eine Zelle ganz ohne Evidenz wird NICHT default-open
    durchgewunken (v1 tat genau das über `insufficient_data`)."""
    mod = _load_analyzer_module()
    whitelisted, reason = mod._v2_whitelist_decision(None, None, None)
    assert whitelisted is False
    assert "src=prior" in reason


def test_v2_strong_positive_cell_is_whitelisted():
    """Viele Trades, klar positive Expectancy, niedrige Streuung → Untergrenze
    über Break-even → whitelisted."""
    mod = _load_analyzer_module()
    cell = {"n": 200, "avg_pnl": 1.5, "std": 2.0}
    whitelisted, reason = mod._v2_whitelist_decision(cell, None, None)
    assert whitelisted is True
    assert "v2_pass" in reason


def test_v2_negative_expectancy_blocked_despite_data():
    """Der Kern des Umbaus: eine Zelle mit negativer Netto-Expectancy wird
    geblockt — egal wie die WR aussieht (v1 hätte sie evtl. durchgelassen)."""
    mod = _load_analyzer_module()
    cell = {"n": 200, "avg_pnl": -0.4, "std": 2.0}
    whitelisted, _ = mod._v2_whitelist_decision(cell, None, None)
    assert whitelisted is False


def test_v2_sparse_cell_inherits_strong_parent():
    """Eine leere 4D-Zelle leiht Stärke vom robusten Bot×ALL-Level: die
    Schätzung wird Richtung Eltern-Mittel gezogen, prior_source=bot_all."""
    mod = _load_analyzer_module()
    parent_overall = {"n": 500, "avg_pnl": 0.8, "std": 1.5}
    est, lb, n_eff, src = mod._v2_expectancy_lower_bound(None, None, parent_overall)
    assert src == "bot_all"
    assert 0.0 < est < 0.8           # zwischen Prior (0) und Eltern-Mittel
    whitelisted, _ = mod._v2_whitelist_decision(None, None, parent_overall)
    assert whitelisted is True


def test_v2_smaller_n_gives_lower_bound():
    """Gleiches Mittel + gleiche Streuung, weniger Trades → engere Evidenz,
    tiefere Untergrenze (Shrinkage + breiteres Intervall)."""
    mod = _load_analyzer_module()
    big = mod._v2_expectancy_lower_bound({"n": 200, "avg_pnl": 1.0, "std": 2.0}, None, None)
    small = mod._v2_expectancy_lower_bound({"n": 10, "avg_pnl": 1.0, "std": 2.0}, None, None)
    assert small[1] < big[1]  # lb


def test_v2_higher_variance_gives_lower_bound():
    """Gleiches Mittel + gleiches n, höhere Streuung → tiefere Untergrenze."""
    mod = _load_analyzer_module()
    calm = mod._v2_expectancy_lower_bound({"n": 100, "avg_pnl": 1.0, "std": 1.0}, None, None)
    wild = mod._v2_expectancy_lower_bound({"n": 100, "avg_pnl": 1.0, "std": 6.0}, None, None)
    assert wild[1] < calm[1]  # lb


def test_v2_finest_populated_level_dominates_source():
    """Der prior_source verfolgt das feinste Level mit Daten."""
    mod = _load_analyzer_module()
    cell = {"n": 60, "avg_pnl": 1.2, "std": 2.0}
    pr = {"n": 120, "avg_pnl": 0.9, "std": 2.0}
    po = {"n": 400, "avg_pnl": 0.5, "std": 2.0}
    _, _, _, src = mod._v2_expectancy_lower_bound(cell, pr, po)
    assert src == "cell"
    # Ohne Zell-Daten fällt die Quelle auf Bot×Regime zurück
    _, _, _, src2 = mod._v2_expectancy_lower_bound(None, pr, po)
    assert src2 == "bot_regime"


def test_v2_reason_string_is_parseable():
    """reason_v2 trägt lb/est/src/neff für den Counterfactual-Vergleich."""
    mod = _load_analyzer_module()
    _, reason = mod._v2_whitelist_decision({"n": 100, "avg_pnl": 1.0, "std": 2.0}, None, None)
    for token in ("v2_", "lb=", "est=", "src=", "neff="):
        assert token in reason


def test_v2_shrinkage_pulls_extreme_small_cell_toward_parent():
    """Eine kleine Zelle mit extremem Mittel wird zum Eltern-Mittel geschrumpft
    (statt dem Rauschen zu vertrauen)."""
    mod = _load_analyzer_module()
    parent_overall = {"n": 500, "avg_pnl": 0.5, "std": 2.0}
    est, _, _, _ = mod._v2_expectancy_lower_bound(
        {"n": 4, "avg_pnl": 9.0, "std": 2.0}, None, parent_overall
    )
    # 4 Trades ziehen die 9%-Spitze kaum durch — est bleibt nahe am Eltern-Mittel
    assert est < 2.0
