# core/shadow_scanners.py — roster and schedule of the rule-based shadow scanners
# that share ONE process (45_shadow_scanner_runner.py, T-2026-KYT-9050-133).
#
# Why this module exists: bots 36 (LIS1), 37 (TSM1), 38 (SKW1) and 39 (XSM1/XSR1)
# were four permanent interpreters — each with its own pandas/numpy import and its
# own minconn-2 DB pool — for a few minutes of work per WEEK. They are all
# rule-based (no model artifact), share the same loop shape (while True + minute
# check) and the same load_coins/read_candles path, so one runner can drive all
# four on their existing schedules.
#
# The scan logic itself is NOT moved: the runner imports the hosted bot modules
# unchanged (they already separate ``run_scan()`` from ``main()``) and calls
# ``run_scan()`` when a slot is due. What lives here is only the part both the
# runner and core/bot_catalog.py need to agree on:
#
#   * the roster — which script names the runner hosts. The hosted scripts keep
#     their identity as *bots*: report mapping (core/bot_catalog.py) and the
#     operator's park markers (control/parked/<script>.py) stay per bot, they
#     just no longer own a process.
#   * the schedule — pure, DB-free slot arithmetic, so "when does this scan run"
#     is testable without a clock and without Postgres.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

RUNNER_SCRIPT = "45_shadow_scanner_runner.py"

# How late a slot may still run. The runner dispatches SEQUENTIALLY, so a long
# scan can push its successor past its own minute — on Monday 00:xx all hosted
# slots sit within 18 minutes of each other, and SKW1's weekly 15m sweep is the
# slow one. Without a grace window a sibling overrun would silently DROP a slot
# (for SKW1/XSM1 that means skipping a whole week); with it the scan runs late
# instead. Late is harmless for all hosted scanners: they read closed candles as-of now,
# and every trigger window (day-3 age band, 4h bar, weekly rebalance) is far
# wider than this grace. Must stay below the tightest slot spacing (LIS1: 60
# min), otherwise a delayed slot could overlap the next one.
CATCH_UP = timedelta(minutes=30)

# Cadence vocabulary of :func:`last_slot`. Deliberately three named cases
# instead of a callable per spec: the specs stay comparable data (frozen
# dataclass, useful repr) and the schedule stays one readable function.
HOURLY = "hourly"
EVERY_4H = "4h"
WEEKLY = "weekly"


@dataclass(frozen=True)
class ScannerSpec:
    """One hosted scanner.

    ``script``      original fleet script name — the park-marker identity, the
                    file the runner imports, and the report mapping key.
    ``logger_name`` the module name the runner imports the file under. The bot
                    modules do ``logger = logging.getLogger(__name__)``, so this
                    string IS the log prefix; it is set to the prefix the bot
                    used as a standalone process ("LIS1_BOT", …) so the log lines
                    keep their shape.
    ``cadence``     HOURLY | EVERY_4H | WEEKLY.
    ``minute``      scan minute (identical to the bot's own SCAN_MINUTE).
    ``hour``        scan hour, WEEKLY only.
    ``weekday``     scan weekday (Monday = 0), WEEKLY only.
    """

    script: str
    logger_name: str
    cadence: str
    minute: int
    hour: int = 0
    weekday: int = 0


# Order = dispatch order within a tick. It matches the ascending slot minutes,
# so on the Monday 00:xx cluster the scans run in the same sequence four
# separate processes would have produced.
SCANNERS: tuple[ScannerSpec, ...] = (
    # 36: hourly at minute 23 (day-3 post-listing fade, K5, shadow-only).
    ScannerSpec("36_ai_lis1_bot.py", "LIS1_BOT", HOURLY, minute=23),
    # 37: minute 29 of the 4h hours 0/4/8/… — shortly after the 4h close (K1).
    ScannerSpec("37_ai_tsm1_bot.py", "TSM1_BOT", EVERY_4H, minute=29),
    # 38: Monday 00:31 UTC weekly skew rotation (K7).
    ScannerSpec("38_ai_skw1_bot.py", "SKW1_BOT", WEEKLY, minute=31, hour=0, weekday=0),
    # 39: Monday 00:37 UTC weekly cross-sectional rotation (K2, XSM1 + XSR1).
    ScannerSpec("39_ai_xsm1_bot.py", "XSM1_BOT", WEEKLY, minute=37, hour=0, weekday=0),
    # 47: hourly at minute 41 (pump-continuation long, T-145 candidate,
    # shadow-only — T-2026-KYT-9050-146). Minute 41 keeps ≥4 min distance to
    # the weekly :37 sibling, well inside the CATCH_UP grace.
    ScannerSpec("47_ai_pcl1_bot.py", "PCL1_BOT", HOURLY, minute=41),
)

# Script names the runner hosts. Registered in core/hosted_fleet.py, which owns
# the one "a hosted bot is active exactly when its runner runs and its own park
# marker is absent" rule for ALL runners (T-2026-KYT-9050-135 generalised the
# expansion that used to live here when 45 was the only runner) — read from
# there by core/bot_catalog.py (report mapping keeps resolving tags onto these
# scripts) and main_watchdog.py (they are no longer started, so a survivor from
# before the switch is an orphan to reap).
HOSTED_SCRIPTS: tuple[str, ...] = tuple(spec.script for spec in SCANNERS)


def last_slot(spec: ScannerSpec, now: datetime) -> datetime:
    """The most recent scheduled slot at or before ``now`` (UTC, minute-truncated).

    Pure arithmetic on the same predicates the four ``main()`` loops used
    (``minute == 23`` / ``hour % 4 == 0 and minute == 29`` / ``weekday() == 0 and
    hour == 0 and minute == 31|37``), just expressed as "which slot is current"
    instead of "is this the minute". Everything is UTC, so there is no DST step
    to fold in.
    """
    anchor = now.replace(second=0, microsecond=0)

    if spec.cadence == HOURLY:
        slot = anchor.replace(minute=spec.minute)
        if slot > anchor:
            slot -= timedelta(hours=1)
        return slot

    if spec.cadence == EVERY_4H:
        slot = anchor.replace(hour=anchor.hour - anchor.hour % 4, minute=spec.minute)
        if slot > anchor:
            slot -= timedelta(hours=4)
        return slot

    if spec.cadence == WEEKLY:
        slot = anchor.replace(hour=spec.hour, minute=spec.minute) - timedelta(
            days=(anchor.weekday() - spec.weekday) % 7
        )
        if slot > anchor:
            slot -= timedelta(days=7)
        return slot

    raise ValueError(f"unknown cadence {spec.cadence!r} for {spec.script}")


# Dispatch verdicts of :func:`slot_action`.
IDLE = "idle"  # current slot already handled — nothing to do
RUN = "run"  # slot is due (or within the grace window) → scan
EXPIRED = "expired"  # slot came and went beyond CATCH_UP → skip it, do not scan


def slot_action(spec: ScannerSpec, now: datetime, last_fired: datetime | None) -> tuple[str, datetime]:
    """(verdict, slot) for one scanner at ``now``, given the last slot it handled.

    ``last_fired`` is the slot the runner last consumed for this scanner —
    consumed, not necessarily scanned: a parked or expired slot counts as
    handled too, exactly like a parked process misses its window and waits for
    the next one. The comparison is ``<=`` rather than ``==`` so a backwards
    clock step cannot re-run a slot that was already handled.
    """
    slot = last_slot(spec, now)
    if last_fired is not None and slot <= last_fired:
        return IDLE, slot
    if now - slot > CATCH_UP:
        return EXPIRED, slot
    return RUN, slot


def initial_slots(now: datetime) -> dict[str, datetime]:
    """Start state: every scanner's current slot counts as already handled.

    A freshly started runner only ever scans on the NEXT matching minute. This
    is a DELIBERATE divergence from the four old loops, not a copy of them: they
    evaluated their minute predicate on the very first iteration, so a bot
    started at 09:23:30 scanned immediately. Skipping that slot is the safe
    direction during the switchover — a runner starting inside a due minute must
    not fire a scan that a not-yet-reaped predecessor process may be running at
    that very moment. What it costs is one slot after a restart, i.e. at worst
    one hourly scan, or a weekly rebalance if the restart lands inside the
    Monday 00:31/00:37 minute; that is cheaper than a double post.
    """
    return {spec.script: last_slot(spec, now) for spec in SCANNERS}
