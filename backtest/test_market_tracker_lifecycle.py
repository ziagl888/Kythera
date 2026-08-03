# backtest/test_market_tracker_lifecycle.py
"""DB-free tests for the 3-way lifecycle classification of the realised PnL
report (T-2026-CU-9050-125): active / shadow / retired / inactive / unmapped.

Uses REAL core.shadow_gate + core.bot_catalog (both DB-free); only the
DB-/Telegram-bound imports are mocked. Pattern like
test_market_tracker_realized.py.

Run: pytest backtest/test_market_tracker_lifecycle.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")


def _load_tracker():
    spec = importlib.util.spec_from_file_location(
        "market_tracker_lifecycle",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "23_market_tracker.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    import pandas  # noqa: F401  (numpy C-extensions must survive the patch.dict)

    # shadow_gate / bot_catalog / bot_naming stay REAL — they are DB-free and
    # exactly the lookups we are testing. Only mock DB-/Telegram bindings.
    with mock.patch.dict(
        "sys.modules",
        {
            "core.config": mock.MagicMock(),
            "core.database": mock.MagicMock(),
            "core.market_utils": mock.MagicMock(),
            "core.realized_pnl": mock.MagicMock(),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


mt = _load_tracker()
from core.bot_catalog import script_for_tag  # noqa: E402  (real, for expected values)


def test_new_gen_tags_bucket_as_shadow():
    active = set()
    assert mt.realized_lifecycle_bucket("ATS2", "LONG", active) == "shadow"
    assert mt.realized_lifecycle_bucket("ATB2", "SHORT", active) == "shadow"


def test_old_generation_tags_bucket_as_retired():
    active = {script_for_tag("MIS1-8h"), script_for_tag("AIM1")}
    # Retired beats live-script gate: even if the bot runs, the TAG is old.
    assert mt.realized_lifecycle_bucket("MIS1-8h", "LONG", active) == "retired"
    assert mt.realized_lifecycle_bucket("AIM1", "SHORT", active) == "retired"
    # MIS2 darf NICHT als retired matchen (Prefix-Grenze).
    assert mt.realized_lifecycle_bucket("MIS2-8h", "LONG", active) != "retired"


def test_live_leg_active_when_script_runs():
    tag = "RUB2"  # live SHORT-Bein, Default-LIVE (nicht in der Shadow-Registry)
    script = script_for_tag(tag)
    assert script is not None
    assert mt.realized_lifecycle_bucket(tag, "SHORT", {script}) == "active"


def test_silent_old_leg_buckets_retired_even_when_script_runs():
    # T-2026-CU-9050-127: ATS1/ATB1 are SILENT (bots 12/14 run for ATS2/ATB2
    # shadow, but the old legs post nothing). Despite running script -> retired,
    # not active — otherwise the report would claim ATS1 is still posting live.
    active = {script_for_tag("ATS1"), script_for_tag("ATB1")}
    assert None not in active
    assert mt.realized_lifecycle_bucket("ATS1", "LONG", active) == "retired"
    assert mt.realized_lifecycle_bucket("ATB1", "SHORT", active) == "retired"


def test_live_leg_inactive_when_script_parked():
    tag = "RUB2"
    assert mt.realized_lifecycle_bucket(tag, "SHORT", set()) == "inactive"


def test_unknown_tag_is_unmapped():
    assert script_for_tag("ZZZ_NOT_A_MODEL") is None
    assert mt.realized_lifecycle_bucket("ZZZ_NOT_A_MODEL", "LONG", set()) == "unmapped"


# ─── is_display_retired: Perf-/Kelly-/A–Z filter (T-2026-CU-9050-182) ───
# Congruent with the RETIRED bucket: retired AND silent out, shadow+live in.


def test_display_retired_hides_old_generations():
    # Superseded tags (is_retired prefix) — both directions RETIRED.
    assert mt.is_display_retired("AIM1") is True
    assert mt.is_display_retired("MIS1-8h") is True
    assert mt.is_display_retired("MIS1-168h") is True


def test_display_retired_hides_silenced_legs():
    # ATS1/ATB1 are SILENT (bots run for ATS2/ATB2 shadow) → out.
    assert mt.is_display_retired("ATS1") is True
    assert mt.is_display_retired("ATB1") is True


def test_display_retired_keeps_shadow_tags():
    # Shadow performance is the basis for swap decisions → must stay visible.
    for tag in ("ATS2", "ATB2", "SRA2", "EPD3", "TSM1"):
        assert mt.is_display_retired(tag) is False, tag


def test_display_retired_keeps_live_tags():
    # Default-LIVE + the prefix neighbour MIS2 must NOT be filtered.
    for tag in ("RUB2", "FastInOut", "MIS2-8h"):
        assert mt.is_display_retired(tag) is False, tag


def test_display_retired_on_raw_pre_normalization_tags():
    # The call site feeds strategy_short = pretty_name(strategy); is_retired lifts
    # over the prefix boundary, so RAW pre-normalisation forms
    # (MIS-pump/dump, MSI1-typo-family) are also correctly identified as retired. Pins
    # the pretty↔raw equivalence so a pretty_name regression doesn't slip through
    # silently.
    assert mt.is_display_retired("MIS1-8H_pump") is True
    assert mt.is_display_retired("MIS1-72h_dump") is True
    assert mt.is_display_retired("MSI1-24h") is True


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
