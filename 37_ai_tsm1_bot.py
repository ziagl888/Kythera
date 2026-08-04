# 37_ai_tsm1_bot.py — TSM1 "Time-Series-Momentum" (Study K1; SHORT LIVE since T-2026-CU-9050-183).
"""
Time-series momentum breakout on the 4h chart: the rate-of-change over L=12
4h candles crosses from OUTSIDE to INSIDE the ±k·σ band (σ = 90d rolling
std of ROC, k=0.5) → momentum signal. Study best cell `4h|L12|k0.5`.

**SHORT ONLY; the SHORT leg has been LIVE since T-2026-CU-9050-183 (→ CH_FIF1, replaces
the parked FIF1).** The study (tsmom_study) is overall `no-op/paper-
falsified` (best OOS cell test −0.05%/trade); the ONLY non-falsified
part is the SHORT direction (positive in EVERY cell, while LONG is deeply negative
— the net loss comes entirely from the LONG leg). The LONG leg is therefore
not emitted at all. Posting runs via `signal_post.post_ai_signal_gated`
(LIVE → Cornix post in CH_FIF1; a withdrawal to shadow = set `("TSM1","SHORT")` in
`_LIFECYCLE` back to SHADOW, tag `TSM1`).

Signal contract == study `tools/tsmom_study.py::signal_events` (cell 4h|L12|k0.5):
  * ROC_L[t] = close[t]/close[t−12] − 1 on native 4h closes.
  * σ = ROC.rolling(90d = 540 4h bars, min_periods=540).std(); band = 0.5·σ.
  * SHORT crossing = ROC[t] ≤ −band[t] AND ROC[t−1] > −band[t−1] (breakout
    DOWN through the −band: previously above, now below).
  * Geometry = shared `hvn_sr_trade_geometry` (SHORT), market fill at 4h close
    (== study entry `a_close`, NOT the ±5% entry2), `ensure_min_tp_distance
    (min_pct=0.05)`, 3 published TPs.

Fires once per coin per open trade (`has_open_ai_signal` + cooldown) — the
re-entry only after close mirrors the study rule "1 open position per
coin/direction, re-entry after exit". Runs every 4h (minute 29 of hours
0/4/8/12/16/20 UTC, shortly after the 4h close). Watchdog: start_delay=247.
"""

import datetime
import logging
import time

import numpy as np
import pandas as pd

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - TSM1_BOT - %(message)s")
logger = logging.getLogger(__name__)

MODEL_ID = "TSM1"
DIRECTION = "SHORT"  # only the non-falsified direction; LONG stays unemitted
TF = "4h"
ROC_L = 12  # lookback in 4h bars (best cell)
K_SIGMA = 0.5  # band width in σ (best cell)
SIGMA_BARS = 540  # 90d rolling std of the ROC at 4h (90*24/4)
MIN_4H_ROWS = SIGMA_BARS + ROC_L + 2  # full σ window + lookback + crossing pair
COOLDOWN_HOURS = 4  # one 4h candle lock; has_open additionally blocks until the close
SHADOW_CONF = 0.5  # rule-based, not a model probability
SCAN_MINUTE = 29  # shortly after the 4h close, only on the 4h hours (see main)
MAX_CANDIDATES = 40  # safety cap per scan (runaway protection)


def short_crossing(close: np.ndarray) -> bool:
    """True if the LAST closed 4h candle is a SHORT crossing
    (ROC from ABOVE −band to BELOW). Pure function → DB-free testable."""
    if len(close) < MIN_4H_ROWS:
        return False
    roc = np.full(len(close), np.nan)
    roc[ROC_L:] = close[ROC_L:] / close[:-ROC_L] - 1.0
    sigma = pd.Series(roc).rolling(window=SIGMA_BARS, min_periods=SIGMA_BARS).std().to_numpy()
    band = K_SIGMA * sigma
    r, rp, b, bp = roc[-1], roc[-2], band[-1], band[-2]
    if not all(np.isfinite(x) for x in (r, rp, b, bp)):
        return False
    return bool(r <= -b and rp > -bp)  # Breakout down through −band (== study signal_events)


def process_coin(conn, symbol: str) -> bool:
    """True if a shadow trade was written (for the candidate cap)."""
    if not shadow_posting_enabled():
        return False
    if leg_status(MODEL_ID, DIRECTION) not in (LIVE, SHADOW):  # SILENT/retired → emit nothing
        return False
    if check_cooldown(conn, MODEL_ID, symbol, DIRECTION, COOLDOWN_HOURS):
        return False
    if has_open_ai_signal(conn, symbol, DIRECTION, MODEL_ID):
        return False

    df = read_candles(conn, symbol, TF, limit=MIN_4H_ROWS + 8, include_forming=False, columns=("open_time", "close"))
    if df is None or len(df) < MIN_4H_ROWS:
        return False
    close = df["close"].to_numpy(dtype=float)
    if not short_crossing(close):
        return False

    entry1 = float(close[-1])
    if entry1 <= 0:
        return False
    supps, resis = get_hvn_and_sr_levels(conn, symbol, entry1)
    _, sl, t_cands = hvn_sr_trade_geometry(entry1, False, supps, resis)
    targets = ensure_min_tp_distance(
        thin_targets(t_cands[:20], entry1, False, keep=N_PUBLISHED_TARGETS),
        entry1,
        False,
        min_pct=0.05,
    )
    if not targets:
        return False

    outcome = post_ai_signal_gated(
        conn,
        MODEL_ID,
        DIRECTION,
        _kcfg.CH_FIF1,  # inherited target channel from FIF1 (T-2026-CU-9050-183)
        symbol,
        SHADOW_CONF,
        entry1,
        entry1,
        sl,
        targets,
        source_desc="AI TSM1 4h Time-Series-Momentum (K1)",
        n_show=3,
    )
    if outcome == "live":
        logger.info(
            f"📈 TSM1 LIVE SHORT {symbol} | 4h ROC crossing @ {entry1:g} (SL {sl:g}, {len(targets)} TP) → CH_FIF1."
        )
    elif outcome == "shadow":
        logger.info(
            f"👻 TSM1 shadow SHORT {symbol} | 4h ROC crossing @ {entry1:g} (SL {sl:g}, {len(targets)} TP) — monitored."
        )
    update_cooldown(conn, MODEL_ID, symbol, DIRECTION)  # commits atomically
    return outcome is not None


def run_scan() -> None:
    coins = load_coins("coins.json", usdt_only=True, uppercase=True)
    logger.info(f"🔍 TSM1 scan (4h close): {len(coins)} coins.")
    conn = get_db_connection()
    conn_dead = False
    fired = 0
    try:
        for symbol in coins:
            try:
                if process_coin(conn, symbol):
                    fired += 1
            except Exception as e:
                logger.error(f"Error for {symbol}: {e}")
            finally:
                try:
                    conn.rollback()  # P2.32; no-op after cooldown commit
                except Exception:
                    logger.error("Rollback failed (dead connection) — scan abort.")
                    conn_dead = True
            if conn_dead or fired >= MAX_CANDIDATES:
                break
    finally:
        conn.close()
    logger.info(f"🏁 TSM1 scan stopped ({fired} shadow signals).")


def main() -> None:
    logger.info("=== 📈 AI TSM1 BOT (Time-Series-Momentum, K1) STARTED — LIVE SHORT → CH_FIF1 ===")
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
        if now.hour % 4 == 0 and now.minute == SCAN_MINUTE:  # shortly after every 4h close
            run_scan()
            time.sleep(60)
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manually stopped (Ctrl+C). Shutting down cleanly...")
