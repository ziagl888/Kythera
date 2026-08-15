# 47_ai_pcl1_bot.py — PCL1 "Pump-Continuation Long" (T-145 candidate, SHADOW-ONLY).
"""
LONG a coin that pumped >=75% within 24h, hold 24h, stop 25% below entry.
Hypothesis (T-2026-KYT-9050-145 / `staging_models/replay/pump_long_sl_study_t145.md`,
the only cell family that passed a pre-registered candidate rule in this study
series): extreme pumpers CONTINUE over the first 24h (net +10.3%/event,
t=2.26, 90% positive weeks, n=55) — but only with a stop wide enough to
survive the median −16.7% shakeout; a tight stop dies 75% of the time.

**Pure shadow-bot (no live post), LIS1 pattern (bot 36).** n=55 in ONE market
regime is a candidate, not an edge — the bot collects forward evidence via
monitored-but-never-posted trades (`post_shadow_ai_signal` → `ai_signals`
WITHOUT `telegram_outbox`; the AI monitor scores entry/SL/expiry to a realized
`closed_ai_signals` row). Fail-safe: if the register leg is not SHADOW, the
bot stays SILENT — never a live post.

Signal contract == study `tools/pump_long_sl_study.py` (candidate cell
75%/24h/SL25):
  * Trigger: implied price (`oi_value_usdt/open_interest` from ``oi_5m``, the
    study's price source) gained >= PUMP_PCT within 24h; both as-of points
    staleness-capped at 45 min (grid parity).
  * Entry = close of the last CLOSED 5m candle (study: open of the first 5m
    candle after the signal hour — one 5m tick apart, same price source as the
    study's path engine). No candle => void, never a fallback fill (P0.12).
  * SL = entry × (1 − SL_PCT/100) — the wick-aware monitor stop.
  * expiry_hours = 24 → the monitor closes the survivor as HORIZON_TIMEOUT at
    the horizon close: the study's hard time-exit ("the edge is a 24h
    phenomenon", verdict read 3).
  * ONE target at entry × (1 + TP_CAP_PCT/100). Divergence from the study
    (which had no TP), documented: the monitor needs >=1 target, so winners
    are capped at +50% — a conservative floor for the recorded edge.

Divergences from the study (intentional, documented):
  * Universe floor = CURRENT oi_value_usdt >= $3M at signal time instead of
    the study's full-sample median (which was itself a documented look-ahead);
    live has no future sample to take a median over.
  * Scan is hourly at minute :41 — the study grid was hourly on the hour; a
    pump crossing the threshold mid-hour fires up to ~41 min later here.

Operator profile (Michi, 2026-08-15): intended LIVE leverage 10x, margin
CROSS — recorded in ``ai_signals.lev`` as metadata. The shadow measurement is
unlevered either way. NOTE for promotion: `cap_leverage_to_sl` (safety 0.5)
allows at most 2x at a 25% stop; at 10x the stop lies far beyond the isolated
liquidation and a −25% move draws 2.5x the position margin from the cross
account. This tension is deliberately parked in the shadow phase and MUST be
resolved before any live post (see the PCL1 register comment in
core/shadow_gate.py).

Hosted by 45_shadow_scanner_runner.py (core/shadow_scanners.py, HOURLY :41) —
no own process, no fleet entry. Standalone `python 47_ai_pcl1_bot.py` works
for debugging.
"""

import datetime
import logging
import time

from core.candles import read_candles
from core.database import get_db_connection
from core.market_utils import check_cooldown, update_cooldown
from core.shadow_gate import SHADOW, leg_status, shadow_posting_enabled
from core.signal_post import has_open_ai_signal, post_shadow_ai_signal

logging.basicConfig(level=logging.INFO, format="%(asctime)s - PCL1_BOT - %(message)s")
logger = logging.getLogger(__name__)

MODEL_ID = "PCL1"
DIRECTION = "LONG"
PUMP_PCT = 75.0  # study candidate: >=75% gain over 24h
SL_PCT = 25.0  # study candidate: stop 25% below entry (band 25-30, lower edge)
TP_CAP_PCT = 50.0  # single far target — winner cap, see docstring divergence
EXPIRY_HOURS = 24  # study: the edge is a 24h phenomenon — hard time exit
MAX_STALE_MIN = 45  # as-of staleness of both oi_5m points (study grid parity)
MIN_OI_USDT = 3_000_000  # universe floor at signal time (divergence, see docstring)
COOLDOWN_HOURS = 24  # study dedupe: one event per symbol per 24h
SHADOW_CONF = 0.5  # rule-based, no model prob — neutral placeholder
SCAN_MINUTE = 41  # own minute (23/29/31/37 taken by bots 36-39)
LEV_PROFILE = "10x"  # operator-intended live profile (cross) — metadata only

# Both as-of points from oi_5m in ONE statement: the freshest point inside the
# staleness window, and the freshest point at/before now-24h inside its own
# 45-min back-window. Symbols missing either point simply drop out (void, no
# fill) — same contract as the study grid's staleness cap.
PUMP_SCAN_SQL = """
    WITH latest AS (
        SELECT DISTINCT ON (symbol) symbol, oi_value_usdt, open_interest
        FROM oi_5m
        WHERE ts >= NOW() - (%(stale)s * INTERVAL '1 minute')
        ORDER BY symbol, ts DESC
    ),
    past AS (
        SELECT DISTINCT ON (symbol) symbol, oi_value_usdt, open_interest
        FROM oi_5m
        WHERE ts <= NOW() - INTERVAL '24 hours'
          AND ts >= NOW() - INTERVAL '24 hours' - (%(stale)s * INTERVAL '1 minute')
        ORDER BY symbol, ts DESC
    )
    SELECT l.symbol,
           l.oi_value_usdt / l.open_interest AS px_now,
           p.oi_value_usdt / p.open_interest AS px_24h_ago,
           l.oi_value_usdt
    FROM latest l
    JOIN past p USING (symbol)
    WHERE l.open_interest > 0 AND p.open_interest > 0
      AND l.oi_value_usdt >= %(min_oi)s
"""


def is_pump(px_now: float, px_24h_ago: float, pump_pct: float = PUMP_PCT) -> bool:
    """Pure trigger predicate — testable without DB. Mirrors the study's
    ``dpx_24h >= PUMP_PCT`` on the same implied-price definition."""
    if px_24h_ago <= 0:
        return False
    return (px_now / px_24h_ago - 1) * 100 >= pump_pct


def trade_geometry(entry: float) -> tuple[float, list[float]]:
    """SL and target ladder of the candidate cell — pure, testable.

    SL 25% below entry (study band 25-30, lower edge = the higher-t cell),
    ONE target at +50% (winner cap divergence, see module docstring)."""
    sl = entry * (1 - SL_PCT / 100)
    targets = [entry * (1 + TP_CAP_PCT / 100)]
    return sl, targets


def scan_pumps(conn) -> list[tuple[str, float, float]]:
    """[(symbol, px_now, px_24h_ago)] of all coins clearing the pump trigger."""
    with conn.cursor() as cur:
        cur.execute(PUMP_SCAN_SQL, {"stale": MAX_STALE_MIN, "min_oi": MIN_OI_USDT})
        rows = cur.fetchall()
    return [(sym, float(now_), float(past)) for sym, now_, past, _oi in rows if is_pump(float(now_), float(past))]


def process_symbol(conn, symbol: str) -> None:
    # 1) Shadow gate first (cheap): the bot never posts live — if the leg is
    #    not SHADOW it is silently skipped (fail-safe to silence, LIS1 pattern).
    if not shadow_posting_enabled():
        return
    if leg_status(MODEL_ID, DIRECTION) != SHADOW:
        return

    # 2) Dedup: one event per symbol per 24h (study cooldown) + open trade.
    if check_cooldown(conn, MODEL_ID, symbol, DIRECTION, COOLDOWN_HOURS):
        return
    if has_open_ai_signal(conn, symbol, DIRECTION, MODEL_ID):
        return

    # 3) Entry from the last CLOSED 5m candle — same source as the study's
    #    path engine. Missing OR STALE feed => void, never a fallback fill:
    #    the trigger feed (oi_5m) and the entry feed (candles) are separate,
    #    so a dead candle ingest must not stamp an hours-old close as entry
    #    while the OI scan still fires (review T-146 M2).
    df = read_candles(conn, symbol, "5m", include_forming=False, columns=("open_time", "close"), limit=2)
    if df is None or not len(df):
        return
    last_open = df["open_time"].iloc[-1]
    last_open = last_open.to_pydatetime() if hasattr(last_open, "to_pydatetime") else last_open
    if last_open.tzinfo is None:
        last_open = last_open.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    if (now - last_open) > datetime.timedelta(minutes=MAX_STALE_MIN):
        logger.warning(f"{symbol}: last closed 5m candle is stale ({last_open}) — event voided.")
        return
    entry = float(df["close"].iloc[-1])
    if entry <= 0:
        return

    sl, targets = trade_geometry(entry)
    if post_shadow_ai_signal(
        conn,
        MODEL_ID,
        symbol,
        DIRECTION,
        SHADOW_CONF,
        entry,
        entry,
        sl,
        targets,
        n_show=1,
        expiry_hours=EXPIRY_HOURS,
        lev=LEV_PROFILE,
    ):
        logger.info(
            f"👻 PCL1-Shadow LONG {symbol} @ {entry:g} (SL {sl:g} = -{SL_PCT:.0f}%, "
            f"TP-cap +{TP_CAP_PCT:.0f}%, exit {EXPIRY_HOURS}h) — monitored, lev-profile {LEV_PROFILE} cross."
        )
    # Cooldown commits atomically with the shadow row (LIS1 pattern); after a
    # no-op post it's just the cooldown stamp.
    update_cooldown(conn, MODEL_ID, symbol, DIRECTION)


def run_scan() -> None:
    conn = get_db_connection()
    conn_dead = False
    try:
        try:
            pumps = scan_pumps(conn)
        except Exception as e:
            logger.error(f"Pump scan failed: {e}")
            return
        logger.info(f"🔍 PCL1-Scan: {len(pumps)} coins >= {PUMP_PCT:.0f}%/24h.")
        for symbol, _px_now, _px_past in pumps:
            try:
                process_symbol(conn, symbol)
            except Exception as e:
                logger.error(f"Error for {symbol}: {e}")
            finally:
                try:
                    conn.rollback()  # P2.32 pattern; no-op after cooldown commit
                except Exception:
                    logger.error("Rollback failed (dead connection) — scan abort.")
                    conn_dead = True
            if conn_dead:
                break
    finally:
        conn.close()
    logger.info("🏁 PCL1-Scan stopped.")


def main() -> None:
    logger.info("=== 🚀 AI PCL1 BOT (Pump-Continuation Long, T-145) STARTED — SHADOW-ONLY ===")
    # Standalone debug path only — in production runner 45 bootstraps this
    # table once for all hosted scanners (LIS1-main parity).
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
        if now.minute == SCAN_MINUTE:
            run_scan()
            time.sleep(60)
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manually stopped (Ctrl+C). Shutting down cleanly...")
