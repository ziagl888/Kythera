#!/usr/bin/env python3
# tools/bot_variants/archive.py — reproduzierbares Modell-/Code-Archiv
# (T-2026-KYT-9050-039, Phase 2 = D2 + D4). Baut auf dem read-only Index
# (index.build_index) auf und materialisiert je Generation ein Manifest.
#
# ZWECK: Aus der Index-Join-Sicht ein REPRODUZIERBARES Archiv machen —
# model_archive/<family>/<tag>/manifest.json —, das jede Generation jederzeit
# (a) live-swapbar (T-037-Muster: altes Artefakt + Code-Revert auf code_ref +
# Register-Flip) oder (b) in Sim gegeneinander lauffähig macht. Der Live-Swap
# und das Sim-A/B sind Phase 3 (stage.py / compare.py).
#
# ENTSCHEIDUNG „Groß-Artefakte" (Spec §3 D2): REFERENCE-BASED statt Voll-Copy.
# ALLE Fleet-Artefakte (root + staging_models, ~48 MB) sind bereits git-tracked;
# das Manifest hält md5 + source_origin + `source_commit` ⇒ jede Generation ist
# über `git show <source_commit>:<path>` byte-genau (md5-verifizierbar)
# rekonstruierbar. Ein Binär-Copy würde 48 MB im Repo verdoppeln, ohne
# Reproduzierbarkeit zu gewinnen. `--copy-binaries` (opt-in) erzeugt bei Bedarf
# ein self-contained Export.
#
# Invariants:
#   * READ-ONLY außerhalb model_archive/. Kein DB-Zugriff, kein Netzwerk, keine
#     Root-Promotion, kein Restart (harte Regeln 1/2/7). git nur lesend.
#   * DETERMINISTISCH/IDEMPOTENT: stabil sortiert; code_ref für aktive
#     Generationen SYMBOLISCH „HEAD" (kein volatiler HEAD-SHA im Manifest);
#     source_commit/lifecycle_history sind historische (stabile) SHAs ⇒ zwei
#     Läufe bei gleichem HEAD = byte-identisch.
#   * KEIN SILENT-DROP: nicht-git-getrackte Artefakte werden markiert
#     (git_tracked=false + Hinweis, dass nur --copy-binaries sie bewahrt).

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
# Zustände, deren Emissions-Logik im AKTUELLEN Baum lebt (checkout HEAD genügt).
_ACTIVE_STATES = (shadow_gate.LIVE, shadow_gate.SHADOW, shadow_gate.SILENT)


# ─────────────────────────────────────────────────────────────────────────────
# git-Helfer (read-only; fail-soft, damit das Archiv nie an git stirbt)
# ─────────────────────────────────────────────────────────────────────────────
def _git(*args: str) -> str:
    """`git <args>` in REPO_ROOT; stdout gestript oder "" bei jedem Fehler."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=ix.REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - git fehlt/Timeout
        logger.warning("git %s fehlgeschlagen: %s", " ".join(args), exc)
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _source_commit(rel_path: str) -> str | None:
    """Letzter Commit, der die Artefakt-Datei geändert hat (git-show-Anker)."""
    sha = _git("log", "-1", "--format=%H", "--", rel_path)
    return sha or None


def _lifecycle_history(tag: str) -> list[dict[str, str]]:
    """Commits, die den Tag im shadow_gate-Register berührt haben (Lifecycle-Historie)."""
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
    """D4: git-Punkt, an dem die Generations-Logik lebt(e).

    Aktive Generation (irgendein live/shadow/silent Bein) ⇒ Logik im aktuellen
    Baum ⇒ symbolisch ``HEAD`` (kein volatiler SHA ins Manifest). Sonst
    (vollständig retired) via ``git log -S`` über das emittierende Script +
    shadow_gate — das ist der T-037-Anker (RUB1-SHORT lag bei ``07c8874^``, dem
    Parent des Removal-Commits; der Live-Swap nutzt ggf. ``<sha>^``)."""
    if any(v in _ACTIVE_STATES for v in gen["lifecycle"].values()):
        return {
            "ref": "HEAD",
            "sha": None,
            "method": "active-in-tree",
            "note": "Logik im aktuellen Baum — checkout HEAD",
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
                "note": "letzte Kommit-Berührung; für den Live-Swap ggf. <ref>^ (T-037-Muster)",
            }
    return {
        "ref": None,
        "sha": None,
        "method": "unresolved",
        "note": "manuell auflösen: git log --follow -S<datei> -- <script>",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Manifest-Bau
# ─────────────────────────────────────────────────────────────────────────────
def _family_dir(family: str | None) -> str:
    return (family or "_unknown").lower()


def archive_dir_for(gen: dict[str, Any]) -> str:
    """Absoluter Zielordner model_archive/<family>/<tag>/ einer Generation."""
    return os.path.join(ix.ARCHIVE_DIR, _family_dir(gen["family"]), gen["tag"])


def _artifact_manifest_entry(art: dict[str, Any]) -> dict[str, Any]:
    """Index-Artefakt-Eintrag → Manifest-Eintrag (+ source_commit / git_tracked)."""
    source_commit = _source_commit(art["path"]) if art["exists"] and art["path"] else None
    return {
        "direction": art["direction"],
        "filename": art["filename"],
        "source_origin": {"location": art["location"], "path": art["path"]},
        "exists": art["exists"],
        "md5": art["md5"],
        "bytes": art["bytes"],
        "source_commit": source_commit,
        "git_tracked": source_commit is not None,
        "archived_copy": None,  # von copy_binaries gesetzt
        "meta": art["meta"],
    }


def build_manifest(gen: dict[str, Any]) -> dict[str, Any]:
    """Vollständiges Manifest-Dict einer Generation (JSON-serialisierbar)."""
    artifacts = [_artifact_manifest_entry(a) for a in gen["artifacts"]]
    notes = list(gen["notes"])
    untracked = sorted({a["filename"] for a in artifacts if a["exists"] and not a["git_tracked"]})
    if untracked:
        notes.append("nicht git-getrackt (nur via --copy-binaries bewahrt): " + ", ".join(untracked))
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
    """Manifeste für alle Generationen des Index (deterministisch sortiert)."""
    index = ix.build_index(load_embedded=load_embedded)
    return [build_manifest(gen) for gen in index["generations"]]


# ─────────────────────────────────────────────────────────────────────────────
# Binär-Copy (opt-in) + md5-Verifikation
# ─────────────────────────────────────────────────────────────────────────────
def copy_binaries(manifest: dict[str, Any], max_copy_mb: float) -> list[str]:
    """Kopiert die Quell-Artefakte einer Generation nach model_archive/<f>/<tag>/.

    md5-verifiziert (Kopie == Quelle, Regel: byte-identisch). Übergroße Dateien
    (> max_copy_mb) werden übersprungen und im Rückgabe-Log genannt (kein Silent-
    Skip). Setzt ``archived_copy`` je Artefakt. Nur nach model_archive/ — NIE
    Root/live (Hard Rule 2)."""
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
            raise RuntimeError(f"md5-Mismatch nach Copy: {art['filename']} (Quelle != Kopie)")
        art["archived_copy"] = ix._rel(dest)
    return skipped


# ─────────────────────────────────────────────────────────────────────────────
# Schreiben / Rendern / Drift-Check
# ─────────────────────────────────────────────────────────────────────────────
def _manifest_path(manifest: dict[str, Any]) -> str:
    return os.path.join(ix.ARCHIVE_DIR, _family_dir(manifest["family"]), manifest["tag"], "manifest.json")


def _dump_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def render_archive_md(manifests: list[dict[str, Any]]) -> str:
    """Menschenlesbarer Archiv-Überblick (generiert)."""
    lines = [
        "# Modell-/Code-Archiv (auto-generiert)",
        "",
        "> Generiert von `tools/bot_variants/archive.py` (T-2026-KYT-9050-039). "
        "**Nicht von Hand editieren** — regenerieren mit "
        "`python -m tools.bot_variants.archive --write`.",
        ">",
        "> Reference-based: die Artefakt-Bytes liegen git-getrackt in root/staging; "
        "je Generation hält `manifest.json` md5 + `source_commit` ⇒ Retrieval via "
        "`git show <source_commit>:<path>`. `--copy-binaries` erzeugt ein self-contained Export.",
        "",
        f"**Generationen:** {len(manifests)}",
        "",
        "| Family | Tag | Lifecycle | code_ref | Artefakte (Richtung:Datei@source_commit) | Manifest |",
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
    """Schreibt alle Manifeste + ARCHIVE.md. Gibt Anzahl geschriebener Dateien."""
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
    """Drift zwischen generierten Manifesten/ARCHIVE.md und den Dateien auf Platte."""
    drift: list[str] = []
    expected = {_manifest_path(m): _dump_json(m) for m in manifests}
    expected[ARCHIVE_MD] = render_archive_md(manifests)
    for path, content in expected.items():
        if not os.path.isfile(path):
            drift.append(f"fehlt: {ix._rel(path)}")
            continue
        with open(path, encoding="utf-8") as fh:
            if fh.read() != content:
                drift.append(f"drift: {ix._rel(path)}")
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bot-Varianten-Archiv (Manifeste + code_ref, T-2026-KYT-9050-039).")
    parser.add_argument("--write", action="store_true", help="Manifeste + ARCHIVE.md schreiben")
    parser.add_argument("--check", action="store_true", help="Drift gegen Platte prüfen (exit 1 bei Drift)")
    parser.add_argument(
        "--copy-binaries",
        action="store_true",
        help="Artefakt-Binaries nach model_archive/ kopieren (opt-in, self-contained Export)",
    )
    parser.add_argument(
        "--max-copy-mb", type=float, default=_DEFAULT_MAX_COPY_MB, help="Copy-Größenlimit je Datei (MB)"
    )
    parser.add_argument("--no-model-meta", action="store_true", help="eingebettete joblib-meta überspringen")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")

    manifests = build_manifests(load_embedded=not args.no_model_meta)

    if args.copy_binaries:
        all_skipped: list[str] = []
        for m in manifests:
            all_skipped.extend(copy_binaries(m, args.max_copy_mb))
        if all_skipped:
            print(f"copy übersprungen (>{args.max_copy_mb} MB): {len(all_skipped)}")
            for s in all_skipped:
                print("  " + s)

    if args.check:
        drift = check_archive(manifests)
        if drift:
            print("ARCHIVE-DRIFT:")
            for d in drift:
                print("  " + d)
            return 1
        print("archive up-to-date (kein Drift)")
        return 0

    if args.write:
        n = write_archive(manifests)
        print(f"geschrieben: {n} Manifeste + {ix._rel(ARCHIVE_MD)}")
    else:
        print(f"Manifeste (dry-run, nicht geschrieben): {len(manifests)} — --write zum Persistieren")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
