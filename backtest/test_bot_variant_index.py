# backtest/test_bot_variant_index.py
"""DB-free tests for tools/bot_variants/index.py (T-2026-KYT-9050-038, phase 1).

Pins the acceptance criteria from tools/bot_variants/SPEC.md:
  AK1  known tags → expected family/script/lifecycle
  AK2  unknown tag + non-classifiable file are COUNTED+listed
       (no silent drop, like bot_catalog)
  AK3  build_index deterministic/idempotent (no now()/randomness)
  AK4  shared filenames (one file under >1 tag) are flagged
  AK5  listed md5 == real md5 of the file on disk

Run: pytest backtest/test_bot_variant_index.py -v
     (fast: the tests use load_embedded=False → no joblib/xgboost)
"""

from __future__ import annotations

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.bot_catalog as bc  # noqa: E402
import core.shadow_gate as sg  # noqa: E402
import tools.bot_variants.index as ix  # noqa: E402


@pytest.fixture(scope="module")
def index():
    # load_embedded=False: sidecar meta only → fast, deterministic, without xgboost.
    return ix.build_index(load_embedded=False)


def _gen(index, tag):
    for g in index["generations"]:
        if g["tag"] == tag:
            return g
    raise AssertionError(f"generation {tag} not in index")


# ── AK1: known tags → family / script / lifecycle ────────────────────────────


@pytest.mark.parametrize(
    ("tag", "family", "script"),
    [
        ("RUB1", "RUB", "13_ai_rub_bot.py"),
        ("RUB2", "RUB", "13_ai_rub_bot.py"),
        ("ATB2", "ATB", "14_ai_atb_bot.py"),
        ("MIS1-8H", "MIS", "11_ai_mis_bot.py"),
        ("MIS2-24H", "MIS", "11_ai_mis_bot.py"),
        ("ABR2", "ABR", "18_ai_abr1_bot.py"),
        ("EPD2", "EPD", "10_pump_dump_detector.py"),
        ("SRA2", "SRA", "9_ai_sr_bot.py"),
    ],
)
def test_known_tag_family_and_script(index, tag, family, script):
    g = _gen(index, tag)
    assert g["family"] == family
    assert g["script"] == script


def test_family_for_tag_reverse_helper():
    # The new bot_catalog reverse helper that the index uses.
    assert bc.family_for_tag("RUB2") == "RUB"
    assert bc.family_for_tag("ABR2") == "ABR"  # longest-wins, not BR
    assert bc.family_for_tag("MIS1-8h") == "MIS"
    assert bc.family_for_tag("Main Channel") is None  # classic → no prefix
    assert bc.family_for_tag("TOTALLY_NEW_9000") is None


def test_lifecycle_matches_shadow_gate(index):
    # RUB1 was live-revived in both directions via T-037.
    assert _gen(index, "RUB1")["lifecycle"] == {"LONG": "live", "SHORT": "live"}
    # ATB2: LONG promoted+live to root by operator (T-037-promote, PR #189),
    # SHORT keeps collecting shadow.
    assert _gen(index, "ATB2")["lifecycle"] == {"LONG": "live", "SHORT": "shadow"}
    # MIS1-8H: LONG parked (shadow), SHORT (dump) live — exactly 1 generation per leg.
    assert _gen(index, "MIS1-8H")["lifecycle"] == {"LONG": "shadow", "SHORT": "live"}
    # AIM1 is retired.
    assert _gen(index, "AIM1")["lifecycle"]["LONG"] == "retired"


def test_lifecycle_values_are_exactly_shadow_gate(index):
    # The index does not invent any lifecycle state — it mirrors leg_status().
    for g in index["generations"]:
        for direction, status in g["lifecycle"].items():
            assert status == sg.leg_status(g["tag"], direction)


def test_code_ref_head_iff_live(index):
    # Phase-1 contract: code_ref=HEAD exactly when one direction is live.
    for g in index["generations"]:
        has_live = any(v == sg.LIVE for v in g["lifecycle"].values())
        assert g["code_ref"] == ("HEAD" if has_live else None)


# ── AK2: no silent drop — unknown tags + unclassified files ──────────────────


def test_unclassified_artifacts_counted_and_listed(index):
    # qm_xgboost_model_v2.pkl lives in root but is not assigned to any generation.
    assert index["unclassified_count"] == len(index["unclassified_artifacts"])
    names = {u["filename"] for u in index["unclassified_artifacts"]}
    assert "qm_xgboost_model_v2.pkl" in names
    # Sidecars/reports must NOT show up as unclassified.
    assert not any(n.endswith("_meta.json") for n in names)
    assert not any(n.endswith("_report.json") for n in names)


def test_threshold_sidecars_not_flagged_as_models(index):
    names = {u["filename"] for u in index["unclassified_artifacts"]}
    assert not any(n.startswith("threshold_") for n in names)


def test_unknown_tag_is_counted(monkeypatch):
    # A tag in the lifecycle register without a fleet script must be COUNTED.
    patched = dict(sg._LIFECYCLE)
    patched[("ZZZNEW9", "LONG")] = sg.SHADOW
    monkeypatch.setattr(sg, "_LIFECYCLE", patched)
    idx = ix.build_index(load_embedded=False)
    assert "ZZZNEW9" in idx["unknown_tags"]
    assert idx["unknown_tag_count"] == len(idx["unknown_tags"])
    g = _gen(idx, "ZZZNEW9")
    assert g["script"] is None
    assert any("unknown tag" in n for n in g["notes"])


# ── AK3: determinism / idempotency ────────────────────────────────────────────


def test_build_index_deterministic():
    a = ix.build_index(load_embedded=False)
    b = ix.build_index(load_embedded=False)
    assert ix._dump_json(a) == ix._dump_json(b)
    assert ix.render_markdown(a) == ix.render_markdown(b)


def test_generations_sorted_by_tag(index):
    tags = [g["tag"] for g in index["generations"]]
    assert tags == sorted(tags)


def test_no_timestamp_in_output(index):
    # No now()/date in the output lines (would break idempotency otherwise).
    md = ix.render_markdown(index)
    assert "generated" in md.lower()
    # trained_at (static file content) is ok; a render timestamp would not be.
    assert "generated_at" not in ix._dump_json(index)


# ── AK4: shared filenames (collision hazard) ──────────────────────────────────


def test_shared_filenames_flagged(index):
    shared = {s["filename"]: set(s["tags"]) for s in index["shared_filenames"]}
    # rub2_model_LONG.pkl: RUB2 retrain AND RUB3 challenger use the same file.
    assert shared.get("rub2_model_LONG.pkl") == {"RUB2", "RUB3"}
    # NOTE: epd2_model_LONG.pkl was EPD2+EPD3-shared until PR #189; since the
    # EPD3-LONG root promotion (own filename epd3_model_LONG.pkl) the
    # collision is resolved → no longer shared. The mechanism stays pinned
    # via rub2_model_LONG.pkl.
    assert "epd2_model_LONG.pkl" not in shared


# ── AK5: md5 == real file md5 ─────────────────────────────────────────────────


def test_md5_matches_real_file(index):
    checked = 0
    for g in index["generations"]:
        for a in g["artifacts"]:
            if not a["exists"]:
                continue
            abspath = os.path.join(ix.REPO_ROOT, a["path"])
            with open(abspath, "rb") as fh:
                real = hashlib.md5(fh.read()).hexdigest()  # noqa: S324
            assert a["md5"] == real, a["filename"]
            checked += 1
    assert checked > 0  # something was actually checked


def test_missing_artifact_has_null_md5(index):
    # EPD2 SHORT retrain (epd2_model_SHORT.pkl) does not exist on disk → MISSING.
    g = _gen(index, "EPD2")
    missing = [a for a in g["artifacts"] if a["filename"] == "epd2_model_SHORT.pkl"]
    assert missing and missing[0]["location"] == "MISSING"
    assert missing[0]["md5"] is None and missing[0]["exists"] is False
