# core/health_monitor.py
# Lightweight operational monitoring — invoked by the watchdog once per minute.
#
# Covers the three failure classes proven LIVE in the audit:
#   1. Data staleness (P2.47): ingestion WS dead while the watchdog is green — candles
#      freeze, the fleet trades on stale data (happened 2x).
#   2. Sustained CPU load: >90% for minutes starves the WS event loops →
#      Binance disconnects, missed klines.
#   3. Outbox failures (P2.11): signals silently disappear after 3 attempts
#      (e.g. 225x "Chat not found" on 04.07 after token rotation).
#
# Alerts go to TELEGRAM_ALERT_CHAT_ID (private chat with the bot — delivery
# still works there even if channel membership is broken) and always
# to watchdog.log. Each alert type is rate-limited to 1x/30min.
# No check may ever crash the watchdog — everything is defensively wrapped.

import logging
import os
import time
from collections import deque
from datetime import datetime, timezone

import psutil
import requests

from core.candles import latest_open_time
from core.process_control import request_restart

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALERT_CHAT_ID = os.getenv("TELEGRAM_ALERT_CHAT_ID", "")

STALE_LIMIT_S = 12 * 60  # BTCUSDT_5m older than 12 min = data flow dead
CPU_ALERT_PCT = 90  # average over 5 minutes
OUTBOX_FAIL_LIMIT = 20  # failed rows in 15 min
OUTBOX_PENDING_AGE_S = 10 * 60  # oldest unsent signal
ALERT_COOLDOWN_S = 30 * 60
# Auto-restart deliberately rare: each ingestion restart creates ~30 WS connects.
# Under a Binance IP throttle (connect-churn penalty), a 30-min restart
# cadence keeps the penalty alive itself — 2h lets it decay; ingestion
# now heals silent connections itself via backoff (1_data_ingestion).
INGESTION_RESTART_COOLDOWN_S = 120 * 60

_cpu_samples: deque = deque(maxlen=5)
_last_alert: dict = {}
_last_ingestion_restart = 0.0


def _alert(key: str, msg: str) -> None:
    """Rate-limited alert: always log, additionally Telegram if possible."""
    now = time.time()
    if now - _last_alert.get(key, 0) < ALERT_COOLDOWN_S:
        return
    _last_alert[key] = now
    logger.error(f"🚨 HEALTH [{key}]: {msg}")
    if BOT_TOKEN and ALERT_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": ALERT_CHAT_ID, "text": f"🚨 KYTHERA HEALTH [{key}]\n{msg}"},
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"Health alert could not be sent via Telegram: {e}")


def _check_data_staleness(conn) -> None:
    """P2.47: candle freshness as ingestion/WS heartbeat, with auto-heal."""
    global _last_ingestion_restart
    # core.candles: latest BTCUSDT_5m open_time, forming candle deliberately included
    # (without it, a fresh-but-forming candle would read as stale and trigger a
    # false-positive DATA_STALE restart — contract 2: include_forming=True). The age
    # is derived in Python from the same wall clock the DB uses (co-located
    # on the VPS); the sub-second difference to the DB-side NOW() is irrelevant
    # against the minute-scale limit STALE_LIMIT_S.
    latest = latest_open_time(conn, "BTCUSDT", "5m", include_forming=True)
    if latest is None:
        return
    age = (datetime.now(timezone.utc) - latest).total_seconds()
    if age > STALE_LIMIT_S:
        _alert(
            "DATA_STALE",
            f"BTCUSDT_5m is {age / 60:.0f} min old (limit {STALE_LIMIT_S // 60} min) — "
            f"ingestion WS presumably dead. Requesting auto-restart of ingestion.",
        )
        now = time.time()
        if now - _last_ingestion_restart > INGESTION_RESTART_COOLDOWN_S:
            _last_ingestion_restart = now
            request_restart("1_data_ingestion.py")
            logger.error("♻️ HEALTH: restart of 1_data_ingestion.py requested (data staleness).")


def _check_cpu() -> None:
    """Sustained CPU load: 5-minute average over non-blocking samples."""
    pct = psutil.cpu_percent(interval=None)  # since last call, non-blocking
    if pct > 0:  # first call returns 0.0 — discard
        _cpu_samples.append(pct)
    if len(_cpu_samples) == _cpu_samples.maxlen:
        avg = sum(_cpu_samples) / len(_cpu_samples)
        if avg >= CPU_ALERT_PCT:
            _alert(
                "CPU_SATURATED",
                f"CPU average {avg:.0f}% over {len(_cpu_samples)} min "
                f"(limit {CPU_ALERT_PCT}%) — WS disconnect risk. "
                f"Top suspects: ingestion catch-up, indicator engine cycle, pump detector.",
            )


def _check_outbox(conn) -> None:
    """P2.11: make silent send failures + a hanging dispatcher visible."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                count(*) FILTER (WHERE failed AND created_at > NOW() - INTERVAL '15 min') AS failed_15m,
                (SELECT last_error FROM telegram_outbox
                  WHERE failed AND created_at > NOW() - INTERVAL '15 min'
                  ORDER BY id DESC LIMIT 1) AS letzter_fehler,
                EXTRACT(EPOCH FROM (NOW() - min(created_at)
                    FILTER (WHERE NOT sent AND NOT failed))) AS aeltestes_pending_s
            FROM telegram_outbox
            """
        )
        failed_15m, letzter_fehler, pending_age = cur.fetchone()
    if failed_15m and failed_15m >= OUTBOX_FAIL_LIMIT:
        _alert(
            "OUTBOX_FAILING",
            f"{failed_15m} outbox messages failed in 15 min "
            f"(last error: {letzter_fehler}) — signals are NOT reaching Cornix/Telegram.",
        )
    if pending_age and pending_age > OUTBOX_PENDING_AGE_S:
        _alert(
            "OUTBOX_STUCK",
            f"Oldest unsent signal is {pending_age / 60:.0f} min old — Telegram dispatcher is hanging or not sending.",
        )


def run_health_checks() -> None:
    """Entry point for the watchdog — must never throw under any circumstances."""
    try:
        _check_cpu()
    except Exception as e:
        logger.warning(f"Health check CPU failed: {e}")

    conn = None
    try:
        from core.database import get_db_connection

        conn = get_db_connection()
        _check_data_staleness(conn)
        _check_outbox(conn)
    except Exception as e:
        logger.warning(f"Health check DB failed: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
