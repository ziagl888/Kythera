# core/database.py — DB connection pool with full backwards compatibility
from __future__ import annotations

import logging
import os
import threading
import time
import warnings
from contextlib import contextmanager
from typing import Iterator

import psycopg2
import psycopg2.extensions
from psycopg2 import pool as pg_pool

warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

from core.config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER

logger = logging.getLogger(__name__)

_POOL: pg_pool.ThreadedConnectionPool | None = None
_POOL_LOCK = threading.Lock()
# FIX P1.34: Fleet-Budget — 27 Prozesse × maxconn kollidieren mit
# max_connections; MIN/MAX darum env-overridable (Ops-Hebel ohne Code-Deploy).
# Defaults bleiben verhaltensgleich (2/8). ACHTUNG psycopg2-Semantik: minconn
# ist der IDLE-CACHE des Pools — putconn SCHLIESST jede Connection, die über
# minconn idle zurückkommt. Ein zu kleines MIN erzeugt also Reconnect-Churn
# bei Prozessen mit >1 gleichzeitiger Connection (Monitor, Market-Tracker).
_POOL_MIN = int(os.getenv("KYTHERA_DB_POOL_MIN", "2"))
_POOL_MAX = int(os.getenv("KYTHERA_DB_POOL_MAX", "8"))

# FIX P2.47: a bot blocked forever on a dead DB socket or a runaway query used to
# stay "green" for the watchdog (process alive, producing nothing → the fleet
# handled stale data). Two conservative, server-/socket-side guards on every
# pooled connection, all env-overridable so a long trainer/housekeeping query can
# opt out PER PROCESS (set the env before the process imports this module):
#   - statement_timeout caps any single query server-side. 0 = disabled (the
#     escape hatch for legitimately long-running trainer/housekeeping queries).
#   - lock_timeout (pre-existing) caps how long a query waits for a lock.
#   - TCP keepalives make a silently-dropped VPS↔Postgres socket fail fast
#     instead of hanging the caller forever on a half-open connection.
# NOTE: no timezone change here — the TZ line (R3/UTC_POLICY.md) is deliberately
# out of scope for this fix.
_DEFAULT_LOCK_TIMEOUT_MS = 30000
_DEFAULT_STATEMENT_TIMEOUT_MS = 30000
_DEFAULT_KEEPALIVES_IDLE_S = 30
_DEFAULT_KEEPALIVES_INTERVAL_S = 10
_DEFAULT_KEEPALIVES_COUNT = 3


def _connect_options() -> str:
    """Builds the libpq ``options`` string (server-side per-session GUCs).

    Read at pool-creation time (not import) so a per-process env override takes
    effect and the value stays testable.
    """
    lock_ms = int(os.getenv("KYTHERA_DB_LOCK_TIMEOUT_MS", str(_DEFAULT_LOCK_TIMEOUT_MS)))
    parts = [f"-c lock_timeout={lock_ms}"]
    stmt_ms = int(os.getenv("KYTHERA_DB_STATEMENT_TIMEOUT_MS", str(_DEFAULT_STATEMENT_TIMEOUT_MS)))
    if stmt_ms > 0:  # 0 disables the cap — long trainer/housekeeping queries.
        parts.append(f"-c statement_timeout={stmt_ms}")
    return " ".join(parts)


def _keepalive_kwargs() -> dict[str, int]:
    """libpq TCP-keepalive connect params — detect a dead server socket."""
    return {
        "keepalives": 1,
        "keepalives_idle": int(os.getenv("KYTHERA_DB_KEEPALIVES_IDLE_S", str(_DEFAULT_KEEPALIVES_IDLE_S))),
        "keepalives_interval": int(os.getenv("KYTHERA_DB_KEEPALIVES_INTERVAL_S", str(_DEFAULT_KEEPALIVES_INTERVAL_S))),
        "keepalives_count": int(os.getenv("KYTHERA_DB_KEEPALIVES_COUNT", str(_DEFAULT_KEEPALIVES_COUNT))),
    }


# Liveness-Probe-Cache (P1.33): id(conn) → monotonic-Zeitpunkt der letzten
# erfolgreichen SELECT-1-Probe. Innerhalb der TTL wird beim Checkout nicht
# erneut geprobt (Hot-Loop-Schonung, siehe get_db_connection-Docstring).
_LIVENESS_TTL_SEC = 30.0
_LAST_VERIFIED: dict[int, float] = {}


def _get_pool() -> pg_pool.ThreadedConnectionPool:
    global _POOL
    if _POOL is not None and not _POOL.closed:
        return _POOL
    with _POOL_LOCK:
        if _POOL is not None and not _POOL.closed:
            return _POOL
        options = _connect_options()
        logger.info(
            f"Creating DB connection pool (min={_POOL_MIN}, max={_POOL_MAX}, options='{options}') "
            f"→ {DB_HOST}:{DB_PORT}/{DB_NAME}"
        )
        _POOL = pg_pool.ThreadedConnectionPool(
            minconn=_POOL_MIN,
            maxconn=_POOL_MAX,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT,
            options=options,
            **_keepalive_kwargs(),
        )
    return _POOL


class PooledConnection:
    """
    Wrapper around a real psycopg2 connection that returns itself to the pool
    on close() instead of destroying it.

    Forwards every attribute and method to the underlying connection so all
    existing bot code (conn.cursor(), conn.commit(), conn.close(), …) works
    exactly as before — no changes needed anywhere else.
    """

    def __init__(self, conn: psycopg2.extensions.connection) -> None:
        # Store under a mangled name so __getattr__ never intercepts it.
        object.__setattr__(self, '_conn', conn)
        # FIX P1.32: markiert, ob die Connection schon an den Pool zurückgegeben
        # wurde — macht close() idempotent (Double-Close hat vorher die
        # Transaktion eines anderen Threads rollbacked / den Pool vergiftet,
        # siehe HOTFIX_README).
        object.__setattr__(self, '_returned', False)

    # ── Attribute forwarding ──────────────────────────────────────────────

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, '_conn'), name)

    def __setattr__(self, name: str, value) -> None:
        if name in ('_conn', '_returned'):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, '_conn'), name, value)

    # ── Context-manager support (with conn: …) ────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    # ── Pool-aware close ──────────────────────────────────────────────────

    def close(self) -> None:
        """Return connection to pool instead of destroying it. Idempotent."""
        # FIX P1.32: Double-Close ist jetzt ein No-op statt eines zweiten
        # putconn derselben Connection (die inzwischen ein anderer Thread aus
        # dem Pool gezogen haben kann → fremde Transaktion rollbacked).
        if object.__getattribute__(self, '_returned'):
            return
        object.__setattr__(self, '_returned', True)
        conn = object.__getattribute__(self, '_conn')
        try:
            if not conn.closed:
                conn.rollback()  # clean up any open transaction
            _get_pool().putconn(conn)
        except Exception as e:
            logger.warning(f"Error returning connection to pool: {e}")
            # FIX P1.33: Slot IMMER freigeben. Vorher wurde bei einem rollback()-
            # Fehler (tote Connection nach DB-Restart) nur conn.close() gerufen —
            # der Pool-Slot blieb belegt und der Pool erschöpfte dauerhaft
            # (Bot "healthy", produziert nichts). putconn(close=True) verwirft
            # die Connection UND gibt den Slot frei.
            try:
                _get_pool().putconn(conn, close=True)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass

    # ── cursor() passthrough ─────

    def cursor(self, *args, **kwargs):
        return object.__getattribute__(self, '_conn').cursor(*args, **kwargs)

    def commit(self) -> None:
        object.__getattribute__(self, '_conn').commit()

    def rollback(self) -> None:
        object.__getattribute__(self, '_conn').rollback()


def get_db_connection() -> PooledConnection:
    """
    Returns a pooled connection.  Drop-in replacement for the old
    psycopg2.connect() call — conn.close() puts it back in the pool.

    FIX P1.33: Liveness-Check beim Checkout — nach einem DB-Restart liegen
    tote Connections im Pool; ohne Check bekommt der Bot eine kaputte
    Connection und jede Query schlägt fehl, bis der Prozess neu startet.
    Tote Connections werden verworfen (Slot bleibt frei) und einmal
    nachgezogen (max 3 Versuche).

    Probe-TTL: der SELECT-1-Roundtrip läuft NICHT bei jedem Checkout —
    Hot-Loop-Caller (Orchestrator: alle 500ms) bekämen sonst fleet-weit
    hunderte Extra-Queries/min. Eine Connection gilt nach erfolgreicher
    Probe für _LIVENESS_TTL_SEC als lebend; nach DB-Restart dauert die
    Erkennung damit schlimmstenfalls TTL Sekunden (der Fehler schlägt dann
    ohnehin im Bot auf und die Connection fliegt beim close()).
    """
    last_err: Exception | None = None
    for _attempt in range(3):
        try:
            raw = _get_pool().getconn()
        except pg_pool.PoolError as e:
            logger.error(f"❌ Pool exhausted or error: {e}")
            raise
        except Exception as e:
            logger.error(f"❌ Kritischer Database connection error: {e}")
            raise
        try:
            if raw.closed:
                raise psycopg2.OperationalError("pooled connection already closed")
            now_mono = time.monotonic()
            if now_mono - _LAST_VERIFIED.get(id(raw), 0.0) >= _LIVENESS_TTL_SEC:
                with raw.cursor() as cur:
                    cur.execute("SELECT 1")
                raw.rollback()  # keine offene Txn aus dem Liveness-Check zurücklassen
                _LAST_VERIFIED[id(raw)] = now_mono
                if len(_LAST_VERIFIED) > 4 * _POOL_MAX:
                    # id()-Reuse-Hygiene: alte Einträge verwerfen
                    cutoff = now_mono - _LIVENESS_TTL_SEC
                    for k in [k for k, v in _LAST_VERIFIED.items() if v < cutoff]:
                        _LAST_VERIFIED.pop(k, None)
            return PooledConnection(raw)
        except Exception as e:
            last_err = e
            _LAST_VERIFIED.pop(id(raw), None)
            logger.warning(f"Tote Pool-Connection verworfen (Versuch {_attempt + 1}/3): {e}")
            try:
                _get_pool().putconn(raw, close=True)
            except Exception:
                try:
                    raw.close()
                except Exception:
                    pass
    logger.error(f"❌ Keine lebende DB-Connection nach 3 Versuchen: {last_err}")
    raise last_err if last_err else RuntimeError("no live DB connection")


def release_db_connection(conn: PooledConnection) -> None:
    """Explicit pool return — not needed when using conn.close() or the context manager."""
    conn.close()


@contextmanager
def db_connection() -> Iterator[PooledConnection]:
    """
    Recommended context manager for new code:

        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
            conn.commit()
    """
    conn = get_db_connection()
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def close_pool() -> None:
    """Closes all pool connections — call on clean shutdown."""
    global _POOL
    if _POOL is not None and not _POOL.closed:
        _POOL.closeall()
        logger.info("DB connection pool closed.")
