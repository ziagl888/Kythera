"""tools/fleet_realized_audit.py — fleet-wide realized-trade audit (phase A, T-2026-KYT-9050-032).

Purpose
-----
Reviewable control table of the realized edge of EVERY bot DIRECTLY FROM THE DB
(no backtesting of its own here), per **tag × direction (LONG/SHORT) × lifecycle
(active/shadow/retired/inactive)**. Ranking → retire candidates (negative
realized edge) vs keep (positive). Pure ANALYSIS + RECOMMENDATION — no
live intervention (hard rule 1/2, retire = Michi escalation).

Data sources (live DB, strictly read-only)
-----------------------------------------
  * `closed_ai_signals` (model = tag): AI bots. NO usable indexes,
    357k duplicate trap (LEGACY re-close event) → DISTINCT ON the report-14
    survivor key (symbol, model, dir, open_time), earliest close. No `sl`
    column → R-multiple cannot be reconstructed here.
  * `closed_trades_master` (strategy = tag): classic detectors (3_detectors).
    Carries `sl` → R-multiple available here. close_price<=0 era before 2026-03 (v1)
    drops out via the entry/close>0 filter.

Outcome classification
----------------------
  * LEGACY rows (status contains "LEGACY") are SYNTHETIC (fixed ±2.5%/-5%
    close prices from the Feb/March migration) → EXCLUDED from the realized
    edge and reported separately as `legacy_n`; their PnL magnitude
    is meaningless (WR only from the status text).
  * Censored (DELISTED/CLEANUP/REGIME_CHANGE/FORCE_CLOSED): externally caused
    closes, neither win nor loss — excluded from WR + edge, counted as `censored_n`.
  * Win = TP1 touched (AI: targets_hit>=1 or "ALL TARGETS"; classic: status
    1..4/SL1..3). Loss = SL0/no target. WR is TP1 touch → secondary; **PnL
    (price move) is primary** (R:R counts, WR alone is misleading).

Metrics per leg (only NON-legacy, NON-censored "decided" trades)
--------------------------------------------------------------------
  n, WR%, unlevered move% (sum/mean/median), net mean (− fee), Sharpe
  (mean/std, NOT annualised), t-stat (mean/(std/√n)); leveraged realized PnL
  (sum/mean) exact-only where targets+lev are persisted; R-multiple (mean/median,
  classic only with sl>0); time span first/last.

Join limits (honestly)
----------------------
  * closed_ai_signals has NO sl → no R-multiple for AI bots.
  * targets+lev are thinly persisted for old tags (bot-8 monitor migration) →
    leveraged PnL is exact-only, coverage reported per leg; unlevered move
    is the coverage-robust edge metric.
  * prob↔outcome is only partially joinable in the live DB → outcome via realized
    trade `status`, not via prob.
  * active-vs-inactive depends on control/parked markers of the LIVE checkout; in
    the worktree the park state may not be visible → controllable via --parked-dir,
    otherwise noted as a limit. shadow/retired/silent are
    code-defined (shadow_gate) and correct in the worktree.

Operating rules (live VPS!)
--------------------------
  DB strictly read-only, BELOW_NORMAL priority, CPU headroom check. No table
  is written. Results under KYTHERA_REPLAY_DIR (JSON + markdown).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime

# core.realized_pnl is pure (stdlib only, no DB) → safe to import for the
# staffed weighted-move math and keep the helpers DB-free testable.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
from core.realized_pnl import weighted_move_pct  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# PURE HELPERS (DB-free, pinned in backtest/test_fleet_realized_audit.py)
# ─────────────────────────────────────────────────────────────────────────────

# Unlevered taker round-trip fee on the price-move scale (%), same assumption as
# tools/audit/step4_results.py. Applied to the unlevered move for the net edge.
FEE_RT_PCT = 0.10

# |unlevered move| above this (%) is a data bug (mirror core.realized_pnl).
MAX_ABS_MOVE_PCT = 100.0

# Outcome classes.
WIN, LOSS, CENSORED, LEGACY, TIMEOUT, UNFILLED, OTHER = (
    "win",
    "loss",
    "censored",
    "legacy",
    "timeout",
    "unfilled",
    "other",
)

_CENSOR_FRAGMENTS = ("DELISTED", "CLEANUP", "ORPHAN", "REGIME", "EXPIRED", "FORCE")


def classify_ai_outcome(status: object, targets_hit: object) -> str:
    """AI row → outcome class. LEGACY first (synthetic), then censorship, then
    win (TP1 touch), then loss/timeout/unfilled."""
    s = str(status or "").upper()
    if "LEGACY" in s:
        return LEGACY
    if any(frag in s for frag in _CENSOR_FRAGMENTS):
        return CENSORED
    if "ENTRY_NOT_FILLED" in s:
        return UNFILLED
    try:
        hit = int(targets_hit) if targets_hit is not None else 0
    except (TypeError, ValueError):
        hit = 0
    if "ALL TARGETS" in s or hit >= 1:
        return WIN
    if s.startswith("SL"):
        return LOSS
    if "TIMEOUT" in s:
        return TIMEOUT
    return OTHER


# Classic status → outcome. Wins carry a hit target index (1..4 or SL1..3 =
# TP1..TP3 touched then SL); losses are 0/SL0; the rest is external/censored.
_CLASSIC_WIN = {"1", "2", "3", "4", "SL1", "SL2", "SL3"}
_CLASSIC_LOSS = {"0", "SL0"}


def classify_classic_outcome(status: object) -> str:
    """closed_trades_master.status → outcome class."""
    s = str(status or "").strip().upper()
    if any(frag in s for frag in _CENSOR_FRAGMENTS):
        return CENSORED
    if s in _CLASSIC_WIN:
        return WIN
    if s in _CLASSIC_LOSS:
        return LOSS
    return OTHER


def signed_move_pct(direction: object, entry: object, close: object) -> float | None:
    """Direction-corrected unlevered price move in % (LONG +, SHORT −).

    None for invalid prices/direction or a move above MAX_ABS_MOVE_PCT (bug)."""
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
    sign = 1.0 if side == "LONG" else -1.0
    move = sign * (c - e) / e * 100.0
    if abs(move) > MAX_ABS_MOVE_PCT:
        return None
    return move


def unlev_move(
    direction: object, entry: object, close: object, targets: list, targets_hit: object, model: object = None
) -> tuple[float | None, bool]:
    """Unlevered realized move in % + staffed flag.

    Prefers the TARGET-STAGGERED move (`core.realized_pnl.weighted_move_pct`,
    the canonical fleet-realized definition, T-115): the stake is split evenly across
    the N targets, k hit targets realize at their price, the
    rest closes at close_price. That is the CORRECT realized edge for
    laddered-TP bots — the raw entry→close move UNDERESTIMATES a winner that
    books TP1/TP2 and runs back to the SL on the remainder (close=SL, but 2/4 booked).

    Falls back to the raw signed_move_pct (entry→close) if no targets
    are persisted (old tags before the bot-8 monitor migration). Returns
    (move, staffed): staffed=True only if the staggered path was used.

    `model` is passed through: ROM1 and AIM2 persist more targets than they post
    to Cornix, the staggering must compute on the actually traded leg count
    (T-2026-KYT-9050-012, core.realized_pnl.PUBLISHED_TARGET_COUNT)."""
    if targets:
        m = weighted_move_pct(direction, entry, close, targets, targets_hit, model)
        if m is not None:
            return m, True
    return signed_move_pct(direction, entry, close), False


def r_from_move(move: float | None, entry: object, sl: object) -> float | None:
    """Realized R-multiple = realized move / planned initial risk.

    Risk = |entry − sl| / entry. Only for sl>0 and a valid move. An SL loss
    yields ≈ −1R. `move` is the (possibly staggered) unlevered move from unlev_move."""
    if move is None:
        return None
    try:
        e = float(entry)
        s = float(sl)
    except (TypeError, ValueError):
        return None
    if e <= 0 or s <= 0:
        return None
    risk = abs(e - s) / e * 100.0
    if risk <= 0:
        return None
    return move / risk


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    n = len(ys)
    mid = n // 2
    return ys[mid] if n % 2 else (ys[mid - 1] + ys[mid]) / 2.0


def _std(xs: list[float]) -> float | None:
    """Sample standard deviation (ddof=1)."""
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var)


def aggregate_leg(rows: list[dict]) -> dict:
    """Fold the (already grouped by (tag,dir,bucket)) rows into leg stats.

    Each row: {outcome, move (float|None), staffed (bool), lev_pnl (float|None),
    r (float|None), ts (datetime|None)}. Pure → DB-free testable. `move`/`lev_pnl`/
    `r` apply only to decided (win/loss) rows; legacy/censored are only counted."""
    decided_moves: list[float] = []
    wins = 0
    losses = 0
    lev_pnls: list[float] = []
    r_vals: list[float] = []
    staffed_n = 0
    legacy_n = 0
    censored_n = 0
    other_n = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    for r in rows:
        oc = r["outcome"]
        ts = r.get("ts")
        if ts is not None:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
        if oc == LEGACY:
            legacy_n += 1
            continue
        if oc == CENSORED:
            censored_n += 1
            continue
        if oc not in (WIN, LOSS):
            other_n += 1
            continue
        # decided
        if oc == WIN:
            wins += 1
        else:
            losses += 1
        mv = r.get("move")
        if mv is not None:
            decided_moves.append(float(mv))
            if r.get("staffed"):
                staffed_n += 1
        lp = r.get("lev_pnl")
        if lp is not None:
            lev_pnls.append(float(lp))
        rr = r.get("r")
        if rr is not None:
            r_vals.append(float(rr))

    n_decided = wins + losses
    mean_move = _mean(decided_moves)
    std_move = _std(decided_moves)
    return {
        "n_decided": n_decided,
        "n_priced": len(decided_moves),
        "wins": wins,
        "losses": losses,
        "wr_pct": round(100 * wins / n_decided, 1) if n_decided else None,
        "sum_move_pct": round(sum(decided_moves), 1) if decided_moves else None,
        "mean_move_pct": round(mean_move, 4) if mean_move is not None else None,
        "median_move_pct": round(_median(decided_moves), 4) if decided_moves else None,
        "net_mean_pct": round(mean_move - FEE_RT_PCT, 4) if mean_move is not None else None,
        "sharpe": round(mean_move / std_move, 3) if (mean_move is not None and std_move) else None,
        "t_stat": (
            round(mean_move / (std_move / math.sqrt(len(decided_moves))), 2)
            if (mean_move is not None and std_move and len(decided_moves) > 1)
            else None
        ),
        "staffed_pct": round(100 * staffed_n / len(decided_moves), 0) if decided_moves else None,
        "lev_n": len(lev_pnls),
        "lev_sum_pct": round(sum(lev_pnls), 1) if lev_pnls else None,
        "lev_mean_pct": round(_mean(lev_pnls), 3) if lev_pnls else None,
        "r_n": len(r_vals),
        "r_mean": round(_mean(r_vals), 3) if r_vals else None,
        "r_median": round(_median(r_vals), 3) if r_vals else None,
        "legacy_n": legacy_n,
        "censored_n": censored_n,
        "other_n": other_n,
        "first": first_ts.date().isoformat() if first_ts else None,
        "last": last_ts.date().isoformat() if last_ts else None,
    }


# Minimum decided trades below which a leg's edge sign is not trusted.
THIN_N = 30


def verdict_for(stats: dict) -> str:
    """Edge verdict from the leg stats. PnL (net_mean) primary, WR secondary."""
    n = stats["n_decided"]
    if n == 0:
        # No real outcomes at all — only synthetic/censored history.
        if stats["legacy_n"] or stats["censored_n"]:
            return "SYNTHETIC/CENSORED-ONLY"
        return "NO-DATA"
    net = stats["net_mean_pct"]
    if net is None:
        return "UNPRICED"
    if n < THIN_N:
        return "THIN"
    if net > 0:
        return "KEEP"
    return "RETIRE-CANDIDATE"


def rank_legs(legs: list[dict]) -> list[dict]:
    """Sort legs worst-edge-first (net_mean asc; None/thin sink to the end)."""

    def key(leg: dict):
        net = leg["stats"]["net_mean_pct"]
        has = net is not None
        return (0 if has else 1, net if has else 0.0)

    return sorted(legs, key=key)


# ─────────────────────────────────────────────────────────────────────────────
# LIFECYCLE (shadow_gate + bot_catalog — code-defined, worktree-correct)
# ─────────────────────────────────────────────────────────────────────────────
def lifecycle_bucket(tag: str, direction: str, active_scripts_set: set[str]) -> str:
    """Mirror of 23_market_tracker.realized_lifecycle_bucket (that module is a
    numbered script, awkward to import). Returns
    active|shadow|retired|inactive|unmapped. shadow/retired/silent are
    code-defined (shadow_gate); only LIVE legs face the running-script gate."""
    from core import shadow_gate
    from core.bot_catalog import script_for_tag

    status = shadow_gate.leg_status(tag, direction)
    if status == shadow_gate.RETIRED:
        return "retired"
    if status == shadow_gate.SHADOW:
        return "shadow"
    if status == shadow_gate.SILENT:
        return "retired"  # muted old leg: script runs, but leg posts nothing
    script = script_for_tag(tag)
    if script is None:
        return "unmapped"
    if script not in active_scripts_set:
        return "inactive"
    return "active"


def resolve_active_scripts(parked_dir: str | None) -> set[str]:
    """Fleet scripts minus parked, with the runner expanded into its hosted bots.

    `parked_dir` points at the LIVE checkout's control/parked so the worktree
    sees the true park state; None falls back to the worktree-relative
    process_control.list_parked(). That override is the only reason this is not
    simply bot_catalog.active_scripts() — the hosted-bot expansion itself is NOT
    restated here but taken from core.shadow_scanners.expand_hosted(), the same
    function the catalog uses (T-2026-KYT-9050-133). Without it the LIVE legs
    TSM1/SKW1/XSM1/XSR1 would be reported as "inactive" while the runner scans
    them, because bots 36-39 no longer carry a fleet entry of their own."""
    from core.fleet import FLEET
    from core.shadow_scanners import expand_hosted

    all_scripts = {entry["script"] for entry in FLEET}
    if parked_dir and os.path.isdir(parked_dir):
        parked = {p for p in os.listdir(parked_dir) if os.path.isfile(os.path.join(parked_dir, p))}
    else:
        from core.process_control import list_parked

        parked = list_parked()
    return expand_hosted({s for s in all_scripts if s not in parked}, parked)


# ─────────────────────────────────────────────────────────────────────────────
# DB LOADERS (strict read-only, deduped on the Report-14 survivor key)
# ─────────────────────────────────────────────────────────────────────────────
_AI_DEDUP_KEY = "symbol, model, upper(btrim(direction)), open_time"
_AI_DEDUP_ORDER = f"{_AI_DEDUP_KEY}, close_time ASC NULLS LAST, targets_hit DESC NULLS LAST, status ASC NULLS LAST"
_CLS_DEDUP_KEY = "coin, strategy, upper(btrim(direction)), time"
_CLS_DEDUP_ORDER = f"{_CLS_DEDUP_KEY}, posted ASC NULLS LAST, status DESC NULLS LAST"


def _parse_targets(value) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _classic_targets(t1, t2, t3, t4) -> list:
    out = []
    for t in (t1, t2, t3, t4):
        try:
            f = float(t)
        except (TypeError, ValueError):
            continue
        if f > 0:
            out.append(f)
    return out


def _parse_hits(status) -> int:
    try:
        return int(float(status))
    except (TypeError, ValueError):
        return 0


def load_ai_rows(conn) -> list[dict]:
    """Deduped AI closes → per-leg row dicts (all history)."""
    from core.bot_naming import pretty_name
    from core.realized_pnl import realized_pnl_pct

    sql = f"""
        SELECT model, upper(btrim(direction)) AS direction, entry, close_price,
               targets_hit, targets, lev, status, close_time
        FROM (
            SELECT DISTINCT ON ({_AI_DEDUP_KEY})
                   model, direction, entry, close_price, targets_hit, targets,
                   lev, status, close_time
            FROM closed_ai_signals
            ORDER BY {_AI_DEDUP_ORDER}
        ) d
    """
    out = []
    with conn.cursor() as cur:
        cur.execute(sql)
        for model, direction, entry, close, hit, targets, lev, status, close_time in cur.fetchall():
            oc = classify_ai_outcome(status, hit)
            move = None
            staffed = False
            lev_pnl = None
            if oc in (WIN, LOSS):
                tlist = _parse_targets(targets)
                move, staffed = unlev_move(direction, entry, close, tlist, hit, model)
                lev_pnl = realized_pnl_pct(direction, entry, close, tlist, hit, lev, model)
            out.append(
                {
                    "tag": pretty_name(str(model)),
                    "direction": str(direction),
                    "outcome": oc,
                    "move": move,
                    "staffed": staffed,
                    "lev_pnl": lev_pnl,
                    "r": None,  # closed_ai_signals has no sl column
                    "ts": close_time,
                    "source": "ai",
                }
            )
    return out


def load_classic_rows(conn) -> list[dict]:
    """Deduped classic closes → per-leg row dicts (has sl → R-multiple)."""
    from core.bot_naming import pretty_name
    from core.realized_pnl import realized_pnl_pct

    sql = f"""
        SELECT strategy, upper(btrim(direction)) AS direction, entry, close_price,
               sl, target1, target2, target3, target4, lev, status, time
        FROM (
            SELECT DISTINCT ON ({_CLS_DEDUP_KEY})
                   strategy, direction, entry, close_price, sl,
                   target1, target2, target3, target4, lev, status, time
            FROM closed_trades_master
            ORDER BY {_CLS_DEDUP_ORDER}
        ) d
    """
    out = []
    with conn.cursor() as cur:
        cur.execute(sql)
        for strat, direction, entry, close, sl, t1, t2, t3, t4, lev, status, ts in cur.fetchall():
            oc = classify_classic_outcome(status)
            move = None
            staffed = False
            r = None
            lev_pnl = None
            if oc in (WIN, LOSS):
                tlist = _classic_targets(t1, t2, t3, t4)
                move, staffed = unlev_move(direction, entry, close, tlist, _parse_hits(status))
                r = r_from_move(move, entry, sl)
                lev_pnl = realized_pnl_pct(direction, entry, close, tlist, _parse_hits(status), lev)
            out.append(
                {
                    "tag": pretty_name(str(strat)),
                    "direction": str(direction),
                    "outcome": oc,
                    "move": move,
                    "staffed": staffed,
                    "lev_pnl": lev_pnl,
                    "r": r,
                    "ts": ts,
                    "source": "classic",
                }
            )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# ASSEMBLY
# ─────────────────────────────────────────────────────────────────────────────
def build_legs(rows: list[dict], active_scripts_set: set[str]) -> list[dict]:
    """Group rows by (tag, direction), aggregate, attach lifecycle + verdict."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["tag"], r["direction"]), []).append(r)
    legs = []
    for (tag, direction), grp in groups.items():
        stats = aggregate_leg(grp)
        bucket = lifecycle_bucket(tag, direction, active_scripts_set)
        source = grp[0]["source"]
        legs.append(
            {
                "tag": tag,
                "direction": direction,
                "bucket": bucket,
                "source": source,
                "stats": stats,
                "verdict": verdict_for(stats),
            }
        )
    return legs


# ─────────────────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────────────────
_BUCKET_ORDER = ("active", "shadow", "retired", "inactive", "unmapped")
_BUCKET_TITLE = {
    "active": "🟢 ACTIVE (live posting)",
    "shadow": "👻 SHADOW (tracked, never live)",
    "retired": "🗄 RETIRED / SILENT (old generation)",
    "inactive": "⏸ INACTIVE (live leg, script parked)",
    "unmapped": "❓ UNMAPPED (tag has no bot_catalog family)",
}


def _fmt(v, spec: str = "") -> str:
    if v is None:
        return "—"
    if spec:
        return format(v, spec)
    return str(v)


def _leg_row(leg: dict) -> str:
    s = leg["stats"]
    lev = f"{_fmt(s['lev_sum_pct'], '+.0f')}({s['lev_n']})" if s["lev_n"] else "—"
    r = _fmt(s["r_mean"], "+.2f") if s["r_n"] else "—"
    stf = f"{_fmt(s['staffed_pct'], '.0f')}" if s["staffed_pct"] is not None else "—"
    return (
        f"| {leg['tag']} | {leg['source']} | {_fmt(s['n_decided'])} | {_fmt(s['wr_pct'])} | "
        f"{_fmt(s['mean_move_pct'], '+.3f')} | {_fmt(s['net_mean_pct'], '+.3f')} | "
        f"{_fmt(s['median_move_pct'], '+.3f')} | {stf} | {_fmt(s['sharpe'], '+.2f')} | {_fmt(s['t_stat'], '+.1f')} | "
        f"{r} | {lev} | {_fmt(s['legacy_n'])} | {_fmt(s['censored_n'])} | "
        f"{_fmt(s['first'])}→{_fmt(s['last'])} | {leg['verdict']} |"
    )


_TABLE_HEAD = (
    "| tag | src | n | WR% | mean% | net% | med% | stf% | Sh | t | R̄ | levΣ%(n) | leg | cen | span | verdict |\n"
    "|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|---|"
)


def build_report(meta: dict) -> str:
    L: list[str] = []
    ap = L.append
    ap("# Fleet Realized-Trade Audit (Phase A) — T-2026-KYT-9050-032\n")
    ap(
        f"_generated {meta['generated_at']} · read-only · dedup=Report-14 survivor key · "
        f"AI rows {meta['n_ai_rows']} · classic rows {meta['n_classic_rows']}_\n"
    )
    ap(
        "**Edge metric (`mean%`/`net%`):** unlevered, TARGET-STAGGERED realized move % per "
        "decided (win/loss, NON-legacy/censored) trade (`core.realized_pnl.weighted_move_pct` — the "
        "stake is split evenly across the N targets; that is the correct realized edge for "
        "laddered-TP bots). Where no targets are persisted (old tags), fallback to the raw "
        f"entry→close move; `stf%` = share of staggered trades. `net%` = mean − {FEE_RT_PCT:.2f}% "
        "round-trip fee. **PnL primary, WR secondary** (WR = TP1 touch, R:R counts). LEGACY closes "
        "(synthetic ±2.5%) excluded (`leg` column). `levΣ%(n)` = leveraged realized PnL "
        "(staggered × leverage, clamped at −100%), GROSS (fee not deducted), exact-only where targets+lev "
        f"are persisted (n). `R̄` classic only (closed_ai_signals has no sl). Verdict based on `net%` (n≥{THIN_N}).\n"
    )

    ap(f"## Ranking — retire candidates vs keep (decided n ≥ {THIN_N})\n")
    for direction in ("LONG", "SHORT"):
        keep = [lg for lg in meta["legs"] if lg["direction"] == direction and lg["verdict"] == "KEEP"]
        retire = [lg for lg in meta["legs"] if lg["direction"] == direction and lg["verdict"] == "RETIRE-CANDIDATE"]
        keep = rank_legs(keep)[::-1]  # best first
        retire = rank_legs(retire)  # worst first
        ap(f"### {direction}\n")
        ap(
            "**RETIRE candidates** (net<0): "
            + (
                ", ".join(
                    f"{lg['tag']}[{lg['bucket'][:3]}] {lg['stats']['net_mean_pct']:+.2f}%×{lg['stats']['n_decided']}"
                    for lg in retire
                )
                or "—"
            )
        )
        ap(
            "\n**KEEP** (net>0): "
            + (
                ", ".join(
                    f"{lg['tag']}[{lg['bucket'][:3]}] {lg['stats']['net_mean_pct']:+.2f}%×{lg['stats']['n_decided']}"
                    for lg in keep
                )
                or "—"
            )
            + "\n"
        )

    for bucket in _BUCKET_ORDER:
        legs = [lg for lg in meta["legs"] if lg["bucket"] == bucket]
        if not legs:
            continue
        ap(f"## {_BUCKET_TITLE[bucket]}\n")
        for direction in ("LONG", "SHORT"):
            dlegs = rank_legs([lg for lg in legs if lg["direction"] == direction])
            if not dlegs:
                continue
            ap(f"**{direction}**\n")
            ap(_TABLE_HEAD)
            for lg in dlegs:
                ap(_leg_row(lg))
            ap("")

    ap("## Join limits (honestly)\n")
    for lim in meta["join_limits"]:
        ap(f"- {lim}")
    ap("")
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# DRIVER
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_OUT_DIR = os.getenv("KYTHERA_REPLAY_DIR", os.path.join(REPO_ROOT, "staging_models", "replay"))
_LIVE_PARKED_DEFAULT = r"C:\Users\Michael\Documents\Kythera\control\parked"

_JOIN_LIMITS = [
    "closed_ai_signals has NO sl → R-multiple only for classic (closed_trades_master).",
    "targets+lev are thinly persisted for old tags (bot-8 monitor migration) → leveraged PnL "
    "is exact-only (levΣ n column); the unlevered move metric is the coverage-robust edge basis.",
    "LEGACY closes (±2.5%/-5%) are synthetic migration prices → excluded from the edge; "
    "tags whose history is almost entirely LEGACY (MIS1-*_pump/dump burst 03-01/03-02) have no "
    "measurable realized edge (SYNTHETIC/CENSORED-ONLY).",
    "WR is TP1 touch (can still be net-negative at R:R<1) — that is why net-mean move is the "
    "verdict basis, not WR.",
    "prob↔outcome is only partially joinable in the live DB → outcome via realized trade status.",
    "active-vs-inactive uses control/parked of the LIVE checkout (--parked-dir); shadow/retired/silent "
    "are code-defined (shadow_gate) and correct independent of that.",
    "Monitor-generated outcomes (P1.2/P2.7/P1.9) historically agree only ~63% with a first-touch "
    "replay → the absolute edge magnitude is noisy; sign + cohort comparisons "
    "are the signal.",
]


def main() -> None:
    ap = argparse.ArgumentParser(description="Fleet realized-trade audit (Phase A, T-2026-KYT-9050-032)")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR)
    ap.add_argument(
        "--parked-dir",
        default=_LIVE_PARKED_DEFAULT,
        help="LIVE-checkout control/parked dir (active-vs-inactive); '' -> worktree-relative",
    )
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from core.database import get_db_connection
    from core.time import utc_now
    from tools.walkforward_sim import check_cpu_headroom, set_low_priority

    def soft_headroom() -> None:
        """CPU courtesy, but do NOT hard-abort: phase A is just two
        read-only scans under BELOW_NORMAL — they must complete
        (task directive: phase A takes precedence if the headroom check aborts)."""
        try:
            check_cpu_headroom()
        except SystemExit as e:
            print(f"WARN {e} — phase A runs anyway (read-only, BELOW_NORMAL).", flush=True)

    set_low_priority()
    soft_headroom()

    parked_dir = args.parked_dir or None
    active_scripts_set = resolve_active_scripts(parked_dir)

    conn = get_db_connection()
    try:
        conn.set_session(readonly=True)  # NEVER writes the live DB
    except Exception:
        pass
    try:
        ai_rows = load_ai_rows(conn)
        print(f"AI rows (deduped): {len(ai_rows)}", flush=True)
        soft_headroom()
        classic_rows = load_classic_rows(conn)
        print(f"classic rows (deduped): {len(classic_rows)}", flush=True)
    finally:
        conn.close()

    legs = build_legs(ai_rows + classic_rows, active_scripts_set)

    meta = {
        "task": "T-2026-KYT-9050-032",
        "phase": "A",
        "generated_at": str(utc_now()),
        "n_ai_rows": len(ai_rows),
        "n_classic_rows": len(classic_rows),
        "parked_dir_used": parked_dir if (parked_dir and os.path.isdir(parked_dir)) else None,
        "active_scripts": sorted(active_scripts_set),
        "thin_n": THIN_N,
        "fee_rt_pct": FEE_RT_PCT,
        "legs": legs,
        "join_limits": _JOIN_LIMITS,
    }

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "fleet_realized_audit.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)
    md = build_report(meta)
    with open(os.path.join(args.out, "fleet_realized_audit.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    print(md)
    print(f"\n-> {os.path.join(args.out, 'fleet_realized_audit.json')}")


if __name__ == "__main__":
    main()
