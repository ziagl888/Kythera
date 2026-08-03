#!/usr/bin/env python3
# tools/bot_variants/stage.py — live-swap staging helper (T-2026-KYT-9050-039, D3).
#
# PURPOSE: stage an archived/indexed generation for live swap — the T-037 pattern
# (RUB1-revive) as repeatable, safe workflow. The helper prints the complete swap
# plan (artifact → staging_models/, code_ref checkout, register flip) and copies
# the artifact on request (--apply) ONLY to staging_models/. It executes NOTHING
# live-impacting.
#
# HARD BOUNDARIES (hard rules 1/2, spec §5): NEVER promote to repo-root/live,
# NEVER touch live DB, NEVER restart the fleet. Root promotion and restart remain
# explicit operator steps (Michi) — the helper prints them only as a checklist.
#
# Invariants:
#   * Default = dry-run (print plan only). --apply writes exclusively to
#     staging_models/ and md5-verifies the copy (byte-identical to source).
#   * Read-only except staging_models/ (only with --apply).

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.shadow_gate as shadow_gate  # noqa: E402
from tools.bot_variants import archive as arch  # noqa: E402
from tools.bot_variants import index as ix  # noqa: E402

logger = logging.getLogger(__name__)

STAGING_DIR = os.path.join(ix.REPO_ROOT, "staging_models")


def _find_generation(index: dict[str, Any], tag: str) -> dict[str, Any] | None:
    norm = tag.strip().upper()
    for gen in index["generations"]:
        if gen["tag"].upper() == norm:
            return gen
    return None


def build_plan(tag: str, direction: str | None = None, load_embedded: bool = True) -> dict[str, Any]:
    """Swap plan for a generation: artifacts, code_ref, register flip, restart.

    Raises ValueError on unknown tag (no silent success)."""
    index = ix.build_index(load_embedded=load_embedded)
    gen = _find_generation(index, tag)
    if gen is None:
        known = ", ".join(sorted(g["tag"] for g in index["generations"]))
        raise ValueError(f"Unknown generation '{tag}'. Known: {known}")

    manifest = arch.build_manifest(gen)
    directions = [direction.upper()] if direction else list(gen["lifecycle"])
    artifacts = [a for a in manifest["artifacts"] if not direction or a["direction"] == direction.upper()]
    return {
        "tag": gen["tag"],
        "family": gen["family"],
        "script": gen["script"],
        "directions": directions,
        "lifecycle": {d: gen["lifecycle"].get(d) for d in directions},
        "code_ref": manifest["code_ref"],
        "artifacts": artifacts,
    }


def apply_staging(plan: dict[str, Any]) -> list[str]:
    """Copies plan artifacts to staging_models/ (md5-verified).

    ONLY staging_models/ (hard rule 2). Returns list of written relative paths.
    Disk-absent artifacts are skipped (the plan prints the git-show path)."""
    written: list[str] = []
    os.makedirs(STAGING_DIR, exist_ok=True)
    for art in plan["artifacts"]:
        if not art["exists"] or not art["source_origin"]["path"]:
            continue
        src = os.path.join(ix.REPO_ROOT, art["source_origin"]["path"])
        dest = os.path.join(STAGING_DIR, art["filename"])
        if os.path.abspath(src) == os.path.abspath(dest):
            continue  # already in staging
        shutil.copyfile(src, dest)
        if ix._md5(dest) != art["md5"]:
            os.remove(dest)
            raise RuntimeError(f"md5 mismatch after staging copy: {art['filename']}")
        written.append(ix._rel(dest))
    return written


def _checkout_step(code_ref: dict[str, Any], script: str | None) -> str:
    if code_ref["ref"] == "HEAD":
        return "code_ref=HEAD — the generation logic is active in the current tree, no checkout needed."
    if code_ref["ref"] and script:
        return (
            f"git checkout {code_ref['ref']} -- {script}    "
            f"# possibly {code_ref['ref']}^ (T-037 pattern: logic was BEFORE the removal commit)"
        )
    return "code_ref unresolved — manually: git log --follow -S<file> -- <script> (see manifest note)."


def render_plan(plan: dict[str, Any], applied: list[str] | None) -> str:
    lines: list[str] = []
    lines.append(f"# Live-swap plan — {plan['tag']} ({plan['family']})")
    lines.append("")
    lines.append("MANUAL — this tool executes NONE of the following live steps.")
    lines.append("")
    lines.append(f"Script: {plan['script']}")
    lines.append("Lifecycle today: " + ", ".join(f"{d}:{s}" for d, s in plan["lifecycle"].items()))
    lines.append("")

    lines.append("## 1. Stage artifact (staging_models/, hard rule 2)")
    for art in plan["artifacts"]:
        if art["exists"] and art["source_origin"]["path"]:
            marker = "✓ copied" if applied and ix._rel(os.path.join(STAGING_DIR, art["filename"])) in applied else "→"
            lines.append(
                f"  {marker} {art['direction']}: {art['source_origin']['path']}  →  staging_models/{art['filename']}  (md5 {(art['md5'] or '')[:8]})"
            )
        elif art["source_commit"]:
            lines.append(
                f"  → {art['direction']}: not on disk — "
                f"git show {art['source_commit'][:8]}:{art['source_origin']['path']} > staging_models/{art['filename']}"
            )
        else:
            lines.append(f"  ⚠ {art['direction']}: {art['filename']} neither on disk nor git-tracked")
    if not applied:
        lines.append("  (dry-run — copy with --apply)")
    lines.append("")

    lines.append("## 2. code_ref (bot logic)")
    lines.append("  " + _checkout_step(plan["code_ref"], plan["script"]))
    lines.append("")

    lines.append("## 3. Register flip (core/shadow_gate.py)")
    for d in plan["directions"]:
        status = plan["lifecycle"].get(d)
        if status == shadow_gate.LIVE:
            lines.append(f"  {d}: already LIVE — no flip.")
        else:
            lines.append(
                f"  {d}: today '{status}'. For LIVE, set the _LIFECYCLE entry ('{plan['tag'].upper()}','{d}') "
                f"to LIVE or remove it (default=LIVE); possibly promote artifact to repo-ROOT "
                f"(operator decision Michi, hard rule 2)."
            )
    lines.append("")

    lines.append("## 4. Go live (operator Michi)")
    lines.append("  - Promote artifact to repo-root (if live loader reads the root path).")
    lines.append("  - Fleet restart: tools/restart_fleet.ps1 (hard rule 1 — NOT from this session).")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live-swap staging helper (T-2026-KYT-9050-039).")
    parser.add_argument("tag", help="Generation tag, e.g. RUB1, MIS1-8H, ATS1_ROBUST")
    parser.add_argument("--direction", choices=["LONG", "SHORT"], help="this direction only")
    parser.add_argument("--apply", action="store_true", help="Copy artifact(s) to staging_models/ (md5-verify)")
    parser.add_argument("--no-model-meta", action="store_true", help="skip embedded joblib-meta")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")

    try:
        plan = build_plan(args.tag, args.direction, load_embedded=not args.no_model_meta)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2

    applied = apply_staging(plan) if args.apply else None
    print(render_plan(plan, applied))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
