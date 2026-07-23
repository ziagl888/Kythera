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
from core.signal_post import (  # noqa: E402
    LEG_LIVE,
    LEG_SHADOW,
    LEG_SKIP,
    post_shadow_ai_signal,
    route_legacy_leg,
)


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


# ── 1. shadow_gate: Default-LIVE + Klassifikation (Stand nach T-2026-KYT-9050-033) ──
def test_default_is_live_for_unlisted_legs():
    # Kein nicht-registriertes Bein darf versehentlich geshadowt werden. Referenz-Tags,
    # die die T-032-Reconfig (T-2026-KYT-9050-033) bewusst NICHT anfasst (Audit KEEP):
    # MAX1, TD_4H, ROM1 — alle bleiben default-LIVE.
    assert sg.leg_status("MAX1", "SHORT") == sg.LIVE
    assert sg.leg_status("TD_4H", "LONG") == sg.LIVE
    assert sg.leg_status("ROM1", "SHORT") == sg.LIVE
    assert sg.leg_status("SomeBrandNewTag", "LONG") == sg.LIVE
    assert sg.is_live("MAX1", "SHORT")
    assert not sg.is_shadow("MAX1", "SHORT")


def test_new_gen_candidates_and_t033_promotions():
    # ATB2 bleibt Klasse-(A)-Shadow (threshold=null → braucht weiter Daten).
    for d in ("LONG", "SHORT"):
        assert sg.leg_status("ATB2", d) == sg.SHADOW
        assert sg.is_shadow("ATB2", d)
        assert not sg.is_live("ATB2", d)
    # T-2026-KYT-9050-033 (Audit T-032): ATS2 SHADOW→LIVE promotet (beide Beine);
    # SRA2 LONG war schon live (T-185), SHORT jetzt ebenfalls LIVE promotet.
    assert sg.leg_status("ATS2", "LONG") == sg.LIVE
    assert sg.leg_status("ATS2", "SHORT") == sg.LIVE
    assert sg.leg_status("SRA2", "LONG") == sg.LIVE
    assert sg.leg_status("SRA2", "SHORT") == sg.LIVE


def test_t033_parked_legs_are_shadow():
    # Fleet-Reconfig T-032 (T-2026-KYT-9050-033): die realisiert blutenden Legacy-Live-
    # Beine sind geparkt → SHADOW (Cornix aus, monitored an). RUB3/EPD3-LONG-Challenger
    # bleiben unverändert Shadow.
    assert sg.is_shadow("RUB3", "LONG")
    assert sg.is_shadow("EPD3", "LONG")
    assert sg.leg_status("EPD3", "SHORT") == sg.SHADOW  # war live (T-185), T-033 geparkt
    # Ganz →SHADOW (beide Beine).
    for tag in ("EPD2", "RUB2", "SRA1", "ABR2", "BB2_4H", "BR1D", "MIS2-8H"):
        assert sg.is_shadow(tag, "LONG"), tag
        assert sg.is_shadow(tag, "SHORT"), tag
    # BR1Hv2 = der aktuelle 1h-BR-Tag (Bot 7, gemischt-case) — case-insensitiv geparkt.
    assert sg.leg_status("BR1Hv2", "LONG") == sg.SHADOW
    assert sg.leg_status("BR1Hv2", "SHORT") == sg.SHADOW


def test_t033_per_direction_parks_keep_the_other_leg_live():
    # Park SHORT →SHADOW, LONG bleibt LIVE (BR/BB/QM Pattern-Bots).
    for tag in ("BR2H", "BR4H", "BB_1H", "BB_4H", "QM_1H"):
        assert sg.leg_status(tag, "SHORT") == sg.SHADOW, tag
        assert sg.leg_status(tag, "LONG") == sg.LIVE, tag
    # Park LONG →SHADOW, SHORT bleibt LIVE (MIS2-Pump-Seite; SHORT/Dump realisiert besser).
    for tag in ("MIS2-24H", "MIS2-72H", "MIS2-168H"):
        assert sg.leg_status(tag, "LONG") == sg.SHADOW, tag
        assert sg.leg_status(tag, "SHORT") == sg.LIVE, tag


def test_t033_fif1_revived_as_shadow():
    # FIF1 war SILENT (T-183, von TSM1 abgelöst); T-033 revived als SHADOW (monitored).
    for d in ("LONG", "SHORT"):
        assert sg.leg_status("FIF1", d) == sg.SHADOW
        assert sg.is_shadow("FIF1", d)
        assert not sg.is_live("FIF1", d)
    # TSM1 (Live-Nachfolger auf CH_FIF1) bleibt unangetastet live.
    assert sg.leg_status("TSM1", "SHORT") == sg.LIVE


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
    # Der ATB2-Retrain daneben bleibt Shadow; noch-live Referenz-Beine bleiben live.
    # (ATS2 ist seit T-033 LIVE promotet; SRA1/RUB2 sind seit T-033 geparkt → hier
    # MAX1/TD_4H als stabile KEEP-Live-Referenzen.)
    assert sg.is_shadow("ATB2", "LONG") and sg.is_shadow("ATB2", "SHORT")
    assert sg.is_live("MAX1", "SHORT") and sg.is_live("TD_4H", "LONG")


def test_leg_status_is_case_insensitive():
    # ATB2 bleibt Shadow (ATS2 ist seit T-033 LIVE) — case-/whitespace-insensitiv.
    assert sg.leg_status("atb2", "long") == sg.SHADOW
    assert sg.leg_status("  Atb2 ", " Short ") == sg.SHADOW
    # gemischt-case Legacy-Tag BR1Hv2 (Bot 7) normalisiert auf den geparkten Key.
    assert sg.leg_status("br1hv2", "long") == sg.SHADOW


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
    # Ein LIVE-Bein lädt sein Artefakt aus dem Repo-Root (Regel 2 = live), ein
    # SHADOW-Bein weiter aus staging. Stand nach T-2026-KYT-9050-033:
    #   * SRA2 LONG+SHORT jetzt LIVE (T-033) → Root (Artefakt-Move = Operator-Vorbedingung).
    #   * ATS2 LONG+SHORT jetzt LIVE (T-033) → Root (dito).
    #   * EPD3 SHORT jetzt SHADOW (T-033-Park) + EPD3 LONG SHADOW → staging.
    assert sg.shadow_artifact_path("SRA2", "LONG") == "sra2_model_LONG.json"
    assert sg.shadow_artifact_path("SRA2", "SHORT") == "sra2_model_SHORT.json"
    assert sg.shadow_artifact_path("ATS2", "LONG") == "ats2_model_LONG.pkl"
    assert sg.shadow_artifact_path("ATS2", "SHORT") == "ats2_model_SHORT.pkl"
    assert sg.shadow_artifact_path("EPD3", "SHORT").startswith(sg.STAGING_DIR)
    assert sg.shadow_artifact_path("EPD3", "LONG").startswith(sg.STAGING_DIR)


def test_challenger_filename_never_aliases_legacy_loader():
    # Review T-2026-CU-9050-185 (CRITICAL): das EPD3-SHORT-Artefakt muss vom
    # EPD2-Legacy-SHORT-Slot (epd2_model_SHORT.pkl) verschieden bleiben — egal ob
    # live (Root) oder, nach dem T-033-Park, shadow (staging) —, sonst lädt der
    # Legacy-Live-Loader dieselbe Datei und postet SHORT doppelt (Regel 4).
    p = sg.shadow_artifact_path("EPD3", "SHORT")
    assert os.path.basename(p) == "epd3_model_SHORT.pkl"
    assert os.path.basename(p) != "epd2_model_SHORT.pkl"  # Bot 10 EPD2_ARTIFACT_PATHS["SHORT"]
    assert p.startswith(sg.STAGING_DIR)  # SHADOW nach T-033


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


# ── 4. route_legacy_leg (T-2026-KYT-9050-033): Legacy-Direktpost-Router ───────
# Der Helper entscheidet an der Emissions-Stelle eines Legacy-Bots (BR/BB/QM/SRA1/
# RUB2/EPD2/ABR2/MIS2), OB der Bot live posten darf — anders als post_ai_signal_gated
# baut er KEINE Message. Default LIVE ⇒ der Aufrufer postet selbst; SHADOW ⇒ hier ein
# monitored ai_signals-Trade (nie telegram_outbox); SILENT/RETIRED/Kill-Switch ⇒ skip.
def test_route_legacy_leg_live_leaves_write_to_caller():
    conn = FakeConn(fetch=None)
    # RUB1 = KEEP-Live-Tag (nicht registriert) → LIVE, der Helper schreibt NICHTS.
    r = route_legacy_leg(conn, "RUB1", "SHORT", "X", 0.8, 100.0, 95.0, 90.0, [110.0, 120.0], n_show=2)
    assert r == LEG_LIVE
    assert conn.statements == []  # keine Zeile — der Aufrufer postet selbst
    assert conn.commits == 0


def test_route_legacy_leg_shadow_writes_monitored_never_outbox():
    conn = FakeConn(fetch=None)  # kein offener Trade
    # RUB2 SHORT ist seit T-033 geparkt → SHADOW.
    r = route_legacy_leg(conn, "RUB2", "SHORT", "X", 0.8, 100.0, 95.0, 90.0, [110.0, 120.0, 130.0], n_show=3)
    assert r == LEG_SHADOW
    joined = " || ".join(conn.statements)
    assert "INSERT INTO ai_signals" in joined
    assert "telegram_outbox" not in joined  # niemals Cornix (Regel 4)
    assert conn.commits == 0  # Regel 8: der Aufrufer committet, nicht der Helper


def test_route_legacy_leg_shadow_dedups_to_skip():
    conn = FakeConn(fetch=(1,))  # has_open_ai_signal -> True (offener Shadow-Trade)
    r = route_legacy_leg(conn, "RUB2", "SHORT", "X", 0.8, 100.0, 95.0, 90.0, [110.0], n_show=1)
    assert r == LEG_SKIP
    assert not any("INSERT INTO ai_signals" in s for s in conn.statements)


def test_route_legacy_leg_silent_and_retired_skip():
    conn = FakeConn(fetch=None)
    # SILENT (ATS1) und RETIRED (AIM1) → SKIP, gar nichts geschrieben.
    assert route_legacy_leg(conn, "ATS1", "LONG", "X", 0.8, 100.0, 95.0, 90.0, [110.0]) == LEG_SKIP
    assert route_legacy_leg(conn, "AIM1", "SHORT", "X", 0.8, 100.0, 95.0, 90.0, [110.0]) == LEG_SKIP
    assert conn.statements == []


def test_route_legacy_leg_respects_master_kill_switch():
    conn = FakeConn(fetch=None)
    prev = os.environ.get("KYTHERA_SHADOW_POSTING")
    os.environ["KYTHERA_SHADOW_POSTING"] = "0"  # Master-Switch aus
    try:
        # SHADOW-Bein, aber Shadow-Posting global aus → SKIP, nichts geschrieben.
        r = route_legacy_leg(conn, "RUB2", "SHORT", "X", 0.8, 100.0, 95.0, 90.0, [110.0])
    finally:
        if prev is None:
            os.environ.pop("KYTHERA_SHADOW_POSTING", None)
        else:
            os.environ["KYTHERA_SHADOW_POSTING"] = prev
    assert r == LEG_SKIP
    assert conn.statements == []


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
