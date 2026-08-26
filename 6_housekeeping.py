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
from core.candles import (
    delete_candles_before,
    delete_indicators_from,
    list_coin_tables,
    read_candles,
    upsert_candles,
)
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
from core.state_utils import atomic_read_json, atomic_write_json

logging.basicConfig(level=logging.INFO, format='%(asctime)s - HOUSEKEEPING - %(message)s')
logger = logging.getLogger(__name__)

# T-2026-KYT-9050-155: the gap filler used to scan a hardcoded 24h and to run
# only at 03:00 UTC. Both failed in the T-154 incident: the 2026-08-24 outage left
# a candle gap that was 42h old by the next nightly run (outside the window), and
# the restart in between never looked for it. We now remember when the filler last
# succeeded and cover everything since.
GAP_FILL_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gap_filler_state.json")
# Never scan less than before — the window may grow, never shrink.
GAP_SCAN_MIN_HOURS = 24.0
# ...and never grow without bound: the filler walks ~524 coins x N timeframes with
# a REST fetch per gap, on a CPU-tight VPS. A longer absence is a repair job.
GAP_SCAN_MAX_HOURS = 168.0
# Overlap so a gap right at the boundary of the last run is not missed.
GAP_SCAN_MARGIN_HOURS = 2.0


def resolve_gap_scan_hours(last_run_iso, now_utc):
    """How far back the gap filler should scan. Returns (hours, level, message).

    Pure: no I/O, no logging. Covers everything since the last successful run
    plus a margin, floored at the previous fixed 24h so behaviour never regresses
    and capped so a long absence cannot turn into an unbounded scan.
    """
    if not last_run_iso:
        return (
            GAP_SCAN_MIN_HOURS,
            "info",
            f"gap filler: no previous-run watermark - scanning the default {GAP_SCAN_MIN_HOURS:.0f}h.",
        )
    try:
        last = datetime.datetime.fromisoformat(last_run_iso)
    except (ValueError, TypeError):
        return (
            GAP_SCAN_MIN_HOURS,
            "warning",
            f"gap filler: unreadable watermark {last_run_iso!r} - scanning the default {GAP_SCAN_MIN_HOURS:.0f}h.",
        )
    if last.tzinfo is None:
        last = last.replace(tzinfo=datetime.timezone.utc)
    since_h = (now_utc - last).total_seconds() / 3600.0
    if since_h < 0:
        return (
            GAP_SCAN_MIN_HOURS,
            "warning",
            f"gap filler: watermark {last_run_iso} is in the future - scanning the default {GAP_SCAN_MIN_HOURS:.0f}h.",
        )
    needed = since_h + GAP_SCAN_MARGIN_HOURS
    if needed > GAP_SCAN_MAX_HOURS:
        return (
            GAP_SCAN_MAX_HOURS,
            "warning",
            f"gap filler: {since_h:.1f}h since the last successful run exceeds the "
            f"{GAP_SCAN_MAX_HOURS:.0f}h cap - scanning {GAP_SCAN_MAX_HOURS:.0f}h. "
            "Gaps older than that stay unrepaired; fill them out of band.",
        )
    hours = max(needed, GAP_SCAN_MIN_HOURS)
    return hours, "info", f"gap filler: {since_h:.1f}h since the last successful run - scanning {hours:.1f}h."


def should_gap_fill_on_start(last_run_iso, now_utc):
    """Whether the startup pass should run the filler. Returns (run, reason).

    Only when at least one nightly run was missed - that is the post-outage
    restart, which is exactly when a gap exists and nothing else looks for it. An
    ordinary restart must stay cheap, and a first deploy (no watermark) must not
    surprise the operator with a full scan.
    """
    if not last_run_iso:
        return False, "no watermark yet - leaving the first scan to the nightly run."
    try:
        last = datetime.datetime.fromisoformat(last_run_iso)
    except (ValueError, TypeError):
        return False, f"unreadable watermark {last_run_iso!r} - leaving the scan to the nightly run."
    if last.tzinfo is None:
        last = last.replace(tzinfo=datetime.timezone.utc)
    since_h = (now_utc - last).total_seconds() / 3600.0
    if since_h > GAP_SCAN_MIN_HOURS:
        return True, f"{since_h:.1f}h since the last successful run - at least one nightly run was missed."
    return False, f"only {since_h:.1f}h since the last successful run - nothing missed."


def _read_gap_watermark():
    return (atomic_read_json(GAP_FILL_STATE_FILE, default={}) or {}).get("last_success_utc")


def _record_gap_success(now_utc) -> None:
    atomic_write_json(GAP_FILL_STATE_FILE, {"last_success_utc": now_utc.isoformat()})


def run_gap_filler(now_utc) -> None:
    """Resolve the window, run the filler, remember the success."""
    hours, level, message = resolve_gap_scan_hours(_read_gap_watermark(), now_utc)
    getattr(logger, level)(message)
    fill_ohlcv_gaps_and_invalidate_indicators(scan_hours=int(round(hours)))
    _record_gap_success(now_utc)


def update_coins_json():
    """Fetches the latest active USDT perpetual futures from Binance and updates the file.

    Filter + atomic write live in ``core.coins`` (the single coins.json writer,
    P2.16) — shared with ``1_data_ingestion.update_trading_pairs`` so the two
    can no longer drift apart. On a refresh failure the live coins.json is left
    untouched (no truncation) and table creation is skipped for this run.
    """
    logger.info("🔄 Updating coins.json from Binance...")
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
        logger.info("Checking table structure for all coins...")
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                for symbol in symbols:
                    for tf in TIMEFRAMES:
                        tablename = f'"{symbol}_{tf}"'
                        # We simply fire off the Create — Postgres ignores it if it exists (very fast)
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
    """Closes open trades on coins that no longer exist in coins.json.

    Background: When Binance delists a coin, no new candles arrive via
    ingestion. The internal monitor would then leave the trade open forever
    (because SL/TP never hit). This massively distorts performance statistics —
    trades with no end count as "still open" instead of neutral closes.

    Solution: During nightly housekeeping (or manually) check which coins
    have disappeared from coins.json, and for those move all open trades in
    closed_trades_master and closed_ai_signals with close_reason =
    "DELISTED / CLEANUP".

    Market-Tracker, Bot-Regime-Analyzer and Signal-Orchestrator classify
    trades with this marker as NEUTRAL — they count neither as win nor loss,
    but are excluded from Kelly and win rate.

    Close-price logic:
      1. Take the last 5m candle of the coin (if still available)
      2. Fallback: use entry price → PnL = 0% → neutral
    """
    logger.info("🧹 Checking open trades on delisted coins...")

    # 1. Load active coin list
    try:
        with open('coins.json') as f:
            active_coins = set(json.load(f))
    except Exception as e:
        logger.error(f"Could not load coins.json: {e} — Delisted-Cleanup skipped")
        return

    if not active_coins:
        logger.warning("coins.json is empty — Delisted-Cleanup skipped (Safety)")
        return

    conn = get_db_connection()
    closed_classic = 0
    closed_ai = 0

    try:
        # ── Classic trades (active_trades_master) ──
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
            logger.info(f"  Classic trades: {len(classic_delisted)} found on delisted coins")
            for trade in classic_delisted:
                coin = trade['coin']
                entry = float(trade['entry']) if trade['entry'] else 0.0
                close_price = _fetch_last_close_or_entry(conn, coin, entry)
                try:
                    with conn.cursor() as cur:
                        # status = "DELISTED" so Market-Tracker/Analyzer can
                        # classify it as neutral (close_reason read from status
                        # in 23_market_tracker and 27-Analyzer)
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
                    logger.warning(f"  ⚠ Classic trade {trade['id']} ({coin}) could not be delisted-closed: {e}")
                    conn.rollback()

        # ── AI trades (ai_signals) ──
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, symbol, model, direction, entry1, price, current_target_hit, open_time FROM ai_signals"
            )
            columns = [desc[0] for desc in cur.description]
            ai_rows = [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]

        # P2.17: same Binance-perp-shape guard as the classic path above.
        ai_delisted = [t for t in ai_rows if t['symbol'] not in active_coins and looks_like_usdt_perp(t['symbol'])]

        if ai_delisted:
            logger.info(f"  AI trades: {len(ai_delisted)} found on delisted coins")
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
                    logger.warning(f"  ⚠ AI trade {trade['id']} ({coin}) could not be delisted-closed: {e}")
                    conn.rollback()

        if closed_classic == 0 and closed_ai == 0:
            logger.info("  ✅ No delisted trades found.")
        else:
            logger.info(f"  ✅ Delisted-Cleanup: {closed_classic} classic + {closed_ai} AI trades closed.")

    except Exception as e:
        logger.error(f"❌ Error during Delisted-Cleanup: {e}", exc_info=True)
        conn.rollback()
    finally:
        conn.close()


def _fetch_last_close_or_entry(conn, coin: str, entry: float) -> float:
    """Fetches the last available 5m close of the coin.

    If no data are available (e.g. coin was never properly traded or table
    is missing), the entry price is returned. This results in PnL=0%, which
    triggers trade classification as NEUTRAL — exactly what we want for
    delisted trades.

    A separate connection sub-transaction would be cleaner, but Postgres
    requires a rollback anyway if a query fails in the main context — so we
    deliberately encapsulate here via SAVEPOINT.
    """
    if entry <= 0:
        return 0.0
    try:
        with conn.cursor() as cur:
            # SAVEPOINT prevents a read error from aborting the cleanup commit
            cur.execute("SAVEPOINT sp_fetch_price")
            try:
                # core.candles: newest 5m close, forming candle deliberately included
                # (price read — contract 2: include_forming=True). read_candles opens
                # its own cursor on the same connection; the SAVEPOINT protects the
                # cleanup transaction even if the table is missing.
                df = read_candles(conn, coin, "5m", limit=1, include_forming=True, columns=("open_time", "close"))
                cur.execute("RELEASE SAVEPOINT sp_fetch_price")
                if not df.empty and df["close"].iloc[-1]:
                    return float(df["close"].iloc[-1])
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_fetch_price")
    except Exception:
        pass
    return float(entry)


def update_max_leverage_json():
    """Fetches the maximum leverage for all coins via the signed Binance API and saves them."""
    logger.info("🔄 Updating max_leverage.json from Binance...")

    if not BINANCE_API_KEY or not BINANCE_SECRET:
        logger.warning("⚠️ Binance API Keys not set (.env). Leverage-Refresh skipped.")
        return

    try:
        url = "https://fapi.binance.com/fapi/v1/leverageBracket"

        # Create signature — recvWindow allows 5s clock drift between us and Binance
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
            # Bracket 1 (index 0) contains the maximum leverage
            if "brackets" in item and len(item["brackets"]) > 0:
                max_lev = item["brackets"][0]["initialLeverage"]
                max_leverages[symbol] = int(max_lev)

        if max_leverages:
            with open('max_leverage.json', 'w') as f:
                json.dump(max_leverages, f, indent=4)
            logger.info(f"✅ max_leverage.json updated successfully. {len(max_leverages)} leverages saved.")
        else:
            logger.warning("⚠️ No leverage data returned from Binance!")

    except Exception as e:
        logger.error(f"❌ Error updating max_leverage.json: {e}")


def cleanup_generated_charts(folder_path="generated_charts", max_age_hours=2):
    """Deletes images that are older than X hours.

    FIX (#31): Additionally we check if the chart is still referenced in the
    telegram_outbox. Previously, a backlog (after rate-limit congestion) could
    cause housekeeping to delete a chart still to be sent, and the Telegram
    bot would then only send text without the image.
    """
    if not os.path.exists(folder_path):
        return

    now = time.time()
    cutoff = now - (max_age_hours * 3600)
    deleted_count = 0
    skipped_referenced = 0

    # Get referenced charts from the outbox (all unsent entries)
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
        logger.warning(f"Could not load outbox references: {e}")

    try:
        for filename in os.listdir(folder_path):
            file_path = os.path.join(folder_path, filename)

            # Only check files (no subdirectories)
            if os.path.isfile(file_path):
                # If the file is still referenced in the outbox, skip it
                # (compare both absolute and relative paths)
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
                f"🧹 HOUSEKEEPING: {deleted_count} old charts deleted, "
                f"{skipped_referenced} skipped (still referenced in outbox) "
                f"in '{folder_path}' (older than {max_age_hours}h)."
            )
    except Exception as e:
        logging.error(f"🔥 Error deleting charts in '{folder_path}': {e}")


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
            logger.info(f"🧹 HOUSEKEEPING: log '{path}' truncated to last {keep // (1024 * 1024)} MB.")
        except Exception as e:
            logger.warning(f"Could not truncate log '{path}': {e}")


def cleanup_telegram_outbox(max_age_days=7):
    """FIX: Deletes old, already-sent telegram_outbox entries.

    Previously the table would grow unbounded — with ~24 bots × multiple signals
    per day × months of operation, that quickly became 100,000+ rows. This
    slowed down the `SELECT * WHERE sent = FALSE` query of the Telegram bot
    (full table scan).
    """
    logger.info(f"🧹 Starting outbox cleanup (entries older than {max_age_days} days)...")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Delete only already-sent messages so unsent ones don't get lost.
            # If the `created_at` column doesn't exist, use the lowest IDs instead
            # as an age heuristic.
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
                # Fallback: Delete sent entries with IDs smaller than the current
                # smallest ID minus a buffer (i.e., the oldest ones). Here just
                # delete all sent=TRUE since there is no time info otherwise.
                cur.execute("DELETE FROM telegram_outbox WHERE sent = TRUE")

            deleted = cur.rowcount
        conn.commit()
        if deleted > 0:
            logger.info(f"🧹 Outbox cleanup: {deleted} old sent entries deleted.")
    except Exception as e:
        logger.error(f"❌ Error during outbox cleanup: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


def clean_old_database_entries():
    """Deletes old candles and indicators to keep the database lean."""
    logger.info("🧹 Starting database cleanup (deleting old data)...")

    # --- NEW: Individual retention times per timeframe ---
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
        # 1. Calendar-accurate cutoffs from the DB — matches the old
        #    `NOW() - INTERVAL '<interval>'` exactly, sampled once up front so
        #    every table is pruned against the same instant.
        cutoffs = {}
        with conn.cursor() as cur:
            for tf, interval in retention_policies.items():
                cur.execute("SELECT now() - %s::interval", (interval,))
                cutoffs[tf] = cur.fetchone()[0]

        cleaned_count = 0

        # 2. list_coin_tables enumerates only real {sym}_{tf}[_indicators] tables —
        #    system tables (trades, telegram, funding_rates, …) never match its
        #    pattern, so the old substring blacklist is gone. A tf outside
        #    retention_policies (1d/1w) has no cutoff → kept forever, as before.
        for symbol, tf, kind in list_coin_tables(conn):
            cutoff = cutoffs.get(tf)
            if cutoff is None:
                continue
            try:
                # Deletes rows older than the cutoff from the candle OR indicator
                # table (kind). Commit after each so the DB's RAM does not balloon.
                delete_candles_before(conn, symbol, tf, cutoff, kind=kind)
                conn.commit()
                cleaned_count += 1
            except Exception:
                # A matched table without open_time shouldn't occur; stay safe.
                conn.rollback()

        # 3. Delete weak pump/dump events (ONCE after the table loop, not for
        #    every table. Previously this ran ~12,600× due to wrong indentation).
        try:
            # Thresholds centrally in core/config.py — the same pair gates the
            # insert in the Detector (10_pump_dump_detector.py, P1.40).
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM pump_dump_events WHERE volume_ratio < %s OR ABS(price_change_60s) < %s;",
                    (PUMP_EVENT_MIN_VOL_RATIO, PUMP_EVENT_MIN_ABS_PCHG_60S),
                )
                deleted_events = cur.rowcount
            if deleted_events > 0:
                logger.info(f"🧹 HOUSEKEEPING: {deleted_events} weak pump/dump events deleted.")
            conn.commit()
        except Exception as e:
            logger.error(f"Error deleting weak pump/dump events: {e}")
            conn.rollback()

        logger.info(f"✅ Database cleaned successfully! {cleaned_count} tables checked and reduced.")

    except Exception as e:
        logger.error(f"❌ Severe error during cleanup: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


# P2.18: Gap-Filler REST with 429/418 handling. The throttle desynchronizes
# the burst across ~9k tables; the ban window stops ALL further gap-fill calls
# on 418 until expiration (continuing to iterate would extend the IP ban and
# also hit trading endpoints — exactly the P2.18 failure mode).
_GAP_FILL_THROTTLE = MinIntervalThrottle()
_GAP_FILL_MIN_INTERVAL_S = 0.25  # ~4 req/s → well below the weight limit
_GAP_FILL_MAX_RETRIES = 5
_GAP_FILL_RETRY_DEADLINE_S = 120.0
_gap_fill_ban_until = 0.0  # monotonic timestamp until which the 418-ban pause holds
# Counts 418s across the entire run (Review PR #21): Binance escalates
# repeat-offender bans — a flat 120s window would re-trigger the ban every ~2
# min if Retry-After header is missing, instead of backing off exponentially.
# Reset only on a successful call.
_gap_fill_consecutive_bans = 0


def _fetch_klines_from_binance(symbol: str, interval: str, start_ms: int, end_ms: int) -> list | None:
    """Fetches klines in the range [start_ms, end_ms] from Binance Futures REST.

    Returns None on error, otherwise a list of klines in Binance format
    [open_time, open, high, low, close, volume, ...].
    Max 1500 candles per call (Binance limit). For larger ranges the caller
    must paginate — not an issue for our gap size (usually <100 candles).

    P2.18: 429 → Retry-After-aware, budgeted backoff; 418 (IP ban) →
    process-wide ban window (>=120s), all subsequent calls return None
    immediately until expiration — the next nightly run will fetch the gaps.
    """
    global _gap_fill_ban_until, _gap_fill_consecutive_bans
    if time.monotonic() < _gap_fill_ban_until:
        return None  # 418-ban window active — don't hammer further

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
    # Unlike in fetch_ohlcv_batch, EVERY attempt (including the first) counts
    # against the budget here — there is no success pagination in this
    # single-range call (Review PR #21, RetryBudget knows both patterns).
    while budget.attempt():
        _GAP_FILL_THROTTLE.wait("binance-fapi", _GAP_FILL_MIN_INTERVAL_S)
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 418:
                _gap_fill_consecutive_bans += 1
                wait = backoff_seconds(418, _gap_fill_consecutive_bans, resp.headers.get("Retry-After"))
                _gap_fill_ban_until = time.monotonic() + wait
                logger.warning(
                    f"Gap-Fill {symbol} {interval}: 418 (IP-Ban signal #{_gap_fill_consecutive_bans}) "
                    f"— Gap-Filler paused {wait:.0f}s"
                )
                return None
            if resp.status_code == 429:
                consecutive_fail += 1
                wait = backoff_seconds(429, consecutive_fail, resp.headers.get("Retry-After"))
                logger.warning(f"Gap-Fill {symbol} {interval}: 429 — backoff {wait:.0f}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            _gap_fill_consecutive_bans = 0  # successful call ends the ban escalation
            return resp.json()
        except requests.exceptions.RequestException as e:
            consecutive_fail += 1
            logger.warning(f"Gap-Fill REST call for {symbol} {interval} failed: {e}")
            time.sleep(backoff_seconds(None, consecutive_fail))
    logger.warning(f"Gap-Fill {symbol} {interval}: retry budget exhausted ({budget.exhausted_reason()})")
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
    """Nightly gap-filler.

    Scans the last `scan_hours` hours for each coin × timeframe for missing
    candles. Gaps are refetched via Binance REST, then the indicator rows from the
    first gap onwards are deleted, so the Indicator-Engine automatically
    recalculates on its next regular run (including full 1000-candle warmup).

    Storage note (T-2026-KYT-9050-155): this reads and writes through
    `core.candles` (read_candles / upsert_candles / delete_indicators_from), so it
    follows KYTHERA_CANDLES_SOURCE and the dual-write mirror. It is NOT hardwired
    to the `{symbol}_{tf}` per-coin tables — an earlier version of this docstring
    said so, which during the T-154 incident produced a wrong "this safety net is
    dead" diagnosis and cost real debugging time.

    The caller should use `run_gap_filler()` rather than calling this directly:
    it resolves the scan window from the last successful run instead of assuming
    a fixed 24h, and records the success watermark.

    Error isolation: exceptions per coin+TF are caught, the rest continues.
    A single faulty coin does not slow down the job.

    Args:
        scan_hours: How far back to scan. Default 24h.
    """
    logger.info(f"🔍 Gap-Filler starting (scan window: {scan_hours}h, {len(TIMEFRAMES)} timeframes)...")
    start_time = time.time()

    try:
        with open("coins.json", encoding="utf-8") as f:
            data = json.load(f)
        coins = data.get("coins", data) if isinstance(data, dict) else data
        coins = [c.upper() for c in coins if c.upper().endswith("USDT")]
    except Exception as e:
        logger.error(f"Gap-Filler could not load coins.json: {e}")
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

                    # 1) Read existing CLOSED candles in the scan window.
                    #    include_forming=False: the forming candle is not a gap,
                    #    so it doesn't belong in the gap diff.
                    scan_start_dt = datetime.datetime.fromtimestamp(scan_start_ms / 1000, tz=datetime.timezone.utc)
                    try:
                        df_scan = read_candles(
                            conn, symbol, tf, start=scan_start_dt, include_forming=False, columns=("open_time",)
                        )
                    except Exception:
                        # Table probably doesn't exist yet (new coin) → skip
                        conn.rollback()
                        continue

                    if len(df_scan) < 2:
                        # No or little data in scan window — too little to detect gaps
                        continue

                    # 2) Find gaps: diff between consecutive open_times
                    #    Expected: tf_seconds; tolerance ×1.5 for minor latencies
                    expected_delta_ms = tf_seconds * 1000
                    tolerance_ms = int(expected_delta_ms * 1.5)

                    times_ms = [int(ts.timestamp() * 1000) for ts in df_scan["open_time"]]
                    gap_ranges = []  # list of (missing_start_ms, missing_end_ms)

                    for i in range(1, len(times_ms)):
                        delta = times_ms[i] - times_ms[i - 1]
                        if delta > tolerance_ms:
                            # Gap! Missing candles lie between [i-1] + expected and [i] - expected
                            gap_start = times_ms[i - 1] + expected_delta_ms
                            gap_end = times_ms[i] - expected_delta_ms
                            if gap_end >= gap_start:
                                gap_ranges.append((gap_start, gap_end))

                    if not gap_ranges:
                        continue  # no gaps in this coin+TF

                    # 3) Refetch via REST per gap-range and insert
                    first_gap_ms = gap_ranges[0][0]  # oldest gap — remember for indicator DELETE
                    candles_inserted_for_cointf = 0

                    for gap_start_ms, gap_end_ms in gap_ranges:
                        # Binance endTime is inclusive — we add expected_delta so the
                        # last candle is definitely included
                        klines = _fetch_klines_from_binance(symbol, tf, gap_start_ms, gap_end_ms + expected_delta_ms)
                        if not klines:
                            continue

                        # 4) upsert_candles per candle (closed=True) — the upsert keys
                        # on (symbol, open_time). FIX P0.9 (historical): the old INSERT
                        # left off `symbol` and used ON CONFLICT (open_time) without a
                        # matching unique index → EVERY insert failed, the except silently
                        # swallowed it, and the gap-filler was a no-op. The central
                        # upsert has the PK correct. Savepoint per row so a single error
                        # doesn't abort the transaction for the rest of the batch.
                        with conn.cursor() as cur:
                            for k in klines:
                                try:
                                    # SAVEPOINT as FIRST statement in try — if it came
                                    # after parsing, a parse error in row N would roll back
                                    # to the savepoint before row N-1's insert and silently
                                    # delete its candle again.
                                    cur.execute("SAVEPOINT gap_fill_row")
                                    ot_ms = int(k[0])
                                    # Only insert MISSING candles [gap_start, gap_end]. The
                                    # right boundary gap_end + expected_delta is times[i] —
                                    # the already-existing candle AFTER the gap that the scan
                                    # saw; exclude it via >=. Otherwise it (no-op upsert) would
                                    # count as "filled" and falsely trigger indicator
                                    # invalidation below on unfillable gaps (Review
                                    # T-2026-CU-9050-114).
                                    if ot_ms < gap_start_ms or ot_ms >= gap_end_ms + expected_delta_ms:
                                        continue
                                    ot = datetime.datetime.fromtimestamp(ot_ms / 1000, tz=datetime.timezone.utc)
                                    row = (
                                        symbol,
                                        ot,
                                        float(k[1]),
                                        float(k[2]),
                                        float(k[3]),
                                        float(k[4]),
                                        float(k[5]),
                                    )
                                    # Gap candles are historical → closed=True. Only genuinely
                                    # missing candles reach here (the pre-existing right boundary
                                    # is filtered out above), so the return (rows sent) equals rows
                                    # actually filled and the ==0 guard below stays meaningful.
                                    # upsert_candles' DO UPDATE ... IS DISTINCT FROM replaces the
                                    # old DO NOTHING (no WAL churn on no-ops) and opens a second
                                    # cursor inside the SAVEPOINT frame (same transaction), so a bad
                                    # row is isolated to its row.
                                    candles_inserted_for_cointf += upsert_candles(conn, symbol, tf, [row], closed=True)
                                except Exception as row_err:
                                    # FIX P0.9: log errors instead of silently swallowing them
                                    logger.warning(f"Gap-Filler: insert error {symbol} {tf} @ {k[0]}: {row_err}")
                                    try:
                                        cur.execute("ROLLBACK TO SAVEPOINT gap_fill_row")
                                    except Exception:
                                        break  # transaction no longer usable — abort batch
                                    continue
                        conn.commit()

                    if candles_inserted_for_cointf == 0:
                        # Gaps detected but REST delivered nothing (or all duplicates)
                        continue

                    # 5) Indicator invalidation: delete all rows from first_gap onwards
                    first_gap_dt = datetime.datetime.fromtimestamp(first_gap_ms / 1000, tz=datetime.timezone.utc)
                    rows_invalidated = 0
                    try:
                        rows_invalidated = delete_indicators_from(conn, symbol, tf, first_gap_dt)
                        conn.commit()
                    except Exception:
                        # Indicator table may not exist — harmless, engine will rebuild it
                        conn.rollback()

                    logger.info(
                        f"🔧 {symbol}_{tf}: {candles_inserted_for_cointf} candles filled "
                        f"(from {first_gap_dt.strftime('%Y-%m-%d %H:%M UTC')}), "
                        f"{rows_invalidated} indicator rows invalidated"
                    )
                    total_coins_affected += 1
                    total_candles_filled += candles_inserted_for_cointf
                    total_indicator_rows_invalidated += rows_invalidated

                    # Be nice to Binance API: short pause between coins with gaps
                    time.sleep(0.1)

                except Exception as e:
                    logger.warning(f"Gap-Filler error for {symbol}_{tf}: {e}")
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
        logger.info(f"✅ Gap-Filler done: no gaps found ({duration:.1f}s).")
    else:
        logger.info(
            f"✅ Gap-Filler done: {total_coins_affected} coin+TF combinations affected, "
            f"{total_candles_filled} candles refetched, "
            f"{total_indicator_rows_invalidated} indicator rows invalidated. "
            f"Duration: {duration:.1f}s. "
            f"Indicator-Engine will automatically recalculate on next run."
        )


def main():
    logger.info("=== 🛡️ HOUSEKEEPING SERVICE STARTED ===")
    logger.info("Running initial pass...")

    # 0. Initial run when starting the script (if manually restarted)
    update_coins_json()
    # Run immediately after update_coins_json so newly delisted coins are
    # cleaned up right away — don't wait until the next 03:00 cycle.
    cleanup_delisted_trades()
    update_max_leverage_json()

    # T-2026-KYT-9050-155: a restart after an outage is exactly when a candle gap
    # exists and, before this, exactly when nothing looked for it — the filler ran
    # only at 03:00. Gated on the watermark so an ordinary restart stays cheap.
    _start_now = datetime.datetime.now(datetime.timezone.utc)
    _run_now, _why = should_gap_fill_on_start(_read_gap_watermark(), _start_now)
    if _run_now:
        logger.warning(f"⏰ startup gap-fill: {_why}")
        run_gap_filler(_start_now)
    else:
        logger.info(f"startup gap-fill skipped: {_why}")

    logger.info("Waiting in the background for 03:00 UTC...")

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)

        # Right at 03:00 (minute 0)
        if now.hour == 3 and now.minute == 00:
            logger.info("⏰ 03:00 reached. Starting nightly maintenance...")

            # 1. Fetch new coins (if Binance listed/delisted any)
            update_coins_json()

            # 2. Clean up delisted trades (after update_coins_json so the
            # freshly updated coins.json is used)
            cleanup_delisted_trades()

            # 3. Update maximum leverage
            update_max_leverage_json()

            # 4. Delete old data in the DB (including weak pump/dumps)
            clean_old_database_entries()

            # 5. Delete old images
            cleanup_generated_charts("generated_charts")  # 7_pattern_detector
            cleanup_generated_charts("charts")  # core/charting.py
            # P3.11: 22_ip_pattern_bot writes here and nothing was cleaning it →
            # unbounded growth. Same outbox-referenced guard as the others.
            cleanup_generated_charts("institutional_charts")

            # 6. FIX: Delete old (sent) outbox entries — otherwise the table
            # grows unbounded.
            cleanup_telegram_outbox(max_age_days=7)

            # 6b. P3.2: cap the unrotated dashboard.log pipe.
            truncate_oversized_logs()

            # 7. Nightly gap-filler: scans for missing candles, refetches them
            # via Binance REST, and invalidates the corresponding indicator
            # entries from the first gap onwards. Indicator-Engine recalculates
            # automatically on its next 30-minute run (including 1000-candle
            # warmup) — no jumps in values.
            # T-2026-KYT-9050-155: the window now covers everything since the last
            # successful run instead of a fixed 24h — a missed run used to make its
            # gap permanently invisible.
            run_gap_filler(now)

            # Sleep 65 seconds so it reaches 03:01
            # and doesn't trigger the routine again today
            logger.info("💤 Maintenance complete. Sleeping until tomorrow 03:00...")
            time.sleep(65)

        # Brief check every 30 seconds is enough (saves CPU)
        time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manually stopped (Ctrl+C). Shutting down cleanly...")
