# core/shadow_gate.py — fleet-weiter Shadow-Posting-Gate (T-2026-CU-9050-125).
#
# ZWECK: Jedes (model_tag, direction)-Bein, das NICHT live postet, soll statt
# Stille einen ÜBERWACHTEN Shadow-Trade erzeugen — damit unterdrückte Beine und
# noch-nicht-promotete Retrains eine realisierte Ergebnis-Historie
# (closed_ai_signals) für die spätere Auswertung aufbauen, inkl. der
# regime-konditionierten Freischaltung (Whitelist-v2-Flip, T-2026-CU-9050-069).
#
# WIE ES SICHER IST — "monitored but unposted": Ein Shadow-Trade ist eine
# ai_signals-Zeile OHNE telegram_outbox-Zeile. Der AI-Monitor (8_ai_trade_monitor)
# liest ai_signals ungefiltert, verfolgt Entry/TP/SL und schreibt beim Close eine
# closed_ai_signals-Zeile — er enthält KEINEN Posting-Code. Ein Kanal-Post
# passiert ausschließlich über eine telegram_outbox-Zeile. Kein Outbox-Insert =>
# nie ein Post (verifiziert T-2026-CU-9050-125). Details: docs/SHADOW_MODE_POSTING.md.
#
# SICHERHEITSVERTRAG (harte Regeln 1/2/4):
#   * DEFAULT = LIVE. Dieses Modul listet NUR Beine, die explizit SHADOW oder
#     RETIRED sind; alles andere ist live. Der Gate darf NIE einen bestehenden
#     Live-Post in einen Shadow-Post verwandeln — die Verdrahtung ist rein
#     ADDITIV am Nicht-Post-Zweig jedes Bots.
#   * Shadow-Trades tragen die Modell-Meta model_id (Regel 6). Live- und
#     Shadow-Beine desselben Modells trennt die `direction`; neue Generationen
#     tragen ohnehin einen neuen Tag (ATS2 vs. ATS1) → keine Kollision in
#     closed_ai_signals oder mit has_open_ai_signal.
#   * Master-Kill-Switch KYTHERA_SHADOW_POSTING=0 schaltet ALLE Shadow-Emission
#     ab (Bots fallen auf das heutige prediction-only-Verhalten zurück).

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Lifecycle-Zustände eines (tag, direction)-Beins.
LIVE = "live"  # postet live (Cornix + ai_signals) — Default
SHADOW = "shadow"  # erzeugt einen überwachten Shadow-Trade, kein Cornix-Post
RETIRED = "retired"  # alte Generation, wird nicht mehr emittiert (nur Historie)
SILENT = "silent"  # bewusst stummgeschaltetes Bein: KEIN Live-Post UND KEIN Shadow —
# das Modell läuft (der Bot ist entparkt), gibt aber nichts aus. Genutzt, um ein
# altes Bein (ATS1/ATB1) abzuschalten, während der Retrain (ATS2/ATB2) im Shadow
# datensammelt (Operator-Entscheid Michi, T-2026-CU-9050-127).

_DIRECTIONS = ("LONG", "SHORT")

# Verzeichnis der noch-nicht-promoteten Retrain-Artefakte (harte Regel 2:
# Modell-Artefakte leben in staging_models/, bis Michi sie in den Repo-Root
# promotet). Überschreibbar für Tests via KYTHERA_STAGING_DIR.
STAGING_DIR = os.environ.get("KYTHERA_STAGING_DIR", "staging_models")


def shadow_posting_enabled() -> bool:
    """Master-Schalter. Default AN; KYTHERA_SHADOW_POSTING=0 => komplett aus."""
    return os.environ.get("KYTHERA_SHADOW_POSTING", "1") == "1"


# ─────────────────────────────────────────────────────────────────────────────
# LIFECYCLE-REGISTER  —  (TAG_UPPER, DIRECTION) → Zustand
# ─────────────────────────────────────────────────────────────────────────────
# Nur NICHT-live Beine werden gelistet. Fehlt ein Bein hier, ist es LIVE.
# Quelle der Wahrheit ist die Kombination aus docs/MODEL_INTENT.md (Operator-
# Entscheide) UND dem tatsächlichen Gating im jeweiligen Bot. Jede Zeile trägt
# ihre Begründung — das ist der von Michi auditierbare Kern dieses Features.
#
# SHADOW-Beine zerfallen in zwei Klassen:
#   (A) Neue Generation, noch nicht promotet: das Modell existiert nur in
#       staging_models/ und läuft PARALLEL zum weiter-live alten Tag
#       (ATS2 neben ATS1, ATB2 neben ATB1, …). Siehe SHADOW_ARTIFACTS.
#   (B) Unterdrücktes Richtungs-Bein eines sonst-live Modells: das Modell ist
#       geladen, aber die Richtung geht (noch) nicht live.
_LIFECYCLE: dict[tuple[str, str], str] = {
    # ── (A) Neue-Generation-Shadow-Kandidaten (staging, nicht promotet) ──
    # ATS2: Retrain von ATS1 (Bot 12). ATS1 ist stummgeschaltet (Block (C),
    # T-2026-CU-9050-127) — Bot 12 läuft nur für die ATS2-Shadow-Sammlung.
    ("ATS2", "LONG"): SHADOW,
    ("ATS2", "SHORT"): SHADOW,
    # ATB2: Converging-Channel-Neuaufbau (Bot 14). ATB1 ist stummgeschaltet (C); ATB2 hat
    # optimal_threshold=null (LONG) bzw. ist nicht deploybar (SHORT) → braucht
    # zwingend Shadow-Datensammlung, bevor je ein Operating-Point wählbar ist.
    ("ATB2", "LONG"): SHADOW,
    ("ATB2", "SHORT"): SHADOW,
    # SRA2: Meta-Filter-Retrain (Bot 9). SRA1 bleibt live. SRA2 war "nicht
    # deploybar", WEIL die Label-Quelle closed_trades3 seit Feb tot ist — ein
    # reines TRAININGS-Problem. Shadow-Serving umgeht das komplett: der AI-Monitor
    # liefert die frischen Outcomes (closed_ai_signals), die der tote Tracker nicht
    # mehr gibt → Shadow REVIVED SRA2. SHORT-Threshold ist null (jedes Setup).
    ("SRA2", "LONG"): SHADOW,
    ("SRA2", "SHORT"): SHADOW,
    # ── (B) Challenger-Beine: der Retrain fordert ein LIVE-Bein heraus, das bereits
    #        unter DEMSELBEN Tag postet → eigener Generations-Tag, sonst würde der
    #        Shadow-Trade über den Active-Trade-Check des Bots einen LIVE-Post
    #        blockieren (Verletzung der rein-additiven Invariante). ──
    # RUB3 = rub2_model_LONG-Retrain vs. LIVE-RUB-LONG (Bot 13 postet Legacy unter
    # "RUB2"). Operator-Entscheid Michi (Regel 6). SHORT bleibt live "RUB2".
    ("RUB3", "LONG"): SHADOW,
    # EPD3 = epd2_model_{LONG,SHORT}-Retrain vs. LIVE-EPD (Bot 10 postet das Legacy-
    # Modell bereits unter Tag "EPD2" = EPD_LEGACY_TAG; ein Shadow unter "EPD2"
    # würde über den dortigen Active-Trade-Check `model IN ('EPD2','EPD2')` einen
    # Live-Post unterdrücken). Deshalb eigener Tag "EPD3" — analog zu RUB3.
    ("EPD3", "LONG"): SHADOW,
    ("EPD3", "SHORT"): SHADOW,
    # ── (C) Stummgeschaltete Alt-Beine (Operator Michi, T-2026-CU-9050-127) ──
    # Bots 12/14 werden entparkt, damit ATS2/ATB2 im Shadow laufen — aber die
    # ALTEN Modelle ATS1/ATB1 sollen NICHT live posten und auch nicht shadowen:
    # sie gehen komplett still. Der Bot fragt is_live() am Post-Zweig; SILENT ⇒
    # nicht live ⇒ der ganze ATS1/ATB1-Ausgabe-Zweig wird übersprungen.
    ("ATS1", "LONG"): SILENT,
    ("ATS1", "SHORT"): SILENT,
    ("ATB1", "LONG"): SILENT,
    ("ATB1", "SHORT"): SILENT,
}

# RETIRED: Tags, die in der closed_ai_signals-Historie vorkommen, aber von keinem
# Live-Bot mehr emittiert werden. Reine Report-Klassifikation (Teil 2) — kein
# Posting-Effekt. Richtung ist hier egal (beide Richtungen retired).
_RETIRED_TAGS: set[str] = {
    "AIM1",  # §9: AIM1-Konzept offiziell abgelöst durch AIM2 (Ranker/Gate).
    "MIS1",  # §1: MIS1 beim MIS2-Go-Live abgeschaltet, Bot 11 ohne Legacy-Load.
    "MSI1",  # historischer MIS-Typo-Family-Tag (bot_naming normalisiert → MIS1).
}


# ─────────────────────────────────────────────────────────────────────────────
# SHADOW-ARTEFAKTE  —  Klasse-(A)-Modelle aus staging_models/
# ─────────────────────────────────────────────────────────────────────────────
# Pro neuem Tag die Artefakt-Dateinamen je Richtung. Der Bot lädt sie über
# load_shadow_artifact() zusätzlich zu seinem Live-Modell und scored parallel.
# Fehlt die Datei (nicht gestaget), liefert der Loader None → der Bot läuft
# unverändert weiter (kein harter Fehler).
SHADOW_ARTIFACTS: dict[str, dict[str, str]] = {
    "ATS2": {"LONG": "ats2_model_LONG.pkl", "SHORT": "ats2_model_SHORT.pkl"},
    "ATB2": {"LONG": "atb2_model_LONG.pkl", "SHORT": "atb2_model_SHORT.pkl"},
    "SRA2": {"LONG": "sra2_model_LONG.json", "SHORT": "sra2_model_SHORT.json"},
    # Challenger-Tags (siehe _LIFECYCLE (B)) — Artefakt-Dateiname trägt weiter die
    # Retrain-Generation, der Tag darüber ist der kollisionsfreie Shadow-Tag.
    "RUB3": {"LONG": "rub2_model_LONG.pkl"},
    "EPD3": {"LONG": "epd2_model_LONG.pkl", "SHORT": "epd2_model_SHORT.pkl"},
}


def _norm(tag: str) -> str:
    return (tag or "").strip().upper()


def is_retired(tag: str, direction: str = "") -> bool:
    """True, wenn der Tag zu einer abgelösten Generation gehört. Prefix-Grenze,
    weil closed_ai_signals-Tags Familien sind (``MIS1-8h``, ``MIS1-72H``) — aber
    ``MIS2-8h`` darf NICHT auf ``MIS1`` matchen."""
    t = _norm(tag)
    for rt in _RETIRED_TAGS:
        if t == rt or t.startswith(rt + "-") or t.startswith(rt + "_"):
            return True
    return False


def leg_status(tag: str, direction: str) -> str:
    """Lifecycle-Zustand eines Beins. Default LIVE (Sicherheitsvertrag)."""
    if is_retired(tag):
        return RETIRED
    return _LIFECYCLE.get((_norm(tag), _norm(direction)), LIVE)


def is_live(tag: str, direction: str) -> bool:
    return leg_status(tag, direction) == LIVE


def is_shadow(tag: str, direction: str) -> bool:
    return leg_status(tag, direction) == SHADOW


def is_silent(tag: str, direction: str) -> bool:
    return leg_status(tag, direction) == SILENT


def shadow_artifact_path(tag: str, direction: str) -> str | None:
    """Absoluter/relativer Pfad des Staging-Artefakts eines Klasse-(A)-Shadow-
    Tags, oder None, wenn der Tag kein Staging-Artefakt hat."""
    fname = SHADOW_ARTIFACTS.get(_norm(tag), {}).get(_norm(direction))
    if not fname:
        return None
    return os.path.join(STAGING_DIR, fname)


def load_shadow_artifact(tag: str, direction: str):
    """Lädt ein Klasse-(A)-Shadow-Modell aus staging_models/ (fail-soft).

    Normalisiert BEIDE Fleet-Artefakt-Formate auf ein schlankes Shadow-Contract-
    Dict ``{model, features, threshold}``:
      * ``.pkl`` — retrain_from_replay joblib-dict (ats2/atb2/rub2/epd2/max1),
        Keys ``model / features / optimal_threshold``.
      * ``.json`` — natives XGB-JSON + ``_meta.json``-Sidecar (sra2/abr2).

    Wichtig: Die PRODUKTIONS-Loader (core.model_artifacts) verweigern hier —
    ``build_contract`` macht ``float(optimal_threshold)`` und CRASHT auf den
    NICHT-deploybaren Retrains (threshold ``null`` bei ATB2/SRA2-SHORT/EPD2-LONG/
    RUB2-LONG). Genau die wollen wir aber shadow-sammeln, deshalb dieser tolerante
    Loader: ``threshold=None`` ist zulässig (→ Emission auf JEDEM Kandidaten).

    Rückgabe: ``{model, features, threshold}`` oder None (Tag unbekannt / Datei
    fehlt / Ladefehler — Bot läuft dann ohne Shadow-Bein weiter, harte Regel 2).
    """
    path = shadow_artifact_path(tag, direction)
    if not path or not os.path.exists(path):
        return None
    try:
        if path.endswith(".json"):
            import json

            import xgboost as xgb

            model = xgb.XGBClassifier()
            model.load_model(path)
            meta_path = path[:-5] + "_meta.json"
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
            return {"model": model, "features": list(meta["features"]), "threshold": meta.get("optimal_threshold")}
        import joblib

        art = joblib.load(path)
        return {"model": art["model"], "features": list(art["features"]), "threshold": art.get("optimal_threshold")}
    except Exception as e:  # pragma: no cover - defensiv, Bot darf nicht sterben
        logger.warning("Shadow-Artefakt %s/%s laden fehlgeschlagen (%s): %s", tag, direction, path, e)
        return None


def artifact_threshold(artifact) -> float | None:
    """Operating-Threshold aus dem Contract-Artefakt (``optimal_threshold``).

    None bedeutet: das Modell hat KEINEN validen Operating-Point (z. B. ATB2 —
    zu dünne Daten, pick_threshold_safe hat verweigert). Der Bot emittiert dann
    auf JEDEM Detektor-Event (der Detektor ist das Gate), damit überhaupt
    Shadow-Daten für eine spätere Threshold-Wahl entstehen. Ist ein Threshold
    gesetzt, emittiert der Bot nur bei prob >= threshold — getreue Vorschau des
    Live-Verhaltens nach einer Promotion.
    """
    if not isinstance(artifact, dict):
        return None
    # Normalisierter Shadow-Contract nutzt "threshold"; roher joblib-dict (falls
    # ein Bot direkt joblib.load nutzt) trägt "optimal_threshold".
    thr = artifact.get("threshold", artifact.get("optimal_threshold"))
    try:
        return float(thr) if thr is not None else None
    except (TypeError, ValueError):
        return None


def score_artifact(artifact, feature_row: dict) -> float:
    """ROHE ``predict_proba[:, 1]`` des Contract-Artefakts auf einem Feature-Dict.

    Gate-Semantik ist ROH: ``pick_threshold_safe`` (tools/retrain_from_replay.py)
    wählt ``optimal_threshold`` auf der rohen predict_proba; der mitgelieferte
    Isotonic-Kalibrator ist nur Reporting (identisch zu Bot 13/25). Der Feature-
    Vertrag (Reihenfolge + Auswahl) kommt aus ``artifact["features"]``.
    """
    import pandas as pd

    feats = artifact["features"]
    X = pd.DataFrame([feature_row]).reindex(columns=feats).fillna(0)
    return float(artifact["model"].predict_proba(X)[0, 1])
