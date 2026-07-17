# 37_ai_tsm1_bot.py — TSM1 "Time-Series-Momentum" (Studie K1, SHADOW-ONLY).
"""
Zeitreihen-Momentum-Ausbruch auf dem 4h-Chart: die Rate-of-Change über L=12
4h-Kerzen kreuzt von AUSSERHALB nach INNERHALB des ±k·σ-Bands (σ = 90d-Rolling-
Std der ROC, k=0,5) → Momentum-Signal. Studien-Bestzelle `4h|L12|k0.5`.

**Reiner Shadow-Bot (kein Live-Post), NUR SHORT.** Die Studie (tsmom_study) ist
insgesamt `no-op/paper-falsified` (bestes OOS-Cell test −0,05 %/Trade); der EINZIGE
nicht-falsifizierte Teil ist die SHORT-Richtung (in JEDER Zelle positiv, während
LONG tief negativ ist — der Netto-Verlust kommt komplett vom LONG-Bein). Das
LONG-Bein wird daher gar nicht erst emittiert (nur `("TSM1","SHORT")` ist als
SHADOW registriert). Der Bot validiert das SHORT-Momentum live über überwachte,
nie gepostete Trades (`ai_signals` ohne `telegram_outbox`, Tag `TSM1`).

Signal-Vertrag == Studie `tools/tsmom_study.py::signal_events` (Zelle 4h|L12|k0.5):
  * ROC_L[t] = close[t]/close[t−12] − 1 auf nativen 4h-Closes.
  * σ = ROC.rolling(90d = 540 4h-Bars, min_periods=540).std(); band = 0,5·σ.
  * SHORT-Crossing = ROC[t] ≤ −band[t] UND ROC[t−1] > −band[t−1] (Durchbruch
    nach UNTEN durch das −Band: vorher oberhalb, jetzt darunter).
  * Geometrie = geteilte `hvn_sr_trade_geometry` (SHORT), Market-Fill am 4h-Close
    (== Studien-Entry `a_close`, NICHT der ±5%-entry2), `ensure_min_tp_distance
    (min_pct=0.05)`, 3 veröffentlichte TPs.

Feuert je Coin einmal pro offenem Trade (`has_open_ai_signal` + Cooldown) — der
Re-Entry erst nach dem Close spiegelt die Studien-Regel "1 offene Position je
Coin/Richtung, Re-Entry nach Exit". Läuft alle 4h (Minute 29 der Stunden
0/4/8/12/16/20 UTC, kurz nach dem 4h-Close). Watchdog: start_delay=247.
"""

import datetime
import logging
import time

import numpy as np
import pandas as pd

from core.candles import read_candles
from core.database import get_db_connection
from core.market_utils import check_cooldown, load_coins, update_cooldown
from core.shadow_gate import SHADOW, leg_status, shadow_posting_enabled
from core.signal_post import has_open_ai_signal, post_shadow_ai_signal
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels, hvn_sr_trade_geometry

logging.basicConfig(level=logging.INFO, format="%(asctime)s - TSM1_BOT - %(message)s")
logger = logging.getLogger(__name__)

MODEL_ID = "TSM1"
DIRECTION = "SHORT"  # nur die nicht-falsifizierte Richtung; LONG bleibt unemittiert
TF = "4h"
ROC_L = 12  # Lookback in 4h-Bars (Bestzelle)
K_SIGMA = 0.5  # Band-Breite in σ (Bestzelle)
SIGMA_BARS = 540  # 90d Rolling-Std der ROC bei 4h (90*24/4)
MIN_4H_ROWS = SIGMA_BARS + ROC_L + 2  # volle σ-Fenster + Lookback + Crossing-Paar
COOLDOWN_HOURS = 4  # eine 4h-Kerze Sperre; has_open blockt zusätzlich bis zum Close
SHADOW_CONF = 0.5  # regelbasiert, kein Modell-Prob
SCAN_MINUTE = 29  # kurz nach dem 4h-Close, nur zu den 4h-Stunden (siehe main)
MAX_CANDIDATES = 40  # Sicherheits-Kappe pro Scan (Runaway-Schutz)


def short_crossing(close: np.ndarray) -> bool:
    """True, wenn die LETZTE geschlossene 4h-Kerze ein SHORT-Crossing ist
    (ROC von OBERHALB −band nach UNTERHALB). Pure Funktion → DB-frei testbar."""
    if len(close) < MIN_4H_ROWS:
        return False
    roc = np.full(len(close), np.nan)
    roc[ROC_L:] = close[ROC_L:] / close[:-ROC_L] - 1.0
    sigma = pd.Series(roc).rolling(window=SIGMA_BARS, min_periods=SIGMA_BARS).std().to_numpy()
    band = K_SIGMA * sigma
    r, rp, b, bp = roc[-1], roc[-2], band[-1], band[-2]
    if not all(np.isfinite(x) for x in (r, rp, b, bp)):
        return False
    return bool(r <= -b and rp > -bp)  # Durchbruch nach unten durchs −Band (== Studie signal_events)


def process_coin(conn, symbol: str) -> bool:
    """True, wenn ein Shadow-Trade geschrieben wurde (für die Kandidaten-Kappe)."""
    if not shadow_posting_enabled():
        return False
    if leg_status(MODEL_ID, DIRECTION) != SHADOW:  # fail-safe: nie live posten
        return False
    if check_cooldown(conn, MODEL_ID, symbol, DIRECTION, COOLDOWN_HOURS):
        return False
    if has_open_ai_signal(conn, symbol, DIRECTION, MODEL_ID):
        return False

    df = read_candles(conn, symbol, TF, limit=MIN_4H_ROWS + 8, include_forming=False, columns=("open_time", "close"))
    if df is None or len(df) < MIN_4H_ROWS:
        return False
    close = df["close"].to_numpy(dtype=float)
    if not short_crossing(close):
        return False

    entry1 = float(close[-1])
    if entry1 <= 0:
        return False
    supps, resis = get_hvn_and_sr_levels(conn, symbol, entry1)
    _, sl, t_cands = hvn_sr_trade_geometry(entry1, False, supps, resis)
    targets = ensure_min_tp_distance(t_cands[:20], entry1, False, min_pct=0.05)
    if not targets:
        return False

    wrote = post_shadow_ai_signal(conn, MODEL_ID, symbol, DIRECTION, SHADOW_CONF, entry1, entry1, sl, targets, n_show=3)
    if wrote:
        logger.info(
            f"👻 TSM1-Shadow SHORT {symbol} | 4h-ROC-Crossing @ {entry1:g} (SL {sl:g}, {len(targets)} TP) — überwacht."
        )
    update_cooldown(conn, MODEL_ID, symbol, DIRECTION)  # committet atomar
    return bool(wrote)


def run_scan() -> None:
    coins = load_coins("coins.json", usdt_only=True, uppercase=True)
    logger.info(f"🔍 TSM1-Scan (4h-Close): {len(coins)} Coins.")
    conn = get_db_connection()
    conn_dead = False
    fired = 0
    try:
        for symbol in coins:
            try:
                if process_coin(conn, symbol):
                    fired += 1
            except Exception as e:
                logger.error(f"Error für {symbol}: {e}")
            finally:
                try:
                    conn.rollback()  # P2.32; nach dem Cooldown-Commit ein No-op
                except Exception:
                    logger.error("Rollback fehlgeschlagen (tote Connection) — Scan-Abbruch.")
                    conn_dead = True
            if conn_dead or fired >= MAX_CANDIDATES:
                break
    finally:
        conn.close()
    logger.info(f"🏁 TSM1-Scan stopped ({fired} Shadow-Signale).")


def main() -> None:
    logger.info("=== 📈 AI TSM1 BOT (Time-Series-Momentum, K1) GESTARTET — SHADOW-ONLY, SHORT ===")
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
        if now.hour % 4 == 0 and now.minute == SCAN_MINUTE:  # kurz nach jedem 4h-Close
            run_scan()
            time.sleep(60)
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")
