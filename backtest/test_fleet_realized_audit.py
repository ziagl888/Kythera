# backtest/test_fleet_realized_audit.py
"""DB-freie Tests für die reinen Helfer von tools/fleet_realized_audit.py
(Phase A, T-2026-KYT-9050-032): Outcome-Klassifikation, gestaffelter/roher
unlevered Move, R-Multiple, Leg-Aggregation, Verdikt und die Lifecycle-
Bucketisierung (echtes core.shadow_gate + core.bot_catalog, DB-frei).

Run: pytest backtest/test_fleet_realized_audit.py -v
     python backtest/test_fleet_realized_audit.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

from tools.fleet_realized_audit import (  # noqa: E402
    aggregate_leg,
    classify_ai_outcome,
    classify_classic_outcome,
    lifecycle_bucket,
    r_from_move,
    rank_legs,
    signed_move_pct,
    unlev_move,
    verdict_for,
)


# ── Outcome-Klassifikation ──────────────────────────────────────────────────
def test_ai_outcome_legacy_first():
    # LEGACY (synthetische ±2.5%) schlägt alles — auch mit targets_hit>=1.
    assert classify_ai_outcome("LEGACY TARGET HIT (+2.5%)", 1) == "legacy"
    assert classify_ai_outcome("LEGACY SL HIT (-2.5%)", 0) == "legacy"


def test_ai_outcome_censored_and_win_loss():
    assert classify_ai_outcome("DELISTED / CLEANUP", 0) == "censored"
    assert classify_ai_outcome("CLOSED_REGIME_CHANGE", 2) == "censored"
    assert classify_ai_outcome("ALL TARGETS HIT", 0) == "win"
    assert classify_ai_outcome("SL Hit (SL: 0.0638)", 2) == "win"  # TP1-Touch-Win
    assert classify_ai_outcome("SL Hit (SL: 0.02)", 0) == "loss"
    assert classify_ai_outcome("ENTRY_NOT_FILLED", 0) == "unfilled"
    assert classify_ai_outcome("HORIZON_TIMEOUT", 0) == "timeout"


def test_classic_outcome():
    for s in ("1", "2", "3", "4", "SL1", "SL2", "SL3"):
        assert classify_classic_outcome(s) == "win", s
    for s in ("0", "SL0"):
        assert classify_classic_outcome(s) == "loss", s
    assert classify_classic_outcome("FORCE_CLOSED") == "censored"
    assert classify_classic_outcome("DELISTED") == "censored"


# ── Move-Mathematik ─────────────────────────────────────────────────────────
def test_signed_move_direction_and_guards():
    assert signed_move_pct("LONG", 100, 110) == 10.0
    assert signed_move_pct("SHORT", 100, 90) == 10.0  # SHORT-Gewinn = +
    assert signed_move_pct("SHORT", 100, 110) == -10.0
    assert signed_move_pct("SIDE", 100, 110) is None
    assert signed_move_pct("LONG", 0, 110) is None
    assert signed_move_pct("LONG", 100, 100000) is None  # > MAX_ABS_MOVE_PCT


def test_unlev_move_prefers_staffed():
    # Zwei Targets, beide getroffen: gestaffelter Move = Mittel der Target-Moves,
    # NICHT der rohe entry->close. staffed=True.
    move, staffed = unlev_move("LONG", 100.0, 104.0, [102.0, 106.0], 2)
    assert staffed is True
    assert abs(move - 4.0) < 1e-9  # (2% + 6%)/2

    # Ohne Targets -> roher entry->close, staffed=False.
    move2, staffed2 = unlev_move("LONG", 100.0, 104.0, [], 0)
    assert staffed2 is False
    assert abs(move2 - 4.0) < 1e-9


def test_r_from_move():
    # 4% Move bei 2% Risiko = +2R.
    assert abs(r_from_move(4.0, 100.0, 98.0) - 2.0) < 1e-9
    # SL-Loss ~ -1R.
    assert abs(r_from_move(-2.0, 100.0, 98.0) - (-1.0)) < 1e-9
    assert r_from_move(None, 100, 98) is None
    assert r_from_move(4.0, 100, 0) is None  # sl<=0


# ── Aggregation ─────────────────────────────────────────────────────────────
def _row(oc, move=None, staffed=False, lev=None, r=None):
    return {"outcome": oc, "move": move, "staffed": staffed, "lev_pnl": lev, "r": r, "ts": None}


def test_aggregate_leg_stats():
    rows = [
        _row("win", move=2.0, staffed=True, lev=40.0, r=1.0),
        _row("win", move=1.0, staffed=True, lev=20.0, r=0.5),
        _row("loss", move=-3.0, staffed=False, lev=-60.0, r=-1.0),
        _row("legacy"),
        _row("censored"),
    ]
    s = aggregate_leg(rows)
    assert s["n_decided"] == 3
    assert s["wins"] == 2 and s["losses"] == 1
    assert s["wr_pct"] == 66.7
    # mean move = (2 + 1 - 3)/3 = 0.0 -> net = -0.10 (Fee).
    assert abs(s["mean_move_pct"]) < 1e-9
    assert abs(s["net_mean_pct"] - (-0.10)) < 1e-9
    assert s["staffed_pct"] == 67  # 2/3 gestaffelt
    assert s["lev_n"] == 3
    assert s["r_n"] == 3
    assert s["legacy_n"] == 1
    assert s["censored_n"] == 1


def test_aggregate_leg_synthetic_only():
    s = aggregate_leg([_row("legacy"), _row("legacy"), _row("censored")])
    assert s["n_decided"] == 0
    assert s["legacy_n"] == 2 and s["censored_n"] == 1
    assert s["mean_move_pct"] is None


# ── Verdikt ─────────────────────────────────────────────────────────────────
def test_verdict():
    keep = aggregate_leg([_row("win", move=1.0)] * 40)
    assert verdict_for(keep) == "KEEP"
    retire = aggregate_leg([_row("loss", move=-1.0)] * 40)
    assert verdict_for(retire) == "RETIRE-CANDIDATE"
    thin = aggregate_leg([_row("win", move=1.0)] * 5)
    assert verdict_for(thin) == "THIN"
    synth = aggregate_leg([_row("legacy")] * 10)
    assert verdict_for(synth) == "SYNTHETIC/CENSORED-ONLY"
    assert verdict_for(aggregate_leg([])) == "NO-DATA"


def test_rank_legs_worst_first():
    legs = [
        {"stats": {"net_mean_pct": +0.5}},
        {"stats": {"net_mean_pct": -1.0}},
        {"stats": {"net_mean_pct": None}},
        {"stats": {"net_mean_pct": +0.1}},
    ]
    ranked = rank_legs(legs)
    nets = [lg["stats"]["net_mean_pct"] for lg in ranked]
    assert nets[0] == -1.0  # worst first
    assert nets[-1] is None  # None sinks to the end


# ── Lifecycle (echtes shadow_gate + bot_catalog) ────────────────────────────
def test_lifecycle_bucket():
    from core.bot_catalog import script_for_tag

    assert lifecycle_bucket("ATS2", "LONG", set()) == "shadow"
    assert lifecycle_bucket("AIM1", "SHORT", set()) == "retired"
    assert lifecycle_bucket("ATS1", "LONG", {script_for_tag("ATS1")}) == "retired"  # SILENT
    rub = script_for_tag("RUB2")
    assert lifecycle_bucket("RUB2", "SHORT", {rub}) == "active"
    assert lifecycle_bucket("RUB2", "SHORT", set()) == "inactive"  # script parked
    assert lifecycle_bucket("ZZZ_NOT_A_MODEL", "LONG", set()) == "unmapped"


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
