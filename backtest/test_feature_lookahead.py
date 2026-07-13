# backtest/test_feature_lookahead.py — Look-ahead-Perturbationstest über die
# geteilten Feature-Builder (core/*_features.py, Trainer == Serving == Replay).
#
# Macht die harten Regeln 5 (nur geschlossene Kerzen) und 7 (geteilte
# Feature-Builder) mechanisch prüfbar statt nur per DO-NOT-Kommentar behauptet.
# Vorbild: HKUDS/Vibe-Trading tests/factors/test_lookahead.py (MIT),
# T-2026-CU-9050-027 D1.
#
# Mechanik je Builder-Klasse:
#   * Frame-/as-of-Builder (Funktion SIEHT Zukunftszeilen im Input und muss sie
#     ignorieren): Baseline berechnen, dann alle Input-Spalten ab der
#     Perturbations-Zeile mit NaN/1e10 vergiften, neu berechnen — die Werte vor
#     der Perturbation müssen bit-nah (atol/rtol 1e-9) invariant bleiben.
#     Ein Future-Leak fällt sofort durch.
#       - mis.add_advanced_features        (ganzer Frame, Features je Zeile)
#       - research.candle_context_features (df + idx; alles > idx ist Zukunft)
#       - research.build_pex1_row / build_fmr1_row / build_fif1_row
#       - funding.funding_features_asof    (volle Historie + ts, interner Slice)
#   * Window-scoped Builder (die Signatur enthält per Kontrakt NUR
#     Vergangenheit — das Fenster endet am Entscheidungszeitpunkt, der Caller
#     schneidet): keine Perturbations-Achse vorhanden. Hier wird stattdessen
#     Determinismus + Input-Nicht-Mutation geprüft; der Leak-Surface liegt im
#     Caller (Bot-SQL / Dataset-Builder) und wird für fetch_context_frame
#     unten explizit mitgetestet.
#       - rub.rub_trend / build_rub_features (Fenster-Arrays)
#       - research.build_trm1_row            (regime_history-Fenster; zusätzlich:
#                                             Zeilen älter als das 12er-Fenster
#                                             dürfen NICHT einfließen)
#       - research.funding_stats             (Settlement-Liste bis "jetzt")
#       - aim2.build_feature_row             (row-scoped, kein Zeit-Input;
#                                             der floor-1-Join ist Caller-Pflicht)
#       - sra.build_sra2_features            (row-scoped, eine Indikator-Zeile rein;
#                                             der floor-1-Join ist Caller-Pflicht)
#   * Bewusst NICHT einzeln getestet (keine Look-ahead-Fläche): pct_distance
#     (elementweises Mapping ohne Fenster/Shift, transitiv via
#     add_advanced_features mitgetestet), assert_features_alive (beide Module,
#     Invarianten-Assertion statt Feature-Berechnung), parity_nonzero_share
#     (Diagnose auf fertigem Vektor), load_funding (reiner DB-Loader; das
#     as-of-Gate liegt in funding_features_asof und ist oben abgedeckt).
#   * walkforward_sim.load_ohlcv / load_joined (R1-Kern, DB-frei via Fake-Reader):
#     die beiden Loader speisen die Labels JEDES Retrains (P0.10). Sie müssen
#     core.candles mit include_forming=False aufrufen; die laufende Kerze darf
#     nicht im Replay-Frame landen. Der Fake-Reader bildet den Cutoff pandas-
#     seitig nach — die echte SQL braucht libpq zum Rendern (test_candles.py).
#   * fetch_context_frame (R1-Kern, DB-frei via Stub-Cursor): eine Forming
#     Candle der aktuellen Stunde in der DB (is_closed ist NICHT durchgesetzt,
#     OPUS-HANDOFF Falle 1) darf weder die gewählte Feature-Kerze noch deren
#     Features ändern — der floor-1-Join muss sie ignorieren.
#
# Läuft ohne DB:  python backtest/test_feature_lookahead.py

import datetime as dt
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import aim2_features, funding_features, research_features, rub_features, sra_features  # noqa: E402
from core.mis_features import (  # noqa: E402
    FEATURE_COLS,
    LEGACY_ONLY_COLS,
    RAW_LINE_COLS,
    REQUIRED_INPUT_COLS,
    add_advanced_features,
    add_advanced_features_multi,
)

# ── Konstanten ────────────────────────────────────────────────────────────────
N_ROWS = 300
PROBE_T = 250  # Zeile, deren Features unter Zukunfts-Vergiftung invariant sein müssen
PERTURB_VALUE = 1e10
RTOL = ATOL = 1e-9  # jeder echte Leak drückt weit größer durch als 1e-9


def poison_future(df: pd.DataFrame, cols: list[str], start: int) -> pd.DataFrame:
    """Kopie von ``df``, in der ``cols`` ab Zeile ``start`` vergiftet sind —
    abwechselnd NaN und 1e10 je Spalte, damit beide Repräsentationen eines
    Leaks (NaN-Propagation und Absurd-Wert-Drift) Abdeckung bekommen."""
    out = df.copy()
    for j, col in enumerate(cols):
        out.iloc[start:, out.columns.get_loc(col)] = np.nan if j % 2 == 0 else PERTURB_VALUE
    return out


def assert_rows_invariant(base: pd.DataFrame, poisoned: pd.DataFrame, cols: list[str], upto: int, label: str) -> None:
    """Alle Zeilen < ``upto`` müssen in ``cols`` bit-nah übereinstimmen."""
    a = base.loc[: upto - 1, cols].to_numpy(dtype=np.float64)
    b = poisoned.loc[: upto - 1, cols].to_numpy(dtype=np.float64)
    nan_a, nan_b = np.isnan(a), np.isnan(b)
    assert np.array_equal(nan_a, nan_b), f"{label}: NaN-Muster vor der Perturbation divergiert (Look-ahead-Leak)"
    np.testing.assert_allclose(
        a[~nan_a], b[~nan_b], rtol=RTOL, atol=ATOL,
        err_msg=f"{label}: Feature-Werte vor der Perturbation divergieren (Look-ahead-Leak)",
    )


def assert_dicts_equal(base: dict, poisoned: dict, label: str) -> None:
    assert set(base) == set(poisoned), f"{label}: Feature-Keys divergieren ({set(base) ^ set(poisoned)})"
    for k in base:
        np.testing.assert_allclose(
            float(base[k]), float(poisoned[k]), rtol=RTOL, atol=ATOL,
            err_msg=f"{label}: Feature '{k}' ändert sich unter Zukunfts-Vergiftung (Look-ahead-Leak)",
        )


# ── Synthetische Frames ───────────────────────────────────────────────────────
def make_mis_df(n=N_ROWS, seed=7) -> pd.DataFrame:
    """1h-Frame mit allen MIS-Pflichtspalten (wie backtest/test_mis_features.py)."""
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    df = pd.DataFrame({
        "open_time": pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"),
        "close": close,
        "volume": rng.uniform(1000, 50000, n),
    })
    for c in ["rsi_6", "rsi_9", "rsi_12", "rsi_14", "rsi_24"]:
        df[c] = rng.uniform(20, 80, n)
    for c in RAW_LINE_COLS:
        df[c] = close * rng.uniform(0.97, 1.03, n)
    df["tsi_fast"] = rng.normal(0, 20, n)
    df["macd_dif"] = rng.normal(0, 0.5, n) * close / 100
    df["macd_dea"] = rng.normal(0, 0.5, n) * close / 100
    df["atr_14"] = close * rng.uniform(0.005, 0.03, n)
    return df


CONTEXT_VALUE_COLS = ["close", "volume", "rsi_14", "ema_21", "ema_200", "atr_14", "boll_upper_20", "boll_lower_20"]


def make_context_df(n=N_ROWS, seed=11) -> pd.DataFrame:
    """ASC-Frame mit den CONTEXT_SQL_SELECT-Spalten (research_features)."""
    rng = np.random.default_rng(seed)
    close = 50 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame({
        "open_time": pd.date_range("2026-01-01", periods=n, freq="h"),
        "close": close,
        "volume": rng.uniform(1000, 50000, n),
        "rsi_14": rng.uniform(20, 80, n),
        "ema_21": close * rng.uniform(0.98, 1.02, n),
        "ema_200": close * rng.uniform(0.95, 1.05, n),
        "atr_14": close * rng.uniform(0.005, 0.03, n),
        "boll_upper_20": close * rng.uniform(1.01, 1.04, n),
        "boll_lower_20": close * rng.uniform(0.96, 0.99, n),
    })


# ── MIS: Frame-Builder ────────────────────────────────────────────────────────
def test_mis_lookahead():
    df = make_mis_df()
    perturb_from = PROBE_T + 10
    for include_legacy in (False, True):
        cols = FEATURE_COLS + ([c for c in LEGACY_ONLY_COLS if c != "atr_14"] if include_legacy else [])
        base = add_advanced_features(df, include_legacy=include_legacy)
        poisoned = add_advanced_features(
            poison_future(df, REQUIRED_INPUT_COLS, perturb_from), include_legacy=include_legacy
        )
        assert_rows_invariant(base, poisoned, cols, perturb_from, f"MIS(include_legacy={include_legacy})")
        # Canary (Detektionskraft): IN der vergifteten Region müssen die Features
        # divergieren — sonst wäre die Perturbation nie beim Builder angekommen
        # und die Invarianz oben ein Scheinerfolg.
        a = base.loc[perturb_from:, FEATURE_COLS].to_numpy(dtype=np.float64)
        b = poisoned.loc[perturb_from:, FEATURE_COLS].to_numpy(dtype=np.float64)
        assert not np.allclose(a, b, equal_nan=True), "Perturbation erreicht den Builder nicht — Test ohne Kraft"
    print(f"OK  MIS add_advanced_features: Zeilen < {perturb_from} invariant unter Zukunfts-Vergiftung (beide Modi)")


def test_mis_multi_lookahead():
    """add_advanced_features_multi (Trainer-Pfad für Multi-Coin-Frames): dieselbe
    Invarianz je Symbol — Vergiftung der Zukunft beider Symbole darf die Zeilen
    davor nicht ändern (die Symbolgrenzen-Parität deckt test_mis_features.py)."""
    perturb_from = PROBE_T + 10
    a = make_mis_df(seed=21).assign(symbol="AAAUSDT")
    b = make_mis_df(seed=22).assign(symbol="BBBUSDT")
    a_p = poison_future(a, REQUIRED_INPUT_COLS, perturb_from)
    b_p = poison_future(b, REQUIRED_INPUT_COLS, perturb_from)

    base = add_advanced_features_multi(pd.concat([a, b], ignore_index=True))
    poisoned = add_advanced_features_multi(pd.concat([a_p, b_p], ignore_index=True))
    for sym in ("AAAUSDT", "BBBUSDT"):
        g_base = base[base["symbol"] == sym].reset_index(drop=True)
        g_pois = poisoned[poisoned["symbol"] == sym].reset_index(drop=True)
        assert_rows_invariant(g_base, g_pois, FEATURE_COLS, perturb_from, f"MIS-multi({sym})")
        # Canary wie in test_mis_lookahead: die vergiftete Region MUSS divergieren.
        a = g_base.loc[perturb_from:, FEATURE_COLS].to_numpy(dtype=np.float64)
        b = g_pois.loc[perturb_from:, FEATURE_COLS].to_numpy(dtype=np.float64)
        assert not np.allclose(a, b, equal_nan=True), f"Perturbation erreicht den Multi-Builder nicht ({sym})"
    print(f"OK  MIS add_advanced_features_multi: Zeilen < {perturb_from} je Symbol invariant")


# ── Research: df+idx-Builder ──────────────────────────────────────────────────
def test_candle_context_lookahead():
    df = make_context_df()
    # Alles > idx ist Zukunft — Vergiftung direkt ab idx+1 (strengster Schnitt).
    poisoned = poison_future(df, CONTEXT_VALUE_COLS, PROBE_T + 1)
    base = research_features.candle_context_features(df, PROBE_T)
    got = research_features.candle_context_features(poisoned, PROBE_T)
    assert_dicts_equal(base, got, "candle_context_features")
    print("OK  candle_context_features: idx-Zeile invariant, Zeilen > idx werden nicht gelesen")


def test_event_row_builders_lookahead():
    df = make_context_df()
    poisoned = poison_future(df, CONTEXT_VALUE_COLS, PROBE_T + 1)

    event = {"volume_ratio": 7.5, "price_change_60s": 2.4, "buy_pressure": 0.8, "volatility": 1.9}
    assert_dicts_equal(
        research_features.build_pex1_row(event, df, PROBE_T),
        research_features.build_pex1_row(event, poisoned, PROBE_T),
        "build_pex1_row",
    )

    rng = np.random.default_rng(3)
    stats = research_features.funding_stats(list(rng.normal(1e-4, 5e-5, 60)))
    assert_dicts_equal(
        research_features.build_fmr1_row(stats, 0.97, "SHORT", df, PROBE_T),
        research_features.build_fmr1_row(stats, 0.97, "SHORT", poisoned, PROBE_T),
        "build_fmr1_row",
    )

    regime_row = {"regime": "CHOP", "confidence": 0.7}
    ts = dt.datetime(2026, 1, 11, 14, 30)
    assert_dicts_equal(
        research_features.build_fif1_row("LONG", df, PROBE_T, regime_row, 12.0, 3, 5, ts),
        research_features.build_fif1_row("LONG", poisoned, PROBE_T, regime_row, 12.0, 3, 5, ts),
        "build_fif1_row",
    )
    print("OK  build_pex1_row / build_fmr1_row / build_fif1_row: invariant unter Zukunfts-Vergiftung")


# ── Funding: as-of-Builder ────────────────────────────────────────────────────
def make_funding_frame(n=120, seed=5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "funding_time": pd.date_range("2026-01-01", periods=n, freq="8h", tz="UTC"),
        "funding_rate": rng.normal(1e-4, 8e-5, n),
    })


def test_funding_asof_lookahead():
    g = make_funding_frame()
    ts = g["funding_time"].iloc[80] + pd.Timedelta(hours=3)  # zwischen zwei Settlements
    base = funding_features.funding_features_asof({"XUSDT": g}, "XUSDT", ts)
    assert base, "Baseline leer — Testaufbau kaputt (Historie zu kurz?)"

    poisoned = g.copy()
    future = poisoned["funding_time"] >= ts
    assert future.any()
    poisoned.loc[future, "funding_rate"] = PERTURB_VALUE
    got = funding_features.funding_features_asof({"XUSDT": poisoned}, "XUSDT", ts)
    assert_dicts_equal(base, got, "funding_features_asof")
    print("OK  funding_features_asof: Sätze mit funding_time >= ts fließen nicht ein")


def test_funding_asof_boundary_strict():
    """Ein Settlement EXAKT zum Ereigniszeitpunkt darf nicht einfließen
    (Kontrakt: 'STRIKT vor dem Ereigniszeitpunkt', Docstring core/funding_features.py)."""
    g = make_funding_frame()
    ts = g["funding_time"].iloc[80]  # exakt auf einem Settlement
    base = funding_features.funding_features_asof({"XUSDT": g}, "XUSDT", ts)

    poisoned = g.copy()
    poisoned.loc[poisoned["funding_time"] >= ts, "funding_rate"] = np.nan  # inkl. der Zeile AT ts
    got = funding_features.funding_features_asof({"XUSDT": poisoned}, "XUSDT", ts)
    assert_dicts_equal(base, got, "funding_features_asof(boundary)")
    print("OK  funding_features_asof: Settlement exakt AT ts bleibt draußen (strikt as-of)")


# ── Window-scoped Builder: Determinismus + Nicht-Mutation ─────────────────────
def test_rub_window_scoped():
    """rub_trend/build_rub_features bekommen per Signatur NUR das Lookback-Fenster
    (endet an der aktuellen Kerze) — es gibt keine Zukunfts-Achse im Input, der
    Leak-Surface ist der Caller-Slice (Bot 13 / Walkforward-Adapter)."""
    rng = np.random.default_rng(9)
    ts_sec = np.arange(0, 95 * 86400, 3600, dtype=np.float64)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.005, len(ts_sec))))
    ts_before, closes_before = ts_sec.copy(), closes.copy()

    a = rub_features.rub_trend(ts_sec, closes, float(closes[-1]))
    b = rub_features.rub_trend(ts_sec, closes, float(closes[-1]))
    assert a == b, "rub_trend nicht deterministisch"
    assert np.array_equal(ts_sec, ts_before) and np.array_equal(closes, closes_before), "rub_trend mutiert Input"

    f1 = rub_features.build_rub_features(a[0], a[1], float(closes[-1]), 28.0, -18.0, -12.0, 0.4, 0.3, 1.2, 95.0)
    f2 = rub_features.build_rub_features(a[0], a[1], float(closes[-1]), 28.0, -18.0, -12.0, 0.4, 0.3, 1.2, 95.0)
    assert f1 == f2 and set(f1) == set(rub_features.RUB_FEATURES)
    print("OK  rub_trend / build_rub_features: deterministisch, Input unmutiert (window-scoped)")


def test_trm1_window_contract():
    """build_trm1_row: Fenster endet am aktuellen Check (window-scoped, keine
    Zukunfts-Achse). Zusätzlich: Zeilen ÄLTER als das 12er-Fenster dürfen das
    Ergebnis nicht beeinflussen (interner [-TRM1_WINDOW_CHECKS:]-Slice)."""
    rng = np.random.default_rng(13)

    def mk_row(i):
        return {
            "regime": ["TRANSITION", "CHOP", "TREND_UP"][i % 3],
            "btc_return_1h": float(rng.normal(0, 0.5)),
            "btc_return_4h": float(rng.normal(0, 1.0)),
            "btc_atr_1h_pct": float(rng.uniform(0.1, 2.0)),
            "btc_atr_4h_pct": float(rng.uniform(0.5, 4.0)),
            "btcdom_return_24h": float(rng.normal(0, 1.0)),
            "confidence_btc": float(rng.uniform(0, 1)),
            "confidence_alt": float(rng.uniform(0, 1)),
        }

    rows = [mk_row(i) for i in range(20)]
    base = research_features.build_trm1_row(rows, 42.0)
    assert base == research_features.build_trm1_row(rows, 42.0), "build_trm1_row nicht deterministisch"

    older_poisoned = [{**r, "btc_return_4h": PERTURB_VALUE, "regime": "HIGH_VOLA"} for r in rows[:-12]] + rows[-12:]
    got = research_features.build_trm1_row(older_poisoned, 42.0)
    assert_dicts_equal(base, got, "build_trm1_row(older-than-window)")
    print("OK  build_trm1_row: deterministisch, Zeilen außerhalb des 12er-Fensters fließen nicht ein")


def test_funding_stats_window_contract():
    """funding_stats: Settlement-Liste endet per Kontrakt am 'jetzt' (window-scoped,
    Caller schneidet). Intern nutzt sie maximal die letzten FMR1_HISTORY_SETTLEMENTS
    Sätze — ältere Elemente dürfen das Ergebnis nicht beeinflussen."""
    rng = np.random.default_rng(23)
    rates = list(rng.normal(1e-4, 5e-5, 150))
    base = research_features.funding_stats(rates)
    assert base == research_features.funding_stats(list(rates)), "funding_stats nicht deterministisch"

    n_hist = research_features.FMR1_HISTORY_SETTLEMENTS
    older_poisoned = [PERTURB_VALUE] * (len(rates) - n_hist) + rates[-n_hist:]
    got = research_features.funding_stats(older_poisoned)
    assert_dicts_equal(base, got, "funding_stats(older-than-window)")
    print(f"OK  funding_stats: deterministisch, Sätze außerhalb der letzten {n_hist} fließen nicht ein")


def test_regime_features_row_scoped():
    """regime_features ist row-scoped (eine regime_history-Zeile + Alter rein,
    One-Hot-Dict raus) — keine Zeit-Achse; prüfbar: Determinismus + Nicht-Mutation."""
    row = {"regime": "TREND_UP", "confidence": 0.8}
    before = dict(row)
    a = research_features.regime_features(row, 15.0)
    b = research_features.regime_features(row, 15.0)
    assert a == b and row == before, "regime_features nicht deterministisch oder mutiert Input"
    assert a["regime_is_TREND_UP"] == 1.0 and a["regime_conf"] == 0.8
    print("OK  regime_features: deterministisch, row-scoped, Input unmutiert")


def test_rub_event_type_pure():
    """rub_event_type ist eine pure Skalar-Funktion (Vorfilter) — Determinismus."""
    args = (-0.10, 25.0, -20.0, 95.0, 96.0, 110.0)
    assert rub_features.rub_event_type(*args) == rub_features.rub_event_type(*args) == "REVERSION_UP"
    assert rub_features.rub_event_type(0.0, 50.0, 0.0, 100.0, 96.0, 110.0) is None
    print("OK  rub_event_type: pure, deterministisch")


def test_aim2_row_scoped():
    """aim2.build_feature_row ist row-scoped: eine Event-Zeile rein, ein
    Feature-Dict raus — kein Zeit-Series-Input, also keine Perturbations-Achse.
    Der floor-1-Join (letzte GESCHLOSSENE Kerze) ist per Vertrag Caller-Pflicht
    (Docstring core/aim2_features.py); hier prüfbar: Determinismus + keine
    Mutation der Input-Dicts."""
    market = {c: 101.0 for c in aim2_features.MARKET_PRICE_COLS}
    market.update({c: 0.5 for c in aim2_features.MARKET_ABS_COLS})
    market.update({c: 1.5 for c in aim2_features.ATR_COLS})
    market["trend_direction"] = "UP"
    regime = {"regime": "CHOP", "alt_context": "ALT_NEUTRAL", "confidence": 0.6, "btc_return_1h": 0.2}
    swarm = {"total_5d": 8, "long_5d": 5, "short_5d": 3, "latest_age_h": 2.5,
             "confl_same_dir_4h": 2, "distinct_src_same_dir_4h": 2}
    source = {"name": "Fast Bot", "type": "conv", "conf": 0.65, "trail_wr_30d": 0.6,
              "trail_n_30d": 12, "entry_drift_pct": 0.1, "direction": "LONG"}
    inputs_before = (dict(market), dict(regime), dict(swarm), dict(source))

    a = aim2_features.build_feature_row(market, 100.0, regime, 30.0, swarm, source)
    b = aim2_features.build_feature_row(market, 100.0, regime, 30.0, swarm, source)
    assert a == b, "build_feature_row nicht deterministisch"
    assert (market, regime, swarm, source) == inputs_before, "build_feature_row mutiert Input-Dicts"
    print("OK  aim2.build_feature_row: deterministisch, row-scoped, Inputs unmutiert")


def test_sra2_row_scoped():
    """sra.build_sra2_features ist row-scoped: eine 1h-Indikator-Zeile rein, ein
    Feature-Dict raus — kein Zeit-Series-Input, also keine Perturbations-Achse.
    Der floor-1-Join (letzte GESCHLOSSENE Kerze) ist Caller-Pflicht (9_ai_sr_bot /
    tools/retrain_sra2.py); hier prüfbar: Determinismus, keine Mutation der
    Input-Zeile und der Key-Vertrag gegen SRA2_FEATURES (die Bot==Trainer-Parität
    aus T-2026-CU-9050-042 haftet an genau diesem Schlüssel-Set)."""
    ind = {
        "close": 101.0, "atr_14": 1.5,
        "rsi_9": 55.0, "rsi_14": 52.0, "rsi_24": 48.0,
        "tsi_fast_12_7_7": 0.3, "tsi_fast_12_7_7_signal": 0.2,
        "macd_dif_fast_9_21_9": 0.4, "macd_dea_fast_9_21_9": 0.35,
        "r_squared": 0.7, "trend_direction": "UP",
        "ema_9": 100.5, "ema_21": 99.8, "wma_9": 100.2, "kama_9": 100.1, "kama_21": 99.5,
        "support_price": 97.0, "resistance_price": 105.0, "boll_mid_20": 100.0,
        "boll_upper_20": 103.0, "boll_lower_20": 97.5,
    }
    ind_before = dict(ind)

    a = sra_features.build_sra2_features(ind)
    b = sra_features.build_sra2_features(ind)
    assert a == b, "build_sra2_features nicht deterministisch"
    assert ind == ind_before, "build_sra2_features mutiert die Input-Zeile"
    assert set(a) == set(sra_features.SRA2_FEATURES), (
        f"build_sra2_features bricht den SRA2-Key-Vertrag ({set(a) ^ set(sra_features.SRA2_FEATURES)})"
    )
    print("OK  sra.build_sra2_features: deterministisch, row-scoped, Input unmutiert, Key-Vertrag")


# ── fetch_context_frame: R1 / Forming-Candle via Fake-Reader ─────────────────
# Nach der Block-5-Umverdrahtung liest fetch_context_frame über core.candles
# (read_candles_with_indicators, include_forming=False) — die forming Kerze
# fällt schon im Read raus (DB-Uhr-Cutoff, mechanisch getestet in
# test_candles.py), nicht mehr erst im floor-1-Join. Der Fake-Reader unten
# (_fake_reader) bildet den include_forming-Cutoff pandas-seitig nach; er wird
# via monkeypatch von research_features.read_candles_with_indicators eingehängt,
# analog zu den walkforward-Loadern. Der Test bleibt damit DB-frei.
_CTX_COLS = ["open_time", "close", "volume", "rsi_14", "ema_21", "ema_200", "atr_14", "boll_upper_20", "boll_lower_20"]


def _ctx_frame_asc(n=60, tf="1h", end_offset_h=0, seed=17):
    """ASC 1h-Frame, now-relativ: n geschlossene Kerzen PLUS die laufende
    (open_time == period_start) — so wie die per-Coin-Tabelle sie heute wirklich
    enthält. ``end_offset_h`` schiebt das ganze Fenster um h Stunden in die
    Vergangenheit (für den Staleness-Fall)."""
    from core import candles

    forming_open = candles.period_start(tf, dt.datetime.now(dt.timezone.utc)) - dt.timedelta(hours=end_offset_h)
    step = candles.timeframe_delta(tf)
    times = [forming_open - i * step for i in range(n, -1, -1)]  # ASC, letzte Zeile = forming
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(times))))
    df = pd.DataFrame({
        "open_time": times,
        "close": close,
        "volume": rng.uniform(1000, 50000, len(times)),
        "rsi_14": rng.uniform(20, 80, len(times)),
        "ema_21": close * 1.01,
        "ema_200": close * 0.99,
        "atr_14": close * 0.02,
        "boll_upper_20": close * 1.03,
        "boll_lower_20": close * 0.97,
    })
    return df[_CTX_COLS], forming_open


def _run_fetch_context(frame, as_of):
    """fetch_context_frame mit eingehängtem Fake-Reader ausführen; gibt
    (result, calls) zurück."""
    calls: list[dict] = []
    original = research_features.read_candles_with_indicators
    try:
        research_features.read_candles_with_indicators = _fake_reader(frame, calls)
        res = research_features.fetch_context_frame(object(), "TESTUSDT", as_of=as_of)
    finally:
        research_features.read_candles_with_indicators = original
    return res, calls


def test_fetch_context_frame_ignores_forming_candle():
    """R1: die forming Kerze der laufenden Stunde darf weder die gewählte
    Feature-Kerze noch deren Features speisen. Nach Block 5 droppt sie der Read
    (include_forming=False) — ihr Wert ist irrelevant. Gegenprobe: eine
    vergiftete forming Zeile ändert das Ergebnis nicht."""
    frame, forming_open = _ctx_frame_asc()
    as_of = dt.datetime.now(dt.timezone.utc)  # Entscheidung in der laufenden Stunde
    poisoned = frame.copy()
    poisoned.iloc[-1, 1:] = PERTURB_VALUE  # forming Kerze (letzte Zeile) vergiften

    (res_a, calls_a) = _run_fetch_context(frame, as_of)
    (res_b, _) = _run_fetch_context(poisoned, as_of)
    assert res_a is not None and res_b is not None, "fetch_context_frame lieferte None trotz ausreichender Historie"
    df_a, idx_a = res_a
    df_b, idx_b = res_b

    # Der Read MUSS geschlossen-only angefordert worden sein (mechanische R1-Prüfung).
    assert calls_a and calls_a[0]["include_forming"] is False, "fetch_context_frame liest die forming Kerze"
    assert calls_a[0]["tf"] == "1h", f"falscher Timeframe angefordert: {calls_a[0]['tf']}"
    assert calls_a[0]["indicator_columns"] == research_features.CONTEXT_IND_COLS, (
        "Indikator-Spalten weichen von der geteilten Quelle ab (harte Regel 7)"
    )

    forming_naive = pd.Timestamp(forming_open).tz_convert("UTC").tz_localize(None)
    last_closed = forming_naive - pd.Timedelta(hours=1)
    for df, idx, label in ((df_a, idx_a, "sauber"), (df_b, idx_b, "vergiftet")):
        chosen = df["open_time"].iloc[idx]
        assert chosen < forming_naive, f"Feature-Kerze ({label}) nicht strikt vor der as_of-Stunde: {chosen}"
        assert chosen == last_closed, f"floor-1-Join wählt nicht die letzte geschlossene Kerze ({label}): {chosen}"

    assert_dicts_equal(
        research_features.candle_context_features(df_a, idx_a),
        research_features.candle_context_features(df_b, idx_b),
        "fetch_context_frame(forming candle)",
    )
    print("OK  fetch_context_frame: forming Kerze der laufenden Stunde ausgeschlossen (include_forming=False)")


def test_fetch_context_frame_staleness_guard():
    """Feature-Kerze älter als CONTEXT_MAX_STALENESS_H → None (Training hätte
    das Event verworfen; Live darf es kein Signal speisen)."""
    frame, _ = _ctx_frame_asc(end_offset_h=6)  # jüngste geschlossene Kerze ~6h alt
    as_of = dt.datetime.now(dt.timezone.utc)  # 6h Lücke > 3h
    got, _ = _run_fetch_context(frame, as_of)
    assert got is None, "Staleness-Guard greift nicht (stale Feature-Kerze wurde geliefert)"
    print("OK  fetch_context_frame: Staleness-Guard (>3h) liefert None")


# ── walkforward_sim: Loader-Kontrakt (die einzige Label-Quelle des Retrains) ──
def _import_walkforward_sim():
    """`tools/walkforward_sim.py` ist DB-frei importierbar, sobald die zwei
    Pflicht-Secrets aus core.config gesetzt sind — die Verbindung baut erst
    main() auf. Die Loader lassen sich damit ohne DB gegen ihren Kontrakt testen."""
    os.environ.setdefault("DB_PASSWORD", "test")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
    import tools.walkforward_sim as wfs

    return wfs


def _closed_plus_forming(tf: str, cols: list[str], n: int = 40) -> pd.DataFrame:
    """Frame aus n geschlossenen Kerzen PLUS der aktuell laufenden (open_time ==
    period_start), so wie die per-Coin-Tabelle sie heute wirklich enthält."""
    from core import candles

    forming_open = candles.period_start(tf, dt.datetime.now(dt.timezone.utc))
    step = candles.timeframe_delta(tf)
    times = [forming_open - i * step for i in range(n, -1, -1)]
    df = pd.DataFrame({"open_time": times})
    for c in cols:
        if c != "open_time":
            df[c] = 1.0 if c != "trend_direction" else "UP"
    return df


def _fake_reader(frame: pd.DataFrame, calls: list[dict]):
    """Ersetzt core.candles.read_candles* durch eine pandas-seitige Nachbildung
    des `include_forming`-Cutoffs (die echte SQL braucht libpq zum Rendern —
    siehe backtest/test_candles.py). Prüft damit BEIDES: dass der Loader den
    Kontrakt richtig aufruft und dass die laufende Kerze wirklich rausfällt."""
    from core import candles

    def _read(conn, symbol, tf, **kw):
        calls.append({"symbol": symbol, "tf": tf, **kw})
        df = frame
        if not kw.get("include_forming", False):
            cutoff = candles.period_start(tf, dt.datetime.now(dt.timezone.utc))
            df = df[df["open_time"] < cutoff]
        return df.reset_index(drop=True)

    return _read


def test_walkforward_loaders_drop_the_forming_candle():
    """`load_ohlcv` / `load_joined` speisen die Labels JEDES Retrains (P0.10).
    Die laufende Kerze darf dort nie als geschlossen ankommen — sonst trägt
    jedes daraus trainierte Modell einen Look-ahead."""
    wfs = _import_walkforward_sim()
    original = wfs.read_candles
    try:
        for tf in ("1h", "4h", "1d"):
            frame = _closed_plus_forming(tf, list(wfs.OHLCV_COLUMNS))
            forming_open = pd.Timestamp(frame["open_time"].iloc[-1])
            calls: list[dict] = []
            wfs.read_candles = _fake_reader(frame, calls)
            got = wfs.load_ohlcv(object(), "TESTUSDT", tf, days=30)

            assert calls and calls[0]["include_forming"] is False, f"load_ohlcv({tf}) liest die forming Kerze"
            assert got is not None and len(got) == len(frame) - 1
            assert (got["open_time"] < forming_open).all(), (
                f"load_ohlcv({tf}): forming Kerze im Replay-Frame — Look-ahead in der Label-Quelle"
            )
    finally:
        wfs.read_candles = original
    print("OK  load_ohlcv: forming Kerze fällt für 1h/4h/1d raus (include_forming=False)")


def test_walkforward_joined_loader_drops_the_forming_candle():
    """Gleiche Invariante für den Sniper-Join (td/bb-Adapter, 1h + 4h)."""
    wfs = _import_walkforward_sim()
    original = wfs.read_candles_with_indicators
    try:
        for tf in ("1h", "4h"):
            frame = _closed_plus_forming(tf, list(wfs.OHLCV_COLUMNS) + wfs.SNIPER_JOIN_INDICATORS)
            forming_open = pd.Timestamp(frame["open_time"].iloc[-1])
            calls: list[dict] = []
            wfs.read_candles_with_indicators = _fake_reader(frame, calls)
            got = wfs.load_joined(object(), "TESTUSDT", tf, days=30)

            assert calls and calls[0]["include_forming"] is False, f"load_joined({tf}) liest die forming Kerze"
            assert calls[0]["indicator_columns"] == wfs.SNIPER_JOIN_INDICATORS
            assert got is not None and len(got) == len(frame) - 1
            assert (got["open_time"] < forming_open).all(), (
                f"load_joined({tf}): forming Kerze im Replay-Frame — Look-ahead in der Label-Quelle"
            )
    finally:
        wfs.read_candles_with_indicators = original
    print("OK  load_joined: forming Kerze fällt für 1h/4h raus (include_forming=False)")


WARMUP_ROWS = 12  # so viele Kopfzeilen haben noch keinen ema_200 (Warmup der DB-Indikatoren)


def test_walkforward_joined_loader_never_backfills_warmup_indicators():
    """Zweiter Look-ahead derselben Funktion (T-2026-CU-9050-045): `ffill` schließt
    Innen-Lücken aus der Vergangenheit, ein `bfill` danach würde die Warmup-Kopfzeilen
    aus der ZUKUNFT füllen. `ema_200` ist am Anfang jeder Coin-Historie NULL, und
    `run_td_bb` emittiert schon ab `t=149` — der Leak landet also, anders als die
    forming Kerze, in GELABELTEN Trainingszeilen. Erwartung: Kopfzeilen verworfen,
    nie mit einem späteren Wert gefüllt."""
    wfs = _import_walkforward_sim()
    original = wfs.read_candles_with_indicators
    try:
        frame = _closed_plus_forming("1h", list(wfs.OHLCV_COLUMNS) + wfs.SNIPER_JOIN_INDICATORS)
        # ema_200 fehlt im Warmup; die erste echte Zeile trägt einen unverwechselbaren Wert.
        frame.loc[: WARMUP_ROWS - 1, "ema_200"] = np.nan
        frame.loc[WARMUP_ROWS:, "ema_200"] = 4711.0
        first_real_open = pd.Timestamp(frame["open_time"].iloc[WARMUP_ROWS])

        wfs.read_candles_with_indicators = _fake_reader(frame, [])
        got = wfs.load_joined(object(), "TESTUSDT", "1h", days=30)

        assert got is not None and not got["ema_200"].isna().any()
        assert (got["open_time"] >= first_real_open).all(), (
            "load_joined: Warmup-Kopfzeile überlebt — ihr ema_200 kann nur aus der Zukunft stammen"
        )
        # closed = len(frame)-1 (forming), davon fallen die WARMUP_ROWS Kopfzeilen weg.
        assert len(got) == len(frame) - 1 - WARMUP_ROWS, (
            f"erwartet {len(frame) - 1 - WARMUP_ROWS} Zeilen, bekommen {len(got)} — bfill füllt noch"
        )
    finally:
        wfs.read_candles_with_indicators = original
    print(f"OK  load_joined: {WARMUP_ROWS} Warmup-Kopfzeilen verworfen statt aus der Zukunft gefüllt")


if __name__ == "__main__":
    # cp1252-Konsole (Windows): Sonderzeichen nicht crashen lassen
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    test_mis_lookahead()
    test_mis_multi_lookahead()
    test_candle_context_lookahead()
    test_event_row_builders_lookahead()
    test_funding_asof_lookahead()
    test_funding_asof_boundary_strict()
    test_rub_window_scoped()
    test_trm1_window_contract()
    test_funding_stats_window_contract()
    test_regime_features_row_scoped()
    test_rub_event_type_pure()
    test_aim2_row_scoped()
    test_sra2_row_scoped()
    test_fetch_context_frame_ignores_forming_candle()
    test_fetch_context_frame_staleness_guard()
    test_walkforward_loaders_drop_the_forming_candle()
    test_walkforward_joined_loader_drops_the_forming_candle()
    test_walkforward_joined_loader_never_backfills_warmup_indicators()
    print("\nAlle Look-ahead-Perturbationstests bestanden — kein Future-Leak in den geteilten Feature-Buildern.")
