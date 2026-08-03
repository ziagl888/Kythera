# backtest/test_rub4_funding_gate.py
"""DB-free tests for the RUB4 funding gate (bot 13, T-2026-CU-9050-164).

RUB4 = funding-gated RUB LONG shadow leg: SAME RUB3 candidate, but only
when ``fund_24h > +3 bps`` (ABR1 LONG threshold). Pure shadow experiment (never
live), own tag → report compares gated (RUB4) vs. ungated (RUB3).

  1. funding_gate_open: strictly > 3.0 bps; None ⇒ closed.
  2. shadow_gate: RUB4-LONG is SHADOW, without own artifact (uses RUB3's model);
     RUB4-SHORT stays default-LIVE (there is no RUB4-SHORT leg).
  3. bot_catalog: tag "RUB4" → 13_ai_rub_bot.py (RUB prefix).
  4. The gate threshold == ABR1 LONG (3.0 bps).

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
        (3.0, False),  # strictly >, not >=
        (2.99, False),
        (0.0, False),
        (-2.0, False),
        (None, False),  # no funding data ⇒ gate closed
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
    # RUB4 uses the RUB3 artifact (bot loads SHADOW_RUB3_LONG) → NO own
    # SHADOW_ARTIFACTS entry; the loader would return None.
    assert "RUB4" not in sg.SHADOW_ARTIFACTS
    assert sg.shadow_artifact_path("RUB4", "LONG") is None
    # no RUB4-SHORT leg → default-LIVE (nothing posts it)
    assert sg.leg_status("RUB4", "SHORT") == sg.LIVE
    # the ungated RUB3-LONG remains separately SHADOW
    assert sg.leg_status("RUB3", "LONG") == sg.SHADOW


# ── 3. bot_catalog ────────────────────────────────────────────────────────────
def test_rub4_tag_maps_to_bot13():
    assert bc.script_for_tag("RUB4") == "13_ai_rub_bot.py"
    assert bc.script_for_tag("RUB3") == "13_ai_rub_bot.py"
