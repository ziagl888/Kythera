#!/usr/bin/env python3
# tools/bot_variants/index.py — read-only Bot-Varianten-Index (T-2026-KYT-9050-038, D1).
#
# ZWECK: Aus dem verstreuten Ist-Zustand (Root-/Staging-/Archiv-Artefakte +
# Lifecycle-Register + Fleet-Script-Mapping + git) je *Bot × Generation* eine
# deterministisch regenerierbare Join-Sicht bauen. Das ist die Grundlage, um
# eine alte Generation (a) mit bestehender Infra live zu schalten (T-037-Muster:
# altes Artefakt + Code-Revert auf einen git-SHA + Tag + Register-Flip) oder
# (b) in Sim gegeneinander antreten zu lassen.
#
# Invariants:
#   * READ-ONLY außerhalb docs/ + model_archive/index.json. Kein DB-Zugriff,
#     kein Netzwerk, keine Modell-Promotion (harte Regeln 1/2).
#   * DETERMINISTISCH/IDEMPOTENT: kein now()/Zufall in den Ausgabezeilen; alle
#     Sammlungen stabil sortiert ⇒ zweimal laufen = byte-identischer Output.
#   * KEIN SILENT-DROP (wie bot_catalog): nicht klassifizierbare Artefakt-Files
#     und unbekannte Tags werden gezählt UND gelistet.
#   * GETEILTE DATEINAMEN sichtbar: ein Artefakt-File unter >1 Tag (Root-
#     Kollisions-Hazard, z.B. rub2_model_LONG.pkl unter RUB2+RUB3) wird geflaggt.
#
# QUELLEN (Join): core.bot_catalog (Tag→Script/Family), core.shadow_gate
# (Lifecycle je (tag,dir) + SHADOW_ARTIFACTS), Artefakt-meta (Sidecar
# *_meta.json oder eingebettet), Dateisystem (root/staging/archive), git (HEAD).

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

# (Label, Verzeichnis) — Scan-Reihenfolge = Auflösungs-Priorität für die
# Fundort-Angabe eines Artefakts (root vor staging vor archive).
_SEARCH_LOCATIONS: tuple[tuple[str, str], ...] = (
    ("root", REPO_ROOT),
    ("staging", STAGING_DIR),
    ("archive", ARCHIVE_DIR),
)

# Dateien, die KEINE eigenständigen Modell-Artefakte sind (Sidecars, Reports,
# Configs). Sie dürfen NICHT als "unclassified" gezählt werden.
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
# KURATIERTE GENERATIONS-REGISTRY  (tag → {direction: [filename, …]})
# ─────────────────────────────────────────────────────────────────────────────
# Der EINE kuratierte Baustein: die Brücke von Generations-Tag zu Artefakt-
# Dateiname(n). Nötig, weil die ältesten Legacy-Artefakte (reversion, pump_model,
# model_tsi) die model_id-Konvention noch nicht tragen — ihr Tag lebt nur im
# Loader des jeweiligen Bot-Scripts (zitiert je Eintrag). Alles ANDERE (Script,
# Lifecycle, Threshold, deployable, trained_at, md5, code_ref) wird gejoint/
# abgeleitet, nicht gepflegt. Die Klasse-(A)/Challenger-Shadow-Tags kommen
# additiv aus shadow_gate.SHADOW_ARTIFACTS dazu (siehe _artifact_registry()).
#
# pump=LONG / dump=SHORT (MIS-Konvention, core.mis_features / bot 11).
_MIS_HORIZONS = ("8", "24", "72", "168")


def _mis_registry(tag_prefix: str, file_prefix: str, file_suffix: str) -> dict[str, dict[str, list[str]]]:
    """MIS-Generation je Horizont: MIS?-{h}H → pump(LONG)/dump(SHORT)-Datei."""
    out: dict[str, dict[str, list[str]]] = {}
    for h in _MIS_HORIZONS:
        out[f"{tag_prefix}-{h}H"] = {
            "LONG": [f"{file_prefix}{h}h_pump{file_suffix}"],
            "SHORT": [f"{file_prefix}{h}h_dump{file_suffix}"],
        }
    return out


# Live-/Legacy-Generationen, die NICHT in shadow_gate.SHADOW_ARTIFACTS stehen
# (dort liegen nur die noch-nicht-promoteten Klasse-(A)/Challenger-Tags).
_LEGACY_ARTIFACTS: dict[str, dict[str, list[str]]] = {
    # Rubberband (bot 13). RUB1 = Original-Legacy, seit T-037 wieder live.
    "RUB1": {"LONG": ["long_reversion_model.joblib"], "SHORT": ["short_reversion_model.joblib"]},
    # RUB2-Retrain: SHORT im Root (gebencht), LONG im Staging. rub2_model_LONG.pkl
    # ist zugleich die RUB3-Challenger-Quelle (SHADOW_ARTIFACTS) → geteilter File.
    "RUB2": {"SHORT": ["rub2_model_SHORT.pkl"], "LONG": ["rub2_model_LONG.pkl"]},
    # Pump/Dump (bot 10). EPD2 = EPD_LEGACY_TAG; der Legacy-Loader lädt das rohe
    # 3-Klassen-Modell pump_dump_model.pkl für BEIDE Richtungen. Zusätzlich trägt
    # die EPD2-Generation ihre Retrain-Artefakte epd2_model_{LONG,SHORT}.pkl
    # (EPD2_ARTIFACT_PATHS in 10_pump_dump_detector.py) — epd2_model_LONG.pkl ist
    # zugleich die EPD3-LONG-Shadow-Quelle (SHADOW_ARTIFACTS) ⇒ geteilter File-
    # Hazard, den der Index sichtbar macht.
    "EPD2": {
        "LONG": ["pump_dump_model.pkl", "epd2_model_LONG.pkl"],
        "SHORT": ["pump_dump_model.pkl", "epd2_model_SHORT.pkl"],
    },
    # MIS (bot 11): MIS1 = pump_model_*_final.pkl (revived, T-034), MIS2 = mis2_model_*.pkl.
    **_mis_registry("MIS1", "pump_model_", "_final.pkl"),
    **_mis_registry("MIS2", "mis2_model_", ".pkl"),
    # Trend-Sniper/ATS (bot 12): ATS1_Robust = model_tsi_*_robust.pkl.
    "ATS1_ROBUST": {"LONG": ["model_tsi_long_robust.pkl"], "SHORT": ["model_tsi_short_robust.pkl"]},
    # Master-Ranker AIM2 (bot 15): richtungs-agnostischer Meta-Ranker (eine Datei).
    "AIM2": {"LONG": ["master_meta_model_aim2.pkl"], "SHORT": ["master_meta_model_aim2.pkl"]},
    # SMC-Sniper (bot 25): BB/TD je Timeframe, ein Modell je File (bidirektional genutzt).
    "BB_1H": {"LONG": ["bb_xgboost_model_1h.pkl"], "SHORT": ["bb_xgboost_model_1h.pkl"]},
    "BB_4H": {"LONG": ["bb_xgboost_model_4h.pkl"], "SHORT": ["bb_xgboost_model_4h.pkl"]},
    "TD_1H": {"LONG": ["td_xgboost_model_1h.pkl"], "SHORT": ["td_xgboost_model_1h.pkl"]},
    "TD_4H": {"LONG": ["td_xgboost_model_4h.pkl"], "SHORT": ["td_xgboost_model_4h.pkl"]},
    # Quasimodo (bot 24): QM je Timeframe.
    "QM_1H": {"LONG": ["qm_xgboost_model_1h.pkl"], "SHORT": ["qm_xgboost_model_1h.pkl"]},
    "QM_4H": {"LONG": ["qm_xgboost_model_4h.pkl"], "SHORT": ["qm_xgboost_model_4h.pkl"]},
    # Break&Retest Gen-2 (bot 18): auf Platte als bt2_model_*.json, meta.model_id=ABR2
    # (Dateiname ≠ Tag — der Index macht genau das sichtbar).
    "ABR2": {"LONG": ["bt2_model_LONG.json"], "SHORT": ["bt2_model_SHORT.json"]},
    # Weitere Einzelmodell-Legacies.
    "MAX1": {"SHORT": ["max1_model_SHORT.pkl"]},
    "FIF1": {"LONG": ["fif1_model.pkl"], "SHORT": ["fif1_model.pkl"]},
    "PEX1": {"LONG": ["pex1_model.pkl"], "SHORT": ["pex1_model.pkl"]},
}

# Kurze Provenienz je Familie (MODEL_INTENT/Task-Referenz). Generation-spezifische
# Overrides in _PROVENANCE_TAG.
_PROVENANCE_FAMILY: dict[str, str] = {
    "RUB": "Rubberband HVN/S-R-Reversion (bot 13); RUB1 revived T-037",
    "EPD": "Pump/Dump-Detector (bot 10); EPD2=EPD_LEGACY_TAG",
    "MIS": "Momentum-Impuls-Spike pump/dump (bot 11); MIS1 revived T-034",
    "ATS": "Trend-Strength-Sniper TSI (bot 12)",
    "ATB": "Converging-Channel Break (bot 14); ATB2-Neuaufbau",
    "AIM": "Master-Ranker/Gate über Kandidaten (bot 15)",
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
}
# Bekannte regelbasierte Live-Generationen OHNE Modell-Artefakt und ohne
# Lifecycle-Register-Eintrag (Default-LIVE). Ohne diese Liste fielen aktive
# Fleet-Tags aus dem Index (kein Artefakt ⇒ nicht entdeckt). Richtungen explizit,
# damit der Index nicht fälschlich eine tote Richtung als live zeigt.
_RULE_ONLY_GENERATIONS: dict[str, list[str]] = {
    "MAX2": ["LONG"],  # SRA2-LONG-Fork nach CH_MAIN (bot 9), LONG-only
    "ROM1": ["LONG", "SHORT"],  # Regime-Re-Forwarder (bot 28)
    "UFI1": ["LONG", "SHORT"],  # bot 29 (nicht-Standard-Leverage)
    "TRM1": ["LONG", "SHORT"],  # bot 32
}

_PROVENANCE_TAG: dict[str, str] = {
    "ATS1_ROBUST": "ATS1_Robust Legacy (model_tsi_*_robust.pkl); ATS2 ist der Nachfolger",
    "EPD3": "EPD2-Retrain-Challenger; LONG teilt epd2_model_LONG.pkl mit EPD2",
    "RUB3": "rub2_model_LONG-Challenger vs. live RUB1-LONG",
    "RUB4": "funding-gegatetes RUB3 (fund_24h>+3bps); nutzt RUB3-Artefakt",
    "MAX2": "kein Modell — SRA2-LONG-Fork nach CH_MAIN (bot 9)",
    "AIM2-TOPN": "High-Conviction-Top-N-Kanal über AIM2; retired T-037",
}


def _md5(path: str) -> str:
    h = hashlib.md5()  # noqa: S324 — Integritäts-/Identitäts-Hash, nicht kryptografisch
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel(path: str) -> str:
    """Repo-relativer POSIX-Pfad (deterministisch über Plattformen)."""
    return os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")


def _locate(filename: str) -> tuple[str, str] | None:
    """Erste Fundstelle (Label, absoluter Pfad) eines Dateinamens, oder None."""
    for label, directory in _SEARCH_LOCATIONS:
        candidate = os.path.join(directory, filename)
        if os.path.isfile(candidate):
            return label, candidate
    return None


def _artifact_registry() -> dict[str, dict[str, list[str]]]:
    """Kuratierte Legacy-Registry + Klasse-(A)/Challenger-Tags aus shadow_gate."""
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
    """Alle Tags, die im shadow_gate-Register (Lifecycle + Retired) vorkommen."""
    tags: set[str] = set()
    lifecycle = getattr(shadow_gate, "_LIFECYCLE", {})
    for tag, _direction in lifecycle:
        tags.add(tag.upper())
    for tag in getattr(shadow_gate, "_RETIRED_TAGS", set()):
        tags.add(tag.upper())
    return tags


def _lifecycle_directions(tag: str) -> list[str]:
    """Richtungen, die für einen Tag im Lifecycle-Register genannt sind."""
    lifecycle = getattr(shadow_gate, "_LIFECYCLE", {})
    dirs = {d for (t, d) in lifecycle if t.upper() == tag}
    return [d for d in _DIRECTIONS if d in dirs]


def _extract_meta_fields(meta: dict[str, Any]) -> dict[str, Any]:
    """Vereinheitlicht die für den Index relevanten Felder aus einem meta-Dict."""
    threshold = meta.get("optimal_threshold", meta.get("threshold"))
    deployable = meta.get("deployable")
    val_stats = meta.get("val_stats")
    if deployable is None and isinstance(val_stats, dict):
        deployable = val_stats.get("deployable")
    features = meta.get("features")
    n_features = len(features) if isinstance(features, list) else None
    return {
        "model_id": meta.get("model_id"),
        "strategy": meta.get("strategy"),
        "trainer": meta.get("trainer"),
        "trained_at": meta.get("trained_at"),
        "threshold": threshold if isinstance(threshold, (int, float)) else None,
        "deployable": deployable if isinstance(deployable, bool) else None,
        "n_features": n_features,
    }


def _read_meta(path: str, load_embedded: bool) -> dict[str, Any] | None:
    """Meta eines Artefakts: Sidecar *_meta.json bevorzugt, sonst eingebettet.

    Sidecar ist billig und deckt die Retrain-Generation (retrain_from_replay).
    Eingebettete meta (im joblib-dict) deckt die Sniper-/Einzelmodelle; das
    Laden ist teuer (xgboost/sklearn) und daher über ``load_embedded`` gated.
    Alle Werte sind statische Datei-Inhalte ⇒ deterministisch (kein now()).
    """
    sidecar = os.path.splitext(path)[0] + "_meta.json"
    if os.path.isfile(sidecar):
        try:
            with open(sidecar, encoding="utf-8") as fh:
                return _extract_meta_fields(json.load(fh))
        except (OSError, ValueError) as exc:  # pragma: no cover - defensiv
            logger.warning("meta-Sidecar %s nicht lesbar: %s", sidecar, exc)
            return None
    if not load_embedded or not path.endswith((".pkl", ".joblib")):
        return None
    try:
        import warnings

        import joblib

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            art = joblib.load(path)
    except Exception as exc:  # pragma: no cover - defensiv, Discovery darf nicht sterben
        logger.warning("Artefakt %s nicht ladbar: %s", path, exc)
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
    return _extract_meta_fields(merged)


def _build_artifact_entry(direction: str, filename: str, load_embedded: bool) -> dict[str, Any]:
    """Ein Artefakt-Eintrag: Fundort + md5 + meta (oder MISSING, wenn nicht da).

    Resilienz (Modul-Invariante „Discovery darf nicht sterben"): der Fundort ist
    per isfile() geprüft, aber zwischen Prüfung und Read kann die Datei auf dem
    Live-VPS von einem Trainings-Lauf gesperrt/überschrieben werden (TOCTOU). Ein
    OSError beim md5/stat degradiert deshalb DIESEN Eintrag (exists=False), statt
    den ganzen Index-Lauf zu reißen — analog zum fail-soft joblib-Pfad."""
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
                "meta": _read_meta(abspath, load_embedded),
            }
        except OSError as exc:  # pragma: no cover - TOCTOU/Lock/Permission-Race
            logger.warning("Artefakt %s nicht lesbar (%s): %s", filename, abspath, exc)
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


def build_index(load_embedded: bool = True) -> dict[str, Any]:
    """Baut den vollständigen Varianten-Index als (JSON-serialisierbares) Dict.

    Deterministisch: alle Generationen/Artefakte/Listen stabil sortiert; kein
    now()/Zufall. ``load_embedded=False`` überspringt das teure joblib-Laden
    (nur Sidecar-meta) — für schnelle/Dependency-arme Läufe und Tests.
    """
    registry = _artifact_registry()
    all_tags = set(registry) | _lifecycle_tags() | set(_RULE_ONLY_GENERATIONS)

    generations: list[dict[str, Any]] = []
    # filename → set(tags), um geteilte Dateinamen (Kollisions-Hazard) zu finden.
    filename_to_tags: dict[str, set[str]] = {}
    unknown_tags: list[str] = []

    for tag in sorted(all_tags):
        family = bot_catalog.family_for_tag(tag)
        script = bot_catalog.script_for_tag(tag)
        if script is None:
            unknown_tags.append(tag)

        art_map = registry.get(tag, {})
        # Richtungen: Artefakt-Registry → Lifecycle-Register → Rule-only-Liste →
        # (Fallback) beide.
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
                artifacts.append(_build_artifact_entry(direction, filename, load_embedded))
                filename_to_tags.setdefault(filename, set()).add(tag)
        artifacts.sort(key=lambda a: (a["direction"], a["filename"]))

        model_ids = sorted({a["meta"]["model_id"] for a in artifacts if a["meta"] and a["meta"].get("model_id")})

        notes: list[str] = []
        if not art_map:
            notes.append("regelbasiert / kein Modell-Artefakt")
        missing = sorted({a["filename"] for a in artifacts if not a["exists"]})
        if missing:
            notes.append("Artefakt fehlt auf Platte: " + ", ".join(missing))
        if script is None:
            notes.append("unbekannter Tag — kein Fleet-Script (bot_catalog)")

        # code_ref (Phase 1, konservativ): HEAD wenn die Generation aktiv (live)
        # ist ⇒ Logik im aktuellen Baum. Sonst null — die exakte git-SHA-Auflösung
        # je Alt-Generation ist D4/Phase 2.
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
    """Dateinamen, die von >1 DISTINKTEM Tag beansprucht werden (Hazard)."""
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
    # threshold_*_final.pkl sind MIS1-Threshold-Sidecars, keine Modelle.
    if filename.startswith("threshold_") and filename.endswith("_final.pkl"):
        return False
    return True


def _unclassified_artifacts(filename_to_tags: dict[str, set[str]]) -> list[dict[str, Any]]:
    """Modell-artige Files in root/staging, die KEINER Generation zugeordnet sind.

    Kein Silent-Drop: was der Index nicht klassifizieren kann, wird gezählt und
    mit Fundort+md5 gelistet (Operator sieht die Lücke)."""
    classified = set(filename_to_tags)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for label, directory in _SEARCH_LOCATIONS:
        if not os.path.isdir(directory):
            continue
        try:
            entries = sorted(os.listdir(directory))
        except OSError as exc:  # pragma: no cover - Permission/Race
            logger.warning("Verzeichnis %s nicht lesbar: %s", directory, exc)
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
                logger.warning("Artefakt %s nicht lesbar: %s", abspath, exc)
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
# Markdown-Rendering (menschenlesbar, generiert)
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
    lines.append("# Bot-Varianten-Index (auto-generiert)")
    lines.append("")
    lines.append(
        "> Generiert von `tools/bot_variants/index.py` (T-2026-KYT-9050-038). "
        "**Nicht von Hand editieren** — regenerieren mit `python -m tools.bot_variants.index --write`."
    )
    lines.append(">")
    lines.append(
        "> Join über `core.bot_catalog` (Tag→Family/Script) · `core.shadow_gate` "
        "(Lifecycle je (Tag,Richtung) + SHADOW_ARTIFACTS) · Artefakt-meta · Dateisystem "
        "(root/staging/archive) · git. Deterministisch/idempotent."
    )
    lines.append("")
    lines.append(
        f"**Generationen:** {index['generation_count']} · "
        f"**geteilte Dateinamen:** {index['shared_filename_count']} · "
        f"**unklassifizierte Artefakte:** {index['unclassified_count']} · "
        f"**unbekannte Tags:** {index['unknown_tag_count']}"
    )
    lines.append("")
    lines.append(
        "`code_ref` in Phase 1 konservativ: `HEAD` wenn die Generation live/aktiv "
        "ist, sonst leer (exakte git-SHA je Alt-Generation folgt in Phase 2 / D4)."
    )
    lines.append("")

    # Generationen — gruppiert nach Familie.
    lines.append("## Generationen")
    lines.append("")
    lines.append(
        "| Family | Tag | Script | Lifecycle | Artefakte (Richtung:Datei@Ort#md5) | model_id | code_ref | Provenienz |"
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

    # Geteilte Dateinamen (Kollisions-Hazard).
    lines.append("## Geteilte Dateinamen (Kollisions-Hazard)")
    lines.append("")
    if index["shared_filenames"]:
        lines.append("| Datei | Tags | Ort |")
        lines.append("|---|---|---|")
        for s in index["shared_filenames"]:
            lines.append(f"| `{s['filename']}` | {', '.join(s['tags'])} | {s['location']} |")
    else:
        lines.append("_keine_")
    lines.append("")

    # Unklassifizierte Artefakte (kein Silent-Drop).
    lines.append("## Unklassifizierte Artefakte")
    lines.append("")
    lines.append("_Modell-artige Dateien ohne Generations-Zuordnung — Operator prüfen:_")
    lines.append("")
    if index["unclassified_artifacts"]:
        lines.append("| Datei | Ort | md5 |")
        lines.append("|---|---|---|")
        for u in index["unclassified_artifacts"]:
            lines.append(f"| `{u['filename']}` | {u['location']} | {(u['md5'] or '')[:8]} |")
    else:
        lines.append("_keine_")
    lines.append("")

    # Unbekannte Tags (kein Fleet-Script).
    lines.append("## Unbekannte Tags (kein Fleet-Script)")
    lines.append("")
    if index["unknown_tags"]:
        for t in index["unknown_tags"]:
            lines.append(f"- `{t}`")
    else:
        lines.append("_keine_")
    lines.append("")

    return "\n".join(lines)


def _dump_json(index: dict[str, Any]) -> str:
    """Deterministisches JSON (sort_keys, feste Einrückung, trailing newline)."""
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
    """Vergleicht generierten Output mit den Dateien auf Platte. Drift-Liste."""
    drift: list[str] = []
    expected = {MARKDOWN_OUT: render_markdown(index), JSON_OUT: _dump_json(index)}
    for path, content in expected.items():
        if not os.path.isfile(path):
            drift.append(f"fehlt: {_rel(path)}")
            continue
        with open(path, encoding="utf-8") as fh:
            if fh.read() != content:
                drift.append(f"drift: {_rel(path)}")
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bot-Varianten-Index (read-only Discovery, T-2026-KYT-9050-038).")
    parser.add_argument(
        "--write", action="store_true", help="docs/bot_variants_index.md + model_archive/index.json schreiben"
    )
    parser.add_argument(
        "--check", action="store_true", help="Drift gegen die Dateien auf Platte prüfen (exit 1 bei Drift)"
    )
    parser.add_argument("--stdout", action="store_true", help="Markdown nach stdout")
    parser.add_argument(
        "--no-model-meta", action="store_true", help="eingebettete joblib-meta überspringen (nur Sidecar)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    # Windows-Konsole ist per Default cp1252 → Unicode (→, —) im Markdown crasht
    # den print. Datei-Writes sind ohnehin utf-8; hier stdout defensiv angleichen.
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
        print("index up-to-date (kein Drift)")
        return 0
    if args.stdout:
        print(render_markdown(index))
        return 0

    if args.write:
        md_path, js_path = write_outputs(index)
        print(f"geschrieben: {_rel(md_path)}  +  {_rel(js_path)}")
    print(
        f"Generationen={index['generation_count']} "
        f"geteilte-Dateinamen={index['shared_filename_count']} "
        f"unklassifiziert={index['unclassified_count']} "
        f"unbekannte-Tags={index['unknown_tag_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
