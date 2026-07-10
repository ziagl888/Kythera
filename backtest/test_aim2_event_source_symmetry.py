"""Standalone (DB-free) guard: the AIM2 event source is defined IDENTICALLY in
trainer and serving.

AIM2_DESIGN.md §3 and the serving comment both claim „identische Definition wie
im Trainer" for the posted-AI event stream. That invariant is load-bearing: if
the trainer ingests events the bot would never treat as candidates (or vice
versa), the model is trained on a different population than it scores at serve
time. The concrete F6 failure it prevents — a future AIM2 retrain labelling its
own meta-gate outputs (AIM1/AIM2/AIM2-TOPN) as training events (T-2026-CU-9050-065,
follow-up to -051).

The trainer (tools/aim2_build_dataset.py) needs a live DB to run, so this pins
the contract statically: both the trainer's `load_events` and the bot's
`load_signal_stream` must exclude exactly the meta-gate tags AIM1, AIM2 and the
AIM2-TOPN routing tag from the posted-AI query.

Run: python backtest/test_aim2_event_source_symmetry.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.aim2_topn import MODEL_TAG  # noqa: E402

TRAINER = (ROOT / "tools" / "aim2_build_dataset.py").read_text(encoding="utf-8")
SERVING = (ROOT / "15_ai_master_bot.py").read_text(encoding="utf-8")

# The exact NOT IN clause both sides must carry (AIM2-TOPN rides in as the %s
# bind so the tag stays single-sourced from core.aim2_topn.MODEL_TAG).
_EXCLUSION = "model_name NOT IN ('AIM1', 'AIM2', %s)"


def test_topn_tag_is_the_expected_routing_tag():
    assert MODEL_TAG == "AIM2-TOPN"


def test_trainer_excludes_all_meta_gate_tags():
    assert _EXCLUSION in TRAINER, "trainer load_events no longer excludes AIM1/AIM2/AIM2-TOPN (F6)"
    assert "(TOPN_TAG, since)" in TRAINER, "trainer must bind the TOPN tag from core.aim2_topn"
    assert "from core.aim2_topn import MODEL_TAG as TOPN_TAG" in TRAINER


def test_serving_excludes_all_meta_gate_tags():
    assert _EXCLUSION in SERVING, "serving load_signal_stream no longer excludes AIM1/AIM2/AIM2-TOPN (F6)"


def test_neither_side_uses_the_old_aim1_only_filter():
    # The pre-fix trainer filter `model_name <> 'AIM1'` let AIM2/AIM2-TOPN leak in.
    old = re.compile(r"model_name\s*<>\s*'AIM1'")
    assert not old.search(TRAINER), "trainer still uses the AIM1-only filter — AIM2/AIM2-TOPN leak into training"
    assert not old.search(SERVING)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
