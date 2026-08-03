#!/usr/bin/env python3
# tools/retag_artifact.py — re-dump a dict-pkl artifact under a new
# meta.model_id (T-2026-KYT-9050-057, part 2).
#
# THE PROBLEM (hard rule 6):
# A challenger inherits its generation trainer's meta on retrain. The
# promoted `epd3_model_SHORT.pkl` carries an embedded `meta.model_id='EPD2'` —
# the tag of the PREVIOUS generation. Live this is inert today (bot 10 passes
# 'EPD3' explicitly at the call site, and the shadow loader does not read model_id),
# but `core.model_artifacts.build_contract` reads the posting tag ONLY from
# `meta.model_id` — every report/verifier that goes through the loader attributes
# the artifact to the wrong generation. `tools/verify_staging_artifacts.py`
# checks exactly this field (check 3, HR-6).
#
# WHAT THIS TOOL DOES: it loads a format-A artifact (dict-pkl), sets ONLY
# `meta.model_id` and writes the result to STAGING. No retrain, no
# refit — model, calibrator, feature list, threshold and every other meta field
# pass through unchanged. After writing, the result is reloaded and
# verified against the source (scores on a probe matrix, calibrator curve,
# meta diff) — a re-dump that changes anything else is a bug.
#
# TWO GUARDS, neither can be switched off:
#
#   1. HARD RULE 2 — writes go ONLY to STAGING_DIR. Promotion into
#      the repo root (= live) stays Michi's decision, never part of a tool
#      run. An --out outside staging aborts.
#   2. VERSION PARITY — a re-dump is a RE-SERIALISATION: the objects
#      are re-pickled with the sklearn/xgboost of the RUNNING interpreter
#      version. If it runs under a different sklearn version than the one that
#      produced the artifact, it writes the estimator internals back in a different
#      state format — for the EPD3-LONG artifact (sklearn 1.9.0, dumped in the
#      3.14 env) a re-dump under fleet Python 3.13 (sklearn 1.7.1) would be a
#      downgrade of the isotonic calibrator. Hence: it loads the artifact with
#      sklearn's own `InconsistentVersionWarning`, aborts the tool and
#      names the interpreter under which the re-dump would be clean. For
#      `epd3_model_SHORT.pkl` (embedded sklearn 1.7.1) a run under
#      py -3.13 is a same-version round trip and therefore warning-free.
#
# Invocation (fleet Python — the same version that serves the artifact):
#   py -3.13 tools/retag_artifact.py epd3_model_SHORT.pkl --model-id EPD3

from __future__ import annotations

import argparse
import os
import sys
import warnings

import joblib
import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Format-A contract keys (tools/retrain_from_replay.save_artifact).
ARTIFACT_KEYS = ("model", "features", "optimal_threshold", "calibrator_isotonic", "meta")

# Probe grid for the equivalence check: fixed seed ⇒ reproducible, and the
# check runs on the SCORES rather than object identity (which a re-dump by
# definition does not preserve).
PROBE_ROWS = 64
PROBE_SEED = 20260801
CALIB_GRID = np.linspace(0.0, 1.0, 101)


def staging_dir() -> str:
    """The staging folder for this checkout (convention from core.shadow_gate:
    repo-relative `staging_models`, overridable via KYTHERA_STAGING_DIR)."""
    return os.path.abspath(os.environ.get("KYTHERA_STAGING_DIR") or os.path.join(REPO_ROOT, "staging_models"))


def resolve_out(source: str, out: str | None) -> str:
    """Target path in STAGING. `--out` may be a bare filename; anything that
    does not land in STAGING is an abort (hard rule 2)."""
    target_dir = staging_dir()
    if target_dir == os.path.abspath(REPO_ROOT):
        raise SystemExit(f"Refuse: STAGING_DIR points to the repo root ({target_dir}) — that would be a promotion.")
    path = os.path.abspath(
        out if out and os.path.dirname(out) else os.path.join(target_dir, os.path.basename(out or source))
    )
    if os.path.dirname(path) != target_dir:
        raise SystemExit(
            f"Refuse: target is not in STAGING_DIR.\n  Target:  {path}\n  Staging: {target_dir}\n"
            "  Promoting an artifact into the repo root is an operator decision (hard rule 2)."
        )
    return path


def load_checked(path: str) -> dict:
    """Loads a format-A artifact and enforces version parity (guard 2).

    sklearn reports a serialisation skew itself: `InconsistentVersionWarning`
    carries the version under which the object was pickled. No warning means
    the running interpreter writes the same state format as the one it just
    read — exactly the condition for a lossless re-dump.
    """
    from sklearn.exceptions import InconsistentVersionWarning

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        art = joblib.load(path)
    skew = [w.message for w in caught if isinstance(w.message, InconsistentVersionWarning)]
    if skew:
        import sklearn

        origins = sorted({str(getattr(m, "original_sklearn_version", "?")) for m in skew})
        names = sorted({str(getattr(m, "estimator_name", "?")) for m in skew})
        raise SystemExit(
            f"Refuse: {os.path.basename(path)} was pickled with sklearn {', '.join(origins)}, "
            f"this interpreter runs {sklearn.__version__} (affected: {', '.join(names)}).\n"
            "  A re-dump would not be a round trip but a format change of the estimator internals.\n"
            f"  Run again under the interpreter with sklearn {origins[0]} — or retrain the artifact."
        )
    if not isinstance(art, dict) or not all(k in art for k in ("model", "features", "meta")):
        raise SystemExit(f"Refuse: {os.path.basename(path)} is not a format-A dict artifact (keys: {_keys(art)}).")
    return art


def _keys(art) -> str:
    return ", ".join(sorted(art)) if isinstance(art, dict) else type(art).__name__


def scores(art: dict) -> np.ndarray:
    """Model output on a deterministic probe matrix."""
    rng = np.random.default_rng(PROBE_SEED)
    x = rng.normal(size=(PROBE_ROWS, len(art["features"])))
    return np.asarray(art["model"].predict_proba(x))


def calib_curve(art: dict) -> np.ndarray | None:
    cal = art.get("calibrator_isotonic")
    return None if cal is None else np.asarray(cal.predict(CALIB_GRID))


def diff_report(before: dict, after: dict) -> list[str]:
    """Everything that changed between source and re-dump. Expected is
    EXACTLY one line: meta.model_id."""
    out: list[str] = []
    for key in sorted(set(before) | set(after)):
        if key not in before or key not in after:
            out.append(f"key {key}: only in {'source' if key in before else 're-dump'}")
    if list(before["features"]) != list(after["features"]):
        out.append("features: list differs")
    if repr(before.get("optimal_threshold")) != repr(after.get("optimal_threshold")):
        out.append(f"optimal_threshold: {before.get('optimal_threshold')!r} -> {after.get('optimal_threshold')!r}")
    if not np.allclose(scores(before), scores(after), rtol=0, atol=0):
        out.append("model: predict_proba differs on the probe matrix")
    cb, ca = calib_curve(before), calib_curve(after)
    if (cb is None) != (ca is None):
        out.append("calibrator_isotonic: None in one of the two versions")
    elif cb is not None and not np.allclose(cb, ca, rtol=0, atol=0):
        out.append("calibrator_isotonic: curve differs")
    mb, ma = dict(before.get("meta", {})), dict(after.get("meta", {}))
    for key in sorted(set(mb) | set(ma)):
        if repr(mb.get(key)) != repr(ma.get(key)):
            out.append(f"meta.{key}: {mb.get(key)!r} -> {ma.get(key)!r}")
    return out


def retag(source: str, model_id: str, out_path: str) -> list[str]:
    """Writes the newly-tagged version and returns the verified diff."""
    art = load_checked(source)
    meta = dict(art.get("meta", {}))
    old = meta.get("model_id")
    meta["model_id"] = model_id
    joblib.dump({**{k: art[k] for k in ARTIFACT_KEYS if k in art}, "meta": meta}, out_path)
    print(f"  💾 {out_path}  (meta.model_id {old!r} -> {model_id!r})")

    # Verification against the SOURCE, not the in-memory object: only the
    # freshly-loaded artifact proves the round trip was lossless.
    return diff_report(load_checked(source), load_checked(out_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-dump a dict-pkl artifact under a new meta.model_id (staging-only, hard rule 2)."
    )
    parser.add_argument("source", help="Source artifact (format A). Read only.")
    parser.add_argument("--model-id", required=True, help="New posting tag, e.g. EPD3 (hard rule 6)")
    parser.add_argument("--out", default=None, help="Target filename in STAGING_DIR (default: source's name)")
    args = parser.parse_args(argv)

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")

    if not os.path.exists(args.source):
        raise SystemExit(f"Refuse: source missing: {args.source}")
    out_path = resolve_out(args.source, args.out)

    import sklearn
    import xgboost

    print(f"python {sys.version.split()[0]} · sklearn {sklearn.__version__} · xgboost {xgboost.__version__}")
    print(f"Source:  {os.path.abspath(args.source)}")

    changes = retag(args.source, args.model_id, out_path)
    unexpected = [c for c in changes if not c.startswith("meta.model_id:")]
    if unexpected:
        print("\n❌ The re-dump changed more than the tag:")
        for c in unexpected:
            print(f"   - {c}")
        return 1
    if not changes:
        print("\n✅ Target already carries this tag — artifact is unchanged, identical to the source.")
        return 0
    print(f"\n✅ Verified: exactly ONE difference from the source — {changes[0]}")
    print("   Model scores, calibrator curve, features, threshold and remaining meta are identical.")
    print("   Promotion into the repo root remains an operator decision (hard rule 2).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
