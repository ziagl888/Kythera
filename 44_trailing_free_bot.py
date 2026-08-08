# 44_trailing_free_bot.py — unrestricted trailing twin: bot 40's engine, no admission caps, two channels.
"""
T-2026-KYT-9050-117. The unfiltered arm of the trailing experiment: identical
behaviour to ``40_trailing_close_bot.py`` — same roster, same trail rule, same
time-stop, same fill logic — but WITHOUT the ±50 exposure cap (operator
decision Michi 2026-08-08). Slot admission itself stays density-ranked;
what changes is its budget: Cornix caps each channel at 500 open trades, so
TWO channels (``CH_TRAILING_FREE_A/B``) give ~1000 seats instead of bot 40's
500. That is not unlimited — this roster's occupancy still peaks near ~2000
in the top ~5% of hours (``staging_models/replay/trailing_slot_budget_live.md``),
and in those hours the densest legs win the seats here too, just twice as
many of them.

This file is deliberately a THIN WRAPPER, not a copy: it sets
``TRAILING_BOT_PROFILE=free`` and re-executes bot 40's module, so every
behaviour change made in ``40_trailing_close_bot.py`` reaches both arms at
once (operator requirement: one code path, two bots). All profile-dependent
wiring — channels, live gate, table ``trailing_free_positions``, tag suffix
``-TRAILF`` — lives in bot 40's profile block; nothing but the profile
selection belongs here.

Safeguards: ``TRAILING_FREE_LIVE_POSTING`` is default 0, and both channels
fall back to ``CH_SHADOW_TEST``, which is not Cornix-executed — until Michi
creates the two real channels and enters them in ``.env``, a deploy alone
posts nothing that trades.

Watchdog: start_delay=299.
"""

import importlib.util
import os
import sys

# Before the engine import: bot 40's profile block reads this at module level.
os.environ["TRAILING_BOT_PROFILE"] = "free"

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("trailing_free_engine", os.path.join(_HERE, "40_trailing_close_bot.py"))
assert _spec is not None and _spec.loader is not None
_engine = importlib.util.module_from_spec(_spec)
sys.modules["trailing_free_engine"] = _engine
_spec.loader.exec_module(_engine)

if __name__ == "__main__":
    try:
        _engine.main()
    except KeyboardInterrupt:
        _engine.logger.info("🛑 Trailing free bot stopped manually (Ctrl+C).")
