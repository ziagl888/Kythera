"""Pin the scan cadence of the two continuous sweepers (T-2026-KYT-9050-137).

Bots 24 (QM) and 25 (SMC sniper) analyse closed candles only, so their sweep
cadence is a pure cost knob: at 180 s the pair produced ~87 % of cluster A's
candle reads (see docs/T-2026-KYT-9050-136-scan-engine-design.md §2.1) while
re-reading mostly identical data. The operator set the cadence to 900 s on
2026-08-11. These tests pin that decision at the source level — both bots load
model artifacts at module scope (with a module-level ``exit(1)`` on failure),
so importing them here is deliberately avoided.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SWEEPERS = {
    "24_quasimodo_bot.py": 900,
    "25_smc_ml_sniper.py": 900,
}


def _source(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def test_scan_interval_constant_is_pinned():
    for name, expected in SWEEPERS.items():
        src = _source(name)
        match = re.search(r"^SCAN_INTERVAL_SECONDS = (\d+)$", src, re.MULTILINE)
        assert match, f"{name}: SCAN_INTERVAL_SECONDS constant missing"
        assert int(match.group(1)) == expected, (
            f"{name}: cadence changed from the operator-decided {expected} s "
            f"to {match.group(1)} s — that is an operator decision, not a tweak"
        )


def test_main_loop_sleeps_on_the_constant():
    for name in SWEEPERS:
        src = _source(name)
        assert "time.sleep(SCAN_INTERVAL_SECONDS)" in src, (
            f"{name}: main loop no longer sleeps on SCAN_INTERVAL_SECONDS"
        )
        assert re.search(r"time\.sleep\(\s*180\s*\)", src) is None, f"{name}: a hardcoded 180 s sleep is back"


def test_m1_log_pattern_survives():
    # docs/T-2026-KYT-9050-136-scan-engine-design.md M1 greps for this prefix
    # to measure per-scan duration; the cadence change must not break it.
    for name in SWEEPERS:
        assert "Radar scan stopped." in _source(name), (
            f"{name}: the 'Radar scan stopped.' log prefix (M1 measurement anchor) disappeared"
        )
