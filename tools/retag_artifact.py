#!/usr/bin/env python3
# tools/retag_artifact.py — Re-Dump eines dict-pkl-Artefakts unter neuem
# meta.model_id (T-2026-KYT-9050-057, Teil 2).
#
# DAS PROBLEM (harte Regel 6):
# Ein Challenger erbt beim Retrain die Meta seines Generations-Trainers. Das
# promotete `epd3_model_SHORT.pkl` trägt eingebettet `meta.model_id='EPD2'` —
# der Tag der VORGÄNGER-Generation. Live ist das heute inert (Bot 10 übergibt
# 'EPD3' explizit am Call-Site, und der Shadow-Loader liest model_id nicht),
# aber `core.model_artifacts.build_contract` liest den Posting-Tag NUR aus
# `meta.model_id` — jeder Report/Verifier, der über den Loader geht, attribuiert
# das Artefakt damit der falschen Generation. `tools/verify_staging_artifacts.py`
# prüft genau dieses Feld (Check 3, HR-6).
#
# WAS DAS TOOL TUT: es lädt ein Format-A-Artefakt (dict-pkl), setzt AUSSCHLIESSLICH
# `meta.model_id` und schreibt das Ergebnis nach STAGING. Kein Retrain, kein
# Refit — Modell, Kalibrator, Feature-Liste, Threshold und jedes andere Meta-Feld
# gehen unverändert durch. Nach dem Schreiben wird das Ergebnis neu geladen und
# gegen die Quelle verifiziert (Scores auf einer Probe-Matrix, Kalibrator-Kurve,
# Meta-Diff) — ein Re-Dump, der irgendetwas anderes verändert, ist ein Fehler.
#
# ZWEI GUARDS, beide nicht abschaltbar:
#
#   1. HARTE REGEL 2 — geschrieben wird NUR nach STAGING_DIR. Die Promotion in
#      den Repo-Root (= live) bleibt Michis Entscheidung, nie Teil eines Tool-
#      Laufs. Ein --out ausserhalb Staging ist ein Abbruch.
#   2. VERSIONS-PARITÄT — ein Re-Dump ist eine RE-SERIALISIERUNG: die Objekte
#      werden mit dem sklearn/xgboost der LAUFENDEN Interpreter-Version neu
#      gepickelt. Läuft er unter einer anderen sklearn-Version als der, die das
#      Artefakt erzeugt hat, schreibt er die Estimator-Innereien in einem anderen
#      State-Format zurück — beim EPD3-LONG-Artefakt (sklearn 1.9.0, im 3.14-Env
#      gedumpt) wäre ein Re-Dump unter der Fleet-Python 3.13 (sklearn 1.7.1) ein
#      Downgrade des Isotonic-Kalibrators. Deshalb: lädt das Artefakt mit
#      sklearn's eigener `InconsistentVersionWarning`, bricht das Tool ab und
#      nennt den Interpreter, unter dem der Re-Dump sauber wäre. Für
#      `epd3_model_SHORT.pkl` (eingebettet sklearn 1.7.1) ist der Lauf unter
#      py -3.13 ein Same-Version-Round-Trip und damit warnungsfrei.
#
# Aufruf (Fleet-Python — dieselbe Version, die das Artefakt serviert):
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

# Format-A-Contract-Keys (tools/retrain_from_replay.save_artifact).
ARTIFACT_KEYS = ("model", "features", "optimal_threshold", "calibrator_isotonic", "meta")

# Probe-Gitter für die Äquivalenz-Prüfung: fixer Seed ⇒ reproduzierbar, und die
# Prüfung läuft auf den SCORES statt auf Objekt-Identität (was ein Re-Dump per
# Definition nicht erhält).
PROBE_ROWS = 64
PROBE_SEED = 20260801
CALIB_GRID = np.linspace(0.0, 1.0, 101)


def staging_dir() -> str:
    """Der Staging-Ordner dieses Checkouts (Konvention aus core.shadow_gate:
    repo-relativ `staging_models`, per KYTHERA_STAGING_DIR überschreibbar)."""
    return os.path.abspath(os.environ.get("KYTHERA_STAGING_DIR") or os.path.join(REPO_ROOT, "staging_models"))


def resolve_out(source: str, out: str | None) -> str:
    """Zielpfad in STAGING. `--out` darf ein blosser Dateiname sein; alles, was
    nicht in STAGING landet, ist ein Abbruch (harte Regel 2)."""
    target_dir = staging_dir()
    if target_dir == os.path.abspath(REPO_ROOT):
        raise SystemExit(f"Refuse: STAGING_DIR zeigt auf den Repo-Root ({target_dir}) — das wäre eine Promotion.")
    path = os.path.abspath(
        out if out and os.path.dirname(out) else os.path.join(target_dir, os.path.basename(out or source))
    )
    if os.path.dirname(path) != target_dir:
        raise SystemExit(
            f"Refuse: Ziel liegt nicht in STAGING_DIR.\n  Ziel:    {path}\n  Staging: {target_dir}\n"
            "  Die Promotion eines Artefakts in den Repo-Root ist Operator-Entscheidung (harte Regel 2)."
        )
    return path


def load_checked(path: str) -> dict:
    """Lädt ein Format-A-Artefakt und erzwingt Versions-Parität (Guard 2).

    sklearn meldet einen Serialisierungs-Skew selbst: `InconsistentVersionWarning`
    trägt die Version, unter der das Objekt gepickelt wurde. Keine Warnung heisst,
    dass der laufende Interpreter dasselbe State-Format schreibt wie das, das er
    gerade gelesen hat — genau die Bedingung für einen verlustfreien Re-Dump.
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
            f"Refuse: {os.path.basename(path)} wurde mit sklearn {', '.join(origins)} gepickelt, "
            f"dieser Interpreter läuft {sklearn.__version__} (betroffen: {', '.join(names)}).\n"
            "  Ein Re-Dump wäre kein Round-Trip, sondern ein Formatwechsel der Estimator-Innereien.\n"
            f"  Erneut unter dem Interpreter mit sklearn {origins[0]} ausführen — oder das Artefakt neu trainieren."
        )
    if not isinstance(art, dict) or not all(k in art for k in ("model", "features", "meta")):
        raise SystemExit(f"Refuse: {os.path.basename(path)} ist kein Format-A-dict-Artefakt (Keys: {_keys(art)}).")
    return art


def _keys(art) -> str:
    return ", ".join(sorted(art)) if isinstance(art, dict) else type(art).__name__


def scores(art: dict) -> np.ndarray:
    """Modell-Ausgabe auf einer deterministischen Probe-Matrix."""
    rng = np.random.default_rng(PROBE_SEED)
    x = rng.normal(size=(PROBE_ROWS, len(art["features"])))
    return np.asarray(art["model"].predict_proba(x))


def calib_curve(art: dict) -> np.ndarray | None:
    cal = art.get("calibrator_isotonic")
    return None if cal is None else np.asarray(cal.predict(CALIB_GRID))


def diff_report(before: dict, after: dict) -> list[str]:
    """Alles, was sich zwischen Quelle und Re-Dump geändert hat. Erwartet wird
    GENAU eine Zeile: meta.model_id."""
    out: list[str] = []
    for key in sorted(set(before) | set(after)):
        if key not in before or key not in after:
            out.append(f"key {key}: nur in {'Quelle' if key in before else 'Re-Dump'}")
    if list(before["features"]) != list(after["features"]):
        out.append("features: Liste weicht ab")
    if repr(before.get("optimal_threshold")) != repr(after.get("optimal_threshold")):
        out.append(f"optimal_threshold: {before.get('optimal_threshold')!r} -> {after.get('optimal_threshold')!r}")
    if not np.allclose(scores(before), scores(after), rtol=0, atol=0):
        out.append("model: predict_proba weicht auf der Probe-Matrix ab")
    cb, ca = calib_curve(before), calib_curve(after)
    if (cb is None) != (ca is None):
        out.append("calibrator_isotonic: in einer der beiden Fassungen None")
    elif cb is not None and not np.allclose(cb, ca, rtol=0, atol=0):
        out.append("calibrator_isotonic: Kurve weicht ab")
    mb, ma = dict(before.get("meta", {})), dict(after.get("meta", {}))
    for key in sorted(set(mb) | set(ma)):
        if repr(mb.get(key)) != repr(ma.get(key)):
            out.append(f"meta.{key}: {mb.get(key)!r} -> {ma.get(key)!r}")
    return out


def retag(source: str, model_id: str, out_path: str) -> list[str]:
    """Schreibt die neu getaggte Fassung und gibt den verifizierten Diff zurück."""
    art = load_checked(source)
    meta = dict(art.get("meta", {}))
    old = meta.get("model_id")
    meta["model_id"] = model_id
    joblib.dump({**{k: art[k] for k in ARTIFACT_KEYS if k in art}, "meta": meta}, out_path)
    print(f"  💾 {out_path}  (meta.model_id {old!r} -> {model_id!r})")

    # Verifikation gegen die QUELLE, nicht gegen das In-Memory-Objekt: nur das
    # neu geladene Artefakt beweist, dass der Round-Trip verlustfrei war.
    return diff_report(load_checked(source), load_checked(out_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-Dump eines dict-pkl-Artefakts unter neuem meta.model_id (Staging-only, harte Regel 2)."
    )
    parser.add_argument("source", help="Quell-Artefakt (Format A). Wird nur gelesen.")
    parser.add_argument("--model-id", required=True, help="Neuer Posting-Tag, z.B. EPD3 (harte Regel 6)")
    parser.add_argument("--out", default=None, help="Ziel-Dateiname in STAGING_DIR (Default: Name der Quelle)")
    args = parser.parse_args(argv)

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")

    if not os.path.exists(args.source):
        raise SystemExit(f"Refuse: Quelle fehlt: {args.source}")
    out_path = resolve_out(args.source, args.out)

    import sklearn
    import xgboost

    print(f"python {sys.version.split()[0]} · sklearn {sklearn.__version__} · xgboost {xgboost.__version__}")
    print(f"Quelle:  {os.path.abspath(args.source)}")

    changes = retag(args.source, args.model_id, out_path)
    unexpected = [c for c in changes if not c.startswith("meta.model_id:")]
    if unexpected:
        print("\n❌ Der Re-Dump hat mehr als den Tag verändert:")
        for c in unexpected:
            print(f"   - {c}")
        return 1
    if not changes:
        print("\n✅ Ziel trägt bereits diesen Tag — Artefakt ist unverändert identisch zur Quelle.")
        return 0
    print(f"\n✅ Verifiziert: genau EIN Unterschied zur Quelle — {changes[0]}")
    print("   Modell-Scores, Kalibrator-Kurve, Features, Threshold und übrige Meta sind identisch.")
    print("   Promotion in den Repo-Root bleibt Operator-Entscheid (harte Regel 2).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
