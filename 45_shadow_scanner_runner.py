# 45_shadow_scanner_runner.py — one process for the four rule-based shadow scanners.
"""
T-2026-KYT-9050-133 (fleet consolidation, cluster B). Bots 36 (LIS1), 37 (TSM1),
38 (SKW1) and 39 (XSM1/XSR1) were four permanent Python interpreters — each with
its own pandas/numpy import and its own minconn-2 DB pool — for a few minutes of
work per WEEK: hourly at minute 23, minute 29 of every 4h hour, Monday 00:31 and
Monday 00:37 UTC. This process drives all four on exactly those schedules and
replaces their four fleet entries with one.

Behaviour-neutral by construction
---------------------------------
The four bot modules are NOT modified. They already separate ``run_scan()`` (the
scan core) from ``main()`` (the loop that owns the schedule), so this runner
imports them via importlib — the pattern from ``44_trailing_free_bot.py`` — and
calls ``run_scan()`` when a slot is due. Signal logic, posting, cooldowns and
shadow gates are the same code, reached through the same call; each ``run_scan``
still opens, rolls back and closes its OWN DB connection per scan, so the runner
itself holds no connection and no transaction. The files also stay runnable
standalone (``python 36_ai_lis1_bot.py``) for debugging.

Two things are deliberately different, both documented at their source:
  * a slot may run LATE (up to ``shadow_scanners.CATCH_UP``) when a sibling scan
    overruns its minute — dispatch is sequential, and running late beats
    dropping a weekly rebalance;
  * a scanner started inside its own due minute waits for the NEXT slot
    (``shadow_scanners.initial_slots``).

Per-bot isolation
-----------------
* **Errors:** every scan runs in its own try/except (bot-29 pattern). An
  exception in one scanner is logged with its traceback and the other three
  still run — as four processes, a crash only ever took its own bot down, and
  that property must survive the merge. The same holds for the import: a module
  that fails to import leaves the other three working instead of killing the
  process.
* **Parking:** the operator keeps single-bot granularity. Before every scan the
  runner checks ``control/parked/<original script>.py`` — i.e. the FOUR original
  markers, not one runner-wide marker — so
  ``python -c "from core.process_control import park; park('37_ai_tsm1_bot.py')"``
  silences TSM1 alone and leaves the other three scanning. Parking the runner
  itself (``45_shadow_scanner_runner.py``, the dashboard's stop button) still
  stops all four, which is what parking a process means. Note the dashboard's
  per-bot buttons follow the fleet list, so the four bots no longer have their
  own row there — the marker file above is their park path.

Logging
-------
The bot modules do ``logger = logging.getLogger(__name__)``, so importing them
under their old log prefixes ("LIS1_BOT", …) plus a ``%(name)s`` format keeps
every scan line byte-identical to what the standalone bots wrote. Only the
destination changes: all four now log into this process's log file.

Watchdog: start_delay=311.
"""

from __future__ import annotations

import datetime
import importlib.util
import logging
import os
import sys
import time
from types import ModuleType

from core.database import get_db_connection
from core.process_control import is_parked
from core.shadow_scanners import EXPIRED, RUN, SCANNERS, ScannerSpec, initial_slots, slot_action

# %(name)s instead of a hard-coded bot tag: the bot modules call
# logging.basicConfig themselves, but the FIRST call wins, so without this the
# whole process would log under whichever bot was imported first.
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(message)s")
logger = logging.getLogger("SHADOW_RUNNER")

TICK_SECONDS = 10  # the finest granularity of the four replaced loops
_HERE = os.path.dirname(os.path.abspath(__file__))


def load_scanner(spec: ScannerSpec) -> ModuleType:
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


def load_scanners() -> dict[str, ModuleType]:
    """Import every hosted scanner; a broken one is skipped, not fatal."""
    modules: dict[str, ModuleType] = {}
    for spec in SCANNERS:
        try:
            modules[spec.script] = load_scanner(spec)
            logger.info(f"✅ {spec.logger_name} loaded from {spec.script}.")
        except Exception:
            logger.exception(f"❌ {spec.logger_name} ({spec.script}) is not importable — this scanner stays silent.")
    return modules


def ensure_cooldown_table() -> None:
    """The ``trade_cooldowns`` DDL all four bots ran at startup — now once.

    Verbatim the statement from their ``main()``; ``IF NOT EXISTS`` makes it a
    no-op against the live table. A DB that is down here kills the runner, just
    as it killed each of the four processes before — the watchdog restarts it.
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trade_cooldowns (
                    module VARCHAR(50), coin VARCHAR(20), direction VARCHAR(10),
                    last_posted_at TIMESTAMP WITH TIME ZONE,
                    PRIMARY KEY (module, coin, direction)
                );
            """)
        conn.commit()
    finally:
        conn.close()


def run_due_scanners(
    modules: dict[str, ModuleType],
    state: dict[str, datetime.datetime],
    now: datetime.datetime,
) -> None:
    """One dispatch tick: run every scanner whose slot is due.

    ``state`` maps script → last handled slot and is updated in place. A slot is
    marked handled BEFORE the scan runs, so a scan that raises is not retried on
    the next tick (which would turn a persistent failure into a hot loop); it
    gets its next regular slot instead.
    """
    for spec in SCANNERS:
        action, slot = slot_action(spec, now, state.get(spec.script))
        if action not in (RUN, EXPIRED):
            continue

        state[spec.script] = slot
        stamp = slot.strftime("%Y-%m-%d %H:%M")

        if action == EXPIRED:
            # A sibling scan ran past this slot's grace window. Skipping is the
            # honest outcome, but it must never be silent.
            logger.warning(f"⏭️  {spec.logger_name}: slot {stamp} UTC missed by more than the grace window — skipped.")
            continue

        if is_parked(spec.script):
            logger.info(f"⏸️  {spec.logger_name} is parked ({spec.script}) — slot {stamp} UTC skipped.")
            continue

        module = modules.get(spec.script)
        if module is None:
            logger.error(f"❌ {spec.logger_name}: module not loaded — slot {stamp} UTC skipped.")
            continue

        try:
            module.run_scan()
        except Exception:
            logger.exception(f"💥 {spec.logger_name}: scan of slot {stamp} UTC failed — other scanners keep running.")


def main() -> None:
    logger.info("=== 🛰️ SHADOW SCANNER RUNNER STARTED — LIS1 / TSM1 / SKW1 / XSM1 in one process ===")
    modules = load_scanners()
    ensure_cooldown_table()

    state = initial_slots(datetime.datetime.now(datetime.timezone.utc))
    logger.info(f"🗓️  Schedule armed for {len(SCANNERS)} scanners — next slots run, the current one is skipped.")

    while True:
        run_due_scanners(modules, state, datetime.datetime.now(datetime.timezone.utc))
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Runner manually stopped (Ctrl+C). Shutting down cleanly...")
