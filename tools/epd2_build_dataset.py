"""
tools/epd2_build_dataset.py — Trainings-Events + Replay-Labels für den
EPD2-Retrain (Momentum-MITFAHREN, MODEL_INTENT §7).

EPD detektiert auf 10s-Ticks — bar-für-bar ist das nicht nachspielbar. Die
Detektor-Logs SIND aber die Events: ``pump_dump_events`` wird von Bot 10 mit
exakt den Live-Gates geschrieben. Pipeline (Muster: tools/pex1_build_dataset.py,
Spiegel der BOT-10-Semantik statt Bot 30):

  1. Events: volume_ratio >= 5 (Alert-Gate des Bots) UND
     |price_change_60s| >= PUMP_EVENT_MIN_ABS_PCHG_60S — BEIDE Richtungen.
  2. Richtung = MITFAHREN (Intent §7): Pump (+60s-Move) → LONG, Dump → SHORT.
  3. TZ-Fix + Dedup 900 s je Symbol (Live-Alert-Throttle von Bot 10).
  4. Entry = echter Preis zum `spike_time` aus ``ticker_10s`` (T-2026-CU-9050-035).
     Früher war das der Schätzer ``close×(1+p_chg_60s/100)``. Der ist seit der
     Normalisierung von ``p_chg_60s`` auf eine Rate pro 60 s FALSCH: die Spalte
     trägt nicht mehr den realisierten Move, und ohne die Fensterlänge lässt er
     sich aus dem Event-Log nicht rekonstruieren (harte Regel 7, Trainer ==
     Serving). ``ticker_10s`` hält den tatsächlich gehandelten Preis in 10s-
     Auflösung und schlägt jeden Schätzer — gemessen finden 7053 von 7055 Events
     der letzten drei Tage einen Tick innerhalb von 60 s, über alle 404
     Event-Symbole.
  5. Geometrie = BOT-10-Geometrie as-of: get_hvn_and_sr_levels(df=95d-Fenster)
     + hvn_sr_trade_geometry + ensure_min_tp_distance (NICHT smart_targets —
     Bot 10 postet HVN/SR-Geometrie).
  6. Label: simulate_exit ab Event-Kerze+2 (Skip-Entry-Hour, aim2-Präzedenz),
     Horizont 7 Tage.
  7. Features: die 10 Live-Features des Bots (sample_fill=1.0 als dokumentierte
     Steady-State-Näherung — der Wert steht nicht im Event-Log) + die 6
     Funding-Features (core/funding_features, Operator-Auftrag 2026-07-06).

Beispiel:
  python tools/epd2_build_dataset.py                    # Vollausbau
  python tools/epd2_build_dataset.py --limit-symbols 10 # Smoke-Test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.pex1_build_dataset import detect_offset_h, spike_time_to_utc  # noqa: E402
from tools.research_dataset_common import (  # noqa: E402
    MIN_WINDOW,
    REPLAY_DIR,
    candles_window_start,
    df_query,
    floor_idx,
    join_is_stale,
    log,
    set_low_priority,
)

from core import config as _kcfg  # noqa: E402
from core.candles import read_candles_with_indicators  # noqa: E402
from core.database import get_db_connection  # noqa: E402
from core.funding_features import funding_features_asof, load_funding  # noqa: E402
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels, hvn_sr_trade_geometry  # noqa: E402
from tools.walkforward_sim import simulate_exit  # noqa: E402

SINCE_DEFAULT = "2026-02-25"      # Beginn belastbarer pump_dump_events-Historie
ALERT_MIN_VOL_RATIO = 5.0         # Alert-Gate von Bot 10 (Training == Serving)
HORIZON_CANDLES = 7 * 24
DEDUP_SECONDS = 900               # Live-Alert-Throttle je Symbol (Bot 10)
TICKER_MAX_LAG_SEC = 60           # max. Abstand Event ↔ ticker_10s-Tick für den Entry
N_PUBLISHED = 3
LEVEL_WINDOW_H = 95 * 24          # HVN/SR-Fenster wie get_hvn_and_sr_levels live

#: 10-Feature-Vertrag von Bot 10 (features_array-Reihenfolge dort).
EPD_FEATURES = [
    "vol_ratio", "p_chg_60s", "buy_pres", "volat", "sample_fill",
    "rsi", "tsi", "macd", "e9_dist", "e21_dist",
]

EPD_SQL_INDICATORS = (
    "i.rsi_14, i.tsi_fast_12_7_7, i.macd_dif_normal_12_26_9, i.ema_9, i.ema_21"
)
# Reine Spaltennamen für read_candles_with_indicators (i.-Präfix entfernt).
EPD_IND_COLS = [c.strip().split(".")[-1] for c in EPD_SQL_INDICATORS.split(",") if c.strip()]


def load_events(conn, since: str, offset_h: int) -> pd.DataFrame:
    ev = df_query(
        conn,
        """
        SELECT symbol, spike_time, volume_ratio, price_change_60s, buy_pressure, volatility,
               rsi_14 AS ev_rsi, tsi AS ev_tsi, macd_dif AS ev_macd,
               ema9_distance_pct AS ev_e9, ema21_distance_pct AS ev_e21
        FROM pump_dump_events
        WHERE volume_ratio >= %s AND ABS(price_change_60s) >= %s
          AND spike_time > %s::timestamptz
        ORDER BY spike_time ASC
        """,
        (ALERT_MIN_VOL_RATIO, _kcfg.PUMP_EVENT_MIN_ABS_PCHG_60S, since),
    )
    # ~30 % der Events tragen EXAKTE Event-Zeitpunkt-Indikatoren (ev_*; ältere
    # Bot-Version schrieb sie mit) — wo vorhanden, schlagen sie den bis zu 1h
    # stalen 1h-Join-Fallback.
    ev["ts"] = spike_time_to_utc(ev["spike_time"], offset_h)
    ev["symbol"] = ev["symbol"].astype(str).str.upper()
    ev = ev[ev["symbol"].str.endswith("USDT")].dropna(subset=["ts"])
    ev = ev[ev["ts"] >= pd.Timestamp(since)]

    # Dedup: 900s-Throttle je Symbol (richtungsübergreifend — wie pd_state live).
    keep, last_ts = [], {}
    for row in ev.itertuples():
        prev = last_ts.get(row.symbol)
        ok = prev is None or (row.ts - prev).total_seconds() >= DEDUP_SECONDS
        keep.append(ok)
        if ok:
            last_ts[row.symbol] = row.ts
    return ev[pd.Series(keep, index=ev.index)].reset_index(drop=True)


def ticker_history_start(conn) -> pd.Timestamp | None:
    """Ältester `ticker_10s`-Tick als naive UTC, oder None bei leerer Tabelle."""
    row = df_query(conn, "SELECT MIN(ts) AS m FROM ticker_10s")["m"].iloc[0]
    if pd.isna(row):
        return None
    return pd.Timestamp(row).tz_convert("UTC").tz_localize(None)


def load_ticker_prices(conn, symbol: str, since: str) -> tuple[np.ndarray, np.ndarray]:
    """10s-Ticks eines Symbols als (Zeiten, Preise), chronologisch.

    `ticker_10s.ts` ist per TZ-Vertrag UTC-aware (core/ticker_10s.py) — anders als
    die naiven Legacy-Spalten. Hier auf naive UTC gebracht, damit der Vergleich
    mit `spike_time_to_utc(...)` auf derselben Basis läuft.
    """
    try:
        df = df_query(
            conn,
            "SELECT ts, price FROM ticker_10s WHERE symbol = %s AND ts >= %s::timestamptz ORDER BY ts ASC",
            (symbol, since),
        )
    except Exception:
        conn.rollback()
        return np.empty(0, dtype="datetime64[ns]"), np.empty(0, dtype=np.float64)
    if df.empty:
        return np.empty(0, dtype="datetime64[ns]"), np.empty(0, dtype=np.float64)
    ts = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None).values.astype("datetime64[ns]")
    return ts, df["price"].to_numpy(dtype=np.float64)


def entry_from_ticker(
    ts_arr: np.ndarray, px_arr: np.ndarray, event_ts: pd.Timestamp, max_lag: int = TICKER_MAX_LAG_SEC
) -> float | None:
    """Tatsächlich gehandelter Preis zum Event-Zeitpunkt, oder None.

    `spike_time` ist die Wanduhr des Detectors NACH dem 60s-Move, der Tick an
    dieser Stelle ist also bereits der Post-Spike-Preis — genau das, was der alte
    Schätzer approximieren wollte. Kein Fallback auf den Schätzer: ohne Tick ist
    der Entry unbekannt, und eine geratene Entry-Geometrie erzeugt ein falsches
    Label, kein fehlendes.
    """
    if ts_arr.size == 0:
        return None
    target = np.datetime64(pd.Timestamp(event_ts).to_datetime64())
    i = int(np.searchsorted(ts_arr, target))
    best_lag, best_px = None, None
    for j in (i - 1, i):
        if 0 <= j < ts_arr.size:
            lag = abs((ts_arr[j] - target) / np.timedelta64(1, "s"))
            if lag <= max_lag and (best_lag is None or lag < best_lag):
                best_lag, best_px = lag, float(px_arr[j])
    if best_px is None or best_px <= 0:
        return None
    return best_px


def load_candles_epd(conn, symbol: str, since: str) -> pd.DataFrame | None:
    """1h-Kerzen + EPD-Indikatorspalten, Lookback 100d (95d-Level-Fenster).

    Über core.candles: GESCHLOSSENE Kerzen (include_forming=False); der Caller
    schneidet per floor_idx ohnehin auf die letzte geschlossene Kerze."""
    try:
        df = read_candles_with_indicators(
            conn,
            symbol,
            "1h",
            start=candles_window_start(since, 100),
            include_forming=False,
            candle_columns=("open_time", "open", "high", "low", "close", "volume"),
            indicator_columns=EPD_IND_COLS,
        )
    except Exception:
        conn.rollback()
        return None
    if df.empty:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True).dt.tz_localize(None)
    for c in df.columns:
        if c != "open_time":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["high", "low", "close"]).reset_index(drop=True)


def _val(df, col, i, default):
    v = df[col].iloc[i]
    try:
        fv = float(v)
        return fv if np.isfinite(fv) else default
    except (TypeError, ValueError):
        return default


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=SINCE_DEFAULT)
    ap.add_argument("--out", default=os.path.join(REPLAY_DIR, "epd2_events.jsonl"))
    ap.add_argument("--limit-symbols", type=int, default=0)
    ap.add_argument(
        "--allow-pre-ticker",
        action="store_true",
        help="Events vor dem ersten ticker_10s-Tick zulassen (sie verlieren ihren Entry).",
    )
    args = ap.parse_args()

    set_low_priority()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    t0 = time.time()

    conn = get_db_connection()
    offset_h = detect_offset_h(conn)
    log(f"spike_time-Offset: {offset_h:+d}h gegen UTC")

    # Der Entry kommt aus ticker_10s (Schritt 4 im Modul-Docstring). Reicht `since`
    # vor den ersten Tick, würde jedes Event davor still an `no_ticker` verloren
    # gehen — das sieht im Log wie ein kleiner Filter aus und ist in Wahrheit ein
    # halber Datensatz. Lieber laut abbrechen als leise schrumpfen.
    tick_start = ticker_history_start(conn)
    if tick_start is None:
        log("FEHLER: ticker_10s ist leer — ohne Ticks kein Entry. Abbruch.")
        sys.exit(2)
    if pd.Timestamp(args.since) < tick_start and not args.allow_pre_ticker:
        log(f"FEHLER: --since {args.since} liegt vor dem ersten ticker_10s-Tick "
            f"({tick_start:%Y-%m-%d %H:%M} UTC). Events davor bekommen keinen Entry.")
        log("       Setze --since auf den Post-Restart-Schnitt (empfohlen), oder "
            "--allow-pre-ticker, wenn der Verlust bewusst ist.")
        sys.exit(2)

    ev = load_events(conn, args.since, offset_h)
    n_long = int((ev["price_change_60s"] > 0).sum())
    log(f"Events nach Gates + Dedup: {len(ev)} ({n_long} Pump/LONG, "
        f"{len(ev) - n_long} Dump/SHORT) über {ev['symbol'].nunique()} Symbole")

    symbols = list(ev["symbol"].drop_duplicates())
    if args.limit_symbols:
        symbols = symbols[: args.limit_symbols]

    stats = {k: 0 for k in ("written", "wins", "open_end", "no_candles", "no_window",
                            "stale_join", "geometry_fail", "no_ticker")}
    with open(args.out, "w", encoding="utf-8") as fh:
        for i, sym in enumerate(symbols, 1):
            df = load_candles_epd(conn, sym, args.since)
            sym_ev = ev[ev["symbol"] == sym]
            if df is None or len(df) < MIN_WINDOW:
                stats["no_candles"] += len(sym_ev)
                continue
            fund_by_sym = load_funding(conn, [sym])
            tick_ts, tick_px = load_ticker_prices(conn, sym, args.since)
            times = df["open_time"].values.astype("datetime64[ns]")
            highs = df["high"].to_numpy(dtype=np.float64)
            lows = df["low"].to_numpy(dtype=np.float64)
            closes = df["close"].to_numpy(dtype=np.float64)

            for row in sym_ev.itertuples():
                idx = floor_idx(times, row.ts)
                if idx < MIN_WINDOW:
                    stats["no_window"] += 1
                    continue
                if join_is_stale(times, idx, row.ts):
                    stats["stale_join"] += 1
                    continue
                p_chg = float(row.price_change_60s)
                direction = "LONG" if p_chg > 0 else "SHORT"
                is_long = direction == "LONG"
                # Entry = echter Post-Spike-Preis aus ticker_10s. `p_chg_60s` ist
                # seit T-035 eine Rate pro 60 s, kein realisierter Move — als
                # Aufschlag auf `close` wäre er schlicht falsch. Das Vorzeichen
                # (Richtung) bleibt von der Normalisierung unberührt.
                entry1 = entry_from_ticker(tick_ts, tick_px, row.ts)
                if entry1 is None:
                    stats["no_ticker"] += 1
                    continue
                try:
                    win = df.iloc[max(0, idx + 1 - LEVEL_WINDOW_H): idx + 1][["high", "low", "close"]]
                    supps, resis = get_hvn_and_sr_levels(None, sym, entry1, df=win)
                    entry2, sl, t_cands = hvn_sr_trade_geometry(entry1, is_long, supps, resis)
                    targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long, min_pct=0.05)
                    if not targets or sl <= 0 or entry1 <= 0:
                        raise ValueError("degenerate geometry")
                    end = min(idx + 2 + HORIZON_CANDLES, len(times))
                    res = simulate_exit(
                        times[:end], highs[:end], lows[:end], closes[:end],
                        start_idx=idx + 2, direction=direction, entry=entry1, sl=sl,
                        targets=targets, n_published=min(N_PUBLISHED, len(targets)),
                    )
                    def _ev(v, fallback):
                        try:
                            fv = float(v)
                            return fv if np.isfinite(fv) else fallback
                        except (TypeError, ValueError):
                            return fallback

                    ema9 = _val(df, "ema_9", idx, entry1)
                    ema21 = _val(df, "ema_21", idx, entry1)
                    feats = {
                        "vol_ratio": float(row.volume_ratio),
                        "p_chg_60s": p_chg,
                        "buy_pres": float(row.buy_pressure),
                        "volat": float(row.volatility),
                        "sample_fill": 1.0,  # Steady-State-Näherung (nicht im Event-Log)
                        # Event-Zeitpunkt-Indikatoren (ev_*) bevorzugt, sonst 1h-Join as-of
                        "rsi": _ev(row.ev_rsi, _val(df, "rsi_14", idx, 50.0)),
                        "tsi": _ev(row.ev_tsi, _val(df, "tsi_fast_12_7_7", idx, 0.0)),
                        "macd": _ev(row.ev_macd, _val(df, "macd_dif_normal_12_26_9", idx, 0.0)),
                        "e9_dist": _ev(row.ev_e9, (entry1 - ema9) / ema9 * 100 if ema9 > 0 else 0.0),
                        "e21_dist": _ev(row.ev_e21, (entry1 - ema21) / ema21 * 100 if ema21 > 0 else 0.0),
                    }
                    feats.update(funding_features_asof(fund_by_sym, sym, pd.Timestamp(row.ts, tz="UTC")))
                except Exception:
                    stats["geometry_fail"] += 1
                    continue

                label = res.get("outcome_tp1")
                if res.get("exit_reason") == "open_at_end":
                    label = None  # Report-13-Regel: offene Trades nicht labeln
                fh.write(json.dumps({
                    "symbol": sym, "ts": pd.Timestamp(row.ts).isoformat(),
                    "direction": direction, "weight": 1.0,
                    "entry": entry1, "entry2": entry2, "sl": sl, "targets": targets[:N_PUBLISHED],
                    "label": label, "net_pnl_pct": res.get("net_pnl_pct"),
                    "exit_reason": res.get("exit_reason"), "risk_pct": res.get("risk_pct"),
                    "features": feats,
                }) + "\n")
                stats["written"] += 1
                stats["wins"] += 1 if label == 1 else 0
                stats["open_end"] += 1 if label is None else 0

            if i % 25 == 0 or i == len(symbols):
                closed = stats["written"] - stats["open_end"]
                wr = stats["wins"] / closed * 100 if closed else 0.0
                log(f"{i}/{len(symbols)} Symbole | geschrieben {stats['written']} "
                    f"(WR geschlossen: {wr:.1f}%) | {time.time() - t0:.0f}s")
    conn.close()
    log(f"FERTIG -> {args.out}")
    log(json.dumps(stats))


if __name__ == "__main__":
    main()
