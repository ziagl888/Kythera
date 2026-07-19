# backtest/test_rub4_funding_gate.py
"""DB-freie Tests für das RUB4-Funding-Gate (Bot 13, T-2026-CU-9050-164).

RUB4 = funding-gegatetes RUB-LONG-Shadow-Bein: DERSELBE RUB3-Kandidat, aber nur
wenn ``fund_24h > +3 bps`` (ABR1-LONG-Schwelle). Reines Shadow-Experiment (nie
live), eigener Tag → Report vergleicht gegatet (RUB4) vs. ungegatet (RUB3).

  1. funding_gate_open: strikt > 3.0 bps; None ⇒ zu.
  2. shadow_gate: RUB4-LONG ist SHADOW, ohne eigenes Artefakt (nutzt RUB3s Modell);
     RUB4-SHORT bleibt Default-LIVE (es gibt kein RUB4-SHORT-Bein).
  3. bot_catalog: Tag "RUB4" → 13_ai_rub_bot.py (RUB-Prefix).
  4. Die Gate-Schwelle == ABR1-LONG (3.0 bps).

Run: pytest backtest/test_rub4_funding_gate.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import core.bot_catalog as bc  # noqa: E402
from core import shadow_gate as sg  # noqa: E402


def _import_rub():
    path = os.path.join(REPO_ROOT, "13_ai_rub_bot.py")
    spec = importlib.util.spec_from_file_location("rub_bot_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rub_bot_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


rub = _import_rub()


# ── 1. funding_gate_open ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("fund_24h", "expected"),
    [
        (3.01, True),
        (5.0, True),
        (3.0, False),  # strikt >, nicht >=
        (2.99, False),
        (0.0, False),
        (-2.0, False),
        (None, False),  # keine Funding-Daten ⇒ Gate zu
    ],
)
def test_funding_gate_open(fund_24h, expected):
    assert rub.funding_gate_open(fund_24h) is expected


def test_gate_threshold_matches_abr1_long():
    assert rub.FUNDING_GATE_LONG_BPS == 3.0


# ── 2. shadow_gate registration ───────────────────────────────────────────────
def test_rub4_long_is_shadow_reusing_rub3_model():
    assert sg.leg_status("RUB4", "LONG") == sg.SHADOW
    assert sg.is_shadow("RUB4", "LONG")
    # RUB4 nutzt das RUB3-Artefakt (Bot lädt SHADOW_RUB3_LONG) → KEIN eigener
    # SHADOW_ARTIFACTS-Eintrag; der Loader würde None liefern.
    assert "RUB4" not in sg.SHADOW_ARTIFACTS
    assert sg.shadow_artifact_path("RUB4", "LONG") is None
    # kein RUB4-SHORT-Bein → Default-LIVE (nichts postet es)
    assert sg.leg_status("RUB4", "SHORT") == sg.LIVE
    # das ungegatete RUB3-LONG bleibt getrennt SHADOW
    assert sg.leg_status("RUB3", "LONG") == sg.SHADOW


# ── 3. bot_catalog ────────────────────────────────────────────────────────────
def test_rub4_tag_maps_to_bot13():
    assert bc.script_for_tag("RUB4") == "13_ai_rub_bot.py"
    assert bc.script_for_tag("RUB3") == "13_ai_rub_bot.py"
