# 36_ai_lis1_bot.py — LIS1 "Post-Listing-Drift Fade" (Study K5, SHADOW-ONLY).
"""
Fade-SHORT of a newly listed coin on day 3 after Binance onboardDate.
Hypothesis (Report K5 / listing_drift_study): new Perp listings drift
market-neutral downwards (median 7d −8% → 90d −34%, β never flips the sign);
the tradeable part is a FRAGILE fade-SHORT, material only in the day-3 cell
(day 7 flips negative, day 14 ≈ 0; high win rate ~0.70 but deep left-tail p5 −27%).

**Pure shadow-bot (no live post).** There is NO model and NO
deployable edge — the bot validates the signal live via monitored but never
posted trades (`post_shadow_ai_signal` → `ai_signals` WITHOUT `telegram_outbox`;
the AI monitor tracks entry/TP/SL until realised close). The
LONG blacklist result (age < 180d ⇒ no LONG) is a gating/risk
decision and is deliberately NOT implemented here (operator gate, Michi).

Signal contract == study `tools/listing_drift_study.py::fade_events` (cell
d3|l0.0 = market-fill, n=152, ø +1.07 %/trade incl. funding, win rate 0.70):
  * Trigger = pure age event: coin reaches day FADE_DAY after onboardDate.
    No drift/return threshold (mandatory, data guards only).
  * onboardDate from GET /fapi/v1/exchangeInfo (onboardDate ms, immutable),
    cache `staging_models/listing_onboard_dates.json`, fallback first 1h candle.
  * Geometry = shared `hvn_sr_trade_geometry` (== study): SHORT-SL from
    resistances above entry*1.05*1.01, targets from supports below entry*0.99,
    `ensure_min_tp_distance(min_pct=0.05)`, 3 published TPs.

Divergence from study (intentional, documented): the live fill is the current
close (market) instead of the exact anchor candle — in continuous operation ≤1h after the
day-3 anchor (hourly scan + fire-once dedup). The study load floor of
120 1h rows (5d full history) does NOT apply to a live day-3 fill (the coin
then has only ~72h) — here counts the geometry floor (≥48 1h rows).

Runs hourly (minute 23). Watchdog: start_delay=239.
"""

import datetime
import json
import logging
import time

from core import config as _kcfg
from core.candles import read_candles
from core.database import get_db_connection
from core.market_utils import check_cooldown, load_coins, update_cooldown
from core.shadow_gate import SHADOW, leg_status, shadow_posting_enabled
from core.signal_post import has_open_ai_signal, post_shadow_ai_signal
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels, hvn_sr_trade_geometry

logging.basicConfig(level=logging.INFO, format="%(asctime)s - LIS1_BOT - %(message)s")
logger = logging.getLogger(__name__)

MODEL_ID = "LIS1"
DIRECTION = "SHORT"  # fade-SHORT only; LONG blacklist gate is Michi's responsibility
FADE_DAY = 3  # only day 3 is materially positive (d7 flips, d14 ≈ 0)
FADE_GRACE_DAYS = 1  # only coins in window [3d, 4d) fire → truly new coins
MIN_1H_ROWS = 48  # geometry floor (≥2d 1h), day-3-suitable; NOT the 120-study load floor
COOLDOWN_HOURS = 24 * 14  # day-3 fires ONCE per coin; long cooldown = belt-and-braces
SHADOW_CONF = 0.5  # rule-based, no model prob — neutral placeholder
SCAN_MINUTE = 23  # own minute (0/2/5/10/13/15/19/23 … occupied/free)

EXCHANGE_INFO_URL = f"{_kcfg.BASE_URL}/fapi/v1/exchangeInfo"
ONBOARD_CACHE = "staging_models/listing_onboard_dates.json"


def fetch_onboard_map() -> dict[str, int]:
    """{SYMBOL: onboard_ms} from exchangeInfo (fresh), else from cache.

    onboardDate is immutable per symbol → a stale cache harms only
    truly NEW, not-yet-cached coins (they fall back to the
    first-candle proxy in :func:`onboard_ts`). Network error ⇒ cache ⇒ {}."""
    try:
        import requests

        r = requests.get(EXCHANGE_INFO_URL, timeout=25)
        r.raise_for_status()
        data = r.json()
        out: dict[str, int] = {}
        for s in data.get("symbols", []):
            ms = s.get("onboardDate")
            if ms and s.get("quoteAsset") == "USDT" and s.get("contractType") == "PERPETUAL":
                out[s["symbol"].upper()] = int(ms)
        if out:
            return out
    except Exception as e:
        logger.warning(f"exchangeInfo not available ({e}) — using onboard cache.")
    try:
        with open(ONBOARD_CACHE, encoding="utf-8") as fh:
            blob = json.load(fh)
        return {k.upper(): int(v["onboard_ms"]) for k, v in blob.get("onboard", {}).items() if v.get("onboard_ms")}
    except Exception as e:
        logger.warning(f"Onboard cache not readable ({e}) — first-candle proxy only.")
        return {}


def in_fade_window(onboard: datetime.datetime, now: datetime.datetime) -> bool:
    """True only in age window [FADE_DAY, FADE_DAY+GRACE) — the day-3 fade
    fires precisely for TRULY new coins (not for long-listed ones). Pure
    function so the trigger boundary is testable without DB."""
    age_days = (now - onboard).total_seconds() / 86400.0
    return FADE_DAY <= age_days < FADE_DAY + FADE_GRACE_DAYS


def _onboard_from_ms(ms: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)


def onboard_ts(conn, symbol: str, onboard_map: dict[str, int], df) -> datetime.datetime | None:
    """onboardDate as tz-aware UTC. Primary exchangeInfo/cache, fallback the
    first closed 1h candle (== study proxy `first_candle_proxy`)."""
    ms = onboard_map.get(symbol.upper())
    if ms:
        return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
    if df is not None and len(df):
        first = df["open_time"].iloc[0]
        ts = first.to_pydatetime() if hasattr(first, "to_pydatetime") else first
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return ts
    return None


def process_coin(conn, symbol: str, onboard_map: dict[str, int], now: datetime.datetime) -> None:
    # 1) Shadow gate first (cheap, no DB/network): the bot never posts live —
    #    if the leg is not SHADOW, it is silently skipped (fail-safe to
    #    silence, never to a live post — the rule has no edge).
    if not shadow_posting_enabled():
        return
    if leg_status(MODEL_ID, DIRECTION) != SHADOW:
        return

    # 2) Age window [FADE_DAY, FADE_DAY+GRACE) — filters ~all coins immediately
    #    without candle read. onboard from map (proxy only if necessary).
    ms = onboard_map.get(symbol.upper())
    if ms is not None and not in_fade_window(_onboard_from_ms(ms), now):
        return

    # 3) Dedup: ONCE per coin (open shadow trade OR cooldown active).
    if check_cooldown(conn, MODEL_ID, symbol, DIRECTION, COOLDOWN_HOURS):
        return
    if has_open_ai_signal(conn, symbol, DIRECTION, MODEL_ID):
        return

    # 4) Candles + age check (incl. proxy for coins without onboardDate).
    df = read_candles(conn, symbol, "1h", include_forming=False, columns=("open_time", "close"))
    if df is None or len(df) < MIN_1H_ROWS:
        return
    if ms is None:  # no exchangeInfo entry → check proxy
        onboard = onboard_ts(conn, symbol, onboard_map, df)
        if onboard is None or not in_fade_window(onboard, now):
            return

    entry1 = float(df["close"].iloc[-1])
    if entry1 <= 0:
        return

    # 5) Geometry == study: shared hvn_sr_trade_geometry (SHORT). The fill
    #    is market (entry1==entry2 → cell l0.0); entry2 of geometry (×1.05)
    #    shapes only the SL and is discarded for posting.
    supps, resis = get_hvn_and_sr_levels(conn, symbol, entry1)
    _, sl, t_cands = hvn_sr_trade_geometry(entry1, False, supps, resis)
    targets = ensure_min_tp_distance(t_cands[:20], entry1, False, min_pct=0.05)
    if not targets:
        return

    if post_shadow_ai_signal(conn, MODEL_ID, symbol, DIRECTION, SHADOW_CONF, entry1, entry1, sl, targets, n_show=3):
        logger.info(
            f"👻 LIS1-Shadow SHORT {symbol} | day-3-fade @ {entry1:g} (SL {sl:g}, {len(targets)} TP) — monitored."
        )
    # Cooldown commits atomically (shadow row + cooldown) — mirror of
    # fire-once dedup; after a no-op post it's just the cooldown stamp.
    update_cooldown(conn, MODEL_ID, symbol, DIRECTION)


def run_scan() -> None:
    coins = load_coins("coins.json", usdt_only=True, uppercase=True)
    onboard_map = fetch_onboard_map()
    now = datetime.datetime.now(datetime.timezone.utc)
    # Pre-filter via map: only coins in age window OR (rarely) without
    # onboardDate need to go through the expensive path. Saves 527 candle reads.
    candidates = [
        c
        for c in coins
        if (c.upper() not in onboard_map) or in_fade_window(_onboard_from_ms(onboard_map[c.upper()]), now)
    ]
    logger.info(
        f"🔍 LIS1-Scan: {len(coins)} coins, {len(onboard_map)} onboard data, {len(candidates)} day-3 candidates."
    )

    conn = get_db_connection()
    conn_dead = False
    try:
        for symbol in candidates:
            try:
                process_coin(conn, symbol, onboard_map, now)
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
    logger.info("🏁 LIS1-Scan stopped.")


def main() -> None:
    logger.info("=== 🆕 AI LIS1 BOT (Post-Listing-Drift Fade, K5) STARTED — SHADOW-ONLY ===")
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
