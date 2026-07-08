"""
tools/pex1_build_dataset.py — Trainings-Events + Replay-Labels für PEX1
"Pump-Exhaustion-Short" (Report 15, S6). Läuft auf dem VPS (Step 2).

Pipeline je Event (pump_dump_events, Gates wie im Live-Bot 30 gespiegelt:
volume_ratio >= 5, price_change_60s >= +1.5):
  1. TZ: spike_time-Offset wird gegen die Wanduhr gemessen (Session-TZ des
     Detectors unbekannt) → Konvertierung nach UTC.
  2. Dedup je Symbol: 4h-Mindestabstand (Spiegel des Live-Cooldowns).
  3. floor-1-Join auf die letzte GESCHLOSSENE 1h-Kerze (kein Lookahead).
  4. Geometrie: calculate_smart_targets SHORT auf dem Kerzenfenster — exakt
     das, was Bot 30 beim Posten berechnet.
  5. Label: simulate_exit (First-Touch, SL-first, Fees), Horizont 7 Tage.
  6. Features: core.research_features.build_pex1_row (geteilter Builder).

Beispiel:
  python tools/pex1_build_dataset.py                 # Vollausbau
  python tools/pex1_build_dataset.py --limit-symbols 15   # Smoke-Test
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

import numpy as np
import pandas as pd

# REPO_ROOT MUSS vor dem ersten tools-/core-Import auf sys.path liegen —
# der Insert in research_dataset_common käme sonst nie zur Ausführung
# (Henne-Ei; Spec-Review-Fix 2026-07-06, Muster tools/aim2_build_dataset.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.research_dataset_common import (  # noqa: E402
    LOCAL_TZ,
    MIN_WINDOW,
    REPLAY_DIR,
    WINDOW_CANDLES,
    df_query,
    floor_idx,
    join_is_stale,
    load_candles_ctx,
    log,
    set_low_priority,
)

from core.database import get_db_connection  # noqa: E402
from core.research_features import PEX1_MIN_PUMP_PCHG_60S, PEX1_MIN_VOL_RATIO, build_pex1_row  # noqa: E402
from core.trade_utils import calculate_smart_targets  # noqa: E402
from tools.walkforward_sim import simulate_exit  # noqa: E402

SINCE_DEFAULT = "2026-02-25"
HORIZON_CANDLES = 7 * 24      # Exhaustion-These lebt Stunden bis Tage
DEDUP_HOURS = 4               # Spiegel des Live-Cooldowns (Bot 30)
N_PUBLISHED = 3


def detect_offset_h(conn) -> int:
    row = df_query(conn, "SELECT MAX(spike_time) AS m FROM pump_dump_events")["m"].iloc[0]
    if pd.isna(row):
        return 0
    row_ts = pd.Timestamp(row)
    if row_ts.tzinfo is not None:
        # timestamptz-Spalte (Ist-Zustand seit Vermessung 2026-07-06): PG liefert
        # aware UTC — keine Offset-Heuristik nötig, Konvertierung übernimmt
        # spike_time_to_utc über den tz-aware-Zweig.
        return 0
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return int(np.clip(round((row_ts - now).total_seconds() / 3600.0), -12, 12))


def spike_time_to_utc(series: pd.Series, offset_h: int) -> pd.Series:
    """spike_time → naive UTC. Ein konstanter Offset über Monate wäre DST-blind
    (Review-Fix 2026-07-06: Pre-DST-Events würden 1h verschoben → Kerze VOR dem
    Event im Label). Offset 2/3h ⇒ Domäne ist die PG-Lokalzeit Europe/Bucharest
    → DST-aware konvertieren; 0 ⇒ bereits UTC; alles andere: konstanter Shift
    mit Warnung (unbekannte Domäne)."""
    # Awareness am ROHWERT prüfen, nicht an der geparsten Spalte: timestamptz
    # über eine DST-Grenze (z. B. 2026-03-29 EET→EEST) liefert GEMISCHTE
    # Offsets (+02/+03). pd.to_datetime ohne utc=True fixiert dann den Offset
    # der ersten Zeile und koerziert alle abweichenden Zeilen zu NaT — der
    # EPD2-Lauf 2026-07-07 verlor so ALLE Events nach dem DST-Wechsel.
    sample = next((v for v in series if v is not None and not pd.isna(v)), None)
    if sample is not None and getattr(sample, "tzinfo", None) is not None:
        # timestamptz: aware → utc=True verkraftet gemischte Offsets → naive UTC.
        s = pd.to_datetime(series, errors="coerce", utc=True)
        return s.dt.tz_localize(None)
    s = pd.to_datetime(series, errors="coerce")
    if offset_h == 0:
        return s
    if offset_h in (2, 3):
        s = s.dt.tz_localize(LOCAL_TZ, nonexistent="shift_forward", ambiguous="NaT")
        return s.dt.tz_convert("UTC").dt.tz_localize(None)
    log(f"WARNUNG: spike_time-Offset {offset_h:+d}h passt zu keiner bekannten TZ-Domäne — "
        f"konstanter Shift (DST-blind).")
    return s - pd.Timedelta(hours=offset_h)


def load_events(conn, since: str, offset_h: int) -> pd.DataFrame:
    ev = df_query(
        conn,
        """
        SELECT symbol, spike_time, volume_ratio, price_change_60s, buy_pressure, volatility
        FROM pump_dump_events
        WHERE volume_ratio >= %s AND price_change_60s >= %s
          AND spike_time > %s::timestamp
        ORDER BY spike_time ASC
        """,
        # Grober SQL-Vorfilter mit 1 Tag Marge; exakter Since-Cut nach der
        # TZ-Konvertierung in pandas.
        (PEX1_MIN_VOL_RATIO, PEX1_MIN_PUMP_PCHG_60S, since),
    )
    ev["ts"] = spike_time_to_utc(ev["spike_time"], offset_h)
    ev["symbol"] = ev["symbol"].astype(str).str.upper()
    ev = ev[ev["symbol"].str.endswith("USDT")].dropna(subset=["ts"])
    ev = ev[ev["ts"] >= pd.Timestamp(since)]

    # Dedup: je Symbol 4h-Mindestabstand (erster Spike gewinnt — wie der Cooldown live).
    keep, last_ts = [], {}
    for row in ev.itertuples():
        prev = last_ts.get(row.symbol)
        ok = prev is None or (row.ts - prev).total_seconds() >= DEDUP_HOURS * 3600
        keep.append(ok)
        if ok:
            last_ts[row.symbol] = row.ts
    return ev[pd.Series(keep, index=ev.index)].reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=SINCE_DEFAULT)
    ap.add_argument("--out", default=os.path.join(REPLAY_DIR, "pex1_events.jsonl"))
    ap.add_argument("--limit-symbols", type=int, default=0)
    args = ap.parse_args()

    set_low_priority()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    t0 = time.time()

    conn = get_db_connection()
    offset_h = detect_offset_h(conn)
    log(f"spike_time-Offset: {offset_h:+d}h gegen UTC")
    ev = load_events(conn, args.since, offset_h)
    log(f"Events nach Gates + Dedup: {len(ev)} über {ev['symbol'].nunique()} Symbole")

    symbols = list(ev["symbol"].drop_duplicates())
    if args.limit_symbols:
        symbols = symbols[: args.limit_symbols]

    stats = {k: 0 for k in ("written", "wins", "open_end", "no_candles", "no_window",
                            "stale_join", "geometry_fail")}
    with open(args.out, "w", encoding="utf-8") as fh:
        for i, sym in enumerate(symbols, 1):
            df = load_candles_ctx(conn, sym, args.since)
            sym_ev = ev[ev["symbol"] == sym]
            if df is None or len(df) < MIN_WINDOW:
                stats["no_candles"] += len(sym_ev)
                continue
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
                # Entry = Spike-Preis-Schätzung, NICHT der Pre-Pump-Close
                # (Review-Fix HIGH 2026-07-06): Bot 30 steigt live POST-Pump ein
                # (live_price nach dem Spike). Der Pre-Pump-Close als Entry hätte
                # pump-korreliert deflationierte Labels erzeugt (der Pump selbst
                # riss den simulierten SL). Schätzer: letzter Close × (1 + 60s-Move).
                entry_est = float(closes[idx]) * (1.0 + float(row.price_change_60s) / 100.0)
                try:
                    win = df.iloc[max(0, idx - WINDOW_CANDLES + 1): idx + 1]
                    setup = calculate_smart_targets(None, sym, "SHORT", entry_est, df=win)
                    entry1 = float(setup["entry1"])
                    sl = float(setup["sl"])
                    targets = [float(t) for t in setup["targets"][:N_PUBLISHED]]
                    if not targets or sl <= 0 or entry1 <= 0:
                        raise ValueError("degenerate geometry")
                    # Replay ab idx+2: die Event-Kerze (idx+1) enthält den
                    # Pump-Run-up VOR unserem Entry — wick-aware First-Touch
                    # würde ihn fälschlich als SL-Riss zählen. Konservativ
                    # (schnelle TP-Treffer derselben Stunde entfallen ebenfalls);
                    # aim2-Präzedenz: --skip-entry-hour.
                    end = min(idx + 2 + HORIZON_CANDLES, len(times))
                    res = simulate_exit(
                        times[:end], highs[:end], lows[:end], closes[:end],
                        start_idx=idx + 2, direction="SHORT", entry=entry1, sl=sl,
                        targets=targets, n_published=len(targets),
                    )
                    event = {
                        "volume_ratio": row.volume_ratio,
                        "price_change_60s": row.price_change_60s,
                        "buy_pressure": row.buy_pressure,
                        "volatility": row.volatility,
                    }
                    feats = build_pex1_row(event, df, idx)
                except Exception:
                    stats["geometry_fail"] += 1
                    continue

                label = res.get("outcome_tp1")
                if res.get("exit_reason") == "open_at_end":
                    label = None  # Report-13-Regel: offene Trades nicht labeln
                fh.write(json.dumps({
                    "symbol": sym, "ts": pd.Timestamp(row.ts).isoformat(),
                    "direction": "SHORT", "weight": 1.0,
                    "entry": entry1, "sl": sl, "targets": targets,
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
