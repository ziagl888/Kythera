# 39_ai_xsm1_bot.py — XSM1/XSR1 "Cross-Sectional Momentum/Reversal" (K2; LIVE seit T-2026-CU-9050-183).
"""
Weekly cross-sectional rotation by F-day return (F=84d, best cell): rank
the liquidity-filtered universe by raw formation return
`close[t]/close[t−F] − 1` and trade the TOP decile (strongest momentum coins).
Two COMPETING hypotheses on the same decile set:
  * **XSM1 (Momentum)** = LONG the top decile (continuation).
  * **XSR1 (Reversal)** = SHORT the top decile (mean reversion).

**BOTH legs LIVE since T-2026-CU-9050-183 (→ CH_ATS).** Study K2 is
`weak/inconsistent-spread, NOT deployable` (0 robust cells; the best val cell
F84|XSR1_SHORT tips OOS to −1.6 % = overfit). There is NO turnkey edge — the
bot runs both hypotheses live against each other (each with its own tag,
tracked independently) to see whether either side holds up. The post runs via
`signal_post.post_ai_signal_gated` (LIVE → Cornix post in CH_ATS; a pullback
into shadow = `(tag, dir)` in `_LIFECYCLE` back to SHADOW).

Signal contract == study `tools/xs_momentum_study.py` (cell F84|raw|absolute):
raw F-day return on closed 1d closes, decile rank, BTC excluded from the
tradable set, liquidity filter (bottom dollar-vol tercile removed),
MIN_COINS_PER_WEEK.

**IMPORTANT divergence (documented):** the study measures an H=28-day hold exit
(timeout, no TP/SL); the shadow monitor tracks first-touch TP/SL (shared
`hvn_sr_trade_geometry`). The shadow PnL is therefore NOT the study PnL, but
a direction-faithful first-touch validation — deliberate (the monitor has no
timeout exit). Market fill (entry1==entry2). The study's known look-ahead
(entry on the 1d OPEN instead of CLOSE) does not apply here: the bot fires
AFTER the weekly close.

Runs weekly (Monday 00:37 UTC). Watchdog: start_delay=263.
"""

import datetime
import logging
import time

import numpy as np

from core import config as _kcfg
from core.candles import read_candles
from core.database import get_db_connection
from core.market_utils import check_cooldown, load_coins, update_cooldown
from core.shadow_gate import LIVE, SHADOW, leg_status, shadow_posting_enabled
from core.signal_post import has_open_ai_signal, post_ai_signal_gated
from core.trade_utils import (
    N_PUBLISHED_TARGETS,
    ensure_min_tp_distance,
    get_hvn_and_sr_levels,
    hvn_sr_trade_geometry,
    thin_targets,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - XSM1_BOT - %(message)s")
logger = logging.getLogger(__name__)

XSM_TAG = "XSM1"  # Momentum → LONG the top decile
XSR_TAG = "XSR1"  # Reversal  → SHORT the top decile
TF = "1d"
F_DAYS = 84  # formation window (best cell)
DECILE_FRAC = 0.10  # top decile
LIQ_EXCLUDE_TERCILE = 1.0 / 3.0
MIN_COINS_PER_WEEK = 20
MIN_1D_ROWS = F_DAYS + 3  # F lookback + buffer
COOLDOWN_HOURS = 24 * 6  # one week lock (fire-once per rebalance/tag)
SHADOW_CONF = 0.5
SCAN_MINUTE = 37
MAX_TOP = 40  # cap for the decile size (shadow-load protection)
EXCLUDE = {"BTCUSDT"}  # BTC not in the tradable cross-sectional set (== study)


def select_top_decile(rows: list[tuple[str, float, float]]) -> list[str]:
    """Pure cross-sectional selection (DB-free testable). ``rows`` = (symbol,
    f_return, dollar_vol). Liquidity filter (bottom tercile removed) → top
    decile by F-return. Both hypotheses (XSM1/XSR1) trade THIS set."""
    if len(rows) < MIN_COINS_PER_WEEK:
        return []
    dvs = np.array([r[2] for r in rows], dtype=float)
    thr = float(np.nanquantile(dvs, LIQ_EXCLUDE_TERCILE))  # NaN-robust (review fix)
    liquid = [r for r in rows if r[2] >= thr]
    if len(liquid) < MIN_COINS_PER_WEEK:
        return []
    liquid.sort(key=lambda r: r[1])  # ascending by F-return
    ndec = max(1, round(len(liquid) * DECILE_FRAC))
    top = [r[0] for r in liquid[-ndec:]]  # highest F-return = top decile
    return top[:MAX_TOP]


def gather(conn, coins: list[str], now: datetime.datetime) -> tuple[list[tuple[str, float, float]], dict[str, float]]:
    """(rows, entries): rows = [(symbol, f_return, dollar_vol)]."""
    rows: list[tuple[str, float, float]] = []
    entries: dict[str, float] = {}
    for symbol in coins:
        if symbol in EXCLUDE:
            continue
        try:
            df = read_candles(
                conn, symbol, TF, limit=MIN_1D_ROWS + 8, include_forming=False, columns=("open_time", "close", "volume")
            )
            if df is None or len(df) < MIN_1D_ROWS:
                continue
            close = df["close"].to_numpy(dtype=float)
            base = close[-1 - F_DAYS]
            if base <= 0:
                continue
            f_return = close[-1] / base - 1.0
            vol = df["volume"].to_numpy(dtype=float)
            dollar_vol = float(np.mean((close * vol)[-F_DAYS:]))
            entry = float(close[-1])
            # dollar_vol finite (review fix, symmetry with the f_return guard) —
            # a NaN candle must not bring the coin into the cross-section.
            if entry <= 0 or not np.isfinite(f_return) or not np.isfinite(dollar_vol):
                continue
            rows.append((symbol, f_return, dollar_vol))
            entries[symbol] = entry
        except Exception as e:
            logger.error(f"gather {symbol}: {e}")
    return rows, entries


def emit(conn, symbol: str, tag: str, direction: str, entry: float) -> None:
    """A shadow leg under ``tag``/``direction`` with shared geometry."""
    if not shadow_posting_enabled() or leg_status(tag, direction) not in (LIVE, SHADOW):
        return
    if check_cooldown(conn, tag, symbol, direction, COOLDOWN_HOURS):
        return
    if has_open_ai_signal(conn, symbol, direction, tag):
        return
    is_long = direction == "LONG"
    supps, resis = get_hvn_and_sr_levels(conn, symbol, entry)
    _, sl, t_cands = hvn_sr_trade_geometry(entry, is_long, supps, resis)
    targets = ensure_min_tp_distance(
        thin_targets(t_cands[:20], entry, is_long, keep=N_PUBLISHED_TARGETS),
        entry,
        is_long,
        min_pct=0.05,
    )
    if not targets:
        return
    outcome = post_ai_signal_gated(
        conn,
        tag,
        direction,
        _kcfg.CH_ATS,  # formerly ATS channel (T-2026-CU-9050-183)
        symbol,
        SHADOW_CONF,
        entry,
        entry,
        sl,
        targets,
        source_desc=f"AI {tag} weekly cross-sectional rotation, top F-decile (K2)",
        n_show=3,
    )
    if outcome == "live":
        logger.info(
            f"📈 {tag} LIVE {direction} {symbol} | top decile @ {entry:g} (SL {sl:g}, {len(targets)} TP) → CH_ATS."
        )
    elif outcome == "shadow":
        logger.info(f"👻 {tag}-Shadow {direction} {symbol} | top decile @ {entry:g} (SL {sl:g}, {len(targets)} TP).")
    update_cooldown(conn, tag, symbol, direction)  # commits atomically


def run_scan() -> None:
    coins = load_coins("coins.json", usdt_only=True, uppercase=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    conn = get_db_connection()
    try:
        rows, entries = gather(conn, coins, now)
        top = select_top_decile(rows)
        logger.info(f"🔍 XSM1/XSR1 rebalance: {len(rows)} scored → top decile {len(top)} (LONG XSM1 + SHORT XSR1).")
        # Both competing hypotheses on the same decile set.
        for symbol in top:
            for tag, direction in ((XSM_TAG, "LONG"), (XSR_TAG, "SHORT")):
                try:
                    emit(conn, symbol, tag, direction, entries[symbol])
                except Exception as e:
                    logger.error(f"emit {symbol} {tag}: {e}")
                finally:
                    try:
                        conn.rollback()  # P2.32; a no-op after the cooldown commit
                    except Exception:
                        logger.error("Rollback failed (dead connection) — aborting.")
                        return
    finally:
        conn.close()
    logger.info("🏁 XSM1/XSR1 rebalance stopped.")


def main() -> None:
    logger.info("=== 🔄 AI XSM1/XSR1 BOT (Cross-Sectional Momentum/Reversal, K2) STARTED — LIVE → CH_ATS ===")
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
        if now.weekday() == 0 and now.hour == 0 and now.minute == SCAN_MINUTE:  # Monday 00:37 UTC
            run_scan()
            time.sleep(60)
        else:
            time.sleep(20)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manually stopped (Ctrl+C). Shutting down cleanly...")
