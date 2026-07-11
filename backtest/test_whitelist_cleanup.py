# backtest/test_whitelist_cleanup.py
"""
Unit tests for the bot_regime_whitelist write-side cleanup (P2.25).

compute_whitelist() only UPSERTs rows for bots in the current analysis window,
so stale rows — the pre-naming-fix raw-name keys (frozen since 2026-04-19, the
exact rows the orchestrator reads) and normalized rows of bots that stopped
trading — are never removed. cleanup_stale_whitelist_rows purges them: raw-name
keys age-independently, plus anything older than WHITELIST_RETENTION_DAYS. The
orchestrator's 48h read gate already distrusts those rows, so the delete changes
no live decision. These tests pin the criteria DB-free.

Run with: pytest backtest/test_whitelist_cleanup.py -v
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime
from unittest.mock import MagicMock

# core.config raises at import when its _required() vars are unset; seed dummies
# before the loader execs the analyzer module (empty .env stub on the build box).
os.environ.setdefault("DB_PASSWORD", "unit-test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unit-test")


def _load_analyzer_module():
    """Lädt 27_bot_regime_analyzer als Modul (wegen Ziffer-im-Dateinamen).

    pretty_name (core.bot_naming) bleibt REAL — die Raw-Namen-Erkennung ist genau
    die Logik, die hier verifiziert wird.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bot_regime_analyzer",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "27_bot_regime_analyzer.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ana = _load_analyzer_module()


def _mock_conn(distinct_bot_names, delete_rowcount=0):
    """Conn where the DISTINCT scan returns the given bot names and the DELETE
    reports delete_rowcount affected rows."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    cur.fetchall = MagicMock(return_value=[(b,) for b in distinct_bot_names])
    cur.rowcount = delete_rowcount
    return conn, cur


# ── Retention constant ────────────────────────────────────────────────────────

def test_retention_is_14_days():
    """Conservative default: well above the daily analyzer cadence and the 48h
    orchestrator read gate."""
    assert ana.WHITELIST_RETENTION_DAYS == 14


# ── Query builder ─────────────────────────────────────────────────────────────

def test_query_has_both_delete_criteria():
    now = datetime.datetime(2026, 7, 11, 12, 0, 0)
    sql, params = ana.build_whitelist_cleanup_query(now, ana.WHITELIST_RETENTION_DAYS)
    # (A) raw-name keys and (B) age, OR-combined.
    assert "bot_name = ANY(%s)" in sql
    assert "computed_at < %s" in sql
    assert " OR " in sql
    # cutoff = now - retention.
    assert params == (now - datetime.timedelta(days=14),)


def test_query_cutoff_tracks_retention_argument():
    now = datetime.datetime(2026, 7, 11, 12, 0, 0)
    sql, params = ana.build_whitelist_cleanup_query(now, 3)
    assert params == (now - datetime.timedelta(days=3),)


# ── Raw-name detection (real pretty_name) ─────────────────────────────────────

def test_cleanup_targets_raw_name_keys():
    """Only names whose pretty_name differs are passed as the ANY(%s) list."""
    conn, cur = _mock_conn(
        distinct_bot_names=[
            "MIS1-8H",           # raw → MIS1-8h
            "Fast In And Out",   # raw → FastInOut
            "MIS1-168H_pump",    # raw → MIS1-168h
            "FastInOut",         # already normalized
            "MIS1-8h",           # already normalized
            "ATS1",              # unchanged
        ],
        delete_rowcount=42,
    )
    deleted = ana.cleanup_stale_whitelist_rows(conn)

    # The DELETE (second execute; first is the DISTINCT scan).
    delete_call = cur.execute.call_args_list[-1]
    sql = delete_call.args[0]
    params = delete_call.args[1]
    assert "DELETE FROM bot_regime_whitelist" in sql
    raw_keys = params[0]
    assert set(raw_keys) == {"MIS1-8H", "Fast In And Out", "MIS1-168H_pump"}
    # Normalized names must NOT be in the raw-key delete list.
    assert "FastInOut" not in raw_keys
    assert "MIS1-8h" not in raw_keys
    assert "ATS1" not in raw_keys
    assert deleted == 42
    conn.commit.assert_called_once()


def test_cleanup_with_no_raw_names_still_runs_age_delete():
    """No raw-name keys → the ANY(%s) list is empty, but the age criterion still
    fires the DELETE (a retired bot's normalized row is age-stale)."""
    conn, cur = _mock_conn(
        distinct_bot_names=["FastInOut", "MIS1-8h", "ATS1"],
        delete_rowcount=3,
    )
    deleted = ana.cleanup_stale_whitelist_rows(conn)

    delete_call = cur.execute.call_args_list[-1]
    params = delete_call.args[1]
    assert params[0] == []          # empty raw-key list
    assert deleted == 3
    conn.commit.assert_called_once()


def test_cleanup_scan_failure_returns_zero_no_commit():
    """A failing DISTINCT scan is swallowed (0 deleted, no commit) — the cleanup
    must never crash the hourly analysis run."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock(side_effect=Exception("scan boom"))
    conn.cursor = MagicMock(return_value=cur)
    assert ana.cleanup_stale_whitelist_rows(conn) == 0
    conn.commit.assert_not_called()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
