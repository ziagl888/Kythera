"""Standalone (DB-free) guard for the MAX1 high-conviction rubberband short.

T-2026-CU-9050-067: MAX1 runs the RUB2-SHORT model as a separate bot with a
selective gate (1-3 trades/day) into the MAIN channel, under its own tag. RUB2
itself stays untouched.

Two halves are pinned here:

  * The pure selection (core.max1_gate.select_signals / load_config) — probability
    floor, per-symbol dedupe, deterministic tie-break, rolling-24h cap — checked
    without a database.
  * The wiring in 34_ai_max1_bot.py, checked statically because the bot needs a
    live DB to run: default-off posting gate, tag from the artifact meta (never a
    constant), single-Cornix-message posting, shared feature/geometry sources,
    separate cooldown space from RUB2, and no money gate flipped anywhere.

Run: python backtest/test_max1_gate.py   (or: pytest backtest/test_max1_gate.py)
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.market_utils import COOLDOWN_MODULE_MAX_LEN  # noqa: E402
from core.max1_gate import (  # noqa: E402
    DEFAULT_MAX_PER_DAY,
    DEFAULT_MIN_PROB,
    DIRECTION,
    MODEL_ID,
    Max1Candidate,
    load_config,
    select_signals,
)

SRC = (ROOT / "34_ai_max1_bot.py").read_text(encoding="utf-8")
RUB_SRC = (ROOT / "13_ai_rub_bot.py").read_text(encoding="utf-8")


def _c(symbol, prob, close=100.0):
    return Max1Candidate(symbol=symbol, prob=prob, close_price=close)


# ------------------------------------------------------------- select_signals


def test_empty_pool_selects_nothing():
    assert select_signals([], max_per_day=3, min_prob=0.93, posts_last_24h=0) == []


def test_cap_already_reached_selects_nothing():
    pool = [_c("BTCUSDT", 0.99)]
    assert select_signals(pool, max_per_day=3, min_prob=0.9, posts_last_24h=3) == []
    assert select_signals(pool, max_per_day=1, min_prob=0.9, posts_last_24h=5) == []


def test_min_prob_floor_excludes_weak_candidates():
    pool = [_c("BTCUSDT", 0.92), _c("ETHUSDT", 0.94)]
    sel = select_signals(pool, max_per_day=3, min_prob=0.93, posts_last_24h=0)
    assert [s.symbol for s in sel] == ["ETHUSDT"]


def test_dedupe_per_symbol_keeps_strongest():
    pool = [_c("BTCUSDT", 0.94), _c("BTCUSDT", 0.97), _c("ETHUSDT", 0.95)]
    sel = select_signals(pool, max_per_day=5, min_prob=0.93, posts_last_24h=0)
    assert [(s.symbol, s.prob) for s in sel] == [("BTCUSDT", 0.97), ("ETHUSDT", 0.95)]


def test_deterministic_order_and_cap_slice():
    pool = [_c("ETHUSDT", 0.95), _c("BTCUSDT", 0.99), _c("SOLUSDT", 0.97)]
    sel = select_signals(pool, max_per_day=2, min_prob=0.93, posts_last_24h=0)
    assert [s.symbol for s in sel] == ["BTCUSDT", "SOLUSDT"]


def test_prob_tie_breaks_by_symbol():
    pool = [_c("SOLUSDT", 0.95), _c("BTCUSDT", 0.95), _c("ETHUSDT", 0.95)]
    sel = select_signals(pool, max_per_day=3, min_prob=0.93, posts_last_24h=0)
    assert [s.symbol for s in sel] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_remaining_slots_respect_prior_posts():
    """The rolling-24h count is what makes "1-3 per day" hold across scans."""
    pool = [_c("BTCUSDT", 0.99), _c("ETHUSDT", 0.98), _c("SOLUSDT", 0.97)]
    sel = select_signals(pool, max_per_day=3, min_prob=0.93, posts_last_24h=2)
    assert [s.symbol for s in sel] == ["BTCUSDT"]  # one slot left


# ----------------------------------------------------------------- load_config

_ENV_KEYS = ("MAX1_LIVE_POSTING", "MAX1_MIN_PROB", "MAX1_MAX_PER_DAY")


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
    assert cfg.live is False, "MAX1 must default to shadow-only (default-off gate)"
    assert cfg.min_prob == DEFAULT_MIN_PROB == 0.93
    assert cfg.max_per_day == DEFAULT_MAX_PER_DAY == 3


def test_config_env_overrides_and_clamps():
    cfg = _with_env(MAX1_LIVE_POSTING="1", MAX1_MIN_PROB="0.91", MAX1_MAX_PER_DAY="2")
    assert cfg.live and cfg.min_prob == 0.91 and cfg.max_per_day == 2
    assert _with_env(MAX1_MAX_PER_DAY="0").max_per_day == 1
    assert _with_env(MAX1_MIN_PROB="1.5").min_prob == 1.0
    assert _with_env(MAX1_MIN_PROB="nonsense").min_prob == DEFAULT_MIN_PROB
    # any value other than "1" is OFF (no accidental truthiness)
    assert _with_env(MAX1_LIVE_POSTING="true").live is False


def test_tag_fits_cooldown_module_limit():
    # trade_cooldowns.module is varchar(10) live (T-2026-CU-9050-024).
    assert len(MODEL_ID) <= COOLDOWN_MODULE_MAX_LEN


# ---------------------------------------------------- static wiring in bot 34


def test_posting_is_behind_default_off_gate():
    assert 'live = cfg.live and TARGET_CHANNEL_ID != 0' in SRC, "unset channel must force shadow-only"
    assert re.search(r'_env_flag\("MAX1_LIVE_POSTING", False\)', (ROOT / "core/max1_gate.py").read_text("utf-8")), (
        "MAX1_LIVE_POSTING must default to False"
    )


def test_gate_never_looser_than_the_model_operating_point():
    assert 'gate = max(cfg.min_prob, float(ARTIFACT["threshold"]))' in SRC


def test_tag_comes_from_artifact_meta_not_a_constant():
    """Harte Regel 6 / Falle 16: a MAX2 retrain must post as MAX2, not silently as MAX1."""
    assert 'tag = ARTIFACT["tag"]' in SRC
    post_path = SRC.split("def post_candidate")[1].split("def run_scan")[0]
    # Everything that carries the model identity — Telegram signal, ai_signals row,
    # prediction log, cooldown key — is written under the ARTIFACT tag. MODEL_ID may
    # appear only as the legacy tag of the transitional dedup.
    assert re.search(r"post_ai_signal\(\s*conn,\s*TARGET_CHANNEL_ID,\s*tag,", post_path)
    assert re.search(r"log_prediction\(\s*conn,\s*tag,", post_path)
    assert "update_cooldown(conn, tag," in post_path
    assert [m for m in re.findall(r"MODEL_ID", post_path)] == ["MODEL_ID"], (
        "MODEL_ID may only appear once in the post path — as legacy_tag"
    )
    assert "legacy_tag=MODEL_ID" in post_path


def test_posts_via_single_message_helper():
    """Harte Regel 4: exactly ONE Cornix-parseable message per signal."""
    assert "from core.signal_post import has_open_ai_signal, log_prediction, post_ai_signal" in SRC
    # no hand-rolled Cornix block in the bot — it would be the second parseable message
    assert "CMP Entry" not in SRC
    assert "Stop Loss" not in SRC
    assert SRC.count("post_ai_signal(") == 1


def test_shares_feature_and_geometry_sources_with_trainer_and_replay():
    """X-R1: features and trade geometry come from the shared modules, not a copy."""
    assert "from core.rub_features import RUB_FEATURES, build_rub_features, rub_event_type, rub_trend" in SRC
    # the cached funding path (T-2026-CU-9050-055) — same values, 110d window
    assert "from core.funding_features import FUNDING_FEATURES, funding_features_cached" in SRC
    assert "funding_features_cached(conn, symbol, ts_decision)" in SRC
    assert "hvn_sr_trade_geometry(entry1, is_long=False, supps=supps, resis=resis)" in SRC
    assert "EXPECTED_FEATURES = RUB_FEATURES + FUNDING_FEATURES" in SRC


def test_short_only():
    assert DIRECTION == "SHORT"
    assert '!= "REVERSION_DOWN"' in SRC, "MAX1 must ignore the LONG rubberband event"


def test_cooldown_space_is_separate_from_rub2():
    """MAX1 and RUB2 must not block each other (task requirement 8).

    Both spaces are keyed by the model tag (trade_cooldowns.module, ai_signals.model,
    ml_predictions_master.model_name). MAX1 only ever passes its own tags, so the
    separation holds as long as no RUB tag is a string literal in this bot.
    """
    assert '"RUB2"' not in SRC and "'RUB2'" not in SRC, "MAX1 must never key on RUB2's tag"
    assert 'RUB_LEGACY_TAG = "RUB2"' in RUB_SRC, "bot 13 keeps its own tag space — sanity anchor"
    assert "def cooldown_tags()" in SRC
    # every cooldown/dedupe/open-trade probe runs over MAX1's own tags
    assert "check_cooldown(conn, t, symbol, DIRECTION, COOLDOWN_HOURS) for t in cooldown_tags()" in SRC
    assert "has_open_ai_signal(conn, symbol, DIRECTION, tag)" in SRC


def test_closed_candle_discipline_preserved():
    """R1 / Falle 1: both queries must exclude the forming candle."""
    assert SRC.count("open_time < date_trunc('hour', NOW())") == 2


def test_does_not_flip_any_money_gate():
    for gate in ("AIM2_LIVE_POSTING", "NEW_IDEAS_LIVE_POSTING", "AIM2_TOPN_LIVE_POSTING"):
        assert gate not in SRC
    assert not re.search(r"MAX1_LIVE_POSTING\s*=\s*['\"]1['\"]", SRC)


def test_artifact_path_is_not_promoted_into_the_live_root_by_code():
    """Harte Regel 2: nothing in the bot or the tool copies an artifact into the live path."""
    tool = (ROOT / "tools/make_max1_artifact.py").read_text(encoding="utf-8")
    assert 'STAGING_DIR = os.path.join(ROOT, "staging_models")' in tool
    assert "shutil.copy" not in tool and "shutil.move" not in tool


def test_registered_in_watchdog():
    # Die Fleet-Prozessliste ist seit T-2026-CU-9050-091 in core/fleet.py
    # zentralisiert; der Watchdog konsumiert sie. Registrierung dort prüfen.
    fleet = (ROOT / "core/fleet.py").read_text(encoding="utf-8")
    assert '"script": "34_ai_max1_bot.py"' in fleet
    assert '"start_delay": 223' in fleet


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
