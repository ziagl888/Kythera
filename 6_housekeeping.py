import datetime
import hashlib
import hmac
import json
import logging
import os
import time
import warnings

import requests

warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

# --- IMPORT CONFIGURATION FROM CORE ---
from core.coins import looks_like_usdt_perp, refresh_coins_json
from core.config import (
    BASE_URL,
    BINANCE_API_KEY,
    BINANCE_SECRET,
    PUMP_EVENT_MIN_ABS_PCHG_60S,
    PUMP_EVENT_MIN_VOL_RATIO,
    TIMEFRAMES,
)
from core.database import get_db_connection
from core.http_retry import MinIntervalThrottle, RetryBudget, backoff_seconds

logging.basicConfig(level=logging.INFO, format='%(asctime)s - HOUSEKEEPING - %(message)s')
logger = logging.getLogger(__name__)


def update_coins_json():
    """Fetches the latest active USDT perpetual futures from Binance and updates the file.

    Filter + atomic write live in ``core.coins`` (the single coins.json writer,
    P2.16) — shared with ``1_data_ingestion.update_trading_pairs`` so the two
    can no longer drift apart. On a refresh failure the live coins.json is left
    untouched (no truncation) and table creation is skipped for this run.
    """
    logger.info("🔄 Updating coins.json von Binance...")
    try:
        symbols = refresh_coins_json(BASE_URL, 'coins.json')
    except Exception as e:
        logger.error(f"❌ Error updating coins.json: {e}")
        return

    if not symbols:
        logger.warning("⚠️ No USDT perpetual coins returned from Binance!")
        return

    logger.info(f"✅ coins.json updated successfully. {len(symbols)} active coins found.")

    # Create tables for new coins immediately
    if symbols:
        logger.info("Checking Tabellenstruktur für alle Coins...")
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                for symbol in symbols:
                    for tf in TIMEFRAMES:
                        tablename = f'"{symbol}_{tf}"'
                        # Wir feuern das Create einfach ab, Postgres ignoriert es wenn existiert (sehr schnell)
                        cur.execute(f"""
                                CREATE TABLE IF NOT EXISTS {tablename} (
                                    symbol TEXT, open_time TIMESTAMP WITH TIME ZONE,
                                    open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
                                    close DOUBLE PRECISION, volume DOUBLE PRECISION,
                                    PRIMARY KEY (symbol, open_time)
                                );
                            """)
            conn.commit()
            logger.info("✅ Table structure checked/updated successfully.")
        except Exception as e:
            logger.error(f"Init Error: {e}")
            conn.rollback()
        finally:
            conn.close()


def cleanup_delisted_trades():
    """Schließt offene Trades auf Coins die nicht mehr in coins.json sind.

    Hintergrund: Wenn Binance einen Coin delisted, kommen keine neuen
    Candles mehr über die Ingestion. Der interne Monitor würde den Trade
    dann auf ewig offen lassen (weil SL/TP nie erreicht wird). Das
    verzerrt die Performance-Statistik massiv — Trades ohne Ende zählen
    als "noch offen" statt als neutrale Closes.

    Lösung: Beim täglichen Housekeeping (oder manuell) prüfen welche
    Coins aus coins.json verschwunden sind, und für diese alle offenen
    Trades in closed_trades_master bzw. closed_ai_signals verschieben
    mit close_reason = "DELISTED / CLEANUP".

    Der Market-Tracker, Bot-Regime-Analyzer und Signal-Orchestrator
    klassifizieren Trades mit diesem Marker als NEUTRAL — sie zählen
    weder als Win noch als Loss, sondern werden aus Kelly und WR
    ausgeschlossen.

    Close-Preis-Logik:
      1. Letzte 5m-Candle des Coins nehmen (falls noch da)
      2. Fallback: Entry-Preis verwenden → PnL = 0% → neutral
    """
    logger.info("🧹 Checking offene Trades auf delisted Coins...")

    # 1. Aktive Coin-Liste laden
    try:
        with open('coins.json') as f:
            active_coins = set(json.load(f))
    except Exception as e:
        logger.error(f"Could not load coins.json: {e} — Delisted-Cleanup skipped")
        return

    if not active_coins:
        logger.warning("coins.json ist leer — Delisted-Cleanup skipped (Safety)")
        return

    conn = get_db_connection()
    closed_classic = 0
    closed_ai = 0

    try:
        # ── Klassische Trades (active_trades_master) ──
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, strategy, time, coin, direction, lev, entry, "
                "target1, target2, target3, target4, sl "
                "FROM active_trades_master"
            )
            columns = [desc[0] for desc in cur.description]
            classic_rows = [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]

        # P2.17: only force-close symbols that have the Binance USDT-perp shape.
        # A symbol "not in coins.json" is NOT proof of a delisting — junk that
        # leaked in (metals XAUUSD, cross pairs ETHBTC, forex) or a momentary
        # coins.json wobble would otherwise get nightly false-closed at PnL 0.
        # Restricting to the shape the fleet actually trades keeps the cleanup
        # to genuinely-delisted USDT perpetuals.
        classic_delisted = [
            t for t in classic_rows if t['coin'] not in active_coins and looks_like_usdt_perp(t['coin'])
        ]

        if classic_delisted:
            logger.info(f"  Klassische Trades: {len(classic_delisted)} auf delisted Coins gefunden")
            for trade in classic_delisted:
                coin = trade['coin']
                entry = float(trade['entry']) if trade['entry'] else 0.0
                close_price = _fetch_last_close_or_entry(conn, coin, entry)
                try:
                    with conn.cursor() as cur:
                        # status = "DELISTED" damit Market-Tracker/Analyzer
                        # das als neutral klassifizieren (close_reason aus
                        # status gelesen in 23_market_tracker und 27-Analyzer)
                        cur.execute(
                            """
                            INSERT INTO closed_trades_master (
                                strategy, time, coin, direction, lev, entry,
                                target1, target2, target3, target4, sl,
                                close_price, posted, status
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                trade['strategy'],
                                trade['time'],
                                coin,
                                trade['direction'],
                                trade['lev'],
                                entry,
                                trade['target1'],
                                trade['target2'],
                                trade['target3'],
                                trade['target4'],
                                trade['sl'],
                                close_price,
                                datetime.datetime.now(datetime.timezone.utc),
                                "DELISTED",
                            ),
                        )
                        cur.execute(
                            "DELETE FROM active_trades_master WHERE id = %s",
                            (trade['id'],),
                        )
                    conn.commit()
                    closed_classic += 1
                except Exception as e:
                    logger.warning(
                        f"  ⚠ Klassischer Trade {trade['id']} ({coin}) konnte nicht delisted-closed werden: {e}"
                    )
                    conn.rollback()

        # ── AI-Trades (ai_signals) ──
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, symbol, model, direction, entry1, price, current_target_hit, open_time FROM ai_signals"
            )
            columns = [desc[0] for desc in cur.description]
            ai_rows = [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]

        # P2.17: same Binance-perp-shape guard as the classic path above.
        ai_delisted = [t for t in ai_rows if t['symbol'] not in active_coins and looks_like_usdt_perp(t['symbol'])]

        if ai_delisted:
            logger.info(f"  AI-Trades: {len(ai_delisted)} auf delisted Coins gefunden")
            for trade in ai_delisted:
                coin = trade['symbol']
                entry = (
                    float(trade['entry1']) if trade['entry1'] else (float(trade['price']) if trade['price'] else 0.0)
                )
                close_price = _fetch_last_close_or_entry(conn, coin, entry)
                targets_hit = int(trade['current_target_hit'] or 0)
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO closed_ai_signals (
                                symbol, model, direction, entry, close_price,
                                targets_hit, open_time, close_time, status
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), %s)
                            """,
                            (
                                coin,
                                trade['model'],
                                trade['direction'],
                                entry,
                                close_price,
                                targets_hit,
                                trade['open_time'],
                                "DELISTED / CLEANUP",
                            ),
                        )
                        cur.execute(
                            "DELETE FROM ai_signals WHERE id = %s",
                            (trade['id'],),
                        )
                    conn.commit()
                    closed_ai += 1
                except Exception as e:
                    logger.warning(f"  ⚠ AI-Trade {trade['id']} ({coin}) konnte nicht delisted-closed werden: {e}")
                    conn.rollback()

        if closed_classic == 0 and closed_ai == 0:
            logger.info("  ✅ Keine delisted Trades gefunden.")
        else:
            logger.info(f"  ✅ Delisted-Cleanup: {closed_classic} klassische + {closed_ai} AI-Trades geschlossen.")

    except Exception as e:
        logger.error(f"❌ Fehler beim Delisted-Cleanup: {e}", exc_info=True)
        conn.rollback()
    finally:
        conn.close()


def _fetch_last_close_or_entry(conn, coin: str, entry: float) -> float:
    """Holt den letzten verfügbaren 5m-Close des Coins.

    Falls no data verfügbar sind (z.B. Coin war nie richtig getraded
    oder Tabelle fehlt), wird der Entry-Preis zurückgegeben. Das führt
    zu PnL=0%, was die Trade-Klassifikation als NEUTRAL auslöst — genau
    was wir für delisted Trades wollen.

    Eine eigene Connection-Sub-Transaktion wäre sauberer, aber Postgres
    macht bei einer fehlgeschlagenen Query im Hauptcontext sowieso einen
    Rollback nötig — daher bewusst hier per SAVEPOINT kapseln.
    """
    if entry <= 0:
        return 0.0
    try:
        with conn.cursor() as cur:
            # SAVEPOINT verhindert dass ein Lese-Error den Cleanup-Commit kippt
            cur.execute("SAVEPOINT sp_fetch_price")
            try:
                cur.execute(f'SELECT close FROM "{coin}_5m" ORDER BY open_time DESC LIMIT 1')
                row = cur.fetchone()
                cur.execute("RELEASE SAVEPOINT sp_fetch_price")
                if row and row[0]:
                    return float(row[0])
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_fetch_price")
    except Exception:
        pass
    return float(entry)


def update_max_leverage_json():
    """Holt die maximalen Hebel für alle Coins über die signierte Binance API und speichert sie."""
    logger.info("🔄 Updating max_leverage.json von Binance...")

    if not BINANCE_API_KEY or not BINANCE_SECRET:
        logger.warning("⚠️ Binance API Keys not set (.env). Leverage-Refresh skipped.")
        return

    try:
        url = "https://fapi.binance.com/fapi/v1/leverageBracket"

        # Signatur erstellen — recvWindow erlaubt 5s Clock-Drift zwischen uns und Binance
        timestamp = int(time.time() * 1000)
        query_string = f"timestamp={timestamp}&recvWindow=5000"

        signature = hmac.new(BINANCE_SECRET.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

        full_url = f"{url}?{query_string}&signature={signature}"
        headers = {'X-MBX-APIKEY': BINANCE_API_KEY}

        response = requests.get(full_url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        max_leverages = {}
        for item in data:
            symbol = item["symbol"]
            # Bracket 1 (Index 0) enthält den absoluten Maximalhebel
            if "brackets" in item and len(item["brackets"]) > 0:
                max_lev = item["brackets"][0]["initialLeverage"]
                max_leverages[symbol] = int(max_lev)

        if max_leverages:
            with open('max_leverage.json', 'w') as f:
                json.dump(max_leverages, f, indent=4)
            logger.info(f"✅ max_leverage.json updated successfully. {len(max_leverages)} Hebel saved.")
        else:
            logger.warning("⚠️ Keine Leverage-Daten von Binance zurückgegeben!")

    except Exception as e:
        logger.error(f"❌ Fehler beim Aktualisieren der max_leverage.json: {e}")


def cleanup_generated_charts(folder_path="generated_charts", max_age_hours=2):
    """Löscht Bilder, die älter als X Stunden sind.

    FIX (#31): Zusätzlich prüfen wir ob der Chart noch in der telegram_outbox
    referenziert ist. Vorher konnte ein Backlog (after Rate-Limit-Stau) dazu
    führen, dass der Housekeeping einen noch-zu-sendenden Chart löscht und
    der Telegram-Bot dann nur den Text ohne Bild schickt.
    """
    if not os.path.exists(folder_path):
        return

    now = time.time()
    cutoff = now - (max_age_hours * 3600)
    deleted_count = 0
    skipped_referenced = 0

    # Referenzierte Charts aus der Outbox holen (alle ungesendeten entries)
    referenced: set[str] = set()
    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT image_path FROM telegram_outbox WHERE sent = FALSE AND image_path IS NOT NULL"
                )
                referenced = {row[0] for row in cur.fetchall() if row[0]}
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Konnte Outbox-Referenzen nicht laden: {e}")

    try:
        for filename in os.listdir(folder_path):
            file_path = os.path.join(folder_path, filename)

            # Nur Dateien prüfen (keine Unterordner)
            if os.path.isfile(file_path):
                # Wenn die Datei noch in der Outbox referenziert ist, skippen
                # (vergleiche sowohl absolute als auch relative Pfade)
                abs_path = os.path.abspath(file_path)
                if abs_path in referenced or file_path in referenced:
                    skipped_referenced += 1
                    continue

                file_time = os.path.getmtime(file_path)
                if file_time < cutoff:
                    os.remove(file_path)
                    deleted_count += 1

        if deleted_count > 0 or skipped_referenced > 0:
            logging.info(
                f"🧹 HOUSEKEEPING: {deleted_count} alte Charts gelöscht, "
                f"{skipped_referenced} skipped (noch in Outbox referenziert) "
                f"in '{folder_path}' (älter als {max_age_hours}h)."
            )
    except Exception as e:
        logging.error(f"🔥 Fehler beim Löschen der Charts in '{folder_path}': {e}")


def truncate_oversized_logs(log_paths=("logs/dashboard.log",), max_bytes=20 * 1024 * 1024):
    """Caps append-only raw-pipe logs that no logging handler rotates (P3.2).

    logs/dashboard.log is the dashboard subprocess' stdout/stderr pipe
    (main_watchdog opens it in append mode), so it grows unbounded — unlike the
    watchdog/indicator logs, which now use RotatingFileHandler. When a file
    exceeds max_bytes we keep only its last half and drop the rest. Best-effort:
    the dashboard keeps its append handle open, so any I/O error is swallowed.
    """
    keep = max_bytes // 2
    for path in log_paths:
        try:
            if not os.path.isfile(path) or os.path.getsize(path) <= max_bytes:
                continue
            with open(path, "rb") as f:
                f.seek(-keep, os.SEEK_END)
                tail = f.read()
            with open(path, "wb") as f:
                f.write(tail)
            logger.info(f"🧹 HOUSEKEEPING: log '{path}' auf die letzten {keep // (1024 * 1024)} MB gekürzt.")
        except Exception as e:
            logger.warning(f"Konnte Log '{path}' nicht kürzen: {e}")


def cleanup_telegram_outbox(max_age_days=7):
    """FIX: Löscht alte, bereits gesendete telegram_outbox-entries.

    Vorher lief die Tabelle unbegrenzt voll — bei ~24 Bots × mehreren signalsn
    pro Tag × monatelangem Betrieb waren das schnell 100.000+ Zeilen. Das hat
    `SELECT * WHERE sent = FALSE` des Telegram-Bots ausgebremst (Full-Scan).
    """
    logger.info(f"🧹 Starting Outbox-Cleanup (entries älter als {max_age_days} Tage)...")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Lösche nur bereits gesendete Nachrichten, damit ungesandte nicht verloren gehen.
            # Falls die Spalte `created_at` nicht existiert, nutzen wir stattdessen
            # die niedrigsten IDs als Alter-Heuristik.
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'telegram_outbox' AND column_name = 'created_at'
            """)
            has_created_at = cur.fetchone() is not None

            if has_created_at:
                cur.execute(
                    """
                    DELETE FROM telegram_outbox
                    WHERE sent = TRUE AND created_at < NOW() - INTERVAL %s
                """,
                    (f'{max_age_days} days',),
                )
            else:
                # Fallback: Lösche gesendete entries mit IDs kleiner als die
                # aktuell kleinste ID minus einen Puffer (d.h. die ältesten).
                # Hier einfach alle sent=TRUE löschen, da sonst keine Zeit-Info da ist.
                cur.execute("DELETE FROM telegram_outbox WHERE sent = TRUE")

            deleted = cur.rowcount
        conn.commit()
        if deleted > 0:
            logger.info(f"🧹 Outbox-Cleanup: {deleted} alte gesendete entries gelöscht.")
    except Exception as e:
        logger.error(f"❌ Fehler beim Outbox-Cleanup: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


def clean_old_database_entries():
    """Löscht alte Kerzen und Indikatoren, um die DB schlank zu halten."""
    logger.info("🧹 Starting Datenbank-Reinigung (Lösche alte Daten)...")

    # --- NEU: Individuelle Aufbewahrungszeiten pro Timeframe ---
    retention_policies = {
        '5m': '1 month',
        '15m': '1 year',
        '30m': '1 year',
        '1h': '1 year',
        '2h': '1 year',
        '4h': '1 year',
    }

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # 1. Alle Tabellennamen aus der DB holen
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
            """)
            tables = [row[0] for row in cur.fetchall()]

            cleaned_count = 0

            # 2. Durch alle Tabellen iterieren
            for table in tables:
                # Skipping unsere System-Tabellen (Trades, Telegram, etc.)
                if "trades" in table or "telegram" in table:
                    continue

                # Prüfen, zu welchem Timeframe die Tabelle gehört
                for tf, interval in retention_policies.items():
                    # Sicherstellen, dass wir 5m nicht mit 15m verwechseln!
                    # Wir prüfen, ob die Tabelle exakt auf "_5m" oder "_5m_indicators" endet.
                    if table.endswith(f"_{tf}") or table.endswith(f"_{tf}_indicators"):
                        try:
                            # Wir löschen alles, was älter als das definierte Interval ist
                            cur.execute(f"""
                                DELETE FROM "{table}"
                                WHERE open_time < NOW() - INTERVAL '{interval}';
                            """)
                            # WICHTIG: Commit after JEDER Tabelle, damit der RAM der DB nicht überläuft!
                            conn.commit()
                            cleaned_count += 1
                        except Exception:
                            # Falls eine Tabelle (aus welchem Grund auch immer) kein open_time hat
                            conn.rollback()
                            pass

                        # Sobald wir den richtigen Timeframe gefunden und verarbeitet haben, abbrechen
                        break

            # 3. Schwache Pump/Dump Events löschen (EINMAL after der Tabellen-Schleife,
            #    nicht bei jeder Tabelle. Vorher lief das ~12.600× durch falsche Einrückung).
            try:
                # Schwellen zentral in core/config.py — dasselbe Paar gated den
                # Insert im Detector (10_pump_dump_detector.py, P1.40).
                cur.execute(
                    "DELETE FROM pump_dump_events WHERE volume_ratio < %s OR ABS(price_change_60s) < %s;",
                    (PUMP_EVENT_MIN_VOL_RATIO, PUMP_EVENT_MIN_ABS_PCHG_60S),
                )
                deleted_events = cur.rowcount
                if deleted_events > 0:
                    logger.info(f"🧹 HOUSEKEEPING: {deleted_events} schwache Pump/Dump Events gelöscht.")
                conn.commit()
            except Exception as e:
                logger.error(f"Fehler beim Löschen schwacher Pump/Dump Events: {e}")
                conn.rollback()

        logger.info(f"✅ Datenbank erfolgreich bereinigt! {cleaned_count} Tabellen wurden geprüft und verkleinert.")

    except Exception as e:
        logger.error(f"❌ Schwerer Error for der Reinigung: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


# P2.18: Gap-Filler-REST mit 429/418-Handling. Der Throttle desynchronisiert
# den Burst über ~9k Tabellen; das Ban-Fenster stoppt bei 418 ALLE weiteren
# Gap-Fill-Calls bis zum Ablauf (weiter iterieren würde den IP-Ban verlängern
# und träfe auch die Trading-Endpoints — genau der P2.18-Fehlermodus).
_GAP_FILL_THROTTLE = MinIntervalThrottle()
_GAP_FILL_MIN_INTERVAL_S = 0.25  # ~4 req/s → weit unter dem Weight-Limit
_GAP_FILL_MAX_RETRIES = 5
_GAP_FILL_RETRY_DEADLINE_S = 120.0
_gap_fill_ban_until = 0.0  # monotonic-Zeitpunkt, bis zu dem die 418-Ban-Pause gilt
# Zählt 418er über den GANZEN Lauf (Review PR #21): Binance eskaliert
# Repeat-Offender-Bans — ein flaches 120s-Fenster würde den Ban bei fehlendem
# Retry-After-Header alle ~2 min re-triggern statt exponentiell zurückzuweichen.
# Reset erst bei einem erfolgreichen Call.
_gap_fill_consecutive_bans = 0


def _fetch_klines_from_binance(symbol: str, interval: str, start_ms: int, end_ms: int) -> list | None:
    """Holt Klines im Range [start_ms, end_ms] von Binance Futures REST.

    Returns None bei Fehler, sonst Liste von Klines im Binance-Format
    [open_time, open, high, low, close, volume, ...].
    Max 1500 Kerzen pro Call (Binance-Limit). Bei größeren Ranges muss der
    Caller paginieren — für unsere Gap-Size (meist <100 candles) kein Thema.

    P2.18: 429 → Retry-After-bewusster, gebudgeteter Backoff; 418 (IP-Ban)
    → prozessweites Ban-Fenster (>=120s), alle weiteren Calls liefern bis zum
    Ablauf sofort None — der nächste nächtliche Lauf holt die Gaps nach.
    """
    global _gap_fill_ban_until, _gap_fill_consecutive_bans
    if time.monotonic() < _gap_fill_ban_until:
        return None  # 418-Ban-Fenster aktiv — nicht weiter hämmern

    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 1500,
    }
    budget = RetryBudget(max_attempts=_GAP_FILL_MAX_RETRIES, deadline_s=_GAP_FILL_RETRY_DEADLINE_S)
    consecutive_fail = 0
    # Anders als in fetch_ohlcv_batch zählt hier JEDER Versuch (inkl. dem
    # ersten) gegen das Budget — es gibt keine Erfolgs-Pagination in diesem
    # Ein-Range-Call (Review PR #21, RetryBudget kennt beide Muster).
    while budget.attempt():
        _GAP_FILL_THROTTLE.wait("binance-fapi", _GAP_FILL_MIN_INTERVAL_S)
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 418:
                _gap_fill_consecutive_bans += 1
                wait = backoff_seconds(418, _gap_fill_consecutive_bans, resp.headers.get("Retry-After"))
                _gap_fill_ban_until = time.monotonic() + wait
                logger.warning(
                    f"Gap-Fill {symbol} {interval}: 418 (IP-Ban-Signal Nr. {_gap_fill_consecutive_bans}) "
                    f"— Gap-Filler pausiert {wait:.0f}s"
                )
                return None
            if resp.status_code == 429:
                consecutive_fail += 1
                wait = backoff_seconds(429, consecutive_fail, resp.headers.get("Retry-After"))
                logger.warning(f"Gap-Fill {symbol} {interval}: 429 — Backoff {wait:.0f}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            _gap_fill_consecutive_bans = 0  # erfolgreicher Call beendet die Ban-Eskalation
            return resp.json()
        except requests.exceptions.RequestException as e:
            consecutive_fail += 1
            logger.warning(f"Gap-Fill REST-Call für {symbol} {interval} fehlgeschlagen: {e}")
            time.sleep(backoff_seconds(None, consecutive_fail))
    logger.warning(f"Gap-Fill {symbol} {interval}: Retry-Budget erschöpft ({budget.exhausted_reason()})")
    return None


def _timeframe_to_seconds(tf: str) -> int:
    """'5m' → 300, '1h' → 3600, '1d' → 86400, '1w' → 604800"""
    mapping = {
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "2h": 7200,
        "4h": 14400,
        "1d": 86400,
        "1w": 604800,
    }
    return mapping.get(tf, 0)


def fill_ohlcv_gaps_and_invalidate_indicators(scan_hours: int = 24) -> None:
    """Nightly Gap-Filler.

    Scannt für jeden Coin × Timeframe die letzten `scan_hours` Stunden der
    `{symbol}_{tf}`-Tabelle auf fehlende Kerzen. Lücken werden via Binance REST
    aftergeladen, anschließend die entsprechenden entries aus der
    `{symbol}_{tf}_indicators`-Tabelle ab dem ersten Gap gelöscht, damit die
    Indicator-Engine beim nächsten regulären Lauf automatisch neu durchrechnet
    (inkl. voller 1000-Kerzen-Warmup).

    Fehler-Isolation: Exceptions pro Coin+TF werden gefangen, der Rest läuft
    weiter. Ein einzelner defekter Coin bremst den Job nicht.

    Args:
        scan_hours: Wie weit zurück gescannt wird. Default 24h.
    """
    logger.info(f"🔍 Gap-Filler startet (Scan-Window: {scan_hours}h, {len(TIMEFRAMES)} Timeframes)...")
    start_time = time.time()

    try:
        with open("coins.json", encoding="utf-8") as f:
            data = json.load(f)
        coins = data.get("coins", data) if isinstance(data, dict) else data
        coins = [c.upper() for c in coins if c.upper().endswith("USDT")]
    except Exception as e:
        logger.error(f"Gap-Filler konnte coins.json nicht laden: {e}")
        return

    now_ms = int(time.time() * 1000)
    scan_start_ms = now_ms - (scan_hours * 3600 * 1000)

    total_coins_affected = 0
    total_candles_filled = 0
    total_indicator_rows_invalidated = 0

    conn = None
    try:
        conn = get_db_connection()

        for symbol in coins:
            for tf in TIMEFRAMES:
                try:
                    tf_seconds = _timeframe_to_seconds(tf)
                    if tf_seconds == 0:
                        continue

                    ohlcv_table = f'"{symbol}_{tf}"'
                    ind_table = f'"{symbol}_{tf}_indicators"'

                    # 1) Existierende Kerzen im Scan-Fenster lesen
                    scan_start_dt = datetime.datetime.fromtimestamp(scan_start_ms / 1000, tz=datetime.timezone.utc)
                    with conn.cursor() as cur:
                        try:
                            cur.execute(
                                f"SELECT open_time FROM {ohlcv_table} WHERE open_time >= %s ORDER BY open_time ASC",
                                (scan_start_dt,),
                            )
                            rows = cur.fetchall()
                        except Exception:
                            # Tabelle existiert vermutlich noch nicht (neuer Coin) → skip
                            conn.rollback()
                            continue

                    if not rows or len(rows) < 2:
                        # Keine oder kaum Daten im Scan-Fenster — zu wenig zum Gaps-Detektieren
                        continue

                    # 2) Gaps finden: diff zwischen aufeinanderfolgenden open_times
                    #    Erwartung: tf_seconds; Toleranz ×1.5 für minor latencies
                    expected_delta_ms = tf_seconds * 1000
                    tolerance_ms = int(expected_delta_ms * 1.5)

                    times_ms = [int(r[0].timestamp() * 1000) for r in rows]
                    gap_ranges = []  # Liste von (missing_start_ms, missing_end_ms)

                    for i in range(1, len(times_ms)):
                        delta = times_ms[i] - times_ms[i - 1]
                        if delta > tolerance_ms:
                            # Gap! Fehlende Kerzen liegen zwischen [i-1] + expected und [i] - expected
                            gap_start = times_ms[i - 1] + expected_delta_ms
                            gap_end = times_ms[i] - expected_delta_ms
                            if gap_end >= gap_start:
                                gap_ranges.append((gap_start, gap_end))

                    if not gap_ranges:
                        continue  # keine Gaps in diesem Coin+TF

                    # 3) Pro Gap-Range via REST afterladen und inserten
                    first_gap_ms = gap_ranges[0][0]  # ältester Gap — merken für Indikator-DELETE
                    candles_inserted_for_cointf = 0

                    for gap_start_ms, gap_end_ms in gap_ranges:
                        # Binance endTime ist inklusive — wir addieren expected_delta damit
                        # die letzte Kerze sicher mit drin ist
                        klines = _fetch_klines_from_binance(symbol, tf, gap_start_ms, gap_end_ms + expected_delta_ms)
                        if not klines:
                            continue

                        # 4) INSERT ... ON CONFLICT DO NOTHING pro Kerze
                        # FIX P0.9: Der PK der Candle-Tabellen ist (symbol, open_time)
                        # — der alte INSERT ließ `symbol` weg und nutzte
                        # ON CONFLICT (open_time) (kein passender Unique-Index)
                        # → JEDER Insert warf, das except verschluckte es still,
                        # und der nächtliche Gap-Filler war ein No-op.
                        # Savepoint pro Row, damit ein Einzel-Fehler nicht die
                        # Transaktion für den Rest des Batches abortet.
                        with conn.cursor() as cur:
                            for k in klines:
                                try:
                                    # SAVEPOINT als ERSTE Anweisung im try — stünde er
                                    # nach dem Parsing, würde ein Parse-Fehler in Row N
                                    # auf den Savepoint VOR Row N-1s Insert zurückrollen
                                    # und deren Kerze still wieder löschen.
                                    cur.execute("SAVEPOINT gap_fill_row")
                                    ot_ms = int(k[0])
                                    # Nur Kerzen im eigentlichen Gap-Range einfügen — falls Binance mehr liefert
                                    if ot_ms < gap_start_ms or ot_ms > gap_end_ms + expected_delta_ms:
                                        continue
                                    ot = datetime.datetime.fromtimestamp(ot_ms / 1000, tz=datetime.timezone.utc)
                                    o_val, h_val, l_val, c_val, v_val = (
                                        float(k[1]),
                                        float(k[2]),
                                        float(k[3]),
                                        float(k[4]),
                                        float(k[5]),
                                    )
                                    cur.execute(
                                        f"INSERT INTO {ohlcv_table} "
                                        "(symbol, open_time, open, high, low, close, volume) "
                                        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                                        "ON CONFLICT (symbol, open_time) DO NOTHING",
                                        (symbol, ot, o_val, h_val, l_val, c_val, v_val),
                                    )
                                    candles_inserted_for_cointf += cur.rowcount
                                except Exception as row_err:
                                    # FIX P0.9: Fehler loggen statt still verschlucken
                                    logger.warning(f"Gap-Filler: Insert-Fehler {symbol} {tf} @ {k[0]}: {row_err}")
                                    try:
                                        cur.execute("ROLLBACK TO SAVEPOINT gap_fill_row")
                                    except Exception:
                                        break  # Transaktion nicht mehr nutzbar — Batch abbrechen
                                    continue
                        conn.commit()

                    if candles_inserted_for_cointf == 0:
                        # Gaps erkannt aber REST lieferte nichts (oder alles Duplikate)
                        continue

                    # 5) Indikator-Invalidierung: alle Rows ab first_gap löschen
                    first_gap_dt = datetime.datetime.fromtimestamp(first_gap_ms / 1000, tz=datetime.timezone.utc)
                    rows_invalidated = 0
                    try:
                        with conn.cursor() as cur:
                            cur.execute(
                                f"DELETE FROM {ind_table} WHERE open_time >= %s",
                                (first_gap_dt,),
                            )
                            rows_invalidated = cur.rowcount
                        conn.commit()
                    except Exception:
                        # Indicator-Tabelle existiert evtl. nicht — harmlos, Engine baut sie neu
                        conn.rollback()

                    logger.info(
                        f"🔧 {symbol}_{tf}: {candles_inserted_for_cointf} Kerzen gefüllt "
                        f"(ab {first_gap_dt.strftime('%Y-%m-%d %H:%M UTC')}), "
                        f"{rows_invalidated} Indikator-Rows invalidiert"
                    )
                    total_coins_affected += 1
                    total_candles_filled += candles_inserted_for_cointf
                    total_indicator_rows_invalidated += rows_invalidated

                    # Sanft zur Binance-API: kurze Pause zwischen Coins mit Gaps
                    time.sleep(0.1)

                except Exception as e:
                    logger.warning(f"Gap-Filler-Error for {symbol}_{tf}: {e}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    continue

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    duration = time.time() - start_time
    if total_coins_affected == 0:
        logger.info(f"✅ Gap-Filler fertig: keine Lücken gefunden ({duration:.1f}s).")
    else:
        logger.info(
            f"✅ Gap-Filler fertig: {total_coins_affected} Coin+TF-Kombis betroffen, "
            f"{total_candles_filled} Kerzen aftergeladen, "
            f"{total_indicator_rows_invalidated} Indikator-Rows invalidiert. "
            f"Dauer: {duration:.1f}s. "
            f"Indicator-Engine rechnet beim nächsten Run automatisch neu."
        )


def main():
    logger.info("=== 🛡️ HOUSEKEEPING SERVICE GESTARTET ===")
    logger.info("Führe Initialen Run aus...")

    # 0. Initialer Run beim Starten des Skripts (falls man es manuell neustartet)
    update_coins_json()
    # Direkt after update_coins_json laufen lassen damit neu delisted Coins
    # sofort bereinigt werden — nicht erst am nächsten 03:00-Cycle warten.
    cleanup_delisted_trades()
    update_max_leverage_json()

    logger.info("Waiting im Hintergrund auf 03:00 Uhr UTC...")

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)

        # Pünktlich um 03:00 Uhr (Minute 0)
        if now.hour == 3 and now.minute == 00:
            logger.info("⏰ 03:00 Uhr erreicht. Beginne nächtliche Wartung...")

            # 1. Neue Coins abholen (falls Binance welche gelistet/delisted hat)
            update_coins_json()

            # 2. Delisted-Trades aufräumen (after update_coins_json damit die
            # frisch aktualisierte coins.json verwendet wird)
            cleanup_delisted_trades()

            # 3. Maximale Hebel aktualisieren
            update_max_leverage_json()

            # 4. Alte Daten in der DB wegwerfen (inkl. schwache Pump/Dumps)
            clean_old_database_entries()

            # 5. Die alten Bilder löschen
            cleanup_generated_charts("generated_charts")
            cleanup_generated_charts("charts")

            # 6. FIX: Alte (gesendete) Outbox-entries löschen — sonst läuft die
            # Tabelle unbegrenzt voll.
            cleanup_telegram_outbox(max_age_days=7)

            # 6b. P3.2: den unrotierten dashboard.log-Pipe kappen.
            truncate_oversized_logs()

            # 7. Nightly Gap-Filler: scannt die letzten 24h aller Coin×TF-Tabellen
            # auf fehlende Kerzen, füllt sie via Binance REST, und invalidiert
            # die entsprechenden Indikator-entries ab dem ersten Gap. Die
            # Indicator-Engine rechnet beim nächsten 30-Minuten-Run automatisch
            # neu (inkl. 1000-Kerzen-Warmup) — keine Sprünge in den Werten.
            fill_ohlcv_gaps_and_invalidate_indicators(scan_hours=24)

            # Schlafe 65 Sekunden, damit er 03:01 Uhr erreicht
            # und die Routine heute nicht nochmal auslöst
            logger.info("💤 Wartung completed. Schlafe bis morgen 03:00 Uhr...")
            time.sleep(65)

        # Kurze Prüfung alle 30 Sekunden reicht völlig (schont die CPU)
        time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")
