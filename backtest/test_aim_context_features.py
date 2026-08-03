"""Standalone (DB-free) guards for the AIM2 serving fixes from P2.35
(T-2026-CU-9050-090):

  (a) Candidate window is catch-up capable (CANDIDATE_WINDOW_MIN) AND protected
      against double processing by a persistent processed table.
  (b) Swarm/context aggregates do NOT count the candidate itself
      (strictly < ts). The AIM1/AIM2/AIM2-TOPN self-exclusion sits at
      stream level and is pinned by test_aim2_event_source_symmetry.
  (c) The conv dedup key is table-AGNOSTIC: a conv signal moves within
      seconds from active_ to closed_trades_master with a NEW serial id while
      the open `time` stays unchanged (5_trade_monitor.close_trade). The
      per-table `id` therefore does not work as a dedup key — otherwise the
      closed form would be re-scored as a fresh candidate (double post) and
      unrelated active/closed rows with the same id would displace each other
      (the original P2.35 collision). Diagnostic like 33_ai_fif1_bot.signal_key.

Rule 7 (trainer == serving feature parity) is NOT touched here: the
dedup key only controls WHICH candidates serving scores/posts — it is not a
model input feature, and the trainer (aim2_build_dataset.py) does not
deduplicate at all. The context self-exclusion (b) is already implemented
identically on both sides (strictly < ts), so NO retrain coupling applies.

The bot is called `15_ai_master_bot.py` (digit prefix → not importable); we
load it via importlib. `core.config` validates two mandatory env vars on
import — set here with dummies, a DB connection is never opened.

Run: python backtest/test_aim_context_features.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pandas as pd  # import before the module load (loader convention)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_PASSWORD", "test-stub")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-stub")

_spec = importlib.util.spec_from_file_location("ai_master_bot", str(ROOT / "15_ai_master_bot.py"))
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

TS = pd.Timestamp("2026-07-11T10:00:00")
BIGINT_MAX = (1 << 63) - 1


# ── (c) conv dedup key: table-agnostic ────────────────────────────────────────
def test_conv_same_signal_survives_active_to_closed_migration():
    """Same signal, active(id=500) → closed(id=777): NEW id, same
    identity → identical key (otherwise double post of the closed form)."""
    active = bot.dedup_key("conv", 500, "Fast In And Out", "BTCUSDT", "LONG", TS, 65000.0)
    closed = bot.dedup_key("conv", 777, "Fast In And Out", "BTCUSDT", "LONG", TS, 65000.0)
    assert active == closed, "conv key hangs off the per-table id → active/closed re-scored (double post)"
    assert active[0] == "conv"


def test_conv_unrelated_rows_sharing_a_per_table_id_are_distinct():
    """The actual P2.35 collision: two UNRELATED conv signals with a
    coincidentally identical per-table id must not deduplicate."""
    a = bot.dedup_key("conv", 500, "Fast In And Out", "BTCUSDT", "LONG", TS, 65000.0)
    b = bot.dedup_key("conv", 500, "Volume Indicator", "ETHUSDT", "SHORT", TS, 3000.0)
    assert a != b, "unrelated conv rows with the same id collide → one is silently discarded"


def test_conv_identity_separates_coin_direction_time_source():
    base = dict(source="Volume Indicator", symbol="ETHUSDT", direction="LONG", ts=TS, entry=3000.0)
    k = bot.conv_signal_identity(**base)
    assert k != bot.conv_signal_identity(**{**base, "symbol": "BTCUSDT"})
    assert k != bot.conv_signal_identity(**{**base, "direction": "SHORT"})
    assert k != bot.conv_signal_identity(**{**base, "ts": TS + pd.Timedelta(hours=1)})
    assert k != bot.conv_signal_identity(**{**base, "source": "Fast In And Out"})
    assert k != bot.conv_signal_identity(**{**base, "entry": 3000.5})


def test_conv_identity_is_bigint_safe_and_deterministic():
    k = bot.conv_signal_identity("Fast In And Out", "BTCUSDT", "LONG", TS, 65000.0)
    assert 0 <= k <= BIGINT_MAX, "signal_id must fit into the signed 64-bit range (BIGINT)"
    assert k == bot.conv_signal_identity("Fast In And Out", "btcusdt", "long", TS, 65000.0), "must be case-stable"


def test_ai_key_keeps_the_stable_prediction_id():
    """ai: ml_predictions_master.id is stable (posted rows never migrate) →
    direct id, separate namespace from conv."""
    assert bot.dedup_key("ai", 123, "MIS1-24H", "BTCUSDT", "LONG", TS, 65000.0) == ("ai_signal", 123)


def test_ai_and_conv_namespaces_do_not_collide():
    ai = bot.dedup_key("ai", 500, "MIS1-24H", "BTCUSDT", "LONG", TS, 65000.0)
    conv = bot.dedup_key("conv", 500, "Fast In And Out", "BTCUSDT", "LONG", TS, 65000.0)
    assert ai[0] != conv[0]


# ── (b) context/swarm: candidate does not count itself ────────────────────────
def _stream(rows):
    return pd.DataFrame(rows, columns=["symbol", "ts", "direction", "source"])


def test_swarm_excludes_the_candidate_itself():
    """A row exactly at ts (the candidate) must NOT go into the 5d aggregates."""
    s = _stream(
        [
            ("BTCUSDT", TS, "LONG", "CANDIDATE"),               # the candidate itself
            ("BTCUSDT", TS - pd.Timedelta(hours=1), "LONG", "X"),
            ("BTCUSDT", TS - pd.Timedelta(hours=2), "SHORT", "Y"),
        ]
    )
    out = bot.swarm_stats(s, "BTCUSDT", TS, "LONG")
    assert out["total_5d"] == 2, "candidate (ts == ts) was counted in — self-counting"
    assert out["long_5d"] == 1 and out["short_5d"] == 1


def test_swarm_confluence_counts_only_prior_same_direction():
    s = _stream(
        [
            ("BTCUSDT", TS, "LONG", "CANDIDATE"),
            ("BTCUSDT", TS - pd.Timedelta(hours=1), "LONG", "X"),
            ("BTCUSDT", TS - pd.Timedelta(hours=3), "LONG", "Z"),
            ("BTCUSDT", TS - pd.Timedelta(hours=6), "LONG", "OLD"),  # >4h → not in confluence
        ]
    )
    out = bot.swarm_stats(s, "BTCUSDT", TS, "LONG")
    assert out["confl_same_dir_4h"] == 2
    assert out["distinct_src_same_dir_4h"] == 2


# ── (a) window + processed table ───────────────────────────────────────────────
def test_candidate_window_is_catch_up_sized():
    assert bot.CANDIDATE_WINDOW_MIN >= 60, "catch-up window after downtime (P2.35)"


def test_processed_dedup_table_exists_and_is_keyed_on_signal_identity():
    src = (ROOT / "15_ai_master_bot.py").read_text(encoding="utf-8")
    assert "master_ai_processed_signals" in src
    assert "PRIMARY KEY (signal_type, signal_id)" in src
    # the insert must take the agnostic key from the candidate, not the raw id
    assert "signal.dkey" in src


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
