# backtest/test_shadow_gate.py
"""DB-freie Tests für den fleet-weiten Shadow-Posting-Gate (T-2026-CU-9050-125).

Pinnt die zwei Sicherheits-Invarianten des Features:
  1. core.shadow_gate: DEFAULT-LIVE (nicht-gelistete Beine bleiben live), die
     SHADOW/RETIRED-Klassifikation, und das ROH-Scoring des Contract-Artefakts.
  2. core.signal_post.post_shadow_ai_signal: schreibt ai_signals ABER NIE
     telegram_outbox (monitored-but-unposted), loggt die Shadow-Prediction als
     posted=False, dedupt gegen offene Trades und committet NICHT (Regel 8).

Run: pytest backtest/test_shadow_gate.py -v   (oder standalone: python …)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# core.config verlangt Secrets; die Build-Maschine liefert ein leeres .env.
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pytest  # noqa: E402

import core.signal_post as _sp  # noqa: E402
from core import shadow_gate as sg  # noqa: E402
from core.signal_post import post_shadow_ai_signal  # noqa: E402


@pytest.fixture(autouse=True)
def _shadow_echo_off(monkeypatch):
    """T-150 gab post_shadow_ai_signal einen optionalen CH_SHADOW_TEST-Echo. Diese
    Tests pinnen die REINE „nur ai_signals, nie telegram_outbox"-Invariante — der
    Echo wird hier hart abgeschaltet, damit sie hermetisch bleiben, egal ob in der
    Umgebung/.env ein CH_SHADOW_TEST gesetzt ist (T-2026-CU-9050-164)."""
    monkeypatch.setattr(_sp, "_shadow_test_channel", lambda: 0)


# ── Fake-DB (identisches Muster wie test_shadow_prediction_cooldown) ──────────
class _Cur:
    def __init__(self, sink: list[str], fetch: object = None) -> None:
        self._sink = sink
        self._fetch = fetch

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._sink.append(" ".join(str(sql).split()))

    def fetchone(self):
        return self._fetch


class FakeConn:
    def __init__(self, fetch: object = None) -> None:
        self.statements: list[str] = []
        self.commits = 0
        self._fetch = fetch

    def cursor(self, *a, **kw):
        return _Cur(self.statements, self._fetch)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


class _FakeModel:
    def predict_proba(self, X):
        import numpy as np

        return np.array([[0.15, 0.85]])  # raw success prob = 0.85 (ndarray wie XGBClassifier)


def _artifact(threshold):
    return {
        "model": _FakeModel(),
        "features": ["a", "b", "c"],
        "optimal_threshold": threshold,
        "calibrator_isotonic": None,
    }


# ── 1. shadow_gate: Default-LIVE + Klassifikation ────────────────────────────
def test_default_is_live_for_unlisted_legs():
    # Kein bestehendes Live-Bein darf versehentlich geshadowt werden. (ATS1/ATB1
    # sind seit T-2026-CU-9050-127 bewusst SILENT — hier MAX1 als noch-live Beispiel.)
    assert sg.leg_status("MAX1", "SHORT") == sg.LIVE
    assert sg.leg_status("RUB2", "SHORT") == sg.LIVE
    assert sg.leg_status("SomeBrandNewTag", "LONG") == sg.LIVE
    assert sg.is_live("MAX1", "SHORT")
    assert not sg.is_shadow("MAX1", "SHORT")


def test_new_gen_candidates_are_shadow():
    for tag in ("ATS2", "ATB2"):
        for d in ("LONG", "SHORT"):
            assert sg.leg_status(tag, d) == sg.SHADOW
            assert sg.is_shadow(tag, d)
            assert not sg.is_live(tag, d)
    # SRA2 (T-2026-CU-9050-185): LONG live promotet (@0.6424 → Repo-Root), SHORT
    # bleibt shadow (kein deploybarer Edge, closed_trades3 tot seit 23.02).
    assert sg.leg_status("SRA2", "LONG") == sg.LIVE
    assert sg.leg_status("SRA2", "SHORT") == sg.SHADOW


def test_challenger_tags_are_shadow_and_dont_collide_with_live_leg():
    # RUB3/EPD3 sind die kollisionsfreien Challenger-Tags: der Shadow-Trade läuft
    # unter dem Challenger-Tag, das LIVE-Bein (RUB2/EPD2) bleibt LIVE — sonst würde
    # ein Shadow-Trade über den Active-Trade-Check einen Live-Post blockieren.
    assert sg.is_shadow("RUB3", "LONG")
    assert sg.is_shadow("EPD3", "LONG")
    # EPD3 SHORT (T-2026-CU-9050-185): live promotet (@0.6737 → Repo-Root),
    # koexistierend mit dem weiter-live EPD2.
    assert sg.leg_status("EPD3", "SHORT") == sg.LIVE
    # Die Live-Tags, unter denen die Bots real posten, dürfen NIE shadow sein.
    assert sg.leg_status("RUB2", "SHORT") == sg.LIVE
    assert sg.leg_status("RUB2", "LONG") == sg.LIVE  # Live-Legacy-LONG unter RUB2
    assert sg.leg_status("EPD2", "LONG") == sg.LIVE
    assert sg.leg_status("EPD2", "SHORT") == sg.LIVE
    assert sg.leg_status("SRA1", "LONG") == sg.LIVE


def test_retired_tags_classified_retired():
    assert sg.leg_status("MIS1", "LONG") == sg.RETIRED
    assert sg.leg_status("AIM1", "SHORT") == sg.RETIRED
    assert sg.is_retired("MSI1")  # typo-family alias
    assert not sg.is_retired("MIS2")


def test_silent_legs_are_neither_live_nor_shadow():
    # T-2026-CU-9050-127: ATS1/ATB1 stummgeschaltet (Bots 12/14 entparkt für
    # ATS2/ATB2-Shadow, aber die Alt-Modelle geben NICHTS aus). Der Bot fragt
    # is_live() am Ausgabe-Zweig -> False -> übersprungen; is_shadow() False ->
    # das Alt-Bein wird auch nicht shadow-emittiert.
    for tag in ("ATS1", "ATB1"):
        for d in ("LONG", "SHORT"):
            assert sg.leg_status(tag, d) == sg.SILENT
            assert sg.is_silent(tag, d)
            assert not sg.is_live(tag, d)
            assert not sg.is_shadow(tag, d)
    # Der Retrain daneben bleibt Shadow, andere Live-Beine bleiben live.
    assert sg.is_shadow("ATS2", "LONG") and sg.is_shadow("ATB2", "SHORT")
    assert sg.is_live("RUB2", "SHORT") and sg.is_live("SRA1", "LONG")


def test_leg_status_is_case_insensitive():
    assert sg.leg_status("ats2", "long") == sg.SHADOW
    assert sg.leg_status("  Ats2 ", " Short ") == sg.SHADOW


def test_artifact_threshold_reads_contract():
    assert sg.artifact_threshold(_artifact(0.7825)) == 0.7825
    assert sg.artifact_threshold(_artifact(None)) is None  # ATB2-Fall
    assert sg.artifact_threshold("not-a-dict") is None


def test_score_artifact_is_raw_proba_reindexed():
    # Feature-Reindex auf den Contract + rohe predict_proba[:,1].
    prob = sg.score_artifact(_artifact(0.5), {"c": 1.0, "a": 2.0, "b": 3.0, "extra": 9.0})
    assert abs(prob - 0.85) < 1e-9


# ── 2. post_shadow_ai_signal: monitored-but-unposted ─────────────────────────
def test_shadow_signal_writes_ai_signals_but_never_outbox():
    conn = FakeConn(fetch=None)  # kein offener Trade, dedup leer
    wrote = post_shadow_ai_signal(
        conn, "ATS2", "TESTUSDT", "LONG", 0.83, 100.0, 95.0, 90.0, [110.0, 120.0, 130.0], n_show=3
    )
    assert wrote is True
    joined = " || ".join(conn.statements)
    assert "INSERT INTO ai_signals" in joined, "shadow trade must be monitored via ai_signals"
    assert "telegram_outbox" not in joined, "shadow trade must NEVER reach a channel (no outbox row)"
    mpm = [s for s in conn.statements if "INSERT INTO ml_predictions_master" in s]
    assert len(mpm) == 1, "shadow prediction must also be logged (posted=False)"
    assert conn.commits == 0, "hard rule 8: caller commits, not the helper"


def test_shadow_signal_dedups_against_open_trade():
    conn = FakeConn(fetch=(1,))  # has_open_ai_signal -> True
    wrote = post_shadow_ai_signal(conn, "ATS2", "TESTUSDT", "LONG", 0.83, 100.0, 95.0, 90.0, [110.0, 120.0], n_show=3)
    assert wrote is False
    assert not any("INSERT INTO ai_signals" in s for s in conn.statements)


def test_shadow_signal_tracks_only_n_show_targets():
    conn = FakeConn(fetch=None)
    post_shadow_ai_signal(conn, "ATB2", "TESTUSDT", "SHORT", 0.5, 100.0, 100.0, 115.0, [90, 80, 70, 60, 50], n_show=3)
    # P2.31-Parität: der Monitor scored genau die veröffentlichten TPs.
    ins = [s for s in conn.statements if "INSERT INTO ai_signals" in s]
    assert len(ins) == 1


# ── 3. FMR2 (K4) Klasse-(A)-Shadow — Registry + committetes Artefakt ──────────
def test_fmr2_leg_is_shadow_both_directions():
    # Der FMR2-Retrain läuft SHADOW neben dem live FMR1-Bein; FMR1 selbst bleibt
    # unter eigenem Tag unangetastet (Default-LIVE, keine Registry-Zeile).
    for d in ("LONG", "SHORT"):
        assert sg.leg_status("FMR2", d) == sg.SHADOW
        assert sg.is_shadow("FMR2", d)
        assert not sg.is_live("FMR2", d)
    assert sg.leg_status("FMR1", "SHORT") == sg.LIVE
    assert sg.leg_status("FMR1", "LONG") == sg.LIVE


def test_fmr2_maps_one_binary_model_to_both_directions():
    # side_short ist ein Feature → EIN Modell bedient beide Richtungen; beide
    # Richtungen müssen auf dieselbe Staging-Datei zeigen.
    p_long = sg.shadow_artifact_path("FMR2", "LONG")
    p_short = sg.shadow_artifact_path("FMR2", "SHORT")
    assert p_long == p_short
    assert p_long is not None and p_long.endswith("fmr2_model.pkl")


def test_promoted_live_leg_loads_from_root_shadow_from_staging():
    # T-2026-CU-9050-185: das promotete LIVE-Bein lädt sein Artefakt aus dem
    # Repo-Root (Regel 2 = live), das verbliebene SHADOW-Bein weiter aus staging.
    assert sg.shadow_artifact_path("SRA2", "LONG") == "sra2_model_LONG.json"
    assert sg.shadow_artifact_path("SRA2", "SHORT").startswith(sg.STAGING_DIR)
    assert sg.shadow_artifact_path("EPD3", "SHORT") == "epd2_model_SHORT.pkl"
    assert sg.shadow_artifact_path("EPD3", "LONG").startswith(sg.STAGING_DIR)


def test_fmr2_staging_artifact_loads_scores_and_gates():
    # Validiert das Artefakt end-to-end, WENN es vorliegt (der eigentliche Zweck
    # des Shadow-Bots): ladbar, 15-Feature-Vertrag == FMR1_FEATURES, gültiger
    # Operating-Threshold, rohe predict_proba in [0, 1]. Das reale pkl liegt —
    # wie bei ATS2/ATB2/RUB3/EPD3 — NICHT im Git, sondern in staging_models/ auf
    # dem VPS (Platzierung = Operator-Schritt, harte Regel 2); fehlt es (oder
    # joblib/xgboost auf schlanker CI), wird der Realteil übersprungen. Die
    # Registry-Tests oben sichern die Verdrahtung dependency- und artefaktfrei ab.
    import pytest

    pytest.importorskip("joblib")
    pytest.importorskip("xgboost")

    from core.research_features import FMR1_FEATURES

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    orig = sg.STAGING_DIR
    sg.STAGING_DIR = os.path.join(repo_root, "staging_models")
    try:
        art = sg.load_shadow_artifact("FMR2", "SHORT")
    finally:
        sg.STAGING_DIR = orig
    if art is None:
        pytest.skip("staging_models/fmr2_model.pkl nicht vorhanden (VPS-Operator-Schritt)")
    assert list(art["features"]) == list(FMR1_FEATURES)  # exakter Feature-Vertrag
    thr = sg.artifact_threshold(art)
    assert thr is not None and 0.0 < thr < 1.0  # FMR2 hat einen validen Operating-Point

    row = dict.fromkeys(FMR1_FEATURES, 0.0)
    prob = sg.score_artifact(art, row)
    assert 0.0 <= prob <= 1.0


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
