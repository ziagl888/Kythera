# tools/funding_liq_gate_study.py — funding × forced-liquidation entry-gate pilot
"""Does the interplay of funding rates and forced liquidations gate the fleet's
entries with a per-direction win-rate AND expectancy lift? T-2026-KYT-9050-120.

DB-FREE BY DESIGN: reads ONLY a DuckDB snapshot produced by
tools/gate_snapshot_export.py (VPS session) — never live Postgres. Iterate as
often as you like without touching the DB (Michi, 2026-08-09).

Pre-registered gates (thresholds fixed in
docs/T-2026-KYT-9050-120-funding-liq-gate-pilot.md BEFORE the conclusive run —
T-116 discipline: never derive a gate from outcomes under the same gate):

  H1  crowded-side flush/squeeze veto: funding extreme in the trade direction
      (LONG: fund_24h > +3 bps crowded longs / SHORT: < −3 bps crowded shorts)
      AND a liquidation cascade against the direction in the last 60 min.
  H2a cascade-against veto, 15 min window (>= 2 same-symbol events).
  H2b cascade-against veto, 60 min window (>= 3 same-symbol events).
  H3  market-wide cascade veto (>= 5 distinct symbols with liqs in 15 min).

"Against the direction" = liquidations of the trade's own side: SELL rows
(longs force-closed, price pushed down) are against a LONG entry; BUY rows
(shorts force-closed, price pushed up) are against a SHORT entry.

Evaluation discipline (T-134/T-094 lineage): paired gate-on/off on identical
trades; chronological val/test halves must BOTH show kept-WR and kept-raw-mean
improving per direction (WR alone is not decisive, repo Rule 8); liq features
are counts/clusters/recency only — the !forceOrder stream is throttled to
1 order/s/symbol, a SAMPLE, so notional sums are secondary and never carry a
verdict. MIN_LIQ_DAYS=21 refuses a verdict on thin coverage (--smoke runs the
plumbing and stamps the report NOT CONCLUDABLE).

Invocation:
  python tools/funding_liq_gate_study.py --snapshot .local/gate_snapshots/gate_snapshot_20260824.duckdb
  python tools/funding_liq_gate_study.py --snapshot ... --smoke   # plumbing run below MIN_LIQ_DAYS
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.chdir(REPO_ROOT)

from core.funding_features import funding_features_asof  # noqa: E402
from core.time import LEGACY_WRITER_TZ  # noqa: E402
from tools.gate_snapshot import read_snapshot  # noqa: E402
from tools.walkforward_sim import FEE_PER_SIDE  # noqa: E402

ROUND_TRIP_FEE = 2.0 * FEE_PER_SIDE  # 0.001 = 0.10 % (P3.6 — we do NOT invent a fee)

MIN_LIQ_DAYS = 21  # below this the study refuses a verdict (T-094/T-095 discipline)
WARMUP_MIN = 60  # trades need a full lookback window inside liq coverage
EXTREME_BPS = 3.0  # T-134 extreme-funding cut, reused verbatim
WINDOWS_MIN = (15, 60)
CASCADE_MIN_N_15 = 2  # same-symbol events against direction, 15 min
CASCADE_MIN_N_60 = 3  # same-symbol events against direction, 60 min
# Recalibrated 2026-08-09 after the first --smoke run: the market ALWAYS has
# liquidations printing (median 78 distinct symbols per 15 min over the first
# 6 liq_events days; ~206k events/6d) — the original pre-registered 5 skipped
# 100% of entries, a degenerate gate. New cut = q90 of the observed FEATURE
# marginal (137 → rounded 140, skips ~10%). Distribution-derived, NOT
# outcome-derived (T-116 discipline holds); amended in the spec doc before any
# conclusive evidence exists.
MKT_CASCADE_SYMS = 140  # distinct symbols with any liq in 15 min (q90 tail event)
MIN_SINCE_CAP_MIN = 1440.0
MIN_CELL_N = 30  # cells below this are shown but never argued from
MIN_SKIP_N = 20  # a gate that skips fewer trades than this cannot be a candidate

_NS_PER_MIN = 60 * 1_000_000_000


# ──────────────────────────────────────────────────────────────────────────────
# population
# ──────────────────────────────────────────────────────────────────────────────


def prepare_trades(raw: pd.DataFrame) -> pd.DataFrame:
    """Dedup (defensive — the export already dedups), price/status filter,
    net PnL, DST-aware open_time localization. Returns one row per real trade
    with ``outcome_pct`` (net %, fee included) and ``open_time_utc``."""
    df = raw.sort_values(["symbol", "model", "direction", "open_time", "id"])
    df = df.drop_duplicates(["symbol", "model", "direction", "open_time"], keep="first").copy()
    df = df[(df["entry"] > 0) & (df["close_price"] > 0)]
    df = df[df["status"].ne("ENTRY_NOT_FILLED") | df["status"].isna()]

    entry = df["entry"].astype(float)
    close = df["close_price"].astype(float)
    is_long = df["direction"].str.upper() == "LONG"
    gross = np.where(is_long, (close - entry) / entry, (entry - close) / entry)
    df["outcome_pct"] = (gross - ROUND_TRIP_FEE) * 100.0

    # open_time is naive wall-clock Europe/Bucharest (legacy writers) — localize
    # DST-aware; ambiguous/nonexistent instants around a DST flip become NaT and drop.
    loc = pd.to_datetime(df["open_time"]).dt.tz_localize(LEGACY_WRITER_TZ, ambiguous="NaT", nonexistent="NaT")
    df["open_time_utc"] = loc.dt.tz_convert("UTC").astype("datetime64[ns, UTC]")
    return df.dropna(subset=["open_time_utc"])


def prepare_trailing(raw: pd.DataFrame) -> pd.DataFrame:
    """Bot-40 realized book (pre-builds T-095's liq arm). ``close_mark_pct`` is
    already an unlevered net %-mark → used verbatim as ``outcome_pct``.
    opened_at is stored UTC (new-generation table; T-094 convention)."""
    df = raw.copy()
    ts = pd.to_datetime(df["opened_at"], utc=True)
    df["open_time_utc"] = ts.astype("datetime64[ns, UTC]")
    df["outcome_pct"] = df["close_mark_pct"].astype(float)
    return df.dropna(subset=["open_time_utc", "outcome_pct"])


# ──────────────────────────────────────────────────────────────────────────────
# features
# ──────────────────────────────────────────────────────────────────────────────


def _ns(series: pd.Series) -> np.ndarray:
    """tz-aware/naive datetime series → int64 epoch-ns (T-073: force ns first)."""
    return series.values.astype("datetime64[ns]").astype("int64")


def liq_features(trades: pd.DataFrame, liq: pd.DataFrame, windows_min: tuple[int, ...] = WINDOWS_MIN) -> pd.DataFrame:
    """Entry-time liquidation features, strictly backward-looking.

    Window is half-open [t−w, t): an event at exactly the entry timestamp is
    EXCLUDED (no simultaneity). Counts/recency/breadth are the primary
    features; ``liq_val_against_60m`` (notional) is SECONDARY — the stream is
    a 1 order/s/symbol sample and volume sums are not trustworthy.
    """
    t = trades.copy()
    dirn = t["direction"].str.upper()
    t["_against"] = np.where(dirn == "LONG", "SELL", "BUY")

    liq = liq.sort_values("ts")
    side_arrs: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]] = {}
    for (s, sd), g in liq.groupby(["symbol", "side"], sort=False):
        arr = _ns(g["ts"])
        cum = np.concatenate(([0.0], np.cumsum(g["value_usdt"].fillna(0.0).to_numpy(dtype=float))))
        side_arrs[(str(s), str(sd))] = (arr, cum)
    sym_arrs = {str(s): _ns(g["ts"]) for s, g in liq.groupby("symbol", sort=False)}
    mkt_ts = _ns(liq["ts"])
    mkt_sym = liq["symbol"].to_numpy()

    for w in windows_min:
        t[f"liq_n_against_{w}m"] = 0
        t[f"liq_n_with_{w}m"] = 0
    t["liq_val_against_60m"] = 0.0
    t["min_since_liq"] = MIN_SINCE_CAP_MIN

    other = {"SELL": "BUY", "BUY": "SELL"}
    for (s, against), idx in t.groupby(["symbol", "_against"], sort=False).groups.items():
        tt = _ns(t.loc[idx, "open_time_utc"])
        for w in windows_min:
            wns = w * _NS_PER_MIN
            for side, col in ((against, f"liq_n_against_{w}m"), (other[against], f"liq_n_with_{w}m")):
                pack = side_arrs.get((str(s), side))
                if pack is None:
                    continue
                arr, cum = pack
                hi = np.searchsorted(arr, tt, side="left")
                lo = np.searchsorted(arr, tt - wns, side="left")
                t.loc[idx, col] = hi - lo
                if side == against and w == 60:
                    t.loc[idx, "liq_val_against_60m"] = cum[hi] - cum[lo]
        all_arr = sym_arrs.get(str(s))
        if all_arr is not None and len(all_arr):
            pos = np.searchsorted(all_arr, tt, side="left")
            mins = np.full(len(tt), MIN_SINCE_CAP_MIN)
            has = pos > 0
            mins[has] = np.minimum((tt[has] - all_arr[pos[has] - 1]) / _NS_PER_MIN, MIN_SINCE_CAP_MIN)
            t.loc[idx, "min_since_liq"] = mins

    tt_all = _ns(t["open_time_utc"])
    lo = np.searchsorted(mkt_ts, tt_all - 15 * _NS_PER_MIN, side="left")
    hi = np.searchsorted(mkt_ts, tt_all, side="left")
    t["mkt_syms_15m"] = [int(len(np.unique(mkt_sym[a:b]))) if b > a else 0 for a, b in zip(lo, hi, strict=True)]

    for w in windows_min:
        tot = t[f"liq_n_against_{w}m"] + t[f"liq_n_with_{w}m"]
        t[f"liq_imb_{w}m"] = (t[f"liq_n_against_{w}m"] - t[f"liq_n_with_{w}m"]) / tot.where(tot > 0)
    return t.drop(columns=["_against"])


def attach_funding(trades: pd.DataFrame, funding: pd.DataFrame) -> pd.DataFrame:
    """fund_24h (bps) as-of entry via the SHARED builder (Rule 7). Trades whose
    symbol lacks MIN_HISTORY settled rates get NaN → zone UNKNOWN."""
    by_sym = {str(s): g.sort_values("funding_time").reset_index(drop=True) for s, g in funding.groupby("symbol")}
    vals = []
    for sym, ts in zip(trades["symbol"], trades["open_time_utc"], strict=True):
        f = funding_features_asof(by_sym, str(sym), ts)
        vals.append(f["fund_24h"] if f else np.nan)
    t = trades.copy()
    t["fund_24h"] = vals
    return t


def funding_zone(bps: pd.Series) -> pd.Series:
    z = pd.Series("NEUTRAL", index=bps.index)
    z[bps > EXTREME_BPS] = "EXTREME_POS"
    z[bps < -EXTREME_BPS] = "EXTREME_NEG"
    z[bps.isna()] = "UNKNOWN"
    return z


def liq_state(t: pd.DataFrame) -> pd.Series:
    return pd.Series(
        np.select(
            [t["liq_n_against_60m"] >= CASCADE_MIN_N_60, t["liq_n_with_60m"] >= CASCADE_MIN_N_60],
            ["CASCADE_AGAINST", "CASCADE_WITH"],
            default="QUIET",
        ),
        index=t.index,
    )


def gate_masks(t: pd.DataFrame) -> dict[str, pd.Series]:
    """The pre-registered veto masks (True = skip the trade). NaN funding
    compares False → H1 never fires on unknown funding (fail-open)."""
    is_long = t["direction"].str.upper() == "LONG"
    crowded = (is_long & (t["fund_24h"] > EXTREME_BPS)) | (~is_long & (t["fund_24h"] < -EXTREME_BPS))
    cascade_60 = t["liq_n_against_60m"] >= CASCADE_MIN_N_60
    return {
        "H1 crowded-side flush/squeeze veto": crowded & cascade_60,
        f"H2a cascade-against-15m veto (n>={CASCADE_MIN_N_15})": t["liq_n_against_15m"] >= CASCADE_MIN_N_15,
        f"H2b cascade-against-60m veto (n>={CASCADE_MIN_N_60})": cascade_60,
        f"H3 market-cascade veto (>={MKT_CASCADE_SYMS} syms/15m)": t["mkt_syms_15m"] >= MKT_CASCADE_SYMS,
    }


# ──────────────────────────────────────────────────────────────────────────────
# evaluation
# ──────────────────────────────────────────────────────────────────────────────


def chrono_halves(t: pd.DataFrame) -> pd.Series:
    """VAL = first chronological half, TEST = second (median split)."""
    med = t["open_time_utc"].median()
    return pd.Series(np.where(t["open_time_utc"] < med, "VAL", "TEST"), index=t.index)


def _stats(pnl: pd.Series) -> dict:
    n = int(len(pnl))
    if n == 0:
        return {"n": 0, "wr": None, "avg_raw_pct": None, "median_pct": None}
    return {
        "n": n,
        "wr": round(float((pnl > 0).mean()), 4),
        "avg_raw_pct": round(float(pnl.mean()), 4),
        "median_pct": round(float(pnl.median()), 4),
    }


def eval_gate(t: pd.DataFrame, skip: pd.Series) -> dict:
    """Paired gate-on/off per direction × half. Candidate rule: kept-WR AND
    kept-raw-mean strictly better than baseline in BOTH halves, with at least
    MIN_SKIP_N skipped trades overall for that direction."""
    out: dict = {}
    for d, gd in t.groupby(t["direction"].str.upper()):
        sk = skip.loc[gd.index]
        entry: dict = {
            "all": {
                "base": _stats(gd["outcome_pct"]),
                "kept": _stats(gd.loc[~sk, "outcome_pct"]),
                "skipped": _stats(gd.loc[sk, "outcome_pct"]),
            },
        }
        both_ok = bool(sk.sum() >= MIN_SKIP_N)
        for h, gh in gd.groupby(gd["half"]):
            skh = sk.loc[gh.index]
            base, kept = _stats(gh["outcome_pct"]), _stats(gh.loc[~skh, "outcome_pct"])
            entry[h.lower()] = {"base": base, "kept": kept, "skipped": _stats(gh.loc[skh, "outcome_pct"])}
            improved = (
                base["n"] > 0
                and kept["n"] > 0
                and kept["wr"] is not None
                and base["wr"] is not None
                and kept["wr"] > base["wr"]
                and kept["avg_raw_pct"] > base["avg_raw_pct"]
            )
            both_ok = both_ok and improved
        # a direction with fewer than 2 halves populated cannot be a candidate
        both_ok = both_ok and all(k in entry for k in ("val", "test"))
        entry["candidate"] = bool(both_ok)
        out[d] = entry
    out["candidate_both_directions"] = bool(
        out.get("LONG", {}).get("candidate") and out.get("SHORT", {}).get("candidate")
    )
    return out


def cell_table(t: pd.DataFrame) -> list[dict]:
    rows = []
    for (d, fz, ls), g in t.groupby([t["direction"].str.upper(), "fund_zone", "liq_state"]):
        row = {"direction": d, "fund_zone": fz, "liq_state": ls, **_stats(g["outcome_pct"])}
        for h, gh in g.groupby("half"):
            row[f"{h.lower()}_avg_raw_pct"] = round(float(gh["outcome_pct"].mean()), 4)
            row[f"{h.lower()}_n"] = int(len(gh))
        va, te = row.get("val_avg_raw_pct"), row.get("test_avg_raw_pct")
        row["halves_sign_agree"] = None if va is None or te is None else bool(np.sign(va) == np.sign(te))
        row["below_min_n"] = bool(row["n"] < MIN_CELL_N)
        rows.append(row)
    return rows


def liq_coverage_days(liq: pd.DataFrame) -> float:
    if not len(liq):
        return 0.0
    return float((liq["ts"].max() - liq["ts"].min()).total_seconds() / 86400.0)


def restrict_to_liq_window(t: pd.DataFrame, liq: pd.DataFrame) -> pd.DataFrame:
    """Only trades whose full lookback window sits inside liq coverage — a
    trade before that has structurally-zero liq features, which would dilute
    every gate toward 'no effect' (measurement artifact, not market truth)."""
    lo = liq["ts"].min() + pd.Timedelta(minutes=WARMUP_MIN)
    hi = liq["ts"].max()
    return t[(t["open_time_utc"] >= lo) & (t["open_time_utc"] <= hi)].copy()


# ──────────────────────────────────────────────────────────────────────────────
# pipeline + report
# ──────────────────────────────────────────────────────────────────────────────


def analyse_book(trades: pd.DataFrame, liq: pd.DataFrame, funding: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    t = restrict_to_liq_window(trades, liq)
    if not len(t):
        return t, {"note": "no trades inside the liq_events coverage window"}
    t = liq_features(t, liq)
    t = attach_funding(t, funding)
    t["fund_zone"] = funding_zone(t["fund_24h"])
    t["liq_state"] = liq_state(t)
    t["half"] = chrono_halves(t)
    gates = {name: eval_gate(t, mask) for name, mask in gate_masks(t).items()}
    result = {
        "population": {
            "n": int(len(t)),
            "span_utc": [str(t["open_time_utc"].min()), str(t["open_time_utc"].max())],
            "funding_coverage": round(float(t["fund_24h"].notna().mean()), 4),
            "by_direction": {d: int(n) for d, n in t["direction"].str.upper().value_counts().items()},
        },
        "cells": cell_table(t),
        "gates": gates,
    }
    return t, result


def render_markdown(meta: dict, fleet: dict, trailing: dict | None) -> str:
    lines = [
        "# Funding × forced-liquidation entry-gate pilot (T-2026-KYT-9050-120)",
        "",
        f"_generated {meta['generated_utc']} · snapshot `{meta['snapshot']}` · "
        f"liq coverage {meta['liq_days']:.1f} days · fee/side {FEE_PER_SIDE}_",
        "",
        f"**{meta['verdict']}**",
        "",
    ]

    def gate_section(title: str, res: dict) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if "note" in res:
            lines.append(res["note"])
            lines.append("")
            return
        pop = res["population"]
        lines.append(
            f"Population: n={pop['n']} ({pop['by_direction']}), span {pop['span_utc'][0]} .. {pop['span_utc'][1]}, "
            f"funding coverage {pop['funding_coverage']:.1%}"
        )
        lines.append("")
        lines.append(
            "| gate | dir | base n | base WR | base avg% | kept WR | kept avg% | "
            "skip n | val ok | test ok | candidate |"
        )
        lines.append("|---|---|--:|--:|--:|--:|--:|--:|:--:|:--:|:--:|")
        for gname, gres in res["gates"].items():
            for d in ("LONG", "SHORT"):
                e = gres.get(d)
                if not e:
                    continue

                def _ok(h: dict | None, base_key: str = "base", kept_key: str = "kept") -> str:
                    if not h or h[base_key]["n"] == 0 or h[kept_key]["n"] == 0 or h[kept_key]["wr"] is None:
                        return "—"
                    good = (
                        h[kept_key]["wr"] > h[base_key]["wr"]
                        and h[kept_key]["avg_raw_pct"] > h[base_key]["avg_raw_pct"]
                    )
                    return "✓" if good else "✗"

                a = e["all"]
                lines.append(
                    f"| {gname} | {d} | {a['base']['n']} | {a['base']['wr']} | {a['base']['avg_raw_pct']} "
                    f"| {a['kept']['wr']} | {a['kept']['avg_raw_pct']} | {a['skipped']['n']} "
                    f"| {_ok(e.get('val'))} | {_ok(e.get('test'))} | {'YES' if e['candidate'] else 'no'} |"
                )
        lines.append("")
        lines.append(f"### Direction × funding zone × liq state (cells < {MIN_CELL_N} shown but never argued from)")
        lines.append("")
        lines.append("| dir | fund zone | liq state | n | WR | avg raw % | median % | val avg% | test avg% | agree |")
        lines.append("|---|---|---|--:|--:|--:|--:|--:|--:|:--:|")
        for c in res["cells"]:
            mark = "†" if c["below_min_n"] else ""
            lines.append(
                f"| {c['direction']} | {c['fund_zone']} | {c['liq_state']} | {c['n']}{mark} | {c['wr']} "
                f"| {c['avg_raw_pct']} | {c['median_pct']} | {c.get('val_avg_raw_pct', '—')} "
                f"| {c.get('test_avg_raw_pct', '—')} | {c.get('halves_sign_agree', '—')} |"
            )
        lines.append("")

    gate_section("Fleet book (deduped closed_ai_signals, net of fees)", fleet)
    if trailing is not None:
        gate_section("Bot-40 trailing book (unlevered marks — the T-095 arm)", trailing)

    lines += [
        "## Caveats",
        "",
        "- liq_events is a SAMPLE (1 order/s/symbol throttle) — counts/clusters/recency only; "
        "`liq_val_against_60m` is computed but secondary and carries no verdict.",
        "- Gates and thresholds were pre-registered in docs/T-2026-KYT-9050-120-funding-liq-gate-pilot.md "
        "before the conclusive run (T-116: no outcome-derived thresholds).",
        "- WR alone is not decisive (Rule 8): candidate = WR AND raw expectancy improve in BOTH chrono halves.",
        f"- Cells / directions with n < {MIN_CELL_N} (†) or skip n < {MIN_SKIP_N} are reported but never argued from.",
        "- Survivorship: funding_rates covers active USDT perps; delisted symbols lack funding (zone UNKNOWN).",
        "- Fleet PnL is the logged realized outcome net of round-trip taker fee, not a re-simulation.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", required=True, help="DuckDB file from tools/gate_snapshot_export.py")
    ap.add_argument(
        "--smoke",
        action="store_true",
        help="run below MIN_LIQ_DAYS coverage (plumbing check; report stamped NOT CONCLUDABLE)",
    )
    ap.add_argument("--out-prefix", default=os.path.join("staging_models", "funding_liq_gate_study"))
    args = ap.parse_args()

    snap = read_snapshot(args.snapshot)
    liq, funding = snap["liq"], snap["funding"]
    days = liq_coverage_days(liq)
    concludable = days >= MIN_LIQ_DAYS
    if not concludable and not args.smoke:
        print(
            f"NOT CONCLUDABLE: liq_events covers only {days:.1f} days (< {MIN_LIQ_DAYS}). "
            "Re-export and re-run ~2026-08-24, or pass --smoke for a plumbing-only run."
        )
        sys.exit(2)

    fleet_trades = prepare_trades(snap["trades"])
    _, fleet = analyse_book(fleet_trades, liq, funding)
    trailing = None
    if "trailing" in snap and len(snap["trailing"]):
        _, trailing = analyse_book(prepare_trailing(snap["trailing"]), liq, funding)

    if not concludable:
        verdict = (
            f"NOT CONCLUDABLE (plumbing run): liq coverage {days:.1f} < {MIN_LIQ_DAYS} days — "
            "numbers below validate the pipeline, they are NOT evidence."
        )
    else:
        cands = [
            f"{g} [{d}]"
            for g, r in fleet.get("gates", {}).items()
            for d in ("LONG", "SHORT")
            if r.get(d, {}).get("candidate")
        ]
        verdict = (
            "CANDIDATE GATES (need a confirmation window before any go-live talk): " + "; ".join(cands)
            if cands
            else "NO EDGE at the pre-registered gates — no funding×liq veto survives both chrono halves."
        )

    meta = {
        "task": "T-2026-KYT-9050-120",
        "generated_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "snapshot": args.snapshot,
        "liq_days": days,
        "concludable": concludable,
        "verdict": verdict,
        "params": {
            "MIN_LIQ_DAYS": MIN_LIQ_DAYS,
            "EXTREME_BPS": EXTREME_BPS,
            "WINDOWS_MIN": list(WINDOWS_MIN),
            "CASCADE_MIN_N_15": CASCADE_MIN_N_15,
            "CASCADE_MIN_N_60": CASCADE_MIN_N_60,
            "MKT_CASCADE_SYMS": MKT_CASCADE_SYMS,
            "MIN_CELL_N": MIN_CELL_N,
            "MIN_SKIP_N": MIN_SKIP_N,
            "fee_per_side": FEE_PER_SIDE,
        },
    }

    os.makedirs(os.path.dirname(args.out_prefix) or ".", exist_ok=True)
    with open(args.out_prefix + ".json", "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "fleet": fleet, "trailing": trailing}, f, indent=2, default=str)
    md = render_markdown(meta, fleet, trailing)
    with open(args.out_prefix + ".md", "w", encoding="utf-8") as f:
        f.write(md)
    print(md)
    print(f"\nwritten: {args.out_prefix}.md / .json")


if __name__ == "__main__":
    main()
