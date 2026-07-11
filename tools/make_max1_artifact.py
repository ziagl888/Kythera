"""Derive the MAX1 staging artifact from the live RUB2-SHORT artifact.

T-2026-CU-9050-067: MAX1 trades the RUB2-SHORT model, but must post under its own
tag (harte Regel 6 — the tag comes from meta.model_id, never from a constant), so
it needs its own artifact file. Model, feature contract, calibrator and the
val-picked operating point are copied VERBATIM; only the identity fields change.
The throttle that makes MAX1 selective lives in the bot config
(MAX1_MIN_PROB / MAX1_MAX_PER_DAY), not in the artifact — so Michi can retune it
without regenerating anything.

Writes to staging_models/ only (harte Regel 2). Promoting the artifact into the
repo root (= live) is an operator decision, never part of a build.

Re-run this after every RUB2-SHORT retrain that MAX1 should follow; a MAX2
generation gets --model-id MAX2 and a new file name.

Usage:
    python tools/make_max1_artifact.py
    python tools/make_max1_artifact.py --source rub2_model_SHORT.pkl --model-id MAX1
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import joblib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGING_DIR = os.path.join(ROOT, "staging_models")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="rub2_model_SHORT.pkl", help="live RUB2-SHORT artifact (dict-pkl, format A)")
    ap.add_argument("--model-id", default="MAX1", help="posting tag written into meta.model_id")
    ap.add_argument("--out", default="", help="output file name inside staging_models/ (default: <model_id>_model_SHORT.pkl)")
    args = ap.parse_args()

    # Plain ASCII output: the VPS console is cp1252, an emoji here would raise
    # UnicodeEncodeError and kill the tool after it already wrote the artifact.
    src = args.source if os.path.isabs(args.source) else os.path.join(ROOT, args.source)
    if not os.path.exists(src):
        print(f"ERROR: Quell-Artefakt fehlt: {src}", file=sys.stderr)
        return 1

    art = joblib.load(src)
    required = {"model", "features", "optimal_threshold"}
    missing = required - set(art)
    if missing:
        print(f"ERROR: {src} ist kein dict-pkl-Artefakt (Format A) — fehlende Keys: {sorted(missing)}", file=sys.stderr)
        return 1

    meta = dict(art.get("meta", {}))
    parent = meta.get("model_id", "?")
    meta.update(
        model_id=args.model_id,
        derived_from=parent,
        derived_from_file=os.path.basename(src),
        derived_by="tools/make_max1_artifact.py (T-2026-CU-9050-067)",
        note=(
            f"Byte-identical {parent}-SHORT model under the {args.model_id} tag. "
            "The 1-3/day throttle is bot config (MAX1_MIN_PROB / MAX1_MAX_PER_DAY), "
            "NOT the artifact threshold — optimal_threshold stays the val-picked "
            f"{float(art['optimal_threshold']):.3f} and remains the hard floor."
        ),
    )
    art["meta"] = meta

    os.makedirs(STAGING_DIR, exist_ok=True)
    out_name = args.out or f"{args.model_id.lower()}_model_SHORT.pkl"
    out_path = os.path.join(STAGING_DIR, out_name)
    joblib.dump(art, out_path)
    with open(out_path.replace(".pkl", "_meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, ensure_ascii=False)

    print(
        f"OK: {out_path}\n"
        f"    Tag {meta['model_id']} (aus {parent}) | {len(art['features'])} Features | "
        f"Threshold {float(art['optimal_threshold']):.3f} | "
        f"Kalibrator: {'ja' if art.get('calibrator_isotonic') is not None else 'nein'}\n"
        f"    Deploy = Operator-Entscheid: nach {ROOT} kopieren, dann Bot 34 neu starten."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
