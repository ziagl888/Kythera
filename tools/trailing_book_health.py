"""T-2026-KYT-9050-052 — can the trailing arm be made profitable, measured on the OPEN BOOK?

Why this tool exists
--------------------
`tools/trailing_slot_budget.py` (PR #198) scored exit rules by REALISED sums and
slot occupancy. That metric has a blind spot this task exists to close: a rule
that closes winners and keeps losers looks great in realised sums while the open
book rots. Bot 40's live book proved it within a day — the trail (armed only
above +2 % peak) can, by construction, only ever close trades that were winners;
losers stay until the fleet's SL fires. In a falling market the book self-selects
into underwater longs, and the arm trails away exactly the shorts that cushion
the hold-arm's book.

So every candidate exit rule here is scored on BOTH sides of the ledger:

  * realised: net sum (0.10 % fee), per-trade, slot occupancy, net per slot-day;
  * the open book over time: count per direction, mean open mark, share of the
    book underwater, and — the headline — max drawdown of the TOTAL equity curve
    (cumulative realised + open mark-to-market), in unlevered %-points.

Population
----------
Deduped `closed_ai_signals` closes since --start (default 2026-03-01, skipping
the Feb LEGACY blob), restricted to the 33 roster legs (`core.trailing_roster`)
that the live arm actually mirrors, LIVE per `shadow_gate.leg_status` per
(tag, direction) — and EXCLUDING ROM1 both directions: ROM1 (bot 28) re-forwards
trades the original legs already post, so counting it doubles those trades
(it was 10 334 % of the 49 204 % headline in PR #198). The live bot suppresses
most ROM1 duplicates anyway through its one-position-per-symbol rule.

Rules simulated (all causal, prior-peak semantics — a trail may only fire against
a peak established on a STRICTLY earlier candle; index 0 never triggers):

  hold                 natural close (the fleet's own SL/TP/timeout)
  trail-a2 / a5 / a10  per-trade trail, x=10 % give-back, activation 2/5/10 %
  trail-a2+ts24/48/72  trail plus a time-stop: a trade that never armed within
                       T hours is closed at the market (candle close). This is
                       the symmetric loser rule — the trail removes winners,
                       the time-stop removes the losers the trail cannot see.
  trail-a2+hs2         trail plus a hard stop at −2 % unlevered (wick-based,
                       filled at the stop level) — the tighter-SL alternative.
  trail-a2-short-only  SHORT legs trail, LONG legs hold (and the mirror image).
  trail-a2-partial50   at the trail trigger close HALF, the rest rides to the
                       natural close — evaporation insurance instead of exit.
  ptf-y10 / y15        NO per-trade trail; flatten the whole book when its
                       aggregate open mark retraces y from its running peak
                       (`core.wave_exit_sim.portfolio_circuit_breaker`).

Read-only: SELECTs against closed_ai_signals + candles. No writes, no live effect.
One sim job at a time on this machine (repo rule).

Usage:
    python tools/trailing_book_health.py --start 2026-03-01 --tf 15m
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import timedelta

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.wave_exit_sim import portfolio_circuit_breaker  # noqa: E402
from tools.trailing_slot_budget import _naive, prior_peak, trail_exit  # noqa: E402
from tools.wave_buildup_study import DEFAULT_OUT_DIR, load_trades, read_coin_wick  # noqa: E402

HOUR = np.timedelta64(1, "h")
FEE_RT = 0.10  # taker round-trip %, repo convention (tools/audit/step4_results.py)
X_FRAC = 0.10  # give-back fraction — the operator's live setting, held fixed here
ACT_LIVE = 2.0  # the live activation; the sweep varies it explicitly


# ─────────────────────────────────────────────────────────────────────────────
# PER-TRADE SERIES (one candle read per coin, shared by every rule)
# ─────────────────────────────────────────────────────────────────────────────


def attach_series(conn, trades: list[dict], tf: str, grid0: np.datetime64, glen: int) -> int:
    """Attach per-trade candle-derived series, in place. Returns #trades w/o candles.

    Per trade:
      fav/adv  favourable / adverse wick move per candle (unlev %, signed)
      cm       close mark per candle (unlev %, signed)
      tt       candle open_times (datetime64[ns], ascending)
      gi0/gie  grid hour of open / natural close (gie exclusive, >= gi0+1)
      hm       hourly close mark on [gi0, gie) (carry-forward across candle gaps)
    """
    by_coin: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_coin[t["sym"]].append(t)

    t0 = time.time()
    no_candle = 0
    for ci, (sym, tl) in enumerate(sorted(by_coin.items()), 1):
        lo = min(t["ot"] for t in tl) - timedelta(hours=2)
        hi = max(t["ct"] for t in tl) + timedelta(hours=2)
        cd = read_coin_wick(conn, sym, lo, hi, tf)
        covered = len(cd["t"]) > 0
        for t in tl:
            ot64 = np.datetime64(_naive(t["ot"]))
            ct64 = np.datetime64(_naive(t["ct"]))
            t["gi0"] = int((ot64.astype("datetime64[h]") - grid0) / HOUR)
            gie = int((ct64.astype("datetime64[h]") - grid0) / HOUR)
            t["gie"] = max(gie, t["gi0"] + 1)  # a sub-hour trade still occupies its hour
            t["series"] = False
            if not covered:
                no_candle += 1
                continue
            m = (cd["t"] >= ot64) & (cd["t"] <= ct64)
            if not m.any():
                no_candle += 1
                continue
            hh, ll, cc, tt = cd["h"][m], cd["l"][m], cd["c"][m], cd["t"][m]
            e, is_long = t["entry"], t["dir"] == "LONG"
            if is_long:
                t["fav"] = (hh - e) / e * 100.0
                t["adv"] = (ll - e) / e * 100.0
                t["cm"] = (cc - e) / e * 100.0
            else:
                t["fav"] = (e - ll) / e * 100.0
                t["adv"] = (e - hh) / e * 100.0
                t["cm"] = (e - cc) / e * 100.0
            t["tt"] = tt
            # hourly close-mark, carry-forward: for each grid hour, the close of
            # the last candle at or before it (a coin with a candle gap keeps its
            # last known mark instead of dropping out of the book).
            hours = np.arange(t["gi0"], t["gie"], dtype=np.int64)
            hour_ts = (grid0 + hours * HOUR).astype("datetime64[ns]")
            pos = np.clip(np.searchsorted(tt, hour_ts, side="right") - 1, 0, len(tt) - 1)
            t["hm"] = t["cm"][pos]
            t["series"] = True
        if ci % 200 == 0 or ci == len(by_coin):
            print(f"  [{ci}/{len(by_coin)}] {time.time() - t0:.0f}s", flush=True)
    return no_candle


# ─────────────────────────────────────────────────────────────────────────────
# EXIT RULES — each returns (exit_grid_idx, realised_unlev, weight_after) per trade
# ─────────────────────────────────────────────────────────────────────────────


def _gi_of(t: dict, k: int, grid0: np.datetime64) -> int:
    """Grid hour of candle k, clamped into the trade's own [gi0+1, gie] window."""
    g = int((t["tt"][k].astype("datetime64[h]") - grid0) / HOUR)
    return max(t["gi0"] + 1, min(g, t["gie"]))


def exit_trail(t: dict, grid0: np.datetime64, act: float) -> tuple[int, float]:
    """The live rule: x=10 % give-back once the prior peak cleared `act`."""
    if not t["series"]:
        return t["gie"], t["real_unlev"]
    k = trail_exit(t["fav"], t["adv"], X_FRAC, act)
    if k is None:
        return t["gie"], t["real_unlev"]
    val = float(prior_peak(t["fav"])[k] * (1.0 - X_FRAC))
    return _gi_of(t, k, grid0), val


def exit_trail_timestop(t: dict, grid0: np.datetime64, act: float, ts_hours: float) -> tuple[int, float]:
    """Trail, plus: never armed within `ts_hours` → close at the candle close.

    Armed trades are the trail's business (an armed trade can never become a deep
    loser — the trail floor sits at act*(1-x) > 0). The time-stop only touches the
    population the trail structurally cannot: trades that never reached the floor.
    """
    if not t["series"]:
        return t["gie"], t["real_unlev"]
    k = trail_exit(t["fav"], t["adv"], X_FRAC, act)
    deadline = np.datetime64(_naive(t["ot"])) + np.timedelta64(int(ts_hours * 3600), "s")
    j = int(np.searchsorted(t["tt"], deadline.astype("datetime64[ns]"), side="left"))
    if j >= len(t["tt"]):  # closed naturally before the deadline
        if k is None:
            return t["gie"], t["real_unlev"]
        return _gi_of(t, k, grid0), float(prior_peak(t["fav"])[k] * (1.0 - X_FRAC))
    armed_by_j = bool(prior_peak(t["fav"])[j] > act) or (k is not None and k <= j)
    if armed_by_j:
        if k is None:
            return t["gie"], t["real_unlev"]
        return _gi_of(t, k, grid0), float(prior_peak(t["fav"])[k] * (1.0 - X_FRAC))
    return _gi_of(t, j, grid0), float(t["cm"][j])


def exit_trail_hardstop(t: dict, grid0: np.datetime64, act: float, stop: float) -> tuple[int, float]:
    """Trail, plus a wick-based hard stop at −`stop` % unlevered, filled at the level."""
    if not t["series"]:
        return t["gie"], t["real_unlev"]
    k = trail_exit(t["fav"], t["adv"], X_FRAC, act)
    hs = np.flatnonzero(t["adv"] <= -stop)
    j = int(hs[0]) if len(hs) else None
    if j is not None and (k is None or j < k):
        return _gi_of(t, j, grid0), -stop
    if k is None:
        return t["gie"], t["real_unlev"]
    return _gi_of(t, k, grid0), float(prior_peak(t["fav"])[k] * (1.0 - X_FRAC))


def exit_one_sided(t: dict, grid0: np.datetime64, act: float, trail_dir: str) -> tuple[int, float]:
    """Trail only one direction; the other side holds to its natural close."""
    if t["dir"] != trail_dir:
        return t["gie"], t["real_unlev"]
    return exit_trail(t, grid0, act)


# ─────────────────────────────────────────────────────────────────────────────
# BOOK AGGREGATION
# ─────────────────────────────────────────────────────────────────────────────


class Book:
    """Hourly open-book accumulators + realised events for one rule."""

    def __init__(self, glen: int):
        self.glen = glen
        self.cnt = {"LONG": np.zeros(glen), "SHORT": np.zeros(glen)}
        self.msum = {"LONG": np.zeros(glen), "SHORT": np.zeros(glen)}
        self.neg = np.zeros(glen)
        self.rz = np.zeros(glen + 1)
        self.n = 0
        self.values: list[float] = []
        self.slot_hours = 0.0

    def add(self, t: dict, exit_gi: int, value: float, weight: float = 1.0, fee: float = FEE_RT) -> None:
        d = t["dir"]
        gi0 = t["gi0"]
        exit_gi = max(gi0 + 1, min(exit_gi, self.glen))
        self.cnt[d][gi0:exit_gi] += weight
        if t["series"]:
            hm = t["hm"][: exit_gi - gi0]
            self.msum[d][gi0 : gi0 + len(hm)] += weight * hm
            self.neg[gi0 : gi0 + len(hm)] += weight * (hm < 0)
        self.rz[min(exit_gi, self.glen)] += weight * (value - fee)
        self.n += weight
        self.values.append(weight * (value - fee))
        self.slot_hours += weight * (exit_gi - gi0)

    def stats(self) -> dict:
        cnt_all = self.cnt["LONG"] + self.cnt["SHORT"]
        msum_all = self.msum["LONG"] + self.msum["SHORT"]
        rz_cum = np.cumsum(self.rz)[1:]
        equity = rz_cum + msum_all
        peak = np.maximum.accumulate(equity)
        dd = peak - equity
        live = cnt_all > 0
        slot_days = self.slot_hours / 24.0
        net = float(sum(self.values))
        return {
            "n": int(round(self.n)),
            "net": round(net, 1),
            # net / weighted trade count — robust for the partial rule, whose
            # `values` holds two half-entries per trade.
            "per_trade_net": round(net / self.n, 4) if self.n else 0.0,
            "occ_mean": round(float(cnt_all.mean()), 1),
            "occ_p95": round(float(np.percentile(cnt_all, 95)), 1),
            "density": round(net / slot_days, 3) if slot_days else 0.0,
            "book_mark_mean": round(float((msum_all[live] / cnt_all[live]).mean()), 3) if live.any() else 0.0,
            "book_underwater_mean": round(float((self.neg[live] / cnt_all[live]).mean() * 100), 1)
            if live.any()
            else 0.0,
            "book_mtm_min": round(float(msum_all.min()), 1),
            "cnt_long_mean": round(float(self.cnt["LONG"].mean()), 1),
            "cnt_short_mean": round(float(self.cnt["SHORT"].mean()), 1),
            "equity_final": round(float(equity[-1]), 1),
            "equity_maxdd": round(float(dd.max()), 1),
            "_equity": equity,
            "_cnt": (self.cnt["LONG"], self.cnt["SHORT"]),
            "_mark_mean": np.where(live, msum_all / np.maximum(cnt_all, 1e-9), np.nan),
        }

    def daily_series(self, grid0: np.datetime64) -> dict:
        """Daily-downsampled book time series — the 'composition over time' view
        the slot-budget study lacked. One sample per day (last hour of the day)."""
        s = self.stats()
        idx = np.arange(23, self.glen, 24)
        days = [(grid0 + np.timedelta64(int(i), "h")).astype("datetime64[D]").astype(str) for i in idx]
        mm = s["_mark_mean"]
        return {
            "day": days,
            "equity": [round(float(v), 1) for v in s["_equity"][idx]],
            "cnt_long": [round(float(v), 1) for v in s["_cnt"][0][idx]],
            "cnt_short": [round(float(v), 1) for v in s["_cnt"][1][idx]],
            "book_mark_mean": [None if np.isnan(mm[i]) else round(float(mm[i]), 2) for i in idx],
        }


def run_rule(trades: list[dict], glen: int, exits: dict[int, tuple[int, float]]) -> Book:
    """exits: trade-index → (exit_gi, value). Missing index = natural close."""
    book = Book(glen)
    for i, t in enumerate(trades):
        gi, val = exits.get(i, (t["gie"], t["real_unlev"]))
        book.add(t, gi, val)
    return book


def run_partial(trades: list[dict], glen: int, grid0: np.datetime64, act: float) -> Book:
    """Half out at the trail trigger, half rides to the natural close."""
    book = Book(glen)
    for t in trades:
        gi, val = exit_trail(t, grid0, act)
        if gi >= t["gie"] and val == t["real_unlev"]:  # trail never fired
            book.add(t, t["gie"], t["real_unlev"])
            continue
        # Each half carries half the notional; `Book.add` weights value AND fee,
        # so passing the full round-trip fee here charges 2 × 0.5 × fee = one
        # round trip per trade — same footing as every other rule.
        book.add(t, gi, val, weight=0.5, fee=FEE_RT)
        book.add(t, t["gie"], t["real_unlev"], weight=0.5, fee=FEE_RT)
    return book


def run_exposure_cap(trades: list[dict], glen: int, grid0: np.datetime64, act: float, cap: int) -> Book:
    """Trail act=2 plus an ADMISSION rule: a new entry whose direction is already
    `cap` positions ahead of the other side is not taken at all.

    This is the only rule here that changes the trade SET, not just the exits —
    its `n` shrinks, so its net is not comparable 1:1; density and the book
    metrics are the honest comparison.
    """
    exits = {i: exit_trail(t, grid0, act) for i, t in enumerate(trades)}
    order = sorted(range(len(trades)), key=lambda i: trades[i]["gi0"])
    exit_events: dict[int, list[int]] = defaultdict(list)  # hour → trade indices
    open_cnt = {"LONG": 0, "SHORT": 0}
    book = Book(glen)
    ei = 0  # sweep pointer over admitted-exit hours via per-hour queue
    for h in range(glen):
        for i in exit_events.get(h, ()):
            open_cnt[trades[i]["dir"]] -= 1
        while ei < len(order) and trades[order[ei]]["gi0"] == h:
            i = order[ei]
            ei += 1
            d = trades[i]["dir"]
            other = "SHORT" if d == "LONG" else "LONG"
            if open_cnt[d] - open_cnt[other] >= cap:
                continue  # not admitted — the arm stays two-sided by construction
            gi, val = exits[i]
            book.add(trades[i], gi, val)
            open_cnt[d] += 1
            exit_events[max(trades[i]["gi0"] + 1, min(gi, glen - 1))].append(i)
    return book


def run_portfolio(trades: list[dict], glen: int, y: float) -> Book:
    """No per-trade trail; flatten the whole open book on a y give-back of its
    aggregate open mark (equal-weight sum, unlevered %-points)."""
    with_series = [(i, t) for i, t in enumerate(trades) if t["series"]]
    ptf = [{"gi": np.arange(t["gi0"], t["gie"], dtype=np.int64), "lm": t["hm"]} for _i, t in with_series]
    flat = portfolio_circuit_breaker(ptf, glen, y)
    exits: dict[int, tuple[int, float]] = {}
    for pos, g in flat.items():
        i, t = with_series[pos]
        rel = min(max(g - t["gi0"], 0), len(t["hm"]) - 1)
        exits[i] = (max(t["gi0"] + 1, g), float(t["hm"][rel]))
    return run_rule(trades, glen, exits)


# ─────────────────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────────────────

RULE_ORDER = [
    ("hold", "Hold (Fleet-Exit, SL/TP/Timeout)"),
    ("trail-a2", "Trail act=2 % (Bot 40 heute)"),
    ("trail-a5", "Trail act=5 %"),
    ("trail-a10", "Trail act=10 %"),
    ("trail-a2+ts24", "Trail 2 % + Zeit-Stop 24 h"),
    ("trail-a2+ts48", "Trail 2 % + Zeit-Stop 48 h"),
    ("trail-a2+ts72", "Trail 2 % + Zeit-Stop 72 h"),
    ("trail-a2+hs2", "Trail 2 % + Hard-Stop −2 %"),
    ("trail-a2-short-only", "Trail 2 % nur SHORT (LONG hält)"),
    ("trail-a2-long-only", "Trail 2 % nur LONG (SHORT hält)"),
    ("trail-a2-partial50", "Trail 2 %, 50 % Teilschließung"),
    ("trail-a2-cap50", "Trail 2 % + Exposure-Cap ±50"),
    ("trail-a2-cap100", "Trail 2 % + Exposure-Cap ±100"),
    ("ptf-y10", "Portfolio-Trail 10 % (kein Einzel-Trail)"),
    ("ptf-y15", "Portfolio-Trail 15 % (kein Einzel-Trail)"),
]


def render_md(meta: dict) -> str:
    L = [
        f"# Trailing-Arm Buch-Gesundheit — Exit-Regeln am offenen Buch gemessen ({meta['task']})",
        "",
        f"_generated {meta['generated_at']} · read-only · Roster-Beine ohne ROM1 · x={X_FRAC:.0%} · "
        f"tf {meta['tf']} · ab {meta['start']} · Gebühr {FEE_RT:.2f} %/Trade · {meta['n_trades']} Trades_",
        "",
        "**Frage:** `tools/trailing_slot_budget.py` maß realisierte Summen und Slots. Eine Regel, die",
        "Gewinner schließt und Verlierer hält, sieht dort gut aus und im offenen Buch schlecht —",
        "Bot 40 hat das live bewiesen. Hier wird jede Regel an BEIDEN Seiten gemessen: realisiert",
        "UND Zusammensetzung des offenen Buchs (Equity = realisierte Summe + offenes MTM,",
        "gleichgewichtet, unlevered %-Punkte).",
        "",
        "| Regel | n | Σ netto | Ø/Trade | Ø Slots | p95 | netto/Slot-Tag | Equity final | **Equity MaxDD** | Ø Buch-Mark | Buch unter Wasser | Ø L offen | Ø S offen |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for key, label in RULE_ORDER:
        s = meta["rules"].get(key)
        if not s:
            continue
        L.append(
            f"| {label} | {s['n']} | {s['net']:.0f} | {s['per_trade_net']:.3f} | {s['occ_mean']:.0f} | "
            f"{s['occ_p95']:.0f} | {s['density']:.3f} | {s['equity_final']:.0f} | **{s['equity_maxdd']:.0f}** | "
            f"{s['book_mark_mean']:+.2f} % | {s['book_underwater_mean']:.0f} % | "
            f"{s['cnt_long_mean']:.0f} | {s['cnt_short_mean']:.0f} |"
        )
    L += ["", "## Lesehilfe", ""]
    L += [
        "- **Equity MaxDD** ist die Kennzahl, die der Studie fehlte: max. Rückgang der Kurve",
        "  (realisiert + offen), in unlevered %-Punkten über das gleichgewichtete Buch.",
        "- **Ø Buch-Mark** = zeitgemittelter Durchschnitts-Mark der offenen Positionen. Ein stark",
        "  negativer Wert heißt: das Buch besteht strukturell aus Verlierern.",
        "- **Buch unter Wasser** = zeitgemittelter Anteil offener Positionen im Minus.",
    ]
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-03-01")
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--lev", type=float, default=20.0)
    ap.add_argument("--out", default=DEFAULT_OUT_DIR)
    args = ap.parse_args()

    from datetime import datetime, timezone

    from core import shadow_gate as sg
    from core.database import get_db_connection
    from core.trailing_roster import ROSTER

    conn = get_db_connection()
    all_trades = load_trades(conn, ["ALL"], lev=args.lev, start=args.start)
    trades = []
    n_rom1 = 0
    for t in all_trades:
        if (t["tag"], t["dir"]) not in ROSTER:
            continue
        if t["tag"] == "ROM1":  # re-forwarder: same trades the original legs post
            n_rom1 += 1
            continue
        if sg.leg_status(t["tag"], t["dir"]) != sg.LIVE:
            continue
        trades.append(t)
    print(f"loaded {len(all_trades)} fleet trades → {len(trades)} roster/LIVE (ROM1 excluded: {n_rom1})", flush=True)

    lo = min(_naive(t["ot"]) for t in trades)
    hi = max(_naive(t["ct"]) for t in trades)
    grid0 = np.datetime64(lo, "h")
    glen = int((np.datetime64(hi, "h") - grid0) / HOUR) + 2
    no_candle = attach_series(conn, trades, args.tf, grid0, glen)
    conn.close()
    print(f"series attached; {no_candle} trades without candle coverage", flush=True)

    rules: dict[str, dict] = {}
    series: dict[str, dict] = {}

    def score(name: str, book: Book) -> None:
        rules[name] = book.stats()
        series[name] = book.daily_series(grid0)
        s = rules[name]
        print(
            f"  {name:<22} net {s['net']:>8.0f}  eqDD {s['equity_maxdd']:>7.0f}  "
            f"mark {s['book_mark_mean']:>+6.2f}%  L/S {s['cnt_long_mean']:.0f}/{s['cnt_short_mean']:.0f}",
            flush=True,
        )

    score("hold", run_rule(trades, glen, {}))
    for act, key in [(2.0, "trail-a2"), (5.0, "trail-a5"), (10.0, "trail-a10")]:
        score(key, run_rule(trades, glen, {i: exit_trail(t, grid0, act) for i, t in enumerate(trades)}))
    for ts, key in [(24.0, "trail-a2+ts24"), (48.0, "trail-a2+ts48"), (72.0, "trail-a2+ts72")]:
        score(
            key,
            run_rule(trades, glen, {i: exit_trail_timestop(t, grid0, ACT_LIVE, ts) for i, t in enumerate(trades)}),
        )
    score(
        "trail-a2+hs2",
        run_rule(trades, glen, {i: exit_trail_hardstop(t, grid0, ACT_LIVE, 2.0) for i, t in enumerate(trades)}),
    )
    for d, key in [("SHORT", "trail-a2-short-only"), ("LONG", "trail-a2-long-only")]:
        score(key, run_rule(trades, glen, {i: exit_one_sided(t, grid0, ACT_LIVE, d) for i, t in enumerate(trades)}))
    score("trail-a2-partial50", run_partial(trades, glen, grid0, ACT_LIVE))
    score("trail-a2-cap50", run_exposure_cap(trades, glen, grid0, ACT_LIVE, 50))
    score("trail-a2-cap100", run_exposure_cap(trades, glen, grid0, ACT_LIVE, 100))
    score("ptf-y10", run_portfolio(trades, glen, 0.10))
    score("ptf-y15", run_portfolio(trades, glen, 0.15))

    meta = {
        "task": "T-2026-KYT-9050-052",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start": args.start,
        "tf": args.tf,
        "x": X_FRAC,
        "fee": FEE_RT,
        "n_trades": len(trades),
        "n_rom1_excluded": n_rom1,
        "no_candle": no_candle,
        "rules": {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in rules.items()},
        "daily_series": series,
    }
    os.makedirs(args.out, exist_ok=True)
    base = os.path.join(args.out, "trailing_book_health")
    with open(base + ".md", "w", encoding="utf-8") as fh:
        fh.write(render_md({**meta, "rules": rules}))
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)
    print(render_md({**meta, "rules": rules}))
    print(f"→ {base}.md / .json")


if __name__ == "__main__":
    main()
