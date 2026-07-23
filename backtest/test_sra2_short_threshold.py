"""Pin the SRA2-SHORT operating threshold (T-2026-KYT-9050-036).

SRA2-SHORT was promoted LIVE (T-033) but its staging meta carried
``optimal_threshold: null`` (stale training verdict — dead closed_trades3 label
source). null means the serving emits on EVERY candidate (Cornix flood) AND the
strict LIVE loader ``build_contract`` crashes on ``float(None)``. The operator set
0.58 from the live-realized SRA2-SHORT trades (+0.9%/trade, 88% WR).

These tests are DB-free: they pin the meta value + that it flows through the
shadow_gate loader as the serving threshold.
"""

from __future__ import annotations

import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

META_PATH = os.path.join(REPO_ROOT, "staging_models", "sra2_model_SHORT_meta.json")
EXPECTED = 0.58


def test_meta_threshold_set_and_floatable():
    """The staging meta carries optimal_threshold=0.58 as a real float — no more
    null (no flood) and build_contract's ``float(optimal_threshold)`` cannot crash."""
    with open(META_PATH, encoding="utf-8") as fh:
        meta = json.load(fh)
    thr = meta.get("optimal_threshold")
    assert thr is not None, "optimal_threshold is null → flood + build_contract crash"
    assert float(thr) == EXPECTED, f"expected {EXPECTED}, got {thr}"
    assert 0.0 < float(thr) < 1.0, "threshold out of a sane probability range"
    # provenance line present so the live-realized origin is not confused with training
    assert "optimal_threshold_source" in meta, "missing provenance for the operator-set threshold"
    print(f"  ok: staging meta optimal_threshold = {thr} (float, provenance present)")


def test_loader_surfaces_threshold_for_serving():
    """The shadow_gate loader must surface 0.58 as the serving threshold for the
    SRA2-SHORT staging artifact. is_live routes LIVE legs to root; we force the
    staging path so the test exercises the real .json+_meta.json loader end-to-end."""
    try:
        import xgboost  # noqa: F401
    except Exception:
        print("  skip: xgboost unavailable in this env — meta test covers the value")
        return
    from core import shadow_gate

    orig_is_live = shadow_gate.is_live
    shadow_gate.is_live = lambda tag, direction: False  # force staging routing
    try:
        art = shadow_gate.load_shadow_artifact("SRA2", "SHORT")
    finally:
        shadow_gate.is_live = orig_is_live
    assert art is not None, "SRA2-SHORT staging artifact failed to load"
    assert art["threshold"] == EXPECTED, f"loader threshold {art['threshold']} != {EXPECTED}"
    assert shadow_gate.artifact_threshold(art) == EXPECTED
    print(f"  ok: load_shadow_artifact('SRA2','SHORT').threshold = {art['threshold']}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"Running {len(fns)} tests...")
    for fn in fns:
        fn()
    print(f"\nAll {len(fns)} tests passed.")
