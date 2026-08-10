import asyncio
import logging
import os
import re
import time
import warnings

from telegram import Bot
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut

# Suppress Pandas warning (in case it appears here)
warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

# --- OUR CLEAN IMPORTS ---
from core.config import CH_ATB_INFO, REGIME_STATUS_CHANNEL_ID, TELEGRAM_BOT_TOKEN
from core.database import get_db_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - TELEGRAM_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- CONSTANTS ---
MAX_ATTEMPTS = 3  # After 3 failed attempts a message is marked as "failed"

#: BadRequest reasons no retry can ever fix, matched case-insensitively against
#: the error text — python-telegram-bot exposes no machine-readable code for them.
#:
#: Deliberately an ALLOWLIST and not "every BadRequest": a malformed-HTML
#: rejection ("Can't parse entities …") is genuinely retryable here, because
#: P2.11 drops ``parse_mode`` on the last attempt and that recovery path must
#: survive. Terminating on every 400 would be the same misclassification as
#: today's, only in the other direction.
#:
#: Evidenced in ``telegram_outbox`` at the time of writing: "Message is too long"
#: 492 rows, "Chat not found" 428. "Message text is empty" was NOT observed and is
#: listed only because it is unambiguously permanent. The retryable counterpart
#: sits right next to them in the same table — "Can't parse entities" with 61 rows,
#: which is exactly what P2.11 recovers from and what this list must never swallow.
#: No caption variant ("caption is too long") appears in the data, so none is
#: guessed at here; add one when it shows up, not before.
PERMANENT_BAD_REQUEST_REASONS = (
    "message is too long",
    "message text is empty",
    "chat not found",
)
FETCH_BATCH_SIZE = 50  # Max messages per DB roundtrip
IDLE_SLEEP_SEC = 5  # Loop delay when outbox is empty

# P1.1: Trading signals age quickly — after downtime, no hour-old entries should
# go out at long-past prices. Info channels have no TTL.
SIGNAL_TTL_MINUTES = 15

# P0.1: 'sending' rows standing longer than this grace period are considered
# orphaned (crash or TimedOut with unknown outcome) and are resolved by
# recover_stale_sending(). Grace > longest realistic send (photo upload +
# Telegram timeout) so that our own in-flight send is not caught.
SENDING_RECOVERY_GRACE_SEC = 120

# P0.1/P1.1: Pure info channels — resend after crash and stale messages are
# harmless there. Everything else (REGIME_TRADING_CHANNEL_ID, all bot signal
# channels, ...) is conservatively treated as a trading channel: false positive
# costs only one lost message, false negative a double trade at Cornix.
INFO_CHANNEL_IDS = frozenset(cid for cid in (REGIME_STATUS_CHANNEL_ID, CH_ATB_INFO) if cid)

# Rate limiting (replaces old fixed ANTI_SPAM_SLEEP_SEC=1):
#
# Telegram limits (official):
#   - 30 messages/second globally per bot
#   - 20 messages/minute per channel/group (= 1 every 3s)
#
# We stay below both limits with a safe buffer, combined with
# intelligent message selection: instead of strict FIFO, pick the next sendable
# message from the batch. This way a channel backlog does not stall
# other channels.
GLOBAL_MIN_INTERVAL_MS = 50  # ~20 sends/s globally (Telegram allows 30/s)
PER_CHANNEL_MIN_INTERVAL_MS = 3100  # ~19/min per channel (Telegram allows 20/min)


def is_trading_channel(channel_id) -> bool:
    """True if real trading can happen on the channel (Cornix reads along).

    Conservative: everything not explicitly known as an info channel is
    a trading channel (P0.1).
    """
    return channel_id not in INFO_CHANNEL_IDS


def ensure_schema(conn) -> None:
    """Ensures telegram_outbox has the required columns.

    Additional columns:
    - attempts:   counter for failed attempts, after MAX_ATTEMPTS the message
                  is marked 'failed' so it does not block the queue.
    - failed:     True when permanently abandoned.
    - last_error: Last error text for debugging.
    - status:     P0.1 — state machine 'pending' → 'sending' → 'sent' or
                  'failed'/'expired'/'dead_letter'. sent/failed remain as
                  booleans for all other readers (housekeeping, health monitor)
                  maintained in parallel.
    - sending_at: P0.1 — when the row went to 'sending' (for orphaned
                  detection in recover_stale_sending).
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
        # Migration for existing installation: add missing columns
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
    """P1.1: Do not send trading messages older than SIGNAL_TTL_MINUTES.

    After downtime, those would be signals at long-past prices — dangerous
    on Cornix channels. Once per poll, with count log.
    Info channels have no TTL.
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
                f"expired: older than {SIGNAL_TTL_MINUTES} min (P1.1)",
                SIGNAL_TTL_MINUTES,
                # Empty list would not be typed by psycopg2 → dummy 0
                list(INFO_CHANNEL_IDS) or [0],
            ),
        )
        expired = cur.rowcount
    conn.commit()
    if expired:
        logger.warning(f"⏰ {expired} trading messages older than {SIGNAL_TTL_MINUTES} min marked as 'expired' (P1.1).")


def recover_stale_sending(conn, min_age_sec: int) -> None:
    """P0.1(c): resolve stuck 'sending' rows (unknown outcome).

    Trading channels: NEVER auto-resend — the send may have gone through,
    a resend would mean Cornix opens the trade twice → dead_letter + WARNING
    (the operator decides manually).
    Info channels: resend is harmless → back to 'pending' (or finally 'failed'
    if attempts are exhausted).

    min_age_sec=0 at process start (no send can still be in-flight), otherwise
    SENDING_RECOVERY_GRACE_SEC so our own in-flight send is not caught.
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
                    f"☠️ Msg {msg_id} (trading channel {channel_id}) was on 'sending' with unknown "
                    f"outcome → dead_letter, NO auto-resend (P0.1)."
                )
            elif attempts >= MAX_ATTEMPTS:
                cur.execute(
                    "UPDATE telegram_outbox SET failed = TRUE, status = 'failed' WHERE id = %s",
                    (msg_id,),
                )
                logger.error(f"❌ Msg {msg_id} to channel {channel_id} after {MAX_ATTEMPTS} attempts finally failed.")
            else:
                cur.execute(
                    "UPDATE telegram_outbox SET status = 'pending', sending_at = NULL WHERE id = %s",
                    (msg_id,),
                )
    conn.commit()


def claim_for_sending(cur, msg_id: int) -> bool:
    """P0.1(a)/P2.10: atomically claim row from pending to 'sending'.

    FOR UPDATE SKIP LOCKED only for this status transition — a second
    consumer skips the row instead of sending it twice. The caller commits
    IMMEDIATELY after (BEFORE the send) so a crash after the send cannot
    produce a re-send.
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
    """Deletes the chart after successful sending to prevent disk filling.

    Chart deletion is ignored if the file does not exist — this happens
    normally in race conditions and is no error.
    """
    if not image_path:
        return
    try:
        if os.path.isfile(image_path):
            os.remove(image_path)
    except Exception as e:
        logger.debug(f"Could not delete chart {image_path}: {e}")


def try_delete_chart_if_unreferenced(cur, image_path: str | None, current_msg_id: int) -> None:
    """FIX (#68/#87): delete chart only if NO other unsent outbox entry
    references the same path.

    Previously, the chart was deleted immediately after the first successful
    send. If two bots wrote the same chart path to the outbox (e.g. because
    the same pattern was logged from different perspectives), the second send
    failed with FileNotFoundError — and fell back to text-only even though
    the message should have had a chart.
    """
    if not image_path:
        return
    try:
        # Are there other unsent outbox entries with exactly this image_path?
        cur.execute(
            "SELECT 1 FROM telegram_outbox WHERE image_path = %s AND sent = FALSE AND id != %s LIMIT 1",
            (image_path, current_msg_id),
        )
        if cur.fetchone() is not None:
            # Other unsent message still needs the file → do not delete
            return
        # No other references → safe to delete
        if os.path.isfile(image_path):
            os.remove(image_path)
    except Exception as e:
        logger.debug(f"Could not delete chart {image_path}: {e}")


def mark_sent(cur, msg_id: int, image_path: str | None) -> None:
    """Marks message as sent and deletes the chart only if no other unsent
    entries still need the file."""
    cur.execute("UPDATE telegram_outbox SET sent = TRUE, status = 'sent' WHERE id = %s", (msg_id,))
    try_delete_chart_if_unreferenced(cur, image_path, msg_id)


def is_permanent_bad_request(reason: str) -> bool:
    """True if a BadRequest text is on the permanent allowlist — pure, no IO."""
    lowered = (reason or "").lower()
    return any(marker in lowered for marker in PERMANENT_BAD_REQUEST_REASONS)


def mark_failed_permanently(cur, msg_id: int, error: str, image_path: str | None) -> None:
    """Terminal failure on the FIRST attempt, for errors no retry can fix.

    ``mark_failure`` only gives up after ``MAX_ATTEMPTS`` — right for an unknown
    or transient outcome. A permanently rejected message has a KNOWN outcome
    (Telegram refused it), so further attempts buy nothing and cost a send slot
    each, plus a ``failed_channels`` entry that stalls the channel for the rest
    of the poll cycle.
    """
    cur.execute(
        """
        UPDATE telegram_outbox
        SET attempts = attempts + 1,
            last_error = %s,
            failed = TRUE,
            status = 'failed',
            sending_at = NULL
        WHERE id = %s
        """,
        (error[:1000], msg_id),
    )
    try_delete_chart_if_unreferenced(cur, image_path, msg_id)


def mark_failure(cur, msg_id: int, error: str, image_path: str | None) -> bool:
    """Increments attempt counter. Returns True if message was marked as failed
    (= max attempts reached) to prevent queue blocking.

    P0.1: for a non-final, clearly failed send (exception with known outcome)
    the row goes back to 'pending' — resend is safe here because Telegram
    definitely did not accept the message.
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
        # Message will never be retried – clean up chart (but only if
        # not referenced by other entries)
        try_delete_chart_if_unreferenced(cur, image_path, msg_id)
    return now_failed


async def process_outbox():
    """Endless loop: polls DB, sends messages with flood control and retry limit.

    Message selection strategy (revised):
    Instead of going through the batch blindly FIFO (which would propagate
    a channel backlog to other channels), each iteration picks the "next
    sendable" message — the first in FIFO order whose channel is not
    currently blocked by the per-channel rate-limit throttle.

    If all messages in the batch belong to blocked channels, the worker waits
    until the earliest channel is free again (not longer).

    FIFO ordering is preserved per channel (important for semantically
    sequential messages like "signal" → "update"); only between channels
    can order be swapped, which is harmless.

    P0.1 (at-most-once for trading channels): every row is committed to
    'sending' before the send via claim_for_sending(). Only clearly failed
    sends go back to 'pending'; TimedOut (outcome unknown) stays 'sending'
    and is resolved by recover_stale_sending() — trading channels land in
    dead_letter instead of an auto-resend.
    """
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    logger.info("🤖 Bot successfully initialised. Monitoring outbox with retry limit...")

    # Ensure schema once at start; then resolve orphaned 'sending' rows from
    # a previous process run (P0.1(c), min_age=0: no send can still be
    # in-flight).
    with get_db_connection() as init_conn:
        ensure_schema(init_conn)
        recover_stale_sending(init_conn, 0)

    # Rate-limit state (process-local, survives loop iterations):
    #   channel_id -> last send timestamp in ms
    last_send_per_channel: dict[int, float] = {}
    last_global_send_ms: float = 0.0

    while True:
        conn = None
        batch_was_empty = False
        try:
            conn = get_db_connection()

            # P1.1: expire stale trading signals once per poll.
            expire_stale_signals(conn)
            # P0.1(c/d): resolve orphaned 'sending' rows (crash or TimedOut)
            # after grace period — trading → dead_letter, info → pending.
            recover_stale_sending(conn, SENDING_RECOVERY_GRACE_SEC)

            with conn.cursor() as cur:
                # Fetch only pending messages, FIFO by ID (oldest first).
                # P1.1: trading messages only within the TTL window.
                # P0.1(d): channels with open 'sending' row (unknown outcome)
                # are completely locked so message n+1 (e.g. SL update) does
                # not go out before n (entry) — until recover_stale_sending
                # resolves the row.
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
                # Convert to list so we can remove entries
                unsent_messages = [list(row) for row in cur.fetchall()]

                if not unsent_messages:
                    # No work — we leave the with block cleanly and do the
                    # idle sleep afterwards in the finally section.
                    # IMPORTANT: no conn.close() here — finally takes care of that,
                    # and closing in the middle of the with cursor block leads to
                    # "connection already closed" errors on exit.
                    batch_was_empty = True
                else:
                    # === Intelligent send loop ===
                    # We work until either the batch is empty or a RetryAfter/
                    # flood control aborts the batch
                    batch_aborted = False
                    # P1.3: channels with send error in this batch — their
                    # remaining messages are skipped so the per-channel FIFO
                    # order does not break (SL update must not go before its
                    # entry).
                    failed_channels: set[int] = set()

                    while unsent_messages and not batch_aborted:
                        now_ms = time.time() * 1000

                        # Finde die erste Message deren Channel JETZT sendbar ist
                        sendable_idx = None
                        earliest_unblock_ms = None
                        any_selectable = False

                        for idx, (_msg_id, channel_id, _text, _image_path, _attempts) in enumerate(unsent_messages):
                            # P1.3: channel already had a failure → skip the
                            # rest of the batch for this channel.
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
                            # Only messages from blocked channels left →
                            # end batch, retry on next poll.
                            break

                        if sendable_idx is None:
                            # No channel free — wait until the earliest is sendable again
                            wait_s = max(0.05, (earliest_unblock_ms - now_ms) / 1000.0)
                            # Cap at 5s so we don't wait forever in extreme congestion
                            # and e.g. new urgent messages can come in the next batch
                            await asyncio.sleep(min(wait_s, 5.0))
                            continue

                        # Remove message from batch and send
                        msg_id, channel_id, text, image_path, attempts = unsent_messages.pop(sendable_idx)

                        # P0.1(a)/P2.10: set row to 'sending' before the send
                        # and commit — a crash between send and sent=TRUE then
                        # leads to dead_letter instead of a re-send.
                        if not claim_for_sending(cur, msg_id):
                            conn.commit()
                            continue  # another consumer claimed the row
                        conn.commit()

                        # P2.11: last attempt without parse_mode — most common
                        # error source is HTML parse errors.
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
                                    logger.info(f"🖼️ Image message {msg_id} sent to channel {channel_id}.")
                                except FileNotFoundError:
                                    logger.warning(f"⚠️ Image not found: {image_path}. Sending text only.")
                                    await bot.send_message(chat_id=channel_id, text=text, parse_mode=parse_mode)
                            else:
                                await bot.send_message(chat_id=channel_id, text=text, parse_mode=parse_mode)
                                logger.info(f"✅ Text message {msg_id} sent to channel {channel_id}.")

                            # Success: update timestamps
                            now_after = time.time() * 1000
                            last_send_per_channel[channel_id] = now_after
                            last_global_send_ms = now_after

                            mark_sent(cur, msg_id, image_path)
                            conn.commit()

                        except RetryAfter as e:
                            # Telegram flood control – pause complete processing,
                            # do NOT count this message as an attempt (is our error, not theirs).
                            # Outcome is clearly "not sent" → back to pending,
                            # the next poll takes it again.
                            wait_time = e.retry_after
                            logger.warning(f"⏳ Flood control. Waiting {wait_time}s...")
                            cur.execute(
                                "UPDATE telegram_outbox SET status = 'pending', sending_at = NULL WHERE id = %s",
                                (msg_id,),
                            )
                            conn.commit()
                            # Explicitly block this channel until RetryAfter expires
                            last_send_per_channel[channel_id] = time.time() * 1000 + wait_time * 1000
                            await asyncio.sleep(wait_time + 1)
                            batch_aborted = True

                        except TimedOut as e:
                            # P0.1(d): TimedOut = outcome unknown — Telegram may have
                            # accepted the message. Row stays 'sending', NO retry
                            # in this pass; recover_stale_sending() decides after
                            # grace period (trading → dead_letter, info → pending).
                            cur.execute(
                                "UPDATE telegram_outbox SET attempts = attempts + 1, last_error = %s WHERE id = %s",
                                (f"TimedOut (unknown outcome): {e}"[:1000], msg_id),
                            )
                            conn.commit()
                            failed_channels.add(channel_id)  # P1.3: protect FIFO
                            # Set rate limit conservatively — the message may
                            # have arrived.
                            now_after = time.time() * 1000
                            last_send_per_channel[channel_id] = now_after
                            last_global_send_ms = now_after
                            logger.warning(
                                f"⚠️ Msg {msg_id} to channel {channel_id} TimedOut — outcome unknown, "
                                f"row stays 'sending' (P0.1)."
                            )

                        except NetworkError as e:
                            # PERMANENT 400s first. In python-telegram-bot
                            # BadRequest is a NetworkError SUBCLASS (22.5:
                            # BadRequest -> NetworkError -> TelegramError), so
                            # this clause already catches them and the terminal
                            # `except TelegramError` below never sees one. Before
                            # this check every 400 was treated as "unknown
                            # outcome" and retried MAX_ATTEMPTS times; for a
                            # permanently undeliverable message each retry also
                            # entered failed_channels — stalling that channel for
                            # the rest of the poll cycle — and burned a global
                            # send slot, delaying OTHER channels' messages toward
                            # the P1.1 15-minute expiry.
                            #
                            # Handled here rather than in an earlier `except
                            # BadRequest` clause on purpose: a non-permanent 400
                            # must fall through to the unknown-outcome path, and
                            # a bare `raise` in a sibling clause would leave the
                            # try block entirely instead of reaching it.
                            if isinstance(e, BadRequest) and is_permanent_bad_request(str(e)):
                                mark_failed_permanently(
                                    cur, msg_id, f"BadRequest (permanent): {e}", image_path
                                )
                                conn.commit()
                                # The request DID reach Telegram and was refused,
                                # so the rate-limit budget is honestly spent —
                                # once, not three times. The channel is NOT
                                # blocked: nothing about this message says the
                                # next one will fail.
                                now_after = time.time() * 1000
                                last_send_per_channel[channel_id] = now_after
                                last_global_send_ms = now_after
                                logger.error(
                                    f"❌ Msg {msg_id} to channel {channel_id} permanently "
                                    f"rejected ({e}) — failed without retry."
                                )
                                continue

                            # Review hardening P0.1(d): non-TimedOut transport errors
                            # (httpx ReadError/RemoteProtocolError = connection reset AFTER
                            # possible request receipt at Telegram) are also UNKNOWN
                            # outcome — same treatment as TimedOut: row stays 'sending',
                            # recover_stale_sending() decides.
                            cur.execute(
                                "UPDATE telegram_outbox SET attempts = attempts + 1, last_error = %s WHERE id = %s",
                                (f"NetworkError (unknown outcome): {e}"[:1000], msg_id),
                            )
                            conn.commit()
                            failed_channels.add(channel_id)  # P1.3: protect FIFO
                            now_after = time.time() * 1000
                            last_send_per_channel[channel_id] = now_after
                            last_global_send_ms = now_after
                            logger.warning(
                                f"⚠️ Msg {msg_id} to channel {channel_id} NetworkError — outcome unknown, "
                                f"row stays 'sending' (P0.1)."
                            )

                        except TelegramError as e:
                            error_msg = str(e)

                            # Some Telegram versions don't throw RetryAfter cleanly; parse as fallback
                            if "Retry in" in error_msg:
                                match = re.search(r'Retry in (\d+)', error_msg)
                                if match:
                                    wait_time = int(match.group(1))
                                    logger.warning(f"⏳ Flood control (regex). Waiting {wait_time}s...")
                                    # Back to pending and block channel
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

                            # "Chat not found" is permanent – mark failed immediately
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

                            # All other errors (message too long, bad HTML, image too large, ...)
                            # → count attempt. After MAX_ATTEMPTS mark as failed.
                            now_failed = mark_failure(cur, msg_id, error_msg, image_path)
                            conn.commit()
                            failed_channels.add(channel_id)  # P1.3: protect FIFO
                            if now_failed:
                                # P2.11: log permanent rejection loudly —
                                # core/health_monitor.py alerts on the failed counter.
                                logger.error(
                                    f"❌ Msg {msg_id} to channel {channel_id} after {MAX_ATTEMPTS} attempts "
                                    f"finally failed: {error_msg}"
                                )
                            else:
                                logger.warning(f"⚠️ Msg {msg_id} send error, will retry: {error_msg}")

                        except Exception as e:
                            # Unexpected error (e.g. file I/O with image) – count it too
                            error_msg = str(e)
                            now_failed = mark_failure(cur, msg_id, error_msg, image_path)
                            conn.commit()
                            failed_channels.add(channel_id)  # P1.3: protect FIFO
                            if now_failed:
                                logger.error(
                                    f"❌ Msg {msg_id} to channel {channel_id} final failed (unexpected): {error_msg}"
                                )
                            else:
                                logger.warning(f"⚠️ Msg {msg_id} error, retry: {error_msg}")

        except Exception as e:
            logger.error(f"⚠️ Loop error: {e}")
        finally:
            if conn:
                conn.close()

        # Idle sleep strategy:
        # - empty batch → wait 5s (otherwise we hammer the DB)
        # - full batch → minimal yield only (more messages likely)
        if batch_was_empty:
            await asyncio.sleep(IDLE_SLEEP_SEC)
        else:
            await asyncio.sleep(0.1)


def main():
    logger.info("=== TELEGRAM BOT ENGINE STARTED ===")
    try:
        asyncio.run(process_outbox())
    except KeyboardInterrupt:
        logger.info("🛑 Telegram bot stopped (Ctrl+C).")


if __name__ == "__main__":
    main()
