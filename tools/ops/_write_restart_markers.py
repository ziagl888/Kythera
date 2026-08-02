#!/usr/bin/env python
"""Write one restart marker per FLEET bot — the unelevated fleet-recycle path.

T-2026-KYT-9050-071. Called by ``tools/restart_fleet.ps1 -MarkerRestart``; kept
in Python because the fleet list lives in ``core/fleet.py`` and duplicating it
into PowerShell is the exact drift class that made the dashboard and the
watchdog disagree before T-2026-CU-9050-091.

The running watchdog consumes ``control/restart/<script>`` on its next cycle
(<=10 s) and recycles that bot, so a bot picks up new code without stopping the
scheduled task. Same mechanism as the dashboard's restart button
(``dashboard.restart_all``).

**No unpark, on purpose.** ``dashboard.restart_process`` calls ``unpark()``
first; this does not. A parked bot is parked deliberately, and silently
re-arming it during a code rollout is precisely the surprise the restart script
exists to avoid. A parked bot never consumes its marker — main_watchdog checks
``is_parked`` before ``consume_restart`` — which leaves a harmless leftover file
and is reported by the caller.

Writes nothing else, touches no process, needs no elevation.
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from core.fleet import FLEET  # noqa: E402
from core.process_control import list_parked, request_restart  # noqa: E402


def main() -> int:
    parked = list_parked()
    written = 0
    for entry in FLEET:
        request_restart(entry["script"])
        written += 1
    print(f"{written} restart marker(s) written for {len(FLEET)} FLEET entries")
    if parked:
        # Named rather than skipped: their markers will linger, and an operator
        # who sees leftover files should know why instead of suspecting a hang.
        print(f"parked (will NOT consume their marker, left parked on purpose): {', '.join(sorted(parked))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
