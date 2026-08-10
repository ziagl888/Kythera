# backtest/test_signal_consumer_runner.py
"""DB-free tests for the shared signal-consumer runner (T-2026-KYT-9050-135).

Bots 9 (SRA), 15 (AIM2), 30 (PEX1), 33 (FIF1) and 43 (FIF2) gave up their own
processes and now poll inside ``46_signal_consumer_runner.py``. Three of the
five reach real money (15 and 33 post live, 43 fills a trailing roster seat), so
the consolidation is only allowed to change WHERE the poll runs, never WHETHER,
WHEN or WITH WHAT STATE. The properties that made five processes safe are pinned
here:

  1. **Cadence** — every consumer still polls on its own interval (60 s, bot 9
     300 s), re-armed from the END of a poll exactly like ``poll(); sleep(N)``
     did, so a slow poll delays its siblings instead of queueing a backlog.
  2. **Arming** — a bot polls only after its ``startup()`` succeeded. Bots 30/33
     build their anti-replay state there (watermark / seen-set); polling without
     it would repost a window that has already been posted.
  3. **Park granularity** — the runner checks the FIVE original park markers
     (``control/parked/<script>.py``) per bot before each poll, so the operator
     can still silence a single bot.
  4. **Error isolation** — an exception in one startup or poll does not stop the
     other four, and a failing consumer is not retried in a hot loop.

Plus the report-side invariant that the five keep their identity in
``core/bot_catalog.py`` even though they no longer have a fleet entry, and that
the hosted expansion is ONE shared implementation rather than a second mirror
next to the shadow-scanner one.

No DB, no sleeping, no filesystem: the cadence arithmetic is pure, the clock is
injected, the bot modules are fakes and ``is_parked`` is patched.

Run: pytest backtest/test_signal_consumer_runner.py -v
"""

from __future__ import annotations

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
from core import hosted_fleet as hf  # noqa: E402
from core import signal_consumers as sc  # noqa: E402


def _import_runner():
    path = os.path.join(REPO_ROOT, "46_signal_consumer_runner.py")
    spec = importlib.util.spec_from_file_location("consumer_runner_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["consumer_runner_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


runner = _import_runner()

SRA, AIM2, PEX1, FIF1, FIF2 = sc.CONSUMERS


class _Clock:
    """Monotonic stand-in: every read advances by ``step`` seconds."""

    def __init__(self, now: float = 1000.0, step: float = 0.0):
        self.now = now
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeBot:
    """Stand-in for a bot module: records calls, optionally explodes."""

    def __init__(self, boom: bool = False, startup_boom: bool = False, duration: float = 0.0, clock=None):
        self.polls = 0
        self.startups = 0
        self.boom = boom
        self.startup_boom = startup_boom
        self.duration = duration
        self.clock = clock

    def startup(self) -> None:
        self.startups += 1
        if self.startup_boom:
            raise RuntimeError("startup exploded")

    def run_poll(self) -> None:
        self.polls += 1
        if self.duration and self.clock is not None:
            self.clock.advance(self.duration)
        if self.boom:
            raise RuntimeError("poll exploded")


def _modules(**kwargs) -> dict[str, _FakeBot]:
    """One fake per consumer; kwargs override a single script's constructor."""
    return {spec.script: _FakeBot(**kwargs.get(spec.script, {})) for spec in sc.CONSUMERS}


def _dispatch(modules, state, armed, clock, parked=frozenset()):
    with mock.patch.object(runner, "is_parked", side_effect=lambda s: s in parked):
        runner.run_due_consumers(modules, state, armed, clock=clock)


def _run_rounds(modules, state, armed, clock, rounds, parked=frozenset()):
    """The runner's loop, minus the sleeping: dispatch, jump to the next due."""
    for _ in range(rounds):
        _dispatch(modules, state, armed, clock, parked)
        nxt = min(state.values())
        if nxt > clock():
            clock.advance(nxt - clock())


# ── 1. Roster ────────────────────────────────────────────────────────────────


def test_roster_covers_exactly_the_five_consolidated_bots():
    assert sc.HOSTED_SCRIPTS == (
        "9_ai_sr_bot.py",
        "15_ai_master_bot.py",
        "30_ai_pex1_bot.py",
        "33_ai_fif1_bot.py",
        "43_ai_fif2_bot.py",
    )
    assert sc.RUNNER_SCRIPT == "46_signal_consumer_runner.py"


@pytest.mark.parametrize("spec", sc.CONSUMERS, ids=lambda s: s.script)
def test_hosted_bot_files_expose_the_two_entry_points(spec):
    # The runner calls startup()/run_poll() on the unchanged module — a rename
    # in a bot file would otherwise only surface at the next poll in production.
    source = open(os.path.join(REPO_ROOT, spec.script), encoding="utf-8").read()
    assert "def startup(" in source
    assert "def run_poll(" in source


@pytest.mark.parametrize("spec", sc.CONSUMERS, ids=lambda s: s.script)
def test_logger_name_matches_the_bots_own_log_prefix(spec):
    # The runner imports each module under spec.logger_name and logs with
    # %(name)s, so this string is what the poll lines are prefixed with. It must
    # stay the prefix the standalone bot used, or the log format silently drifts.
    source = open(os.path.join(REPO_ROOT, spec.script), encoding="utf-8").read()
    assert f"- {spec.logger_name} -" in source


@pytest.mark.parametrize("spec", sc.CONSUMERS, ids=lambda s: s.script)
def test_interval_matches_the_bots_own_sleep(spec):
    # The cadence is restated in the spec table; the bot still carries its own
    # sleep in main(). A drift between the two is the whole failure mode of
    # "behaviour-neutral" — pin the sleep the standalone loop uses.
    source = open(os.path.join(REPO_ROOT, spec.script), encoding="utf-8").read()
    literal = f"time.sleep({int(spec.interval)})"
    named = "time.sleep(POLL_SECONDS)"  # bot 43 names its 60 s constant
    assert literal in source or (named in source and f"POLL_SECONDS = {int(spec.interval)}" in source)


@pytest.mark.parametrize("spec", sc.CONSUMERS, ids=lambda s: s.script)
def test_main_still_drives_startup_then_the_poll_loop(spec):
    # The bots stay runnable standalone: main() is the same loop, only its body
    # now lives in run_poll(). If someone deletes main(), the debugging path
    # ("python 33_ai_fif1_bot.py") is gone without anyone noticing.
    source = open(os.path.join(REPO_ROOT, spec.script), encoding="utf-8").read()
    assert "    startup()\n    while True:\n        run_poll()\n        time.sleep(" in source


def test_model_reload_stays_inside_the_poll_of_bots_9_and_15():
    # Constraint 4 of the task: the 24 h artifact reload must survive the move.
    # It only survives if it sits in run_poll — hoisting it into startup would
    # pin the runner to the artifacts it booted with, and a deploy would need a
    # fleet restart instead of a day.
    sra = open(os.path.join(REPO_ROOT, "9_ai_sr_bot.py"), encoding="utf-8").read()
    aim2 = open(os.path.join(REPO_ROOT, "15_ai_master_bot.py"), encoding="utf-8").read()
    assert "maybe_reload" in sra.split("def run_poll(")[1].split("\ndef ")[0]
    aim2_poll = aim2.split("def run_poll(")[1].split("\ndef ")[0]
    assert "MODEL_RELOAD_S" in aim2_poll and "load_model()" in aim2_poll


# ── 2. Cadence arithmetic ────────────────────────────────────────────────────


def test_every_consumer_is_due_immediately_at_startup():
    # Unlike the shadow-scanner runner (which skips the slot it starts inside),
    # an immediate first poll is the faithful behaviour: all five bots polled
    # right after their own startup, and their anti-replay state comes from
    # startup(), not from skipping a cycle.
    state = sc.initial_due(1000.0)
    assert {s.script for s in sc.due_specs(state, 1000.0)} == set(sc.HOSTED_SCRIPTS)


def test_due_order_is_the_fleet_order():
    state = sc.initial_due(1000.0)
    assert [s.script for s in sc.due_specs(state, 1000.0)] == list(sc.HOSTED_SCRIPTS)


def test_reschedule_arms_one_interval_after_the_poll_finished():
    state = sc.initial_due(1000.0)
    sc.reschedule(FIF1, state, 1005.0)  # poll took 5s
    assert state[FIF1.script] == 1065.0
    # …i.e. the period is interval + poll duration, exactly like poll(); sleep(N)
    assert FIF1 not in sc.due_specs(state, 1064.9)
    assert FIF1 in sc.due_specs(state, 1065.0)


def test_a_slow_poll_does_not_queue_a_backlog_behind_itself():
    # The property that rules out catch-up storms: the due time is computed from
    # the END of the poll, so a poll that overran by 10 intervals still owes
    # exactly ONE next poll, not eleven.
    overran_at = 1000.0 + 10 * AIM2.interval
    state = sc.initial_due(1000.0)
    sc.reschedule(AIM2, state, overran_at)
    assert AIM2 not in sc.due_specs(state, overran_at)
    assert AIM2 in sc.due_specs(state, overran_at + AIM2.interval)


def test_bot_9_keeps_its_five_minute_cadence():
    assert SRA.interval == 300.0
    assert {s.interval for s in (AIM2, PEX1, FIF1, FIF2)} == {60.0}


def test_sleep_is_bounded_and_never_negative():
    state = sc.initial_due(1000.0)
    # everything overdue → do not sleep at all
    assert sc.sleep_seconds(state, 2000.0) == 0.0
    for spec in sc.CONSUMERS:
        sc.reschedule(spec, state, 1000.0)
    # next due is 60s away, but one sleep is capped so the loop stays responsive
    assert sc.sleep_seconds(state, 1000.0) == sc.MAX_SLEEP_S
    assert sc.sleep_seconds({}, 1000.0) == sc.MAX_SLEEP_S


def test_lateness_is_measured_against_the_due_time():
    state = sc.initial_due(1000.0)
    assert sc.lateness(FIF2, state, 1000.0) == 0.0
    assert sc.lateness(FIF2, state, 1042.0) == 42.0
    # An unknown script is never "late" — it is simply due.
    assert sc.lateness(FIF2, {}, 1042.0) == 0.0


def test_late_warning_stays_below_the_tightest_freshness_bound():
    # FIF2 drops mirrors older than MAX_MIRROR_AGE_S (240s). A warning threshold
    # at or above that would only fire once mirrors are already being lost.
    fif2 = open(os.path.join(REPO_ROOT, "43_ai_fif2_bot.py"), encoding="utf-8").read()
    max_age = int(fif2.split("MAX_MIRROR_AGE_S = ")[1].split()[0])
    assert sc.LATE_WARNING_S < max_age


# ── 3. Dispatch: the right bot polls, and only when it is due ────────────────


def test_only_due_consumers_poll():
    clock = _Clock(1000.0)
    modules, state, armed = _modules(), sc.initial_due(1000.0), set()
    _dispatch(modules, state, armed, clock)
    assert [modules[s.script].polls for s in sc.CONSUMERS] == [1, 1, 1, 1, 1]

    # 60s later the four minute-pollers are due again, bot 9 is not.
    clock.advance(60.0)
    _dispatch(modules, state, armed, clock)
    assert modules[SRA.script].polls == 1
    assert [modules[s.script].polls for s in (AIM2, PEX1, FIF1, FIF2)] == [2, 2, 2, 2]

    # …and at 300s bot 9 joins the pass.
    clock.advance(240.0)
    _dispatch(modules, state, armed, clock)
    assert modules[SRA.script].polls == 2


def test_a_poll_is_not_repeated_within_its_interval():
    clock = _Clock(1000.0)
    modules, state, armed = _modules(), sc.initial_due(1000.0), set()
    for _ in range(6):
        _dispatch(modules, state, armed, clock)
        clock.advance(5.0)
    assert modules[FIF1.script].polls == 1


def test_a_slow_sibling_delays_but_never_starves_the_others():
    # AIM2's poll takes 150s — two and a half of its own intervals. Over four
    # rounds every minute-poller still gets exactly its four turns (no
    # starvation, no lock-out of the slow one either); what they lose is
    # punctuality, and that is what the WARNING below is for.
    clock = _Clock(1000.0)
    modules = _modules()
    modules[AIM2.script] = _FakeBot(duration=150.0, clock=clock)
    state, armed = sc.initial_due(1000.0), set()
    _run_rounds(modules, state, armed, clock, rounds=6)
    counts = {spec.script: modules[spec.script].polls for spec in (AIM2, PEX1, FIF1, FIF2)}
    assert len(set(counts.values())) == 1, counts  # lockstep: nobody falls behind
    assert min(counts.values()) >= 4, counts  # …and nobody is locked out either
    assert modules[SRA.script].polls >= 2  # 300s cadence over ~1400s of sim time


def test_a_late_poll_is_never_silent(caplog):
    # The honest-degradation rule: lateness is tolerated (running late beats
    # dropping a poll) but it has to be visible before it eats FIF2's mirror
    # window.
    clock = _Clock(1000.0)
    modules = _modules()
    modules[SRA.script] = _FakeBot(duration=sc.LATE_WARNING_S + 10.0, clock=clock)
    state, armed = sc.initial_due(1000.0), set()
    with caplog.at_level("WARNING", logger="CONSUMER_RUNNER"):
        _dispatch(modules, state, armed, clock)
    late = [r for r in caplog.records if "late" in r.message]
    # SRA runs first and overruns, so the four behind it start late — and each
    # of them says so.
    assert {r.message.split(":")[0].split()[-1] for r in late} == {
        AIM2.logger_name,
        PEX1.logger_name,
        FIF1.logger_name,
        FIF2.logger_name,
    }


def test_missing_module_does_not_stop_the_others():
    # One bot file failed to import at startup — the rest must keep polling.
    clock = _Clock(1000.0)
    modules = _modules()
    del modules[SRA.script]
    state, armed = sc.initial_due(1000.0), set()
    _dispatch(modules, state, armed, clock)
    assert modules[AIM2.script].polls == 1
    # …and the missing bot is rescheduled, so it does not spin every iteration.
    assert state[SRA.script] == 1000.0 + SRA.interval


# ── 4. Arming: no poll before a successful startup ──────────────────────────


def test_startup_runs_once_and_then_never_again():
    clock = _Clock(1000.0)
    modules, state, armed = _modules(), sc.initial_due(1000.0), set()
    for _ in range(3):
        _dispatch(modules, state, armed, clock)
        clock.advance(60.0)
    assert modules[FIF1.script].startups == 1
    assert modules[FIF1.script].polls == 3


def test_a_failed_startup_blocks_that_bots_poll_and_is_retried():
    # The money-path property: bots 30/33 build their anti-replay state in
    # startup. Polling an unarmed bot would repost a whole window.
    clock = _Clock(1000.0)
    modules = _modules()
    modules[FIF1.script] = _FakeBot(startup_boom=True)
    state, armed = sc.initial_due(1000.0), set()
    _dispatch(modules, state, armed, clock)
    assert modules[FIF1.script].startups == 1
    assert modules[FIF1.script].polls == 0
    assert FIF1.script not in armed
    # The other four are unaffected…
    assert modules[PEX1.script].polls == 1
    # …and the next cycle tries the startup again (the DB may be back).
    modules[FIF1.script].startup_boom = False
    clock.advance(60.0)
    _dispatch(modules, state, armed, clock)
    assert modules[FIF1.script].startups == 2
    assert modules[FIF1.script].polls == 1


def test_a_bot_parked_at_boot_arms_only_when_it_is_unparked():
    # Arming lazily is what makes this correct: FIF1 unparked after two hours
    # must bootstrap its seen-set from the CURRENT window, not from boot time.
    clock = _Clock(1000.0)
    modules, state, armed = _modules(), sc.initial_due(1000.0), set()
    _dispatch(modules, state, armed, clock, parked={FIF1.script})
    assert modules[FIF1.script].startups == 0
    clock.advance(7200.0)
    _dispatch(modules, state, armed, clock)
    assert modules[FIF1.script].startups == 1
    assert modules[FIF1.script].polls == 1


# ── 5. Park granularity: the FIVE original markers, checked per cycle ────────


def test_parking_one_bot_silences_only_that_bot():
    clock = _Clock(1000.0)
    modules, state, armed = _modules(), sc.initial_due(1000.0), set()
    _dispatch(modules, state, armed, clock, parked={AIM2.script})
    assert modules[AIM2.script].polls == 0
    assert [modules[s.script].polls for s in (SRA, PEX1, FIF1, FIF2)] == [1, 1, 1, 1]


def test_parking_the_runner_script_alone_does_not_silence_a_bot():
    # Parking the runner is the watchdog's job (the process never starts). A
    # marker under the runner's own name must not be read as "AIM2 is parked" —
    # the check is per bot script, deliberately.
    clock = _Clock(1000.0)
    modules, state, armed = _modules(), sc.initial_due(1000.0), set()
    _dispatch(modules, state, armed, clock, parked={sc.RUNNER_SCRIPT})
    assert modules[AIM2.script].polls == 1


def test_park_is_checked_per_cycle_not_once_per_process():
    # Unparking must take effect on the next cycle without restarting the runner.
    clock = _Clock(1000.0)
    modules, state, armed = _modules(), sc.initial_due(1000.0), set()
    _dispatch(modules, state, armed, clock, parked={PEX1.script})
    assert modules[PEX1.script].polls == 0
    clock.advance(60.0)
    _dispatch(modules, state, armed, clock)
    assert modules[PEX1.script].polls == 1


def test_a_parked_cycle_is_consumed_not_deferred():
    # A parked bot misses its cycle exactly like a parked process does — it must
    # not poll the moment it is unparked mid-interval.
    clock = _Clock(1000.0)
    modules, state, armed = _modules(), sc.initial_due(1000.0), set()
    _dispatch(modules, state, armed, clock, parked={PEX1.script})
    clock.advance(30.0)
    _dispatch(modules, state, armed, clock)
    assert modules[PEX1.script].polls == 0


# ── 6. Error isolation (bot-29 pattern) ─────────────────────────────────────


def test_one_exploding_poll_does_not_take_the_others_down():
    clock = _Clock(1000.0)
    modules = _modules()
    modules[FIF2.script] = _FakeBot(boom=True)
    state, armed = sc.initial_due(1000.0), set()
    _dispatch(modules, state, armed, clock)
    assert modules[FIF2.script].polls == 1  # it did run, and it did raise
    assert [modules[s.script].polls for s in (SRA, AIM2, PEX1, FIF1)] == [1, 1, 1, 1]


def test_all_five_exploding_still_leaves_the_loop_alive():
    clock = _Clock(1000.0)
    modules = {spec.script: _FakeBot(boom=True) for spec in sc.CONSUMERS}
    state, armed = sc.initial_due(1000.0), set()
    _dispatch(modules, state, armed, clock)  # must not raise
    assert [m.polls for m in modules.values()] == [1, 1, 1, 1, 1]


def test_a_failed_poll_is_not_retried_in_a_hot_loop():
    # A persistently failing consumer burns one cycle per interval, not every
    # loop iteration — the finally-reschedule is what guarantees that.
    clock = _Clock(1000.0)
    modules = _modules()
    modules[PEX1.script] = _FakeBot(boom=True)
    state, armed = sc.initial_due(1000.0), set()
    for _ in range(5):
        _dispatch(modules, state, armed, clock)
        clock.advance(1.0)
    assert modules[PEX1.script].polls == 1


def test_an_exploding_poll_stays_armed():
    # startup() must not re-run after a poll failure: for bot 33 that would
    # rebuild the seen-set and for 43 reset the mirror dedup mid-flight.
    clock = _Clock(1000.0)
    modules = _modules()
    modules[FIF1.script] = _FakeBot(boom=True)
    state, armed = sc.initial_due(1000.0), set()
    for _ in range(3):
        _dispatch(modules, state, armed, clock)
        clock.advance(60.0)
    assert modules[FIF1.script].startups == 1
    assert modules[FIF1.script].polls == 3


def test_failed_import_leaves_nothing_behind_in_sys_modules(tmp_path, monkeypatch):
    # load_consumer registers the module before exec_module (CPython's own
    # order, so a self-import finds the partial object). If exec then raises,
    # the half-initialised module must not survive under the bot's logger name —
    # anything importing that name later would get the broken object instead of
    # an error, and the failure would look like a working consumer.
    broken = tmp_path / "zz_broken_consumer.py"
    broken.write_text("raise RuntimeError('boom during import')\n", encoding="utf-8")
    spec = sc.ConsumerSpec("zz_broken_consumer.py", "ZZ_BROKEN_CONSUMER_UNDER_TEST", 60.0)
    monkeypatch.setattr(runner, "_HERE", str(tmp_path))

    with pytest.raises(RuntimeError):
        runner.load_consumer(spec)
    assert spec.logger_name not in sys.modules


# ── 7. Report identity survives the consolidation ───────────────────────────


@pytest.mark.parametrize(
    ("tag", "script"),
    [
        ("SRA1", "9_ai_sr_bot.py"),
        ("SRA2", "9_ai_sr_bot.py"),
        ("AIM2", "15_ai_master_bot.py"),
        ("AIM2-TOPN", "15_ai_master_bot.py"),
        ("PEX1", "30_ai_pex1_bot.py"),
        ("FIF1", "33_ai_fif1_bot.py"),
        ("FIF2", "43_ai_fif2_bot.py"),
    ],
)
def test_tags_still_map_onto_their_bot_not_onto_the_runner(tag, script):
    assert bc.script_for_tag(tag) == script


def test_hosted_bots_count_as_active_while_the_runner_runs():
    with mock.patch.object(bc, "list_parked", return_value=set()):
        active = bc.active_scripts()
    assert sc.RUNNER_SCRIPT in active
    for script in sc.HOSTED_SCRIPTS:
        assert script in active
    assert bc.is_bot_active("AIM2", active) is True


def test_parking_a_hosted_bot_marks_only_that_tag_inactive():
    with mock.patch.object(bc, "list_parked", return_value={"33_ai_fif1_bot.py"}):
        active = bc.active_scripts()
    assert "33_ai_fif1_bot.py" not in active
    assert "43_ai_fif2_bot.py" in active
    assert bc.is_bot_active("FIF1", active) is False
    assert bc.is_bot_active("FIF2", active) is True


def test_parking_the_runner_marks_all_five_inactive():
    with mock.patch.object(bc, "list_parked", return_value={sc.RUNNER_SCRIPT}):
        active = bc.active_scripts()
    for script in sc.HOSTED_SCRIPTS:
        assert script not in active
    for tag in ("SRA1", "AIM2", "PEX1", "FIF1", "FIF2"):
        assert bc.is_bot_active(tag, active) is False


def test_runner_reports_the_families_of_the_bots_it_hosts():
    # Roster order (9, 15, 30, 33, 43), each bot's own family order preserved.
    assert bc.families_for_script(sc.RUNNER_SCRIPT) == ["SRA", "AIM", "PEX", "FIF", "FIF2"]


# ── 8. One expansion, two runners ───────────────────────────────────────────
#
# The T-133 review killed a second, drifting mirror of the expansion rule. This
# section pins that the generalisation kept it single: both runners are served
# by the same core/hosted_fleet.expand_hosted, the audit mirror included.


def test_both_runners_are_registered_and_disjoint():
    from core import shadow_scanners as ss

    assert set(hf.HOSTED_SCRIPTS_BY_RUNNER) == {ss.RUNNER_SCRIPT, sc.RUNNER_SCRIPT}
    assert set(ss.HOSTED_SCRIPTS).isdisjoint(sc.HOSTED_SCRIPTS)
    assert len(hf.ALL_HOSTED_SCRIPTS) == len(set(hf.ALL_HOSTED_SCRIPTS))
    for script in sc.HOSTED_SCRIPTS:
        assert hf.runner_for(script) == sc.RUNNER_SCRIPT
    assert hf.runner_for("11_ai_mis_bot.py") is None


def test_expansion_of_one_runner_does_not_conjure_the_others_bots():
    from core import shadow_scanners as ss

    expanded = hf.expand_hosted({sc.RUNNER_SCRIPT}, set())
    assert set(sc.HOSTED_SCRIPTS) <= expanded
    assert expanded.isdisjoint(ss.HOSTED_SCRIPTS)


@pytest.mark.parametrize(
    "parked",
    [
        set(),  # nothing parked → all five hosted bots count as active
        {"33_ai_fif1_bot.py"},  # one bot parked → only that one drops out
        {sc.RUNNER_SCRIPT},  # runner parked → all five drop out
        {sc.RUNNER_SCRIPT, "15_ai_master_bot.py"},  # both markers at once
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


def test_audit_mirror_keeps_the_hosted_consumers_out_of_the_inactive_bucket(tmp_path):
    # The MEDIUM this section was written for: the audit's lifecycle bucket asks
    # "is the emitting script active?", and the emitting script of an AIM2 leg is
    # bot 15 — which has no fleet entry anymore.
    from tools.fleet_realized_audit import resolve_active_scripts

    active = resolve_active_scripts(str(tmp_path))
    for script in sc.HOSTED_SCRIPTS:
        assert script in active

    (tmp_path / "15_ai_master_bot.py").touch()
    parked_one = resolve_active_scripts(str(tmp_path))
    assert "15_ai_master_bot.py" not in parked_one
    assert "33_ai_fif1_bot.py" in parked_one
