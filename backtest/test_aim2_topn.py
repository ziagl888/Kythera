"""Standalone (DB-free) guard for the AIM2-TOPN high-conviction channel.

T-2026-CU-9050-051: the base AIM2 gate posts everything above its ~34 %-pass
threshold; AIM2-TOPN is the selective second consumer that routes at most N of
the day's strongest candidates to their own channel/tag.

Two halves are pinned here:

  * The pure ranking (core.aim2_topn.select_topn / load_config) — cap, min-prob
    floor, parity/trusted filter, per-(coin,direction) dedupe, deterministic
    tie-break — checked without a database.
  * The wiring in 15_ai_master_bot.py, checked statically because the bot needs a
    live DB to run: the whole path sits behind the default-off AIM2_TOPN_ENABLED
    gate, the TOPN tag is excluded from AIM2's own candidate/swarm stream (F6
    self-feedback), posting goes through the audited single-message helper, and
    none of the money gates (AIM2_LIVE_POSTING / NEW_IDEAS_LIVE_POSTING) are
    flipped.

Run: python backtest/test_aim2_topn.py   (or: pytest backtest/test_aim2_topn.py)
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.aim2_topn import (  # noqa: E402
    DEFAULT_MIN_PROB,
    DEFAULT_N,
    MODEL_TAG,
    TopNCandidate,
    load_config,
    select_topn,
)
from core.market_utils import COOLDOWN_MODULE_MAX_LEN  # noqa: E402

SRC = (ROOT / "15_ai_master_bot.py").read_text(encoding="utf-8")


def _c(coin, direction, prob, trusted=True, source="EPD1", close=100.0):
    return TopNCandidate(coin=coin, direction=direction, prob=prob, trusted=trusted, source=source, close_price=close)


# --------------------------------------------------------------- select_topn


def test_empty_pool_selects_nothing():
    assert select_topn([], n=3, min_prob=0.9, posts_last_24h=0) == []


def test_cap_already_reached_selects_nothing():
    pool = [_c("BTCUSDT", "LONG", 0.99)]
    assert select_topn(pool, n=1, min_prob=0.9, posts_last_24h=1) == []
    assert select_topn(pool, n=2, min_prob=0.9, posts_last_24h=5) == []


def test_min_prob_floor_excludes_weak_candidates():
    pool = [_c("BTCUSDT", "LONG", 0.94), _c("ETHUSDT", "SHORT", 0.96)]
    sel = select_topn(pool, n=3, min_prob=0.95, posts_last_24h=0)
    assert [s.coin for s in sel] == ["ETHUSDT"]


def test_untrusted_candidates_never_post():
    # OOD / parity-guard failure must not reach the high-conviction channel.
    pool = [_c("BTCUSDT", "LONG", 0.99, trusted=False)]
    assert select_topn(pool, n=3, min_prob=0.9, posts_last_24h=0) == []


def test_dedupe_per_coin_direction_keeps_strongest():
    pool = [
        _c("BTCUSDT", "LONG", 0.96, source="A"),
        _c("BTCUSDT", "LONG", 0.99, source="B"),  # same trade, higher prob wins
        _c("BTCUSDT", "SHORT", 0.97, source="C"),  # different direction survives
    ]
    sel = select_topn(pool, n=5, min_prob=0.9, posts_last_24h=0)
    assert len(sel) == 2
    long_pick = next(s for s in sel if s.direction == "LONG")
    assert long_pick.source == "B" and long_pick.prob == 0.99


def test_deterministic_order_and_cap_slice():
    pool = [
        _c("ETHUSDT", "LONG", 0.97),
        _c("BTCUSDT", "LONG", 0.99),
        _c("SOLUSDT", "SHORT", 0.98),
    ]
    sel = select_topn(pool, n=2, min_prob=0.9, posts_last_24h=0)
    # prob desc, tie-break coin/direction; capped to 2
    assert [s.coin for s in sel] == ["BTCUSDT", "SOLUSDT"]


def test_remaining_slot_respects_prior_posts():
    pool = [_c("BTCUSDT", "LONG", 0.99), _c("ETHUSDT", "SHORT", 0.98)]
    sel = select_topn(pool, n=3, min_prob=0.9, posts_last_24h=2)
    assert [s.coin for s in sel] == ["BTCUSDT"]  # only one slot left


def test_prob_tie_breaks_by_coin_then_direction():
    pool = [_c("ETHUSDT", "SHORT", 0.95), _c("ETHUSDT", "LONG", 0.95), _c("BTCUSDT", "LONG", 0.95)]
    sel = select_topn(pool, n=3, min_prob=0.9, posts_last_24h=0)
    assert [(s.coin, s.direction) for s in sel] == [
        ("BTCUSDT", "LONG"),
        ("ETHUSDT", "LONG"),
        ("ETHUSDT", "SHORT"),
    ]


# --------------------------------------------------------------- load_config

_ENV_KEYS = ("AIM2_TOPN_ENABLED", "AIM2_TOPN_LIVE_POSTING", "AIM2_TOPN_N", "AIM2_TOPN_MIN_PROB")


def _with_env(**kw):
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in kw.items():
        os.environ[k] = v
    try:
        return load_config()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_config_defaults_are_off_and_conservative():
    cfg = _with_env()
    assert cfg.enabled is False, "AIM2-TOPN must default OFF (default-off gate)"
    assert cfg.live is False, "AIM2-TOPN must be shadow-first"
    assert cfg.n == DEFAULT_N == 1
    assert cfg.min_prob == DEFAULT_MIN_PROB == 0.95


def test_config_env_overrides_and_clamps():
    cfg = _with_env(AIM2_TOPN_ENABLED="1", AIM2_TOPN_LIVE_POSTING="1", AIM2_TOPN_N="3", AIM2_TOPN_MIN_PROB="0.88")
    assert cfg.enabled and cfg.live and cfg.n == 3 and cfg.min_prob == 0.88
    # N clamps to >= 1, min_prob into [0,1], garbage falls back to defaults
    assert _with_env(AIM2_TOPN_N="0").n == 1
    assert _with_env(AIM2_TOPN_MIN_PROB="1.5").min_prob == 1.0
    assert _with_env(AIM2_TOPN_MIN_PROB="nan-ish").min_prob == DEFAULT_MIN_PROB
    # any value other than "1" is OFF (no accidental truthiness)
    assert _with_env(AIM2_TOPN_ENABLED="true").enabled is False


def test_tag_fits_cooldown_module_limit():
    assert len(MODEL_TAG) <= COOLDOWN_MODULE_MAX_LEN


# ---------------------------------------------------- static wiring in bot 15


def test_topn_path_is_behind_default_off_gate():
    assert "if topn_cfg.enabled and topn_pool:" in SRC, "TOPN posting must be gated by topn_cfg.enabled"
    assert "if topn_cfg.enabled and trusted and prob >= topn_min:" in SRC, (
        "pool accumulation must also be gated — disabled ⇒ zero base-AIM2 behaviour change"
    )


def test_topn_tag_excluded_from_candidate_stream():
    # F6 self-feedback: TOPN rows are meta-gate output, never a base signal.
    assert "NOT IN ('AIM1', 'AIM2', %s)" in SRC
    assert "(TOPN_TAG, since_local)" in SRC


def test_topn_min_never_below_base_gate():
    assert 'topn_min = max(topn_cfg.min_prob, ARTIFACT["threshold"])' in SRC


def test_topn_posts_via_single_message_helper():
    # Regel 4: exactly one Cornix-parseable message. Reuse the audited helper
    # instead of hand-rolling a second posting block.
    assert "post_ai_signal(" in SRC
    assert "from core.signal_post import has_open_ai_signal, post_ai_signal" in SRC


def test_topn_does_not_flip_money_gates():
    # This task must not arm any live-posting gate. Only reads AIM2_LIVE_POSTING;
    # never assigns it, and never mentions NEW_IDEAS_LIVE_POSTING.
    assert 'os.getenv("AIM2_LIVE_POSTING"' in SRC
    assert not re.search(r"AIM2_LIVE_POSTING\s*=\s*['\"]1['\"]", SRC)
    assert "NEW_IDEAS_LIVE_POSTING" not in SRC


def test_unset_topn_channel_forces_shadow():
    assert "topn_cfg.live and TOPN_CHANNEL_ID != 0" in SRC


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
