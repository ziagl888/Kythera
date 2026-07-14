"""Standalone (DB-free) tests for tools/verify_staging_artifacts.py.

T-2026-CU-9050-120. The verifier gates Retrain-Artefakte before Michi's
promotion decision (C2). These tests pin the mechanical contract checks so a
regression can't silently turn a FAIL into a PASS — the whole point of the tool
is that a broken artifact (missing tag, wrong xgboost, dead threshold, feature
drift) is caught BEFORE it reaches the live repo-root.

The pure check_* helpers are unit-tested; verify_artifact is exercised
end-to-end against a synthetic Format-A artifact built with a fake model (no
training, no DB, no xgboost pickling quirks).

Run: pytest backtest/test_verify_staging_artifacts.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import joblib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_verifier():
    path = ROOT / "tools" / "verify_staging_artifacts.py"
    spec = importlib.util.spec_from_file_location("verify_staging_artifacts", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


V = _load_verifier()


def _status(result: tuple[str, str]) -> str:
    return result[0]


# ---------------------------------------------------------------- check_threshold
def test_threshold_valid_is_pass():
    assert _status(V.check_threshold(0.5)) == V.OK
    assert _status(V.check_threshold("0.42")) == V.OK  # string-numerisch ist ok


def test_threshold_idle_default_and_out_of_range_fail():
    assert _status(V.check_threshold(1.0)) == V.FAIL  # 1.0 = Idle-Default, kein Gate
    assert _status(V.check_threshold(0.0)) == V.FAIL
    assert _status(V.check_threshold(1.5)) == V.FAIL


def test_threshold_none_fails_not_crashes():
    # rub2/epd2 LONG tragen real threshold=None (not-deployable) — muss FAIL sein,
    # nicht eine ungefangene Exception.
    assert _status(V.check_threshold(None)) == V.FAIL


# ---------------------------------------------------------------------- check_tag
def test_tag_match_pass_mismatch_and_missing_fail():
    assert _status(V.check_tag({"model_id": "TD2_4H"}, "TD2_4H")) == V.OK
    assert _status(V.check_tag({"model_id": "TD2_1H"}, "TD2_4H")) == V.FAIL
    assert _status(V.check_tag({}, "ABR2")) == V.FAIL  # fehlendes model_id (real: alte ABR-Artefakte)


# ------------------------------------------------------------------- check_features
def test_features_equal_is_pass():
    ref = ["a", "b", "c"]
    assert _status(V.check_features(["a", "b", "c"], ref)) == V.OK


def test_features_unknown_extra_is_fail():
    # Artefakt verlangt eine Spalte, die der Builder nicht liefert -> der Loader
    # lehnt ab (check_feature_contract), also FAIL.
    assert _status(V.check_features(["a", "b", "zzz"], ["a", "b", "c"])) == V.FAIL


def test_features_missing_reference_is_warn_and_empty_is_fail():
    assert _status(V.check_features(["a", "b"], ["a", "b", "c"])) == V.WARN
    assert _status(V.check_features([], ["a"])) == V.FAIL
    assert _status(V.check_features(None, ["a"])) == V.FAIL


# --------------------------------------------------------------- check_xgb_version
def test_xgb_version_parity_and_drift():
    import xgboost as xgb

    running = str(xgb.__version__)
    maj, minor = running.split(".")[:2]
    assert _status(V.check_xgb_version({"xgboost_version": running})) == V.OK
    assert _status(V.check_xgb_version({})) == V.WARN  # fehlend -> nicht prüfbar
    assert _status(V.check_xgb_version({"xgboost_version": "99.0.0"})) == V.FAIL  # Major-Drift = Skew
    assert _status(V.check_xgb_version({"xgboost_version": f"{maj}.{int(minor) + 1}.0"})) == V.WARN


# ------------------------------------------------------------------------ _tf_from
def test_tf_from_extracts_timeframe():
    assert V._tf_from("td_xgboost_model_4h.pkl") == "4h"
    assert V._tf_from("bb_xgboost_model_1h.pkl") == "1h"
    assert V._tf_from("mis2_model_8h_pump.pkl") == ""  # kein reines _1h/_4h-Suffix


# ------------------------------------------------------------------- metric_verdict
def test_metric_verdict_good_block_is_pass():
    block = {"test_stats": {"wr": 68.0, "base_rate_test": 63.7, "sum_net_pnl_pct": 115.6, "n_taken": 75}}
    assert _status(V.metric_verdict(block)) == V.OK


def test_metric_verdict_flags_under_base_negative_pnl_and_thin_n():
    under_base = {"test_stats": {"wr": 59.2, "base_rate_test": 60.7, "sum_net_pnl_pct": 19.4, "n_taken": 76}}
    assert _status(V.metric_verdict(under_base)) == V.WARN  # td_4h-Realfall
    neg_pnl = {"test_stats": {"wr": 70.0, "base_rate_test": 60.0, "sum_net_pnl_pct": -50.0, "n_taken": 100}}
    assert _status(V.metric_verdict(neg_pnl)) == V.WARN
    thin = {"test_stats": {"wr": 80.0, "base_rate_test": 50.0, "sum_net_pnl_pct": 10.0, "n_taken": 12}}
    assert _status(V.metric_verdict(thin)) == V.WARN


# ---------------------------------------------------------------- _iter_stat_blocks
def test_iter_stat_blocks_flat_and_nested():
    flat = {"test_stats": {"wr": 1}}
    labels = [lbl for lbl, _ in V._iter_stat_blocks(flat)]
    assert labels == ["root"]

    nested = {"strategy": "rub2", "LONG": {"val_stats": {"n": 1}}, "SHORT": {"test_stats": {"wr": 2}}}
    got = {lbl for lbl, _ in V._iter_stat_blocks(nested)}
    assert got == {"LONG", "SHORT"}  # je Richtung ein Block, der String-Schlüssel wird ignoriert


# ------------------------------------------------------------------ check_residency
def test_residency_in_staging_passes_outside_fails(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    inside = staging / "td_xgboost_model_4h.pkl"
    inside.write_bytes(b"x")
    assert _status(V.check_residency(str(inside), str(staging))) == V.OK

    outside = tmp_path / "td_xgboost_model_4h.pkl"
    outside.write_bytes(b"x")
    assert _status(V.check_residency(str(outside), str(staging))) == V.FAIL  # HR-2-Verstoß


# ---------------------------------------------------------- verify_artifact end-to-end
class _FakeModel:
    """Minimaler Klassifikator-Stand-in: der Loader ruft predict_proba nie, er
    prüft nur die Existenz des Attributs."""

    def predict_proba(self, X):  # noqa: N803, ANN001, ANN201 - Signatur-Stub
        raise NotImplementedError


def _write_format_a(path: Path, features, threshold, model_id, xgb_ver):
    joblib.dump(
        {
            "model": _FakeModel(),
            "features": list(features),
            "optimal_threshold": threshold,
            "calibrator_isotonic": None,
            "meta": {"model_id": model_id, "xgboost_version": xgb_ver},
        },
        path,
    )


def _spec(features):
    return {
        "family": "td",
        "glob": "td_xgboost_model_*.pkl",
        "fmt": "A",
        "features": list(features),
        "tag": lambda fn: "TD2_4H",
        "stats": lambda fn: "retrain_td_4h_stats.json",
    }


def test_verify_artifact_clean_artifact_passes(tmp_path):
    import xgboost as xgb

    staging = tmp_path / "staging"
    staging.mkdir()
    feats = list(V._load_retrain_module().SNIPER_FEATURES)
    art = staging / "td_xgboost_model_4h.pkl"
    _write_format_a(art, feats, 0.55, "TD2_4H", str(xgb.__version__))

    res = V.verify_artifact(str(art), _spec(feats), str(staging))
    statuses = {name: st for name, st, _ in res["checks"]}
    assert statuses["threshold"] == V.OK
    assert statuses["tag"] == V.OK
    assert statuses["features"] == V.OK
    assert statuses["xgb_version"] == V.OK
    assert statuses["loader"] == V.OK  # der Bot-eigene Loader akzeptiert es
    assert V.worst(res["checks"]) in (V.OK, V.WARN)  # keine mechanischen FAILs


def test_verify_artifact_bad_tag_and_threshold_fail(tmp_path):
    import xgboost as xgb

    staging = tmp_path / "staging"
    staging.mkdir()
    feats = list(V._load_retrain_module().SNIPER_FEATURES)
    art = staging / "td_xgboost_model_4h.pkl"
    # Falscher Tag + Idle-Threshold 1.0 -> beide FAIL, Gesamt-FAIL.
    _write_format_a(art, feats, 1.0, "TD_1H", str(xgb.__version__))

    res = V.verify_artifact(str(art), _spec(feats), str(staging))
    statuses = {name: st for name, st, _ in res["checks"]}
    assert statuses["tag"] == V.FAIL
    assert statuses["threshold"] == V.FAIL
    assert V.worst(res["checks"]) == V.FAIL


def test_report_metrics_handles_missing_file(tmp_path):
    out = V.report_metrics(str(tmp_path), "retrain_nope_stats.json")
    assert out and out[0][1] == V.WARN  # fehlende Stats -> WARN, kein Crash


def test_report_metrics_reads_real_shape(tmp_path):
    stats = {
        "test_stats": {"wr": 59.2, "base_rate_test": 60.7, "sum_net_pnl_pct": 19.4, "n_taken": 76},
    }
    (tmp_path / "retrain_td_4h_stats.json").write_text(json.dumps(stats), encoding="utf-8")
    out = V.report_metrics(str(tmp_path), "retrain_td_4h_stats.json")
    assert out[0][1] == V.WARN  # unter Base-Rate
    assert "Base" in out[0][2]


# ------------------------------------------------- MIS discovery (HIGH-Regression)
def test_mis_family_is_discovered_not_silently_skipped(tmp_path):
    """Der Retrainer schreibt MIS als mis1_model_*.pkl (Trainer-Prefix), NICHT
    mis2_model_* (Bot-Promotion-Slot). Eine mis2_-Glob hätte die GANZE MIS-Familie
    still übersprungen — genau die Coverage-Lücke, die dieses Tool verhindern soll."""
    import glob as _glob

    reg = V.build_registry(V._load_retrain_module())
    fams = {s["family"]: s for s in reg}
    assert fams["mis1"]["glob"] == "mis1_model_*.pkl"
    assert "mis1_move" in fams and "mis1_move_wick" in fams  # Move-Modi = eigene Familien

    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "mis1_model_8h_pump.pkl").write_bytes(b"x")
    (staging / "mis1_move_model_24h_dump.pkl").write_bytes(b"x")

    found = [Path(p).name for p in _glob.glob(str(staging / fams["mis1"]["glob"]))]
    assert found == ["mis1_model_8h_pump.pkl"], "geometry-Glob muss das Staging-File finden"
    assert not any("move" in n for n in found), "geometry-Glob darf die Move-Variante nicht einsammeln"
    # Regression-Guard: die alte falsche mis2_-Glob findet nichts.
    assert _glob.glob(str(staging / "mis2_model_*.pkl")) == []


if __name__ == "__main__":
    # Standalone-Lauf ohne pytest: alle test_*-Funktionen der Reihe nach.
    import inspect
    import tempfile

    fns = [f for name, f in sorted(globals().items()) if name.startswith("test_") and callable(f)]
    passed = 0
    for fn in fns:
        params = inspect.signature(fn).parameters
        if "tmp_path" in params:
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d))
        else:
            fn()
        passed += 1
    print(f"OK - {passed}/{len(fns)} tests passed")
