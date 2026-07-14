r"""
tools/verify_staging_artifacts.py — Post-Retrain Staging-Artefakt-Verifikation
(T-2026-CU-9050-120).

READ-ONLY. Kein DB-Zugriff, kein Live-Touch, KEINE Promotion — die Promotion
eines Artefakts in den Repo-Root (= live) bleibt eine explizite Operator-
Entscheidung von Michi (harte Regel 2 / Eskalation). Dieses Tool liefert nur
den Befund, auf dessen Basis Michi entscheidet.

Prüft jedes Retrain-Artefakt in STAGING_DIR gegen die Fleet-Verträge:

  1. HR-2  Residenz         — Artefakt liegt in STAGING_DIR (nicht Repo-Root);
                              meldet, ob eine Promotion eine vorhandene Live-Datei
                              gleichen Namens im Repo-Root überschriebe (nur Existenz,
                              kein mtime-Vergleich).
  2. HR-7/P0.12 Feature-Vertrag — Artefakt lädt über core.model_artifacts (den
                              Bot-eigenen Loader) UND seine feature-Liste stimmt
                              mit der Trainer/Serving-Referenz (core.*_features
                              bzw. die Konstanten in retrain_from_replay) überein.
  3. HR-6  Modell-Tag       — meta.model_id == erwarteter Generations-Tag der
                              Familie (TD2/BB2/ABR2/MIS2/RUB2/EPD2/ATB2); zum
                              Abgleich wird der aktuell deployte Live-Tag gezeigt.
  4. Threshold              — optimal_threshold ∈ (0,1), nicht der 1.0-Idle-Default.
  5. P3.4  xgboost-Version  — meta.xgboost_version == laufendes xgboost.__version__
                              (stiller predict_proba-Skew bei Major-Drift).
  6. Format B               — model_type startswith "binary" + Kalibrator-Sidecar.
  7. Modell-Objekt          — predict_proba vorhanden (lädt als Klassifikator).
  8. C2-Report              — val/test-WR vs Base-Rate, Netto-PnL und n aus
                              retrain_<name>_stats.json → Go/No-Go-Hinweis (ADVISORY,
                              kein Hard-Fail — Michi-Urteil). Die Kalibrierungs-
                              Monotonie der Buckets bleibt manuelle Sichtung (Doku).

Exit-Code: 1, wenn irgendein MECHANISCHER Contract-Check FAIL ist (lädt nicht /
Feature-Drift / xgb-Version-Skew / Tag falsch / Threshold ungültig); sonst 0.
Metrik-Bedenken (Check 8) sind WARN und ändern den Exit-Code NICHT — ob ein
unterdurchschnittliches Modell trotzdem promotet wird, entscheidet der Operator.

Beispiele:
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

# core.model_artifacts ist der Loader, den die Live-Bots benutzen — genau der
# soll das Artefakt akzeptieren. Import ist DB-frei.
from core import model_artifacts  # noqa: E402

# Statusmarken
OK = "PASS"
WARN = "WARN"
FAIL = "FAIL"


def _load_retrain_module():
    """Lädt tools/retrain_from_replay.py per Pfad, um seine Feature-Konstanten
    und STAGING_DIR zu bekommen. Wird als Datei geladen (kein Package-Import),
    weil tools/ kein installiertes Package ist. Der Modulkopf ist DB-frei."""
    path = os.path.join(REPO_ROOT, "tools", "retrain_from_replay.py")
    spec = importlib.util.spec_from_file_location("retrain_from_replay", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"retrain_from_replay.py nicht ladbar: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# Familien-Registry: Artefakt-Muster -> (Feature-Referenz, erwarteter Tag,
# Format, Stats-Datei, Live-Datei im Repo-Root für den Promotion-Vergleich).
# Die Feature-Referenzen kommen aus dem Retrainer selbst (dieselbe Quelle, aus
# der die Artefakte erzeugt werden — und über core.*_features dieselbe, die die
# Bots servieren, harte Regel 7).
# --------------------------------------------------------------------------- #
def build_registry(R) -> list[dict]:
    """R = das geladene retrain_from_replay-Modul."""
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
        # MIS: der Retrainer SCHREIBT nach STAGING unter dem Trainer-Prefix
        # (mis1_model_*), NICHT unter dem Bot-Promotion-Slot mis2_model_* — die
        # Meta trägt "MIS2" (Bot hängt den Horizont an: MIS2-8H …). Drei Label-
        # Modi (geometry + move/close + move/wick) landen unter je eigenem Prefix;
        # jeder braucht eine Registry-Zeile, sonst wird die Familie still übersprungen.
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
    """'td_xgboost_model_4h.pkl' -> '4h'. Fällt auf '' zurück, wenn kein
    bekanntes TF im Namen steht (Tag-Check schlägt dann sichtbar fehl)."""
    base = os.path.basename(filename)
    for tf in ("1h", "4h"):
        if f"_{tf}." in base or base.endswith(f"_{tf}.pkl"):
            return tf
    return ""


# --------------------------------------------------------------------------- #
# Einzel-Checks. Jede Funktion gibt (status, message) zurück.
# --------------------------------------------------------------------------- #
def check_residency(path: str, staging_dir: str) -> tuple[str, str]:
    in_staging = os.path.abspath(os.path.dirname(path)) == os.path.abspath(staging_dir)
    if not in_staging:
        return FAIL, f"liegt NICHT in STAGING_DIR ({os.path.dirname(path)}) — HR-2-Verstoß"
    # Promotion-Vorschau: existiert eine gleichnamige Live-Datei im Repo-Root?
    # Bewusst KEIN mtime-Vergleich — mtimes sind über Checkouts/Worktrees hinweg
    # kein verlässliches "Staging neuer als Live"-Signal. Nur Existenz-Info.
    live = os.path.join(REPO_ROOT, os.path.basename(path))
    if os.path.exists(live):
        return OK, "in STAGING_DIR; Promotion überschriebe die vorhandene Live-Datei gleichen Namens"
    return OK, "in STAGING_DIR; kein gleichnamiges Live-Artefakt (neuer Slot)"


def check_xgb_version(meta: dict) -> tuple[str, str]:
    import xgboost as xgb

    art_ver = str(meta.get("xgboost_version", "")).strip()
    run_ver = str(xgb.__version__)
    if not art_ver:
        return WARN, f"meta.xgboost_version fehlt (Serving läuft {run_ver}) — Skew nicht prüfbar"
    if art_ver == run_ver:
        return OK, f"xgboost {art_ver} == Serving {run_ver}"
    if art_ver.split(".")[0] != run_ver.split(".")[0]:
        return FAIL, f"xgboost-MAJOR-Drift: Artefakt {art_ver} vs Serving {run_ver} (stiller predict-Skew, P3.4)"
    return WARN, f"xgboost-Minor-Drift: Artefakt {art_ver} vs Serving {run_ver}"


def check_tag(meta: dict, expected_tag: str) -> tuple[str, str]:
    model_id = str(meta.get("model_id", "")).strip()
    if not model_id:
        return FAIL, "meta.model_id fehlt — Bot postet unter Fallback-Konstante (HR-6-Risiko)"
    if model_id != expected_tag:
        return FAIL, f"model_id '{model_id}' != erwarteter Gen-Tag '{expected_tag}' (HR-6)"
    return OK, f"model_id '{model_id}' == erwarteter Gen-Tag"


def check_threshold(threshold) -> tuple[str, str]:
    try:
        t = float(threshold)
    except (TypeError, ValueError):
        return FAIL, f"optimal_threshold nicht numerisch: {threshold!r}"
    if not (0.0 < t < 1.0):
        return FAIL, f"optimal_threshold {t} ausserhalb (0,1) — 1.0 ist der Idle-Default (kein Gate)"
    return OK, f"optimal_threshold {t:.3f} ∈ (0,1)"


def check_features(art_features, ref_features: list[str]) -> tuple[str, str]:
    if not art_features:
        return FAIL, "Artefakt trägt keine Feature-Liste"
    art = list(art_features)
    if art == ref_features:
        return OK, f"{len(art)} Features == Trainer/Serving-Referenz"
    art_set, ref_set = set(art), set(ref_features)
    extra = [c for c in art if c not in ref_set]
    if extra:
        # Der Loader würde das ohnehin ablehnen (check_feature_contract) — der
        # Bot-Builder liefert diese Spalten nicht.
        return FAIL, f"Artefakt verlangt {len(extra)} unbekannte Feature(s): {extra[:5]} (Feature-Drift)"
    missing = [c for c in ref_features if c not in art_set]
    if missing:
        return (
            WARN,
            f"{len(art)} Features, aber {len(missing)} Referenz-Feature(s) fehlen: {missing[:5]} (Builder-Drift?)",
        )
    # Gleiche Menge, andere Reihenfolge: benign — der Bot selektiert per Namen
    # (df[features]), die Reihenfolge im Artefakt ist folgenlos.
    return OK, f"{len(art)} Features == Referenz (gleiche Menge, abweichende Reihenfolge — benign)"


def check_model_object(model) -> tuple[str, str]:
    if model is None:
        return FAIL, "kein Modell-Objekt im Artefakt"
    if not hasattr(model, "predict_proba"):
        return FAIL, f"Modell hat kein predict_proba ({type(model).__name__})"
    return OK, f"{type(model).__name__} mit predict_proba"


# --------------------------------------------------------------------------- #
# Format-spezifisches Laden — sowohl roh (granulare Meta-Checks) als auch über
# den Bot-Loader (der ultimative "akzeptiert der Bot es?"-Check).
# --------------------------------------------------------------------------- #
def load_raw_A(path: str) -> dict:
    """Format A (dict-pkl): {model, features, optimal_threshold,
    calibrator_isotonic, meta}."""
    d = joblib.load(path)
    if not isinstance(d, dict) or "model" not in d:
        raise ValueError("kein dict-Artefakt (Format A) — evtl. rohes Modell")
    return {
        "model": d.get("model"),
        "features": d.get("features"),
        "threshold": d.get("optimal_threshold"),
        "calibrator": d.get("calibrator_isotonic"),
        "meta": dict(d.get("meta", {})),
        "model_type_ok": (OK, "Format A (kein model_type-Vertrag)"),
    }


def load_raw_B(path: str) -> dict:
    """Format B (natives XGB-JSON + _meta.json + _calib.pkl)."""
    import xgboost as xgb

    model = xgb.XGBClassifier()
    model.load_model(path)
    meta_path = path.replace(".json", "_meta.json")
    calib_path = path.replace(".json", "_calib.pkl")
    if not os.path.exists(meta_path):
        raise ValueError(f"{os.path.basename(meta_path)} fehlt — Legacy-Vertrag, kein Retrain-Artefakt")
    with open(meta_path, encoding="utf-8") as fh:
        meta = json.load(fh)
    mtype = str(meta.get("model_type", ""))
    if not mtype.startswith("binary"):
        mt_status = (FAIL, f"model_type '{mtype}' startet nicht mit 'binary' (Loader läse falsche Spalte)")
    elif not os.path.exists(calib_path):
        mt_status = (WARN, "model_type binary, aber _calib.pkl-Sidecar fehlt")
    else:
        mt_status = (OK, f"model_type '{mtype}' + Kalibrator-Sidecar präsent")
    return {
        "model": model,
        "features": meta.get("features"),
        "threshold": meta.get("optimal_threshold"),
        "calibrator": None,
        "meta": dict(meta),
        "model_type_ok": mt_status,
    }


def loader_accepts(path: str, fmt: str, ref_features: list[str], default_tag: str) -> tuple[str, str]:
    """Fährt den EXAKTEN Bot-Loader (core.model_artifacts). loaded=True heisst:
    der Live-Bot würde dieses Artefakt beim Start akzeptieren."""
    if fmt == "A":
        c = model_artifacts.load_artifact(path, ref_features, default_tag)
    else:
        c = model_artifacts.load_artifact_json(path, ref_features, default_tag)
    if c.get("loaded"):
        return OK, f"core.model_artifacts akzeptiert das Artefakt (tag={c.get('tag')})"
    return FAIL, "core.model_artifacts lehnt das Artefakt ab (loaded=False) — Bot liefe im Idle-Modus"


# --------------------------------------------------------------------------- #
# C2-Metrik-Report (advisory)
# --------------------------------------------------------------------------- #
def _iter_stat_blocks(obj, path=""):
    """Findet rekursiv alle Dicts, die 'test_stats' oder 'val_stats' tragen,
    und gibt (label, block) zurück — deckt flache (td/bb) wie verschachtelte
    (rub/epd/mis pro Richtung/Horizont) Stats-JSONs ab."""
    if isinstance(obj, dict):
        if "test_stats" in obj or "val_stats" in obj:
            yield path or "root", obj
        for k, v in obj.items():
            yield from _iter_stat_blocks(v, f"{path}.{k}" if path else str(k))


def metric_verdict(block: dict) -> tuple[str, str]:
    """Advisory Go/No-Go aus einem Stats-Block: schlägt das Modell auf dem
    Test-Slice seine Base-Rate und ist der Netto-PnL positiv?"""
    ts = block.get("test_stats") or {}
    wr = ts.get("wr")
    base = ts.get("base_rate_test")
    pnl = ts.get("sum_net_pnl_pct")
    bits = []
    verdict = OK
    if wr is not None and base is not None:
        bits.append(f"Test-WR {wr:.1f}% vs Base {base:.1f}%")
        if wr < base:
            verdict = WARN
            bits.append("↓ unter Base-Rate")
    if pnl is not None:
        bits.append(f"ΣNet-PnL {pnl:+.1f}%")
        if pnl <= 0:
            verdict = WARN
            bits.append("≤0")
    n = ts.get("n_taken") or ts.get("n")
    if n is not None:
        bits.append(f"n={n}")
        if isinstance(n, (int, float)) and n < 30:
            verdict = WARN
            bits.append("dünn (n<30)")
    if not bits:
        return WARN, "keine test_stats im Block"
    return verdict, "; ".join(bits)


def report_metrics(staging_dir: str, stats_name: str) -> list[tuple[str, str, str]]:
    """Liste (label, status, message) je Stats-Block der Datei."""
    fp = os.path.join(staging_dir, stats_name)
    if not os.path.exists(fp):
        return [("(stats)", WARN, f"{stats_name} noch nicht vorhanden (Retrain gelaufen?)")]
    try:
        with open(fp, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return [("(stats)", WARN, f"{stats_name} nicht lesbar: {e}")]
    out = []
    for label, block in _iter_stat_blocks(data):
        status, msg = metric_verdict(block)
        out.append((label, status, msg))
    if not out:
        out.append(("(stats)", WARN, f"{stats_name} enthält keine test_stats/val_stats"))
    return out


# --------------------------------------------------------------------------- #
# Orchestrierung
# --------------------------------------------------------------------------- #
def verify_artifact(path: str, spec: dict, staging_dir: str) -> dict:
    fn = os.path.basename(path)
    ref_features = spec["features"]
    expected_tag = spec["tag"](fn)
    checks: list[tuple[str, str, str]] = []

    st, msg = check_residency(path, staging_dir)
    checks.append(("residency", st, msg))

    # Rohes Laden (granulare Meta-Checks)
    try:
        raw = load_raw_A(path) if spec["fmt"] == "A" else load_raw_B(path)
    except Exception as e:  # noqa: BLE001 — jede Ladefehlerklasse ist ein FAIL
        checks.append(("load", FAIL, f"lädt nicht: {e}"))
        return {"file": fn, "family": spec["family"], "tag": expected_tag, "checks": checks}

    meta = raw["meta"]
    checks.append(("model", *check_model_object(raw["model"])))
    checks.append(("features", *check_features(raw["features"], ref_features)))
    checks.append(("tag", *check_tag(meta, expected_tag)))
    checks.append(("threshold", *check_threshold(raw["threshold"])))
    checks.append(("xgb_version", *check_xgb_version(meta)))
    if spec["fmt"] == "B":
        checks.append(("format_b", *raw["model_type_ok"]))

    # Der ultimative Check: akzeptiert der Bot-eigene Loader es?
    try:
        checks.append(("loader", *loader_accepts(path, spec["fmt"], ref_features, expected_tag)))
    except Exception as e:  # noqa: BLE001
        checks.append(("loader", FAIL, f"core.model_artifacts wirft: {e}"))

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

    # Der Bot-Loader loggt bei Ablehnung selbst einen error — hier redundant zu
    # unserem eigenen FAIL-Report und würde die Ausgabe out-of-order verrauschen.
    import logging

    logging.getLogger("core.model_artifacts").setLevel(logging.CRITICAL)

    ap = argparse.ArgumentParser(description="Post-Retrain Staging-Artefakt-Verifikation (read-only).")
    ap.add_argument(
        "--staging-dir", default=None, help="Default: KYTHERA_STAGING_DIR bzw. retrain_from_replay.STAGING_DIR"
    )
    ap.add_argument(
        "--only",
        default="",
        help="Kommaliste von Familien (td,bb,abr1,mis1,mis1_move,mis1_move_wick,rub,epd,atb2)",
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
        print(f"❌ STAGING_DIR existiert nicht: {staging_dir}")
        return 1
    import xgboost as xgb

    print(f"Serving xgboost: {xgb.__version__} · python {sys.version.split()[0]}\n")

    any_fail = False
    seen_stats: set[str] = set()

    for spec in registry:
        # _meta.json/_calib.pkl-Sidecars sind KEINE Modell-Artefakte — sonst
        # versucht der Format-B-Loader die Meta-JSON als XGB-Modell zu laden.
        paths = [
            p
            for p in sorted(glob.glob(os.path.join(staging_dir, spec["glob"])))
            if not os.path.basename(p).endswith(("_meta.json", "_calib.pkl"))
        ]
        if not paths:
            continue
        print(f"── {spec['family'].upper()} ({len(paths)} Artefakt(e)) " + "─" * 30)
        for path in paths:
            res = verify_artifact(path, spec, staging_dir)
            status = worst(res["checks"])
            any_fail = any_fail or status == FAIL
            print(f"  {ICON[status]}{res['file']}")
            for name, st, msg in res["checks"]:
                if st != OK:
                    print(f"       {ICON[st]}{name}: {msg}")
        # C2-Metrik-Report je Familie (einmal pro Stats-Datei)
        stats_name = spec["stats"](os.path.basename(paths[0]))
        if stats_name not in seen_stats:
            seen_stats.add(stats_name)
            for label, st, msg in report_metrics(staging_dir, stats_name):
                print(f"     📊 [{label}] {ICON.get(st, '')}{msg}")
        print()

    print("─" * 60)
    if any_fail:
        print("❌ Mindestens ein MECHANISCHER Contract-Check ist FAIL — NICHT promoten, bis behoben.")
        print("   (Metrik-WARNs sind ADVISORY und ändern den Exit-Code nicht — Promotion bleibt Michis Entscheid.)")
        return 1
    print("✅ Keine mechanischen Contract-Fehler. Promotion bleibt Operator-Entscheid (Metrik-WARNs prüfen).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
