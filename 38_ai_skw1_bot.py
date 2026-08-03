# 38_ai_skw1_bot.py — SKW1 "cross-sectional skewness L/S" (study K7; live since T-2026-CU-9050-183).
"""
Weekly cross-sectional rotation by realised return skewness: rank the
liquidity-filtered universe by `mom_skew_7d` (7d skew of 15m log returns);
**SHORT the top decile** (high-positive skew = lottery coins), **LONG the
bottom decile** (negative skew). Study K7: decile monotonicity ρ=−0.88,
net spread ≈ +2.5%/week val+test stable.

**BOTH legs live since T-2026-CU-9050-183 (→ CH_ATS).** The study rates SKW1 as
VALIDATED feature/retrain input, NOT as turnkey edge (net only models
fees+funding, no slippage/impact/borrow; the LONG-low-skew leg is
tail-driven, WR<0.5 in each decile). Posting runs via
`signal_post.post_ai_signal_gated` (LIVE → Cornix post in CH_ATS; withdrawal to
shadow = `("SKW1", dir)` in `_LIFECYCLE` back to SHADOW, tag `SKW1`).

Signal contract == study `tools/skewness_study.py` + shared builder
`core/moment_features.py` (rule 7): `build_moment_panel`→`moment_features_asof`
yields `mom_skew_7d` as-of, closed 15m bars, native NaN. Reimplemented
(study-local): cross-sectional decile rank, weekly Monday 00:00 UTC grid,
liquidity filter (trailing 7d dollar volume, bottom tertile out),
MIN_COINS_PER_WEEK.

**IMPORTANT divergence (documented):** the study measures a 1-week hold exit
(timeout, no TP/SL). The shadow monitor tracks first-touch TP/SL instead
(shared `hvn_sr_trade_geometry`). Shadow PnL is thus NOT study PnL,
but a direction-faithful first-touch validation of the same signal —
deliberate, since the monitor knows no timeout exit (live money, no
monitor rework). Market fill (entry1==entry2).

Runs weekly (Monday 00:00 UTC, minute 31). Watchdog: start_delay=255.
"""

import datetime
import logging
import time

import numpy as np

from core import config as _kcfg
from core.candles import read_candles
from core.database import get_db_connection
from core.market_utils import check_cooldown, load_coins, update_cooldown
from core.moment_features import build_moment_panel, moment_features_asof
from core.shadow_gate import LIVE, SHADOW, leg_status, shadow_posting_enabled
from core.signal_post import has_open_ai_signal, post_ai_signal_gated
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels, hvn_sr_trade_geometry

logging.basicConfig(level=logging.INFO, format="%(asctime)s - SKW1_BOT - %(message)s")
logger = logging.getLogger(__name__)

MODEL_ID = "SKW1"
TF = "15m"
SKEW_FEATURE = "mom_skew_7d"
N_DECILES = 10
LIQ_EXCLUDE_TERCILE = 1.0 / 3.0  # bottom dollar-vol tercile out (== study)
MIN_COINS_PER_WEEK = 20  # cross-section too thin → week skipped
LIQ_BARS = 7 * 24 * 4  # 7d in 15m bars for the trailing dollar volume
MIN_15M_ROWS = 7 * 24 * 4 + 8  # full 7d skew window (672) + buffer
COOLDOWN_HOURS = 24 * 6  # one week lock (fire-once per rebalance/direction)
SHADOW_CONF = 0.5  # rule-based, no model prob
SCAN_MINUTE = 31
MAX_PER_SIDE = 40  # runaway protection


def select_deciles(rows: list[tuple[str, float, float]]) -> tuple[list[str], list[str]]:
    """Pure cross-sectional selection (testable without DB). ``rows`` = (symbol, skew,
    dollar_vol). Liquidity filter (bottom tercile out) → decile rank by skew.
    Return (longs, shorts): LONG = lowest skew decile, SHORT = highest."""
    if len(rows) < MIN_COINS_PER_WEEK:
        return [], []
    dvs = np.array([r[2] for r in rows], dtype=float)
    thr = float(np.nanquantile(dvs, LIQ_EXCLUDE_TERCILE))  # NaN-robust (review fix)
    liquid = [r for r in rows if r[2] >= thr]
    if len(liquid) < MIN_COINS_PER_WEEK:
        return [], []
    liquid.sort(key=lambda r: r[1])  # ascending by skew
    ndec = max(1, round(len(liquid) / N_DECILES))
    longs = [r[0] for r in liquid[:ndec]]  # lowest skew → LONG
    shorts = [r[0] for r in liquid[-ndec:]]  # highest skew → SHORT
    return longs[:MAX_PER_SIDE], shorts[:MAX_PER_SIDE]


def gather(conn, coins: list[str], now: datetime.datetime) -> tuple[list[tuple[str, float, float]], dict[str, float]]:
    """(rows, entries): rows = [(symbol, skew, dollar_vol)], entries = {symbol:
    entry_price}. Reads the 15m candles once per coin, builds the moment panel
    (shared builder) and reads the 7d skew as-of ``now``."""
    rows: list[tuple[str, float, float]] = []
    entries: dict[str, float] = {}
    for symbol in coins:
        try:
            # limit = only the most recent bars (the 7d skew window + buffer) — a
            # full 15m read over the entire history would be a weekly CPU spike
            # with no benefit (min_periods=672 only uses the edge). Review fix.
            df = read_candles(
                conn,
                symbol,
                TF,
                limit=MIN_15M_ROWS + 16,
                include_forming=False,
                columns=("open_time", "close", "volume"),
            )
            if df is None or len(df) < MIN_15M_ROWS:
                continue
            panel = build_moment_panel(df, tf=TF)
            skew = moment_features_asof(panel, now, tf=TF).get(SKEW_FEATURE)
            if skew is None:
                continue
            close = df["close"].to_numpy(dtype=float)
            vol = df["volume"].to_numpy(dtype=float)
            dollar_vol = float(np.mean((close * vol)[-LIQ_BARS:]))
            entry = float(close[-1])
            # dollar_vol MUST be finite: a NaN would otherwise be rendered harmless
            # by np.nanquantile in the selector, but a coin with a NaN candle should
            # not enter the cross-section in the first place (review fix, symmetry with the skew-None guard).
            if entry <= 0 or not np.isfinite(dollar_vol):
                continue
            rows.append((symbol, float(skew), dollar_vol))
            entries[symbol] = entry
        except Exception as e:
            logger.error(f"gather {symbol}: {e}")
    return rows, entries


def emit(conn, symbol: str, direction: str, entry: float) -> None:
    """A shadow leg (LONG/SHORT) with shared geometry. Fail-safe: never live."""
    if not shadow_posting_enabled() or leg_status(MODEL_ID, direction) not in (LIVE, SHADOW):
        return
    if check_cooldown(conn, MODEL_ID, symbol, direction, COOLDOWN_HOURS):
        return
    if has_open_ai_signal(conn, symbol, direction, MODEL_ID):
        return
    is_long = direction == "LONG"
    supps, resis = get_hvn_and_sr_levels(conn, symbol, entry)
    _, sl, t_cands = hvn_sr_trade_geometry(entry, is_long, supps, resis)
    targets = ensure_min_tp_distance(t_cands[:20], entry, is_long, min_pct=0.05)
    if not targets:
        return
    outcome = post_ai_signal_gated(
        conn,
        MODEL_ID,
        direction,
        _kcfg.CH_ATS,  # formerly ATS channel (T-2026-CU-9050-183)
        symbol,
        SHADOW_CONF,
        entry,
        entry,
        sl,
        targets,
        source_desc="AI SKW1 weekly cross-sectional skew rotation (K7)",
        n_show=3,
    )
    if outcome == "live":
        logger.info(
            f"📈 SKW1 LIVE {direction} {symbol} | Skew decile @ {entry:g} (SL {sl:g}, {len(targets)} TP) → CH_ATS."
        )
    elif outcome == "shadow":
        logger.info(f"👻 SKW1-Shadow {direction} {symbol} | Skew decile @ {entry:g} (SL {sl:g}, {len(targets)} TP).")
    update_cooldown(conn, MODEL_ID, symbol, direction)  # commits atomically


def run_scan() -> None:
    coins = load_coins("coins.json", usdt_only=True, uppercase=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    conn = get_db_connection()
    try:
        rows, entries = gather(conn, coins, now)
        longs, shorts = select_deciles(rows)
        logger.info(f"🔍 SKW1 rebalance: {len(rows)} eligible → {len(longs)} LONG / {len(shorts)} SHORT.")
        for direction, syms in (("LONG", longs), ("SHORT", shorts)):
            for symbol in syms:
                try:
                    emit(conn, symbol, direction, entries[symbol])
                except Exception as e:
                    logger.error(f"emit {symbol} {direction}: {e}")
                finally:
                    try:
                        conn.rollback()  # P2.32; a no-op after the cooldown commit
                    except Exception:
                        logger.error("Rollback failed (dead connection) — aborting.")
                        return
    finally:
        conn.close()
    logger.info("🏁 SKW1 rebalance stopped.")


def main() -> None:
    logger.info("=== 🎲 AI SKW1 BOT (Cross-Sectional Skewness L/S, K7) STARTED — LIVE L/S → CH_ATS ===")
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_cooldowns (
                module VARCHAR(50), coin VARCHAR(20), direction VARCHAR(10),
                last_posted_at TIMESTAMP WITH TIME ZONE,
                PRIMARY KEY (module, coin, direction)
            );
        """)
    conn.commit()
    conn.close()

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        if now.weekday() == 0 and now.hour == 0 and now.minute == SCAN_MINUTE:  # Monday 00:31 UTC
            run_scan()
            time.sleep(60)
        else:
            time.sleep(20)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manually stopped (Ctrl+C). Shutting down cleanly...")
