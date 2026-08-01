"""Standalone (DB-free) pin for the re-tagged EPD3-SHORT staging artifact.

T-2026-KYT-9050-057 Teil 2. `epd3_model_SHORT.pkl` was promoted on 2026-07-21
carrying the meta of the generation that trained it — `meta.model_id='EPD2'`,
the PREVIOUS tag (hard rule 6). `tools/retag_artifact.py` re-dumped it into
`staging_models/` under `model_id='EPD3'`; these tests pin that the re-dump did
exactly that and nothing else:

  * the staging artifact loads through core.model_artifacts — the loader the live
    bots use — and reports tag EPD3, taken from the artifact, never from the
    caller's fallback constant;
  * every OTHER field is identical to the promoted root artifact: the same
    feature list in the same order, the same threshold, the same model scores on
    a fixed probe matrix, the same isotonic calibrator curve, and the same meta
    apart from the one key. A re-dump is a re-serialisation, not a retrain — a
    changed score here would mean the round-trip silently altered the model;
  * every mechanical contract check of tools/verify_staging_artifacts.py passes
    over the file, including the HR-6 tag check that is the whole point.

The comparison is a PIN on this specific pair of files. A genuine EPD3 retrain
replaces both and must replace this pin with the new evidence — it is not a
"refresh the golden and move on" case.

NOTE on the sibling artifact: `epd3_model_LONG.pkl` carries the same
`model_id='EPD2'` defect but was pickled under sklearn 1.9.0 (the 3.14 env),
while the fleet serves 1.7.1. Re-dumping it under the fleet interpreter would
downgrade its isotonic calibrator, so retag_artifact.py refuses it — see the
tool's version-parity guard. Out of scope here on purpose.

Run: pytest backtest/test_epd3_artifact_model_id.py
     python backtest/test_epd3_artifact_model_id.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import joblib
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import model_artifacts  # noqa: E402

STAGING = ROOT / "staging_models" / "epd3_model_SHORT.pkl"
PROMOTED = ROOT / "epd3_model_SHORT.pkl"

EXPECTED_TAG = "EPD3"
# Ein Sentinel statt des echten Fallbacks: bekäme der Contract seinen Tag aus der
# Call-Site statt aus meta.model_id, stünde genau dieser String im Ergebnis.
SENTINEL_TAG = "TAG-FROM-CALLER-NOT-ARTIFACT"

PROBE_ROWS = 64
PROBE_SEED = 20260801
CALIB_GRID = np.linspace(0.0, 1.0, 101)


def _load_module(name: str):
    path = ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"{path} nicht ladbar"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


V = _load_module("verify_staging_artifacts")
R = V._load_retrain_module()
EPD_FEATURES = list(R.EPD2_FEATURES)


def _raw(path: Path) -> dict:
    assert path.exists(), f"Artefakt fehlt (git-getrackt, sollte im Checkout liegen): {path}"
    return joblib.load(path)


def _scores(art: dict) -> np.ndarray:
    rng = np.random.default_rng(PROBE_SEED)
    return np.asarray(art["model"].predict_proba(rng.normal(size=(PROBE_ROWS, len(art["features"])))))


# ------------------------------------------------------------------ der eigentliche Fix
def test_staging_artifact_loads_through_the_bot_loader_as_epd3():
    contract = model_artifacts.load_artifact(str(STAGING), EPD_FEATURES, SENTINEL_TAG)
    assert contract["loaded"], "core.model_artifacts lehnt das Staging-Artefakt ab"
    assert contract["tag"] == EXPECTED_TAG, f"Tag {contract['tag']!r} statt {EXPECTED_TAG!r}"
    assert contract["meta"]["model_id"] == EXPECTED_TAG
    assert contract["calibrator"] is not None, "Kalibrator im Round-Trip verloren gegangen"
    assert 0.0 < contract["threshold"] < 1.0


def test_promoted_root_artifact_is_untouched_by_this_task():
    # Harte Regel 2: der Root-Promote ist Michis Entscheidung. Dieser Task hat NUR
    # nach staging_models/ geschrieben — das Root-Artefakt trägt weiterhin den
    # ererbten Tag. Nach einer späteren Promotion trägt es EPD3; beides ist hier
    # zulässig, unzulässig wäre ein DRITTER Wert (jemand hat daran gedreht).
    assert str(_raw(PROMOTED)["meta"]["model_id"]) in {"EPD2", EXPECTED_TAG}


# ------------------------------------------------------------------ nichts sonst geändert
def test_redump_changed_only_the_model_id():
    before, after = _raw(PROMOTED), _raw(STAGING)

    assert sorted(before) == sorted(after), "Artefakt-Keys weichen ab"
    assert list(before["features"]) == list(after["features"]), "Feature-Liste/Reihenfolge weicht ab"
    assert repr(before["optimal_threshold"]) == repr(after["optimal_threshold"]), "Threshold weicht ab"

    # Scores statt Objekt-Identität: das ist die Eigenschaft, die zählt — ein
    # Re-Dump erhält keine Objekt-Identität, wohl aber jede Vorhersage.
    np.testing.assert_array_equal(_scores(before), _scores(after))

    cal_before = before["calibrator_isotonic"].predict(CALIB_GRID)
    cal_after = after["calibrator_isotonic"].predict(CALIB_GRID)
    np.testing.assert_array_equal(cal_before, cal_after)

    meta_before, meta_after = dict(before["meta"]), dict(after["meta"])
    assert meta_after.pop("model_id") == EXPECTED_TAG
    meta_before.pop("model_id", None)
    differing = [
        k for k in sorted(set(meta_before) | set(meta_after)) if repr(meta_before.get(k)) != repr(meta_after.get(k))
    ]
    assert not differing, f"meta ausserhalb model_id verändert: {differing}"


# ------------------------------------------------------------------ der Verifier-Vertrag
def _epd3_spec() -> dict:
    """Die Verifier-Familie, unter der ein EPD3-Artefakt geprüft gehört.

    build_registry() globt heute nur `epd2_model_*.pkl` — die EPD3-Challenger-
    Dateien fallen durch das Raster und werden vom CLI-Lauf still übersprungen.
    Der Contract, den der Verifier prüft, gilt trotzdem, und dieser Test fährt
    ihn deshalb direkt. Die Registry-Erweiterung ist Folgearbeit: sie zieht
    epd3_model_LONG.pkl mit herein, das denselben Tag-Defekt trägt, aber (siehe
    Modul-Docstring) hier nicht sauber re-dumpbar ist.
    """
    return {
        "family": "epd3",
        "glob": "epd3_model_*.pkl",
        "fmt": "A",
        "features": EPD_FEATURES,
        "tag": lambda fn: EXPECTED_TAG,
        "stats": lambda fn: "retrain_epd2_stats.json",
    }


def test_verifier_contract_checks_pass_on_the_staging_artifact():
    res = V.verify_artifact(str(STAGING), _epd3_spec(), str(STAGING.parent))
    failed = [(name, msg) for name, st, msg in res["checks"] if st == V.FAIL]
    assert not failed, f"mechanische Contract-Checks FAIL: {failed}"
    by_name = {name: st for name, st, _ in res["checks"]}
    assert by_name["tag"] == V.OK, "HR-6-Tag-Check ist der Grund für diesen Re-Dump"
    assert by_name["loader"] == V.OK
    assert by_name["features"] == V.OK


if __name__ == "__main__":
    import logging

    logging.getLogger("core.model_artifacts").setLevel(logging.CRITICAL)
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("OK — EPD3-SHORT-Staging-Artefakt trägt model_id=EPD3, sonst unverändert")
