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
    # FIX: Vorher naive `datetime.now()` (Server-Lokalzeit). Auf DE/AT-Servern
    # schreibt das bis zu 2h after vorn verschobene Zeitstempel in `posted`,
    # während andere Scripts `datetime.now() - timedelta` in UTC vergleichen
    # (→ frisch geschlossene Trades werden fälschlich als "zu alt" behandelt).
    # Jetzt konsequent UTC.
    now = datetime.datetime.now(datetime.timezone.utc)
    with conn.cursor() as cur:
        # FIX P2.8: DELETE ... RETURNING zuerst — der Insert in die Closed-Tabelle
        # läuft NUR wenn WIR die Row wirklich entfernt haben. Sonst schreiben zwei
        # Iterationen/Prozesse denselben Trade doppelt in closed_trades_master.
        cur.execute("DELETE FROM active_trades_master WHERE id = %s RETURNING id", (trade['id'],))
        if cur.fetchone() is None:
            conn.commit()
            logger.warning(
                f"⚠️ Trade {trade.get('id')} ({trade.get('coin')}) bereits geschlossen — Doppel-Close verhindert."
            )
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

    # FIX P2.7: In-Memory-Wasserzeichen pro Trade-ID (erste Stufe, kein DB-Schema-Change:
    # active_trades_master hat keine passende Spalte). Merkt sich die open_time der
    # zuletzt gescorten 5m-Kerze; ab dort wird VORWÄRTS über alle neuen Kerzen gescannt,
    # statt nur die neueste zu prüfen → SL/TP-Hits zwischen Polls/nach Stale-Phasen
    # gehen nicht mehr verloren. Nach Prozess-Neustart startet jeder Trade an der
    # neuesten Kerze (kein Rückwirkend-Scoring von Alt-Trades).
    last_checked = {}

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

            # FIX P2.7: Wasserzeichen von nicht mehr aktiven Trades aufräumen
            # (sonst wächst das Dict über die Prozess-Lifetime unbegrenzt).
            active_ids = set(t['id'] for t in active_trades)
            for tid in [k for k in last_checked if k not in active_ids]:
                del last_checked[tid]

            if not active_trades:
                continue

            # 1. Eindeutige Coins aus den aktiven Trades filtern
            active_coins = set(t['coin'] for t in active_trades)
            coin_candles = {}
            stale_coins = set()

            # 2. Wick-aware: high/low/close der 5m-Kerzen holen.
            #    Damit erkennen wir SL/TP-Hits auch wenn der Preis intra-Candle durchschießt
            #    und am Close wieder zurückläuft.
            #
            #    FIX P2.7: statt nur der neuesten Kerze werden ALLE Kerzen seit dem
            #    ältesten Wasserzeichen der Trades dieses Coins geholt (aufsteigend),
            #    damit Hits zwischen zwei Polls nicht verloren gehen.
            #
            #    STALE-GUARD: Wenn die neueste 5m-Kerze älter als 30min ist,
            #    markieren wir den Coin als stale. Trades auf diesem Coin
            #    werden dann NICHT gegen veraltete Preise geprüft — sie bleiben
            #    offen bis entweder frische Daten kommen oder das Housekeeping
            #    den Coin als DELISTED schließt.
            now_utc = datetime.datetime.now(pytz.UTC)
            stale_cutoff_seconds = 1800  # 30 min

            coin_min_wm = {}
            for t in active_trades:
                wm = last_checked.get(t['id'])
                if wm is not None:
                    prev = coin_min_wm.get(t['coin'])
                    if prev is None or wm < prev:
                        coin_min_wm[t['coin']] = wm

            # core.candles: 5m-Scoring-Kerzen, forming candle bewusst inkludiert
            # (Monitore scoren SL/TP intra-candle — contract 2: include_forming=True).
            # Erster Lauf ohne Wasserzeichen: nur die neueste Kerze. Sonst das ganze
            # Fenster ab dem Wasserzeichen (start= ist `>=`-inklusiv, damit die noch
            # formende neueste Kerze wie bisher in jedem Zyklus erneut geprüft wird).
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
                        logger.debug(f"⏸ {coin}: 5m-Candle {age_sec:.0f}s alt — skippe Trade-Checks")
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
            if not coin_candles:
                continue

            # Monitor each active trade
            for trade in active_trades:
                coin = trade['coin']
                # Wenn wir für diesen Coin keine Kerzen aus der DB bekommen haben, skippingn
                if coin not in coin_candles:
                    continue

                candles_all = coin_candles[coin]
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

                # FIX P2.7: Kerzen-Zufuhr — ab Wasserzeichen vorwärts in Zeitreihenfolge.
                # `>=` statt `>`, damit die noch formende neueste Kerze wie bisher in
                # jedem Zyklus erneut geprüft wird (high/low wachsen intra-Candle).
                # Neuer Trade (kein Wasserzeichen): nur die neueste Kerze.
                wm = last_checked.get(trade['id'])
                if wm is None:
                    trade_candles = candles_all[-1:]
                else:
                    trade_candles = [k for k in candles_all if k['open_time'] >= wm]

                closed = False
                for candle in trade_candles:
                    last_checked[trade['id']] = candle['open_time']

                    # SL CHECK — Wick-aware: LONG stopped out wenn low unter SL,
                    # SHORT stopped out wenn high über SL
                    # FIX P2.9: sl>0-Guard — ein SHORT mit sl=0 (kaputter Writer)
                    # wäre sonst sofort "ausgestoppt" bei Preis 0 → +100% Fake-PnL.
                    sl_price = float(trade['sl'] or 0)
                    if sl_price <= 0:
                        sl_hit = False
                    elif dir_long:
                        sl_hit = candle['low'] <= sl_price
                    else:
                        sl_hit = candle['high'] >= sl_price

                    if sl_hit:
                        end_status = "0" if current_level == 0 else f"{current_level}"
                        # Close-Preis = SL (realistischer als letzter Close, da
                        # der SL in der realen Welt genau am SL-Level getriggert wird)
                        close_trade(c, trade, float(trade['sl']), end_status)
                        closed = True
                        break

                    # TP CHECK — Wick-aware: LONG TP getriggert wenn high über Target,
                    # SHORT TP getriggert wenn low unter Target
                    if current_level < 4:
                        next_target = targets[current_level]
                        if next_target == 0:
                            # Korrupter Trade ohne SL UND ohne weiteres Target hätte
                            # keinerlei Close-Pfad mehr (der sl>0-Guard oben hat den
                            # alten Sofort-Fake-Stop entfernt) → neutral am Entry
                            # schließen statt Zombie in active_trades_master.
                            # entry>0-Guard: close_trade rechnet PnL = Δ/entry — bei
                            # entry 0/None würde das die ganze Monitor-Iteration killen.
                            entry_price = float(trade['entry'] or 0)
                            if sl_price <= 0 and entry_price > 0:
                                logger.warning(
                                    f"Korrupter Trade {trade.get('id')} ({coin}): sl<=0 und kein Target — neutral geschlossen."
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
                                # P2.7: lokalen Stand nachziehen, damit die nächste
                                # Kerze im selben Scan gegen den neuen SL/Level prüft
                                # (vorher machte das der DB-Re-Read im nächsten Zyklus).
                                trade['sl'] = trade['entry']
                                current_level = new_level
                                if trade['target2'] == 0:
                                    close_trade(c, trade, float(next_target), "1")
                                    closed = True
                                    break
                            elif new_level < 4 and targets[new_level] != 0:
                                # FIX P1.2: Trailing-SL = zuletzt erreichtes Target
                                # (targets[new_level-2]). Vorher wurde der ALTE SL
                                # (trade['sl']) übergeben → der SL zog nie nach und
                                # alle Multi-Target-PnL/Winrates waren systematisch
                                # falsch. 8_ai_trade_monitor macht es bereits so.
                                update_trade_level(c, trade, new_level, targets[new_level - 2])
                                trade['sl'] = targets[new_level - 2]
                                current_level = new_level
                            else:
                                close_trade(c, trade, float(next_target), "4")
                                closed = True
                                break

                if closed:
                    # Wasserzeichen des geschlossenen Trades freigeben
                    last_checked.pop(trade['id'], None)

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
