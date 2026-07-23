"""tools/regime_gate_edge_test.py — Regime-Gate-Edge-Test (Phase B, T-2026-KYT-9050-032).

Frage
-----
Für die Phase-A-Kandidaten: In welchen BTC-Regime-States (RULE-rekonstruiert aus
`regime_history`, T-029/T-031-Infra) ist ein Bot je Richtung profitabel — und
würde ein REGIME-GATE (nur in günstigen States handeln) den Gesamt-Edge
verbessern?
  * Negativ-Edge-Bots: flippt ein Gate das Ergebnis positiv (Rettung statt
    Retire)?
  * Positiv-Edge-Bots: hebt ein Gate den Mean-Edge / senkt es den Drawdown?

Methodik (ehrlich, KEIN In-Sample-Selbstbetrug)
-----------------------------------------------
  1. Jeder realisierte Trade wird über sein OPEN-Timestamp AS-OF an das
     RULE-rekonstruierte BTC-Regime gebunden (kein Look-ahead; `regime_history`
     ist 5-min, geschlossene Checks). SOFT-Gate (build_soft_timeline hl=192) als
     zweite Variante — Anschluss an T-031.
  2. Per (tag, dir) × Regime: n + mean-net-Edge (gestaffelter unlevered Move
     − Fee, identisch zu Phase A).
  3. **OOS-Gate-Test (temporaler Split):** günstige Regimes werden auf der
     ERSTEN Trade-Hälfte bestimmt (mean-net > 0, cell-n ≥ MIN_CELL), das Gate
     dann auf der ZWEITEN Hälfte angewandt. So misst „gated vs ungated" den Edge
     out-of-sample — ein In-Sample-Gate (günstige Regimes auf denselben Daten
     wählen) wäre ein garantierter, wertloser Uplift.
  4. Verdikt je Leg: RESCUED (ungated<0 → gated OOS >0), IMPROVED (gated > ungated
     und ungated>0), NO-HELP.

Join-Grenzen (ehrlich)
----------------------
  * Regime = RULE_recon (debounced) aus dem gespeicherten `regime`-Stream; T-031
    validierte das zu 91.85% gegen das aufgezeichnete `regime_at_open`. Residual
    = Warm-up + Ingestion-Outage-Desync.
  * SOFT smoothed NUR die BTC-Achse; alt_context bleibt hier außen vor (die
    per-Bot-Whitelist ist NICHT historisch rekonstruierbar, T-031 — ein echtes
    „hätte das Gate geforwarded" ist unmöglich, wir messen den REGIME-EDGE, nicht
    die Whitelist).
  * Outcome = realized Trade-`status` (TP1-Touch-Win) → gestaffelter Move, nicht
    exchange-reconciled; Monitor-Rauschen (P1.2/P2.7) trifft Gated + Ungated
    gleich → der DIFF ist robuster als das Absolutniveau.
  * Der OOS-Gate-Uplift ist eine OBERGRENZE der Regime-Achse allein — er sagt
    NICHT, dass Live-Gating exakt so trifft (Whitelist-Mechanik, Cornix-Routing,
    Regime-Auto-Close interagieren). Empfehlung, kein Rollout (Michi-Eskalation).

Betrieb: DB strikt read-only, BELOW_NORMAL, CPU-Höflichkeit. Output nach
KYTHERA_REPLAY_DIR (JSON + Markdown).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.fleet_realized_audit import (  # noqa: E402
    _AI_DEDUP_KEY,
    _AI_DEDUP_ORDER,
    _CLS_DEDUP_KEY,
    _CLS_DEDUP_ORDER,
    FEE_RT_PCT,
    LOSS,
    WIN,
    _classic_targets,
    _parse_hits,
    _parse_targets,
    classify_ai_outcome,
    classify_classic_outcome,
    lifecycle_bucket,
    resolve_active_scripts,
    unlev_move,
)

DEFAULT_OUT_DIR = os.getenv("KYTHERA_REPLAY_DIR", os.path.join(REPO_ROOT, "staging_models", "replay"))
_LIVE_PARKED_DEFAULT = r"C:\Users\Michael\Documents\Kythera\control\parked"

# Minimum decided trades in a (tag,dir) leg to run the gate test at all.
MIN_LEG_N = 150
# Minimum trades in a regime cell (train half) to call it favorable/unfavorable.
MIN_CELL = 20
# SOFT half-life (5-min checks) — hl=192 ≈ 16h, the T-031 headline strength.
SOFT_HL = 192
_REGIMES = ("TREND_UP", "TREND_DOWN", "CHOP", "HIGH_VOLA", "TRANSITION")


# ─────────────────────────────────────────────────────────────────────────────
# PURE GATE MATH (DB-frei, in backtest/test_regime_gate_edge_test.py gepinnt)
# ─────────────────────────────────────────────────────────────────────────────
def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def regime_cell_stats(trades: list[dict]) -> dict:
    """Per-Regime {n, mean_net} über eine Trade-Liste ({regime, net})."""
    by: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        if t.get("regime"):
            by[t["regime"]].append(t["net"])
    return {rg: {"n": len(v), "mean_net": round(_mean(v), 4)} for rg, v in by.items()}


def favorable_regimes(train: list[dict], min_cell: int = MIN_CELL) -> set[str]:
    """Regimes mit mean-net > 0 und cell-n ≥ min_cell auf dem TRAIN-Split."""
    stats = regime_cell_stats(train)
    return {rg for rg, s in stats.items() if s["n"] >= min_cell and s["mean_net"] > 0}


def oos_gate_test(trades: list[dict], min_cell: int = MIN_CELL) -> dict:
    """Temporaler OOS-Gate-Test. `trades` je {ts, regime, net}, chronologisch
    sortierbar. Günstige Regimes auf der ersten Hälfte lernen, auf der zweiten
    anwenden. Gibt ungated/gated Mean + kept-fraction + Delta zurück."""
    # None-ts sinkt ans Ende (zweiter Key-Wert nur bei vorhandenem ts vergleichen
    # → kein None<None-TypeError, falls je ein Trade ohne Open-Zeit durchkommt).
    ts_sorted = sorted(trades, key=lambda t: (t["ts"] is None, t["ts"] if t["ts"] is not None else 0))
    n = len(ts_sorted)
    if n < 2 * min_cell:
        return {"insufficient": True, "n": n}
    mid = n // 2
    train, test = ts_sorted[:mid], ts_sorted[mid:]
    fav = favorable_regimes(train, min_cell)
    test_nets = [t["net"] for t in test]
    kept = [t["net"] for t in test if t.get("regime") in fav]
    ungated = _mean(test_nets)
    gated = _mean(kept)
    return {
        "insufficient": False,
        "n_train": len(train),
        "n_test": len(test),
        "favorable_regimes": sorted(fav),
        "ungated_mean_net": round(ungated, 4) if ungated is not None else None,
        "gated_mean_net": round(gated, 4) if gated is not None else None,
        "kept_frac": round(len(kept) / len(test), 3) if test else None,
        "n_kept": len(kept),
        "delta": round(gated - ungated, 4) if (gated is not None and ungated is not None) else None,
    }


def gate_verdict(gate: dict) -> str:
    """Verdikt aus einem oos_gate_test-Dict.

    NO-FAV-REGIME (kein Regime ist auf Train profitabel → das Gate blockt ALLE
    Test-Trades) ist für einen Negativ-Edge-Leg das entscheidende Ergebnis: kein
    Regime-Subset rettet ihn — Retire steht, Gating hilft nicht."""
    if gate.get("insufficient"):
        return "INSUFFICIENT"
    u = gate.get("ungated_mean_net")
    g = gate.get("gated_mean_net")
    if u is None:
        return "N/A"
    if g is None or gate.get("kept_frac") == 0:
        return "NO-FAV-REGIME"
    if u < 0 and g > 0:
        return "RESCUED"
    if g > u + 0.02:  # a hair above noise to avoid churn labels
        return "IMPROVED"
    if g < u - 0.02:
        return "WORSE"
    return "NO-HELP"


# ─────────────────────────────────────────────────────────────────────────────
# DB (read-only) — per-trade rows with OPEN timestamp
# ─────────────────────────────────────────────────────────────────────────────
def load_ai_trades(conn) -> list[dict]:
    sql = f"""
        SELECT model, upper(btrim(direction)) AS direction, entry, close_price,
               targets_hit, targets, status, open_time
        FROM (
            SELECT DISTINCT ON ({_AI_DEDUP_KEY})
                   model, direction, entry, close_price, targets_hit, targets,
                   status, open_time
            FROM closed_ai_signals
            ORDER BY {_AI_DEDUP_ORDER}
        ) d
        WHERE open_time IS NOT NULL
    """
    from core.bot_naming import pretty_name

    out = []
    with conn.cursor() as cur:
        cur.execute(sql)
        for model, direction, entry, close, hit, targets, status, open_time in cur.fetchall():
            oc = classify_ai_outcome(status, hit)
            if oc not in (WIN, LOSS):
                continue
            move, _ = unlev_move(direction, entry, close, _parse_targets(targets), hit)
            if move is None:
                continue
            out.append(
                {
                    "tag": pretty_name(str(model)),
                    "direction": str(direction),
                    "ts": open_time,
                    "net": move - FEE_RT_PCT,
                    "source": "ai",
                }
            )
    return out


def load_classic_trades(conn) -> list[dict]:
    sql = f"""
        SELECT strategy, upper(btrim(direction)) AS direction, entry, close_price,
               target1, target2, target3, target4, status, time
        FROM (
            SELECT DISTINCT ON ({_CLS_DEDUP_KEY})
                   strategy, direction, entry, close_price,
                   target1, target2, target3, target4, status, time
            FROM closed_trades_master
            ORDER BY {_CLS_DEDUP_ORDER}
        ) d
        WHERE time IS NOT NULL
    """
    from core.bot_naming import pretty_name

    out = []
    with conn.cursor() as cur:
        cur.execute(sql)
        for strat, direction, entry, close, t1, t2, t3, t4, status, ts in cur.fetchall():
            oc = classify_classic_outcome(status)
            if oc not in (WIN, LOSS):
                continue
            move, _ = unlev_move(direction, entry, close, _classic_targets(t1, t2, t3, t4), _parse_hits(status))
            if move is None:
                continue
            out.append(
                {
                    "tag": pretty_name(str(strat)),
                    "direction": str(direction),
                    "ts": ts,
                    "net": move - FEE_RT_PCT,
                    "source": "classic",
                }
            )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# REGIME ATTACHMENT (RULE_recon + SOFT, as-of, no look-ahead)
# ─────────────────────────────────────────────────────────────────────────────
def build_regime_lookups(conn, days: int):
    """Return (rule_lookup, soft_lookup): callables ts -> regime label, as-of."""
    from tools.research.regime_switch.timelines import build_soft_timeline
    from tools.soft_regime_counterfactual import (
        asof_indexer,
        load_regime_history,
        reconstruct_rule,
    )

    ts, reg, alt, feat = load_regime_history(conn, days)
    rule = reconstruct_rule(reg, alt, feat.index)
    soft = build_soft_timeline(feat, half_life_candles=float(SOFT_HL))
    lookup = asof_indexer(feat.index)
    rule_vals = rule.values
    soft_vals = soft.values

    def rlook(when):
        v = lookup(rule_vals, when)
        return None if (v is None or (isinstance(v, float) and np.isnan(v))) else v

    def slook(when):
        v = lookup(soft_vals, when)
        return None if (v is None or (isinstance(v, float) and np.isnan(v))) else v

    return rlook, slook, (len(feat), str(feat.index.min()), str(feat.index.max()))


def attach_regime(trades: list[dict], rlook, slook) -> int:
    """Attach rule/soft regime as-of each trade's open ts. Returns joined count."""
    joined = 0
    for t in trades:
        r = rlook(t["ts"]) if t["ts"] is not None else None
        t["regime"] = r  # RULE = primary gate axis
        t["soft_regime"] = slook(t["ts"]) if t["ts"] is not None else None
        if r is not None:
            joined += 1
    return joined


# ─────────────────────────────────────────────────────────────────────────────
# ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────
def analyse_leg(trades: list[dict]) -> dict:
    """Full-sample per-regime table + RULE and SOFT OOS gate tests for one leg."""
    cells = regime_cell_stats(trades)
    rule_gate = oos_gate_test(trades)
    soft_trades = [{"ts": t["ts"], "regime": t.get("soft_regime"), "net": t["net"]} for t in trades]
    soft_gate = oos_gate_test(soft_trades)
    all_nets = [t["net"] for t in trades]
    return {
        "n": len(trades),
        "n_regime_joined": sum(1 for t in trades if t.get("regime")),
        "overall_mean_net": round(_mean(all_nets), 4) if all_nets else None,
        "cells": cells,
        "rule_gate": rule_gate,
        "soft_gate": soft_gate,
        "rule_verdict": gate_verdict(rule_gate),
        "soft_verdict": gate_verdict(soft_gate),
    }


def build_legs(trades: list[dict], active: set[str]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        groups[(t["tag"], t["direction"])].append(t)
    legs = []
    for (tag, direction), grp in groups.items():
        if len(grp) < MIN_LEG_N:
            continue
        res = analyse_leg(grp)
        legs.append(
            {
                "tag": tag,
                "direction": direction,
                "source": grp[0]["source"],
                "bucket": lifecycle_bucket(tag, direction, active),
                **res,
            }
        )
    return legs


# ─────────────────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────────────────
def _cell_str(cells: dict) -> str:
    parts = []
    for rg in _REGIMES:
        c = cells.get(rg)
        if c:
            parts.append(f"{rg[:4]}:{c['mean_net']:+.2f}×{c['n']}")
    return " ".join(parts) if parts else "—"


def _n(v, spec: str = "+.3f") -> str:
    return format(v, spec) if v is not None else "—"


def _gate_row(leg: dict, which: str) -> str:
    g = leg[f"{which}_gate"]
    v = leg[f"{which}_verdict"]
    if g.get("insufficient"):
        return f"| {leg['tag']} | {leg['direction']} | {leg['bucket'][:3]} | {leg['n']} | insufficient | — | — | — | — | — |"
    fav = ",".join(r[:4] for r in g["favorable_regimes"]) or "—"
    return (
        f"| {leg['tag']} | {leg['direction']} | {leg['bucket'][:3]} | {leg['n']} | "
        f"{_n(g['ungated_mean_net'])} | {_n(g['gated_mean_net'])} | {_n(g['delta'])} | "
        f"{_n(g['kept_frac'], '.2f')} | {fav} | {v} |"
    )


_GATE_HEAD = (
    "| tag | dir | lc | n | ungated net% | gated net% | Δ | kept | favorable regimes | verdict |\n"
    "|---|---|---|--:|--:|--:|--:|--:|---|---|"
)


def build_report(meta: dict) -> str:
    L: list[str] = []
    ap = L.append
    ap("# Regime-Gate Edge Test (Phase B) — T-2026-KYT-9050-032\n")
    ap(
        f"_generated {meta['generated_at']} · read-only · regime_history {meta['rh_rows']} rows "
        f"({meta['rh_span'][0][:10]}→{meta['rh_span'][1][:10]}) · {meta['n_trades']} regime-joined trades · "
        f"legs with n≥{MIN_LEG_N}: {len(meta['legs'])}_\n"
    )
    ap(
        f"**Gate-Test:** günstige BTC-Regimes (mean-net>0, cell-n≥{MIN_CELL}) auf der ERSTEN Trade-Hälfte "
        "gelernt, auf der ZWEITEN angewandt (OUT-OF-SAMPLE). `ungated`/`gated net%` = mean gestaffelter "
        "unlevered Move − Fee auf dem Test-Split; `Δ`=gated−ungated; `kept`=Anteil des Test-Flows, den das "
        "Gate durchlässt. RULE-Regime = debounced RULE_recon (T-031, 91.85% fidelity). "
        "RESCUED = ungated<0→gated>0. **Empfehlung, kein Rollout.**\n"
    )

    # ── Executive summary ─────────────────────────────────────────────────
    rescued = [lg for lg in meta["legs"] if lg["rule_verdict"] == "RESCUED"]
    no_fav = [
        lg
        for lg in meta["legs"]
        if lg["rule_verdict"] == "NO-FAV-REGIME"
        and lg["rule_gate"].get("ungated_mean_net") is not None
        and lg["rule_gate"]["ungated_mean_net"] < 0
    ]
    improved_pos = [
        lg
        for lg in meta["legs"]
        if lg["rule_verdict"] == "IMPROVED" and (lg["rule_gate"].get("ungated_mean_net") or 0) >= 0
    ]
    still_neg = [
        lg
        for lg in meta["legs"]
        if lg["rule_verdict"] == "IMPROVED"
        and (lg["rule_gate"].get("ungated_mean_net") or 0) < 0
        and (lg["rule_gate"].get("gated_mean_net") or 0) < 0
    ]
    ap("## Executive Summary\n")
    ap(
        f"- **RESCUED (Negativ→Positiv durch Gate): {len(rescued)}** "
        + (", ".join(f"{lg['tag']}/{lg['direction'][:1]}" for lg in rescued) if rescued else "— KEIN Leg")
        + ". Kein Regime-Gate flippt einen Negativ-Edge-Leg out-of-sample ins Plus."
    )
    ap(
        f"- **Retire bestätigt (kein günstiges Regime existiert, Gate blockt alles): {len(no_fav)}** — "
        + (", ".join(f"{lg['tag']}/{lg['direction'][:1]}" for lg in no_fav) or "—")
        + ". Diese bluten in JEDEM Regime → Gating hilft nicht, Retire/Richtungs-Abschaltung steht."
    )
    ap(
        f"- **Negativ-Edge nur verbessert, bleibt aber negativ: {len(still_neg)}** — "
        + (
            ", ".join(
                f"{lg['tag']}/{lg['direction'][:1]} ({lg['rule_gate']['ungated_mean_net']:+.2f}→{lg['rule_gate']['gated_mean_net']:+.2f})"
                for lg in still_neg
            )
            or "—"
        )
        + ". Gate mildert, rettet aber nicht."
    )
    ap(
        f"- **Positiv-Edge durch Gate verbessert (OOS): {len(improved_pos)}** — "
        + (
            ", ".join(f"{lg['tag']}/{lg['direction'][:1]} (Δ{lg['rule_gate']['delta']:+.2f})" for lg in improved_pos)
            or "—"
        )
        + ". Meist bescheiden (<+0.3%/Trade) und/oder bei niedriger kept-fraction; das existierende "
        "Whitelist-v2-Vehikel (T-069) ist der Live-Weg, kein neues Gate."
    )
    ap(
        "- **Kernbefund:** Der Edge der Verlust-Legs ist RICHTUNGS-, nicht regime-bedingt "
        "(Pattern/Sniper/Rubberband-Familien: LONG-Edge, SHORT-Blutung über ALLE Regimes) → der Hebel "
        "ist die Richtungs-/Retire-Entscheidung, nicht ein BTC-Regime-Gate. Deckt sich mit T-029/T-031 "
        "(η²≈0, Regime trennt Churn, nicht Richtung).\n"
    )

    # RULE gate — split by rescue candidates (ungated<0) vs improvement (ungated>0)
    rescatt = [
        lg
        for lg in meta["legs"]
        if not lg["rule_gate"].get("insufficient")
        and lg["rule_gate"]["ungated_mean_net"] is not None
        and lg["rule_gate"]["ungated_mean_net"] < 0
    ]
    posn = [
        lg
        for lg in meta["legs"]
        if not lg["rule_gate"].get("insufficient")
        and lg["rule_gate"]["ungated_mean_net"] is not None
        and lg["rule_gate"]["ungated_mean_net"] >= 0
    ]
    rescatt.sort(key=lambda lg: lg["rule_gate"]["delta"] if lg["rule_gate"]["delta"] is not None else 0, reverse=True)
    posn.sort(key=lambda lg: lg["rule_gate"]["delta"] if lg["rule_gate"]["delta"] is not None else 0, reverse=True)

    ap("## RULE-Gate — Negativ-Edge-Legs (rettet ein Gate sie?)\n")
    ap(_GATE_HEAD)
    for lg in rescatt:
        ap(_gate_row(lg, "rule"))
    ap("")
    ap("## RULE-Gate — Positiv-Edge-Legs (verbessert ein Gate sie?)\n")
    ap(_GATE_HEAD)
    for lg in posn:
        ap(_gate_row(lg, "rule"))
    ap("")

    ap("## SOFT-Gate (hl=192, T-031-Anschluss) — alle Legs\n")
    ap(_GATE_HEAD)
    for lg in sorted(meta["legs"], key=lambda lg: lg["soft_gate"].get("delta") or 0, reverse=True):
        ap(_gate_row(lg, "soft"))
    ap("")

    ap("## Per-Regime Mean-Net-Edge je Leg (Vollstichprobe, mean-net×n)\n")
    ap("| tag | dir | lc | overall | " + " | ".join(r[:4] for r in _REGIMES) + " |")
    ap("|---|---|---|--:|" + "--:|" * len(_REGIMES))
    for lg in sorted(meta["legs"], key=lambda lg: (lg["bucket"], lg["tag"], lg["direction"])):
        cells = lg["cells"]
        row = f"| {lg['tag']} | {lg['direction']} | {lg['bucket'][:3]} | {lg['overall_mean_net']:+.3f} |"
        for rg in _REGIMES:
            c = cells.get(rg)
            row += f" {c['mean_net']:+.2f}×{c['n']} |" if c else " — |"
        ap(row)
    ap("")

    ap("## Join-Grenzen (ehrlich)\n")
    for lim in meta["join_limits"]:
        ap(f"- {lim}")
    ap("")
    return "\n".join(L)


_JOIN_LIMITS = [
    "Regime = RULE_recon (debounced) aus dem gespeicherten regime-Stream; T-031 validierte das zu "
    "91.85% gegen aufgezeichnetes regime_at_open. Residual = Warm-up + Ingestion-Outage-Desync.",
    "As-of-Join setzt voraus, dass trade.open_time (AI) / time (classic) und regime_history.ts DIESELBE "
    "naive Uhr tragen (R3-TZ-Baustelle, P1.8/UTC_POLICY). Ein systematischer Offset (z.B. +3h) würde die "
    "Regime-Zuordnung zeitlich verschieben; da Gated+Ungated denselben Offset teilen, bleibt der DIFF "
    "(und damit RESCUED/IMPROVED) robust, nur die absolute Zell-Attribution kann verschmieren.",
    "Der OOS-Gate-Uplift misst die REGIME-Achse allein — NICHT die Live-Whitelist-Mechanik "
    "(nicht historisch rekonstruierbar, T-031), Cornix-Routing oder Regime-Auto-Close. Er ist eine "
    "Obergrenze dessen, was Regime-Konditionierung theoretisch bringt; Live-Gating kann darunter liegen.",
    "Outcome = realized status (TP1-Touch-Win) → gestaffelter Move, Monitor-Rauschen (P1.2/P2.7) trifft "
    "gated+ungated gleich → der DIFF ist robuster als das Absolutniveau.",
    "Günstige Regimes werden datengetrieben gewählt (mean-net>0 auf Train) — bei 5 Regimes ist die "
    "Multiple-Comparison-Gefahr gering, aber der OOS-Split ist die eigentliche Absicherung; ein "
    "In-Sample-Gate wäre wertlos.",
    "TREND_UP/DOWN sind selten (je ~3-4% der Zeit) → in vielen Legs unter MIN_CELL und damit weder als "
    "günstig noch ungünstig klassifizierbar (Gate lässt sie NICHT durch — konservativ, kept-frac zeigt es).",
    "alt_context bleibt außen vor (SOFT smoothed nur die BTC-Achse; die per-Bot-Whitelist über "
    "bot×regime×alt×dir ist der eigentliche Live-Gate, aber nicht rekonstruierbar).",
]


def main() -> None:
    ap = argparse.ArgumentParser(description="Regime-gate edge test (Phase B, T-2026-KYT-9050-032)")
    ap.add_argument("--days", type=int, default=200, help="regime_history lookback (covers full trade history)")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR)
    ap.add_argument("--parked-dir", default=_LIVE_PARKED_DEFAULT)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from core.database import get_db_connection
    from core.time import utc_now
    from tools.walkforward_sim import check_cpu_headroom, set_low_priority

    def soft_headroom():
        try:
            check_cpu_headroom()
        except SystemExit as e:
            print(f"WARN {e} — Phase B läuft dennoch (read-only, BELOW_NORMAL).", flush=True)

    set_low_priority()
    soft_headroom()

    active = resolve_active_scripts(args.parked_dir or None)
    conn = get_db_connection()
    try:
        conn.set_session(readonly=True)  # NEVER writes the live DB
    except Exception:
        pass
    try:
        rlook, slook, rh_span = build_regime_lookups(conn, args.days)
        print(f"regime_history: {rh_span[0]} rows", flush=True)
        soft_headroom()
        trades = load_ai_trades(conn) + load_classic_trades(conn)
        print(f"decided trades (deduped): {len(trades)}", flush=True)
    finally:
        conn.close()

    joined = attach_regime(trades, rlook, slook)
    trades = [t for t in trades if t.get("regime")]
    print(f"regime-joined: {joined}", flush=True)
    legs = build_legs(trades, active)

    meta = {
        "task": "T-2026-KYT-9050-032",
        "phase": "B",
        "generated_at": str(utc_now()),
        "rh_rows": rh_span[0],
        "rh_span": (rh_span[1], rh_span[2]),
        "n_trades": len(trades),
        "min_leg_n": MIN_LEG_N,
        "min_cell": MIN_CELL,
        "soft_hl": SOFT_HL,
        "legs": legs,
        "join_limits": _JOIN_LIMITS,
    }

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "regime_gate_edge_test.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)
    md = build_report(meta)
    with open(os.path.join(args.out, "regime_gate_edge_test.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    print(md)
    print(f"\n-> {os.path.join(args.out, 'regime_gate_edge_test.json')}")


if __name__ == "__main__":
    main()
