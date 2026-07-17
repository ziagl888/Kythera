# 38_ai_skw1_bot.py — SKW1 "Cross-Sectional Skewness L/S" (Studie K7, SHADOW-ONLY).
"""
Wöchentliche Querschnitts-Rotation nach realisierter Return-Schiefe: ranke das
liquiditätsgefilterte Universum nach `mom_skew_7d` (7d-Skew der 15m-Log-Returns);
**SHORT das oberste Dezil** (hoch-positive Schiefe = Lotterie-Coins), **LONG das
unterste Dezil** (negative Schiefe). Studie K7: Dezil-Monotonie ρ=−0,88,
Netto-Spread ≈ +2,5 %/Woche val+test-stabil.

**Reiner Shadow-Bot (kein Live-Post), BEIDE Beine.** Die Studie stuft SKW1 als
VALIDIERTES Feature/Retrain-Input ein, NICHT als schlüsselfertigen Edge (Netto nur
Fees+Funding modelliert, kein Slippage/Impact/Borrow; das LONG-Low-Skew-Bein ist
tail-getrieben, WR<0,5 in jedem Dezil). Der Bot validiert das Signal live über
überwachte, nie gepostete Trades (`ai_signals` ohne `telegram_outbox`, Tag `SKW1`).

Signal-Vertrag == Studie `tools/skewness_study.py` + geteilter Builder
`core/moment_features.py` (Regel 7): `build_moment_panel`→`moment_features_asof`
liefert `mom_skew_7d` as-of, geschlossene 15m-Bars, native-NaN. Reimplementiert
(studien-lokal): Querschnitts-Dezil-Rang, wöchentliches Montag-00:00-UTC-Raster,
Liquiditäts-Filter (trailing 7d-Dollar-Volumen, unteres Terzil raus),
MIN_COINS_PER_WEEK.

**WICHTIGE Divergenz (dokumentiert):** die Studie misst einen 1-WOCHEN-HALTE-Exit
(Timeout, kein TP/SL). Der Shadow-Monitor verfolgt aber First-Touch-TP/SL
(geteilte `hvn_sr_trade_geometry`). Der Shadow-PnL ist damit NICHT die
Studien-PnL, sondern eine richtungs-getreue First-Touch-Validierung desselben
Signals — bewusst, da der Monitor keinen Timeout-Exit kennt (Live-Money, kein
Monitor-Umbau). Market-Fill (entry1==entry2).

Läuft wöchentlich (Montag 00:00 UTC, Minute 31). Watchdog: start_delay=255.
"""

import datetime
import logging
import time

import numpy as np

from core.candles import read_candles
from core.database import get_db_connection
from core.market_utils import check_cooldown, load_coins, update_cooldown
from core.moment_features import build_moment_panel, moment_features_asof
from core.shadow_gate import SHADOW, leg_status, shadow_posting_enabled
from core.signal_post import has_open_ai_signal, post_shadow_ai_signal
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels, hvn_sr_trade_geometry

logging.basicConfig(level=logging.INFO, format="%(asctime)s - SKW1_BOT - %(message)s")
logger = logging.getLogger(__name__)

MODEL_ID = "SKW1"
TF = "15m"
SKEW_FEATURE = "mom_skew_7d"
N_DECILES = 10
LIQ_EXCLUDE_TERCILE = 1.0 / 3.0  # unteres Dollar-Vol-Terzil raus (== Studie)
MIN_COINS_PER_WEEK = 20  # zu dünner Querschnitt → Woche übersprungen
LIQ_BARS = 7 * 24 * 4  # 7d in 15m-Bars für das trailing Dollar-Volumen
MIN_15M_ROWS = 7 * 24 * 4 + 8  # volles 7d-Skew-Fenster (672) + Puffer
COOLDOWN_HOURS = 24 * 6  # eine Woche Sperre (fire-once je Rebalance/Richtung)
SHADOW_CONF = 0.5  # regelbasiert, kein Modell-Prob
SCAN_MINUTE = 31
MAX_PER_SIDE = 40  # Runaway-Schutz


def select_deciles(rows: list[tuple[str, float, float]]) -> tuple[list[str], list[str]]:
    """Pure Querschnitts-Selektion (DB-frei testbar). ``rows`` = (symbol, skew,
    dollar_vol). Liquiditäts-Filter (unteres Terzil raus) → Dezil-Rang nach Skew.
    Rückgabe (longs, shorts): LONG = unterstes Skew-Dezil, SHORT = oberstes."""
    if len(rows) < MIN_COINS_PER_WEEK:
        return [], []
    dvs = np.array([r[2] for r in rows], dtype=float)
    thr = float(np.nanquantile(dvs, LIQ_EXCLUDE_TERCILE))  # NaN-robust (Review-Fix)
    liquid = [r for r in rows if r[2] >= thr]
    if len(liquid) < MIN_COINS_PER_WEEK:
        return [], []
    liquid.sort(key=lambda r: r[1])  # aufsteigend nach Skew
    ndec = max(1, round(len(liquid) / N_DECILES))
    longs = [r[0] for r in liquid[:ndec]]  # niedrigste Skew → LONG
    shorts = [r[0] for r in liquid[-ndec:]]  # höchste Skew → SHORT
    return longs[:MAX_PER_SIDE], shorts[:MAX_PER_SIDE]


def gather(conn, coins: list[str], now: datetime.datetime) -> tuple[list[tuple[str, float, float]], dict[str, float]]:
    """(rows, entries): rows = [(symbol, skew, dollar_vol)], entries = {symbol:
    entry_price}. Liest je Coin die 15m-Kerzen einmal, baut das Moment-Panel
    (geteilter Builder) und liest den 7d-Skew as-of ``now``."""
    rows: list[tuple[str, float, float]] = []
    entries: dict[str, float] = {}
    for symbol in coins:
        try:
            # limit = nur die jüngsten Bars (das 7d-Skew-Fenster + Puffer) — ein
            # 15m-Voll-Read über die ganze Historie wäre ein wöchentlicher CPU-Spike
            # ohne Nutzen (min_periods=672 nutzt nur den Rand). Review-Fix.
            df = read_candles(
                conn,
                symbol,
                TF,
                limit=MIN_15M_ROWS + 16,
                include_forming=False,
                columns=("open_time", "close", "volume"),
            )
            if df is None or len(df) < MIN_15M_ROWS:
                continue
            panel = build_moment_panel(df, tf=TF)
            skew = moment_features_asof(panel, now, tf=TF).get(SKEW_FEATURE)
            if skew is None:
                continue
            close = df["close"].to_numpy(dtype=float)
            vol = df["volume"].to_numpy(dtype=float)
            dollar_vol = float(np.mean((close * vol)[-LIQ_BARS:]))
            entry = float(close[-1])
            # dollar_vol MUSS endlich sein: ein NaN würde sonst np.nanquantile im
            # Selector unschädlich machen, aber ein Coin mit NaN-Kerze soll gar nicht
            # erst in den Querschnitt (Review-Fix, Symmetrie zum skew-None-Guard).
            if entry <= 0 or not np.isfinite(dollar_vol):
                continue
            rows.append((symbol, float(skew), dollar_vol))
            entries[symbol] = entry
        except Exception as e:
            logger.error(f"gather {symbol}: {e}")
    return rows, entries


def emit(conn, symbol: str, direction: str, entry: float) -> None:
    """Ein Shadow-Bein (LONG/SHORT) mit geteilter Geometrie. Fail-safe: nie live."""
    if not shadow_posting_enabled() or leg_status(MODEL_ID, direction) != SHADOW:
        return
    if check_cooldown(conn, MODEL_ID, symbol, direction, COOLDOWN_HOURS):
        return
    if has_open_ai_signal(conn, symbol, direction, MODEL_ID):
        return
    is_long = direction == "LONG"
    supps, resis = get_hvn_and_sr_levels(conn, symbol, entry)
    _, sl, t_cands = hvn_sr_trade_geometry(entry, is_long, supps, resis)
    targets = ensure_min_tp_distance(t_cands[:20], entry, is_long, min_pct=0.05)
    if not targets:
        return
    if post_shadow_ai_signal(conn, MODEL_ID, symbol, direction, SHADOW_CONF, entry, entry, sl, targets, n_show=3):
        logger.info(f"👻 SKW1-Shadow {direction} {symbol} | Skew-Dezil @ {entry:g} (SL {sl:g}, {len(targets)} TP).")
    update_cooldown(conn, MODEL_ID, symbol, direction)  # committet atomar


def run_scan() -> None:
    coins = load_coins("coins.json", usdt_only=True, uppercase=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    conn = get_db_connection()
    try:
        rows, entries = gather(conn, coins, now)
        longs, shorts = select_deciles(rows)
        logger.info(f"🔍 SKW1-Rebalance: {len(rows)} bewertbar → {len(longs)} LONG / {len(shorts)} SHORT.")
        for direction, syms in (("LONG", longs), ("SHORT", shorts)):
            for symbol in syms:
                try:
                    emit(conn, symbol, direction, entries[symbol])
                except Exception as e:
                    logger.error(f"emit {symbol} {direction}: {e}")
                finally:
                    try:
                        conn.rollback()  # P2.32; nach dem Cooldown-Commit ein No-op
                    except Exception:
                        logger.error("Rollback fehlgeschlagen (tote Connection) — Abbruch.")
                        return
    finally:
        conn.close()
    logger.info("🏁 SKW1-Rebalance stopped.")


def main() -> None:
    logger.info("=== 🎲 AI SKW1 BOT (Cross-Sectional Skewness L/S, K7) GESTARTET — SHADOW-ONLY ===")
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
        if now.weekday() == 0 and now.hour == 0 and now.minute == SCAN_MINUTE:  # Montag 00:31 UTC
            run_scan()
            time.sleep(60)
        else:
            time.sleep(20)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")
