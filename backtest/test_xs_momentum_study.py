# backtest/test_xs_momentum_study.py
"""DB-free tests for the K2 study machinery `tools/xs_momentum_study.py`
(T-2026-KYT-9050-013, follow-up from the K2 review T-2026-CU-9050-143 / PR #133).

Two machinery defects are pinned here — both do NOT change the NEGATIVE verdict of
the study (`weak/inconsistent-spread`), they fix the measurement:

  1. **market-neutral frame was a no-op.** `sig = sig_abs - btc_sig` is a
     per-rebalance SCALAR shift ⇒ argsort-invariant, and the PnL used absolute
     coin returns ⇒ all 60 `market_neutral` cells were byte-identical to their
     `absolute` twin. The beta adjust belongs on the RETURNS (like K5:
     `r_abs - (btc_H/btc_0 - 1)`), not on the rank-preserving signal.
  2. **Stage-2 entry ~1 daily bar too early (look-ahead).** `dates[t]` is the
     daily OPEN (`load_1d` floored to 'D'), but the ranking signal is `close[t]`.
     The entry at the first 1h close from `dates[t]` therefore sits ~23h BEFORE
     the signal is even observable. Correct: `dates[t] + 86400`.

Run: pytest backtest/test_xs_momentum_study.py -v
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import tools.xs_momentum_study as m  # noqa: E402

DAY = 86400
T0 = int(pd.Timestamp("2025-01-01T00:00:00Z").timestamp())
N_DAYS = 140  # > max(F)+max(H)+2 = 114, gives 4 rebalance rows in the weekly grid

# Geometric paths: constant daily return per coin ⇒ fwd over H is identical across
# all rebalances ⇒ the cell means can be recomputed in closed form.
BTC_GROWTH = 1.0020
COIN_GROWTH = {
    "AAAUSDT": 1.0009,
    "BBBUSDT": 1.0011,
    "CCCUSDT": 1.0013,
    "DDDUSDT": 1.0015,
    "EEEUSDT": 1.0017,
    "FFFUSDT": 1.0019,
}


def _coin_arrays(growth: float, base: float = 100.0) -> dict:
    closes = [base * growth**i for i in range(N_DAYS)]
    return {
        "d": [T0 + i * DAY for i in range(N_DAYS)],
        "c": closes,
        "l": [c * 0.99 for c in closes],   # rising series ⇒ min(low) = low[i_form]
        "qv": [1.0e7 for _ in closes],     # identical volume ⇒ tercile filter cuts nothing
        "ft": [],
        "fr": [],
    }


def _panel() -> dict:
    coin_data = {sym: _coin_arrays(g) for sym, g in COIN_GROWTH.items()}
    coin_data["BTCUSDT"] = _coin_arrays(BTC_GROWTH)
    return m.build_panel(coin_data)


def _cells() -> dict:
    panel = _panel()
    split_mid = float(panel["dates"][N_DAYS // 2])
    return m.build_cells(m.run_stage1(panel, split_mid, None))


def _btc_fwd(H: int) -> float:
    """The BTC return over an H-day holding window (constant in the synthetic panel)."""
    return BTC_GROWTH**H - 1.0


def _twin(ck: str) -> str:
    return ck.replace("|market_neutral|", "|absolute|")


# ── 1. market-neutral frame is no longer a no-op ──────────────────────────────
def test_market_neutral_cells_differ_from_absolute() -> None:
    """Reproduction of the defect: BEFORE the fix, every market_neutral cell was
    byte-identical to its absolute twin (60/60). With the beta adjust on the returns,
    the net level must differ as long as BTC did not run flat."""
    cells = _cells()
    mn = [ck for ck, c in cells.items() if c["frame"] == "market_neutral"]
    assert mn, "no market_neutral cells generated"
    identical = [
        ck for ck in mn
        if cells[ck]["all"]["avg_net_pct"] == cells[_twin(ck)]["all"]["avg_net_pct"]
    ]
    assert not identical, (
        f"{len(identical)}/{len(mn)} market_neutral cells are identical to absolute — "
        "the frame does not remove any beta (scalar shift on the signal is argsort-invariant)"
    )


def test_market_neutral_net_is_absolute_minus_btc_return() -> None:
    """The beta adjust sits on the RETURNS: LONG loses the BTC return, SHORT
    gains it (the short side profits when the market falls)."""
    cells = _cells()
    checked = 0
    for ck, c in cells.items():
        if c["frame"] != "market_neutral":
            continue
        twin = cells[_twin(ck)]
        for half in ("all", "val", "test"):
            if c[half]["avg_net_pct"] is None or twin[half]["avg_net_pct"] is None:
                continue
            sign = -1.0 if c["direction"] == "XSM1_LONG" else +1.0
            expected = twin[half]["avg_net_pct"] + sign * _btc_fwd(c["H"]) * 100.0
            assert abs(c[half]["avg_net_pct"] - expected) < 1e-3, (
                f"{ck}/{half}: {c[half]['avg_net_pct']} != {expected} "
                f"(absolute {twin[half]['avg_net_pct']}, btc_fwd {_btc_fwd(c['H']) * 100:.4f}%)"
            )
            checked += 1
    assert checked > 0


def test_absolute_frame_scoring_is_untouched() -> None:
    """Collateral protection: the `absolute` frame is the signal contract of bot 39
    (`39_ai_xsm1_bot.py` references cell F84|raw|absolute). The beta adjust
    MUST NOT touch it — closed form: net_LONG = (g_top^H − 1) − fee."""
    cells = _cells()
    g_top = max(COIN_GROWTH.values())
    for F in m.F_GRID:
        for H in m.H_GRID:
            c = cells[m.cell_key(F, H, "raw", "absolute", "XSM1_LONG")]
            expected = ((g_top**H - 1.0) - m.ROUND_TRIP_FEE) * 100.0
            assert abs(c["all"]["avg_net_pct"] - expected) < 1e-3, f"F{F}|H{H}: {c['all']}"
            s = cells[m.cell_key(F, H, "raw", "absolute", "XSR1_SHORT")]
            expected_s = (-(g_top**H - 1.0) - m.ROUND_TRIP_FEE) * 100.0
            assert abs(s["all"]["avg_net_pct"] - expected_s) < 1e-3, f"F{F}|H{H}: {s['all']}"


def test_market_neutral_selection_is_unchanged() -> None:
    """The frame may only change the SCORING, not the selection: the
    BTC signal subtraction is a scalar shift, so both frames rank the same —
    same n, same top-minus-bottom spread (beta cancels out in the spread).
    (Equal n holds as long as BTC is gap-free across the window — if the
    benchmark close is missing, the market_neutral cell deliberately skips the rebalance.)"""
    cells = _cells()
    for ck, c in cells.items():
        if c["frame"] != "market_neutral":
            continue
        twin = cells[_twin(ck)]
        assert c["all"]["n"] == twin["all"]["n"], f"{ck}: mismatched event count"
        assert (c["spread_top_minus_bottom"]["avg_net_pct"]
                == twin["spread_top_minus_bottom"]["avg_net_pct"]), (
            f"{ck}: the top-minus-bottom spread is beta-invariant and must "
            "stay identical between the frames"
        )


def test_shipped_full_run_artifact_is_flagged_pre_fix() -> None:
    """The checked-in full-run artifact comes from the code BEFORE this fix; its
    60 market_neutral cells are duplicates of the absolute twins. As long as the
    `semantics_version` in the artifact lags behind the code, exactly that must hold
    (and the report must flag it as STALE). After a re-run with the current
    semantics version, the expectation flips."""
    path = os.path.join(REPO_ROOT, "staging_models", "xs_momentum_study.json")
    if not os.path.exists(path):
        return  # artifact is optional (full run runs on the VPS)
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    cells = blob["cells"]
    dupes = [
        ck for ck, c in cells.items()
        if c["frame"] == "market_neutral"
        and _twin(ck) in cells
        and c["all"]["avg_net_pct"] == cells[_twin(ck)]["all"]["avg_net_pct"]
    ]
    version = int(blob["meta"].get("semantics_version", 1))
    if version < m.STUDY_SEMANTICS_VERSION:
        assert dupes, "pre-fix artifact without the expected market_neutral duplication?"
        assert "STALE" in m.build_markdown(
            blob["meta"], cells, blob["verdict"], blob.get("stage2", {})
        ), "A pre-fix artifact must be flagged as STALE in the report"
    else:
        assert not dupes, "a re-run with current semantics must no longer have frame duplicates"


def test_report_marks_current_run_as_fixed() -> None:
    """A run with the current semantics version carries NO STALE banner and
    describes both fixes as done."""
    meta = {
        "generated_at": "2026-08-01T00:00:00+00:00", "n_coins": 7, "n_universe": 7,
        "status": "complete", "semantics_version": m.STUDY_SEMANTICS_VERSION,
        "state_path": "/tmp/x.json",
    }
    md = m.build_markdown(meta, _cells(), {
        "verdict": "no-op/structure-does-not-replicate", "min_half_rebalances": 4,
        "min_robust_net_pct": 0.3, "n_cells": 0, "n_cells_val_positive": 0,
        "n_cells_passing": 0, "n_cells_robust": 0, "robust_cells": [],
        "passing_cells": [], "best_cell_selected_on_val": None,
    }, {})
    assert "STALE" not in md
    assert "beta-adjusted" in md.lower()


# ── 2. stage-2 entry: no more look-ahead ───────────────────────────────────────
def _hourly(symbol: str) -> pd.DataFrame:
    """1h candles over the whole panel window, price = hour index (uniquely
    back-computable to the entry timestamp)."""
    n = N_DAYS * 24
    idx = pd.to_datetime([(T0 + h * 3600) * 10**9 for h in range(n)], utc=True)
    price = np.arange(1.0, n + 1.0)
    return pd.DataFrame({"open_time": idx, "high": price, "low": price, "close": price})


def _run_stage2_capture(monkeypatch_target: dict) -> list[float]:
    """Runs run_stage2 with stubbed 1h loads + stubbed geometry and returns the
    entry timestamps (epoch seconds of the entry candle OPEN)."""
    seen: list[float] = []

    def fake_load_1h(_conn, symbol):
        return _hourly(symbol)

    def fake_geo_net(entry, is_long, t1h, h1h, l1h, c1h, start_idx):
        seen.append(pd.Timestamp(t1h[start_idx]).tz_localize("UTC").timestamp())
        return 0.01

    orig_load, orig_geo = m.load_1h_utc, m._geo_net
    m.load_1h_utc, m._geo_net = fake_load_1h, fake_geo_net
    try:
        panel = _panel()
        cells = m.build_cells(m.run_stage1(panel, float(panel["dates"][N_DAYS // 2]), None))
        ck = next(k for k, c in cells.items()
                  if c["F"] == 7 and c["H"] == 7 and c["variant"] == "raw"
                  and c["frame"] == "absolute" and c["direction"] == "XSM1_LONG")
        m.run_stage2(None, panel, cells, [ck], monkeypatch_target, None)
    finally:
        m.load_1h_utc, m._geo_net = orig_load, orig_geo
    return seen


def test_stage2_entry_does_not_precede_signal_observability() -> None:
    """Look-ahead proof: `dates[t]` is the daily OPEN, but the ranking signal is
    `close[t]` — so it is only known at the daily END (`dates[t]+86400`). An entry
    before that point trades on information that did not exist yet."""
    panel = _panel()
    rebal = [float(panel["dates"][i]) for i in m.rebalance_rows(panel["dates"], None)]
    entries = _run_stage2_capture({"processed_cells": [], "acc": {}})
    assert entries, "stage 2 replicated no events"
    for ts in entries:
        signal_known_at = min(r for r in rebal if r + DAY <= ts + 1e-6) + DAY \
            if any(r + DAY <= ts + 1e-6 for r in rebal) else None
        assert signal_known_at is not None, (
            f"entry {ts} precedes EVERY observable signal close "
            f"(earliest rebalance close {min(rebal) + DAY}) — look-ahead of "
            f"{(min(rebal) + DAY - ts) / 3600:.1f} h"
        )


def test_stage2_entry_is_first_hourly_bar_after_the_daily_close() -> None:
    """Exact semantics: entry candle = first 1h candle from `dates[t] + 86400`
    (the daily close at which the signal becomes observable)."""
    panel = _panel()
    rebal = [float(panel["dates"][i]) for i in m.rebalance_rows(panel["dates"], None)]
    entries = sorted(set(_run_stage2_capture({"processed_cells": [], "acc": {}})))
    expected = sorted(r + DAY for r in rebal if r + DAY <= float(panel["dates"][-1]))
    assert entries == expected[: len(entries)], (
        f"entry timestamps {entries[:4]} != expected {expected[:4]} "
        "(entry must anchor on the daily close, not the daily open)"
    )


def test_stage2_is_resume_safe_across_cells() -> None:
    """The fix must not touch the resume semantics: a cell that has already been
    processed is not replicated again."""
    state = {"processed_cells": [], "acc": {}}
    first = _run_stage2_capture(state)
    assert first and state["processed_cells"], "cell was not marked as processed"
    again = _run_stage2_capture(state)
    assert not again, "already processed cell was replicated again"
