import datetime
import json
import logging
import time
import warnings

import pytz

# --- IMPORT CONFIGURATION FROM CORE ---
from core.database import get_db_connection

warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_MONITOR - %(message)s')
logger = logging.getLogger(__name__)


def main():
    logger.info("=== 🤖 AI TRADE MONITOR STARTED (local DB mode) ===")

    conn = get_db_connection()

    # Schema-Sicherung: close_time-Spalte in closed_ai_signals (falls von alter Version fehlt)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE closed_ai_signals
                ADD COLUMN IF NOT EXISTS close_time TIMESTAMPTZ DEFAULT NOW()
            """)
        conn.commit()
    except Exception as e:
        logger.warning(f"Could not migrate close_time column: {e}")
        conn.rollback()

    while True:
        try:
            # Synchronise with the 10-second cadence of the ingestion script
            now = datetime.datetime.now(pytz.UTC)
            seconds = now.second
            sleep_time = (10 - seconds % 10) if seconds % 10 != 0 else 10
            time.sleep(sleep_time)

            # FIX: Wenn der vorherige Reconnect fehlschlug, erneut versuchen.
            if conn is None:
                conn = get_db_connection()

            # Transaktions-Sicht der DB zurücksetzen
            conn.commit()

            with conn.cursor() as cur:
                # Loading ALLE aktiven AI-Trades
                cur.execute("""
                    SELECT id, symbol, model, direction, entry1, price, sl, targets, current_target_hit, open_time
                    FROM ai_signals
                """)
                active_trades = cur.fetchall()

            if not active_trades:
                continue

            # 1. Eindeutige Coins filtern
            active_coins = set(t[1] for t in active_trades)
            live_prices = {}
            stale_coins = set()

            # 2. Wick-aware: high/low/close der neuesten 5m-Kerze holen.
            #    SL/TP werden intra-Candle getriggert, nicht erst am Candle-Close.
            #
            #    STALE-GUARD: Wenn die neueste 5m-Kerze älter als 30min ist,
            #    markieren wir den Coin als stale. Trades auf diesem Coin
            #    werden dann NICHT gegen veraltete Preise geprüft — sie bleiben
            #    offen bis entweder frische Daten kommen oder das Housekeeping
            #    den Coin als DELISTED schließt.
            #
            #    Warum 30 Minuten? Die Ingestion liefert 5m-Kerzen alle 5 Minuten.
            #    Wenn eine Kerze >30 Minuten fehlt, ist die Datenlage zu unsicher
            #    um SL/TP-Events verlässlich zu erkennen — ein Preis-Move könnte
            #    Liquidationen ausgelöst haben die wir nie sehen.
            now_utc = datetime.datetime.now(pytz.UTC)
            stale_cutoff_seconds = 1800  # 30 min

            with conn.cursor() as cur:
                for coin in active_coins:
                    try:
                        cur.execute(
                            f'SELECT open_time, high, low, close FROM "{coin}_5m" ORDER BY open_time DESC LIMIT 1'
                        )
                        row = cur.fetchone()
                        if row:
                            open_time = row[0]
                            # Age berechnen (open_time ist TIMESTAMPTZ, now_utc ist auch TZ-aware)
                            if open_time.tzinfo is None:
                                open_time = open_time.replace(tzinfo=pytz.UTC)
                            age_sec = (now_utc - open_time).total_seconds()
                            if age_sec > stale_cutoff_seconds:
                                stale_coins.add(coin)
                                # Nur debug-log damit das Monitor-Log nicht explodiert
                                logger.debug(
                                    f"⏸ {coin}: 5m-Candle {age_sec:.0f}s alt — "
                                    f"skippe Trade-Checks (waiting for fresh data)"
                                )
                                continue
                            live_prices[coin] = {
                                'high': float(row[1]),
                                'low': float(row[2]),
                                'close': float(row[3]),
                            }
                    except Exception:
                        conn.rollback()
                        pass

            if not live_prices:
                continue

            # Stale-Coin-Summary: einmal pro Stunde bei Minute 0 loggen
            # damit wir sehen wenn viele Coins keine frischen Daten haben
            # (= Indiz für Delisting oder Ingestion-Probleme).
            if stale_coins and now_utc.minute == 0 and now_utc.second < 10:
                logger.warning(
                    f"⏸ {len(stale_coins)} Coin(s) mit staleten 5m-Daten — "
                    f"Trades darauf bleiben offen bis Housekeeping sie räumt: "
                    f"{sorted(stale_coins)[:10]}{'...' if len(stale_coins) > 10 else ''}"
                )

            # === NEU: BATCH PROCESSING VARIABLEN ===
            BATCH_SIZE = 50
            updates_pending = 0

            with conn.cursor() as cur:
                for trade in active_trades:
                    (
                        trade_id,
                        symbol,
                        model,
                        direction,
                        entry1,
                        db_price,
                        current_sl,
                        targets_data,
                        targets_hit,
                        open_time,
                    ) = trade

                    candle = live_prices.get(symbol)
                    if not candle:
                        continue

                    # close = letzter Marktpreis, für Logging und Legacy-PnL.
                    # high/low für Wick-aware SL/TP-Detection.
                    current_price = candle['close']
                    candle_high = candle['high']
                    candle_low = candle['low']

                    entry = float(entry1) if entry1 is not None else (float(db_price) if db_price is not None else None)
                    if entry is None or entry <= 0:
                        continue

                    is_closed = False
                    close_reason = ""
                    close_price = current_price  # wird überschrieben wenn SL/TP genau am Level getriggert wird
                    new_sl = current_sl
                    # FIX: targets_hit defensiv zu Int konvertieren.
                    # Je after DB-Schema (TEXT vs INTEGER) kann hier ein String oder Int
                    # ankommen — ohne Cast führt `range(new_targets_hit, ...)` zu TypeError
                    # wenn das Schema als TEXT angelegt wurde.
                    try:
                        new_targets_hit = int(targets_hit) if targets_hit is not None else 0
                    except (ValueError, TypeError):
                        new_targets_hit = 0
                    db_was_changed = False  # Hilfsvariable für den Batch-Counter

                    if targets_data is None:
                        # LEGACY: einfache %-Schwellen gegen Close (keine Level-Info vorhanden)
                        if direction == "LONG":
                            pnl_pct = (current_price - entry) / entry * 100
                        else:
                            pnl_pct = (entry - current_price) / entry * 100

                        if pnl_pct >= 2.5:
                            is_closed = True
                            close_reason = "LEGACY TARGET HIT (+2.5%)"
                        elif pnl_pct <= -5.0:
                            is_closed = True
                            close_reason = "LEGACY FALLBACK SL (-5.0%)"

                    # B) MODERNE TRADES (MIT TARGETS UND SL) — Wick-aware
                    else:
                        targets = json.loads(targets_data) if isinstance(targets_data, str) else targets_data

                        if direction == "LONG":
                            # SL: LONG gestoppt wenn low unter SL
                            if current_sl is not None and candle_low <= float(current_sl):
                                is_closed = True
                                close_reason = f"SL Hit (SL: {current_sl})"
                                close_price = float(current_sl)
                            else:
                                # TPs: LONG TP getriggert wenn high über Target
                                for i in range(new_targets_hit, len(targets)):
                                    if candle_high >= float(targets[i]):
                                        new_targets_hit = i + 1
                                        if new_targets_hit == 1:
                                            new_sl = entry
                                        elif new_targets_hit > 1:
                                            new_sl = targets[new_targets_hit - 2]
                                    else:
                                        break
                                if new_targets_hit == len(targets):
                                    is_closed = True
                                    close_reason = "ALL TARGETS HIT"
                                    close_price = float(targets[-1])

                        elif direction == "SHORT":
                            # SL: SHORT gestoppt wenn high über SL
                            if current_sl is not None and candle_high >= float(current_sl):
                                is_closed = True
                                close_reason = f"SL Hit (SL: {current_sl})"
                                close_price = float(current_sl)
                            else:
                                # TPs: SHORT TP getriggert wenn low unter Target
                                for i in range(new_targets_hit, len(targets)):
                                    if candle_low <= float(targets[i]):
                                        new_targets_hit = i + 1
                                        if new_targets_hit == 1:
                                            new_sl = entry
                                        elif new_targets_hit > 1:
                                            new_sl = targets[new_targets_hit - 2]
                                    else:
                                        break
                                if new_targets_hit == len(targets):
                                    is_closed = True
                                    close_reason = "ALL TARGETS HIT"
                                    close_price = float(targets[-1])

                    # C) DATENBANK UPDATES AUSFÜHREN
                    if is_closed:
                        pnl = (
                            (close_price - entry) / entry * 100
                            if direction == "LONG"
                            else (entry - close_price) / entry * 100
                        )
                        logger.info(
                            f"🔒 AI Trade {symbol} ({model}) geschlossen! Grund: {close_reason} | PnL: {pnl:.2f}%"
                        )

                        cur.execute(
                            """
                            INSERT INTO closed_ai_signals (symbol, model, direction, entry, close_price, targets_hit, open_time, close_time, status)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), %s)
                        """,
                            (
                                symbol,
                                model,
                                direction,
                                float(entry),
                                float(close_price),
                                int(new_targets_hit),
                                open_time,
                                close_reason,
                            ),
                        )

                        cur.execute("DELETE FROM ai_signals WHERE id = %s", (trade_id,))
                        db_was_changed = True

                    elif targets_data is not None and new_targets_hit > (targets_hit or 0):
                        logger.info(
                            f"🎯 AI Trade {symbol} ({model}) hat Target {new_targets_hit} erreicht! SL auf {new_sl:.6f} gezogen."
                        )
                        cur.execute(
                            """
                            UPDATE ai_signals
                            SET current_target_hit = %s, sl = %s
                            WHERE id = %s
                        """,
                            (new_targets_hit, new_sl, trade_id),
                        )
                        db_was_changed = True

                    # === NEU: BATCH COMMIT AUSFÜHREN ===
                    if db_was_changed:
                        updates_pending += 1
                        if updates_pending >= BATCH_SIZE:
                            conn.commit()
                            logger.info(
                                f"💾 Batch Commit: {BATCH_SIZE} Trades in der Datenbank gespeichert (Speicher geleert)."
                            )
                            updates_pending = 0

            # Finaler Commit für den Rest der Trades (die z.B. nur 12 waren, also den 50er Threshold nicht erreicht haben)
            if updates_pending > 0:
                conn.commit()

        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error(f"Fehler im AI Trade Monitor: {e}")
            # FIX: Bei DB-Fehler Connection neu aufbauen statt mit toter weiterzumachen.
            try:
                if conn:
                    conn.rollback()
            except Exception:
                pass
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            try:
                conn = get_db_connection()
            except Exception as reconnect_err:
                logger.error(f"Reconnect fehlgeschlagen: {reconnect_err}")
                conn = None
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 AI Trade Monitor Bot manuell stopped (Strg+C). Shutting down cleanly...")
