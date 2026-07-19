import asyncio
import datetime
import json
import random
import time
import warnings
from concurrent.futures import ProcessPoolExecutor  # Catch-up in eigenen Prozessen (GIL-Fix)

import pytz
import requests
import websockets

try:
    # Optional (T-2026-CU-9050-169, Maßnahme 5): schnelleres Parsing der
    # ~2-3k WS-Messages/s. Nicht in den Fleet-Requirements — ohne Installation
    # läuft unverändert stdlib-json (Parse-Ergebnis ist identisch).
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
from core.market_utils import load_coins  # reines coins.json-Re-Read (P2.15)
from core.ws_utils import apply_keepalive as _apply_keepalive

warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

# --- IMPORT CONFIGURATION FROM CORE ---
from core.coins import refresh_coins_json
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
    """Fetches the latest futures pairs from Binance.

    Filter + atomic write live in ``core.coins`` (the single coins.json writer,
    P2.16) so this and ``6_housekeeping.update_coins_json`` cannot drift apart.
    On any refresh failure we fall back to the on-disk list (startup needs a
    coin set to bring up the WS fleet) and never truncate the live file.
    """
    logger.info("Updating Coin-Liste...")
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
    # P2.14: gebudgeteter Retry statt while-True — ein stuck Symbol darf den
    # 12h-Catch-up nicht mehr blockieren; nur FEHL-Versuche zählen gegen das
    # Budget, Erfolgs-Seiten paginieren frei weiter. 418 = Binance-IP-Ban-
    # Eskalation: nie unter 120s, exponentiell (weiter hämmern verlängert den
    # Ban). Bei erschöpftem Budget werden die bereits geholten Teildaten
    # verwendet — der nächste 12h-Lauf setzt am MAX(open_time) wieder auf.
    budget = RetryBudget(max_attempts=CATCHUP_MAX_RETRIES, deadline_s=CATCHUP_RETRY_DEADLINE_S)
    consecutive_fail = 0
    while True:
        params = {'symbol': symbol, 'interval': interval, 'startTime': curr, 'endTime': end_ts, 'limit': 1500}
        try:
            resp = session.get(url, params=params, timeout=10)
            if resp.status_code in [429, 418]:
                if not budget.attempt():
                    logger.warning(
                        f"Catch-up {symbol} {interval}: Retry-Budget erschöpft "
                        f"({budget.exhausted_reason()}) — {len(all_data)} Kerzen Teildaten werden verwendet."
                    )
                    break
                consecutive_fail += 1
                wait = backoff_seconds(resp.status_code, consecutive_fail, resp.headers.get("Retry-After"))
                if resp.status_code == 418:
                    logger.warning(f"Catch-up {symbol} {interval}: 418 (IP-Ban-Signal) — Backoff {wait:.0f}s")
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
            time.sleep(0.1)  # Reduziert für mehr Speed, Limit bei Binance Futures ist recht hoch
        except Exception:
            if not budget.attempt():
                logger.warning(
                    f"Catch-up {symbol} {interval}: Retry-Budget erschöpft "
                    f"({budget.exhausted_reason()}) — {len(all_data)} Kerzen Teildaten werden verwendet."
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

# P2.14: Retry-Budget je Symbol×TF-Batch im 12h-Catch-up. Nur Fehlversuche
# (429/418/Netzfehler) zählen; Erfolgs-Seiten paginieren unbegrenzt weiter.
CATCHUP_MAX_RETRIES = 8
CATCHUP_RETRY_DEADLINE_S = 300.0

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


async def freshness_fallback_loop(tracked):
    """Hintergrund-Brücke: hält die heißen TFs frisch, wenn (und nur wenn) der WS tot ist.

    ``tracked`` ist das geteilte Symbol-Set (vom Coin-Refresh mutiert); pro Durchlauf
    geschnappschusst, damit neu nachgezogene Coins die Freshness-Abdeckung mitbekommen.
    """
    loop = asyncio.get_running_loop()
    await asyncio.sleep(CATCHUP_WARMUP_SEC + 60)  # WS + erster Catch-up zuerst
    logger.info("🩹 Freshness-Fallback bereit (aktiviert sich nur bei totem WS).")
    while True:
        if time.time() - WS_LAST_DATA_TS < FRESHNESS_WS_HEALTHY_SEC:
            await asyncio.sleep(FRESHNESS_IDLE_SLEEP_SEC)
            continue
        logger.warning("🩹 WS liefert keine Daten — REST-Freshness-Durchlauf startet (heiße TFs).")
        updated = await loop.run_in_executor(None, run_freshness_job, sorted(tracked))
        logger.info(f"🩹 Freshness-Durchlauf fertig: {updated} Kerzen-Upserts.")
        await asyncio.sleep(FRESHNESS_IDLE_SLEEP_SEC)


async def periodic_rest_catchup(tracked):
    """Background loop: erster Lauf nach Warm-up, danach alle 12h.

    ``tracked`` ist das geteilte Symbol-Set (vom Coin-Refresh mutiert); pro Zyklus
    geschnappschusst, damit neu nachgezogene Coins die 12h-Abdeckung mitbekommen.
    """
    loop = asyncio.get_running_loop()
    logger.info(f"⏳ Catch-up wartet {CATCHUP_WARMUP_SEC}s (WS-Fleet zuerst verbinden lassen)...")
    await asyncio.sleep(CATCHUP_WARMUP_SEC)
    while True:
        # In Thread auslagern (blockiert die Loop nicht); die CPU-Arbeit selbst
        # passiert in den Child-Prozessen des ProcessPools.
        await loop.run_in_executor(None, run_catchup_job, sorted(tracked))

        logger.info("💤 Catch-Up Job schläft für 12 Stunden...")
        await asyncio.sleep(12 * 3600)  # 12 Stunden warten


async def _spawn_ws_workers_for(new_symbols):
    """Spawnt zusaetzliche WS-Worker fuer neue Symbole (additiv, Sharding + Stagger
    wie im initialen Fleet). Die Worker schreiben in denselben WS_KLINE_BUFFER; der
    globale db_buffer_flusher (im initialen Fleet gestartet) persistiert sie mit."""
    for idx, chunk in enumerate(_new_symbol_stream_chunks(new_symbols)):
        wid = _allocate_ws_worker_id()
        startup_delay = idx * WS_STARTUP_STAGGER_SEC  # 300-Connects/5min-Limit schonen
        asyncio.create_task(binance_ws_worker(wid, chunk, startup_delay=startup_delay))
        logger.info(f"🆕 WS-Worker {wid}: {len(chunk)} neue Streams gestartet.")


async def coin_refresh_loop(tracked):
    """P2.15: zieht neu in coins.json aufgetauchte Coins ohne Prozess-Restart nach.

    Pro neuem Symbol: Tabellen + einmaliger 730d-Catch-up (Child-Prozesse, GIL-frei)
    und ein zusaetzlicher WS-Worker. ``tracked`` wird mit den Catch-up-/Freshness-Loops
    geteilt (die es pro Zyklus schnappschussen), sodass neue Coins auch die 12h-Catch-up-
    und Freshness-Abdeckung bekommen. Erst nach Catch-up + WS wird das Symbol als
    bekannt markiert (sonst wuerde ein paralleler Loop es ohne Tabelle sehen)."""
    loop = asyncio.get_running_loop()
    await asyncio.sleep(CATCHUP_WARMUP_SEC + 60)  # WS-Fleet + erster Catch-up zuerst
    logger.info("🆕 Coin-Refresh bereit (zieht neue Listings ohne Restart nach).")
    while True:
        await asyncio.sleep(COIN_REFRESH_INTERVAL_SEC)
        try:
            new_symbols = compute_new_symbols(set(load_coins()), tracked)
            if not new_symbols:
                continue
            preview = ", ".join(new_symbols[:10]) + (" …" if len(new_symbols) > 10 else "")
            logger.info(f"🆕 {len(new_symbols)} neue Coins in coins.json: {preview}")
            # 1. Tabellen + einmaliger Catch-up (Child-Prozesse, blockiert die Loop nicht)
            await loop.run_in_executor(None, run_catchup_job, new_symbols)
            # 2. WS-Streams additiv nachziehen
            await _spawn_ws_workers_for(new_symbols)
            # 3. Erst jetzt als bekannt markieren (Catch-up + WS sind live)
            tracked.update(new_symbols)
            logger.info(f"✅ {len(new_symbols)} neue Coins live (Tabellen + Catch-up + WS).")
        except Exception as e:
            logger.error(f"Coin-Refresh-Fehler: {e}")


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


# Persistente Flush-Connection (T-2026-CU-9050-169): vorher öffnete/schloss
# JEDER 3s-Flush eine eigene Connection. Nur der Flusher-Thread benutzt sie —
# db_buffer_flusher awaited jeden asyncio.to_thread-Aufruf, es laufen also nie
# zwei _flush_to_db gleichzeitig. Bei jedem Fehler wird sie verworfen und beim
# nächsten Flush neu aufgebaut (Muster der Monitore: ensure/reset).
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
    """Gruppen-Flush: ein upsert_candles pro (symbol, tf, closed)-Gruppe.

    Fallback- und Legacy-Primary-Pfad. SAVEPOINT pro GRUPPE statt pro Row
    (T-2026-CU-9050-169): die reale Fehlerklasse — fehlende per-Coin-Tabelle
    auf dem Legacy-Backend — betrifft immer die ganze (symbol, tf)-Gruppe;
    Row-genaue Isolation kostete ~2 Zusatz-Statements pro Kerze. Semantik wie
    vorher: eine fehlerhafte Gruppe wird verworfen und geloggt, alle anderen
    committen. upsert_candles trägt den D3 IS DISTINCT FROM No-op-Guard und
    persistiert is_closed pro Aufruf (deshalb ist `closed` Teil des
    Gruppen-Schlüssels — die Flag-Semantik pro Kerze bleibt exakt erhalten).
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
                # Nur einmal pro Tabelle loggen, nicht pro Gruppe/Zeile
                if (sym, tf) not in failed_tables:
                    failed_tables.add((sym, tf))
                    logger.warning(f"Insert-Fehler für {sym}_{tf}: {grp_err}")
    conn.commit()
    if failed_tables:
        logger.info(
            f"Flush: {success_rows}/{total_rows} erfolgreich, {len(failed_tables)} Tabellen mit Fehlern skipped."
        )


def _flush_to_db(buffer_copy):
    """Hilfsfunktion: Schreibt den asynchronen Buffer via psycopg2 in die DB.

    T-2026-CU-9050-169: auf dem Hyper-Primary geht der komplette Buffer als EIN
    execute_values-Batch raus (upsert_candles_many — identisches Statement,
    identischer IS DISTINCT FROM-Guard wie der Einzel-Pfad; der Buffer-Key
    (sym, tf, open_time) garantiert die ON-CONFLICT-Eindeutigkeit im Batch).
    Vorher liefen ~3.185 Einzel-INSERTs/s mit je eigenem SAVEPOINT/RELEASE —
    der dominante DB- und Client-CPU-Posten der Ingestion. Schlägt der Batch
    fehl (oder ist der Write-Primary 'legacy'), greift der Gruppen-Flush mit
    SAVEPOINT-Isolation pro (symbol, tf, closed)-Gruppe.

    Verlust-Semantik unverändert konservativ: ein verlorener Flush wird vom
    24h-Catch-up-Overlap bzw. den laufenden WS-Re-Upserts geheilt.
    """
    try:
        conn = _get_flush_conn()
    except Exception as e:
        logger.error(f"Flush: keine DB-Connection ({e}) — Buffer verworfen (Catch-up-Overlap heilt).")
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
                # Rollback + Fallback auf den isolierenden Gruppen-Pfad — eine
                # einzelne kaputte Row darf nicht den ganzen Flush kosten.
                conn.rollback()
                logger.warning(f"Batch-Flush fehlgeschlagen ({batch_err}) — Fallback auf Gruppen-Flush.")
        _flush_groups_fallback(conn, buffer_copy)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error(f"Flush Error (gesamt): {e}")
        _reset_flush_conn()


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

# 1d/1w vom WebSocket nehmen (C-Gate Phase 2, D-2026-CLD-109 #3): die zwei
# langsamsten Frames aktualisieren höchstens einmal pro Tag bzw. Woche, ein
# Live-Kline-Stream dafür sind ~1.300 verschwendete Streams (IP-Drossel-Risiko)
# für eine Kerze, die der REST-Catch-up ohnehin jeden Zyklus holt. Sie BLEIBEN
# auf dem REST-/Catch-up-Pfad (dort weiter `TIMEFRAMES`, unverändert) — NUR die
# WS-Subscription-Menge lässt sie fallen. WS bleibt für 5m–4h.
WS_EXCLUDED_TIMEFRAMES = frozenset({"1d", "1w"})
WS_TIMEFRAMES = [tf for tf in TIMEFRAMES if tf not in WS_EXCLUDED_TIMEFRAMES]

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

# P2.15: coins.json wird zur Laufzeit von 6_housekeeping (taeglich 03:00 UTC)
# aktualisiert. Ohne Re-Read bekaemen neu gelistete Coins bis zum Prozess-Restart
# keine Daten (kein WS-Stream, kein Catch-up). Der Refresh liest coins.json
# periodisch neu und zieht neue Symbole ADDITIV nach: Tabellen + einmaliger
# Catch-up + eigener WS-Worker. Konservativ — entfernte Coins werden NICHT
# abgebaut (Stream-Teardown bleibt dem Restart), damit ein faelschlich (torn/leerer
# coins.json-Read) fehlender Coin nicht live aus der Ingestion faellt.
COIN_REFRESH_INTERVAL_SEC = 900

# Fortlaufende WS-Worker-ID ueber initialen Fleet + Refresh-Worker hinweg, damit
# nachgezogene Worker eindeutige IDs (Logs, Reconnect-Spread) bekommen.
_next_ws_worker_id = 1


def _allocate_ws_worker_id() -> int:
    global _next_ws_worker_id
    wid = _next_ws_worker_id
    _next_ws_worker_id += 1
    return wid


def compute_new_symbols(current: set, tracked: set) -> list:
    """Neue, noch nicht getrackte Symbole (additiv, sortiert).

    Konservativ: ein leeres ``current`` (torn/leerer coins.json-Read) ergibt keine
    Aenderung — nie Coins entfernen, nie auf einem kaputten Read reagieren.
    load_coins() liefert bei kaputtem Read [] (all-or-nothing json.load).
    """
    if not current:
        return []
    return sorted(current - tracked)


def _new_symbol_stream_chunks(new_symbols: list) -> list:
    """Baut die kline-Stream-Namen fuer neue Symbole und shardet sie wie der
    initiale Fleet (<= WS_STREAMS_PER_WORKER Streams/Connection)."""
    all_streams = [f"{sym.lower()}@kline_{tf}" for sym in new_symbols for tf in WS_TIMEFRAMES]
    return [all_streams[i : i + WS_STREAMS_PER_WORKER] for i in range(0, len(all_streams), WS_STREAMS_PER_WORKER)]


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
                            # orjson wenn installiert, sonst stdlib (identisches Ergebnis).
                            payload = orjson.loads(msg) if orjson is not None else json.loads(msg)
                        except ValueError:
                            # JSONDecodeError beider Bibliotheken ist ValueError-Subklasse.
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
                            # Value carries the real Binance closed flag k['x'] alongside
                            # the row, so _flush_to_db can persist is_closed per candle
                            # (this WS path is where the flag first enters the data model).
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
        for tf in WS_TIMEFRAMES:
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
        tasks.append(binance_ws_worker(_allocate_ws_worker_id(), chunk, startup_delay=startup_delay))

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
    # Geteiltes Symbol-Set: der Coin-Refresh (P2.15) mutiert es, die Catch-up-/
    # Freshness-Loops schnappschussen es pro Zyklus. Der initiale WS-Fleet bekommt
    # den Start-Snapshot (seine Streams sind in die Connection-URLs gebacken).
    tracked = set(symbols)

    # 2. Den Background-Task für den 7-Tage-Check (läuft direkt 1x an und dann alle 12h)
    catchup_task = asyncio.create_task(periodic_rest_catchup(tracked))

    # 3. WebSockets sofort starten
    ws_task = asyncio.create_task(start_websocket_fleet(symbols))

    # 4. REST-Freshness-Brücke (aktiv nur bei totem WS — z.B. IP-Drossel)
    freshness_task = asyncio.create_task(freshness_fallback_loop(tracked))

    # 5. Coin-Refresh: zieht neu gelistete Coins ohne Restart nach (P2.15)
    refresh_task = asyncio.create_task(coin_refresh_loop(tracked))

    await asyncio.gather(catchup_task, ws_task, freshness_task, refresh_task)


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("🛑 Data Ingestion stopped (Strg+C).")


if __name__ == "__main__":
    main()
