"""Standalone (DB-free) test for the regression-guard disarm semantics (P2.51).

Background (T-2026-CU-9050-076, split from P2.50): `guard.py verify` protects the
deterministic indicator seam. Its "not armed -> pass" escape hatch existed for the
pre-live-DB-freeze state, but it fired on *any* empty golden/ directory — so once
the guard was armed (goldens + manifest committed, 4765e25), deleting golden/ or
losing it in a merge silently disarmed the guard while the pre-commit hook stayed
green. The fix keys "legitimately not armed" on the manifest: manifest.json is
written by `refresh` next to the goldens, so manifest-present-but-goldens-gone is
the disarm signal and must exit 1, while manifest-absent stays a legitimate pass.

Honest evidence note: `test_manifest_present_but_goldens_missing_fails` is a true
witness of the bug — on the PRE-FIX guard.py, mode_verify returned 0 in exactly
this state, so this assertion (== 1) FAILS against the old code. The other two
cases pin the surrounding invariants so the fix stays surgical (the genuinely
never-armed repo still passes; the reverse goldens-without-fixtures case still
fails, guard.py:139-140, which this task must not touch).

The armed-and-matching happy path (goldens + fixtures + engine recompute) is
covered end-to-end by `python tools/regression_guard/guard.py smoke`; it needs the
numpy compute path and is out of scope for this disarm-semantics test.

Run: py -3.13 backtest/test_regression_guard_disarm.py
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD_DIR = ROOT / "tools" / "regression_guard"


def _load_guard_and_core():
    """Import guard.py by path and its rgcore helper. Neither touches the DB:
    guard.py's module-level imports are stdlib only (rgcore is imported lazily
    inside main(), which we bypass), and rgcore is pure file/array helpers."""
    if str(GUARD_DIR) not in sys.path:
        sys.path.insert(0, str(GUARD_DIR))
    import rgcore  # noqa: E402  (after sys.path setup)

    spec = importlib.util.spec_from_file_location("kythera_guard_cli_under_test", GUARD_DIR / "guard.py")
    assert spec and spec.loader, "cannot build import spec for guard.py"
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    return guard, rgcore


def _run_verify(guard, rgcore, tmp, *, write_manifest, golden_names, fixture_names):
    """Point the guard's directory globals at an isolated temp layout and run
    mode_verify. Golden/fixture files are created as empty .npz stubs — on the
    branches under test (no goldens, or goldens-without-fixtures) mode_verify
    returns before any file is deserialized, so stub content is never read."""
    golden_dir = os.path.join(tmp, "golden")
    fixtures_dir = os.path.join(tmp, "fixtures")
    os.makedirs(golden_dir, exist_ok=True)
    os.makedirs(fixtures_dir, exist_ok=True)

    guard.GOLDEN_DIR = golden_dir
    guard.FIXTURES_DIR = fixtures_dir
    guard.MANIFEST_PATH = os.path.join(golden_dir, "manifest.json")

    if write_manifest:
        rgcore.write_json(guard.MANIFEST_PATH, {"generated_at": "test", "git_commit": "test"})
    for name in golden_names:
        open(os.path.join(golden_dir, name), "wb").close()
    for name in fixture_names:
        open(os.path.join(fixtures_dir, name), "wb").close()

    return guard.mode_verify(rgcore)


def test_manifest_present_but_goldens_missing_fails():
    """P2.51 core: armed once (manifest committed) + goldens gone -> exit 1.
    Pre-fix this returned 0 (silent disarm); this assertion witnesses the bug."""
    guard, rgcore = _load_guard_and_core()
    with tempfile.TemporaryDirectory(prefix="kythera_guard_disarm_") as tmp:
        rc = _run_verify(guard, rgcore, tmp, write_manifest=True, golden_names=[], fixture_names=[])
    assert rc == 1, f"manifest-present-but-goldens-missing must exit 1 (silent disarm), got {rc}"


def test_no_manifest_no_goldens_still_passes():
    """The genuinely never-armed state (no manifest, no goldens) is the
    pre-live-DB-freeze condition and must still pass — the fix must not turn
    every empty golden/ into a hard fail."""
    guard, rgcore = _load_guard_and_core()
    with tempfile.TemporaryDirectory(prefix="kythera_guard_disarm_") as tmp:
        rc = _run_verify(guard, rgcore, tmp, write_manifest=False, golden_names=[], fixture_names=[])
    assert rc == 0, f"never-armed repo (no manifest) must pass, got {rc}"


def test_goldens_without_fixtures_still_fails():
    """The reverse inconsistency (goldens present, fixtures gone) is already
    handled at guard.py:139-140 and must stay exit 1 — pinned so this task's
    change does not regress it."""
    guard, rgcore = _load_guard_and_core()
    with tempfile.TemporaryDirectory(prefix="kythera_guard_disarm_") as tmp:
        rc = _run_verify(
            guard, rgcore, tmp, write_manifest=True, golden_names=["BTCUSDT_1h.npz"], fixture_names=[]
        )
    assert rc == 1, f"goldens-without-fixtures must exit 1, got {rc}"


if __name__ == "__main__":
    test_manifest_present_but_goldens_missing_fails()
    test_no_manifest_no_goldens_still_passes()
    test_goldens_without_fixtures_still_fails()
    print("OK — regression-guard disarm semantics hold (P2.51)")
