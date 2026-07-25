#!/usr/bin/env python3
# tools/bot_variants/stage.py — Live-Swap-Staging-Helfer (T-2026-KYT-9050-039, D3).
#
# ZWECK: Eine archivierte/indizierte Generation für den Live-Swap BEREITLEGEN —
# das T-037-Muster (RUB1-Revive) als wiederholbaren, sicheren Ablauf. Der Helfer
# druckt den vollständigen Swap-Plan (Artefakt → staging_models/, code_ref-
# Checkout, Register-Flip) und kopiert das Artefakt auf Wunsch (--apply) NUR nach
# staging_models/. Er führt NICHTS Live-Wirksames aus.
#
# HARTE GRENZEN (Hard Rules 1/2, Spec §5): NIE nach Repo-Root/live promoten, NIE
# die Live-DB anfassen, NIE die Fleet neu starten. Die Root-Promotion und der
# Restart bleiben explizite Operator-Schritte (Michi) — der Helfer druckt sie
# nur als Checkliste.
#
# Invariants:
#   * Default = DRY-RUN (nur Plan drucken). --apply schreibt ausschließlich nach
#     staging_models/ und md5-verifiziert die Kopie (byte-identisch zur Quelle).
#   * Read-only außer staging_models/ (nur mit --apply).

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
    """Swap-Plan einer Generation: Artefakte, code_ref, Register-Flip, Restart.

    Wirft ValueError bei unbekanntem Tag (kein Silent-Erfolg)."""
    index = ix.build_index(load_embedded=load_embedded)
    gen = _find_generation(index, tag)
    if gen is None:
        known = ", ".join(sorted(g["tag"] for g in index["generations"]))
        raise ValueError(f"Unbekannte Generation '{tag}'. Bekannt: {known}")

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
    """Kopiert die Plan-Artefakte nach staging_models/ (md5-verifiziert).

    NUR staging_models/ (Hard Rule 2). Gibt die Liste der geschriebenen
    relativen Pfade. Nicht-auf-Platte-Artefakte werden übersprungen (der Plan
    druckt den git-show-Weg)."""
    written: list[str] = []
    os.makedirs(STAGING_DIR, exist_ok=True)
    for art in plan["artifacts"]:
        if not art["exists"] or not art["source_origin"]["path"]:
            continue
        src = os.path.join(ix.REPO_ROOT, art["source_origin"]["path"])
        dest = os.path.join(STAGING_DIR, art["filename"])
        if os.path.abspath(src) == os.path.abspath(dest):
            continue  # liegt bereits im Staging
        shutil.copyfile(src, dest)
        if ix._md5(dest) != art["md5"]:
            os.remove(dest)
            raise RuntimeError(f"md5-Mismatch nach Staging-Copy: {art['filename']}")
        written.append(ix._rel(dest))
    return written


def _checkout_step(code_ref: dict[str, Any], script: str | None) -> str:
    if code_ref["ref"] == "HEAD":
        return "code_ref=HEAD — die Generations-Logik ist im aktuellen Baum aktiv, kein Checkout nötig."
    if code_ref["ref"] and script:
        return (
            f"git checkout {code_ref['ref']} -- {script}    "
            f"# ggf. {code_ref['ref']}^ (T-037-Muster: Logik lag VOR dem Removal-Commit)"
        )
    return "code_ref unaufgelöst — manuell: git log --follow -S<datei> -- <script> (siehe Manifest-Hinweis)."


def render_plan(plan: dict[str, Any], applied: list[str] | None) -> str:
    lines: list[str] = []
    lines.append(f"# Live-Swap-Plan — {plan['tag']} ({plan['family']})")
    lines.append("")
    lines.append("MANUELL — dieses Tool führt KEINEN der folgenden Live-Schritte aus.")
    lines.append("")
    lines.append(f"Script: {plan['script']}")
    lines.append("Lifecycle heute: " + ", ".join(f"{d}:{s}" for d, s in plan["lifecycle"].items()))
    lines.append("")

    lines.append("## 1. Artefakt bereitstellen (staging_models/, Hard Rule 2)")
    for art in plan["artifacts"]:
        if art["exists"] and art["source_origin"]["path"]:
            marker = "✓ kopiert" if applied and ix._rel(os.path.join(STAGING_DIR, art["filename"])) in applied else "→"
            lines.append(
                f"  {marker} {art['direction']}: {art['source_origin']['path']}  →  staging_models/{art['filename']}  (md5 {(art['md5'] or '')[:8]})"
            )
        elif art["source_commit"]:
            lines.append(
                f"  → {art['direction']}: nicht auf Platte — "
                f"git show {art['source_commit'][:8]}:{art['source_origin']['path']} > staging_models/{art['filename']}"
            )
        else:
            lines.append(f"  ⚠ {art['direction']}: {art['filename']} weder auf Platte noch git-getrackt")
    if not applied:
        lines.append("  (dry-run — mit --apply kopieren)")
    lines.append("")

    lines.append("## 2. code_ref (Bot-Logik)")
    lines.append("  " + _checkout_step(plan["code_ref"], plan["script"]))
    lines.append("")

    lines.append("## 3. Register-Flip (core/shadow_gate.py)")
    for d in plan["directions"]:
        status = plan["lifecycle"].get(d)
        if status == shadow_gate.LIVE:
            lines.append(f"  {d}: bereits LIVE — kein Flip.")
        else:
            lines.append(
                f"  {d}: heute '{status}'. Für LIVE den _LIFECYCLE-Eintrag ('{plan['tag'].upper()}','{d}') "
                f"auf LIVE setzen bzw. entfernen (Default=LIVE); ggf. Artefakt nach Repo-ROOT promoten "
                f"(Operator-Entscheid Michi, Hard Rule 2)."
            )
    lines.append("")

    lines.append("## 4. Live schalten (Operator Michi)")
    lines.append("  - Artefakt nach Repo-Root promoten (falls Live-Loader den Root-Pfad liest).")
    lines.append("  - Fleet-Restart: tools/restart_fleet.ps1 (Hard Rule 1 — NICHT aus dieser Session).")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live-Swap-Staging-Helfer (T-2026-KYT-9050-039).")
    parser.add_argument("tag", help="Generations-Tag, z.B. RUB1, MIS1-8H, ATS1_ROBUST")
    parser.add_argument("--direction", choices=["LONG", "SHORT"], help="nur diese Richtung")
    parser.add_argument("--apply", action="store_true", help="Artefakt(e) nach staging_models/ kopieren (md5-verify)")
    parser.add_argument("--no-model-meta", action="store_true", help="eingebettete joblib-meta überspringen")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")

    try:
        plan = build_plan(args.tag, args.direction, load_embedded=not args.no_model_meta)
    except ValueError as exc:
        print(f"FEHLER: {exc}")
        return 2

    applied = apply_staging(plan) if args.apply else None
    print(render_plan(plan, applied))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
