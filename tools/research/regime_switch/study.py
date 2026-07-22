"""study.py — driver for the regime-weighting study (T-2026-KYT-9050-029).

Pipeline:
  1. fetch BTC + BTCDOM 15m off ccxt (or --csv-dir cache) — no DB,
  2. reconstruct the classifier features over the full history (features.py),
  3. build the four regime timelines A/B/C/D (timelines.py),
  4. slice to the common window (past the slowest warmup),
  5. score whipsaw / TREND-hold / separation per variant (metrics.py),
  6. an explicit EDGE / NO-EDGE verdict: does HMM or SOFT beat the RULE on
     whipsaw AND hold-or-improve eta² separation, without worsening TREND-hold?

Run:  python -m tools.research.regime_switch.study --days 365
      python -m tools.research.regime_switch.study --csv-dir <cache> --days 365

The honest boundary (also printed): this measures regime-TIMELINE quality and
BTC-conditional separation only. The PnL effect on real bot forwards is DB-bound
(tools/rom1_counterfactual.py on the VPS). This study is the necessary precursor:
if a variant doesn't even separate the timeline better, the DB counterfactual is
not worth the VPS time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.research.regime_switch.features import build_feature_frame  # noqa: E402
from tools.research.regime_switch.metrics import common_index, variant_report  # noqa: E402
from tools.research.regime_switch.timelines import (  # noqa: E402
    build_hmm_timeline,
    build_raw_timeline,
    build_rule_timeline,
    build_soft_timeline,
)

BTC = "BTC/USDT:USDT"
BTCDOM = "BTCDOM/USDT:USDT"


def _load(symbol: str, csv_dir: str | None, days: int, exchange: str) -> pd.DataFrame:
    from tools.research.regime_switch.ccxt_data import fetch_ohlc, load_ohlc_csv, normalize_ohlc

    safe = symbol.replace("/", "_").replace(":", "_")
    if csv_dir:
        path = os.path.join(csv_dir, f"{safe}_15m.csv")
        if os.path.exists(path):
            return load_ohlc_csv(path)
    df = fetch_ohlc(symbol, exchange_id=exchange, timeframe="15m", max_bars=days * 96 + 96 * 32)
    if csv_dir:
        os.makedirs(csv_dir, exist_ok=True)
        normalize_ohlc(df).to_csv(os.path.join(csv_dir, f"{safe}_15m.csv"), index=False)
    return df


def build_timelines(feat: pd.DataFrame, hmm: bool = True) -> dict[str, pd.Series]:
    tl = {
        "RAW": build_raw_timeline(feat),
        "RULE": build_rule_timeline(feat),
        "SOFT": build_soft_timeline(feat),
    }
    if hmm:
        tl["HMM"] = build_hmm_timeline(feat, verbose=True)
    return tl


def make_verdict(reports: dict[str, dict]) -> dict:
    """EDGE iff a candidate (HMM/SOFT) beats RULE on whipsaw AND holds-or-improves
    eta² separation AND does not worsen the TREND-hold <1h share."""
    rule = reports.get("RULE")
    if not rule:
        return {"verdict": "INSUFFICIENT", "reason": "no RULE baseline"}
    base_sw = rule["whipsaw"]["switches_per_30d"]
    base_eta = rule["separation"]["eta_squared"] or 0.0
    base_u1h = rule["trend_hold"]["pct_trend_episodes_under_1h"]

    findings = {}
    edge = False
    for cand in ("HMM", "SOFT"):
        rep = reports.get(cand)
        if not rep:
            continue
        sw = rep["whipsaw"]["switches_per_30d"]
        eta = rep["separation"]["eta_squared"] or 0.0
        u1h = rep["trend_hold"]["pct_trend_episodes_under_1h"]
        beats_whipsaw = sw is not None and base_sw is not None and sw < base_sw
        holds_sep = eta >= base_eta * 0.98  # allow 2% slack
        not_worse_trend = (u1h is None or base_u1h is None) or (u1h <= base_u1h + 5.0)
        is_edge = bool(beats_whipsaw and holds_sep and not_worse_trend)
        edge = edge or is_edge
        findings[cand] = {
            "switches_per_30d": sw,
            "vs_rule_switches": None if (sw is None or base_sw is None) else round(sw - base_sw, 2),
            "eta_squared": eta,
            "vs_rule_eta": round(eta - base_eta, 5),
            "beats_whipsaw": beats_whipsaw,
            "holds_separation": holds_sep,
            "not_worse_trend_hold": not_worse_trend,
            "is_edge": is_edge,
        }
    verdict = "EDGE" if edge else "NO-EDGE"
    reason = (
        "a candidate beats RULE on whipsaw while holding eta² separation"
        if edge
        else "no candidate improves whipsaw without losing separation / worsening TREND-hold"
    )
    return {"verdict": verdict, "reason": reason, "baseline_rule": {
        "switches_per_30d": base_sw, "eta_squared": base_eta,
        "pct_trend_episodes_under_1h": base_u1h}, "candidates": findings}


def _print(reports: dict[str, dict], verdict: dict, window: dict) -> None:
    print(f"\n  COMMON WINDOW: {window['start']} → {window['end']}  ({window['n_candles']} candles, ~{window['days']}d)\n")
    hdr = f"  {'variant':7} {'sw/30d':>7} {'medDwell_h':>11} {'ep<1h%':>7} {'trend%':>7} {'trEp<1h%':>9} {'eta²':>8}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for name in ("RAW", "RULE", "SOFT", "HMM"):
        r = reports.get(name)
        if not r:
            continue
        w, th, sep = r["whipsaw"], r["trend_hold"], r["separation"]
        print(f"  {name:7} {_n(w['switches_per_30d']):>7} {_n(w['median_dwell_hours']):>11} "
              f"{_n(w['pct_episodes_under_1h']):>7} {_n(th['pct_time_trend']):>7} "
              f"{_n(th['pct_trend_episodes_under_1h']):>9} {_n(sep['eta_squared']):>8}")
    print(f"\n  eta² (separation) by forward horizon — higher = regime explains more of forward return:")
    print(f"    {'variant':7} {'1h':>9} {'4h':>9} {'24h':>9}")
    for name in ("RAW", "RULE", "SOFT", "HMM"):
        r = reports.get(name)
        if not r:
            continue
        bh = r["separation"].get("eta_squared_by_horizon", {})
        print(f"    {name:7} {_n(bh.get('1h')):>9} {_n(bh.get('4h')):>9} {_n(bh.get('24h')):>9}")

    print(f"\n  VERDICT: {verdict['verdict']} — {verdict['reason']}")
    for cand, f in verdict.get("candidates", {}).items():
        print(f"    {cand}: Δsw/30d={f['vs_rule_switches']}  Δeta²={f['vs_rule_eta']}  "
              f"edge={f['is_edge']} (whipsaw={f['beats_whipsaw']}, sep_held={f['holds_separation']}, "
              f"trend_ok={f['not_worse_trend_hold']})")
    print("\n  Per-state forward-return separation (RULE vs best candidate) is in the JSON.")
    print("  NOTE: timeline/separation only — PnL on real bot forwards is DB-bound "
          "(tools/rom1_counterfactual.py on the VPS). This is the precursor gate.\n")


def _n(v) -> str:
    return "—" if v is None else (f"{v:.4f}" if isinstance(v, float) and abs(v) < 1 else f"{v}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Regime-weighting study (T-2026-KYT-9050-029)")
    ap.add_argument("--days", type=int, default=365, help="history window in days")
    ap.add_argument("--exchange", default="binanceusdm")
    ap.add_argument("--csv-dir", default=None, help="cache dir for fetched klines (offline reruns)")
    ap.add_argument("--no-hmm", action="store_true", help="skip the HMM timeline (fast smoke run)")
    ap.add_argument("--out", default=os.getenv("KYTHERA_REPLAY_DIR", os.path.join(REPO_ROOT, "staging_models", "replay")))
    ap.add_argument("--json", action="store_true", help="print full JSON instead of the table")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    btc = _load(BTC, args.csv_dir, args.days, args.exchange)
    dom = None
    try:
        dom = _load(BTCDOM, args.csv_dir, args.days, args.exchange)
    except SystemExit:
        print("  (BTCDOM unavailable — alt-context falls back to ALT_NEUTRAL)", file=sys.stderr)

    feat = build_feature_frame(btc, dom)
    timelines = build_timelines(feat, hmm=not args.no_hmm)

    cidx = common_index(timelines)
    if len(cidx) < 96 * 10:
        raise SystemExit(f"common window too short ({len(cidx)} candles) — fetch more history")
    sliced = {k: v.reindex(cidx) for k, v in timelines.items()}
    price = feat["btc_price"]

    reports = {name: variant_report(name, labels, price) for name, labels in sliced.items()}
    verdict = make_verdict(reports)
    window = {
        "start": str(cidx[0]), "end": str(cidx[-1]),
        "n_candles": len(cidx), "days": round(len(cidx) / 96, 1),
    }

    os.makedirs(args.out, exist_ok=True)
    payload = {"window": window, "config": vars(args), "reports": reports, "verdict": verdict}
    out_path = os.path.join(args.out, "regime_switch_study.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        _print(reports, verdict, window)
    print(f"  JSON: {out_path}")


if __name__ == "__main__":
    main()
