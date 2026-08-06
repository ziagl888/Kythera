#!/usr/bin/env python3
# tools/bot_variants/index.py — read-only bot variant index (T-2026-KYT-9050-038, D1).
#
# PURPOSE: build a deterministically regenerable join view per *bot × generation*
# from the scattered current state (root/staging/archive artifacts +
# lifecycle register + fleet script mapping + git). This is the basis for
# (a) putting an old generation live with existing infra (T-037 pattern:
# old artifact + code revert to a git SHA + tag + register flip), or
# (b) racing them against each other in sim.
#
# Invariants:
#   * READ-ONLY outside docs/ + model_archive/index.json. No DB access,
#     no network, no model promotion (hard rules 1/2).
#   * DETERMINISTIC/IDEMPOTENT: no now()/randomness in the output rows; all
#     collections stably sorted ⇒ running twice = byte-identical output.
#   * NO SILENT DROP (like bot_catalog): unclassifiable artifact files
#     and unknown tags are counted AND listed.
#   * SHARED FILENAMES visible: an artifact file under >1 tag (root
#     collision hazard, e.g. rub2_model_LONG.pkl under RUB2+RUB3) is flagged.
#
# SOURCES (join): core.bot_catalog (tag→script/family), core.shadow_gate
# (lifecycle per (tag,dir) + SHADOW_ARTIFACTS), artifact meta (sidecar
# *_meta.json or embedded), filesystem (root/staging/archive), git (HEAD).

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.bot_catalog as bot_catalog  # noqa: E402
import core.shadow_gate as shadow_gate  # noqa: E402

logger = logging.getLogger(__name__)

SCHEMA = "bot_variants_index/v1"
_DIRECTIONS = ("LONG", "SHORT")

# ─────────────────────────────────────────────────────────────────────────────
# Pfade
# ─────────────────────────────────────────────────────────────────────────────
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STAGING_DIR = os.path.join(REPO_ROOT, "staging_models")
ARCHIVE_DIR = os.path.join(REPO_ROOT, "model_archive")
MARKDOWN_OUT = os.path.join(REPO_ROOT, "docs", "bot_variants_index.md")
JSON_OUT = os.path.join(ARCHIVE_DIR, "index.json")

# (label, directory) — scan order = resolution priority for the
# location shown for an artifact (root before staging before archive).
_SEARCH_LOCATIONS: tuple[tuple[str, str], ...] = (
    ("root", REPO_ROOT),
    ("staging", STAGING_DIR),
    ("archive", ARCHIVE_DIR),
)

# Files that are NOT standalone model artifacts (sidecars, reports,
# configs). They must NOT be counted as "unclassified".
_NON_MODEL_SUFFIXES: tuple[str, ...] = (
    "_meta.json",
    "_report.json",
    "_calib.pkl",
    "_smoke.pkl",
    "_smoke_report.json",
    "_study.json",
)
_NON_MODEL_FILENAMES: frozenset[str] = frozenset(
    {"coins.json", "bot_config.example.json", "listing_onboard_dates.json", "index.json"}
)
_MODEL_EXTENSIONS: tuple[str, ...] = (".pkl", ".joblib", ".json")


# ─────────────────────────────────────────────────────────────────────────────
# CURATED GENERATION REGISTRY  (tag → {direction: [filename, …]})
# ─────────────────────────────────────────────────────────────────────────────
# The ONE curated building block: the bridge from generation tag to artifact
# filename(s). Needed because the oldest legacy artifacts (reversion, pump_model,
# model_tsi) do not yet carry the model_id convention — their tag lives only in
# the loader of the respective bot script (cited per entry). Everything ELSE
# (script, lifecycle, threshold, deployable, trained_at, md5, code_ref) is
# joined/derived, not maintained here. The class-(A)/challenger shadow tags are
# added additively from shadow_gate.SHADOW_ARTIFACTS (see _artifact_registry()).
#
# pump=LONG / dump=SHORT (MIS convention, core.mis_features / bot 11).
_MIS_HORIZONS = ("8", "24", "72", "168")


def _mis_registry(tag_prefix: str, file_prefix: str, file_suffix: str) -> dict[str, dict[str, list[str]]]:
    """MIS generation per horizon: MIS?-{h}H → pump(LONG)/dump(SHORT) file."""
    out: dict[str, dict[str, list[str]]] = {}
    for h in _MIS_HORIZONS:
        out[f"{tag_prefix}-{h}H"] = {
            "LONG": [f"{file_prefix}{h}h_pump{file_suffix}"],
            "SHORT": [f"{file_prefix}{h}h_dump{file_suffix}"],
        }
    return out


# Live/legacy generations that are NOT in shadow_gate.SHADOW_ARTIFACTS
# (that only holds the not-yet-promoted class-(A)/challenger tags).
_LEGACY_ARTIFACTS: dict[str, dict[str, list[str]]] = {
    # Rubberband (bot 13). RUB1 = original legacy, live again since T-037.
    "RUB1": {"LONG": ["long_reversion_model.joblib"], "SHORT": ["short_reversion_model.joblib"]},
    # RUB2 retrain: SHORT in root (benched), LONG in staging. rub2_model_LONG.pkl
    # is also the RUB3 challenger source (SHADOW_ARTIFACTS) → shared file.
    "RUB2": {"SHORT": ["rub2_model_SHORT.pkl"], "LONG": ["rub2_model_LONG.pkl"]},
    # Pump/Dump (bot 10). EPD2 = EPD_LEGACY_TAG; the legacy loader loads the raw
    # 3-class model pump_dump_model.pkl for BOTH directions. In addition,
    # the EPD2 generation carries its retrain artifacts epd2_model_{LONG,SHORT}.pkl
    # (EPD2_ARTIFACT_PATHS in 10_pump_dump_detector.py) — epd2_model_LONG.pkl is
    # also the EPD3-LONG shadow source (SHADOW_ARTIFACTS) ⇒ shared-file
    # hazard that the index makes visible.
    "EPD2": {
        "LONG": ["pump_dump_model.pkl", "epd2_model_LONG.pkl"],
        "SHORT": ["pump_dump_model.pkl", "epd2_model_SHORT.pkl"],
    },
    # MIS (bot 11): MIS1 = pump_model_*_final.pkl (revived, T-034), MIS2 = mis2_model_*.pkl.
    **_mis_registry("MIS1", "pump_model_", "_final.pkl"),
    **_mis_registry("MIS2", "mis2_model_", ".pkl"),
    # Trend-Sniper/ATS (bot 12): ATS1_Robust = model_tsi_*_robust.pkl.
    "ATS1_ROBUST": {"LONG": ["model_tsi_long_robust.pkl"], "SHORT": ["model_tsi_short_robust.pkl"]},
    # Master-Ranker AIM2 (bot 15): direction-agnostic meta ranker (one file).
    "AIM2": {"LONG": ["master_meta_model_aim2.pkl"], "SHORT": ["master_meta_model_aim2.pkl"]},
    # SMC sniper (bot 25): BB/TD per timeframe, one model per file (used bidirectionally).
    "BB_1H": {"LONG": ["bb_xgboost_model_1h.pkl"], "SHORT": ["bb_xgboost_model_1h.pkl"]},
    "BB_4H": {"LONG": ["bb_xgboost_model_4h.pkl"], "SHORT": ["bb_xgboost_model_4h.pkl"]},
    "TD_1H": {"LONG": ["td_xgboost_model_1h.pkl"], "SHORT": ["td_xgboost_model_1h.pkl"]},
    "TD_4H": {"LONG": ["td_xgboost_model_4h.pkl"], "SHORT": ["td_xgboost_model_4h.pkl"]},
    # Quasimodo (bot 24): QM per timeframe.
    "QM_1H": {"LONG": ["qm_xgboost_model_1h.pkl"], "SHORT": ["qm_xgboost_model_1h.pkl"]},
    "QM_4H": {"LONG": ["qm_xgboost_model_4h.pkl"], "SHORT": ["qm_xgboost_model_4h.pkl"]},
    # Break&Retest gen-2 (bot 18): on disk as bt2_model_*.json, meta.model_id=ABR2
    # (filename ≠ tag — the index makes exactly that visible).
    "ABR2": {"LONG": ["bt2_model_LONG.json"], "SHORT": ["bt2_model_SHORT.json"]},
    # Further single-model legacies.
    "MAX1": {"SHORT": ["max1_model_SHORT.pkl"]},
    "FIF1": {"LONG": ["fif1_model.pkl"], "SHORT": ["fif1_model.pkl"]},
    "PEX1": {"LONG": ["pex1_model.pkl"], "SHORT": ["pex1_model.pkl"]},
}

# Short provenance per family (MODEL_INTENT/task reference). Generation-specific
# overrides in _PROVENANCE_TAG.
_PROVENANCE_FAMILY: dict[str, str] = {
    "RUB": "Rubberband HVN/S-R-Reversion (bot 13); RUB1 revived T-037",
    "EPD": "Pump/Dump-Detector (bot 10); EPD2=EPD_LEGACY_TAG",
    "MIS": "Momentum-Impuls-Spike pump/dump (bot 11); MIS1 revived T-034",
    "ATS": "Trend-Strength-Sniper TSI (bot 12)",
    "ATB": "Converging-Channel Break (bot 14); ATB2 rebuild",
    "AIM": "Master-Ranker/Gate over candidates (bot 15)",
    "BB": "SMC-ML-Sniper Break (bot 25)",
    "TD": "SMC-ML-Sniper Trend-Detect (bot 25)",
    "QM": "Quasimodo-Pattern (bot 24)",
    "ABR": "Break&Retest binary + Funding-Gate (bot 18)",
    "MAX": "MAX1 (bot 34) / MAX2 SRA2-LONG-Fork (bot 9)",
    "FIF": "First-In-First-Out (bot 33)",
    "PEX": "Price-Extension (bot 30)",
    "FMR": "Funding-Mean-Reversion-Exit (bot 31)",
    "SRA": "Support/Resistance-AI (bot 9)",
    "BR": "Pattern-Breakout-Detector (bot 7)",
    "ROM": "Regime-Orchestrator Re-Forwarder (bot 28)",
    "LIS": "Post-Listing-Drift-Fade (bot 36)",
    "TSM": "Time-Series-Momentum (bot 37)",
    "SKW": "Cross-Sectional-Skewness (bot 38)",
    "XSM": "Cross-Sectional-Momentum (bot 39)",
    "XSR": "Cross-Sectional-Reversal (bot 39)",
    "UFI": "UFI1 (bot 29)",
    "TRM": "TRM1 (bot 32)",
    "ODS": "OI-Divergence-Short (bot 42)",
}
# Known rule-based live generations WITHOUT a model artifact and without
# a lifecycle register entry (default LIVE). Without this list, active
# fleet tags would fall out of the index (no artifact ⇒ not discovered).
# Directions explicit so the index does not falsely show a dead direction as live.
_RULE_ONLY_GENERATIONS: dict[str, list[str]] = {
    "MAX2": ["LONG"],  # SRA2-LONG fork to CH_MAIN (bot 9), LONG-only
    "ROM1": ["LONG", "SHORT"],  # regime re-forwarder (bot 28)
    "UFI1": ["LONG", "SHORT"],  # bot 29 (non-standard leverage)
    "TRM1": ["LONG", "SHORT"],  # bot 32
    "ODS1": ["SHORT"],  # bot 42, OI-divergence short — SHORT-only by construction
}

_PROVENANCE_TAG: dict[str, str] = {
    "ATS1_ROBUST": "ATS1_Robust legacy (model_tsi_*_robust.pkl); ATS2 is the successor",
    "EPD3": "EPD2 retrain challenger; LONG+SHORT promoted to root (epd3_model_*.pkl, PR #189)",
    "RUB3": "rub2_model_LONG challenger vs. live RUB1-LONG",
    "RUB4": "funding-gated RUB3 (fund_24h>+3bps); uses RUB3 artifact",
    "MAX2": "no model — SRA2-LONG fork to CH_MAIN (bot 9)",
    "AIM2-TOPN": "high-conviction top-N channel over AIM2; retired T-037",
}


def legacy_artifact_slots() -> dict[str, dict[str, list[str]]]:
    """Public view of the curated legacy/live registry (tag → direction
    → root filename(s)).

    Second consumer besides the index itself: ``tools/promotion_guard.py``
    needs exactly this tag↔filename bridge to detect whether a challenger
    artifact would hijack the loader slot of a FOREIGN tag when promoted
    to the repo root (T-2026-KYT-9050-057). Deliberately an accessor instead of a
    second curated dict — one source, already tested in the index.
    Copy so a caller cannot mutate the registry.
    """
    return {tag: {d: list(files) for d, files in dirs.items()} for tag, dirs in _LEGACY_ARTIFACTS.items()}


def _md5(path: str) -> str:
    h = hashlib.md5()  # noqa: S324 — integrity/identity hash, not cryptographic
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel(path: str) -> str:
    """Repo-relative POSIX path (deterministic across platforms)."""
    return os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")


def _locate(filename: str) -> tuple[str, str] | None:
    """First match location (label, absolute path) of a filename, or None."""
    for label, directory in _SEARCH_LOCATIONS:
        candidate = os.path.join(directory, filename)
        if os.path.isfile(candidate):
            return label, candidate
    return None


def _artifact_registry() -> dict[str, dict[str, list[str]]]:
    """Curated legacy registry + class-(A)/challenger tags from shadow_gate."""
    registry: dict[str, dict[str, list[str]]] = {}
    for tag, dirs in _LEGACY_ARTIFACTS.items():
        registry[tag] = {d: list(files) for d, files in dirs.items()}
    for tag, dirmap in shadow_gate.SHADOW_ARTIFACTS.items():
        bucket = registry.setdefault(tag.upper(), {})
        for direction, filename in dirmap.items():
            files = bucket.setdefault(direction.upper(), [])
            if filename not in files:
                files.append(filename)
    return registry


def _lifecycle_tags() -> set[str]:
    """All tags that appear in the shadow_gate register (lifecycle + retired)."""
    tags: set[str] = set()
    lifecycle = getattr(shadow_gate, "_LIFECYCLE", {})
    for tag, _direction in lifecycle:
        tags.add(tag.upper())
    for tag in getattr(shadow_gate, "_RETIRED_TAGS", set()):
        tags.add(tag.upper())
    return tags


def _lifecycle_directions(tag: str) -> list[str]:
    """Directions listed for a tag in the lifecycle register."""
    lifecycle = getattr(shadow_gate, "_LIFECYCLE", {})
    dirs = {d for (t, d) in lifecycle if t.upper() == tag}
    return [d for d in _DIRECTIONS if d in dirs]


def _extract_meta_fields(meta: dict[str, Any], include_features: bool = False) -> dict[str, Any]:
    """Normalizes the index-relevant fields from a meta dict.

    ``include_features`` appends the FULL feature list (for the archive manifest,
    D2 — the feature contract). Default off, so the D1 index (docs/…md +
    index.json) stays lean/unchanged (only ``n_features``)."""
    threshold = meta.get("optimal_threshold", meta.get("threshold"))
    deployable = meta.get("deployable")
    val_stats = meta.get("val_stats")
    if deployable is None and isinstance(val_stats, dict):
        deployable = val_stats.get("deployable")
    features = meta.get("features")
    n_features = len(features) if isinstance(features, list) else None
    fields = {
        "model_id": meta.get("model_id"),
        "strategy": meta.get("strategy"),
        "trainer": meta.get("trainer"),
        "trained_at": meta.get("trained_at"),
        "threshold": threshold if isinstance(threshold, (int, float)) else None,
        "deployable": deployable if isinstance(deployable, bool) else None,
        "n_features": n_features,
    }
    if include_features:
        fields["features"] = features if isinstance(features, list) else None
    return fields


def _read_meta(path: str, load_embedded: bool, include_features: bool = False) -> dict[str, Any] | None:
    """Meta of an artifact: sidecar *_meta.json preferred, otherwise embedded.

    Sidecar is cheap and covers the retrain generation (retrain_from_replay).
    Embedded meta (in the joblib dict) covers the sniper/single models; loading
    it is expensive (xgboost/sklearn) and therefore gated via ``load_embedded``.
    All values are static file contents ⇒ deterministic (no now()).
    """
    sidecar = os.path.splitext(path)[0] + "_meta.json"
    if os.path.isfile(sidecar):
        try:
            with open(sidecar, encoding="utf-8") as fh:
                return _extract_meta_fields(json.load(fh), include_features)
        except (OSError, ValueError) as exc:  # pragma: no cover - defensive
            logger.warning("meta sidecar %s not readable: %s", sidecar, exc)
            return None
    if not load_embedded or not path.endswith((".pkl", ".joblib")):
        return None
    try:
        import warnings

        import joblib

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            art = joblib.load(path)
    except Exception as exc:  # pragma: no cover - defensive, discovery must not die
        logger.warning("artifact %s not loadable: %s", path, exc)
        return None
    if not isinstance(art, dict):
        return None
    merged: dict[str, Any] = {}
    nested = art.get("meta")
    if isinstance(nested, dict):
        merged.update(nested)
    for key in ("model_id", "optimal_threshold", "threshold", "features", "trainer", "trained_at", "deployable"):
        if key in art and key not in merged:
            merged[key] = art[key]
    return _extract_meta_fields(merged, include_features)


def _build_artifact_entry(
    direction: str, filename: str, load_embedded: bool, include_features: bool = False
) -> dict[str, Any]:
    """One artifact entry: location + md5 + meta (or MISSING, if not there).

    Resilience (module invariant "discovery must not die"): the location is
    checked via isfile(), but between the check and the read the file can be
    locked/overwritten on the live VPS by a training run (TOCTOU). An
    OSError on md5/stat therefore degrades THIS entry (exists=False), instead
    of tearing down the whole index run — analogous to the fail-soft joblib path."""
    found = _locate(filename)
    if found is not None:
        label, abspath = found
        try:
            return {
                "direction": direction,
                "filename": filename,
                "location": label,
                "path": _rel(abspath),
                "exists": True,
                "md5": _md5(abspath),
                "bytes": os.path.getsize(abspath),
                "meta": _read_meta(abspath, load_embedded, include_features),
            }
        except OSError as exc:  # pragma: no cover - TOCTOU/Lock/Permission-Race
            logger.warning("artifact %s not readable (%s): %s", filename, abspath, exc)
    return {
        "direction": direction,
        "filename": filename,
        "location": "MISSING",
        "path": None,
        "exists": False,
        "md5": None,
        "bytes": None,
        "meta": None,
    }


def _provenance(family: str | None, tag: str) -> str:
    if tag in _PROVENANCE_TAG:
        return _PROVENANCE_TAG[tag]
    if family and family in _PROVENANCE_FAMILY:
        return _PROVENANCE_FAMILY[family]
    return ""


def build_index(load_embedded: bool = True, include_features: bool = False) -> dict[str, Any]:
    """Builds the full variant index as a (JSON-serializable) dict.

    Deterministic: all generations/artifacts/lists stably sorted; no
    now()/randomness. ``load_embedded=False`` skips the expensive joblib load
    (sidecar meta only) — for fast/dependency-light runs and tests.
    ``include_features`` appends the full feature list to the artifact meta
    (archive manifest, D2); default off ⇒ the D1 index stays unchanged.
    """
    registry = _artifact_registry()
    all_tags = set(registry) | _lifecycle_tags() | set(_RULE_ONLY_GENERATIONS)

    generations: list[dict[str, Any]] = []
    # filename → set(tags), to find shared filenames (collision hazard).
    filename_to_tags: dict[str, set[str]] = {}
    unknown_tags: list[str] = []

    for tag in sorted(all_tags):
        family = bot_catalog.family_for_tag(tag)
        script = bot_catalog.script_for_tag(tag)
        if script is None:
            unknown_tags.append(tag)

        art_map = registry.get(tag, {})
        # Directions: artifact registry → lifecycle register → rule-only list →
        # (fallback) both.
        directions = (
            [d for d in _DIRECTIONS if d in art_map]
            or _lifecycle_directions(tag)
            or _RULE_ONLY_GENERATIONS.get(tag)
            or list(_DIRECTIONS)
        )

        lifecycle = {d: shadow_gate.leg_status(tag, d) for d in directions}

        artifacts: list[dict[str, Any]] = []
        for direction in _DIRECTIONS:
            for filename in sorted(art_map.get(direction, [])):
                artifacts.append(_build_artifact_entry(direction, filename, load_embedded, include_features))
                filename_to_tags.setdefault(filename, set()).add(tag)
        artifacts.sort(key=lambda a: (a["direction"], a["filename"]))

        model_ids = sorted({a["meta"]["model_id"] for a in artifacts if a["meta"] and a["meta"].get("model_id")})

        notes: list[str] = []
        if not art_map:
            notes.append("rule-based / no model artifact")
        missing = sorted({a["filename"] for a in artifacts if not a["exists"]})
        if missing:
            notes.append("artifact missing on disk: " + ", ".join(missing))
        if script is None:
            notes.append("unknown tag — no fleet script (bot_catalog)")

        # code_ref (phase 1, conservative): HEAD if the generation is active (live)
        # ⇒ logic in the current tree. Otherwise null — the exact git SHA resolution
        # per old generation is D4/phase 2.
        code_ref = "HEAD" if any(v == shadow_gate.LIVE for v in lifecycle.values()) else None

        generations.append(
            {
                "family": family,
                "tag": tag,
                "generation": tag,
                "script": script,
                "lifecycle": lifecycle,
                "artifacts": artifacts,
                "model_ids": model_ids,
                "code_ref": code_ref,
                "provenance": _provenance(family, tag),
                "notes": notes,
            }
        )

    shared_filenames = _shared_filenames(filename_to_tags)
    unclassified = _unclassified_artifacts(filename_to_tags)

    return {
        "schema": SCHEMA,
        "generation_count": len(generations),
        "unclassified_count": len(unclassified),
        "unknown_tag_count": len(unknown_tags),
        "shared_filename_count": len(shared_filenames),
        "generations": generations,
        "shared_filenames": shared_filenames,
        "unclassified_artifacts": unclassified,
        "unknown_tags": sorted(unknown_tags),
    }


def _shared_filenames(filename_to_tags: dict[str, set[str]]) -> list[dict[str, Any]]:
    """Filenames claimed by >1 DISTINCT tag (hazard)."""
    out: list[dict[str, Any]] = []
    for filename, tags in filename_to_tags.items():
        if len(tags) > 1:
            found = _locate(filename)
            out.append(
                {
                    "filename": filename,
                    "tags": sorted(tags),
                    "location": found[0] if found else "MISSING",
                }
            )
    out.sort(key=lambda e: e["filename"])
    return out


def _is_model_file(filename: str) -> bool:
    if filename in _NON_MODEL_FILENAMES:
        return False
    if not filename.endswith(_MODEL_EXTENSIONS):
        return False
    if any(filename.endswith(suffix) for suffix in _NON_MODEL_SUFFIXES):
        return False
    # threshold_*_final.pkl are MIS1 threshold sidecars, not models.
    if filename.startswith("threshold_") and filename.endswith("_final.pkl"):
        return False
    return True


def _unclassified_artifacts(filename_to_tags: dict[str, set[str]]) -> list[dict[str, Any]]:
    """Model-like files in root/staging that are NOT assigned to any generation.

    No silent drop: whatever the index cannot classify is counted and
    listed with location+md5 (operator sees the gap)."""
    classified = set(filename_to_tags)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for label, directory in _SEARCH_LOCATIONS:
        if not os.path.isdir(directory):
            continue
        try:
            entries = sorted(os.listdir(directory))
        except OSError as exc:  # pragma: no cover - Permission/Race
            logger.warning("directory %s not readable: %s", directory, exc)
            continue
        for filename in entries:
            if filename in seen or filename in classified:
                continue
            if not _is_model_file(filename):
                continue
            abspath = os.path.join(directory, filename)
            if not os.path.isfile(abspath):
                continue
            try:
                md5 = _md5(abspath)
            except OSError as exc:  # pragma: no cover - TOCTOU/Lock/Permission-Race
                logger.warning("artifact %s not readable: %s", abspath, exc)
                continue
            seen.add(filename)
            out.append(
                {
                    "filename": filename,
                    "location": label,
                    "path": _rel(abspath),
                    "md5": md5,
                }
            )
    out.sort(key=lambda e: (e["location"], e["filename"]))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Markdown rendering (human-readable, generated)
# ─────────────────────────────────────────────────────────────────────────────
def _fmt_lifecycle(lifecycle: dict[str, str]) -> str:
    return ", ".join(f"{d}:{lifecycle[d]}" for d in _DIRECTIONS if d in lifecycle)


def _fmt_artifacts(artifacts: list[dict[str, Any]]) -> str:
    if not artifacts:
        return "—"
    parts = []
    for a in artifacts:
        md5 = (a["md5"] or "")[:8] if a["md5"] else "—"
        loc = a["location"]
        parts.append(f"{a['direction']}:`{a['filename']}`@{loc}#{md5}")
    return "<br>".join(parts)


def render_markdown(index: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Bot Variant Index (auto-generated)")
    lines.append("")
    lines.append(
        "> Generated by `tools/bot_variants/index.py` (T-2026-KYT-9050-038). "
        "**Do not edit by hand** — regenerate with `python -m tools.bot_variants.index --write`."
    )
    lines.append(">")
    lines.append(
        "> Join over `core.bot_catalog` (tag→family/script) · `core.shadow_gate` "
        "(lifecycle per (tag,direction) + SHADOW_ARTIFACTS) · artifact meta · filesystem "
        "(root/staging/archive) · git. Deterministic/idempotent."
    )
    lines.append("")
    lines.append(
        f"**Generations:** {index['generation_count']} · "
        f"**shared filenames:** {index['shared_filename_count']} · "
        f"**unclassified artifacts:** {index['unclassified_count']} · "
        f"**unknown tags:** {index['unknown_tag_count']}"
    )
    lines.append("")
    lines.append(
        "`code_ref` conservative in phase 1: `HEAD` if the generation is live/active, "
        "otherwise empty (exact git SHA per old generation follows in phase 2 / D4)."
    )
    lines.append("")

    # Generations — grouped by family.
    lines.append("## Generations")
    lines.append("")
    lines.append(
        "| Family | Tag | Script | Lifecycle | Artifacts (direction:file@location#md5) | model_id | code_ref | Provenance |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for gen in index["generations"]:
        family = gen["family"] or "—"
        script = gen["script"] or "—"
        model_ids = ", ".join(gen["model_ids"]) or "—"
        code_ref = gen["code_ref"] or "—"
        prov = gen["provenance"] or "—"
        note = ""
        if gen["notes"]:
            note = "<br>_" + "; ".join(gen["notes"]) + "_"
        lines.append(
            f"| {family} | `{gen['tag']}` | {script} | {_fmt_lifecycle(gen['lifecycle'])} "
            f"| {_fmt_artifacts(gen['artifacts'])}{note} | {model_ids} | {code_ref} | {prov} |"
        )
    lines.append("")

    # Shared filenames (collision hazard).
    lines.append("## Shared Filenames (Collision Hazard)")
    lines.append("")
    if index["shared_filenames"]:
        lines.append("| File | Tags | Location |")
        lines.append("|---|---|---|")
        for s in index["shared_filenames"]:
            lines.append(f"| `{s['filename']}` | {', '.join(s['tags'])} | {s['location']} |")
    else:
        lines.append("_none_")
    lines.append("")

    # Unclassified artifacts (no silent drop).
    lines.append("## Unclassified Artifacts")
    lines.append("")
    lines.append("_Model-like files without a generation assignment — operator check:_")
    lines.append("")
    if index["unclassified_artifacts"]:
        lines.append("| File | Location | md5 |")
        lines.append("|---|---|---|")
        for u in index["unclassified_artifacts"]:
            lines.append(f"| `{u['filename']}` | {u['location']} | {(u['md5'] or '')[:8]} |")
    else:
        lines.append("_none_")
    lines.append("")

    # Unknown tags (no fleet script).
    lines.append("## Unknown Tags (No Fleet Script)")
    lines.append("")
    if index["unknown_tags"]:
        for t in index["unknown_tags"]:
            lines.append(f"- `{t}`")
    else:
        lines.append("_none_")
    lines.append("")

    return "\n".join(lines)


def _dump_json(index: dict[str, Any]) -> str:
    """Deterministic JSON (sort_keys, fixed indentation, trailing newline)."""
    return json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def write_outputs(index: dict[str, Any]) -> tuple[str, str]:
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(MARKDOWN_OUT), exist_ok=True)
    md = render_markdown(index)
    js = _dump_json(index)
    with open(MARKDOWN_OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(md)
    with open(JSON_OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(js)
    return MARKDOWN_OUT, JSON_OUT


def check_outputs(index: dict[str, Any]) -> list[str]:
    """Compares generated output with the files on disk. Drift list."""
    drift: list[str] = []
    expected = {MARKDOWN_OUT: render_markdown(index), JSON_OUT: _dump_json(index)}
    for path, content in expected.items():
        if not os.path.isfile(path):
            drift.append(f"missing: {_rel(path)}")
            continue
        with open(path, encoding="utf-8") as fh:
            if fh.read() != content:
                drift.append(f"drift: {_rel(path)}")
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bot variant index (read-only discovery, T-2026-KYT-9050-038).")
    parser.add_argument(
        "--write", action="store_true", help="write docs/bot_variants_index.md + model_archive/index.json"
    )
    parser.add_argument(
        "--check", action="store_true", help="check drift against the files on disk (exit 1 on drift)"
    )
    parser.add_argument("--stdout", action="store_true", help="markdown to stdout")
    parser.add_argument(
        "--no-model-meta", action="store_true", help="skip embedded joblib meta (sidecar only)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    # Windows console is cp1252 by default → unicode (→, —) in the markdown crashes
    # print. File writes are utf-8 anyway; defensively align stdout here.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    index = build_index(load_embedded=not args.no_model_meta)

    if args.check:
        drift = check_outputs(index)
        if drift:
            print("INDEX-DRIFT:")
            for d in drift:
                print("  " + d)
            return 1
        print("index up-to-date (no drift)")
        return 0
    if args.stdout:
        print(render_markdown(index))
        return 0

    if args.write:
        md_path, js_path = write_outputs(index)
        print(f"written: {_rel(md_path)}  +  {_rel(js_path)}")
    print(
        f"generations={index['generation_count']} "
        f"shared-filenames={index['shared_filename_count']} "
        f"unclassified={index['unclassified_count']} "
        f"unknown-tags={index['unknown_tag_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
