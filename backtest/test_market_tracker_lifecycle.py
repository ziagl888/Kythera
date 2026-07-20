# backtest/test_market_tracker_lifecycle.py
"""DB-freie Tests für die 3-Wege-Lifecycle-Klassifikation des Realized-PnL-
Reports (T-2026-CU-9050-125): active / shadow / retired / inactive / unmapped.

Nutzt das ECHTE core.shadow_gate + core.bot_catalog (beide DB-frei); nur die
DB-/Telegram-gebundenen Imports werden gemockt. Muster wie
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

    # shadow_gate / bot_catalog / bot_naming bleiben ECHT — sie sind DB-frei und
    # genau die Lookups, die wir testen. Nur DB-/Telegram-Bindungen mocken.
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
from core.bot_catalog import script_for_tag  # noqa: E402  (echt, für die Erwartungswerte)


def test_new_gen_tags_bucket_as_shadow():
    active = set()
    assert mt.realized_lifecycle_bucket("ATS2", "LONG", active) == "shadow"
    assert mt.realized_lifecycle_bucket("ATB2", "SHORT", active) == "shadow"


def test_old_generation_tags_bucket_as_retired():
    active = {script_for_tag("MIS1-8h"), script_for_tag("AIM1")}
    # Retired schlägt Live-Skript-Gate: auch wenn der Bot läuft, ist der TAG alt.
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
    # T-2026-CU-9050-127: ATS1/ATB1 sind SILENT (Bots 12/14 laufen für ATS2/ATB2-
    # Shadow, aber die Alt-Beine posten nichts). Trotz laufendem Skript -> retired,
    # nicht active — sonst behauptete der Report, ATS1 poste noch live.
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


# ─── is_display_retired: Perf-/Kelly-/A–Z-Filter (T-2026-CU-9050-182) ───
# Deckungsgleich mit dem RETIRED-Bucket: retired UND silent raus, shadow+live rein.


def test_display_retired_hides_old_generations():
    # Abgelöste Tags (is_retired-Prefix) — beide Richtungen RETIRED.
    assert mt.is_display_retired("AIM1") is True
    assert mt.is_display_retired("MIS1-8h") is True
    assert mt.is_display_retired("MIS1-168h") is True


def test_display_retired_hides_silenced_legs():
    # ATS1/ATB1 sind SILENT (Bots laufen für ATS2/ATB2-Shadow) → raus.
    assert mt.is_display_retired("ATS1") is True
    assert mt.is_display_retired("ATB1") is True


def test_display_retired_keeps_shadow_tags():
    # Shadow-Perf ist die Entscheidungsgrundlage für Swaps → sichtbar bleiben.
    for tag in ("ATS2", "ATB2", "SRA2", "EPD3", "TSM1"):
        assert mt.is_display_retired(tag) is False, tag


def test_display_retired_keeps_live_tags():
    # Default-LIVE + der Prefix-Nachbar MIS2 dürfen NICHT gefiltert werden.
    for tag in ("RUB2", "FastInOut", "MIS2-8h"):
        assert mt.is_display_retired(tag) is False, tag


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
