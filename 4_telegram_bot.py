import asyncio
import logging
import os
import re
import time
import warnings

from telegram import Bot
from telegram.error import TelegramError, RetryAfter

# Suppress Pandas warning (in case it appears here)
warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

# --- UNSERE SAUBEREN IMPORTS ---
from core.database import get_db_connection
from core.config import TELEGRAM_BOT_TOKEN

logging.basicConfig(level=logging.INFO, format='%(asctime)s - TELEGRAM_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- KONSTANTEN ---
MAX_ATTEMPTS = 3           # After 3 failed attempts a message is marked as "failed"
FETCH_BATCH_SIZE = 50      # Max Messages pro DB-Roundtrip
IDLE_SLEEP_SEC = 5         # Loop-Delay wenn Outbox leer

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
GLOBAL_MIN_INTERVAL_MS = 50         # ~20 sends/s globally (Telegram allows 30/s)
PER_CHANNEL_MIN_INTERVAL_MS = 3100  # ~19/min per channel (Telegram allows 20/min)


def ensure_schema(conn) -> None:
    """Ensures telegram_outbox has the required columns.

    Additional columns:
    - attempts:   Zähler für Fehlversuche, after MAX_ATTEMPTS wird die Message
                  is marked 'failed' so it does not block the queue.
    - failed:     True when permanently abandoned.
    - last_error: Last error text for debugging.
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
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Migration für bestehende Installation: fehlende Spalten afterziehen
        for col_sql in [
            "ALTER TABLE telegram_outbox ADD COLUMN IF NOT EXISTS attempts INTEGER DEFAULT 0",
            "ALTER TABLE telegram_outbox ADD COLUMN IF NOT EXISTS failed BOOLEAN DEFAULT FALSE",
            "ALTER TABLE telegram_outbox ADD COLUMN IF NOT EXISTS last_error TEXT",
            "ALTER TABLE telegram_outbox ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()",
        ]:
            cur.execute(col_sql)
    conn.commit()


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
    cur.execute("UPDATE telegram_outbox SET sent = TRUE WHERE id = %s", (msg_id,))
    try_delete_chart_if_unreferenced(cur, image_path, msg_id)


def mark_failure(cur, msg_id: int, error: str, image_path: str | None) -> bool:
    """Erhöht attempt-counter. Gibt True zurück wenn Message als failed markiert wurde
    (= max attempts erreicht), damit der Queue nicht blockiert."""
    cur.execute(
        """
        UPDATE telegram_outbox
        SET attempts = attempts + 1,
            last_error = %s,
            failed = CASE WHEN attempts + 1 >= %s THEN TRUE ELSE failed END
        WHERE id = %s
        RETURNING failed
        """,
        (error[:1000], MAX_ATTEMPTS, msg_id),
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
    """
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    logger.info("🤖 Bot erfolgreich initialisiert. Überwache Outbox mit Retry-Limit...")

    # Schema einmal beim Start absichern
    with get_db_connection() as init_conn:
        ensure_schema(init_conn)

    # Rate-Limit-State (prozesslokal, überlebt Loop-Iterationen):
    #   channel_id -> letzter Send-Timestamp in ms
    last_send_per_channel: dict[int, float] = {}
    last_global_send_ms: float = 0.0

    while True:
        conn = None
        batch_was_empty = False
        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                # Nur nicht-gesendete und nicht-failed Messages holen, FIFO after ID
                # (damit älteste Nachrichten Vorrang haben vor neuen)
                cur.execute(
                    """
                    SELECT id, channel_id, message, image_path
                    FROM telegram_outbox
                    WHERE sent = FALSE AND failed = FALSE
                    ORDER BY id ASC
                    LIMIT %s
                    """,
                    (FETCH_BATCH_SIZE,),
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

                    while unsent_messages and not batch_aborted:
                        now_ms = time.time() * 1000

                        # Finde die erste Message deren Channel JETZT sendbar ist
                        sendable_idx = None
                        earliest_unblock_ms = None

                        for idx, (msg_id, channel_id, text, image_path) in enumerate(unsent_messages):
                            last_ch = last_send_per_channel.get(channel_id, 0.0)
                            ch_ready_at = last_ch + PER_CHANNEL_MIN_INTERVAL_MS
                            global_ready_at = last_global_send_ms + GLOBAL_MIN_INTERVAL_MS
                            ready_at = max(ch_ready_at, global_ready_at)

                            if ready_at <= now_ms:
                                sendable_idx = idx
                                break

                            if earliest_unblock_ms is None or ready_at < earliest_unblock_ms:
                                earliest_unblock_ms = ready_at

                        if sendable_idx is None:
                            # Kein Channel frei — warten bis der früheste wieder sendbar ist
                            wait_s = max(0.05, (earliest_unblock_ms - now_ms) / 1000.0)
                            # Cap auf 5s damit wir bei extremem Stau nicht endlos warten
                            # und z.B. neue dringende Messages in den nächsten Batch kommen
                            await asyncio.sleep(min(wait_s, 5.0))
                            continue

                        # Message aus Batch nehmen und senden
                        msg_id, channel_id, text, image_path = unsent_messages.pop(sendable_idx)

                        try:
                            if image_path:
                                try:
                                    with open(image_path, 'rb') as photo_file:
                                        await bot.send_photo(
                                            chat_id=channel_id,
                                            photo=photo_file,
                                            caption=text,
                                            parse_mode="HTML",
                                        )
                                    logger.info(f"🖼️ Bild-Nachricht {msg_id} an Kanal {channel_id} gesendet.")
                                except FileNotFoundError:
                                    logger.warning(f"⚠️ Bild not found: {image_path}. Sending nur Text.")
                                    await bot.send_message(
                                        chat_id=channel_id, text=text, parse_mode="HTML"
                                    )
                            else:
                                await bot.send_message(
                                    chat_id=channel_id, text=text, parse_mode="HTML"
                                )
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
                            # Zurück in den Batch legen damit sie beim nächsten Versuch wieder dran ist.
                            wait_time = e.retry_after
                            logger.warning(f"⏳ Flood Control. Waiting {wait_time}s...")
                            unsent_messages.insert(sendable_idx, [msg_id, channel_id, text, image_path])
                            # Diesen Channel explizit blocken bis RetryAfter abgelaufen ist
                            last_send_per_channel[channel_id] = time.time() * 1000 + wait_time * 1000
                            await asyncio.sleep(wait_time + 1)
                            batch_aborted = True

                        except TelegramError as e:
                            error_msg = str(e)

                            # Manche Telegram-Versionen werfen RetryAfter nicht sauber; parsen als Fallback
                            if "Retry in" in error_msg:
                                match = re.search(r'Retry in (\d+)', error_msg)
                                if match:
                                    wait_time = int(match.group(1))
                                    logger.warning(f"⏳ Flood Control (Regex). Waiting {wait_time}s...")
                                    # Zurücklegen und Channel blocken
                                    unsent_messages.insert(sendable_idx, [msg_id, channel_id, text, image_path])
                                    last_send_per_channel[channel_id] = time.time() * 1000 + wait_time * 1000
                                    await asyncio.sleep(wait_time + 1)
                                    batch_aborted = True
                                    continue

                            # "Chat not found" ist dauerhaft – sofort failed markieren
                            if "Chat not found" in error_msg or "chat not found" in error_msg:
                                logger.error(f"❌ Chat {channel_id} not found. Msg {msg_id} → failed.")
                                cur.execute(
                                    "UPDATE telegram_outbox SET failed = TRUE, last_error = %s "
                                    "WHERE id = %s",
                                    (error_msg[:1000], msg_id),
                                )
                                try_delete_chart(image_path)
                                conn.commit()
                                continue

                            # Alle anderen Fehler (message too long, bad HTML, image too large, ...)
                            # → attempt zählen. Nach MAX_ATTEMPTS als failed markieren.
                            now_failed = mark_failure(cur, msg_id, error_msg, image_path)
                            conn.commit()
                            if now_failed:
                                logger.error(
                                    f"❌ Msg {msg_id} after {MAX_ATTEMPTS} Versuchen endgültig failed: {error_msg}"
                                )
                            else:
                                logger.warning(f"⚠️ Msg {msg_id} Sendefehler, wird erneut versucht: {error_msg}")

                        except Exception as e:
                            # Unerwarteter Fehler (z.B. Datei-I/O beim Bild) – auch zählen
                            error_msg = str(e)
                            now_failed = mark_failure(cur, msg_id, error_msg, image_path)
                            conn.commit()
                            if now_failed:
                                logger.error(f"❌ Msg {msg_id} final failed (unerwartet): {error_msg}")
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
