import datetime
import logging
import time
import warnings

import pytz

from core.candles import read_candles
from core.database import get_db_connection

warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - MONITOR - %(message)s')
logger = logging.getLogger(__name__)


# DATABASE HELPER FUNCTIONS
def create_closed_trades_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS closed_trades_master (
                id SERIAL PRIMARY KEY,
                strategy TEXT, time TIMESTAMP WITHOUT TIME ZONE, coin TEXT,
                direction TEXT, lev TEXT, entry REAL, target1 REAL, target2 REAL,
                target3 REAL, target4 REAL, sl REAL, close_price REAL,
                posted TIMESTAMP WITHOUT TIME ZONE, status TEXT
            )
        """)
    conn.commit()


def close_trade(conn, trade, close_price, end_status):
    """Removes from active and saves to closed (silent — no Telegram)."""
    # FIX: previously naive `datetime.now()` (server local time). On DE/AT servers
    # this writes timestamps shifted up to 2h forward into `posted`,
    # while other scripts compare `datetime.now() - timedelta` in UTC
    # (→ freshly closed trades falsely treated as "too old").
    # now consistently UTC.
    now = datetime.datetime.now(datetime.timezone.utc)
    with conn.cursor() as cur:
        # FIX P2.8: DELETE ... RETURNING first — insert to closed table
        # runs ONLY if WE actually removed the row. otherwise two
        # iterations/processes write same trade twice to closed_trades_master.
        cur.execute("DELETE FROM active_trades_master WHERE id = %s RETURNING id", (trade['id'],))
        if cur.fetchone() is None:
            conn.commit()
            logger.warning(f"⚠️ trade {trade.get('id')} ({trade.get('coin')}) already closed — double-close prevented.")
            return
        cur.execute(
            """
            INSERT INTO closed_trades_master (
                strategy, time, coin, direction, lev, entry, target1, target2, target3, target4, sl, close_price, posted, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
            (
                trade['strategy'],
                trade['time'],
                trade['coin'],
                trade['direction'],
                trade['lev'],
                trade['entry'],
                trade['target1'],
                trade['target2'],
                trade['target3'],
                trade['target4'],
                trade['sl'],
                close_price,
                now,
                end_status,
            ),
        )
    conn.commit()

    # only local logging for you, no Telegram spam for Cornix!
    pct_change = ((close_price - trade['entry']) / trade['entry']) * 100
    if trade['direction'] == 'SHORT':
        pct_change = -pct_change
    logger.info(
        f"💾 DB-UPDATE: [{trade['strategy']}] {trade['coin']} CLOSED ({end_status}) at {close_price}. PnL: {pct_change:.2f}%"
    )


def update_trade_level(conn, trade, new_level, new_sl):
    """updates target level in DB (silent)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE active_trades_master SET status = %s, sl = %s WHERE id = %s", (str(new_level), new_sl, trade['id'])
        )
    conn.commit()
    logger.info(
        f"💾 DB-UPDATE: [{trade['strategy']}] {trade['coin']} TARGET {new_level} HIT. SL internally pulled to {new_sl:.8f}."
    )


# MAIN MONITOR LOOP (LOCAL DB MODE)
def monitor_loop():
    logger.info("=== TRADE MONITOR STARTED (local DB mode) ===")

    # FIX: previously a SINGLE connection held for entire bot lifetime.
    # on DB hiccup (network glitch, DB restart, etc.) connection died
    # and monitor looped with useless connection forward.
    # now: connection built at start and rebuilt on errors.
    conn = None

    def ensure_conn():
        nonlocal conn
        if conn is None:
            conn = get_db_connection()
            create_closed_trades_table(conn)
        return conn

    def reset_conn():
        nonlocal conn
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        conn = None

    # FIX P2.7: in-memory watermark per trade ID (first stage, no DB schema change:
    # active_trades_master has no suitable column). remembers open_time of
    # last scored 5m candle; from there scans FORWARD over all new candles,
    # instead of checking only latest → SL/TP hits between polls/after stale phases
    # don't get lost anymore. after process restart each trade starts at
    # newest candle (no retroactive scoring of old trades).
    last_checked = {}

    while True:
        try:
            now = datetime.datetime.now(pytz.UTC)
            seconds = now.second
            sleep_time = (10 - seconds % 10) if seconds % 10 != 0 else 10
            time.sleep(sleep_time)

            c = ensure_conn()

            # IMPORTANT: commit resets the transaction view of the DB,
            # so that we see the fresh data from ingestion!
            c.commit()

            with c.cursor() as cur:
                cur.execute("SELECT * FROM active_trades_master")
                columns = [desc[0] for desc in cur.description]
                active_trades = [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]

            # FIX P2.7: clean up watermarks from no-longer-active trades
            # (otherwise dict grows unbounded over process lifetime).
            active_ids = set(t['id'] for t in active_trades)
            for tid in [k for k in last_checked if k not in active_ids]:
                del last_checked[tid]

            if not active_trades:
                continue

            # 1. filter unique coins from active trades
            active_coins = set(t['coin'] for t in active_trades)
            coin_candles = {}
            stale_coins = set()

            # 2. wick-aware: get high/low/close of 5m candles.
            #    thus we spot SL/TP hits even if price shoots through intra-candle
            #    and returns at close.
            #
            #    FIX P2.7: instead of just newest candle, get ALL candles since
            #    oldest watermark of trades for this coin (ascending),
            #    so hits between two polls don't get lost.
            #
            #    STALE GUARD: if newest 5m candle older than 30min,
            #    mark coin as stale. trades on this coin
            #    then NOT checked against stale prices — they stay
            #    open until either fresh data comes or housekeeping
            #    closes coin as DELISTED.
            now_utc = datetime.datetime.now(pytz.UTC)
            stale_cutoff_seconds = 1800  # 30 min

            coin_min_wm = {}
            for t in active_trades:
                wm = last_checked.get(t['id'])
                if wm is not None:
                    prev = coin_min_wm.get(t['coin'])
                    if prev is None or wm < prev:
                        coin_min_wm[t['coin']] = wm

            # core.candles: 5m scoring candles, forming candle intentionally included
            # (monitors score SL/TP intra-candle — contract 2: include_forming=True).
            # first run without watermark: only newest candle. otherwise whole
            # window from watermark (start= is `>=` inclusive, so still-
            # forming newest candle is checked again each cycle as before).
            for coin in active_coins:
                try:
                    start_wm = coin_min_wm.get(coin)
                    if start_wm is None:
                        df = read_candles(
                            c,
                            coin,
                            "5m",
                            limit=1,
                            include_forming=True,
                            columns=("open_time", "high", "low", "close"),
                        )
                    else:
                        df = read_candles(
                            c,
                            coin,
                            "5m",
                            start=start_wm,
                            include_forming=True,
                            columns=("open_time", "high", "low", "close"),
                        )
                    rows = list(df.itertuples(index=False, name=None))
                    if not rows:
                        continue
                    newest_open = rows[-1][0]
                    if newest_open.tzinfo is None:
                        newest_open = newest_open.replace(tzinfo=pytz.UTC)
                    age_sec = (now_utc - newest_open).total_seconds()
                    if age_sec > stale_cutoff_seconds:
                        stale_coins.add(coin)
                        logger.debug(f"⏸ {coin}: 5m candle {age_sec:.0f}s old — skipping trade checks")
                        continue
                    coin_candles[coin] = [
                        {
                            'open_time': r[0],
                            'high': float(r[1]),
                            'low': float(r[2]),
                            'close': float(r[3]),
                        }
                        for r in rows
                    ]
                except Exception:
                    # If the table doesn't exist, ignore the error and continue
                    c.rollback()
                    pass

            # Hourly summary when coins are stale
            if stale_coins and now_utc.minute == 0 and now_utc.second < 10:
                logger.warning(
                    f"⏸ {len(stale_coins)} coin(s) with stale 5m data: "
                    f"{sorted(stale_coins)[:10]}{'...' if len(stale_coins) > 10 else ''}"
                )

            # Not a single price found? Then wait.
            if not coin_candles:
                continue

            # Monitor each active trade
            for trade in active_trades:
                coin = trade['coin']
                # If we didn't get any candles from the DB for this coin, skip
                if coin not in coin_candles:
                    continue

                candles_all = coin_candles[coin]
                dir_long = trade['direction'] == 'LONG'

                # Status can be 'WORKING' or '1'/'2'/'3'. Parse defensively,
                # in case another bot ever writes something unexpected.
                status_str = trade.get('status', 'WORKING')
                if status_str == 'WORKING':
                    current_level = 0
                else:
                    try:
                        current_level = int(status_str)
                    except (ValueError, TypeError):
                        logger.warning(
                            f"Unexpected status '{status_str}' for trade {trade.get('id')} ({coin}). Skipping."
                        )
                        continue
                targets = [trade['target1'], trade['target2'], trade['target3'], trade['target4']]

                # FIX P2.7: candle feed — forward from the watermark in chronological order.
                # `>=` instead of `>`, so the still-forming newest candle is re-checked
                # each cycle as before (high/low grow intra-candle).
                # New trade (no watermark): only the newest candle.
                wm = last_checked.get(trade['id'])
                if wm is None:
                    trade_candles = candles_all[-1:]
                else:
                    trade_candles = [k for k in candles_all if k['open_time'] >= wm]

                closed = False
                for candle in trade_candles:
                    last_checked[trade['id']] = candle['open_time']

                    # SL CHECK — wick-aware: LONG stopped out when low below SL,
                    # SHORT stopped out when high above SL
                    # FIX P2.9: sl>0 guard — a SHORT with sl=0 (broken writer)
                    # would otherwise be immediately "stopped out" at price 0 → +100% fake PnL.
                    sl_price = float(trade['sl'] or 0)
                    if sl_price <= 0:
                        sl_hit = False
                    elif dir_long:
                        sl_hit = candle['low'] <= sl_price
                    else:
                        sl_hit = candle['high'] >= sl_price

                    if sl_hit:
                        end_status = "0" if current_level == 0 else f"{current_level}"
                        # Close price = SL (more realistic than the last close, since
                        # in the real world the SL is triggered exactly at the SL level)
                        close_trade(c, trade, float(trade['sl']), end_status)
                        closed = True
                        break

                    # TP CHECK — wick-aware: LONG TP triggered when high above target,
                    # SHORT TP triggered when low below target
                    if current_level < 4:
                        next_target = targets[current_level]
                        if next_target == 0:
                            # A corrupt trade with no SL AND no further target would have
                            # no close path left at all (the sl>0 guard above removed the
                            # old instant fake stop) → close neutrally at entry
                            # instead of leaving a zombie in active_trades_master.
                            # entry>0 guard: close_trade computes PnL = Δ/entry — with
                            # entry 0/None that would kill the whole monitor iteration.
                            entry_price = float(trade['entry'] or 0)
                            if sl_price <= 0 and entry_price > 0:
                                logger.warning(
                                    f"Corrupt trade {trade.get('id')} ({coin}): sl<=0 and no target — closed neutrally."
                                )
                                close_trade(c, trade, entry_price, "0" if current_level == 0 else f"{current_level}")
                                closed = True
                                break
                            continue

                        if dir_long:
                            target_hit = candle['high'] >= next_target
                        else:
                            target_hit = candle['low'] <= next_target

                        if target_hit:
                            new_level = current_level + 1
                            if new_level == 1:
                                update_trade_level(c, trade, new_level, trade['entry'])
                                # P2.7: update the local state so that the next
                                # candle in the same scan is checked against the new SL/level
                                # (previously the DB re-read in the next cycle did this).
                                trade['sl'] = trade['entry']
                                current_level = new_level
                                if trade['target2'] == 0:
                                    close_trade(c, trade, float(next_target), "1")
                                    closed = True
                                    break
                            elif new_level < 4 and targets[new_level] != 0:
                                # FIX P1.2: trailing SL = last target reached
                                # (targets[new_level-2]). Previously the OLD SL
                                # (trade['sl']) was passed → the SL never trailed and
                                # all multi-target PnL/win rates were systematically
                                # wrong. 8_ai_trade_monitor already does it this way.
                                update_trade_level(c, trade, new_level, targets[new_level - 2])
                                trade['sl'] = targets[new_level - 2]
                                current_level = new_level
                            else:
                                close_trade(c, trade, float(next_target), "4")
                                closed = True
                                break

                if closed:
                    # Release the watermark of the closed trade
                    last_checked.pop(trade['id'], None)

        except KeyboardInterrupt:
            raise  # caught below
        except Exception as e:
            logger.error(f"Error in the monitor loop: {e}")
            # FIX: rebuild the connection on connection errors.
            # Previously the dead connection kept being used and every iteration
            # failed again.
            reset_conn()
            time.sleep(5)


def main():
    try:
        monitor_loop()
    except KeyboardInterrupt:
        logger.info("🛑 Trade Monitor stopped (Strg+C).")


if __name__ == "__main__":
    main()
