import datetime
import logging
import time
import warnings

import pytz

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
    # FIX: Vorher naive `datetime.now()` (Server-Lokalzeit). Auf DE/AT-Servern
    # schreibt das bis zu 2h after vorn verschobene Zeitstempel in `posted`,
    # während andere Scripts `datetime.now() - timedelta` in UTC vergleichen
    # (→ frisch geschlossene Trades werden fälschlich als "zu alt" behandelt).
    # Jetzt konsequent UTC.
    now = datetime.datetime.now(datetime.timezone.utc)
    with conn.cursor() as cur:
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

        cur.execute("DELETE FROM active_trades_master WHERE id = %s", (trade['id'],))
    conn.commit()

    # Nur noch lokales Logging für dich, kein Telegram-Spam für Cornix!
    pct_change = ((close_price - trade['entry']) / trade['entry']) * 100
    if trade['direction'] == 'SHORT':
        pct_change = -pct_change
    logger.info(
        f"💾 DB-UPDATE: [{trade['strategy']}] {trade['coin']} CLOSED ({end_status}) zu {close_price}. PnL: {pct_change:.2f}%"
    )


def update_trade_level(conn, trade, new_level, new_sl):
    """Aktualisiert das Target-Level in der DB (STUMM)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE active_trades_master SET status = %s, sl = %s WHERE id = %s", (str(new_level), new_sl, trade['id'])
        )
    conn.commit()
    logger.info(
        f"💾 DB-UPDATE: [{trade['strategy']}] {trade['coin']} TARGET {new_level} HIT. SL intern auf {new_sl:.8f} gezogen."
    )


# HAUPT-MONITOR-SCHLEIFE (LOKALER DB-MODUS)
def monitor_loop():
    logger.info("=== TRADE MONITOR GESTARTET (Lokaler DB-Modus) ===")

    # FIX: Vorher wurde eine EINZIGE Connection für die gesamte Bot-Lifetime
    # offengehalten. Bei DB-Hiccup (Netzwerk-Glitch, DB-Restart, etc.) blieb
    # die Connection tot und der Monitor loopte mit nutzloser Connection weiter.
    # Jetzt: Connection wird zu Beginn aufgebaut und bei Fehlern neu aufgebaut.
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

    while True:
        try:
            now = datetime.datetime.now(pytz.UTC)
            seconds = now.second
            sleep_time = (10 - seconds % 10) if seconds % 10 != 0 else 10
            time.sleep(sleep_time)

            c = ensure_conn()

            # IMPORTANT: commit resets the transaction view of the DB,
            # damit wir die frischen Daten der Ingestion sehen!
            c.commit()

            with c.cursor() as cur:
                cur.execute("SELECT * FROM active_trades_master")
                columns = [desc[0] for desc in cur.description]
                active_trades = [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]

            if not active_trades:
                continue

            # 1. Eindeutige Coins aus den aktiven Trades filtern
            active_coins = set(t['coin'] for t in active_trades)
            live_prices = {}
            stale_coins = set()

            # 2. Wick-aware: high/low/close der neuesten 5m-Kerze holen.
            #    Damit erkennen wir SL/TP-Hits auch wenn der Preis intra-Candle durchschießt
            #    und am Close wieder zurückläuft.
            #
            #    STALE-GUARD: Wenn die neueste 5m-Kerze älter als 30min ist,
            #    markieren wir den Coin als stale. Trades auf diesem Coin
            #    werden dann NICHT gegen veraltete Preise geprüft — sie bleiben
            #    offen bis entweder frische Daten kommen oder das Housekeeping
            #    den Coin als DELISTED schließt.
            now_utc = datetime.datetime.now(pytz.UTC)
            stale_cutoff_seconds = 1800  # 30 min

            with c.cursor() as cur:
                for coin in active_coins:
                    try:
                        cur.execute(
                            f'SELECT open_time, high, low, close FROM "{coin}_5m" ORDER BY open_time DESC LIMIT 1'
                        )
                        row = cur.fetchone()
                        if row:
                            open_time = row[0]
                            if open_time.tzinfo is None:
                                open_time = open_time.replace(tzinfo=pytz.UTC)
                            age_sec = (now_utc - open_time).total_seconds()
                            if age_sec > stale_cutoff_seconds:
                                stale_coins.add(coin)
                                logger.debug(f"⏸ {coin}: 5m-Candle {age_sec:.0f}s alt — skippe Trade-Checks")
                                continue
                            live_prices[coin] = {
                                'high': float(row[1]),
                                'low': float(row[2]),
                                'close': float(row[3]),
                            }
                    except Exception:
                        # Falls Tabelle nicht existiert, Fehler ignorieren und weiter
                        c.rollback()
                        pass

            # Stündliche Summary wenn Coins stale sind
            if stale_coins and now_utc.minute == 0 and now_utc.second < 10:
                logger.warning(
                    f"⏸ {len(stale_coins)} Coin(s) mit staleten 5m-Daten: "
                    f"{sorted(stale_coins)[:10]}{'...' if len(stale_coins) > 10 else ''}"
                )

            # Kein einziger Preis gefunden? Dann warten.
            if not live_prices:
                continue

            # Monitor each active trade
            for trade in active_trades:
                coin = trade['coin']
                # Wenn wir für diesen Coin keinen Preis aus der DB bekommen haben, skippingn
                if coin not in live_prices:
                    continue

                candle = live_prices[coin]
                dir_long = trade['direction'] == 'LONG'

                # Status kann 'WORKING' oder '1'/'2'/'3' sein. Defensiv parsen,
                # falls ein anderer Bot mal etwas Unerwartetes reinschreibt.
                status_str = trade.get('status', 'WORKING')
                if status_str == 'WORKING':
                    current_level = 0
                else:
                    try:
                        current_level = int(status_str)
                    except (ValueError, TypeError):
                        logger.warning(
                            f"Unerwarteter Status '{status_str}' für Trade {trade.get('id')} ({coin}). Skipping."
                        )
                        continue
                targets = [trade['target1'], trade['target2'], trade['target3'], trade['target4']]

                # SL CHECK — Wick-aware: LONG stopped out wenn low unter SL,
                # SHORT stopped out wenn high über SL
                if dir_long:
                    sl_hit = candle['low'] <= trade['sl']
                else:
                    sl_hit = candle['high'] >= trade['sl']

                if sl_hit:
                    end_status = "0" if current_level == 0 else f"{current_level}"
                    # Close-Preis = SL (realistischer als letzter Close, da
                    # der SL in der realen Welt genau am SL-Level getriggert wird)
                    close_trade(c, trade, float(trade['sl']), end_status)
                    continue

                # TP CHECK — Wick-aware: LONG TP getriggert wenn high über Target,
                # SHORT TP getriggert wenn low unter Target
                if current_level < 4:
                    next_target = targets[current_level]
                    if next_target == 0:
                        continue

                    if dir_long:
                        target_hit = candle['high'] >= next_target
                    else:
                        target_hit = candle['low'] <= next_target

                    if target_hit:
                        new_level = current_level + 1
                        if new_level == 1:
                            update_trade_level(c, trade, new_level, trade['entry'])
                            if trade['target2'] == 0:
                                close_trade(c, trade, float(next_target), "1")
                        elif new_level < 4 and targets[new_level] != 0:
                            update_trade_level(c, trade, new_level, trade['sl'])
                        else:
                            close_trade(c, trade, float(next_target), "4")

        except KeyboardInterrupt:
            raise  # Wird unten abgefangen
        except Exception as e:
            logger.error(f"Fehler im Monitor-Loop: {e}")
            # FIX: Bei Connection-Fehlern die Connection neu aufbauen.
            # Vorher wurde die tote Connection weiter benutzt und jede Iteration
            # schlug erneut fehl.
            reset_conn()
            time.sleep(5)


def main():
    try:
        monitor_loop()
    except KeyboardInterrupt:
        logger.info("🛑 Trade Monitor stopped (Strg+C).")


if __name__ == "__main__":
    main()
