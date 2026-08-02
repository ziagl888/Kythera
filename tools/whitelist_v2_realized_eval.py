"""tools/whitelist_v2_realized_eval.py — what the v1→v2 whitelist flip would
have done to REAL traffic, measured against REALIZED outcomes (T-2026-KYT-9050-007).

Why this exists next to `tools/whitelist_v2_flip_eval.py`
---------------------------------------------------------
The 069 tool answers the flip question with a *counterfactual replay* of the
ROM1 geometry (T-047 machinery): it re-simulates what a trade would have done.
That is the right yardstick when no real outcome exists — but for the traffic
the gate actually forwarded, and for the source signal behind every blocked
event, a REAL, monitored, closed trade exists in the DB. This tool uses those.

It answers exactly two questions, per Bot × Direction:

  Q1  How many real gate events would v2 decide differently than v1 —
      additionally BLOCK (`v2_would_block`) or additionally OPEN
      (`v2_would_open`)?
  Q2  What did precisely those signals REALIZE?

Two realized yardsticks, deliberately kept apart
------------------------------------------------
  * TRIGGER leg — the source bot's own tracked trade (`closed_ai_signals`
    model=<tag> / `closed_trades_master` strategy=<tag>). Exists on BOTH sides
    of the gate (a blocked signal was still posted to the bot's own channel and
    scored by the monitors), so it is the only SYMMETRIC realized measure.
    Geometry is the bot's own, not ROM1's (docs/REGIME_ORCHESTRATOR.md P1.10).
  * ROM1 leg — the trade the orchestrator actually opened for a forwarded
    event (`closed_ai_signals` model='ROM1'). This is the real money, but it
    exists ONLY on the forwarded side. `v2_would_open` events have no ROM1 leg
    and never can — that asymmetry is structural, not a data gap.

Measured environment facts this tool does not assume (it detects and reports)
----------------------------------------------------------------------------
  * TIME DOMAIN per tag. The naive legacy timestamp columns carry two domains
    (core/time.py: LEGACY_WRITER_TZ vs UTC). Measured on 2026-08-01 on the live
    DB: the orchestrator tables and ROM1 rows are UTC, while the bots' own rows
    in `closed_ai_signals` / `closed_trades_master` still carry
    Europe/Bucharest wall clock (+3h in summer). A twin join that ignores this
    matches 0.0% of events. The tool therefore tries BOTH readings per tag,
    picks the one with more matches, and reports both rates — so it keeps
    working after the R3 history decision (docs/UTC_POLICY.md §6) flips a
    writer, instead of silently returning an empty result.
  * SNAPSHOT APPROXIMATION. `bot_regime_whitelist` is UPSERT-only without
    history, so the v2 verdict per event comes from TODAY's snapshot, not from
    the state at signal time. The v1 drift metric (recorded gate path vs
    today's v1 cell) quantifies that error; it is reported, never hidden.

Operating rules (live VPS)
--------------------------
DB strictly read-only, BELOW_NORMAL priority, CPU headroom check. Nothing is
written to any live table. Artefacts go to KYTHERA_REPLAY_DIR.

Examples
--------
  python tools/whitelist_v2_realized_eval.py
  python tools/whitelist_v2_realized_eval.py --since 2026-07-11T00:00:00
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.bot_naming import pretty_name  # noqa: E402
from core.database import get_db_connection  # noqa: E402
from core.realized_pnl import realized_pnl_pct  # noqa: E402
from core.time import LEGACY_WRITER_TZ, utc_now  # noqa: E402

# One source for the realized math and the outcome vocabulary (T-032).
from tools.fleet_realized_audit import (  # noqa: E402
    LOSS,
    WIN,
    _classic_targets,
    _parse_hits,
    _parse_targets,
    aggregate_leg,
    classify_ai_outcome,
    classify_classic_outcome,
    unlev_move,
)
from tools.walkforward_sim import check_cpu_headroom, set_low_priority  # noqa: E402

# One source for the flip classification (T-069) — this tool only swaps the
# scoring layer (counterfactual replay → realized outcome), never the gate
# semantics. Re-deriving them here would create a second truth about which
# gate paths the flip touches.
from tools.whitelist_v2_flip_eval import (  # noqa: E402
    BOTH_BLOCK,
    BOTH_OPEN,
    V2_MISSING,
    V2_WOULD_BLOCK,
    V2_WOULD_OPEN,
    classify_flip_effect,
    daily_counts,
    divergence_matrix,
    drift_rate,
    load_gate_events,
    load_whitelist_snapshot,
    snapshot_prereqs,
    volume_effect,
)

# The T-068 fleet restart brought the 048 shadow columns live.
DEFAULT_SINCE = "2026-07-11T00:00:00"
# Trigger-leg twin tolerance: the orchestrator reads telegram_outbox every
# 500ms, the bot writes its own ai_signals row in the same breath — so the two
# timestamps differ by seconds. 10 minutes is slack for outbox backlog, not a
# fuzzy match; the matched-|Δ| percentiles in the summary make it auditable.
DEFAULT_TWIN_TOL_SEC = 600
# ROM1 leg: mirrors 28_signal_orchestrator.sync_closed_trades (±60s anchor),
# doubled for the same backlog slack.
DEFAULT_ROM1_TOL_SEC = 120

# Flip classes that carry a decision difference (the answer to Q1).
DIVERGENT = (V2_WOULD_BLOCK, V2_WOULD_OPEN)

# 27_bot_regime_analyzer.REFERENCE_WINDOW_DAYS — the window whose closed trigger
# legs feed `bot_regime_performance`, i.e. the data v2's expectancy lower bound
# is FITTED on. Any run whose window overlaps it measures v2 in-sample on the
# trigger leg. Mirrored (not imported) because the analyzer module name starts
# with a digit; `--until` exists so the out-of-sample read is one flag away.
REFERENCE_WINDOW_DAYS = 30


# ─────────────────────────────────────────────────────────────────────────────
# PURE HELPERS (DB-free — pinned in backtest/test_whitelist_v2_realized_eval.py)
# ─────────────────────────────────────────────────────────────────────────────
def nearest_index(sorted_ts: list[datetime], target: datetime) -> int | None:
    """Index of the entry in `sorted_ts` closest to `target` (None if empty)."""
    if not sorted_ts:
        return None
    i = bisect.bisect_left(sorted_ts, target)
    if i == 0:
        return 0
    if i >= len(sorted_ts):
        return len(sorted_ts) - 1
    before, after = sorted_ts[i - 1], sorted_ts[i]
    return i if (after - target) < (target - before) else i - 1


def pick_domain(n_utc: int, n_legacy: int) -> str:
    """Which reading of a tag's naive timestamps matches more events.

    Ties go to ``utc`` — the target state of docs/UTC_POLICY.md. A tag with no
    match under either reading also lands on ``utc`` (nothing to explain away).
    """
    return "legacy" if n_legacy > n_utc else "utc"


def realized_from_ai(row: dict) -> dict:
    """closed_ai_signals row → the canonical realized leg dict (T-032 math)."""
    oc = classify_ai_outcome(row.get("status"), row.get("targets_hit"))
    move = None
    staffed = False
    lev_pnl = None
    if oc in (WIN, LOSS):
        tlist = _parse_targets(row.get("targets"))
        move, staffed = unlev_move(
            row.get("direction"), row.get("entry"), row.get("close_price"), tlist, row.get("targets_hit")
        )
        lev_pnl = realized_pnl_pct(
            row.get("direction"),
            row.get("entry"),
            row.get("close_price"),
            tlist,
            row.get("targets_hit"),
            row.get("lev"),
        )
    return {"outcome": oc, "move": move, "staffed": staffed, "lev_pnl": lev_pnl, "r": None}


def realized_from_classic(row: dict) -> dict:
    """closed_trades_master row → the canonical realized leg dict (T-032 math).

    `targets_hit` is derived from `status` exactly as fleet_realized_audit does
    (a `SL2` row counts as a win but contributes 0 staged targets) — keeping the
    two reports on one definition matters more than the small conservatism.
    """
    oc = classify_classic_outcome(row.get("status"))
    move = None
    staffed = False
    lev_pnl = None
    if oc in (WIN, LOSS):
        tlist = _classic_targets(row.get("target1"), row.get("target2"), row.get("target3"), row.get("target4"))
        hits = _parse_hits(row.get("status"))
        move, staffed = unlev_move(row.get("direction"), row.get("entry"), row.get("close_price"), tlist, hits)
        lev_pnl = realized_pnl_pct(
            row.get("direction"), row.get("entry"), row.get("close_price"), tlist, hits, row.get("lev")
        )
    return {"outcome": oc, "move": move, "staffed": staffed, "lev_pnl": lev_pnl, "r": None}


def summarize_legs(events: list[dict], leg_key: str) -> dict:
    """Aggregate the attached realized legs of `events` (T-032 leg stats).

    `leg_key` is ``trigger`` or ``rom1``. Events without an attached leg are
    counted as `n_no_leg` — never dropped, never treated as a zero.
    """
    rows = []
    n_no_leg = 0
    for e in events:
        leg = e.get(leg_key)
        if not leg:
            n_no_leg += 1
            continue
        rows.append({**leg, "ts": e.get("ts")})
    stats = aggregate_leg(rows)
    stats["n_events"] = len(events)
    stats["n_with_leg"] = len(rows)
    stats["n_no_leg"] = n_no_leg
    stats["leg_coverage_pct"] = round(100.0 * len(rows) / len(events), 1) if events else None
    return stats


def by_bot_direction(events: list[dict], leg_key: str) -> list[dict]:
    """Per (bot, direction) realized breakdown, sorted by |sum_move| desc."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in events:
        groups[(e["tag"], e["direction"])].append(e)
    out = []
    for (tag, direction), grp in groups.items():
        out.append({"tag": tag, "direction": direction, "stats": summarize_legs(grp, leg_key)})
    out.sort(key=lambda r: abs(r["stats"]["sum_move_pct"] or 0.0), reverse=True)
    return out


def v1_path_of(event: dict) -> str:
    """The recorded v1 gate path of an event (`bucket` is `<class>:<path>`)."""
    bucket = event.get("bucket") or ""
    return bucket.split(":", 1)[1] if ":" in bucket else "unknown"


def path_breakdown(events: list[dict], leg_key: str) -> list[dict]:
    """Per recorded v1 gate path — does v2 remove the crutch or override merit?

    The `insufficient_data` path is v1's default-open crutch (n < 30 in the
    cell); `wr_above_overall` / `counter_trend_specialist` are v1 decisions ON
    MERIT. A flip that mostly hits the crutch is a different change than one
    that mostly overrides merit — and the cell matrix and the traffic answer
    that question differently, so it has to be measured on the traffic.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        groups[v1_path_of(e)].append(e)
    out = [{"v1_path": path, "stats": summarize_legs(grp, leg_key)} for path, grp in groups.items()]
    out.sort(key=lambda r: r["stats"]["n_events"], reverse=True)
    return out


def agreement_split(events: list[dict], leg_key: str) -> dict:
    """Split a divergence class into the clean and the drift-contaminated part.

    A flip class compares the RECORDED v1 decision (a fact, from the event's
    gate path) against TODAY's v2 cell (an approximation). Where today's v1
    cell no longer matches the recorded v1 decision, the cell has moved since
    the signal — and the "divergence" then compares two different cell states,
    not v1 against v2. Only the `v1_agree` part is a clean v1-vs-v2 read; the
    `v1_drifted` part is reported separately instead of being folded in.

    (Measured 2026-08-01: in the pre-July window every single `v2_would_open`
    event was drift-contaminated — the cells whose v1 blocked back then are
    v1-open today. Folding them in would have produced a confident,
    meaningless number.)
    """
    agree = [e for e in events if e.get("v1_snapshot_agree") is True]
    drifted = [e for e in events if e.get("v1_snapshot_agree") is False]
    unknown = [e for e in events if e.get("v1_snapshot_agree") is None]
    return {
        "v1_agree": summarize_legs(agree, leg_key),
        "v1_drifted": summarize_legs(drifted, leg_key),
        "n_unknown": len(unknown),
    }


def flip_delta(per_class: dict[str, dict]) -> dict:
    """The decision line: what v2 gives up and what it buys, in realized %.

    `v2_would_block` is money v1 actually took (positive sum = v2 costs it),
    `v2_would_open` is money v1 left on the table (positive sum = v2 buys it).
    The delta is their difference — the sign of the flip's realized effect on
    the measured population.
    """
    removed = per_class.get(V2_WOULD_BLOCK, {})
    added = per_class.get(V2_WOULD_OPEN, {})
    r_sum = removed.get("sum_move_pct") or 0.0
    a_sum = added.get("sum_move_pct") or 0.0
    return {
        "v2_removes_n": removed.get("n_decided", 0),
        "v2_removes_sum_move_pct": round(r_sum, 2),
        "v2_removes_net_mean_pct": removed.get("net_mean_pct"),
        "v2_adds_n": added.get("n_decided", 0),
        "v2_adds_sum_move_pct": round(a_sum, 2),
        "v2_adds_net_mean_pct": added.get("net_mean_pct"),
        "delta_sum_move_pct": round(a_sum - r_sum, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# DB (read-only)
# ─────────────────────────────────────────────────────────────────────────────
def load_ai_closes(conn, since: datetime) -> list[dict]:
    """Deduped `closed_ai_signals` rows in the window, both time readings.

    The dedup key is the Report-14 survivor key used by fleet_realized_audit —
    `closed_ai_signals` carries ~357k duplicate re-close rows and an
    un-deduped join would count the same trade many times.
    """
    sql = f"""
        SELECT model, symbol, upper(btrim(direction)) AS direction, entry, close_price,
               targets_hit, targets, lev, status, open_time, close_time,
               (open_time AT TIME ZONE '{LEGACY_WRITER_TZ}' AT TIME ZONE 'UTC') AS open_time_legacy
        FROM (
            SELECT DISTINCT ON (symbol, model, upper(btrim(direction)), open_time)
                   model, symbol, direction, entry, close_price, targets_hit, targets,
                   lev, status, open_time, close_time
            FROM closed_ai_signals
            WHERE open_time >= %s
            ORDER BY symbol, model, upper(btrim(direction)), open_time,
                     close_time ASC NULLS LAST, targets_hit DESC NULLS LAST, status ASC NULLS LAST
        ) d
    """
    out = []
    with conn.cursor() as cur:
        cur.execute(sql, (since - timedelta(days=1),))
        for r in cur.fetchall():
            out.append(
                {
                    "tag": pretty_name(str(r[0])),
                    "coin": r[1],
                    "direction": r[2],
                    "entry": r[3],
                    "close_price": r[4],
                    "targets_hit": r[5],
                    "targets": r[6],
                    "lev": r[7],
                    "status": r[8],
                    "ts_utc": r[9],
                    "close_time": r[10],
                    "ts_legacy": r[11],
                    "source": "ai",
                }
            )
    return out


def load_classic_closes(conn, since: datetime) -> list[dict]:
    """Deduped `closed_trades_master` rows in the window, both time readings."""
    sql = f"""
        SELECT strategy, coin, upper(btrim(direction)) AS direction, entry, close_price,
               target1, target2, target3, target4, lev, status, time,
               (time AT TIME ZONE '{LEGACY_WRITER_TZ}' AT TIME ZONE 'UTC') AS time_legacy
        FROM (
            SELECT DISTINCT ON (coin, strategy, upper(btrim(direction)), time)
                   strategy, coin, direction, entry, close_price,
                   target1, target2, target3, target4, lev, status, time
            FROM closed_trades_master
            WHERE time >= %s
            ORDER BY coin, strategy, upper(btrim(direction)), time,
                     posted ASC NULLS LAST, status DESC NULLS LAST
        ) d
    """
    out = []
    with conn.cursor() as cur:
        cur.execute(sql, (since - timedelta(days=1),))
        for r in cur.fetchall():
            out.append(
                {
                    "tag": pretty_name(str(r[0])),
                    "coin": r[1],
                    "direction": r[2],
                    "entry": r[3],
                    "close_price": r[4],
                    "target1": r[5],
                    "target2": r[6],
                    "target3": r[7],
                    "target4": r[8],
                    "lev": r[9],
                    "status": r[10],
                    "ts_utc": r[11],
                    "ts_legacy": r[12],
                    "source": "classic",
                }
            )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# TWIN MATCHING (domain-detecting)
# ─────────────────────────────────────────────────────────────────────────────
class TwinIndex:
    """Nearest-timestamp index over closed trades, per (tag, coin, direction).

    Holds both readings of the stored naive timestamp (as-UTC and as
    LEGACY_WRITER_TZ) and decides PER TAG which one the writer actually used,
    by counting matches under each. That decision is data, not configuration —
    it is reported, and it survives the pending R3 history decision either way.
    """

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self._by_domain: dict[str, dict[tuple, tuple[list, list]]] = {}
        for domain, field in (("utc", "ts_utc"), ("legacy", "ts_legacy")):
            buckets: dict[tuple, list] = defaultdict(list)
            for row in rows:
                if row.get(field) is None:
                    continue
                buckets[(row["tag"], row["coin"], row["direction"])].append((row[field], row))
            packed: dict[tuple, tuple[list, list]] = {}
            for key, items in buckets.items():
                items.sort(key=lambda p: p[0])
                packed[key] = ([p[0] for p in items], [p[1] for p in items])
            self._by_domain[domain] = packed
        self.domain_by_tag: dict[str, str] = {}
        self.domain_stats: dict[str, dict] = {}

    def _match(self, domain: str, tag: str, coin: str, direction: str, ts: datetime, tol_sec: int):
        packed = self._by_domain[domain].get((tag, coin, direction))
        if not packed:
            return None, None
        stamps, rows = packed
        i = nearest_index(stamps, ts)
        if i is None:
            return None, None
        delta = abs((stamps[i] - ts).total_seconds())
        if delta > tol_sec:
            return None, delta
        return rows[i], delta

    def calibrate(self, events: list[dict], tol_sec: int) -> None:
        """Decide the time domain per tag by counting matches under each."""
        counts: dict[str, dict[str, int]] = defaultdict(lambda: {"utc": 0, "legacy": 0})
        for e in events:
            for domain in ("utc", "legacy"):
                row, _ = self._match(domain, e["tag"], e["coin"], e["direction"], e["ts"], tol_sec)
                if row is not None:
                    counts[e["tag"]][domain] += 1
        totals: dict[str, int] = defaultdict(int)
        for e in events:
            totals[e["tag"]] += 1
        for tag, n_total in totals.items():
            c = counts.get(tag, {"utc": 0, "legacy": 0})
            domain = pick_domain(c["utc"], c["legacy"])
            self.domain_by_tag[tag] = domain
            self.domain_stats[tag] = {
                "n_events": n_total,
                "matched_as_utc": c["utc"],
                "matched_as_legacy": c["legacy"],
                "domain_used": domain,
                "hit_pct": round(100.0 * c[domain] / n_total, 1) if n_total else None,
            }

    def match(self, event: dict, tol_sec: int):
        domain = self.domain_by_tag.get(event["tag"], "utc")
        return self._match(domain, event["tag"], event["coin"], event["direction"], event["ts"], tol_sec)


def _claim_nearest(candidates: list[tuple[float, int, dict, dict]]) -> tuple[int, int]:
    """Greedy 1:1 assignment of closed trades to events, closest pair first.

    Without this, two gate events of the same (tag, coin, direction) inside the
    tolerance would both attach the SAME closed trade and its realized PnL
    would be counted twice. Returns (n_claimed, n_collisions).
    """
    used: set[int] = set()
    n_claimed = 0
    n_collisions = 0
    for delta, _seq, event, row in sorted(candidates, key=lambda c: (c[0], c[1])):
        rid = id(row)
        if rid in used:
            n_collisions += 1
            continue
        used.add(rid)
        event["_claim"] = (row, delta)
        n_claimed += 1
    return n_claimed, n_collisions


def attach_trigger_legs(events: list[dict], index: TwinIndex, tol_sec: int) -> dict:
    """Attach every event's own source-bot realized leg (symmetric yardstick)."""
    candidates = []
    for seq, e in enumerate(events):
        row, delta = index.match(e, tol_sec)
        if row is not None:
            candidates.append((delta, seq, e, row))
    n_claimed, n_collisions = _claim_nearest(candidates)

    deltas: list[float] = []
    for e in events:
        claim = e.pop("_claim", None)
        if claim is None:
            e["trigger"] = None
            e["trigger_skip"] = "no_twin"
            continue
        row, delta = claim
        e["trigger"] = realized_from_ai(row) if row["source"] == "ai" else realized_from_classic(row)
        e["trigger_delta_sec"] = round(delta, 1)
        deltas.append(delta)
    deltas.sort()

    def pct(p: float):
        return round(deltas[min(int(p * len(deltas)), len(deltas) - 1)], 1) if deltas else None

    return {
        "n_events": len(events),
        "n_matched": n_claimed,
        "n_collisions": n_collisions,
        "match_pct": round(100.0 * n_claimed / len(events), 1) if events else None,
        "delta_sec_p50": pct(0.50),
        "delta_sec_p90": pct(0.90),
        "delta_sec_max": round(deltas[-1], 1) if deltas else None,
    }


def attach_rom1_legs(events: list[dict], index: TwinIndex, tol_sec: int) -> dict:
    """Attach the ROM1 leg the orchestrator actually opened (forwarded only)."""
    forwarded = [e for e in events if e["side"] == "forwarded"]
    for e in events:
        if e["side"] != "forwarded":
            e["rom1"] = None
            e["rom1_skip"] = "not_forwarded"

    candidates = []
    for seq, e in enumerate(forwarded):
        probe = {"tag": "ROM1", "coin": e["coin"], "direction": e["direction"], "ts": e["ts"]}
        row, delta = index.match(probe, tol_sec)
        if row is not None:
            candidates.append((delta, seq, e, row))
    n_claimed, n_collisions = _claim_nearest(candidates)

    for e in forwarded:
        claim = e.pop("_claim", None)
        if claim is None:
            e["rom1"] = None
            e["rom1_skip"] = "no_rom1_leg"
            continue
        row, delta = claim
        e["rom1"] = realized_from_ai(row)
        e["rom1_delta_sec"] = round(delta, 1)
    return {
        "n_forwarded": len(forwarded),
        "n_matched": n_claimed,
        "n_collisions": n_collisions,
        "match_pct": round(100.0 * n_claimed / len(forwarded), 1) if forwarded else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────────────────
_CLASS_ORDER = (V2_WOULD_BLOCK, V2_WOULD_OPEN, BOTH_OPEN, BOTH_BLOCK, V2_MISSING, "cell_missing", "unaffected")


def _fmt(v, spec: str = "") -> str:
    if v is None:
        return "—"
    return format(v, spec) if spec else str(v)


_LEG_HEAD = (
    "| Bot | Dir | Events | mit Leg | zensiert | decided | WR% | Σ Move% | Ø netto% | Σ lev% (n) |",
    "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
)


def _leg_cells(s: dict) -> str:
    """The measurement columns of one leg row.

    `zensiert` and the `(n)` behind the leveraged sum are not decoration: the
    ROM1 book is dominated by REGIME_CHANGE closes (counted as censored, not as
    a loss) and `lev` is persisted only for part of the fleet — without both
    counts the sums read as if they covered the whole population.
    """
    return (
        f"{s['n_events']} | {s['n_with_leg']} | {s['censored_n']} | {s['n_decided']} | "
        f"{_fmt(s['wr_pct'], '.1f')} | {_fmt(s['sum_move_pct'], '.1f')} | "
        f"{_fmt(s['net_mean_pct'], '.3f')} | {_fmt(s['lev_sum_pct'], '.1f')} ({s['lev_n']})"
    )


def _leg_table(rows: list[dict]) -> list[str]:
    out = list(_LEG_HEAD)
    for r in rows:
        out.append(f"| {r['tag']} | {r['direction']} | {_leg_cells(r['stats'])} |")
    return out


def build_markdown(meta: dict) -> str:
    p = meta["prereqs"]
    m = meta["divergence_matrix"]
    v = meta["volume"]
    d = meta["drift"]
    ap: list[str] = []
    ap.append("# whitelist_v2 Flip — realisierte Entscheidungsgrundlage (T-2026-KYT-9050-007)\n")
    ap.append(f"**Fenster:** {meta['since']} → {meta['generated_at']} (UTC)  ")
    ap.append(
        f"**Snapshot:** {p['n_rows']} Zellen, v2-Coverage {p['v2_coverage_pct']}%, Alter {p['snapshot_age_hours']}h "
    )
    ap.append(f"({'Analyzer lebt' if p['analyzer_alive'] else '⚠️ ANALYZER STALE'})\n")

    ap.append("## 1. Zell-Divergenz (heutiger Snapshot)\n")
    ap.append("| Klasse | Zellen | Anteil |")
    ap.append("|---|---:|---:|")
    n_cells = m["n_cells"] or 1
    for cls in (BOTH_OPEN, BOTH_BLOCK, V2_WOULD_BLOCK, V2_WOULD_OPEN, V2_MISSING):
        n = m["totals"].get(cls, 0)
        ap.append(f"| {cls} | {n} | {100.0 * n / n_cells:.1f}% |")
    ap.append("")

    ap.append("## 2. Echter Gate-Traffic\n")
    ap.append(
        f"- Events gesamt: **{v['n_events_total']}**, davon zell-entschieden (flip-relevant): **{v['n_cell_decided']}**"
    )
    ap.append(f"- Gate-Rate offen: v1 **{v['v1_open_rate_pct']}%** → v2 **{v['v2_open_rate_pct']}%**")
    ap.append(
        f"- ROM1-Forwards/Tag: v1 **{v['forwarded_per_day_v1']}** → v2 (Prognose) "
        f"**{v['forwarded_per_day_v2_projected']}**"
    )
    ap.append(
        f"- v1-Drift der Snapshot-Näherung: {d['n_agree']}/{d['n_comparable']} = **{d['agree_pct']}%** Übereinstimmung"
    )
    ap.append("")

    ap.append("## 3. Was die divergenten Signale REALISIERT haben\n")
    ap.append("### 3a. Trigger-Leg (eigener Trade des Quell-Bots — symmetrisch, beide Seiten)\n")
    ap.append("| Klasse | Events | mit Leg | zensiert | decided | WR% | Σ Move% | Ø netto% | Σ lev% (n) |")
    ap.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for cls in _CLASS_ORDER:
        s = meta["trigger_by_class"].get(cls)
        if not s:
            continue
        ap.append(f"| {cls} | {_leg_cells(s)} |")
    ap.append("")
    fd = meta["flip_delta_trigger"]
    ap.append(
        f"**Flip-Bilanz auf dem Trigger-Leg:** v2 nimmt weg Σ {fd['v2_removes_sum_move_pct']}% "
        f"({fd['v2_removes_n']} entschiedene Trades), v2 schaltet frei Σ {fd['v2_adds_sum_move_pct']}% "
        f"({fd['v2_adds_n']}) → **Δ {fd['delta_sum_move_pct']}%** (unlevered Move).\n"
    )

    ap.append("### 3b. ROM1-Leg (das echte Geld — existiert nur auf der forwarded-Seite)\n")
    ap.append("| Klasse | Events | mit ROM1-Leg | zensiert | decided | WR% | Σ Move% | Ø netto% | Σ lev% (n) |")
    ap.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for cls in _CLASS_ORDER:
        s = meta["rom1_by_class"].get(cls)
        if not s or not s["n_with_leg"]:
            continue
        ap.append(f"| {cls} | {_leg_cells(s)} |")
    ap.append(
        "\n> `v2_would_open` hat strukturell KEIN ROM1-Leg: diese Signale wurden nie geforwardet, "
        "also nie als ROM1 gehandelt. Die zusätzlich freigeschaltete Seite ist in ROM1-Geld "
        "grundsätzlich nicht messbar — nur im Trigger-Leg (3a), und das trägt eine andere "
        "Geometrie (P1.10).\n"
    )

    ap.append("## 3c. Sauber vs. drift-kontaminiert (die belastbare Teilmenge)\n")
    ap.append(
        "Die Flip-Klasse vergleicht die AUFGEZEICHNETE v1-Entscheidung mit der HEUTIGEN v2-Zelle. "
        "Wo die heutige v1-Zelle nicht mehr zur aufgezeichneten Entscheidung passt, hat sich die Zelle "
        "seither bewegt — dann vergleicht die Klasse zwei verschiedene Zellstände, nicht v1 gegen v2. "
        "Nur `v1_agree` ist ein sauberer v1-vs-v2-Lesewert.\n"
    )
    ap.append("| Klasse | Teilmenge | Events | mit Leg | zensiert | decided | WR% | Σ Move% | Ø netto% | Σ lev% (n) |")
    ap.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for cls in DIVERGENT:
        split = meta["agreement_trigger"].get(cls) or {}
        for subset in ("v1_agree", "v1_drifted"):
            s = split.get(subset)
            if not s or not s["n_events"]:
                continue
            ap.append(f"| {cls} | {subset} | {_leg_cells(s)} |")
    ap.append("")

    ap.append("## 4. Über welchen v1-Pfad kam der divergente Traffic?\n")
    ap.append(
        "`insufficient_data` ist v1s Default-Open-Krücke (n < 30 in der Zelle), "
        "`wr_above_overall` / `counter_trend_specialist` sind v1-Entscheidungen AUF MERIT. "
        "Die Zell-Matrix und der Traffic beantworten das unterschiedlich.\n"
    )
    for cls in DIVERGENT:
        rows = meta["by_v1_path"].get(cls) or []
        if not rows:
            continue
        ap.append(f"### {cls} — Trigger-Leg nach v1-Pfad\n")
        ap.append("| v1-Pfad | Events | mit Leg | zensiert | decided | WR% | Σ Move% | Ø netto% | Σ lev% (n) |")
        ap.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for r in rows:
            ap.append(f"| {r['v1_path']} | {_leg_cells(r['stats'])} |")
        ap.append("")

    ap.append("## 5. Aufschlüsselung nach Bot × Richtung\n")
    for cls in DIVERGENT:
        rows = meta["by_bot_direction"].get(cls) or []
        ap.append(f"### {cls} — Trigger-Leg\n")
        if not rows:
            ap.append("_keine Events in dieser Klasse._\n")
            continue
        ap.extend(_leg_table(rows))
        ap.append("")

    ap.append("## 6. Messgrenzen (gemessen, nicht angenommen)\n")
    for lim in meta["limits"]:
        ap.append(f"- {lim}")
    ap.append("")
    return "\n".join(ap)


def print_console_report(meta: dict) -> None:
    p = meta["prereqs"]
    m = meta["divergence_matrix"]
    v = meta["volume"]
    print("\n── Prereqs ──")
    print(
        f"bot_regime_whitelist: {p['n_rows']} Zellen, v2-Coverage {p['v2_coverage_pct']}%, "
        f"Alter {p['snapshot_age_hours']}h "
        f"({'Analyzer lebt' if p['analyzer_alive'] else '⚠️ ANALYZER STALE — Ergebnis nicht belastbar'})"
    )

    print("\n── Zell-Divergenz (Snapshot) ──")
    for cls in (BOTH_OPEN, BOTH_BLOCK, V2_WOULD_BLOCK, V2_WOULD_OPEN, V2_MISSING):
        n = m["totals"].get(cls, 0)
        print(f"  {cls:16} {n:6d}  ({100.0 * n / (m['n_cells'] or 1):5.1f}%)")

    print("\n── Traffic pro Tag ──")
    for day, c in meta["daily_counts"].items():
        print(f"  {day}: forwarded {c['forwarded']:4d}  suppressed(gate) {c['suppressed']:4d}")

    d = meta["drift"]
    print(f"\n── v1-Drift (Snapshot-Näherung) ── {d['n_agree']}/{d['n_comparable']} = {d['agree_pct']}%")
    print(
        f"\n── Volumen ──\n  zell-entschieden {v['n_cell_decided']}/{v['n_events_total']}\n"
        f"  Gate-Rate offen: v1 {v['v1_open_rate_pct']}% → v2 {v['v2_open_rate_pct']}%\n"
        f"  ROM1-Forwards/Tag: v1 {v['forwarded_per_day_v1']} → v2 {v['forwarded_per_day_v2_projected']}"
    )

    print("\n── Zeit-Domäne pro Tag (gemessen) ──")
    for tag, st in sorted(meta["time_domains"].items(), key=lambda kv: -kv[1]["n_events"])[:20]:
        print(
            f"  {tag:18} n={st['n_events']:6d}  utc={st['matched_as_utc']:6d}  "
            f"legacy={st['matched_as_legacy']:6d}  → {st['domain_used']:6}  hit {st['hit_pct']}%"
        )

    print(f"\n── Twin-Match ── Trigger {meta['trigger_match']} \n   ROM1 {meta['rom1_match']}")

    hdr = f"{'flip_class':16} {'events':>7} {'leg':>6} {'dec':>6} {'WR%':>6} {'ΣMove%':>9} {'Ønetto%':>9} {'Σlev%':>10}"
    for title, key in (("Trigger-Leg", "trigger_by_class"), ("ROM1-Leg", "rom1_by_class")):
        print(f"\n── Realisiert pro Flip-Klasse ({title}) ──")
        print(hdr)
        print("-" * len(hdr))
        for cls in _CLASS_ORDER:
            s = meta[key].get(cls)
            if not s:
                continue
            print(
                f"{cls:16} {s['n_events']:7d} {s['n_with_leg']:6d} {s['n_decided']:6d} "
                f"{_fmt(s['wr_pct'], '6.1f'):>6} {_fmt(s['sum_move_pct'], '9.1f'):>9} "
                f"{_fmt(s['net_mean_pct'], '9.3f'):>9} {_fmt(s['lev_sum_pct'], '10.1f'):>10}"
            )

    print("\n── Divergenz sauber vs. drift-kontaminiert (Trigger-Leg) ──")
    for cls in DIVERGENT:
        split = meta["agreement_trigger"].get(cls) or {}
        for subset in ("v1_agree", "v1_drifted"):
            s = split.get(subset)
            if not s or not s["n_events"]:
                continue
            print(
                f"  {cls:16} {subset:11} events {s['n_events']:6d}  decided {s['n_decided']:6d}  "
                f"WR {_fmt(s['wr_pct'], '.1f')}%  ΣMove {_fmt(s['sum_move_pct'], '.1f')}%  "
                f"Ønetto {_fmt(s['net_mean_pct'], '.3f')}%"
            )

    for cls in DIVERGENT:
        rows = meta["by_v1_path"].get(cls) or []
        if not rows:
            continue
        print(f"\n── {cls} nach v1-Pfad (Trigger-Leg) ──")
        for r in rows:
            s = r["stats"]
            print(
                f"  {r['v1_path']:28} events {s['n_events']:6d}  decided {s['n_decided']:6d}  "
                f"WR {_fmt(s['wr_pct'], '.1f')}%  ΣMove {_fmt(s['sum_move_pct'], '.1f')}%"
            )

    fd = meta["flip_delta_trigger"]
    print(
        f"\n── Flip-Bilanz (Trigger-Leg, unlevered) ──\n"
        f"  v2 nimmt weg:    Σ {fd['v2_removes_sum_move_pct']}%  ({fd['v2_removes_n']} decided)\n"
        f"  v2 schaltet frei:Σ {fd['v2_adds_sum_move_pct']}%  ({fd['v2_adds_n']} decided)\n"
        f"  Δ (v2 − v1):     {fd['delta_sum_move_pct']}%"
    )
    print("\n" + "\n".join(f"! {lim}" for lim in meta["limits"]))


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def build_limits(meta: dict) -> list[str]:
    """The honest measurement limits — computed from THIS run, not boilerplate."""
    d = meta["drift"]
    p = meta["prereqs"]
    out = []
    if meta["in_sample_overlap"]:
        out.append(
            "IN-SAMPLE auf dem Trigger-Leg: `27_bot_regime_analyzer` baut `bot_regime_performance` aus genau "
            f"diesen geschlossenen Trigger-Trades der letzten {REFERENCE_WINDOW_DAYS} Tage (ab "
            f"{meta['reference_window_start']}), und v2 entscheidet eine Zelle allein aus deren avg_pnl/stddev. "
            "Dass v2 hier Zellen blockt, deren Trigger-Trades negativ realisiert haben, ist deshalb zum großen "
            "Teil eine Umformulierung von v2s Anpassungskriterium — KEIN unabhängiger Beleg. Unabhängig sind "
            "(a) das ROM1-Leg, auf das v2 nicht gefittet wurde, und (b) ein Lauf mit `--until` vor "
            f"{meta['reference_window_start'][:10]}."
        )
    out += [
        "Snapshot-Näherung: `bot_regime_whitelist` ist UPSERT-only ohne Historie — der v2-Verdikt pro Event "
        f"stammt aus dem heutigen Snapshot ({p['max_computed_at']}), nicht aus dem Stand zur Signal-Zeit. "
        f"Die v1-Drift ({d['agree_pct']}% Übereinstimmung über {d['n_comparable']} Events) misst diesen Fehler "
        "an der einzigen Achse, auf der beide Stände bekannt sind.",
        "Die historische Whitelist ist damit weiterhin NICHT rekonstruierbar (T-031-Befund bestätigt): weder "
        "`bot_regime_whitelist` noch `bot_regime_performance` führen eine Historie, und Bot 28 loggt pro Signal "
        "nur den v1-Pfad, nie den v2-Verdikt.",
        "`v2_would_open` hat kein ROM1-Leg — diese Signale wurden nie gehandelt. Die freigeschaltete Seite ist "
        "nur über den Trade des Quell-Bots messbar, der eine ANDERE Geometrie trägt als ROM1 "
        "(docs/REGIME_ORCHESTRATOR.md, P1.10).",
        "Trigger-Leg-Coverage < 100%: unmatched Events sind als `no_twin` gezählt, nicht als 0 gewertet. "
        "Ursachen: Signal noch offen, Trade nie eröffnet, Monitor-Lücke.",
        "WR ist TP1-Touch, PnL ist der target-gestaffelte unlevered Move (core.realized_pnl, T-115-Definition). "
        "`lev`-PnL ist exact-only — Coverage pro Zeile über `n_with_leg` ablesbar.",
    ]
    if not p["analyzer_alive"]:
        out.insert(0, "⚠️ Der Analyzer (Bot 27) ist stale — der Snapshot bildet den heutigen Stand NICHT ab.")
    return out


def _naive_utc(raw: str) -> datetime:
    """ISO string → naive UTC (the domain of the legacy TIMESTAMP columns)."""
    dt = datetime.fromisoformat(raw)
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def wait_for_cpu_headroom(max_wait_min: int, force: bool, poll_sec: int = 60) -> float | None:
    """`check_cpu_headroom`, but patient — and, on request, overridable.

    The VPS shares its ten cores with the trading fleet and sits at a measured
    100% for hours at a time, where the shared guard (built for CPU-bound
    walk-forward training) aborts immediately. This tool is a read-only
    SELECT-and-aggregate run at BELOW_NORMAL priority, so waiting is the right
    default and `--force-on-busy` is the documented escape for a box that never
    frees up. The measured load is returned so the run records what it ran on
    instead of claiming headroom it did not have.
    """
    deadline = time.monotonic() + max_wait_min * 60
    while True:
        try:
            check_cpu_headroom()
            return _cpu_now()
        except SystemExit:
            if time.monotonic() >= deadline:
                cpu = _cpu_now()
                if not force:
                    raise
                # ASCII on purpose: this is the ONLY line on the --force-on-busy
                # path, and a Windows console defaults to cp1252 — an emoji here
                # raised UnicodeEncodeError and killed the run at the exact
                # moment the operator had asked it to proceed anyway. The
                # documented escape hatch must not depend on the code page.
                print(f"[WARN] CPU {cpu}% - --force-on-busy gesetzt, Lauf startet trotzdem (read-only, BELOW_NORMAL).")
                return cpu
            print(f"CPU busy - warte {poll_sec}s auf Headroom (bis zu {max_wait_min} min) ...", flush=True)
            time.sleep(poll_sec)


def _cpu_now() -> float | None:
    try:
        import psutil

        return psutil.cpu_percent(interval=1)
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Realized v1-vs-v2 whitelist flip evaluation (T-2026-KYT-9050-007)")
    ap.add_argument("--since", default=DEFAULT_SINCE, help="ISO timestamp (naive UTC)")
    ap.add_argument(
        "--until",
        default=None,
        help=(
            "ISO timestamp (naive UTC), exclusive. Use it to score events OUTSIDE the analyzer's "
            f"{REFERENCE_WINDOW_DAYS}-day reference window — v2 is fitted on the trigger legs inside that "
            "window, so a run that ends before it is the only out-of-sample read of the same cells."
        ),
    )
    ap.add_argument("--twin-tolerance-sec", type=int, default=DEFAULT_TWIN_TOL_SEC)
    ap.add_argument("--rom1-tolerance-sec", type=int, default=DEFAULT_ROM1_TOL_SEC)
    ap.add_argument("--limit", type=int, default=None, help="first N rows per side (smoke runs)")
    ap.add_argument("--cpu-wait-min", type=int, default=20, help="minutes to wait for CPU headroom (0 = abort)")
    ap.add_argument(
        "--force-on-busy",
        action="store_true",
        help="run even if the CPU guard never clears (read-only, BELOW_NORMAL) — recorded in the summary",
    )
    ap.add_argument(
        "--out", default=os.getenv("KYTHERA_REPLAY_DIR", os.path.join(REPO_ROOT, "staging_models", "replay"))
    )
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    since = _naive_utc(args.since)
    until = _naive_utc(args.until) if args.until else None
    set_low_priority()
    cpu_at_start = wait_for_cpu_headroom(args.cpu_wait_min, args.force_on_busy)

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
        if until is not None:
            events = [e for e in events if e["ts"] < until]
        for e in events:
            e.update(classify_flip_effect(e, snapshot))
            e["tag"] = pretty_name(e["bot_name"] or "")
            e["direction"] = str(e["direction"] or "").strip().upper()
        print(f"{len(events)} Gate-Events seit {since} geladen und klassifiziert", flush=True)

        closes = load_ai_closes(conn, since) + load_classic_closes(conn, since)
        print(f"{len(closes)} deduplizierte Closes geladen", flush=True)
    finally:
        conn.close()

    index = TwinIndex(closes)
    index.calibrate(events, args.twin_tolerance_sec)
    trigger_match = attach_trigger_legs(events, index, args.twin_tolerance_sec)

    rom1_probe = [
        {"tag": "ROM1", "coin": e["coin"], "direction": e["direction"], "ts": e["ts"]}
        for e in events
        if e["side"] == "forwarded"
    ]
    index.calibrate(rom1_probe, args.rom1_tolerance_sec)
    rom1_match = attach_rom1_legs(events, index, args.rom1_tolerance_sec)

    by_class: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_class[e["flip_class"] or "unclassified"].append(e)

    window_end = until or utc_now().replace(tzinfo=None)
    window_days = max((window_end - since).total_seconds() / 86400.0, 0.0)
    ref_start = utc_now().replace(tzinfo=None) - timedelta(days=REFERENCE_WINDOW_DAYS)
    meta: dict = {
        "since": str(since),
        "until": str(until) if until else None,
        "generated_at": str(utc_now()),
        # True when the scored window lies (partly) inside the analyzer's fitting
        # window — then the trigger-leg verdict is in-sample, see build_limits.
        "in_sample_overlap": window_end > ref_start,
        "reference_window_start": str(ref_start),
        "cpu_at_start_pct": cpu_at_start,
        "twin_tolerance_sec": args.twin_tolerance_sec,
        "rom1_tolerance_sec": args.rom1_tolerance_sec,
        "prereqs": prereqs,
        "divergence_matrix": matrix,
        "daily_counts": daily_counts(events),
        "drift": drift_rate(events),
        "volume": volume_effect(events, window_days),
        "time_domains": index.domain_stats,
        "trigger_match": trigger_match,
        "rom1_match": rom1_match,
        "trigger_by_class": {cls: summarize_legs(evs, "trigger") for cls, evs in by_class.items()},
        "rom1_by_class": {cls: summarize_legs(evs, "rom1") for cls, evs in by_class.items()},
        "by_bot_direction": {cls: by_bot_direction(by_class.get(cls, []), "trigger") for cls in DIVERGENT},
        "by_v1_path": {cls: path_breakdown(by_class.get(cls, []), "trigger") for cls in DIVERGENT},
        "agreement_trigger": {cls: agreement_split(by_class.get(cls, []), "trigger") for cls in DIVERGENT},
        "agreement_rom1": {cls: agreement_split(by_class.get(cls, []), "rom1") for cls in DIVERGENT},
        "rom1_by_bot_direction": {cls: by_bot_direction(by_class.get(cls, []), "rom1") for cls in DIVERGENT},
    }
    meta["flip_delta_trigger"] = flip_delta(meta["trigger_by_class"])
    meta["flip_delta_rom1"] = flip_delta(meta["rom1_by_class"])
    meta["limits"] = build_limits(meta)

    os.makedirs(args.out, exist_ok=True)
    # Parameterized name (047/069 pattern): comparison runs must not overwrite
    # each other — the whole point is reading the windows side by side.
    tag = f"whitelist_v2_realized_eval_{since.date()}" + (f"_to_{until.date()}" if until else "")
    jsonl_path = os.path.join(args.out, f"{tag}.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps({**e, "ts": str(e["ts"])}, default=str) + "\n")
    with open(os.path.join(args.out, f"{tag}_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)
    md_path = os.path.join(args.out, f"{tag}.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(build_markdown(meta))

    print_console_report(meta)
    print(f"\nRecords: {jsonl_path}\nReport:  {md_path}")


if __name__ == "__main__":
    main()
