import asyncio
import datetime
import json
import random
import time
import warnings
from concurrent.futures import ProcessPoolExecutor  # Catch-up in separate processes (GIL fix)

import pytz
import requests
import websockets

try:
    # Optional (T-2026-CU-9050-169, measure 5): faster parsing of
    # ~2-3k WS messages/s. Not in fleet requirements — without installation
    # runs unchanged stdlib-json (parse result is identical).
    import orjson
except ImportError:
    orjson = None

from core.candles import (
    candles_write_primary,
    latest_open_time,
    period_start,
    upsert_candles,
    upsert_candles_many,
)
from core.database import get_db_connection
from core.http_retry import RetryBudget, backoff_seconds
from core.market_utils import load_coins  # pure coins.json re-read (P2.15)
from core.ws_utils import apply_keepalive as _apply_keepalive

warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

# --- IMPORT CONFIGURATION FROM CORE ---
from core.coins import refresh_coins_json
from core.config import BASE_URL, NUM_WORKERS, TIMEFRAMES

# File logging (logs/DATA_INGESTION.log) instead of invisible console —
# without it, WS disconnects and flush errors in production were not diagnosable.
from core.logging_setup import setup_logging

logger = setup_logging("DATA_INGESTION")

# --- GLOBAL RAM BUFFER FOR WEBSOCKETS ---
WS_KLINE_BUFFER = {}

# Timestamp of the last real WS data message (across all workers).
# Governs the REST freshness fallback: if WS delivers, fallback sleeps.
WS_LAST_DATA_TS = 0.0


# PHASE 0: UPDATE COIN LIST
def update_trading_pairs(filename='coins.json'):
    """Fetches the latest futures pairs from Binance.

    Filter + atomic write live in ``core.coins`` (the single coins.json writer,
    P2.16) so this and ``6_housekeeping.update_coins_json`` cannot drift apart.
    On any refresh failure we fall back to the on-disk list (startup needs a
    coin set to bring up the WS fleet) and never truncate the live file.
    """
    logger.info("Updating coin list...")
    try:
        trading_pairs = refresh_coins_json(BASE_URL, filename)
        logger.info(f"✅ {len(trading_pairs)} pairs in '{filename}' saved.")
        return trading_pairs
    except Exception as e:
        logger.error(f"Error during coin update: {e}")
        try:
            with open(filename) as f:
                return json.load(f)
        except Exception:
            return ["BTCUSDT", "ETHUSDT"]


# PHASE 1: THE TURBO-SCRAPER (REST API Catch-Up)


def create_table_if_needed(conn, symbol, timeframe):
    tablename = f'"{symbol}_{timeframe}"'
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {tablename} (
                    symbol TEXT, open_time TIMESTAMP WITH TIME ZONE,
                    open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
                    close DOUBLE PRECISION, volume DOUBLE PRECISION,
                    PRIMARY KEY (symbol, open_time)
                );
            """)
        conn.commit()
    except Exception:
        conn.rollback()


def get_latest_open_time(conn, symbol, timeframe):
    # Resume/catch-up watermark: the newest row we wrote, forming or not
    # (include_forming=True) — byte-equal to the old to_regclass + MAX(open_time).
    # The API returns None for a missing table; the try/except preserves the
    # original resume semantics (any error → None + rollback, never a crash).
    try:
        return latest_open_time(conn, symbol, timeframe, include_forming=True)
    except Exception:
        conn.rollback()
        return None


def fetch_ohlcv_batch(session, symbol, interval, start_ts, end_ts):
    url = BASE_URL + '/fapi/v1/klines'
    all_data = []
    curr = start_ts
    # P2.14: budgeted retry instead of while-True — a stuck symbol must not
    # block the 12h catch-up any longer; only failed attempts count against the
    # budget, successful pages paginate freely. 418 = Binance IP-ban escalation:
    # never below 120s, exponential (further hammering extends the ban).
    # With budget exhausted, already fetched partial data is used — the next
    # 12h run resumes at MAX(open_time).
    budget = RetryBudget(max_attempts=CATCHUP_MAX_RETRIES, deadline_s=CATCHUP_RETRY_DEADLINE_S)
    consecutive_fail = 0
    while True:
        params = {'symbol': symbol, 'interval': interval, 'startTime': curr, 'endTime': end_ts, 'limit': 1500}
        try:
            resp = session.get(url, params=params, timeout=10)
            if resp.status_code in [429, 418]:
                if not budget.attempt():
                    logger.warning(
                        f"Catch-up {symbol} {interval}: retry budget exhausted "
                        f"({budget.exhausted_reason()}) — {len(all_data)} candles partial data will be used."
                    )
                    break
                consecutive_fail += 1
                wait = backoff_seconds(resp.status_code, consecutive_fail, resp.headers.get("Retry-After"))
                if resp.status_code == 418:
                    logger.warning(f"Catch-up {symbol} {interval}: 418 (IP ban signal) — backoff {wait:.0f}s")
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                break

            consecutive_fail = 0
            data = resp.json()
            if not data:
                break
            all_data.extend(data)

            curr = data[-1][6] + 1
            if curr >= end_ts:
                break
            time.sleep(0.1)  # Reduced for more speed, limit at Binance Futures is quite high
        except Exception:
            if not budget.attempt():
                logger.warning(
                    f"Catch-up {symbol} {interval}: retry budget exhausted "
                    f"({budget.exhausted_reason()}) — {len(all_data)} candles partial data will be used."
                )
                break
            consecutive_fail += 1
            time.sleep(backoff_seconds(None, consecutive_fail))
    return all_data


def insert_fast(conn, data, symbol, timeframe):
    if not data:
        return 0
    tuples = []
    for row in data:
        ts = datetime.datetime.fromtimestamp(row[0] / 1000, pytz.utc)
        tuples.append((symbol, ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])))

    # The REST catch-up returns closed history plus, as its last row, possibly the
    # currently-forming candle. upsert_candles() takes one `closed` bool per call,
    # so split on the clock: open_time < period_start(tf, now) is closed, the rest
    # (at most the current period) is forming. This is where the real is_closed
    # flag enters via REST. The IS DISTINCT FROM no-op guard (audit D3: no WAL
    # churn on identical re-upserts) lives inside upsert_candles. Both calls share
    # one transaction; this function is the caller and commits once (hard rule 8).
    cutoff = period_start(timeframe, datetime.datetime.now(pytz.utc))
    closed_rows = [t for t in tuples if t[1] < cutoff]
    forming_rows = [t for t in tuples if t[1] >= cutoff]
    try:
        if closed_rows:
            upsert_candles(conn, symbol, timeframe, closed_rows, closed=True)
        if forming_rows:
            upsert_candles(conn, symbol, timeframe, forming_rows, closed=False)
        conn.commit()
        return len(tuples)
    except Exception:
        conn.rollback()
        return 0


def process_coin(symbol, resume_points):
    try:
        time.sleep(random.uniform(0.1, 1.0))  # Light jitter against rate limits
        conn = get_db_connection()
        session = requests.Session()
        session.headers.update({"User-Agent": "CryptoBot/2.0"})

        now = datetime.datetime.now(datetime.timezone.utc)
        end_ts = int(now.timestamp() * 1000)

        for tf in TIMEFRAMES:
            create_table_if_needed(conn, symbol, tf)

            latest_db = resume_points.get(f"{symbol}_{tf}")

            # Gap-aware catch-up (Audit 02/P1.11 follow-up fix): Previously ALWAYS
            # took min(latest_db, now-7d) → 7-day full rewrite for ~5,500 combos
            # on EVERY start (~20+ min full load, GIL starvation of WS loop).
            # The 7d rewrite was just the workaround for the boundary-overwrite bug
            # (buffer key without open_time) — that is now fixed. 24h overlap
            # remains as safety net (covers WS gaps + partial candles).
            if latest_db:
                start_dt = latest_db - datetime.timedelta(hours=24)
            else:
                # Fallback if table is completely empty (e.g. new coin)
                start_dt = now - datetime.timedelta(days=730)

            start_ts = int(start_dt.timestamp() * 1000)

            if start_ts >= end_ts:
                continue
            raw = fetch_ohlcv_batch(session, symbol, tf, start_ts, end_ts)
            if raw:
                insert_fast(conn, raw, symbol, tf)

    except Exception as e:
        logger.error(f"Error {symbol}: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()
        if 'session' in locals():
            session.close()


def create_db_snapshot(symbols):
    """Scans the DB and records the exact state of all coins."""
    resume_points = {}
    conn = get_db_connection()
    try:
        for sym in symbols:
            for tf in TIMEFRAMES:
                latest = get_latest_open_time(conn, sym, tf)
                if latest:
                    resume_points[f"{sym}_{tf}"] = latest
        return resume_points
    finally:
        conn.close()


def _catchup_child_low_priority():
    """Initializer for catch-up child processes: BELOW_NORMAL priority.

    Second part of WS stability fix: even with ProcessPool, catch-up children
    starved the WS event loop at 100% total CPU (10 cores: catch-up + engine
    cycle + 25 bots) at the OS scheduler level. BELOW_NORMAL means: catch-up
    uses only CPU that no one else wants — the WS process wins.
    """
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


def run_catchup_job(symbols):
    """Executes the REST catch-up with snapshot — in SEPARATE PROCESSES.

    GIL fix (cause of cyclic WS disconnects): previously catch-up ran in the
    ThreadPool OF THE SAME process as the WS event loop. JSON/insert threads
    fought for minutes with the event loop for the GIL → WS consumer lagged →
    TCP backpressure → Binance disconnects slow consumer → DATA_STALE cycle.
    ProcessPool isolates CPU work completely from the event loop's GIL; each
    child has its own DB pool.
    """
    logger.info("📸 Creating database snapshot for REST catch-up...")
    resume_points = create_db_snapshot(symbols)

    logger.info(f"⏳ Starting REST catch-up (gap-aware, 24h overlap) for {len(symbols)} coins...")
    with ProcessPoolExecutor(max_workers=NUM_WORKERS, initializer=_catchup_child_low_priority) as exe:
        # Per symbol pass only its own resume points (small & pickleable).
        futures = {
            exe.submit(
                process_coin,
                sym,
                {f"{sym}_{tf}": resume_points[f"{sym}_{tf}"] for tf in TIMEFRAMES if f"{sym}_{tf}" in resume_points},
            ): sym
            for sym in symbols
        }
        for fut, sym in futures.items():
            try:
                fut.result()
            except Exception as e:
                logger.error(f"Catch-up error {sym}: {e}")
    logger.info("✅ REST catch-up complete!")


# Warm-up: let the WS fleet connect first (stagger ~70s) and live candles
# flow BEFORE catch-up pulls CPU. Live data is there immediately,
# history comes 2 min later — instead of the other way around.
CATCHUP_WARMUP_SEC = 120

# P2.14: retry budget per symbol×TF batch in 12h catch-up. Only failed attempts
# (429/418/network errors) count; successful pages paginate freely.
CATCHUP_MAX_RETRIES = 8
CATCHUP_RETRY_DEADLINE_S = 300.0

# ── REST FRESHNESS FALLBACK (WS outage bridge) ────────────────────────────────
# Binance can throttle this IP at the WS data level (seen on 04.07.: handshake
# ok, but 0 messages, even on individual streams — REST runs normally meanwhile).
# So the fleet doesn't trade on stale data, this loop keeps hot TFs fresh via REST
# (limit=2 → weight 1 per request; 657 coins × 3 TFs at ~3 req/s ≈ 180 weight/min
# of 2400 allowed — safe). It is INACTIVE as long as WS delivers (WS_LAST_DATA_TS
# < 3 min old).
FRESHNESS_HOT_TFS = ['5m', '30m', '1h']
FRESHNESS_WS_HEALTHY_SEC = 180  # WS data fresher than this → fallback sleeps
FRESHNESS_REQ_SPACING_SEC = 0.3  # ~3 req/s
FRESHNESS_IDLE_SLEEP_SEC = 60


def run_freshness_job(symbols):
    """One run: latest 2 candles of hot TFs for all coins via REST."""
    conn = get_db_connection()
    session = requests.Session()
    session.headers.update({"User-Agent": "CryptoBot/2.0"})
    updated = 0
    try:
        # TF-prioritised instead of symbol-wise: first ALL coins 5m (~3.5 min cycle),
        # then 30m, then 1h. This keeps the time-critical TF for each symbol
        # under the 12-min DATA_STALE limit, instead of Z coins lagging by
        # ~20-min cycles on all TFs.
        for tf in FRESHNESS_HOT_TFS:
            for sym in symbols:
                # Stop as soon as WS delivers again — no double effort.
                if time.time() - WS_LAST_DATA_TS < FRESHNESS_WS_HEALTHY_SEC:
                    logger.info("🔌 Freshness fallback: WS delivers again — run aborted.")
                    return updated
                try:
                    resp = session.get(
                        BASE_URL + '/fapi/v1/klines',
                        params={'symbol': sym, 'interval': tf, 'limit': 2},
                        timeout=10,
                    )
                    if resp.status_code in (429, 418):
                        wait = int(resp.headers.get("Retry-After", 30)) + 2
                        logger.warning(f"Freshness fallback: rate limit ({resp.status_code}), waiting {wait}s")
                        time.sleep(wait)
                        continue
                    if resp.status_code == 200:
                        updated += insert_fast(conn, resp.json(), sym, tf)
                except Exception:
                    pass
                time.sleep(FRESHNESS_REQ_SPACING_SEC)
        return updated
    finally:
        conn.close()
        session.close()


async def freshness_fallback_loop(tracked):
    """Background bridge: keeps hot TFs fresh if (and only if) WS is dead.

    ``tracked`` is the shared symbol set (mutated by coin refresh); per run,
    snapshotted so newly pulled coins get freshness coverage.
    """
    loop = asyncio.get_running_loop()
    await asyncio.sleep(CATCHUP_WARMUP_SEC + 60)  # WS + first catch-up first
    logger.info("🩹 Freshness fallback ready (activates only with dead WS).")
    while True:
        if time.time() - WS_LAST_DATA_TS < FRESHNESS_WS_HEALTHY_SEC:
            await asyncio.sleep(FRESHNESS_IDLE_SLEEP_SEC)
            continue
        logger.warning("🩹 WS delivers no data — REST freshness run starts (hot TFs).")
        updated = await loop.run_in_executor(None, run_freshness_job, sorted(tracked))
        logger.info(f"🩹 Freshness run complete: {updated} candle upserts.")
        await asyncio.sleep(FRESHNESS_IDLE_SLEEP_SEC)


async def periodic_rest_catchup(tracked):
    """Background loop: first run after warm-up, then every 12h.

    ``tracked`` is the shared symbol set (mutated by coin refresh); per cycle,
    snapshotted so newly pulled coins get 12h coverage.
    """
    loop = asyncio.get_running_loop()
    logger.info(f"⏳ Catch-up waiting {CATCHUP_WARMUP_SEC}s (let WS fleet connect first)...")
    await asyncio.sleep(CATCHUP_WARMUP_SEC)
    while True:
        # Offload to thread (doesn't block loop); CPU work itself happens
        # in ProcessPool child processes.
        await loop.run_in_executor(None, run_catchup_job, sorted(tracked))

        logger.info("💤 Catch-up job sleeping for 12 hours...")
        await asyncio.sleep(12 * 3600)  # Wait 12 hours


async def _spawn_ws_workers_for(new_symbols):
    """Spawn additional WS workers for new symbols (additive, sharding + stagger
    like in initial fleet). Workers write to the same WS_KLINE_BUFFER; the
    global db_buffer_flusher (started in initial fleet) persists them."""
    for idx, chunk in enumerate(_new_symbol_stream_chunks(new_symbols)):
        wid = _allocate_ws_worker_id()
        startup_delay = idx * WS_STARTUP_STAGGER_SEC  # Respect 300-connects/5min limit
        asyncio.create_task(binance_ws_worker(wid, chunk, startup_delay=startup_delay))
        logger.info(f"🆕 WS worker {wid}: {len(chunk)} new streams started.")


async def coin_refresh_loop(tracked):
    """P2.15: pulls newly appeared coins from coins.json without process restart.

    Per new symbol: tables + one-time 730d catch-up (child processes, GIL-free)
    and one additional WS worker. ``tracked`` is shared with catch-up/freshness
    loops (which snapshot it per cycle), so new coins also get 12h catch-up and
    freshness coverage. Only after catch-up + WS is the symbol marked as known
    (else a parallel loop would see it without its table)."""
    loop = asyncio.get_running_loop()
    await asyncio.sleep(CATCHUP_WARMUP_SEC + 60)  # WS fleet + first catch-up first
    logger.info("🆕 Coin refresh ready (pulls new listings without restart).")
    while True:
        await asyncio.sleep(COIN_REFRESH_INTERVAL_SEC)
        try:
            new_symbols = compute_new_symbols(set(load_coins()), tracked)
            if not new_symbols:
                continue
            preview = ", ".join(new_symbols[:10]) + (" …" if len(new_symbols) > 10 else "")
            logger.info(f"🆕 {len(new_symbols)} new coins in coins.json: {preview}")
            # 1. Tables + one-time catch-up (child processes, doesn't block loop)
            await loop.run_in_executor(None, run_catchup_job, new_symbols)
            # 2. Pull WS streams additively
            await _spawn_ws_workers_for(new_symbols)
            # 3. Only now mark as known (catch-up + WS are live)
            tracked.update(new_symbols)
            logger.info(f"✅ {len(new_symbols)} new coins live (tables + catch-up + WS).")
        except Exception as e:
            logger.error(f"Coin refresh error: {e}")


# PHASE 2 & 3: WEBSOCKET STREAMING & DB FLUSHER
async def db_buffer_flusher():
    """Writes the RAM buffer to DB every 3 seconds resource-efficiently.

    Important: atomic swap instead of copy-then-clear, so WS messages arriving
    between the two operations don't get lost.
    """
    global WS_KLINE_BUFFER
    logger.info("💾 DB buffer flusher started (interval: 3s)")
    while True:
        await asyncio.sleep(3)
        if not WS_KLINE_BUFFER:
            continue

        # Atomic swap: we exchange the buffer in ONE statement.
        # Old content goes to buffer_copy, new empty buffer is immediately active.
        # Since Python asyncio is single-threaded, no other coroutine can run
        # between the two assignments (no await in between).
        buffer_copy = WS_KLINE_BUFFER
        WS_KLINE_BUFFER = {}

        try:
            await asyncio.to_thread(_flush_to_db, buffer_copy)
        except Exception as e:
            logger.error(f"Error during DB flush: {e}")


# Persistent flush connection (T-2026-CU-9050-169): previously every 3s flush
# opened/closed its own connection. Only the flusher thread uses it —
# db_buffer_flusher awaits every asyncio.to_thread call, so _flush_to_db never
# runs concurrently. On any error it is discarded and rebuilt on the next flush
# (monitor pattern: ensure/reset).
_FLUSH_CONN = None


def _get_flush_conn():
    global _FLUSH_CONN
    if _FLUSH_CONN is None or getattr(_FLUSH_CONN, "closed", 1):
        _FLUSH_CONN = get_db_connection()
    return _FLUSH_CONN


def _reset_flush_conn():
    global _FLUSH_CONN
    try:
        if _FLUSH_CONN is not None:
            _FLUSH_CONN.close()
    except Exception:
        pass
    _FLUSH_CONN = None


def _flush_groups_fallback(conn, buffer_copy):
    """Group flush: one upsert_candles per (symbol, tf, closed) group.

    Fallback and legacy primary path. SAVEPOINT per GROUP not per row
    (T-2026-CU-9050-169): the real error class — missing per-coin table on
    legacy backend — always affects the entire (symbol, tf) group; row-level
    isolation cost ~2 extra statements per candle. Semantics as before: a
    faulty group is discarded and logged, all others commit. upsert_candles
    carries the D3 IS DISTINCT FROM no-op guard and persists is_closed per call
    (that's why `closed` is part of the group key — the flag semantics per
    candle remain exactly preserved).
    """
    groups: dict = {}
    for (sym, tf, _open_time), (row, closed) in buffer_copy.items():
        groups.setdefault((sym, tf, closed), []).append(row)

    failed_tables = set()
    success_rows = 0
    total_rows = len(buffer_copy)
    with conn.cursor() as cur:
        for i, ((sym, tf, closed), rows) in enumerate(groups.items()):
            sp_name = f"sp_{i}"
            try:
                cur.execute(f"SAVEPOINT {sp_name}")
                upsert_candles(conn, sym, tf, rows, closed=closed)
                cur.execute(f"RELEASE SAVEPOINT {sp_name}")
                success_rows += len(rows)
            except Exception as grp_err:
                cur.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                # Log only once per table, not per group/row
                if (sym, tf) not in failed_tables:
                    failed_tables.add((sym, tf))
                    logger.warning(f"Insert error for {sym}_{tf}: {grp_err}")
    conn.commit()
    if failed_tables:
        logger.info(f"Flush: {success_rows}/{total_rows} successful, {len(failed_tables)} tables with errors skipped.")


def _flush_to_db(buffer_copy):
    """Helper function: writes async buffer to DB via psycopg2.

    T-2026-CU-9050-169: on Hyper primary the complete buffer goes out as ONE
    execute_values batch (upsert_candles_many — identical statement, identical
    IS DISTINCT FROM guard as the single-row path; buffer key (sym, tf,
    open_time) guarantees ON-CONFLICT uniqueness in batch). Previously ran ~3,185
    single INSERTs/s each with its own SAVEPOINT/RELEASE — the dominant DB and
    client CPU item of ingestion. If batch fails (or write primary is 'legacy'),
    group flush with SAVEPOINT isolation per (symbol, tf, closed) group applies.

    Loss semantics unchanged: conservative — a lost flush is healed by 24h
    catch-up overlap or running WS re-upserts.
    """
    try:
        conn = _get_flush_conn()
    except Exception as e:
        logger.error(f"Flush: no DB connection ({e}) — buffer discarded (catch-up overlap heals).")
        return
    try:
        if candles_write_primary() == "hyper":
            bulk_rows = [
                (sym, tf, row[1], row[2], row[3], row[4], row[5], row[6], closed)
                for (sym, tf, _open_time), (row, closed) in buffer_copy.items()
            ]
            try:
                upsert_candles_many(conn, bulk_rows)
                conn.commit()
                return
            except Exception as batch_err:
                # Rollback + fallback to isolating group path — a single bad row
                # must not cost the entire flush.
                conn.rollback()
                logger.warning(f"Batch flush failed ({batch_err}) — fallback to group flush.")
        _flush_groups_fallback(conn, buffer_copy)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error(f"Flush error (total): {e}")
        _reset_flush_conn()


# ═════════════════════════════════════════════════════════════════════════════
# WEBSOCKET FLEET
# ═════════════════════════════════════════════════════════════════════════════
# Configuration with safety margin to Binance limits:
#   - Max 1024 streams per connection → we use 800 (reserve)
#   - Max 300 connect attempts per 5min per IP → we limit strictly to 60 per worker
#     (with backoff), and initial start staggers
#   - Max 10 messages/s per connection → we send at most 1 SUBSCRIBE/s
# Binance sends its own ping every 180s; the websockets library responds automatically.
# We disable the library's own ping (ping_interval=None) to avoid the collision:
# both sides pinging simultaneously → library waits for its pong → times out → disconnect.
# Dead connections are detected by the message watchdog (WS_MESSAGE_WATCHDOG_SEC=120s).
#
# Watchdog: tracks latest message time per worker. If > 120s nothing arrives
# despite open connection, forces reconnect.
# ═════════════════════════════════════════════════════════════════════════════

# Streams per WS connection. Using URL-encoded combined stream format
# (wss://fstream.binance.com/stream?streams=s1/s2/...) instead of SUBSCRIBE
# messages — this uses the documented 1024-stream limit reliably.
# SUBSCRIBE-based connections appear to be dropped by Binance at ~150-200s
# when carrying 800+ streams, despite the documented 1024 limit.
#
# FIX HTTP 414 (URI Too Long): 860 streams resulted in ~19-KB URLs → Binance
# rejected some handshakes with 414.
# FIX SILENT-CAP (the real killer): Binance USDⓈ-M futures delivers only ~200
# streams per connection — with more the handshake is ACCEPTED but NO messages
# arrive (no error!). With 400 streams all 14 workers hit the 120s message
# watchdog and reconnected silently in a loop. The audit documented identical
# behaviour with whale-logger (P1.42: "fapi cap ~200/conn"). 180 = safety
# margin below the cap.
WS_STREAMS_PER_WORKER = 180

# Take 1d/1w from WebSocket (C-gate phase 2, D-2026-CLD-109 #3): the two
# slowest frames update at most once per day or week; a live kline stream for
# that is ~1,300 wasted streams (IP-throttle risk) for a candle that REST catch-up
# fetches every cycle anyway. They STAY on the REST/catch-up path (continue in
# `TIMEFRAMES`, unchanged) — ONLY the WS subscription pool drops them. WS stays
# for 5m–4h.
WS_EXCLUDED_TIMEFRAMES = frozenset({"1d", "1w"})
WS_TIMEFRAMES = [tf for tf in TIMEFRAMES if tf not in WS_EXCLUDED_TIMEFRAMES]

# SUBSCRIBE chunk size and spacing. Binance allows 10 msg/s per connection
# (futures); we stay at 1 msg/s = 10x safety margin, important with
# simultaneous startup of many workers.
WS_SUBSCRIBE_CHUNK_SIZE = 200
WS_SUBSCRIBE_DELAY_SEC = 1.0

# Staggered startup: on first start deploy workers staggered to not violate
# the 300-connects-per-5min rule. With ~30 workers (180 streams/conn): 5s stagger
# = 150s start spread — all workers up within 2.5 min, 30 connects/5min << limit
# 300.
WS_STARTUP_STAGGER_SEC = 5.0

# Reconnect backoff: starts at 5s, doubles, capped at 900s. Jitter ±20%
# prevents all workers reconnecting simultaneously. IMPORTANT (anti-ban): backoff
# resets only after the FIRST DATA message, not on connect — Binance can accept
# connections and stay silent (IP throttle after connect churn). With reset-on-
# connect, 30 silent workers reconnected at 120s intervals (~900 connects/h) and
# renewed the throttle endlessly.
WS_RECONNECT_MIN_SEC = 5.0
WS_RECONNECT_MAX_SEC = 900.0

# If no message arrives for longer than this many seconds → consider connection
# dead and reconnect (Binance streams tick almost constantly, especially 5m
# streams).
WS_MESSAGE_WATCHDOG_SEC = 120.0

# Unsolicited pong interval: send a pong frame every 120s as a keepalive safety net.
# Spec allows this (>15min is the documented minimum — 2min is more conservative).
# Guards against event-loop hiccups that might delay the auto-pong response.
WS_UNSOLICITED_PONG_SEC = 120.0

# Ping config (adapted to Binance futures specification)
WS_PING_INTERVAL_SEC = None  # Disable library pings — Binance sends its own ping every
# 180s and the websockets library auto-responds with pong.
# Running our own ping_interval=180 causes a collision:
# both sides send pings simultaneously → library times out
# waiting for its pong → false disconnect after ~206s.
WS_PING_TIMEOUT_SEC = None  # Not needed when ping_interval=None

# P2.15: coins.json is updated at runtime by 6_housekeeping (daily 03:00 UTC).
# Without re-read, newly listed coins get no data until process restart (no WS
# stream, no catch-up). The refresh reads coins.json periodically and pulls new
# symbols ADDITIVELY: tables + one-time catch-up + own WS worker. Conservative —
# removed coins are NOT torn down (stream teardown stays with restart), so a
# faulty (torn/empty coins.json read) missing coin doesn't drop from ingestion
# live.
COIN_REFRESH_INTERVAL_SEC = 900

# Continuous WS worker ID across initial fleet + refresh workers, so pulled
# workers get unique IDs (logs, reconnect spread).
_next_ws_worker_id = 1


def _allocate_ws_worker_id() -> int:
    global _next_ws_worker_id
    wid = _next_ws_worker_id
    _next_ws_worker_id += 1
    return wid


def compute_new_symbols(current: set, tracked: set) -> list:
    """New, not yet tracked symbols (additive, sorted).

    Conservative: empty ``current`` (torn/empty coins.json read) produces no
    change — never remove coins, never react to a faulty read. load_coins()
    returns [] on faulty read (all-or-nothing json.load).
    """
    if not current:
        return []
    return sorted(current - tracked)


def _new_symbol_stream_chunks(new_symbols: list) -> list:
    """Builds kline stream names for new symbols and shards them like the
    initial fleet (<= WS_STREAMS_PER_WORKER streams/connection)."""
    all_streams = [f"{sym.lower()}@kline_{tf}" for sym in new_symbols for tf in WS_TIMEFRAMES]
    return [all_streams[i : i + WS_STREAMS_PER_WORKER] for i in range(0, len(all_streams), WS_STREAMS_PER_WORKER)]


async def binance_ws_worker(worker_id: int, streams: list, startup_delay: float = 0.0):
    """A single WebSocket worker with robust reconnect and watchdog.

    Improvements vs. old version:
      - Staggered startup: initial delay prevents 16 connects at once
      - Exponential backoff with jitter on disconnects
      - Message watchdog: forces reconnect if no data for too long
      - Unique SUBSCRIBE IDs (worker_id * 1000 + chunk_idx)
      - Subscribe response check (with timeout)
      - ping_interval/timeout adapted to Binance specification
      - Slower subscribes (1s instead of 0.5s) to respect msg/s limit
    """
    if startup_delay > 0:
        logger.info(f"⏳ WS worker {worker_id} waiting {startup_delay:.0f}s for staggered start...")
        await asyncio.sleep(startup_delay)

    # URL-encoded combined stream — all streams in the query string.
    # This uses the documented 1024-stream limit and avoids SUBSCRIBE messages.
    # IMPORTANT (root cause of "silent" connections, found 05.07.2026):
    # Binance shut down legacy URLs /ws and /stream on 23.04.2026 — unrouted
    # connections still handshake successfully but deliver NO /market streams
    # (kline/aggTrade/markPrice) any more. New routed URL:
    url = "wss://fstream.binance.com/market/stream?streams=" + "/".join(streams)

    backoff = WS_RECONNECT_MIN_SEC
    consecutive_failures = 0

    while True:
        connected_at = None
        try:
            async with websockets.connect(
                url,
                ping_interval=WS_PING_INTERVAL_SEC,
                ping_timeout=WS_PING_TIMEOUT_SEC,
                open_timeout=30,
                close_timeout=10,
                max_size=2**22,
            ) as ws:
                _apply_keepalive(ws)
                connected_at = datetime.datetime.now(pytz.UTC)
                logger.info(f"🟢 WS worker {worker_id} connected ({len(streams)} streams, URL-encoded)")

                # No SUBSCRIBE needed — streams are in the URL.
                # Backoff does NOT reset here, only on the first real data
                # message (silent connections count as failures — anti-ban, see
                # WS_RECONNECT_MAX_SEC).
                got_data = False

                # --- PONG TASK: unsolicited pong every 120s ---
                # Spec allows this as keepalive. Guards against event-loop
                # hiccups that could delay the library's auto-pong response.
                async def _pong_task():
                    while True:
                        await asyncio.sleep(WS_UNSOLICITED_PONG_SEC)
                        try:
                            await ws.pong()
                        except Exception:
                            break  # WS closed — let outer loop handle reconnect

                pong_task = asyncio.create_task(_pong_task())

                # --- MAIN LOOP mit Message-Watchdog ---
                last_msg_ts = datetime.datetime.now(pytz.UTC)

                try:
                    while True:
                        try:
                            # Timeout here so we can check the watchdog
                            msg = await asyncio.wait_for(
                                ws.recv(),
                                timeout=WS_MESSAGE_WATCHDOG_SEC,
                            )
                        except asyncio.TimeoutError:
                            # No message within watchdog window
                            silence_sec = (datetime.datetime.now(pytz.UTC) - last_msg_ts).total_seconds()
                            logger.warning(
                                f"⏰ WS worker {worker_id}: {silence_sec:.0f}s no messages, forcing reconnect"
                            )
                            break

                        last_msg_ts = datetime.datetime.now(pytz.UTC)

                        try:
                            # orjson if installed, else stdlib (identical result).
                            payload = orjson.loads(msg) if orjson is not None else json.loads(msg)
                        except ValueError:
                            # JSONDecodeError of both libraries is ValueError subclass.
                            continue

                        # Pass through SUBSCRIBE responses
                        if 'result' in payload:
                            if payload.get('result') is not None:
                                # Non-null result → error (Binance responds with null on success)
                                logger.warning(f"WS worker {worker_id}: subscribe error: {payload}")
                            continue

                        # Error response
                        if 'error' in payload:
                            logger.warning(f"WS worker {worker_id}: error response: {payload}")
                            continue

                        # Data message
                        if 'data' in payload and 'k' in payload['data']:
                            if not got_data:
                                got_data = True
                                consecutive_failures = 0
                                backoff = WS_RECONNECT_MIN_SEC
                            global WS_LAST_DATA_TS
                            WS_LAST_DATA_TS = time.time()
                            k = payload['data']['k']
                            sym = k['s']
                            tf = k['i']
                            open_time = datetime.datetime.fromtimestamp(k['t'] / 1000, pytz.UTC)

                            # P1.11: key incl. open_time — previously the first message of
                            # the NEW candle overwrote the final update of the old candle in
                            # the buffer (at each candle boundary), the stored "closed" candle
                            # stayed slightly wrong until REST catch-up. Value carries the
                            # real Binance closed flag k['x'] alongside the row, so _flush_to_db
                            # can persist is_closed per candle (this WS path is where the flag
                            # first enters the data model).
                            WS_KLINE_BUFFER[(sym, tf, open_time)] = (
                                (
                                    sym,
                                    open_time,
                                    float(k['o']),
                                    float(k['h']),
                                    float(k['l']),
                                    float(k['c']),
                                    float(k['v']),
                                ),
                                bool(k['x']),
                            )

                finally:
                    pong_task.cancel()
                    try:
                        await pong_task
                    except (asyncio.CancelledError, Exception):
                        pass

            # Watchdog break path (connection was open but silent): previously
            # reconnected IMMEDIATELY without backoff → 30 silent workers = 120s
            # reconnect hammer, endlessly renewing an IP throttle. Now: silent
            # connection = failed attempt with exponential backoff.
            if not got_data:
                consecutive_failures += 1
                jitter = random.uniform(0.8, 1.2)
                wait_sec = min(backoff * jitter, WS_RECONNECT_MAX_SEC)
                logger.warning(
                    f"🔇 WS worker {worker_id}: connection stayed silent — backoff {wait_sec:.0f}s "
                    f"(attempt #{consecutive_failures})"
                )
                await asyncio.sleep(wait_sec)
                backoff = min(backoff * 2.0, WS_RECONNECT_MAX_SEC)

        except asyncio.CancelledError:
            logger.info(f"🛑 WS worker {worker_id} stopped (cancelled).")
            raise
        except Exception as e:
            consecutive_failures += 1
            uptime_str = ""
            if connected_at is not None:
                uptime_sec = (datetime.datetime.now(pytz.UTC) - connected_at).total_seconds()
                uptime_str = f" (was {uptime_sec:.0f}s connected)"

            # Exponential backoff with jitter
            jitter = random.uniform(0.8, 1.2)
            wait_sec = min(backoff * jitter, WS_RECONNECT_MAX_SEC)

            # Add worker_id spread so workers don't all reconnect simultaneously
            spread_sec = (worker_id - 1) * 2.0
            total_wait = wait_sec + spread_sec
            logger.warning(
                f"🔴 WS worker {worker_id} disconnected{uptime_str}: {type(e).__name__}: {e}. "
                f"Reconnect in {total_wait:.1f}s (attempt #{consecutive_failures}, spread +{spread_sec:.0f}s)"
            )
            await asyncio.sleep(total_wait)
            # Double backoff until cap
            backoff = min(backoff * 2.0, WS_RECONNECT_MAX_SEC)


async def start_websocket_fleet(symbols):
    """Divides streams onto WS connections with staggered startup.

    Design principles:
      - Few full connections better than many half-full (Binance overhead)
      - Startup stagger prevents rate limiting on initial connect
      - Exponential backoff also engages on reconnect storm
    """
    all_streams = []
    for sym in symbols:
        for tf in WS_TIMEFRAMES:
            all_streams.append(f"{sym.lower()}@kline_{tf}")

    stream_chunks = [
        all_streams[i : i + WS_STREAMS_PER_WORKER] for i in range(0, len(all_streams), WS_STREAMS_PER_WORKER)
    ]

    n_workers = len(stream_chunks)
    logger.info(
        f"🚀 Starting {n_workers} WS workers for {len(all_streams)} streams "
        f"(~{len(all_streams) // max(n_workers, 1)} streams/worker, "
        f"stagger {WS_STARTUP_STAGGER_SEC}s between starts)"
    )

    # Warning if we could make too many connects in 5 minutes.
    # Binance allows 300 connect attempts per 5min per IP.
    expected_connects_per_5min = n_workers  # initial, without reconnects
    if expected_connects_per_5min > 60:
        logger.warning(
            f"⚠️  {n_workers} workers + reconnects could exceed Binance limit "
            f"(300 connects per 5min). Consider more streams per worker."
        )

    tasks = [db_buffer_flusher()]
    for i, chunk in enumerate(stream_chunks):
        startup_delay = i * WS_STARTUP_STAGGER_SEC
        tasks.append(binance_ws_worker(_allocate_ws_worker_id(), chunk, startup_delay=startup_delay))

    await asyncio.gather(*tasks)


# MAIN ENTRY POINT
async def main_async():
    logger.info("=== DATA INGESTION SYSTEM START ===")

    # WS stability: ingestion is the data heartbeat of the entire fleet — its
    # event loop must not starve under CPU saturation (engine cycle, bots) at the
    # OS scheduler level. ABOVE_NORMAL (not HIGH: that would contend with OS
    # itself for resources).
    try:
        import psutil

        psutil.Process().nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
        logger.info("⚡ Process priority set to ABOVE_NORMAL (WS loop protection).")
    except Exception as e:
        logger.warning(f"Priority could not be set: {e}")

    # 1. Get current list (sync)
    symbols = update_trading_pairs()
    # Shared symbol set: coin refresh (P2.15) mutates it, catch-up/freshness
    # loops snapshot it per cycle. Initial WS fleet gets the start snapshot
    # (its streams are baked into connection URLs).
    tracked = set(symbols)

    # 2. Background task for 7-day check (runs once then every 12h)
    catchup_task = asyncio.create_task(periodic_rest_catchup(tracked))

    # 3. Start WebSockets immediately
    ws_task = asyncio.create_task(start_websocket_fleet(symbols))

    # 4. REST freshness bridge (active only with dead WS — e.g. IP throttle)
    freshness_task = asyncio.create_task(freshness_fallback_loop(tracked))

    # 5. Coin refresh: pulls newly listed coins without restart (P2.15)
    refresh_task = asyncio.create_task(coin_refresh_loop(tracked))

    await asyncio.gather(catchup_task, ws_task, freshness_task, refresh_task)


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("🛑 Data ingestion stopped (Ctrl+C).")


if __name__ == "__main__":
    main()
