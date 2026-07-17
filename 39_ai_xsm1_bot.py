# 39_ai_xsm1_bot.py — XSM1/XSR1 "Cross-Sectional Momentum/Reversal" (K2, SHADOW-ONLY).
"""
Wöchentliche Querschnitts-Rotation nach F-Tage-Rendite (F=84d, Bestzelle): ranke
das liquiditätsgefilterte Universum nach roher Formations-Rendite
`close[t]/close[t−F] − 1` und handle das OBERSTE Dezil (stärkste Momentum-Coins).
Zwei KONKURRIERENDE Hypothesen auf derselben Dezil-Menge:
  * **XSM1 (Momentum)** = LONG das oberste Dezil (Fortsetzung).
  * **XSR1 (Reversal)** = SHORT das oberste Dezil (Mean-Reversion).

**Reiner Shadow-Bot (kein Live-Post).** Die Studie K2 ist `weak/inconsistent-
spread, NICHT deploybar` (0 robuste Zellen; die beste Val-Zelle F84|XSR1_SHORT
kippt OOS auf −1,6 % = Overfit). Es gibt KEINEN Edge — der Bot lässt beide
Hypothesen live gegeneinander laufen (jeweils eigener Tag, unabhängig überwacht),
um zu sehen, ob eine Seite überhaupt trägt. Überwachte, nie gepostete Trades
(`ai_signals` ohne `telegram_outbox`).

Signal-Vertrag == Studie `tools/xs_momentum_study.py` (Zelle F84|raw|absolute):
roher F-Tage-Return auf geschlossenen 1d-Closes, Dezil-Rang, BTC aus der
handelbaren Menge ausgeschlossen, Liquiditäts-Filter (unteres Dollar-Vol-Terzil
raus), MIN_COINS_PER_WEEK.

**WICHTIGE Divergenz (dokumentiert):** die Studie misst einen H=28-Tage-Halte-Exit
(Timeout, kein TP/SL); der Shadow-Monitor verfolgt First-Touch-TP/SL (geteilte
`hvn_sr_trade_geometry`). Der Shadow-PnL ist damit NICHT die Studien-PnL, sondern
eine richtungs-getreue First-Touch-Validierung — bewusst (Monitor kennt keinen
Timeout-Exit). Market-Fill (entry1==entry2). Der bekannte Studien-Look-Ahead
(Entry am 1d-OPEN statt CLOSE) entfällt hier: der Bot feuert NACH dem Wochen-Close.

Läuft wöchentlich (Montag 00:37 UTC). Watchdog: start_delay=263.
"""

import datetime
import logging
import time

import numpy as np

from core.candles import read_candles
from core.database import get_db_connection
from core.market_utils import check_cooldown, load_coins, update_cooldown
from core.shadow_gate import SHADOW, leg_status, shadow_posting_enabled
from core.signal_post import has_open_ai_signal, post_shadow_ai_signal
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels, hvn_sr_trade_geometry

logging.basicConfig(level=logging.INFO, format="%(asctime)s - XSM1_BOT - %(message)s")
logger = logging.getLogger(__name__)

XSM_TAG = "XSM1"  # Momentum → LONG das oberste Dezil
XSR_TAG = "XSR1"  # Reversal  → SHORT das oberste Dezil
TF = "1d"
F_DAYS = 84  # Formations-Fenster (Bestzelle)
DECILE_FRAC = 0.10  # oberstes Dezil
LIQ_EXCLUDE_TERCILE = 1.0 / 3.0
MIN_COINS_PER_WEEK = 20
MIN_1D_ROWS = F_DAYS + 3  # F-Lookback + Puffer
COOLDOWN_HOURS = 24 * 6  # eine Woche Sperre (fire-once je Rebalance/Tag)
SHADOW_CONF = 0.5
SCAN_MINUTE = 37
MAX_TOP = 40  # Kappe für die Dezil-Größe (Shadow-Last-Schutz)
EXCLUDE = {"BTCUSDT"}  # BTC nicht in der handelbaren Querschnitts-Menge (== Studie)


def select_top_decile(rows: list[tuple[str, float, float]]) -> list[str]:
    """Pure Querschnitts-Selektion (DB-frei testbar). ``rows`` = (symbol,
    f_return, dollar_vol). Liquiditäts-Filter (unteres Terzil raus) → oberstes
    Dezil nach F-Rendite. Beide Hypothesen (XSM1/XSR1) handeln DIESE Menge."""
    if len(rows) < MIN_COINS_PER_WEEK:
        return []
    dvs = np.array([r[2] for r in rows], dtype=float)
    thr = float(np.nanquantile(dvs, LIQ_EXCLUDE_TERCILE))  # NaN-robust (Review-Fix)
    liquid = [r for r in rows if r[2] >= thr]
    if len(liquid) < MIN_COINS_PER_WEEK:
        return []
    liquid.sort(key=lambda r: r[1])  # aufsteigend nach F-Rendite
    ndec = max(1, round(len(liquid) * DECILE_FRAC))
    top = [r[0] for r in liquid[-ndec:]]  # höchste F-Rendite = oberstes Dezil
    return top[:MAX_TOP]


def gather(conn, coins: list[str], now: datetime.datetime) -> tuple[list[tuple[str, float, float]], dict[str, float]]:
    """(rows, entries): rows = [(symbol, f_return, dollar_vol)]."""
    rows: list[tuple[str, float, float]] = []
    entries: dict[str, float] = {}
    for symbol in coins:
        if symbol in EXCLUDE:
            continue
        try:
            df = read_candles(
                conn, symbol, TF, limit=MIN_1D_ROWS + 8, include_forming=False, columns=("open_time", "close", "volume")
            )
            if df is None or len(df) < MIN_1D_ROWS:
                continue
            close = df["close"].to_numpy(dtype=float)
            base = close[-1 - F_DAYS]
            if base <= 0:
                continue
            f_return = close[-1] / base - 1.0
            vol = df["volume"].to_numpy(dtype=float)
            dollar_vol = float(np.mean((close * vol)[-F_DAYS:]))
            entry = float(close[-1])
            # dollar_vol endlich (Review-Fix, Symmetrie zum f_return-Guard) —
            # eine NaN-Kerze darf den Coin nicht in den Querschnitt bringen.
            if entry <= 0 or not np.isfinite(f_return) or not np.isfinite(dollar_vol):
                continue
            rows.append((symbol, f_return, dollar_vol))
            entries[symbol] = entry
        except Exception as e:
            logger.error(f"gather {symbol}: {e}")
    return rows, entries


def emit(conn, symbol: str, tag: str, direction: str, entry: float) -> None:
    """Ein Shadow-Bein unter ``tag``/``direction`` mit geteilter Geometrie."""
    if not shadow_posting_enabled() or leg_status(tag, direction) != SHADOW:
        return
    if check_cooldown(conn, tag, symbol, direction, COOLDOWN_HOURS):
        return
    if has_open_ai_signal(conn, symbol, direction, tag):
        return
    is_long = direction == "LONG"
    supps, resis = get_hvn_and_sr_levels(conn, symbol, entry)
    _, sl, t_cands = hvn_sr_trade_geometry(entry, is_long, supps, resis)
    targets = ensure_min_tp_distance(t_cands[:20], entry, is_long, min_pct=0.05)
    if not targets:
        return
    if post_shadow_ai_signal(conn, tag, symbol, direction, SHADOW_CONF, entry, entry, sl, targets, n_show=3):
        logger.info(f"👻 {tag}-Shadow {direction} {symbol} | Top-Dezil @ {entry:g} (SL {sl:g}, {len(targets)} TP).")
    update_cooldown(conn, tag, symbol, direction)  # committet atomar


def run_scan() -> None:
    coins = load_coins("coins.json", usdt_only=True, uppercase=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    conn = get_db_connection()
    try:
        rows, entries = gather(conn, coins, now)
        top = select_top_decile(rows)
        logger.info(f"🔍 XSM1/XSR1-Rebalance: {len(rows)} bewertbar → Top-Dezil {len(top)} (LONG XSM1 + SHORT XSR1).")
        # Beide konkurrierenden Hypothesen auf derselben Dezil-Menge.
        for symbol in top:
            for tag, direction in ((XSM_TAG, "LONG"), (XSR_TAG, "SHORT")):
                try:
                    emit(conn, symbol, tag, direction, entries[symbol])
                except Exception as e:
                    logger.error(f"emit {symbol} {tag}: {e}")
                finally:
                    try:
                        conn.rollback()  # P2.32; nach dem Cooldown-Commit ein No-op
                    except Exception:
                        logger.error("Rollback fehlgeschlagen (tote Connection) — Abbruch.")
                        return
    finally:
        conn.close()
    logger.info("🏁 XSM1/XSR1-Rebalance stopped.")


def main() -> None:
    logger.info("=== 🔄 AI XSM1/XSR1 BOT (Cross-Sectional Momentum/Reversal, K2) GESTARTET — SHADOW-ONLY ===")
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_cooldowns (
                module VARCHAR(50), coin VARCHAR(20), direction VARCHAR(10),
                last_posted_at TIMESTAMP WITH TIME ZONE,
                PRIMARY KEY (module, coin, direction)
            );
        """)
    conn.commit()
    conn.close()

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        if now.weekday() == 0 and now.hour == 0 and now.minute == SCAN_MINUTE:  # Montag 00:37 UTC
            run_scan()
            time.sleep(60)
        else:
            time.sleep(20)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")
