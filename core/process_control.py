# core/process_control.py
# Shared parking control for the bot fleet.
#
# The watchdog (main_watchdog.py) is the SINGLE actuator of process lifecycle.
# The dashboard never starts or kills bots directly anymore; it records intent
# here and the watchdog acts on it during its next monitor cycle (<=10s):
#
#   - parked  : an intentionally-stopped bot. The watchdog stops it (if running)
#               and does NOT auto-restart it — a dashboard "stop" now stays
#               stopped instead of being revived within 10s (the old bug).
#   - restart : a one-shot recycle request. The watchdog kills + respawns the
#               bot once, then consumes (clears) the request.
#
# Intent is stored as marker files (one file per script) rather than a shared
# JSON blob, so the two processes never do a racy read-modify-write on the same
# file: creating/removing an individual marker is atomic enough for a single
# operator's dashboard.

from pathlib import Path

_CONTROL_DIR = Path(__file__).resolve().parent.parent / "control"
_PARKED_DIR = _CONTROL_DIR / "parked"
_RESTART_DIR = _CONTROL_DIR / "restart"


def _marker(base: Path, script: str) -> Path:
    # Scripts are bare filenames (e.g. "7_pattern_detector.py"); guard against
    # any stray path separators so the marker name is always a single file.
    safe = script.replace("\\", "_").replace("/", "_")
    return base / safe


# ── Parking (persistent intent: "keep this bot stopped") ─────────────────────


def park(script: str) -> None:
    """Mark a bot as intentionally stopped. The watchdog stops it and will not
    auto-restart it until it is unparked."""
    _PARKED_DIR.mkdir(parents=True, exist_ok=True)
    _marker(_PARKED_DIR, script).touch()


def unpark(script: str) -> None:
    """Clear the parked mark so the watchdog resumes supervising the bot."""
    _marker(_PARKED_DIR, script).unlink(missing_ok=True)


def is_parked(script: str) -> bool:
    return _marker(_PARKED_DIR, script).exists()


def list_parked() -> set[str]:
    """Set of currently-parked script filenames."""
    if not _PARKED_DIR.exists():
        return set()
    return {p.name for p in _PARKED_DIR.iterdir() if p.is_file()}


# ── Restart (one-shot intent: "recycle this bot once") ───────────────────────


def request_restart(script: str) -> None:
    """Request a one-shot restart. Consumed by the watchdog on its next cycle."""
    _RESTART_DIR.mkdir(parents=True, exist_ok=True)
    _marker(_RESTART_DIR, script).touch()


def consume_restart(script: str) -> bool:
    """Return True and clear the request if a restart was pending for this bot."""
    marker = _marker(_RESTART_DIR, script)
    if marker.exists():
        marker.unlink(missing_ok=True)
        return True
    return False
