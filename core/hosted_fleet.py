# core/hosted_fleet.py — which fleet scripts run inside a shared runner process,
# and what that means for "is this bot active?".
#
# T-2026-KYT-9050-135 generalised what T-2026-KYT-9050-133 built for ONE runner.
# Since the fleet consolidation there are two of them:
#
#   * 45_shadow_scanner_runner.py  hosts bots 36-39 (rule-based shadow scanners,
#                                  slot schedule — core/shadow_scanners.py)
#   * 46_signal_consumer_runner.py hosts bots 9/15/30/33/43 (DB pollers, fixed
#                                  cadences — core/signal_consumers.py)
#
# and there will plausibly be more. The T-133 review explicitly killed a second
# drifting mirror of the expansion rule, so the rule lives here ONCE, over a
# registry, instead of once per runner module. Each runner module owns its own
# roster and schedule (that is genuinely runner-specific); what they share — "a
# hosted bot counts as active exactly when its runner runs and its OWN park
# marker is absent" — is this file.
#
# Direction of the dependency: this module imports the runner spec modules, they
# never import it. That keeps shadow_scanners.py free of any knowledge about
# signal_consumers.py and vice versa.

from __future__ import annotations

from core import shadow_scanners, signal_consumers

# runner script → the scripts it hosts. The tuples are DERIVED from each
# runner's spec table, never restated, so a bot added to a roster is
# automatically hosting-aware everywhere this registry is read.
HOSTED_SCRIPTS_BY_RUNNER: dict[str, tuple[str, ...]] = {
    shadow_scanners.RUNNER_SCRIPT: shadow_scanners.HOSTED_SCRIPTS,
    signal_consumers.RUNNER_SCRIPT: signal_consumers.HOSTED_SCRIPTS,
}

# Every hosted script across all runners — the flat view main_watchdog.py needs
# for its orphan-reap set (scripts it must be able to REAP but never STARTS).
ALL_HOSTED_SCRIPTS: tuple[str, ...] = tuple(script for hosted in HOSTED_SCRIPTS_BY_RUNNER.values() for script in hosted)


def runner_for(script: str) -> str | None:
    """The runner hosting ``script``, or None when it owns its own process."""
    for runner, hosted in HOSTED_SCRIPTS_BY_RUNNER.items():
        if script in hosted:
            return runner
    return None


def expand_hosted(scripts: set[str], parked: set[str]) -> set[str]:
    """``scripts`` plus the bots hosted by whichever runners are in it.

    The ONE definition of "a hosted bot is active exactly when its runner runs
    and its OWN park marker is absent" — the same check a runner does before
    each dispatch, seen from the report side. Two callers restate nothing:
    ``core.bot_catalog.active_scripts()`` and the tools/ mirror
    ``tools.fleet_realized_audit.resolve_active_scripts()`` (which exists only
    because the audit needs a parked-dir override, so it can point at the LIVE
    checkout from a worktree). Without the expansion every hosted bot's legs
    drop into the "inactive" bucket of those reports the moment the fleet
    switches over — the bots run, but no longer own a fleet entry.

    Pure set arithmetic, no filesystem: the caller decides where ``parked``
    comes from. Returns a new set; the input is not mutated.
    """
    expanded = set(scripts)
    for runner, hosted in HOSTED_SCRIPTS_BY_RUNNER.items():
        if runner not in scripts:
            continue
        expanded |= {script for script in hosted if script not in parked}
    return expanded
