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
  trail-tp1 (+f2)      activation is NOT a fixed percentage but the source
                       signal's OWN first target: the trail arms once the peak
                       clears TP1. `f2` additionally floors it at 2 % (see
                       `act_tp1`). Operator question 2026-08-04.
  ptf-y10 / y15        NO per-trade trail; flatten the whole book when its
                       aggregate open mark retraces y from its running peak
                       (`core.wave_exit_sim.portfolio_circuit_breaker`).

Read-only: SELECTs against closed_ai_signals + candles. No writes, no live effect.
One sim job at a time on this machine (repo rule).

Usage:
    python tools/trailing_book_health.py --start 2026-03-01 --tf 15m
    python tools/trailing_book_health.py --start 2026-07-01 --tp1-only   # TP1 rules
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
        # 26h lead-in instead of 2h: the trailing 24h pre-move of the coin at
        # entry time (mover gate, operator question 2026-07-28) needs candles
        # BEFORE the trade.
        lo = min(t["ot"] for t in tl) - timedelta(hours=26)
        hi = max(t["ct"] for t in tl) + timedelta(hours=2)
        cd = read_coin_wick(conn, sym, lo, hi, tf)
        covered = len(cd["t"]) > 0
        for t in tl:
            ot64 = np.datetime64(_naive(t["ot"]))
            # 24h pre-move strictly from candles BEFORE the entry (causal); None,
            # if the coin does not reach back far enough (young listing).
            t["mv24"] = None
            if covered:
                p_now = int(np.searchsorted(cd["t"], ot64, side="left")) - 1
                p_24h = int(np.searchsorted(cd["t"], ot64 - np.timedelta64(24, "h"), side="left")) - 1
                if p_now > p_24h >= 0:
                    t["mv24"] = float((cd["c"][p_now] / cd["c"][p_24h] - 1.0) * 100.0)
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
# TP1 GEOMETRY (operator question 2026-08-04: arm the trail at TP1, not at +2 %)
# ─────────────────────────────────────────────────────────────────────────────

#: Largest TP1 distance still treated as a real target. Beyond this the row is a
#: geometry artefact (a target written in the wrong unit or against a different
#: entry), and letting it through would silently turn that trade into a `hold`.
TP1_MAX_PCT = 200.0


def attach_tp1(conn, trades: list[dict]) -> tuple[int, int]:
    """Attach ``tp1_pct`` — the source signal's first target as an unlevered,
    direction-signed % move from ITS OWN entry. Returns ``(matched, usable)``.

    ``load_trades`` does not read ``targets``, so this is a second pass over the
    same deduped population, keyed the same way ``load_trades`` dedupes
    (symbol, tag, direction, open_time).

    Coverage is the load-bearing caveat, not a detail: ``closed_ai_signals.targets``
    is only populated from ~2026-06 onward (measured 2026-08-04: 0 % Mar–May,
    2 % Jun, 77 % Jul, 100 % Aug — 19.4 % over the whole March window). A TP1 rule
    can therefore only be measured causally on the covered window; everything
    before it would silently fall back to the fixed activation and report a
    trail-a2 clone under a TP1 label. Hence ``--tp1-only``, which restricts the
    WHOLE sweep to covered trades so every rule scores the same trades.
    """
    from core.bot_naming import pretty_name

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (symbol, model, upper(btrim(direction)), open_time)
                   symbol, model, upper(btrim(direction)), entry, targets, open_time
            FROM closed_ai_signals
            WHERE entry IS NOT NULL AND targets IS NOT NULL
              AND (status IS NULL OR status NOT ILIKE %s)
            ORDER BY symbol, model, upper(btrim(direction)), open_time, close_time ASC
            """,
            ("%LEGACY%",),
        )
        rows = cur.fetchall()

    by_key: dict[tuple, float] = {}
    for sym, model, d, entry, targets, ot in rows:
        try:
            tg = targets if isinstance(targets, list) else json.loads(targets)
        except (TypeError, ValueError):
            continue
        if not tg or entry is None or float(entry) <= 0:
            continue
        e, tp1 = float(entry), float(tg[0])
        pct = (tp1 - e) / e * 100.0 if d == "LONG" else (e - tp1) / e * 100.0
        by_key.setdefault((str(sym).upper(), pretty_name(str(model)), d, ot), pct)

    matched = usable = 0
    for t in trades:
        pct = by_key.get((t["sym"], t["tag"], t["dir"], t["ot"]))
        t["tp1_pct"] = None
        if pct is None:
            continue
        matched += 1
        # A non-positive or absurd TP1 is not a target the bot could ever have
        # armed on — dropped rather than clamped, so it lands in the uncovered
        # bucket instead of quietly becoming a different rule.
        if 0.0 < pct < TP1_MAX_PCT:
            t["tp1_pct"] = pct
            usable += 1
    return matched, usable


def impute_tp1(trades: list[dict]) -> int:
    """Fill missing ``tp1_pct`` with the leg's own median TP1 — ROBUSTNESS ONLY.

    An imputed TP1 is a per-leg CONSTANT, so it cannot reproduce the per-trade
    variation that is the entire point of the rule (a leg whose TP1 sits at 1,7 %
    on one trade and 9 % on the next arms at two very different peaks). For the
    fixed-geometry legs (MIS2: TP1 is literally constant) the imputation is exact;
    for the S/R-derived legs it is a stand-in. Every imputed trade is flagged
    ``tp1_imputed`` so a run's coverage stays readable in the JSON.
    """
    per_leg: dict[tuple[str, str], list[float]] = defaultdict(list)
    for t in trades:
        if t.get("tp1_pct") is not None:
            per_leg[(t["tag"], t["dir"])].append(t["tp1_pct"])
    med = {k: float(np.median(v)) for k, v in per_leg.items()}
    n = 0
    for t in trades:
        t["tp1_imputed"] = False
        if t.get("tp1_pct") is not None:
            continue
        m = med.get((t["tag"], t["dir"]))
        if m is None:
            continue  # leg has no coverage at all — stays on the fallback activation
        t["tp1_pct"] = m
        t["tp1_imputed"] = True
        n += 1
    return n


def act_tp1(t: dict, floor: float | None = None, fallback: float = ACT_LIVE) -> float:
    """Activation for ONE trade: its own TP1 instead of the fleet-wide constant.

    ``floor`` exists because TP1 is not automatically the higher bar the operator's
    question assumes: measured over the roster legs, 24 % of trades carry a TP1
    below 2 %, so a bare TP1 activation arms EARLIER than today on a quarter of the
    book — which is exactly the micro-scalper the fixed 2 % floor was introduced
    against (`core.trailing_roster.ACTIVATION_PCT`). ``max(TP1, 2)`` keeps the
    floor and only ever delays arming.

    ``fallback`` is what an uncovered trade gets. It is the live activation, so an
    uncovered trade behaves exactly like today's bot — the honest neutral element,
    but it also means a run over an uncovered period measures trail-a2 wearing a
    TP1 label. Use ``--tp1-only``.
    """
    a = t.get("tp1_pct")
    if a is None:
        return fallback
    return max(a, floor) if floor is not None else a


# ─────────────────────────────────────────────────────────────────────────────
# EXIT RULES — each returns (exit_grid_idx, realised_unlev, weight_after) per trade
# ─────────────────────────────────────────────────────────────────────────────


def _gi_of(t: dict, k: int, grid0: np.datetime64) -> int:
    """Grid hour of candle k, clamped into the trade's own [gi0+1, gie] window."""
    g = int((t["tt"][k].astype("datetime64[h]") - grid0) / HOUR)
    return max(t["gi0"] + 1, min(g, t["gie"]))


def exit_trail(t: dict, grid0: np.datetime64, act: float, x: float = X_FRAC) -> tuple[int, float]:
    """The live rule: `x` give-back once the prior peak cleared `act`."""
    if not t["series"]:
        return t["gie"], t["real_unlev"]
    k = trail_exit(t["fav"], t["adv"], x, act)
    if k is None:
        return t["gie"], t["real_unlev"]
    val = float(prior_peak(t["fav"])[k] * (1.0 - x))
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
    # SL-first on the same candle (monitor convention; PR-#206-review finding —
    # the earlier j<k tie-break was optimistic for the stop rule).
    if j is not None and (k is None or j <= k):
        return _gi_of(t, j, grid0), -stop
    if k is None:
        return t["gie"], t["real_unlev"]
    return _gi_of(t, k, grid0), float(prior_peak(t["fav"])[k] * (1.0 - X_FRAC))


def exit_deployed_slcap(t: dict, grid0: np.datetime64, act: float, ts_hours: float, stop: float) -> tuple[int, float]:
    """The DEPLOYED rule (trail + causal time-stop) plus an SL cap at −`stop` %
    unlevered (operator question 2026-07-29: 'SL at 5 % movement = max −100 %
    at 20x?'). Earliest event wins; on the same candle the stop is filled first
    (monitor convention)."""
    if not t["series"]:
        return t["gie"], t["real_unlev"]
    pk = prior_peak(t["fav"])
    cands: list[tuple[int, int, float]] = []  # (candle_idx, prio, value)
    hs = np.flatnonzero(t["adv"] <= -stop)
    if len(hs):
        cands.append((int(hs[0]), 0, -stop))
    k = trail_exit(t["fav"], t["adv"], X_FRAC, act)
    if k is not None:
        cands.append((k, 1, float(pk[k] * (1.0 - X_FRAC))))
    deadline = np.datetime64(_naive(t["ot"])) + np.timedelta64(int(ts_hours * 3600), "s")
    j = int(np.searchsorted(t["tt"], deadline.astype("datetime64[ns]"), side="left"))
    if j < len(t["tt"]) and not (pk[j] > act or (k is not None and k <= j)):
        cands.append((j, 2, float(t["cm"][j])))
    if not cands:
        return t["gie"], t["real_unlev"]
    idx, _prio, val = min(cands)
    return _gi_of(t, idx, grid0), val


def exit_one_sided(t: dict, grid0: np.datetime64, act: float, trail_dir: str) -> tuple[int, float]:
    """Trail only one direction; the other side holds to its natural close."""
    if t["dir"] != trail_dir:
        return t["gie"], t["real_unlev"]
    return exit_trail(t, grid0, act)


def exit_breakeven(t: dict, grid0: np.datetime64, act: float, ts_hours: float | None = None) -> tuple[int, float]:
    """SL ratchet instead of a full trail: once the prior peak clears `act`, the
    stop ratchets to BREAKEVEN (entry). The trade then rides until it touches the
    entry again (exit at 0.0) or reaches its natural close — evaporation is
    bounded at zero instead of captured at peak*(1-x), keeping the upside open.

    With `ts_hours`, trades not armed BY THE DEADLINE additionally get the
    time-stop (the loser bound the ratchet alone cannot provide — an unarmed
    trade has no stop to ratchet).

    CAUSALITY: the time-stop decision uses only what is on the tape at the
    deadline. A trade that first clears `act` AFTER `ts_hours` is stopped at
    the deadline all the same — the live bot cannot know it would have armed
    later. The first version of this function checked arming over the whole
    series and thereby let every late winner escape the stop (look-ahead that
    inflated the be+ts results; found while porting the rule to the bot).
    """
    if not t["series"]:
        return t["gie"], t["real_unlev"]
    pk = prior_peak(t["fav"])
    armed = np.flatnonzero(pk > act)
    k_arm = int(armed[0]) if len(armed) else None
    if ts_hours is not None:
        deadline = np.datetime64(_naive(t["ot"])) + np.timedelta64(int(ts_hours * 3600), "s")
        j = int(np.searchsorted(t["tt"], deadline.astype("datetime64[ns]"), side="left"))
        if j < len(t["tt"]) and (k_arm is None or k_arm > j):
            # Not armed by the deadline → time-stop, regardless of what the
            # trade would have done afterwards.
            return _gi_of(t, j, grid0), float(t["cm"][j])
    if k_arm is not None:
        touch = np.flatnonzero(t["adv"][k_arm:] <= 0.0)
        if len(touch):
            return _gi_of(t, k_arm + int(touch[0]), grid0), 0.0
    return t["gie"], t["real_unlev"]


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
            # Equal-capital view: what one AVERAGE occupied slot earned / drew
            # down over the whole period. Raw net favours whichever rule binds
            # the most capital; these two make rules with different book sizes
            # comparable "at the end of the period".
            "net_per_slot": round(net / float(cnt_all.mean()), 2) if cnt_all.mean() > 0 else 0.0,
            "dd_per_slot": round(float(dd.max()) / float(cnt_all.mean()), 2) if cnt_all.mean() > 0 else 0.0,
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


def run_exposure_cap(
    trades: list[dict],
    glen: int,
    grid0: np.datetime64,
    act: float,
    cap: int,
    exits: dict[int, tuple[int, float]] | None = None,
) -> Book:
    """Per-trade exits plus an ADMISSION rule: a new entry whose direction is
    already `cap` positions ahead of the other side is not taken at all.

    `exits` defaults to the plain trail; pass a different map to combine the cap
    with another exit rule. This is the only rule family that changes the trade
    SET, not just the exits — its `n` shrinks, so its net is not comparable 1:1;
    density and the book metrics are the honest comparison.
    """
    if exits is None:
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


def run_feedback_gate(
    trades: list[dict],
    glen: int,
    grid0: np.datetime64,
    act: float,
    thresh: float = -1.0,
    min_n: int = 10,
) -> Book:
    """Regime-fit WITHOUT a forecast: the arm's own open book is the sensor.

    A new entry of direction D is admitted only while the CURRENT mean mark of
    the arm's open D-positions is above `thresh` — i.e. keep taking longs only
    as long as the longs already on the book are working. Below `min_n` open
    positions the gate is open (no signal to read). Exits stay the plain trail,
    so the measurement isolates the admission effect.

    This is the operator's question (b): "only post when it fits the market
    situation" — implemented as feedback from realised market state instead of a
    regime classifier (ROM's whitelist measured ~zero discriminative power).
    """
    exits = {i: exit_trail(t, grid0, act) for i, t in enumerate(trades)}
    order = sorted(range(len(trades)), key=lambda i: trades[i]["gi0"])
    exit_events: dict[int, list[int]] = defaultdict(list)
    open_set: dict[str, dict[int, None]] = {"LONG": {}, "SHORT": {}}
    book = Book(glen)
    ei = 0
    for h in range(glen):
        for i in exit_events.get(h, ()):
            open_set[trades[i]["dir"]].pop(i, None)
        while ei < len(order) and trades[order[ei]]["gi0"] == h:
            i = order[ei]
            ei += 1
            t = trades[i]
            d = t["dir"]
            cur = open_set[d]
            if len(cur) >= min_n:
                marks = [
                    trades[j]["hm"][min(h - trades[j]["gi0"], len(trades[j]["hm"]) - 1)]
                    for j in cur
                    if trades[j]["series"]
                ]
                if marks and float(np.mean(marks)) <= thresh:
                    continue  # book of this direction is bleeding — stop feeding it
            gi, val = exits[i]
            book.add(t, gi, val)
            open_set[d][i] = None
            exit_events[max(t["gi0"] + 1, min(gi, glen - 1))].append(i)
    return book


def run_total_cap(
    trades: list[dict],
    glen: int,
    exits: dict[int, tuple[int, float]],
    cap: int = 500,
) -> Book:
    """The deployable view: a hard TOTAL slot cap (Cornix' 500 per channel).

    A new entry is refused while `cap` positions are open — the sim's admission
    is arrival-ordered, while Bot 40's real layer (AK4) refuses by leg density
    within a cycle; arrival order is the more conservative approximation. Rules
    whose occupancy p95 bursts the cap lose exactly the trades the live bot
    would have to reject, which makes their end-of-period result comparable to
    rules that fit under the cap naturally.
    """
    order = sorted(range(len(trades)), key=lambda i: trades[i]["gi0"])
    exit_events: dict[int, list[int]] = defaultdict(list)
    book = Book(glen)
    open_n = 0
    ei = 0
    for h in range(glen):
        for _i in exit_events.get(h, ()):
            open_n -= 1
        while ei < len(order) and trades[order[ei]]["gi0"] == h:
            i = order[ei]
            ei += 1
            if open_n >= cap:
                continue
            gi, val = exits.get(i, (trades[i]["gie"], trades[i]["real_unlev"]))
            book.add(trades[i], gi, val)
            open_n += 1
            exit_events[max(trades[i]["gi0"] + 1, min(gi, glen - 1))].append(i)
    return book


def chases_the_move(direction: str, mv24: float | None, thresh: float) -> bool:
    """True if the entry runs AFTER the coin's prior 24h move: a LONG into a coin
    already up more than `thresh` %, or a SHORT into one already down that far.
    Counter-move entries (shorting a pump, longing a dump) are never 'chasing' —
    that is the MIS family's entire edge and must not be gated away."""
    if mv24 is None:
        return False
    if direction == "LONG":
        return mv24 > thresh
    return mv24 < -thresh


def run_move_gate(
    trades: list[dict],
    glen: int,
    exits: dict[int, tuple[int, float]],
    *,
    abs_thresh: float | None = None,
    chase_thresh: float | None = None,
) -> Book:
    """Admission gate on the coin's prior 24h move (operator question 2026-07-28:
    'should coins with ±50 % in a few hours be traded at all?').

    `abs_thresh`: skip every entry on a coin whose |24h move| exceeds it (blanket).
    `chase_thresh`: skip only entries that chase the move (see chases_the_move).
    Trades without coverage (young listings) pass — an absent signal must not veto.
    """
    book = Book(glen)
    for i, t in enumerate(trades):
        mv = t.get("mv24")
        if abs_thresh is not None and mv is not None and abs(mv) > abs_thresh:
            continue
        if chase_thresh is not None and chases_the_move(t["dir"], mv, chase_thresh):
            continue
        gi, val = exits.get(i, (t["gie"], t["real_unlev"]))
        book.add(t, gi, val)
    return book


def mover_buckets(trades: list[dict], exits: dict[int, tuple[int, float]]) -> list[dict]:
    """Per-trade expectancy by prior-24h-move bucket and direction — the empirical
    answer to 'should movers be traded at all', separated so the counter-move edge
    (MIS shorting pumps) is visible instead of averaged away."""
    edges = [(-1e9, -50.0), (-50.0, -20.0), (-20.0, 20.0), (20.0, 50.0), (50.0, 1e9)]
    labels = ["<-50", "-50..-20", "-20..20", "+20..+50", ">+50"]
    out = []
    for (lo, hi), label in zip(edges, labels, strict=True):
        for d in ("LONG", "SHORT"):
            sel = [
                (i, t)
                for i, t in enumerate(trades)
                if t["dir"] == d and t.get("mv24") is not None and lo <= t["mv24"] < hi
            ]
            if not sel:
                continue
            hold = float(np.mean([t["real_unlev"] for _i, t in sel]))
            ruled = float(np.mean([exits.get(i, (t["gie"], t["real_unlev"]))[1] for i, t in sel]))
            out.append(
                {
                    "bucket": label,
                    "dir": d,
                    "n": len(sel),
                    "hold_per_trade": round(hold, 3),
                    "rule_per_trade": round(ruled, 3),
                }
            )
    return out


def run_direction_gate(
    trades: list[dict],
    glen: int,
    grid0: np.datetime64,
    act: float,
    allow_long: np.ndarray,
    allow_short: np.ndarray,
) -> Book:
    """Regime-fit via the bluntest observable: BTC 24h momentum sign at entry.

    `allow_long`/`allow_short` are per-grid-hour booleans (LONG admitted only
    while BTC's trailing 24h return is positive, SHORT only while negative).
    Baseline for question (b) — the repo's regime classifiers repeatedly
    measured no edge, so this is the simplest possible directional gate to
    beat, not a recommendation.
    """
    book = Book(glen)
    for t in trades:
        h = min(t["gi0"], glen - 1)
        ok = allow_long[h] if t["dir"] == "LONG" else allow_short[h]
        if not ok:
            continue
        gi, val = exit_trail(t, grid0, act)
        book.add(t, gi, val)
    return book


def btc_momentum_gates(conn, grid0: np.datetime64, glen: int, tf: str) -> tuple[np.ndarray, np.ndarray]:
    """(allow_long, allow_short) per grid hour from BTCUSDT's trailing 24h return.

    Hours before the first candle (or without BTC data at all) stay open on both
    sides — an absent signal must not silently veto trades.
    """
    lo = (grid0 - np.timedelta64(26, "h")).astype("datetime64[us]").astype(object)
    hi = (grid0 + np.timedelta64(int(glen) + 1, "h")).astype("datetime64[us]").astype(object)
    cd = read_coin_wick(conn, "BTCUSDT", lo, hi, tf)
    allow_long = np.ones(glen, dtype=bool)
    allow_short = np.ones(glen, dtype=bool)
    if len(cd["t"]) == 0:
        return allow_long, allow_short
    hours = (grid0 + np.arange(glen) * HOUR).astype("datetime64[ns]")
    pos_now = np.clip(np.searchsorted(cd["t"], hours, side="right") - 1, 0, len(cd["t"]) - 1)
    pos_24h = np.clip(np.searchsorted(cd["t"], hours - np.timedelta64(24, "h"), side="right") - 1, 0, len(cd["t"]) - 1)
    ret24 = cd["c"][pos_now] / cd["c"][pos_24h] - 1.0
    covered = pos_now > pos_24h  # both lookups resolved to distinct candles
    allow_long = ~covered | (ret24 > 0.0)
    allow_short = ~covered | (ret24 < 0.0)
    return allow_long, allow_short


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
    ("trail-a2", "Trail act=2 % (Bot 40 today)"),
    ("trail-a5", "Trail act=5 %"),
    ("trail-a10", "Trail act=10 %"),
    ("trail-a2-x20", "Trail act=2 %, x=20 % (closes slower)"),
    ("trail-a2-x30", "Trail act=2 %, x=30 %"),
    ("trail-a10-x20", "Trail act=10 %, x=20 %"),
    ("trail-a2+ts24", "Trail 2 % + Time-Stop 24 h"),
    ("trail-a2+ts48", "Trail 2 % + Time-Stop 48 h"),
    ("trail-a2+ts72", "Trail 2 % + Time-Stop 72 h"),
    ("trail-a2+hs2", "Trail 2 % + Hard-Stop −2 %"),
    ("trail-a2-short-only", "Trail 2 % only SHORT (LONG holds)"),
    ("trail-a2-long-only", "Trail 2 % only LONG (SHORT holds)"),
    ("trail-a2-partial50", "Trail 2 %, 50 % partial close"),
    ("trail-tp1", "Trail armed at TP1 (per trade, no floor)"),
    ("trail-tp1f2", "Trail armed at max(TP1, 2 %)"),
    ("trail-tp1+ts24", "Trail at TP1 + Time-Stop 24 h"),
    ("trail-tp1f2+ts24", "Trail at max(TP1, 2 %) + Time-Stop 24 h"),
    ("trail-tp1+ts24+cap50", "Trail at TP1 + Time-Stop 24 h + Cap ±50"),
    ("trail-tp1f2+ts24+cap50", "Trail at max(TP1, 2 %) + ts24 + Cap ±50"),
    ("trail-a2-cap50", "Trail 2 % + Exposure-Cap ±50"),
    ("trail-a2-cap100", "Trail 2 % + Exposure-Cap ±100"),
    ("trail-a2+ts24+cap50", "Trail 2 % + Time-Stop 24 h + Cap ±50"),
    ("be2", "SL ratchet: breakeven from +2 % (no trail)"),
    ("be2+ts24", "Breakeven from +2 % + Time-Stop 24 h"),
    ("be2+ts24+cap50", "Breakeven 2 % + Time-Stop 24 h + Cap ±50"),
    ("be2+ts24+cap100", "Breakeven 2 % + Time-Stop 24 h + Cap ±100"),
    ("be5+ts24", "Breakeven from +5 % + Time-Stop 24 h"),
    ("hold@500", "Hold under a hard 500-slot cap"),
    ("be2+ts24@500", "Breakeven 2 % + Time-Stop 24 h @ 500-Cap"),
    ("be5+ts24@500", "Breakeven 5 % + Time-Stop 24 h @ 500-Cap"),
    ("hold@1000", "Hold @ 1000 (2 Channels, least-loaded)"),
    ("be5+ts24@1000", "Breakeven 5 % + Time-Stop 24 h @ 1000 (2 Channels)"),
    ("hold@1500", "Hold @ 1500 (3 Channels)"),
    ("be5+ts24@1500", "Breakeven 5 % + Time-Stop 24 h @ 1500 (3 Channels)"),
    ("feedback-gate", "Book feedback gate (D only if open D book > −1 %)"),
    ("btc-dir-gate", "BTC direction gate (LONG only if 24h ret > 0)"),
    ("mover-abs30", "Mover gate: ignore coin |24h| > 30 % (Trail a2)"),
    ("mover-abs50", "Mover gate: ignore coin |24h| > 50 % (Trail a2)"),
    ("mover-chase20", "Chase gate: ignore only chasing > 20 %"),
    ("mover-chase50", "Chase gate: ignore only chasing > 50 %"),
    ("trail-a2+slcap5", "Trail a2 + SL cap −5 % unlev (−100 % @20x)"),
    ("deployed+slcap5", "DEPLOYED (Trail+ts24+Cap50) + SL cap −5 %"),
    ("deployed", "DEPLOYED today: Trail+ts24+Cap50 (causal, reference)"),
    ("ptf-y10", "Portfolio trail 10 % (no per-trade trail)"),
    ("ptf-y15", "Portfolio trail 15 % (no per-trade trail)"),
]


def render_md(meta: dict) -> str:
    L = [
        f"# Trailing arm book health — exit rules measured on the open book ({meta['task']})",
        "",
        f"_generated {meta['generated_at']} · read-only · roster legs excluding ROM1 · x={X_FRAC:.0%} · "
        f"tf {meta['tf']} · since {meta['start']} · fee {FEE_RT:.2f} %/trade · {meta['n_trades']} trades_",
        "",
        f"_TP1 coverage: {meta.get('tp1_usable', 0)} trades with a usable TP1"
        + (f", {meta['tp1_imputed']} imputed from the leg median" if meta.get("tp1_imputed") else "")
        + (
            " · population restricted to covered trades (`--tp1-only`)_"
            if meta.get("tp1_only")
            else " · uncovered trades fall back to act=2 %, so the `trail-tp1` rows are a BLEND_"
        ),
        "",
        "**Question:** `tools/trailing_slot_budget.py` measured realised sums and slots. A rule that",
        "closes winners and holds losers looks good there and bad in the open book —",
        "Bot 40 proved that live. Here every rule is measured on BOTH sides: realised",
        "AND composition of the open book (equity = realised sum + open MTM,",
        "equal-weighted, unlevered %-points).",
        "",
        "| Rule | n | Σ net | avg/trade | avg slots | p95 | net/slot-day | Equity final | **Equity MaxDD** | net/avg slot | DD/avg slot | avg book mark | book underwater | avg L open | avg S open |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for key, label in RULE_ORDER:
        s = meta["rules"].get(key)
        if not s:
            continue
        L.append(
            f"| {label} | {s['n']} | {s['net']:.0f} | {s['per_trade_net']:.3f} | {s['occ_mean']:.0f} | "
            f"{s['occ_p95']:.0f} | {s['density']:.3f} | {s['equity_final']:.0f} | **{s['equity_maxdd']:.0f}** | "
            f"{s['net_per_slot']:.0f} | {s['dd_per_slot']:.1f} | "
            f"{s['book_mark_mean']:+.2f} % | {s['book_underwater_mean']:.0f} % | "
            f"{s['cnt_long_mean']:.0f} | {s['cnt_short_mean']:.0f} |"
        )
    L += ["", "## Reading Guide", ""]
    L += [
        "- **Equity MaxDD** is the metric the study lacked: max. drawdown of the curve",
        "  (realised + open), in unlevered %-points across the equal-weighted book.",
        "- **avg book mark** = time-averaged mean mark of the open positions. A strongly",
        "  negative value means: the book structurally consists of losers.",
        "- **book underwater** = time-averaged share of open positions in the red.",
    ]
    return "\n".join(L) + "\n"


def main() -> None:
    # Redirected stdout on this Windows box defaults to cp1252, and every progress
    # line here carries a "→" or "±". Without this the run dies at the FIRST print
    # — after the full population load, before a single rule is scored.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-03-01")
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--lev", type=float, default=20.0)
    ap.add_argument("--out", default=DEFAULT_OUT_DIR)
    ap.add_argument(
        "--tp1-only",
        action="store_true",
        help="restrict the WHOLE population to trades with a known TP1 — the only "
        "honest basis for the TP1 rules (targets are only stored from ~2026-06 on)",
    )
    ap.add_argument(
        "--tp1-impute",
        action="store_true",
        help="fill a missing TP1 with the leg's median TP1 (robustness run only; a "
        "per-leg constant cannot reproduce per-trade geometry)",
    )
    ap.add_argument("--tag", default="", help="filename suffix, so a second run does not overwrite the first")
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

    tp1_matched, tp1_usable = attach_tp1(conn, trades)
    n_imputed = impute_tp1(trades) if args.tp1_impute else 0
    n_before = len(trades)
    if args.tp1_only:
        trades = [t for t in trades if t.get("tp1_pct") is not None]
    print(
        f"TP1 geometry: {tp1_matched} matched / {tp1_usable} usable of {n_before} "
        f"({tp1_usable / n_before * 100:.1f} %)"
        + (f", {n_imputed} imputed from the leg median" if args.tp1_impute else "")
        + (f" → population restricted to {len(trades)}" if args.tp1_only else ""),
        flush=True,
    )
    # Counted AFTER imputation, because that is what the rules actually see — the
    # raw coverage number would overstate the blend on an imputed run.
    n_fallback = sum(1 for t in trades if t.get("tp1_pct") is None)
    if n_fallback:
        # Loud, because the failure is silent otherwise: uncovered trades fall back
        # to ACT_LIVE, so the TP1 rules converge on trail-a2 the less coverage there is.
        print(
            f"  ⚠ {n_fallback} trades without TP1 fall back to act={ACT_LIVE} % — "
            f"the trail-tp1 rows below are a BLEND to that extent ({n_fallback / len(trades) * 100:.1f} %). "
            "Use --tp1-only for a clean TP1 population.",
            flush=True,
        )

    lo = min(_naive(t["ot"]) for t in trades)
    hi = max(_naive(t["ct"]) for t in trades)
    grid0 = np.datetime64(lo, "h")
    glen = int((np.datetime64(hi, "h") - grid0) / HOUR) + 2
    no_candle = attach_series(conn, trades, args.tf, grid0, glen)
    btc_gates = btc_momentum_gates(conn, grid0, glen, args.tf)
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
    for act, x, key in [(2.0, 0.20, "trail-a2-x20"), (2.0, 0.30, "trail-a2-x30"), (10.0, 0.20, "trail-a10-x20")]:
        score(key, run_rule(trades, glen, {i: exit_trail(t, grid0, act, x) for i, t in enumerate(trades)}))
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
    # TP1 activation (operator question 2026-08-04). Same trail, same give-back —
    # only the arming threshold becomes per-trade instead of fleet-wide.
    for floor, suffix in [(None, ""), (2.0, "f2")]:
        tp1_exits = {i: exit_trail(t, grid0, act_tp1(t, floor)) for i, t in enumerate(trades)}
        tp1_ts_exits = {i: exit_trail_timestop(t, grid0, act_tp1(t, floor), 24.0) for i, t in enumerate(trades)}
        score(f"trail-tp1{suffix}", run_rule(trades, glen, tp1_exits))
        score(f"trail-tp1{suffix}+ts24", run_rule(trades, glen, tp1_ts_exits))
        # The cap layer needs an activation for its own admission bookkeeping only;
        # the exits are already per-trade, so ACT_LIVE here changes nothing.
        score(
            f"trail-tp1{suffix}+ts24+cap50",
            run_exposure_cap(trades, glen, grid0, ACT_LIVE, 50, exits=tp1_ts_exits),
        )
    score("trail-a2-cap50", run_exposure_cap(trades, glen, grid0, ACT_LIVE, 50))
    score("trail-a2-cap100", run_exposure_cap(trades, glen, grid0, ACT_LIVE, 100))
    ts24_exits = {i: exit_trail_timestop(t, grid0, ACT_LIVE, 24.0) for i, t in enumerate(trades)}
    score("trail-a2+ts24+cap50", run_exposure_cap(trades, glen, grid0, ACT_LIVE, 50, exits=ts24_exits))
    score("be2", run_rule(trades, glen, {i: exit_breakeven(t, grid0, ACT_LIVE) for i, t in enumerate(trades)}))
    score(
        "be2+ts24",
        run_rule(trades, glen, {i: exit_breakeven(t, grid0, ACT_LIVE, 24.0) for i, t in enumerate(trades)}),
    )
    be_ts_exits = {i: exit_breakeven(t, grid0, ACT_LIVE, 24.0) for i, t in enumerate(trades)}
    score("be2+ts24+cap50", run_exposure_cap(trades, glen, grid0, ACT_LIVE, 50, exits=be_ts_exits))
    score("be2+ts24+cap100", run_exposure_cap(trades, glen, grid0, ACT_LIVE, 100, exits=be_ts_exits))
    score("be5+ts24", run_rule(trades, glen, {i: exit_breakeven(t, grid0, 5.0, 24.0) for i, t in enumerate(trades)}))
    be5_exits = {i: exit_breakeven(t, grid0, 5.0, 24.0) for i, t in enumerate(trades)}
    score("hold@500", run_total_cap(trades, glen, {}))
    score("be2+ts24@500", run_total_cap(trades, glen, be_ts_exits))
    score("be5+ts24@500", run_total_cap(trades, glen, be5_exits))
    # 2 Channels + least-loaded assignment == one global 1000 cap: the emptier
    # channel has room exactly while total open < 1000 (operator idea 2026-07-28).
    score("hold@1000", run_total_cap(trades, glen, {}, cap=1000))
    score("be5+ts24@1000", run_total_cap(trades, glen, be5_exits, cap=1000))
    score("hold@1500", run_total_cap(trades, glen, {}, cap=1500))
    score("be5+ts24@1500", run_total_cap(trades, glen, be5_exits, cap=1500))
    score("feedback-gate", run_feedback_gate(trades, glen, grid0, ACT_LIVE))
    score("btc-dir-gate", run_direction_gate(trades, glen, grid0, ACT_LIVE, *btc_gates))
    a2_exits = {i: exit_trail(t, grid0, ACT_LIVE) for i, t in enumerate(trades)}
    score("mover-abs30", run_move_gate(trades, glen, a2_exits, abs_thresh=30.0))
    score("mover-abs50", run_move_gate(trades, glen, a2_exits, abs_thresh=50.0))
    score("mover-chase20", run_move_gate(trades, glen, a2_exits, chase_thresh=20.0))
    score("mover-chase50", run_move_gate(trades, glen, a2_exits, chase_thresh=50.0))
    score(
        "trail-a2+slcap5",
        run_rule(trades, glen, {i: exit_trail_hardstop(t, grid0, ACT_LIVE, 5.0) for i, t in enumerate(trades)}),
    )
    dep_exits = {i: exit_trail_timestop(t, grid0, ACT_LIVE, 24.0) for i, t in enumerate(trades)}
    score("deployed", run_exposure_cap(trades, glen, grid0, ACT_LIVE, 50, exits=dep_exits))
    depcap_exits = {i: exit_deployed_slcap(t, grid0, ACT_LIVE, 24.0, 5.0) for i, t in enumerate(trades)}
    score("deployed+slcap5", run_exposure_cap(trades, glen, grid0, ACT_LIVE, 50, exits=depcap_exits))

    # recovery statistics: what does a −5 % cap give up, what does it eat?
    dipped = [t for t in trades if t["series"] and len(t["adv"]) and float(np.min(t["adv"])) <= -5.0]
    if dipped:
        holds = np.array([t["real_unlev"] for t in dipped])
        rec_pos = float((holds >= 0).mean() * 100)
        rec_5 = float((holds > -5.0).mean() * 100)
        print(
            f"\nRecovery statistics SL cap −5 %: {len(dipped)}/{len(trades)} trades dipped below −5 % unlev."
            f"\n  of those ended on hold: >= 0 %: {rec_pos:.1f} %  ·  better than −5 %: {rec_5:.1f} %"
            f"  ·  avg hold result of the dipped: {holds.mean():+.3f} %"
        )
    buckets = mover_buckets(trades, a2_exits)
    print("\nMover buckets (24h pre-move at entry, avg/trade unlev %):")
    print(f"  {'Bucket':<10}{'Dir':<7}{'n':>7}{'hold':>8}{'trail-a2':>10}")
    for b in buckets:
        print(f"  {b['bucket']:<10}{b['dir']:<7}{b['n']:>7}{b['hold_per_trade']:>8.3f}{b['rule_per_trade']:>10.3f}")
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
        "tp1_matched": tp1_matched,
        "tp1_usable": tp1_usable,
        "tp1_imputed": n_imputed,
        "tp1_fallback": n_fallback,
        "tp1_only": args.tp1_only,
        "rules": {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in rules.items()},
        "mover_buckets": buckets,
        "daily_series": series,
    }
    os.makedirs(args.out, exist_ok=True)
    base = os.path.join(args.out, "trailing_book_health" + (f"_{args.tag}" if args.tag else ""))
    with open(base + ".md", "w", encoding="utf-8") as fh:
        fh.write(render_md({**meta, "rules": rules}))
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)
    print(render_md({**meta, "rules": rules}))
    print(f"→ {base}.md / .json")


if __name__ == "__main__":
    main()
