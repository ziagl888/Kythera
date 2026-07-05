# backtest/test_abr1_detection.py
"""
Unit tests für die Break&Retest-Erkennung in 18_ai_abr1_bot
(find_pivot_levels + find_break_retest_setups, Detektor-Rework 2026-07).

Deckt genau die Fehlerklassen der alten Inline-Logik ab:
  1. Richtungs-Kopplung: High-Touch von unten an gebrochenen Widerstand
     (= gescheiterter Ausbruch) darf KEIN LONG mehr sein.
  2. Hold-Check: Close unter dem Level zwischen Break und Retest invalidiert.
  3. Erst-Touch: nur der erste Retest nach dem Break zählt.
  4. Bestätigte Pivots: Edge-Pivots ohne PIVOT_WINDOW Kerzen Bestätigung
     existieren nicht mehr (Repainting, R07-ABR1-b).

Run with: pytest backtest/test_abr1_detection.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


def _import_abr1():
    path = os.path.join(REPO_ROOT, "18_ai_abr1_bot.py")
    spec = importlib.util.spec_from_file_location("abr1_bot_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["abr1_bot_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


abr1 = _import_abr1()

LEVEL = 100.0  # Widerstands-Level der LONG-Szenarien (Band bei ±0.5%: 99.5–100.5)


def make_df(rows):
    """rows: Liste von (open, high, low, close) — Volume/open_time synthetisch."""
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"]).astype(float)
    df["volume"] = 1000.0
    df["open_time"] = pd.date_range("2026-01-01", periods=len(df), freq="h", tz="UTC")
    return df


def build_long_series(hold_candle=None, retest=None):
    """Widerstand 100 (Pivot t=10), Aufwärtsbreak t=26, Hold t=27–36, Retest t=37.

    hold_candle: optional {idx: (o,h,l,c)} um einzelne Hold-Kerzen zu ersetzen.
    retest: optionale (o,h,l,c) für die Retest-Kerze t=37.
    """
    rows = []
    for t in range(10):  # Baseline unter dem Level, leicht steigend (keine Neben-Pivots)
        base = 90.0 + 0.05 * t
        rows.append((base, base + 0.5, base - 2.0, base))
    rows.append((95.0, LEVEL, 93.0, 95.0))  # t=10: Pivot-High = Level
    for t in range(11, 26):  # unter dem Level bleiben
        base = 94.0 + 0.05 * (t - 11)
        rows.append((base, base + 1.0, base - 1.0, base))
    rows.append((95.0, 102.0, 94.5, 101.5))  # t=26: Break (prev close < 100 < close)
    for k in range(10):  # t=27..36: Hold über dem Level, kein Band-Touch (lows > 100.5)
        rows.append((101.0 + 0.1 * k, 101.8 + 0.1 * k, 100.8 + 0.1 * k, 101.3 + 0.1 * k))
    rows.append(retest or (101.4, 101.6, 100.2, 101.2))  # t=37: Erst-Retest von oben

    if hold_candle:
        for idx, candle in hold_candle.items():
            rows[idx] = candle
    return make_df(rows)


def build_short_series():
    """Support 90 (Pivot t=10), Abwärtsbreak t=26, Hold t=27–36, Retest t=37 von unten."""
    rows = []
    for t in range(10):  # Baseline über dem Level, leicht fallend
        base = 97.0 - 0.05 * t
        rows.append((base + 1.0, base + 2.0, base, base + 1.0))
    rows.append((93.0, 94.0, 90.0, 92.5))  # t=10: Pivot-Low = Level 90
    for t in range(11, 26):
        base = 91.2 + 0.03 * (t - 11)
        rows.append((92.0, 93.5, base, 92.2))
    rows.append((92.0, 92.5, 88.0, 88.5))  # t=26: Break down (prev close > 90 > close)
    for k in range(10):  # t=27..36: Hold unter dem Level, highs < 89.55 (kein Band-Touch)
        rows.append((88.5, 89.3 - 0.02 * k, 87.8 - 0.02 * k, 88.4 - 0.02 * k))
    rows.append((89.5, 90.2, 88.9, 89.2))  # t=37: Retest von unten, Close < 90
    return make_df(rows)


# ── Pivot-Bestätigung ─────────────────────────────────────────────────────────

def test_confirmed_resistance_pivot_found():
    df = build_long_series()
    levels = abr1.find_pivot_levels(df)
    res = [l for l in levels if l["type"] == "resistance" and l["price"] == LEVEL]
    assert len(res) == 1
    assert res[0]["index"] == 10


def test_unconfirmed_edge_pivot_ignored():
    """Spike in den letzten PIVOT_WINDOW Kerzen darf KEIN Level mehr sein (Repainting)."""
    rows = [(90 + 0.05 * t, 90.5 + 0.05 * t, 88 + 0.05 * t, 90 + 0.05 * t) for t in range(30)]
    rows[27] = (95.0, 100.0, 93.0, 95.0)  # Spike 3 Kerzen vor Schluss — unbestätigt
    df = make_df(rows)
    assert abr1.find_pivot_levels(df) == []


# ── Gültige Setups ────────────────────────────────────────────────────────────

def test_valid_long_break_retest_detected():
    df = build_long_series()
    levels = abr1.find_pivot_levels(df)
    setups = abr1.find_break_retest_setups(df, len(df) - 1, levels)
    assert len(setups) == 1
    s = setups[0]
    assert s["direction"] == "LONG"
    assert s["level_price"] == LEVEL
    assert s["break_idx"] == 26
    f = s["features"]
    assert f["setup_candles_since_break"] == 11.0
    assert f["setup_level_age_candles"] == 27.0
    assert f["setup_break_strength_pct"] == pytest.approx(1.5)
    assert f["setup_dist_close_level_pct"] == pytest.approx(1.2)
    assert f["setup_retest_wick_pct"] == pytest.approx((101.2 - 100.2) / 101.2 * 100)


def test_valid_short_break_retest_detected():
    df = build_short_series()
    levels = abr1.find_pivot_levels(df)
    setups = abr1.find_break_retest_setups(df, len(df) - 1, levels)
    assert len(setups) == 1
    s = setups[0]
    assert s["direction"] == "SHORT"
    assert s["level_price"] == 90.0
    assert s["break_idx"] == 26


# ── Fehlerklasse 1: Richtungs-Kopplung ───────────────────────────────────────

def test_failed_breakout_high_touch_is_not_long():
    """Preis fällt nach dem Break zurück unter das Level und rallyt von UNTEN
    an das Band (High-Touch). Die alte OR-Logik machte daraus ein LONG —
    das ist die Trainings-LOSS-Klasse (failed_breakout) und muss leer sein."""
    hold = {i: (98.0, 99.0, 97.5, 98.0 + 0.02 * (i - 27)) for i in range(27, 37)}
    hold[27] = (101.0, 101.5, 97.5, 98.0)  # Rückfall unter das Level
    df = build_long_series(hold_candle=hold, retest=(98.5, 100.2, 98.0, 98.5))
    levels = abr1.find_pivot_levels(df)
    assert abr1.find_break_retest_setups(df, len(df) - 1, levels) == []


def test_retest_close_back_below_level_rejected():
    """Low im Band, aber Close zurück unter dem Level → kein Hold, kein LONG."""
    df = build_long_series(retest=(101.0, 101.3, 100.2, 99.8))
    levels = abr1.find_pivot_levels(df)
    assert abr1.find_break_retest_setups(df, len(df) - 1, levels) == []


# ── Fehlerklasse 2: Hold-Check ────────────────────────────────────────────────

def test_close_below_level_just_before_retest_rejected():
    """Die Kerze direkt vor dem Retest schließt unter dem Level → kein
    gültiger Break mehr zwischen Level-Verlust und Retest → Setup invalidiert."""
    df = build_long_series(hold_candle={36: (101.0, 101.5, 99.0, 99.7)})
    levels = abr1.find_pivot_levels(df)
    assert abr1.find_break_retest_setups(df, len(df) - 1, levels) == []


def test_dip_and_rebreak_anchors_to_fresh_break():
    """Dip unter das Level mitten im Hold + erneuter Ausbruch danach: das ist
    ein FRISCHER Break (Trainer-Semantik — jeder Cross ist ein Break-Event).
    Das Setup muss am Re-Break t=33 ankern, nicht am Original-Break t=26."""
    df = build_long_series(hold_candle={32: (101.0, 101.5, 99.0, 99.7)})
    levels = abr1.find_pivot_levels(df)
    setups = abr1.find_break_retest_setups(df, len(df) - 1, levels)
    assert len(setups) == 1
    assert setups[0]["direction"] == "LONG"
    assert setups[0]["break_idx"] == 33
    assert setups[0]["features"]["setup_candles_since_break"] == 4.0


# ── Fehlerklasse 3: Erst-Touch ────────────────────────────────────────────────

def test_second_touch_rejected_first_touch_detected():
    """t=32 berührt das Band bereits (Low 100.3) — der Retest bei t=37 ist
    dann der ZWEITE Touch und zählt nicht; t=32 selbst ist der gültige."""
    df = build_long_series(hold_candle={32: (101.5, 101.8, 100.3, 101.5)})
    levels = abr1.find_pivot_levels(df)
    assert abr1.find_break_retest_setups(df, len(df) - 1, levels) == []
    first_touch = abr1.find_break_retest_setups(df, 32, levels)
    assert len(first_touch) == 1
    assert first_touch[0]["direction"] == "LONG"
    assert first_touch[0]["break_idx"] == 26


# Hinweis: Ein Test "Break im Pivot-Bestätigungsfenster wird abgelehnt" ist
# geometrisch unmöglich zu konstruieren — eine Kerze, die über das Pivot-Hoch
# schließt, verhindert per greater_equal die Pivot-Bestätigung selbst. Der
# earliest-break-Guard in find_break_retest_setups ist ein redundantes
# Sicherheitsnetz (Trainer-Semantik), kein eigenständig testbarer Pfad.
