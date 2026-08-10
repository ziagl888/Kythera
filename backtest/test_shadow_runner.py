# backtest/test_shadow_runner.py
"""DB-free tests for the shared shadow-scanner runner (T-2026-KYT-9050-133).

Bots 36 (LIS1), 37 (TSM1), 38 (SKW1) and 39 (XSM1/XSR1) gave up their own
processes and now run inside ``45_shadow_scanner_runner.py``. The consolidation
is only allowed to change WHERE the scan runs, never WHETHER or WHEN — so the
three properties that made four processes safe are pinned here:

  1. **Schedule** — every scan still fires on its own slot (minute 23 hourly /
     minute 29 of the 4h hours / Monday 00:31 / Monday 00:37 UTC), exactly once
     per slot, including the 4h-grid and week-wrap edges. Plus the grace window
     that keeps a sibling overrun from silently dropping a weekly rebalance.
  2. **Park granularity** — the runner checks the FOUR original park markers
     (``control/parked/<script>.py``) per bot before each scan, so the operator
     can still silence a single bot.
  3. **Error isolation** — an exception in one scan does not stop the other
     three, and a failed slot is not retried in a hot loop (bot-29 pattern).

Plus the report-side invariant that the four keep their identity in
``core/bot_catalog.py`` even though they no longer have a fleet entry.

No DB, no clock, no filesystem: the schedule is pure arithmetic, the scan
modules are injected fakes, and ``is_parked`` is patched.

Run: pytest backtest/test_shadow_runner.py -v
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import sys
import unittest.mock as mock

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# core.config wants secrets; the build machine supplies an empty .env.
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import core.bot_catalog as bc  # noqa: E402
from core import shadow_scanners as ss  # noqa: E402

UTC = datetime.timezone.utc


def _import_runner():
    path = os.path.join(REPO_ROOT, "45_shadow_scanner_runner.py")
    spec = importlib.util.spec_from_file_location("shadow_runner_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["shadow_runner_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


runner = _import_runner()

LIS1, TSM1, SKW1, XSM1 = ss.SCANNERS


def _spec(script: str) -> ss.ScannerSpec:
    return next(s for s in ss.SCANNERS if s.script == script)


class _FakeScan:
    """Stand-in for a bot module: records calls, optionally explodes."""

    def __init__(self, boom: bool = False):
        self.calls = 0
        self.boom = boom

    def run_scan(self) -> None:
        self.calls += 1
        if self.boom:
            raise RuntimeError("scan exploded")


def _modules(**boom: bool) -> dict[str, _FakeScan]:
    return {s.script: _FakeScan(boom=boom.get(s.script, False)) for s in ss.SCANNERS}


# ── 1. Roster ────────────────────────────────────────────────────────────────


def test_roster_covers_exactly_the_four_consolidated_bots():
    assert ss.HOSTED_SCRIPTS == (
        "36_ai_lis1_bot.py",
        "37_ai_tsm1_bot.py",
        "38_ai_skw1_bot.py",
        "39_ai_xsm1_bot.py",
    )
    assert ss.RUNNER_SCRIPT == "45_shadow_scanner_runner.py"


@pytest.mark.parametrize("spec", ss.SCANNERS, ids=lambda s: s.script)
def test_hosted_scanner_files_exist_and_expose_run_scan(spec):
    # The runner calls run_scan() on the unchanged module — a rename in a bot
    # file would otherwise only surface at the next scheduled slot in production.
    source = open(os.path.join(REPO_ROOT, spec.script), encoding="utf-8").read()
    assert "def run_scan(" in source


@pytest.mark.parametrize("spec", ss.SCANNERS, ids=lambda s: s.script)
def test_logger_name_matches_the_bots_own_log_prefix(spec):
    # The runner imports each module under spec.logger_name and logs with
    # %(name)s, so this string is what the scan lines are prefixed with. It must
    # stay the prefix the standalone bot used, or the log format silently drifts.
    source = open(os.path.join(REPO_ROOT, spec.script), encoding="utf-8").read()
    assert f"- {spec.logger_name} -" in source


@pytest.mark.parametrize("spec", ss.SCANNERS, ids=lambda s: s.script)
def test_scan_minute_matches_the_bot_constant(spec):
    # The schedule is restated here; the bot still carries its own SCAN_MINUTE.
    source = open(os.path.join(REPO_ROOT, spec.script), encoding="utf-8").read()
    assert f"SCAN_MINUTE = {spec.minute}" in source


def _loop_predicate(source: str) -> str:
    """The one ``if …:`` condition in a bot's main() loop that gates the scan.

    Comment stripped, so a wording change in the trailing comment does not trip
    the pin — only the condition itself does.
    """
    lines = [ln.strip() for ln in source.splitlines() if "SCAN_MINUTE" in ln and ln.strip().startswith("if ")]
    assert len(lines) == 1, f"expected exactly one scan-gate line, found {lines}"
    return lines[0].split("#")[0].strip().removeprefix("if ").rstrip(":").strip()


def _expected_predicate(spec: ss.ScannerSpec) -> str:
    if spec.cadence == ss.HOURLY:
        return "now.minute == SCAN_MINUTE"
    if spec.cadence == ss.EVERY_4H:
        return "now.hour % 4 == 0 and now.minute == SCAN_MINUTE"
    return f"now.weekday() == {spec.weekday} and now.hour == {spec.hour} and now.minute == SCAN_MINUTE"


@pytest.mark.parametrize("spec", ss.SCANNERS, ids=lambda s: s.script)
def test_cadence_matches_the_bots_own_loop_predicate(spec):
    # SCAN_MINUTE alone is only a third of the schedule: bot 37 additionally
    # gates on `hour % 4 == 0`, bots 38/39 on `weekday() == 0 and hour == 0`.
    # The runner re-expresses those predicates as cadence/hour/weekday in
    # ScannerSpec, so the whole condition is pinned against the bot source —
    # comparing the FULL line, not a substring, because "now.minute ==
    # SCAN_MINUTE" also sits inside the 4h and weekly conditions and would keep
    # matching if a bot's schedule silently gained or lost a qualifier.
    source = open(os.path.join(REPO_ROOT, spec.script), encoding="utf-8").read()
    assert _loop_predicate(source) == _expected_predicate(spec)


# ── 2. Schedule: the slot each cadence is currently in ───────────────────────


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # inside the due minute → that slot
        (datetime.datetime(2026, 8, 12, 9, 23, 0, tzinfo=UTC), datetime.datetime(2026, 8, 12, 9, 23, tzinfo=UTC)),
        (datetime.datetime(2026, 8, 12, 9, 23, 59, tzinfo=UTC), datetime.datetime(2026, 8, 12, 9, 23, tzinfo=UTC)),
        # after it → still that slot, until the next hour comes round
        (datetime.datetime(2026, 8, 12, 9, 59, 0, tzinfo=UTC), datetime.datetime(2026, 8, 12, 9, 23, tzinfo=UTC)),
        # before it → the PREVIOUS hour's slot (hour rollover, incl. midnight)
        (datetime.datetime(2026, 8, 12, 9, 22, 0, tzinfo=UTC), datetime.datetime(2026, 8, 12, 8, 23, tzinfo=UTC)),
        (datetime.datetime(2026, 8, 12, 0, 5, 0, tzinfo=UTC), datetime.datetime(2026, 8, 11, 23, 23, tzinfo=UTC)),
    ],
)
def test_hourly_slot(now, expected):
    assert ss.last_slot(LIS1, now) == expected


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # on the 4h grid, inside the minute
        (datetime.datetime(2026, 8, 12, 8, 29, 0, tzinfo=UTC), datetime.datetime(2026, 8, 12, 8, 29, tzinfo=UTC)),
        # grid hour but before the minute → the previous 4h slot, NOT 08:29
        (datetime.datetime(2026, 8, 12, 8, 28, 0, tzinfo=UTC), datetime.datetime(2026, 8, 12, 4, 29, tzinfo=UTC)),
        # off-grid hours all belong to the last grid slot
        (datetime.datetime(2026, 8, 12, 9, 0, 0, tzinfo=UTC), datetime.datetime(2026, 8, 12, 8, 29, tzinfo=UTC)),
        (datetime.datetime(2026, 8, 12, 11, 59, 0, tzinfo=UTC), datetime.datetime(2026, 8, 12, 8, 29, tzinfo=UTC)),
        # day rollover: 00:00-00:28 belongs to yesterday's 20:29
        (datetime.datetime(2026, 8, 12, 0, 10, 0, tzinfo=UTC), datetime.datetime(2026, 8, 11, 20, 29, tzinfo=UTC)),
    ],
)
def test_four_hourly_slot(now, expected):
    assert ss.last_slot(TSM1, now) == expected


@pytest.mark.parametrize(
    ("spec", "now", "expected"),
    [
        # 2026-08-10 is a Monday, 2026-08-12 a Wednesday.
        (SKW1, datetime.datetime(2026, 8, 10, 0, 31, 0, tzinfo=UTC), datetime.datetime(2026, 8, 10, 0, 31, tzinfo=UTC)),
        # Monday before the minute → LAST week's slot (the week-wrap edge)
        (SKW1, datetime.datetime(2026, 8, 10, 0, 30, 0, tzinfo=UTC), datetime.datetime(2026, 8, 3, 0, 31, tzinfo=UTC)),
        # any other weekday → this Monday's slot
        (SKW1, datetime.datetime(2026, 8, 12, 17, 0, 0, tzinfo=UTC), datetime.datetime(2026, 8, 10, 0, 31, tzinfo=UTC)),
        (SKW1, datetime.datetime(2026, 8, 16, 23, 59, 0, tzinfo=UTC), datetime.datetime(2026, 8, 10, 0, 31, tzinfo=UTC)),
        # XSM1 sits six minutes later, on the same weekly grid
        (XSM1, datetime.datetime(2026, 8, 10, 0, 33, 0, tzinfo=UTC), datetime.datetime(2026, 8, 3, 0, 37, tzinfo=UTC)),
        (XSM1, datetime.datetime(2026, 8, 10, 0, 37, 0, tzinfo=UTC), datetime.datetime(2026, 8, 10, 0, 37, tzinfo=UTC)),
    ],
)
def test_weekly_slot(spec, now, expected):
    assert ss.last_slot(spec, now) == expected


def test_unknown_cadence_is_rejected():
    bogus = ss.ScannerSpec("x.py", "X", "fortnightly", minute=5)
    with pytest.raises(ValueError):
        ss.last_slot(bogus, datetime.datetime(2026, 8, 12, 9, 0, tzinfo=UTC))


# ── 3. Slot dispatch verdicts ────────────────────────────────────────────────


def test_slot_runs_once_and_then_goes_idle():
    now = datetime.datetime(2026, 8, 12, 9, 23, 4, tzinfo=UTC)
    action, slot = ss.slot_action(LIS1, now, None)
    assert action == ss.RUN
    # Same slot already handled → idle for the rest of the hour.
    assert ss.slot_action(LIS1, now, slot)[0] == ss.IDLE
    assert ss.slot_action(LIS1, datetime.datetime(2026, 8, 12, 9, 59, tzinfo=UTC), slot)[0] == ss.IDLE
    # Next hour's slot is a new one.
    assert ss.slot_action(LIS1, datetime.datetime(2026, 8, 12, 10, 23, tzinfo=UTC), slot)[0] == ss.RUN


def test_late_slot_still_runs_inside_the_grace_window():
    # The point of CATCH_UP: a sibling scan overran minute 31, so SKW1 only gets
    # dispatched at 00:52. Running late beats dropping the week.
    late = datetime.datetime(2026, 8, 10, 0, 52, tzinfo=UTC)
    action, slot = ss.slot_action(SKW1, late, None)
    assert action == ss.RUN
    assert slot == datetime.datetime(2026, 8, 10, 0, 31, tzinfo=UTC)


def test_slot_expires_beyond_the_grace_window():
    action, slot = ss.slot_action(SKW1, datetime.datetime(2026, 8, 10, 1, 30, tzinfo=UTC), None)
    assert action == ss.EXPIRED
    assert slot == datetime.datetime(2026, 8, 10, 0, 31, tzinfo=UTC)


def test_grace_window_stays_below_the_tightest_slot_spacing():
    # LIS1 runs hourly — a grace window of >= 60 min would let a delayed slot
    # overlap the next one.
    assert ss.CATCH_UP < datetime.timedelta(hours=1)


def test_backwards_clock_step_does_not_replay_a_handled_slot():
    fired = datetime.datetime(2026, 8, 12, 9, 23, tzinfo=UTC)
    # NTP correction back into the same hour: the slot is behind `fired`.
    assert ss.slot_action(LIS1, datetime.datetime(2026, 8, 12, 9, 20, tzinfo=UTC), fired)[0] == ss.IDLE


def test_startup_skips_the_slot_it_starts_inside():
    # Restart at 09:23:30 must NOT scan: the predecessor process may still be
    # running that very scan. The next slot (10:23) fires normally.
    start = datetime.datetime(2026, 8, 12, 9, 23, 30, tzinfo=UTC)
    state = ss.initial_slots(start)
    assert ss.slot_action(LIS1, start, state[LIS1.script])[0] == ss.IDLE
    assert ss.slot_action(LIS1, datetime.datetime(2026, 8, 12, 10, 23, tzinfo=UTC), state[LIS1.script])[0] == ss.RUN


# ── 4. Runner dispatch: schedule → the right scan, and only that one ─────────


def _dispatch(now, modules, state=None, parked=frozenset()):
    state = {} if state is None else state
    with mock.patch.object(runner, "is_parked", side_effect=lambda s: s in parked):
        runner.run_due_scanners(modules, state, now)
    return state


def test_only_the_due_scanner_runs():
    modules = _modules()
    # 09:23 is LIS1's hourly slot; 9 is not a 4h hour and it is a Wednesday.
    _dispatch(datetime.datetime(2026, 8, 12, 9, 23, 5, tzinfo=UTC), modules, state=ss.initial_slots(
        datetime.datetime(2026, 8, 12, 9, 22, tzinfo=UTC)
    ))
    assert modules["36_ai_lis1_bot.py"].calls == 1
    assert modules["37_ai_tsm1_bot.py"].calls == 0
    assert modules["38_ai_skw1_bot.py"].calls == 0
    assert modules["39_ai_xsm1_bot.py"].calls == 0


def test_monday_cluster_dispatches_all_four_in_slot_order():
    # Monday 2026-08-10: 00:23 LIS1, 00:29 TSM1 (hour 0 is a 4h hour),
    # 00:31 SKW1, 00:37 XSM1 — the one tick where all four collide.
    modules = _modules()
    state = ss.initial_slots(datetime.datetime(2026, 8, 10, 0, 22, tzinfo=UTC))
    for minute in (23, 29, 31, 37):
        state = _dispatch(datetime.datetime(2026, 8, 10, 0, minute, 2, tzinfo=UTC), modules, state=state)
    assert [modules[s.script].calls for s in ss.SCANNERS] == [1, 1, 1, 1]


def test_a_scan_is_not_repeated_within_its_slot():
    modules = _modules()
    state = ss.initial_slots(datetime.datetime(2026, 8, 12, 9, 22, tzinfo=UTC))
    for second in (0, 10, 20, 30, 40, 50):
        state = _dispatch(datetime.datetime(2026, 8, 12, 9, 23, second, tzinfo=UTC), modules, state=state)
    assert modules["36_ai_lis1_bot.py"].calls == 1


def test_expired_slot_is_skipped_not_scanned():
    modules = _modules()
    # Runner blocked for an hour: the 09:23 slot is long gone when it looks.
    state = {"36_ai_lis1_bot.py": datetime.datetime(2026, 8, 12, 8, 23, tzinfo=UTC)}
    state = _dispatch(datetime.datetime(2026, 8, 12, 10, 10, tzinfo=UTC), modules, state=state)
    assert modules["36_ai_lis1_bot.py"].calls == 0
    # …and the slot counts as handled, so it does not linger into the next tick.
    assert state["36_ai_lis1_bot.py"] == datetime.datetime(2026, 8, 12, 9, 23, tzinfo=UTC)


def test_missing_module_does_not_stop_the_others():
    # One bot file failed to import at startup — the rest must keep scanning.
    modules = _modules()
    del modules["36_ai_lis1_bot.py"]
    state = ss.initial_slots(datetime.datetime(2026, 8, 10, 0, 22, tzinfo=UTC))
    for minute in (23, 29):
        state = _dispatch(datetime.datetime(2026, 8, 10, 0, minute, 2, tzinfo=UTC), modules, state=state)
    assert modules["37_ai_tsm1_bot.py"].calls == 1


# ── 5. Park granularity: the FOUR original markers, checked per bot ──────────


def test_parking_one_bot_silences_only_that_bot():
    modules = _modules()
    state = ss.initial_slots(datetime.datetime(2026, 8, 10, 0, 22, tzinfo=UTC))
    for minute in (23, 29, 31, 37):
        state = _dispatch(
            datetime.datetime(2026, 8, 10, 0, minute, 2, tzinfo=UTC),
            modules,
            state=state,
            parked={"37_ai_tsm1_bot.py"},
        )
    assert modules["37_ai_tsm1_bot.py"].calls == 0
    assert modules["36_ai_lis1_bot.py"].calls == 1
    assert modules["38_ai_skw1_bot.py"].calls == 1
    assert modules["39_ai_xsm1_bot.py"].calls == 1


def test_parking_the_runner_script_alone_does_not_silence_a_bot():
    # Parking the runner is the watchdog's job (the process never starts). A
    # marker under the runner's own name must not be read as "TSM1 is parked" —
    # the check is per bot script, deliberately.
    modules = _modules()
    state = ss.initial_slots(datetime.datetime(2026, 8, 10, 0, 22, tzinfo=UTC))
    state = _dispatch(
        datetime.datetime(2026, 8, 10, 0, 29, 2, tzinfo=UTC),
        modules,
        state=state,
        parked={ss.RUNNER_SCRIPT},
    )
    assert modules["37_ai_tsm1_bot.py"].calls == 1


def test_park_is_checked_per_slot_not_once_per_process():
    # Unparking must take effect at the next slot without restarting the runner.
    modules = _modules()
    state = ss.initial_slots(datetime.datetime(2026, 8, 12, 9, 22, tzinfo=UTC))
    state = _dispatch(
        datetime.datetime(2026, 8, 12, 9, 23, 2, tzinfo=UTC), modules, state=state, parked={"36_ai_lis1_bot.py"}
    )
    assert modules["36_ai_lis1_bot.py"].calls == 0
    state = _dispatch(datetime.datetime(2026, 8, 12, 10, 23, 2, tzinfo=UTC), modules, state=state)
    assert modules["36_ai_lis1_bot.py"].calls == 1


def test_parked_slot_is_consumed_not_deferred():
    # A parked bot misses its window, exactly like a parked process does — it
    # must not scan the moment it is unparked mid-slot.
    modules = _modules()
    state = ss.initial_slots(datetime.datetime(2026, 8, 12, 9, 22, tzinfo=UTC))
    state = _dispatch(
        datetime.datetime(2026, 8, 12, 9, 23, 2, tzinfo=UTC), modules, state=state, parked={"36_ai_lis1_bot.py"}
    )
    state = _dispatch(datetime.datetime(2026, 8, 12, 9, 40, tzinfo=UTC), modules, state=state)
    assert modules["36_ai_lis1_bot.py"].calls == 0


# ── 6. Error isolation (bot-29 pattern) ─────────────────────────────────────


def test_one_exploding_scan_does_not_take_the_others_down():
    modules = _modules(**{"38_ai_skw1_bot.py": True})
    state = ss.initial_slots(datetime.datetime(2026, 8, 10, 0, 22, tzinfo=UTC))
    for minute in (23, 29, 31, 37):
        state = _dispatch(datetime.datetime(2026, 8, 10, 0, minute, 2, tzinfo=UTC), modules, state=state)
    assert modules["38_ai_skw1_bot.py"].calls == 1  # it did run, and it did raise
    assert modules["36_ai_lis1_bot.py"].calls == 1
    assert modules["37_ai_tsm1_bot.py"].calls == 1
    assert modules["39_ai_xsm1_bot.py"].calls == 1


def test_all_four_exploding_still_leaves_the_loop_alive():
    modules = _modules(**{s.script: True for s in ss.SCANNERS})
    state = ss.initial_slots(datetime.datetime(2026, 8, 10, 0, 22, tzinfo=UTC))
    for minute in (23, 29, 31, 37):
        state = _dispatch(datetime.datetime(2026, 8, 10, 0, minute, 2, tzinfo=UTC), modules, state=state)
    assert [m.calls for m in modules.values()] == [1, 1, 1, 1]


def test_a_failed_scan_is_not_retried_in_a_hot_loop():
    # The slot is marked handled BEFORE the scan runs: a persistently failing
    # scanner burns one slot, not every tick.
    modules = _modules(**{"36_ai_lis1_bot.py": True})
    state = ss.initial_slots(datetime.datetime(2026, 8, 12, 9, 22, tzinfo=UTC))
    for second in (2, 12, 22, 32):
        state = _dispatch(datetime.datetime(2026, 8, 12, 9, 23, second, tzinfo=UTC), modules, state=state)
    assert modules["36_ai_lis1_bot.py"].calls == 1


def test_failed_import_leaves_nothing_behind_in_sys_modules(tmp_path, monkeypatch):
    # load_scanner registers the module before exec_module (CPython's own order,
    # so a self-import finds the partial object). If exec then raises, the
    # half-initialised module must not survive under the bot's logger name —
    # anything importing that name later would get the broken object instead of
    # an error, and the failure would look like a working scanner.
    broken = tmp_path / "zz_broken_bot.py"
    broken.write_text("raise RuntimeError('boom during import')\n", encoding="utf-8")
    spec = ss.ScannerSpec("zz_broken_bot.py", "ZZ_BROKEN_BOT_UNDER_TEST", ss.HOURLY, minute=5)
    monkeypatch.setattr(runner, "_HERE", str(tmp_path))

    with pytest.raises(RuntimeError):
        runner.load_scanner(spec)
    assert spec.logger_name not in sys.modules


# ── 7. Report identity survives the consolidation ───────────────────────────


@pytest.mark.parametrize(
    ("tag", "script"),
    [
        ("LIS1", "36_ai_lis1_bot.py"),
        ("TSM1", "37_ai_tsm1_bot.py"),
        ("SKW1", "38_ai_skw1_bot.py"),
        ("XSM1", "39_ai_xsm1_bot.py"),
        ("XSR1", "39_ai_xsm1_bot.py"),
    ],
)
def test_tags_still_map_onto_their_bot_not_onto_the_runner(tag, script):
    assert bc.script_for_tag(tag) == script


def test_hosted_bots_count_as_active_while_the_runner_runs():
    with mock.patch.object(bc, "list_parked", return_value=set()):
        active = bc.active_scripts()
    assert ss.RUNNER_SCRIPT in active
    for script in ss.HOSTED_SCRIPTS:
        assert script in active
    assert bc.is_bot_active("TSM1", active) is True


def test_parking_a_hosted_bot_marks_only_that_tag_inactive():
    with mock.patch.object(bc, "list_parked", return_value={"37_ai_tsm1_bot.py"}):
        active = bc.active_scripts()
    assert "37_ai_tsm1_bot.py" not in active
    assert "38_ai_skw1_bot.py" in active
    assert bc.is_bot_active("TSM1", active) is False
    assert bc.is_bot_active("SKW1", active) is True


def test_parking_the_runner_marks_all_four_inactive():
    with mock.patch.object(bc, "list_parked", return_value={ss.RUNNER_SCRIPT}):
        active = bc.active_scripts()
    for script in ss.HOSTED_SCRIPTS:
        assert script not in active
    for tag in ("LIS1", "TSM1", "SKW1", "XSM1", "XSR1"):
        assert bc.is_bot_active(tag, active) is False


def test_runner_reports_the_families_of_the_bots_it_hosts():
    # Roster order (36, 37, 38, 39), each bot's own family order preserved.
    assert bc.families_for_script(ss.RUNNER_SCRIPT) == ["LIS", "TSM", "SKW", "XSM", "XSR"]


def test_expand_hosted_is_a_no_op_without_the_runner():
    # Pure set arithmetic, and inert when the runner is not active: parking the
    # runner (or a fleet without it) must not conjure the four bots into a
    # report's active set.
    scripts = {"11_ai_mis_bot.py"}
    assert ss.expand_hosted(scripts, set()) == scripts
    assert scripts == {"11_ai_mis_bot.py"}  # input untouched


# ── 8. The tools/ audit mirror resolves activity identically ────────────────
#
# tools/fleet_realized_audit.resolve_active_scripts() exists only because the
# audit needs a parked-dir override (worktree looking at the LIVE checkout's
# control/parked). It must NOT be a second, drifting definition of "active":
# both sides go through shadow_scanners.expand_hosted, and this pins that they
# agree — park-marker subtraction included. Before the expansion reached the
# mirror, every TSM1/SKW1/XSM1/XSR1 leg was reported "inactive" while the runner
# was scanning them.


@pytest.mark.parametrize(
    "parked",
    [
        set(),  # nothing parked → all four hosted bots count as active
        {"37_ai_tsm1_bot.py"},  # one bot parked → only that one drops out
        {ss.RUNNER_SCRIPT},  # runner parked → all four drop out
        {ss.RUNNER_SCRIPT, "38_ai_skw1_bot.py"},  # both markers at once
        {"11_ai_mis_bot.py"},  # unrelated bot parked → hosted set unaffected
    ],
    ids=["none", "one_hosted", "runner", "runner_and_hosted", "unrelated"],
)
def test_audit_mirror_matches_bot_catalog_on_the_hosted_expansion(tmp_path, parked):
    from tools.fleet_realized_audit import resolve_active_scripts

    for name in parked:
        (tmp_path / name).touch()

    with mock.patch.object(bc, "list_parked", return_value=set(parked)):
        canonical = bc.active_scripts()

    assert resolve_active_scripts(str(tmp_path)) == canonical


def test_audit_mirror_keeps_the_hosted_bots_out_of_the_inactive_bucket(tmp_path):
    # The MEDIUM this section was written for: the audit's lifecycle bucket asks
    # "is the emitting script active?", and the emitting script of a TSM1 leg is
    # bot 37 — which has no fleet entry anymore.
    from tools.fleet_realized_audit import resolve_active_scripts

    active = resolve_active_scripts(str(tmp_path))
    for script in ss.HOSTED_SCRIPTS:
        assert script in active

    (tmp_path / "37_ai_tsm1_bot.py").touch()
    parked_one = resolve_active_scripts(str(tmp_path))
    assert "37_ai_tsm1_bot.py" not in parked_one
    assert "38_ai_skw1_bot.py" in parked_one
