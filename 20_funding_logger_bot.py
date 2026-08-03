import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import aiohttp

from core import config as _kcfg  # channel ids

# Import DB connection (for Telegram delivery)
from core.market_utils import load_coins, send_telegram

# 🛠️ CONFIGURATION
logging.basicConfig(level=logging.INFO, format='%(asctime)s - FUNDING_LOGGER - %(message)s')
logger = logging.getLogger(__name__)

COINS_FILE = "coins.json"
DATA_DIR = "funding_data"

SAVE_INTERVAL_SEC = 300  # poll every 5 minutes
MAX_AGE_SEC = 3 * 24 * 3600  # keep 3 days in seconds in RAM

TELEGRAM_CHANNEL_ID = _kcfg.CH_MARKET_DATA

# Overview Timer
LAST_OVERVIEW_TIME = 0
OVERVIEW_INTERVAL = 1800  # 30 minutes in seconds

# Top 20 Alert Timer
LAST_TOP20_ALERT = 0
TOP20_COOLDOWN = 900  # 15 minutes in seconds

TOP20_FUNDING_COINS = [
    "BTCUSDT",
    "ETHUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "TRXUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "BCHUSDT",
    "HYPEUSDT",
    "LINKUSDT",
    "ZECUSDT",
    "XLMUSDT",
    "LTCUSDT",
    "HBARUSDT",
    "AVAXUSDT",
    "SUIUSDT",
    "UNIUSDT",
    "TONUSDT",
    "DOTUSDT",
]

# Global working memory
FUNDING_HISTORY = []
# Per-coin index for O(log n) lookup in get_historical_rate:
# {symbol: [(ts, rate), ...]} sorted by ts ascending
FUNDING_BY_SYMBOL: dict[str, list[tuple[float, float]]] = {}
os.makedirs(DATA_DIR, exist_ok=True)


def rebuild_symbol_index():
    """Rebuilds FUNDING_BY_SYMBOL from FUNDING_HISTORY (sorted per symbol by ts)."""
    global FUNDING_BY_SYMBOL
    idx: dict[str, list[tuple[float, float]]] = {}
    for rec in FUNDING_HISTORY:
        idx.setdefault(rec["sym"], []).append((rec["ts"], rec["rate"]))
    for sym in idx:
        idx[sym].sort(key=lambda x: x[0])
    FUNDING_BY_SYMBOL = idx


def get_historical_rate(symbol, target_ts, tolerance=900):
    """
    Looks up the funding rate for a coin at the given timestamp (±tolerance).
    O(log n) via bisect instead of O(n) linear scan.
    """
    import bisect

    series = FUNDING_BY_SYMBOL.get(symbol)
    if not series:
        return None
    # Find insert position for target_ts
    timestamps = [r[0] for r in series]
    pos = bisect.bisect_left(timestamps, target_ts)
    # Check both candidates (pos-1 and pos) for tolerance
    best = None
    best_diff = tolerance + 1
    for cand_pos in (pos - 1, pos):
        if 0 <= cand_pos < len(series):
            diff = abs(series[cand_pos][0] - target_ts)
            if diff <= tolerance and diff < best_diff:
                best = series[cand_pos][1]
                best_diff = diff
    return best


# 📡 TELEGRAM HELPER


def calc_diff_bps(current, historical):
    """Difference between two funding rates in basis points (1 bps = 0.0001 = 0.01%).

    Funding rates are stored in decimal form (e.g. 0.00010 = 0.01%).
    The difference in basis points is the industry-standard metric for rate deltas.

    FIX (#83): Previously 0.0 as a fallback for None → the display then falsely
    showed "+0.0bps" (= "stable"), even though the info is "no data".
    Now: return None. The display decides for itself how to render it.
    """
    if historical is None:
        return None
    return (current - historical) * 10_000


def check_top20_positive_pct(current_rates_dict):
    """Calculates what percentage of top-20 coins are positive.

    FIX (#82): Previously 50.0 as a fallback for an empty dict → that faked a
    "neutral sentiment" when in reality there was no data. Now: None as an
    explicit "no data" — the caller must decide whether that is an alert-relevant
    problem or just applies at historical timestamps.
    """
    pos_count = 0
    total = 0
    for coin in TOP20_FUNDING_COINS:
        if coin in current_rates_dict:
            total += 1
            if current_rates_dict[coin] > 0:
                pos_count += 1

    if total == 0:
        return None
    return (pos_count / total) * 100


# P2.40: the 75% threshold fired in the normal state — the funding baseline is
# slightly positive (~+0.01%), so routinely ~75%+ of the top-20 coins are positive.
# The 75 trigger thus reported "EXTREME" almost permanently. 95/85 keeps the alert
# only for genuinely one-sided funding (operator decision Michi 2026-07-11).
FUNDING_EXTREME_THRESHOLDS = [95, 85]


def classify_funding_extreme(pos_pct):
    """Classifies the top-20 funding sentiment as (triggered, direction, pct_value).

    pos_pct = percentage of the top-20 coins with positive funding (0..100).
    Returns (False, "", 0) if neither the positive nor the negative side
    reaches an extreme threshold (FUNDING_EXTREME_THRESHOLDS).
    """
    neg_pct = 100.0 - pos_pct
    for threshold in FUNDING_EXTREME_THRESHOLDS:
        if pos_pct >= threshold:
            return True, "POSITIVE", pos_pct
        if neg_pct >= threshold:
            return True, "NEGATIVE", neg_pct
    return False, "", 0


# 🚨 SENTIMENT ENGINE (extreme alerts only)
async def evaluate_funding_sentiment(api_data, now_ts):
    global LAST_TOP20_ALERT

    current_rates = {}
    for item in api_data:
        current_rates[item["symbol"]] = float(item["lastFundingRate"])

    # FEATURE: TOP 20 EXTREME ALERT (permanently monitored on a 5-minute cadence)
    if now_ts - LAST_TOP20_ALERT >= TOP20_COOLDOWN:
        pos_pct = check_top20_positive_pct(current_rates)
        # FIX (#82): check_top20_positive_pct returns None when there is no data
        # (API outage, empty dict). Then simply don't trigger an alert — previously
        # 50.0 would always have faked "neutral" and run the alert logic below
        # with false numbers.
        if pos_pct is None:
            return

        triggered, direction, pct_value = classify_funding_extreme(pos_pct)

        if triggered:
            alert_msg = f"""<pre>
🚨 <b>FUNDING EXTREME ALERT</b> 🚨
<b>{pct_value:.0f}%</b> of TOP20 Coins are <b>{direction}</b>!

<b>Current Rates:</b>\n"""
            for coin in TOP20_FUNDING_COINS:
                if coin in current_rates:
                    val = current_rates[coin]
                    alert_msg += f"{coin:<10} {val:+.5f}\n"

            alert_msg += "</pre>"
            send_telegram(alert_msg, TELEGRAM_CHANNEL_ID)
            LAST_TOP20_ALERT = now_ts
            logger.info(f"Top 20 alert fired ({pct_value:.0f}% {direction})")


# 📊 FUNDING OVERVIEW LOOP (Exakt :00:10 und :30:10)
async def funding_overview_loop():
    """Background task: posts exactly at the half and full hour + 10 seconds."""
    logger.info("Funding overview started (sync at :00:10 and :30:10).")

    async with aiohttp.ClientSession() as session:
        while True:
            # 1. Timezone calculation
            now = datetime.now(timezone.utc)

            if now.minute < 30 or (now.minute == 30 and now.second < 10):
                target = now.replace(minute=30, second=10, microsecond=0)
            else:
                target = (now + timedelta(hours=1)).replace(minute=0, second=10, microsecond=0)

            sleep_sec = (target - now).total_seconds()
            await asyncio.sleep(sleep_sec)

            try:
                # 2. Fetch fresh data for the overview right now
                async with session.get("https://fapi.binance.com/fapi/v1/premiumIndex", timeout=15) as resp:
                    if resp.status != 200:
                        continue
                    api_data = await resp.json()

                now_ts = time.time()
                current_rates = {item["symbol"]: float(item["lastFundingRate"]) for item in api_data}

                # A) BTC & ETH Stats
                btc_rate = current_rates.get("BTCUSDT", 0.0)
                btc_1h = get_historical_rate("BTCUSDT", now_ts - 3600)
                btc_24h = get_historical_rate("BTCUSDT", now_ts - 86400)
                btc_1h_diff = calc_diff_bps(btc_rate, btc_1h)
                btc_24h_diff = calc_diff_bps(btc_rate, btc_24h)

                eth_rate = current_rates.get("ETHUSDT", 0.0)
                eth_1h = get_historical_rate("ETHUSDT", now_ts - 3600)
                eth_24h = get_historical_rate("ETHUSDT", now_ts - 86400)
                eth_1h_diff = calc_diff_bps(eth_rate, eth_1h)
                eth_24h_diff = calc_diff_bps(eth_rate, eth_24h)

                # FIX (#83): calc_diff_bps can return None when history is missing.
                # Convert to formatted strings here, None → "N/A".
                def _fmt_bps(v):
                    return f"{v:+.1f}bps" if v is not None else "N/A"

                btc_1h_str = _fmt_bps(btc_1h_diff)
                btc_24h_str = _fmt_bps(btc_24h_diff)
                eth_1h_str = _fmt_bps(eth_1h_diff)
                eth_24h_str = _fmt_bps(eth_24h_diff)

                # B) Top 20 Stats
                # FIX (#82): None fallback for empty data (API outage, etc.)
                top20_pos_now = check_top20_positive_pct(current_rates)
                hist_1h_rates = {}
                for coin in TOP20_FUNDING_COINS:
                    val = get_historical_rate(coin, now_ts - 3600)
                    if val is not None:
                        hist_1h_rates[coin] = val
                top20_pos_1h = check_top20_positive_pct(hist_1h_rates) if hist_1h_rates else top20_pos_now

                # Format for display — None as "N/A"
                top20_pos_now_str = f"{top20_pos_now:.1f}%" if top20_pos_now is not None else "N/A"
                top20_pos_1h_str = f"{top20_pos_1h:.1f}%" if top20_pos_1h is not None else "N/A"

                # C) Top 5 positive & negative
                valid_rates = {
                    k: v for k, v in current_rates.items() if get_historical_rate(k, now_ts - 300) is not None or v != 0
                }
                sorted_rates = sorted(valid_rates.items(), key=lambda x: x[1])

                top3_neg = sorted_rates[:5]
                top3_pos = sorted_rates[-5:]
                top3_pos.reverse()

                msg = f"""<pre>
📊 <b>FUNDING OVERVIEW</b> 📊

<b>BTC</b> {btc_rate:.5f} (1h:{btc_1h_str}, 24h:{btc_24h_str})
<b>ETH</b> {eth_rate:.5f} (1h:{eth_1h_str}, 24h:{eth_24h_str})

<b>TOP20 Coins:</b> {top20_pos_now_str} pos. (1h: {top20_pos_1h_str})

🔴 <b>TOP5 Coins Negative:</b>"""
                for sym, rate in top3_neg:
                    msg += f"\n{sym:<10} {rate:+.5f}"

                msg += "\n\n🟢 <b>TOP5 Coins Positive:</b>"
                for sym, rate in top3_pos:
                    msg += f"\n{sym:<10} {rate:+.5f}"

                msg += "</pre>"

                send_telegram(msg, TELEGRAM_CHANNEL_ID)
                logger.info("✅ On-time 30-minute funding overview sent.")

            except Exception as e:
                logger.error(f"Error in funding overview loop: {e}", exc_info=True)


# ⚙️ CORE SYSTEM


def load_existing_funding_data():
    global FUNDING_HISTORY
    FUNDING_HISTORY = []
    now = datetime.now(timezone.utc)

    for i in range(4):
        check_date = now - timedelta(days=i)
        date_str = check_date.strftime('%Y-%m-%d')
        filepath = os.path.join(DATA_DIR, f"funding_history_{date_str}.json")

        if os.path.exists(filepath):
            try:
                with open(filepath, encoding="utf-8") as f:
                    day_data = json.load(f)
                    FUNDING_HISTORY.extend(day_data)
            except Exception as e:
                logger.error(f"Error loading from {filepath}: {e}")

    cutoff = time.time() - MAX_AGE_SEC
    FUNDING_HISTORY = [t for t in FUNDING_HISTORY if t["ts"] >= cutoff]
    FUNDING_HISTORY.sort(key=lambda x: x["ts"])
    rebuild_symbol_index()
    logger.info(f"History loaded: {len(FUNDING_HISTORY)} records active in RAM.")


def sync_group_and_save(data_copy, only_today: bool = False):
    """Saves the funding history as JSON files (one per day).

    only_today=True writes only today's file (the default case in the monitor loop,
    every 5 min). This reduces disk I/O drastically — previously the
    entire 3-day history was re-serialised every time.
    only_today=False writes all daily files (for shutdown/init).
    """
    daily_groups = {}
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d') if only_today else None

    for d in data_copy:
        dt = datetime.fromtimestamp(d["ts"], tz=timezone.utc)
        date_str = dt.strftime('%Y-%m-%d')
        if only_today and date_str != today_str:
            continue
        if date_str not in daily_groups:
            daily_groups[date_str] = []
        daily_groups[date_str].append(d)

    for date_str, data in daily_groups.items():
        filepath = os.path.join(DATA_DIR, f"funding_history_{date_str}.json")
        tmppath = os.path.join(DATA_DIR, f"funding_history_{date_str}.tmp")
        try:
            with open(tmppath, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(',', ':'))
            os.replace(tmppath, filepath)
        except Exception as e:
            logger.error(f"Write error for file {filepath}: {e}")


async def funding_monitor_loop():
    global FUNDING_HISTORY
    coins = load_coins()
    if not coins:
        logger.error("No coins found. Stopping monitor.")
        return

    valid_coins = set(coins)
    logger.info(f"Funding monitor started. Tracking {len(coins)} coins.")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get("https://fapi.binance.com/fapi/v1/premiumIndex", timeout=15) as resp:
                    if resp.status != 200:
                        logger.warning(f"Binance API Error: HTTP {resp.status}")
                        await asyncio.sleep(60)
                        continue
                    api_data = await resp.json()

                now_ts = time.time()
                saved_count = 0

                for item in api_data:
                    symbol = item["symbol"]
                    if symbol in valid_coins:
                        rate = float(item["lastFundingRate"])
                        record = {"ts": now_ts, "sym": symbol, "rate": rate}
                        FUNDING_HISTORY.append(record)
                        saved_count += 1

                cutoff = now_ts - MAX_AGE_SEC
                FUNDING_HISTORY = [t for t in FUNDING_HISTORY if t["ts"] >= cutoff]

                # Rebuild index for O(log n) lookup
                rebuild_symbol_index()

                # Save only today's file (instead of re-serialising all 3 days)
                history_copy = list(FUNDING_HISTORY)
                await asyncio.to_thread(sync_group_and_save, history_copy, True)

                # 💥 SENTIMENT ANALYSIS STARTS HERE 💥
                await evaluate_funding_sentiment(api_data, now_ts)

            except asyncio.TimeoutError:
                logger.warning("Timeout on Binance API request.")
            except Exception as e:
                logger.error(f"Error in funding monitor: {e}", exc_info=True)

            await asyncio.sleep(SAVE_INTERVAL_SEC)


async def main():
    logger.info("=== 💰 FUNDING SENTIMENT TRACKER START ===")
    load_existing_funding_data()
    asyncio.create_task(funding_overview_loop())

    # Starts the regular 5-minute data collector & extreme-alert checker
    await funding_monitor_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Funding Logger Bot manually stopped (Ctrl+C). Saving data...")
        history_copy = list(FUNDING_HISTORY)
        sync_group_and_save(history_copy)
        logger.info("✅ Data successfully saved. Shutdown stopped.")
    except Exception as e:
        logger.error(f"Critical error in funding monitor: {e}", exc_info=True)
