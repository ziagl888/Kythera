# backtest/test_bot_naming.py
"""
Unit tests für core/bot_naming.pretty_name().
Run with: pytest backtest/test_bot_naming.py -v
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core.bot_naming import pretty_name


# ── Klassische Aliase ─────────────────────────────────────────────────────────

def test_fast_in_and_out_alias():
    assert pretty_name("Fast In And Out") == "FastInOut"


def test_support_resistance_alias():
    assert pretty_name("Support Resistance") == "SR"


def test_volume_indicator_alias():
    assert pretty_name("Volume Indicator") == "VolIndic"


def test_5_percent_alias():
    assert pretty_name("5 Percent") == "5Percent"


# ── MIS1 Konsolidierung ───────────────────────────────────────────────────────

def test_mis1_uppercase_h_lowered():
    """MIS1-8H → MIS1-8h (Case-Konsolidierung)"""
    assert pretty_name("MIS1-8H") == "MIS1-8h"
    assert pretty_name("MIS1-24H") == "MIS1-24h"
    assert pretty_name("MIS1-72H") == "MIS1-72h"
    assert pretty_name("MIS1-168H") == "MIS1-168h"


def test_mis1_pump_dump_variants_collapsed():
    """MIS1-Nh_pump/dump → MIS1-Nh (Pump/Dump-Konsolidierung)"""
    assert pretty_name("MIS1-8h_pump") == "MIS1-8h"
    assert pretty_name("MIS1-8h_dump") == "MIS1-8h"
    assert pretty_name("MIS1-8H_pump") == "MIS1-8h"
    assert pretty_name("MIS1-8H_dump") == "MIS1-8h"
    assert pretty_name("MIS1-168H_PUMP") == "MIS1-168h"
    assert pretty_name("MIS1-168H_DUMP") == "MIS1-168h"


def test_mis1_lowercase_already_idempotent():
    """Already normalised names remain unchanged"""
    assert pretty_name("MIS1-8h") == "MIS1-8h"
    assert pretty_name("MIS1-168h") == "MIS1-168h"


# ── MSI1 Typo-Fix ─────────────────────────────────────────────────────────────

def test_msi1_typo_fixed():
    """MSI1 → MIS1 (historischer Typo)"""
    assert pretty_name("MSI1-24h") == "MIS1-24h"
    assert pretty_name("MSI1-8H") == "MIS1-8h"
    assert pretty_name("MSI1") == "MIS1"


def test_msi1_with_pump_dump_combined():
    """MSI1-Nh_pump → MIS1-Nh (Typo + Konsolidierung)"""
    assert pretty_name("MSI1-8H_pump") == "MIS1-8h"
    assert pretty_name("MSI1-168h_dump") == "MIS1-168h"


# ── Idempotenz ────────────────────────────────────────────────────────────────

def test_idempotent_on_already_normalized():
    """pretty_name(pretty_name(x)) == pretty_name(x) für alle x"""
    samples = [
        "FastInOut", "SR", "VolIndic", "5Percent",
        "MIS1-8h", "MIS1-168h", "MIS1",
        "ATS1", "EPD1", "QM_1H", "BR2H", "TD_4H",
    ]
    for s in samples:
        assert pretty_name(pretty_name(s)) == pretty_name(s), f"Not idempotent: {s}"


# ── Unveränderte Namen ────────────────────────────────────────────────────────

def test_unchanged_names():
    """Namen die keiner der Regeln entsprechen bleiben unverändert."""
    unchanged = [
        "ATS1", "ATS1_Robust", "EPD1", "AIM1", "ABR1",
        "RUB1", "ATB1", "SRA1", "ROM1",
        "QM_1H", "QM_4H", "BB_1H", "BB_4H", "TD_1H", "TD_4H",
        "BR1H", "BR2H", "BR4H", "BR1D",
        "Pattern Detector",    # original bot name, not an alias
        "Main Channel",        # classic entry, not an alias
        "SMC_15M", "SMC_30M", "SMC_4H",
    ]
    for s in unchanged:
        assert pretty_name(s) == s, f"Unexpected change: {s} → {pretty_name(s)}"


# ── Edge Cases ────────────────────────────────────────────────────────────────

def test_empty_string():
    assert pretty_name("") == ""


def test_none_input():
    assert pretty_name(None) == ""


def test_whitespace_stripped():
    assert pretty_name("  ATS1  ") == "ATS1"
    assert pretty_name("  MIS1-8H  ") == "MIS1-8h"


def test_generation_preserved_across_normalisation():
    """Die Case-Konsolidierung normalisiert generationsübergreifend, ohne
    Generationen zu vermischen (harte Regel 6: Retrains posten unter neuem Tag).

    Der Horizon-Suffix wird auch bei MIS2 auf lowercase gezogen, aber MIS2
    bleibt MIS2 — nur der historische Typo MSI1 wird auf MIS1 gemappt.
    """
    assert pretty_name("MIS2-8H") == "MIS2-8h"
    assert pretty_name("MIS2-8H_pump") == "MIS2-8h"
    # Generationen bleiben getrennt
    assert pretty_name("MIS2-8H") != pretty_name("MIS1-8H")


def test_similar_but_not_matching():
    """Names resembling but not matching the pattern remain unchanged."""
    # MIS1 ohne Horizon bleibt
    assert pretty_name("MIS1") == "MIS1"
    # MIS1 mit komischen Suffix matcht nicht den Pattern
    assert pretty_name("MIS1-xyz") == "MIS1-xyz"
    # Ziffernloses Präfix matcht das MIS\d+-Pattern nicht
    assert pretty_name("MIS-8H") == "MIS-8H"


# ── Regression ────────────────────────────────────────────────────────────────

def test_regression_analyzer_market_tracker_agree():
    """Kernzweck des Fixes: Analyzer und Market-Tracker müssen für
    denselben Input-Namen denselben normalisierten Namen produzieren.

    Das ist genau garantiert weil beide dieselbe Funktion nutzen, aber
    wir testen es explizit weil das der ganze Grund für core/bot_naming
    war.
    """
    # Historische Analyzer-Schreibweisen vs. Market-Tracker-Erwartungen
    # (vor dem Fix schrieb Analyzer "Fast In And Out", Tracker fragte mit "FastInOut")
    analyzer_wrote = [
        "Fast In And Out", "Support Resistance", "Volume Indicator",
        "5 Percent", "MIS1-8H", "MIS1-168H_pump",
    ]
    tracker_queries = [
        "FastInOut", "SR", "VolIndic",
        "5Percent", "MIS1-8h", "MIS1-168h",
    ]
    for raw, expected in zip(analyzer_wrote, tracker_queries):
        assert pretty_name(raw) == expected, (
            f"Analyzer-schreibt {raw!r} und Tracker-fragt {expected!r} "
            f"sollten beide auf {expected!r} normalisieren, "
            f"bekomme aber {pretty_name(raw)!r}"
        )
