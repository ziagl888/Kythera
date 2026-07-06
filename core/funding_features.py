# core/funding_features.py
"""Geteilter Funding-Feature-Builder — EINE Quelle für Studien, Trainer und Bots.

Herkunft: Report 21 Addendum 2 (ABR1-LONG-Studie 2026-07-06). Die dort
validierten Feature-Definitionen sind hier kanonisch festgehalten, damit
kommende Retrains (RUB2, EPD2, …) exakt dieselben Größen benutzen wie die
Studie und der Live-Bot (kein Train/Serve-Skew — gleiche Regel wie
core/mis_features.py und core/aim2_features.py).

Datenquelle offline: Tabelle ``funding_rates`` (voll backfillt via
``tools/backfill_funding_rates.py``; 8h-Raster). Live holt der Bot dieselben
Werte per REST (siehe 18_ai_abr1_bot.get_funding_24h_bps).

Alle Features sind as-of: es gehen nur Funding-Sätze ein, deren funding_time
STRIKT vor dem Ereigniszeitpunkt liegt — kein Lookahead.

Validierte Schwellen (ABR-Familie, Stand 2026-07-06):
  * LONG-Gate:   fund_24h > +3,0 bps  (+1,12 %/Trade, 74 % WR)
  * SHORT-Veto:  fund_24h > +1,5 bps  (−1,21 %/Trade in der Zone)
Referenz: Binance-Default-Funding = +1,0 bps/8h — dort kleben ~75 % der Werte,
das Signal steckt STRIKT darüber/darunter.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FUNDING_FEATURES = [
    "fund_last",      # letzter abgerechneter Satz (bps)
    "fund_24h",       # Mittel letzte 3 Sätze (bps) — Gate-/Veto-Größe
    "fund_72h",       # Mittel letzte 9 Sätze (bps)
    "fund_7d_cum",    # Summe letzte 21 Sätze (bps)
    "fund_pctl_90d",  # Perzentil des letzten Satzes vs. eigene 90d-Historie
    "fund_trend",     # fund_24h − fund_72h (bps)
]

#: Mindestanzahl historischer Sätze, bevor Features berechnet werden (7 Tage).
MIN_HISTORY = 21


def load_funding(conn, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Lädt die Funding-Historie je Symbol aus ``funding_rates`` (aufsteigend)."""
    fr = pd.read_sql_query(
        "SELECT symbol, funding_time, funding_rate FROM funding_rates "
        "WHERE symbol = ANY(%(syms)s) ORDER BY symbol, funding_time",
        conn, params={"syms": list(symbols)},
    )
    fr["funding_time"] = pd.to_datetime(fr["funding_time"], utc=True)
    return {s: g.reset_index(drop=True) for s, g in fr.groupby("symbol")}


def funding_features_asof(by_sym: dict[str, pd.DataFrame], symbol: str, ts_utc) -> dict:
    """Die 6 FUNDING_FEATURES für ein Ereignis zu Zeitpunkt ``ts_utc`` (tz-aware).

    Rückgabe {} wenn Symbol fehlt oder Historie < MIN_HISTORY — der Aufrufer
    entscheidet (Trainer: Zeile verwerfen; Gate: fail-closed/-open je Politik).
    """
    g = by_sym.get(symbol)
    if g is None:
        return {}
    i = int(np.searchsorted(g["funding_time"].values, np.datetime64(pd.Timestamp(ts_utc))))
    if i < MIN_HISTORY:
        return {}
    rates = g["funding_rate"].values[:i] * 1e4  # → bps
    last, m3, m9 = rates[-1], rates[-3:].mean(), rates[-9:].mean()
    hist90 = rates[-270:]
    return {
        "fund_last": float(last),
        "fund_24h": float(m3),
        "fund_72h": float(m9),
        "fund_7d_cum": float(rates[-21:].sum()),
        "fund_pctl_90d": float((hist90 <= last).mean() * 100),
        "fund_trend": float(m3 - m9),
    }
