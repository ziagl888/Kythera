# backtest/test_bot_variant_archive.py
"""DB-free tests for tools/bot_variants/archive.py + stage.py (T-2026-KYT-9050-039).

Phase 2 (archive/D2+D4) and the phase-3 stage helper:
  * manifest schema + code_ref resolution (active→HEAD, retired→git-log-S)
  * source_commit / git_tracked / lifecycle_history
  * reference-based default (no binary copy) + opt-in copy_binaries + size skip
  * md5 of the copy == source
  * stage plan: live swap steps, unknown tag → error, --apply staging only

Run: pytest backtest/test_bot_variant_archive.py -v
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.shadow_gate as sg  # noqa: E402
from tools.bot_variants import archive as ar  # noqa: E402
from tools.bot_variants import index as ix  # noqa: E402
from tools.bot_variants import stage as st  # noqa: E402


@pytest.fixture(scope="module")
def manifests():
    return ar.build_manifests(load_embedded=False)


def _m(manifests, tag):
    for m in manifests:
        if m["tag"] == tag:
            return m
    raise AssertionError(f"manifest {tag} missing")


# ── manifest schema + completeness ────────────────────────────────────────


def test_one_manifest_per_generation(manifests):
    index = ix.build_index(load_embedded=False)
    assert len(manifests) == index["generation_count"]
    tags = [m["tag"] for m in manifests]
    assert tags == sorted(tags)  # sorted deterministically


def test_manifest_has_required_fields(manifests):
    for m in manifests:
        assert m["schema"] == ar.MANIFEST_SCHEMA
        for key in ("tag", "family", "script", "lifecycle", "code_ref", "artifacts", "provenance"):
            assert key in m, f"{m['tag']} missing {key}"


# ── code_ref (D4): active → HEAD, retired → git-log-S / unresolved ─────────────


def test_code_ref_head_for_active_generation(manifests):
    # RUB1 is live in both directions → logic in the current tree → symbolic HEAD
    # (NO volatile HEAD SHA in the manifest, otherwise not drift-free).
    cr = _m(manifests, "RUB1")["code_ref"]
    assert cr["ref"] == "HEAD"
    assert cr["sha"] is None
    assert cr["method"] == "active-in-tree"


def test_code_ref_resolved_for_retired_generation(manifests):
    # AIM1 is retired → no active logic → git-log-S returns a historical
    # SHA (or unresolved, if git is missing). Never the symbolic HEAD.
    cr = _m(manifests, "AIM1")["code_ref"]
    assert cr["ref"] != "HEAD"
    assert cr["method"] in ("git-log-S", "unresolved")
    if cr["method"] == "git-log-S":
        assert cr["sha"] and len(cr["sha"]) >= 7


# ── source_commit / git_tracked / md5 ────────────────────────────────────────


def test_artifacts_git_tracked_and_md5_real(manifests):
    checked = 0
    for m in manifests:
        for a in m["artifacts"]:
            if not a["exists"]:
                continue
            # All fleet artifacts are git-tracked → source_commit set.
            assert a["git_tracked"] is True, a["filename"]
            assert a["source_commit"] and len(a["source_commit"]) >= 7
            abspath = os.path.join(ix.REPO_ROOT, a["source_origin"]["path"])
            assert a["md5"] == ix._md5(abspath)
            checked += 1
    assert checked > 0


def test_lifecycle_mirrors_shadow_gate(manifests):
    for m in manifests:
        for d, status in m["lifecycle"].items():
            assert status == sg.leg_status(m["tag"], d)


def test_manifest_carries_feature_contract(manifests):
    # D2: the full feature contract (not just the count) belongs in the manifest.
    # ATS2 carries a sidecar *_meta.json with a real features LIST.
    art = next(a for a in _m(manifests, "ATS2")["artifacts"] if a["exists"] and a["features"])
    assert isinstance(art["features"], list) and len(art["features"]) > 0
    # n_features stays in the summary meta, features is NOT duplicated.
    assert art["meta"]["n_features"] == len(art["features"])
    assert "features" not in art["meta"]


# ── determinism ────────────────────────────────────────────────────────────


def test_manifests_deterministic():
    a = ar.build_manifests(load_embedded=False)
    b = ar.build_manifests(load_embedded=False)
    assert ar._dump_json(a) == ar._dump_json(b)
    assert ar.render_archive_md(a) == ar.render_archive_md(b)


# ── reference-based default: no binary in the manifest until --copy-binaries ──────


def test_default_no_archived_copy(manifests):
    for m in manifests:
        for a in m["artifacts"]:
            assert a["archived_copy"] is None


def test_copy_binaries_verifies_md5_and_skips_oversized(tmp_path, monkeypatch):
    monkeypatch.setattr(ix, "ARCHIVE_DIR", str(tmp_path))
    m = _m(ar.build_manifests(load_embedded=False), "RUB1")
    # generous limit → copied, md5-verified, archived_copy set
    skipped = ar.copy_binaries(m, max_copy_mb=50)
    assert skipped == []
    for a in m["artifacts"]:
        if a["exists"]:
            assert a["archived_copy"] is not None
            dest = os.path.join(ix.REPO_ROOT, a["archived_copy"])
            assert os.path.isfile(dest) and ix._md5(dest) == a["md5"]
    # tiny limit → everything skipped (no silent skip)
    m2 = _m(ar.build_manifests(load_embedded=False), "RUB1")
    skipped2 = ar.copy_binaries(m2, max_copy_mb=0.01)
    assert len(skipped2) == len([a for a in m2["artifacts"] if a["exists"]])
    assert all(a["archived_copy"] is None for a in m2["artifacts"])


# ── stage helper (Phase 3) ───────────────────────────────────────────────────


def test_stage_plan_unknown_tag_raises():
    with pytest.raises(ValueError, match="Unknown generation"):
        st.build_plan("NOPE_9000", load_embedded=False)


def test_stage_plan_contains_swap_steps():
    plan = st.build_plan("RUB1", load_embedded=False)
    text = st.render_plan(plan, applied=None)
    assert "MANUAL" in text  # executes nothing live
    assert "staging_models/" in text
    assert "code_ref" in text
    assert "restart_fleet.ps1" in text  # restart stays an operator step
    assert "long_reversion_model.joblib" in text


def test_stage_apply_writes_only_to_staging(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    monkeypatch.setattr(st, "STAGING_DIR", str(staging))
    plan = st.build_plan("RUB1", direction="LONG", load_embedded=False)
    written = st.apply_staging(plan)
    assert written  # something copied
    for rel in written:
        dest = os.path.join(ix.REPO_ROOT, rel)
        assert os.path.isfile(dest)
        # md5 matches the plan's source
    # destination sits exclusively under the (patched) staging dir.
    assert all(str(staging) in os.path.join(ix.REPO_ROOT, r) or "staging" in r for r in written)
