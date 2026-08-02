# backtest/test_retrain_model_id.py
"""DB-freie Tests für den EPD-Generations-Tag am Retrainer (T-2026-KYT-9050-004).

`run_epd` schrieb Tag und Dateinamen als Literale: `meta.model_id="EPD2"` und
`epd2_model_{LONG,SHORT}.pkl`. Ein Retrain auf der NEUEN (post-P1.39) Feature-
Definition darf beides nicht erben — harte Regel 6 verlangt einen neuen Tag, und
der Dateiname muss mit ihm mitwandern, sonst legt die spätere Promotion das neue
Artefakt in den Loader-Slot der alten Generation (der EPD3-SHORT-Vorfall vom
2026-07-21, tools/promotion_guard.py).

Gepinnt wird:

  AK1  `artifact_slot` leitet den Dateinamen-Präfix aus dem Tag ab und stimmt mit
       `tools.promotion_guard.tag_prefix` überein (eine Konvention, zwei Module)
  AK2  Default `EPD2` lässt Tag, Dateinamen und Stats-Namen unverändert (No-op)
  AK3  ein neuer Tag setzt meta.model_id UND den Dateinamen GEMEINSAM
  AK4  die CLI weist ein `--model-id` an einer nicht verdrahteten Strategie ab und
       akzeptiert keinen Tag, der kein Modell-Tag ist
  AK5  der gewählte Tag EPD4 ist in allen Code-Registern frei (Regel 6)

Run: pytest backtest/test_retrain_model_id.py -v
     python backtest/test_retrain_model_id.py
"""

from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.shadow_gate as sg  # noqa: E402
import tools.bot_variants.index as variant_index  # noqa: E402
import tools.promotion_guard as pg  # noqa: E402
import tools.retrain_from_replay as R  # noqa: E402

NEW_TAG = "EPD4"


# ── AK1: eine Konvention, zwei Module ────────────────────────────────────────


@pytest.mark.parametrize("tag", ["EPD2", "EPD4", "RUB3", "ATS2", "MIS2-8H", "MIS2-168H"])
def test_artifact_slot_matches_the_promotion_guard(tag):
    assert R.artifact_slot(tag) == pg.tag_prefix(tag)


def test_artifact_slot_normalises_case_and_dashes():
    assert R.artifact_slot("  epd4 ") == "epd4"
    assert R.artifact_slot("MIS2-8H") == "mis28h"


def test_the_slot_of_the_new_tag_is_not_a_foreign_loader_slot():
    """Der eigentliche Zweck: `epd4_model_LONG.pkl` darf keinen Slot belegen, den
    eine andere Generation liest — sonst postet EIN Artefakt unter zwei Tags."""
    fname = f"{R.artifact_slot(NEW_TAG)}_model_LONG.pkl"
    assert pg.check_staging_filename(fname)[0] == pg.OK
    assert fname not in pg.slot_claims()


# ── AK2/AK3: Tag und Dateiname wandern gemeinsam ─────────────────────────────


class _Recorder:
    """Fängt ab, WOHIN run_epd schreibt und WELCHEN Tag es in die meta legt,
    ohne zu trainieren — das Training selbst prüfen die Retrain-Läufe."""

    def __init__(self):
        self.artifacts: list[str] = []
        self.metas: list[dict] = []

    def save_artifact(self, path, model, feature_cols, thresh, iso, meta):
        self.artifacts.append(os.path.basename(path))
        self.metas.append(dict(meta))


def _run_epd_recorded(tmp_path, model_id=None):
    """run_epd mit einem Mini-Datensatz und ohne echtes Training/IO."""
    rec = _Recorder()
    events = tmp_path / "events.jsonl"
    events.write_text("", encoding="utf-8")

    import pandas as pd

    # Gross genug für die Mindestmengen von run_epd (>=600 gesamt, >=300 je
    # Richtung) und lang genug, dass der 7d-Purge-Gap nicht-leere Slices lässt —
    # genau die Bedingung, an der der Post-Cut-Datensatz dieses Tasks scheitert.
    n = 2400
    df = pd.DataFrame(
        {
            "signal_time": pd.date_range("2026-01-01", periods=n, freq="3h"),
            "symbol": ["BTCUSDT"] * n,
            "direction": ["LONG" if i % 2 else "SHORT" for i in range(n)],
            "outcome": [i % 2 for i in range(n)],
            "net_pnl_pct": [0.1 if i % 2 else -0.1 for i in range(n)],
            **{c: [float(i % 7) for i in range(n)] for c in R.EPD2_FEATURES},
        }
    )

    def fake_train_binary(train, val, test, feature_cols, hyper=None, picker=None):
        return object(), object(), 0.5, {"deployable": True}, {"n_taken": 0}, []

    written: list[str] = []
    real_open = open

    def fake_open(path, *a, **kw):
        if isinstance(path, str) and path.endswith("_meta.json"):
            written.append(os.path.basename(path))
            path = os.path.join(str(tmp_path), os.path.basename(path))
        return real_open(path, *a, **kw)

    kwargs = {} if model_id is None else {"model_id": model_id}
    with (
        mock.patch.object(R, "load_replay", return_value=df),
        mock.patch.object(R, "train_binary", fake_train_binary),
        mock.patch.object(R, "save_artifact", rec.save_artifact),
        mock.patch.object(R, "top_importance", lambda *a, **kw: []),
        mock.patch("builtins.open", fake_open),
    ):
        result = R.run_epd(str(events), **kwargs)
    return rec, written, result


def test_default_keeps_the_epd2_slot_and_tag(tmp_path):
    rec, meta_files, result = _run_epd_recorded(tmp_path)
    assert rec.artifacts == ["epd2_model_LONG.pkl", "epd2_model_SHORT.pkl"]
    assert meta_files == ["epd2_model_LONG_meta.json", "epd2_model_SHORT_meta.json"]
    assert {m["model_id"] for m in rec.metas} == {"EPD2"}
    assert result["model_id"] == "EPD2"


def test_a_new_tag_moves_meta_and_filename_together(tmp_path):
    rec, meta_files, result = _run_epd_recorded(tmp_path, model_id=NEW_TAG)
    assert rec.artifacts == ["epd4_model_LONG.pkl", "epd4_model_SHORT.pkl"]
    assert meta_files == ["epd4_model_LONG_meta.json", "epd4_model_SHORT_meta.json"]
    assert {m["model_id"] for m in rec.metas} == {NEW_TAG}
    assert result["model_id"] == NEW_TAG
    # Der Feature-Vertrag bleibt derselbe — es ist eine neue GENERATION, kein
    # neues Modell-Design (harte Regel 7).
    assert result["features"] == list(R.EPD2_FEATURES)


# ── AK4: die CLI schluckt kein falsches Flag ─────────────────────────────────


def _main_with(argv):
    with mock.patch.object(sys, "argv", ["retrain_from_replay.py", *argv]):
        R.main()


def test_model_id_on_a_non_epd_strategy_is_refused():
    with pytest.raises(SystemExit) as e:
        _main_with(["--strategy", "rub", "--model-id", "RUB5"])
    assert "epd" in str(e.value)


def test_a_non_tag_model_id_is_refused():
    for bad in ("epd 4", "4EPD", "epd_4", ""):
        with pytest.raises(SystemExit) as e:
            _main_with(["--strategy", "epd", "--model-id", bad])
        assert "Modell-Tag" in str(e.value), bad


def test_lowercase_model_id_is_normalised_to_the_register_casing(tmp_path):
    """core/shadow_gate hält seine Keys UPPER — ein kleingeschriebener Tag in der
    meta liefe am Lifecycle-Lookup vorbei und postete als Default-LIVE."""
    rec, _, result = _run_epd_recorded(tmp_path, model_id="epd4")
    assert result["model_id"] == NEW_TAG
    assert {m["model_id"] for m in rec.metas} == {NEW_TAG}
    assert rec.artifacts == ["epd4_model_LONG.pkl", "epd4_model_SHORT.pkl"]


# ── AK5: EPD4 ist frei (harte Regel 6) ───────────────────────────────────────


def test_epd4_is_free_in_every_code_registry():
    """EPD1 (Pre-Rename), EPD2 (Legacy-Direktpost) und EPD3 (Retrain-Challenger,
    LONG live) sind vergeben. Kippt eine dieser Registrierungen, muss der Tag der
    nächsten EPD-Generation neu gewählt werden — deshalb hier gepinnt."""
    occupied = (
        set(variant_index.legacy_artifact_slots())
        | set(sg.SHADOW_ARTIFACTS)
        | {tag for tag, _ in sg._LIFECYCLE}
        | set(sg._RETIRED_TAGS)
    )
    epd_tags = {t for t in occupied if t.upper().startswith("EPD")}
    assert epd_tags == {"EPD1", "EPD2", "EPD3"}, f"EPD-Tag-Belegung hat sich geändert: {sorted(epd_tags)}"
    assert NEW_TAG not in occupied
    # Default LIVE ist der Sicherheitsvertrag des Gates: ein unregistrierter Tag
    # ist live, nicht shadow. Eine EPD4-Emission braucht daher VOR dem ersten
    # Post eine explizite Zeile in _LIFECYCLE.
    assert sg.leg_status(NEW_TAG, "LONG") == sg.LIVE


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
