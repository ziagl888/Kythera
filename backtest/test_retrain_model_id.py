# backtest/test_retrain_model_id.py
"""DB-free tests for the EPD generation tag on the retrainer (T-2026-KYT-9050-004).

`run_epd` wrote tag and filename as literals: `meta.model_id="EPD2"` and
`epd2_model_{LONG,SHORT}.pkl`. A retrain on the NEW (post-P1.39) feature
definition must not inherit either — hard rule 6 requires a new tag, and
the filename must move with it, otherwise the later promotion puts the new
artifact into the loader slot of the old generation (the EPD3-SHORT incident of
2026-07-21, tools/promotion_guard.py).

Pinned here:

  AK1  `artifact_slot` derives the filename prefix from the tag and matches
       `tools.promotion_guard.tag_prefix` (one convention, two modules)
  AK2  default `EPD2` leaves tag, filename and stats name unchanged (no-op)
  AK3  a new tag sets meta.model_id AND the filename TOGETHER
  AK4  the CLI rejects a `--model-id` on a strategy that is not wired up and
       accepts no tag that is not a model tag
  AK5  the chosen tag EPD4 is free in every code register (rule 6)

Run: pytest backtest/test_retrain_model_id.py -v
     python backtest/test_retrain_model_id.py
"""

from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.shadow_gate as sg  # noqa: E402
import tools.bot_variants.index as variant_index  # noqa: E402
import tools.promotion_guard as pg  # noqa: E402
import tools.retrain_from_replay as R  # noqa: E402

NEW_TAG = "EPD4"


# ── AK1: one convention, two modules ─────────────────────────────────────────


@pytest.mark.parametrize("tag", ["EPD2", "EPD4", "RUB3", "ATS2", "MIS2-8H", "MIS2-168H"])
def test_artifact_slot_matches_the_promotion_guard(tag):
    assert R.artifact_slot(tag) == pg.tag_prefix(tag)


def test_artifact_slot_normalises_case_and_dashes():
    assert R.artifact_slot("  epd4 ") == "epd4"
    assert R.artifact_slot("MIS2-8H") == "mis28h"


def test_the_slot_of_the_new_tag_is_not_a_foreign_loader_slot():
    """The actual point: `epd4_model_LONG.pkl` must not occupy a slot that
    another generation reads — otherwise ONE artifact posts under two tags."""
    fname = f"{R.artifact_slot(NEW_TAG)}_model_LONG.pkl"
    assert pg.check_staging_filename(fname)[0] == pg.OK
    assert fname not in pg.slot_claims()


# ── AK2/AK3: tag and filename move together ──────────────────────────────────


class _Recorder:
    """Captures WHERE run_epd writes and WHICH tag it puts in the meta,
    without training — the retrain runs themselves check the training."""

    def __init__(self):
        self.artifacts: list[str] = []
        self.metas: list[dict] = []

    def save_artifact(self, path, model, feature_cols, thresh, iso, meta):
        self.artifacts.append(os.path.basename(path))
        self.metas.append(dict(meta))


def _run_epd_recorded(tmp_path, model_id=None):
    """run_epd with a mini dataset and without real training/IO."""
    rec = _Recorder()
    events = tmp_path / "events.jsonl"
    events.write_text("", encoding="utf-8")

    import pandas as pd

    # Large enough for run_epd's minimum sizes (>=600 total, >=300 per
    # direction) and long enough that the 7d purge gap leaves non-empty slices —
    # exactly the condition on which this task's post-cut dataset fails.
    n = 2400
    df = pd.DataFrame(
        {
            "signal_time": pd.date_range("2026-01-01", periods=n, freq="3h"),
            "symbol": ["BTCUSDT"] * n,
            "direction": ["LONG" if i % 2 else "SHORT" for i in range(n)],
            "outcome": [i % 2 for i in range(n)],
            "net_pnl_pct": [0.1 if i % 2 else -0.1 for i in range(n)],
            **{c: [float(i % 7) for i in range(n)] for c in R.EPD2_FEATURES},
        }
    )

    def fake_train_binary(train, val, test, feature_cols, hyper=None, picker=None):
        return object(), object(), 0.5, {"deployable": True}, {"n_taken": 0}, []

    written: list[str] = []
    real_open = open

    def fake_open(path, *a, **kw):
        if isinstance(path, str) and path.endswith("_meta.json"):
            written.append(os.path.basename(path))
            path = os.path.join(str(tmp_path), os.path.basename(path))
        return real_open(path, *a, **kw)

    kwargs = {} if model_id is None else {"model_id": model_id}
    with (
        mock.patch.object(R, "load_replay", return_value=df),
        mock.patch.object(R, "train_binary", fake_train_binary),
        mock.patch.object(R, "save_artifact", rec.save_artifact),
        mock.patch.object(R, "top_importance", lambda *a, **kw: []),
        mock.patch("builtins.open", fake_open),
    ):
        result = R.run_epd(str(events), **kwargs)
    return rec, written, result


def test_default_keeps_the_epd2_slot_and_tag(tmp_path):
    rec, meta_files, result = _run_epd_recorded(tmp_path)
    assert rec.artifacts == ["epd2_model_LONG.pkl", "epd2_model_SHORT.pkl"]
    assert meta_files == ["epd2_model_LONG_meta.json", "epd2_model_SHORT_meta.json"]
    assert {m["model_id"] for m in rec.metas} == {"EPD2"}
    assert result["model_id"] == "EPD2"


def test_a_new_tag_moves_meta_and_filename_together(tmp_path):
    rec, meta_files, result = _run_epd_recorded(tmp_path, model_id=NEW_TAG)
    assert rec.artifacts == ["epd4_model_LONG.pkl", "epd4_model_SHORT.pkl"]
    assert meta_files == ["epd4_model_LONG_meta.json", "epd4_model_SHORT_meta.json"]
    assert {m["model_id"] for m in rec.metas} == {NEW_TAG}
    assert result["model_id"] == NEW_TAG
    # The feature contract stays the same — it is a new GENERATION, not
    # a new model design (hard rule 7).
    assert result["features"] == list(R.EPD2_FEATURES)


# ── AK4: the CLI does not swallow a wrong flag ────────────────────────────────


def _main_with(argv):
    with mock.patch.object(sys, "argv", ["retrain_from_replay.py", *argv]):
        R.main()


def test_model_id_on_a_non_epd_strategy_is_refused():
    with pytest.raises(SystemExit) as e:
        _main_with(["--strategy", "rub", "--model-id", "RUB5"])
    assert "epd" in str(e.value)


def test_a_non_tag_model_id_is_refused():
    for bad in ("epd 4", "4EPD", "epd_4", ""):
        with pytest.raises(SystemExit) as e:
            _main_with(["--strategy", "epd", "--model-id", bad])
        assert "model tag" in str(e.value), bad


def test_lowercase_model_id_is_normalised_to_the_register_casing(tmp_path):
    """core/shadow_gate keeps its keys UPPER — a lowercase tag in the
    meta would slip past the lifecycle lookup and post as default-LIVE."""
    rec, _, result = _run_epd_recorded(tmp_path, model_id="epd4")
    assert result["model_id"] == NEW_TAG
    assert {m["model_id"] for m in rec.metas} == {NEW_TAG}
    assert rec.artifacts == ["epd4_model_LONG.pkl", "epd4_model_SHORT.pkl"]


# ── AK5: EPD4 is free (hard rule 6) ──────────────────────────────────────────


def test_epd4_is_free_in_every_code_registry():
    """EPD1 (pre-rename), EPD2 (legacy direct post) and EPD3 (retrain challenger,
    LONG live) are taken. If any of these registrations flips, the tag of the
    next EPD generation must be re-chosen — hence pinned here."""
    occupied = (
        set(variant_index.legacy_artifact_slots())
        | set(sg.SHADOW_ARTIFACTS)
        | {tag for tag, _ in sg._LIFECYCLE}
        | set(sg._RETIRED_TAGS)
    )
    epd_tags = {t for t in occupied if t.upper().startswith("EPD")}
    assert epd_tags == {"EPD1", "EPD2", "EPD3"}, f"EPD tag assignment has changed: {sorted(epd_tags)}"
    assert NEW_TAG not in occupied
    # Default LIVE is the gate's safety contract: an unregistered tag
    # is live, not shadow. An EPD4 emission therefore needs an explicit line
    # in _LIFECYCLE BEFORE the first post.
    assert sg.leg_status(NEW_TAG, "LONG") == sg.LIVE


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
