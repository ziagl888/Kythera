"""tools/dca_all_bots.py — fleet-wide DCA effect (T-2026-KYT-9050-045, read-only).

Runs the DCA A-vs-B experiment (T-043) across EVERY bot that has enough
telegram_outbox geometry, each over its maximum available window, and ranks the
DCA drag fleet-wide.

Why per-bot windows (T-044 finding): `closed_ai_signals` has no `entry2` — its
recorded realised PnL is already ~arm B (single entry1, no DCA). The DCA effect
(arm A) only lives in the immutable Cornix text (`telegram_outbox`), whose per-bot
retention bounds each window. entry2/SL are NOT fixed % offsets (empirically the
std is ~half the mean), so they cannot be reconstructed from entry1 — each bot is
tested exactly, as far as its geometry reaches.

Arms (from tools.wave_exit_overlay, the merged T-043 harness):
  A = cornix3       DCA real (entry1+entry2, orig SL)     — what Cornix executes
  B = single_origsl entry1 only, orig SL                  — the no-DCA world
  C = single_slE2   entry1 only, SL = entry2              — the tight-stop variant
Metric = risk-adjusted (T-041): per-trade leveraged Sharpe + fixed-2% compounding
MaxDD. DCA drag = B_sharpe − A_sharpe (positive → dropping the DCA helps).

Betriebsregeln (Live-VPS!): DB strictly read-only, BELOW_NORMAL priority,
coin-windowed reads. Output ONLY to staging_models/replay/. Long batch (~2-3 min
per bot × N bots) — run with the VPS in mind.

Beispiel
--------
  python tools/dca_all_bots.py --allow-high-cpu
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, "staging_models", "replay")
MIN_OUTBOX = 30  # min entry2-carrying Cornix msgs for a bot to be testable
THIN_N = 30  # below this a bot's arm-sign is not trusted
DEFAULT_END = "2026-07-27 00:00:00"


def discover_bots(conn) -> list[dict]:
    """closed models (real, post-Feb-backfill) with >=MIN_OUTBOX entry2 Cornix
    msgs → [{model, start, outbox_n}], start = the earliest such message (its max
    window). Harness-accurate: same %model% substring the geometry loader uses."""
    out = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT model, count(*) FROM closed_ai_signals "
            "WHERE open_time >= '2026-04-01' GROUP BY model HAVING count(*) >= 50"
        )
        models = [r[0] for r in cur.fetchall()]
        for m in models:
            cur.execute(
                "SELECT count(*), min(created_at) FROM telegram_outbox "
                "WHERE message LIKE %s AND message LIKE '%%Entry 2%%' "
                "AND message NOT LIKE '%%<pre>%%' AND message LIKE '%%CMP Entry%%'",
                (f"%{m}%",),
            )
            n, mn = cur.fetchone()
            if n >= MIN_OUTBOX and mn is not None:
                out.append({"model": m, "start": mn.strftime("%Y-%m-%d 00:00:00"), "outbox_n": int(n)})
    out.sort(key=lambda d: d["start"])  # longest-window bots first
    return out


def _arm(a: dict, key: str) -> dict:
    return (a or {}).get(key, {}) or {}


def render_md(meta: dict) -> str:
    rows = meta["bots"]
    L = []
    ap = L.append
    ap("# DCA-Effekt fleet-weit — A (DCA) vs B (single-entry1) vs C (SL@entry2) — T-2026-KYT-9050-045\n")
    ap(
        f"_generated {meta['generated_at']} · read-only · {len(rows)} Bots · je Max-Outbox-Fenster · bis {meta['end']}_\n"
    )
    ap(
        "**Warum per-Bot-Fenster:** `closed_ai_signals` hat kein entry2 → seine Realized-Zahlen sind schon "
        "≈ **Arm B** (single entry1, ohne DCA). Der DCA-Effekt (Arm A) lebt nur im immutablen Cornix-Text "
        "(`telegram_outbox`), dessen per-Bot-Retention jedes Fenster begrenzt; entry2/SL sind **keine** "
        "festen %-Abstände → nicht rekonstruierbar. Metrik risiko-adjustiert (T-041): per-Trade leveraged "
        "**Sharpe** + kompoundierende **MaxDD (2%)**. **DCA-Drag = B−A** (Sharpe; positiv → DCA weglassen hilft).\n"
    )
    ap(
        "| Bot | Fenster ab | n | **A Sharpe/MaxDD** | **B Sharpe/MaxDD** | **C Sharpe/MaxDD** | **DCA-Drag (B−A)** | C−B | Verdikt |"
    )
    ap("|---|---|--:|--:|--:|--:|--:|--:|---|")
    for r in rows:
        A, B, C = _arm(r, "A"), _arm(r, "B"), _arm(r, "C")
        shA, shB, shC = A.get("sharpe_lev"), B.get("sharpe_lev"), C.get("sharpe_lev")
        drag = round(shB - shA, 3) if (shA is not None and shB is not None) else None
        cb = round(shC - shB, 3) if (shB is not None and shC is not None) else None
        thin = r["n"] < THIN_N
        verdict = (
            "THIN"
            if thin
            else ("DCA-SCHADET" if (drag or 0) > 0.02 else ("neutral" if abs(drag or 0) <= 0.02 else "DCA-hilft"))
        )

        def sd(a: dict):
            s, d = a.get("sharpe_lev"), a.get("maxdd_2pct")
            return f"{s}/{d}%" if s is not None else "—"

        ap(
            f"| {r['model']} | {r['start'][:10]} | {r['n']} | {sd(A)} | {sd(B)} | {sd(C)} | "
            f"**{'—' if drag is None else f'{drag:+}'}** | {'—' if cb is None else f'{cb:+}'} | {verdict} |"
        )

    # fleet verdict
    solid = [r for r in rows if r["n"] >= THIN_N]
    drags = [
        _arm(r, "B").get("sharpe_lev") - _arm(r, "A").get("sharpe_lev")
        for r in solid
        if _arm(r, "A").get("sharpe_lev") is not None and _arm(r, "B").get("sharpe_lev") is not None
    ]
    hurts = sum(1 for d in drags if d > 0.02)
    cbs = [
        _arm(r, "C").get("sharpe_lev") - _arm(r, "B").get("sharpe_lev")
        for r in solid
        if _arm(r, "B").get("sharpe_lev") is not None and _arm(r, "C").get("sharpe_lev") is not None
    ]
    c_wins = sum(1 for d in cbs if d > 0.02)
    ap("\n### Fleet-Verdikt\n")
    if drags:
        ap(
            f"- **DCA schadet bei {hurts}/{len(drags)} soliden Bots** (n≥{THIN_N}, DCA-Drag B−A > +0,02). "
            f"Median-Drag {round(sorted(drags)[len(drags) // 2], 3):+}."
        )
    if cbs:
        ap(f"- **entry2-als-SL (C) schlägt B bei {c_wins}/{len(cbs)}** — bot-typ-abhängig (Trend vs Mean-Reversion).")
    ap(
        "\n**Ehrliche Grenzen:** je Bot nur so weit die Outbox-Geometrie reicht (die meisten ~2-3 Wochen; "
        "ROM1/MIS1-168H ~2,5 Monate). entry2-vorhandenes Set, Compounding sequenziell-nach-close. "
        "DCA-Weglassen = eigener Deploy-Kandidat (Michi-Entscheid). NO-EDGE/kein-Effekt ist valide.\n"
    )
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="Fleet-wide DCA A-vs-B (T-2026-KYT-9050-045)")
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--out", default=DEFAULT_OUT_DIR)
    ap.add_argument("--only", default=None, help="comma-separated model subset (default: auto-discover all)")
    ap.add_argument("--allow-high-cpu", action="store_true")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from core.database import get_db_connection
    from core.time import utc_now
    from tools.walkforward_sim import check_cpu_headroom, set_low_priority
    from tools.wave_exit_overlay import analyse_entrysl, run_validate

    set_low_priority()
    if args.allow_high_cpu:
        print("CPU-Headroom-Check übersprungen (--allow-high-cpu; BELOW_NORMAL + read-only).")
    else:
        try:
            check_cpu_headroom()
        except SystemExit as e:
            print(f"WARN {e} — läuft dennoch (read-only, BELOW_NORMAL).", flush=True)

    conn = get_db_connection()
    try:
        conn.set_session(readonly=True)
    except Exception:
        pass

    bots_meta: list[dict] = []
    try:
        discovered = discover_bots(conn)
        if args.only:
            keep = {m.strip() for m in args.only.split(",")}
            discovered = [d for d in discovered if d["model"] in keep]
        print(f"testable bots: {len(discovered)} → {[d['model'] for d in discovered]}", flush=True)
        t0 = time.time()
        for i, d in enumerate(discovered, 1):
            model, start = d["model"], d["start"]
            print(f"\n[{i}/{len(discovered)}] {model} from {start} ({time.time() - t0:.0f}s)", flush=True)
            try:
                result = run_validate(conn, model, start, args.end, None, collect_arts=False)
                e = analyse_entrysl(result["records"])
                allrow = e["by_dir"].get("ALL", {})
                bots_meta.append(
                    {
                        "model": model,
                        "start": start,
                        "outbox_n": d["outbox_n"],
                        "n": e["n_scored"],
                        "A": allrow.get("A"),
                        "B": allrow.get("B"),
                        "C": allrow.get("C"),
                    }
                )
            except Exception as ex:  # one bot failing must not abort the fleet run
                print(f"  SKIP {model}: {type(ex).__name__}: {ex}", flush=True)
                bots_meta.append({"model": model, "start": start, "outbox_n": d["outbox_n"], "n": 0, "skip": str(ex)})
    finally:
        conn.close()

    # rank by DCA drag (B_sharpe - A_sharpe) desc; unscored/thin sink
    def drag(r: dict):
        A, B = _arm(r, "A"), _arm(r, "B")
        if A.get("sharpe_lev") is None or B.get("sharpe_lev") is None:
            return (0, -1e9)
        return (1, B["sharpe_lev"] - A["sharpe_lev"])

    bots_meta.sort(key=drag, reverse=True)
    meta = {
        "task": "T-2026-KYT-9050-045",
        "generated_at": str(utc_now()),
        "end": args.end,
        "min_outbox": MIN_OUTBOX,
        "bots": bots_meta,
    }
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "dca_all_bots.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)
    md = render_md(meta)
    with open(os.path.join(args.out, "dca_all_bots.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    print("\n" + md)
    print(f"\nJSON: {os.path.join(args.out, 'dca_all_bots.json')}")


if __name__ == "__main__":
    main()
