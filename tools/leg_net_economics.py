"""T-2026-KYT-9050-065 — what does a leg actually net, after REAL fees and funding?

Why this exists
---------------
Every evaluation in T-062/063/064 charged a flat 0.10 % round trip and ignored funding
entirely. Both were wrong, and for a high-frequency leg the first one decides the sign:

  * 0.10 % is taker/taker — the WORST case. On Binance USDⓈ-M with the BNB discount the
    real rates are maker 0.018 % and taker 0.045 %. Cornix fills a posted entry ZONE as
    a limit order (maker); a take-profit exit is likewise limit (maker); an SL, a regime
    close or a timeout is a market order (taker). So the round trip is 0.036 % for a
    TP-exiting trade and 0.063 % for one that exits at market — not 0.10 %.
  * Funding was missing. A short RECEIVES when the rate is positive and PAYS when it is
    negative, at each 8 h stamp inside its holding window.

Measured effect on EPD3 SHORT (8 466 trades): at a flat 0.10 % it nets −0.0005 %/trade,
i.e. exactly nothing. At its real blended fee it nets +0.0365 %/trade — +0.73 % of margin
at 20×, +309 points over three weeks. The leg did not change; the cost model did.

A warning the same measurement produced: the funding sign cannot be taken from the
universe. Across all symbols 82 % of rates are positive, which suggests shorts collect.
On the coins EPD3 actually shorts the trade-weighted funding is **negative** (−0.0099 %/
trade) — it shorts after pumps, and that is exactly where funding turns against a short.
Funding must be summed over each trade's own window on its own symbol, never assumed.

What this does NOT change
-------------------------
The market-adjusted residuals from `short_leg_trail_value.py`. Both sides of that
comparison pay the same round trip, so the fee cancels. A leg that trails below the
index still does — it simply loses less than a flat 0.10 % suggested.

Read-only. No writes, no live effect.

Usage:
    python tools/leg_net_economics.py --direction SHORT --start 2026-06-01
    python tools/leg_net_economics.py --direction SHORT --leverage 20 --min-trades 50
"""

from __future__ import annotations

import argparse
import bisect
import os
import sys
from collections import defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.time import LEGACY_WRITER_TZ  # noqa: E402

#: Binance USDⓈ-M with the BNB discount (operator-confirmed, 2026-08-01).
MAKER = 0.018
TAKER = 0.045

#: An unlevered move past this is a data bug, not a trade (mirrors wave_buildup_study).
MAX_ABS_MOVE_PCT = 100.0

#: Statuses whose exit is a LIMIT order (maker). Everything else closes at market:
#: SL hits, regime closes, horizon timeouts, delistings. Kept as an explicit allow-list
#: because assuming "market" for an unknown status is the conservative direction — it
#: charges the higher fee rather than flattering the leg.
MAKER_EXIT_MARKERS = ("ALL TARGETS HIT", "TARGET HIT", "TP HIT")


def exit_is_maker(status: str | None) -> bool:
    """True when the close was a resting limit order (take-profit)."""
    s = (status or "").upper()
    return any(m in s for m in MAKER_EXIT_MARKERS)


def round_trip_fee(status: str | None) -> float:
    """Entry is always a posted limit zone (maker); the exit decides the rest."""
    return MAKER + (MAKER if exit_is_maker(status) else TAKER)


def signed_move(entry: float, close_price: float, is_long: bool) -> float | None:
    if entry is None or close_price is None:
        return None
    entry, close_price = float(entry), float(close_price)
    if entry <= 0 or close_price <= 0:
        return None
    mv = ((close_price - entry) / entry * 100.0) if is_long else ((entry - close_price) / entry * 100.0)
    return None if abs(mv) > MAX_ABS_MOVE_PCT else mv


def funding_pct(rate_times: list, rates: list[float], ot, ct, is_long: bool) -> float:
    """Funding over one trade's window, in unlevered % of notional, signed by direction.

    A positive rate means longs pay shorts, so a SHORT collects it and a LONG pays. Only
    stamps strictly inside ``(ot, ct]`` count — a position opened after a stamp did not
    hold through it.
    """
    if not rate_times:
        return 0.0
    i = bisect.bisect_right(rate_times, ot)
    j = bisect.bisect_right(rate_times, ct)
    total = sum(rates[i:j]) * 100.0
    return -total if is_long else total


SQL_TRADES = """
    SELECT upper(model) AS tag, symbol, entry, close_price, status,
           open_time  AT TIME ZONE %(tz)s AS ot,
           close_time AT TIME ZONE %(tz)s AS ct
    FROM (
        SELECT DISTINCT ON (symbol, model, upper(btrim(direction)), open_time)
               model, symbol, entry, close_price, status, open_time, close_time, targets_hit
        FROM closed_ai_signals
        WHERE upper(btrim(direction)) = %(dir)s
          AND close_price IS NOT NULL AND close_price > 0
          AND entry IS NOT NULL AND entry > 0
          AND close_time IS NOT NULL
          AND (status IS NULL OR status NOT ILIKE '%%LEGACY%%')
          AND open_time >= %(since)s
        ORDER BY symbol, model, upper(btrim(direction)), open_time, close_time
    ) d
"""

SQL_FUNDING = """
    SELECT symbol, funding_time, funding_rate
    FROM funding_rates
    WHERE symbol IN %(syms)s AND funding_time BETWEEN %(lo)s AND %(hi)s
    ORDER BY symbol, funding_time
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--direction", default="SHORT", choices=("SHORT", "LONG"))
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--leverage", type=float, default=20.0)
    ap.add_argument("--min-trades", type=int, default=30)
    args = ap.parse_args()
    is_long = args.direction == "LONG"

    from core.database import get_db_connection

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(SQL_TRADES, {"tz": LEGACY_WRITER_TZ, "dir": args.direction, "since": args.start})
            trades = cur.fetchall()
            syms = tuple({r[1] for r in trades})
            if not syms:
                print("no trades")
                return 0
            lo = min(r[5] for r in trades)
            hi = max(r[6] for r in trades)
            cur.execute(SQL_FUNDING, {"syms": syms, "lo": lo, "hi": hi})
            fr: dict = defaultdict(lambda: ([], []))
            for sym, t, rate in cur.fetchall():
                fr[sym][0].append(t)
                fr[sym][1].append(float(rate))
    finally:
        conn.close()

    per: dict = defaultdict(lambda: {"g": [], "f": [], "fee": [], "maker": 0})
    for tag, sym, entry, close_price, status, ot, ct in trades:
        mv = signed_move(entry, close_price, is_long)
        if mv is None:
            continue
        times, rates = fr.get(sym, ([], []))
        p = per[tag]
        p["g"].append(mv)
        p["f"].append(funding_pct(times, rates, ot, ct, is_long))
        p["fee"].append(round_trip_fee(status))
        p["maker"] += 1 if exit_is_maker(status) else 0

    days = max(1.0, (hi - lo).total_seconds() / 86400.0)
    print("=" * 104)
    print("NET ECONOMICS PER LEG  (%s, %s → %s, maker %.3f %% / taker %.3f %%, %.0fx leverage)"
          % (args.direction, args.start, hi.date(), MAKER, TAKER, args.leverage))
    print("=" * 104)
    print("  %-13s %6s %6s %9s %9s %8s %6s %10s %9s %11s"
          % ("leg", "n", "/day", "gross", "funding", "fee", "TP%", "NET/trade", "xlev %", "sum xlev"))
    rows = []
    for tag, p in per.items():
        n = len(p["g"])
        if n == 0:
            continue
        g = sum(p["g"]) / n
        f = sum(p["f"]) / n
        fee = sum(p["fee"]) / n
        net = g + f - fee
        rows.append((tag, n, n / days, g, f, fee, 100.0 * p["maker"] / n,
                     net, net * args.leverage, net * args.leverage * n))
    for tag, n, per_day, g, f, fee, tp, net, lev, tot in sorted(rows, key=lambda r: -r[9]):
        if n < args.min_trades:
            continue
        print("  %-13s %6d %6.1f %+9.4f %+9.4f %8.4f %6.0f %+10.4f %+9.2f %+11.0f"
              % (tag, n, per_day, g, f, fee, tp, net, lev, tot))

    print()
    print("  fee is blended per leg from its own exit mix: maker+maker on a TP exit,")
    print("  maker+taker on SL / regime close / timeout. TP%% is the maker-exit share.")
    print("  gross and funding are unlevered %%; xlev is per-trade in %% of margin.")
    print("  Funding is summed over each trade's OWN window on its OWN symbol — the")
    print("  universe median points the other way and must not be substituted for it.")
    print("  These are ABSOLUTE returns. They do NOT say whether a leg beat the market;")
    print("  for that see tools/short_leg_trail_value.py, whose residuals are fee-independent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
