#!/usr/bin/env python3
# tools/bot_variants/archive.py — reproducible model/code archive
# (T-2026-KYT-9050-039, phase 2 = D2 + D4). Builds on the read-only index
# (index.build_index) and materialises a manifest per generation.
#
# PURPOSE: make a REPRODUCIBLE archive from the index-join perspective —
# model_archive/<family>/<tag>/manifest.json — that makes each generation
# (a) live-swappable (T-037 pattern: old artifact + code revert to code_ref +
# register flip) or (b) runnable against each other in sim at any time.
# Live swap and sim A/B are phase 3 (stage.py / compare.py).
#
# DECISION "large artifacts" (spec §3 D2): REFERENCE-BASED instead of full copy.
# ALL fleet artifacts (root + staging_models, ~48 MB) are already git-tracked;
# the manifest holds md5 + source_origin + `source_commit` ⇒ each generation is
# reconstructible byte-exact (md5-verifiable) via `git show <source_commit>:<path>`.
# A binary copy would double 48 MB in the repo without gaining reproducibility.
# `--copy-binaries` (opt-in) creates a self-contained export as needed.
#
# Invariants:
#   * READ-ONLY outside model_archive/. No DB access, no network, no root
#     promotion, no restart (hard rules 1/2/7). git read-only.
#   * DETERMINISTIC/IDEMPOTENT: stably sorted; code_ref for active
#     generations symbolically "HEAD" (no volatile HEAD-SHA in manifest);
#     source_commit/lifecycle_history are historical (stable) SHAs ⇒ two runs
#     at same HEAD = byte-identical.
#   * NO SILENT DROP: non-git-tracked artifacts are marked
#     (git_tracked=false + note that only --copy-binaries preserves them).

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.shadow_gate as shadow_gate  # noqa: E402
from tools.bot_variants import index as ix  # noqa: E402

logger = logging.getLogger(__name__)

MANIFEST_SCHEMA = "bot_variants_manifest/v1"
ARCHIVE_MD = os.path.join(ix.ARCHIVE_DIR, "ARCHIVE.md")
_SHADOW_GATE_REL = "core/shadow_gate.py"
_DEFAULT_MAX_COPY_MB = 8.0
# States whose emission logic lives in the CURRENT tree (checkout HEAD suffices).
_ACTIVE_STATES = (shadow_gate.LIVE, shadow_gate.SHADOW, shadow_gate.SILENT)


# ─────────────────────────────────────────────────────────────────────────────
# git helper (read-only; fail-soft so archive never dies on git)
# ─────────────────────────────────────────────────────────────────────────────
def _git(*args: str) -> str:
    """`git <args>` in REPO_ROOT; stdout stripped or "" on any error."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=ix.REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - git missing/timeout
        logger.warning("git %s failed: %s", " ".join(args), exc)
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _source_commit(rel_path: str) -> str | None:
    """Last commit that changed the artifact file (git-show anchor)."""
    sha = _git("log", "-1", "--format=%H", "--", rel_path)
    return sha or None


def _lifecycle_history(tag: str) -> list[dict[str, str]]:
    """Commits that touched the tag in the shadow_gate register (lifecycle history)."""
    raw = _git(
        "log",
        "--format=%h\x1f%ad\x1f%s",
        "--date=short",
        "-S",
        tag,
        "--",
        _SHADOW_GATE_REL,
    )
    history: list[dict[str, str]] = []
    for line in raw.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            history.append({"sha": parts[0], "date": parts[1], "subject": parts[2]})
    return history


def _resolve_code_ref(gen: dict[str, Any]) -> dict[str, Any]:
    """D4: git point where the generation logic lives(d).

    Active generation (any live/shadow/silent leg) ⇒ logic in current tree ⇒
    symbolically ``HEAD`` (no volatile SHA in manifest). Otherwise (completely
    retired) via ``git log -S`` over the emitting script + shadow_gate — that's
    the T-037 anchor (RUB1-SHORT was at ``07c8874^``, parent of the removal
    commit; live swap uses ``<sha>^`` if needed)."""
    if any(v in _ACTIVE_STATES for v in gen["lifecycle"].values()):
        return {
            "ref": "HEAD",
            "sha": None,
            "method": "active-in-tree",
            "note": "Logic in current tree — checkout HEAD",
        }
    script = gen["script"]
    paths = [p for p in (script, _SHADOW_GATE_REL) if p]
    tokens = [a["filename"] for a in gen["artifacts"]] + [gen["tag"]]
    for token in tokens:
        line = _git("log", "-1", "--format=%H\x1f%s", "-S", token, "--", *paths)
        if line:
            sha, _, subject = line.partition("\x1f")
            return {
                "ref": sha,
                "sha": sha,
                "method": "git-log-S",
                "token": token,
                "subject": subject,
                "note": "last commit touch; for live-swap use <ref>^ if needed (T-037 pattern)",
            }
    return {
        "ref": None,
        "sha": None,
        "method": "unresolved",
        "note": "resolve manually: git log --follow -S<file> -- <script>",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Manifest building
# ─────────────────────────────────────────────────────────────────────────────
def _family_dir(family: str | None) -> str:
    return (family or "_unknown").lower()


def archive_dir_for(gen: dict[str, Any]) -> str:
    """Absolute destination folder model_archive/<family>/<tag>/ of a generation."""
    return os.path.join(ix.ARCHIVE_DIR, _family_dir(gen["family"]), gen["tag"])


def _artifact_manifest_entry(art: dict[str, Any]) -> dict[str, Any]:
    """Index artifact entry → manifest entry (+ source_commit / git_tracked).

    The full feature contract (D2 "features") is lifted from meta to manifest
    level; the summary meta keeps ``n_features``."""
    source_commit = _source_commit(art["path"]) if art["exists"] and art["path"] else None
    meta = art["meta"]
    features = None
    if isinstance(meta, dict):
        features = meta.get("features")
        meta = {k: v for k, v in meta.items() if k != "features"}
    return {
        "direction": art["direction"],
        "filename": art["filename"],
        "source_origin": {"location": art["location"], "path": art["path"]},
        "exists": art["exists"],
        "md5": art["md5"],
        "bytes": art["bytes"],
        "source_commit": source_commit,
        "git_tracked": source_commit is not None,
        "archived_copy": None,  # set by copy_binaries
        "features": features,
        "meta": meta,
    }


def build_manifest(gen: dict[str, Any]) -> dict[str, Any]:
    """Complete manifest dict of a generation (JSON-serializable)."""
    artifacts = [_artifact_manifest_entry(a) for a in gen["artifacts"]]
    notes = list(gen["notes"])
    untracked = sorted({a["filename"] for a in artifacts if a["exists"] and not a["git_tracked"]})
    if untracked:
        notes.append("not git-tracked (only preserved via --copy-binaries): " + ", ".join(untracked))
    return {
        "schema": MANIFEST_SCHEMA,
        "tag": gen["tag"],
        "family": gen["family"],
        "generation": gen["generation"],
        "script": gen["script"],
        "lifecycle": gen["lifecycle"],
        "lifecycle_history": _lifecycle_history(gen["tag"]),
        "model_ids": gen["model_ids"],
        "provenance": gen["provenance"],
        "code_ref": _resolve_code_ref(gen),
        "artifacts": artifacts,
        "notes": notes,
        "generated_by": "tools/bot_variants/archive.py",
    }


def build_manifests(load_embedded: bool = True) -> list[dict[str, Any]]:
    """Manifests for all generations in the index (deterministically sorted)."""
    index = ix.build_index(load_embedded=load_embedded, include_features=True)
    return [build_manifest(gen) for gen in index["generations"]]


# ─────────────────────────────────────────────────────────────────────────────
# Binary copy (opt-in) + md5 verification
# ─────────────────────────────────────────────────────────────────────────────
def copy_binaries(manifest: dict[str, Any], max_copy_mb: float) -> list[str]:
    """Copies source artifacts of a generation to model_archive/<f>/<tag>/.

    md5-verified (copy == source, rule: byte-identical). Oversized files
    (> max_copy_mb) are skipped and listed in the return log (no silent skip).
    Sets ``archived_copy`` per artifact. Only to model_archive/ — NEVER
    root/live (hard rule 2)."""
    skipped: list[str] = []
    dest_dir = os.path.join(ix.ARCHIVE_DIR, _family_dir(manifest["family"]), manifest["tag"])
    for art in manifest["artifacts"]:
        if not art["exists"] or not art["source_origin"]["path"]:
            continue
        src = os.path.join(ix.REPO_ROOT, art["source_origin"]["path"])
        size_mb = (art["bytes"] or 0) / (1024 * 1024)
        if size_mb > max_copy_mb:
            skipped.append(f"{art['filename']} ({size_mb:.1f} MB > {max_copy_mb} MB)")
            continue
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, art["filename"])
        shutil.copyfile(src, dest)
        if ix._md5(dest) != art["md5"]:
            os.remove(dest)
            raise RuntimeError(f"md5 mismatch after copy: {art['filename']} (source != copy)")
        art["archived_copy"] = ix._rel(dest)
    return skipped


# ─────────────────────────────────────────────────────────────────────────────
# Write / render / drift check
# ─────────────────────────────────────────────────────────────────────────────
def _manifest_path(manifest: dict[str, Any]) -> str:
    return os.path.join(ix.ARCHIVE_DIR, _family_dir(manifest["family"]), manifest["tag"], "manifest.json")


def _dump_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def render_archive_md(manifests: list[dict[str, Any]]) -> str:
    """Human-readable archive overview (generated)."""
    lines = [
        "# Model/code archive (auto-generated)",
        "",
        "> Generated by `tools/bot_variants/archive.py` (T-2026-KYT-9050-039). "
        "**Do not edit by hand** — regenerate with "
        "`python -m tools.bot_variants.archive --write`.",
        ">",
        "> Reference-based: artifact bytes are git-tracked in root/staging; "
        "each generation holds `manifest.json` md5 + `source_commit` ⇒ retrieval via "
        "`git show <source_commit>:<path>`. `--copy-binaries` creates a self-contained export.",
        "",
        f"**Generations:** {len(manifests)}",
        "",
        "| Family | Tag | Lifecycle | code_ref | Artifacts (direction:file@source_commit) | Manifest |",
        "|---|---|---|---|---|---|",
    ]
    for m in manifests:
        family = m["family"] or "—"
        lifecycle = ", ".join(f"{d}:{s}" for d, s in m["lifecycle"].items())
        cr = m["code_ref"]
        code_ref = cr["ref"] if cr["ref"] else "—"
        if cr["method"] == "git-log-S" and cr["sha"]:
            code_ref = f"`{cr['sha'][:8]}`"
        arts = (
            "<br>".join(
                f"{a['direction']}:`{a['filename']}`@"
                + ((a['source_commit'] or 'untracked')[:8] if a['exists'] else 'MISSING')
                for a in m["artifacts"]
            )
            or "—"
        )
        rel_manifest = ix._rel(_manifest_path(m))
        lines.append(f"| {family} | `{m['tag']}` | {lifecycle} | {code_ref} | {arts} | `{rel_manifest}` |")
    lines.append("")
    return "\n".join(lines)


def write_archive(manifests: list[dict[str, Any]]) -> int:
    """Writes all manifests + ARCHIVE.md. Returns the number of files written."""
    count = 0
    for m in manifests:
        path = _manifest_path(m)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_dump_json(m))
        count += 1
    os.makedirs(ix.ARCHIVE_DIR, exist_ok=True)
    with open(ARCHIVE_MD, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_archive_md(manifests))
    return count


def check_archive(manifests: list[dict[str, Any]]) -> list[str]:
    """Drift between generated manifests/ARCHIVE.md and the files on disk."""
    drift: list[str] = []
    expected = {_manifest_path(m): _dump_json(m) for m in manifests}
    expected[ARCHIVE_MD] = render_archive_md(manifests)
    for path, content in expected.items():
        if not os.path.isfile(path):
            drift.append(f"missing: {ix._rel(path)}")
            continue
        with open(path, encoding="utf-8") as fh:
            if fh.read() != content:
                drift.append(f"drift: {ix._rel(path)}")
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bot variant archive (manifests + code_ref, T-2026-KYT-9050-039).")
    parser.add_argument("--write", action="store_true", help="write manifests + ARCHIVE.md")
    parser.add_argument("--check", action="store_true", help="check drift against disk (exit 1 on drift)")
    parser.add_argument(
        "--copy-binaries",
        action="store_true",
        help="copy artifact binaries to model_archive/ (opt-in, self-contained export)",
    )
    parser.add_argument(
        "--max-copy-mb", type=float, default=_DEFAULT_MAX_COPY_MB, help="copy size limit per file (MB)"
    )
    parser.add_argument("--no-model-meta", action="store_true", help="skip embedded joblib meta")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")

    # --copy-binaries mutates the manifests (archived_copy) → only makes sense
    # with --write (otherwise orphaned, unpersisted copies or false drift on
    # --check). As a hard precondition, not a silent no-op.
    if args.copy_binaries and not args.write:
        print("ERROR: --copy-binaries requires --write (otherwise orphaned, unpersisted copies).")
        return 2

    manifests = build_manifests(load_embedded=not args.no_model_meta)

    if args.copy_binaries:
        all_skipped: list[str] = []
        for m in manifests:
            all_skipped.extend(copy_binaries(m, args.max_copy_mb))
        if all_skipped:
            print(f"copy skipped (>{args.max_copy_mb} MB): {len(all_skipped)}")
            for s in all_skipped:
                print("  " + s)

    if args.check:
        drift = check_archive(manifests)
        if drift:
            print("ARCHIVE-DRIFT:")
            for d in drift:
                print("  " + d)
            return 1
        print("archive up-to-date (no drift)")
        return 0

    if args.write:
        n = write_archive(manifests)
        print(f"written: {n} manifests + {ix._rel(ARCHIVE_MD)}")
    else:
        print(f"manifests (dry-run, not written): {len(manifests)} — --write to persist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
