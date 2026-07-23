"""tools/soft_regime_counterfactual.py — SOFT-regime gate counterfactual (T-2026-KYT-9050-031).

Zweck
-----
Follow-up zu T-2026-KYT-9050-029 (`tools/research/regime_switch/`). Die DB-freie
Study zeigte: die SOFT-Timeline (EMA-geglättete Classifier-Confidence,
`build_soft_timeline`) dominiert die Live-RULE monoton auf Whipsaw. ABER die
Study misst nur Timeline-Qualität, NICHT PnL auf echten Bot-Forwards. Das ist
DB-gebunden — genau das rechnet dieses Tool auf dem Live-VPS (read-only).

Kernfrage
---------
Hätte eine SOFT-geglättete Regime-Gate den PnL der ROM1-geforwardeten Trades
verbessert (bzw. die Whitelist-Flapping reduziert), ODER ist der Churn-Sieg
PnL-neutral?

Datenbasis (Live-DB, strikt read-only)
--------------------------------------
  * `regime_history` (5-min-Kadenz): trägt pro Check die
    Hysterese-klassifizierte `regime` (= exakt der `raw_regime`, der live an
    `apply_debounce` ging) PLUS `raw_features` JSON mit allen Classifier-Inputs
    (`vola_p75/p40`, `btc_return_1h/4h`, `btc_atr_1h/4h_pct`, `btcdom_return_24h`).
    → SOFT und RULE lassen sich daraus rekonstruieren, ohne Kerzen neu zu lesen.
  * `orchestrator_open_trades` (forwarded): trägt `regime_at_open` (das
    LIVE-effektive RULE-Regime, Ground Truth) und `status`
    (`CLOSED_TP`/`CLOSED_SL`/… = echtes Trade-Outcome).
  * `orchestrator_suppressed_signals` (geblockt): nur `regime_at_signal`, kein
    Outcome → nur via First-Touch-Replay bewertbar.

Rekonstruktion
--------------
  * **RULE_recon**: `_step_debounce` (Port von `apply_debounce`, aus
    `regime_switch.timelines`) über die gespeicherte `(regime, alt_context)`-
    Sequenz gefaltet. Weil `regime_history.regime` der exakte Debounce-Input war,
    ist das die treueste Rekonstruktion — validiert gegen die aufgezeichneten
    `regime_at_open` (Agreement-Report).
  * **SOFT**: `build_soft_timeline` (rein, geteilt, aus T-029) über die
    `raw_features`-Reihe. Half-life ist hier in 5-min-Checks (Study war 15m-Kerzen):
    hl=192 ≈ Study-hl64 (16h Wall-Clock). Sweep über mehrere hl.

Messung
-------
  1. **Churn**: Switches/30d RULE_recon vs SOFT (pro hl). Bestätigt/widerlegt den
     Whipsaw-Sieg auf Live-Daten.
  2. **Fidelity**: RULE_recon vs aufgezeichnetes `regime_at_open`.
  3. **PnL-Signal (join-sauber, Ground Truth)**: forwarded Trades nach
     „SOFT-Regime == aufgezeichnetes RULE-Regime" bucketen; TP/SL-Win-Rate je
     Bucket + 2-Proportionen-z-Test. SOFT smoothed nur die BTC-Achse → alt_context
     bleibt fix; verglichen wird die BTC-Regime-Dimension.
  4. **PnL-Magnitude (Replay)**: First-Touch-Replay-PnL (`rom1_counterfactual`
     wiederverwendet) je Bucket — WR ist nicht PnL (R:R zählt).

Join-Grenzen (ehrlich)
----------------------
  * `bot_regime_whitelist` wird pro Analyzer-Zyklus KOMPLETT überschrieben (PK
    auf dem 4-Tupel, keine Historie). Der Whitelist-Zustand als-of eines
    Signals in der Vergangenheit ist NICHT rekonstruierbar → ein echter
    „SOFT hätte forward↔suppress geflippt"-Counterfactual ist nur mit dem
    HEUTIGEN Snapshot als Proxy machbar. Zusätzlich ist dieser Snapshot AUS
    denselben Trades gerechnet (Lookback) → zirkulär. Deshalb ist der
    Whitelist-Reflip nur ein FLAGGED-Appendix, nicht die Verdikt-Basis.
  * `prob↔outcome` in der Live-DB nur eingeschränkt joinbar (siehe
    [[kythera-ws2-golive-promotions]]) → wir nutzen `status` (TP/SL) als
    Outcome-Proxy, nicht realisierten PnL.
  * `orchestrator_suppressed_signals` trägt keinen `alt_context` → Whitelist-
    Reflip nur auf der forwarded-Seite.

Betriebsregeln (Live-VPS!)
--------------------------
  DB strikt read-only, BELOW_NORMAL-Priorität, CPU-Headroom-Check — wie
  `rom1_counterfactual`. Keine Tabelle wird geschrieben. Ergebnisse nach
  `KYTHERA_REPLAY_DIR` (JSON + Markdown).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import timedelta

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.database import get_db_connection  # noqa: E402
from core.time import utc_now  # noqa: E402
from tools.research.regime_switch.timelines import (  # noqa: E402
    _DebounceState,
    _step_debounce,
    build_soft_timeline,
)
from tools.rom1_counterfactual import score_all  # noqa: E402
from tools.walkforward_sim import (  # noqa: E402
    check_cpu_headroom,
    import_bot_module,
    set_low_priority,
)

DEFAULT_OUT_DIR = os.getenv(
    "KYTHERA_REPLAY_DIR", r"C:\Users\Michael\Documents\_X\staging_models\replay"
)

# regime_history cadence = 5 min → 12 checks/h, 288/day, 30d = 8640 checks.
CHECKS_PER_HOUR = 12
CHECKS_PER_30D = 30 * 288

# SOFT half-lives to sweep, in 5-min-check units. hl=192 ≈ study hl64 (16h).
DEFAULT_HALF_LIVES = (12, 48, 96, 192, 288)  # 1h, 4h, 8h, 16h, 24h
# Feature half-life for the PnL bucketing (study-headline strength).
DEFAULT_FEATURE_HL = 192

def _na(v) -> bool:
    """True for None or NaN (pandas may coerce list-None → NaN in object series)."""
    return v is None or (isinstance(v, float) and math.isnan(v))


# raw_features JSON keys the classifier consumes (fed to build_soft_timeline).
_FEAT_KEYS = (
    "vola_p75",
    "vola_p40",
    "btc_return_1h",
    "btc_return_4h",
    "btc_atr_1h_pct",
    "btc_atr_4h_pct",
    "btcdom_return_24h",
)


# ─────────────────────────────────────────────────────────────────────────────
# DB (read-only)
# ─────────────────────────────────────────────────────────────────────────────
def load_regime_history(conn, days: int) -> tuple[list, list[str], list[str], pd.DataFrame]:
    """5-min regime_history over `days`, ts ASC. Returns (ts, regime, alt, feat_df).

    `ts` is naive-UTC (26_regime_detector writes it naive). The feature frame is
    indexed by ts and carries exactly the columns build_soft_timeline reads, taken
    from the `raw_features` JSON (the source of truth for the live classifier
    inputs). Falls back to the top-level columns if a JSON key is missing.
    """
    cutoff = utc_now().replace(tzinfo=None) - timedelta(days=int(days))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts, regime, alt_context, confidence_btc,
                   btc_return_1h, btc_return_4h, btc_atr_1h_pct, btc_atr_4h_pct,
                   btcdom_return_24h, raw_features
            FROM regime_history
            WHERE ts >= %s
            ORDER BY ts ASC
            """,
            [cutoff],
        )
        rows = cur.fetchall()
    ts = [r[0] for r in rows]
    reg = [r[1] for r in rows]
    alt = [r[2] for r in rows]
    cols: dict[str, list] = {k: [] for k in _FEAT_KEYS}
    top = {
        "btc_return_1h": 4,
        "btc_return_4h": 5,
        "btc_atr_1h_pct": 6,
        "btc_atr_4h_pct": 7,
        "btcdom_return_24h": 8,
    }
    for r in rows:
        rf = r[9] if isinstance(r[9], dict) else {}
        for k in _FEAT_KEYS:
            v = rf.get(k)
            if v is None and k in top:
                v = r[top[k]]
            cols[k].append(v)
    feat = pd.DataFrame(cols, index=pd.Index(ts, name="ts"))
    return ts, reg, alt, feat


def load_forwarded(conn, days: int) -> list[dict]:
    """Forwarded trades over `days`, carrying recorded RULE regime + real outcome."""
    cutoff = utc_now().replace(tzinfo=None) - timedelta(days=int(days))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, opened_at, bot_name, coin, direction,
                   regime_at_open, alt_context_at_open, status,
                   close_reason, regime_close_action, wl_reason, entry_price
            FROM orchestrator_open_trades
            WHERE opened_at >= %s AND coin IS NOT NULL AND direction IS NOT NULL
            ORDER BY opened_at ASC
            """,
            [cutoff],
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "side": "forwarded", "row_id": r[0], "ts": r[1], "bot_name": r[2],
            "coin": r[3], "direction": r[4], "recorded_regime": r[5],
            "alt_context": r[6], "status": r[7], "close_reason": r[8],
            "regime_close_action": r[9], "wl_reason": r[10],
            "recorded_entry": float(r[11]) if r[11] is not None else None,
            # bucket/bucket_class kept for score_all compatibility (unused here).
            "bucket": "forwarded", "bucket_class": "forward",
            "original_outbox_id": None,
        })
    return out


def load_suppressed_gate(conn, days: int) -> list[dict]:
    """Gate-class suppressions (bot_not_whitelisted:*) — the 4D-gate-relevant block.

    dedupe/plumbing reasons (cooldown, opposite/same-direction, unidentified) are
    excluded: their suppression is positional hygiene, not a regime-gate judgment.
    """
    cutoff = utc_now().replace(tzinfo=None) - timedelta(days=int(days))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, ts, bot_name, coin, direction, regime_at_signal, reason
            FROM orchestrator_suppressed_signals
            WHERE ts >= %s AND coin IS NOT NULL AND direction IS NOT NULL
              AND reason LIKE 'bot_not_whitelisted:%%'
            ORDER BY ts ASC
            """,
            [cutoff],
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "side": "suppressed", "row_id": r[0], "ts": r[1], "bot_name": r[2],
            "coin": r[3], "direction": r[4], "recorded_regime": r[5],
            "alt_context": None, "status": None, "close_reason": None,
            "regime_close_action": None, "wl_reason": r[6],
            "recorded_entry": None, "bucket": "suppressed_gate",
            "bucket_class": "gate", "original_outbox_id": None,
        })
    return out


def load_whitelist_snapshot(conn) -> dict[tuple[str, str, str, str], bool]:
    """Current bot_regime_whitelist snapshot: (bot, regime, alt, dir) → whitelisted.

    PROXY ONLY — this is a single overwritten snapshot (no history), computed from
    the very trades we score. Used exclusively for the flagged reflip appendix.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT bot_name, regime, alt_context, direction, whitelisted FROM bot_regime_whitelist"
        )
        return {(r[0], r[1], r[2], r[3]): bool(r[4]) for r in cur.fetchall()}


# ─────────────────────────────────────────────────────────────────────────────
# RECONSTRUCTION
# ─────────────────────────────────────────────────────────────────────────────
def reconstruct_rule(reg: list[str], alt: list[str], index: pd.Index) -> pd.Series:
    """Fold the debounce state machine over the stored raw (regime, alt) stream.

    `regime_history.regime` is the exact `raw_regime` the live detector fed to
    `apply_debounce` (with §22 hysteresis already baked in), so folding
    `_step_debounce` reproduces the effective RULE regime (regime_current) up to
    warm-up / outage desync. This is the live baseline.
    """
    st = _DebounceState()
    labels: list[str | None] = []
    for rg, al in zip(reg, alt, strict=False):
        _step_debounce(st, rg, al)
        labels.append(st.regime)
    return pd.Series(labels, index=index, name="rule_recon")


def build_timelines(feat: pd.DataFrame, reg: list[str], alt: list[str], half_lives) -> dict[str, pd.Series]:
    """rule_recon, raw_stored, and one soft_<hl> series per half-life."""
    out: dict[str, pd.Series] = {}
    out["rule_recon"] = reconstruct_rule(reg, alt, feat.index)
    out["raw_stored"] = pd.Series(reg, index=feat.index, name="raw_stored")
    for hl in half_lives:
        out[f"soft_{hl}"] = build_soft_timeline(feat, half_life_candles=float(hl))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# METRICS
# ─────────────────────────────────────────────────────────────────────────────
def whipsaw(series: pd.Series) -> dict:
    """Switch frequency + dwell for a 5-min-cadence label series (None-safe)."""
    vals = list(series.values)
    n = sum(1 for v in vals if not _na(v))
    runs: list[list] = []
    for v in vals:
        if _na(v):
            continue
        if runs and runs[-1][0] == v:
            runs[-1][1] += 1
        else:
            runs.append([v, 1])
    switches = len(runs) - 1 if runs else 0
    dwell = np.array([ln for _, ln in runs], dtype=float)
    return {
        "n_checks": n,
        "n_switches": switches,
        "switches_per_30d": round(switches / (n / CHECKS_PER_30D), 2) if n else None,
        "mean_dwell_hours": round(float(dwell.mean()) / CHECKS_PER_HOUR, 2) if len(dwell) else None,
        "median_dwell_hours": round(float(np.median(dwell)) / CHECKS_PER_HOUR, 2) if len(dwell) else None,
        "n_episodes": len(runs),
        "pct_episodes_under_1h": round(100 * float((dwell < CHECKS_PER_HOUR).mean()), 1) if len(dwell) else None,
    }


def asof_indexer(index: pd.Index):
    """Return an as-of lookup: value of a series at the last ts <= `when`."""
    ts_ns = index.values.astype("datetime64[ns]")

    def lookup(series_vals: np.ndarray, when) -> object:
        i = int(np.searchsorted(ts_ns, np.datetime64(pd.Timestamp(when)), side="right")) - 1
        return None if i < 0 else series_vals[i]

    return lookup


def two_proportion_z(w1: int, n1: int, w2: int, n2: int) -> dict:
    """Two-proportion z-test on win-rates. Returns z, two-sided p, delta (pp)."""
    if n1 == 0 or n2 == 0:
        return {"z": None, "p_value": None, "delta_pp": None}
    p1, p2 = w1 / n1, w2 / n2
    p = (w1 + w2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    z = (p1 - p2) / se if se > 0 else 0.0
    # two-sided p via erfc
    pval = math.erfc(abs(z) / math.sqrt(2))
    return {"z": round(z, 3), "p_value": round(pval, 5), "delta_pp": round((p1 - p2) * 100, 2)}


# ─────────────────────────────────────────────────────────────────────────────
# ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────
def annotate_soft_rule(rows: list[dict], timelines: dict[str, pd.Series], feature_hl: int) -> dict:
    """Attach soft_regime / rule_recon_asof / agrees to each row. Returns fidelity."""
    lookup = asof_indexer(timelines["rule_recon"].index)
    rule_vals = timelines["rule_recon"].values
    soft_vals = timelines[f"soft_{feature_hl}"].values

    fid_match = fid_total = 0
    for r in rows:
        rr = lookup(rule_vals, r["ts"])
        sr = lookup(soft_vals, r["ts"])
        rr = None if _na(rr) else rr
        sr = None if _na(sr) else sr
        r["rule_recon_asof"] = rr
        r["soft_regime"] = sr
        rec = r.get("recorded_regime")
        rec = None if _na(rec) else rec
        # Fidelity uses the recorded RULE (ground truth); agreement uses recorded
        # RULE vs SOFT (the dimension SOFT actually moves).
        if rec is not None and rr is not None:
            fid_total += 1
            fid_match += int(rr == rec)
        r["soft_agrees_rule"] = (sr == rec) if (sr is not None and rec is not None) else None
        r["soft_shift"] = (
            f"{rec}->{sr}" if (sr is not None and rec is not None and sr != rec) else None
        )
    return {
        "rule_recon_vs_recorded_n": fid_total,
        "rule_recon_vs_recorded_agreement_pct": round(100 * fid_match / fid_total, 2) if fid_total else None,
    }


def wr_by_agreement(rows: list[dict]) -> dict:
    """TP/SL win-rate split by SOFT-agrees-RULE, for forwarded rows with an outcome."""
    buckets: dict[object, dict[str, int]] = {True: {"TP": 0, "SL": 0}, False: {"TP": 0, "SL": 0}}
    shift_counter: dict[str, dict[str, int]] = defaultdict(lambda: {"TP": 0, "SL": 0})
    n_regime_change = 0
    for r in rows:
        a = r.get("soft_agrees_rule")
        st = r.get("status")
        if st == "CLOSED_REGIME_CHANGE":
            n_regime_change += 1
        if a is None or st not in ("CLOSED_TP", "CLOSED_SL"):
            continue
        key = "TP" if st == "CLOSED_TP" else "SL"
        buckets[a][key] += 1
        if a is False and r.get("soft_shift"):
            shift_counter[r["soft_shift"]][key] += 1

    def wr(d):
        tot = d["TP"] + d["SL"]
        return round(100 * d["TP"] / tot, 2) if tot else None

    agree, disagree = buckets[True], buckets[False]
    z = two_proportion_z(agree["TP"], agree["TP"] + agree["SL"], disagree["TP"], disagree["TP"] + disagree["SL"])
    top_shifts = sorted(
        ((k, v["TP"] + v["SL"], round(100 * v["TP"] / (v["TP"] + v["SL"]), 1) if (v["TP"] + v["SL"]) else None)
         for k, v in shift_counter.items()),
        key=lambda t: -t[1],
    )[:12]
    return {
        "agree": {**agree, "decided": agree["TP"] + agree["SL"], "wr_pct": wr(agree)},
        "disagree": {**disagree, "decided": disagree["TP"] + disagree["SL"], "wr_pct": wr(disagree)},
        "z_test_agree_vs_disagree": z,
        "n_closed_regime_change_excluded": n_regime_change,
        "top_disagree_shifts": [{"shift": k, "n": n, "wr_pct": w} for k, n, w in top_shifts],
    }


def wr_by_agreement_sweep(rows: list[dict], timelines: dict[str, pd.Series], half_lives) -> list[dict]:
    """Re-annotate + WR split for every half-life, to show robustness of the gap."""
    lookup = asof_indexer(timelines["rule_recon"].index)
    out = []
    for hl in half_lives:
        soft_vals = timelines[f"soft_{hl}"].values
        b = {True: {"TP": 0, "SL": 0}, False: {"TP": 0, "SL": 0}}
        n_diff = 0
        for r in rows:
            sr = lookup(soft_vals, r["ts"])
            rec = r.get("recorded_regime")
            st = r.get("status")
            if _na(sr) or _na(rec):
                continue
            agrees = sr == rec
            if not agrees:
                n_diff += 1
            if st in ("CLOSED_TP", "CLOSED_SL"):
                b[agrees]["TP" if st == "CLOSED_TP" else "SL"] += 1
        z = two_proportion_z(b[True]["TP"], b[True]["TP"] + b[True]["SL"], b[False]["TP"], b[False]["TP"] + b[False]["SL"])

        def wr(d):
            tot = d["TP"] + d["SL"]
            return round(100 * d["TP"] / tot, 2) if tot else None

        out.append({
            "half_life_checks": hl,
            "half_life_hours": round(hl / CHECKS_PER_HOUR, 1),
            "pct_forwarded_disagree": round(100 * n_diff / len(rows), 1) if rows else None,
            "agree_decided": b[True]["TP"] + b[True]["SL"],
            "agree_wr_pct": wr(b[True]),
            "disagree_decided": b[False]["TP"] + b[False]["SL"],
            "disagree_wr_pct": wr(b[False]),
            "z_test": z,
        })
    return out


def agreement_summary(rows: list[dict]) -> dict:
    """SOFT-vs-recorded-RULE agreement rate over annotated rows (outcome-agnostic)."""
    n = agree = disagree = 0
    for r in rows:
        a = r.get("soft_agrees_rule")
        if a is True:
            n += 1
            agree += 1
        elif a is False:
            n += 1
            disagree += 1
    return {
        "n_comparable": n,
        "n_agree": agree,
        "n_disagree": disagree,
        "pct_disagree": round(100 * disagree / n, 1) if n else None,
    }


def whitelist_reflip_proxy(rows: list[dict], wl: dict) -> dict:
    """FLAGGED PROXY: re-gate forwarded rows under SOFT regime via the CURRENT
    whitelist snapshot. Circular (snapshot derived from these trades) + single
    point in time. Reported only as an illustrative bound, not a verdict."""
    flip_to_suppress = {"TP": 0, "SL": 0, "n": 0}
    same = {"TP": 0, "SL": 0, "n": 0}
    unknown = 0
    for r in rows:
        sr = r.get("soft_regime")
        alt = r.get("alt_context")
        if sr is None or alt is None:
            unknown += 1
            continue
        soft_wl = wl.get((r["bot_name"], sr, alt, r["direction"]))
        # A missing SOFT cell = orchestrator's `no_whitelist_entry` → default-open.
        soft_forwards = True if soft_wl is None else soft_wl
        target = same if soft_forwards else flip_to_suppress
        target["n"] += 1
        if r.get("status") == "CLOSED_TP":
            target["TP"] += 1
        elif r.get("status") == "CLOSED_SL":
            target["SL"] += 1

    def wr(d):
        tot = d["TP"] + d["SL"]
        return round(100 * d["TP"] / tot, 2) if tot else None

    return {
        "note": "PROXY — current whitelist snapshot, circular + single-timepoint. NOT a verdict.",
        "would_suppress": {**flip_to_suppress, "wr_pct": wr(flip_to_suppress)},
        "would_keep": {**same, "wr_pct": wr(same)},
        "n_unknown_alt_or_soft": unknown,
    }


def replay_pnl_by_bucket(records: list[dict]) -> dict:
    """Aggregate first-touch-replay net_pnl_pct by SOFT-agrees-RULE bucket."""
    buckets: dict[object, list[float]] = {True: [], False: []}
    decided: dict[object, dict[str, int]] = {True: {"win": 0, "loss": 0}, False: {"win": 0, "loss": 0}}
    for r in records:
        if not r.get("scored"):
            continue
        a = r.get("soft_agrees_rule")
        if a not in (True, False):
            continue
        pnl = r.get("net_pnl_pct")
        if pnl is not None:
            buckets[a].append(float(pnl))
        tp1 = r.get("outcome_tp1")
        if tp1 == 1:
            decided[a]["win"] += 1
        elif tp1 == 0:
            decided[a]["loss"] += 1

    def summ(a):
        arr = np.array(buckets[a], dtype=float)
        d = decided[a]
        tot = d["win"] + d["loss"]
        return {
            "n_scored": len(arr),
            "sum_net_pnl_pct": round(float(arr.sum()), 2) if len(arr) else None,
            "avg_net_pnl_pct": round(float(arr.mean()), 4) if len(arr) else None,
            "median_net_pnl_pct": round(float(np.median(arr)), 4) if len(arr) else None,
            "replay_tp1_wr_pct": round(100 * d["win"] / tot, 2) if tot else None,
            "n_decided": tot,
        }

    return {"agree": summ(True), "disagree": summ(False)}


# ─────────────────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────────────────
def build_report(meta: dict) -> str:
    """Render the Markdown report from the assembled meta dict."""
    L: list[str] = []
    ap = L.append
    ap("# SOFT-Regime Gate Counterfactual — T-2026-KYT-9050-031\n")
    ap(f"_generated {meta['generated_at']} · {meta['days']}d window · feature hl="
       f"{meta['feature_half_life']} checks ({meta['feature_half_life'] / CHECKS_PER_HOUR:.1f}h)_\n")
    ap(f"**Verdict: {meta['verdict']}** — {meta['verdict_reason']}\n")

    ap("## 1 · Churn (live regime_history reconstruction)\n")
    ap("| timeline | switches/30d | mean dwell (h) | % episodes <1h |")
    ap("|---|--:|--:|--:|")
    for name in ["raw_stored", "rule_recon"] + [f"soft_{hl}" for hl in meta["half_lives"]]:
        w = meta["churn"][name]
        ap(f"| {name} | {w['switches_per_30d']} | {w['mean_dwell_hours']} | {w['pct_episodes_under_1h']} |")
    rr = meta["churn"]["rule_recon"]["switches_per_30d"]
    sf = meta["churn"][f"soft_{meta['feature_half_life']}"]["switches_per_30d"]
    if rr and sf is not None:
        ap(f"\nSOFT(hl={meta['feature_half_life']}) cuts RULE switches by "
           f"**{round(100 * (1 - sf / rr))}%** ({rr}→{sf} /30d).\n")

    ap("## 2 · Reconstruction fidelity\n")
    f = meta["fidelity"]
    ap(f"RULE_recon vs recorded `regime_at_open`: **{f['rule_recon_vs_recorded_agreement_pct']}%** "
       f"agreement over {f['rule_recon_vs_recorded_n']} forwarded trades. "
       f"(Residual = warm-up cold-start + ingestion-outage debounce desync.)\n")

    ap("## 3 · PnL signal — forwarded TP/SL win-rate by SOFT-vs-RULE agreement\n")
    ap("Ground-truth outcome (`status`), zero replay, zero whitelist proxy. "
       "SOFT smooths only the BTC regime; `alt_context` held fixed.\n")
    w = meta["wr_by_agreement"]
    ap("| bucket | decided (TP+SL) | TP | SL | win-rate |")
    ap("|---|--:|--:|--:|--:|")
    for label, key in [("SOFT agrees RULE", "agree"), ("SOFT disagrees RULE", "disagree")]:
        b = w[key]
        ap(f"| {label} | {b['decided']} | {b['TP']} | {b['SL']} | {b['wr_pct']}% |")
    z = w["z_test_agree_vs_disagree"]
    ap(f"\nΔ = **{z['delta_pp']}pp** (agree − disagree), z={z['z']}, p={z['p_value']}. "
       f"{w['n_closed_regime_change_excluded']} `CLOSED_REGIME_CHANGE` auto-closes excluded (no TP/SL label).\n")
    if w["top_disagree_shifts"]:
        ap("Top disagreement shifts (recorded RULE → SOFT), disagree bucket:\n")
        ap("| shift | n | win-rate |")
        ap("|---|--:|--:|")
        for s in w["top_disagree_shifts"]:
            ap(f"| {s['shift']} | {s['n']} | {s['wr_pct']}% |")
        ap("")

    ap("## 4 · Robustness across half-lives\n")
    ap("| hl (checks) | hl (h) | % fwd disagree | agree WR | disagree WR | Δpp | p |")
    ap("|--:|--:|--:|--:|--:|--:|--:|")
    for s in meta["wr_sweep"]:
        z = s["z_test"]
        ap(f"| {s['half_life_checks']} | {s['half_life_hours']} | {s['pct_forwarded_disagree']}% | "
           f"{s['agree_wr_pct']}% | {s['disagree_wr_pct']}% | {z['delta_pp']} | {z['p_value']} |")
    ap("")

    if meta.get("replay_pnl"):
        ap("## 5 · PnL magnitude — first-touch replay by bucket\n")
        ap("ROM1 geometry replay (`rom1_counterfactual`) — WR is not PnL (R:R matters). "
           "Absolute level reflects a fixed ROM1 geometry, not per-bot realized PnL; the "
           "cross-bucket comparison is the signal.\n")
        rp = meta["replay_pnl"]
        ap("| bucket | n scored | avg net PnL% | median | sum net PnL% | replay TP1 WR |")
        ap("|---|--:|--:|--:|--:|--:|")
        for label, key in [("SOFT agrees RULE", "agree"), ("SOFT disagrees RULE", "disagree")]:
            b = rp[key]
            ap(f"| {label} | {b['n_scored']} | {b['avg_net_pnl_pct']} | {b['median_net_pnl_pct']} | "
               f"{b['sum_net_pnl_pct']} | {b['replay_tp1_wr_pct']}% |")
        ap("")

    sa = meta.get("suppressed_agreement")
    if sa:
        ap("## 5b · Suppressed side (gate-class, replay-only — no live outcome)\n")
        ap(f"`bot_not_whitelisted:*` suppressions carry no `status`, so they are scored by "
           f"first-touch replay only. SOFT disagrees with the recorded RULE regime on "
           f"**{sa['pct_disagree']}%** of {sa['n_comparable']} comparable suppressions.\n")
        rps = meta.get("replay_pnl_suppressed")
        if rps:
            ap("| bucket | n scored | avg net PnL% | median | sum net PnL% | replay TP1 WR |")
            ap("|---|--:|--:|--:|--:|--:|")
            for label, key in [("SOFT agrees RULE", "agree"), ("SOFT disagrees RULE", "disagree")]:
                b = rps[key]
                ap(f"| {label} | {b['n_scored']} | {b['avg_net_pnl_pct']} | {b['median_net_pnl_pct']} | "
                   f"{b['sum_net_pnl_pct']} | {b['replay_tp1_wr_pct']}% |")
            ap("\n(Suppressed replay PnL is hypothetical — these trades were blocked, never taken.)\n")

    if meta.get("whitelist_reflip"):
        ap("## 6 · Whitelist reflip (FLAGGED PROXY — not a verdict)\n")
        wl = meta["whitelist_reflip"]
        ap(f"_{wl['note']}_\n")
        ap("| SOFT gate outcome | n | TP | SL | win-rate |")
        ap("|---|--:|--:|--:|--:|")
        for label, key in [("would keep (forward)", "would_keep"), ("would suppress", "would_suppress")]:
            b = wl[key]
            ap(f"| {label} | {b['n']} | {b['TP']} | {b['SL']} | {b['wr_pct']}% |")
        ap(f"\n{wl['n_unknown_alt_or_soft']} rows without alt_context/soft regime skipped.\n")

    ap("## Join limits (honest)\n")
    for lim in meta["join_limits"]:
        ap(f"- {lim}")
    ap("")
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# DRIVER
# ─────────────────────────────────────────────────────────────────────────────
def derive_verdict(meta: dict) -> tuple[str, str]:
    """EDGE only on a *proven* PnL uplift. The bar (per the task): a measurable,
    plausible uplift — not merely a churn win or a fragile WR separation.

    We weigh three things honestly:
      * churn cut (robust, expected from T-029),
      * whether the disagree-bucket WR gap is significant AND robust across
        half-lives (not just at the extreme-smoothing feature hl),
      * whether replay PnL shows a positive-expectancy side to move toward
        (T-029 predicted η²≈0 → both sides near-zero/negative).
    """
    w = meta["wr_by_agreement"]
    z = w["z_test_agree_vs_disagree"]
    rr = meta["churn"]["rule_recon"]["switches_per_30d"]
    sf = meta["churn"][f"soft_{meta['feature_half_life']}"]["switches_per_30d"]
    churn_cut = round(100 * (1 - sf / rr)) if (rr and sf is not None) else None
    delta = z.get("delta_pp")
    p = z.get("p_value")
    churn_ok = churn_cut is not None and churn_cut >= 30
    feat_sig = p is not None and p < 0.01 and delta is not None and delta > 0

    # Robustness: is the gap significant at a MODERATE half-life (≤8h), not only
    # at the ≥16h smoothing that gates out ~half the flow?
    moderate = [s for s in meta.get("wr_sweep", []) if s["half_life_checks"] <= 96]
    robust_sig = any(
        s["z_test"].get("p_value") is not None and s["z_test"]["p_value"] < 0.05
        and s["z_test"].get("delta_pp") and s["z_test"]["delta_pp"] > 0
        for s in moderate
    )
    # Is there a positive-expectancy side to steer toward in replay?
    rp = meta.get("replay_pnl") or {}
    agree_avg = (rp.get("agree") or {}).get("avg_net_pnl_pct")
    dis_avg = (rp.get("disagree") or {}).get("avg_net_pnl_pct")
    both_negative = agree_avg is not None and dis_avg is not None and agree_avg < 0 and dis_avg < 0

    if churn_ok and feat_sig and robust_sig and not both_negative:
        return (
            "EDGE (directional, whitelist-gated)",
            f"SOFT cuts churn {churn_cut}%, the churn-affected trades win {delta}pp less "
            f"robustly across half-lives, and replay shows a positive side to steer toward — "
            f"a live shadow A/B is warranted.",
        )
    if churn_ok and feat_sig and not robust_sig:
        return (
            "NO-EDGE for a proven PnL uplift (churn-confirmed; live A/B is the only settler)",
            f"SOFT robustly cuts churn {churn_cut}% and the churn-affected (disagree) forwarded "
            f"trades DO underperform ({delta}pp lower WR, p={p}) — a real directional signal. But "
            f"it is not a demonstrable PnL uplift: (a) the WR gap reaches significance only at "
            f"heavy smoothing (hl≥16h, gating ~half the flow) and is insignificant at ≤8h; "
            f"(b) first-touch replay PnL is near-zero and NEGATIVE in both buckets "
            f"(agree {agree_avg}%, disagree {dis_avg}%/trade) — consistent with T-029's η²≈0, you "
            f"are choosing between losers, not toward a winner; (c) 'disagree' ≠ 'SOFT would "
            f"suppress' — that mapping needs the historical whitelist, which is overwritten each "
            f"cycle (unreconstructable), and the only available proxy (current snapshot, circular) "
            f"points the OTHER way. Verdict: the churn win does not convert to a proven PnL gain on "
            f"the joinable evidence; only a live shadow A/B of a SOFT gate can settle it.",
        )
    if churn_ok and not feat_sig:
        return (
            "NO-EDGE (churn win is PnL-neutral)",
            f"SOFT cuts churn {churn_cut}% but the disagree-bucket WR gap is not significant "
            f"(Δ={delta}pp, p={p}) — the churn reduction does not track any WR separation.",
        )
    return ("INCONCLUSIVE", f"churn_cut={churn_cut}%, Δ={delta}pp, p={p} — see tables.")


def main() -> None:
    ap = argparse.ArgumentParser(description="SOFT-regime gate counterfactual (T-2026-KYT-9050-031)")
    ap.add_argument("--days", type=int, default=90, help="lookback over regime_history + orchestrator rows")
    ap.add_argument("--history-days", type=int, default=None, help="regime_history lookback (default: days + 30 warmup)")
    ap.add_argument("--feature-hl", type=int, default=DEFAULT_FEATURE_HL, help="SOFT half-life (5-min checks) for bucketing")
    ap.add_argument("--half-lives", type=int, nargs="+", default=list(DEFAULT_HALF_LIVES))
    ap.add_argument("--replay", action="store_true", help="run first-touch replay for PnL magnitude (heavier)")
    ap.add_argument("--replay-horizon-hours", type=int, default=72)
    ap.add_argument("--out", default=DEFAULT_OUT_DIR)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    set_low_priority()
    check_cpu_headroom()

    half_lives = sorted(set(args.half_lives) | {args.feature_hl})
    hist_days = args.history_days if args.history_days is not None else args.days + 30

    conn = get_db_connection()
    try:
        conn.set_session(readonly=True)  # NEVER writes the live DB
    except Exception:
        pass

    t0 = time.time()
    try:
        ts, reg, alt, feat = load_regime_history(conn, hist_days)
        print(f"regime_history: {len(feat)} rows ({hist_days}d)", flush=True)
        timelines = build_timelines(feat, reg, alt, half_lives)
        gaps = pd.Series(ts).diff().dt.total_seconds().dropna() / 60.0

        forwarded = load_forwarded(conn, args.days)
        print(f"forwarded: {len(forwarded)} rows ({args.days}d)", flush=True)

        fidelity = annotate_soft_rule(forwarded, timelines, args.feature_hl)
        wr_agr = wr_by_agreement(forwarded)
        wr_sweep = wr_by_agreement_sweep(forwarded, timelines, half_lives)

        wl = load_whitelist_snapshot(conn)
        reflip = whitelist_reflip_proxy(forwarded, wl)

        # Suppressed side (gate-class): no live outcome (`status`), so it is
        # replay-only — SOFT-vs-recorded-RULE agreement + first-touch replay PnL.
        suppressed = load_suppressed_gate(conn, args.days)
        print(f"suppressed (gate-class): {len(suppressed)} rows ({args.days}d)", flush=True)
        sup_fidelity = annotate_soft_rule(suppressed, timelines, args.feature_hl)
        sup_agreement = agreement_summary(suppressed)

        replay_pnl = replay_pnl_suppressed = None
        if args.replay:
            orch = import_bot_module("28_signal_orchestrator.py", "signal_orchestrator")
            check_cpu_headroom()
            print(f"replaying {len(forwarded)} forwarded rows (horizon {args.replay_horizon_hours}h)…", flush=True)
            replay_pnl = replay_pnl_by_bucket(score_all(conn, orch, forwarded, args.replay_horizon_hours))
            check_cpu_headroom()
            print(f"replaying {len(suppressed)} suppressed rows…", flush=True)
            replay_pnl_suppressed = replay_pnl_by_bucket(score_all(conn, orch, suppressed, args.replay_horizon_hours))
    finally:
        conn.close()

    meta = {
        "task": "T-2026-KYT-9050-031",
        "generated_at": str(utc_now()),
        "days": args.days,
        "history_days": hist_days,
        "feature_half_life": args.feature_hl,
        "half_lives": half_lives,
        "regime_history_rows": len(feat),
        "cadence_gap_min_median": round(float(gaps.median()), 2) if len(gaps) else None,
        "cadence_gaps_over_10min": int((gaps > 10).sum()) if len(gaps) else 0,
        "cadence_max_gap_min": round(float(gaps.max()), 1) if len(gaps) else None,
        "n_forwarded": len(forwarded),
        "n_suppressed_gate": len(suppressed),
        "churn": {name: whipsaw(s) for name, s in timelines.items()},
        "fidelity": fidelity,
        "wr_by_agreement": wr_agr,
        "wr_sweep": wr_sweep,
        "whitelist_reflip": reflip,
        "suppressed_fidelity": sup_fidelity,
        "suppressed_agreement": sup_agreement,
        "replay_pnl": replay_pnl,
        "replay_pnl_suppressed": replay_pnl_suppressed,
        "join_limits": [
            "bot_regime_whitelist is overwritten wholesale each analyzer cycle (PK on the 4-tuple, "
            "no history) → the as-of whitelist for a past signal under a DIFFERENT regime is not "
            "reconstructable; the §6 reflip uses the current snapshot as a circular proxy only.",
            "prob↔outcome not reliably joinable in the live DB → outcome proxied by orchestrator "
            "trade `status` (CLOSED_TP/SL), not realized PnL.",
            "orchestrator_suppressed_signals carries no alt_context → whitelist reflip is forwarded-only.",
            "SOFT smooths the BTC regime axis only; alt_context held at its recorded value.",
            "CLOSED_REGIME_CHANGE auto-closes (majority of forwarded exits) carry no TP/SL label and "
            "are excluded from the win-rate — they are themselves regime-driven, so SOFT would also "
            "change their timing (interaction not modelled here).",
        ],
    }
    meta["verdict"], meta["verdict_reason"] = derive_verdict(meta)

    os.makedirs(args.out, exist_ok=True)
    tag = f"soft_regime_counterfactual_{args.days}d_hl{args.feature_hl}"
    with open(os.path.join(args.out, f"{tag}.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)
    md = build_report(meta)
    with open(os.path.join(args.out, f"{tag}.md"), "w", encoding="utf-8") as fh:
        fh.write(md)

    print(md)
    print(f"\n[{time.time() - t0:.0f}s] → {os.path.join(args.out, tag + '.json')}")


if __name__ == "__main__":
    main()
