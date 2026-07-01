# 27_bot_regime_analyzer.py
"""
Bot-Regime-Analyzer — Phase 2 des Regime-Orchestrators.

Läuft stündlich zu XX:05:00 und:
  1. Berechnet für jeden Bot × Regime × Alt-Context × Direction die historische Performance
  2. Schreibt Ergebnisse in bot_regime_performance (UPSERT)
  3. Berechnet bot_regime_whitelist (zweistufige Logik)
  4. Postet täglich um 07:00 UTC ein Cross-Table-Post

Aufruf mit --initial-run für vollständigen Einmal-Durchlauf.

Watchdog: start_delay=167
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import warnings
from datetime import datetime, timedelta, timezone

# Pandas beschwert sich bei psycopg2-Connections mit einer UserWarning
# ("only supports SQLAlchemy connectable ..."). Der Code läuft trotzdem
# korrekt; andere Module im Projekt machen dasselbe Suppression.
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

# ─────────────────────────────────────────────────────────────────────────────
# TRADE-OUTCOME-KLASSIFIKATION
# ─────────────────────────────────────────────────────────────────────────────
# Wir klassifizieren jeden Trade als 'win', 'loss' oder 'neutral'. Neutrale
# Trades (Housekeeping-Closes, Delistings, Ausreißer) werden vollständig aus
# den Performance-Statistiken ausgeschlossen — sie sind weder Win noch Loss
# und würden WR + avg_pnl verzerren.
#
# Der Fix adressiert vier bekannte Bugs in 8_ai_trade_monitor.py:
#   1. LEGACY TARGET HIT (+2.5%) schreibt targets_hit=0 statt 1 → alte Wins
#      wurden als Losses gezählt (EPD1 zeigte dadurch ~0.28% WR statt ~58%).
#   2. DELISTED/CLEANUP-Closes haben targets_hit=0 → wurden als Losses gezählt
#      obwohl der Trade durch Symbol-Delisting erzwungen stopped wurde.
#   3. close_reason "SL Hit (SL: 0.007)" ist pro Trade unique (enthält SL-Wert)
#      → nicht gruppierbar, aber irrelevant wenn wir auf PnL schauen.
#   4. Ausreißer mit |pnl| > 100% deuten auf Daten-Bugs hin (z.B. negative
#      Close-Preise) und verzerren avg_loss/avg_win massiv.
#
# Die Logik ist PnL-basiert statt targets_hit-basiert:
OUTCOME_MIN_PNL_PCT: float = 0.1  # |pnl| <= 0.1% → neutral (Housekeeping)
OUTCOME_MAX_ABS_PNL_PCT: float = 100.0  # |pnl| > 100% → neutral (Daten-Bug)

BTC_REGIMES = ["TREND_UP", "TREND_DOWN", "CHOP", "HIGH_VOLA", "TRANSITION"]
ALT_CONTEXTS = ["ALT_STRONG", "ALT_NEUTRAL", "ALT_WEAK"]
DIRECTIONS = ["LONG", "SHORT"]

# Counter-trend Richtungen per BTC-Regime
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
# TRADE-DATEN LADEN
# ─────────────────────────────────────────────────────────────────────────────


def _classify_outcome(close_reason: str, pnl_pct: float) -> str:
    """Klassifiziert einen einzelnen Trade.

    Returns 'win', 'loss', oder 'neutral'. 'neutral' schließt den Trade aus
    der Performance-Statistik komplett aus.

    Siehe Konstanten OUTCOME_MIN_PNL_PCT und OUTCOME_MAX_ABS_PNL_PCT sowie
    die Bug-Beschreibung am Datei-Anfang.
    """
    reason = (close_reason or "").upper()
    # Housekeeping-Closes: weder Win noch Loss (extern verursacht,
    # nicht vom Bot-Signal). Umfasst Delisting, Orphan-Cleanup und
    # Regime-Wechsel-Forced-Closes.
    if "DELISTED" in reason or "CLEANUP" in reason or "ORPHAN" in reason or "REGIME_CHANGE" in reason:
        return "neutral"
    if pnl_pct is None or (isinstance(pnl_pct, float) and pd.isna(pnl_pct)):
        return "neutral"
    try:
        p = float(pnl_pct)
    except (TypeError, ValueError):
        return "neutral"
    # Ausreißer-Filter (wahrscheinlich Daten-Bug)
    if abs(p) > OUTCOME_MAX_ABS_PNL_PCT:
        return "neutral"
    # Neutrale Micro-Bewegungen (Housekeeping mit close ≈ entry)
    if abs(p) <= OUTCOME_MIN_PNL_PCT:
        return "neutral"
    return "win" if p > 0 else "loss"


def _apply_outcome_classification(df: pd.DataFrame) -> pd.DataFrame:
    """Wendet _classify_outcome auf einen DataFrame an und überschreibt is_win.

    Fügt die 'outcome'-Spalte hinzu und filtert neutrale Trades raus. Der
    zurückgegebene DataFrame enthält nur noch 'win' und 'loss'-Trades.
    """
    if df.empty:
        return df
    df = df.copy()
    # close_reason-Spalte könnte fehlen (klassische Trades) → leer setzen
    if "close_reason" not in df.columns:
        df["close_reason"] = ""
    df["close_reason"] = df["close_reason"].fillna("").astype(str)
    # Outcome berechnen
    df["outcome"] = df.apply(
        lambda r: _classify_outcome(r["close_reason"], r["pnl_pct"]),
        axis=1,
    )
    # is_win neu setzen (überschreibt den Wert aus der SQL-Query)
    df["is_win"] = (df["outcome"] == "win").astype(int)
    # Neutrale ausschließen
    n_before = len(df)
    df = df[df["outcome"].isin(["win", "loss"])].copy()
    n_after = len(df)
    if n_before > n_after:
        logger.info(
            f"Outcome-Klassifikation: {n_before - n_after}/{n_before} Trades als "
            f"neutral ausgeschlossen (Delisting/Housekeeping/Outlier)"
        )
    return df


def load_trades_with_regime(conn, window_days: int) -> pd.DataFrame:
    """
    Loads all closed trades (classic + AI) from the last window_days,
    annotated with the regime at trade open time.

    Returns DataFrame with columns:
        bot_name, direction, entry, close_price, is_win, pnl_pct,
        regime, alt_context, opened_at, close_reason, outcome

    Neutrale Trades (DELISTED, Outlier, Micro-PnL) sind BEREITS ausgefiltert
    im zurückgegebenen DataFrame — der Caller muss sich darum nicht kümmern.
    is_win wurde after PnL neu gesetzt, überschreibt damit den Wert aus SQL.
    """
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=window_days)

    # ── Klassische Trades ──
    # Hinweis: is_win wird nicht aus SQL gelesen — _apply_outcome_classification
    # überschreibt das ohnehin PnL-basiert. Das vermeidet auch den Crash wenn
    # status Werte wie "SL1", "WORKING" oder andere Non-Integer-Strings enthält
    # (von Legacy-Bots oder manuellen DB-Edits). close_reason gibt es bei
    # klassischen Trades nicht — wir nehmen stattdessen den rohen status-String
    # mit, damit ungewöhnliche entries in Logs sichtbar sind (z.B. für späteren
    # DELISTED-Check falls jemand das dort einbaut).
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
    # Die `status`-Spalte in closed_ai_signals enthält den close_reason
    # (z.B. "LEGACY TARGET HIT (+2.5%)", "DELISTED / CLEANUP", "SL Hit (SL: ...)")
    # — siehe 8_ai_trade_monitor.py Zeile 192-195.
    #
    # is_win wird hier nicht gelesen — _apply_outcome_classification überschreibt
    # es sowieso PnL-basiert. Das vermeidet auch Probleme falls targets_hit
    # irgendwann NULL enthält oder andere Überraschungen liefert.
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
        logger.warning(f"closed_ai_signals nicht ladbar: {e}")
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

    # Normalisiere Bot-Namen bevor aggregiert wird — sonst landen
    # MIS1-8H, MIS1-8h, MIS1-8h_pump als getrennte Bots in
    # bot_regime_performance und der Market-Tracker (der mit pretty_name
    # anfragt) findet sie nicht. Gleiches gilt für "Fast In And Out" vs
    # "FastInOut" etc. pretty_name ist idempotent, d.h. bereits
    # normalisierte Namen bleiben unverändert.
    df["bot_name"] = df["bot_name"].apply(pretty_name)

    # ──────────────────────────────────────────────────────────────────
    # KERN-FIX: Klassifikation von Win/Loss basierend auf tatsächlichem
    # PnL und close_reason, nicht auf dem fehlerhaften targets_hit-Feld.
    # Filtert neutrale Trades gleich raus.
    # ──────────────────────────────────────────────────────────────────
    df = _apply_outcome_classification(df)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# PERFORMANCE BERECHNEN & UPSERTEN
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
    conn.commit()
    return len(rows)


# ─────────────────────────────────────────────────────────────────────────────
# WHITELIST BERECHNEN
# ─────────────────────────────────────────────────────────────────────────────


def compute_whitelist(conn) -> int:
    """
    Computes bot_regime_whitelist from bot_regime_performance.
    Uses 2-stage logic:
      Stage 1: Counter-trend direction? → strict rule (≥60% AND ≥overall+10pp)
      Stage 2: Standard direction?      → wr ≥ overall suffices
      n < MIN_TRADES_FOR_DECISION        → whitelisted (insufficient data)

    Returns number of whitelist rows written.
    """
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    # Load all relevant performance data
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bot_name, regime, alt_context, direction,
                   n_trades, win_rate
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
        perf[key] = {"n": row[4], "wr": row[5] or 0.0}

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
                        )
                    )

    with conn.cursor() as cur:
        pg_extras.execute_values(
            cur,
            """
            INSERT INTO bot_regime_whitelist
                (bot_name, regime, alt_context, direction,
                 whitelisted, reason, computed_at)
            VALUES %s
            ON CONFLICT (bot_name, regime, alt_context, direction)
            DO UPDATE SET
                whitelisted = EXCLUDED.whitelisted,
                reason = EXCLUDED.reason,
                computed_at = EXCLUDED.computed_at
            """,
            whitelist_rows,
        )
    conn.commit()
    logger.info(f"✅ Whitelist computed: {len(whitelist_rows)} entries")
    return len(whitelist_rows)


# ─────────────────────────────────────────────────────────────────────────────
# TÄGLICHER CROSS-TABLE-POST
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

        lines.append(
            f"\nAktuelles Regime: {cur_regime}  |  Aktueller Alt-Context: {cur_alt}\n"
            f"↑ WR ≥ Overall+10pp → STARK\n"
            f"↓ WR ≤ Overall-10pp → SCHWACH\n"
            f"--- < 20 Trades in dieser Zelle"
        )

        msg = "<pre>" + "\n".join(lines) + "</pre>"
        send_telegram(msg, REGIME_STATUS_CHANNEL_ID)
        logger.info(f"✅ Daily cross-table post sent ({cur_regime})")

    except Exception as e:
        logger.error(f"Fehler beim täglichen Cross-Table-Post: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# ANALYSE JOB
# ─────────────────────────────────────────────────────────────────────────────


def cleanup_stale_performance_rows(conn) -> int:
    """Löscht bot_regime_performance-Rows mit nicht-normalisierten Bot-Namen.

    Hintergrund: Vor dem Naming-Fix hat der Analyzer Rows mit Rohnamen
    wie 'Fast In And Out', 'MIS1-8H' oder 'MIS1-168H_pump' geschrieben.
    Der Market-Tracker fragt aber mit pretty_name() an — also z.B.
    'FastInOut', 'MIS1-8h'. Die alten Rows sind für immer stumm.

    Dieser Cleanup macht einen one-time Reset: alle Rows deren bot_name
    sich von pretty_name(bot_name) unterscheidet werden gelöscht. Der
    afterfolgende run_analysis() baut sie mit normalisierten Namen neu auf.

    Nach dem ersten Clean-Run ist diese Funktion idempotent (löscht nix),
    weil alle neuen Rows bereits normalisiert geschrieben werden.

    Returns: Anzahl der gelöschten Rows.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT bot_name FROM bot_regime_performance")
            bot_names = [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"Cleanup-Scan fehlgeschlagen: {e}")
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
            f"{deleted} Rows von {len(stale)} bot_names bereinigt "
            f"(z.B. {stale[:3]})"
        )
        return deleted
    except Exception as e:
        logger.warning(f"Cleanup-Delete fehlgeschlagen: {e}")
        conn.rollback()
        return 0


async def run_analysis() -> None:
    """Main analysis job: loads trades, computes performance, updates whitelist."""
    conn = None
    try:
        conn = get_db_connection()
        # One-time Cleanup: entfernt stale rows mit nicht-normalisierten
        # Bot-Namen. Nach erstem Lauf idempotent (tut nichts mehr).
        cleanup_stale_performance_rows(conn)

        total_rows = 0
        for window in ANALYSIS_WINDOWS:
            df = load_trades_with_regime(conn, window)
            n = compute_and_upsert_performance(conn, df, window)
            total_rows += n
            logger.info(f"Window {window}d: {len(df)} Trades → {n} performance rows upserted")
        wl_count = compute_whitelist(conn)
        logger.info(f"✅ Analyse completed: {total_rows} Performance-Rows, {wl_count} Whitelist-entries")
    except Exception as e:
        logger.error(f"Analyse-Error: {e}", exc_info=True)
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
        logger.info("--initial-run: Vollständiger Durchlauf wird started...")
        await run_analysis()
        logger.info("Initial-Run completed.")
        return

    # Ersten Lauf sofort
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
        logger.info("Bot Regime Analyzer manuell stopped (Strg+C).")
