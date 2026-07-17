# core/moment_features.py
"""Geteilter Realized-Moments-Feature-Builder (X-R1) — EINE Quelle für Studie,
Trainer und (später) Bot.

Herkunft: docs/MODEL_CANDIDATES_SPEC_2026-07.md §K7 (MOM/SKW1). Die Hypothese:
die realisierten Verteilungs-Momente der jüngeren Renditegeschichte eines Coins
(Volatilität, Schiefe, Wölbung) tragen Information — insbesondere prädiziert
realisierte **Schiefe** negativ (Short-Kandidatenfilter SKW1), und RV/Kurtosis
ergänzen kommende Retrains (ATS2, QM2, BR-Gate). Wie core/funding_features.py und
core/breadth_features.py ist dieser Builder kanonisch: Studie, Trainer und Bot
rechnen exakt dieselben Größen (kein Train/Serve-Skew).

⚠ FALLE (§K7, F6): Das hier ist REALISIERTE SCHIEFE (drittes Moment der
Renditeverteilung), NICHT ein MAX-/Lotterie-Feature. MAX-basierte Shorts sind in
Krypto kontraindiziert (F6 — der MAX-Effekt invertiert). Es wird bewusst KEIN
"maximale Einzelrendite im Fenster" gebaut, sondern die Standard-Momentschätzer
(pandas rolling std/skew/kurt) über die Renditereihe.

Datenquelle offline: die per-Coin ``{SYM}_15m``-Kerzen über core.candles.read_candles.
BEWUSST 15m, NICHT 5m: 5m hat nur ~1 Monat Retention, 15m reicht ~1 Jahr zurück
(§K7). Rollierende Fenster {24h, 7d} — bei 15m sind das 96 bzw. 672 geschlossene
Balken.

As-of-Vertrag (R1, nur geschlossene Kerzen): ein 15m-Balken mit open_time D
schließt erst bei D + 15m. ``moment_features_asof(panel, ts)`` liefert deshalb den
jüngsten Balken D mit D + tf <= ts — kein Lookahead. Der Load nutzt
``include_forming=False``.

Native-NaN-Politik (XGB-Muster P1.20): fehlende WERTE bleiben NaN und werden NIE
mit 0 ersetzt. Ein Coin mit zu kurzer Historie liefert an einem Zeitpunkt NaN
(bzw. ``None`` aus der As-of-Funktion) — der Aufrufer entscheidet (Trainer:
Zeile verwerfen; ein Gate: fail-closed/-open je Politik). ``fillna(0)`` würde eine
flache Verteilung (Vol 0, Skew 0) vortäuschen und das Modell vergiften.

Feature-Vertrag (X-R1): fehlende SPALTEN im geladenen Frame ⇒ ``MomentFeatureError``
(Load-Fehler), NIE ``fillna(0)`` als Vertragsersatz. Fehlende WERTE sind KEIN
Vertragsbruch — sie bleiben NaN (siehe oben).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from core.candles import TF_SECONDS, read_candles


class MomentFeatureError(RuntimeError):
    """X-R1-Vertragsbruch: ein geladener Frame hat nicht die Pflicht-Spalten."""


#: Spalten, die jeder geladene Kerzen-Frame tragen MUSS (sonst Load-Fehler).
REQUIRED_COLUMNS: tuple[str, ...] = ("open_time", "close")

#: Rollierende Fenster (§K7). Werte in Sekunden → bar-Anzahl hängt am tf (15m→96/672).
WINDOW_SECONDS: dict[str, int] = {"24h": 86400, "7d": 604800}

#: Der Realized-Moments-Feature-Vertrag (kanonische Namen, Reihenfolge fix).
#: 3 Momente × 2 Fenster = 6 Features (parallel zu den 6 Funding-Features).
MOMENT_FEATURES: list[str] = [
    "mom_rv_24h",  # realisierte Vol: StdAbw der 15m-Log-Returns über trailing 24h
    "mom_rv_7d",  # ... über trailing 7d
    "mom_skew_24h",  # realisierte (Sample-)Schiefe der 15m-Log-Returns über 24h
    "mom_skew_7d",  # ... über 7d
    "mom_kurt_24h",  # realisierte Exzess-Wölbung (Fisher) über 24h
    "mom_kurt_7d",  # ... über 7d
]

#: Standard-tf des Builders (§K7: 15m wegen Retention).
DEFAULT_TF = "15m"


def window_bars(tf: str = DEFAULT_TF) -> dict[str, int]:
    """Balken-Anzahl je Fenster für ein gegebenes tf (24h/7d in bars).

    ``MomentFeatureError`` bei unbekanntem tf oder wenn ein Fenster kein ganzes
    Vielfaches der Balkendauer ist (dann wäre die Fensterlänge undefiniert).
    """
    if tf not in TF_SECONDS:
        raise MomentFeatureError(f"unbekanntes tf {tf!r} — kein TF_SECONDS-Eintrag")
    step = TF_SECONDS[tf]
    bars: dict[str, int] = {}
    for name, secs in WINDOW_SECONDS.items():
        if secs % step != 0:
            raise MomentFeatureError(f"Fenster {name} ({secs}s) ist kein Vielfaches der {tf}-Balkendauer ({step}s)")
        bars[name] = secs // step
    return bars


def load_moment_candles(
    conn: Any,
    symbol: str,
    *,
    tf: str = DEFAULT_TF,
    start: Any | None = None,
) -> pd.DataFrame:
    """Lädt EINE geschlossene 15m-Kerzenhistorie eines Coins (aufsteigend).

    Eine Query je Coin (read_candles, include_forming=False). Fehlt dem GELADENEN
    Frame eine Pflicht-Spalte, ist das ein X-R1-Load-Fehler (MomentFeatureError),
    nie fillna(0). Ein leerer Frame (kein Coin/keine Daten) wird unverändert
    zurückgegeben — der Aufrufer überspringt den Coin (Survivorship).

    Rückgabe: DataFrame[open_time(UTC, tz-aware), close], aufsteigend.
    """
    df = read_candles(
        conn,
        symbol,
        tf,
        start=start,
        include_forming=False,
        columns=("open_time", "close"),
    )
    if df.empty:
        return df
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise MomentFeatureError(f"{symbol}_{tf}: fehlende Pflicht-Spalten {missing} — X-R1-Vertrag, kein fillna(0)")
    df = df.copy()
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("open_time").reset_index(drop=True)
    return df[["open_time", "close"]]


def build_moment_panel(df: pd.DataFrame, *, tf: str = DEFAULT_TF) -> pd.DataFrame:
    """Baut das per-Coin Moment-Panel EINMAL (rollierend über die 15m-Returns).

    Index = 15m-open_time (UTC, tz-aware). Spalten = MOMENT_FEATURES. Danach ist
    ``moment_features_asof`` nur noch ein O(log n)-Lookup.

    Returns = Log-Returns ``ln(close_t / close_{t-1})``. Für jedes Fenster wird die
    Standardabweichung (mom_rv, ddof=1), die Sample-Schiefe (pandas .skew(),
    Fisher-Pearson bias-korrigiert) und die Exzess-Wölbung (pandas .kurt(), Fisher)
    über die trailing Renditen gerechnet. ``min_periods`` = volle Fensterbreite:
    solange das Fenster nicht voll ist, bleibt der Wert NaN (native-NaN-Politik —
    KEIN fillna).

    Ein leerer/zu kurzer Frame liefert ein leeres Panel mit den richtigen Spalten.
    """
    bars = window_bars(tf)
    cols = {feat: np.nan for feat in MOMENT_FEATURES}
    if df.empty:
        return pd.DataFrame(cols, index=pd.DatetimeIndex([], tz="UTC", name="open_time"))

    d = df.sort_values("open_time").reset_index(drop=True)
    idx = pd.DatetimeIndex(pd.to_datetime(d["open_time"], utc=True), name="open_time")
    # Log-Returns; die erste Zeile hat keinen Vorgänger → NaN (bleibt NaN).
    ret = np.log(d["close"].to_numpy(dtype=float))
    ret = pd.Series(np.diff(ret, prepend=np.nan), index=idx)

    out: dict[str, pd.Series] = {}
    for win_name, n in bars.items():
        roll = ret.rolling(window=n, min_periods=n)
        out[f"mom_rv_{win_name}"] = roll.std(ddof=1)
        out[f"mom_skew_{win_name}"] = roll.skew()
        out[f"mom_kurt_{win_name}"] = roll.kurt()

    panel = pd.DataFrame({feat: out[feat] for feat in MOMENT_FEATURES}, index=idx)
    return panel.sort_index()


def moment_features_asof(panel: pd.DataFrame, ts_utc: Any, *, tf: str = DEFAULT_TF) -> dict:
    """Die Realized-Moment-Features as-of ``ts_utc`` (tz-aware oder naiv=UTC).

    Liefert den jüngsten 15m-Balken D mit D + tf <= ts (nur geschlossene Kerzen,
    kein Lookahead). Rückgabe {} bei leerem Panel oder wenn ts vor der ersten
    verwertbaren Zeile liegt. Werte, die NaN sind, kommen als ``None`` zurück (der
    Aufrufer entscheidet — Trainer verwirft die Zeile; ein Gate fail-closed/-open)
    — NIE als 0.
    """
    if panel.empty:
        return {}
    if tf not in TF_SECONDS:
        raise MomentFeatureError(f"unbekanntes tf {tf!r} — kein TF_SECONDS-Eintrag")
    ts = pd.Timestamp(ts_utc)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    cutoff = ts - pd.Timedelta(seconds=TF_SECONDS[tf])
    idx = int(panel.index.searchsorted(cutoff, side="right")) - 1
    if idx < 0:
        return {}
    row = panel.iloc[idx]
    return {col: (float(row[col]) if pd.notna(row[col]) else None) for col in panel.columns}


def build_symbol_moment_panels(
    conn: Any,
    symbols: list[str],
    *,
    tf: str = DEFAULT_TF,
    start: Any | None = None,
) -> dict[str, pd.DataFrame]:
    """Convenience für Studie/Trainer: je Coin EINE Query → fertiges Moment-Panel.

    Coins ohne Tabelle/Daten (delisted, Survivorship) werden übersprungen — das ist
    KEIN Vertragsbruch. Fehlt einem GELADENEN Frame eine Pflicht-Spalte, propagiert
    der MomentFeatureError (X-R1-Load-Fehler).

    Rückgabe: {symbol -> Moment-Panel}. Coins ohne verwertbares Panel fehlen.
    """
    panels: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df = load_moment_candles(conn, sym, tf=tf, start=start)
        except MomentFeatureError:
            raise
        except Exception:
            # Fehlende per-Coin-Tabelle o.ä. → Survivorship, überspringen.
            try:
                conn.rollback()
            except Exception:
                pass
            continue
        if df.empty:
            continue
        panel = build_moment_panel(df, tf=tf)
        if panel.empty:
            continue
        panels[sym] = panel
    return panels
