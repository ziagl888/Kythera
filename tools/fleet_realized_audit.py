"""tools/fleet_realized_audit.py — Fleet-weiter Realized-Trade-Audit (Phase A, T-2026-KYT-9050-032).

Zweck
-----
Reviewbare Kontroll-Tabelle des realisierten Edge JEDES Bots DIREKT AUS DER DB
(kein eigenes Backtesting hier), pro **Tag × Richtung (LONG/SHORT) × Lifecycle
(active/shadow/retired/inactive)**. Ranking → Retire-Kandidaten (negativer
realisierter Edge) vs Keep (positiv). Reine ANALYSE + EMPFEHLUNG — kein
Live-Eingriff (harte Regel 1/2, Retire = Michi-Eskalation).

Datenquellen (Live-DB, strikt read-only)
-----------------------------------------
  * `closed_ai_signals` (model = Tag): AI-Bots. KEINE brauchbaren Indizes,
    357k-Duplikat-Falle (LEGACY-Re-Close-Event) → DISTINCT ON dem Report-14-
    Survivor-Key (symbol, model, dir, open_time), earliest close. Keine `sl`-
    Spalte → R-Multiple hier nicht rekonstruierbar.
  * `closed_trades_master` (strategy = Tag): klassische Detektoren (3_detectors).
    Trägt `sl` → R-Multiple hier verfügbar. close_price<=0-Ära vor 2026-03 (v1)
    fällt über den entry/close>0-Filter raus.

Outcome-Klassifikation
----------------------
  * LEGACY-Rows (status enthält "LEGACY") sind SYNTHETISCH (fixe ±2.5%/-5%
    Close-Preise aus der Feb/März-Migration) → aus dem realisierten Edge
    AUSGESCHLOSSEN und separat als `legacy_n` ausgewiesen; ihre PnL-Magnitude
    ist bedeutungslos (WR nur aus dem Status-Text).
  * Zensiert (DELISTED/CLEANUP/REGIME_CHANGE/FORCE_CLOSED): extern verursachte
    Closes, weder Win noch Loss — aus WR + Edge raus, als `censored_n` gezählt.
  * Win = TP1 berührt (AI: targets_hit>=1 oder "ALL TARGETS"; classic: status
    1..4/SL1..3). Loss = SL0/kein Target. WR ist TP1-Touch → sekundär; **PnL
    (Preis-Move) ist primär** (R:R zählt, WR allein irreführend).

Metriken pro Leg (nur NICHT-legacy, NICHT-censored „decided"-Trades)
--------------------------------------------------------------------
  n, WR%, unlevered Move% (sum/mean/median), Net-Mean (− Fee), Sharpe
  (mean/std, NICHT annualisiert), t-Stat (mean/(std/√n)); leveraged Realized-PnL
  (sum/mean) exact-only wo targets+lev persistiert; R-Multiple (mean/median,
  nur classic mit sl>0); Zeitspanne first/last.

Join-Grenzen (ehrlich)
----------------------
  * closed_ai_signals hat KEIN sl → kein R-Multiple für AI-Bots.
  * targets+lev sind für Alt-Tags dünn persistiert (Bot-8-Monitor-Migration) →
    leveraged PnL ist exact-only, Coverage pro Leg ausgewiesen; unlevered Move
    ist die Coverage-robuste Edge-Metrik.
  * prob↔outcome in der Live-DB nur eingeschränkt joinbar → Outcome via realized
    Trade-`status`, nicht via prob.
  * active-vs-inactive hängt an control/parked-Markern des LIVE-Checkouts; im
    Worktree ist der Park-Zustand ggf. nicht sichtbar → über --parked-dir
    steuerbar, sonst als Limit vermerkt. shadow/retired/silent sind
    code-definiert (shadow_gate) und im Worktree korrekt.

Betriebsregeln (Live-VPS!)
--------------------------
  DB strikt read-only, BELOW_NORMAL-Priorität, CPU-Headroom-Check. Keine Tabelle
  wird geschrieben. Ergebnisse nach KYTHERA_REPLAY_DIR (JSON + Markdown).
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
# PURE HELPERS (DB-frei, in backtest/test_fleet_realized_audit.py gepinnt)
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
    """AI-Row → Outcome-Klasse. LEGACY zuerst (synthetisch), dann Zensur, dann
    Win (TP1-Touch), dann Loss/Timeout/Unfilled."""
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
    """closed_trades_master.status → Outcome-Klasse."""
    s = str(status or "").strip().upper()
    if any(frag in s for frag in _CENSOR_FRAGMENTS):
        return CENSORED
    if s in _CLASSIC_WIN:
        return WIN
    if s in _CLASSIC_LOSS:
        return LOSS
    return OTHER


def signed_move_pct(direction: object, entry: object, close: object) -> float | None:
    """Direction-korrigierter unlevered Preis-Move in % (LONG +, SHORT −).

    None bei ungültigen Preisen/Richtung oder Move über MAX_ABS_MOVE_PCT (Bug)."""
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
    direction: object, entry: object, close: object, targets: list, targets_hit: object
) -> tuple[float | None, bool]:
    """Unlevered realisierter Move in % + staffed-Flag.

    Bevorzugt den TARGET-GESTAFFELTEN Move (`core.realized_pnl.weighted_move_pct`,
    die kanonische Fleet-Realized-Definition, T-115): der Einsatz wird gleich auf
    die N Targets verteilt, k getroffene Targets realisieren bei ihrem Preis, der
    Rest schließt bei close_price. Das ist der KORREKTE realisierte Edge für
    laddered-TP-Bots — der rohe entry→close-Move UNTERSCHÄTZT einen Gewinner, der
    TP1/TP2 bucht und auf dem Rest zum SL zurückläuft (close=SL, aber 2/4 gebucht).

    Fallback auf den rohen signed_move_pct (entry→close), wenn keine Targets
    persistiert sind (Alt-Tags vor der Bot-8-Monitor-Migration). Rückgabe
    (move, staffed): staffed=True nur, wenn der gestaffelte Pfad genutzt wurde."""
    if targets:
        m = weighted_move_pct(direction, entry, close, targets, targets_hit)
        if m is not None:
            return m, True
    return signed_move_pct(direction, entry, close), False


def r_from_move(move: float | None, entry: object, sl: object) -> float | None:
    """Realisierter R-Multiple = realisierter Move / geplantes Anfangsrisiko.

    Risiko = |entry − sl| / entry. Nur bei sl>0 und gültigem Move. Ein SL-Loss
    ergibt ≈ −1R. `move` ist der (ggf. gestaffelte) unlevered Move aus unlev_move."""
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
    """Falte die (schon nach (tag,dir,bucket) gruppierten) Rows zu Leg-Stats.

    Jede Row: {outcome, move (float|None), staffed (bool), lev_pnl (float|None),
    r (float|None), ts (datetime|None)}. Pure → DB-frei testbar. `move`/`lev_pnl`/
    `r` gelten nur für decided (win/loss) Rows; legacy/censored werden nur gezählt."""
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
    """Edge-Verdikt aus den Leg-Stats. PnL (net_mean) primär, WR sekundär."""
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
    """Fleet scripts minus parked. `parked_dir` points at the LIVE checkout's
    control/parked so the worktree sees the true park state; None falls back to
    the worktree-relative process_control.list_parked()."""
    from core.fleet import FLEET

    all_scripts = {entry["script"] for entry in FLEET}
    if parked_dir and os.path.isdir(parked_dir):
        parked = {p for p in os.listdir(parked_dir) if os.path.isfile(os.path.join(parked_dir, p))}
    else:
        from core.process_control import list_parked

        parked = list_parked()
    return {s for s in all_scripts if s not in parked}


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
                move, staffed = unlev_move(direction, entry, close, tlist, hit)
                lev_pnl = realized_pnl_pct(direction, entry, close, tlist, hit, lev)
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
        "**Edge-Metrik (`mean%`/`net%`):** unlevered, TARGET-GESTAFFELTER realisierter Move % pro "
        "decided (Win/Loss, NICHT-legacy/censored) Trade (`core.realized_pnl.weighted_move_pct` — der "
        "Einsatz wird gleich auf die N Targets verteilt; das ist der korrekte realisierte Edge für "
        "laddered-TP-Bots). Wo keine Targets persistiert sind (Alt-Tags), Fallback auf rohen "
        f"entry→close-Move; `stf%` = Anteil gestaffelter Trades. `net%` = mean − {FEE_RT_PCT:.2f}% "
        "Round-Trip-Fee. **PnL primär, WR sekundär** (WR = TP1-Touch, R:R zählt). LEGACY-Closes "
        "(synthetische ±2.5%) ausgeschlossen (`leg`-Spalte). `levΣ%(n)` = leveraged realized PnL "
        "(gestaffelt × Hebel, −100% geclampt), GROSS (Fee nicht abgezogen), exact-only wo targets+lev "
        f"persistiert (n). `R̄` nur classic (closed_ai_signals hat kein sl). Verdikt auf `net%` (n≥{THIN_N}).\n"
    )

    ap(f"## Ranking — Retire-Kandidaten vs Keep (decided n ≥ {THIN_N})\n")
    for direction in ("LONG", "SHORT"):
        keep = [lg for lg in meta["legs"] if lg["direction"] == direction and lg["verdict"] == "KEEP"]
        retire = [lg for lg in meta["legs"] if lg["direction"] == direction and lg["verdict"] == "RETIRE-CANDIDATE"]
        keep = rank_legs(keep)[::-1]  # best first
        retire = rank_legs(retire)  # worst first
        ap(f"### {direction}\n")
        ap(
            "**RETIRE-Kandidaten** (net<0): "
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

    ap("## Join-Grenzen (ehrlich)\n")
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
    "closed_ai_signals hat KEIN sl → R-Multiple nur für classic (closed_trades_master).",
    "targets+lev sind für Alt-Tags dünn persistiert (Bot-8-Monitor-Migration) → leveraged PnL "
    "ist exact-only (levΣ n-Spalte); die unlevered Move-Metrik ist die Coverage-robuste Edge-Basis.",
    "LEGACY-Closes (±2.5%/-5%) sind synthetische Migrations-Preise → aus dem Edge ausgeschlossen; "
    "Tags, deren Historie fast nur LEGACY ist (MIS1-*_pump/dump-Burst 03-01/03-02), haben keinen "
    "messbaren realisierten Edge (SYNTHETIC/CENSORED-ONLY).",
    "WR ist TP1-Touch (kann bei R:R<1 trotzdem netto-negativ sein) — deshalb ist net-mean-Move die "
    "Verdikt-Basis, nicht WR.",
    "prob↔outcome in der Live-DB nur eingeschränkt joinbar → Outcome via realized Trade-status.",
    "active-vs-inactive nutzt control/parked des LIVE-Checkouts (--parked-dir); shadow/retired/silent "
    "sind code-definiert (shadow_gate) und unabhängig davon korrekt.",
    "Monitor-generierte Outcomes (P1.2/P2.7/P1.9) stimmen historisch nur ~63% mit einem First-Touch-"
    "Replay überein → die absolute Edge-Höhe ist rauschbehaftet; Vorzeichen + Kohorten-Vergleiche "
    "sind das Signal.",
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
        """CPU-Höflichkeit, aber NICHT hart abbrechen: Phase A sind nur zwei
        read-only Scans unter BELOW_NORMAL — sie müssen vollständig werden
        (Task-Vorgabe: Phase A hat Vorrang, wenn der Headroom-Check abbricht)."""
        try:
            check_cpu_headroom()
        except SystemExit as e:
            print(f"WARN {e} — Phase A läuft dennoch (read-only, BELOW_NORMAL).", flush=True)

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
