"""Standalone (DB-free) guard tests for cooldown module tags.

Background (T-2026-CU-9050-024): the LIVE trade_cooldowns.module column is
character varying(10) — narrower than every in-repo bootstrap DDL
(VARCHAR(50)/TEXT), because the live table predates them and
CREATE TABLE IF NOT EXISTS never widens columns. A tag longer than 10 chars
makes update_cooldown throw StringDataRightTruncation before the signal is
returned/persisted, which silenced the Volume Indicator for five days and
made the Mayank bot re-post the same FVG setup every scan cycle.

These tests enforce the invariant without a DB:
  1. check_cooldown/update_cooldown reject over-long tags loudly.
  2. The literal/derived tags used by the strategies fit varchar(10).

Run: py -3.13 backtest/test_cooldown_tags.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.market_utils import (  # noqa: E402
    COOLDOWN_MODULE_MAX_LEN,
    check_cooldown,
    update_cooldown,
)


def test_guard_rejects_long_tags():
    long_tag = "Volume Indicator"  # the original 16-char offender
    for fn, args in (
        (check_cooldown, (None, long_tag, "BTCUSDT", "LONG", 12)),
        (update_cooldown, (None, long_tag, "BTCUSDT", "LONG")),
    ):
        try:
            fn(*args)
        except ValueError as e:
            assert "varchar" in str(e), f"unexpected guard message: {e}"
        else:
            raise AssertionError(f"{fn.__name__} accepted a {len(long_tag)}-char tag")


def test_volume_indicator_tag_fits():
    src = (ROOT / "strategies" / "strat_volume_indicator.py").read_text(encoding="utf-8")
    m = re.search(r"module_tag\s*=\s*['\"]([^'\"]+)['\"]", src)
    assert m, "module_tag assignment not found in strat_volume_indicator.py"
    tag = m.group(1)
    assert len(tag) <= COOLDOWN_MODULE_MAX_LEN, f"tag '{tag}' exceeds varchar({COOLDOWN_MODULE_MAX_LEN})"


def test_mayank_tags_fit():
    src = (ROOT / "17_mayank_bot.py").read_text(encoding="utf-8")
    tf_match = re.search(r"TIMEFRAMES\s*=\s*\[([^\]]+)\]", src)
    assert tf_match, "TIMEFRAMES not found in 17_mayank_bot.py"
    timeframes = re.findall(r"['\"]([^'\"]+)['\"]", tf_match.group(1))
    assert timeframes, "no timeframes parsed"

    tag_templates = re.findall(r"module_tag\s*=\s*f['\"]([^'\"]+)['\"]", src)
    assert tag_templates, "no f-string module_tag found in 17_mayank_bot.py"
    for template in tag_templates:
        assert "{symbol" not in template, f"tag template still embeds the symbol: {template}"
        for tf in timeframes:
            tag = template.replace("{tf.upper()}", tf.upper())
            assert len(tag) <= COOLDOWN_MODULE_MAX_LEN, (
                f"mayank tag '{tag}' exceeds varchar({COOLDOWN_MODULE_MAX_LEN})"
            )


def test_mis_horizon_tags_fit():
    """MIS posts cooldowns under a derived tag ``f"{generation}-{horizon}"``
    (11_ai_mis_bot.py:301), NOT a string literal — so test_static_tag_literals
    below never sees it. The fallback generation is MODEL_GENERATION and the
    horizons are the MIS_CHANNELS routing keys; MIS2-168H is exactly 10 chars,
    flush against varchar(10) (T-2026-CU-9050-024 error class; P3.13). Parse both
    from source so a new horizon (e.g. 720H) or a longer generation tag trips the
    boundary here instead of silently in a swallowed ValueError at runtime."""
    src = (ROOT / "11_ai_mis_bot.py").read_text(encoding="utf-8")

    gen_match = re.search(r"MODEL_GENERATION\s*=\s*['\"]([^'\"]+)['\"]", src)
    assert gen_match, "MODEL_GENERATION not found in 11_ai_mis_bot.py"
    generation = gen_match.group(1)

    ch_match = re.search(r"MIS_CHANNELS\s*=\s*\{([^}]+)\}", src)
    assert ch_match, "MIS_CHANNELS dict not found in 11_ai_mis_bot.py"
    horizons = re.findall(r"['\"]([0-9]+H)['\"]\s*:", ch_match.group(1))
    assert horizons, "no horizon keys parsed from MIS_CHANNELS"

    for horizon in horizons:
        tag = f"{generation}-{horizon}"  # mirrors module_tag at 11_ai_mis_bot.py:301
        assert len(tag) <= COOLDOWN_MODULE_MAX_LEN, (
            f"MIS tag '{tag}' exceeds varchar({COOLDOWN_MODULE_MAX_LEN})"
        )


def test_vol_indicator_cooldown_is_atomic_via_detector():
    """The Volume Indicator must NOT write its own cooldown — it requests it
    via signal['cooldown_module'] and 3_detectors.write_signal_atomic writes
    it in the same transaction as the signal (T-2026-CU-9050-024 round 2:
    a strategy-side upsert leaked into an earlier signal's commit)."""
    strat_src = (ROOT / "strategies" / "strat_volume_indicator.py").read_text(encoding="utf-8")
    assert "update_cooldown(" not in strat_src, "vol strategy writes its own cooldown again"
    assert '"cooldown_module": module_tag' in strat_src, "cooldown_module request missing from signal dict"

    det_src = (ROOT / "3_detectors.py").read_text(encoding="utf-8")
    assert "cooldown_module" in det_src, "write_signal_atomic does not honor cooldown_module"
    assert "commit=False" in det_src, "cooldown in write_signal_atomic must join the signal transaction"


def test_static_tag_literals_fleetwide():
    """Every string literal passed directly as the module arg to
    check_cooldown/update_cooldown anywhere in the repo root + strategies/
    must fit the live column."""
    offenders = []
    scan_dirs = [ROOT, ROOT / "strategies", ROOT / "core"]
    for py in [p for d in scan_dirs for p in d.glob("*.py")]:
        src = py.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"(?:check|update)_cooldown\(\s*[^,]+,\s*['\"]([^'\"]+)['\"]", src):
            tag = m.group(1)
            if len(tag) > COOLDOWN_MODULE_MAX_LEN:
                offenders.append(f"{py.name}: '{tag}'")
    assert not offenders, f"over-long literal cooldown tags: {offenders}"


if __name__ == "__main__":
    test_guard_rejects_long_tags()
    test_volume_indicator_tag_fits()
    test_mayank_tags_fit()
    test_mis_horizon_tags_fit()
    test_vol_indicator_cooldown_is_atomic_via_detector()
    test_static_tag_literals_fleetwide()
    print("OK — all cooldown-tag invariants hold")
