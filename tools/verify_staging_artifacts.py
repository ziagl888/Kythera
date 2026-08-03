r"""
tools/verify_staging_artifacts.py — post-retrain staging artifact verification
(T-2026-CU-9050-120).

READ-ONLY. No DB access, no live touch, NO promotion — promoting an artifact
into the repo root (= live) remains an explicit operator decision by Michi
(hard rule 2 / escalation). This tool only delivers the finding on which
Michi decides.

Checks every retrain artifact in STAGING_DIR against the fleet contracts:

  1. HR-2  Residency        — artifact sits in STAGING_DIR (not repo root);
                              reports whether a promotion would overwrite an
                              existing live file of the same name in the repo
                              root (existence only, no mtime comparison).
  2. HR-7/P0.12 Feature contract — artifact loads via core.model_artifacts (the
                              bot's own loader) AND its feature list matches
                              the trainer/serving reference (core.*_features
                              or the constants in retrain_from_replay).
  3. HR-6  Model tag        — meta.model_id == expected generation tag of the
                              family (TD2/BB2/ABR2/MIS2/RUB2/EPD2/ATB2); for
                              comparison the currently deployed live tag is shown.
  4. Threshold              — optimal_threshold ∈ (0,1), not the 1.0 idle default.
  5. P3.4  xgboost version  — meta.xgboost_version == running xgboost.__version__
                              (silent predict_proba skew on major drift).
  6. Format B               — model_type startswith "binary" + calibrator sidecar.
  7. Model object           — predict_proba present (loads as a classifier).
  8. C2 report              — val/test WR vs base rate, net PnL and n from
                              retrain_<name>_stats.json → go/no-go hint (ADVISORY,
                              not a hard fail — Michi's judgment). The calibration
                              monotonicity of the buckets stays manual review (docs).
  9. Rule 4/6 promotion slot — would promoting this filename occupy a
                              LOADER SLOT that a SECOND generation tag
                              reads? (tools/promotion_guard.py — the EPD3-SHORT case
                              from 2026-07-21, T-2026-KYT-9050-057.) Per-file WARN,
                              because the intent is not readable from the filename;
                              additionally the guard's registry scan runs at the end,
                              whose FAIL (LIVE leg on a foreign slot) sets the exit
                              code to 1.

Exit code: 1 if any MECHANICAL contract check is FAIL (does not load /
feature drift / xgb version skew / wrong tag / invalid threshold / LIVE leg on
a foreign root slot); otherwise 0. Metric concerns (check 8) and the per-file
slot hint (check 9) are WARN and do NOT change the exit code — whether an
underperforming model is promoted anyway is the operator's decision.

Examples:
  python tools/verify_staging_artifacts.py
  python tools/verify_staging_artifacts.py --only td,bb
  python tools/verify_staging_artifacts.py --staging-dir D:\some\staging_models
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import sys

import joblib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# core.model_artifacts is the loader the live bots use — exactly that one
# should accept the artifact. Import is DB-free.
from core import model_artifacts  # noqa: E402
from tools import promotion_guard  # noqa: E402

# Status markers
OK = "PASS"
WARN = "WARN"
FAIL = "FAIL"


def _load_retrain_module():
    """Loads tools/retrain_from_replay.py by path to get its feature constants
    and STAGING_DIR. Loaded as a file (not a package import),
    because tools/ is not an installed package. The module head is DB-free."""
    path = os.path.join(REPO_ROOT, "tools", "retrain_from_replay.py")
    spec = importlib.util.spec_from_file_location("retrain_from_replay", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"retrain_from_replay.py not loadable: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# Family registry: artifact pattern -> (feature reference, expected tag,
# format, stats file, live file in the repo root for the promotion comparison).
# The feature references come from the retrainer itself (the same source that
# generates the artifacts — and via core.*_features the same one the
# bots serve, hard rule 7).
# --------------------------------------------------------------------------- #
def build_registry(R) -> list[dict]:
    """R = the loaded retrain_from_replay module."""
    return [
        {
            "family": "td",
            "glob": "td_xgboost_model_*.pkl",
            "fmt": "A",
            "features": list(R.SNIPER_FEATURES),
            "tag": lambda fn: f"TD2_{_tf_from(fn).upper()}",
            "stats": lambda fn: f"retrain_td_{_tf_from(fn)}_stats.json",
        },
        {
            "family": "bb",
            "glob": "bb_xgboost_model_*.pkl",
            "fmt": "A",
            "features": list(R.SNIPER_FEATURES),
            "tag": lambda fn: f"BB2_{_tf_from(fn).upper()}",
            "stats": lambda fn: f"retrain_bb_{_tf_from(fn)}_stats.json",
        },
        {
            "family": "abr1",
            "glob": "bt2_model_*.json",
            "fmt": "B",
            "features": list(R.ABR1_FEATURES),
            "tag": lambda fn: "ABR2",
            "stats": lambda fn: "retrain_abr1_stats.json",
        },
        # MIS: the retrainer WRITES to STAGING under the trainer prefix
        # (mis1_model_*), NOT under the bot promotion slot mis2_model_* — the
        # meta carries "MIS2" (the bot appends the horizon: MIS2-8H …). Three label
        # modes (geometry + move/close + move/wick) land under their own prefix
        # each; each needs a registry row, otherwise the family is silently skipped.
        {
            "family": "mis1",
            "glob": "mis1_model_*.pkl",
            "fmt": "A",
            "features": list(R.MIS1_FEATURES),
            "tag": lambda fn: "MIS2",
            "stats": lambda fn: "retrain_mis1_stats.json",
        },
        {
            "family": "mis1_move",
            "glob": "mis1_move_model_*.pkl",
            "fmt": "A",
            "features": list(R.MIS1_FEATURES),
            "tag": lambda fn: "MIS2",
            "stats": lambda fn: "retrain_mis1_move_stats.json",
        },
        {
            "family": "mis1_move_wick",
            "glob": "mis1_move_wick_model_*.pkl",
            "fmt": "A",
            "features": list(R.MIS1_FEATURES),
            "tag": lambda fn: "MIS2",
            "stats": lambda fn: "retrain_mis1_move_wick_stats.json",
        },
        {
            "family": "rub",
            "glob": "rub2_model_*.pkl",
            "fmt": "A",
            "features": list(R.RUB2_FEATURES),
            "tag": lambda fn: "RUB2",
            "stats": lambda fn: "retrain_rub2_stats.json",
        },
        {
            "family": "epd",
            "glob": "epd2_model_*.pkl",
            "fmt": "A",
            "features": list(R.EPD2_FEATURES),
            "tag": lambda fn: "EPD2",
            "stats": lambda fn: "retrain_epd2_stats.json",
        },
        {
            "family": "atb2",
            "glob": "atb2_model_*.pkl",
            "fmt": "A",
            "features": list(R.ATB2_FEATURES),
            "tag": lambda fn: "ATB2",
            "stats": lambda fn: "retrain_atb2_stats.json",
        },
    ]


def _tf_from(filename: str) -> str:
    """'td_xgboost_model_4h.pkl' -> '4h'. Falls back to '' if no
    known tf is in the name (the tag check then fails visibly)."""
    base = os.path.basename(filename)
    for tf in ("1h", "4h"):
        if f"_{tf}." in base or base.endswith(f"_{tf}.pkl"):
            return tf
    return ""


# --------------------------------------------------------------------------- #
# Individual checks. Each function returns (status, message).
# --------------------------------------------------------------------------- #
def check_residency(path: str, staging_dir: str) -> tuple[str, str]:
    in_staging = os.path.abspath(os.path.dirname(path)) == os.path.abspath(staging_dir)
    if not in_staging:
        return FAIL, f"is NOT in STAGING_DIR ({os.path.dirname(path)}) — HR-2 violation"
    # Promotion preview: does a live file of the same name exist in the repo root?
    # Deliberately NO mtime comparison — mtimes are not a reliable "staging newer
    # than live" signal across checkouts/worktrees. Existence info only.
    live = os.path.join(REPO_ROOT, os.path.basename(path))
    if os.path.exists(live):
        return OK, "in STAGING_DIR; promotion would overwrite the existing live file of the same name"
    return OK, "in STAGING_DIR; no live artifact of the same name (new slot)"


def check_xgb_version(meta: dict) -> tuple[str, str]:
    import xgboost as xgb

    art_ver = str(meta.get("xgboost_version", "")).strip()
    run_ver = str(xgb.__version__)
    if not art_ver:
        return WARN, f"meta.xgboost_version missing (serving runs {run_ver}) — skew not checkable"
    if art_ver == run_ver:
        return OK, f"xgboost {art_ver} == serving {run_ver}"
    if art_ver.split(".")[0] != run_ver.split(".")[0]:
        return FAIL, f"xgboost MAJOR drift: artifact {art_ver} vs serving {run_ver} (silent predict skew, P3.4)"
    return WARN, f"xgboost minor drift: artifact {art_ver} vs serving {run_ver}"


def check_tag(meta: dict, expected_tag: str) -> tuple[str, str]:
    model_id = str(meta.get("model_id", "")).strip()
    if not model_id:
        return FAIL, "meta.model_id missing — bot posts under fallback constant (HR-6 risk)"
    if model_id != expected_tag:
        return FAIL, f"model_id '{model_id}' != expected gen tag '{expected_tag}' (HR-6)"
    return OK, f"model_id '{model_id}' == expected gen tag"


def check_threshold(threshold) -> tuple[str, str]:
    try:
        t = float(threshold)
    except (TypeError, ValueError):
        return FAIL, f"optimal_threshold not numeric: {threshold!r}"
    if not (0.0 < t < 1.0):
        return FAIL, f"optimal_threshold {t} outside (0,1) — 1.0 is the idle default (no gate)"
    return OK, f"optimal_threshold {t:.3f} ∈ (0,1)"


def check_features(art_features, ref_features: list[str]) -> tuple[str, str]:
    if not art_features:
        return FAIL, "artifact carries no feature list"
    art = list(art_features)
    if art == ref_features:
        return OK, f"{len(art)} features == trainer/serving reference"
    art_set, ref_set = set(art), set(ref_features)
    extra = [c for c in art if c not in ref_set]
    if extra:
        # The loader would reject this anyway (check_feature_contract) — the
        # bot builder does not deliver these columns.
        return FAIL, f"artifact requires {len(extra)} unknown feature(s): {extra[:5]} (feature drift)"
    missing = [c for c in ref_features if c not in art_set]
    if missing:
        return (
            WARN,
            f"{len(art)} features, but {len(missing)} reference feature(s) missing: {missing[:5]} (builder drift?)",
        )
    # Same set, different order: benign — the bot selects by name
    # (df[features]), the order in the artifact has no consequence.
    return OK, f"{len(art)} features == reference (same set, different order — benign)"


def check_model_object(model) -> tuple[str, str]:
    if model is None:
        return FAIL, "no model object in artifact"
    if not hasattr(model, "predict_proba"):
        return FAIL, f"model has no predict_proba ({type(model).__name__})"
    return OK, f"{type(model).__name__} with predict_proba"


# --------------------------------------------------------------------------- #
# Format-specific loading — both raw (granular meta checks) and via
# the bot loader (the ultimate "does the bot accept it?" check).
# --------------------------------------------------------------------------- #
def load_raw_A(path: str) -> dict:
    """Format A (dict pkl): {model, features, optimal_threshold,
    calibrator_isotonic, meta}."""
    d = joblib.load(path)
    if not isinstance(d, dict) or "model" not in d:
        raise ValueError("not a dict artifact (Format A) — possibly a raw model")
    return {
        "model": d.get("model"),
        "features": d.get("features"),
        "threshold": d.get("optimal_threshold"),
        "calibrator": d.get("calibrator_isotonic"),
        "meta": dict(d.get("meta", {})),
        "model_type_ok": (OK, "Format A (kein model_type-Vertrag)"),
    }


def load_raw_B(path: str) -> dict:
    """Format B (native XGB JSON + _meta.json + _calib.pkl)."""
    import xgboost as xgb

    model = xgb.XGBClassifier()
    model.load_model(path)
    meta_path = path.replace(".json", "_meta.json")
    calib_path = path.replace(".json", "_calib.pkl")
    if not os.path.exists(meta_path):
        raise ValueError(f"{os.path.basename(meta_path)} missing — legacy contract, not a retrain artifact")
    with open(meta_path, encoding="utf-8") as fh:
        meta = json.load(fh)
    mtype = str(meta.get("model_type", ""))
    if not mtype.startswith("binary"):
        mt_status = (FAIL, f"model_type '{mtype}' does not start with 'binary' (loader would read the wrong column)")
    elif not os.path.exists(calib_path):
        mt_status = (WARN, "model_type binary, but _calib.pkl sidecar missing")
    else:
        mt_status = (OK, f"model_type '{mtype}' + calibrator sidecar present")
    return {
        "model": model,
        "features": meta.get("features"),
        "threshold": meta.get("optimal_threshold"),
        "calibrator": None,
        "meta": dict(meta),
        "model_type_ok": mt_status,
    }


def loader_accepts(path: str, fmt: str, ref_features: list[str], default_tag: str) -> tuple[str, str]:
    """Runs the EXACT bot loader (core.model_artifacts). loaded=True means:
    the live bot would accept this artifact at startup."""
    if fmt == "A":
        c = model_artifacts.load_artifact(path, ref_features, default_tag)
    else:
        c = model_artifacts.load_artifact_json(path, ref_features, default_tag)
    if c.get("loaded"):
        return OK, f"core.model_artifacts accepts the artifact (tag={c.get('tag')})"
    return FAIL, "core.model_artifacts rejects the artifact (loaded=False) — bot would run in idle mode"


# --------------------------------------------------------------------------- #
# C2 metric report (advisory)
# --------------------------------------------------------------------------- #
def _iter_stat_blocks(obj, path=""):
    """Recursively finds all dicts that carry 'test_stats' or 'val_stats'
    and returns (label, block) — covers flat (td/bb) as well as nested
    (rub/epd/mis per direction/horizon) stats JSONs."""
    if isinstance(obj, dict):
        if "test_stats" in obj or "val_stats" in obj:
            yield path or "root", obj
        for k, v in obj.items():
            yield from _iter_stat_blocks(v, f"{path}.{k}" if path else str(k))


def metric_verdict(block: dict) -> tuple[str, str]:
    """Advisory go/no-go from a stats block: does the model beat its
    base rate on the test slice, and is the net PnL positive?"""
    ts = block.get("test_stats") or {}
    wr = ts.get("wr")
    base = ts.get("base_rate_test")
    pnl = ts.get("sum_net_pnl_pct")
    bits = []
    verdict = OK
    if wr is not None and base is not None:
        bits.append(f"test WR {wr:.1f}% vs base {base:.1f}%")
        if wr < base:
            verdict = WARN
            bits.append("↓ below base rate")
    if pnl is not None:
        bits.append(f"ΣNet PnL {pnl:+.1f}%")
        if pnl <= 0:
            verdict = WARN
            bits.append("≤0")
    n = ts.get("n_taken") or ts.get("n")
    if n is not None:
        bits.append(f"n={n}")
        if isinstance(n, (int, float)) and n < 30:
            verdict = WARN
            bits.append("thin (n<30)")
    if not bits:
        return WARN, "no test_stats in block"
    return verdict, "; ".join(bits)


def report_metrics(staging_dir: str, stats_name: str) -> list[tuple[str, str, str]]:
    """List (label, status, message) per stats block of the file."""
    fp = os.path.join(staging_dir, stats_name)
    if not os.path.exists(fp):
        return [("(stats)", WARN, f"{stats_name} not yet present (retrain run?)")]
    try:
        with open(fp, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return [("(stats)", WARN, f"{stats_name} not readable: {e}")]
    out = []
    for label, block in _iter_stat_blocks(data):
        status, msg = metric_verdict(block)
        out.append((label, status, msg))
    if not out:
        out.append(("(stats)", WARN, f"{stats_name} contains no test_stats/val_stats"))
    return out


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def verify_artifact(path: str, spec: dict, staging_dir: str) -> dict:
    fn = os.path.basename(path)
    ref_features = spec["features"]
    expected_tag = spec["tag"](fn)
    checks: list[tuple[str, str, str]] = []

    st, msg = check_residency(path, staging_dir)
    checks.append(("residency", st, msg))
    checks.append(("promotion_slot", *promotion_guard.check_staging_filename(fn)))

    # Raw load (granular meta checks)
    try:
        raw = load_raw_A(path) if spec["fmt"] == "A" else load_raw_B(path)
    except Exception as e:  # noqa: BLE001 — every load-error class is a FAIL
        checks.append(("load", FAIL, f"does not load: {e}"))
        return {"file": fn, "family": spec["family"], "tag": expected_tag, "checks": checks}

    meta = raw["meta"]
    checks.append(("model", *check_model_object(raw["model"])))
    checks.append(("features", *check_features(raw["features"], ref_features)))
    checks.append(("tag", *check_tag(meta, expected_tag)))
    checks.append(("threshold", *check_threshold(raw["threshold"])))
    checks.append(("xgb_version", *check_xgb_version(meta)))
    if spec["fmt"] == "B":
        checks.append(("format_b", *raw["model_type_ok"]))

    # The ultimate check: does the bot's own loader accept it?
    try:
        checks.append(("loader", *loader_accepts(path, spec["fmt"], ref_features, expected_tag)))
    except Exception as e:  # noqa: BLE001
        checks.append(("loader", FAIL, f"core.model_artifacts raises: {e}"))

    return {"file": fn, "family": spec["family"], "tag": expected_tag, "checks": checks}


def worst(checks: list[tuple[str, str, str]]) -> str:
    order = {OK: 0, WARN: 1, FAIL: 2}
    return max((c[1] for c in checks), key=lambda s: order[s], default=OK)


ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    # The bot loader logs its own error on rejection — redundant here to
    # our own FAIL report and would clutter the output out-of-order.
    import logging

    logging.getLogger("core.model_artifacts").setLevel(logging.CRITICAL)

    ap = argparse.ArgumentParser(description="Post-retrain staging artifact verification (read-only).")
    ap.add_argument(
        "--staging-dir", default=None, help="Default: KYTHERA_STAGING_DIR or retrain_from_replay.STAGING_DIR"
    )
    ap.add_argument(
        "--only",
        default="",
        help="Comma list of families (td,bb,abr1,mis1,mis1_move,mis1_move_wick,rub,epd,atb2)",
    )
    args = ap.parse_args()

    R = _load_retrain_module()
    staging_dir = args.staging_dir or R.STAGING_DIR
    registry = build_registry(R)
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    if only:
        registry = [s for s in registry if s["family"] in only]

    print(f"STAGING_DIR: {staging_dir}")
    if not os.path.isdir(staging_dir):
        print(f"❌ STAGING_DIR does not exist: {staging_dir}")
        return 1
    import xgboost as xgb

    print(f"Serving xgboost: {xgb.__version__} · python {sys.version.split()[0]}\n")

    any_fail = False
    seen_stats: set[str] = set()

    for spec in registry:
        # _meta.json/_calib.pkl sidecars are NOT model artifacts — otherwise
        # the format B loader tries to load the meta JSON as an XGB model.
        paths = [
            p
            for p in sorted(glob.glob(os.path.join(staging_dir, spec["glob"])))
            if not os.path.basename(p).endswith(("_meta.json", "_calib.pkl"))
        ]
        if not paths:
            continue
        print(f"── {spec['family'].upper()} ({len(paths)} artifact(s)) " + "─" * 30)
        for path in paths:
            res = verify_artifact(path, spec, staging_dir)
            status = worst(res["checks"])
            any_fail = any_fail or status == FAIL
            print(f"  {ICON[status]}{res['file']}")
            for name, st, msg in res["checks"]:
                if st != OK:
                    print(f"       {ICON[st]}{name}: {msg}")
        # C2 metric report per family (once per stats file)
        stats_name = spec["stats"](os.path.basename(paths[0]))
        if stats_name not in seen_stats:
            seen_stats.add(stats_name)
            for label, st, msg in report_metrics(staging_dir, stats_name):
                print(f"     📊 [{label}] {ICON.get(st, '')}{msg}")
        print()

    # Registry scan (check 9, second half): independent of which files are in
    # STAGING_DIR — it reads ONLY core.shadow_gate. A FAIL means: a
    # challenger leg already flipped to LIVE loads from the root slot of a
    # foreign generation (rule-4 double-post). That is a hard promotion stop.
    findings = promotion_guard.scan()
    if findings:
        print("── PROMOTION NAME GUARD " + "─" * 35)
        for f in findings:
            print(f"  {ICON[f.severity]}{f.as_line()}")
        any_fail = any_fail or any(f.severity == FAIL for f in findings)
        print()

    print("─" * 60)
    if any_fail:
        print("❌ At least one MECHANICAL contract check is FAIL — DO NOT promote until fixed.")
        print("   (Metric WARNs are ADVISORY and do not change the exit code — promotion remains Michi's decision.)")
        return 1
    print("✅ No mechanical contract errors. Promotion remains an operator decision (check metric WARNs).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
