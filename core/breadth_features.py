# core/breadth_features.py
"""Geteilter Markt-Breadth/Dispersion-Feature-Builder (X-R1) — EINE Quelle für
Studie, Trainer und (später) Bot/Orchestrator.

Herkunft: docs/MODEL_CANDIDATES_SPEC_2026-07.md §K6 (BRD). Die Hypothese: Breadth-
Größen über das ~530er USDT-Perp-Universum (Anteil Coins > EMA200/EMA50,
Median-7d-Return, Advance/Decline, Return-Dispersion vs. BTC) ergänzen die
BTC-only-Regime-Klassifikation und liefern potenziell das fehlende Regime-Gate
für RUB-LONG. Wie core/funding_features.py und core/aim2_features.py ist dieser
Builder kanonisch: Studie, Trainer und Bot rechnen exakt dieselben Größen (kein
Train/Serve-Skew).

Datenquelle offline: die per-Coin ``{SYM}_1d``-Kerzen + ``{SYM}_1d_indicators``
(EMA50/EMA200 liegen dort vor) über core.candles.read_candles_with_indicators.

Effizienz (Pflicht, §K6): EINE Query je Coin (``load_universe_panels``), danach
wird das gesamte Cross-Section-Gerüst EINMAL in-memory gebaut
(``build_breadth_panel``); die As-of-Auswertung (``breadth_features_asof``) ist
danach ein O(log n)-Lookup in dieses vorberechnete Tagespanel — es werden NICHT
530 Tabellen je Zeitpunkt einzeln angefragt. Prozess-Priorität BELOW_NORMAL setzt
der Aufrufer (tools/walkforward_sim.set_low_priority).

As-of-Vertrag (R1, nur geschlossene Kerzen): ein Tagesbalken mit open_time D
schließt erst bei D + 1d. ``breadth_features_asof(panel, ts)`` liefert deshalb den
jüngsten Tagesbalken D mit D + tf <= ts — kein Lookahead. Der Load nutzt
``include_forming=False``.

Feature-Vertrag (X-R1): fehlende SPALTEN im geladenen Frame ⇒ ``BreadthFeatureError``
(Load-Fehler), NIE ``fillna(0)`` als Vertragsersatz. Fehlende WERTE (Coin mit zu
kurzer Historie, delisteter Coin ohne Tabelle) sind KEIN Vertragsbruch: der Coin
fällt an diesem Zeitpunkt schlicht aus der Cross-Section (Ausschluss, nicht Null).
Die beitragende Coin-Zahl steht als Diagnose-Größe ``brd_n_universe`` in jeder
Zeile.

TOTAL3-Proxy — EHRLICHKEITS-HINWEIS (§K6-Addendum): Wir haben KEINE echten
Marktkapitalisierungs-Gewichte. Der hier gebaute Preis-Index über das Universum
OHNE BTC/ETH ist ein PROXY für den realen TOTAL3-Index (Altcoin-Marktkap ohne
BTC/ETH). Zwei Varianten:
  * gleichgewichtet (EW): jeder Alt-Coin trägt seinen Tagesrendite-Beitrag gleich.
  * volumengewichtet (VW): Gewicht = Tages-Turnover-Proxy (close·volume, USD-nah,
    aber Basisvolumen·Preis, keine echte Quote-Volume-Spalte).
Beide Indizes sind Rendite-verkettete Levels (Basis 100), NICHT ein realer
Marktkap-Index. Der Praktiker-Gate-Gedanke „Alt-Trades nur wenn TOTAL3 über Level"
(KB ingest-c1e5112dea7f) ist damit als Proxy testbar, aber als Proxy zu
dokumentieren — nie als echter TOTAL3 auszugeben.

Survivorship (Regel 9): coins.json führt die AKTIVEN USDT-Perps; delistete Coins
fehlen teils. Jede Breadth-Zeile ist damit über ein survivorship-verzerrtes
Universum gerechnet — bekannte, dokumentierte Bias-Quelle.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from core.candles import TF_SECONDS, read_candles_with_indicators


class BreadthFeatureError(RuntimeError):
    """X-R1-Vertragsbruch: ein geladener Frame hat nicht die Pflicht-Spalten."""


#: Spalten, die jeder geladene Panel-Frame tragen MUSS (sonst Load-Fehler).
REQUIRED_COLUMNS: tuple[str, ...] = ("open_time", "close", "volume", "ema_50", "ema_200")

#: Indikator-Spalten, die aus ``{SYM}_{tf}_indicators`` gezogen werden.
PANEL_INDICATOR_COLS: tuple[str, ...] = ("ema_50", "ema_200")

#: Der Breadth/Dispersion-Feature-Vertrag (kanonische Namen, Reihenfolge fix).
BREADTH_FEATURES: list[str] = [
    "brd_pct_above_ema200",  # Anteil Coins mit close > EMA200 (as-of)
    "brd_pct_above_ema50",  # Anteil Coins mit close > EMA50 (as-of)
    "brd_median_ret_7d",  # Median 7d-Return über das Universum
    "brd_adv_decline_ratio",  # Advancer/Decliner des jüngsten Tagesbalkens
    "brd_dispersion_vs_btc",  # Cross-Section-StdAbw von (7d-Return − BTC-7d-Return)
    "total3_ew_level",  # EW-Preis-Index (Proxy) Level, Basis 100
    "total3_ew_dist_reg90d",  # Abstand zur 90d-Regressionslinie (EW), relativ
    "total3_ew_breakout",  # EW-Level > 90d-Vorlauf-Hoch (1/0)
    "total3_vw_level",  # VW-Preis-Index (Proxy) Level, Basis 100
    "total3_vw_dist_reg90d",  # Abstand zur 90d-Regressionslinie (VW), relativ
    "total3_vw_breakout",  # VW-Level > 90d-Vorlauf-Hoch (1/0)
]

#: Diagnose-Größe (nicht Teil des Feature-Vertrags), pro Zeile mitgeführt.
DIAGNOSTIC_COLUMNS: list[str] = ["brd_n_universe"]

RET_LOOKBACK_BARS = 7  # 7 Tagesbalken → 7d-Return
REG_WINDOW_BARS = 90  # 90 Tagesbalken → 90d-Regression / Breakout-Fenster
INDEX_BASE = 100.0
BTC_SYMBOL = "BTCUSDT"
#: Aus dem TOTAL3-Proxy AUSGESCHLOSSEN (Definition des realen TOTAL3).
EXCLUDED_FROM_TOTAL3: frozenset[str] = frozenset({"BTCUSDT", "ETHUSDT"})


def load_universe_panels(
    conn: Any,
    symbols: list[str],
    *,
    tf: str = "1d",
    start: Any | None = None,
) -> dict[str, pd.DataFrame]:
    """Lädt je Coin EINE geschlossene Kerzen+Indikator-Historie (aufsteigend).

    Eine Query je Coin (read_candles_with_indicators, include_forming=False). Coins
    ohne Tabelle/Daten (delisted, Survivorship) werden übersprungen — das ist KEIN
    Vertragsbruch. Fehlt einem GELADENEN Frame eine Pflicht-Spalte, ist das ein
    X-R1-Load-Fehler (BreadthFeatureError), nie fillna(0).

    Rückgabe: {symbol -> DataFrame[open_time(UTC, tz-aware), close, volume, ema_50,
    ema_200]}, aufsteigend nach open_time.
    """
    panels: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df = read_candles_with_indicators(
                conn,
                sym,
                tf,
                start=start,
                include_forming=False,
                candle_columns=("open_time", "close", "volume"),
                indicator_columns=list(PANEL_INDICATOR_COLS),
            )
        except Exception:
            # Fehlende per-Coin-Tabelle o.ä. → Survivorship, überspringen.
            conn.rollback()
            continue
        if df.empty:
            continue
        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise BreadthFeatureError(
                f"{sym}_{tf}: fehlende Pflicht-Spalten {missing} — X-R1-Vertrag, kein fillna(0)"
            )
        df = df.copy()
        df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
        for col in ("close", "volume", "ema_50", "ema_200"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"]).sort_values("open_time").reset_index(drop=True)
        if df.empty:
            continue
        panels[sym] = df[["open_time", "close", "volume", "ema_50", "ema_200"]]
    if not panels:
        raise BreadthFeatureError("keine verwertbaren Panels geladen (Universum leer?)")
    return panels


def _wide_field(panels: dict[str, pd.DataFrame], field: str) -> pd.DataFrame:
    """Baut eine (Datum × Symbol)-Matrix eines Feldes; Union-Index, NaN wo fehlend."""
    series = {sym: df.set_index("open_time")[field] for sym, df in panels.items()}
    wide = pd.DataFrame(series).sort_index()
    # Doppel-open_times je Coin (dürfte nicht vorkommen) → letzte gewinnt.
    return wide[~wide.index.duplicated(keep="last")]


def _rolling_reg_distance(level: pd.Series, window: int) -> pd.Series:
    """Relativer Abstand des Levels zu seiner eigenen rollierenden OLS-Linie.

    Für jede Position i (ab window-1) OLS von level[i-window+1 : i+1] gegen
    x = 0..window-1, Vorhersage am rechten Rand, dist = (level - pred) / pred.
    NaN, solange das Fenster nicht voll / ein NaN enthält.
    """
    vals = level.to_numpy(dtype=float)
    n = len(vals)
    out = np.full(n, np.nan)
    if n < window:
        return pd.Series(out, index=level.index)
    x = np.arange(window, dtype=float)
    xm = x.mean()
    xd = x - xm
    denom = float((xd * xd).sum())
    x_last = x[-1]
    for i in range(window - 1, n):
        y = vals[i - window + 1 : i + 1]
        if np.isnan(y).any():
            continue
        ym = y.mean()
        slope = float((xd * (y - ym)).sum()) / denom
        intercept = ym - slope * xm
        pred = slope * x_last + intercept
        if pred != 0:
            out[i] = (vals[i] - pred) / pred
    return pd.Series(out, index=level.index)


def _rolling_breakout(level: pd.Series, window: int) -> pd.Series:
    """1.0 wenn das Level das Hoch der VORHERIGEN ``window`` Balken überschreitet."""
    prior_max = level.shift(1).rolling(window).max()
    flag = (level > prior_max).astype(float)
    return flag.where(prior_max.notna())


def _index_levels(
    daily_ret: pd.DataFrame,
    turnover: pd.DataFrame,
    alt_cols: list[str],
) -> tuple[pd.Series, pd.Series]:
    """EW- und VW-Preis-Index (Proxy, Basis 100) über die Alt-Coins.

    Rendite-verkettet: ein Tag ohne Daten trägt flach (Rendite 0) — das ist
    Index-Konstruktion, kein Feature-fillna. Die VW-Gewichte kommen aus dem
    Turnover-Proxy (close·volume) desselben Tages.
    """
    if not alt_cols:
        empty = pd.Series(np.nan, index=daily_ret.index)
        return empty, empty
    alt_ret = daily_ret[alt_cols]
    ew_daily = alt_ret.mean(axis=1, skipna=True)
    ew_level = (1.0 + ew_daily.fillna(0.0)).cumprod() * INDEX_BASE

    turn = turnover[alt_cols]
    weights = turn.div(turn.sum(axis=1), axis=0)
    vw_daily = (alt_ret * weights).sum(axis=1, min_count=1)
    vw_level = (1.0 + vw_daily.fillna(0.0)).cumprod() * INDEX_BASE
    return ew_level, vw_level


def build_breadth_panel(panels: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Baut das Tages-Cross-Section-Panel aller Breadth/Dispersion-Features EINMAL.

    Index = Tages-open_time (UTC, tz-aware). Spalten = BREADTH_FEATURES +
    DIAGNOSTIC_COLUMNS. Danach ist ``breadth_features_asof`` nur noch ein Lookup.
    """
    close_wide = _wide_field(panels, "close")
    ema200_wide = _wide_field(panels, "ema_200")
    ema50_wide = _wide_field(panels, "ema_50")
    vol_wide = _wide_field(panels, "volume")
    turnover = close_wide * vol_wide

    # % über EMA200 / EMA50 (nur Coins mit beiden Werten zählen).
    valid200 = close_wide.notna() & ema200_wide.notna() & (ema200_wide > 0)
    valid50 = close_wide.notna() & ema50_wide.notna() & (ema50_wide > 0)
    above200 = (close_wide > ema200_wide) & valid200
    above50 = (close_wide > ema50_wide) & valid50
    n200 = valid200.sum(axis=1)
    n50 = valid50.sum(axis=1)
    pct_above_ema200 = above200.sum(axis=1) / n200.where(n200 > 0)
    pct_above_ema50 = above50.sum(axis=1) / n50.where(n50 > 0)

    daily_ret = close_wide.pct_change()
    ret7d = close_wide / close_wide.shift(RET_LOOKBACK_BARS) - 1.0
    median_ret_7d = ret7d.median(axis=1, skipna=True)

    adv = (daily_ret > 0).sum(axis=1)
    dec = (daily_ret < 0).sum(axis=1)
    adv_decline_ratio = adv / dec.where(dec > 0)

    if BTC_SYMBOL in ret7d.columns:
        rel = ret7d.sub(ret7d[BTC_SYMBOL], axis=0)
        dispersion_vs_btc = rel.std(axis=1, skipna=True)
    else:
        dispersion_vs_btc = pd.Series(np.nan, index=close_wide.index)

    alt_cols = [c for c in close_wide.columns if c not in EXCLUDED_FROM_TOTAL3]
    ew_level, vw_level = _index_levels(daily_ret, turnover, alt_cols)

    n_universe = close_wide.notna().sum(axis=1)

    panel = pd.DataFrame(
        {
            "brd_pct_above_ema200": pct_above_ema200,
            "brd_pct_above_ema50": pct_above_ema50,
            "brd_median_ret_7d": median_ret_7d,
            "brd_adv_decline_ratio": adv_decline_ratio,
            "brd_dispersion_vs_btc": dispersion_vs_btc,
            "total3_ew_level": ew_level,
            "total3_ew_dist_reg90d": _rolling_reg_distance(ew_level, REG_WINDOW_BARS),
            "total3_ew_breakout": _rolling_breakout(ew_level, REG_WINDOW_BARS),
            "total3_vw_level": vw_level,
            "total3_vw_dist_reg90d": _rolling_reg_distance(vw_level, REG_WINDOW_BARS),
            "total3_vw_breakout": _rolling_breakout(vw_level, REG_WINDOW_BARS),
            "brd_n_universe": n_universe.astype(float),
        },
        index=close_wide.index,
    )
    return panel.sort_index()


def breadth_features_asof(panel: pd.DataFrame, ts_utc: Any, *, tf: str = "1d") -> dict:
    """Die Breadth-Features as-of ``ts_utc`` (tz-aware oder naiv=UTC).

    Liefert den jüngsten Tagesbalken D mit D + tf <= ts (nur geschlossene Kerzen,
    kein Lookahead). Rückgabe {} bei leerem Panel oder wenn ts vor der ersten
    verwertbaren Zeile liegt. Werte, die NaN sind, kommen als ``None`` zurück (der
    Aufrufer entscheidet — Trainer verwirft die Zeile; ein Gate fail-closed/-open) —
    NIE als 0.
    """
    if panel.empty:
        return {}
    ts = pd.Timestamp(ts_utc)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    cutoff = ts - pd.Timedelta(seconds=TF_SECONDS[tf])
    idx = int(panel.index.searchsorted(cutoff, side="right")) - 1
    if idx < 0:
        return {}
    row = panel.iloc[idx]
    return {col: (float(row[col]) if pd.notna(row[col]) else None) for col in panel.columns}
