# backtest/test_bot_catalog.py
"""
Unit tests for core/bot_catalog.py (T-2026-CU-9050-115) — the model-tag /
strategy-name → fleet-script mapping and the active-bot filter used by the
realized-PnL report.

Key risk under test: family PREFIX matching must survive artifact-driven tag
rotation (ABR1→ABR2, MIS1→MIS2, …; OPUS-HANDOFF Falle 16) and must not
confuse overlapping families (ABR… is bot 18, BR… is bot 7).

Run with: pytest backtest/test_bot_catalog.py -v
"""

from __future__ import annotations

import os
import sys
import unittest.mock as mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.bot_catalog as bc  # noqa: E402
from core.fleet import FLEET  # noqa: E402

# ── AI family mapping ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tag", "script"),
    [
        # every family, incl. next-generation tags that do not exist yet
        ("ABR1", "18_ai_abr1_bot.py"),
        ("ABR2", "18_ai_abr1_bot.py"),
        ("AIM2-TOPN", "15_ai_master_bot.py"),
        ("ATB2", "14_ai_atb_bot.py"),
        ("ATS1_Robust", "12_ai_ats_bot.py"),
        ("BB_4H", "25_smc_ml_sniper.py"),
        ("BB2_1H", "25_smc_ml_sniper.py"),  # sniper retrain generation
        ("BR15M", "7_pattern_detector.py"),
        ("BR1Hv2", "7_pattern_detector.py"),
        ("EPD2", "10_pump_dump_detector.py"),
        ("FIF1", "33_ai_fif1_bot.py"),
        ("FMR1", "31_ai_fmr1_bot.py"),
        ("MAX1", "34_ai_max1_bot.py"),
        ("MIS1-8h", "11_ai_mis_bot.py"),
        ("MIS2-72H", "11_ai_mis_bot.py"),
        ("PEX1", "30_ai_pex1_bot.py"),
        ("QM_4H", "24_quasimodo_bot.py"),
        ("ROM1", "28_signal_orchestrator.py"),
        ("RUB2", "13_ai_rub_bot.py"),
        ("SRA1", "9_ai_sr_bot.py"),
        ("TD_1H", "25_smc_ml_sniper.py"),
        ("TD2_4H", "25_smc_ml_sniper.py"),  # sniper retrain generation
        ("TRM1", "32_ai_trm1_bot.py"),
        ("UFI1", "29_ufi1_bot.py"),
    ],
)
def test_ai_family_mapping(tag, script):
    assert bc.script_for_tag(tag) == script


def test_abr_wins_over_br():
    # Longest-prefix rule: ABR2 must NOT fall into bot 7's BR… family.
    assert bc.script_for_tag("ABR2") != "7_pattern_detector.py"


@pytest.mark.parametrize(
    ("tag", "family"),
    [
        ("RUB2", "RUB"),
        ("MIS1-8h", "MIS"),
        ("MIS2-72H", "MIS"),
        ("ABR2", "ABR"),  # longest-prefix wins, not BR
        ("BB_4H", "BB"),
        ("ATS1_Robust", "ATS"),
        ("Main Channel", None),  # classic strategy → no family prefix
        ("TOTALLY_NEW_MODEL_9000", None),
        ("", None),
        (None, None),
    ],
)
def test_family_for_tag(tag, family):
    # Reverse companion of script_for_tag for the bot-variant index
    # (T-2026-KYT-9050-038): tag → stable family prefix, same normalisation.
    assert bc.family_for_tag(tag) == family


def test_mis_pump_dump_and_typo_variants_normalise_first():
    # pretty_name folds MIS1-8h_pump/_dump and the MSI typo before matching.
    assert bc.script_for_tag("MIS1-8H_pump") == "11_ai_mis_bot.py"
    assert bc.script_for_tag("MSI1-24h") == "11_ai_mis_bot.py"


def test_mapped_scripts_exist_in_fleet():
    fleet_scripts = {e["script"] for e in FLEET}
    mapped = {script for _prefix, script in bc._AI_FAMILY_TO_SCRIPT}
    mapped |= set(bc._CLASSIC_TO_SCRIPT.values())
    assert mapped <= fleet_scripts, f"catalog points at non-fleet scripts: {mapped - fleet_scripts}"


# ── Classic strategy names ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "Fast In And Out",  # raw DB form
        "FastInOut",  # pretty form
        "Support Resistance",
        "SR",
        "Volume Indicator",
        "VolIndic",
        "5 Percent",
        "5Percent",
        "Main Channel",
    ],
)
def test_classic_names_map_to_detectors(name):
    assert bc.script_for_tag(name) == "3_detectors.py"


def test_unknown_tag_returns_none():
    assert bc.script_for_tag("TOTALLY_NEW_MODEL_9000") is None
    assert bc.script_for_tag("") is None
    assert bc.script_for_tag(None) is None


# ── Leverage convention ───────────────────────────────────────────────────────


def test_ufi_has_non_standard_leverage():
    # UFI1 caps leverage against the SL distance (P0.6/R4) — the close-time
    # 20x default would be wrong, so the monitor stores NULL for it.
    assert bc.has_standard_leverage("UFI1") is False
    assert bc.has_standard_leverage("RUB2") is True
    assert bc.has_standard_leverage(None) is True  # unknown → default path


# ── Active filter ─────────────────────────────────────────────────────────────


def test_active_scripts_excludes_parked():
    with mock.patch.object(bc, "list_parked", return_value={"13_ai_rub_bot.py"}):
        active = bc.active_scripts()
    assert "13_ai_rub_bot.py" not in active
    assert "11_ai_mis_bot.py" in active
    assert len(active) == len(FLEET) - 1


def test_is_bot_active_respects_parking_and_mapping():
    active = {e["script"] for e in FLEET} - {"13_ai_rub_bot.py"}
    assert bc.is_bot_active("RUB2", active) is False  # parked
    assert bc.is_bot_active("MIS1-8h", active) is True
    assert bc.is_bot_active("TOTALLY_NEW_MODEL_9000", active) is False  # unmapped
