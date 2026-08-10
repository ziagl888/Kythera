# 46_signal_consumer_runner.py — one process for the five signal consumers.
"""
T-2026-KYT-9050-135 (fleet consolidation, cluster C). Bots 9 (SRA), 15 (AIM2),
30 (PEX1), 33 (FIF1) and 43 (FIF2) were five permanent Python interpreters —
each with its own pandas/numpy import and its own minconn-2 DB pool — for one
job: polling a table. They scan no coins; they wake up every 60 s (bot 9: every
300 s), ask the DB "anything new?", act on what they find and sleep again. This
process drives all five on exactly those cadences and replaces their five fleet
entries with one.

Money path
----------
Unlike cluster B (T-133, four shadow-only scanners) three of these five reach
real money: 15 and 33 post LIVE, and 43 fills a trailing roster seat that bot 40
mirrors into the Cornix-executed channel. The consolidation therefore changes
WHERE the poll runs and nothing else — no posting, cooldown, gate, watermark or
dedup code is touched, and every piece of in-memory state stays owned by its own
bot module.

Behaviour-neutral by construction
---------------------------------
Each bot module was split at exactly one seam: ``main()`` became
``startup()`` + ``run_poll()`` + the unchanged ``while True: run_poll();
time.sleep(N)``. ``run_poll()`` IS the body of that loop — same statements, same
order — and ``startup()`` IS everything the bot did before entering it. The
files stay runnable standalone (``python 33_ai_fif1_bot.py``) for debugging.
The runner imports them via importlib (the ``44_trailing_free_bot.py`` pattern)
and calls those two functions. Loop state that used to be a local of ``main()``
(bot 30's watermark, bot 33's seen-set, bot 43's samples/seen/bootstrap) is now a
module-level global of the SAME module — still one instance per bot, still
invisible to the other four.

Each poll core keeps opening, rolling back and closing its OWN DB connection per
cycle, so the runner itself holds no connection and no transaction: a poisoned
transaction cannot leak from one bot into the next, because they never share
one.

Two things are deliberately different, both documented at their source:
  * a poll may start LATE when a sibling overruns — dispatch is sequential
    (``signal_consumers.LATE_WARNING_S`` and the grace rule above it);
  * a bot is ARMED lazily: its ``startup()`` runs at its first dispatch, and a
    startup that fails means that bot does not poll until a later cycle gets it
    started. That is the money-path-safe direction — bots 30/33 build their
    anti-replay state (watermark / seen-set) in startup, so polling without it
    would replay a window that has already been posted.

Per-bot isolation
-----------------
* **Errors:** every startup and every poll runs in its own try/except (bot-29
  pattern). An exception is logged with its traceback and the other four still
  poll — as five processes, a crash only ever took its own bot down, and that
  property must survive the merge. The same holds for the import: a module that
  fails to import leaves the other four working instead of killing the process.
* **Parking:** the operator keeps single-bot granularity. Before every poll the
  runner checks ``control/parked/<original script>.py`` — i.e. the FIVE original
  markers, not one runner-wide marker — so
  ``python -c "from core.process_control import park; park('33_ai_fif1_bot.py')"``
  silences FIF1 alone and leaves the other four polling. Parking the runner
  itself (``46_signal_consumer_runner.py``, the dashboard's stop button) stops
  all five, which is what parking a process means. Note the dashboard's per-bot
  buttons follow the fleet list, so the five bots no longer have their own row
  there — the marker file above is their park path.

Logging
-------
The bot modules do ``logger = logging.getLogger(__name__)``, so importing them
under their old log prefixes ("AI_MASTER_BOT", …) plus a ``%(name)s`` format
keeps every line byte-identical to what the standalone bots wrote. Only the
destination changes: all five now log into this process's log file.

Watchdog: start_delay=319.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import time
from collections.abc import Callable
from types import ModuleType

from core.process_control import is_parked
from core.signal_consumers import (
    CONSUMERS,
    LATE_WARNING_S,
    ConsumerSpec,
    due_specs,
    initial_due,
    lateness,
    reschedule,
    sleep_seconds,
)

# %(name)s instead of a hard-coded bot tag: the bot modules call
# logging.basicConfig themselves, but the FIRST call wins, so without this the
# whole process would log under whichever bot was imported first.
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(message)s")
logger = logging.getLogger("CONSUMER_RUNNER")

_HERE = os.path.dirname(os.path.abspath(__file__))


def load_consumer(spec: ConsumerSpec) -> ModuleType:
    """Import a digit-prefixed bot module under its own logger name.

    Digit-prefixed filenames are not importable as packages; importlib from the
    file path is the fleet's established way in (``44_trailing_free_bot.py``).
    The module name is ``spec.logger_name`` on purpose — it becomes both the
    ``sys.modules`` key and the module's ``__name__``, and therefore the log
    prefix of every line the bot writes.
    """
    path = os.path.join(_HERE, spec.script)
    module_spec = importlib.util.spec_from_file_location(spec.logger_name, path)
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"{spec.script} is not loadable from {path}")
    module = importlib.util.module_from_spec(module_spec)
    # Registered BEFORE exec_module, like CPython's own import: a module that
    # imports itself (or is reached again during its own execution) must find
    # the partially-built object, not start a second execution. The flip side is
    # that a failed exec would leave that half-initialised module behind under
    # the bot's logger name, where a later import — or anything else reaching
    # for the name — would silently get the broken object instead of an error,
    # so the failing path removes it again.
    sys.modules[spec.logger_name] = module
    try:
        module_spec.loader.exec_module(module)
    except BaseException:
        # pop, not del: a module that blew up mid-import may already have
        # removed its own entry, and that must not mask the original error.
        sys.modules.pop(spec.logger_name, None)
        raise
    return module


def load_consumers() -> dict[str, ModuleType]:
    """Import every hosted consumer; a broken one is skipped, not fatal."""
    modules: dict[str, ModuleType] = {}
    for spec in CONSUMERS:
        try:
            modules[spec.script] = load_consumer(spec)
            logger.info(f"✅ {spec.logger_name} loaded from {spec.script}.")
        except Exception:
            logger.exception(f"❌ {spec.logger_name} ({spec.script}) is not importable — this consumer stays silent.")
    return modules


def run_due_consumers(
    modules: dict[str, ModuleType],
    state: dict[str, float],
    armed: set[str],
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """One dispatch pass: poll every consumer whose due time has come.

    ``state`` maps script → next due time on the monotonic clock and is updated
    in place; ``armed`` holds the scripts whose ``startup()`` has already run.
    A consumer is ALWAYS re-armed after its turn — parked, failed or polled —
    so a persistent failure costs one cycle per interval instead of turning into
    a hot loop, exactly like a crashing standalone bot cost one watchdog restart
    cycle.
    """
    for spec in due_specs(state, clock()):
        # Read the clock again per bot, not once per pass: what makes a poll
        # late is precisely the time a SIBLING dispatched before it spent, and
        # that only shows on a fresh read.
        late = lateness(spec, state, clock())
        if late > LATE_WARNING_S:
            # Not an error and never a dropped poll — but a pass that regularly
            # runs this late is eating into FIF2's 240 s mirror window, and that
            # must be visible in the log before it costs mirrors.
            logger.warning(f"⏱️  {spec.logger_name}: poll starts {late:.0f}s late — a sibling poll overran.")

        try:
            if is_parked(spec.script):
                logger.info(f"⏸️  {spec.logger_name} is parked ({spec.script}) — poll skipped.")
                continue

            module = modules.get(spec.script)
            if module is None:
                logger.error(f"❌ {spec.logger_name}: module not loaded — poll skipped.")
                continue

            if spec.script not in armed:
                # One-time init: DDL/indices, model load, and — for 30/33/43 —
                # the anti-replay state the poll depends on. It runs here rather
                # than at process start so that a bot parked at startup arms
                # itself on the cycle it is unparked, with a FRESH watermark /
                # seen-set instead of one from hours ago. A raise leaves the bot
                # unarmed and skips its poll (the except below), and the next
                # cycle tries again — the standalone equivalent of a bot dying
                # in main() and being restarted by the watchdog.
                module.startup()
                armed.add(spec.script)
                logger.info(f"🚀 {spec.logger_name} armed — polling every {spec.interval:.0f}s.")

            module.run_poll()
        except Exception:
            logger.exception(f"💥 {spec.logger_name}: poll failed — the other consumers keep running.")
        finally:
            # From the END of the poll, not from the due time: that is what
            # `while True: poll(); sleep(N)` did per process, and it is what
            # keeps a slow poll from queueing a backlog behind itself.
            reschedule(spec, state, clock())


def main() -> None:
    logger.info("=== 📡 SIGNAL CONSUMER RUNNER STARTED — SRA / AIM2 / PEX1 / FIF1 / FIF2 in one process ===")
    modules = load_consumers()

    state = initial_due(time.monotonic())
    armed: set[str] = set()
    logger.info(
        f"🗓️  Cadences armed for {len(CONSUMERS)} consumers: "
        + ", ".join(f"{spec.logger_name} {spec.interval:.0f}s" for spec in CONSUMERS)
    )

    while True:
        run_due_consumers(modules, state, armed)
        time.sleep(sleep_seconds(state, time.monotonic()))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Runner manually stopped (Ctrl+C). Shutting down cleanly...")
