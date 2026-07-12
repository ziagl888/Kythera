"""DB-freie Tests für den ATB2-Converging-Channel-Detektor (core/atb2_features).

Baut synthetische, deterministische Kanäle (konvergierend, Volumen-Kontraktion,
bestätigte Pivots) und prüft Detektion, Feature-Vertrag, No-Repaint-Kontrakt und
die Startup-Assertion. Kein DB-Zugriff — läuft standalone in CI-freier Umgebung.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import atb2_features as atb  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixture-Generator                                                            #
# --------------------------------------------------------------------------- #
def make_converging_channel(n_pre: int = 30, span: int = 80, period: int = 20,
                            u0: float = 110.0, l0: float = 90.0, conv: float = 0.05,
                            break_dir: str = "up", add_forming: bool = False,
                            in_vol: float = 4000.0, pre_vol: float = 12000.0,
                            break_vol: float = 8000.0, break_gap: float = 2.0,
                            n_post: int = 0) -> pd.DataFrame:
    """Erzeugt einen konvergierenden Kanal, der am Ende (geschlossen) ausbricht.

    Vorlauf = fallende Rampe mit hohem Volumen (für Volumen-Kontraktion & ATR/RSI-
    Warmlauf); Kanalfenster = Zickzack zwischen konvergierenden Grenzen mit
    bestätigten Pivots an den Wendepunkten; danach eine Ausbruchskerze.
    """
    def upper(j: float) -> float:
        return u0 - conv * j

    def lower(j: float) -> float:
        return l0 + conv * j

    # Wendepunkte im Kanalfenster: j%period==0 -> Low-Turn, j%period==period/2 -> High-Turn.
    turns: list[tuple[int, str]] = []
    half = period // 2
    for j in range(0, span + 1):
        if j % period == 0:
            turns.append((j, "low"))
        elif j % period == half:
            turns.append((j, "high"))

    def turn_val(j: int, kind: str) -> float:
        return upper(j) if kind == "high" else lower(j)

    opens, highs, lows, closes, vols, times = [], [], [], [], [], []
    t0 = pd.Timestamp("2026-01-01", tz=None)

    # Vorlauf: fallende Rampe 101 -> 97, hohes Volumen.
    for k in range(n_pre):
        px = 101.0 - (4.0 * k / max(1, n_pre - 1))
        opens.append(px + 0.1)
        highs.append(px + 0.3)
        lows.append(px - 0.3)
        closes.append(px)
        vols.append(pre_vol)
        times.append(t0 + pd.Timedelta(hours=len(times)))

    # Kanalfenster: close = stückweise linear zwischen Wendepunkten.
    for j in range(0, span):
        # bracketing turns
        prev_t = max([t for t in turns if t[0] <= j], key=lambda x: x[0])
        next_candidates = [t for t in turns if t[0] > j]
        next_t = min(next_candidates, key=lambda x: x[0]) if next_candidates else prev_t
        pv = turn_val(*prev_t)
        nv = turn_val(*next_t)
        if next_t[0] == prev_t[0]:
            close = pv
        else:
            frac = (j - prev_t[0]) / (next_t[0] - prev_t[0])
            close = pv + (nv - pv) * frac
        up_v, lo_v = upper(j), lower(j)
        is_high_turn = (j % period == half)
        is_low_turn = (j % period == 0)
        if is_high_turn:
            hi = up_v          # exakt auf der Oberkante -> Touch
            lo = close - 0.3
        elif is_low_turn:
            lo = lo_v          # exakt auf der Unterkante -> Touch
            hi = close + 0.3
        else:
            hi = min(close + 0.3, up_v - 0.4)
            lo = max(close - 0.3, lo_v + 0.4)
        opens.append(closes[-1])
        highs.append(hi)
        lows.append(lo)
        closes.append(close)
        vols.append(in_vol)   # In-Kanal-Volumen < Vorlauf -> Kontraktion
        times.append(t0 + pd.Timedelta(hours=len(times)))

    # Ausbruchskerze (geschlossen): schließt jenseits der Grenze bei j=span.
    up_end, lo_end = upper(span), lower(span)
    if break_dir == "up":
        oc, cc = up_end - 1.0, up_end + break_gap
        hi, lo = cc + 1.0, oc - 0.5
    else:
        oc, cc = lo_end + 1.0, lo_end - break_gap
        hi, lo = oc + 0.5, cc - 1.0
    opens.append(oc)
    highs.append(hi)
    lows.append(lo)
    closes.append(cc)
    vols.append(break_vol)       # Volumen-Spike
    times.append(t0 + pd.Timedelta(hours=len(times)))

    # Fortsetzungskerzen nach dem Ausbruch (damit simulate_exit im Adapter eine
    # Folgekerze hat und der Break nicht die letzte Zeile ist).
    for k in range(n_post):
        step = (break_gap + 1.0) if break_dir == "up" else -(break_gap + 1.0)
        px = closes[-1] + step
        opens.append(closes[-1])
        highs.append(px + 0.5)
        lows.append(px - 0.5)
        closes.append(px)
        vols.append(in_vol)
        times.append(t0 + pd.Timedelta(hours=len(times)))

    if add_forming:
        # Eine noch offene Forming-Candle mit Müll-Werten (der Aufrufer schneidet
        # sie via df.iloc[:-1] ab, R1) — darf das Ergebnis nicht verändern.
        opens.append(closes[-1])
        highs.append(closes[-1] + 50.0)
        lows.append(closes[-1] - 50.0)
        closes.append(closes[-1] + 30.0)
        vols.append(50.0)
        times.append(t0 + pd.Timedelta(hours=len(times)))

    return pd.DataFrame({
        "open_time": times, "open": opens, "high": highs,
        "low": lows, "close": closes, "volume": vols,
    })


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #
def test_detects_converging_channel_breakout_long():
    df = atb.compute_indicators(make_converging_channel(break_dir="up"))
    setup = atb.find_channel_breakout(df)
    assert setup is not None, "konvergierender Ausbruch nach oben nicht erkannt"
    assert setup["direction"] == "LONG"
    f = setup["features"]
    assert f["break_up"] == 1.0
    assert f["chan_convergence"] >= atb.CONVERGENCE_MIN
    assert f["chan_touch_upper"] >= atb.MIN_TOUCHES
    assert f["chan_touch_lower"] >= atb.MIN_TOUCHES
    assert atb.WIDTH_MIN_ATR <= f["chan_width_atr"] <= atb.WIDTH_MAX_ATR
    assert f["chan_vol_contraction"] < atb.VOL_CONTRACTION_MAX


def test_detects_converging_channel_breakout_short():
    df = atb.compute_indicators(make_converging_channel(break_dir="down"))
    setup = atb.find_channel_breakout(df)
    assert setup is not None, "konvergierender Ausbruch nach unten nicht erkannt"
    assert setup["direction"] == "SHORT"
    assert setup["features"]["break_up"] == 0.0


def test_forming_candle_must_be_sliced_by_caller():
    """No-Repaint: mit abgeschnittener Forming-Candle liefert der Detektor
    dasselbe Ergebnis wie ohne — die Müllkerze verändert nichts (R1-Kontrakt)."""
    df_full = atb.compute_indicators(make_converging_channel(break_dir="up", add_forming=True))
    df_closed = df_full.iloc[:-1].reset_index(drop=True)
    setup = atb.find_channel_breakout(df_closed)
    assert setup is not None and setup["direction"] == "LONG"


def test_feature_contract_complete_and_finite():
    df = atb.compute_indicators(make_converging_channel(break_dir="up"))
    setup = atb.find_channel_breakout(df)
    assert setup is not None
    f = setup["features"]
    assert set(f.keys()) == set(atb.ATB2_FEATURES), "Feature-Set weicht vom Vertrag ab"
    for k, v in f.items():
        assert np.isfinite(v), f"Feature {k} nicht endlich: {v}"


def test_no_channel_on_pure_trend():
    """Reiner Aufwärtstrend ohne Konsolidierung -> kein konvergierender Kanal."""
    n = 160
    px = np.linspace(90.0, 130.0, n)
    df = pd.DataFrame({
        "open_time": pd.date_range("2026-01-01", periods=n, freq="h"),
        "open": px, "high": px + 0.3, "low": px - 0.3, "close": px,
        "volume": np.full(n, 5000.0),
    })
    setup = atb.find_channel_breakout(atb.compute_indicators(df))
    assert setup is None


def test_assert_features_alive_raises_on_missing_and_constant():
    df = atb.compute_indicators(make_converging_channel(break_dir="up"))
    setup = atb.find_channel_breakout(df)
    assert setup is not None
    # Mehrere variierte Kanäle -> kontinuierliche Features variieren garantiert.
    rows = []
    for i, kw in enumerate([
        dict(conv=0.05, span=80, in_vol=4000, break_gap=2.0, break_vol=8000),
        dict(conv=0.06, span=100, in_vol=3500, break_gap=3.0, break_vol=9000),
        dict(conv=0.045, span=70, in_vol=4500, break_gap=1.5, break_vol=7000, period=14),
        dict(conv=0.055, span=90, in_vol=3000, break_gap=2.5, break_vol=8500),
    ]):
        d = atb.compute_indicators(make_converging_channel(break_dir="up", **kw))
        s = atb.find_channel_breakout(d)
        assert s is not None, f"Fixture {i} bildete keinen Kanal"
        rows.append(s["features"])
    feat_df = pd.DataFrame(rows)
    atb.assert_features_alive(feat_df)  # darf nicht werfen
    row_a = rows[0]

    import pytest
    with pytest.raises(ValueError):
        atb.assert_features_alive(feat_df.drop(columns=["rsi"]))
    const_df = pd.DataFrame([row_a, row_a])  # alles konstant
    with pytest.raises(ValueError):
        atb.assert_features_alive(const_df)


def test_measured_move_targets_geometry():
    df = atb.compute_indicators(make_converging_channel(break_dir="up"))
    setup = atb.find_channel_breakout(df)
    tg = atb.measured_move_targets(setup["channel"], setup["breakout"], setup["entry"])
    assert tg["targets"] == sorted(tg["targets"]), "LONG-Targets müssen aufsteigen"
    assert tg["sl"] < setup["entry"], "LONG-SL muss unter dem Entry liegen"
    assert setup["entry"] * 0.85 <= tg["sl"] <= setup["entry"], "SL-Cap verletzt"


def test_run_atb2_adapter_emits_record(monkeypatch):
    """DB-freier End-to-End-Test des Walkforward-Adapters: load_ohlcv und
    calculate_smart_targets werden gestubbt, simulate_exit läuft echt."""
    import tools.walkforward_sim as w

    df = make_converging_channel(break_dir="up", n_pre=150, n_post=15)
    monkeypatch.setattr(w, "load_ohlcv", lambda conn, sym, tf, days: df.copy())
    monkeypatch.setattr(
        w, "calculate_smart_targets",
        lambda conn, sym, direction, price, df=None, levels=None: {
            "entry1": price, "entry2": price, "sl": price * 0.95,
            "targets": [price * 1.02, price * 1.04, price * 1.06],
        },
    )
    trades = w.run_atb2(conn=None, symbol="TEST_USDT", days=365)
    assert len(trades) >= 1, "Adapter emittierte keinen Ausbruch"
    tr = trades[0]
    assert tr["strategy"] == "atb2" and tr["direction"] == "LONG"
    assert set(tr["features"].keys()) == set(atb.ATB2_FEATURES)
    # simulate_exit-Ergebnis ist eingespreizt:
    assert "outcome_tp1" in tr and "net_pnl_pct" in tr
    # §11-Vergleichsfelder:
    assert "smart_outcome_tp1" in tr and "smart_net_pnl_pct" in tr


def test_indicators_deterministic_and_finite():
    df = atb.compute_indicators(make_converging_channel(break_dir="up"))
    assert df["atr"].iloc[-1] > 0 and np.isfinite(df["atr"].iloc[-1])
    assert 0 <= df["rsi"].iloc[-1] <= 100
    assert len(df) == len(df.dropna(subset=["atr"])) + (atb.ATR_PERIOD - 1)
