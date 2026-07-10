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

import datetime

import numpy as np
import pandas as pd

FUNDING_FEATURES = [
    "fund_last",  # letzter abgerechneter Satz (bps)
    "fund_24h",  # Mittel letzte 3 Sätze (bps) — Gate-/Veto-Größe
    "fund_72h",  # Mittel letzte 9 Sätze (bps)
    "fund_7d_cum",  # Summe letzte 21 Sätze (bps)
    "fund_pctl_90d",  # Perzentil des letzten Satzes vs. eigene 90d-Historie
    "fund_trend",  # fund_24h − fund_72h (bps)
]

#: Mindestanzahl historischer Sätze, bevor Features berechnet werden (7 Tage).
MIN_HISTORY = 21


def load_funding(conn, symbols: list[str], since=None) -> dict[str, pd.DataFrame]:
    """Lädt die Funding-Historie je Symbol aus ``funding_rates`` (aufsteigend).

    since: optionale Untergrenze (tz-aware). Live-Bots begrenzen damit den
    Load — funding_features_asof nutzt maximal die letzten 270 Sätze (~90d),
    die volle Historie je Trigger zu ziehen ist verschenkte DB-Arbeit.
    Trainer/Replays lassen since weg (as-of über den gesamten Zeitraum).
    """
    query = "SELECT symbol, funding_time, funding_rate FROM funding_rates WHERE symbol = ANY(%(syms)s)"
    params: dict = {"syms": list(symbols)}
    if since is not None:
        query += " AND funding_time >= %(since)s"
        params["since"] = since
    query += " ORDER BY symbol, funding_time"
    fr = pd.read_sql_query(query, conn, params=params)
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


# --- Per-Stunde-Cache für Hochfrequenz-Aufrufer (T-2026-CU-9050-055) ---
#
# ``funding_features_asof`` hängt vom Zeitstempel AUSSCHLIESSLICH über den
# searchsorted-Schnitt ab — also über die Zahl der Sätze mit funding_time < ts.
# Binance rechnet Funding auf VOLLEN Stunden ab (8h-Raster, einzelne Paare
# 4h/1h). Innerhalb einer angebrochenen Stunde kommt kein Satz dazu, der Schnitt
# bleibt gleich, und alle Aggregate sind Suffixe (rates[-3:], rates[-270:], …) —
# sie hängen nicht an der ``since``-Untergrenze des Loads. Ein Cache mit dem
# Stunden-Key liefert deshalb BIT-IDENTISCHE Werte; er ist keine Näherung.
#
# Ein naiver Zeit-TTL wäre eine: der verschöbe den As-of-Zeitpunkt quer über eine
# Abrechnungsgrenze und bräche die Trainer-Parität (Train == Serve == Replay).
#
# Ausnahme Ingestion-Lag: die Zeile für hh:00 landet erst Sekunden später in
# ``funding_rates``. Ein Cache-Eintrag, der in den ersten ``CACHE_MIN_AGE_S``
# einer Stunde gebaut wurde, kann sie verpasst haben und wird verworfen.
CACHE_MIN_AGE_S = 120
CACHE_SINCE_DAYS = 95  # as-of nutzt max. die letzten 270 Sätze

#: symbol → (Stunden-Key, Bauzeitpunkt, Features)
_CACHE: dict[str, tuple[datetime.datetime, datetime.datetime, dict]] = {}


def clear_funding_cache() -> None:
    """Nur für Tests / Prozess-Reset."""
    _CACHE.clear()


def funding_features_cached(conn, symbol: str, ts_utc: datetime.datetime, loader=load_funding) -> dict:
    """Wie ``funding_features_asof``, aber höchstens ein DB-Roundtrip je Symbol
    und Stunde. Die Werte sind identisch zum ungecachten Aufruf (siehe oben).

    ``loader`` ist injizierbar, damit der Cache DB-frei testbar bleibt.
    """
    hour = ts_utc.replace(minute=0, second=0, microsecond=0)
    hit = _CACHE.get(symbol)
    if hit is not None:
        cached_hour, built_at, feats = hit
        if cached_hour == hour and (built_at - hour).total_seconds() >= CACHE_MIN_AGE_S:
            return feats

    by_sym = loader(conn, [symbol], since=ts_utc - datetime.timedelta(days=CACHE_SINCE_DAYS))
    feats = funding_features_asof(by_sym, symbol, ts_utc)
    _CACHE[symbol] = (hour, ts_utc, feats)
    return feats
