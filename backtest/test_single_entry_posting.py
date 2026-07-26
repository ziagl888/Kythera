# backtest/test_single_entry_posting.py — T-2026-KYT-9050-042 guard: the fleet posts a
# SINGLE entry.
#
# Background: every posting bot published a DCA bracket — market entry on entry1, a
# top-up limit on entry2, SL behind entry2. T-2026-KYT-9050-043/044/045 measured that
# bracket risk-adjusted on the high-fidelity harness and found the top-up HURTS: arm B
# (entry1 only, original SL) lifted the per-trade Sharpe on 15 of 17 bots with a usable
# sample (median drag B−A +0.073), and the effect got STRONGER on the longest window
# available (MIS1-168H, 2.5 months: 0.06 → 0.16). Operator decision (Michi): ship arm B
# by dropping the published entry2 line — the geometry itself is untouched, so entry2 is
# still computed, the SL still sits behind it, and ai_signals.entry2 is still written.
#
# ROM1 (28_signal_orchestrator) is the deliberate exception: it was one of the two bots
# without DCA damage (drag +0.014) and its top-up buys real drawdown protection
# (MaxDD 10 % → 14.5 % without it).
#
# Two layers, both DB-free: behavioural against core/signal_post (the real outbox +
# ai_signals path) and structural source pins against the inline-posting bots — loading
# those needs the DB and heavy deps, so we assert on their source, exactly like
# test_published_targets.py does for P2.31.
#
# Runs without a DB:  python backtest/test_single_entry_posting.py

import json
import os
import sys
import types

# Pre-seed pandas BEFORE any sys.modules stubbing (see test_published_targets.py): the
# stub install/teardown can otherwise tear numpy/pandas down mid-interpreter.
import pandas  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The published DCA line, verbatim as the bots emitted it. Cornix keys on the price
# lines, so the emoji + label is the thing that must be gone from the message.
ENTRY2_MARKER = "Entry 2"

# Every module that used to publish an entry2 line and must not any more.
SINGLE_ENTRY_SOURCES = [
    "core/signal_post.py",
    "7_pattern_detector.py",
    "9_ai_sr_bot.py",
    "10_pump_dump_detector.py",
    "11_ai_mis_bot.py",
    "12_ai_ats_bot.py",
    "13_ai_rub_bot.py",
    "14_ai_atb_bot.py",
    "15_ai_master_bot.py",
    "16_smc_forex_metals_bot.py",
    "18_ai_abr1_bot.py",
    "25_smc_ml_sniper.py",
    "handlers/open_handler.py",
]

ROM1_SOURCE = "28_signal_orchestrator.py"


def _read(rel_path: str) -> str:
    """Source of a repo file. encoding is explicit — the Cornix blocks are emoji-heavy
    and the Windows default codepage would mangle (or refuse) them."""
    with open(os.path.join(ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


# --- DB-free import of the shared poster (pattern from test_published_targets.py) -----
def _import_poster_hermetically():
    stubs = {
        "core.charting": {"generate_minichart_image": lambda symbol, minutes=240: None},
        "core.market_utils": {"get_max_leverage": lambda symbol, default=20: "20x"},
        "core.trade_utils": {"format_price": lambda x: f"{float(x):.8f}"},
    }
    saved = {name: sys.modules.get(name) for name in stubs}
    saved["core.signal_post"] = sys.modules.get("core.signal_post")
    import core as _core_pkg

    saved_pkg_attr = getattr(_core_pkg, "signal_post", None)
    try:
        for name, attrs in stubs.items():
            mod = types.ModuleType(name)
            for k, v in attrs.items():
                setattr(mod, k, v)
            sys.modules[name] = mod
        sys.modules.pop("core.signal_post", None)
        from core.signal_post import post_ai_signal as _fn

        return _fn
    finally:
        for name, prev in saved.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev
        if saved_pkg_attr is None:
            if hasattr(_core_pkg, "signal_post"):
                delattr(_core_pkg, "signal_post")
        else:
            _core_pkg.signal_post = saved_pkg_attr


post_ai_signal = _import_poster_hermetically()


class _FakeCursor:
    def __init__(self, calls):
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._calls.append((sql, params))

    def fetchone(self):
        return None


class _FakeConn:
    """Records every execute() — no commit, matching the caller-commits contract."""

    def __init__(self):
        self.calls = []

    def cursor(self):
        return _FakeCursor(self.calls)


def _post(entry1=1.0, entry2=0.99):
    """Drive the real poster and return (cornix_message, ai_signals_insert_params)."""
    conn = _FakeConn()
    post_ai_signal(
        conn,
        channel_id=-1000000000001,
        model_tag="TEST1",
        symbol="AAAUSDT",
        direction="LONG",
        confidence=0.9,
        entry1=entry1,
        entry2=entry2,
        sl=0.95,
        targets=[1.01, 1.02, 1.03],
        source_desc="unit-test",
        with_chart=False,
    )
    cornix, stored = None, None
    for sql, params in conn.calls:
        if "INSERT INTO ai_signals" in sql:
            stored = params
        elif "telegram_outbox" in sql and params and "TP1" in str(params[1]):
            cornix = params[1]
    assert cornix is not None, "no Cornix message captured"
    assert stored is not None, "no ai_signals insert captured"
    return cornix, stored


# --- AK1 + AK4: nothing publishes entry2 any more --------------------------------------


def test_no_module_publishes_an_entry2_line():
    """Structural pin over every former DCA publisher — Cornix block AND the HTML info
    message (11/25 rendered entry2 a second time in their caption)."""
    offenders = [rel for rel in SINGLE_ENTRY_SOURCES if ENTRY2_MARKER in _read(rel)]
    assert not offenders, f"these modules still publish an entry2 line: {offenders}"


def test_html_info_messages_show_entry1_only():
    """AK4 explicitly: the two bots with an entry ladder in their HTML caption (MIS,
    SNIPER) keep exactly one entry row."""
    for rel in ("11_ai_mis_bot.py", "25_smc_ml_sniper.py"):
        src = _read(rel)
        assert "Entry 1:" in src, f"{rel} lost its HTML entry row entirely"
        assert "Entry 2:" not in src, f"{rel} still renders an Entry 2 row"


# --- AK2: ROM1 is the carve-out --------------------------------------------------------


def test_rom1_still_publishes_its_entry2():
    """ROM1 computes its own entry2 (ROM1_ENTRY2_OFFSET_PCT) and keeps publishing it —
    it was measured neutral-to-protective, and it does not inherit the upstream bots'
    geometry. If this ever fails, the carve-out was removed by accident."""
    src = _read(ROM1_SOURCE)
    assert ENTRY2_MARKER in src, "ROM1 lost its entry2 line — the T-042 carve-out is gone"
    assert "ROM1_ENTRY2_OFFSET_PCT" in src, "ROM1 no longer computes its own entry2 offset"


# --- AK1 behavioural: exactly one entry line in the real message -----------------------


def test_poster_publishes_exactly_one_entry_line():
    cornix, _ = _post(entry1=1.0, entry2=0.99)
    entry_lines = [ln for ln in cornix.splitlines() if "Entry" in ln]
    assert len(entry_lines) == 1, f"expected a single entry line, got {entry_lines}"
    assert "CMP Entry" in entry_lines[0]
    assert ENTRY2_MARKER not in cornix


def test_poster_drops_entry2_even_when_far_from_entry1():
    """The pre-fix poster suppressed the line only when entry2 FORMATTED identical to
    entry1 (the FIF1 single-entry case). A genuinely different entry2 used to be
    published — that is the case this change is about."""
    cornix, _ = _post(entry1=100.0, entry2=95.0)
    assert ENTRY2_MARKER not in cornix
    assert "95.00000000" not in cornix, "the entry2 price leaked into the message"


# --- AK3: geometry and DB are untouched ------------------------------------------------


def test_ai_signals_still_stores_entry2():
    """Only the PUBLISHING changed. ai_signals.entry2 stays populated — the monitor, the
    realized-PnL report and the outbox-based research harnesses read it."""
    _, stored = _post(entry1=100.0, entry2=95.0)
    # INSERT column order: symbol, price, model, direction, confidence, entry1, entry2, sl, targets
    assert stored[5] == 100.0, f"entry1 changed: {stored[5]}"
    assert stored[6] == 95.0, f"entry2 is no longer stored: {stored[6]}"
    assert stored[7] == 0.95, f"the SL moved: {stored[7]} — arm B keeps the original SL"
    assert json.loads(stored[8]) == [1.01, 1.02, 1.03]


def test_shadow_poster_still_stores_entry2():
    """The shadow leg (post_shadow_ai_signal) was never a publisher — it must keep
    writing the full geometry so shadow trades stay comparable to live ones."""
    src = _read("core/signal_post.py")
    shadow = src.split("def post_shadow_ai_signal", 1)[1].split("\ndef ", 1)[0]
    assert "entry1, entry2, sl, targets" in shadow, "shadow leg no longer inserts entry2"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
