# backtest/test_shadow_gate.py
"""DB-free tests for the fleet-wide shadow-posting gate (T-2026-CU-9050-125).

Pins the two security invariants of the feature:
  1. core.shadow_gate: DEFAULT-LIVE (unlisted legs remain live), the
     SHADOW/RETIRED classification, and raw scoring of the contract artifact.
  2. core.signal_post.post_shadow_ai_signal: writes ai_signals BUT NEVER
     telegram_outbox (monitored-but-unposted), logs the shadow prediction as
     posted=False, dedups against open trades and does NOT commit (rule 8).

Run: pytest backtest/test_shadow_gate.py -v   (or standalone: python …)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# core.config requires secrets; the build machine provides an empty .env.
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
    """T-150 gave post_shadow_ai_signal an optional CH_SHADOW_TEST echo. These
    tests pin the pure "ai_signals only, never telegram_outbox" invariant — the
    echo is hard disabled here to keep them hermetic, regardless of whether
    CH_SHADOW_TEST is set in the environment/.env (T-2026-CU-9050-164)."""
    monkeypatch.setattr(_sp, "_shadow_test_channel", lambda: 0)


# ── Fake-DB (same pattern as test_shadow_prediction_cooldown) ──────────
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

        return np.array([[0.15, 0.85]])  # raw success prob = 0.85 (ndarray like XGBClassifier)


def _artifact(threshold):
    return {
        "model": _FakeModel(),
        "features": ["a", "b", "c"],
        "optimal_threshold": threshold,
        "calibrator_isotonic": None,
    }


# ── 1. shadow_gate: Default-LIVE + classification (state after T-2026-KYT-9050-033) ──
def test_default_is_live_for_unlisted_legs():
    # No unregistered leg must be accidentally shadowed. Reference tags that
    # the T-032 reconfig (T-2026-KYT-9050-033) deliberately does NOT touch (audit KEEP):
    # MAX1, TD_4H, ROM1 — all remain default-LIVE.
    assert sg.leg_status("MAX1", "SHORT") == sg.LIVE
    assert sg.leg_status("TD_4H", "LONG") == sg.LIVE
    assert sg.leg_status("ROM1", "SHORT") == sg.LIVE
    assert sg.leg_status("SomeBrandNewTag", "LONG") == sg.LIVE
    assert sg.is_live("MAX1", "SHORT")
    assert not sg.is_shadow("MAX1", "SHORT")


def test_new_gen_candidates_and_t033_promotions():
    # ATB2 SHORT remains class-(A) shadow (threshold=null → still needs data);
    # ATB2 LONG is LIVE promoted per T-2026-KYT-9050-037 (operator, see
    # test_t037_epd3_long_atb2_long_deployed).
    assert sg.leg_status("ATB2", "SHORT") == sg.SHADOW
    assert sg.is_shadow("ATB2", "SHORT")
    assert not sg.is_live("ATB2", "SHORT")
    # T-2026-KYT-9050-033 (audit T-032): ATS2 promoted SHADOW→LIVE (both directions);
    # SRA2 LONG was already live (T-185), SHORT now also LIVE promoted.
    assert sg.leg_status("ATS2", "LONG") == sg.LIVE
    assert sg.leg_status("ATS2", "SHORT") == sg.LIVE
    assert sg.leg_status("SRA2", "LONG") == sg.LIVE
    assert sg.leg_status("SRA2", "SHORT") == sg.LIVE


def test_t033_parked_legs_are_shadow():
    # Fleet reconfig T-032 (T-2026-KYT-9050-033): the bleeding legacy-live
    # legs are parked → SHADOW (Cornix off, monitored on). RUB3 LONG challenger remains
    # shadow. EPD3 SHORT remains parked; EPD3 LONG is LIVE promoted per T-037 (operator,
    # see test_t037_epd3_long_atb2_long_deployed).
    assert sg.is_shadow("RUB3", "LONG")
    assert sg.leg_status("EPD3", "SHORT") == sg.SHADOW  # was live (T-185), parked T-033
    # Completely → SHADOW (both directions).
    for tag in ("EPD2", "RUB2", "SRA1", "ABR2", "BB2_4H", "BR1D", "MIS2-8H"):
        assert sg.is_shadow(tag, "LONG"), tag
        assert sg.is_shadow(tag, "SHORT"), tag
    # BR1Hv2 = the current 1h BR tag (bot 7, mixed-case) — parked case-insensitively.
    assert sg.leg_status("BR1Hv2", "LONG") == sg.SHADOW
    assert sg.leg_status("BR1Hv2", "SHORT") == sg.SHADOW


def test_t033_per_direction_parks_keep_the_other_leg_live():
    # Park SHORT → SHADOW, LONG stays LIVE (BR/BB/QM pattern bots).
    for tag in ("BR2H", "BR4H", "BB_1H", "BB_4H", "QM_1H"):
        assert sg.leg_status(tag, "SHORT") == sg.SHADOW, tag
        assert sg.leg_status(tag, "LONG") == sg.LIVE, tag
    # Park LONG → SHADOW, SHORT stays LIVE (MIS2 pump side; SHORT/dump performs better).
    for tag in ("MIS2-24H", "MIS2-72H", "MIS2-168H"):
        assert sg.leg_status(tag, "LONG") == sg.SHADOW, tag
        assert sg.leg_status(tag, "SHORT") == sg.LIVE, tag


def test_t033_fif1_revived_as_shadow():
    # FIF1 was SILENT (T-183, superseded by TSM1); T-033 revived as SHADOW (monitored).
    for d in ("LONG", "SHORT"):
        assert sg.leg_status("FIF1", d) == sg.SHADOW
        assert sg.is_shadow("FIF1", d)
        assert not sg.is_live("FIF1", d)
    # TSM1 (live successor on CH_FIF1) remains untouched live.
    assert sg.leg_status("TSM1", "SHORT") == sg.LIVE


def test_retired_tags_classified_retired():
    # T-2026-KYT-9050-034: MIS1 is NOT retired anymore (revive, bot 11 loads the
    # pump_model_*_final.pkl again — operator decision Michi, audit T-032). The
    # bare "MIS1" classification is thus LIVE-default; the actual lifecycle per
    # (tag, direction) is tested in test_mis1_revive_lifecycle. AIM1 remains retired.
    assert not sg.is_retired("MIS1")
    assert sg.leg_status("AIM1", "SHORT") == sg.RETIRED
    assert sg.is_retired("MSI1")  # typo-family alias remains retired
    assert not sg.is_retired("MIS2")


def test_t037_rub1_revive_and_rub3_short_park():
    # T-2026-KYT-9050-037 (Michi bot_results.xlsx): bot 13 reverted to the original
    # RUB1 legacy models, both directions LIVE under tag RUB1 (explicitly registered,
    # defense-in-depth). The benched RUB2 generation remains SHADOW. RUB3-SHORT parked.
    assert sg.leg_status("RUB1", "LONG") == sg.LIVE
    assert sg.leg_status("RUB1", "SHORT") == sg.LIVE
    assert sg.is_live("RUB1", "LONG") and sg.is_live("RUB1", "SHORT")
    assert sg.leg_status("RUB2", "LONG") == sg.SHADOW
    assert sg.leg_status("RUB2", "SHORT") == sg.SHADOW
    # RUB3 now both directions SHADOW (LONG challenger + SHORT park).
    assert sg.leg_status("RUB3", "LONG") == sg.SHADOW
    assert sg.leg_status("RUB3", "SHORT") == sg.SHADOW
    # Spec §5 register-assert (exact expected vector).
    assert (
        sg.leg_status("RUB1", "LONG"),
        sg.leg_status("RUB1", "SHORT"),
        sg.leg_status("RUB2", "LONG"),
        sg.leg_status("RUB3", "SHORT"),
    ) == (sg.LIVE, sg.LIVE, sg.SHADOW, sg.SHADOW)


def test_t037_retires_aim2_topn_and_ats1_robust():
    # T-2026-KYT-9050-037: AIM2-TOPN ("too thin") + ATS1_Robust ("synthetic only")
    # RETIRE both directions (register/report classification; DB delete is a
    # separate operator step). Case-insensitive (is_retired normalizes).
    for tag in ("AIM2-TOPN", "aim2-topn", "ATS1_Robust", "ats1_robust"):
        assert sg.is_retired(tag), tag
        for d in ("LONG", "SHORT"):
            assert sg.leg_status(tag, d) == sg.RETIRED, (tag, d)
    # The live base AIM2 generation must NOT be retired together (prefix boundary).
    assert not sg.is_retired("AIM2")
    assert sg.leg_status("AIM2", "LONG") == sg.LIVE
    # ATS1 remains SILENT (T-127), not RETIRED — different tag than ATS1_Robust.
    assert sg.leg_status("ATS1", "LONG") == sg.SILENT
    assert not sg.is_retired("ATS1")


def test_t037_epd3_long_atb2_long_deployed():
    # T-2026-KYT-9050-037 (operator decision Michi, bot_results.xlsx #3/#4): EPD3-LONG
    # + ATB2-LONG promoted SHADOW→LIVE (deploy "per requirement" despite shadow no-edge
    # for EPD3-LONG and n=17 for ATB2-LONG — threshold cap 0.76 / blind 0.60). The
    # other direction each remains SHADOW.
    assert sg.leg_status("EPD3", "LONG") == sg.LIVE
    assert sg.leg_status("EPD3", "SHORT") == sg.SHADOW
    assert sg.leg_status("ATB2", "LONG") == sg.LIVE
    assert sg.leg_status("ATB2", "SHORT") == sg.SHADOW
    # LIVE legs load from repo root under challenger-distinct filenames —
    # epd3_model_LONG.pkl must never be the legacy EPD2-LONG slot (epd2_model_LONG.pkl),
    # otherwise the promotion hijacks the EPD2 live loader → double-post (rule 4).
    assert sg.shadow_artifact_path("EPD3", "LONG") == "epd3_model_LONG.pkl"
    assert sg.shadow_artifact_path("EPD3", "LONG") != "epd2_model_LONG.pkl"
    assert sg.shadow_artifact_path("ATB2", "LONG") == "atb2_model_LONG.pkl"
    # ATB2-SHORT remains shadow from staging (not aliasing the LONG root file).
    assert sg.shadow_artifact_path("ATB2", "SHORT").startswith(sg.STAGING_DIR)


def test_t037_deploy_staging_artifacts_carry_operator_thresholds():
    # Validates operator-set thresholds in the staged artifact, IF it
    # exists (VPS operator step, hard rule 2; if joblib/pkl missing on lean
    # CI, the real part is skipped — the registry tests above secure the
    # wiring dependency-free).
    import pytest

    pytest.importorskip("joblib")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    orig = sg.STAGING_DIR
    sg.STAGING_DIR = os.path.join(repo_root, "staging_models")
    try:
        # EPD3-LONG loads live from root; for the threshold check load the staged pkl directly.
        import joblib

        for fname, want in (("epd3_model_LONG.pkl", 0.76), ("atb2_model_LONG.pkl", 0.60)):
            path = os.path.join(sg.STAGING_DIR, fname)
            if not os.path.exists(path):
                pytest.skip(f"{fname} not present (VPS operator step)")
            art = joblib.load(path)
            assert art.get("optimal_threshold") == want, (fname, art.get("optimal_threshold"))
            assert "threshold_provenance" in (art.get("meta") or {}), f"{fname} without provenance note"
    finally:
        sg.STAGING_DIR = orig


def test_mis1_revive_lifecycle():
    # T-2026-KYT-9050-034: MIS1 revive — the GOOD legs (audit T-032) are
    # default-LIVE and revive the MIS2 legs parked by T-033; the weak
    # MIS1 legs are SHADOW. Per (horizon, direction) exactly ONE live generation.
    assert sg.is_live("MIS1-8H", "SHORT")  # good (dump side 8h)
    assert sg.is_live("MIS1-24H", "LONG")
    assert sg.is_live("MIS1-72H", "LONG")
    assert sg.is_live("MIS1-168H", "LONG")
    # weak legs parked
    assert sg.is_shadow("MIS1-8H", "LONG")
    assert sg.is_shadow("MIS1-24H", "SHORT")
    assert sg.is_shadow("MIS1-72H", "SHORT")
    assert sg.is_shadow("MIS1-168H", "SHORT")
    # No Cornix double-post: where MIS1 is live, the MIS2 counterpart is parked.
    assert sg.is_live("MIS1-24H", "LONG") and sg.is_shadow("MIS2-24H", "LONG")
    assert sg.is_live("MIS1-8H", "SHORT") and sg.is_shadow("MIS2-8H", "SHORT")
    # Conversely MIS2-SHORT (24/72/168) remains live, MIS1-SHORT parked there.
    assert sg.is_live("MIS2-24H", "SHORT") and sg.is_shadow("MIS1-24H", "SHORT")
    # case-insensitive (leg_status normalizes).
    assert sg.leg_status("mis1-8h", "short") == sg.LIVE


def test_silent_legs_are_neither_live_nor_shadow():
    # T-2026-CU-9050-127: ATS1/ATB1 silenced (bots 12/14 unparked for
    # ATS2/ATB2 shadow, but the old models produce NOTHING). The bot checks
    # is_live() at the output branch -> False -> skipped; is_shadow() False ->
    # the old leg is not shadow-emitted either.
    for tag in ("ATS1", "ATB1"):
        for d in ("LONG", "SHORT"):
            assert sg.leg_status(tag, d) == sg.SILENT
            assert sg.is_silent(tag, d)
            assert not sg.is_live(tag, d)
            assert not sg.is_shadow(tag, d)
    # The ATB2-SHORT retrain alongside remains shadow (ATB2 LONG is live per T-037);
    # still-live reference legs remain live (MAX1/TD_4H as stable KEEP live references).
    assert sg.is_shadow("ATB2", "SHORT")
    assert sg.is_live("MAX1", "SHORT") and sg.is_live("TD_4H", "LONG")


def test_leg_status_is_case_insensitive():
    # ATB2 SHORT remains shadow (LONG is live per T-037) — case-/whitespace-insensitive.
    assert sg.leg_status("atb2", "short") == sg.SHADOW
    assert sg.leg_status("  Atb2 ", " Short ") == sg.SHADOW
    # mixed-case legacy tag BR1Hv2 (bot 7) normalizes to the parked key.
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
    conn = FakeConn(fetch=None)  # no open trade, dedup empty
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
    # P2.31 parity: the monitor scores exactly the published TPs.
    ins = [s for s in conn.statements if "INSERT INTO ai_signals" in s]
    assert len(ins) == 1


# ── 3. FMR2 (K4) class-(A) shadow — registry + committed artifact ──────────
def test_fmr2_leg_is_shadow_both_directions():
    # The FMR2 retrain runs SHADOW alongside the live FMR1 leg; FMR1 itself remains
    # untouched under its own tag (default-LIVE, no registry row).
    for d in ("LONG", "SHORT"):
        assert sg.leg_status("FMR2", d) == sg.SHADOW
        assert sg.is_shadow("FMR2", d)
        assert not sg.is_live("FMR2", d)
    assert sg.leg_status("FMR1", "SHORT") == sg.LIVE
    assert sg.leg_status("FMR1", "LONG") == sg.LIVE


def test_fmr2_maps_one_binary_model_to_both_directions():
    # side_short is a feature → ONE model serves both directions; both
    # directions must point to the same staging file.
    p_long = sg.shadow_artifact_path("FMR2", "LONG")
    p_short = sg.shadow_artifact_path("FMR2", "SHORT")
    assert p_long == p_short
    assert p_long is not None and p_long.endswith("fmr2_model.pkl")


def test_promoted_live_leg_loads_from_root_shadow_from_staging():
    # A LIVE leg loads its artifact from repo root (rule 2 = live), a
    # SHADOW leg continues from staging. State after T-2026-KYT-9050-037:
    #   * SRA2 LONG+SHORT + ATS2 LONG+SHORT LIVE (T-033) → root.
    #   * EPD3 LONG now LIVE (T-037, operator) → root (bare filename);
    #     EPD3 SHORT remains SHADOW (T-033 park) → staging.
    assert sg.shadow_artifact_path("SRA2", "LONG") == "sra2_model_LONG.json"
    assert sg.shadow_artifact_path("SRA2", "SHORT") == "sra2_model_SHORT.json"
    assert sg.shadow_artifact_path("ATS2", "LONG") == "ats2_model_LONG.pkl"
    assert sg.shadow_artifact_path("ATS2", "SHORT") == "ats2_model_SHORT.pkl"
    assert sg.shadow_artifact_path("EPD3", "LONG") == "epd3_model_LONG.pkl"  # LIVE → root
    assert sg.shadow_artifact_path("EPD3", "SHORT").startswith(sg.STAGING_DIR)  # SHADOW → staging


def test_challenger_filename_never_aliases_legacy_loader():
    # Review T-2026-CU-9050-185 (CRITICAL): the EPD3-SHORT artifact must remain
    # distinct from the EPD2 legacy-SHORT slot (epd2_model_SHORT.pkl) — whether
    # live (root) or, after the T-033 park, shadow (staging) — otherwise the
    # legacy live loader loads the same file and posts SHORT double (rule 4).
    p = sg.shadow_artifact_path("EPD3", "SHORT")
    assert os.path.basename(p) == "epd3_model_SHORT.pkl"
    assert os.path.basename(p) != "epd2_model_SHORT.pkl"  # Bot 10 EPD2_ARTIFACT_PATHS["SHORT"]
    assert p.startswith(sg.STAGING_DIR)  # SHADOW after T-033


def test_fmr2_staging_artifact_loads_scores_and_gates():
    # Validates the artifact end-to-end, IF it exists (the actual purpose
    # of the shadow bot): loadable, 15-feature contract == FMR1_FEATURES, valid
    # operating threshold, raw predict_proba in [0, 1]. The real pkl lives —
    # like ATS2/ATB2/RUB3/EPD3 — NOT in git, but in staging_models/ on
    # the VPS (placement = operator step, hard rule 2); if missing (or
    # joblib/xgboost on lean CI), the real part is skipped. The
    # registry tests above secure the wiring dependency- and artifact-free.
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
        pytest.skip("staging_models/fmr2_model.pkl not present (VPS operator step)")
    assert list(art["features"]) == list(FMR1_FEATURES)  # exact feature contract
    thr = sg.artifact_threshold(art)
    assert thr is not None and 0.0 < thr < 1.0  # FMR2 has a valid operating point

    row = dict.fromkeys(FMR1_FEATURES, 0.0)
    prob = sg.score_artifact(art, row)
    assert 0.0 <= prob <= 1.0


# ── 4. route_legacy_leg (T-2026-KYT-9050-033): Legacy direct-post router ───────
# The helper decides at the emission point of a legacy bot (BR/BB/QM/SRA1/
# RUB2/EPD2/ABR2/MIS2) WHETHER the bot may post live — unlike post_ai_signal_gated
# it builds NO message. Default LIVE ⇒ the caller posts itself; SHADOW ⇒ here a
# monitored ai_signals trade (never telegram_outbox); SILENT/RETIRED/kill-switch ⇒ skip.
def test_route_legacy_leg_live_leaves_write_to_caller():
    conn = FakeConn(fetch=None)
    # RUB1 = revived original legacy generation, since T-037 explicitly LIVE registered
    # (defense-in-depth) → LIVE, helper writes NOTHING (bot posts itself).
    r = route_legacy_leg(conn, "RUB1", "SHORT", "X", 0.8, 100.0, 95.0, 90.0, [110.0, 120.0], n_show=2)
    assert r == LEG_LIVE
    assert conn.statements == []  # no row — the caller posts itself
    assert conn.commits == 0


def test_route_legacy_leg_shadow_writes_monitored_never_outbox():
    conn = FakeConn(fetch=None)  # no open trade
    # RUB2 SHORT has been parked since T-033 → SHADOW.
    r = route_legacy_leg(conn, "RUB2", "SHORT", "X", 0.8, 100.0, 95.0, 90.0, [110.0, 120.0, 130.0], n_show=3)
    assert r == LEG_SHADOW
    joined = " || ".join(conn.statements)
    assert "INSERT INTO ai_signals" in joined
    assert "telegram_outbox" not in joined  # never Cornix (rule 4)
    assert conn.commits == 0  # rule 8: caller commits, not helper


def test_route_legacy_leg_shadow_dedups_to_skip():
    conn = FakeConn(fetch=(1,))  # has_open_ai_signal -> True (open shadow trade)
    r = route_legacy_leg(conn, "RUB2", "SHORT", "X", 0.8, 100.0, 95.0, 90.0, [110.0], n_show=1)
    assert r == LEG_SKIP
    assert not any("INSERT INTO ai_signals" in s for s in conn.statements)


def test_route_legacy_leg_silent_and_retired_skip():
    conn = FakeConn(fetch=None)
    # SILENT (ATS1) and RETIRED (AIM1) → SKIP, nothing written.
    assert route_legacy_leg(conn, "ATS1", "LONG", "X", 0.8, 100.0, 95.0, 90.0, [110.0]) == LEG_SKIP
    assert route_legacy_leg(conn, "AIM1", "SHORT", "X", 0.8, 100.0, 95.0, 90.0, [110.0]) == LEG_SKIP
    assert conn.statements == []


def test_route_legacy_leg_respects_master_kill_switch():
    conn = FakeConn(fetch=None)
    prev = os.environ.get("KYTHERA_SHADOW_POSTING")
    os.environ["KYTHERA_SHADOW_POSTING"] = "0"  # Master switch off
    try:
        # SHADOW leg, but shadow-posting globally off → SKIP, nothing written.
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
