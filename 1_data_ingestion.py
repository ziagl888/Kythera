import asyncio
import datetime
import json
import random
import socket
import time
import warnings
from concurrent.futures import ProcessPoolExecutor  # Catch-up in eigenen Prozessen (GIL-Fix)

import pytz
import requests
import websockets
from psycopg2 import extras

from core.database import get_db_connection

warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

# --- IMPORT CONFIGURATION FROM CORE ---
from core.config import BASE_URL, NUM_WORKERS, TIMEFRAMES

# File-Logging (logs/DATA_INGESTION.log) statt nur unsichtbarer Konsole —
# ohne das waren WS-Disconnects und Flush-Fehler im Betrieb nicht diagnostizierbar.
from core.logging_setup import setup_logging

logger = setup_logging("DATA_INGESTION")

# --- GLOBAL RAM BUFFER FOR WEBSOCKETS ---
WS_KLINE_BUFFER = {}

# Zeitstempel der letzten echten WS-Daten-Message (über alle Worker).
# Steuert den REST-Freshness-Fallback: liefert der WS, schläft der Fallback.
WS_LAST_DATA_TS = 0.0


# PHASE 0: UPDATE COIN LIST
def update_trading_pairs(filename='coins.json'):
    """Fetches the latest futures pairs from Binance."""
    logger.info("Updating Coin-Liste...")
    url = BASE_URL + '/fapi/v1/exchangeInfo'
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Gleicher Filter wie 6_housekeeping.update_coins_json — beide schreiben
        # coins.json und dürfen sich nicht widersprechen. Der alte Lose-Filter
        # (nur status=TRADING) ließ Binance-Neuprodukte durch: Quote-Assets
        # "U"/"USD1" (→ Symbol "ETHU"), Cross-Pairs (ETHBTC), Quartals-Futures
        # (_260925) und TRADIFI_PERPETUAL (Aktien/Metalle) — die Flotte ist nur
        # auf USDT-Krypto-Perpetuals validiert (Vorfall 2026-07-06: ABR2-Signale
        # auf ETHU/COSTUSDT/XAUUSDT).
        trading_pairs = [
            symbol['symbol']
            for symbol in data['symbols']
            if symbol['quoteAsset'] == 'USDT'
            and symbol['status'] == 'TRADING'
            and symbol['contractType'] == 'PERPETUAL'
        ]

        with open(filename, 'w') as f:
            json.dump(trading_pairs, f, indent=2)
        logger.info(f"✅ {len(trading_pairs)} pairs in '{filename}' saved.")
        return trading_pairs
    except Exception as e:
        logger.error(f"Error during coin update: {e}")
        try:
            with open(filename) as f:
                return json.load(f)
        except Exception:
            return ["BTCUSDT", "ETHUSDT"]


# PHASE 1: DER TURBO-GREPPER (REST API Catch-Up)


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
    tablename = f'"{symbol}_{timeframe}"'
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s)", (tablename,))
            if cursor.fetchone()[0] is None:
                return None
            cursor.execute(f'SELECT MAX(open_time) FROM {tablename}')
            res = cursor.fetchone()
            return res[0].astimezone(pytz.utc) if res and res[0] else None
    except Exception:
        conn.rollback()
        return None


def fetch_ohlcv_batch(session, symbol, interval, start_ts, end_ts):
    url = BASE_URL + '/fapi/v1/klines'
    all_data = []
    curr = start_ts
    while True:
        params = {'symbol': symbol, 'interval': interval, 'startTime': curr, 'endTime': end_ts, 'limit': 1500}
        try:
            resp = session.get(url, params=params, timeout=10)
            if resp.status_code in [429, 418]:
                wait = int(resp.headers.get("Retry-After", 10)) + 2
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                break

            data = resp.json()
            if not data:
                break
            all_data.extend(data)

            curr = data[-1][6] + 1
            if curr >= end_ts:
                break
            time.sleep(0.1)  # Reduziert für mehr Speed, Limit bei Binance Futures ist recht hoch
        except Exception:
            time.sleep(5)
    return all_data


def insert_fast(conn, data, symbol, timeframe):
    if not data:
        return 0
    tablename = f'"{symbol}_{timeframe}"'
    tuples = []
    for row in data:
        ts = datetime.datetime.fromtimestamp(row[0] / 1000, pytz.utc)
        tuples.append((symbol, ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])))

    # D3 (Audit Report 18 A1): das WHERE macht den Upsert zum No-op, wenn sich
    # nichts geaendert hat — sonst schreibt jeder identische Re-Upsert eine neue
    # Row-Version ins WAL (~2,8 Mio sinnlose Updates/Tag allein auf 5m).
    sql = f"""
        INSERT INTO {tablename} AS t (symbol, open_time, open, high, low, close, volume)
        VALUES %s ON CONFLICT (symbol, open_time) DO UPDATE
        SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close, volume=EXCLUDED.volume
        WHERE (t.open, t.high, t.low, t.close, t.volume)
              IS DISTINCT FROM (EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.volume)
    """
    try:
        with conn.cursor() as cur:
            extras.execute_values(cur, sql, tuples)
        conn.commit()
        return len(tuples)
    except Exception:
        conn.rollback()
        return 0


def process_coin(symbol, resume_points):
    try:
        time.sleep(random.uniform(0.1, 1.0))  # Leichter Jitter gegen Rate-Limits
        conn = get_db_connection()
        session = requests.Session()
        session.headers.update({"User-Agent": "CryptoBot/2.0"})

        now = datetime.datetime.now(datetime.timezone.utc)
        end_ts = int(now.timestamp() * 1000)

        for tf in TIMEFRAMES:
            create_table_if_needed(conn, symbol, tf)

            latest_db = resume_points.get(f"{symbol}_{tf}")

            # Gap-aware Catch-up (Audit 02/P1.11-Folgefix): Vorher wurde IMMER
            # min(latest_db, now-7d) genommen → 7-Tage-Vollrewrite für ~5.500
            # Kombos bei JEDEM Start (~20+ min Vollast, GIL-Starvation der WS-Loop).
            # Der 7d-Rewrite war nur die Krücke für den Boundary-Overwrite-Bug
            # (Buffer-Key ohne open_time) — der ist jetzt gefixt. 24h-Overlap
            # bleibt als Sicherheitsnetz (deckt WS-Lücken + Partial-Kerzen ab).
            if latest_db:
                start_dt = latest_db - datetime.timedelta(hours=24)
            else:
                # Fallback, wenn die Tabelle komplett leer ist (z.B. neuer Coin)
                start_dt = now - datetime.timedelta(days=730)

            start_ts = int(start_dt.timestamp() * 1000)

            if start_ts >= end_ts:
                continue
            raw = fetch_ohlcv_batch(session, symbol, tf, start_ts, end_ts)
            if raw:
                insert_fast(conn, raw, symbol, tf)

    except Exception as e:
        logger.error(f"Fehler {symbol}: {e}")
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
    """Initializer für Catch-up-Child-Prozesse: BELOW_NORMAL-Priorität.

    Zweiter Teil des WS-Stabilitäts-Fixes: Auch mit ProcessPool starvten die
    Catch-up-Kinder bei 100% Gesamt-CPU (10 Kerne: Catch-up + Engine-Zyklus +
    25 Bots) die WS-Event-Loop auf OS-Scheduler-Ebene. BELOW_NORMAL heißt:
    Catch-up nutzt nur CPU, die sonst niemand will — der WS-Prozess gewinnt.
    """
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


def run_catchup_job(symbols):
    """Führt den REST-Catch-Up mit Snapshot aus — in EIGENEN PROZESSEN.

    GIL-Fix (Ursache der zyklischen WS-Disconnects): Vorher lief der Catch-up
    im ThreadPool DESSELBEN Prozesses wie die WS-Event-Loop. Die JSON-/Insert-
    Threads kämpften minutenlang mit der Event-Loop um den GIL → der WS-Consumer
    kam nicht hinterher → TCP-Backpressure → Binance trennt Slow-Consumer →
    DATA_STALE-Zyklus. ProcessPool isoliert die CPU-Arbeit komplett vom GIL
    der Event-Loop; jeder Child hat seinen eigenen DB-Pool.
    """
    logger.info("📸 Erstelle Datenbank-Snapshot für REST Catch-Up...")
    resume_points = create_db_snapshot(symbols)

    logger.info(f"⏳ Starting REST Catch-Up (gap-aware, 24h-Overlap) für {len(symbols)} Coins...")
    with ProcessPoolExecutor(max_workers=NUM_WORKERS, initializer=_catchup_child_low_priority) as exe:
        # Pro Symbol nur dessen eigene Resume-Points übergeben (klein & picklebar).
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
                logger.error(f"Catch-up-Fehler {sym}: {e}")
    logger.info("✅ REST Catch-Up vollständig completed!")


# Warm-up: Die WS-Fleet zuerst verbinden lassen (Stagger ~70s) und Live-Kerzen
# fließen lassen, BEVOR der Catch-up CPU zieht. Live-Daten sind sofort da,
# die Historie kommt 2 min später — statt umgekehrt.
CATCHUP_WARMUP_SEC = 120

# ── REST-FRESHNESS-FALLBACK (WS-Ausfall-Brücke) ──────────────────────────────
# Binance kann diese IP auf WS-Datenebene drosseln (erlebt am 04.07.: Handshake
# ok, aber 0 Messages, auch auf Einzelstreams — REST läuft dabei normal weiter).
# Damit die Fleet dann nicht auf stundenaltem Stand handelt, hält diese Schleife
# die heißen TFs per REST frisch (limit=2 → Weight 1 pro Request; 657 Coins ×
# 3 TFs bei ~3 req/s ≈ 180 Weight/min von 2400 erlaubten — ungefährlich).
# Sie ist STROMLOS, solange der WS liefert (WS_LAST_DATA_TS < 3 min alt).
FRESHNESS_HOT_TFS = ['5m', '30m', '1h']
FRESHNESS_WS_HEALTHY_SEC = 180  # WS-Daten jünger als das → Fallback schläft
FRESHNESS_REQ_SPACING_SEC = 0.3  # ~3 req/s
FRESHNESS_IDLE_SLEEP_SEC = 60


def run_freshness_job(symbols):
    """Ein Durchlauf: jüngste 2 Kerzen der heißen TFs für alle Coins per REST."""
    conn = get_db_connection()
    session = requests.Session()
    session.headers.update({"User-Agent": "CryptoBot/2.0"})
    updated = 0
    try:
        # TF-priorisiert statt symbolweise: erst ALLE Coins 5m (~3,5 min Zyklus),
        # dann 30m, dann 1h. So bleibt der zeitkritischste TF für jedes Symbol
        # unter dem 12-min-DATA_STALE-Limit, statt dass Z-Coins alle TFs eines
        # ~20-min-Zyklus hinterherhängen.
        for tf in FRESHNESS_HOT_TFS:
            for sym in symbols:
                # Abbrechen, sobald der WS wieder liefert — kein Doppel-Aufwand.
                if time.time() - WS_LAST_DATA_TS < FRESHNESS_WS_HEALTHY_SEC:
                    logger.info("🔌 Freshness-Fallback: WS liefert wieder — Durchlauf abgebrochen.")
                    return updated
                try:
                    resp = session.get(
                        BASE_URL + '/fapi/v1/klines',
                        params={'symbol': sym, 'interval': tf, 'limit': 2},
                        timeout=10,
                    )
                    if resp.status_code in (429, 418):
                        wait = int(resp.headers.get("Retry-After", 30)) + 2
                        logger.warning(f"Freshness-Fallback: Rate-Limit ({resp.status_code}), warte {wait}s")
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


async def freshness_fallback_loop(symbols):
    """Hintergrund-Brücke: hält die heißen TFs frisch, wenn (und nur wenn) der WS tot ist."""
    loop = asyncio.get_running_loop()
    await asyncio.sleep(CATCHUP_WARMUP_SEC + 60)  # WS + erster Catch-up zuerst
    logger.info("🩹 Freshness-Fallback bereit (aktiviert sich nur bei totem WS).")
    while True:
        if time.time() - WS_LAST_DATA_TS < FRESHNESS_WS_HEALTHY_SEC:
            await asyncio.sleep(FRESHNESS_IDLE_SLEEP_SEC)
            continue
        logger.warning("🩹 WS liefert keine Daten — REST-Freshness-Durchlauf startet (heiße TFs).")
        updated = await loop.run_in_executor(None, run_freshness_job, symbols)
        logger.info(f"🩹 Freshness-Durchlauf fertig: {updated} Kerzen-Upserts.")
        await asyncio.sleep(FRESHNESS_IDLE_SLEEP_SEC)


async def periodic_rest_catchup(symbols):
    """Background loop: erster Lauf nach Warm-up, danach alle 12h."""
    loop = asyncio.get_running_loop()
    logger.info(f"⏳ Catch-up wartet {CATCHUP_WARMUP_SEC}s (WS-Fleet zuerst verbinden lassen)...")
    await asyncio.sleep(CATCHUP_WARMUP_SEC)
    while True:
        # In Thread auslagern (blockiert die Loop nicht); die CPU-Arbeit selbst
        # passiert in den Child-Prozessen des ProcessPools.
        await loop.run_in_executor(None, run_catchup_job, symbols)

        logger.info("💤 Catch-Up Job schläft für 12 Stunden...")
        await asyncio.sleep(12 * 3600)  # 12 Stunden warten


# PHASE 2 & 3: WEBSOCKET STREAMING & DB FLUSHER
async def db_buffer_flusher():
    """Schreibt den RAM-Buffer alle 3 Sekunden ressourcenschonend in die DB.

    Wichtig: Atomic-Swap statt copy-then-clear, damit WS-Messages die zwischen
    den beiden Operationen ankommen nicht verloren gehen.
    """
    global WS_KLINE_BUFFER
    logger.info("💾 DB Buffer Flusher started (Intervall: 3s)")
    while True:
        await asyncio.sleep(3)
        if not WS_KLINE_BUFFER:
            continue

        # Atomic swap: wir tauschen den Buffer in EINEM Statement aus.
        # Der alte Inhalt geht in buffer_copy, neuer leerer Buffer ist sofort aktiv.
        # Da Python asyncio single-threaded ist, kann zwischen den beiden Zuweisungen
        # kein anderer Coroutine laufen (kein await dazwischen).
        buffer_copy = WS_KLINE_BUFFER
        WS_KLINE_BUFFER = {}

        try:
            await asyncio.to_thread(_flush_to_db, buffer_copy)
        except Exception as e:
            logger.error(f"Fehler beim DB Flush: {e}")


def _flush_to_db(buffer_copy):
    """Hilfsfunktion: Schreibt asynchronen Buffer via psycopg2 in DB (mit Chunking).

    FIX: Vorher rollte ein Fehler in EINER Zeile (z.B. fehlende Tabelle für neuen
    Coin) den kompletten Batch zurück — hunderte Kerzen verloren. Jetzt nutzen
    wir SAVEPOINTs pro Row: eine einzelne fehlerhafte Row wird verworfen,
    alle anderen committen sauber.
    """
    conn = get_db_connection()
    failed_tables = set()
    try:
        with conn.cursor() as cur:
            count = 0
            success = 0
            for (sym, tf, _open_time), data in buffer_copy.items():
                table_name = f'"{sym}_{tf}"'
                # D3: WHERE-Klausel wie in insert_fast — unveraenderte Kerzen
                # erzeugen keine neue Row-Version (WAL-Write-Amplification).
                sql = f"""
                    INSERT INTO {table_name} AS t (symbol, open_time, open, high, low, close, volume)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (symbol, open_time) DO UPDATE
                    SET open = EXCLUDED.open, high = EXCLUDED.high,
                        low = EXCLUDED.low, close = EXCLUDED.close, volume = EXCLUDED.volume
                    WHERE (t.open, t.high, t.low, t.close, t.volume)
                          IS DISTINCT FROM (EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.volume)
                """
                # SAVEPOINT: Jede Zeile läuft in einer Sub-Transaktion.
                sp_name = f"sp_{count}"
                try:
                    cur.execute(f"SAVEPOINT {sp_name}")
                    cur.execute(sql, data)
                    cur.execute(f"RELEASE SAVEPOINT {sp_name}")
                    success += 1
                except Exception as row_err:
                    cur.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                    # Nur einmal pro Tabelle loggen, nicht pro Zeile
                    if (sym, tf) not in failed_tables:
                        failed_tables.add((sym, tf))
                        logger.warning(f"Insert-Fehler für {sym}_{tf}: {row_err}")
                count += 1

                if count % 100 == 0:
                    conn.commit()

            conn.commit()
            if failed_tables:
                logger.info(f"Flush: {success}/{count} erfolgreich, {len(failed_tables)} Tabellen mit Fehlern skipped.")
    except Exception as e:
        conn.rollback()
        logger.error(f"Flush Error (gesamt): {e}")
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# WEBSOCKET-FLEET
# ═══════════════════════════════════════════════════════════════════════════
# Konfiguration mit Sicherheitsmarge zu Binance-Limits:
#   - Max 1024 Streams pro Connection → wir nutzen 800 (Reserve)
#   - Max 300 Connect-Attempts pro 5min pro IP → wir begrenzen uns hart auf 60
#     per-Worker (mit Backoff), und der initiale Start staggert
#   - Max 10 messages/s pro Connection → wir schicken max 1 SUBSCRIBE/s
# Binance sends its own ping every 180s; the websockets library responds automatically.
# We disable the library's own ping (ping_interval=None) to avoid the collision:
# both sides pinging simultaneously → library waits for its pong → times out → disconnect.
# Dead connections are detected by the message watchdog (WS_MESSAGE_WATCHDOG_SEC=120s).
#
# Watchdog: tracked letzte Message-Zeit pro Worker. Wenn > 120s nichts ankommt
# trotz offener Connection, erzwingt Reconnect.
# ═══════════════════════════════════════════════════════════════════════════

# Streams per WS connection. Using URL-encoded combined stream format
# (wss://fstream.binance.com/stream?streams=s1/s2/...) instead of SUBSCRIBE
# messages — this uses the documented 1024-stream limit reliably.
# SUBSCRIBE-based connections appear to be dropped by Binance at ~150-200s
# when carrying 800+ streams, despite the documented 1024 limit.
#
# FIX HTTP 414 (URI Too Long): 860 Streams ergaben ~19-KB-URLs → Binance
# lehnte manche Handshakes mit 414 ab.
# FIX SILENT-CAP (der eigentliche Killer): Binance USDⓈ-M-Futures liefert
# pro Connection nur ~200 Streams — bei mehr wird der Handshake AKZEPTIERT,
# aber es kommen NIE Messages (kein Fehler!). Mit 400 Streams liefen alle
# 14 Worker in den 120s-Message-Watchdog und reconnecteten endlos stumm.
# Identisches Verhalten hatte das Audit beim Whale-Logger dokumentiert
# (P1.42: "fapi-Cap ~200/Conn"). 180 = Sicherheitsmarge unter dem Cap.
WS_STREAMS_PER_WORKER = 180

# SUBSCRIBE-Chunk-Größe und Abstand. Binance erlaubt 10 msg/s pro Connection
# (Futures); wir bleiben bei 1 msg/s = 10x Sicherheitsmarge, wichtig beim
# gleichzeitigen Startup vieler Worker.
WS_SUBSCRIBE_CHUNK_SIZE = 200
WS_SUBSCRIBE_DELAY_SEC = 1.0

# Staggered Startup: beim ersten Start Worker versetzt anlegen um die
# 300-connects-pro-5min-Regel nicht zu verletzen.
# Bei ~30 Workern (180 Streams/Conn): 5s Stagger = 150s Startspread —
# alle Worker binnen 2,5 min oben, 30 Connects/5min << Limit 300.
WS_STARTUP_STAGGER_SEC = 5.0

# Reconnect-Backoff: start bei 5s, verdoppelt sich, gedeckelt bei 900s.
# Jitter ±20% verhindert dass alle Worker gleichzeitig reconnecten.
# WICHTIG (Anti-Ban): Der Backoff wird erst nach der ERSTEN DATEN-Message
# zurückgesetzt, nicht beim Connect — Binance kann Verbindungen annehmen und
# stumm lassen (IP-Drossel nach Connect-Churn). Mit Reset-on-Connect
# reconnecteten 30 stumme Worker im 120s-Takt (~900 Connects/h) und
# erneuerten die Drossel endlos selbst.
WS_RECONNECT_MIN_SEC = 5.0
WS_RECONNECT_MAX_SEC = 900.0

# Wenn länger als so viele Sekunden keine Message reinkommt → Connection
# für tot halten und reconnecten (Binance-Streams ticken praktisch ständig,
# besonders die 5m-Streams).
WS_MESSAGE_WATCHDOG_SEC = 120.0

# Unsolicited pong interval: send a pong frame every 120s as a keepalive safety net.
# Spec allows this (>15min is the documented minimum — 2min is more conservative).
# Guards against event-loop hiccups that might delay the auto-pong response.
WS_UNSOLICITED_PONG_SEC = 120.0

# Ping-Config (an Binance-Futures-Spezifikation angepasst)
WS_PING_INTERVAL_SEC = None  # Disable library pings — Binance sends its own ping every
# 180s and the websockets library auto-responds with pong.
# Running our own ping_interval=180 causes a collision:
# both sides send pings simultaneously → library times out
# waiting for its pong → false disconnect after ~206s.
WS_PING_TIMEOUT_SEC = None  # Not needed when ping_interval=None


def _apply_keepalive(ws) -> None:
    """Applies TCP keepalive to an already-connected WebSocket.

    Called AFTER websockets.connect() succeeds. Gets the underlying socket
    from the transport and sets SO_KEEPALIVE + platform-specific intervals.
    This avoids the Windows WinError 10057 that occurs when passing an
    unconnected socket to websockets.connect(sock=...).

    Prevents NAT/firewall idle-timeout disconnects (~300-360s) by sending
    TCP-level ACK probes every 60s.
    """
    import sys

    try:
        sock = ws.transport.get_extra_info("socket")
        if sock is None:
            return
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if sys.platform == "win32":
            # SIO_KEEPALIVE_VALS: (onoff, keepalivetime_ms, keepaliveinterval_ms)
            # First probe after 60s idle, then every 10s
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 60_000, 10_000))
        else:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 6)
    except (AttributeError, OSError):
        pass  # Non-fatal — connection still works without keepalive


async def binance_ws_worker(worker_id: int, streams: list, startup_delay: float = 0.0):
    """Ein einzelner WebSocket-Worker mit robustem Reconnect und Watchdog.

    Verbesserungen ggü. der alten Version:
      - Staggered Startup: initialer Delay verhindert 16 Connects auf einmal
      - Exponential Backoff mit Jitter bei Disconnects
      - Message-Watchdog: erzwingt Reconnect wenn zu lange no data kommen
      - Eindeutige SUBSCRIBE-IDs (worker_id * 1000 + chunk_idx)
      - Subscribe-Response-Check (mit Timeout)
      - ping_interval/timeout an Binance-Spezifikation angepasst
      - Langsamere Subscribes (1s statt 0.5s) um msg/s-Limit einzuhalten
    """
    if startup_delay > 0:
        logger.info(f"⏳ WS-Worker {worker_id} wartet {startup_delay:.0f}s für staggered start...")
        await asyncio.sleep(startup_delay)

    # URL-encoded combined stream — all streams in the query string.
    # This uses the documented 1024-stream limit and avoids SUBSCRIBE messages.
    # WICHTIG (Root-Cause der "stummen" Verbindungen, gefunden 05.07.2026):
    # Binance hat die Legacy-URLs /ws und /stream zum 23.04.2026 abgeschaltet —
    # ungeroutete Verbindungen handshaken weiter erfolgreich, pushen aber KEINE
    # /market-Streams (kline/aggTrade/markPrice) mehr. Neue geroutete URL:
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
                logger.info(f"🟢 WS-Worker {worker_id} connected ({len(streams)} streams, URL-encoded)")

                # No SUBSCRIBE needed — streams are in the URL.
                # Backoff wird NICHT hier zurückgesetzt, sondern erst bei der
                # ersten echten Daten-Message (stumme Verbindungen zählen als
                # Fehlversuch weiter — Anti-Ban, siehe WS_RECONNECT_MAX_SEC).
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
                            # Timeout hier damit wir den Watchdog checken können
                            msg = await asyncio.wait_for(
                                ws.recv(),
                                timeout=WS_MESSAGE_WATCHDOG_SEC,
                            )
                        except asyncio.TimeoutError:
                            # Keine Message innerhalb Watchdog-Fenster
                            silence_sec = (datetime.datetime.now(pytz.UTC) - last_msg_ts).total_seconds()
                            logger.warning(
                                f"⏰ WS-Worker {worker_id}: {silence_sec:.0f}s keine Messages, erzwinge Reconnect"
                            )
                            break

                        last_msg_ts = datetime.datetime.now(pytz.UTC)

                        try:
                            payload = json.loads(msg)
                        except json.JSONDecodeError:
                            continue

                        # SUBSCRIBE-Response durchgehen lassen
                        if 'result' in payload:
                            if payload.get('result') is not None:
                                # Nicht-Null Result → Fehler (Binance antwortet mit null bei Erfolg)
                                logger.warning(f"WS-Worker {worker_id}: Subscribe-Error: {payload}")
                            continue

                        # Fehler-Response
                        if 'error' in payload:
                            logger.warning(f"WS-Worker {worker_id}: Error-Response: {payload}")
                            continue

                        # Daten-Message
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

                            # P1.11: Key inkl. open_time — vorher überschrieb die erste
                            # Message der NEUEN Kerze das finale Update der alten Kerze
                            # im Buffer (an jeder Kerzengrenze), die gespeicherte
                            # "Closed"-Kerze blieb bis zum REST-Catch-up leicht falsch.
                            WS_KLINE_BUFFER[(sym, tf, open_time)] = (
                                sym,
                                open_time,
                                float(k['o']),
                                float(k['h']),
                                float(k['l']),
                                float(k['c']),
                                float(k['v']),
                            )

                finally:
                    pong_task.cancel()
                    try:
                        await pong_task
                    except (asyncio.CancelledError, Exception):
                        pass

            # Watchdog-Break-Pfad (Verbindung war offen, aber stumm): Vorher
            # reconnectete das SOFORT ohne Backoff → 30 stumme Worker = 120s-
            # Reconnect-Hammer, der eine IP-Drossel endlos erneuert. Jetzt:
            # stumme Verbindung = Fehlversuch mit exponentiellem Backoff.
            if not got_data:
                consecutive_failures += 1
                jitter = random.uniform(0.8, 1.2)
                wait_sec = min(backoff * jitter, WS_RECONNECT_MAX_SEC)
                logger.warning(
                    f"🔇 WS-Worker {worker_id}: Verbindung blieb stumm — Backoff {wait_sec:.0f}s "
                    f"(Versuch #{consecutive_failures})"
                )
                await asyncio.sleep(wait_sec)
                backoff = min(backoff * 2.0, WS_RECONNECT_MAX_SEC)

        except asyncio.CancelledError:
            logger.info(f"🛑 WS-Worker {worker_id} stopped (cancelled).")
            raise
        except Exception as e:
            consecutive_failures += 1
            uptime_str = ""
            if connected_at is not None:
                uptime_sec = (datetime.datetime.now(pytz.UTC) - connected_at).total_seconds()
                uptime_str = f" (war {uptime_sec:.0f}s verbunden)"

            # Exponential Backoff mit Jitter
            jitter = random.uniform(0.8, 1.2)
            wait_sec = min(backoff * jitter, WS_RECONNECT_MAX_SEC)

            # Add worker_id spread so workers don't all reconnect simultaneously
            spread_sec = (worker_id - 1) * 2.0
            total_wait = wait_sec + spread_sec
            logger.warning(
                f"🔴 WS-Worker {worker_id} getrennt{uptime_str}: {type(e).__name__}: {e}. "
                f"Reconnect in {total_wait:.1f}s (Versuch #{consecutive_failures}, spread +{spread_sec:.0f}s)"
            )
            await asyncio.sleep(total_wait)
            # Backoff verdoppeln bis zum Cap
            backoff = min(backoff * 2.0, WS_RECONNECT_MAX_SEC)


async def start_websocket_fleet(symbols):
    """Teilt die Streams auf WS-Connections auf mit staggered Startup.

    Auslegungsprinzipien:
      - Wenige, volle Connections besser als viele halbvolle (Binance-Overhead)
      - Startup-Stagger verhindert Rate-Limit beim initialen Connect
      - Bei Reconnect-Storm greift zusätzlich der Exponential Backoff
    """
    all_streams = []
    for sym in symbols:
        for tf in TIMEFRAMES:
            all_streams.append(f"{sym.lower()}@kline_{tf}")

    stream_chunks = [
        all_streams[i : i + WS_STREAMS_PER_WORKER] for i in range(0, len(all_streams), WS_STREAMS_PER_WORKER)
    ]

    n_workers = len(stream_chunks)
    logger.info(
        f"🚀 Starting {n_workers} WS-Worker für {len(all_streams)} Streams "
        f"(~{len(all_streams) // max(n_workers, 1)} Streams/Worker, "
        f"Stagger {WS_STARTUP_STAGGER_SEC}s zwischen Starts)"
    )

    # Warnung wenn wir zu viele Connects in 5 Minuten haben könnten.
    # Binance erlaubt 300 Connect-Attempts pro 5min pro IP.
    expected_connects_per_5min = n_workers  # initial, ohne Reconnects
    if expected_connects_per_5min > 60:
        logger.warning(
            f"⚠️  {n_workers} Worker + Reconnects könnten das Binance-Limit "
            f"(300 Connects pro 5min) sprengen. Erwäge mehr Streams pro Worker."
        )

    tasks = [db_buffer_flusher()]
    for i, chunk in enumerate(stream_chunks):
        startup_delay = i * WS_STARTUP_STAGGER_SEC
        tasks.append(binance_ws_worker(i + 1, chunk, startup_delay=startup_delay))

    await asyncio.gather(*tasks)


# HAUPT-EINSTIEGSPUNKT
async def main_async():
    logger.info("=== DATA INGESTION SYSTEM START ===")

    # WS-Stabilität: Die Ingestion ist der Daten-Herzschlag der ganzen Fleet —
    # ihre Event-Loop darf in CPU-Saturationsphasen (Engine-Zyklus, Bots) nicht
    # vom Scheduler verhungern. ABOVE_NORMAL (nicht HIGH: das würde dem OS
    # selbst Ressourcen streitig machen).
    try:
        import psutil

        psutil.Process().nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
        logger.info("⚡ Prozess-Priorität auf ABOVE_NORMAL gesetzt (WS-Loop-Schutz).")
    except Exception as e:
        logger.warning(f"Priorität konnte nicht gesetzt werden: {e}")

    # 1. Aktuelle Liste holen (Synchron)
    symbols = update_trading_pairs()

    # 2. Den Background-Task für den 7-Tage-Check (läuft direkt 1x an und dann alle 12h)
    catchup_task = asyncio.create_task(periodic_rest_catchup(symbols))

    # 3. WebSockets sofort starten
    ws_task = asyncio.create_task(start_websocket_fleet(symbols))

    # 4. REST-Freshness-Brücke (aktiv nur bei totem WS — z.B. IP-Drossel)
    freshness_task = asyncio.create_task(freshness_fallback_loop(symbols))

    await asyncio.gather(catchup_task, ws_task, freshness_task)


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("🛑 Data Ingestion stopped (Strg+C).")


if __name__ == "__main__":
    main()
