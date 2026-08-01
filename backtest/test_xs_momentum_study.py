# backtest/test_xs_momentum_study.py
"""DB-freie Tests für die K2-Studien-Maschinerie `tools/xs_momentum_study.py`
(T-2026-KYT-9050-013, Follow-up aus dem K2-Review T-2026-CU-9050-143 / PR #133).

Zwei Maschinen-Defekte werden hier gepinnt — beide ändern das NEGATIVE Verdikt der
Studie nicht (`weak/inconsistent-spread`), sie reparieren die Messung:

  1. **market-neutral-Frame war ein No-op.** `sig = sig_abs - btc_sig` ist ein
     Per-Rebalance-SKALAR-Shift ⇒ argsort-invariant, und die PnL benutzte absolute
     Coin-Returns ⇒ alle 60 `market_neutral`-Zellen waren byte-identisch zu ihrem
     `absolute`-Zwilling. Der Beta-Adjust gehört auf die RETURNS (wie K5:
     `r_abs - (btc_H/btc_0 - 1)`), nicht auf das rang-erhaltende Signal.
  2. **Stage-2-Entry ~1 Tages-Balken zu früh (Look-ahead).** `dates[t]` ist der
     Tages-OPEN (`load_1d` floored auf 'D'), das Ranking-Signal ist aber `close[t]`.
     Der Entry am ersten 1h-Close ab `dates[t]` liegt damit ~23 h BEVOR das Signal
     überhaupt beobachtbar ist. Korrekt: `dates[t] + 86400`.

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
N_DAYS = 140  # > max(F)+max(H)+2 = 114, gibt 4 Rebalance-Zeilen im Wochenraster

# Geometrische Pfade: konstante Tages-Rendite je Coin ⇒ fwd über H ist über alle
# Rebalances identisch ⇒ die Zell-Mittelwerte sind geschlossen nachrechenbar.
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
        "l": [c * 0.99 for c in closes],   # steigende Serie ⇒ min(low) = low[i_form]
        "qv": [1.0e7 for _ in closes],     # identisches Volumen ⇒ Terzil-Filter schneidet nichts
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
    """Der BTC-Return über ein H-Tage-Halte-Fenster (konstant im synthetischen Panel)."""
    return BTC_GROWTH**H - 1.0


def _twin(ck: str) -> str:
    return ck.replace("|market_neutral|", "|absolute|")


# ── 1. market-neutral-Frame ist kein No-op mehr ───────────────────────────────
def test_market_neutral_cells_differ_from_absolute() -> None:
    """Reproduktion des Defekts: VOR dem Fix war jede market_neutral-Zelle
    byte-identisch zum absolute-Zwilling (60/60). Mit Beta-Adjust auf den Returns
    muss sich der Netto-Level unterscheiden, solange BTC nicht flach lief."""
    cells = _cells()
    mn = [ck for ck, c in cells.items() if c["frame"] == "market_neutral"]
    assert mn, "keine market_neutral-Zellen erzeugt"
    identical = [
        ck for ck in mn
        if cells[ck]["all"]["avg_net_pct"] == cells[_twin(ck)]["all"]["avg_net_pct"]
    ]
    assert not identical, (
        f"{len(identical)}/{len(mn)} market_neutral-Zellen sind identisch zu absolute — "
        "der Frame entfernt kein Beta (Skalar-Shift auf dem Signal ist argsort-invariant)"
    )


def test_market_neutral_net_is_absolute_minus_btc_return() -> None:
    """Der Beta-Adjust liegt auf den RETURNS: LONG verliert den BTC-Return, SHORT
    gewinnt ihn (die Short-Seite profitiert, wenn der Markt fällt)."""
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
    """Kollateral-Schutz: der `absolute`-Frame ist der Signal-Vertrag von Bot 39
    (`39_ai_xsm1_bot.py` referenziert die Zelle F84|raw|absolute). Der Beta-Adjust
    darf ihn NICHT anfassen — geschlossene Form: net_LONG = (g_top^H − 1) − Fee."""
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
    """Der Frame darf nur die BEWERTUNG ändern, nicht die Auswahl: die
    BTC-Signal-Subtraktion ist ein Skalar-Shift, also ranken beide Frames gleich —
    gleiche n, gleicher Top-minus-Bottom-Spread (Beta kürzt sich im Spread weg).
    (Gleiches n gilt, solange BTC über das Fenster lückenlos vorliegt — fehlt der
    Benchmark-Close, überspringt die market_neutral-Zelle das Rebalance bewusst.)"""
    cells = _cells()
    for ck, c in cells.items():
        if c["frame"] != "market_neutral":
            continue
        twin = cells[_twin(ck)]
        assert c["all"]["n"] == twin["all"]["n"], f"{ck}: abweichende Ereigniszahl"
        assert (c["spread_top_minus_bottom"]["avg_net_pct"]
                == twin["spread_top_minus_bottom"]["avg_net_pct"]), (
            f"{ck}: der Top-minus-Bottom-Spread ist beta-invariant und muss "
            "zwischen den Frames identisch bleiben"
        )


def test_shipped_full_run_artifact_is_flagged_pre_fix() -> None:
    """Das eingecheckte Voll-Lauf-Artefakt stammt aus dem Code VOR diesem Fix; seine
    60 market_neutral-Zellen sind Duplikate der absolute-Zwillinge. Solange die
    `semantics_version` im Artefakt hinter dem Code liegt, muss genau das gelten (und
    der Report muss es als STALE ausweisen). Nach einem Re-Run mit aktueller
    Semantik-Version dreht die Erwartung um."""
    path = os.path.join(REPO_ROOT, "staging_models", "xs_momentum_study.json")
    if not os.path.exists(path):
        return  # Artefakt ist optional (Voll-Lauf läuft auf dem VPS)
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
        assert dupes, "Pre-Fix-Artefakt ohne die erwartete market_neutral-Duplizität?"
        assert "STALE" in m.build_markdown(
            blob["meta"], cells, blob["verdict"], blob.get("stage2", {})
        ), "Ein Pre-Fix-Artefakt muss im Report als STALE gekennzeichnet werden"
    else:
        assert not dupes, "Re-Run mit aktueller Semantik darf keine Frame-Duplikate mehr haben"


def test_report_marks_current_run_as_fixed() -> None:
    """Ein Lauf mit aktueller Semantik-Version trägt KEINEN STALE-Banner und
    beschreibt beide Fixes als erledigt."""
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


# ── 2. Stage-2-Entry: kein Look-ahead mehr ────────────────────────────────────
def _hourly(symbol: str) -> pd.DataFrame:
    """1h-Kerzen über das ganze Panel-Fenster, Preis = Stunden-Index (eindeutig
    rückrechenbar auf den Entry-Zeitpunkt)."""
    n = N_DAYS * 24
    idx = pd.to_datetime([(T0 + h * 3600) * 10**9 for h in range(n)], utc=True)
    price = np.arange(1.0, n + 1.0)
    return pd.DataFrame({"open_time": idx, "high": price, "low": price, "close": price})


def _run_stage2_capture(monkeypatch_target: dict) -> list[float]:
    """Fährt run_stage2 mit gestubbten 1h-Loads + gestubbter Geometrie und gibt die
    Entry-Zeitstempel (Epoch-Sekunden des Entry-Kerzen-OPEN) zurück."""
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
    """Look-ahead-Nachweis: `dates[t]` ist der Tages-OPEN, das Ranking-Signal ist
    `close[t]` — also erst zum Tages-ENDE (`dates[t]+86400`) bekannt. Ein Entry vor
    diesem Zeitpunkt handelt Information, die es noch nicht gab."""
    panel = _panel()
    rebal = [float(panel["dates"][i]) for i in m.rebalance_rows(panel["dates"], None)]
    entries = _run_stage2_capture({"processed_cells": [], "acc": {}})
    assert entries, "stage 2 hat keine Ereignisse repliziert"
    for ts in entries:
        signal_known_at = min(r for r in rebal if r + DAY <= ts + 1e-6) + DAY \
            if any(r + DAY <= ts + 1e-6 for r in rebal) else None
        assert signal_known_at is not None, (
            f"Entry {ts} liegt vor JEDEM beobachtbaren Signal-Close "
            f"(frühester Rebalance-Close {min(rebal) + DAY}) — Look-ahead von "
            f"{(min(rebal) + DAY - ts) / 3600:.1f} h"
        )


def test_stage2_entry_is_first_hourly_bar_after_the_daily_close() -> None:
    """Exakte Semantik: Entry-Kerze = erste 1h-Kerze ab `dates[t] + 86400`
    (dem Tages-Close, an dem das Signal beobachtbar wird)."""
    panel = _panel()
    rebal = [float(panel["dates"][i]) for i in m.rebalance_rows(panel["dates"], None)]
    entries = sorted(set(_run_stage2_capture({"processed_cells": [], "acc": {}})))
    expected = sorted(r + DAY for r in rebal if r + DAY <= float(panel["dates"][-1]))
    assert entries == expected[: len(entries)], (
        f"Entry-Zeitpunkte {entries[:4]} != erwartete {expected[:4]} "
        "(Entry muss am Tages-Close ansetzen, nicht am Tages-Open)"
    )


def test_stage2_is_resume_safe_across_cells() -> None:
    """Der Fix darf die Resume-Semantik nicht anfassen: eine bereits verarbeitete
    Zelle wird nicht erneut repliziert."""
    state = {"processed_cells": [], "acc": {}}
    first = _run_stage2_capture(state)
    assert first and state["processed_cells"], "Zelle wurde nicht als verarbeitet vermerkt"
    again = _run_stage2_capture(state)
    assert not again, "bereits verarbeitete Zelle wurde erneut repliziert"
