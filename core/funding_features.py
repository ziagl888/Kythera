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


# --- Abrechnungs-gebundener Cache für Hochfrequenz-Aufrufer (T-2026-CU-9050-055) ---
#
# ``funding_features_asof`` hängt vom Zeitstempel AUSSCHLIESSLICH über den
# searchsorted-Schnitt ab — über die Zahl der Sätze mit funding_time < ts. Solange
# keine neue Abrechnung dazukommt, ist das Ergebnis konstant: der Schnitt bleibt
# gleich, und alle Aggregate sind Suffixe (rates[-3:], rates[-270:], …), hängen
# also nicht an der wandernden ``since``-Untergrenze des Loads.
#
# Der Cache-Schlüssel kommt deshalb aus den DATEN, nicht aus der Wanduhr: ein
# Eintrag gilt bis zu der Abrechnung, die das Ergebnis als Nächstes verändern kann
# (siehe ``next_feature_change``). Zwei Fehlerklassen, die ein uhr-gebundener Key
# (z.B. "eine Stunde") hätte, entfallen damit:
#
#   * Abrechnungen, die nicht auf einer vollen Stunde liegen. Nichts erzwingt
#     das — ``tools/backfill_funding_rates.py`` schreibt ``funding_time`` mit
#     voller Millisekunden-Auflösung. Ein Satz um 12:30 wäre unter einem
#     Stunden-Key bis 13:00 unsichtbar geblieben.
#   * Ingestion-Verzug. Ist die fällige Zeile noch nicht da, ist der Eintrag
#     bereits abgelaufen: es wird neu geladen, bis sie erscheint — dann schiebt
#     der frische ``funding_time`` die Grenze weiter. Der Cache korrigiert sich
#     selbst, statt auf eine Ingestion-SLA zu wetten.
#
# Ein naiver Zeit-TTL leistet beides nicht: er kann eine Abrechnungsgrenze
# überspannen und dem Modell veraltetes Funding servieren — Bruch der
# Trainer-Parität (Train == Serve == Replay).
# as-of nutzt max. die letzten 270 Sätze (rates[-270:] für fund_pctl_90d). Bei
# 8h-Kadenz sind das exakt 90 Tage — 95d gäben nur 5 Tage Puffer, ein Coin mit
# >5d kumulierter Funding-Lücke in seinen letzten 270 Sätzen bekäme live weniger
# als 270 Samples und wiche in fund_pctl_90d minimal vom Trainer ab (der über die
# volle Historie rechnet). 110d gibt 20 Tage Lücken-Puffer über das 90d-Minimum.
CACHE_SINCE_DAYS = 110
#: Wie viele der jüngsten Abstände in die Intervall-Schätzung eingehen.
CACHE_INTERVAL_SAMPLES = 8

#: symbol → (gültig_bis = nächste fällige Abrechnung, Features)
_CACHE: dict[str, tuple[pd.Timestamp, dict]] = {}


def clear_funding_cache() -> None:
    """Nur für Tests / Prozess-Reset."""
    _CACHE.clear()


def next_feature_change(g: pd.DataFrame, ts_utc) -> pd.Timestamp | None:
    """Bis wann das As-of-Ergebnis für ``ts_utc`` unverändert bleibt.

    ``funding_features_asof`` schneidet mit ``searchsorted(..., 'left')``: es gehen
    die Sätze mit ``funding_time < ts`` ein. Das Ergebnis kippt also erst, wenn ts
    den NÄCHSTEN Satz überschreitet. Der steht entweder schon in den Daten (dann
    ist er die Grenze — auch wenn er auf keiner vollen Stunde liegt), oder er ist
    noch nicht abgerechnet: dann wird er aus der Historie geschätzt.

    Die Schätzung nimmt bewusst das **Minimum** der jüngsten Abstände, nicht den
    Median. Die beiden Fehlerrichtungen sind nämlich nicht gleich teuer:

      * Zu KURZ geschätzt → der Eintrag läuft zu früh ab, ein zusätzlicher
        DB-Roundtrip. Kostet Zeit, nie Korrektheit.
      * Zu LANG geschätzt → der Cache sitzt über einer echten Abrechnung und
        liefert einen stale Wert. Genau der Paritäts-Bruch, den dieser Cache
        verhindern soll.

    Verkürzt ein Coin seine Kadenz (Binance 8h → 4h/1h) oder verzerrt eine
    Ingestion-Lücke die jüngsten Abstände, überschätzt ein Median das nächste
    Intervall um Stunden. Das Minimum kann die BEOBACHTETEN Abstände nicht
    überschätzen — den allerersten Satz einer plötzlich kürzeren Kadenz kann
    keine Historien-Schätzung vorhersehen (ein Onset-Overshoot bleibt), aber ab
    dem zweiten kurzen Satz zieht das Minimum nach, ein Median-Fenster hinge noch
    stundenlang am alten Wert.

    ``None`` bei zu kurzer Historie — ohne zwei Sätze ist kein Intervall bestimmbar,
    dann wird nicht gecacht.
    """
    ft = g["funding_time"]
    if len(ft) < 2:
        return None
    i = int(ft.searchsorted(pd.Timestamp(ts_utc), side="left"))
    if i < len(ft):
        return ft.iloc[i]
    step = ft.diff().dropna().iloc[-CACHE_INTERVAL_SAMPLES:].min()
    if pd.isna(step) or step <= pd.Timedelta(0):
        return None
    return ft.iloc[-1] + step


def funding_features_cached(conn, symbol: str, ts_utc: datetime.datetime, loader=load_funding) -> dict:
    """Wie ``funding_features_asof``, aber ohne den DB-Roundtrip zu wiederholen,
    solange keine neue Abrechnung das Ergebnis verändern kann. Die Werte sind
    identisch zum ungecachten Aufruf (Begründung oben).

    ``loader`` ist injizierbar, damit der Cache DB-frei testbar bleibt.
    """
    hit = _CACHE.get(symbol)
    if hit is not None and pd.Timestamp(ts_utc) <= hit[0]:
        return hit[1]

    by_sym = loader(conn, [symbol], since=ts_utc - datetime.timedelta(days=CACHE_SINCE_DAYS))
    feats = funding_features_asof(by_sym, symbol, ts_utc)

    g = by_sym.get(symbol)
    valid_until = next_feature_change(g, ts_utc) if g is not None else None
    # Ein LEERES Ergebnis (Historie < MIN_HISTORY) NICHT cachen: der nächste Satz,
    # der den Coin über die Schwelle hebt, kann früher fallen als das geschätzte
    # Intervall, und bis dahin würde `{}` ausgeliefert — genau dann, wenn der Coin
    # handelbar wird. Wie beim Late-Row-Fall: lieber jeden Tick neu laden, bis echte
    # Features da sind.
    if feats and valid_until is not None and pd.Timestamp(ts_utc) <= valid_until:
        _CACHE[symbol] = (valid_until, feats)
    else:
        # Historie zu kurz (kein Intervall bestimmbar) ODER die Abrechnung ist
        # überfällig, weil die Zeile noch nicht ingested ist. Im zweiten Fall wäre
        # der Eintrag ohnehin sofort abgelaufen — die Uhr läuft vorwärts, ein
        # abgelaufener Eintrag wird nie ausgeliefert. Das `pop` ist also Hygiene
        # (und ein Netz gegen einen NTP-Rückschritt), nicht die tragende Sperre.
        # Die tragende Sperre ist der `<=`-Vergleich oben.
        _CACHE.pop(symbol, None)
    return feats
