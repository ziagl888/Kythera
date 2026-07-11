# backtest/test_published_targets.py — P2.31 guard: track exactly the published targets.
#
# Background (AUDIT_TODO P2.31, T-2026-CU-9050-083): the signal bots 9/11/12/13 (and the
# shared research poster core/signal_post.post_ai_signal) publish only the first n_show
# take-profits in the Cornix block a subscriber sees (TP1-3, MIS: TP1-5), but stored the
# FULL computed target list (up to 20 support/resistance zones) into ai_signals.targets.
# The AI trade monitor (8_ai_trade_monitor.py) scores whatever is stored —
#
#     for i in range(new_targets_hit, len(targets)):   # 8_ai_trade_monitor.py
#         if candle_high >= float(targets[i]): ...
#     if new_targets_hit == len(targets): close_reason = "ALL TARGETS HIT"
#
# so it scored up to 10-20 phantom TPs the subscriber never had. The win definition and
# the trailing-SL semantics ran on targets that were never published. The monitor scores
# exactly len(stored), so the whole distortion reduces to one invariant:
#
#     len(ai_signals.targets)  ==  number of TPs published in the Cornix block  ==  n_show
#
# The fix caps the stored list to n_show at the insert. This file locks that invariant
# without a DB: behaviourally against the shared poster core/signal_post (the real insert
# path), and structurally against the four inline-posting bots (loading them needs the DB
# + heavy deps, so — like the *_tag.py guards — we assert on their source). Every check
# here fails on the pre-fix code (which stored the full list).
#
# Runs without a DB:  python backtest/test_published_targets.py

import json
import os
import re
import sys
import types

# Pre-seed pandas BEFORE any mock.patch.dict(sys.modules) work: patching sys.modules and
# then letting the block unwind can tear numpy/pandas down mid-interpreter. Importing it
# for real here parks it in sys.modules so our core.* stubs never own (or evict) it.
import pandas  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# --- DB-free import of the shared poster --------------------------------------------
# core/signal_post imports three core.* helpers that pull matplotlib/pandas. Stub them so
# the poster loads standalone; we only need their call-through values, not their behaviour.
# The stubs are installed ONLY around the import and then torn down: leaving permanent
# core.* stubs in sys.modules would shadow the real modules for other tests in a combined
# pytest run (test_cooldown_tags / test_max1_gate / test_signal_orchestrator import them).
# post_ai_signal keeps the stub function references it bound at import — that is all we
# need — so restoring sys.modules afterwards is safe.
def _import_poster_hermetically():
    stubs = {
        "core.charting": {"generate_minichart_image": lambda symbol, minutes=240: None},
        "core.market_utils": {"get_max_leverage": lambda symbol, default=20: "20x"},
        "core.trade_utils": {"format_price": lambda x: f"{float(x):.8f}"},
    }
    saved = {name: sys.modules.get(name) for name in stubs}
    saved["core.signal_post"] = sys.modules.get("core.signal_post")
    try:
        for name, attrs in stubs.items():
            mod = types.ModuleType(name)
            for k, v in attrs.items():
                setattr(mod, k, v)
            sys.modules[name] = mod
        # Force a fresh import of the poster against the stubs, not a cached real one.
        sys.modules.pop("core.signal_post", None)
        from core.signal_post import post_ai_signal as _fn

        return _fn
    finally:
        for name, prev in saved.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev


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
    """Records every execute() so we can inspect what would be written — no commit,
    matching the caller-commits contract of post_ai_signal."""

    def __init__(self):
        self.calls = []

    def cursor(self):
        return _FakeCursor(self.calls)


def _capture_post(n_show, n_targets=8):
    """Drive the real poster with more raw targets than n_show and return
    (stored_targets_list, cornix_message)."""
    targets = [round(1.0 + 0.01 * i, 8) for i in range(1, n_targets + 1)]
    conn = _FakeConn()
    post_ai_signal(
        conn,
        channel_id=-1000000000001,
        model_tag="TEST1",
        symbol="AAAUSDT",
        direction="LONG",
        confidence=0.9,
        entry1=1.0,
        entry2=0.99,
        sl=0.95,
        targets=targets,
        source_desc="unit-test",
        n_show=n_show,
        with_chart=False,
    )
    stored = None
    cornix = None
    for sql, params in conn.calls:
        if "INSERT INTO ai_signals" in sql:
            stored = json.loads(params[-1])
        elif "telegram_outbox" in sql and params and "TP1" in str(params[1]):
            cornix = params[1]
    assert stored is not None, "no ai_signals insert captured"
    assert cornix is not None, "no Cornix message captured"
    return stored, cornix, targets


def test_poster_stores_exactly_the_published_targets():
    """The behavioural core: for n_show=3 with 8 raw targets, ai_signals.targets holds
    exactly the 3 published prices — not the full 8. Fails on the pre-fix poster, which
    stored all 8 and let the monitor score 5 phantom TPs."""
    for n_show in (3, 5):
        stored, cornix, targets = _capture_post(n_show=n_show, n_targets=8)
        published_tp_lines = re.findall(r"TP\d+:", cornix)
        assert len(published_tp_lines) == n_show, (
            f"Cornix published {len(published_tp_lines)} TPs, expected n_show={n_show}"
        )
        assert len(stored) == n_show, (
            f"ai_signals.targets stored {len(stored)} targets, but only {n_show} were "
            f"published — the monitor would score {len(stored) - n_show} phantom TPs"
        )
        assert stored == [float(t) for t in targets[:n_show]], (
            "stored targets are not exactly the published slice (order/values drifted)"
        )
    print("OK  poster: ai_signals.targets == published Cornix targets (n_show)")


def test_poster_stores_all_when_fewer_than_n_show():
    """Guard the boundary: with fewer real targets than n_show, everything computed is
    also everything published, so nothing is dropped."""
    stored, cornix, targets = _capture_post(n_show=5, n_targets=2)
    assert len(stored) == 2 == len(re.findall(r"TP\d+:", cornix)), (
        "with 2 real targets both must be published and stored"
    )
    print("OK  poster: no over-trim when real targets < n_show")


# --- Structural guards for the four inline-posting bots ------------------------------
# Each bot computes up to 20 zones (ensure_min_tp_distance(..., t_cands[:20], ...)),
# publishes targets[:n_show] in the Cornix loop and MUST store the same slice. The
# pre-fix insert bind was a bare json.dumps(targets) — that literal is what regressed.
_BOTS = {
    "9_ai_sr_bot.py": 3,
    "11_ai_mis_bot.py": 5,
    "12_ai_ats_bot.py": 3,
    "13_ai_rub_bot.py": 3,
}


def _src(name):
    return (open(os.path.join(ROOT, name), encoding="utf-8")).read()


def test_bots_define_n_show_and_publish_with_it():
    for name, n in _BOTS.items():
        src = _src(name)
        assert re.search(rf"n_show\s*=\s*{n}\b", src), f"{name}: n_show = {n} not defined"
        assert re.search(r"enumerate\(targets\[:n_show\]", src), (
            f"{name}: the Cornix TP loop no longer slices targets[:n_show]"
        )
    print("OK  bots: n_show drives the published Cornix block")


def test_bots_store_only_the_published_slice():
    for name in _BOTS:
        src = _src(name)
        assert re.search(r"json\.dumps\(targets\[:n_show\]\)", src), (
            f"{name}: the ai_signals insert no longer stores the published slice "
            "targets[:n_show]"
        )
        # The pre-fix regression was a bare json.dumps(targets). It must not come back.
        assert not re.search(r"json\.dumps\(targets\)", src), (
            f"{name}: ai_signals still stores the FULL target list (json.dumps(targets)) "
            "— the monitor would score phantom TPs again"
        )
    print("OK  bots: ai_signals stores exactly targets[:n_show], never the full list")


def test_shared_poster_source_slices_at_insert():
    src = _src(os.path.join("core", "signal_post.py"))
    assert re.search(r"for\s+t\s+in\s+targets\[:n_show\]", src), (
        "core/signal_post: ai_signals insert no longer slices targets[:n_show]"
    )
    assert not re.search(r"for\s+t\s+in\s+targets\]\)", src) and not re.search(
        r"json\.dumps\(\[float\(t\)\s+for\s+t\s+in\s+targets\]\)", src
    ), "core/signal_post: still stores the full target list into ai_signals"
    print("OK  core/signal_post: ai_signals stores the published slice")


def test_monitor_scores_the_whole_stored_list():
    """Documents WHY capping the stored list is the correct lever: the AI monitor scores
    every stored target (range(.., len(targets))) and declares ALL TARGETS HIT at
    len(targets). It has no independent target ceiling — so the published count must be
    enforced at storage. Catches a future monitor change that reintroduces a hardcoded
    or unbounded scoring loop that would decouple scoring from the stored list."""
    src = _src("8_ai_trade_monitor.py")
    assert re.search(r"for i in range\(new_targets_hit,\s*len\(targets\)\)", src), (
        "8_ai_trade_monitor no longer scores exactly the stored target list — the P2.31 "
        "invariant (len(stored) == published) may no longer bound the monitor"
    )
    print("OK  monitor: scores exactly len(stored targets) — storage is the right lever")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    test_poster_stores_exactly_the_published_targets()
    test_poster_stores_all_when_fewer_than_n_show()
    test_bots_define_n_show_and_publish_with_it()
    test_bots_store_only_the_published_slice()
    test_shared_poster_source_slices_at_insert()
    test_monitor_scores_the_whole_stored_list()
    print("\nAll P2.31 published-target guards passed.")
