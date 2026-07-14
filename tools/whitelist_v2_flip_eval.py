"""
tools/whitelist_v2_flip_eval.py — v1-vs-v2 whitelist flip evaluation (T-2026-CU-9050-069).

Purpose
-------
Since the T-068 deploy (2026-07-11) the analyzer (bot 27) writes the shadow
columns `whitelisted_v2`/`reason_v2` (net-expectancy lower bound with
hierarchical EB shrinkage, T-2026-CU-9050-048) next to the live v1 gate.
This tool produces the decision basis for the v1->v2 flip (Michi's call):

  1. Divergence matrix v1 vs v2 over the current `bot_regime_whitelist`
     snapshot: which cells decide differently, in which direction.
  2. Counterfactual PnL of the real gate traffic since the deploy, scored
     with the T-047 machinery (`tools/rom1_counterfactual.score_row` ->
     `compute_rom1_trade_params` + `simulate_exit`), bucketed by flip class.
  3. Volume effect: v1 vs v2 gate rate on the same traffic, ROM1 trades/day
     projection.
  4. A summary JSON + console report. The recommendation text and the flip
     itself are NOT produced here (VPS session + operator decision, Stop-B
     is a valid outcome).

Method & caveats (docs/WHITELIST_V2_FLIP_EVAL.md has the full list)
-------------------------------------------------------------------
  * SNAPSHOT APPROXIMATION: the per-event v2 verdict comes from TODAY's
    whitelist snapshot, not from the (unlogged) state at signal time. The
    v1 drift metric — recorded v1 decision per event vs today's v1 cell —
    quantifies that approximation; read v2 numbers as trend if drift is high.
  * Fallback-path traffic (`no_whitelist_entry`, `whitelist_stale:*`,
    `*fallback*`, NULL wl_reason) is UNAFFECTED by the flip (bot 28 only
    swaps the 4D-cell read) and is counted, never scored.
  * Counterfactual scoring on BOTH sides (also for actually-forwarded
    trades): one yardstick, no monitor-label dependency (report 17).
  * `open_at_horizon` trades count mark-to-market into PnL sums, not into
    the win rate (047 semantics).

Operating rules (live VPS!)
---------------------------
DB strictly read-only (SELECTs only), BELOW_NORMAL priority, CPU headroom
check — identical to walkforward_sim. No table is written.

Examples
--------
  python tools/whitelist_v2_flip_eval.py --skip-replay
  python tools/whitelist_v2_flip_eval.py --since 2026-07-11T00:00:00 --horizon-hours 72
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.bot_naming import pretty_name  # noqa: E402
from core.database import get_db_connection  # noqa: E402
from core.time import utc_now  # noqa: E402
from tools.rom1_counterfactual import (  # noqa: E402
    DEFAULT_OUT_DIR,
    aggregate,
    load_1h,
    score_row,
)
from tools.walkforward_sim import (  # noqa: E402
    check_cpu_headroom,
    import_bot_module,
    set_low_priority,
)

# T-068 fleet restart brought the 048 shadow columns live on 2026-07-11.
DEFAULT_SINCE = "2026-07-11T00:00:00"
# Short shadow window -> short horizon (168h would leave almost everything
# open_at_horizon). Mark-to-market still counts into the PnL sums.
DEFAULT_HORIZON_HOURS = 72

# The flip swaps ONLY the 4D-cell read in bot 28 (whitelisted -> whitelisted_v2).
# Every other gate path is identical under v1 and v2:
CELL_OPEN_REASONS = {"wr_above_overall", "counter_trend_specialist", "insufficient_data"}
CELL_BLOCK_REASONS = {"wr_below_overall", "counter_trend_insufficient"}

# Divergence classes (cells and events share the vocabulary).
BOTH_OPEN = "both_open"
BOTH_BLOCK = "both_block"
V2_WOULD_BLOCK = "v2_would_block"  # v1 open / v2 block -> v2 takes traffic away
V2_WOULD_OPEN = "v2_would_open"  # v1 block / v2 open -> v2 adds traffic
V2_MISSING = "v2_missing"  # shadow column NULL (pre-048 row)


# ─────────────────────────────────────────────────────────────────────────────
# PURE HELPERS (DB-free, unit-tested in backtest/test_whitelist_v2_flip_eval.py)
# ─────────────────────────────────────────────────────────────────────────────
def parse_v2_reason(reason_v2: str | None) -> dict:
    """Parses `v2_pass:lb=0.123:est=0.456:src=cell:neff=32` into a dict.

    Tolerant: unknown/missing parts yield None fields instead of raising —
    the reason string is a diagnostic channel, not a contract.
    """
    out: dict = {"verdict": None, "lb": None, "est": None, "src": None, "neff": None}
    if not reason_v2:
        return out
    parts = reason_v2.split(":")
    head = parts[0]
    if head.startswith("v2_"):
        out["verdict"] = head[3:] or None
    for p in parts[1:]:
        key, _, val = p.partition("=")
        if key in ("lb", "est", "neff"):
            try:
                out[key] = float(val)
            except (TypeError, ValueError):
                pass
        elif key == "src" and val:
            out["src"] = val
    return out


def cell_divergence_class(whitelisted: bool, whitelisted_v2: bool | None) -> str:
    """Maps one snapshot cell's (v1, v2) pair to its divergence class."""
    if whitelisted_v2 is None:
        return V2_MISSING
    if whitelisted and whitelisted_v2:
        return BOTH_OPEN
    if not whitelisted and not whitelisted_v2:
        return BOTH_BLOCK
    if whitelisted and not whitelisted_v2:
        return V2_WOULD_BLOCK
    return V2_WOULD_OPEN


def divergence_matrix(snapshot: dict[tuple, dict]) -> dict:
    """Aggregates the snapshot into the divergence matrix (T-069 question 1).

    Returns totals per class, breakdowns per regime/direction/bot, and the
    lower-bound distribution per class (from reason_v2). Every cell lands in
    exactly one class; the class counts sum to len(snapshot).
    """
    totals: dict[str, int] = defaultdict(int)
    by_regime: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_direction: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_bot: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    lb_by_class: dict[str, list[float]] = defaultdict(list)

    for (bot, regime, _alt, direction), cell in snapshot.items():
        cls = cell_divergence_class(cell["v1"], cell["v2"])
        totals[cls] += 1
        by_regime[regime][cls] += 1
        by_direction[direction][cls] += 1
        by_bot[bot][cls] += 1
        lb = parse_v2_reason(cell.get("reason_v2"))["lb"]
        if lb is not None:
            lb_by_class[cls].append(lb)

    def _lb_stats(values: list[float]) -> dict | None:
        if not values:
            return None
        s = sorted(values)
        return {
            "n": len(s),
            "min": round(s[0], 3),
            "median": round(s[len(s) // 2], 3),
            "max": round(s[-1], 3),
        }

    return {
        "n_cells": len(snapshot),
        "totals": dict(totals),
        "by_regime": {k: dict(v) for k, v in by_regime.items()},
        "by_direction": {k: dict(v) for k, v in by_direction.items()},
        "by_bot": {k: dict(v) for k, v in by_bot.items()},
        # one entry per seen class — None (not a missing key) when no lb exists
        "lb_stats": {k: _lb_stats(lb_by_class.get(k, [])) for k in totals},
    }


def _cell_path(side: str, v1_path: str | None) -> tuple[bool, str]:
    """(is_cell_decided, normalized_path) for one event's recorded v1 path.

    Forwarded events carry `wl_reason`, suppressed events carry the suffix
    after `bot_not_whitelisted:`. Only cell-decided paths change under the
    flip; fallback/stale/no-entry/NULL traffic behaves identically under v2.
    """
    path = (v1_path or "").strip()
    if side == "forwarded":
        return path in CELL_OPEN_REASONS, path or "wl_reason_missing"
    return path in CELL_BLOCK_REASONS, path or "reason_missing"


def classify_flip_effect(event: dict, snapshot: dict[tuple, dict]) -> dict:
    """Classifies one gate event against the v2 snapshot (T-069 question 2/3).

    `event` needs: side ('forwarded'|'suppressed'), bot_name, regime,
    alt_context, direction, v1_path. Returns::

        {affected, flip_class, bucket, v2_verdict, v2_lb,
         v1_snapshot_agree, skip_reason}

    Never raises on missing data — `cell_missing`/`v2_missing` are counted
    classes, not silent drops (AK3).
    """
    side = event["side"]
    cell_decided, path = _cell_path(side, event.get("v1_path"))
    base = {
        "affected": False,
        "flip_class": None,
        "bucket": None,
        "v2_verdict": None,
        "v2_lb": None,
        "v1_snapshot_agree": None,
        "skip_reason": None,
    }
    if not cell_decided:
        base["flip_class"] = "unaffected"
        base["bucket"] = f"unaffected:{path}"
        return base

    key = (
        pretty_name(event["bot_name"] or ""),
        event.get("regime"),
        event.get("alt_context"),
        event["direction"],
    )
    cell = snapshot.get(key)
    if cell is None:
        base["flip_class"] = "cell_missing"
        base["bucket"] = f"cell_missing:{path}"
        base["skip_reason"] = "cell_missing"
        return base

    v1_now = bool(cell["v1"])
    recorded_open = side == "forwarded"
    base["v1_snapshot_agree"] = v1_now == recorded_open

    v2 = cell["v2"]
    if v2 is None:
        base["flip_class"] = V2_MISSING
        base["bucket"] = f"{V2_MISSING}:{path}"
        base["skip_reason"] = "v2_missing"
        return base

    if recorded_open:
        flip_class = BOTH_OPEN if v2 else V2_WOULD_BLOCK
    else:
        flip_class = V2_WOULD_OPEN if v2 else BOTH_BLOCK
    base.update(
        {
            "affected": True,
            "flip_class": flip_class,
            "bucket": f"{flip_class}:{path}",
            "v2_verdict": bool(v2),
            "v2_lb": parse_v2_reason(cell.get("reason_v2"))["lb"],
        }
    )
    return base


def drift_rate(events: list[dict]) -> dict:
    """v1 drift: recorded v1 decision vs today's v1 snapshot cell (AK5).

    Only cell-decided events with a resolvable cell carry a boolean
    `v1_snapshot_agree`; everything else is excluded from the denominator.
    """
    flags = [e["v1_snapshot_agree"] for e in events if e.get("v1_snapshot_agree") is not None]
    n = len(flags)
    agree = sum(1 for f in flags if f)
    return {
        "n_comparable": n,
        "n_agree": agree,
        "agree_pct": round(agree / n * 100, 2) if n else None,
    }


def volume_effect(events: list[dict], window_days: float) -> dict:
    """Gate rates v1 vs v2 on cell-decided traffic + trades/day projection (AK6).

    v1 open  = both_open + v2_would_block   (what v1 actually forwarded)
    v2 open  = both_open + v2_would_open    (what v2 would forward)
    Unaffected traffic forwards identically under both gates and is reported
    separately (it dampens the relative volume change fleet-wide).
    """
    counts: dict[str, int] = defaultdict(int)
    for e in events:
        counts[e["flip_class"] or "unclassified"] += 1
    n_cell = counts[BOTH_OPEN] + counts[BOTH_BLOCK] + counts[V2_WOULD_BLOCK] + counts[V2_WOULD_OPEN]
    v1_open = counts[BOTH_OPEN] + counts[V2_WOULD_BLOCK]
    v2_open = counts[BOTH_OPEN] + counts[V2_WOULD_OPEN]
    n_unaffected_fwd = sum(1 for e in events if e["flip_class"] == "unaffected" and e["side"] == "forwarded")
    days = max(window_days, 1e-9)
    return {
        "n_events_total": len(events),
        "n_cell_decided": n_cell,
        "counts": dict(counts),
        "v1_open_rate_pct": round(v1_open / n_cell * 100, 2) if n_cell else None,
        "v2_open_rate_pct": round(v2_open / n_cell * 100, 2) if n_cell else None,
        "forwarded_per_day_v1": round((v1_open + n_unaffected_fwd) / days, 2),
        "forwarded_per_day_v2_projected": round((v2_open + n_unaffected_fwd) / days, 2),
    }


def daily_counts(events: list[dict]) -> dict[str, dict[str, int]]:
    """Events per UTC day and side — makes ingestion/outage gaps visible (AK8)."""
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"forwarded": 0, "suppressed": 0})
    for e in events:
        day = str(e["ts"])[:10]
        out[day][e["side"]] += 1
    return {d: dict(v) for d, v in sorted(out.items())}


# ─────────────────────────────────────────────────────────────────────────────
# DB (read-only)
# ─────────────────────────────────────────────────────────────────────────────
def load_whitelist_snapshot(conn) -> dict[tuple, dict]:
    """Current bot_regime_whitelist keyed by (bot, regime, alt, direction)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bot_name, regime, alt_context, direction,
                   whitelisted, reason, whitelisted_v2, reason_v2, computed_at
            FROM bot_regime_whitelist
            """
        )
        rows = cur.fetchall()
    snap: dict[tuple, dict] = {}
    for r in rows:
        snap[(r[0], r[1], r[2], r[3])] = {
            "v1": bool(r[4]),
            "reason": r[5],
            "v2": r[6] if r[6] is None else bool(r[6]),
            "reason_v2": r[7],
            "computed_at": r[8],
        }
    return snap


def snapshot_prereqs(conn) -> dict:
    """Freshness + v2 coverage checks the report leads with (AK8).

    `computed_at` is naive UTC (bot 27 writes utc_now naive), so the age is
    computed against a naive-UTC now — comparing against NOW() in SQL would be
    session-local (UTC_POLICY R3).
    """
    now_naive = utc_now().replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(computed_at), COUNT(*), COUNT(whitelisted_v2)
            FROM bot_regime_whitelist
            """
        )
        max_computed, n_rows, n_v2 = cur.fetchone()
    age_h = None if max_computed is None else (now_naive - max_computed).total_seconds() / 3600.0
    return {
        "max_computed_at": str(max_computed),
        "snapshot_age_hours": None if age_h is None else round(age_h, 2),
        "analyzer_alive": age_h is not None and age_h <= 3.0,  # hourly cadence + slack
        "n_rows": n_rows,
        "v2_coverage_pct": round(n_v2 / n_rows * 100, 2) if n_rows else None,
    }


def load_gate_events(conn, since: datetime, limit: int | None) -> list[dict]:
    """Forwarded + whitelist-suppressed signal events since `since`.

    Suppressed side: only the `bot_not_whitelisted:*` family — the other
    suppression families (dedupe/cooldown/plumbing) are untouched by the flip
    and already measured by T-047. alt_context is reconstructed from
    regime_history at ts (the analyzer's own attribution pattern, P2.22 skew
    documented). Forwarded side carries alt_context_at_open natively.
    """
    events: list[dict] = []

    sql_fwd = """
        SELECT id, opened_at, bot_name, coin, direction,
               regime_at_open, alt_context_at_open, wl_reason,
               original_outbox_id, entry_price
        FROM orchestrator_open_trades
        WHERE opened_at >= %s
        ORDER BY opened_at ASC
    """
    sql_sup = """
        SELECT s.id, s.ts, s.bot_name, s.coin, s.direction,
               s.regime_at_signal, s.reason, s.original_outbox_id,
               (
                   SELECT rh.alt_context FROM regime_history rh
                   WHERE rh.ts <= s.ts
                   ORDER BY rh.ts DESC LIMIT 1
               ) AS alt_context
        FROM orchestrator_suppressed_signals s
        WHERE s.ts >= %s AND s.coin IS NOT NULL AND s.direction IS NOT NULL
          AND s.reason LIKE 'bot_not_whitelisted:%%'
        ORDER BY s.ts ASC
    """
    if limit:
        sql_fwd += " LIMIT %s"
        sql_sup += " LIMIT %s"
    fwd_params: tuple = (since, limit) if limit else (since,)

    with conn.cursor() as cur:
        cur.execute(sql_fwd, fwd_params)
        for r in cur.fetchall():
            events.append(
                {
                    "side": "forwarded",
                    "row_id": r[0],
                    "ts": r[1],
                    "bot_name": r[2],
                    "coin": r[3],
                    "direction": r[4],
                    "regime": r[5],
                    "regime_at_signal": r[5],
                    "alt_context": r[6],
                    "v1_path": r[7],
                    "reason": r[7],
                    "original_outbox_id": r[8],
                    "recorded_entry": float(r[9]) if r[9] is not None else None,
                }
            )
        cur.execute(sql_sup, fwd_params)
        for r in cur.fetchall():
            reason = r[6] or ""
            events.append(
                {
                    "side": "suppressed",
                    "row_id": r[0],
                    "ts": r[1],
                    "bot_name": r[2],
                    "coin": r[3],
                    "direction": r[4],
                    "regime": r[5],
                    "regime_at_signal": r[5],
                    "alt_context": r[8],
                    "v1_path": reason.partition(":")[2],
                    "reason": reason,
                    "original_outbox_id": r[7],
                    "recorded_entry": None,
                }
            )
    return events


# ─────────────────────────────────────────────────────────────────────────────
# SCORING (delegates to the T-047 machinery — one geometry source, AK4)
# ─────────────────────────────────────────────────────────────────────────────
def score_events(conn, orch, events: list[dict], horizon_hours: int) -> list[dict]:
    """First-touch counterfactual for every flip-affected event, coin-batched.

    Unaffected/missing events pass through unscored (scored=False + their
    classification), so the JSONL keeps the full population (AK3/AK8).
    """
    to_score = [e for e in events if e.get("affected")]
    passthrough = [e for e in events if not e.get("affected")]

    by_coin: dict[str, list[dict]] = defaultdict(list)
    for e in to_score:
        by_coin[e["coin"]].append(e)

    records: list[dict] = []
    t0 = time.time()
    for i, (coin, coin_rows) in enumerate(sorted(by_coin.items()), 1):
        df = load_1h(conn, coin, min(r["ts"] for r in coin_rows), horizon_hours)
        if df is None:
            records.extend(
                {**r, "bucket_class": "flip", "ts": str(r["ts"]), "scored": False, "skip_reason": "no_candles"}
                for r in coin_rows
            )
            continue
        for r in coin_rows:
            # score_row buckets by r["bucket"]/r["bucket_class"] — set to the
            # flip class so the 047 aggregation reports per flip bucket.
            row = {**r, "bucket_class": "flip"}
            try:
                records.append(score_row(orch, row, df, horizon_hours))
            except Exception as e:  # one broken row must not kill the run
                print(f"  !! {coin} row#{r['row_id']}: {e}")
                records.append({**row, "ts": str(row["ts"]), "scored": False, "skip_reason": "error"})
        if i % 25 == 0 or i == len(by_coin):
            print(f"[{i}/{len(by_coin)}] {coin}: {len(records)} rows ({time.time() - t0:.0f}s)", flush=True)

    for e in passthrough:
        records.append(
            {**e, "ts": str(e["ts"]), "scored": False, "skip_reason": e.get("skip_reason") or "not_flip_affected"}
        )
    return records


def portfolio_comparison(records: list[dict]) -> dict:
    """v1 selection vs v2 selection on identical traffic (report tail).

    v1 portfolio = both_open + v2_would_block (what v1 forwarded)
    v2 portfolio = both_open + v2_would_open  (what v2 would forward)
    Sums are counterfactual net PnL over the SCORED events of each class.
    """

    def _sum(classes: set[str]) -> dict:
        rows = [
            r
            for r in records
            if r.get("scored") and r.get("flip_class") in classes and r.get("net_pnl_pct") is not None
        ]
        return {
            "n_scored": len(rows),
            "sum_net_pnl_pct": round(sum(r["net_pnl_pct"] for r in rows), 2),
        }

    v1 = _sum({BOTH_OPEN, V2_WOULD_BLOCK})
    v2 = _sum({BOTH_OPEN, V2_WOULD_OPEN})
    removed = _sum({V2_WOULD_BLOCK})
    added = _sum({V2_WOULD_OPEN})
    return {
        "v1_selection": v1,
        "v2_selection": v2,
        "v2_removes": removed,  # positive sum here = v2 would give up money
        "v2_adds": added,  # positive sum here = v2 would unlock money
        "delta_sum_net_pnl_pct": round(v2["sum_net_pnl_pct"] - v1["sum_net_pnl_pct"], 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────────────────
def print_console_report(meta: dict) -> None:
    p = meta["prereqs"]
    print("\n── Prereqs ──")
    print(
        f"bot_regime_whitelist: {p['n_rows']} rows, v2 coverage {p['v2_coverage_pct']}%, "
        f"snapshot age {p['snapshot_age_hours']}h "
        f"({'analyzer alive' if p['analyzer_alive'] else '⚠️ ANALYZER STALE — Ergebnis nicht belastbar'})"
    )

    m = meta["divergence_matrix"]
    print("\n── Divergenz-Matrix (Zellen, aktueller Snapshot) ──")
    for cls in (BOTH_OPEN, BOTH_BLOCK, V2_WOULD_BLOCK, V2_WOULD_OPEN, V2_MISSING):
        n = m["totals"].get(cls, 0)
        lb = m["lb_stats"].get(cls)
        lb_s = f"  lb median {lb['median']}" if lb else ""
        print(f"  {cls:16} {n:6d}{lb_s}")

    print("\n── Traffic seit --since (per Tag) ──")
    for day, c in meta["daily_counts"].items():
        print(f"  {day}: forwarded {c['forwarded']:4d}  suppressed(gate) {c['suppressed']:4d}")

    d = meta["drift"]
    print(
        f"\n── v1-Drift (Snapshot-Näherung, AK5) ──\n"
        f"  {d['n_agree']}/{d['n_comparable']} Events stimmen mit dem heutigen v1-Snapshot überein "
        f"({d['agree_pct']}%)"
        + (
            "  ⚠️ >15% Drift — v2-Zahlen nur als Tendenz lesen"
            if d["agree_pct"] is not None and d["agree_pct"] < 85
            else ""
        )
    )

    v = meta["volume"]
    print(
        f"\n── Volumen-Effekt ──\n"
        f"  zell-entschieden: {v['n_cell_decided']}/{v['n_events_total']} Events\n"
        f"  Gate-Rate offen: v1 {v['v1_open_rate_pct']}%  →  v2 {v['v2_open_rate_pct']}%\n"
        f"  ROM1-Forwards/Tag: v1 {v['forwarded_per_day_v1']}  →  v2 (Prognose) {v['forwarded_per_day_v2_projected']}"
    )

    if meta.get("buckets"):
        hdr = f"{'bucket':40} {'n':>6} {'scored':>7} {'wr%':>7} {'avgPnL%':>9} {'sumPnL%':>10}"
        print("\n── Counterfactual pro Flip-Bucket (047-Replay) ──")
        print(hdr)
        print("-" * len(hdr))
        for s in meta["buckets"]:
            wr = f"{s['tp1_first_touch_wr']:.2f}" if s["tp1_first_touch_wr"] is not None else "—"
            avg = f"{s['avg_net_pnl_pct']:.3f}" if s["avg_net_pnl_pct"] is not None else "—"
            tot = f"{s['sum_net_pnl_pct']:.2f}" if s["sum_net_pnl_pct"] is not None else "—"
            print(f"{s['bucket'][:40]:40} {s['n_signals']:6d} {s['n_scored']:7d} {wr:>7} {avg:>9} {tot:>10}")

        pc = meta["portfolio"]
        print(
            f"\n── Portfolio-Vergleich (identischer Traffic) ──\n"
            f"  v1-Auswahl: {pc['v1_selection']['n_scored']} Trades, Σ {pc['v1_selection']['sum_net_pnl_pct']}%\n"
            f"  v2-Auswahl: {pc['v2_selection']['n_scored']} Trades, Σ {pc['v2_selection']['sum_net_pnl_pct']}%\n"
            f"  v2 nimmt weg: Σ {pc['v2_removes']['sum_net_pnl_pct']}% ({pc['v2_removes']['n_scored']} Trades)\n"
            f"  v2 schaltet frei: Σ {pc['v2_adds']['sum_net_pnl_pct']}% ({pc['v2_adds']['n_scored']} Trades)\n"
            f"  Δ (v2 − v1): {pc['delta_sum_net_pnl_pct']}%"
        )

    print(
        "\nLesehinweis: v2-Verdicts stammen aus dem HEUTIGEN Snapshot (Näherung, siehe Drift oben); "
        "open_at_horizon zählt mark-to-market in Σ, nicht in die WR. Empfehlung + Flip = Operator."
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Whitelist v1-vs-v2 flip evaluation (T-2026-CU-9050-069)")
    ap.add_argument("--since", default=DEFAULT_SINCE, help="ISO timestamp (naive UTC) — T-068 deploy time")
    ap.add_argument("--horizon-hours", type=int, default=DEFAULT_HORIZON_HOURS)
    ap.add_argument("--limit", type=int, default=None, help="first N rows per side (smoke runs)")
    ap.add_argument("--skip-replay", action="store_true", help="matrix + volume only, no counterfactual")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    since = datetime.fromisoformat(args.since)
    set_low_priority()
    check_cpu_headroom()

    conn = get_db_connection()
    try:
        conn.set_session(readonly=True)  # this tool NEVER writes the live DB
    except Exception:
        pass

    try:
        prereqs = snapshot_prereqs(conn)
        snapshot = load_whitelist_snapshot(conn)
        matrix = divergence_matrix(snapshot)

        events = load_gate_events(conn, since, args.limit)
        for e in events:
            e.update(classify_flip_effect(e, snapshot))
        print(f"{len(events)} Gate-Events seit {since} geladen und klassifiziert")

        window_days = max((utc_now().replace(tzinfo=None) - since).total_seconds() / 86400.0, 0.0)
        meta: dict = {
            "since": str(since),
            "horizon_hours": args.horizon_hours,
            "generated_at": str(utc_now()),
            "prereqs": prereqs,
            "divergence_matrix": matrix,
            "daily_counts": daily_counts(events),
            "drift": drift_rate(events),
            "volume": volume_effect(events, window_days),
        }

        if args.skip_replay:
            records = [{**e, "ts": str(e["ts"]), "scored": False, "skip_reason": "skip_replay"} for e in events]
        else:
            orch = import_bot_module("28_signal_orchestrator.py", "signal_orchestrator")
            records = score_events(conn, orch, events, args.horizon_hours)
            meta["buckets"] = aggregate([r for r in records if r.get("affected")])
            meta["portfolio"] = portfolio_comparison(records)
    finally:
        conn.close()

    os.makedirs(args.out, exist_ok=True)
    jsonl_path = os.path.join(args.out, "whitelist_v2_flip_eval.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, default=str) + "\n")
    with open(os.path.join(args.out, "whitelist_v2_flip_eval_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)

    print_console_report(meta)
    print(f"\nRecords: {jsonl_path}")


if __name__ == "__main__":
    main()
