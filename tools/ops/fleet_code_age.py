#!/usr/bin/env python
"""Is the running fleet older than the code in the checkout?

T-2026-KYT-9050-071. On 2026-08-02 the live checkout sat 45 commits behind
origin/main while the fleet traded happily on the code it had loaded the
evening before — including an unshipped money-path fix (T-009, zone targets on
the losing side). Nothing alarmed. It surfaced only because a bot had been
throwing the same error for 17 days and someone happened to grep the log.

The gap opens silently because a REBOOT starts the fleet without pulling. Only
tools/restart_fleet.ps1 pulls, so a reboot looks like a restart and is not one.
This canary makes that state loud: if the oldest fleet process started BEFORE
the HEAD commit was made, the fleet cannot be running HEAD.

Read-only. No DB, no network, no elevation - PID/ParentPID/create_time are
readable for the elevated fleet, CommandLine is not (which is why the fleet is
identified structurally, not by name).

    python tools/ops/fleet_code_age.py            # verdict + exit code
    python tools/ops/fleet_code_age.py --json     # machine-readable

Exit codes: 0 = fleet at least as new as HEAD (or nothing running to judge),
1 = fleet older than HEAD, 2 = could not measure.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def head_commit_time(repo: str = _REPO) -> datetime | None:
    """Author/commit timestamp of HEAD as aware UTC, or None if git fails."""
    try:
        out = subprocess.run(
            ["git", "-C", repo, "log", "-1", "--format=%ct"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    raw = (out.stdout or "").strip()
    if not raw.isdigit():
        return None
    return datetime.fromtimestamp(int(raw), tz=timezone.utc)


def fleet_processes() -> list[dict]:
    """The watchdog's python children — the fleet, and nothing else.

    NOT simply "python whose parent is python", which is the fingerprint
    restart_fleet.ps1 uses. That set also contains a running trainer's or
    backfill's workers, and one long-lived foreign job drags the oldest-process
    verdict backwards: measured 2026-08-02, a funding-backfill worker from 02:29
    made a fleet restarted at 19:30 look 13 h stale. The script can afford the
    looser set because it only ever REPORTS those PIDs; here the oldest one is
    the whole verdict, so it has to be the right set.

    The watchdog is identified structurally, as the python process with the most
    python children. Its CommandLine is unreadable from an unelevated session
    (the fleet runs elevated), so matching on 'main_watchdog' would silently
    find nothing — that exact miss cost an hour on 2026-08-02.
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a fleet dependency
        return []

    # Keep the plain info dicts, not the psutil objects: everything below needs
    # only pid/ppid/create_time, and a dict keeps this testable with a stub.
    pythons: dict[int, dict] = {}
    for proc in psutil.process_iter(["pid", "name", "ppid", "create_time"]):
        info = dict(proc.info)
        name = (info.get("name") or "").lower()
        if name.startswith("python") or name.startswith("py."):
            pythons[info["pid"]] = info

    children: dict[int, list[int]] = {}
    for pid, info in pythons.items():
        ppid = info.get("ppid")
        if ppid in pythons:
            children.setdefault(ppid, []).append(pid)
    if not children:
        return []

    watchdog_pid = max(children, key=lambda p: len(children[p]))
    out = [{"pid": pid, "create_time": pythons[pid].get("create_time")} for pid in children[watchdog_pid]]
    return [p for p in out if p["create_time"]]


def assess(repo: str = _REPO) -> dict:
    head_ts = head_commit_time(repo)
    procs = fleet_processes()

    if head_ts is None:
        return {"verdict": "unmeasurable", "reason": "git HEAD timestamp not readable", "exit_code": 2}
    if not procs:
        # Not an alarm: no fleet running is a legitimate state (stopped box,
        # build machine). Claiming staleness here would cry wolf.
        return {
            "verdict": "no_fleet",
            "reason": "no python parent/child pairs found — nothing to judge",
            "head_commit_utc": head_ts.isoformat(),
            "exit_code": 0,
        }

    head_epoch = head_ts.timestamp()
    stale_procs = [p for p in procs if p["create_time"] < head_epoch]
    oldest = min(p["create_time"] for p in procs)
    oldest_dt = datetime.fromtimestamp(oldest, tz=timezone.utc)
    lag_h = (head_ts - oldest_dt).total_seconds() / 3600.0

    # HOW MANY are stale, not just whether the oldest is. "1 of 41" and "41 of
    # 41" demand different actions, and the one-of case is the normal one here:
    # main_watchdog starts dashboard.py through its own start_dashboard(), NOT
    # through core.fleet.FLEET, so a marker-based restart recycles the 40 bots
    # and leaves the dashboard on whatever it booted with (observed 2026-08-02).
    return {
        "verdict": "stale" if stale_procs else "current",
        "n_processes": len(procs),
        "n_stale": len(stale_procs),
        "oldest_process_start_utc": oldest_dt.isoformat(),
        "head_commit_utc": head_ts.isoformat(),
        "lag_hours": round(lag_h, 2),
        "partial": bool(stale_procs) and len(stale_procs) < len(procs),
        "exit_code": 1 if stale_procs else 0,
    }


def _behind_count(repo: str = _REPO) -> int | None:
    """Commits HEAD is behind origin/main — context only, never the verdict.

    Deliberately separate: being behind the REMOTE says the checkout is old,
    which is a different failure than the fleet running older code than the
    checkout. Today both were true at once; they do not have to be.
    """
    try:
        out = subprocess.run(
            ["git", "-C", repo, "rev-list", "--count", "HEAD..origin/main"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = (out.stdout or "").strip()
    return int(raw) if out.returncode == 0 and raw.isdigit() else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Is the running fleet older than the checked-out code?")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--repo", default=_REPO)
    args = ap.parse_args()

    result = assess(args.repo)
    result["behind_origin_main"] = _behind_count(args.repo)

    if args.json:
        print(json.dumps(result, indent=2))
        return int(result["exit_code"])

    v = result["verdict"]
    if v == "unmeasurable":
        print(f"[UNKLAR] {result['reason']} - der Vergleich konnte nicht gemacht werden.")
    elif v == "no_fleet":
        print("[OK] Keine Fleet-Prozesse gefunden - nichts zu beurteilen.")
    elif v == "current":
        print(
            f"[OK] Fleet laeuft auf HEAD oder neuer "
            f"(aeltester Prozess {result['oldest_process_start_utc']}, HEAD {result['head_commit_utc']})."
        )
    else:
        scope = (
            f"{result['n_stale']} von {result['n_processes']} Prozessen"
            if result["partial"]
            else f"ALLE {result['n_processes']} Prozesse"
        )
        print(
            f"[STALE] {scope} laufen mit Code, der AELTER ist als HEAD.\n"
            f"        aeltester Prozess: {result['oldest_process_start_utc']}\n"
            f"        HEAD-Commit:       {result['head_commit_utc']}\n"
            f"        Rueckstand:        {result['lag_hours']} h"
        )
        if result["partial"]:
            print(
                "        Teilbefund - typischerweise das Dashboard: main_watchdog startet\n"
                "        dashboard.py ueber start_dashboard(), NICHT ueber core.fleet.FLEET.\n"
                "        Ein Marker-Restart recycelt die 40 Bots und laesst es stehen."
            )
        else:
            print(
                "        Gemergt heisst hier NICHT live. Ein Reboot startet die Fleet OHNE Pull -\n"
                "        nur tools/restart_fleet.ps1 pullt."
            )
    behind = result.get("behind_origin_main")
    if behind:
        print(f"        Zusaetzlich: der Checkout selbst ist {behind} Commit(s) hinter origin/main.")
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
