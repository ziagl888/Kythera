# 27_bot_regime_analyzer.py
"""
Bot Regime Analyzer — Phase 2 of the Regime Orchestrator.

Runs hourly at XX:05:00 and:
  1. Computes historical performance for each Bot × Regime × Alt-Context × Direction
  2. Writes results to bot_regime_performance (UPSERT)
  3. Computes bot_regime_whitelist:
       v1 (LIVE) — two-stage WR logic, authoritative for the gate
       v2 (SHADOW, T-2026-CU-9050-048) — net expectancy lower bound with
          hierarchical EB shrinkage in columns whitelisted_v2/reason_v2;
          NOT read by the live gate (flip is Michi's decision)
  4. Posts daily at 07:00 UTC a cross-table post

Invocation with --initial-run for a complete one-time run.

Watchdog: start_delay=167
"""

from __future__ import annotations

import asyncio
import math
import os
import statistics
import sys
import warnings
from datetime import datetime, timedelta, timezone

# Pandas complains about psycopg2 connections with a UserWarning
# ("only supports SQLAlchemy connectable ..."). The code runs correctly anyway;
# other modules in the project use the same suppression.
warnings.filterwarnings(
    "ignore",
    message=".*SQLAlchemy connectable.*",
    category=UserWarning,
)

import pandas as pd
from psycopg2 import extras as pg_extras

from core.bot_naming import pretty_name
from core.database import get_db_connection
from core.logging_setup import setup_logging

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
COUNTER_TREND_MIN_WR_PCT: float = 60.0
COUNTER_TREND_MIN_ADVANTAGE_PP: float = 10.0
MIN_TRADES_FOR_DECISION: int = 30
REFERENCE_WINDOW_DAYS: int = 30
ANALYSIS_WINDOWS: list[int] = [7, 30, 90]

# P2.25 (write-side): retention for bot_regime_whitelist rows the analyzer no
# longer refreshes. compute_whitelist() only UPSERTs rows for bots that appear
# in the current analysis window (all_bots); a bot that stops trading, or a
# pre-naming-fix raw-name key, leaves its row frozen forever. The orchestrator's
# read side already distrusts any cell older than 48h (28:WHITELIST_MAX_AGE_HOURS
# → overall fallback), so a row untouched for 14 DAYS is provably non-authoritative
# there. 14d is deliberately conservative: comfortably above the analyzer's daily
# cadence (a few missed runs never purge an otherwise-active bot — and if it is
# active, compute_whitelist re-creates the row with a fresh computed_at in the
# same pass) and far above the 48h read gate. Raw-name keys are purged directly
# regardless of age (same criterion as cleanup_stale_performance_rows).
WHITELIST_RETENTION_DAYS: int = 14

# ─────────────────────────────────────────────────────────────────────────────
# WHITELIST v2 — net expectancy gate with hierarchical shrinkage
# (T-2026-CU-9050-048, Report 16 recommendations 6+7)
# ─────────────────────────────────────────────────────────────────────────────
# The v1 gate (wr_bot >= wr_overall) has two structural bugs (Report 16):
#   B1 — 89% of fresh cells are `insufficient_data` → default-open
#        (n < MIN_TRADES_FOR_DECISION passes through instead of deciding).
#   B2 — Median 7 trades/cell: the WR point estimator is too noisy to carry a
#        4D selection; a 55%-WR bot with tiny wins + large losses is net a loser
#        that the WR gate lets through.
#
# v2 replaces the WR point estimator with the LOWER confidence bound of
# net expectancy (avg_pnl_pct), estimated with Empirical-Bayes shrinkage over
# the hierarchy Bot×Regime×Alt → Bot×Regime → Bot×ALL. The cell is only
# whitelisted if this conservative lower bound exceeds break-even — there is no
# `insufficient_data` crutch anymore; every cell yields a usable, downward-biased
# estimate (no default-open).
#
# IMPORTANT: v2 is a SHADOW column (whitelisted_v2). The live gate continues to
# read v1. Sharp-switching is exclusively Michi's decision after the counterfactual
# comparison (T-2026-CU-9050-047). The constants below are deliberately conservative
# starting values — they are calibrated on the VPS DB before each flip, not hardcoded
# here.
#
# Break-even in avg_pnl_pct units: avg_pnl_pct is the raw (unleveraged) price move
# in %, NOT fee-adjusted. Round-trip taker fees on the notional are roughly one-tenth
# of a percent; we demand the lower bound > this floor rather than just > 0. Happens
# to match OUTCOME_MIN_PNL_PCT (neutral band).
V2_BREAK_EVEN_PNL_PCT: float = 0.1
# Prior strength of shrinkage in pseudo-observations (EB: τ² = σ²/k). A cell with
# n real trades carries weight n/(n+k) against the parent mean; the posterior variance
# is σ²/(n+k). Larger = more trust in the prior (parent level), smaller = the cell
# breaks through faster.
V2_SHRINKAGE_PSEUDO_COUNT: float = 25.0
# z multiplier of the one-sided lower bound (~95% ≈ 1.64). Higher = stricter.
V2_LOWER_BOUND_Z: float = 1.64
# Neutral prior mean, towards which the coarsest level shrinks. 0.0 = "for a cell
# without evidence we assume break-even" — a cell with no data stays at 0, the lower
# bound goes negative, it is NOT whitelisted (that is the B1 fix: no default-open).
V2_PRIOR_MEAN_PNL_PCT: float = 0.0
# Fallback variance when no level has a reliable stddev (n>=2). Only relevant for
# cells with almost no data — they fail anyway via the wide lower bound; this value
# just prevents division-by-zero.
V2_DEFAULT_PNL_STDDEV: float = 5.0

# ─────────────────────────────────────────────────────────────────────────────
# TRADE OUTCOME CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────
# We classify each trade as 'win', 'loss', or 'neutral'. Neutral trades
# (housekeeping closes, delistings, outliers) are completely excluded from
# performance statistics — they are neither win nor loss and would skew
# WR + avg_pnl.
#
# The fix addresses four known bugs in 8_ai_trade_monitor.py:
#   1. LEGACY TARGET HIT (+2.5%) writes targets_hit=0 instead of 1 → old wins
#      were counted as losses (EPD1 showed ~0.28% WR instead of ~58%).
#   2. DELISTED/CLEANUP closes have targets_hit=0 → were counted as losses
#      even though the trade was force-closed by symbol delisting.
#   3. close_reason "SL Hit (SL: 0.007)" is unique per trade (contains SL value)
#      → not groupable, but irrelevant if we look at PnL.
#   4. Outliers with |pnl| > 100% hint at data bugs (e.g. negative close prices)
#      and severely skew avg_loss/avg_win.
#
# The logic is PnL-based instead of targets_hit-based:
OUTCOME_MIN_PNL_PCT: float = 0.1  # |pnl| <= 0.1% → neutral (housekeeping)
OUTCOME_MAX_ABS_PNL_PCT: float = 100.0  # |pnl| > 100% → neutral (data bug)

BTC_REGIMES = ["TREND_UP", "TREND_DOWN", "CHOP", "HIGH_VOLA", "TRANSITION"]
ALT_CONTEXTS = ["ALT_STRONG", "ALT_NEUTRAL", "ALT_WEAK"]
DIRECTIONS = ["LONG", "SHORT"]

# Counter-trend directions per BTC regime
COUNTER_TREND_DIRECTIONS: dict[str, str] = {
    "TREND_UP": "SHORT",
    "TREND_DOWN": "LONG",
}

DAILY_POST_HOUR_UTC = 7  # 07:00 UTC

# ─────────────────────────────────────────────────────────────────────────────
logger = setup_logging("BOT_REGIME_ANALYZER")


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────


def is_counter_trend(regime: str, direction: str) -> bool:
    """True if the direction is the 'hard' counter-trend direction for this BTC regime."""
    return COUNTER_TREND_DIRECTIONS.get(regime) == direction


def _compute_stats(pnl_pcts: list[float], is_wins: list[int]) -> dict:
    """Computes performance statistics from lists of PnL% and win flags."""
    n = len(pnl_pcts)
    if n == 0:
        return {}
    win_rate = sum(is_wins) / n * 100
    avg_pnl = statistics.mean(pnl_pcts)
    median_pnl = statistics.median(pnl_pcts)
    stddev = statistics.stdev(pnl_pcts) if n > 1 else 0.0
    sharpe = avg_pnl / stddev if stddev > 0 else None
    worst = min(pnl_pcts)
    best = max(pnl_pcts)
    return {
        "n_trades": n,
        "win_rate": win_rate,
        "avg_pnl_pct": avg_pnl,
        "median_pnl_pct": median_pnl,
        "pnl_stddev": stddev,
        "sharpe_like": sharpe,
        "worst_trade_pct": worst,
        "best_trade_pct": best,
    }


# ─────────────────────────────────────────────────────────────────────────────
# LOAD TRADE DATA
# ─────────────────────────────────────────────────────────────────────────────


def _classify_outcome(close_reason: str, pnl_pct: float) -> str:
    """Classifies a single trade.

    Returns 'win', 'loss', or 'neutral'. 'neutral' excludes the trade from
    performance statistics entirely.

    See constants OUTCOME_MIN_PNL_PCT and OUTCOME_MAX_ABS_PNL_PCT as well as
    the bug description at the start of the file.
    """
    reason = (close_reason or "").upper()
    # Housekeeping closes: neither win nor loss (externally caused,
    # not from bot signal). Includes delisting and orphan cleanup.
    #
    # B9 censorship fix (T-2026-CU-9050-048): REGIME_CHANGE is NOT included
    # anymore. A regime-change forced close has real PnL at the close time — treating
    # it as "neutral" censors exactly the losses that the orchestrator itself
    # realizes through auto-close, and biases the measured ROM1 WR upward (Report 16,
    # B9). Such closes now go through normal PnL classification; near-0% closes remain
    # neutral via the micro-PnL filter (OUTCOME_MIN_PNL_PCT) anyway. In practice only
    # model='ROM1' carries this marker (P1.9), so the fix does not touch foreign-bot
    # statistics.
    if "DELISTED" in reason or "CLEANUP" in reason or "ORPHAN" in reason:
        return "neutral"
    if pnl_pct is None or (isinstance(pnl_pct, float) and pd.isna(pnl_pct)):
        return "neutral"
    try:
        p = float(pnl_pct)
    except (TypeError, ValueError):
        return "neutral"
    # Outlier filter (probably data bug)
    if abs(p) > OUTCOME_MAX_ABS_PNL_PCT:
        return "neutral"
    # Neutral micro-movements (housekeeping with close ≈ entry)
    if abs(p) <= OUTCOME_MIN_PNL_PCT:
        return "neutral"
    return "win" if p > 0 else "loss"


def _apply_outcome_classification(df: pd.DataFrame) -> pd.DataFrame:
    """Applies _classify_outcome to a DataFrame and overwrites is_win.

    Adds the 'outcome' column and filters out neutral trades. The returned
    DataFrame contains only 'win' and 'loss' trades.
    """
    if df.empty:
        return df
    df = df.copy()
    # close_reason column might be missing (classic trades) → set to empty
    if "close_reason" not in df.columns:
        df["close_reason"] = ""
    df["close_reason"] = df["close_reason"].fillna("").astype(str)
    # Compute outcome
    df["outcome"] = df.apply(
        lambda r: _classify_outcome(r["close_reason"], r["pnl_pct"]),
        axis=1,
    )
    # Recompute is_win (overrides value from SQL query)
    df["is_win"] = (df["outcome"] == "win").astype(int)
    # Exclude neutral trades
    n_before = len(df)
    df = df[df["outcome"].isin(["win", "loss"])].copy()
    n_after = len(df)
    if n_before > n_after:
        logger.info(
            f"Outcome classification: {n_before - n_after}/{n_before} trades "
            f"excluded as neutral (delisting/housekeeping/outlier)"
        )
    return df


def load_trades_with_regime(conn, window_days: int) -> pd.DataFrame:
    """
    Loads all closed trades (classic + AI) from the last window_days,
    annotated with the regime at trade open time.

    Returns DataFrame with columns:
        bot_name, direction, entry, close_price, is_win, pnl_pct,
        regime, alt_context, opened_at, close_reason, outcome

    Neutral trades (DELISTED, outlier, micro-PnL) are ALREADY filtered out
    in the returned DataFrame — the caller does not need to worry about them.
    is_win was recomputed from PnL, overriding the SQL value.
    """
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=window_days)

    # ── Classic Trades ──
    # Note: is_win is not read from SQL — _apply_outcome_classification
    # overrides it anyway based on PnL. This also avoids crashes if status
    # contains values like "SL1", "WORKING" or other non-integer strings
    # (from legacy bots or manual DB edits). close_reason does not exist for
    # classic trades — we take the raw status string instead so unusual entries
    # are visible in logs (e.g. for later DELISTED check if someone adds that).
    try:
        df_classic = pd.read_sql_query(
            """
            SELECT
                t.strategy AS bot_name,
                t.time AS opened_at,
                t.direction,
                t.entry,
                t.close_price,
                0 AS is_win,
                COALESCE(t.status::text, '') AS close_reason,
                CASE
                    WHEN t.direction = 'LONG'
                        THEN (t.close_price - t.entry) / NULLIF(t.entry, 0) * 100
                    ELSE (t.entry - t.close_price) / NULLIF(t.entry, 0) * 100
                END AS pnl_pct,
                (
                    SELECT rh.regime FROM regime_history rh
                    WHERE rh.ts <= t.time
                    ORDER BY rh.ts DESC LIMIT 1
                ) AS regime,
                (
                    SELECT rh.alt_context FROM regime_history rh
                    WHERE rh.ts <= t.time
                    ORDER BY rh.ts DESC LIMIT 1
                ) AS alt_context
            FROM closed_trades_master t
            WHERE t.time >= %s
              AND t.entry IS NOT NULL AND t.close_price IS NOT NULL
              AND t.entry > 0
            """,
            conn,
            params=(since,),
        )
    except Exception as e:
        logger.warning(f"closed_trades_master nicht ladbar: {e}")
        df_classic = pd.DataFrame()

    # ── AI Trades ──
    # The `status` column in closed_ai_signals contains the close_reason
    # (e.g. "LEGACY TARGET HIT (+2.5%)", "DELISTED / CLEANUP", "SL Hit (SL: ...)")
    # — see 8_ai_trade_monitor.py lines 192-195.
    #
    # is_win is not read here — _apply_outcome_classification overrides it anyway
    # based on PnL. This also avoids issues if targets_hit ever contains NULL or
    # delivers other surprises.
    try:
        df_ai = pd.read_sql_query(
            """
            SELECT
                t.model AS bot_name,
                t.open_time AS opened_at,
                t.direction,
                t.entry AS entry,
                t.close_price,
                0 AS is_win,
                COALESCE(t.status, '') AS close_reason,
                CASE
                    WHEN t.direction = 'LONG'
                        THEN (t.close_price - t.entry) / NULLIF(t.entry, 0) * 100
                    ELSE (t.entry - t.close_price) / NULLIF(t.entry, 0) * 100
                END AS pnl_pct,
                (
                    SELECT rh.regime FROM regime_history rh
                    WHERE rh.ts <= t.open_time
                    ORDER BY rh.ts DESC LIMIT 1
                ) AS regime,
                (
                    SELECT rh.alt_context FROM regime_history rh
                    WHERE rh.ts <= t.open_time
                    ORDER BY rh.ts DESC LIMIT 1
                ) AS alt_context
            FROM closed_ai_signals t
            WHERE t.open_time >= %s
              AND t.entry IS NOT NULL AND t.close_price IS NOT NULL
              AND t.entry > 0
            """,
            conn,
            params=(since,),
        )
    except Exception as e:
        logger.warning(f"closed_ai_signals not loadable: {e}")
        df_ai = pd.DataFrame()

    frames = [f for f in [df_classic, df_ai] if not f.empty]
    if not frames:
        return pd.DataFrame(
            columns=[
                "bot_name",
                "direction",
                "entry",
                "close_price",
                "is_win",
                "pnl_pct",
                "regime",
                "alt_context",
                "opened_at",
                "close_reason",
                "outcome",
            ]
        )

    df = pd.concat(frames, ignore_index=True)
    df = df.dropna(subset=["regime", "alt_context", "pnl_pct"])

    # Normalise bot names before aggregating — otherwise MIS1-8H, MIS1-8h,
    # MIS1-8h_pump end up as separate bots in bot_regime_performance and the
    # market tracker (which queries with pretty_name) cannot find them. Same goes
    # for "Fast In And Out" vs "FastInOut" etc. pretty_name is idempotent, i.e.
    # already normalised names remain unchanged.
    df["bot_name"] = df["bot_name"].apply(pretty_name)

    # ──────────────────────────────────────────────────────────────────
    # CORE FIX: Classification of win/loss based on actual PnL and close_reason,
    # not on the faulty targets_hit field. Filters out neutral trades immediately.
    # ──────────────────────────────────────────────────────────────────
    df = _apply_outcome_classification(df)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# COMPUTE & UPSERT PERFORMANCE
# ─────────────────────────────────────────────────────────────────────────────


def compute_and_upsert_performance(conn, df: pd.DataFrame, window_days: int) -> int:
    """
    Computes bot_regime_performance stats and upserts into DB.

    Also computes aggregate rows:
      regime='ALL', alt_context='ALL' → overall performance per bot/direction
      regime=<x>,   alt_context='ALL' → per-btc-regime aggregated over alt_context
      regime='ALL', alt_context=<y>   → per-alt-context aggregated over regimes

    Returns number of rows upserted.
    """
    if df.empty:
        return 0

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = []

    bots = df["bot_name"].unique()

    for bot in bots:
        df_bot = df[df["bot_name"] == bot]

        for direction in DIRECTIONS:
            df_dir = df_bot[df_bot["direction"] == direction]

            # Overall aggregate (ALL × ALL)
            overall_stats = _compute_stats(
                df_dir["pnl_pct"].tolist(),
                df_dir["is_win"].tolist(),
            )
            if overall_stats:
                rows.append(
                    (
                        bot,
                        "ALL",
                        "ALL",
                        direction,
                        window_days,
                        overall_stats["n_trades"],
                        overall_stats["win_rate"],
                        overall_stats["avg_pnl_pct"],
                        overall_stats["median_pnl_pct"],
                        overall_stats["pnl_stddev"],
                        overall_stats["sharpe_like"],
                        overall_stats["worst_trade_pct"],
                        overall_stats["best_trade_pct"],
                        now_utc,
                    )
                )

            # Per BTC-Regime, aggregated over alt_context (regime × ALL)
            for regime in BTC_REGIMES:
                df_reg = df_dir[df_dir["regime"] == regime]
                stats = _compute_stats(
                    df_reg["pnl_pct"].tolist(),
                    df_reg["is_win"].tolist(),
                )
                if stats:
                    rows.append(
                        (
                            bot,
                            regime,
                            "ALL",
                            direction,
                            window_days,
                            stats["n_trades"],
                            stats["win_rate"],
                            stats["avg_pnl_pct"],
                            stats["median_pnl_pct"],
                            stats["pnl_stddev"],
                            stats["sharpe_like"],
                            stats["worst_trade_pct"],
                            stats["best_trade_pct"],
                            now_utc,
                        )
                    )

                # 4D: regime × alt_context
                for alt in ALT_CONTEXTS:
                    df_cell = df_reg[df_reg["alt_context"] == alt]
                    stats_4d = _compute_stats(
                        df_cell["pnl_pct"].tolist(),
                        df_cell["is_win"].tolist(),
                    )
                    if stats_4d:
                        rows.append(
                            (
                                bot,
                                regime,
                                alt,
                                direction,
                                window_days,
                                stats_4d["n_trades"],
                                stats_4d["win_rate"],
                                stats_4d["avg_pnl_pct"],
                                stats_4d["median_pnl_pct"],
                                stats_4d["pnl_stddev"],
                                stats_4d["sharpe_like"],
                                stats_4d["worst_trade_pct"],
                                stats_4d["best_trade_pct"],
                                now_utc,
                            )
                        )

            # Per alt_context, aggregated over regimes (ALL × alt_context)
            for alt in ALT_CONTEXTS:
                df_alt = df_dir[df_dir["alt_context"] == alt]
                stats = _compute_stats(
                    df_alt["pnl_pct"].tolist(),
                    df_alt["is_win"].tolist(),
                )
                if stats:
                    rows.append(
                        (
                            bot,
                            "ALL",
                            alt,
                            direction,
                            window_days,
                            stats["n_trades"],
                            stats["win_rate"],
                            stats["avg_pnl_pct"],
                            stats["median_pnl_pct"],
                            stats["pnl_stddev"],
                            stats["sharpe_like"],
                            stats["worst_trade_pct"],
                            stats["best_trade_pct"],
                            now_utc,
                        )
                    )

        # BOTH direction aggregate
        df_both = df_bot
        for regime in BTC_REGIMES + ["ALL"]:
            df_r = df_both if regime == "ALL" else df_both[df_both["regime"] == regime]
            for alt in ALT_CONTEXTS + ["ALL"]:
                df_cell = df_r if alt == "ALL" else df_r[df_r["alt_context"] == alt]
                stats = _compute_stats(
                    df_cell["pnl_pct"].tolist(),
                    df_cell["is_win"].tolist(),
                )
                if stats:
                    rows.append(
                        (
                            bot,
                            regime,
                            alt,
                            "BOTH",
                            window_days,
                            stats["n_trades"],
                            stats["win_rate"],
                            stats["avg_pnl_pct"],
                            stats["median_pnl_pct"],
                            stats["pnl_stddev"],
                            stats["sharpe_like"],
                            stats["worst_trade_pct"],
                            stats["best_trade_pct"],
                            now_utc,
                        )
                    )

    if not rows:
        return 0

    with conn.cursor() as cur:
        pg_extras.execute_values(
            cur,
            """
            INSERT INTO bot_regime_performance
                (bot_name, regime, alt_context, direction, window_days,
                 n_trades, win_rate, avg_pnl_pct, median_pnl_pct,
                 pnl_stddev, sharpe_like, worst_trade_pct, best_trade_pct,
                 last_computed)
            VALUES %s
            ON CONFLICT (bot_name, regime, alt_context, direction, window_days)
            DO UPDATE SET
                n_trades = EXCLUDED.n_trades,
                win_rate = EXCLUDED.win_rate,
                avg_pnl_pct = EXCLUDED.avg_pnl_pct,
                median_pnl_pct = EXCLUDED.median_pnl_pct,
                pnl_stddev = EXCLUDED.pnl_stddev,
                sharpe_like = EXCLUDED.sharpe_like,
                worst_trade_pct = EXCLUDED.worst_trade_pct,
                best_trade_pct = EXCLUDED.best_trade_pct,
                last_computed = EXCLUDED.last_computed
            """,
            rows,
        )
    append_performance_history(conn, rows)
    conn.commit()
    return len(rows)


# ─────────────────────────────────────────────────────────────────────────────
# SNAPSHOT HISTORY (T-2026-KYT-9050-072)
# ─────────────────────────────────────────────────────────────────────────────
#
# bot_regime_performance is a SNAPSHOT: exactly one row per
# (bot, regime, alt_context, direction, window_days), overwritten on every run.
# Measured 2026-08-02: zero cells with more than one row.
#
# Consequence: the cell statistics the whitelist gate decided on at the time of
# any past event no longer exist. No gate variant — v1, v2 or a future one — can
# therefore be checked cleanly against its own past. T-2026-KYT-9050-007 had to
# score today's statistics against yesterday's traffic and could not separate the
# parameter effect from cell drift; T-031 hit the same wall.
#
# One row per cell per DAY fixes that going forward. Volume: ~2272 cells per
# 30d window x 3 windows ~ 6900 rows/day, ~2.5M/year — trivial for Postgres and
# capped by retention.
HISTORY_TABLE = "bot_regime_performance_history"
HISTORY_RETENTION_ENV = "KYTHERA_REGIME_HISTORY_RETENTION_DAYS"
HISTORY_RETENTION_DEFAULT_DAYS = 400  # a year of hindsight plus buffer


def _history_retention_days() -> int:
    raw = (os.getenv(HISTORY_RETENTION_ENV) or "").strip()
    if not raw:
        return HISTORY_RETENTION_DEFAULT_DAYS
    try:
        value = int(raw)
    except ValueError:
        return HISTORY_RETENTION_DEFAULT_DAYS
    return value if value > 0 else HISTORY_RETENTION_DEFAULT_DAYS


def append_performance_history(conn, rows: list[tuple]) -> int:
    """Append today's state as its own row per cell.

    `rows` is the IDENTICAL tuple list the main upsert writes — one source, not a
    second computation that can drift. The snapshot key is the calendar DAY
    (UTC): if the analyzer runs several times a day the later run overwrites the
    earlier one, so exactly one row per cell per day survives.

    RUNS IN A SAVEPOINT. This history is a measurement aid; the gate beneath it
    is the money path. A failure here (missing rights, full disk) must not drag
    the main upsert into the rollback — without the savepoint it would, because
    both live in the same transaction. Returns rows written, 0 if the append
    failed.
    """
    if not rows:
        return 0
    snapshot_day = datetime.now(timezone.utc).date()
    hist_rows = [(snapshot_day, *r) for r in rows]
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT regime_history")
            try:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
                        snapshot_date   date NOT NULL,
                        bot_name        text NOT NULL,
                        regime          text NOT NULL,
                        alt_context     text NOT NULL,
                        direction       text NOT NULL,
                        window_days     integer NOT NULL,
                        n_trades        integer,
                        win_rate        double precision,
                        avg_pnl_pct     double precision,
                        median_pnl_pct  double precision,
                        pnl_stddev      double precision,
                        sharpe_like     double precision,
                        worst_trade_pct double precision,
                        best_trade_pct  double precision,
                        last_computed   timestamp,
                        PRIMARY KEY (snapshot_date, bot_name, regime, alt_context, direction, window_days)
                    )
                    """
                )
                pg_extras.execute_values(
                    cur,
                    f"""
                    INSERT INTO {HISTORY_TABLE}
                        (snapshot_date, bot_name, regime, alt_context, direction, window_days,
                         n_trades, win_rate, avg_pnl_pct, median_pnl_pct,
                         pnl_stddev, sharpe_like, worst_trade_pct, best_trade_pct,
                         last_computed)
                    VALUES %s
                    ON CONFLICT (snapshot_date, bot_name, regime, alt_context, direction, window_days)
                    DO UPDATE SET
                        n_trades = EXCLUDED.n_trades,
                        win_rate = EXCLUDED.win_rate,
                        avg_pnl_pct = EXCLUDED.avg_pnl_pct,
                        median_pnl_pct = EXCLUDED.median_pnl_pct,
                        pnl_stddev = EXCLUDED.pnl_stddev,
                        sharpe_like = EXCLUDED.sharpe_like,
                        worst_trade_pct = EXCLUDED.worst_trade_pct,
                        best_trade_pct = EXCLUDED.best_trade_pct,
                        last_computed = EXCLUDED.last_computed
                    """,
                    hist_rows,
                )
                cur.execute(
                    f"DELETE FROM {HISTORY_TABLE} WHERE snapshot_date < %s",
                    (snapshot_day - timedelta(days=_history_retention_days()),),
                )
                cur.execute("RELEASE SAVEPOINT regime_history")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT regime_history")
                raise
    except Exception as exc:  # noqa: BLE001 - never break the gate for a measurement aid
        logger.warning(
            "%s: snapshot history could not be written (%s) - the main upsert stands. "
            "Without this history no gate variant can be checked against its own past "
            "(T-2026-KYT-9050-072).",
            HISTORY_TABLE,
            exc,
        )
        return 0
    return len(hist_rows)


# ─────────────────────────────────────────────────────────────────────────────
# COMPUTE WHITELIST
# ─────────────────────────────────────────────────────────────────────────────


def _v2_expectancy_lower_bound(
    cell: dict | None,
    parent_regime: dict | None,
    parent_overall: dict | None,
) -> tuple[float, float, float, str]:
    """Hierarchical Empirical-Bayes lower bound of a cell's net expectancy.

    Each level dict (or None if that level has no performance row) carries::

        {"n": int, "avg_pnl": float | None, "std": float | None}

    with avg_pnl = avg_pnl_pct and std = pnl_stddev from bot_regime_performance.
    The three levels form the hierarchy (finest first, as the caller passes them):

      cell           = Bot × Regime × Alt × Direction  — the 4D cell we decide
      parent_regime  = Bot × Regime × ALL × Direction  — pooled over alt_context
      parent_overall = Bot × ALL    × ALL × Direction  — the bot's overall dir

    Shrinkage (sequential EB, coarse→fine): start at V2_PRIOR_MEAN_PNL_PCT and,
    for each level that has data, blend the running estimate toward that level's
    mean with weight n / (n + k). The finest well-populated level dominates; a
    sparse 4D cell inherits the coarser (Bot×Regime, then Bot×ALL) estimate; a
    cell with no data at ANY level stays at the neutral prior (→ lb < break-even
    → not whitelisted). This is the B1 fix: no `insufficient_data` default-open.

    Lower bound (EB posterior): se = std_used / sqrt(n_cell + k),
    lb = est − z·se. n_cell is the 4D cell's OWN n (0 if absent) — the prior
    contributes k pseudo-observations of certainty, so a sparse cell keeps a
    wide interval even when a strong parent lifts `est`, and must earn its own
    evidence (or lean on a robustly-positive parent) to clear break-even.
    std_used is the finest level with a usable spread (n≥2), else the coarser
    fallback, else V2_DEFAULT_PNL_STDDEV.

    Returns (shrunk_mean, lower_bound, n_eff, prior_source). Pure/DB-free.
    """
    # Coarse → fine so the finest populated level wins the sequential blend.
    ordered = [
        ("bot_all", parent_overall),
        ("bot_regime", parent_regime),
        ("cell", cell),
    ]

    est = V2_PRIOR_MEAN_PNL_PCT
    prior_source = "prior"
    for name, lvl in ordered:
        if lvl and lvl.get("n") and lvl["n"] > 0 and lvl.get("avg_pnl") is not None:
            n = float(lvl["n"])
            w = n / (n + V2_SHRINKAGE_PSEUDO_COUNT)
            est = w * float(lvl["avg_pnl"]) + (1.0 - w) * est
            prior_source = name

    # Variance: finest level with reliable stddev (fine → coarse).
    std_used: float | None = None
    for _name, lvl in reversed(ordered):
        if lvl and lvl.get("n") and lvl["n"] >= 2 and lvl.get("std") is not None:
            s = float(lvl["std"])
            if math.isfinite(s) and s > 0:
                std_used = s
                break
    if std_used is None:
        std_used = V2_DEFAULT_PNL_STDDEV

    n_cell = float(cell["n"]) if (cell and cell.get("n")) else 0.0
    n_eff = n_cell + V2_SHRINKAGE_PSEUDO_COUNT
    se = std_used / math.sqrt(n_eff)
    lb = est - V2_LOWER_BOUND_Z * se
    return est, lb, n_eff, prior_source


def _v2_whitelist_decision(
    cell: dict | None,
    parent_regime: dict | None,
    parent_overall: dict | None,
) -> tuple[bool, str]:
    """Shadow v2 gate decision for one 4D cell (see _v2_expectancy_lower_bound).

    whitelisted_v2 = (lower bound of net expectancy > break-even). The reason
    string carries the numbers so the counterfactual comparison
    (tools/rom1_counterfactual.py) can bucket by gate path and audit the flip.
    """
    est, lb, n_eff, src = _v2_expectancy_lower_bound(cell, parent_regime, parent_overall)
    whitelisted = lb > V2_BREAK_EVEN_PNL_PCT
    verdict = "pass" if whitelisted else "block"
    reason = f"v2_{verdict}:lb={lb:.3f}:est={est:.3f}:src={src}:neff={n_eff:.0f}"
    return whitelisted, reason


def compute_whitelist(conn) -> int:
    """
    Computes bot_regime_whitelist from bot_regime_performance.

    v1 (LIVE gate, unchanged) — 2-stage logic:
      Stage 1: Counter-trend direction? → strict rule (≥60% AND ≥overall+10pp)
      Stage 2: Standard direction?      → wr ≥ overall suffices
      n < MIN_TRADES_FOR_DECISION        → whitelisted (insufficient data)

    v2 (SHADOW column whitelisted_v2, T-2026-CU-9050-048) — net-expectancy
    lower bound with hierarchical EB shrinkage (_v2_whitelist_decision). Written
    alongside v1; NOT read by the live gate. The flip to v2 is Michi's call
    after the counterfactual comparison (T-2026-CU-9050-047) — never here.

    Returns number of whitelist rows written.
    """
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    # Load all relevant performance data. avg_pnl_pct + pnl_stddev feed the v2
    # shadow gate (net-expectancy shrinkage); v1 only needs n + win_rate.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bot_name, regime, alt_context, direction,
                   n_trades, win_rate, avg_pnl_pct, pnl_stddev
            FROM bot_regime_performance
            WHERE window_days = %s
            """,
            (REFERENCE_WINDOW_DAYS,),
        )
        perf_rows = cur.fetchall()

    if not perf_rows:
        logger.warning("No performance data for whitelist computation.")
        return 0

    # Index by (bot, regime, alt_context, direction)
    perf: dict[tuple, dict] = {}
    for row in perf_rows:
        key = (row[0], row[1], row[2], row[3])
        perf[key] = {
            "n": row[4],
            "wr": row[5] or 0.0,
            "avg_pnl": row[6],
            "std": row[7],
        }

    # Collect all bots
    all_bots = {r[0] for r in perf_rows}

    whitelist_rows = []

    for bot in all_bots:
        for regime in BTC_REGIMES:
            for alt in ALT_CONTEXTS:
                for direction in DIRECTIONS:
                    # 4D-specific performance
                    cell_key = (bot, regime, alt, direction)
                    cell = perf.get(cell_key, {})
                    n = cell.get("n", 0)
                    wr_bot = cell.get("wr", 0.0)

                    # Overall (ALL × ALL) performance in this direction
                    overall_key = (bot, "ALL", "ALL", direction)
                    overall = perf.get(overall_key, {})
                    wr_overall = overall.get("wr", 0.0)

                    # ── v2 SHADOW decision (T-2026-CU-9050-048) ──────────────
                    # Net-expectancy lower bound with hierarchical EB shrinkage
                    # Bot×Regime×Alt → Bot×Regime → Bot×ALL. Written to the
                    # shadow columns only; the live gate stays on v1 below.
                    whitelisted_v2, reason_v2 = _v2_whitelist_decision(
                        perf.get(cell_key),
                        perf.get((bot, regime, "ALL", direction)),
                        perf.get(overall_key),
                    )

                    # ── v1 LIVE decision (unchanged, authoritative) ──────────
                    # Decision logic
                    if n < MIN_TRADES_FOR_DECISION:
                        whitelisted = True
                        reason = "insufficient_data"

                    elif is_counter_trend(regime, direction):
                        # Strict rule for counter-trend
                        if wr_bot >= COUNTER_TREND_MIN_WR_PCT and wr_bot >= wr_overall + COUNTER_TREND_MIN_ADVANTAGE_PP:
                            whitelisted = True
                            reason = "counter_trend_specialist"
                        else:
                            whitelisted = False
                            reason = "counter_trend_insufficient"

                    else:
                        # Standard rule
                        if wr_bot >= wr_overall:
                            whitelisted = True
                            reason = "wr_above_overall"
                        else:
                            whitelisted = False
                            reason = "wr_below_overall"

                    whitelist_rows.append(
                        (
                            bot,
                            regime,
                            alt,
                            direction,
                            whitelisted,
                            reason,
                            now_utc,
                            whitelisted_v2,
                            reason_v2,
                        )
                    )

    with conn.cursor() as cur:
        pg_extras.execute_values(
            cur,
            """
            INSERT INTO bot_regime_whitelist
                (bot_name, regime, alt_context, direction,
                 whitelisted, reason, computed_at,
                 whitelisted_v2, reason_v2)
            VALUES %s
            ON CONFLICT (bot_name, regime, alt_context, direction)
            DO UPDATE SET
                whitelisted = EXCLUDED.whitelisted,
                reason = EXCLUDED.reason,
                computed_at = EXCLUDED.computed_at,
                whitelisted_v2 = EXCLUDED.whitelisted_v2,
                reason_v2 = EXCLUDED.reason_v2
            """,
            whitelist_rows,
        )
    conn.commit()
    logger.info(f"✅ Whitelist computed: {len(whitelist_rows)} entries")
    return len(whitelist_rows)


# ─────────────────────────────────────────────────────────────────────────────
# DAILY CROSS-TABLE POST
# ─────────────────────────────────────────────────────────────────────────────


async def post_daily_cross_table() -> None:
    """
    Posts daily Bot × Alt-Context × Direction performance table
    to REGIME_STATUS_CHANNEL_ID at 07:00 UTC.
    """
    from core.config import REGIME_STATUS_CHANNEL_ID
    from core.market_utils import send_telegram

    conn = None
    try:
        conn = get_db_connection()

        # Get current regime
        with conn.cursor() as cur:
            cur.execute("SELECT regime, alt_context FROM regime_current WHERE id = 1")
            row = cur.fetchone()

        cur_regime = row[0] if row else "TREND_UP"
        cur_alt = row[1] if row else "ALT_NEUTRAL"

        # Load performance data for current BTC-Regime
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT bot_name, alt_context, direction, n_trades, win_rate
                FROM bot_regime_performance
                WHERE regime = %s AND window_days = %s
                  AND direction IN ('LONG', 'SHORT')
                ORDER BY bot_name, direction, alt_context
                """,
                (cur_regime, REFERENCE_WINDOW_DAYS),
            )
            rows = cur.fetchall()

        if not rows:
            send_telegram(
                f"📊 Bot×Regime no data (Regime: {cur_regime})",
                REGIME_STATUS_CHANNEL_ID,
            )
            return

        # Build table
        df = pd.DataFrame(rows, columns=["bot", "alt", "dir", "n", "wr"])

        def _cell(bot, alt, dir_):
            mask = (df["bot"] == bot) & (df["alt"] == alt) & (df["dir"] == dir_)
            sub = df[mask]
            if sub.empty:
                return "  ---"
            n = sub.iloc[0]["n"]
            wr = sub.iloc[0]["wr"]
            if n is None or n < 20:
                return "  ---"
            return f"{wr:>4.0f}%"

        bots = sorted(df["bot"].unique())
        header = (
            f"📊 BOT × ALT-CONTEXT PERFORMANCE — {cur_regime} (30d)\n\n"
            f"Bot          LONG                          SHORT\n"
            f"             ALT_W    ALT_N    ALT_S       ALT_W    ALT_N    ALT_S\n"
            f"{'─' * 67}"
        )

        lines = [header]
        for bot in bots:
            ll = _cell(bot, "ALT_WEAK", "LONG")
            ln = _cell(bot, "ALT_NEUTRAL", "LONG")
            ls = _cell(bot, "ALT_STRONG", "LONG")
            sl = _cell(bot, "ALT_WEAK", "SHORT")
            sn = _cell(bot, "ALT_NEUTRAL", "SHORT")
            ss = _cell(bot, "ALT_STRONG", "SHORT")
            lines.append(f"{bot:<12} {ll}    {ln}    {ls}       {sl}    {sn}    {ss}")

        # P3.10 (spec-drift, documented not fixed): this ↑/↓ legend is ORPHANED —
        # _cell() above only ever returns "{wr}%" or "---", it never appends an
        # arrow, so no cross-table cell renders ↑/↓. Kept as-is (cosmetic); the
        # marker feature was specced (REGIME_ORCHESTRATOR.md) but never built.
        lines.append(
            f"\nCurrent regime: {cur_regime}  |  Current alt-context: {cur_alt}\n"
            f"↑ WR ≥ overall+10pp → STRONG\n"
            f"↓ WR ≤ overall-10pp → WEAK\n"
            f"--- < 20 trades in this cell"
        )

        msg = "<pre>" + "\n".join(lines) + "</pre>"
        send_telegram(msg, REGIME_STATUS_CHANNEL_ID)
        logger.info(f"✅ Daily cross-table post sent ({cur_regime})")

    except Exception as e:
        logger.error(f"Error in daily cross-table post: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# ANALYSIS JOB
# ─────────────────────────────────────────────────────────────────────────────


def cleanup_stale_performance_rows(conn) -> int:
    """Deletes bot_regime_performance rows with non-normalised bot names.

    Background: before the naming fix, the analyzer wrote rows with raw names
    like 'Fast In And Out', 'MIS1-8H' or 'MIS1-168H_pump'. The market tracker
    queries with pretty_name() though — e.g. 'FastInOut', 'MIS1-8h'. The old rows
    are silent forever.

    This cleanup does a one-time reset: all rows whose bot_name differs from
    pretty_name(bot_name) are deleted. The subsequent run_analysis() rebuilds
    them with normalised names.

    After the first clean run, this function is idempotent (deletes nothing)
    because all new rows are already written normalised.

    Returns: number of deleted rows.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT bot_name FROM bot_regime_performance")
            bot_names = [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"Cleanup scan failed: {e}")
        return 0

    stale = [b for b in bot_names if pretty_name(b) != b]
    if not stale:
        return 0

    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM bot_regime_performance WHERE bot_name = ANY(%s)",
                (stale,),
            )
            deleted = cur.rowcount
        conn.commit()
        logger.info(
            f"🧹 Cleanup stale bot_names in bot_regime_performance: "
            f"{deleted} rows cleaned from {len(stale)} bot_names "
            f"(e.g. {stale[:3]})"
        )
        return deleted
    except Exception as e:
        logger.warning(f"Cleanup delete failed: {e}")
        conn.rollback()
        return 0


def build_whitelist_cleanup_query(now_utc: datetime, retention_days: int) -> tuple[str, tuple]:
    """Builds the DELETE for stale bot_regime_whitelist rows (P2.25 write-side).

    Two disjoint DELETE criteria, OR-combined:
      (A) raw-name keys — `pretty_name(bot_name) <> bot_name`. These are the
          pre-naming-fix rows (frozen since 2026-04-19) that the P0.4 root cause
          was about: the analyzer now writes pretty_name()-normalized keys and
          the orchestrator reads them, so a raw-name row is provably orphaned —
          never re-UPSERTed, never read. Same criterion as
          cleanup_stale_performance_rows, applied age-independently.
      (B) age — `computed_at < now - retention_days`. A normalized-key row the
          analyzer stopped refreshing (a bot that left the analysis window). The
          orchestrator's 48h read gate already treats it as stale → overall
          fallback, so purging it after 14d changes no live decision; if the bot
          trades again, compute_whitelist re-creates the row.

    Returned as (sql, params) so a DB-free test can assert the exact predicate
    shape without a live connection. The pretty_name normalization is done in
    Python (build the raw-name list from a DISTINCT scan) — Postgres has no
    pretty_name(); the caller passes the resolved list.
    """
    cutoff = now_utc - timedelta(days=retention_days)
    sql = "DELETE FROM bot_regime_whitelist WHERE bot_name = ANY(%s)    OR computed_at < %s"
    return sql, (cutoff,)


def cleanup_stale_whitelist_rows(conn) -> int:
    """Deletes stale bot_regime_whitelist rows (P2.25, write-side).

    compute_whitelist() only UPSERTs rows for bots in the current analysis window
    (all_bots) — rows from bots that no longer trade, as well as old raw-name keys
    (frozen since 19.04.) stay forever. The orchestrator reads them via 48h-staleness
    gate but no longer authoritatively (T-046), but the rows themselves were never
    cleaned up.

    Criteria: see build_whitelist_cleanup_query: (A) raw-name keys (pretty_name
    mismatch, age-independent) OR (B) computed_at older than WHITELIST_RETENTION_DAYS.
    Conservative: the read gate already invalidates everything >48h, active bots are
    rewritten fresh in the same run.

    Returns: number of deleted rows.
    """
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT bot_name FROM bot_regime_whitelist")
            bot_names = [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"Whitelist cleanup scan failed: {e}")
        return 0

    raw_name_keys = [b for b in bot_names if pretty_name(b) != b]

    sql, params = build_whitelist_cleanup_query(now_utc, WHITELIST_RETENTION_DAYS)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (raw_name_keys,) + params)
            deleted = cur.rowcount
        conn.commit()
        if deleted:
            logger.info(
                f"🧹 Cleanup stale bot_regime_whitelist: {deleted} rows removed "
                f"(raw-name keys: {len(raw_name_keys)}, e.g. {raw_name_keys[:3]}; "
                f"+ rows older than {WHITELIST_RETENTION_DAYS}d)"
            )
        return deleted
    except Exception as e:
        logger.warning(f"Whitelist cleanup delete failed: {e}")
        conn.rollback()
        return 0


async def run_analysis() -> None:
    """Main analysis job: loads trades, computes performance, updates whitelist."""
    conn = None
    try:
        conn = get_db_connection()
        # One-time cleanup: removes stale rows with non-normalised bot names.
        # After first run idempotent (does nothing anymore).
        cleanup_stale_performance_rows(conn)
        # P2.25 (write-side): clean up the same stale-row class in the whitelist
        # table (raw-name keys + rows older than retention) that the orchestrator
        # reads. Before compute_whitelist(), which rewrites active bots fresh
        # immediately afterward.
        cleanup_stale_whitelist_rows(conn)

        total_rows = 0
        for window in ANALYSIS_WINDOWS:
            df = load_trades_with_regime(conn, window)
            n = compute_and_upsert_performance(conn, df, window)
            total_rows += n
            logger.info(f"Window {window}d: {len(df)} Trades → {n} performance rows upserted")
        wl_count = compute_whitelist(conn)
        logger.info(f"✅ Analysis completed: {total_rows} performance rows, {wl_count} whitelist entries")
    except Exception as e:
        logger.error(f"Analysis error: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────────────────────────────────────


async def schedule_hourly_analysis() -> None:
    """Runs analysis at XX:05:00 every hour."""
    while True:
        now = datetime.now(timezone.utc)
        next_run = now.replace(minute=5, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(hours=1)
        sleep_sec = (next_run - now).total_seconds()
        await asyncio.sleep(max(sleep_sec, 1))
        await run_analysis()
        await asyncio.sleep(1)


async def schedule_daily_post() -> None:
    """Posts cross-table at 07:00:00 UTC daily."""
    while True:
        now = datetime.now(timezone.utc)
        next_run = now.replace(hour=DAILY_POST_HOUR_UTC, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        sleep_sec = (next_run - now).total_seconds()
        await asyncio.sleep(max(sleep_sec, 1))
        await post_daily_cross_table()
        await asyncio.sleep(1)


async def main(initial_run: bool = False) -> None:
    logger.info("=== 📊 BOT REGIME ANALYZER STARTED ===")

    if initial_run:
        logger.info("--initial-run: complete run starting...")
        await run_analysis()
        logger.info("Initial-Run completed.")
        return

    # First run immediately
    await run_analysis()

    await asyncio.gather(
        schedule_hourly_analysis(),
        schedule_daily_post(),
    )


if __name__ == "__main__":
    initial = "--initial-run" in sys.argv
    try:
        asyncio.run(main(initial_run=initial))
    except KeyboardInterrupt:
        logger.info("Bot Regime Analyzer manually stopped (Ctrl+C).")
