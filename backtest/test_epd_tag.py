"""Standalone (DB-free) guard for the EPD model-tag and artifact contract.

Background (T-2026-CU-9050-042, finding P1.45 side-note): 10_pump_dump_detector
loaded pump_dump_model.pkl as a raw model, read no meta and posted under the
source constant "EPD2". Unlike its siblings the gap was not just a dropped tag —
the retrain OUTPUT format and the live LOAD format had diverged:

  * live: ONE raw 3-class model, 10 features as a POSITIONAL array, success =
    class 2 (pump) / class 0 (dump), threshold hardcoded 0.60;
  * epd2_model_{LONG,SHORT}.pkl: per-direction BINARY dict-artifacts, features
    BY NAME including the 6 funding columns, threshold + model_id in the meta.

Rolling the artifacts out against the old load path would have either broken the
bot or posted the new generation under the old tag. So the load path had to move
first; the tag falls out of it.

What this guard protects:
  * both paths coexist — the legacy branch keeps its positional order and its
    3-class success semantics (it is what runs live right now);
  * with an artifact deployed, tag AND threshold come from its meta (rule 6);
  * the funding features are served as-of the event, mirroring the dataset
    builder, and a missing funding HISTORY yields 0 exactly as the trainer's
    fillna(0) — while a missing feature NAME still refuses the artifact (P0.12);
  * the shadow-log dedupe probes the legacy tag too, because the tag is the
    dedupe key and it flips on the generation switch;
  * an active-trade check against ai_signals stops a second live position on a coin
    whose trade outlived the in-memory 900s timer (T-2026-CU-9050-055), and the
    funding load goes through the value-neutral settlement-bound cache.

Run: python backtest/test_epd_tag.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.signal_post import log_prediction  # noqa: E402

SRC = (ROOT / "10_pump_dump_detector.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- tag contract


def test_tag_and_threshold_come_from_the_artifact():
    assert re.search(r"module_tag\s*=\s*best_art\[[\"']tag[\"']\]", SRC), (
        "the posting tag is no longer read from the artifact meta — an EPD3 retrain would post as EPD2"
    )
    assert re.search(r"post_threshold\s*=\s*float\(best_art\[[\"']threshold[\"']\]\)", SRC), (
        "the posting threshold no longer comes from the artifact — the retrained operating point is ignored"
    )
    assert not re.search(r"^\s*module_tag\s*=\s*[\"']EPD2[\"']", SRC, re.MULTILINE), (
        "module_tag is a hardcoded literal again (rule 6)"
    )
    assert re.search(r"^EPD_LEGACY_TAG\s*=\s*[\"']EPD2[\"']", SRC, re.MULTILINE), "EPD_LEGACY_TAG constant missing"


def test_artifacts_load_through_the_shared_loader_with_the_feature_contract():
    assert re.search(r"load_artifact\(path,\s*EPD_EXPECTED_FEATURES,\s*EPD_LEGACY_TAG\)", SRC), (
        "the EPD2 artifacts no longer go through the shared loader / feature contract"
    )
    assert re.search(r"^EPD_EXPECTED_FEATURES\s*=\s*EPD_BASE_FEATURES\s*\+\s*list\(FUNDING_FEATURES\)", SRC, re.M), (
        "the expected-feature contract no longer covers the funding columns the EPD2 artifact demands"
    )


def test_missing_model_idles_instead_of_crashing():
    assert re.search(r"if model is None and not epd2:\s*\n\s*return", SRC), "the idle guard is gone (Falle 3)"


# ------------------------------------------------------- legacy path must survive


def test_legacy_positional_feature_order_is_preserved():
    """The live 3-class model takes an unnamed array. Reordering EPD_BASE_FEATURES
    or building the array from a dict literal in another order silently feeds it
    the wrong columns. Since T-2026-CU-9050-060 the array reads from the
    NULL-contract-imputed view of base_features (rsi -> 50, rest -> 0 on
    non-finite; see backtest/test_nan_feature_guards.py) — the order contract
    is unchanged."""
    assert re.search(r"np\.array\(\[\[imputed\[c\] for c in EPD_BASE_FEATURES\]\]\)", SRC), (
        "the legacy feature array is no longer built from EPD_BASE_FEATURES in contract order"
    )
    assert re.search(r"for c, v in base_features\.items\(\)", SRC), (
        "the imputed dict no longer derives from base_features — wrong column source"
    )
    order = re.search(r"^EPD_BASE_FEATURES = \[(.*?)\]", SRC, re.S | re.M).group(1)
    names = re.findall(r"[\"'](\w+)[\"']", order)
    assert names == [
        "vol_ratio",
        "p_chg_60s",
        "buy_pres",
        "volat",
        "sample_fill",
        "rsi",
        "tsi",
        "macd",
        "e9_dist",
        "e21_dist",
    ], f"EPD_BASE_FEATURES order changed — the live 3-class model would be fed shuffled columns: {names}"


def test_legacy_three_class_semantics_are_untouched():
    assert re.search(r"prob_dump = prob\[classes\.index\(0\)\]", SRC), "legacy dump class lookup changed"
    assert re.search(r"prob_pump = prob\[classes\.index\(2\)\]", SRC), "legacy pump class lookup changed"
    assert re.search(r"module_tag = EPD_LEGACY_TAG", SRC), "the legacy branch no longer posts under the legacy tag"


# --------------------------------------------------------- EPD2 serving semantics


def test_epd2_uses_binary_success_probability():
    assert re.search(r"art\[[\"']model[\"']\]\.predict_proba\(ml_input\)\[0,\s*1\]", SRC), (
        "the EPD2 branch no longer reads the binary success column predict_proba[:, 1]"
    )


def test_funding_features_are_served_asof_the_event_through_the_cache():
    """tools/epd2_build_dataset.py takes funding as-of the event timestamp; the live
    event IS this tick, so serving must use `now`, not a candle boundary.

    The funding features are model INPUT, so the load cannot move behind the trade
    decision — what the settlement-bound cache removes is the repetition (T-2026-CU-9050-055).
    Its values are identical; see backtest/test_funding_cache.py."""
    assert re.search(r"funding_features_cached\(conn,\s*symbol,\s*now\)", SRC), (
        "funding is no longer served as-of the event through the settlement-bound cache"
    )
    assert not re.search(r"\bload_funding\(", SRC), (
        "a raw load_funding call is back in the 10s loop — the cache was bypassed"
    )


def test_active_trade_check_blocks_a_second_position():
    """T-2026-CU-9050-055: the 900s re-fire timer is a FREQUENCY guard and lives only
    in memory. An EPD trade routinely outlives it, so without a check against the open
    trades in ai_signals the next signal opens a SECOND live position on the same coin.
    Same shape as 11_ai_mis_bot.py:318 and 13_ai_rub_bot.py."""
    assert re.search(
        r"SELECT 1 FROM ai_signals\s*\n\s*WHERE symbol = %s AND direction = %s AND model IN \(%s, %s\)", SRC
    ), "the active-trade check against ai_signals is gone — a trade outliving the 900s timer could re-fire"
    assert re.search(r"if cur\.fetchone\(\):\s*\n\s*return", SRC), (
        "the active-trade check no longer skips the signal when an open trade exists"
    )


def test_active_trade_check_uses_the_posting_tag_and_the_legacy_tag():
    """The check keys on the tag, so it must bind the tag the post path writes plus the
    pre-fix tag. On an EPD3 rollout an open EPD2 position would otherwise stop blocking,
    reopening exactly the hole the guard closes."""
    assert re.search(r"\(symbol,\s*best_direction,\s*module_tag,\s*EPD_LEGACY_TAG\)", SRC), (
        "the active-trade check no longer binds (module_tag, EPD_LEGACY_TAG)"
    )


def test_active_trade_check_runs_before_the_shadow_log():
    """Like MIS/RUB the guard suppresses the shadow row too — the open trade is what
    matters, not the log line. Below the branching it would post first."""
    assert SRC.index("SELECT 1 FROM ai_signals") < SRC.index("if best_prob < EPD_SHADOW_THRESHOLD"), (
        "the active-trade check moved below the shadow/post branching"
    )


def test_missing_funding_history_is_zeroed_like_the_trainer():
    """Trainer parity: train_binary fits on feature_cols.fillna(0). A missing
    funding HISTORY is a missing value (→ 0), not a missing contract column."""
    assert re.search(r"pd\.DataFrame\(\[feats\]\)\.reindex\(columns=art\[[\"']features[\"']\]\)\.fillna\(0\)", SRC), (
        "the EPD2 serving frame no longer mirrors the trainer's fillna(0) over missing funding values"
    )


# --------------------------------------------------- transitional dedup (behaviour)


class _FakeCursor:
    def __init__(self, sink, hit):
        self.sink, self.hit = sink, hit

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sink.append((" ".join(sql.split()), params))

    def fetchone(self):
        return (1,) if self.hit else None


class _FakeConn:
    def __init__(self, hit=False):
        self.calls: list = []
        self.hit = hit

    def cursor(self):
        return _FakeCursor(self.calls, self.hit)


def test_shadow_dedupe_probes_both_tags_on_a_generation_switch():
    conn = _FakeConn(hit=False)
    log_prediction(conn, "EPD3", "BTCUSDT", "LONG", 1.0, 0.4, posted=False, legacy_tag="EPD2")
    select_sql, select_params = conn.calls[0]
    assert "model_name IN (%s, %s)" in select_sql, f"dedupe does not probe both tags: {select_sql}"
    assert select_params[2] == "EPD3" and select_params[3] == "EPD2", select_params

    insert_sql, insert_params = conn.calls[1]
    assert "INSERT INTO ml_predictions_master" in insert_sql
    assert insert_params[0] == "EPD3", "the row must be written under the NEW tag, never the legacy one"


def test_shadow_dedupe_stays_single_tag_when_the_tags_agree():
    """Today's no-op case: identical tags must not grow a second bind."""
    for legacy in ("EPD2", None):
        conn = _FakeConn(hit=False)
        log_prediction(conn, "EPD2", "BTCUSDT", "LONG", 1.0, 0.4, posted=False, legacy_tag=legacy)
        select_sql, params = conn.calls[0]
        assert "model_name = %s" in select_sql, f"legacy_tag={legacy!r} changed the no-op query: {select_sql}"
        assert len(params) == 4


def test_shadow_dedupe_suppresses_the_row_when_the_legacy_tag_is_still_in_the_window():
    """The point of the transitional bind: an EPD2 row inside the 4h window must
    stop the freshly tagged EPD3 row from being written."""
    conn = _FakeConn(hit=True)
    log_prediction(conn, "EPD3", "BTCUSDT", "LONG", 1.0, 0.4, posted=False, legacy_tag="EPD2")
    assert len(conn.calls) == 1, "a duplicate shadow row was written despite a hit on the legacy tag"


def test_epd_passes_the_legacy_tag_into_the_shadow_log():
    assert re.search(r"legacy_tag=EPD_LEGACY_TAG", SRC), (
        "the EPD shadow log no longer carries the transitional dedupe tag"
    )


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("OK — EPD model-tag + artifact contract holds")
