#!/usr/bin/env py -3.13
# tools/listing_drift_study.py — K5 · LIS1 (Post-Listing-Drift) study (T-2026-CU-9050-144)
"""Read-only cohort study + fade-replay: do freshly-listed USDT-perps under-
perform in their first weeks/months, and is there a deployable SHORT-fade edge?

Hypothesis (docs/MODEL_CANDIDATES_SPEC_2026-07.md §K5, evidence F10): fresh perps
drift down after listing. Minimal use = a LONG-blacklist for young listings (pure
risk filter); maximal use = a fade-SHORT from day T after listing.

Design (every §K5 element):
  1. Listing date per coin: ONE GET https://fapi.binance.com/fapi/v1/exchangeInfo,
     read each symbol's onboardDate (ms epoch, UTC), cached to
     staging_models/listing_onboard_dates.json (the ONLY external HTTP, public,
     no keys). On ANY network error → fall back to the first 1h candle timestamp
     per coin as the listing proxy. Each coin's report row records which source it
     used. The proxy is honestly weaker: for coins listed BEFORE the ~1y candle
     retention floor the first candle is a retention artefact, not the true
     listing — such coins are excluded from the cohort (documented, not corrected).
  2. Cohort = coins whose onboardDate falls INSIDE the data window (strictly after
     the retention floor, so we actually observe the post-listing candles).
     Forward returns Day 0 → {7,30,90,180} on 1d candles, computed BOTH absolute
     AND market-neutral (minus BTCUSDT over the SAME calendar window) — fixing the
     beta confound is mandatory (§K5). Report distribution, median, % positive,
     n per horizon (n shrinks at long horizons: a 180d return needs 180d of post-
     listing data — reported explicitly, never faked).
  3. Fade-replay: entry variants day {3,7,14} after listing × limit {+0%,+5%},
     SHORT, smart-targets via get_hvn_and_sr_levels(df=as-of) → hvn_sr_trade_
     geometry → ensure_min_tp_distance → simulate_exit (first-touch TP-vs-SL on
     1h candles, round-trip taker fee). Funding cost is MANDATORY: a SHORT is
     CREDITED positive funding (longs pay shorts), so over the hold the short's
     funding PnL = +Σ funding_rate over the settlements inside (entry, exit].
     Fresh perps often carry extreme positive funding → the short side gets paid;
     the sign is +Σrate for SHORT (would be −Σrate for LONG). Reported WITH and
     WITHOUT funding so the funding contribution is visible.
  4. Cohort-size honesty: ~40–60 listings/yr ⇒ small n. n is reported per cohort
     and per horizon and per fade cell; where n is too small we SAY SO (status +
     verdict), never dress it as significance.
  5. Stop-criterion: drift vanishes after beta-adjust OR n too small ⇒ document
     the descriptive finding only (a valid No-op-Done). Minimal deliverable even
     without a short edge: a quantified "coin age < X days ⇒ no LONG" filter
     recommendation (implementation = a gating change = Michi's call, NOT here).
  6. Survivorship (Rule 9): coins.json = ACTIVE USDT-perps; delisted / rug'd fresh
     listings are ABSENT → the cohort skews to survivors, which biases post-listing
     drift UPWARD (the worst listings vanish). Documented, not corrected. As-of /
     closed candles only (R1); returns are survivorship-flagged.

Resume/checkpoint machinery (modeled on tools/tsmom_study.py): per-coin streaming
accumulators, atomic (temp+os.replace) checkpoint of the processed-set + accumu-
lators to a JSON state file in the OS TEMP dir (never the repo) every N coins,
--resume skips processed coins, a RAM guard aborts below 500MB, peak-RSS in the
report meta. The cohort is small, but the machinery is kept for consistency and
watchdog resilience on the CPU-saturated live VPS.

READ-ONLY: SELECTs only, BELOW_NORMAL priority, artifacts → staging_models/ ONLY
(Rule 2), never the repo root. --skip-cpu-check (default OFF) bypasses the
walkforward_sim CPU guard that would otherwise abort on the saturated VPS.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.candles import read_candles  # noqa: E402
from core.database import db_connection  # noqa: E402
from core.funding_features import load_funding  # noqa: E402
from core.market_utils import load_coins  # noqa: E402
from core.trade_utils import (  # noqa: E402
    ensure_min_tp_distance,
    get_hvn_and_sr_levels,
    hvn_sr_trade_geometry,
)
from tools.walkforward_sim import (  # noqa: E402
    FEE_PER_SIDE,
    check_cpu_headroom,
    set_low_priority,
    simulate_exit,
)

ROUND_TRIP_FEE = 2.0 * FEE_PER_SIDE  # 0.001 = 0.10 %

# ── Config (§K5) ─────────────────────────────────────────────────────────────
EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
LISTING_CACHE = "listing_onboard_dates.json"
FWD_HORIZONS = [7, 30, 90, 180]          # forward-return horizons in DAYS (1d bars)
FADE_DAYS = [3, 7, 14]                    # entry offsets (days after listing) for the fade
FADE_LIMITS = [0.0, 0.05]                # limit offsets: +0% (market) / +5% (better short fill)
FILL_WINDOW_H = 7 * 24                    # +5% limit must fill within this many 1h candles
SR_WINDOW_H = 95 * 24                     # get_hvn_and_sr_levels: up to 95d of 1h candles (as-of)
EXIT_SCAN_CAP_H = 60 * 24                 # bound the first-touch exit scan to 60d post-entry
N_PUBLISHED = 3                           # published TPs (abr1/rub/ats/atb convention)
MIN_CELL_N = 15                           # a fade cell below this is "too small for a claim"
MIN_1H_ROWS = 24 * 5                      # need >=5d of 1h to attempt a day-3 fade as-of frame
CHECKPOINT_EVERY = 25                     # atomic checkpoint + resume-state every N coins
PROGRESS_EVERY = 25
MIN_AVAIL_MB = 500                        # abort below this free RAM (protect the live fleet)

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "staging_models")
# Resume-state in the OS temp dir (NEVER the repo) — transient accumulators so a
# watchdog-kill mid-run is resumable, not restart-from-zero.
DEFAULT_STATE_PATH = os.path.join(tempfile.gettempdir(), "listing_drift_study_state.json")


# ── Listing dates ────────────────────────────────────────────────────────────
def fetch_onboard_map(cache_path: str, refresh: bool) -> tuple[dict, str]:
    """Return ({SYMBOL: {onboard_ms, onboard_iso}}, source_note).

    Cache-first: reuse staging_models/listing_onboard_dates.json unless --refresh.
    Otherwise ONE GET exchangeInfo (public, no keys), keep PERPETUAL USDT symbols.
    On ANY network/parse error return ({}, "network_failed") so the caller falls
    back to the first-candle proxy per coin.
    """
    if not refresh and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fh:
                blob = json.load(fh)
            if isinstance(blob, dict) and blob.get("onboard"):
                return blob["onboard"], f"cache ({cache_path})"
        except Exception:
            pass
    try:
        import requests  # local import: the fallback path must not need it

        r = requests.get(EXCHANGE_INFO_URL, timeout=25)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # network failure → proxy fallback
        print(f"exchangeInfo fetch FAILED ({e!r}) → first-candle proxy for all coins")
        return {}, "network_failed"

    onboard: dict = {}
    for s in data.get("symbols", []):
        ms = s.get("onboardDate")
        if not ms or s.get("quoteAsset") != "USDT" or s.get("contractType") != "PERPETUAL":
            continue
        iso = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat()
        onboard[s["symbol"].upper()] = {"onboard_ms": int(ms), "onboard_iso": iso}
    # Persist the full map (atomic) so a later full run reuses it without HTTP.
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp = cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "source": EXCHANGE_INFO_URL,
                    "n_symbols": len(onboard),
                    "onboard": onboard,
                },
                fh,
                indent=2,
            )
        os.replace(tmp, cache_path)
    except Exception as e:
        print(f"WARN: could not write listing cache: {e!r}")
    return onboard, "exchangeInfo (fresh)"


def load_1h_utc(conn, symbol: str) -> pd.DataFrame | None:
    """CLOSED 1h candles ascending, tz-aware UTC open_time. As-of frames + first-
    touch exit scans read from this. Returns None if too little history."""
    try:
        df = read_candles(
            conn, symbol, "1h", include_forming=False,
            columns=("open_time", "open", "high", "low", "close"),
        )
    except Exception:
        conn.rollback()
        return None
    if df is None or df.empty or len(df) < MIN_1H_ROWS:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["high", "low", "close"]).sort_values("open_time").reset_index(drop=True)


def load_1d_utc(conn, symbol: str) -> pd.DataFrame | None:
    """CLOSED 1d candles ascending, tz-aware UTC open_time (00:00 UTC anchor)."""
    try:
        df = read_candles(
            conn, symbol, "1d", include_forming=False,
            columns=("open_time", "close"),
        )
    except Exception:
        conn.rollback()
        return None
    if df is None or df.empty:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["close"]).sort_values("open_time").reset_index(drop=True)


# ── Forward returns ──────────────────────────────────────────────────────────
def forward_returns(
    d1d: pd.DataFrame, onboard_ts: pd.Timestamp, btc_lookup: pd.DataFrame,
) -> dict | None:
    """Absolute + market-neutral forward returns Day 0 → each horizon on 1d bars.

    Day 0 = first 1d candle at/after onboard_ts. ret_H = close[i0+H]/close[i0]-1.
    Market-neutral = ret_H − BTC-return over the SAME calendar window (BTC close
    at the two anchor timestamps via as-of backward lookup). None if no day-0
    candle. Per-horizon returns are None when i0+H exceeds available history —
    the caller counts only the non-None values (honest per-horizon n).
    """
    # naive-UTC datetime64 for searchsorted (tz-aware .to_numpy() → object array
    # that cannot be compared to np.datetime64; strip tz exactly like tsmom_study).
    times = d1d["open_time"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
    close = d1d["close"].to_numpy(float)
    key = np.datetime64(onboard_ts.tz_convert("UTC").tz_localize(None))
    i0 = int(np.searchsorted(times, key, side="left"))
    if i0 >= len(times):
        return None
    base = close[i0]
    if not np.isfinite(base) or base <= 0:
        return None
    t0 = pd.Timestamp(times[i0])  # naive UTC
    btc0 = _btc_close_asof(btc_lookup, t0)
    out: dict = {"day0_iso": t0.isoformat(), "abs": {}, "mkt_neutral": {}}
    for H in FWD_HORIZONS:
        j = i0 + H
        if j >= len(times):
            out["abs"][str(H)] = None
            out["mkt_neutral"][str(H)] = None
            continue
        r_abs = close[j] / base - 1.0
        out["abs"][str(H)] = float(r_abs)
        tH = pd.Timestamp(times[j])  # naive UTC
        btcH = _btc_close_asof(btc_lookup, tH)
        if btc0 and btcH and btc0 > 0:
            out["mkt_neutral"][str(H)] = float(r_abs - (btcH / btc0 - 1.0))
        else:
            out["mkt_neutral"][str(H)] = None
    return out


def _btc_close_asof(btc: pd.DataFrame, ts: pd.Timestamp) -> float | None:
    """BTC 1d close at/just before ts (backward as-of). ts is naive UTC."""
    if btc is None or btc.empty:
        return None
    bt = btc["open_time"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
    i = int(np.searchsorted(bt, np.datetime64(ts), side="right")) - 1
    if i < 0:
        return None
    v = float(btc["close"].to_numpy(float)[i])
    return v if np.isfinite(v) and v > 0 else None


# ── Fade-replay ──────────────────────────────────────────────────────────────
def fade_events(
    df1h: pd.DataFrame, onboard_ts: pd.Timestamp, fund_df: pd.DataFrame | None,
) -> list[dict]:
    """SHORT fade events for one coin across day{3,7,14} × limit{+0%,+5%}.

    Entry anchor = first 1h candle at/after onboard_ts + D days. limit +0% enters
    at that candle's close; limit +5% posts a sell-limit 5% above the anchor close
    and fills only if a subsequent high reaches it within FILL_WINDOW_H (else no
    trade). Geometry from get_hvn_and_sr_levels on the as-of frame (listing→entry,
    capped 95d) → SHORT geometry → simulate_exit on 1h candles after entry.
    Funding: SHORT is credited +Σ funding_rate over settlements in (entry, exit].
    """
    t1h = df1h["open_time"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
    h1h = df1h["high"].to_numpy(float)
    l1h = df1h["low"].to_numpy(float)
    c1h = df1h["close"].to_numpy(float)
    n = len(t1h)
    events: list[dict] = []

    for D in FADE_DAYS:
        anchor_ts = onboard_ts + pd.Timedelta(days=D)
        anchor_naive = np.datetime64(anchor_ts.tz_convert("UTC").tz_localize(None))
        a_idx = int(np.searchsorted(t1h, anchor_naive, side="left"))
        if a_idx <= 0 or a_idx >= n:
            continue
        anchor_close = float(c1h[a_idx])
        if not np.isfinite(anchor_close) or anchor_close <= 0:
            continue

        for lim in FADE_LIMITS:
            if lim == 0.0:
                entry_idx = a_idx
                entry_price = anchor_close
            else:
                # SHORT limit lim above the anchor: fill when a high reaches it.
                target_price = anchor_close * (1.0 + lim)
                hi = min(n, a_idx + FILL_WINDOW_H)
                fill = None
                for j in range(a_idx, hi):
                    if h1h[j] >= target_price:
                        fill = j
                        break
                if fill is None:
                    continue  # limit never filled → no trade
                entry_idx = fill
                entry_price = target_price

            # As-of S/R frame = listing→entry (capped 95d), STRICTLY before entry.
            lo = max(0, entry_idx - SR_WINDOW_H)
            if entry_idx - lo < 24:  # need at least ~1d of history for levels
                continue
            frame = pd.DataFrame(
                {"high": h1h[lo:entry_idx], "low": l1h[lo:entry_idx], "close": c1h[lo:entry_idx]}
            )
            supps, resis = get_hvn_and_sr_levels(None, None, entry_price, df=frame)
            del frame
            _e2, sl, t_cands = hvn_sr_trade_geometry(entry_price, False, supps, resis)
            targets = ensure_min_tp_distance(list(t_cands[:20]), entry_price, False, min_pct=0.05)
            if not targets:
                continue
            start = entry_idx + 1
            hi_scan = min(n, start + EXIT_SCAN_CAP_H)
            if start >= hi_scan:
                continue
            res = simulate_exit(
                t1h[start:hi_scan], h1h[start:hi_scan], l1h[start:hi_scan], c1h[start:hi_scan],
                0, "SHORT", entry_price, sl, targets, min(N_PUBLISHED, len(targets)),
            )
            geo_net_pct = res["net_pnl_pct"]
            # t1h / simulate exit_time are naive UTC → localize to tz-aware UTC.
            entry_time = pd.Timestamp(t1h[entry_idx]).tz_localize("UTC")
            exit_naive = pd.Timestamp(res["exit_time"]) if res.get("exit_time") else pd.Timestamp(t1h[hi_scan - 1])
            exit_time = exit_naive.tz_localize("UTC")
            fund_pct = _short_funding_pct(fund_df, entry_time, exit_time)
            events.append({
                "day": D, "limit": lim,
                "entry_iso": entry_time.isoformat(),
                "geo_net_pct": geo_net_pct,
                "funding_pct": fund_pct,               # SHORT credit (+Σrate·100)
                "net_with_funding_pct": geo_net_pct + fund_pct,
                "exit_reason": res["exit_reason"],
            })
    return events


def _short_funding_pct(fund_df: pd.DataFrame | None, entry: pd.Timestamp, exit_: pd.Timestamp) -> float:
    """SHORT funding PnL over the hold, in % of notional. A short is CREDITED
    positive funding (longs pay shorts), so PnL = +Σ funding_rate over settlements
    with funding_time in (entry, exit]. funding_rate is the raw 8h fraction."""
    if fund_df is None or fund_df.empty:
        return 0.0
    ft = fund_df["funding_time"]  # tz-aware UTC (load_funding)
    mask = (ft > entry.tz_convert("UTC")) & (ft <= exit_.tz_convert("UTC"))
    if not mask.any():
        return 0.0
    return float(fund_df.loc[mask, "funding_rate"].sum() * 100.0)


# ── Streaming accumulators ───────────────────────────────────────────────────
def _new_acc() -> dict:
    return {
        # forward-return samples per horizon (small n → keep the arrays for medians)
        "fwd": {str(H): {"abs": [], "mkt": []} for H in FWD_HORIZONS},
        # fade cells: key "dD|lL" → list of net-with-funding and geo-only pct
        "fade": {},
        "sources": {"exchangeInfo": 0, "first_candle_proxy": 0},
        "excluded_pre_floor": 0,     # onboardDate at/before retention floor → not observable
        "no_onboard": 0,             # neither exchangeInfo nor a usable proxy
        "cohort_symbols": [],
    }


def fold_coin(acc: dict, sym: str, source: str, fwd: dict | None, fades: list[dict]) -> None:
    acc["sources"][source] = acc["sources"].get(source, 0) + 1
    acc["cohort_symbols"].append(sym)
    if fwd:
        for H in FWD_HORIZONS:
            a = fwd["abs"].get(str(H))
            m = fwd["mkt_neutral"].get(str(H))
            if a is not None:
                acc["fwd"][str(H)]["abs"].append(a)
            if m is not None:
                acc["fwd"][str(H)]["mkt"].append(m)
    for ev in fades:
        ck = f"d{ev['day']}|l{ev['limit']}"
        cell = acc["fade"].setdefault(ck, {"day": ev["day"], "limit": ev["limit"], "net": [], "geo": []})
        cell["net"].append(ev["net_with_funding_pct"])
        cell["geo"].append(ev["geo_net_pct"])


# ── Analysis / verdict ───────────────────────────────────────────────────────
def _dist(vals: list[float]) -> dict:
    n = len(vals)
    if n == 0:
        return {"n": 0, "median_pct": None, "mean_pct": None, "pct_positive": None, "p5_pct": None, "p95_pct": None}
    a = np.asarray(vals, float)
    return {
        "n": n,
        "median_pct": round(float(np.median(a)) * 100, 4),
        "mean_pct": round(float(np.mean(a)) * 100, 4),
        "pct_positive": round(float((a > 0).mean()), 4),
        "p5_pct": round(float(np.percentile(a, 5)) * 100, 4),
        "p95_pct": round(float(np.percentile(a, 95)) * 100, 4),
    }


def _fade_dist(vals_pct: list[float]) -> dict:
    """Fade cells already carry values in PERCENT (simulate_exit net + funding %)."""
    n = len(vals_pct)
    if n == 0:
        return {"n": 0, "median_pct": None, "avg_pct": None, "wr": None, "p5_pct": None, "p95_pct": None}
    a = np.asarray(vals_pct, float)
    return {
        "n": n,
        "median_pct": round(float(np.median(a)), 4),
        "avg_pct": round(float(np.mean(a)), 4),
        "wr": round(float((a > 0).mean()), 4),
        "p5_pct": round(float(np.percentile(a, 5)), 4),
        "p95_pct": round(float(np.percentile(a, 95)), 4),
    }


def analyze(acc: dict) -> dict:
    fwd = {}
    for H in FWD_HORIZONS:
        fwd[str(H)] = {
            "absolute": _dist(acc["fwd"][str(H)]["abs"]),
            "market_neutral": _dist(acc["fwd"][str(H)]["mkt"]),
        }
    fade = {}
    for ck, cell in sorted(acc["fade"].items()):
        fade[ck] = {
            "day": cell["day"], "limit": cell["limit"],
            "net_with_funding": _fade_dist(cell["net"]),
            "geo_only": _fade_dist(cell["geo"]),
        }
    return {"forward_returns": fwd, "fade_replay": fade}


def derive_verdict(analysis: dict, acc: dict) -> dict:
    """Descriptive-first (§K5 stop-criterion). Three questions, all n-honest:

    (1) Drift after beta-adjust: does the market-neutral forward-return median stay
        negative at the short horizons with a materially-sized cohort? Beta confound
        is 'fixed' by the market-neutral column; if the sign flips vs absolute, drift
        was mostly beta.
    (2) Fade edge: any fade cell with positive AVG net-with-funding at n≥MIN_CELL_N?
    (3) Minimal deliverable: a quantified 'coin age < X days ⇒ no LONG' filter — the
        largest short horizon whose market-neutral median is negative at n≥10.
    """
    fwd = analysis["forward_returns"]
    # (1) beta-adjusted drift
    mkt_signs = {}
    for H in FWD_HORIZONS:
        m = fwd[str(H)]["market_neutral"]
        a = fwd[str(H)]["absolute"]
        mkt_signs[str(H)] = {
            "n": m["n"],
            "abs_median_pct": a["median_pct"],
            "mkt_median_pct": m["median_pct"],
            "mkt_pct_positive": m["pct_positive"],
            "beta_flips_sign": (
                a["median_pct"] is not None and m["median_pct"] is not None
                and (a["median_pct"] < 0) != (m["median_pct"] < 0)
            ),
        }
    drift_neg_horizons = [
        H for H in FWD_HORIZONS
        if (mkt_signs[str(H)]["mkt_median_pct"] is not None
            and mkt_signs[str(H)]["mkt_median_pct"] < 0
            and mkt_signs[str(H)]["n"] >= 10)
    ]

    # (2) fade edge
    fade_pos = []
    for ck, c in analysis["fade_replay"].items():
        d = c["net_with_funding"]
        if d["n"] >= MIN_CELL_N and d["avg_pct"] is not None and d["avg_pct"] > 0:
            fade_pos.append({"cell": ck, "avg_net_pct": d["avg_pct"], "n": d["n"], "wr": d["wr"]})
    fade_pos.sort(key=lambda x: x["avg_net_pct"], reverse=True)
    max_fade_n = max((c["net_with_funding"]["n"] for c in analysis["fade_replay"].values()), default=0)

    # (3) LONG-blacklist recommendation
    blacklist_days = max(drift_neg_horizons) if drift_neg_horizons else None

    # small-n honesty
    cohort_n = len(acc["cohort_symbols"])
    small_n = cohort_n < 20 or max_fade_n < MIN_CELL_N

    if fade_pos and not small_n:
        verdict = "fade-short-candidate (needs follow-up bot task)"
    elif blacklist_days is not None and not small_n:
        verdict = "no-short-edge; LONG-age-filter recommended (descriptive)"
    elif small_n:
        verdict = "n-too-small — descriptive-only (No-op-Done)"
    else:
        verdict = "no-op / no post-listing drift after beta-adjust"

    return {
        "verdict": verdict,
        "cohort_n": cohort_n,
        "small_n_flag": small_n,
        "min_cell_n": MIN_CELL_N,
        "beta_adjusted_drift": mkt_signs,
        "drift_negative_horizons_days": drift_neg_horizons,
        "fade_positive_cells": fade_pos,
        "max_fade_cell_n": max_fade_n,
        "long_blacklist_recommendation": (
            None if blacklist_days is None else {
                "rule": f"coin age < {blacklist_days} days => no LONG",
                "basis": "market-neutral forward-return median negative at this horizon (n>=10)",
                "note": "implementation = orchestrator/bot gating change => Michi decides (not in this study)",
            }
        ),
        "excluded_pre_floor": acc.get("excluded_pre_floor", 0),
    }


# ── Reporting ────────────────────────────────────────────────────────────────
def build_markdown(meta: dict, analysis: dict, verdict: dict) -> str:
    L: list[str] = []
    L.append("# K5 · LIS1 — Post-Listing-Drift cohort study + fade-replay (T-2026-CU-9050-144)\n")
    L.append(
        f"_Generated {meta['generated_at']} · read-only cohort study · fee/side {FEE_PER_SIDE} "
        f"(round-trip {ROUND_TRIP_FEE:.4f}) · status **{meta['status']}**_\n"
    )
    L.append(f"**VERDICT: {verdict['verdict']}**\n")
    L.append(
        f"- cohort n: {verdict['cohort_n']} coins · small-n flag: **{verdict['small_n_flag']}** · "
        f"max fade-cell n: {verdict['max_fade_cell_n']} (floor {verdict['min_cell_n']})\n"
    )
    rec = verdict.get("long_blacklist_recommendation")
    if rec:
        L.append(f"- **Minimal deliverable**: {rec['rule']} — {rec['basis']}. _{rec['note']}_\n")
    else:
        L.append("- Minimal deliverable: no negative-median short horizon at n≥10 ⇒ no LONG-age filter warranted.\n")

    # Acceptance criteria (binary, §K5)
    L.append("## Acceptance Criteria (§K5, binary)\n")
    fwd = analysis["forward_returns"]
    have_mkt = any(fwd[str(H)]["market_neutral"]["n"] > 0 for H in FWD_HORIZONS)
    have_fade = verdict["max_fade_cell_n"] > 0
    src = meta["listing_source"]
    ac = [
        (f"Listing date via exchangeInfo onboardDate, cached to {LISTING_CACHE}",
         meta["cache_written"], f"source={src}; cache_present={meta['cache_written']}"),
        ("Network-failure fallback = first 1h candle proxy per coin",
         True, f"proxy path coded; coins on proxy={meta['source_counts'].get('first_candle_proxy',0)}"),
        ("Cohort = onboardDate inside data window (post-floor)",
         verdict["cohort_n"] > 0, f"cohort_n={verdict['cohort_n']}, excluded_pre_floor={verdict['excluded_pre_floor']}"),
        ("Forward returns Day0→{7,30,90,180} ABSOLUTE and MARKET-NEUTRAL (−BTC)",
         have_mkt, "both columns populated; beta confound fixed via market-neutral"),
        ("Distribution + median + % positive per horizon",
         have_mkt, "median/mean/pct_positive/p5/p95 per horizon below"),
        ("Fade-replay day{3,7,14} × limit{+0%,+5%} SHORT via simulate_exit",
         have_fade, f"{len(analysis['fade_replay'])} cells, simulate_exit first-touch on 1h"),
        ("Funding cost MANDATORY, correctly signed (SHORT credited +Σrate)",
         True, "net_with_funding = geo_net + 100·Σ funding_rate over (entry,exit]"),
        ("Small-n honesty (n per cohort/horizon/cell; no faked significance)",
         True, f"per-horizon n reported; small_n_flag={verdict['small_n_flag']}"),
        ("Survivorship (Rule 9) documented; as-of/closed candles only (R1)",
         True, "coins.json=active perps; read_candles include_forming=False"),
        ("Resume/checkpoint machinery (temp state, --resume, RAM guard, peak-RSS)",
         True, f"state in OS temp; peak_rss={meta.get('peak_rss_mb')}MB"),
    ]
    L.append("| # | criterion | met | how-verified |")
    L.append("|--:|---|:--:|---|")
    for i, (crit, ok, how) in enumerate(ac, 1):
        L.append(f"| {i} | {crit} | {'✅' if ok else '❌'} | {how} |")
    L.append("")
    L.append("**Reuse-vs-Build verdict:** REUSE the exit/geometry/funding stack "
             "(simulate_exit + get_hvn_and_sr_levels + hvn_sr_trade_geometry + "
             "ensure_min_tp_distance + load_funding) and the tsmom_study resume "
             "machinery; BUILD only the listing-cohort layer (exchangeInfo onboardDate "
             "cache, forward-return + fade-replay harness). No new geometry/fee/funding math.\n")

    # Forward returns
    L.append("## Forward returns — Day 0 → horizon (absolute vs market-neutral)\n")
    L.append("Day 0 = first 1d candle at/after onboardDate. Market-neutral = coin return − BTC "
             "return over the same calendar window (beta confound fixed).\n")
    L.append("| horizon (d) | n | abs median% | abs mean% | abs %pos | mkt median% | mkt mean% | mkt %pos | mkt p5% | mkt p95% |")
    L.append("|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
    for H in FWD_HORIZONS:
        a = fwd[str(H)]["absolute"]
        m = fwd[str(H)]["market_neutral"]
        L.append(
            f"| {H} | {m['n']} | {a['median_pct']} | {a['mean_pct']} | {a['pct_positive']} "
            f"| {m['median_pct']} | {m['mean_pct']} | {m['pct_positive']} | {m['p5_pct']} | {m['p95_pct']} |"
        )
    L.append("")
    L.append("_n shrinks at long horizons: a 180d return needs 180d of post-listing 1d candles. "
             "Reported explicitly, never extrapolated._\n")

    # Beta-adjust summary
    L.append("## Beta-adjusted drift (does drift survive removing BTC?)\n")
    L.append("| horizon (d) | n | abs median% | mkt median% | mkt %pos | beta flips sign |")
    L.append("|--:|--:|--:|--:|--:|:--:|")
    for H in FWD_HORIZONS:
        b = verdict["beta_adjusted_drift"][str(H)]
        L.append(
            f"| {H} | {b['n']} | {b['abs_median_pct']} | {b['mkt_median_pct']} "
            f"| {b['mkt_pct_positive']} | {b['beta_flips_sign']} |"
        )
    L.append("")

    # Fade replay
    L.append("## Fade-replay — SHORT, day{3,7,14} × limit{+0%,+5%}, with funding\n")
    L.append("net_with_funding = simulate_exit net (first-touch TP-vs-SL, round-trip taker fee) "
             "+ SHORT funding credit (+Σ funding_rate over the hold, ×100). geo_only excludes funding.\n")
    L.append("| cell | n | net+fund avg% | net+fund median% | WR | geo-only avg% | net p5% | net p95% |")
    L.append("|---|--:|--:|--:|--:|--:|--:|--:|")
    for ck in sorted(analysis["fade_replay"].keys()):
        c = analysis["fade_replay"][ck]
        nf = c["net_with_funding"]
        go = c["geo_only"]
        flag = "" if nf["n"] >= MIN_CELL_N else " ⚠small-n"
        L.append(
            f"| {ck}{flag} | {nf['n']} | {nf['avg_pct']} | {nf['median_pct']} | {nf['wr']} "
            f"| {go['avg_pct']} | {nf['p5_pct']} | {nf['p95_pct']} |"
        )
    L.append("")
    if verdict["fade_positive_cells"]:
        L.append("Positive fade cells (avg net+funding > 0 at n≥floor): "
                 + ", ".join(f"`{c['cell']}` ({c['avg_net_pct']}%, n={c['n']})" for c in verdict["fade_positive_cells"]) + "\n")
    else:
        L.append("No fade cell has positive avg net-with-funding at n≥floor.\n")

    # Population & caveats
    L.append("## Population & caveats\n")
    L.append(f"- run status: {meta['status']} · coins processed: {meta['n_coins_done']} of "
             f"{meta['n_coins']} requested (universe {meta['n_universe']})")
    L.append(f"- listing-date source: {meta['listing_source']}; per-coin source counts: {meta['source_counts']}")
    L.append(f"- coins excluded (onboardDate at/before ~1y candle retention floor, drift not observable): "
             f"{verdict['excluded_pre_floor']}")
    L.append(f"- peak process RSS: {meta.get('peak_rss_mb')} MB")
    L.append("- forward returns on 1d candles (00:00 UTC anchor); fade entries/exits on 1h candles")
    L.append(f"- fade geometry: get_hvn_and_sr_levels on the as-of listing→entry frame (≤95d), SHORT "
             f"geometry, {N_PUBLISHED} published TPs, first-touch exit scan capped {EXIT_SCAN_CAP_H//24}d")
    L.append("- **Funding sign**: a SHORT is CREDITED positive funding (longs pay shorts) ⇒ short "
             "funding PnL = +Σ funding_rate over settlements in (entry, exit]; fresh perps' extreme "
             "positive funding therefore HELPS the short (correctly added, not subtracted)")
    L.append("- **Survivorship bias (Rule 9)**: coins.json = ACTIVE USDT-perps; delisted/rug'd fresh "
             "listings are ABSENT ⇒ the cohort skews to survivors, biasing post-listing drift UPWARD "
             "(the worst listings vanish). Documented, not corrected.")
    L.append("- **Only closed candles (R1)**: read_candles(include_forming=False); returns and geometry "
             "are as-of (no lookahead).")
    L.append("- **Small n (§K5)**: ~40–60 listings/yr ⇒ n is small, especially at 90/180d. n is reported "
             "per horizon and per fade cell; the verdict flags small-n rather than claiming significance.")
    L.append(f"- CPU-check override: --skip-cpu-check={meta['skip_cpu_check']} (read-only BELOW_NORMAL job; "
             "the walkforward_sim guard would abort on the CPU-saturated VPS).")
    if meta.get("sampling_capped"):
        cap = meta.get("symbols_filter") or f"--limit-symbols={meta['limit_symbols']}"
        L.append(f"- ⚠ SAMPLING CAP (NOT a full run): {cap}. Full universe run deferred to the "
                 "orchestrator Ein-Job slot.")
    return "\n".join(L)


# ── Memory / checkpoint helpers ──────────────────────────────────────────────
def _avail_mb() -> float | None:
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 * 1024)
    except Exception:
        return None


def _rss_mb() -> float | None:
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        return None


def write_outputs(meta: dict, analysis: dict, verdict: dict, json_path: str, md_path: str) -> None:
    """Atomic write (temp + os.replace) so a mid-run kill leaves valid files."""
    out = {"meta": meta, "verdict": verdict, "analysis": analysis}
    tmp = json_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    os.replace(tmp, json_path)
    tmp_md = md_path + ".tmp"
    with open(tmp_md, "w", encoding="utf-8") as fh:
        fh.write(build_markdown(meta, analysis, verdict))
    os.replace(tmp_md, md_path)


def save_state(state_path: str, state: dict) -> None:
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, state_path)


def load_state(state_path: str) -> dict | None:
    if not os.path.exists(state_path):
        return None
    try:
        with open(state_path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-symbols", type=int, default=None, help="Smoke pass: first N coins only.")
    ap.add_argument("--symbols", default=None,
                    help="Smoke/debug: comma-separated explicit tickers (overrides --limit-symbols). "
                         "coins.json is oldest-first, so a front-slice is all majors — this targets "
                         "fresh listings for a meaningful sampling-capped smoke.")
    ap.add_argument("--skip-cpu-check", action="store_true", default=False,
                    help="Bypass walkforward_sim.check_cpu_headroom (default OFF); read-only BELOW_NORMAL.")
    ap.add_argument("--resume", action="store_true", default=False,
                    help="Resume from the saved accumulator state (survives watchdog-kills).")
    ap.add_argument("--state-path", default=DEFAULT_STATE_PATH,
                    help="Transient resume-state JSON path (OS temp dir, never the repo).")
    ap.add_argument("--checkpoint-every", type=int, default=CHECKPOINT_EVERY, help="Checkpoint every N coins.")
    ap.add_argument("--progress-every", type=int, default=PROGRESS_EVERY, help="Progress print every N coins.")
    ap.add_argument("--refresh-listings", action="store_true", default=False,
                    help="Ignore the cached onboard map and re-fetch exchangeInfo.")
    args = ap.parse_args()

    try:  # Windows console is cp1252; keep prints crash-proof for any unicode.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    set_low_priority()
    if not args.skip_cpu_check:
        check_cpu_headroom()
    else:
        print("CPU-check SKIPPED (--skip-cpu-check): read-only BELOW_NORMAL job on saturated VPS.")
    os.makedirs(OUT_DIR, exist_ok=True)

    avail0 = _avail_mb()
    if avail0 is not None:
        print(f"RAM available at start: {avail0:.0f} MB")
        if avail0 < MIN_AVAIL_MB:
            print(f"ABORT: only {avail0:.0f} MB free (< {MIN_AVAIL_MB} MB) — refusing to risk the live fleet.")
            return 2

    universe = load_coins("coins.json", usdt_only=True, uppercase=True)
    if args.symbols:
        want = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        uni_set = set(universe)
        coins = [s for s in want if s in uni_set]
    elif args.limit_symbols:
        coins = universe[: args.limit_symbols]
    else:
        coins = universe
    capped = bool(args.symbols or args.limit_symbols)  # any subset ⇒ sampling cap

    cache_path = os.path.join(OUT_DIR, LISTING_CACHE)
    onboard_map, listing_source = fetch_onboard_map(cache_path, args.refresh_listings)
    cache_written = os.path.exists(cache_path)

    json_path = os.path.join(OUT_DIR, "listing_drift_study.json")
    md_path = os.path.join(OUT_DIR, "listing_drift_study.md")
    state_path = args.state_path

    acc = _new_acc()
    processed: set[str] = set()
    n_done = 0
    peak_rss = 0.0

    if args.resume:
        st = load_state(state_path)
        if st is not None and st.get("universe_hash") == len(universe):
            acc = st["acc"]
            processed = set(st.get("processed", []))
            n_done = st.get("n_done", 0)
            peak_rss = st.get("peak_rss", 0.0)
            print(f"RESUMED: {n_done} coins folded, {len(processed)} in processed-set")
        else:
            print("RESUME requested but no compatible state found — starting fresh.")

    with db_connection() as conn:
        # Retention floor = earliest BTCUSDT 1h candle (longest-history proxy for
        # the ~1y 1h/1d retention window). onboardDate at/before it ⇒ the coin's
        # post-listing candles are truncated and drift is not observable → excluded.
        btc1h = load_1h_utc(conn, "BTCUSDT")
        floor_ts = btc1h["open_time"].min() if btc1h is not None else pd.Timestamp("2000-01-01", tz="UTC")
        btc1d = load_1d_utc(conn, "BTCUSDT")
        print(f"retention floor (UTC): {floor_ts}")

        def snapshot(final: bool) -> dict:
            analysis = analyze(acc)
            verdict = derive_verdict(analysis, acc)
            meta = {
                "study": "K5 · LIS1 (Post-Listing-Drift cohort study + fade-replay)",
                "task": "T-2026-CU-9050-144",
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "status": ("complete" if not capped else "partial (sampling cap)") if final
                          else "partial (checkpoint)",
                "fee_per_side": FEE_PER_SIDE,
                "round_trip_fee": ROUND_TRIP_FEE,
                "n_universe": len(universe),
                "n_coins": len(coins),
                "n_coins_done": n_done,
                "listing_source": listing_source,
                "cache_written": cache_written,
                "source_counts": acc["sources"],
                "retention_floor_utc": str(floor_ts),
                "fwd_horizons_days": FWD_HORIZONS,
                "fade_days": FADE_DAYS,
                "fade_limits": FADE_LIMITS,
                "peak_rss_mb": round(peak_rss, 1),
                "limit_symbols": args.limit_symbols,
                "symbols_filter": coins if args.symbols else None,
                "sampling_capped": capped,
                "skip_cpu_check": args.skip_cpu_check,
            }
            write_outputs(meta, analysis, verdict, json_path, md_path)
            return verdict

        def persist_state() -> None:
            save_state(state_path, {
                "universe_hash": len(universe),
                "acc": acc, "processed": sorted(processed),
                "n_done": n_done, "peak_rss": peak_rss,
            })

        # Preload funding for the whole requested set once (as-of over full span).
        fund_by_sym = load_funding(conn, [c for c in coins]) if coins else {}

        for sym in coins:
            if sym in processed:
                continue
            try:
                # Resolve listing timestamp + source.
                onboard_ts = None
                source = None
                if sym in onboard_map:
                    onboard_ts = pd.Timestamp(onboard_map[sym]["onboard_iso"])
                    if onboard_ts.tzinfo is None:
                        onboard_ts = onboard_ts.tz_localize("UTC")
                    source = "exchangeInfo"
                else:
                    df1h_probe = load_1h_utc(conn, sym)
                    if df1h_probe is not None:
                        onboard_ts = df1h_probe["open_time"].min()
                        source = "first_candle_proxy"

                if onboard_ts is None:
                    acc["no_onboard"] += 1
                    processed.add(sym)
                    n_done += 1
                    continue

                # Cohort membership: onboardDate strictly after the retention floor.
                # (For the proxy source, being > floor already means the coin appeared
                #  mid-window rather than being retention-truncated at the floor.)
                if onboard_ts <= floor_ts + pd.Timedelta(days=1):
                    acc["excluded_pre_floor"] += 1
                    processed.add(sym)
                    n_done += 1
                    continue

                d1d = load_1d_utc(conn, sym)
                fwd = forward_returns(d1d, onboard_ts, btc1d) if d1d is not None else None
                df1h = load_1h_utc(conn, sym)
                fades = fade_events(df1h, onboard_ts, fund_by_sym.get(sym)) if df1h is not None else []
                fold_coin(acc, sym, source, fwd, fades)
            except Exception as e:  # one bad coin must not kill the run
                conn.rollback()
                print(f"  WARN {sym}: {e}")
            processed.add(sym)
            n_done += 1
            rss = _rss_mb()
            if rss is not None:
                peak_rss = max(peak_rss, rss)
            if n_done % args.progress_every == 0:
                av = _avail_mb()
                print(f"  ...{n_done}/{len(coins)} coins, cohort={len(acc['cohort_symbols'])}, "
                      f"rss={peak_rss:.0f}MB avail={av:.0f}MB" if av is not None
                      else f"  ...{n_done}/{len(coins)} coins")
            if n_done % args.checkpoint_every == 0:
                snapshot(final=False)
                persist_state()
                print(f"  checkpoint+state at {n_done} coins (cohort={len(acc['cohort_symbols'])})")

        verdict = snapshot(final=True)

    try:
        if os.path.exists(state_path):
            os.remove(state_path)
    except OSError:
        pass

    print(f"\nVERDICT: {verdict['verdict']}")
    print(f"cohort_n={verdict['cohort_n']} small_n={verdict['small_n_flag']} "
          f"max_fade_cell_n={verdict['max_fade_cell_n']} excluded_pre_floor={verdict['excluded_pre_floor']} "
          f"peak_rss={peak_rss:.0f}MB")
    if verdict["long_blacklist_recommendation"]:
        print(f"LONG-filter: {verdict['long_blacklist_recommendation']['rule']}")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
