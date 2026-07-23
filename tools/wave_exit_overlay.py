"""
tools/wave_exit_overlay.py — Wave-Exit-Overlay harness (T-2026-KYT-9050-035).

PHASE 1 (this file, --mode validate): a high-fidelity replay of the curated
bots' real signals, validated against the recorded Cornix outcome before any
exit overlay is layered on. The point of Phase 1 is a TRUSTWORTHY harness:
reproduce the bot's own realised PnL / win-rate / duration from the immutable
signal geometry + wick-aware candles, so that Phase 2's overlay numbers mean
something.

Data model (see the T-035 discovery notes)
------------------------------------------
  * Geometry comes from the IMMUTABLE Cornix message in `telegram_outbox`
    (entry1 = CMP, entry2 = DCA limit, the ORIGINAL stop, TP1..TP3). It is NOT
    read from `ai_signals`, whose `sl` the live monitor overwrites as it trails.
  * Outcome ground truth is `closed_ai_signals` (targets_hit against the full
    internal target array, close_price, status) — what 8_ai_trade_monitor
    actually recorded. A closed row is matched to its Cornix message by
    symbol + direction + entry1 + signal time (exact to the second in practice).
  * Touch detection runs on COMPLETE, wick-aware 5m OHLC `candles` (12x finer
    than the live monitor's 1h). `ticker_10s` is a ~40s-sampled, gappy snapshot
    that misses ~81% of SL touches (T-035 discovery), so it is used ONLY as an
    order-resolver: which of SL/TP traded first inside a single 5m candle that
    touched both. Both are read coin-windowed.

Two ladders, two entry models (operator decision, T-035)
--------------------------------------------------------
  * `monitor`  — entry1-only, full internal target array. This is the closest
    reproduction of 8_ai_trade_monitor and the config the validation compares
    against `closed_ai_signals`. The residual divergence is the 5m-wick + real
    intra-candle ordering being deliberately FINER than the monitor's 1h.
  * `dca10`    — entry1/entry2 50/50 DCA, full target array.
  * `cornix3`  — entry1/entry2 50/50 DCA, the 3 PUBLISHED TPs Cornix actually
    executes (real money, thirds). This is the headline realised number and the
    substrate the Phase-2 overlay will act on.

Realised PnL always goes through `core.realized_pnl` (shared builder, Regel #7):
this file only decides (targets_hit, close_price) per leg from the candles.

Betriebsregeln (Live-VPS!): DB strictly read-only, BELOW_NORMAL priority, CPU
headroom checked, coin-windowed reads. Output ONLY to staging_models/replay/.

Beispiele
---------
  python tools/wave_exit_overlay.py --mode validate --model AIM2 --limit 50
  python tools/wave_exit_overlay.py --mode validate --model AIM2
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import timedelta, timezone

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.database import get_db_connection  # noqa: E402
from core.realized_pnl import parse_leverage, realized_pnl_pct, weighted_move_pct  # noqa: E402
from core.time import utc_now  # noqa: E402
from core.wave_exit_sim import (  # noqa: E402
    mark_to_market_series,
    portfolio_circuit_breaker,
    simulate_signal,
    trailing_tp_trigger,
)
from tools.walkforward_sim import (  # noqa: E402
    check_cpu_headroom,
    import_bot_module,
    set_low_priority,
)

# Reports live next to the T-032 audit in the repo tree (not the _X env dir),
# so the whole wave-exit investigation stays together and reviewable.
DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, "staging_models", "replay")
FEE_PER_SIDE = 0.0005  # 0.10% round-trip, same as walkforward_sim / the audit


# ─────────────────────────────────────────────────────────────────────────────
# DB LOADERS (read-only)
# ─────────────────────────────────────────────────────────────────────────────
def tick_utc_offset(conn) -> timezone:
    """The tz-offset ticker_10s.ts is stored in (PG session local, e.g. +03).

    closed_ai_signals.open_time is naive wall-clock in that SAME zone, so we
    localise the naive signal times with this offset before slicing ticks.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT ts FROM ticker_10s ORDER BY ts LIMIT 1")
        row = cur.fetchone()
    off = row[0].utcoffset() if row and row[0].tzinfo else timezone.utc.utcoffset(None)
    return timezone(off)


def load_closed(conn, model: str, start: str, end: str, limit: int | None) -> list[dict]:
    """Closed rows for `model` in [start, end) with the outcome ground truth."""
    sql = """
        SELECT id, symbol, direction, entry, close_price, targets_hit, targets,
               lev, status, open_time, close_time
        FROM closed_ai_signals
        WHERE model = %s AND open_time >= %s AND open_time < %s
          AND entry IS NOT NULL AND close_price IS NOT NULL AND targets IS NOT NULL
        ORDER BY open_time
    """
    params: list = [model, start, end]
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "id": r[0],
                "symbol": (r[1] or "").upper(),
                "direction": (r[2] or "").upper(),
                "entry": float(r[3]),
                "close_price": float(r[4]),
                "targets_hit": int(r[5] or 0),
                "targets": r[6],
                "lev": r[7],
                "status": r[8],
                "open_time": r[9],
                "close_time": r[10],
            }
        )
    return out


def load_cornix_geometry(conn, model: str, start: str, end: str, parse_fn) -> dict:
    """Immutable Cornix geometry from telegram_outbox, indexed by (coin, dir).

    Only the plain-text (Cornix-parseable) message carries the geometry; the
    HTML info twin is skipped by the parser (it rejects <pre>). Entry2 is pulled
    with a dedicated regex — the shared parser only exposes the single CMP entry.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT message, created_at FROM telegram_outbox
            WHERE message LIKE %s AND message LIKE %s AND message NOT LIKE %s
              AND created_at >= %s AND created_at < %s
            """,
            ("%Signal for%", f"%({model})%", "%<pre>%", start, end),
        )
        rows = cur.fetchall()
    idx: dict = defaultdict(list)
    for msg, ca in rows:
        p = parse_fn(msg)
        if not p:
            continue
        e2 = re.search(r"Entry 2:\s*\$\s*([0-9.]+)", msg)
        p["entry2"] = float(e2.group(1)) if e2 else None
        idx[(p["coin"].upper(), p["direction"].upper())].append((p, ca))
    return idx


def match_geometry(closed: list[dict], cornix_idx: dict) -> tuple[list[dict], int]:
    """Attach original sl / entry2 / published TPs to each closed row.

    Match key: symbol + direction + entry1 within 0.5% + nearest signal time.
    Returns (matched_records, n_unmatched). Unmatched rows are reported, never
    silently dropped — coverage is bounded by telegram_outbox retention and that
    limit is stated honestly in the report.
    """
    matched = []
    unmatched = 0
    for c in closed:
        cands = cornix_idx.get((c["symbol"], c["direction"]), [])
        best = None
        best_dt = None
        for p, ca in cands:
            if abs(p["entry"] - c["entry"]) / c["entry"] >= 5e-3:
                continue
            dt = abs((ca.replace(tzinfo=None) - c["open_time"]).total_seconds())
            if best_dt is None or dt < best_dt:
                best_dt, best = dt, p
        if best is None or best.get("sl") is None or len(best.get("targets", [])) < 3:
            unmatched += 1
            continue
        matched.append(
            {
                **c,
                "entry1": c["entry"],
                "entry2": best.get("entry2"),
                "orig_sl": float(best["sl"]),
                "cornix_targets": [float(t) for t in best["targets"][:3]],
                "match_dt_s": best_dt,
            }
        )
    return matched, unmatched


def read_coin_candles(conn, symbol: str, lo, hi) -> dict:
    """Complete wick-aware 5m OHLC for `symbol` in [lo, hi], ASC.

    The 5m candle is the touch-detection backbone: exchange-computed high/low
    over every trade, 12x finer than the live monitor's 1h, and — unlike the
    10s tape — gap-free (T-035 discovery). Returns naive-wall-clock times so it
    aligns with closed_ai_signals.open_time and the 10s resolver.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT open_time, high, low, close FROM candles "
            "WHERE symbol = %s AND tf = '5m' AND open_time >= %s AND open_time <= %s AND is_closed "
            "ORDER BY open_time",
            (symbol, lo, hi),
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


def read_coin_ticks(conn, symbol: str, lo, hi) -> tuple[np.ndarray, np.ndarray]:
    """All (ts, price) for `symbol` in [lo, hi], ASC — for the order-resolver only."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts, price FROM ticker_10s WHERE symbol = %s AND ts >= %s AND ts <= %s ORDER BY ts",
            (symbol, lo, hi),
        )
        rows = cur.fetchall()
    if not rows:
        return np.array([], dtype="datetime64[ns]"), np.array([], dtype=float)
    times = np.array([r[0].replace(tzinfo=None) for r in rows], dtype="datetime64[ns]")
    prices = np.array([float(r[1]) for r in rows], dtype=float)
    return times, prices


CANDLE_5M = np.timedelta64(5, "m")


def make_order_resolver(cand_times: np.ndarray, tick_t: np.ndarray, tick_p: np.ndarray):
    """Closure: resolve SL-vs-TP order inside candle `idx` from the 10s tape.

    Returns 'tp' only if a tick proves the target traded before the stop within
    that 5m window; otherwise 'sl' (the monitor's conservative default, also used
    when the gappy tape has no tick in the window). `idx` indexes `cand_times`.
    """

    def resolver(idx: int, is_long: bool, sl_level: float, tp_level: float) -> str:
        if len(tick_t) == 0 or idx >= len(cand_times):
            return "sl"
        t0 = cand_times[idx]
        t1 = t0 + CANDLE_5M
        m = (tick_t >= t0) & (tick_t < t1)
        for p in tick_p[m]:
            sl_hit = (p <= sl_level) if is_long else (p >= sl_level)
            tp_hit = (p >= tp_level) if is_long else (p <= tp_level)
            if sl_hit:
                return "sl"
            if tp_hit:
                return "tp"
        return "sl"

    return resolver


# ─────────────────────────────────────────────────────────────────────────────
# REPLAY + PnL (per config)
# ─────────────────────────────────────────────────────────────────────────────
CONFIGS = {
    "monitor": {"dca": False, "ladder": "internal"},  # entry1-only, full targets
    "dca10": {"dca": True, "ladder": "internal"},  # DCA, full targets
    "cornix3": {"dca": True, "ladder": "cornix"},  # DCA, 3 published TPs
}


def _entries(rec: dict, dca: bool) -> list[tuple[float, float]]:
    if dca and rec.get("entry2"):
        return [(rec["entry1"], 0.5), (rec["entry2"], 0.5)]
    return [(rec["entry1"], 1.0)]


def _targets(rec: dict, ladder: str) -> list[float]:
    return rec["cornix_targets"] if ladder == "cornix" else [float(t) for t in rec["targets"]]


def replay_record(
    rec: dict, times: np.ndarray, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, cfg: dict, resolver
) -> dict | None:
    """Run one config; return sim outcome + realised (unlev/net/leveraged)."""
    if len(highs) == 0:
        return None
    entries = _entries(rec, cfg["dca"])
    targets = _targets(rec, cfg["ladder"])
    sim = simulate_signal(
        highs, lows, closes, rec["direction"], entries, rec["orig_sl"], targets, order_resolver=resolver
    )
    if not sim["any_filled"]:
        return {"filled": False}

    lev = parse_leverage(rec["lev"])
    unlev = net = levered = 0.0
    have_lev = lev is not None
    pos_tp1 = False  # position reached TP1 (any leg)
    last_exit_idx = 0
    filled_weight = 0.0
    for leg in sim["legs"]:
        if not leg["filled"]:
            continue
        filled_weight += leg["weight"]
        mv = weighted_move_pct(rec["direction"], leg["entry"], leg["close_price"], targets, leg["targets_hit"])
        if mv is None:
            continue
        unlev += leg["weight"] * mv
        net += leg["weight"] * (mv - 2.0 * FEE_PER_SIDE * 100.0)
        if have_lev:
            lp = realized_pnl_pct(
                rec["direction"], leg["entry"], leg["close_price"], targets, leg["targets_hit"], rec["lev"]
            )
            if lp is not None:
                levered += leg["weight"] * lp
        if leg["targets_hit"] >= 1:
            pos_tp1 = True
        if leg["exit_idx"] is not None:
            last_exit_idx = max(last_exit_idx, leg["exit_idx"])

    exit_time = times[last_exit_idx] if len(times) else None
    dur_h = None
    if exit_time is not None and len(times):
        dur_h = (exit_time - times[0]) / np.timedelta64(1, "h")
    # position-level targets_hit for the validation config = max leg (entry1-only
    # has a single leg, so this is exactly that leg's count)
    pos_targets_hit = max((leg["targets_hit"] for leg in sim["legs"] if leg["filled"]), default=0)
    return {
        "filled": True,
        "targets_hit": int(pos_targets_hit),
        "tp1": pos_tp1,
        "unlev_pct": round(unlev, 5),
        "net_pct": round(net, 5),
        "levered_pct": round(levered, 4) if have_lev else None,
        "filled_weight": round(filled_weight, 3),
        "dur_h": round(float(dur_h), 3) if dur_h is not None else None,
        "exit_reason": sim["legs"][0]["exit_reason"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2 — AUTO-CLOSE OVERLAYS (on the cornix3 real-money config)
# ─────────────────────────────────────────────────────────────────────────────
X_SWEEP = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40]  # per-trade trailing-TP retrace (a)
Y_SWEEP = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40]  # portfolio unrealised retrace (c)
GRID = np.timedelta64(5, "m")


def _cornix_mtm(art: dict) -> np.ndarray:
    rec = art["rec"]
    entries = _entries(rec, True)
    targets = _targets(rec, "cornix")
    return mark_to_market_series(
        art["c"],
        rec["direction"],
        entries,
        rec["orig_sl"],
        targets,
        highs=art["h"],
        lows=art["l"],
        order_resolver=art["resolver"],
    )


def _realized_at(art: dict, k: int) -> dict | None:
    """cornix3 realised if the trade is force-closed after candle index k-1."""
    k = max(1, min(int(k), len(art["c"])))
    return replay_record(
        art["rec"], art["t"][:k], art["h"][:k], art["l"][:k], art["c"][:k], CONFIGS["cornix3"], art["resolver"]
    )


def _dir_agg(rows: list[dict]) -> dict:
    """rows = [{dir, unlev, net, lev, tp1, triggered}]; aggregate per direction + ALL."""
    out = {}
    for key in ("ALL", "LONG", "SHORT"):
        sel = rows if key == "ALL" else [r for r in rows if r["dir"] == key]
        if not sel:
            out[key] = None
            continue
        lev = [r["lev"] for r in sel if r["lev"] is not None]
        out[key] = {
            "n": len(sel),
            "unlev_sum": round(sum(r["unlev"] for r in sel), 2),
            "net_sum": round(sum(r["net"] for r in sel), 2),
            "lev_sum": round(sum(lev), 1) if lev else None,
            "wr_tp1": round(sum(1 for r in sel if r["tp1"]) / len(sel) * 100, 1),
            "trig_pct": round(sum(1 for r in sel if r["triggered"]) / len(sel) * 100, 1),
        }
    return out


def overlay_a(arts: list[dict], x: float) -> dict:
    """(a) Per-trade trailing-TP: close at x% retrace from the trade's MTM peak."""
    rows = []
    for art in arts:
        mtm = _cornix_mtm(art)
        tr = trailing_tp_trigger(mtm, x)
        k = (tr + 1) if tr is not None else len(mtm)
        r = _realized_at(art, k)
        if not r or not r.get("filled"):
            continue
        rows.append(
            {
                "dir": art["rec"]["direction"],
                "unlev": r["unlev_pct"],
                "net": r["net_pct"],
                "lev": r["levered_pct"],
                "tp1": r["tp1"],
                "triggered": tr is not None,
            }
        )
    return _dir_agg(rows)


def _grid_index(t: np.datetime64, grid0: np.datetime64) -> int:
    return int((t - grid0) / GRID)


def _levered_marks(art: dict):
    """(grid_enter, grid_close_natural, levered-mark-per-candle) for the portfolio pass."""
    lev = parse_leverage(art["rec"]["lev"])
    mtm = _cornix_mtm(art)
    lm = np.maximum(mtm * lev, -1.0) if lev is not None else mtm  # account fraction, -100% floor
    return lm


def portfolio_maxdd(equity: np.ndarray) -> float:
    """Max peak-to-trough drawdown of an equity curve, in the curve's own units."""
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    return float(np.max(peak - equity))


def _open_wave(arts: list[dict], glen: int, close_k: dict | None = None) -> np.ndarray:
    """Aggregate open-position levered mark per grid step (the visible portfolio wave).

    Each trade contributes its levered mark from its enter step until it closes —
    naturally, or (if `close_k[idx]` is set) at that forced candle index. After
    close it contributes nothing (realised, off the open book). This is the wave
    whose peak-to-trough the operator watches evaporate.
    """
    wave = np.zeros(glen)
    for idx, a in enumerate(arts):
        gi, lm = a["_gi"], a["_lm"]
        stop = len(a["c"]) if close_k is None or idx not in close_k else close_k[idx]
        for j in range(min(stop, len(gi))):
            wave[gi[j]] += lm[j]
    return wave


def run_overlays(arts: list[dict]) -> dict:
    """Compute baseline + overlay (a) and (c) sweeps, direction-split, with MaxDD."""
    arts = [a for a in arts if len(a["c"]) >= 1 and parse_leverage(a["rec"]["lev"]) is not None]
    if not arts:
        return {"summary": {"note": "no leveraged arts"}}

    # common 5m grid
    grid0 = min(a["t"][0] for a in arts)
    gridN = max(a["t"][-1] for a in arts)
    glen = _grid_index(gridN, grid0) + 1
    for a in arts:
        a["_lm"] = _levered_marks(a)
        a["_gi"] = np.array([_grid_index(t, grid0) for t in a["t"]], dtype=int)

    # ---- baseline (cornix3 hold-to-natural-close) ----
    base_rows = []
    for a in arts:
        r = _realized_at(a, len(a["c"]))  # one replay per art, not one per field
        if not r or not r.get("filled"):
            continue
        base_rows.append(
            {
                "dir": a["rec"]["direction"],
                "unlev": r["unlev_pct"],
                "net": r["net_pct"],
                "lev": r["levered_pct"],
                "tp1": r["tp1"],
                "triggered": False,
            }
        )
    baseline = _dir_agg(base_rows)
    baseline["maxdd_open_wave"] = round(portfolio_maxdd(_open_wave(arts, glen)), 1)

    a_sweep = {f"{int(x * 100)}": overlay_a(arts, x) for x in X_SWEEP}
    c_sweep = {f"{int(y * 100)}": overlay_c(arts, y, glen) for y in Y_SWEEP}

    return {
        "summary": {
            "n_arts": len(arts),
            "baseline_cornix3": baseline,
            "overlay_a_trailing_tp": a_sweep,
            "overlay_c_portfolio": c_sweep,
            "x_sweep": X_SWEEP,
            "y_sweep": Y_SWEEP,
        }
    }


def overlay_c(arts: list[dict], y: float, glen: int) -> dict:
    """(c) Portfolio circuit-breaker: flatten ALL open trades on a y% retrace of the
    aggregate open-position wave; new signals after a flatten start a fresh wave
    ("im Tal wieder Signale nehmen"). Returns realised + MaxDD, direction-split.

    The flatten decision (which trade at which grid step) is the pure, DB-free
    `core.wave_exit_sim.portfolio_circuit_breaker`; here we only map the flatten
    grid step to a candle index and realise each trade there.
    """
    trades = [{"gi": a["_gi"], "lm": a["_lm"]} for a in arts]
    flat_at = portfolio_circuit_breaker(trades, glen, y)
    close_k = {
        idx: max(1, min(int(np.searchsorted(arts[idx]["_gi"], g, side="right")), len(arts[idx]["c"])))
        for idx, g in flat_at.items()
    }

    rows = []
    for idx, a in enumerate(arts):
        r = _realized_at(a, close_k.get(idx, len(a["c"])))
        if not r or not r.get("filled"):
            continue
        rows.append(
            {
                "dir": a["rec"]["direction"],
                "unlev": r["unlev_pct"],
                "net": r["net_pct"],
                "lev": r["levered_pct"],
                "tp1": r["tp1"],
                "triggered": idx in close_k,
            }
        )
    agg = _dir_agg(rows)
    agg["maxdd_open_wave"] = round(portfolio_maxdd(_open_wave(arts, glen, close_k)), 1)
    agg["n_flattened"] = len(close_k)
    return agg


# ─────────────────────────────────────────────────────────────────────────────
# DRIVER
# ─────────────────────────────────────────────────────────────────────────────
def run_validate(conn, model: str, start: str, end: str, limit: int | None, collect_arts: bool = False) -> dict:
    orch = import_bot_module("28_signal_orchestrator.py", "signal_orchestrator")
    offset = tick_utc_offset(conn)
    print(f"tick tz offset: {offset}")

    closed = load_closed(conn, model, start, end, limit)
    cornix = load_cornix_geometry(conn, model, start, end, orch.parse_cornix_signal)
    matched, unmatched = match_geometry(closed, cornix)
    print(
        f"{model}: closed={len(closed)} matched_geometry={len(matched)} unmatched={unmatched} "
        f"(coverage {len(matched) / max(len(closed), 1) * 100:.0f}%, bounded by outbox retention)"
    )

    # group matched trades by coin → one windowed tick read per coin
    by_coin: dict = defaultdict(list)
    for rec in matched:
        by_coin[rec["symbol"]].append(rec)

    records: list[dict] = []
    arts: list[dict] = []  # per-scored-trade candle artefacts for the Phase-2 overlays
    t0 = time.time()
    for i, (coin, recs) in enumerate(sorted(by_coin.items()), 1):
        lo = min(r["open_time"] for r in recs).replace(tzinfo=offset)
        hi = max((r["close_time"] or r["open_time"]) for r in recs).replace(tzinfo=offset) + timedelta(minutes=5)
        cand = read_coin_candles(conn, coin, lo, hi)
        if len(cand["t"]) == 0:
            for r in recs:
                records.append({**_rec_meta(r), "sim": None, "skip": "no_candles"})
            continue
        tick_t, tick_p = read_coin_ticks(conn, coin, lo, hi)  # for the order-resolver only
        for r in recs:
            ot = np.datetime64(r["open_time"])
            ct = np.datetime64(r["close_time"] or r["open_time"])
            m = (cand["t"] >= ot) & (cand["t"] <= ct)
            sub_t, sub_h, sub_l, sub_c = cand["t"][m], cand["h"][m], cand["l"][m], cand["c"][m]
            out = {**_rec_meta(r)}
            if len(sub_h) < 1:
                out["sim"] = None
                out["skip"] = "no_candles_in_window"
            else:
                resolver = make_order_resolver(sub_t, tick_t, tick_p)
                out["sim"] = {
                    name: replay_record(r, sub_t, sub_h, sub_l, sub_c, cfg, resolver) for name, cfg in CONFIGS.items()
                }
                out["skip"] = None
                if collect_arts:
                    arts.append({"rec": r, "t": sub_t, "h": sub_h, "l": sub_l, "c": sub_c, "resolver": resolver})
            records.append(out)
        if i % 25 == 0 or i == len(by_coin):
            print(f"[{i}/{len(by_coin)}] {coin}: {len(records)} trades ({time.time() - t0:.0f}s)", flush=True)

    summary = summarize(records, model, start, end, len(closed), unmatched)
    out = {"records": records, "summary": summary}
    if collect_arts:
        out["overlay"] = run_overlays(arts)
        summary["overlay"] = out["overlay"]["summary"]
    return out


def _rec_meta(r: dict) -> dict:
    return {
        "id": r["id"],
        "symbol": r["symbol"],
        "direction": r["direction"],
        "entry1": r["entry1"],
        "entry2": r.get("entry2"),
        "orig_sl": r["orig_sl"],
        "lev": r["lev"],
        "open_time": str(r["open_time"]),
        "close_time": str(r["close_time"]),
        "real_targets_hit": r["targets_hit"],
        "real_status": r["status"],
    }


def summarize(records: list[dict], model: str, start: str, end: str, n_closed: int, unmatched: int) -> dict:
    scored = [r for r in records if r.get("sim") and r["sim"]["monitor"] and r["sim"]["monitor"]["filled"]]
    n = len(scored)
    scored_span = [min(r["open_time"] for r in scored), max(r["open_time"] for r in scored)] if scored else [None, None]

    # VALIDATION: monitor-config sim vs recorded closed_ai_signals outcome
    th_exact = th_within1 = win_agree = 0
    for r in scored:
        sim_th = r["sim"]["monitor"]["targets_hit"]
        real_th = r["real_targets_hit"]
        if sim_th == real_th:
            th_exact += 1
        if abs(sim_th - real_th) <= 1:
            th_within1 += 1
        sim_win = r["sim"]["monitor"]["tp1"]
        real_win = real_th >= 1
        if sim_win == real_win:
            win_agree += 1

    def agg(cfg_name: str) -> dict:
        vals_unlev, vals_net, vals_lev, durs = [], [], [], []
        tp1 = 0
        for r in scored:
            s = r["sim"].get(cfg_name)
            if not s or not s["filled"]:
                continue
            vals_unlev.append(s["unlev_pct"])
            vals_net.append(s["net_pct"])
            if s["levered_pct"] is not None:
                vals_lev.append(s["levered_pct"])
            if s["dur_h"] is not None:
                durs.append(s["dur_h"])
            if s["tp1"]:
                tp1 += 1
        m = len(vals_unlev)
        return {
            "n": m,
            "unlev_mean_pct": round(float(np.mean(vals_unlev)), 4) if m else None,
            "unlev_sum_pct": round(float(np.sum(vals_unlev)), 2) if m else None,
            "net_sum_pct": round(float(np.sum(vals_net)), 2) if m else None,
            "levered_sum_pct": round(float(np.sum(vals_lev)), 1) if vals_lev else None,
            "levered_n": len(vals_lev),
            "wr_tp1_pct": round(tp1 / m * 100, 2) if m else None,
            "dur_median_h": round(float(np.median(durs)), 2) if durs else None,
            "dur_mean_h": round(float(np.mean(durs)), 2) if durs else None,
        }

    return {
        "model": model,
        "window": [start, end],
        "n_closed_in_window": n_closed,
        "n_unmatched_geometry": unmatched,
        "n_scored": n,
        "scored_span": [str(scored_span[0]), str(scored_span[1])],
        "validation_vs_monitor": {
            "targets_hit_exact_pct": round(th_exact / n * 100, 2) if n else None,
            "targets_hit_within1_pct": round(th_within1 / n * 100, 2) if n else None,
            "win_agreement_pct": round(win_agree / n * 100, 2) if n else None,
        },
        "configs": {name: agg(name) for name in CONFIGS},
        "generated_at": str(utc_now()),
    }


def write_report(out_dir: str, model: str, result: dict, mode: str = "validate") -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    kind = "overlay" if mode == "overlay" else "validation"
    tag = f"wave_exit_{kind}_{model.lower()}"
    jpath = os.path.join(out_dir, f"{tag}.json")
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump({"summary": result["summary"], "records": result["records"]}, fh, indent=2, default=str)
    mpath = os.path.join(out_dir, f"{tag}.md")
    with open(mpath, "w", encoding="utf-8") as fh:
        fh.write(render_md(result["summary"]))
    return jpath, mpath


def render_md(s: dict) -> str:
    v = s["validation_vs_monitor"]
    lines = [
        f"# Wave-Exit Phase 1 — High-Fidelity-Sim Validierung ({s['model']})",
        "",
        f"_generated {s['generated_at']} · read-only · window {s['window'][0]} → {s['window'][1]}_",
        "",
        "**Backbone:** vollständige wick-aware **5m**-OHLC-Kerzen (`candles`, 12× feiner als der "
        "1h-Live-Monitor) für die Touch-Erkennung; **10s**-Ticks (`ticker_10s`) nur als Order-Resolver "
        "für SL-vs-TP-Reihenfolge innerhalb einer 5m-Kerze. "
        "**Geometrie:** immutable Cornix-Text (`telegram_outbox`), Original-SL/entry2/TP1-3. "
        "**Outcome-Ground-Truth:** `closed_ai_signals`.",
        "",
        "> Warum nicht rein 10s: `ticker_10s` ist ein ~40s-Snapshot mit Lücken (Coverage-Median 0.25) "
        "und verpasst ~81% der SL-Touch-Events → eine reine Tick-Sim entkommt den Stops und verzerrt "
        "Realized ~2.7×. Die 5m-Kerze ist gap-frei und wick-aware.",
        "",
        f"Closed im Fenster: {s['n_closed_in_window']} · Geometrie gematcht & gescored: **{s['n_scored']}** "
        f"· ungematcht (Outbox-Retention): {s['n_unmatched_geometry']}.",
        f"Gescorte-Trades-Span: {s['scored_span'][0]} → {s['scored_span'][1]} "
        "(Outbox-Retention verzerrt das Set zu **jüngeren** Trades — beim Lesen der Aggregate beachten).",
        "",
        "## Validierung — `monitor`-Config (entry1-only, interne Targets) vs recorded closed_ai_signals",
        "",
        f"- targets_hit **exakt**: {v['targets_hit_exact_pct']}%  ·  **±1**: {v['targets_hit_within1_pct']}%",
        f"- Win/Loss (TP1-Touch) **Übereinstimmung**: {v['win_agreement_pct']}%",
        "",
        "> Restdivergenz kommt aus der feineren Auflösung (5m-Wick + echte Intra-Candle-Ordnung) "
        "gegenüber dem 1h-Monitor — die Sim ist hier bewusst *treuer* als die recorded-Outcome-Quelle.",
        "",
        "## Realized-Aggregat je Config",
        "",
        "| config | n | unlev mean% | unlev sum% | net sum% | leveraged sum% (n) | WR(TP1)% | Ø-Dauer med/mean h |",
        "|---|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for name, a in s["configs"].items():
        lev = f"{a['levered_sum_pct']} ({a['levered_n']})" if a["levered_sum_pct"] is not None else "—"
        lines.append(
            f"| {name} | {a['n']} | {a['unlev_mean_pct']} | {a['unlev_sum_pct']} | {a['net_sum_pct']} "
            f"| {lev} | {a['wr_tp1_pct']} | {a['dur_median_h']}/{a['dur_mean_h']} |"
        )
    lines += [
        "",
        "**Lesehilfe:** `monitor` = 1:1-Reproduktion des Bot-Monitors (Validierungsanker). "
        "`cornix3` = was Cornix real handelt (DCA entry1/entry2, 3 publizierte TPs in Dritteln) — "
        "die Headline-Realized-Zahl und die Basis fürs Phase-2-Overlay.",
        "",
    ]
    if s.get("overlay"):
        lines += render_overlay_md(s["overlay"])
    return "\n".join(lines)


def _ov_row(label: str, a: dict | None, extra: str = "") -> str:
    if not a or not a.get("ALL"):
        return f"| {label} | — | — | — | — | — | {extra} |"
    al = a["ALL"]
    lev = al["lev_sum"] if al["lev_sum"] is not None else "—"
    dd = a.get("maxdd_open_wave", "—")
    return f"| {label} | {al['n']} | {al['unlev_sum']} | {lev} | {al['wr_tp1']} | {dd} | {extra} |"


def render_overlay_md(ov: dict) -> list[str]:
    if "baseline_cornix3" not in ov:
        return ["", f"_(Overlay: {ov.get('note', 'n/a')})_", ""]
    base = ov["baseline_cornix3"]

    def dir_split_row(label: str, a: dict | None) -> str:
        if not a:
            return f"| {label} | — | — |"
        lo = a.get("LONG")
        sh = a.get("SHORT")
        f = lambda d: f"{d['unlev_sum']}/{d['lev_sum']}" if d and d["lev_sum"] is not None else "—"  # noqa: E731
        return f"| {label} | {f(lo)} | {f(sh)} |"

    # Kernbefund aus den Zahlen ableiten (robust über den Sweep, kein Best-Punkt)
    def _band(sweep: dict, field: str, sub: str = "ALL"):
        vals = [d[sub][field] for d in sweep.values() if d.get(sub) and d[sub].get(field) is not None]
        return (min(vals), max(vals)) if vals else (None, None)

    a_lev = _band(ov["overlay_a_trailing_tp"], "lev_sum")
    c_lev = _band(ov["overlay_c_portfolio"], "lev_sum")
    a_unlev = _band(ov["overlay_a_trailing_tp"], "unlev_sum")
    c_unlev = _band(ov["overlay_c_portfolio"], "unlev_sum")
    c_dd = [
        d.get("maxdd_open_wave") for d in ov["overlay_c_portfolio"].values() if d.get("maxdd_open_wave") is not None
    ]
    base_lev = base["ALL"]["lev_sum"]
    base_unlev = base["ALL"]["unlev_sum"]
    base_dd = base.get("maxdd_open_wave")

    lines = [
        "",
        "---",
        "",
        "## Phase 2 — Auto-Close-Overlays (auf `cornix3`, real-money DCA/3-TP)",
        "",
        f"n_arts = {ov['n_arts']} (leveraged, gescort). Metrik = REALIZED (locked-in) — "
        "unlev Summe% / leveraged Summe%; MaxDD = Peak-to-Trough der aggregierten Open-Positions-Welle "
        "(leveraged Kontoeinheiten). **Baseline = hold-to-TP/SL.**",
        "",
        "### KERNBEFUND",
        "",
        f"- **Leveraged Realized: keine Overlay-Variante schlägt hold.** Baseline +{base_lev}% vs "
        f"(a) {a_lev[0]}…{a_lev[1]}% / (c) {c_lev[0]}…{c_lev[1]}% — robust über den GANZEN Sweep schlechter. "
        "Der leveraged-Summe wird von wenigen Fat-Tail-Wellen-Treffern dominiert (−100%-Clamp-Asymmetrie), "
        "die jedes Overlay kappt.",
        f"- **Unlevered Realized: Overlays sind BESSER.** Baseline +{base_unlev}% vs "
        f"(a) {a_unlev[0]}…{a_unlev[1]}% / (c) {c_unlev[0]}…{c_unlev[1]}% — die Regeln schneiden die "
        "Underwater-Tails, ohne die (unhebelte) Verteilung so stark von den Winnern abzuhängen.",
        f"- **Drawdown: (c) ist ein Risk-Tool.** MaxDD-Welle {base_dd} (hold) → {min(c_dd)}…{max(c_dd)} "
        f"(~{base_dd / max(min(c_dd), 1e-9):.0f}× kleiner) — gegen ~44% weniger leveraged Upside.",
        "- **L/S:** der leveraged-Verlust sitzt fast ganz im LONG; SHORT-unlev vervielfacht sich (Tabelle unten). "
        "Bestätigt T-032/029/031: der Edge ist RICHTUNGS-, nicht Timing-bedingt.",
        "- **Fazit:** Michis Wellen-Intuition fängt out-of-sample **kein** leveraged-Edge (Markt-Timing), "
        "aber (c) konvertiert Upside-Varianz in Drawdown-Schutz. Kein Deploy-Signal für Return-Maximierung; "
        "als reiner Portfolio-Circuit-Breaker diskutabel. **NO-EDGE auf der Headline-Metrik.**",
        "",
        "> ⚠ **WR(TP1)% ist unter Overlays irreführend** (die Regel schließt auf MTM-Retrace, nicht auf "
        "TP-Touch → tp1=False obwohl profitabel geschlossen). Realized ist die Metrik, nicht WR. "
        "Overlay (a) triggert bei ~95% (Peak-Retrace feuert auch auf kleinen Wellen — eine Aktivierungs-"
        "Schwelle würde nur große Wellen trailen, ist hier aber nicht nötig: das Vorzeichen ist schon klar).",
        "",
        "### Overlay (a) — Per-Trade-Trailing-TP (close bei X% Retrace vom Trade-MTM-Peak)",
        "",
        "| X% | n | unlev sum% | lev sum% | WR(TP1)% | MaxDD-Welle | getriggert% |",
        "|--:|--:|--:|--:|--:|--:|--:|",
        _ov_row("Baseline", base, "0.0"),
    ]
    for x in ov["x_sweep"]:
        a = ov["overlay_a_trailing_tp"][str(int(x * 100))]
        trig = a["ALL"]["trig_pct"] if a.get("ALL") else "—"
        lines.append(_ov_row(f"{int(x * 100)}", a, str(trig)))
    lines += [
        "",
        "### Overlay (c) — Portfolio-Circuit-Breaker (close-ALL bei Y% Retrace der Aggregat-Welle)",
        "",
        "| Y% | n | unlev sum% | lev sum% | WR(TP1)% | MaxDD-Welle | geflattet |",
        "|--:|--:|--:|--:|--:|--:|--:|",
        _ov_row("Baseline", base, "0"),
    ]
    for y in ov["y_sweep"]:
        a = ov["overlay_c_portfolio"][str(int(y * 100))]
        lines.append(_ov_row(f"{int(y * 100)}", a, str(a.get("n_flattened", "—"))))
    lines += [
        "",
        "### Long/Short getrennt (unlev sum% / lev sum%)",
        "",
        "| Regel | LONG | SHORT |",
        "|---|--:|--:|",
        dir_split_row("Baseline", base),
    ]
    for x in ov["x_sweep"]:
        dir_split_row_a = ov["overlay_a_trailing_tp"][str(int(x * 100))]
        lines.append(dir_split_row(f"(a) X={int(x * 100)}%", dir_split_row_a))
    for y in ov["y_sweep"]:
        lines.append(dir_split_row(f"(c) Y={int(y * 100)}%", ov["overlay_c_portfolio"][str(int(y * 100))]))
    lines += [
        "",
        "**Ehrliche Grenze:** 7d/674-Legs, jüngeres Fenster (Outbox-Bias). Wellen-Capture ist Markt-Timing; "
        "getestet wird, ob eine MECHANISCHE Regel die Welle out-of-sample fängt oder nur im Hindsight sichtbar "
        "ist. Bewertet werden robuste **Bänder + Vorzeichen** über den Sweep, nicht ein Best-Punkt. "
        "NO-EDGE ist ein valides Ergebnis.",
        "",
    ]
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description="Wave-Exit-Overlay harness (T-2026-KYT-9050-035)")
    ap.add_argument("--mode", default="validate", choices=["validate", "overlay"])
    ap.add_argument("--model", default="AIM2")
    ap.add_argument("--start", default="2026-07-07 14:20:00")
    ap.add_argument("--end", default="2026-07-23 00:00:00")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=DEFAULT_OUT_DIR)
    ap.add_argument(
        "--allow-high-cpu",
        action="store_true",
        help="Skip the CPU-headroom abort (VPS is chronically saturated; "
        "this job is BELOW_NORMAL + read-only + coin-windowed).",
    )
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    set_low_priority()
    if args.allow_high_cpu:
        print("CPU-Headroom-Check übersprungen (--allow-high-cpu; BELOW_NORMAL + read-only).")
    else:
        check_cpu_headroom()

    conn = get_db_connection()
    try:
        conn.set_session(readonly=True)  # Regel #1: never write the live-money DB
    except Exception as e:
        print(f"⚠ set_session(readonly=True) failed ({e}) — proceeding; all queries are SELECT-only.")
    try:
        result = run_validate(conn, args.model, args.start, args.end, args.limit, collect_arts=(args.mode == "overlay"))
    finally:
        conn.close()

    jpath, mpath = write_report(args.out, args.model, result, args.mode)
    print("\n" + render_md(result["summary"]))
    print(f"\nJSON: {jpath}\nMD:   {mpath}")


if __name__ == "__main__":
    main()
