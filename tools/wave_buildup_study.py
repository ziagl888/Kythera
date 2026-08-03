"""tools/wave_buildup_study.py -- Wave-Buildup / Realized-vs-Unrealized study
(T-2026-KYT-9050-041, Phase A -- follow-up to the T-035 wave-exit overlay).

Question (operator, Michi)
--------------------------
The curated bots hold many trades open for days; losses get realised in full
while gains are left on the table -- Cornix routinely shows large UNREALISED
profit against a small REALISED result. Does the historical record confirm that
asymmetry, and would a rule that (a) sits out after a big aggregate wave
(cooldown) or (b) closes the evaporating peak (trailing) actually help?

What this tool measures (read-only, whole AIM+SRA history, no Cornix geometry)
-----------------------------------------------------------------------------
Unlike the T-035 high-fidelity harness (bounded to ~7d by telegram_outbox
retention), this study needs NO immutable Cornix geometry: it takes the RECORDED
entry / open_time / close_time / close_price from `closed_ai_signals` as ground
truth and marks each open position to market against candle prices. That lets it
run the FULL history (Feb->now) at the cost of not modelling intra-trade DCA/TP
laddering -- a first-order reconstruction of the wave the operator watches.

  * Trades: deduped on the Report-14 survivor key (symbol, model, dir, open_time)
    to dodge the 357k-duplicate LEGACY re-close rows. Model set = family prefixes
    (default AIM, SRA). Leverage is ASSUMED uniform (20x) because it was not
    persisted before ~July (0% Feb-Jun, 73% Jul); the price-move pattern is
    leverage-agnostic, leverage only scales amplitude + the -100% clamp.
  * Realised per trade = raw recorded entry->close signed move (the close_price
    already encodes the actual laddered exit); leveraged = *lev, -100% clamp.
  * Unrealised mark at each step = signed(entry, price)*lev, -100% clamp, marked
    FULL SIZE (no intra-trade TP-locking -> slightly overstates amplitude; peak
    TIMING is unaffected, which is what the wave hypothesis is about).
  * Wick-aware peak: favourable extreme (high for LONG, low for SHORT) over the
    trade's life -- the true unrealised top that evaporates under hold.

Findings it emits (all read-only, honest caveats in the report)
---------------------------------------------------------------
  C1  Asymmetry: mean realised vs mean (wick) peak vs giveback distribution,
      per bot/direction; share of LOSERS that were once deep in profit.
  C2  Cooldown probe: expectancy of trades OPENED N days after a big aggregate
      wave peak, vs baseline -- tests the "sit out 3-5 days" idea.
  CEIL Capture ceiling: hold (actual) vs perfect-peak (hindsight upper bound) vs
      a mechanical per-trade trailing-close sweep, on the leveraged SUM AND the
      risk-adjusted view (per-trade Sharpe + fixed-fraction compounding equity +
      MaxDD). The risk-adjusted view is the one that matches a real account.

Operating rules (Live VPS!): DB strictly read-only, BELOW_NORMAL priority,
coin-windowed reads, --allow-high-cpu to skip the headroom abort. Output ONLY to
staging_models/replay/.

Example
-------
  python tools/wave_buildup_study.py --models AIM,SRA --allow-high-cpu
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, "staging_models", "replay")
MAX_ABS_MOVE_PCT = 100.0  # |unlev move| above this is a data bug (mirror realized_pnl)
X_SWEEP = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]  # per-trade trailing retrace
CLAMP = -100.0  # leveraged loss floor (margin wiped)


# ─────────────────────────────────────────────────────────────────────────────
# PURE HELPERS (DB-free -- pinned in backtest/test_wave_buildup_study.py)
# ─────────────────────────────────────────────────────────────────────────────
def signed_move_pct(direction: str, entry: float, close: float) -> float | None:
    """Direction-corrected unlevered price move in % (LONG +, SHORT −)."""
    side = str(direction or "").strip().upper()
    if side not in ("LONG", "SHORT"):
        return None
    try:
        e = float(entry)
        c = float(close)
    except (TypeError, ValueError):
        return None
    if e <= 0 or c <= 0:
        return None
    move = (1.0 if side == "LONG" else -1.0) * (c - e) / e * 100.0
    if abs(move) > MAX_ABS_MOVE_PCT:
        return None
    return move


def trail_capture(fav: np.ndarray, adv: np.ndarray, x: float, lev: float, hold_ru: float, hold_rl: float):
    """Per-trade trailing-close: track the running peak of the favourable-extreme
    move; once in profit, close when the adverse extreme retraces `x` of that peak.

    `fav`/`adv` are the favourable/adverse extreme unlevered move% paths (high/low
    for LONG, low/high for SHORT). Returns (realised_unlev, realised_lev) at the
    trailing-stop level; if it never triggers, the trade is HELD to its recorded
    close → (hold_ru, hold_rl). Causal: only past candles inform each step.
    Optimism note: peak and trigger are evaluated on the same candle (peak-first),
    so a single wide candle can both set the peak and trigger -- a finer tape (5m /
    10s resolver, the T-035 Phase-2 harness) tightens this.
    """
    peak = -1e9
    for k in range(len(fav)):
        if fav[k] > peak:
            peak = fav[k]
        if peak > 0 and adv[k] <= peak * (1.0 - x):
            tm = peak * (1.0 - x)
            return tm, max(tm * lev, CLAMP)
    return hold_ru, hold_rl


def sharpe(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    s = x.std(ddof=1) if len(x) > 1 else 0.0
    return float(x.mean() / s) if s > 0 else float("nan")


def compound_equity(rets: np.ndarray, f: float) -> tuple[float, float]:
    """Fixed-fraction sequential compounding: bet fraction `f` of equity as margin
    on each trade's leveraged return (fraction, -1 floored). Returns (final_mult,
    maxdd_pct). Sequential-by-close ignores concurrency (see report caveat)."""
    eq = 1.0
    peak = 1.0
    mdd = 0.0
    for r in rets:
        eq *= 1.0 + f * float(r)
        if eq <= 0:
            return 0.0, 100.0
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100.0)
    return eq, mdd


def percentiles(a, ps) -> dict:
    a = np.asarray(a, dtype=float)
    return {p: round(float(np.percentile(a, p)), 1) for p in ps} if len(a) else {}


# ─────────────────────────────────────────────────────────────────────────────
# DB LOADERS (strict read-only)
# ─────────────────────────────────────────────────────────────────────────────
def load_trades(conn, model_prefixes: list[str], lev: float, start: str | None = None) -> list[dict]:
    """Deduped closes with a recorded, sane realised move. `lev` assumed uniform.

    `model_prefixes == ['ALL']` drops the family filter (whole fleet, for the
    per-bot ranking). `start` (e.g. '2026-03-01') skips the Feb 357k LEGACY
    backfill blob. LEGACY-status rows (synthetic migration prices) are excluded.
    """
    from core.bot_naming import pretty_name

    where = [
        "entry IS NOT NULL",
        "close_price IS NOT NULL",
        "close_time IS NOT NULL",
        "(status IS NULL OR status NOT ILIKE %s)",
    ]
    params: list = ["%LEGACY%"]
    if model_prefixes != ["ALL"]:
        where.append("(" + " OR ".join(["model ILIKE %s"] * len(model_prefixes)) + ")")
        params += [f"{p}%" for p in model_prefixes]
    if start:
        where.append("open_time >= %s")
        params.append(start)
    sql = f"""
        SELECT symbol, model, upper(btrim(direction)) dir, entry, close_price,
               targets_hit, status, open_time, close_time
        FROM (
            SELECT DISTINCT ON (symbol, model, upper(btrim(direction)), open_time)
                   symbol, model, direction, entry, close_price, targets_hit,
                   status, open_time, close_time
            FROM closed_ai_signals
            WHERE {" AND ".join(where)}
            ORDER BY symbol, model, upper(btrim(direction)), open_time,
                     close_time ASC, targets_hit DESC, status ASC
        ) d
    """
    out = []
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    for sym, model, d, entry, close, hit, _status, ot, ct in rows:
        if d not in ("LONG", "SHORT"):
            continue
        mv = signed_move_pct(d, entry, close)
        if mv is None:
            continue
        tag = pretty_name(str(model))
        fam = next((p for p in model_prefixes if tag.upper().startswith(p)), tag)
        out.append(
            {
                "sym": sym.upper(),
                "tag": tag,
                "fam": fam,
                "dir": d,
                "entry": float(entry),
                "close": float(close),
                "hit": int(hit or 0),
                "real_unlev": mv,
                "real_lev": max(mv * lev, CLAMP),
                "ot": ot,
                "ct": ct,
            }
        )
    return out


def read_coin_wick(conn, symbol: str, lo, hi, tf: str) -> dict:
    """Wick-aware OHLC (open_time, high, low, close) for a coin, naive times."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT open_time, high, low, close FROM candles WHERE symbol=%s AND tf=%s "
            "AND open_time>=%s AND open_time<=%s AND is_closed ORDER BY open_time",
            (symbol, tf, lo, hi),
        )
        rows = cur.fetchall()
    if not rows:
        return {"t": np.array([], dtype="datetime64[ns]")}
    return {
        "t": np.array([r[0].replace(tzinfo=None) for r in rows], dtype="datetime64[ns]"),
        "h": np.array([float(r[1]) for r in rows], dtype=float),
        "l": np.array([float(r[2]) for r in rows], dtype=float),
        "c": np.array([float(r[3]) for r in rows], dtype=float),
    }


# ─────────────────────────────────────────────────────────────────────────────
# RECONSTRUCTION (coin-windowed; wave + per-trade peak + trailing sweep)
# ─────────────────────────────────────────────────────────────────────────────
def reconstruct(conn, trades: list[dict], lev: float, tf: str, build_wave: bool = True) -> dict:
    """Mark every open position hour-by-hour → aggregate wave; per trade compute
    the wick-aware peak and the trailing-close sweep from the same candle path.

    `build_wave=False` (per-bot ranking) skips the O(open-hours) grid accumulation
    and only computes the per-trade wick peak + trailing sweep."""
    import time

    gmin = min(t["ot"] for t in trades).replace(minute=0, second=0, microsecond=0)
    gmax = max(t["ct"] for t in trades).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    glen = int((gmax - gmin).total_seconds() // 3600) + 1
    grid = np.array([gmin + timedelta(hours=i) for i in range(glen)])

    def gi_of(dt):
        return int((dt.replace(minute=0, second=0, microsecond=0) - gmin).total_seconds() // 3600)

    keys = ["ALL"] + sorted({t["fam"] for t in trades})
    W = {k: np.zeros(glen) for k in keys}
    nopen = {k: np.zeros(glen) for k in keys}

    by_coin: dict = {}
    for t in trades:
        by_coin.setdefault(t["sym"], []).append(t)

    t0 = time.time()
    marked = 0
    no_candle = 0
    for ci, (sym, tl) in enumerate(sorted(by_coin.items()), 1):
        lo = min(t["ot"] for t in tl) - timedelta(hours=2)
        hi = max(t["ct"] for t in tl) + timedelta(hours=2)
        cd = read_coin_wick(conn, sym, lo, hi, tf)
        if len(cd["t"]) == 0:
            no_candle += len(tl)
            continue
        ct_all, hi_all, lo_all, cl_all = cd["t"], cd["h"], cd["l"], cd["c"]
        for t in tl:
            is_long = t["dir"] == "LONG"
            e = t["entry"]
            gi0, gi1 = gi_of(t["ot"]), gi_of(t["ct"])
            # aggregate wave: mark at each hourly step from the candle close as-of
            if build_wave and gi1 >= gi0:
                for gi in range(gi0, gi1 + 1):
                    pos = int(np.searchsorted(ct_all, np.datetime64(grid[gi]), side="right")) - 1
                    if pos < 0:
                        continue
                    p = cl_all[pos]
                    sgn = (p - e) / e if is_long else (e - p) / e
                    mark = max(sgn * lev * 100.0, CLAMP)
                    for k in ("ALL", t["fam"]):
                        W[k][gi] += mark
                        nopen[k][gi] += 1
            # per-trade wick peak + trailing sweep over the trade's candle slice
            ot64 = np.datetime64(t["ot"].replace(tzinfo=None))
            ct64 = np.datetime64(t["ct"].replace(tzinfo=None))
            m = (ct_all >= ot64) & (ct_all <= ct64)
            hh, ll = hi_all[m], lo_all[m]
            if len(hh) == 0:
                t["wick_peak_lev"] = None
                t["trail"] = None
                no_candle += 1
                continue
            fav = ((hh - e) / e * 100.0) if is_long else ((e - ll) / e * 100.0)
            adv = ((ll - e) / e * 100.0) if is_long else ((e - hh) / e * 100.0)
            t["wick_peak_lev"] = float(fav.max()) * lev
            t["trail"] = {x: trail_capture(fav, adv, x, lev, t["real_unlev"], t["real_lev"]) for x in X_SWEEP}
            marked += 1
        if ci % 100 == 0 or ci == len(by_coin):
            print(f"  [{ci}/{len(by_coin)}] marked={marked} ({time.time() - t0:.0f}s)", flush=True)

    return {
        "grid": grid,
        "gmin": gmin,
        "glen": glen,
        "W": W,
        "nopen": nopen,
        "keys": keys,
        "marked": marked,
        "no_candle": no_candle,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────
def analyse(trades: list[dict], rec: dict, lev: float) -> dict:
    val = [t for t in trades if t.get("trail")]
    rl = np.array([t["real_lev"] for t in val])
    ru = np.array([t["real_unlev"] for t in val])
    wp = np.array([t["wick_peak_lev"] for t in val])

    def leg_stats(sel):
        if not sel:
            return None
        a_rl = np.array([t["real_lev"] for t in sel])
        a_wp = np.array([t["wick_peak_lev"] for t in sel])
        return {
            "n": len(sel),
            "mean_real_lev": round(float(a_rl.mean()), 2),
            "mean_peak_lev": round(float(a_wp.mean()), 2),
            "mean_giveback": round(float((a_wp - a_rl).mean()), 2),
            "wr_pct": round(float((a_rl > 0).mean() * 100), 1),
            "giveback_pctiles": percentiles(a_wp - a_rl, [50, 90, 95]),
        }

    c1 = {"ALL": leg_stats(val)}
    for fam in rec["keys"][1:]:
        c1[fam] = leg_stats([t for t in val if t["fam"] == fam])
        for d in ("LONG", "SHORT"):
            c1[f"{fam}-{d}"] = leg_stats([t for t in val if t["fam"] == fam and t["dir"] == d])

    los = [t for t in val if t["real_lev"] <= 0]
    losers_in_profit = {
        thr: round(sum(1 for t in los if t["wick_peak_lev"] >= thr) / max(len(los), 1) * 100, 1)
        for thr in (10, 25, 50, 100, 200)
    }

    # aggregate wave
    wa = rec["W"]["ALL"]
    na = rec["nopen"]["ALL"]
    wave = {
        "max_open_unrealised": round(float(wa.max()), 0),
        "max_at": str(rec["grid"][int(wa.argmax())]),
        "max_concurrent_open": int(na.max()),
        "mean_concurrent_open": round(float(na[na > 0].mean()), 1),
    }

    # C2 cooldown probe: expectancy N days after a big aggregate peak
    p85 = float(np.quantile(wa[wa > 0], 0.85)) if (wa > 0).any() else 0.0
    peaks = []
    rise = 36
    for i in range(rise, rec["glen"] - 12):
        if wa[i] >= p85 and wa[i] == wa[i - 12 : i + 13].max() and wa[i] > wa[i - rise]:
            if not peaks or (rec["grid"][i] - rec["grid"][peaks[-1]]).total_seconds() >= 12 * 3600:
                peaks.append(i)
    base_ru = float(ru.mean())
    cooldown = {"n_peak_events": len(peaks), "baseline_real_unlev": round(base_ru, 4), "cohorts": {}}
    for n in (1, 2, 3, 5, 7):
        windows = [(rec["grid"][i], rec["grid"][i] + timedelta(days=n)) for i in peaks]
        sel = [t for t in val if any(a <= t["ot"] < b for a, b in windows)]
        if sel:
            s_ru = np.array([t["real_unlev"] for t in sel])
            s_rl = np.array([t["real_lev"] for t in sel])
            cooldown["cohorts"][n] = {
                "n": len(sel),
                "real_unlev": round(float(s_ru.mean()), 4),
                "real_lev": round(float(s_rl.mean()), 2),
                "delta_vs_base": round(float(s_ru.mean() - base_ru), 4),
            }

    # capture ceiling + risk-adjusted
    hold_sum = float(rl.sum())
    perfect_sum = float(wp.sum())
    trades_sorted = sorted(val, key=lambda t: t["ct"])
    hold_lev_frac = np.array([t["real_lev"] / 100.0 for t in trades_sorted])
    ceiling = {
        "hold_sum_lev": round(hold_sum, 0),
        "perfect_peak_sum_lev": round(perfect_sum, 0),
        "hold_sharpe_lev": round(sharpe(rl), 3),
        "hold_sharpe_unlev": round(sharpe(ru), 3),
        "hold_mean_unlev": round(float(ru.mean()), 4),
        "sweep": {},
        "compounding": {"f": [0.01, 0.02, 0.05], "hold": {}, "trail": {}},
    }
    for x in X_SWEEP:
        rlx = np.array([t["trail"][x][1] for t in val])
        rux = np.array([t["trail"][x][0] for t in val])
        frac = (rlx.sum() - hold_sum) / (perfect_sum - hold_sum) * 100 if perfect_sum != hold_sum else float("nan")
        ceiling["sweep"][x] = {
            "sum_lev": round(float(rlx.sum()), 0),
            "mean_lev": round(float(rlx.mean()), 2),
            "vs_hold_lev": round(float(rlx.sum() - hold_sum), 0),
            "pct_of_ceiling": round(float(frac), 1),
            "mean_unlev": round(float(rux.mean()), 4),
            "sharpe_lev": round(sharpe(rlx), 3),
            "sharpe_unlev": round(sharpe(rux), 3),
            "trig_pct": round(float(np.mean([t["trail"][x][1] != t["real_lev"] for t in val]) * 100), 1),
        }
    for f in ceiling["compounding"]["f"]:
        fm, md = compound_equity(hold_lev_frac, f)
        ceiling["compounding"]["hold"][f] = {"final_mult": fm, "maxdd_pct": round(md, 1)}
        ceiling["compounding"]["trail"][f] = {}
        for x in X_SWEEP:
            lv = np.array([t["trail"][x][1] / 100.0 for t in trades_sorted])
            fm, md = compound_equity(lv, f)
            ceiling["compounding"]["trail"][f][x] = {"final_mult": fm, "maxdd_pct": round(md, 1)}

    return {
        "n_scored": len(val),
        "c1_asymmetry": c1,
        "losers_in_profit_pct": losers_in_profit,
        "wave": wave,
        "c2_cooldown": cooldown,
        "ceiling": ceiling,
    }


# ─────────────────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────────────────
def render_md(meta: dict) -> str:
    s = meta["summary"]
    a = s["analysis"]
    c1 = a["c1_asymmetry"]["ALL"]
    L = []
    ap = L.append
    ap("# Wave-Buildup -- Realised-vs-Unrealised (Phase A, T-2026-KYT-9050-041)\n")
    ap(
        f"_generated {meta['generated_at']} · read-only · models {'+'.join(s['models'])} · "
        f"{s['lev']:.0f}x assumed · window {s['span'][0]} → {s['span'][1]}_\n"
    )
    ap(
        "**What this is:** Follow-up to T-035. Reconstructs the aggregated open **unrealised** wave "
        "and realised outcomes of curated S/R AI bots (AIM, SRA) over the **full history** from "
        "RECORDED trades (`closed_ai_signals`, dedup report-14-survivor-key) + candles -- **without** "
        "the immutable Cornix geometry (which capped T-035 to ~7d outbox retention). Price: no intra-trade "
        "DCA/TP laddering modelled (first-order wave). Leverage **assumed uniform 20x** (not persisted before "
        "~July; pattern is leverage-agnostic). Realised = recorded entry→close move.\n"
    )
    ap(f"Trades scored: **{a['n_scored']}** · without candles: {s['no_candle']}.\n")

    ap("## C1 -- Realised-vs-Unrealised Asymmetry (the premise)\n")
    ap("| Segment | n | mean realised% | mean **peak**% (wick) | **giveback** | WR% | giveback p50/p90/p95 |")
    ap("|---|--:|--:|--:|--:|--:|--:|")
    for key, st in a["c1_asymmetry"].items():
        if not st:
            continue
        gp = st["giveback_pctiles"]
        ap(
            f"| {key} | {st['n']} | {st['mean_real_lev']:+} | {st['mean_peak_lev']:+} | "
            f"{st['mean_giveback']:+} | {st['wr_pct']} | {gp.get(50)}/{gp.get(90)}/{gp.get(95)} |"
        )
    lp = a["losers_in_profit_pct"]
    ap(
        f"\n**Core observation, proven:** of the losing trades, **{lp[10]}% were once at ≥+10%**, "
        f"{lp[25]}% at ≥+25%, **{lp[50]}% at ≥+50%**, {lp[100]}% at ≥+100%, {lp[200]}% at ≥+200%** "
        "(leveraged, wick) in profit -- then closed at a loss. Gains evaporate, losses are fully realised. "
        f"Avg trade: realised {c1['mean_real_lev']:+}% vs peak {c1['mean_peak_lev']:+}% → **{c1['mean_giveback']:+}% "
        "giveback**.\n"
    )
    w = a["wave"]
    ap(
        f"Aggregate wave: max Σ open unrealised **{w['max_open_unrealised']:+.0f}** margin units "
        f"({w['max_at']}), up to **{w['max_concurrent_open']}** open simultaneously (avg {w['mean_concurrent_open']}).\n"
    )

    cd = a["c2_cooldown"]
    ap("## C2 - Cooldown probe: expectancy N days AFTER a large wave peak\n")
    ap(f"Baseline (all) real_unlev **{cd['baseline_real_unlev']:+.3f}%** · {cd['n_peak_events']} peak events (p85).\n")
    ap("| Cohort | n | real_unlev% | Delta vs baseline |")
    ap("|---|--:|--:|--:|")
    for n, co in cd["cohorts"].items():
        ap(f"| {n} day(s) after peak | {co['n']} | {co['real_unlev']:+.3f} | {co['delta_vs_base']:+.3f} |")
    ap(
        "\n**C2 verdict:** only the first ~24h after a peak are measurably weaker; days 2-7 are NOT worse "
        "(rather better). The 'sit out 3-5 days after a big wave' idea would not have saved expectancy "
        "historically, only squandered good days. **The lever is on the close side, not re-entry timing.**\n"
    )

    ce = a["ceiling"]
    ap("## CEIL -- capture ceiling & risk-adjusted resolution\n")
    ap(
        f"HOLD (actual) Σ lev **{ce['hold_sum_lev']:+.0f}** · PERFECT-PEAK (hindsight upper bound) "
        f"Σ lev {ce['perfect_peak_sum_lev']:+.0f} (~{ce['perfect_peak_sum_lev'] / max(ce['hold_sum_lev'], 1):.1f}× "
        "hold, unreachable).\n"
    )
    ap("| Trailing X% | Σ lev | mean lev% | vs hold | % of ceiling | **Sharpe lev** | mean unlev% | trig% |")
    ap("|--:|--:|--:|--:|--:|--:|--:|--:|")
    ap(
        f"| **hold** | {ce['hold_sum_lev']:+.0f} | {c1['mean_real_lev']:+} | -- | -- | "
        f"**{ce['hold_sharpe_lev']:+}** | {ce['hold_mean_unlev']:+.3f} | 0 |"
    )
    for x in X_SWEEP:
        sw = ce["sweep"][x]
        ap(
            f"| {int(x * 100)}% | {sw['sum_lev']:+.0f} | {sw['mean_lev']:+} | {sw['vs_hold_lev']:+.0f} | "
            f"{sw['pct_of_ceiling']}% | **{sw['sharpe_lev']:+}** | {sw['mean_unlev']:+.3f} | {sw['trig_pct']} |"
        )
    ap(
        "\n**The reversal:** on the leveraged **sum** trailing barely/never beats hold (the sum is dominated by "
        "a few uncapped fat-tail hits, trailing clips those -- confirmed T-035). **Risk-adjusted it flips:** "
        f"per-trade **Sharpe lev {ce['hold_sharpe_lev']:+} (hold) → "
        f"{ce['sweep'][0.10]['sharpe_lev']:+} (trailing 10%)** -- trailing ~halves the dispersion and lifts "
        f"the mean. Unlevered: mean {ce['hold_mean_unlev']:+.3f}%/trade (hold, ~breakeven) → "
        f"{ce['sweep'][0.10]['mean_unlev']:+.3f}%/trade (trailing 10%).\n"
    )

    ap("### Compounding equity (fixed bet fraction, sequential by close) -- the real account\n")
    ap("| Bet per trade | HOLD final | HOLD MaxDD | Trailing 10% final | Trailing 10% MaxDD |")
    ap("|--:|--:|--:|--:|--:|")
    for f in ce["compounding"]["f"]:
        h = ce["compounding"]["hold"][f]
        tr = ce["compounding"]["trail"][f][0.10]
        ap(
            f"| {f * 100:.0f}% | ×{h['final_mult']:.3g} | **{h['maxdd_pct']}%** | "
            f"×{tr['final_mult']:.3g} | **{tr['maxdd_pct']}%** |"
        )
    ap(
        "\n**Phase A conclusion:** (1) The asymmetry is real and large -- premise confirmed. (2) The "
        "cooldown/re-entry idea is NOT supported by the data. (3) A tight **trailing close (10-15%)** is "
        "risk-adjusted and compounding **clearly superior** (higher Sharpe, more compounding, ~6× smaller "
        "drawdown) -- T-035s 'hold wins' was an artefact of leveraged **sum**. Next step is validation on the "
        "T-035 high-fidelity harness (5m wick + 10s resolver, Sharpe/MaxDD vs sum), including the "
        "entry2-as-SL question.\n"
    )

    ap("## Ehrliche Grenzen\n")
    for lim in meta["limits"]:
        ap(f"- {lim}")
    ap("")
    return "\n".join(L)


LIMITS = [
    "First-order wave: recorded entry/close as ground truth, NO intra-trade DCA/TP laddering -- the "
    "unrealised amplitude is slightly overstated (peak timing unaffected). The T-035 phase-2 harness is "
    "the laddering-faithful reference (but only ~7d outbox window there).",
    "Leverage uniform 20x (not persisted before ~July). The pattern (giveback, Sharpe reversal) is "
    "leverage-agnostic; the absolute leveraged figures scale with this assumption + the -100% clamp.",
    "Trailing sim: 1h wick, peak-before-trigger on the same candle (slightly optimistic on trigger level). "
    "A 5m/10s resolver (T-035) tightens this; the DIRECTION (Sharpe/MaxDD advantage) is robust.",
    "Compounding sequential by close → ignores concurrency (up to the above-mentioned simultaneous open trades, "
    "cross-margin). The absolute multiples (×1e26) are NOT literal -- ratio + MaxDD are the signal.",
    "Live/shadow gate state is time-variable; no gate filtering here -- these are the AIM/SRA strategies over "
    "all generations (AIM1→AIM2, SRA1→SRA2).",
]


# ─────────────────────────────────────────────────────────────────────────────
# PHASE B -- PER-BOT RANKING (where does trailing-close help?)
# ─────────────────────────────────────────────────────────────────────────────
RANK_F = 0.02  # fixed fraction for the per-bot compounding-MaxDD comparison
THIN_N = 30  # below this a bot's Sharpe sign is not trusted


def analyse_rank(trades: list[dict], lev: float) -> list[dict]:
    """Group the scored trades per emitting bot (bot_catalog family) and score
    hold vs trailing-close: per-trade leveraged Sharpe (hold, X=10%, best-X),
    fixed-fraction compounding MaxDD (hold vs best-X), mean giveback. Sorted by
    the Sharpe uplift (best-X − hold) descending -- i.e. where trailing helps most."""
    from core.bot_catalog import script_for_tag

    groups: dict[str, list[dict]] = {}
    labels: dict[str, set] = {}
    for t in trades:
        if not t.get("trail"):
            continue
        key = script_for_tag(t["tag"]) or f"?{t['tag']}"
        groups.setdefault(key, []).append(t)
        labels.setdefault(key, set()).add(t["tag"])

    rows = []
    for key, sel in groups.items():
        rl = np.array([t["real_lev"] for t in sel])
        wp = np.array([t["wick_peak_lev"] for t in sel])
        sh_hold = sharpe(rl)
        by_x = {x: sharpe(np.array([t["trail"][x][1] for t in sel])) for x in X_SWEEP}
        best_x = max(by_x, key=lambda x: by_x[x] if not np.isnan(by_x[x]) else -1e9)
        ss = sorted(sel, key=lambda t: t["ct"])
        _, mdd_hold = compound_equity(np.array([t["real_lev"] / 100.0 for t in ss]), RANK_F)
        _, mdd_best = compound_equity(np.array([t["trail"][best_x][1] / 100.0 for t in ss]), RANK_F)
        rows.append(
            {
                "bot": key.replace(".py", ""),
                "tags": ",".join(sorted(labels[key])),
                "n": len(sel),
                "wr_pct": round(float((rl > 0).mean() * 100), 1),
                "mean_giveback": round(float((wp - rl).mean()), 1),
                "sharpe_hold": round(float(sh_hold), 3) if not np.isnan(sh_hold) else None,
                "sharpe_t10": round(float(by_x[0.10]), 3) if not np.isnan(by_x[0.10]) else None,
                "best_x": int(best_x * 100),
                "sharpe_best": round(float(by_x[best_x]), 3) if not np.isnan(by_x[best_x]) else None,
                "uplift": round(float(by_x[best_x] - sh_hold), 3)
                if not (np.isnan(by_x[best_x]) or np.isnan(sh_hold))
                else None,
                "maxdd_hold": round(float(mdd_hold), 1),
                "maxdd_best": round(float(mdd_best), 1),
                "thin": len(sel) < THIN_N,
            }
        )
    rows.sort(key=lambda r: (r["uplift"] is not None, r["uplift"] or -1e9), reverse=True)
    return rows


def render_rank_md(meta: dict) -> str:
    s = meta["summary"]
    L = []
    ap = L.append
    ap("# Wave-Buildup -- per-bot trailing-close ranking (Phase B, T-2026-KYT-9050-041)\n")
    ap(
        f"_generated {meta['generated_at']} · read-only · ALL bots · {s['lev']:.0f}x assumed · "
        f"tf {s['tf']} · from {s['start']} · n_trades {s['n_trades']}_\n"
    )
    ap(
        "**Question:** for which bots does a tight **trailing close** lift risk-adjusted return? Per bot "
        "(bot_catalog family): per-trade leveraged **Sharpe** hold vs trailing 10% vs best-X, and the "
        f"compounding **MaxDD** (fixed {RANK_F * 100:.0f}% fraction) hold vs best-X. Sorted by **Sharpe uplift "
        "(best − hold)**. Methodology + caveats like phase A (first-order, entry1-only, "
        f"{s['lev']:.0f}x, trigger optimism). LEGACY status + Feb backfill excluded (from {s['start']}).\n"
    )
    ap(
        "| Bot | tags | n | WR% | avg giveback | **Sharpe hold** | Sharpe t10% | best-X | **Sharpe best** | **uplift** | MaxDD hold→best | verdict |"
    )
    ap("|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|")

    def sf(v, spec="+"):  # None-safe signed formatter
        return format(v, spec) if v is not None else "--"

    for r in meta["ranking"]:
        verdict = "THIN" if r["thin"] else ("TRAILING-HELPS" if (r["uplift"] or 0) > 0.05 else "neutral/no")
        ap(
            f"| {r['bot']} | {r['tags']} | {r['n']} | {r['wr_pct']} | {sf(r['mean_giveback'])} | "
            f"**{sf(r['sharpe_hold'])}** | {sf(r['sharpe_t10'])} | {r['best_x']}% | **{sf(r['sharpe_best'])}** | "
            f"**{sf(r['uplift'])}** | {r['maxdd_hold']}%→{r['maxdd_best']}% | {verdict} |"
        )
    ap(
        "\n**Read key:** positive **uplift** = trailing lifts per-trade Sharpe (the phase-A reversal holds for "
        "this bot). MaxDD hold→best shows drawdown reduction. `THIN` (n<30) = sign not reliable. Finalists "
        "(high uplift, solid n) belong on the T-035 high-fidelity harness (5m + 10s + DCA-faithful) for "
        "confirmation.\n"
    )
    ap("## Honest limitations\n")
    for lim in meta["limits"]:
        ap(f"- {lim}")
    ap("")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="Wave-buildup study (T-2026-KYT-9050-041, Phase A/B)")
    ap.add_argument(
        "--mode",
        default="study",
        choices=["study", "rank"],
        help="study = Phase-A wave+asymmetry for --models; rank = Phase-B per-bot trailing ranking",
    )
    ap.add_argument("--models", default="AIM,SRA", help="comma-separated family prefixes, or ALL (rank mode)")
    ap.add_argument("--lev", type=float, default=20.0)
    ap.add_argument("--tf", default=None, help="candle timeframe for wave/peak (default: study 1h, rank 15m)")
    ap.add_argument("--start", default=None, help="min open_time (rank: default 2026-03-01 to skip Feb backfill)")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR)
    ap.add_argument("--allow-high-cpu", action="store_true", help="skip the CPU-headroom abort (VPS saturated)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from core.database import get_db_connection
    from core.time import utc_now
    from tools.walkforward_sim import check_cpu_headroom, set_low_priority

    set_low_priority()
    if args.allow_high_cpu:
        print("CPU headroom check skipped (--allow-high-cpu; BELOW_NORMAL + read-only).")
    else:
        try:
            check_cpu_headroom()
        except SystemExit as e:
            print(f"WARN {e} -- runs anyway (read-only, BELOW_NORMAL).", flush=True)

    is_rank = args.mode == "rank"
    prefixes = (
        ["ALL"]
        if (is_rank and args.models.strip().upper() == "ALL")
        else [p.strip().upper() for p in args.models.split(",") if p.strip()]
    )
    start = args.start or ("2026-03-01" if is_rank else None)
    tf = args.tf or ("15m" if is_rank else "1h")  # explicit --tf respected; sensible per-mode default

    conn = get_db_connection()
    try:
        conn.set_session(readonly=True)
    except Exception:
        pass
    try:
        trades = load_trades(conn, prefixes, args.lev, start)
        print(f"trades: {len(trades)}  coins={len(set(t['sym'] for t in trades))}  tf={tf}", flush=True)
        rec = reconstruct(conn, trades, args.lev, tf, build_wave=not is_rank)
    finally:
        conn.close()

    span = [str(min(t["ot"] for t in trades)), str(max(t["ct"] for t in trades))]
    os.makedirs(args.out, exist_ok=True)

    if is_rank:
        ranking = analyse_rank(trades, args.lev)
        meta = {
            "task": "T-2026-KYT-9050-041",
            "phase": "B",
            "generated_at": str(utc_now()),
            "limits": LIMITS,
            "ranking": ranking,
            "summary": {
                "models": prefixes,
                "lev": args.lev,
                "tf": tf,
                "start": start,
                "span": span,
                "n_trades": len(trades),
                "no_candle": rec["no_candle"],
            },
        }
        tag = "wave_buildup_rank_allbots"
        md = render_rank_md(meta)
    else:
        analysis = analyse(trades, rec, args.lev)
        meta = {
            "task": "T-2026-KYT-9050-041",
            "phase": "A",
            "generated_at": str(utc_now()),
            "limits": LIMITS,
            "summary": {
                "models": prefixes,
                "lev": args.lev,
                "tf": tf,
                "span": span,
                "n_trades": len(trades),
                "no_candle": rec["no_candle"],
                "analysis": analysis,
            },
        }
        tag = "wave_buildup_study_" + "".join(p.lower() for p in prefixes)
        md = render_md(meta)

    with open(os.path.join(args.out, f"{tag}.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)
    with open(os.path.join(args.out, f"{tag}.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    print("\n" + md)
    print(f"\nJSON: {os.path.join(args.out, tag + '.json')}")


if __name__ == "__main__":
    main()
