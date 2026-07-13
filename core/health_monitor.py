# core/health_monitor.py
# Leichtgewichtige Betriebsüberwachung — vom Watchdog einmal pro Minute aufgerufen.
#
# Deckt die drei Ausfallklassen ab, die im Audit LIVE belegt wurden:
#   1. Daten-Staleness (P2.47): Ingestion-WS tot bei grünem Watchdog — Kerzen
#      frieren ein, die Fleet handelt auf Stale-Daten (2x passiert).
#   2. CPU-Dauerlast: >90% über Minuten verhungert die WS-Event-Loops →
#      Binance-Disconnects, verpasste Klines.
#   3. Outbox-Failures (P2.11): Signale verschwinden still nach 3 Versuchen
#      (z.B. 225x "Chat not found" am 04.07. nach Token-Rotation).
#
# Alerts gehen an TELEGRAM_ALERT_CHAT_ID (private Chat mit dem Bot — dort
# funktioniert Zustellung auch, wenn Channel-Membership kaputt ist) und immer
# ins watchdog.log. Jeder Alert-Typ ist auf 1x/30min rate-limitiert.
# Kein Check darf je den Watchdog crashen — alles ist defensiv gekapselt.

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

STALE_LIMIT_S = 12 * 60  # BTCUSDT_5m aelter als 12 min = Datenfluss tot
CPU_ALERT_PCT = 90  # Durchschnitt ueber 5 Minuten
OUTBOX_FAIL_LIMIT = 20  # failed-Rows in 15 min
OUTBOX_PENDING_AGE_S = 10 * 60  # aeltestes ungesendetes Signal
ALERT_COOLDOWN_S = 30 * 60
# Auto-Restart bewusst selten: Jeder Ingestion-Restart erzeugt ~30 WS-Connects.
# Bei einer Binance-IP-Drossel (Connect-Churn-Strafe) hält ein 30-min-Restart-
# Takt die Strafe selbst am Leben — 2h lassen sie abklingen; die Ingestion
# heilt stumme Verbindungen inzwischen selbst per Backoff (1_data_ingestion).
INGESTION_RESTART_COOLDOWN_S = 120 * 60

_cpu_samples: deque = deque(maxlen=5)
_last_alert: dict = {}
_last_ingestion_restart = 0.0


def _alert(key: str, msg: str) -> None:
    """Rate-limitierter Alert: immer loggen, zusaetzlich Telegram wenn moeglich."""
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
            logger.warning(f"Health-Alert konnte nicht via Telegram gesendet werden: {e}")


def _check_data_staleness(conn) -> None:
    """P2.47: Kerzen-Frische als Ingestion-/WS-Heartbeat, mit Auto-Heal."""
    global _last_ingestion_restart
    # core.candles: neueste BTCUSDT_5m-open_time, forming candle bewusst inkludiert
    # (ohne sie läse eine frische-aber-formende Kerze als stale und triggerte einen
    # false-positive DATA_STALE-Restart — contract 2: include_forming=True). Das Alter
    # wird in Python aus derselben Wall-Clock abgeleitet, die die DB nutzt (auf dem VPS
    # co-lokiert); der Sub-Sekunden-Unterschied zum DB-seitigen NOW() ist gegen das
    # Minuten-Limit STALE_LIMIT_S irrelevant.
    latest = latest_open_time(conn, "BTCUSDT", "5m", include_forming=True)
    if latest is None:
        return
    age = (datetime.now(timezone.utc) - latest).total_seconds()
    if age > STALE_LIMIT_S:
        _alert(
            "DATA_STALE",
            f"BTCUSDT_5m ist {age / 60:.0f} min alt (Limit {STALE_LIMIT_S // 60} min) — "
            f"Ingestion-WS vermutlich tot. Auto-Restart der Ingestion wird angefordert.",
        )
        now = time.time()
        if now - _last_ingestion_restart > INGESTION_RESTART_COOLDOWN_S:
            _last_ingestion_restart = now
            request_restart("1_data_ingestion.py")
            logger.error("♻️ HEALTH: Restart von 1_data_ingestion.py angefordert (Daten-Staleness).")


def _check_cpu() -> None:
    """CPU-Dauerlast: 5-Minuten-Durchschnitt ueber nicht-blockierende Samples."""
    pct = psutil.cpu_percent(interval=None)  # seit letztem Aufruf, non-blocking
    if pct > 0:  # erster Aufruf liefert 0.0 — verwerfen
        _cpu_samples.append(pct)
    if len(_cpu_samples) == _cpu_samples.maxlen:
        avg = sum(_cpu_samples) / len(_cpu_samples)
        if avg >= CPU_ALERT_PCT:
            _alert(
                "CPU_SATURATED",
                f"CPU-Durchschnitt {avg:.0f}% ueber {len(_cpu_samples)} min "
                f"(Limit {CPU_ALERT_PCT}%) — WS-Disconnect-Gefahr. "
                f"Top-Verdaechtige: Ingestion-Catch-up, Indicator-Engine-Zyklus, Pump-Detector.",
            )


def _check_outbox(conn) -> None:
    """P2.11: stille Sende-Failures + haengender Dispatcher sichtbar machen."""
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
            f"{failed_15m} Outbox-Messages in 15 min fehlgeschlagen "
            f"(letzter Fehler: {letzter_fehler}) — Signale erreichen Cornix/Telegram NICHT.",
        )
    if pending_age and pending_age > OUTBOX_PENDING_AGE_S:
        _alert(
            "OUTBOX_STUCK",
            f"Aeltestes ungesendetes Signal ist {pending_age / 60:.0f} min alt — "
            f"Telegram-Dispatcher haengt oder sendet nicht.",
        )


def run_health_checks() -> None:
    """Einstiegspunkt fuer den Watchdog — darf unter keinen Umstaenden werfen."""
    try:
        _check_cpu()
    except Exception as e:
        logger.warning(f"Health-Check CPU fehlgeschlagen: {e}")

    conn = None
    try:
        from core.database import get_db_connection

        conn = get_db_connection()
        _check_data_staleness(conn)
        _check_outbox(conn)
    except Exception as e:
        logger.warning(f"Health-Check DB fehlgeschlagen: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
