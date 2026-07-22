"""Standalone (DB-free) guard for the SRA model-tag and feature contract.

Background (T-2026-CU-9050-042, finding P1.45 side-note): 9_ai_sr_bot loaded its
XGB-JSON models raw, read no meta and posted both directions under the source
constant 'SRA1'. An SRA2 rollout would have merged with SRA1 in ai_signals and in
the per-bot win rate the orchestrator gates on (versioning rule 6).

Two things this guard protects:

  * The posting tag comes from the artifact meta (model_id), and it is the same
    value that reaches ai_signals.model, ml_predictions_master.model_name and the
    cooldown key. The legacy tag rides along in both dedupe probes, because the
    tag IS the dedupe key and it flips on the generation switch.
  * The SRA2 feature semantics live in ONE builder (core/sra_features.py) shared
    by trainer and serving (X-R1). This is not cosmetic: the old bot vector and
    the SRA2 builder disagree on what `pct_ema9` MEANS (denominator close vs
    ema9) and the bot never built macd_dif_pct/macd_dea_pct/atr_pct at all.
    Serving the SRA2 model the bot's own vector would have fed it foreign numbers
    under familiar names.

The static checks are the load-bearing net: a runtime assertion would be
swallowed by the fleet-wide broad except blocks (lesson from T-2026-CU-9050-024).

Run: python backtest/test_sra_tag.py
"""

import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.sra_features import SRA2_FEATURES, build_sra2_features  # noqa: E402

SRC = (ROOT / "9_ai_sr_bot.py").read_text(encoding="utf-8")
TRAINER = (ROOT / "tools" / "retrain_sra2.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- tag contract


def test_tag_comes_from_the_artifact_not_a_constant():
    assert re.search(r"module_name\s*=\s*artifact\[['\"]tag['\"]\]", SRC), (
        "the posting tag is no longer read from the artifact — an SRA3 retrain would post as SRA2"
    )
    assert not re.search(r"module_name\s*=\s*['\"]SRA\d['\"]", SRC), "module_name is a hardcoded literal again (rule 6)"


def test_artifacts_load_through_the_shared_json_loader():
    """load_artifact_json resolves tag = meta.model_id and validates the feature
    contract; hand-rolling xgb.XGBClassifier().load_model() skips both."""
    assert re.search(r"load_artifact_json\(path,\s*expected,\s*SRA_LEGACY_TAG,\s*SRA_LEGACY_THRESHOLD\)", SRC), (
        "the SRA artifacts no longer go through the shared loader"
    )
    assert not re.search(r"xgb\.XGBClassifier\(\)", SRC), "the bot hand-builds an XGB container again"
    assert re.search(r"^SRA_LEGACY_TAG\s*=\s*['\"]SRA1['\"]", SRC, re.MULTILINE), "SRA_LEGACY_TAG constant missing"


def test_threshold_comes_from_the_artifact():
    """A retrain picks its operating point on the validation slice. A hardcoded
    0.65 would silently override it."""
    assert re.search(r"if conf >= artifact\[['\"]threshold['\"]\]", SRC), (
        "the posting threshold is no longer the artifact's — the retrained operating point is ignored"
    )


def test_missing_artifact_idles_instead_of_exiting():
    """Falle 3: a bot without an artifact starts and does nothing (no watchdog
    restart loop). The old code called exit(1)."""
    assert re.search(r"if not artifact or not artifact\[['\"]loaded['\"]\]:\s*\n\s*continue", SRC), (
        "the per-direction idle guard is gone"
    )
    assert not re.search(r"^\s*exit\(1\)", SRC, re.MULTILINE), (
        "a failed model load exits the process again instead of idling"
    )


# ------------------------------------------------------------ transitional dedup


def test_master_log_dedupe_covers_the_legacy_tag():
    """The tag is the dedupe key. Without the old tag in the IN, an SRA2 rollout
    would consider every already-processed trade fresh and post it a second time."""
    assert re.search(r"SELECT 1 FROM ml_predictions_master WHERE trade_id = %s AND model_name IN \(%s, %s\)", SRC), (
        "the master-log duplicate check no longer probes both tags"
    )
    assert re.search(r"\(t_id,\s*module_name,\s*SRA_LEGACY_TAG\)", SRC), (
        "the duplicate check no longer binds (module_name, SRA_LEGACY_TAG)"
    )


def test_active_trade_check_blocks_a_second_position():
    """T-2026-CU-9050-055: the 4h cooldown is a FREQUENCY guard, not a position guard,
    and the trade_id duplicate check only stops the SAME setup being scored twice — not
    a NEW setup on a coin that already carries an open trade. Without the ai_signals
    probe the second setup opens a second live position (the RUB lesson, T-043)."""
    assert re.search(r"SELECT 1 FROM ai_signals WHERE symbol = %s AND direction = %s AND model IN \(%s, %s\)", SRC), (
        "the active-trade check against ai_signals is gone"
    )
    assert re.search(r"\(coin,\s*direction,\s*module_name,\s*SRA_LEGACY_TAG\)", SRC), (
        "the active-trade check no longer binds (module_name, SRA_LEGACY_TAG) — it lost the posting or the legacy tag"
    )


def test_active_trade_check_runs_before_the_expensive_prediction():
    """SRA knows the direction up front (it comes from active_trades_master), so the
    guard belongs before the indicator fetch and predict_proba.

    Anchored on the actual call, not on the string `predict_proba` — that also appears
    in the P1.20 comment far above and would make the ordering assertion vacuous."""
    check = SRC.index("SELECT 1 FROM ai_signals")
    # _emit_sra2_shadow (T-2026-CU-9050-125) also calls get_indicators_at_time ABOVE
    # the main loop, so a bare .index() would anchor on that occurrence instead of the
    # process_ai_trade one and make this ordering assertion meaningless. Search for the
    # main-loop occurrences from the active-trade check onward (T-2026-KYT-9050-020).
    inds = SRC.index("inds = get_indicators_at_time", check)
    predict = SRC.index("artifact['model'].predict_proba", check)
    assert check < inds < predict, "the active-trade check moved below the indicator fetch / prediction"


def test_cooldown_covers_the_legacy_tag():
    """Same story on the cooldown: a fresh SRA1 row must keep blocking an SRA2
    signal on the same coin, or Cornix opens a second live position."""
    assert re.search(
        r"cooldown_tags\s*=\s*\[module\]\s*if\s*module\s*==\s*SRA_LEGACY_TAG\s*else\s*\[module,\s*SRA_LEGACY_TAG\]",
        SRC,
    ), "the cooldown no longer probes the legacy tag on a generation switch"
    assert re.search(
        r"any\(check_cooldown\(conn,\s*t,\s*symbol,\s*direction,\s*4\)\s*for\s*t\s*in\s*cooldown_tags\)", SRC
    ), "the cooldown check no longer blocks when EITHER tag is still cooling down"


# ---------------------------------------------------------- shared feature builder


def test_trainer_and_bot_import_the_same_builder():
    """X-R1: one builder for trainer and serving. Two copies drift."""
    assert re.search(r"from core\.sra_features import .*build_sra2_features", SRC), (
        "the bot no longer imports the shared SRA2 builder"
    )
    assert re.search(r"from core\.sra_features import .*build_sra2_features", TRAINER), (
        "the trainer no longer imports the shared SRA2 builder — it grew a private copy again"
    )
    assert not re.search(r"^def build_features\(", TRAINER, re.MULTILINE), (
        "tools/retrain_sra2.py defines its own feature builder again"
    )


def test_artifact_frame_uses_the_shared_builder_legacy_frame_does_not():
    """The SRA2 frame must come from build_serving_row (shared semantics); the
    legacy frame must stay the old vector, bit-for-bit — it is the contract of
    the model that is deployed right now."""
    assert re.search(r"serving = build_serving_row\(direction, inds\)", SRC), (
        "the artifact frame no longer goes through the shared builder"
    )
    assert re.search(r"X = pd\.DataFrame\(\[serving\]\)\[artifact\[['\"]features['\"]\]\]", SRC), (
        "the artifact frame is no longer aligned to the artifact's feature contract"
    )
    assert re.search(r"else:\s*\n\s*X = pd\.DataFrame\(\[features\]\)", SRC), (
        "the legacy frame changed — the currently deployed SRA1 model would see a different vector"
    )
    assert not re.search(r"\.fillna\(", SRC), (
        "a fillna() call crept into the serving path — missing contract columns must idle the bot (P0.12)"
    )


def test_builder_emits_exactly_the_contract():
    row = build_sra2_features({"close": 100.0, "atr_14": 2.0, "ema_9": 99.0})
    assert set(row.keys()) == set(SRA2_FEATURES), f"builder/contract drift: {set(row) ^ set(SRA2_FEATURES)}"


def test_pct_features_are_scale_free_against_their_reference():
    """The SRA2 semantics: (value - reference) / REFERENCE. The old bot vector
    divided by close instead. Same name, different number.

    The inputs are chosen so the two denominators DISAGREE (close=150, ema9=100
    → 50.0 vs 33.3). With close == reference both formulas coincide and the test
    would pass against the wrong builder."""
    row = build_sra2_features({"close": 150.0, "ema_9": 100.0, "atr_14": 1.0})
    assert math.isclose(row["pct_ema9"], 50.0), (
        f"pct_ema9 must be (close-ema9)/ema9*100 = 50.0, got {row['pct_ema9']} "
        f"({100 / 3:.1f} would mean the old close-denominator semantics)"
    )
    # atr_pct = ((atr + close) - close) / close * 100 = atr/close*100
    row = build_sra2_features({"close": 200.0, "atr_14": 4.0})
    assert math.isclose(row["atr_pct"], 2.0), f"atr_pct must be atr/close*100, got {row['atr_pct']}"


def test_missing_inputs_stay_nan_and_are_never_zero_faked():
    """XGBoost handles NaN natively; a 0 would be an invented observation."""
    row = build_sra2_features({"close": 100.0})  # kein ATR, keine EMA
    assert math.isnan(row["pct_ema9"]), "missing ema_9 must yield NaN, not 0.0"
    for col in ("support_atr", "resist_atr", "boll_width_atr"):
        assert math.isnan(row[col]), f"{col} must be NaN without ATR, not 0.0"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("OK — SRA model-tag + feature contract holds")
