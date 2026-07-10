"""
tools/rom1_counterfactual.py — ROM1 Counterfactual-Scorer (T-2026-CU-9050-047).

Zweck
-----
Der Orchestrator-Gate (Bot 28) unterdrückt Signale, ohne dass jemals gemessen
wurde, was diese Unterdrückung wert ist (Report 16, §8). Dieses Tool rechnet
für jede Row in `orchestrator_suppressed_signals` das hypothetische Outcome
nach: Welche ROM1-Geometrie hätte der Orchestrator zum Signal-Zeitpunkt
gepostet, und wie wäre dieser Trade im First-Touch-Replay ausgegangen?

Ergebnis pro Suppression-Reason (`bot_not_whitelisted:wr_below_overall`,
`orchestrator_cooldown`, `bot_unidentified`, …): Win-Rate, Netto-PnL, R —
also **was das Gate gekostet oder erspart hat**. Positiver Netto-PnL auf der
suppressed-Seite = das Gate hat Geld liegen gelassen.

Beide Seiten desselben Gates
----------------------------
`--side suppressed` (Default) scored die geblockte Seite.
`--side forwarded` scored die durchgelassene Seite aus
`orchestrator_open_trades`, gebucketed nach `wl_reason` (B8,
T-2026-CU-9050-046) — also pro Gate-PFAD: echte 4D-Zelle vs.
`no_whitelist_entry` (default-open) vs. Fallback-Pfade.
`--side both` läuft beides und stellt die Buckets nebeneinander.

Erst der Vergleich beider Seiten beantwortet die eigentliche Frage: Trennt der
Gate-Pfad Gewinner von Verlierern, oder ist der +8pp-ROM1-WR ein Artefakt der
89% default-open-Rate?

Methodik (und ihre Grenzen)
---------------------------
  * **Kein Look-ahead.** Entscheidungskerze = die letzte 1h-Kerze, die zum
    Zeitpunkt der Suppression bereits GESCHLOSSEN war. Der Exit-Scan startet
    auf der Kerze danach (R1-Disziplin).
  * **Geometrie aus EINER Quelle**: `28_signal_orchestrator.compute_rom1_trade_params`
    mit den As-of-Parametern `price=`/`df=`. Kein Nachbau, kein Skew (X-R1).
  * **Exits** via `tools.walkforward_sim.simulate_exit` — wick-aware
    First-Touch, SL-first bei Ambiguität, Monitor-Trailing, Fees; Ladder über
    die 3 tatsächlich publizierten TPs (`ROM1_PUBLISHED_TARGETS`).
  * **Bewusste Näherungen** (im Report als Bias-Richtung zu lesen):
      - Live nimmt ROM1 den letzten 5m-Close als CMP, der Replay den Close der
        Entscheidungs-1h-Kerze (bis zu 59 Minuten früher).
      - Der Horizont ist gekappt (`--horizon-hours`, Default 168h). Live würde
        ein Regime-Wechsel den Trade früher schließen (Auto-Close) — die
        Counterfactuals sind daher eher optimistisch für lange Läufer.
      - `same_direction_open`/`opposite_direction_open`/`orchestrator_cooldown`
        sind **Dedupe**, kein Regime-Urteil: ihr Counterfactual misst den Wert
        der Positions-Hygiene, nicht die Qualität des 4D-Gates. Die Ausgabe
        trennt die Klassen deshalb (`bucket_class`).

Betriebsregeln (Live-VPS!)
--------------------------
  * DB strikt read-only (nur SELECTs), Prozess auf BELOW_NORMAL, CPU-Check —
    identisch zu walkforward_sim. Keine Tabelle wird geschrieben.
  * Ergebnisse als JSONL + Summary-JSON nach `KYTHERA_REPLAY_DIR`.

Beispiele
---------
  python tools/rom1_counterfactual.py --days 90
  python tools/rom1_counterfactual.py --days 90 --side both --horizon-hours 72
  python tools/rom1_counterfactual.py --days 30 --side forwarded --limit 200
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
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.candles import read_candles  # noqa: E402
from core.database import get_db_connection  # noqa: E402
from core.time import utc_now  # noqa: E402
from tools.walkforward_sim import (  # noqa: E402
    check_cpu_headroom,
    import_bot_module,
    set_low_priority,
    simulate_exit,
)

DEFAULT_OUT_DIR = os.getenv(
    "KYTHERA_REPLAY_DIR", r"C:\Users\Michael\Documents\_X\staging_models\replay"
)

# get_hvn_and_sr_levels liest live 95 Tage 1h-Kerzen und braucht >= 50 Rows.
SR_WINDOW_HOURS = 95 * 24
MIN_SR_ROWS = 50
DEFAULT_HORIZON_HOURS = 168

OHLCV_COLUMNS = ("open_time", "open", "high", "low", "close", "volume")

# Wofür der jeweilige Reason steht — nur `gate` misst das 4D-Whitelist-Urteil.
REASON_CLASS = {
    "bot_not_whitelisted": "gate",
    "orchestrator_cooldown": "dedupe",
    "opposite_direction_open": "dedupe",
    "same_direction_open": "dedupe",
    "bot_unidentified": "plumbing",
    "rom1_params_unavailable": "plumbing",
}


# ─────────────────────────────────────────────────────────────────────────────
# REASON-BUCKETS
# ─────────────────────────────────────────────────────────────────────────────
def parse_reason(reason: str | None) -> tuple[str, str]:
    """`reason` → (bucket, bucket_class).

    Der Whitelist-Block trägt den eigentlichen Gate-Pfad im Suffix
    (`bot_not_whitelisted:wr_below_overall`). Genau dieses Suffix ist die
    interessante Achse — der Prefix allein wäre ein einziger 90%-Bucket.
    """
    if not reason:
        return "unknown", "unknown"
    family, _, detail = reason.partition(":")
    bucket = f"{family}:{detail}" if detail else family
    return bucket, REASON_CLASS.get(family, "unknown")


def forwarded_bucket(wl_reason: str | None) -> tuple[str, str]:
    """Forward-Seite: der Gate-Pfad steht in `orchestrator_open_trades.wl_reason`.

    Rows aus der Zeit vor B8 (T-2026-CU-9050-046) haben NULL — die werden als
    eigener Bucket gezählt statt einem Pfad zugeraten.
    """
    if not wl_reason:
        return "forwarded:wl_reason_missing", "forward"
    return f"forwarded:{wl_reason}", "forward"


# ─────────────────────────────────────────────────────────────────────────────
# DB (read-only)
# ─────────────────────────────────────────────────────────────────────────────
def load_suppressed(conn, days: int, limit: int | None) -> list[dict]:
    """Suppressed-Rows der letzten `days` Tage.

    `ts` ist naiv-UTC (Default `NOW() AT TIME ZONE 'UTC'`, 26_regime_detector) —
    der Vergleich gegen NOW() wäre session-lokal (UTC_POLICY §R3). Wir schneiden
    deshalb gegen einen explizit gerechneten naiven UTC-Cutoff.
    """
    cutoff = utc_now().replace(tzinfo=None) - timedelta(days=int(days))
    sql = """
        SELECT id, ts, bot_name, coin, direction, regime_at_signal, reason, original_outbox_id
        FROM orchestrator_suppressed_signals
        WHERE ts >= %s AND coin IS NOT NULL AND direction IS NOT NULL
        ORDER BY ts ASC
    """
    params: list = [cutoff]
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    out = []
    for r in rows:
        bucket, cls = parse_reason(r[6])
        out.append({
            "side": "suppressed", "row_id": r[0], "ts": r[1], "bot_name": r[2],
            "coin": r[3], "direction": r[4], "regime_at_signal": r[5],
            "reason": r[6], "bucket": bucket, "bucket_class": cls,
            "original_outbox_id": r[7], "recorded_entry": None,
        })
    return out


def load_forwarded(conn, days: int, limit: int | None) -> list[dict]:
    """Forwarded-Rows (die durchgelassene Seite), gebucketed nach wl_reason."""
    cutoff = utc_now().replace(tzinfo=None) - timedelta(days=int(days))
    sql = """
        SELECT id, opened_at, bot_name, coin, direction, regime_at_open,
               wl_reason, original_outbox_id, entry_price
        FROM orchestrator_open_trades
        WHERE opened_at >= %s
        ORDER BY opened_at ASC
    """
    params: list = [cutoff]
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    out = []
    for r in rows:
        bucket, cls = forwarded_bucket(r[6])
        out.append({
            "side": "forwarded", "row_id": r[0], "ts": r[1], "bot_name": r[2],
            "coin": r[3], "direction": r[4], "regime_at_signal": r[5],
            "reason": r[6], "bucket": bucket, "bucket_class": cls,
            "original_outbox_id": r[7],
            "recorded_entry": float(r[8]) if r[8] is not None else None,
        })
    return out


def load_1h(conn, coin: str, oldest_ts, horizon_hours: int) -> pd.DataFrame | None:
    """1h-Kerzen ab (ältester Signalzeitpunkt − S/R-Fenster), nur GESCHLOSSENE.

    Das Fenster reicht bewusst weit vor das erste Signal zurück: die
    S/R-Level-Berechnung braucht dieselben 95 Tage Historie, die der Live-Bot
    beim Posten gesehen hätte.
    """
    start = pd.Timestamp(oldest_ts)
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    start = (start - pd.Timedelta(hours=SR_WINDOW_HOURS + 2)).to_pydatetime()
    try:
        df = read_candles(conn, coin, "1h", start=start, include_forming=False, columns=OHLCV_COLUMNS)
    except Exception:
        conn.rollback()
        return None
    if df.empty:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["high", "low", "close"]).reset_index(drop=True)
    return df if len(df) >= MIN_SR_ROWS else None


# ─────────────────────────────────────────────────────────────────────────────
# AS-OF-INDEX (R1: nur geschlossene Kerzen)
# ─────────────────────────────────────────────────────────────────────────────
def as_of_index(open_times: np.ndarray, ts) -> int:
    """Index der letzten 1h-Kerze, die zum Zeitpunkt `ts` bereits GESCHLOSSEN war.

    Eine Kerze mit open_time `o` schließt bei `o + 1h`. Gesucht ist also das
    letzte `o` mit `o + 1h <= ts`, d.h. `o <= ts - 1h`. Die Kerze, die `ts`
    enthält, ist zur Entscheidungszeit noch forming und darf nicht gesehen
    werden — genau hier baut man sonst Look-ahead ein (Falle R1).

    `open_times` ist das naive-UTC datetime64-Array aus `df["open_time"].values`.
    Rückgabe -1, wenn keine Kerze früh genug liegt.
    """
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    cutoff = (t - pd.Timedelta(hours=1)).to_datetime64()
    return int(np.searchsorted(open_times, cutoff, side="right")) - 1


# ─────────────────────────────────────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────────────────────────────────────
def score_row(orch, row: dict, df: pd.DataFrame, horizon_hours: int) -> dict:
    """Ein Signal counterfactual bewerten. Gibt den angereicherten Record zurück.

    `scored=False` + `skip_reason` bei Rows, die nicht bewertbar sind (zu kurze
    Historie, keine Geometrie) — die werden gezählt, nicht stillschweigend
    verworfen: eine Auswertung, die 30% der Suppressions unter den Tisch fallen
    lässt, misst nicht mehr den Gate-Wert.
    """
    rec = dict(row)
    rec["ts"] = str(row["ts"])
    direction = (row["direction"] or "").upper()
    if direction not in ("LONG", "SHORT"):
        return {**rec, "scored": False, "skip_reason": "bad_direction"}

    open_times = df["open_time"].values
    t = as_of_index(open_times, row["ts"])
    if t < MIN_SR_ROWS:
        return {**rec, "scored": False, "skip_reason": "insufficient_history"}
    if t >= len(df) - 1:
        return {**rec, "scored": False, "skip_reason": "no_forward_candles"}

    entry_price = float(df["close"].values[t])
    if not np.isfinite(entry_price) or entry_price <= 0:
        return {**rec, "scored": False, "skip_reason": "bad_entry_price"}

    win = df.iloc[max(0, t + 1 - SR_WINDOW_HOURS): t + 1][["high", "low", "close"]]
    params = orch.compute_rom1_trade_params(None, row["coin"], direction, price=entry_price, df=win)
    if params is None:
        return {**rec, "scored": False, "skip_reason": "rom1_params_unavailable"}

    # Horizont-Kappung: die Arrays enden am Horizont, `open_at_end` heißt dann
    # "nach N Stunden weder TP1 noch SL" (Rest mark-to-market am Horizont-Close).
    end = min(len(df), t + 1 + int(horizon_hours))
    res = simulate_exit(
        open_times[:end],
        df["high"].values[:end],
        df["low"].values[:end],
        df["close"].values[:end],
        t + 1,
        direction,
        params["entry1"],
        params["sl"],
        params["targets"],
        orch.ROM1_PUBLISHED_TARGETS,
    )
    rec.update({
        "scored": True,
        "skip_reason": None,
        "decision_candle": str(pd.Timestamp(open_times[t])),
        "entry": params["entry1"],
        "sl": params["sl"],
        "targets": params["targets"][: orch.ROM1_PUBLISHED_TARGETS],
        "horizon_hours": int(horizon_hours),
        "full_horizon": end == t + 1 + int(horizon_hours),
        **res,
    })
    if row.get("recorded_entry"):
        # Drift zwischen Live-CMP (5m-Close) und Replay-Entry (1h-Close) —
        # die Messlatte für die "bis zu 59 Minuten früher"-Näherung.
        rec["entry_drift_pct"] = round(
            (params["entry1"] - row["recorded_entry"]) / row["recorded_entry"] * 100, 4
        )
    return rec


# ─────────────────────────────────────────────────────────────────────────────
# AGGREGATION
# ─────────────────────────────────────────────────────────────────────────────
def aggregate(records: list[dict]) -> list[dict]:
    """Pro Bucket: n, Win-Rate, PnL, R. Sortiert nach Signal-Anzahl.

    `n_open_at_horizon` sind Trades, die am Horizont weder TP1 noch SL berührt
    hatten — sie zählen NICHT in die Win-Rate (kein Label), ihr
    mark-to-market-PnL fließt aber in die PnL-Summe ein, weil die Position real
    offen gewesen wäre.
    """
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_bucket[r["bucket"]].append(r)

    out = []
    for bucket, rows in by_bucket.items():
        scored = [r for r in rows if r.get("scored")]
        decided = [r for r in scored if r.get("outcome_tp1") is not None]
        wins = sum(1 for r in decided if r["outcome_tp1"] == 1)
        pnl = [r["net_pnl_pct"] for r in scored if r.get("net_pnl_pct") is not None]
        r_vals = [r["r_multiple"] for r in scored if r.get("r_multiple") is not None]
        skips: dict[str, int] = defaultdict(int)
        for r in rows:
            if not r.get("scored"):
                skips[r.get("skip_reason") or "unknown"] += 1
        out.append({
            "bucket": bucket,
            "bucket_class": rows[0]["bucket_class"],
            "side": rows[0]["side"],
            "n_signals": len(rows),
            "n_scored": len(scored),
            "n_unscorable": len(rows) - len(scored),
            "unscorable_by_reason": dict(skips),
            "n_decided": len(decided),
            "n_open_at_horizon": len(scored) - len(decided),
            "tp1_first_touch_wr": round(wins / len(decided) * 100, 2) if decided else None,
            "sum_net_pnl_pct": round(float(np.sum(pnl)), 2) if pnl else None,
            "avg_net_pnl_pct": round(float(np.mean(pnl)), 4) if pnl else None,
            "median_net_pnl_pct": round(float(np.median(pnl)), 4) if pnl else None,
            "avg_r": round(float(np.mean(r_vals)), 4) if r_vals else None,
        })
    return sorted(out, key=lambda d: -d["n_signals"])


def print_report(summary: list[dict]) -> None:
    if not summary:
        print("Keine Rows im Fenster — nichts zu bewerten.")
        return
    hdr = f"{'bucket':46} {'class':9} {'n':>6} {'scored':>7} {'wr%':>7} {'avgPnL%':>9} {'sumPnL%':>10} {'avgR':>7}"
    print("\n" + hdr)
    print("-" * len(hdr))
    for s in summary:
        wr = f"{s['tp1_first_touch_wr']:.2f}" if s["tp1_first_touch_wr"] is not None else "—"
        avg = f"{s['avg_net_pnl_pct']:.3f}" if s["avg_net_pnl_pct"] is not None else "—"
        tot = f"{s['sum_net_pnl_pct']:.2f}" if s["sum_net_pnl_pct"] is not None else "—"
        avr = f"{s['avg_r']:.3f}" if s["avg_r"] is not None else "—"
        print(f"{s['bucket'][:46]:46} {s['bucket_class']:9} {s['n_signals']:6d} "
              f"{s['n_scored']:7d} {wr:>7} {avg:>9} {tot:>10} {avr:>7}")

    gate = [s for s in summary if s["bucket_class"] == "gate" and s["avg_net_pnl_pct"] is not None]
    fwd = [s for s in summary if s["bucket_class"] == "forward" and s["avg_net_pnl_pct"] is not None]
    if gate:
        n = sum(s["n_scored"] for s in gate)
        tot = sum(s["sum_net_pnl_pct"] for s in gate)
        print(f"\nGate-Seite (bot_not_whitelisted, {n} bewertete Suppressions): "
              f"Summe {tot:.2f}% Nominal — positiv = das Gate hat Geld liegen gelassen.")
    if fwd:
        n = sum(s["n_scored"] for s in fwd)
        tot = sum(s["sum_net_pnl_pct"] for s in fwd)
        print(f"Forward-Seite ({n} bewertete Forwards): Summe {tot:.2f}% Nominal.")
    print("\nLesehinweis: `dedupe`-Buckets messen Positions-Hygiene, nicht das 4D-Gate. "
          "Vergleichbar sind nur `gate` vs `forward` bei gleichem Horizont.")


# ─────────────────────────────────────────────────────────────────────────────
# DRIVER
# ─────────────────────────────────────────────────────────────────────────────
def score_all(conn, orch, rows: list[dict], horizon_hours: int) -> list[dict]:
    """Rows coin-weise abarbeiten — ein Kerzen-Load je Coin, nicht je Signal."""
    by_coin: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_coin[r["coin"]].append(r)

    records: list[dict] = []
    t0 = time.time()
    for i, (coin, coin_rows) in enumerate(sorted(by_coin.items()), 1):
        df = load_1h(conn, coin, min(r["ts"] for r in coin_rows), horizon_hours)
        if df is None:
            records.extend({**r, "ts": str(r["ts"]), "scored": False, "skip_reason": "no_candles"} for r in coin_rows)
            continue
        for r in coin_rows:
            try:
                records.append(score_row(orch, r, df, horizon_hours))
            except Exception as e:  # eine kaputte Row reißt den Lauf nicht ab
                print(f"  !! {coin} row#{r['row_id']}: {e}")
                records.append({**r, "ts": str(r["ts"]), "scored": False, "skip_reason": "error"})
        if i % 25 == 0 or i == len(by_coin):
            print(f"[{i}/{len(by_coin)}] {coin}: {len(records)} Rows ({time.time() - t0:.0f}s)", flush=True)
    return records


def main() -> None:
    ap = argparse.ArgumentParser(description="ROM1 Counterfactual-Scorer (T-2026-CU-9050-047)")
    ap.add_argument("--days", type=int, default=90, help="Rückblick über ts/opened_at")
    ap.add_argument("--side", default="suppressed", choices=["suppressed", "forwarded", "both"])
    ap.add_argument("--horizon-hours", type=int, default=DEFAULT_HORIZON_HOURS)
    ap.add_argument("--limit", type=int, default=None, help="nur die ersten N Rows je Seite")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    set_low_priority()
    check_cpu_headroom()

    orch = import_bot_module("28_signal_orchestrator.py", "signal_orchestrator")

    conn = get_db_connection()
    try:
        conn.set_session(readonly=True)  # der Scorer schreibt NIE in die Live-DB
    except Exception:
        pass

    try:
        rows: list[dict] = []
        if args.side in ("suppressed", "both"):
            rows += load_suppressed(conn, args.days, args.limit)
        if args.side in ("forwarded", "both"):
            rows += load_forwarded(conn, args.days, args.limit)
        print(f"{len(rows)} Rows im Fenster ({args.days}d, side={args.side})")
        records = score_all(conn, orch, rows, args.horizon_hours)
    finally:
        conn.close()

    summary = aggregate(records)
    os.makedirs(args.out, exist_ok=True)
    tag = f"rom1_counterfactual_{args.side}_{args.days}d"
    jsonl_path = os.path.join(args.out, f"{tag}.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, default=str) + "\n")
    meta = {
        "days": args.days,
        "side": args.side,
        "horizon_hours": args.horizon_hours,
        "n_rows": len(records),
        "n_scored": sum(1 for r in records if r.get("scored")),
        "generated_at": str(utc_now()),
        "buckets": summary,
    }
    with open(os.path.join(args.out, f"{tag}_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)

    print_report(summary)
    print(f"\nRecords: {jsonl_path}")


if __name__ == "__main__":
    main()
