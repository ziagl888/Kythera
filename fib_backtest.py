"""
fib_backtest_v3.py — Fibonacci Backtest (korrigierte Logik)

Kernproblem der v2: Zu viele False-Setups durch:
  - Zu enges SL (nur 0.5% über Swing)
  - 0.786-Level ist kein valider Entry (zu tief im Retracement)
  - Jeder 8%-Swing wird als Setup gewertet (zu viele schlechte Setups)
  - LONG-SL zu nah am Entry gesetzt

Fixes in v3:
  - SL = ganzer Swing-Abstand * SL_BUFFER (realistisches SL)
  - Nur Fib-Levels 0.382, 0.5, 0.618 als Entry (klassische Levels)
  - Min-Swing auf 15% erhöht (nur signifikante Swings)
  - Qualitäts-Filter: Swing muss sich von Vorgänger-Swing abheben
  - Exit: T1 bei 1.0 (Wiederholung des Swings), T2/T3 Extensions

Aufruf:
    py fib_backtest_v3.py
    py fib_backtest_v3.py --direction short
    py fib_backtest_v3.py --min-swing 20
    py fib_backtest_v3.py --entry-levels 0.382 0.5 0.618
"""

from __future__ import annotations

import argparse
import os
import warnings
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv

from core.market_utils import load_coins as _core_load_coins

warnings.filterwarnings("ignore")
load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# KONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
ENTRY_FIB_LEVELS = [0.382, 0.5, 0.618]  # Nur klassische Entry-Levels
FIB_TARGETS = [1.0, 1.272, 1.618, 2.0, 2.618]

SWING_LOOKBACK = 5  # Kerzen links+rechts für Swing
MIN_SWING_PCT = 15.0  # Mindest-Swing 15% (nur signifikante Swings)
FIB_ENTRY_TOL = 0.02  # ±2% Toleranz (etwas großzügiger)
MAX_RETRACE_BARS = 15  # Max Kerzen für Retracement-Einstieg
SL_BUFFER = 0.03  # SL = 3% jenseits des Swing-Extrems
BACKTEST_DAYS = 365
COINS_FILE = "coins.json"


# ─────────────────────────────────────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────────────────────────────────────


def get_conn():
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME", "cryptodata"),
        user=os.getenv("DB_USER", "dbfiller"),
        password=os.getenv("DB_PASSWORD", ""),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
    )


def load_coins() -> list[str]:
    # Delegates to core.market_utils.load_coins (P3.1). The ["BTCUSDT","ETHUSDT"]
    # fallback is kept for the unreadable/empty-coins.json case so this offline
    # backtest never runs on an empty universe.
    return _core_load_coins(COINS_FILE, usdt_only=True, uppercase=True) or ["BTCUSDT", "ETHUSDT"]


def load_ohlcv(conn, symbol: str, since: datetime) -> pd.DataFrame | None:
    candidates = [f"{symbol}_1d", f"{symbol.lower()}_1d"]
    table = None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename=ANY(%s) LIMIT 1", (candidates,)
        )
        row = cur.fetchone()
        if row:
            table = row[0]
    if not table:
        return None
    try:
        df = pd.read_sql_query(
            f'SELECT open_time,open,high,low,close,volume FROM "{table}" WHERE open_time>=%s ORDER BY open_time ASC',
            conn,
            params=(since,),
        )
    except Exception:
        return None
    if df.empty or len(df) < SWING_LOOKBACK * 2 + 5:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df.set_index("open_time")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["high", "low", "close"])


# ─────────────────────────────────────────────────────────────────────────────
# FIBONACCI
# ─────────────────────────────────────────────────────────────────────────────


def fib_levels(high: float, low: float) -> dict[float, float]:
    """Retracement-Levels von High → Low Richtung."""
    diff = high - low
    return {lvl: high - diff * lvl for lvl in ENTRY_FIB_LEVELS}


def fib_ext_short(high: float, low: float) -> dict[float, float]:
    """Extension-Targets unterhalb des Lows (SHORT Targets)."""
    diff = high - low
    return {lvl: low - diff * (lvl - 1.0) for lvl in FIB_TARGETS}


def fib_ext_long(low: float, high: float) -> dict[float, float]:
    """Extension-Targets oberhalb des Highs (LONG Targets)."""
    diff = high - low
    return {lvl: high + diff * (lvl - 1.0) for lvl in FIB_TARGETS}


def nearest_fib(price: float, levels: dict[float, float]) -> tuple[float, float] | None:
    best, best_dist = None, float("inf")
    for ratio, lp in levels.items():
        if lp <= 0:
            continue
        d = abs(price - lp) / lp
        if d < best_dist:
            best_dist, best = d, (ratio, lp)
    return best if best and best_dist <= FIB_ENTRY_TOL else None


# ─────────────────────────────────────────────────────────────────────────────
# SWING-ERKENNUNG
# ─────────────────────────────────────────────────────────────────────────────


def find_swings(df: pd.DataFrame) -> tuple[list[int], list[int]]:
    """Gibt (swing_high_indices, swing_low_indices) zurück."""
    h = df["high"].values
    low = df["low"].values
    n = len(h)
    SH, SL = [], []
    for i in range(SWING_LOOKBACK, n - SWING_LOOKBACK):
        wh = h[i - SWING_LOOKBACK : i + SWING_LOOKBACK + 1]
        wl = low[i - SWING_LOOKBACK : i + SWING_LOOKBACK + 1]
        if h[i] == max(wh) and list(wh).count(h[i]) == 1:
            SH.append(i)
        if low[i] == min(wl) and list(wl).count(low[i]) == 1:
            SL.append(i)
    return SH, SL


# ─────────────────────────────────────────────────────────────────────────────
# TRADE DATACLASS
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Trade:
    symbol: str
    direction: Literal["LONG", "SHORT"]
    entry_date: datetime
    entry_price: float
    sl_price: float
    swing_high: float
    swing_low: float
    entry_fib: float
    swing_pct: float  # Größe des Swings in %
    targets: dict[float, float]
    targets_hit: list[float] = field(default_factory=list)
    exit_price: float = 0.0
    exit_date: datetime | None = None
    exit_reason: str = ""

    @property
    def risk(self) -> float:
        return abs(self.entry_price - self.sl_price) or 1e-9

    @property
    def is_win(self) -> bool:
        return len(self.targets_hit) >= 1

    @property
    def pnl_r(self) -> float:
        if self.direction == "SHORT":
            return round((self.entry_price - self.exit_price) / self.risk, 2)
        return round((self.exit_price - self.entry_price) / self.risk, 2)

    @property
    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        if self.direction == "SHORT":
            return (self.entry_price - self.exit_price) / self.entry_price * 100
        return (self.exit_price - self.entry_price) / self.entry_price * 100


# ─────────────────────────────────────────────────────────────────────────────
# SETUP-SUCHE
# ─────────────────────────────────────────────────────────────────────────────


def find_setups(df: pd.DataFrame, symbol: str, direction: str | None) -> list[Trade]:
    """
    Sucht Fib-Setups mit verbesserter Logik:

    SHORT:
      1. Swing-High erkannt
      2. Danach ein tieferes Low (min. MIN_SWING_PCT unter Swing-High)
      3. Retracement zurück zu 0.382 / 0.5 / 0.618
      4. Bestätigung: Close der Entry-Kerze UNTER dem Retracement-Level
         (Kerze schließt bereits wieder runter = Richtungsbestätigung)
      5. SL = Swing-High + SL_BUFFER%

    LONG:
      1. Swing-Low erkannt
      2. Danach ein höheres High (min. MIN_SWING_PCT über Swing-Low)
      3. Retracement zurück zu 0.382 / 0.5 / 0.618
      4. Bestätigung: Close der Entry-Kerze ÜBER dem Retracement-Level
      5. SL = Swing-Low - SL_BUFFER%
    """
    trades = []
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    dates = df.index.tolist()
    n = len(df)

    swing_high_idxs, swing_low_idxs = find_swings(df)

    # ── SHORT Setups ──────────────────────────────────────────────────────────
    if direction != "long":
        for sh_idx in swing_high_idxs:
            sh_price = highs[sh_idx]

            # Suche das Tief nach dem Swing-High (innerhalb MAX_RETRACE_BARS*2)
            search_end = min(sh_idx + MAX_RETRACE_BARS * 2, n)
            seg_lows = lows[sh_idx + 1 : search_end]
            if len(seg_lows) == 0:
                continue
            min_offset = int(np.argmin(seg_lows))
            min_low_idx = sh_idx + 1 + min_offset
            min_low = lows[min_low_idx]

            # Swing groß genug?
            swing_pct = (sh_price - min_low) / sh_price * 100
            if swing_pct < MIN_SWING_PCT:
                continue

            # Das Tief sollte idealerweise ein Swing-Low sein
            # (tolerant: akzeptiere auch wenn es kein perfektes Swing-Low ist)

            # Fib-Retracement berechnen
            retr = fib_levels(sh_price, min_low)
            ext_tgt = fib_ext_short(sh_price, min_low)

            # Suche Retracement-Entry nach dem Tief
            search_end2 = min(min_low_idx + MAX_RETRACE_BARS, n)
            for j in range(min_low_idx + 1, search_end2):
                # Nutze Kerzen-High für Retracement-Check (nicht nur Close)
                candle_high = highs[j]
                candle_close = closes[j]

                hit = nearest_fib(candle_high, retr)
                if hit is None:
                    # auch Close prüfen
                    hit = nearest_fib(candle_close, retr)
                if hit is None:
                    continue

                fib_ratio, fib_price = hit

                # RICHTUNGSBESTÄTIGUNG: Close der Entry-Kerze muss UNTER dem
                # Retracement-Level schließen (bearishe Ablehnung)
                if candle_close >= fib_price:
                    continue  # Kerze schließt noch oben → keine Bestätigung

                entry_price = candle_close
                sl_price = sh_price * (1 + SL_BUFFER)

                # Risk darf nicht mehr als 50% des Swing-Abstands sein
                risk = sl_price - entry_price
                swing_range = sh_price - min_low
                if risk > swing_range * 0.5:
                    continue

                tgts = {k: v for k, v in ext_tgt.items() if v < entry_price}
                if not tgts:
                    continue

                trades.append(
                    Trade(
                        symbol=symbol,
                        direction="SHORT",
                        entry_date=dates[j],
                        entry_price=entry_price,
                        sl_price=sl_price,
                        swing_high=sh_price,
                        swing_low=min_low,
                        entry_fib=fib_ratio,
                        swing_pct=swing_pct,
                        targets=tgts,
                    )
                )
                break  # Ein Entry pro Swing

    # ── LONG Setups ───────────────────────────────────────────────────────────
    if direction != "short":
        for sl_idx in swing_low_idxs:
            sl_price = lows[sl_idx]

            search_end = min(sl_idx + MAX_RETRACE_BARS * 2, n)
            seg_highs = highs[sl_idx + 1 : search_end]
            if len(seg_highs) == 0:
                continue
            max_offset = int(np.argmax(seg_highs))
            max_high_idx = sl_idx + 1 + max_offset
            max_high = highs[max_high_idx]

            swing_pct = (max_high - sl_price) / sl_price * 100
            if swing_pct < MIN_SWING_PCT:
                continue

            retr = fib_levels(max_high, sl_price)
            ext_tgt = fib_ext_long(sl_price, max_high)

            search_end2 = min(max_high_idx + MAX_RETRACE_BARS, n)
            for j in range(max_high_idx + 1, search_end2):
                candle_low = lows[j]
                candle_close = closes[j]

                hit = nearest_fib(candle_low, retr)
                if hit is None:
                    hit = nearest_fib(candle_close, retr)
                if hit is None:
                    continue

                fib_ratio, fib_price = hit

                # RICHTUNGSBESTÄTIGUNG: Close ÜBER dem Fib-Level (bullishe Ablehnung)
                if candle_close <= fib_price:
                    continue

                entry_price = candle_close
                sl_price_t = sl_price * (1 - SL_BUFFER)

                risk = entry_price - sl_price_t
                swing_range = max_high - sl_price
                if risk > swing_range * 0.5:
                    continue

                tgts = {k: v for k, v in ext_tgt.items() if v > entry_price}
                if not tgts:
                    continue

                trades.append(
                    Trade(
                        symbol=symbol,
                        direction="LONG",
                        entry_date=dates[j],
                        entry_price=entry_price,
                        sl_price=sl_price_t,
                        swing_high=max_high,
                        swing_low=sl_price,
                        entry_fib=fib_ratio,
                        swing_pct=swing_pct,
                        targets=tgts,
                    )
                )
                break

    return trades


# ─────────────────────────────────────────────────────────────────────────────
# SIMULATION (mit SL-Trailing)
# ─────────────────────────────────────────────────────────────────────────────


def simulate(trade: Trade, df: pd.DataFrame) -> Trade:
    """
    Simulation mit Trailing-SL:
      - Nach T1: SL → Break-Even
      - Nach T2+: SL → vorheriges Target
    """
    entry_dt = trade.entry_date
    entry_idx = None
    try:
        entry_idx = df.index.get_loc(entry_dt)
    except KeyError:
        for i, d in enumerate(df.index):
            if d.replace(tzinfo=None) == entry_dt.replace(tzinfo=None):
                entry_idx = i
                break
    if entry_idx is None:
        trade.exit_price = trade.entry_price
        trade.exit_reason = "END"
        trade.exit_date = trade.entry_date
        return trade

    highs = df["high"].values
    lows = df["low"].values
    dates = df.index.tolist()
    n = len(df)

    sorted_tgts = sorted(
        trade.targets.items(),
        key=lambda x: x[1],
        reverse=(trade.direction == "SHORT"),
    )
    t_idx = 0
    n_tgts = len(sorted_tgts)
    curr_sl = trade.sl_price

    for i in range(entry_idx + 1, n):
        h, low = highs[i], lows[i]

        if trade.direction == "SHORT":
            if h >= curr_sl:
                trade.exit_price = curr_sl
                trade.exit_date = dates[i]
                trade.exit_reason = "SL" if t_idx == 0 else f"T{t_idx}_SL"
                return trade
            while t_idx < n_tgts and low <= sorted_tgts[t_idx][1]:
                trade.targets_hit.append(sorted_tgts[t_idx][0])
                curr_sl = trade.entry_price if t_idx == 0 else sorted_tgts[t_idx - 1][1]
                t_idx += 1
            if t_idx >= n_tgts:
                trade.exit_price = sorted_tgts[-1][1]
                trade.exit_date = dates[i]
                trade.exit_reason = f"T{n_tgts}"
                return trade
        else:
            if low <= curr_sl:
                trade.exit_price = curr_sl
                trade.exit_date = dates[i]
                trade.exit_reason = "SL" if t_idx == 0 else f"T{t_idx}_SL"
                return trade
            while t_idx < n_tgts and h >= sorted_tgts[t_idx][1]:
                trade.targets_hit.append(sorted_tgts[t_idx][0])
                curr_sl = trade.entry_price if t_idx == 0 else sorted_tgts[t_idx - 1][1]
                t_idx += 1
            if t_idx >= n_tgts:
                trade.exit_price = sorted_tgts[-1][1]
                trade.exit_date = dates[i]
                trade.exit_reason = f"T{n_tgts}"
                return trade

    trade.exit_price = float(df["close"].iloc[-1])
    trade.exit_date = dates[-1]
    trade.exit_reason = "END"
    return trade


# ─────────────────────────────────────────────────────────────────────────────
# ERGEBNIS
# ─────────────────────────────────────────────────────────────────────────────


def print_results(trades: list[Trade], direction_filter: str | None) -> None:
    done = [t for t in trades if t.exit_reason != ""]
    if not done:
        print("\n❌ Keine abgeschlossenen Trades.")
        return

    n = len(done)
    n_sl = sum(1 for t in done if t.exit_reason == "SL")
    n_win = sum(1 for t in done if t.is_win)
    n_t = [sum(1 for t in done if len(t.targets_hit) >= i) for i in range(1, 6)]
    wr = n_win / n * 100
    avg_r = sum(t.pnl_r for t in done) / n
    tot_r = sum(t.pnl_r for t in done)
    avg_p = sum(t.pnl_pct for t in done) / n

    wins = [t for t in done if t.is_win]
    losses = [t for t in done if not t.is_win]
    aw_r = sum(t.pnl_r for t in wins) / len(wins) if wins else 0
    al_r = sum(t.pnl_r for t in losses) / len(losses) if losses else 0

    longs = [t for t in done if t.direction == "LONG"]
    shorts = [t for t in done if t.direction == "SHORT"]

    coin_counts = Counter(t.symbol for t in done)
    entry_dist = Counter(round(t.entry_fib, 3) for t in done)

    # Swing-Größe Analyse
    swing_buckets = {"15-25%": 0, "25-40%": 0, "40-60%": 0, "60%+": 0}
    for t in done:
        if t.swing_pct < 25:
            swing_buckets["15-25%"] += 1
        elif t.swing_pct < 40:
            swing_buckets["25-40%"] += 1
        elif t.swing_pct < 60:
            swing_buckets["40-60%"] += 1
        else:
            swing_buckets["60%+"] += 1

    # WR nach Swing-Größe
    swing_wr = {}
    for bucket in swing_buckets:
        bucket_trades = []
        for t in done:
            in_bucket = (
                (bucket == "15-25%" and 15 <= t.swing_pct < 25)
                or (bucket == "25-40%" and 25 <= t.swing_pct < 40)
                or (bucket == "40-60%" and 40 <= t.swing_pct < 60)
                or (bucket == "60%+" and t.swing_pct >= 60)
            )
            if in_bucket:
                bucket_trades.append(t)
        if bucket_trades:
            wr_b = sum(1 for t in bucket_trades if t.is_win) / len(bucket_trades) * 100
            avg_b = sum(t.pnl_r for t in bucket_trades) / len(bucket_trades)
            swing_wr[bucket] = (len(bucket_trades), wr_b, avg_b)

    def chance_move(pct: float) -> float:
        hits = sum(
            1
            for t in done
            if any(
                abs(t.targets.get(r, t.entry_price) - t.entry_price) / t.entry_price * 100 >= pct for r in t.targets_hit
            )
        )
        return hits / n * 100

    W = 64
    print("\n" + "═" * W)
    print("  FIB BACKTEST v3 ERGEBNISSE")
    print("═" * W)
    print(f"  Zeitraum:       letzte {BACKTEST_DAYS} Tage (1D-Kerzen)")
    print(f"  Richtung:       {direction_filter.upper() if direction_filter else 'LONG + SHORT'}")
    print(f"  Entry-Levels:   {ENTRY_FIB_LEVELS}")
    print(
        f"  Swing min:      {MIN_SWING_PCT}%  |  SL-Buffer: {SL_BUFFER * 100:.0f}%  |  Tol: ±{FIB_ENTRY_TOL * 100:.0f}%"
    )
    print()
    print(f"  Trades gesamt:  {n}")
    if longs and shorts:
        wr_l = sum(1 for t in longs if t.is_win) / len(longs) * 100
        wr_s = sum(1 for t in shorts if t.is_win) / len(shorts) * 100
        al_l = sum(t.pnl_r for t in longs) / len(longs)
        al_s = sum(t.pnl_r for t in shorts) / len(shorts)
        print(f"    LONG:  {len(longs):>4}  WR {wr_l:>4.1f}%  Avg {al_l:+.2f}R")
        print(f"    SHORT: {len(shorts):>4}  WR {wr_s:>4.1f}%  Avg {al_s:+.2f}R")
    print()
    print(f"  {'── PERFORMANCE ':-<{W - 4}}")
    print(f"  Win-Rate:       {wr:.1f}%")
    print(f"  SL getroffen:   {n_sl} ({n_sl / n * 100:.1f}%)")
    print(f"  Avg PnL:        {avg_r:+.2f}R  /  {avg_p:+.1f}%")
    print(f"  Total PnL:      {tot_r:+.1f}R")
    if wins:
        print(f"  Avg Win:        {aw_r:+.2f}R")
    if losses:
        print(f"  Avg Loss:       {al_r:+.2f}R")
    if wins and losses and al_r != 0:
        print(f"  Risk/Reward:    {abs(aw_r / al_r):.2f}:1")
    print()
    print(f"  {'── TARGETS ':-<{W - 4}}")
    labels = ["1.000", "1.272", "1.618", "2.000", "2.618"]
    max_t = n_t[0] if n_t[0] > 0 else 1
    for i, (lbl, cnt) in enumerate(zip(labels, n_t, strict=False)):
        bar = "█" * (cnt * 25 // max_t)
        print(f"  T{i + 1} ({lbl}):  {cnt:>4} ({cnt / n * 100:4.1f}%)  {bar}")
    print()
    print(f"  Chance ≥50% Bewegung:  {chance_move(50):.1f}%")
    print(f"  Chance ≥70% Bewegung:  {chance_move(70):.1f}%")
    print()
    print(f"  {'── SWING-GRÖSSE vs. PERFORMANCE ':-<{W - 4}}")
    for bucket, (cnt, wr_b, avg_b) in swing_wr.items():
        print(f"  {bucket:<10} {cnt:>4} Trades  WR {wr_b:>4.1f}%  Avg {avg_b:+.2f}R")
    print()
    print(f"  {'── ENTRY-FIB-LEVELS ':-<{W - 4}}")
    max_e = max(entry_dist.values()) if entry_dist else 1
    for lvl, cnt in sorted(entry_dist.items()):
        bar = "█" * (cnt * 20 // max_e)
        wr_l2 = sum(1 for t in done if round(t.entry_fib, 3) == lvl and t.is_win)
        wr_l2 = wr_l2 / cnt * 100 if cnt else 0
        print(f"  {lvl:.3f}:  {cnt:>4} Trades  WR {wr_l2:>4.1f}%  {bar}")
    print()
    print(f"  {'── TOP 10 COINS ':-<{W - 4}}")
    for coin, cnt in coin_counts.most_common(10):
        wt = [t for t in done if t.symbol == coin]
        wrc = sum(1 for t in wt if t.is_win) / len(wt) * 100
        avg = sum(t.pnl_r for t in wt) / len(wt)
        print(f"  {coin:<16} {cnt:>3} Trades  WR {wrc:>4.1f}%  Avg {avg:+.2f}R")
    print()
    print(f"  {'── BEWERTUNG ':-<{W - 4}}")
    if tot_r > 0 and wr >= 45:
        verdict = "✅ STRATEGIE PROFITABEL"
    elif tot_r > 0:
        verdict = "⚠️  LEICHT POSITIV — weiteres Testing empfohlen"
    elif wr >= 40 and abs(aw_r) > abs(al_r) * 1.2:
        verdict = "⚠️  WR niedrig aber gutes R/R — Parameter tunen"
    else:
        verdict = "❌ NICHT PROFITABEL in diesem Zeitraum/Parameterset"
    print(f"  {verdict}")
    print("═" * W + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    global MIN_SWING_PCT, FIB_ENTRY_TOL, SL_BUFFER, ENTRY_FIB_LEVELS

    parser = argparse.ArgumentParser(description="Fib Backtest v3")
    parser.add_argument("--direction", choices=["long", "short"], default=None)
    parser.add_argument("--top", type=int, default=None)
    parser.add_argument("--min-swing", type=float, default=MIN_SWING_PCT)
    parser.add_argument("--tolerance", type=float, default=FIB_ENTRY_TOL)
    parser.add_argument("--sl-buffer", type=float, default=SL_BUFFER)
    parser.add_argument(
        "--entry-levels",
        type=float,
        nargs="+",
        default=ENTRY_FIB_LEVELS,
        help="Fib Entry-Levels z.B. --entry-levels 0.382 0.5 0.618",
    )
    args = parser.parse_args()

    MIN_SWING_PCT = args.min_swing
    FIB_ENTRY_TOL = args.tolerance
    SL_BUFFER = args.sl_buffer
    ENTRY_FIB_LEVELS = args.entry_levels

    since = datetime.now(timezone.utc) - timedelta(days=BACKTEST_DAYS)
    coins = load_coins()
    if args.top:
        coins = coins[: args.top]

    print("\n🔍 Fib Backtest v3")
    print(f"   Coins:       {len(coins)}")
    print(f"   Entry-Levels: {ENTRY_FIB_LEVELS}")
    print(f"   Min-Swing:   {MIN_SWING_PCT}%  |  SL-Buffer: {SL_BUFFER * 100:.0f}%\n")

    conn = get_conn()
    all_trades: list[Trade] = []
    n_ok = n_miss = 0

    for i, symbol in enumerate(coins):
        if (i + 1) % 100 == 0:
            print(f"  [{i + 1}/{len(coins)}] — {n_ok} Coins, {len(all_trades)} Setups...")
        df = load_ohlcv(conn, symbol, since)
        if df is None:
            n_miss += 1
            continue
        n_ok += 1
        for trade in find_setups(df, symbol, args.direction):
            simulate(trade, df)
            all_trades.append(trade)

    conn.close()
    print(f"\n✅ Fertig: {n_ok} Coins, {n_miss} ohne Daten, {len(all_trades)} Setups")
    print_results(all_trades, args.direction)


if __name__ == "__main__":
    main()
