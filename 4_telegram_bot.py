import asyncio
import logging
import os
import re
import time
import warnings

from telegram import Bot
from telegram.error import RetryAfter, TelegramError, TimedOut

# Suppress Pandas warning (in case it appears here)
warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

# --- UNSERE SAUBEREN IMPORTS ---
from core.config import CH_ATB_INFO, REGIME_STATUS_CHANNEL_ID, TELEGRAM_BOT_TOKEN
from core.database import get_db_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - TELEGRAM_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- KONSTANTEN ---
MAX_ATTEMPTS = 3  # After 3 failed attempts a message is marked as "failed"
FETCH_BATCH_SIZE = 50  # Max Messages pro DB-Roundtrip
IDLE_SLEEP_SEC = 5  # Loop-Delay wenn Outbox leer

# P1.1: Trading-Signale altern schnell — nach Downtime dürfen keine stundenalten
# Entries zu längst vergangenen Preisen mehr rausgehen. Info-Channels haben kein TTL.
SIGNAL_TTL_MINUTES = 15

# P0.1: 'sending'-Rows, die länger als diese Grace-Zeit stehen, gelten als
# verwaist (Crash oder TimedOut mit unbekanntem Outcome) und werden von
# recover_stale_sending() aufgelöst. Grace > längster realistischer Send
# (Photo-Upload + Telegram-Timeout), damit der eigene laufende Send nicht
# einkassiert wird.
SENDING_RECOVERY_GRACE_SEC = 120

# P0.1/P1.1: Reine Info-Channels — dort sind Resend nach Crash und stale
# Messages harmlos. ALLES andere (REGIME_TRADING_CHANNEL_ID, alle
# Bot-Signal-Channels, ...) wird konservativ als Trading-Channel behandelt:
# falsch-positiv kostet nur eine verlorene Message, falsch-negativ einen
# Doppel-Trade bei Cornix.
INFO_CHANNEL_IDS = frozenset(cid for cid in (REGIME_STATUS_CHANNEL_ID, CH_ATB_INFO) if cid)

# Rate limiting (replaces old fixed ANTI_SPAM_SLEEP_SEC=1):
#
# Telegram limits (official):
#   - 30 Messages/Sekunde global pro Bot
#   - 20 Messages/Minute pro Channel/Group (= 1 alle 3s)
#
# We stay below both limits with a safe buffer, combined with
# intelligent message selection: instead of strict FIFO, pick the next sendable
# message from the batch. This way a channel backlog does not stall
# other channels.
GLOBAL_MIN_INTERVAL_MS = 50  # ~20 sends/s globally (Telegram allows 30/s)
PER_CHANNEL_MIN_INTERVAL_MS = 3100  # ~19/min per channel (Telegram allows 20/min)


def is_trading_channel(channel_id) -> bool:
    """True wenn auf dem Channel real gehandelt werden kann (Cornix liest mit).

    Konservativ: alles, was nicht explizit als Info-Channel bekannt ist,
    ist ein Trading-Channel (P0.1).
    """
    return channel_id not in INFO_CHANNEL_IDS


def ensure_schema(conn) -> None:
    """Ensures telegram_outbox has the required columns.

    Additional columns:
    - attempts:   Zähler für Fehlversuche, after MAX_ATTEMPTS wird die Message
                  is marked 'failed' so it does not block the queue.
    - failed:     True when permanently abandoned.
    - last_error: Last error text for debugging.
    - status:     P0.1 — Zustandsmaschine 'pending' → 'sending' → 'sent' bzw.
                  'failed'/'expired'/'dead_letter'. sent/failed bleiben als
                  Booleans für alle anderen Reader (Housekeeping, Health-Monitor)
                  parallel gepflegt.
    - sending_at: P0.1 — wann die Row auf 'sending' ging (für die
                  Verwaist-Erkennung in recover_stale_sending).
    """
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS telegram_outbox (
                id SERIAL PRIMARY KEY,
                channel_id BIGINT,
                message TEXT,
                image_path TEXT,
                sent BOOLEAN DEFAULT FALSE,
                attempts INTEGER DEFAULT 0,
                failed BOOLEAN DEFAULT FALSE,
                last_error TEXT,
                status TEXT DEFAULT 'pending',
                sending_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Migration für bestehende Installation: fehlende Spalten afterziehen
        for col_sql in [
            "ALTER TABLE telegram_outbox ADD COLUMN IF NOT EXISTS attempts INTEGER DEFAULT 0",
            "ALTER TABLE telegram_outbox ADD COLUMN IF NOT EXISTS failed BOOLEAN DEFAULT FALSE",
            "ALTER TABLE telegram_outbox ADD COLUMN IF NOT EXISTS last_error TEXT",
            "ALTER TABLE telegram_outbox ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()",
            "ALTER TABLE telegram_outbox ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pending'",
            "ALTER TABLE telegram_outbox ADD COLUMN IF NOT EXISTS sending_at TIMESTAMPTZ",
        ]:
            cur.execute(col_sql)
    conn.commit()


def expire_stale_signals(conn) -> None:
    """P1.1: Trading-Messages älter als SIGNAL_TTL_MINUTES nicht mehr senden.

    Nach Downtime wären das Signale zu längst vergangenen Preisen — auf
    Cornix-Channels gefährlich. Einmal pro Poll, mit Count-Log.
    Info-Channels haben kein TTL.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE telegram_outbox
            SET failed = TRUE, status = 'expired', last_error = %s
            WHERE sent = FALSE AND failed = FALSE
              AND COALESCE(status, 'pending') = 'pending'
              AND created_at <= NOW() - %s * INTERVAL '1 minute'
              AND NOT (channel_id = ANY(%s))
            """,
            (
                f"expired: aelter als {SIGNAL_TTL_MINUTES} min (P1.1)",
                SIGNAL_TTL_MINUTES,
                # Leere Liste würde psycopg2 nicht typisieren können → Dummy 0
                list(INFO_CHANNEL_IDS) or [0],
            ),
        )
        expired = cur.rowcount
    conn.commit()
    if expired:
        logger.warning(
            f"⏰ {expired} Trading-Messages älter als {SIGNAL_TTL_MINUTES} min als 'expired' markiert (P1.1)."
        )


def recover_stale_sending(conn, min_age_sec: int) -> None:
    """P0.1(c): stehen gebliebene 'sending'-Rows (unbekanntes Outcome) auflösen.

    Trading-Channels: NIE automatisch neu senden — der Send kann durchgegangen
    sein, ein Resend hieße Cornix eröffnet den Trade doppelt → dead_letter
    + WARNING (der Operator entscheidet manuell).
    Info-Channels: Resend ist harmlos → zurück auf 'pending' (bzw. endgültig
    'failed' wenn attempts erschöpft).

    min_age_sec=0 beim Prozessstart (kein Send kann mehr in-flight sein),
    sonst SENDING_RECOVERY_GRACE_SEC damit der eigene laufende Send nicht
    einkassiert wird.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, channel_id, attempts
            FROM telegram_outbox
            WHERE COALESCE(status, '') = 'sending' AND sent = FALSE AND failed = FALSE
              AND (sending_at IS NULL OR sending_at <= NOW() - %s * INTERVAL '1 second')
            """,
            (min_age_sec,),
        )
        rows = cur.fetchall()
        for msg_id, channel_id, attempts in rows:
            if is_trading_channel(channel_id):
                cur.execute(
                    """
                    UPDATE telegram_outbox
                    SET failed = TRUE, status = 'dead_letter',
                        last_error = COALESCE(last_error, 'sending interrupted (P0.1)')
                    WHERE id = %s
                    """,
                    (msg_id,),
                )
                logger.warning(
                    f"☠️ Msg {msg_id} (Trading-Kanal {channel_id}) stand auf 'sending' mit unbekanntem "
                    f"Outcome → dead_letter, KEIN Auto-Resend (P0.1)."
                )
            elif attempts >= MAX_ATTEMPTS:
                cur.execute(
                    "UPDATE telegram_outbox SET failed = TRUE, status = 'failed' WHERE id = %s",
                    (msg_id,),
                )
                logger.error(f"❌ Msg {msg_id} an Kanal {channel_id} nach {MAX_ATTEMPTS} Versuchen endgültig failed.")
            else:
                cur.execute(
                    "UPDATE telegram_outbox SET status = 'pending', sending_at = NULL WHERE id = %s",
                    (msg_id,),
                )
    conn.commit()


def claim_for_sending(cur, msg_id: int) -> bool:
    """P0.1(a)/P2.10: Row atomar von pending auf 'sending' übernehmen.

    FOR UPDATE SKIP LOCKED nur für diesen Statusübergang — ein zweiter
    Consumer überspringt die Row, statt sie doppelt zu senden. Der Caller
    committet SOFORT danach (VOR dem Send), damit ein Crash nach dem Send
    keinen Re-Send produzieren kann.
    """
    cur.execute(
        """
        SELECT id FROM telegram_outbox
        WHERE id = %s AND sent = FALSE AND failed = FALSE
          AND COALESCE(status, 'pending') = 'pending'
        FOR UPDATE SKIP LOCKED
        """,
        (msg_id,),
    )
    if cur.fetchone() is None:
        return False
    cur.execute(
        "UPDATE telegram_outbox SET status = 'sending', sending_at = NOW() WHERE id = %s",
        (msg_id,),
    )
    return True


def try_delete_chart(image_path: str) -> None:
    """Löscht den Chart after erfolgreichem Versand, um Disk-Füllen zu verhindern.

    Chart-Löschung wird ignoriert wenn die Datei nicht existiert — das passiert
    bei Race-Conditions normal und ist no error.
    """
    if not image_path:
        return
    try:
        if os.path.isfile(image_path):
            os.remove(image_path)
    except Exception as e:
        logger.debug(f"Konnte Chart {image_path} nicht löschen: {e}")


def try_delete_chart_if_unreferenced(cur, image_path: str, current_msg_id: int) -> None:
    """FIX (#68/#87): Chart nur löschen wenn KEIN weiterer ungesendeter Outbox-Eintrag
    denselben Pfad referenziert.

    Vorher wurde der Chart sofort after dem ersten erfolgreichen Send gelöscht.
    Wenn zwei Bots denselben Chart-Pfad in die Outbox schrieben (z.B. weil
    derselbe Pattern von verschiedenen Perspektiven geloggt wird) scheiterte
    der zweite Send mit FileNotFoundError — und fiel auf Text-Only zurück,
    obwohl die Nachricht eigentlich einen Chart hätte haben sollen.
    """
    if not image_path:
        return
    try:
        # Gibt es weitere ungesendete Outbox-entries mit genau diesem image_path?
        cur.execute(
            "SELECT 1 FROM telegram_outbox WHERE image_path = %s AND sent = FALSE AND id != %s LIMIT 1",
            (image_path, current_msg_id),
        )
        if cur.fetchone() is not None:
            # Andere ungesendete Msg braucht die Datei noch → nicht löschen
            return
        # Keine anderen Referenzen mehr → sicher zu löschen
        if os.path.isfile(image_path):
            os.remove(image_path)
    except Exception as e:
        logger.debug(f"Konnte Chart {image_path} nicht löschen: {e}")


def mark_sent(cur, msg_id: int, image_path: str | None) -> None:
    """Markiert Nachricht als gesendet und löscht den Chart nur wenn keine anderen
    ungesendeten entries die Datei noch brauchen."""
    cur.execute("UPDATE telegram_outbox SET sent = TRUE, status = 'sent' WHERE id = %s", (msg_id,))
    try_delete_chart_if_unreferenced(cur, image_path, msg_id)


def mark_failure(cur, msg_id: int, error: str, image_path: str | None) -> bool:
    """Erhöht attempt-counter. Gibt True zurück wenn Message als failed markiert wurde
    (= max attempts erreicht), damit der Queue nicht blockiert.

    P0.1: bei einem NICHT-finalen, eindeutig fehlgeschlagenen Send (Exception mit
    bekanntem Outcome) geht die Row zurück auf 'pending' — Resend ist hier sicher,
    weil Telegram die Message definitiv nicht angenommen hat.
    """
    cur.execute(
        """
        UPDATE telegram_outbox
        SET attempts = attempts + 1,
            last_error = %s,
            failed = CASE WHEN attempts + 1 >= %s THEN TRUE ELSE failed END,
            status = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE 'pending' END,
            sending_at = NULL
        WHERE id = %s
        RETURNING failed
        """,
        (error[:1000], MAX_ATTEMPTS, MAX_ATTEMPTS, msg_id),
    )
    row = cur.fetchone()
    now_failed = bool(row and row[0])
    if now_failed:
        # Message wird nie mehr versucht – Chart aufräumen (aber nur wenn
        # nicht von anderen entriesn referenziert)
        try_delete_chart_if_unreferenced(cur, image_path, msg_id)
    return now_failed


async def process_outbox():
    """Endlosschleife: pollt DB, sendet Nachrichten mit Flood-Control und Retry-Limit.

    Message-Auswahl-Strategie (überarbeitet):
    Statt den Batch stur FIFO durchzugehen (was einen Channel-Stau auf andere
    Channels übertragen würde), wird per Iteration die "nächste sendbare"
    Message gepickt — die erste in FIFO-Reihenfolge deren Channel aktuell
    nicht durch die Per-Channel-Rate-Limit-Sperre blockiert ist.

    Wenn alle Messages im Batch blockierten Channels gehören, wartet der
    Worker bis der früheste Channel wieder frei wird (nicht länger).

    FIFO-Ordering bleibt pro Channel erhalten (wichtig für semantisch
    aufeinanderfolgende Messages wie "Signal" → "Update"); nur zwischen
    Channels kann Reihenfolge vertauscht werden, was harmlos ist.

    P0.1 (at-most-once für Trading-Channels): jede Row wird VOR dem Send per
    claim_for_sending() auf 'sending' committet. Nur eindeutig fehlgeschlagene
    Sends gehen zurück auf 'pending'; TimedOut (Outcome unbekannt) bleibt
    'sending' und wird von recover_stale_sending() aufgelöst — Trading-Channels
    landen im dead_letter statt in einem Auto-Resend.
    """
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    logger.info("🤖 Bot erfolgreich initialisiert. Überwache Outbox mit Retry-Limit...")

    # Schema einmal beim Start absichern; danach verwaiste 'sending'-Rows
    # aus einem früheren Prozesslauf auflösen (P0.1(c), min_age=0: es kann
    # kein Send mehr in-flight sein).
    with get_db_connection() as init_conn:
        ensure_schema(init_conn)
        recover_stale_sending(init_conn, 0)

    # Rate-Limit-State (prozesslokal, überlebt Loop-Iterationen):
    #   channel_id -> letzter Send-Timestamp in ms
    last_send_per_channel: dict[int, float] = {}
    last_global_send_ms: float = 0.0

    while True:
        conn = None
        batch_was_empty = False
        try:
            conn = get_db_connection()

            # P1.1: abgelaufene Trading-Signale einmal pro Poll expiren.
            expire_stale_signals(conn)
            # P0.1(c/d): verwaiste 'sending'-Rows (Crash oder TimedOut) nach
            # Grace-Zeit auflösen — Trading → dead_letter, Info → pending.
            recover_stale_sending(conn, SENDING_RECOVERY_GRACE_SEC)

            with conn.cursor() as cur:
                # Nur pending Messages holen, FIFO after ID (älteste zuerst).
                # P1.1: Trading-Messages nur innerhalb des TTL-Fensters.
                # P0.1(d): Channels mit offener 'sending'-Row (unbekanntes
                # Outcome) sind komplett gesperrt, damit Message n+1 (z.B.
                # SL-Update) nicht vor n (Entry) rausgeht — bis
                # recover_stale_sending die Row aufgelöst hat.
                cur.execute(
                    """
                    SELECT id, channel_id, message, image_path, attempts
                    FROM telegram_outbox
                    WHERE sent = FALSE AND failed = FALSE
                      AND COALESCE(status, 'pending') = 'pending'
                      AND (channel_id = ANY(%s) OR created_at > NOW() - %s * INTERVAL '1 minute')
                      AND channel_id NOT IN (
                          SELECT channel_id FROM telegram_outbox
                          WHERE COALESCE(status, '') = 'sending'
                            AND sent = FALSE AND failed = FALSE
                            AND channel_id IS NOT NULL
                      )
                    ORDER BY id ASC
                    LIMIT %s
                    """,
                    (list(INFO_CHANNEL_IDS) or [0], SIGNAL_TTL_MINUTES, FETCH_BATCH_SIZE),
                )
                # Als Liste umwandeln damit wir entries rausnehmen können
                unsent_messages = [list(row) for row in cur.fetchall()]

                if not unsent_messages:
                    # Keine Arbeit — wir verlassen den with-Block sauber und
                    # machen den Idle-Sleep daafter im finally-Nachgang.
                    # WICHTIG: kein conn.close() hier — das übernimmt finally,
                    # und mitten im with-cursor-Block schließen führt zu
                    # "connection already closed"-Errors beim Exit.
                    batch_was_empty = True
                else:
                    # === Intelligenter Send-Loop ===
                    # Wir arbeiten solange bis entweder der Batch leer ist oder
                    # eine RetryAfter/Flood-Control den Batch abbricht
                    batch_aborted = False
                    # P1.3: Channels mit Sendefehler in diesem Batch — deren
                    # restliche Messages werden übersprungen, damit die
                    # Per-Channel-FIFO-Reihenfolge nicht bricht (SL-Update
                    # darf nicht vor seinem Entry ankommen).
                    failed_channels: set[int] = set()

                    while unsent_messages and not batch_aborted:
                        now_ms = time.time() * 1000

                        # Finde die erste Message deren Channel JETZT sendbar ist
                        sendable_idx = None
                        earliest_unblock_ms = None
                        any_selectable = False

                        for idx, (_msg_id, channel_id, _text, _image_path, _attempts) in enumerate(unsent_messages):
                            # P1.3: Channel hatte bereits einen Fehlschlag →
                            # Rest des Batches für diesen Channel skippen.
                            if channel_id in failed_channels:
                                continue
                            any_selectable = True

                            last_ch = last_send_per_channel.get(channel_id, 0.0)
                            ch_ready_at = last_ch + PER_CHANNEL_MIN_INTERVAL_MS
                            global_ready_at = last_global_send_ms + GLOBAL_MIN_INTERVAL_MS
                            ready_at = max(ch_ready_at, global_ready_at)

                            if ready_at <= now_ms:
                                sendable_idx = idx
                                break

                            if earliest_unblock_ms is None or ready_at < earliest_unblock_ms:
                                earliest_unblock_ms = ready_at

                        if not any_selectable:
                            # Nur noch Messages geblockter Channels übrig →
                            # Batch beenden, Retry über den nächsten Poll.
                            break

                        if sendable_idx is None:
                            # Kein Channel frei — warten bis der früheste wieder sendbar ist
                            wait_s = max(0.05, (earliest_unblock_ms - now_ms) / 1000.0)
                            # Cap auf 5s damit wir bei extremem Stau nicht endlos warten
                            # und z.B. neue dringende Messages in den nächsten Batch kommen
                            await asyncio.sleep(min(wait_s, 5.0))
                            continue

                        # Message aus Batch nehmen und senden
                        msg_id, channel_id, text, image_path, attempts = unsent_messages.pop(sendable_idx)

                        # P0.1(a)/P2.10: Row VOR dem Send auf 'sending' setzen
                        # und committen — ein Crash zwischen Send und sent=TRUE
                        # führt dann in den dead_letter statt in einen Re-Send.
                        if not claim_for_sending(cur, msg_id):
                            conn.commit()
                            continue  # anderer Consumer hat die Row übernommen
                        conn.commit()

                        # P2.11: letzter Versuch ohne parse_mode — häufigste
                        # Fehlerquelle sind HTML-Parse-Errors.
                        parse_mode = None if attempts >= MAX_ATTEMPTS - 1 else "HTML"

                        try:
                            if image_path:
                                try:
                                    with open(image_path, 'rb') as photo_file:
                                        await bot.send_photo(
                                            chat_id=channel_id,
                                            photo=photo_file,
                                            caption=text,
                                            parse_mode=parse_mode,
                                        )
                                    logger.info(f"🖼️ Bild-Nachricht {msg_id} an Kanal {channel_id} gesendet.")
                                except FileNotFoundError:
                                    logger.warning(f"⚠️ Bild not found: {image_path}. Sending nur Text.")
                                    await bot.send_message(chat_id=channel_id, text=text, parse_mode=parse_mode)
                            else:
                                await bot.send_message(chat_id=channel_id, text=text, parse_mode=parse_mode)
                                logger.info(f"✅ Text-Nachricht {msg_id} an Kanal {channel_id} gesendet.")

                            # Erfolg: Timestamps updaten
                            now_after = time.time() * 1000
                            last_send_per_channel[channel_id] = now_after
                            last_global_send_ms = now_after

                            mark_sent(cur, msg_id, image_path)
                            conn.commit()

                        except RetryAfter as e:
                            # Telegram Flood-Control – komplette Verarbeitung pausieren,
                            # diese Message NICHT als attempt werten (ist unser Fehler, nicht ihrer).
                            # Outcome ist eindeutig "nicht gesendet" → zurück auf
                            # pending, der nächste Poll nimmt sie wieder mit.
                            wait_time = e.retry_after
                            logger.warning(f"⏳ Flood Control. Waiting {wait_time}s...")
                            cur.execute(
                                "UPDATE telegram_outbox SET status = 'pending', sending_at = NULL WHERE id = %s",
                                (msg_id,),
                            )
                            conn.commit()
                            # Diesen Channel explizit blocken bis RetryAfter abgelaufen ist
                            last_send_per_channel[channel_id] = time.time() * 1000 + wait_time * 1000
                            await asyncio.sleep(wait_time + 1)
                            batch_aborted = True

                        except TimedOut as e:
                            # P0.1(d): TimedOut = Outcome unbekannt — Telegram kann
                            # die Message angenommen haben. Row bleibt 'sending',
                            # KEIN Retry in diesem Pass; recover_stale_sending()
                            # entscheidet nach der Grace-Zeit (Trading → dead_letter,
                            # Info → pending).
                            cur.execute(
                                "UPDATE telegram_outbox SET attempts = attempts + 1, last_error = %s WHERE id = %s",
                                (f"TimedOut (unknown outcome): {e}"[:1000], msg_id),
                            )
                            conn.commit()
                            failed_channels.add(channel_id)  # P1.3: FIFO schützen
                            # Rate-Limit konservativ setzen — die Message kann
                            # angekommen sein.
                            now_after = time.time() * 1000
                            last_send_per_channel[channel_id] = now_after
                            last_global_send_ms = now_after
                            logger.warning(
                                f"⚠️ Msg {msg_id} an Kanal {channel_id} TimedOut — Outcome unbekannt, "
                                f"Row bleibt 'sending' (P0.1)."
                            )

                        except TelegramError as e:
                            error_msg = str(e)

                            # Manche Telegram-Versionen werfen RetryAfter nicht sauber; parsen als Fallback
                            if "Retry in" in error_msg:
                                match = re.search(r'Retry in (\d+)', error_msg)
                                if match:
                                    wait_time = int(match.group(1))
                                    logger.warning(f"⏳ Flood Control (Regex). Waiting {wait_time}s...")
                                    # Zurück auf pending und Channel blocken
                                    cur.execute(
                                        "UPDATE telegram_outbox SET status = 'pending', sending_at = NULL "
                                        "WHERE id = %s",
                                        (msg_id,),
                                    )
                                    conn.commit()
                                    last_send_per_channel[channel_id] = time.time() * 1000 + wait_time * 1000
                                    await asyncio.sleep(wait_time + 1)
                                    batch_aborted = True
                                    continue

                            # "Chat not found" ist dauerhaft – sofort failed markieren
                            if "Chat not found" in error_msg or "chat not found" in error_msg:
                                logger.error(f"❌ Chat {channel_id} not found. Msg {msg_id} → failed.")
                                cur.execute(
                                    "UPDATE telegram_outbox SET failed = TRUE, status = 'failed', "
                                    "last_error = %s WHERE id = %s",
                                    (error_msg[:1000], msg_id),
                                )
                                try_delete_chart(image_path)
                                conn.commit()
                                failed_channels.add(channel_id)  # P1.3
                                continue

                            # Alle anderen Fehler (message too long, bad HTML, image too large, ...)
                            # → attempt zählen. Nach MAX_ATTEMPTS als failed markieren.
                            now_failed = mark_failure(cur, msg_id, error_msg, image_path)
                            conn.commit()
                            failed_channels.add(channel_id)  # P1.3: FIFO schützen
                            if now_failed:
                                # P2.11: endgültiges Verwerfen laut loggen —
                                # core/health_monitor.py alertet auf den failed-Zähler.
                                logger.error(
                                    f"❌ Msg {msg_id} an Kanal {channel_id} nach {MAX_ATTEMPTS} Versuchen "
                                    f"endgültig failed: {error_msg}"
                                )
                            else:
                                logger.warning(f"⚠️ Msg {msg_id} Sendefehler, wird erneut versucht: {error_msg}")

                        except Exception as e:
                            # Unerwarteter Fehler (z.B. Datei-I/O beim Bild) – auch zählen
                            error_msg = str(e)
                            now_failed = mark_failure(cur, msg_id, error_msg, image_path)
                            conn.commit()
                            failed_channels.add(channel_id)  # P1.3: FIFO schützen
                            if now_failed:
                                logger.error(
                                    f"❌ Msg {msg_id} an Kanal {channel_id} final failed (unerwartet): {error_msg}"
                                )
                            else:
                                logger.warning(f"⚠️ Msg {msg_id} Fehler, Retry: {error_msg}")

        except Exception as e:
            logger.error(f"⚠️ Loop-Error: {e}")
        finally:
            if conn:
                conn.close()

        # Idle-Sleep-Strategie:
        # - Leerer Batch → 5s warten (sonst hämmern wir die DB)
        # - Voller Batch → nur minimaler Yield (weitere Messages wahrscheinlich da)
        if batch_was_empty:
            await asyncio.sleep(IDLE_SLEEP_SEC)
        else:
            await asyncio.sleep(0.1)


def main():
    logger.info("=== TELEGRAM BOT ENGINE GESTARTET ===")
    try:
        asyncio.run(process_outbox())
    except KeyboardInterrupt:
        logger.info("🛑 Telegram Bot stopped (Strg+C).")


if __name__ == "__main__":
    main()
