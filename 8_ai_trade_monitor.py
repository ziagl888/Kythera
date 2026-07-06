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
            # Limit-Entry-Support (MIS2-SHORT, 2026-07-06): entry_filled=FALSE
            # heißt "Limit-Order noch nicht gefüllt" — kein Scoring vor dem Fill;
            # expiry_hours = Horizont für Verfall (Entry nie erreicht) und
            # Timeout-Exit (gehört zur studien-validierten Bracket-Geometrie).
            cur.execute("ALTER TABLE ai_signals ADD COLUMN IF NOT EXISTS entry_filled BOOLEAN DEFAULT TRUE")
            cur.execute("ALTER TABLE ai_signals ADD COLUMN IF NOT EXISTS expiry_hours INTEGER")
        conn.commit()
    except Exception as e:
        logger.warning(f"Could not migrate schema columns: {e}")
        conn.rollback()

    # FIX P2.7: In-Memory-Wasserzeichen pro Trade-ID (erste Stufe, kein DB-Schema-Change:
    # ai_signals hat keine passende Spalte). Merkt sich die open_time der zuletzt
    # gescorten 5m-Kerze; ab dort wird VORWÄRTS über alle neuen Kerzen gescannt,
    # statt nur die neueste zu prüfen → SL/TP-Hits zwischen Polls/nach Stale-Phasen
    # gehen nicht mehr verloren. Nach Prozess-Neustart startet jeder Trade an der
    # neuesten Kerze (kein Rückwirkend-Scoring von Alt-Trades).
    last_checked = {}

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
                    SELECT id, symbol, model, direction, entry1, price, sl, targets, current_target_hit, open_time,
                           entry_filled, expiry_hours
                    FROM ai_signals
                """)
                active_trades = cur.fetchall()

            # FIX P2.7: Wasserzeichen von nicht mehr aktiven Trades aufräumen
            # (sonst wächst das Dict über die Prozess-Lifetime unbegrenzt).
            active_ids = set(t[0] for t in active_trades)
            for tid in [k for k in last_checked if k not in active_ids]:
                del last_checked[tid]

            if not active_trades:
                continue

            # 1. Eindeutige Coins filtern
            active_coins = set(t[1] for t in active_trades)
            coin_candles = {}
            stale_coins = set()

            # 2. Wick-aware: high/low/close der 5m-Kerzen holen.
            #    SL/TP werden intra-Candle getriggert, nicht erst am Candle-Close.
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
            #
            #    Warum 30 Minuten? Die Ingestion liefert 5m-Kerzen alle 5 Minuten.
            #    Wenn eine Kerze >30 Minuten fehlt, ist die Datenlage zu unsicher
            #    um SL/TP-Events verlässlich zu erkennen — ein Preis-Move könnte
            #    Liquidationen ausgelöst haben die wir nie sehen.
            now_utc = datetime.datetime.now(pytz.UTC)
            stale_cutoff_seconds = 1800  # 30 min

            coin_min_wm = {}
            for t in active_trades:
                wm = last_checked.get(t[0])
                if wm is not None:
                    prev = coin_min_wm.get(t[1])
                    if prev is None or wm < prev:
                        coin_min_wm[t[1]] = wm

            with conn.cursor() as cur:
                for coin in active_coins:
                    try:
                        start_wm = coin_min_wm.get(coin)
                        if start_wm is None:
                            # Kein Trade dieses Coins hat ein Wasserzeichen (erster Lauf):
                            # nur die neueste Kerze — kein Rückwirkend-Scoring.
                            cur.execute(
                                f'SELECT open_time, high, low, close FROM "{coin}_5m" ORDER BY open_time DESC LIMIT 1'
                            )
                            rows = cur.fetchall()
                        else:
                            cur.execute(
                                f'SELECT open_time, high, low, close FROM "{coin}_5m" '
                                f'WHERE open_time >= %s ORDER BY open_time ASC',
                                (start_wm,),
                            )
                            rows = cur.fetchall()
                        if not rows:
                            continue
                        newest_open = rows[-1][0]
                        # Age berechnen (open_time ist TIMESTAMPTZ, now_utc ist auch TZ-aware)
                        if newest_open.tzinfo is None:
                            newest_open = newest_open.replace(tzinfo=pytz.UTC)
                        age_sec = (now_utc - newest_open).total_seconds()
                        if age_sec > stale_cutoff_seconds:
                            stale_coins.add(coin)
                            # Nur debug-log damit das Monitor-Log nicht explodiert
                            logger.debug(
                                f"⏸ {coin}: 5m-Candle {age_sec:.0f}s alt — skippe Trade-Checks (waiting for fresh data)"
                            )
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
                        conn.rollback()
                        pass

            if not coin_candles:
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
                        entry_filled,
                        expiry_hours,
                    ) = trade

                    candles_all = coin_candles.get(symbol)
                    if not candles_all:
                        continue

                    entry = float(entry1) if entry1 is not None else (float(db_price) if db_price is not None else None)
                    if entry is None or entry <= 0:
                        continue

                    # FIX P2.7: Kerzen-Zufuhr — ab Wasserzeichen vorwärts in Zeitreihenfolge.
                    # `>=` statt `>`, damit die noch formende neueste Kerze wie bisher in
                    # jedem Zyklus erneut geprüft wird (high/low wachsen intra-Candle).
                    # Neuer Trade (kein Wasserzeichen): nur die neueste Kerze.
                    wm = last_checked.get(trade_id)
                    if wm is None:
                        trade_candles = candles_all[-1:]
                    else:
                        trade_candles = [k for k in candles_all if k['open_time'] >= wm]

                    # FIX: targets_hit defensiv zu Int konvertieren.
                    # Je after DB-Schema (TEXT vs INTEGER) kann hier ein String oder Int
                    # ankommen — ohne Cast führt `range(new_targets_hit, ...)` zu TypeError
                    # wenn das Schema als TEXT angelegt wurde.
                    try:
                        hit_state = int(targets_hit) if targets_hit is not None else 0
                    except (ValueError, TypeError):
                        hit_state = 0
                    # P2.7: lokaler Stand über die Kerzen hinweg (statt DB-Re-Read pro Zyklus)
                    sl_state = current_sl

                    targets = None
                    if targets_data is not None:
                        targets = json.loads(targets_data) if isinstance(targets_data, str) else targets_data

                    # Limit-Entry-Status (MIS2-SHORT: Entry = Limit-Sell +5 % über
                    # Signalkurs — Scoring erst NACH dem Fill, sonst Phantom-Trades).
                    filled = True if entry_filled is None else bool(entry_filled)
                    expiry = int(expiry_hours) if expiry_hours is not None else None
                    ot_aware = open_time
                    if ot_aware is not None and ot_aware.tzinfo is None:
                        ot_aware = ot_aware.replace(tzinfo=pytz.UTC)

                    for candle in trade_candles:
                        last_checked[trade_id] = candle['open_time']

                        # close = Marktpreis der Kerze, für Logging und Legacy-PnL.
                        # high/low für Wick-aware SL/TP-Detection.
                        current_price = candle['close']
                        candle_high = candle['high']
                        candle_low = candle['low']

                        is_closed = False
                        close_reason = ""
                        close_price = current_price  # wird überschrieben wenn SL/TP genau am Level getriggert wird
                        new_sl = sl_state
                        new_targets_hit = hit_state
                        db_was_changed = False  # Hilfsvariable für den Batch-Counter
                        tp_allowed = True

                        # Horizont-Alter dieser Kerze relativ zum Signal
                        c_ot = candle['open_time']
                        if c_ot.tzinfo is None:
                            c_ot = c_ot.replace(tzinfo=pytz.UTC)
                        past_expiry = (
                            expiry is not None and ot_aware is not None
                            and (c_ot - ot_aware) >= datetime.timedelta(hours=expiry)
                        )

                        if not filled:
                            if past_expiry:
                                # Entry innerhalb des Horizonts nie erreicht → Verfall,
                                # PnL 0 (war nie im Markt). Consumers filtern den Status.
                                is_closed = True
                                close_reason = "ENTRY_NOT_FILLED"
                                close_price = entry
                            elif (direction == "SHORT" and candle_high >= entry) or (
                                direction == "LONG" and candle_low <= entry
                            ):
                                filled = True
                                tp_allowed = False  # Fill-Kerze: konservativ nur SL (wie Studie)
                                cur.execute(
                                    "UPDATE ai_signals SET entry_filled = TRUE WHERE id = %s", (trade_id,)
                                )
                                db_was_changed = True
                                logger.info(f"📥 {symbol} ({model}): Limit-Entry {entry} gefüllt.")
                            else:
                                continue  # vor dem Fill kein SL/TP-Scoring

                        if is_closed:
                            pass  # ENTRY_NOT_FILLED → direkt zum Close-Block C)
                        elif past_expiry:
                            # Studien-Geometrie: hartes Timeout am Horizontende → Exit zum Close
                            is_closed = True
                            close_reason = "HORIZON_TIMEOUT"
                            close_price = current_price
                        elif targets is None:
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
                            if direction == "LONG":
                                # SL: LONG gestoppt wenn low unter SL
                                if sl_state is not None and candle_low <= float(sl_state):
                                    is_closed = True
                                    close_reason = f"SL Hit (SL: {sl_state})"
                                    close_price = float(sl_state)
                                elif tp_allowed:
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
                                if sl_state is not None and candle_high >= float(sl_state):
                                    is_closed = True
                                    close_reason = f"SL Hit (SL: {sl_state})"
                                    close_price = float(sl_state)
                                elif tp_allowed:
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

                            # FIX P2.8: DELETE ... RETURNING zuerst — der Insert in die
                            # Closed-Tabelle läuft NUR wenn WIR die Row wirklich entfernt
                            # haben. Sonst schreiben zwei Iterationen/Prozesse denselben
                            # Trade doppelt in closed_ai_signals. Beides in derselben
                            # Transaktion (Batch-Commit unten).
                            cur.execute("DELETE FROM ai_signals WHERE id = %s RETURNING id", (trade_id,))
                            if cur.fetchone() is not None:
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
                                db_was_changed = True
                            else:
                                logger.warning(
                                    f"⚠️ AI Trade {trade_id} ({symbol}) bereits geschlossen — Doppel-Close verhindert."
                                )
                            # Wasserzeichen des geschlossenen Trades freigeben
                            last_checked.pop(trade_id, None)

                        elif targets is not None and new_targets_hit > hit_state:
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
                            hit_state = new_targets_hit
                            sl_state = new_sl

                        # === NEU: BATCH COMMIT AUSFÜHREN ===
                        if db_was_changed:
                            updates_pending += 1
                            if updates_pending >= BATCH_SIZE:
                                conn.commit()
                                logger.info(
                                    f"💾 Batch Commit: {BATCH_SIZE} Trades in der Datenbank gespeichert (Speicher geleert)."
                                )
                                updates_pending = 0

                        if is_closed:
                            break

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
