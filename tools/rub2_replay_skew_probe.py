# tools/rub2_replay_skew_probe.py — read-only Replay-vs-Live parity probe for RUB2-SHORT
"""Does the walk-forward replay score the same signals the live bot scored?

T-2026-KYT-9050-008. Motivated by the T-2026-CU-9050-070 finding that, for the
same (symbol, candle), live confidence (``ml_predictions_master``, RUB2-SHORT)
and replay probability correlated **-0.37** over 49 pairs on 06./07.07.2026 —
same model, same candle, therefore a feature skew between serving and replay.
The hypothesis on the ticket was the funding features.

The probe answers that question by rebuilding, per matched signal, exactly what
each side saw, and it does so in five layers so the answer names a *cause*
rather than a symptom:

  1. ``match``    — join live predictions to replay events on (symbol, candle).
                    The live ``time`` column is naive PG-local, the replay stamps
                    UTC; the join converts rather than assuming an offset.
  2. ``artifact`` — score BOTH candidate artifacts on the replay features and
                    report which one reproduces the live confidence per day. A
                    model generation swapped mid-window is indistinguishable from
                    a feature skew if you only look at the pooled correlation.
  3. ``features`` — rebuild all 15 features (9 rub + 6 funding) from today's DB
                    and diff them against the values frozen in the replay file,
                    per feature. Two regression windows are built: the replay's
                    (last N rows) and the live bot's (every closed row inside a
                    95-day window), because those are not the same window on a
                    coin with candle gaps.
  4. ``funding``  — bound the funding block's influence: re-score with the
                    funding features at their missing-history fallback (0, what
                    ``funding_features_asof`` degrades to live). A residual larger
                    than that swing cannot be a funding problem, whatever the
                    funding history looked like at serving time.
  5. ``curve``    — the threshold curve on the replay's test slice (the retrain's
                    chronological split reproduced) next to the live curve. This
                    is the MAX1_MIN_PROB question T-070 had to leave open.

STRICTLY READ-ONLY on the live tables. The only writes go to ``staging_models/``
(hard rule 2). No training, no promotion, no posting.

Run it with the FLEET interpreter (Python 3.13 / pandas 2.x / sklearn 1.7.1 —
the versions the artifacts were pickled with):

    "C:\\Program Files\\Python313_12\\python.exe" tools/rub2_replay_skew_probe.py

From a worktree without its own ``.env``, point the loader at the operator file:

    KYTHERA_ENV_FILE=C:\\Users\\Michael\\Documents\\Kythera\\.env

Interpreting the output: agreement is expected to be EXACT (to float32, the
storage type of ``confidence``) whenever both sides read the same DB content.
A systematic residual is the interesting signal — it dates a change in the
underlying data, not a bug in the model.
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

# A worktree checkout has no .env of its own and load_dotenv's upward search does
# not escape it — let the operator hand one in instead of hard-coding a path.
# Loading it BEFORE core.config means the file only fills what the environment
# does not already define (python-dotenv does not override).
_ENV_FILE = os.getenv("KYTHERA_ENV_FILE")
if _ENV_FILE:
    from dotenv import load_dotenv

    load_dotenv(_ENV_FILE)

from core.candles import read_candles_with_indicators  # noqa: E402
from core.database import get_db_connection  # noqa: E402
from core.funding_features import FUNDING_FEATURES, funding_features_asof, load_funding  # noqa: E402
from core.model_artifacts import load_artifact  # noqa: E402
from core.rub_features import RUB_FEATURES, build_rub_features, rub_trend  # noqa: E402
from core.time import epoch_seconds  # noqa: E402
from tools.walkforward_sim import RUB_REG_WINDOW_H, set_low_priority  # noqa: E402

EXPECTED_FEATURES = RUB_FEATURES + FUNDING_FEATURES

#: The indicator columns Bot 13 reads, in the names the DB uses.
IND_COLUMNS = [
    "rsi_14",
    "tsi_fast_12_7_7",
    "tsi_fast_12_7_7_signal",
    "macd_dif_normal_12_26_9",
    "macd_dea_normal_12_26_9",
    "atr_14",
    "ema_200",
]

#: 13_ai_rub_bot loaded candles with `since=now - 95d` while RUB2-SHORT was live.
LIVE_LOAD_DAYS = 95
#: The bot scans at hh:10 and anchors its as-of at the candle close (hh:00).
LIVE_SCAN_OFFSET_MIN = 10

DEFAULT_REPLAY = os.getenv(
    "KYTHERA_RUB_REPLAY", r"C:\Users\Michael\Documents\_X\staging_models\replay\rub_replay_365d.jsonl"
)
DEFAULT_OUT = os.path.join(REPO_ROOT, "staging_models", "rub2_replay_skew_probe")


# ── loading ──────────────────────────────────────────────────────────────────


def load_replay_short(path: str, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> pd.DataFrame:
    """SHORT events of the replay, features flattened as r_*.

    Loaded unwindowed for the threshold curve — the retrain's chronological split
    runs over the whole 365-day file, so slicing first would produce a different
    (and much later) "test" slice than the one the artifact was validated on.
    """
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            if d.get("direction") != "SHORT":
                continue
            ts = pd.Timestamp(d["signal_time"], tz="UTC")
            if (start is not None and ts < start) or (end is not None and ts >= end):
                continue
            rows.append(
                {
                    "coin": d["symbol"],
                    "candle": ts,
                    "r_entry": float(d["entry"]),
                    "outcome_tp1": d.get("outcome_tp1"),
                    "net_pnl_pct": d.get("net_pnl_pct"),
                    **{f"r_{k}": v for k, v in d["features"].items()},
                }
            )
    return pd.DataFrame(rows)


def load_live_predictions(conn, tag: str, direction: str) -> pd.DataFrame:
    """Live predictions with their timestamp converted out of the naive PG-local column.

    ``ml_predictions_master.time`` is TIMESTAMP WITHOUT TIME ZONE, written by
    handing an aware UTC datetime to psycopg2 — Postgres cast it to the session
    zone on the way in. ``AT TIME ZONE current_setting('TimeZone')`` is the exact
    inverse of that cast, which is why the offset is read from the server instead
    of being pinned to the -3h the T-070 report observed.
    """
    return pd.read_sql_query(
        """
        SELECT coin,
               (time AT TIME ZONE current_setting('TimeZone')) AS ts_utc,
               entry AS live_entry,
               confidence,
               posted
        FROM ml_predictions_master
        WHERE model_name = %(tag)s AND direction = %(dir)s
        ORDER BY time
        """,
        conn,
        params={"tag": tag, "dir": direction},
    )


def match_pairs(live: pd.DataFrame, replay: pd.DataFrame) -> pd.DataFrame:
    """One row per (symbol, candle) seen by BOTH sides.

    The replay stamps ``signal_time`` = candle open + 1h, i.e. the close of the
    signal candle; the bot scans at hh:10 and floors to that same hour. So the
    join key is the floored live scan hour — no offset guessing.
    """
    live = live.copy()
    live["ts_utc"] = pd.to_datetime(live["ts_utc"], utc=True)
    live["candle"] = live["ts_utc"].dt.floor("h")
    m = live.merge(replay, on=["coin", "candle"], how="inner")
    return m.drop_duplicates(subset=["coin", "candle"]).reset_index(drop=True)


# ── scoring helpers ──────────────────────────────────────────────────────────


def score(artifact: dict, frame: pd.DataFrame, prefix: dict[str, str] | None = None) -> np.ndarray:
    """predict_proba on the artifact's own feature order, live's fillna(0) semantics.

    ``prefix`` overrides where a single feature is taken from ("r_" replay,
    "t_" rebuilt on the replay window, "L_" rebuilt on the live window), which is
    what makes the per-group substitution in section 4 possible.
    """
    prefix = prefix or {}
    cols = {f: frame[prefix.get(f, "r_") + f] for f in EXPECTED_FEATURES}
    x = pd.DataFrame(cols).reindex(columns=artifact["features"]).astype(float).fillna(0.0)
    return artifact["model"].predict_proba(x)[:, 1]


def agreement(live_conf: pd.Series, prob: np.ndarray) -> dict:
    """How close two probability vectors are — correlation AND level."""
    diff = np.abs(live_conf.to_numpy() - prob)
    return {
        "n": int(len(prob)),
        "pearson": round(float(np.corrcoef(live_conf, prob)[0, 1]), 4) if len(prob) > 2 else None,
        "spearman": round(float(pd.Series(live_conf.to_numpy()).corr(pd.Series(prob), method="spearman")), 4)
        if len(prob) > 2
        else None,
        "mean_abs_diff": round(float(diff.mean()), 5),
        "p90_abs_diff": round(float(np.quantile(diff, 0.90)), 5),
        # confidence is REAL (float32) in the DB, so "identical" bottoms out at ~1e-7.
        "pct_identical": round(float((diff < 1e-6).mean()) * 100, 1),
    }


# ── section 3: rebuild the features from today's DB ──────────────────────────


def rebuild_features(conn, matched: pd.DataFrame, max_symbols: int | None = None) -> pd.DataFrame:
    """Per matched signal, rebuild all 15 features from the DB as it stands today.

    Two variants, because the two sides do NOT window the regression the same way:

      * ``t_*`` — the replay's window: the last ``RUB_REG_WINDOW_H`` ROWS.
      * ``L_*`` — the live bot's window: every closed row inside a 95-DAY span
        anchored at the scan time. On a coin with candle gaps that span holds
        fewer rows, so the regression differs. Quantifying that difference is the
        point; it is not assumed to be zero.

    Both use the shared builders (``core/rub_features``, ``core/funding_features``)
    — the X-R1 rule: probing serving-vs-replay with a third implementation would
    only measure the probe.
    """
    symbols = sorted(matched["coin"].unique())
    if max_symbols:
        symbols = symbols[:max_symbols]
    out: list[dict] = []
    for i, sym in enumerate(symbols, 1):
        grp = matched[matched["coin"] == sym]
        start = (grp["candle"].min() - pd.Timedelta(days=LIVE_LOAD_DAYS + 5)).to_pydatetime()
        try:
            df = read_candles_with_indicators(
                conn,
                sym,
                "1h",
                start=start,
                include_forming=False,
                candle_columns=("open_time", "close"),
                indicator_columns=IND_COLUMNS,
            )
        except Exception as exc:  # a delisted coin's table can be gone — skip, do not abort
            conn.rollback()
            print(f"  ! {sym}: candle read failed ({exc}) — skipped", flush=True)
            continue
        if df is None or df.empty:
            continue
        df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
        for col in df.columns:
            if col != "open_time":
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"]).reset_index(drop=True)
        open_times = df["open_time"].values
        ts_sec = epoch_seconds(df["open_time"])
        closes = df["close"].to_numpy(float)
        funding = load_funding(conn, [sym])

        for _, row in grp.iterrows():
            # The signal candle OPENS one hour before the stamped signal time.
            want = np.datetime64((row["candle"] - pd.Timedelta(hours=1)).tz_localize(None))
            idx = int(np.searchsorted(open_times, want))
            if idx >= len(df) or open_times[idx] != want:
                continue  # candle no longer in the DB (retention/relisting)
            close = float(closes[idx])
            lo_rows = max(0, idx + 1 - RUB_REG_WINDOW_H)
            scan = row["candle"] + pd.Timedelta(minutes=LIVE_SCAN_OFFSET_MIN)
            lo_time = int(
                np.searchsorted(
                    open_times, np.datetime64((scan - pd.Timedelta(days=LIVE_LOAD_DAYS)).tz_localize(None))
                )
            )

            def ind(col: str, default: float, at: int = idx) -> float:
                val = df[col].iloc[at]
                return float(val) if np.isfinite(val) else default

            shared = dict(
                rsi=ind("rsi_14", 50.0),
                tsi_line=ind("tsi_fast_12_7_7", 0.0),
                tsi_signal=ind("tsi_fast_12_7_7_signal", 0.0),
                macd_line=ind("macd_dif_normal_12_26_9", 0.0),
                macd_signal=ind("macd_dea_normal_12_26_9", 0.0),
                atr_14=ind("atr_14", 0.0),
                ema_200=ind("ema_200", close),
            )
            rec: dict = {"coin": sym, "candle": row["candle"], "n_rows_replay_window": idx + 1 - lo_rows,
                         "n_rows_live_window": idx + 1 - lo_time}
            for tag, lo in (("t_", lo_rows), ("L_", lo_time)):
                dist, slope = rub_trend(ts_sec[lo : idx + 1], closes[lo : idx + 1], close)
                rec.update({tag + k: v for k, v in build_rub_features(dist, slope, close, **shared).items()})
            fund = funding_features_asof(funding, sym, row["candle"])
            # Both windows share the funding as-of: the replay and the bot call the
            # same function on the same table, only the load bound differed (95d vs
            # full history), and that bound cannot change a suffix aggregate.
            rec.update({"t_" + k: v for k, v in fund.items()})
            rec.update({"L_" + k: v for k, v in fund.items()})
            out.append(rec)
        if i % 25 == 0:
            print(f"  rebuilt {i}/{len(symbols)} symbols", flush=True)
    return pd.DataFrame(out)


def feature_diffs(joined: pd.DataFrame) -> list[dict]:
    """Per feature: how far the replay's frozen value is from today's rebuild."""
    rows = []
    for feat in EXPECTED_FEATURES:
        stored = joined["r_" + feat].astype(float)
        entry = {"feature": feat}
        for tag, label in (("t_", "replay_window"), ("L_", "live_window")):
            if tag + feat not in joined:
                continue
            diff = (stored - joined[tag + feat].astype(float)).abs()
            entry[label] = {
                "pct_identical": round(float((diff < 1e-9).mean()) * 100, 1),
                "mean_abs_diff": float(f"{diff.mean():.6g}"),
                "p95_abs_diff": float(f"{diff.quantile(0.95):.6g}"),
            }
        rows.append(entry)
    return rows


# ── section 5: threshold curve ───────────────────────────────────────────────


def chrono_test_slice(df: pd.DataFrame, gap_hours: int = 7 * 24) -> pd.DataFrame:
    """The retrain's test slice, reproduced (tools/retrain_from_replay.chrono_split)."""
    t_val = df["candle"].quantile(0.85)
    return df[df["candle"] > t_val + pd.Timedelta(hours=gap_hours)]


def threshold_curve(probs: np.ndarray, pnl: np.ndarray, outcome: np.ndarray, days: float,
                    grid=(0.829, 0.85, 0.88, 0.90, 0.91, 0.93, 0.94)) -> list[dict]:
    """Selectivity curve on the T-070 grid, so the two sides stay comparable."""
    curve = []
    for thr in grid:
        m = probs >= thr
        n = int(m.sum())
        curve.append(
            {
                "threshold": thr,
                "n": n,
                "per_day": round(n / days, 2) if days else None,
                "wr_pct": round(float(outcome[m].mean()) * 100, 1) if n else None,
                "avg_pnl_pct": round(float(pnl[m].mean()), 2) if n else None,
                "sum_pnl_pct": round(float(pnl[m].sum()), 1) if n else None,
            }
        )
    return curve


# ── report ───────────────────────────────────────────────────────────────────


def _md_table(rows: list[dict], cols: list[str]) -> str:
    head = "| " + " | ".join(cols) + " |\n| " + " | ".join("---" for _ in cols) + " |\n"
    body = "".join("| " + " | ".join("" if r.get(c) is None else str(r.get(c)) for c in cols) + " |\n" for r in rows)
    return head + body


def render_markdown(report: dict) -> str:
    out = ["# RUB2 Replay-vs-Live parity probe (T-2026-KYT-9050-008)", ""]
    out.append(f"Window {report['window']['start']} → {report['window']['end']}, "
               f"{report['match']['n_matched']} matched (symbol, candle) pairs, "
               f"{report['match']['n_symbols']} symbols.")
    out.append(f"\nReplay: `{report['inputs']['replay']}`  \nArtifacts: "
               + ", ".join(f"`{os.path.basename(p)}` (thr {t})" for p, t in report["inputs"]["artifacts"]))
    out.append("\n## Agreement per day\n")
    out.append(_md_table(report["per_day"], ["day", "n", "pearson", "mean_abs_diff", "pct_identical", "best_artifact"]))
    out.append("\n## Pooled agreement\n")
    out.append(_md_table(
        [{"artifact": k, **v} for k, v in report["pooled"].items()],
        ["artifact", "n", "pearson", "spearman", "mean_abs_diff", "pct_identical"]))
    if report.get("features"):
        out.append("\n## Feature reconstruction (replay file vs today's DB)\n")
        rows = [{"feature": f["feature"],
                 "identical_%": f.get("replay_window", {}).get("pct_identical"),
                 "mean|d| (replay window)": f.get("replay_window", {}).get("mean_abs_diff"),
                 "mean|d| (live window)": f.get("live_window", {}).get("mean_abs_diff")}
                for f in report["features"]]
        out.append(_md_table(rows, ["feature", "identical_%", "mean|d| (replay window)", "mean|d| (live window)"]))
    if report.get("funding_bound"):
        fb = report["funding_bound"]
        out.append(f"\n## Funding bound\n\nZeroing the whole funding block moves the probability by "
                   f"{fb['mean_swing']} on average (p90 {fb['p90_swing']}). The live-vs-replay residual stays "
                   f"inside that swing for {fb['pct_within']} % of rows.\n")
    if report.get("curve"):
        sl = report["curve"]["slice"]
        out.append(f"\n## Threshold curve — replay test slice\n\n{sl['n']} events, {sl['from']} → {sl['to']} "
                   f"({sl['days']} d), scored with `{report.get('primary_artifact')}`. "
                   f"p99 prob {sl['p99_prob']}, max {sl['max_prob']}.\n")
        out.append(_md_table(report["curve"]["replay"],
                             ["threshold", "n", "per_day", "wr_pct", "avg_pnl_pct", "sum_pnl_pct"]))
    return "\n".join(out) + "\n"


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replay", default=DEFAULT_REPLAY)
    ap.add_argument("--tag", default="RUB2", help="model_name in ml_predictions_master")
    ap.add_argument("--artifact", action="append", default=None,
                    help="artifact to score with; repeatable. Default: the live RUB2 artifact "
                         "plus the MAX1 staging copy (the pre-14.07. RUB2 generation).")
    ap.add_argument("--start", default="2026-07-01")
    ap.add_argument("--end", default="2026-08-01")
    ap.add_argument("--max-symbols", type=int, default=None, help="cap the rebuild (section 3) for a quick run")
    ap.add_argument("--skip-rebuild", action="store_true", help="sections 1/2/5 only — no per-symbol candle reads")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    set_low_priority()  # the fleet shares this box (sequential-jobs rule)
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")

    paths = args.artifact or [
        os.path.join(REPO_ROOT, "rub2_model_SHORT.pkl"),
        os.path.join(REPO_ROOT, "staging_models", "max1_model_SHORT.pkl"),
    ]
    artifacts = {}
    for p in paths:
        if not os.path.exists(p):
            print(f"! artifact missing, skipped: {p}")
            continue
        art = load_artifact(p, EXPECTED_FEATURES, "RUB2")
        if art["loaded"]:
            artifacts[os.path.basename(p)] = art
    if not artifacts:
        raise SystemExit("no artifact loaded — nothing to score with")

    print(f"replay: {args.replay}")
    replay_all = load_replay_short(args.replay)
    replay = replay_all[(replay_all["candle"] >= start) & (replay_all["candle"] < end)].reset_index(drop=True)
    print(f"  {len(replay_all)} SHORT events total, {len(replay)} in the match window")
    conn = get_db_connection()
    live = load_live_predictions(conn, args.tag, "SHORT")
    print(f"live: {len(live)} {args.tag}-SHORT predictions")
    matched = match_pairs(live, replay)
    print(f"matched: {len(matched)} pairs, {matched['coin'].nunique()} symbols")
    if matched.empty:
        raise SystemExit("no overlap between the replay window and the live predictions")

    report: dict = {
        "task": "T-2026-KYT-9050-008",
        "inputs": {"replay": args.replay,
                   "artifacts": [(n, a["threshold"]) for n, a in artifacts.items()]},
        "window": {"start": str(start), "end": str(end)},
        "match": {"n_matched": int(len(matched)), "n_symbols": int(matched["coin"].nunique()),
                  "n_live_total": int(len(live)), "n_replay_in_window": int(len(replay))},
    }

    for name, art in artifacts.items():
        matched[f"p::{name}"] = score(art, matched)
    report["pooled"] = {n: agreement(matched["confidence"], matched[f"p::{n}"]) for n in artifacts}

    # Everything downstream is scored with the generation that actually reproduces
    # the live confidences, not with whatever sits in the repo root today: the RUB2
    # artifact was retrained and re-promoted on 14.07.2026, so the root file is the
    # WRONG model for a July window. Picked by fit, not by filename.
    primary = min(artifacts, key=lambda n: report["pooled"][n]["mean_abs_diff"])
    report["primary_artifact"] = primary
    print(f"primary artifact (best live fit): {primary}")
    matched["day"] = matched["candle"].dt.date
    per_day = []
    for day, grp in matched.groupby("day"):
        best = min(artifacts, key=lambda n: float(np.abs(grp["confidence"] - grp[f"p::{n}"]).mean()))
        row = {"day": str(day), "best_artifact": best}
        row.update(agreement(grp["confidence"], grp[f"p::{primary}"].to_numpy()))
        per_day.append(row)
    report["per_day"] = per_day

    if not args.skip_rebuild:
        print("rebuilding features from today's DB ...")
        rebuilt = rebuild_features(conn, matched, args.max_symbols)
        if not rebuilt.empty:
            joined = matched.merge(rebuilt, on=["coin", "candle"], how="inner")
            print(f"  rebuilt {len(joined)}/{len(matched)} signals")
            report["features"] = feature_diffs(joined)
            art = artifacts[primary]
            variants = {
                "replay_stored": {},
                "rub_from_db_replay_window": {f: "t_" for f in RUB_FEATURES},
                "rub_from_db_live_window": {f: "L_" for f in RUB_FEATURES},
                "funding_from_db": {f: "t_" for f in FUNDING_FEATURES},
                "all_from_db_live_window": {f: "L_" for f in EXPECTED_FEATURES},
            }
            report["substitution"] = {
                k: agreement(joined["confidence"], score(art, joined, v)) for k, v in variants.items()
            }
            # Section 4: how far can the funding block alone move the probability?
            zeroed = joined.copy()
            for f in FUNDING_FEATURES:
                zeroed["r_" + f] = 0.0
            swing = np.abs(score(art, joined) - score(art, zeroed))
            resid = np.abs(joined["confidence"].to_numpy() - score(art, joined))
            report["funding_bound"] = {
                "mean_swing": round(float(swing.mean()), 5),
                "p90_swing": round(float(np.quantile(swing, 0.9)), 5),
                "pct_within": round(float((resid <= swing + 1e-9).mean()) * 100, 1),
                "n_residual_exceeds_swing": int((resid > swing + 1e-9).sum()),
            }

    # Section 5 — the replay's own test slice, on the T-070 grid.
    labelled = replay_all.dropna(subset=["net_pnl_pct", "outcome_tp1"]).reset_index(drop=True)
    if not labelled.empty:
        test = chrono_test_slice(labelled)
        if len(test) > 20:
            probs = score(artifacts[primary], test)
            days = max((test["candle"].max() - test["candle"].min()).total_seconds() / 86400.0, 1.0)
            report["curve"] = {
                "slice": {"n": int(len(test)), "days": round(days, 1),
                          "from": str(test["candle"].min()), "to": str(test["candle"].max()),
                          "p99_prob": round(float(np.quantile(probs, 0.99)), 4),
                          "max_prob": round(float(probs.max()), 4)},
                "replay": threshold_curve(probs, test["net_pnl_pct"].to_numpy(float),
                                          test["outcome_tp1"].to_numpy(float), days),
            }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out + ".json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    with open(args.out + ".md", "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report))
    print(f"\nwrote {args.out}.json / .md")
    print(json.dumps({"pooled": report["pooled"], "funding_bound": report.get("funding_bound")},
                     indent=2, default=str))


if __name__ == "__main__":
    main()
