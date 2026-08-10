# core/bot_catalog.py — mapping from DB model tags / strategy names to the
# fleet script that emits them, plus the "active bot" filter.
#
# Why this exists (T-2026-CU-9050-115): reports aggregate per model tag
# (closed_ai_signals.model) or per classic strategy name
# (closed_trades_master.strategy), but "is this bot active?" is a property of
# the SCRIPT (core/fleet.py FLEET minus control/parked markers). No central
# tag→script mapping existed before this module.
#
# A script is not always a process (T-2026-KYT-9050-133): LIS/TSM/SKW/XSM/XSR
# still map onto bots 36-39, but those four run inside 45_shadow_scanner_runner.py
# instead of owning a fleet entry. The mapping deliberately keeps pointing at the
# bot, because that is the unit reports and park markers speak about; only
# active_scripts()/families_for_script() know about the hosting, see there.
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
from core.shadow_scanners import HOSTED_SCRIPTS, RUNNER_SCRIPT

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
    # FIF2 before FIF: longest prefix wins, and the successor bot (43, T-112)
    # must not resolve onto FIF1's script.
    ("FIF2", "43_ai_fif2_bot.py"),
    ("FIF", "33_ai_fif1_bot.py"),
    ("FMR", "31_ai_fmr1_bot.py"),
    ("LIS", "36_ai_lis1_bot.py"),  # K5 Post-Listing-Drift-Fade (Shadow-only, T-149)
    ("MAX", "34_ai_max1_bot.py"),
    ("MIS", "11_ai_mis_bot.py"),
    ("MSI", "11_ai_mis_bot.py"),  # historical typo family, see core/bot_naming
    ("ODS", "42_ai_ods1_bot.py"),  # OI-divergence short (T-2026-KYT-9050-106)
    ("PEX", "30_ai_pex1_bot.py"),
    ("QM", "24_quasimodo_bot.py"),
    # ROM1: both close writers persist targets/lev — bot 8 (SL/TP path,
    # T-115) and the regime auto-close in 28_signal_orchestrator (T-116).
    ("ROM", "28_signal_orchestrator.py"),
    ("RUB", "13_ai_rub_bot.py"),
    ("SKW", "38_ai_skw1_bot.py"),  # K7 Cross-Sectional Skewness (Shadow-only, T-149)
    ("SRA", "9_ai_sr_bot.py"),
    ("TD", "25_smc_ml_sniper.py"),  # TD_4H and retrain generations (TD2_4H)
    ("TRM", "32_ai_trm1_bot.py"),
    ("TSM", "37_ai_tsm1_bot.py"),  # K1 Time-Series-Momentum (Shadow-only, T-149)
    ("UFI", "29_ufi1_bot.py"),
    ("XSM", "39_ai_xsm1_bot.py"),  # K2 Cross-Sectional Momentum (Shadow-only, T-149)
    ("XSR", "39_ai_xsm1_bot.py"),  # K2 Cross-Sectional Reversal (same bot 39)
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


def family_for_tag(tag: str | None) -> str | None:
    """Family prefix a model tag belongs to (reverse of the prefix table).

    Companion to script_for_tag() for the bot-variant index
    (T-2026-KYT-9050-038): a generation tag like ``RUB2`` / ``MIS1-8h`` /
    ``BB_4H`` collapses onto its stable family prefix (``RUB`` / ``MIS`` /
    ``BB``), the grouping key under which every generation of a bot lives.
    Matching mirrors script_for_tag exactly — same loop, same precedence:
    pretty_name normalisation + first-match in _AI_FAMILY_TO_SCRIPT table order,
    which is ordered so the more specific ``ABR`` precedes ``BR`` ⇒ ``ABR2``
    resolves to ``ABR`` (bot 18), not ``BR`` (bot 7). Classic strategy names have
    no family prefix and return None (the
    caller treats the pretty name itself as the group). Unknown tags → None.
    """
    if not tag:
        return None
    name = pretty_name(tag)
    if name in _CLASSIC_TO_SCRIPT:
        return None
    upper = name.upper()
    for prefix, _script in _AI_FAMILY_TO_SCRIPT:
        if upper.startswith(prefix):
            return prefix
    return None


def families_for_script(script: str) -> list[str]:
    """Model-tag families / classic strategy names emitted by `script`.

    Reverse of script_for_tag() (T-2026-CU-9050-152 fleet-registry panel):
    given a fleet script, which tag(s) does it post under? Most scripts map
    to exactly one AI family; 25_smc_ml_sniper.py posts both BB and TD, and
    3_detectors.py posts all five classic strategy names. Declaration order
    is preserved for the AI side (matches _AI_FAMILY_TO_SCRIPT precedence);
    classic names are sorted since there is no precedence among them.
    """
    if script == RUNNER_SCRIPT:
        # The runner posts nothing itself; it drives the four hosted bots, so its
        # families are theirs (T-2026-KYT-9050-133). Without this the only fleet
        # entry that emits LIS/TSM/SKW/XSM/XSR would report no families at all.
        return [family for hosted in HOSTED_SCRIPTS for family in families_for_script(hosted)]
    families = [prefix for prefix, s in _AI_FAMILY_TO_SCRIPT if s == script]
    classics = sorted(name for name, s in _CLASSIC_TO_SCRIPT.items() if s == script)
    return families + classics


def has_standard_leverage(tag: str | None) -> bool:
    """True when the bot posts get_max_leverage(symbol, 20) verbatim."""
    upper = (tag or "").strip().upper()
    return not any(upper.startswith(p) for p in _NON_STANDARD_LEVERAGE_PREFIXES)


def active_scripts() -> set[str]:
    """Scripts that are part of the fleet AND not parked by the operator.

    The four scanners hosted by 45_shadow_scanner_runner.py (T-2026-KYT-9050-133)
    have no fleet entry of their own anymore, but they still scan — so they
    count as active exactly when the runner runs and their OWN park marker is
    absent. That mirrors what the runner does before each scan, and it keeps the
    report semantics of parking a single bot: without this expansion every
    TSM1/SKW1/XSM1/XSR1 leg would silently fall into the "inactive" bucket of
    23_market_tracker's realised report the moment the fleet switches over.
    """
    parked = list_parked()
    active = {entry["script"] for entry in FLEET if entry["script"] not in parked}
    if RUNNER_SCRIPT in active:
        active |= {script for script in HOSTED_SCRIPTS if script not in parked}
    return active


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
