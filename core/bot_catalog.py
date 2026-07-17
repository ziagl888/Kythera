# core/bot_catalog.py — mapping from DB model tags / strategy names to the
# fleet script that emits them, plus the "active bot" filter.
#
# Why this exists (T-2026-CU-9050-115): reports aggregate per model tag
# (closed_ai_signals.model) or per classic strategy name
# (closed_trades_master.strategy), but "is this bot active?" is a property of
# the SCRIPT (core/fleet.py FLEET minus control/parked markers). No central
# tag→script mapping existed before this module.
#
# Matching is FAMILY-PREFIX based, not exact-tag based: model tags come from
# artifact meta.model_id (OPUS-HANDOFF Falle 16) and rotate on every retrain
# (ABR1→ABR2, RUB2→RUB3, …). A prefix keeps the mapping stable across
# generations. Unknown tags return None — callers must surface the count
# (no silent drops) instead of guessing.

from __future__ import annotations

from core.bot_naming import pretty_name
from core.fleet import FLEET
from core.process_control import list_parked

# Family prefix → emitting script. Order matters: longest prefix wins, so
# "ABR…" must resolve to bot 18 before the "BR…" family of bot 7 can match.
# Tags are matched case-insensitively against upper-cased prefixes.
_AI_FAMILY_TO_SCRIPT: tuple[tuple[str, str], ...] = (
    ("ABR", "18_ai_abr1_bot.py"),
    ("AIM", "15_ai_master_bot.py"),
    ("ATB", "14_ai_atb_bot.py"),
    ("ATS", "12_ai_ats_bot.py"),
    # Sniper tags rotate as BB_4H → BB2_4H on retrain (model_id from the
    # artifact) — the prefix must survive that, hence "BB"/"TD" without the
    # underscore. Disjoint from "BR" (bot 7) and "TRM" (bot 32).
    ("BB", "25_smc_ml_sniper.py"),
    ("BR", "7_pattern_detector.py"),  # BR15M / BR1Hv2 / BR4H …
    ("EPD", "10_pump_dump_detector.py"),
    ("FIF", "33_ai_fif1_bot.py"),
    ("FMR", "31_ai_fmr1_bot.py"),
    ("LIS", "36_ai_lis1_bot.py"),  # K5 Post-Listing-Drift-Fade (Shadow-only, T-149)
    ("MAX", "34_ai_max1_bot.py"),
    ("MIS", "11_ai_mis_bot.py"),
    ("MSI", "11_ai_mis_bot.py"),  # historical typo family, see core/bot_naming
    ("PEX", "30_ai_pex1_bot.py"),
    ("QM", "24_quasimodo_bot.py"),
    # ROM1: both close writers persist targets/lev — bot 8 (SL/TP path,
    # T-115) and the regime auto-close in 28_signal_orchestrator (T-116).
    ("ROM", "28_signal_orchestrator.py"),
    ("RUB", "13_ai_rub_bot.py"),
    ("SRA", "9_ai_sr_bot.py"),
    ("TD", "25_smc_ml_sniper.py"),  # TD_4H and retrain generations (TD2_4H)
    ("TRM", "32_ai_trm1_bot.py"),
    ("UFI", "29_ufi1_bot.py"),
)

# Classic strategies all run inside 3_detectors.py (strategies/strat_*.py are
# imported there, not separate processes). Keys are the pretty_name() forms;
# raw DB names ("Fast In And Out") normalise onto these via pretty_name.
_CLASSIC_TO_SCRIPT: dict[str, str] = {
    "5Percent": "3_detectors.py",
    "FastInOut": "3_detectors.py",
    "Main Channel": "3_detectors.py",
    "SR": "3_detectors.py",
    "VolIndic": "3_detectors.py",
}

# Model families whose POSTED leverage is not get_max_leverage(symbol, 20):
# UFI1 caps leverage against the SL distance (P0.6/R4, typically 1-2x).
# 8_ai_trade_monitor must store NULL lev for these at close instead of the
# 20x default — a wrong persisted leverage is worse than an excluded row.
_NON_STANDARD_LEVERAGE_PREFIXES: tuple[str, ...] = ("UFI",)


def script_for_tag(tag: str | None) -> str | None:
    """Emitting fleet script for a model tag or classic strategy name.

    Accepts raw DB values; classic names are normalised via pretty_name.
    Returns None for unknown tags — callers decide how to surface that.
    """
    if not tag:
        return None
    name = pretty_name(tag)
    classic = _CLASSIC_TO_SCRIPT.get(name)
    if classic is not None:
        return classic
    upper = name.upper()
    for prefix, script in _AI_FAMILY_TO_SCRIPT:
        if upper.startswith(prefix):
            return script
    return None


def has_standard_leverage(tag: str | None) -> bool:
    """True when the bot posts get_max_leverage(symbol, 20) verbatim."""
    upper = (tag or "").strip().upper()
    return not any(upper.startswith(p) for p in _NON_STANDARD_LEVERAGE_PREFIXES)


def active_scripts() -> set[str]:
    """Scripts that are part of the fleet AND not parked by the operator."""
    parked = list_parked()
    return {entry["script"] for entry in FLEET if entry["script"] not in parked}


def is_bot_active(tag: str | None, active: set[str] | None = None) -> bool:
    """True when the tag maps to a fleet script that is currently active.

    ``active`` lets callers resolve active_scripts() once per report instead
    of hitting the filesystem per row.
    """
    script = script_for_tag(tag)
    if script is None:
        return False
    if active is None:
        active = active_scripts()
    return script in active
