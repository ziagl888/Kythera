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

from core import shadow_gate as sg  # noqa: E402
from core.signal_post import post_shadow_ai_signal  # noqa: E402


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
    # Kein bestehendes Live-Bein darf versehentlich geshadowt werden.
    assert sg.leg_status("ATS1", "LONG") == sg.LIVE
    assert sg.leg_status("RUB2", "SHORT") == sg.LIVE
    assert sg.leg_status("SomeBrandNewTag", "LONG") == sg.LIVE
    assert sg.is_live("ATS1", "SHORT")
    assert not sg.is_shadow("ATS1", "SHORT")


def test_new_gen_candidates_are_shadow():
    for tag in ("ATS2", "ATB2", "SRA2"):
        for d in ("LONG", "SHORT"):
            assert sg.leg_status(tag, d) == sg.SHADOW
            assert sg.is_shadow(tag, d)
            assert not sg.is_live(tag, d)


def test_challenger_tags_are_shadow_and_dont_collide_with_live_leg():
    # RUB3/EPD3 sind die kollisionsfreien Challenger-Tags: der Shadow-Trade läuft
    # unter dem Challenger-Tag, das LIVE-Bein (RUB2/EPD2) bleibt LIVE — sonst würde
    # ein Shadow-Trade über den Active-Trade-Check einen Live-Post blockieren.
    assert sg.is_shadow("RUB3", "LONG")
    assert sg.is_shadow("EPD3", "LONG")
    assert sg.is_shadow("EPD3", "SHORT")
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
