# core/signal_consumers.py — roster and cadence arithmetic of the five signal
# consumers that share ONE process (46_signal_consumer_runner.py,
# T-2026-KYT-9050-135).
#
# Why this module exists: bots 9 (SRA), 15 (AIM2), 30 (PEX1), 33 (FIF1) and 43
# (FIF2) were five permanent interpreters — each with its own pandas/numpy
# import and its own minconn-2 DB pool — for one thing: polling a table. They
# scan no coins and read no candle windows of their own; they wake up every
# 60 s (9: every 300 s), ask the DB "anything new?" and go back to sleep. One
# process can drive all five on exactly those cadences.
#
# The poll logic itself is NOT moved: the runner imports the five bot modules
# and calls their ``run_poll()`` — the body of their own ``while True`` loop,
# unchanged — when a bot is due. What lives here is only the part the runner,
# core/bot_catalog.py and the tests need to agree on:
#
#   * the roster — which script names the runner hosts. The five scripts keep
#     their identity as *bots*: report mapping (core/bot_catalog.py) and the
#     operator's park markers (control/parked/<script>.py) stay per bot, they
#     just no longer own a process. The hosting itself is registered in
#     core/hosted_fleet.py, shared with the shadow-scanner runner.
#   * the cadence arithmetic — pure, DB-free, clock-injectable, so "when is this
#     poll due" is testable without sleeping and without Postgres.
#
# Deliberately NOT modelled as slots (the shadow-scanner substrate does that):
# these five have no scheduled minute. Their contract is "at most N seconds
# between polls", so the schedule is a due time per bot, re-armed from the
# moment a poll FINISHES — exactly what ``while True: poll(); sleep(N)`` did in
# each of the five processes.

from __future__ import annotations

from dataclasses import dataclass

RUNNER_SCRIPT = "46_signal_consumer_runner.py"


@dataclass(frozen=True)
class ConsumerSpec:
    """One hosted signal consumer.

    ``script``      original fleet script name — the park-marker identity, the
                    file the runner imports, and the report mapping key.
    ``logger_name`` the module name the runner imports the file under. The bot
                    modules do ``logger = logging.getLogger(__name__)``, so this
                    string IS the log prefix; it is set to the prefix the bot
                    used as a standalone process ("PEX1_BOT", …) so the log
                    lines keep their shape.
    ``interval``    seconds between the END of one poll and the start of the
                    next — the bot's own ``time.sleep(...)`` value, verbatim.
    """

    script: str
    logger_name: str
    interval: float


# Order = dispatch order within a pass. It follows the ascending fleet
# start_delay the five entries used to carry (31 / 79 / 191 / 215 / 291), so on
# a tick where several are due at once they run in the sequence five separate
# processes would have started in.
CONSUMERS: tuple[ConsumerSpec, ...] = (
    # 9: S&R candidates from active_trades_master, 300 s. Reloads both direction
    #    artifacts per poll (maybe_reload, 24 h internally).
    ConsumerSpec("9_ai_sr_bot.py", "AI_SR_BOT", 300.0),
    # 15: AIM2 master gate over the whole fleet signal stream, 60 s + a 24 h
    #     model reload. LIVE posting path (AIM2_LIVE_POSTING).
    ConsumerSpec("15_ai_master_bot.py", "AI_MASTER_BOT", 60.0),
    # 30: pump_dump_events behind a watermark, 60 s.
    ConsumerSpec("30_ai_pex1_bot.py", "PEX1_BOT", 60.0),
    # 33: FIFO signals from active/closed_trades_master behind a seen-set, 60 s.
    #     LIVE posting path (NEW_IDEAS_LIVE_POSTING).
    ConsumerSpec("33_ai_fif1_bot.py", "FIF1_BOT", 60.0),
    # 43: fresh ai_signals rows behind a vol gate with JSON state, 60 s. Feeds
    #     the trailing roster, i.e. the money path via bot 40.
    ConsumerSpec("43_ai_fif2_bot.py", "FIF2_BOT", 60.0),
)

# Script names the runner hosts — registered in core/hosted_fleet.py, which is
# what core/bot_catalog.py and main_watchdog.py read.
HOSTED_SCRIPTS: tuple[str, ...] = tuple(spec.script for spec in CONSUMERS)

# Dispatch is SEQUENTIAL, so a poll that overruns delays its siblings. The grace
# rule, and why it is a delay and never a drop:
#
#   * A due time is re-armed from the moment a poll FINISHES, never from the
#     moment it was due. A backlog therefore cannot accumulate: one pass runs
#     each due bot at most once, and a slow sibling shifts the others later
#     instead of queueing extra polls behind them. That is the same property
#     ``while True: poll(); sleep(N)`` had per process — the effective period
#     was always ``N + poll duration``, never exactly N.
#   * What IS new is that a bot's period now also absorbs the polls of the
#     siblings dispatched before it. That is the honest cost of one process:
#     period(bot) ≈ interval + own duration + duration of the siblings that
#     become due in the same pass. In the measured shape of these five (each
#     poll is a handful of indexed queries plus, for 15/33, a model predict on a
#     handful of rows) that sum is seconds, not minutes.
#   * The tightest freshness bound in the cluster is FIF2's MAX_MIRROR_AGE_S
#     (240 s): a mirror older than that is dropped by the bot itself. So a pass
#     that regularly took minutes would silently cost FIF2 mirrors — which is
#     why lateness is not just tolerated but LOGGED (see LATE_WARNING_S).
#
# No starvation is possible under this rule, but slow degradation is, and it
# must not be silent.
LATE_WARNING_S = 30.0

# Upper bound on one idle sleep. The loop sleeps until the next due time, so it
# does not spin; the cap only keeps the process reacting to a stop signal within
# a few seconds instead of sitting in a 300 s sleep.
MAX_SLEEP_S = 5.0


def initial_due(now: float) -> dict[str, float]:
    """Start state: every consumer is due immediately.

    Unlike the shadow-scanner runner (which skips the slot it starts inside,
    because a scan is tied to a wall-clock minute a predecessor process may be
    running right now), an immediate first poll is the FAITHFUL behaviour here:
    all five bots polled once directly after their own startup. It is also the
    safe direction, because the anti-replay state each of these bots needs is
    built in its own startup and not by skipping a cycle — bot 30 reads the
    watermark from ``MAX(spike_time)``, bot 33 marks the current window as seen,
    bot 43 enters its cycle in bootstrap mode, bots 9/15 dedup in the DB. A
    skipped first poll would not add safety, it would only lose a minute.
    """
    return {spec.script: now for spec in CONSUMERS}


def due_specs(state: dict[str, float], now: float) -> list[ConsumerSpec]:
    """Consumers whose next poll is due at ``now``, in dispatch order.

    An unknown script counts as due — a consumer that somehow missed the
    initial arming must poll, not go silent.
    """
    return [spec for spec in CONSUMERS if now >= state.get(spec.script, now)]


def reschedule(spec: ConsumerSpec, state: dict[str, float], finished_at: float) -> None:
    """Re-arm ``spec`` one interval after the poll that just finished."""
    state[spec.script] = finished_at + spec.interval


def lateness(spec: ConsumerSpec, state: dict[str, float], now: float) -> float:
    """How long past its due time this poll is starting (seconds, >= 0)."""
    return max(0.0, now - state.get(spec.script, now))


def sleep_seconds(state: dict[str, float], now: float) -> float:
    """Seconds to sleep before the next due poll, capped at MAX_SLEEP_S.

    Clamped at 0 so an already-overdue consumer is dispatched on the next loop
    iteration instead of the loop sleeping a negative amount (and at MAX_SLEEP_S
    so the process stays responsive). Everything is measured on a MONOTONIC
    clock in the runner, so an NTP step or a DST change cannot move a due time.
    """
    if not state:
        return MAX_SLEEP_S
    return max(0.0, min(MAX_SLEEP_S, min(state.values()) - now))
