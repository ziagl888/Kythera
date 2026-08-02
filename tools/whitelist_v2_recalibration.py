#!/usr/bin/env python
"""Parameter sensitivity of the v2 whitelist gate, scored on realized ROM1 legs.

T-2026-KYT-9050-007 follow-up. The flip evaluation (PR #239) measured ONE v2
configuration — the one in `27_bot_regime_analyzer` — and found no PnL case for
flipping. The open question was whether v2 is merely mis-tuned. This tool
answers it by re-deciding every gate event under a grid of (z, k, break-even)
and scoring each configuration against the trades ROM1 actually made.

WHAT THIS IS NOT
----------------
**It is not a backtest, and no run of it can become one.** `bot_regime_performance`
is a SNAPSHOT: exactly one row per (bot, regime, alt_context, direction,
window_days), overwritten on every analyzer run — verified, zero duplicate rows
per cell. The per-cell statistics the gate actually decided on at the time of a
past event no longer exist, so a re-decision necessarily uses TODAY's statistics
on YESTERDAY's traffic. That mixes two things: what the parameters do, and how
the cells drifted since.

What survives that limitation is still worth having:

  * the SHAPE of the parameter response (which knob moves the gate at all), and
  * a sign check — whether the traffic a configuration would newly block was
    losing money, or winning it.

Both are read-only, and neither licenses a flip. The only thing that can is a
live shadow A/B, exactly as T-031 concluded for the SOFT regime gate.

The same wall is why the companion task to historise `bot_regime_performance`
matters: one snapshot row per day makes this question answerable in 30 days.

REUSE (one truth, hard rule 7)
------------------------------
Gate semantics, event loading and cell keys come from
`tools/whitelist_v2_flip_eval` (T-069); realized-leg attachment and aggregation
from `tools/whitelist_v2_realized_eval` (T-007) and `tools/fleet_realized_audit`
(T-032); the decision function itself is imported from the live analyzer
`27_bot_regime_analyzer` — this file re-parameterises it, it does not reimplement
it. If the gate changes there, this tool follows automatically.

Usage (read-only, VPS):
    python tools/whitelist_v2_recalibration.py --since 2026-07-11 --out report.md
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from core.bot_naming import pretty_name  # noqa: E402
from core.database import get_db_connection  # noqa: E402
from core.time import utc_now  # noqa: E402
from tools.walkforward_sim import set_low_priority  # noqa: E402
from tools.whitelist_v2_flip_eval import load_gate_events, load_whitelist_snapshot  # noqa: E402
from tools.whitelist_v2_realized_eval import (  # noqa: E402
    TwinIndex,
    attach_rom1_legs,
    load_ai_closes,
    load_classic_closes,
    summarize_legs,
    wait_for_cpu_headroom,
)


def _load_analyzer():
    """Import the live analyzer (a digit-prefixed module) for its gate function."""
    path = os.path.join(_REPO, "27_bot_regime_analyzer.py")
    spec = importlib.util.spec_from_file_location("bot_regime_analyzer", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import plumbing
        raise RuntimeError(f"cannot import {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bot_regime_analyzer"] = mod
    spec.loader.exec_module(mod)
    return mod


# The grid. z dominates by construction (it multiplies the posterior sd), so it
# gets the finest sampling; break-even and the shrinkage prior are carried along
# to show — not assume — how little they move the gate.
DEFAULT_Z = (1.64, 1.28, 1.04, 0.67, 0.0)
DEFAULT_K = (25.0, 10.0, 5.0)
DEFAULT_BREAK_EVEN = (0.1, 0.0, -0.1)


def load_perf_snapshot(conn, window_days: int) -> dict[tuple, dict]:
    """Today's per-cell performance rows, keyed like the whitelist."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bot_name, regime, alt_context, direction,
                   n_trades, win_rate, avg_pnl_pct, pnl_stddev
            FROM bot_regime_performance
            WHERE window_days = %s
            """,
            (window_days,),
        )
        rows = cur.fetchall()
    return {(r[0], r[1], r[2], r[3]): {"n": r[4], "wr": r[5] or 0.0, "avg_pnl": r[6], "std": r[7]} for r in rows}


def snapshot_is_history(conn) -> dict:
    """Prove (not assume) that the performance table carries no history.

    The whole 'not a backtest' caveat rests on this, so it is measured on every
    run and printed with the report rather than asserted in a docstring.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM (
                SELECT 1 FROM bot_regime_performance
                GROUP BY bot_name, regime, alt_context, direction, window_days
                HAVING count(*) > 1
            ) dup
            """
        )
        dup_cells = cur.fetchone()[0]
        cur.execute("SELECT min(last_computed), max(last_computed) FROM bot_regime_performance")
        lo, hi = cur.fetchone()
    return {
        "duplicate_cells": dup_cells,
        "is_snapshot": dup_cells == 0,
        "last_computed_min": lo,
        "last_computed_max": hi,
    }


def decide_cell(analyzer, perf: dict[tuple, dict], bot: str, regime: str, alt: str, direction: str):
    """v2 verdict for one cell under the analyzer's CURRENT module-level params."""
    cell = perf.get((bot, regime, alt, direction), {})
    parent_regime = perf.get((bot, regime, "ALL", direction), {})
    parent_overall = perf.get((bot, "ALL", "ALL", direction), {})
    return analyzer._v2_whitelist_decision(cell, parent_regime, parent_overall)


def apply_config(analyzer, z: float, k: float, break_even: float) -> None:
    analyzer.V2_LOWER_BOUND_Z = z
    analyzer.V2_SHRINKAGE_PSEUDO_COUNT = k
    analyzer.V2_BREAK_EVEN_PNL_PCT = break_even


def score_config(analyzer, perf, forwarded: list[dict], z: float, k: float, break_even: float) -> dict:
    """Split the traffic ROM1 actually traded into keep/block under one config.

    Only the FORWARDED side is scored. The suppressed side has no ROM1 leg by
    construction — the trade was never made, so there is no realized outcome to
    attribute to it. Counting it would invent money that never moved.
    """
    apply_config(analyzer, z, k, break_even)
    kept, blocked, undecidable = [], [], []
    for e in forwarded:
        bot = e.get("bot_name")
        regime, alt, direction = e.get("regime"), e.get("alt_context"), e.get("direction")
        if not bot or not regime or not alt or not direction:
            undecidable.append(e)
            continue
        ok, _reason = decide_cell(analyzer, perf, bot, regime, alt, direction)
        (kept if ok else blocked).append(e)

    n = len(forwarded)
    return {
        "z": z,
        "k": k,
        "break_even": break_even,
        "n_forwarded": n,
        "n_kept": len(kept),
        "n_blocked": len(blocked),
        "n_undecidable": len(undecidable),
        "keep_rate_pct": round(100.0 * len(kept) / n, 2) if n else None,
        "kept": summarize_legs(kept, "rom1"),
        "blocked": summarize_legs(blocked, "rom1"),
    }


def verdict_of(row: dict, baseline: dict) -> str:
    """One-line reading of a configuration against what actually happened.

    The question is not 'is the kept side positive' — it is whether the traffic
    the configuration REMOVES was losing money. A gate that blocks winners is
    worse than no gate however good its remainder looks.
    """
    blocked_mean = (row["blocked"] or {}).get("mean_move_pct")
    kept_mean = (row["kept"] or {}).get("mean_move_pct")
    base_mean = (baseline or {}).get("mean_move_pct")
    if row["n_blocked"] == 0:
        return "no-op (blockt nichts)"
    if blocked_mean is None or kept_mean is None or base_mean is None:
        return "keine Legs — nicht bewertbar"
    if blocked_mean < 0 and kept_mean > base_mean:
        return "entfernt Verlierer"
    if blocked_mean > 0:
        return "entfernt GEWINNER"
    return "kein klares Vorzeichen"


def build_report(meta: dict) -> str:
    L: list[str] = []
    L.append("# v2-Whitelist: Parameter-Sensitivität gegen realisierte ROM1-Beine")
    L.append("")
    L.append(f"- Fenster: `{meta['since']}` → `{meta['until'] or 'jetzt'}`  ({meta['window_days']:.1f} Tage)")
    L.append(f"- Gate-Events: {meta['n_events']} (davon geforwardet: {meta['n_forwarded']})")
    L.append(f"- ROM1-Legs angehängt: {meta['rom1_leg_coverage_pct']}%")
    L.append(f"- Erzeugt: {meta['generated_at']}  |  CPU bei Start: {meta['cpu_at_start_pct']}")
    L.append("")
    hist = meta["snapshot_check"]
    L.append("## Warum das KEIN Backtest ist (gemessen, nicht behauptet)")
    L.append("")
    L.append(
        f"`bot_regime_performance` trägt **{hist['duplicate_cells']}** Zellen mit mehr als einer Zeile "
        f"→ {'reiner Snapshot, keine Historie' if hist['is_snapshot'] else 'ENTHÄLT HISTORIE — Caveat prüfen!'}. "
        f"`last_computed` von `{hist['last_computed_min']}` bis `{hist['last_computed_max']}`."
    )
    L.append("")
    L.append(
        "Die Zellstatistiken, auf denen das Gate zum Zeitpunkt eines vergangenen Events entschieden hat, "
        "existieren nicht mehr. Jede Neu-Entscheidung unten benutzt **heutige** Statistiken auf **damaligem** "
        "Verkehr und vermischt damit zwei Effekte: was die Parameter tun, und wie die Zellen seither gedriftet "
        "sind. Belastbar bleiben die *Form* der Parameter-Antwort und das *Vorzeichen* der geblockten Beine. "
        "Ein Flip lässt sich daraus nicht rechtfertigen — das kann nur ein Live-Shadow-A/B (wie T-031)."
    )
    L.append("")
    base = meta["baseline"]
    L.append("## Referenz: was v1 tatsächlich durchgelassen hat")
    L.append("")
    L.append(
        f"- {base['n_with_leg']} von {base['n_events']} geforwardeten Events mit ROM1-Leg  "
        f"| Σ move {base['sum_move_pct']} %  | Ø {base['mean_move_pct']} %/Trade"
    )
    L.append("")
    L.append("## Parameter-Gitter")
    L.append("")
    L.append("| z | k | break-even | behalten | Durchlass | Ø behalten % | Ø geblockt % | Σ geblockt % | Lesart |")
    L.append("|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for r in meta["grid"]:
        mark = " **(heute)**" if (r["z"], r["k"], r["break_even"]) == meta["live_config"] else ""
        L.append(
            f"| {r['z']}{mark} | {r['k']} | {r['break_even']} | {r['n_kept']}/{r['n_forwarded']} | "
            f"{r['keep_rate_pct']} % | {r['kept'].get('mean_move_pct')} | "
            f"{r['blocked'].get('mean_move_pct')} | {r['blocked'].get('sum_move_pct')} | {r['verdict']} |"
        )
    L.append("")
    L.append("## Grenzen")
    L.append("")
    for line in meta["limits"]:
        L.append(f"- {line}")
    L.append("")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="v2 whitelist parameter sensitivity on realized ROM1 legs")
    ap.add_argument("--since", default=None, help="ISO timestamp (naive UTC); default 21 days back")
    ap.add_argument("--until", default=None, help="ISO timestamp (naive UTC), exclusive")
    ap.add_argument("--rom1-tolerance-sec", type=int, default=90)
    ap.add_argument("--twin-tolerance-sec", type=int, default=90)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--cpu-wait-min", type=int, default=0)
    ap.add_argument("--force-on-busy", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    set_low_priority()
    cpu_at_start = wait_for_cpu_headroom(args.cpu_wait_min, args.force_on_busy)

    since = datetime.fromisoformat(args.since) if args.since else utc_now().replace(tzinfo=None) - timedelta(days=21)
    until = datetime.fromisoformat(args.until) if args.until else None

    analyzer = _load_analyzer()
    live_config = (
        analyzer.V2_LOWER_BOUND_Z,
        analyzer.V2_SHRINKAGE_PSEUDO_COUNT,
        analyzer.V2_BREAK_EVEN_PNL_PCT,
    )

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        snapshot_check = snapshot_is_history(conn)
        perf = load_perf_snapshot(conn, analyzer.REFERENCE_WINDOW_DAYS)
        _ = load_whitelist_snapshot(conn)  # freshness probe, same source as the flip eval
        events = load_gate_events(conn, since, args.limit)
        if until is not None:
            events = [e for e in events if e["ts"] < until]
        for e in events:
            e["tag"] = pretty_name(e["bot_name"] or "")
            e["direction"] = str(e["direction"] or "").strip().upper()
        closes = load_ai_closes(conn, since) + load_classic_closes(conn, since)
    finally:
        conn.close()

    forwarded = [e for e in events if e.get("side") == "forwarded"]
    index = TwinIndex(closes)
    rom1_probe = [{"tag": "ROM1", "coin": e["coin"], "direction": e["direction"], "ts": e["ts"]} for e in forwarded]
    index.calibrate(rom1_probe, args.rom1_tolerance_sec)
    attach_rom1_legs(events, index, args.rom1_tolerance_sec)

    baseline = summarize_legs(forwarded, "rom1")

    grid = []
    for z in DEFAULT_Z:
        for k in DEFAULT_K:
            for be in DEFAULT_BREAK_EVEN:
                row = score_config(analyzer, perf, forwarded, z, k, be)
                row["verdict"] = verdict_of(row, baseline)
                grid.append(row)
    apply_config(analyzer, *live_config)  # leave the imported module as we found it

    window_end = until or utc_now().replace(tzinfo=None)
    limits = [
        "Snapshot statt Historie: heutige Zellstatistiken auf damaligem Verkehr — Parametereffekt und Zell-Drift "
        "sind nicht trennbar (oben gemessen).",
        "Nur die geforwardete Seite ist gescort. Unterdrückte Signale haben per Konstruktion kein ROM1-Bein — "
        "ihr Ausgang ist nicht beobachtet, nicht null.",
        "Events ohne angehängtes Bein zählen als `n_no_leg` und werden nie als 0 verrechnet.",
        "Kein Ergebnis dieses Laufs rechtfertigt einen Gate-Flip. Entscheidbar wird die Frage erst über ein "
        "Live-Shadow-A/B oder nachdem bot_regime_performance historisiert ist.",
    ]
    meta = {
        "since": str(since),
        "until": str(until) if until else None,
        "generated_at": str(utc_now()),
        "window_days": max((window_end - since).total_seconds() / 86400.0, 0.0),
        "cpu_at_start_pct": cpu_at_start,
        "n_events": len(events),
        "n_forwarded": len(forwarded),
        "rom1_leg_coverage_pct": baseline.get("leg_coverage_pct"),
        "snapshot_check": {
            **snapshot_check,
            "last_computed_min": str(snapshot_check["last_computed_min"]),
            "last_computed_max": str(snapshot_check["last_computed_max"]),
        },
        "live_config": live_config,
        "baseline": baseline,
        "grid": grid,
        "limits": limits,
    }

    report = build_report(meta)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report)
        with open(os.path.splitext(args.out)[0] + "_summary.json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, default=str)
        print(f"\ngeschrieben: {args.out}")


if __name__ == "__main__":
    main()
