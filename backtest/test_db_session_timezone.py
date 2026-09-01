"""Standalone (DB-free) guard for the pinned session timezone (T-2026-KYT-9050-153).

AUDIT_TODO P2.4 flagged `closed_ai_signals.close_time` as written in mixed time
domains across three writers: `NOW()` from the server in one, a Python value in
another. Measured 2026-08-26 against the live code, that premise no longer holds
— but only because of ONE fact, and nothing else in the repo defends it:

    core.database._connect_options() puts `-c timezone=UTC` on every pooled
    connection (the R3 flip, T-2026-KYT-9050-005).

Under that session TZ, all three land in the same naive-UTC domain:

  * 8_ai_trade_monitor.py  — an explicit naive-UTC value from the triggering 5m
    candle (since T-2026-KYT-9050-150).
  * 6_housekeeping.py      — server-side `NOW()`, cast into the naive column
    under the session TZ. Correct semantics here: it closes DELISTED coins,
    where there is no triggering candle and the close really does happen at
    housekeeping time.
  * 28_signal_orchestrator.py — a tz-aware UTC Python value, cast the same way.
    Note it does NOT use `NOW()`, contrary to what the P2.4 entry claimed.

Flip that constant and all three drift apart silently: no test fails, no log
line appears, and the damage only shows up later as timestamps three hours off.
That is what this file exists to prevent — it pins the load-bearing fact, not
the writer inventory (which belongs in the AUDIT_TODO prose and was itself stale:
every line reference in P2.4 pointed at the wrong place).

Run: python backtest/test_db_session_timezone.py
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_PASSWORD", "test-stub")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-stub")

from core import database as db  # noqa: E402  (path must be set before the import)


# ── the pinned constant ───────────────────────────────────────────────────────
def test_session_timezone_is_utc():
    assert db._DEFAULT_SESSION_TZ == "UTC", (
        "the whole close_time domain agreement rests on this; changing it moves "
        "every NOW() writer against every Python-UTC writer"
    )


def test_connect_options_carry_the_timezone():
    opts = db._connect_options()
    assert "-c timezone=UTC" in opts, f"session TZ missing from the libpq options string: {opts!r}"


def test_timezone_is_not_env_overridable():
    """lock_timeout and statement_timeout read env on purpose; the timezone must
    not, or a process that starts without .env would silently use another domain
    — the T-138 class of failure, where three fleet entries never loaded .env."""
    for var, value in (
        ("KYTHERA_DB_LOCK_TIMEOUT_MS", "1234"),
        ("KYTHERA_DB_STATEMENT_TIMEOUT_MS", "4321"),
        ("TZ", "Europe/Bucharest"),
        ("PGTZ", "Europe/Bucharest"),
    ):
        old = os.environ.get(var)
        os.environ[var] = value
        try:
            assert "-c timezone=UTC" in db._connect_options(), f"{var} must not be able to move the session TZ"
        finally:
            if old is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = old


def test_pool_actually_uses_connect_options():
    """A pinned constant that never reaches libpq would be decoration. The pool
    builder must pass the options string through."""
    src = (ROOT / "core" / "database.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_connect_options"
    ]
    assert calls, "_connect_options() is never called"
    assert "options=options" in src or "options = _connect_options()" in src, (
        "the options string is built but not handed to the connection pool"
    )


# ── the corrected P2.4 claims, so the entry cannot drift back ────────────────
def test_orchestrator_does_not_use_now_for_close_time():
    """P2.4 claimed 28_signal_orchestrator writes NOW(). It does not — it passes a
    tz-aware Python UTC value. Pinned so a future reader does not 'fix' a writer
    that was never broken."""
    src = (ROOT / "28_signal_orchestrator.py").read_text(encoding="utf-8")
    for chunk in src.split("INSERT INTO closed_ai_signals")[1:]:
        head = chunk[:400]
        assert "NOW()" not in head, "orchestrator close_time is a Python value, not server-side NOW()"


def test_housekeeping_still_uses_now_and_that_is_correct():
    """The counterpart: housekeeping SHOULD keep NOW(). It closes delisted coins,
    where no candle triggered anything and the close genuinely happens now.
    Porting bot 8's candle stamp here would be wrong."""
    src = (ROOT / "6_housekeeping.py").read_text(encoding="utf-8")
    chunks = src.split("INSERT INTO closed_ai_signals")
    assert len(chunks) == 2, "expected exactly one closed_ai_signals writer in housekeeping"
    assert "NOW()" in chunks[1][:400]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
