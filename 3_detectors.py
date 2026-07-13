import warnings

warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

import datetime
import json
import logging
import time

from core.candles import read_indicators
from core.config import MAIN_CHANNEL_COINS, TELEGRAM_CHANNELS
from core.database import get_db_connection
from core.live_price import get_live_price, get_live_prices_batch
from core.market_utils import update_cooldown
from strategies.strat_5_percent import analyze_coin as analyze_5_pct
from strategies.strat_fast_in_out import analyze_coin as analyze_fast

# --- IMPORT ALL STRATEGIES ---
from strategies.strat_main_channel import analyze_coin as analyze_main
from strategies.strat_support_resistance import analyze_coin as analyze_sr
from strategies.strat_volume_indicator import analyze_coin as analyze_vol

logging.basicConfig(level=logging.INFO, format='%(asctime)s - DETECTOR - %(message)s')
logger = logging.getLogger(__name__)

STATE_FILE = 'indicator_state.json'


def write_signal_atomic(conn, signal):
    """FIX: Schreibt active_trades_master + telegram_outbox in EINER Transaktion.

    Vorher waren das zwei separate Commits → wenn der Process zwischen Commit 1
    und Commit 2 crashte (OOM, SIGKILL, DB-Hiccup), hatte man einen "Geister-Trade":
    in active_trades_master ist er drin, aber in der Outbox nicht → niemand weiß
    dass er existiert, aber der Trade-Monitor verfolgt ihn trotzdem.
    """
    now = datetime.datetime.now()  # noqa: DTZ005 — P2.3: naive Server-Lokalzeit, Fix kommt mit dem R3-Pool-Flip (docs/UTC_POLICY.md §5)
    t2, t3, t4 = signal.get('target2', 0), signal.get('target3', 0), signal.get('target4', 0)

    msg = f"📈 Signal for {signal['coin']} 📈\n\n🚨 Direction: {signal['direction']}\n🚨 Leverage: {signal['lev']}\n🚨 Margin: Cross\n🏦 CMP Entry: $ {signal['entry']:.8f}\n"
    msg += f"💰 TP1: $ {signal['target1']:.8f}\n"
    if t2 > 0:
        msg += f"💰 TP2: $ {t2:.8f}\n"
    if t3 > 0:
        msg += f"💰 TP3: $ {t3:.8f}\n"
    if t4 > 0:
        msg += f"💰 TP4: $ {t4:.8f}\n"
    msg += f"\n💸 Stop Loss: $ {signal['sl']:.8f}\n\n🧠 {signal['strategy']} Strategy - V3"
    target_channel = TELEGRAM_CHANNELS.get(signal['strategy'])

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS active_trades_master (
                    id SERIAL PRIMARY KEY, strategy TEXT, time TIMESTAMP WITHOUT TIME ZONE,
                    coin TEXT, direction TEXT, lev TEXT, target1 REAL, target2 REAL,
                    target3 REAL, target4 REAL, sl REAL, entry REAL, posted TIMESTAMP WITHOUT TIME ZONE, status TEXT
                )
            """)
            cur.execute(
                "CREATE TABLE IF NOT EXISTS telegram_outbox (id SERIAL PRIMARY KEY, channel_id BIGINT, message TEXT, sent BOOLEAN DEFAULT FALSE)"
            )

            cur.execute(
                """
                INSERT INTO active_trades_master (
                    strategy, time, coin, direction, lev, target1, target2, target3, target4, sl, entry, posted, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'WORKING')
            """,
                (
                    signal['strategy'],
                    now,
                    signal['coin'],
                    signal['direction'],
                    signal['lev'],
                    signal['target1'],
                    t2,
                    t3,
                    t4,
                    signal['sl'],
                    signal['entry'],
                    now,
                ),
            )

            cur.execute("INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (target_channel, msg))

        # T-2026-CU-9050-024: a strategy can request its cooldown row via
        # signal['cooldown_module'] — written HERE so it commits atomically
        # with active_trades + outbox for exactly this signal. A strategy-side
        # upsert was either committed prematurely (cooldown persisted although
        # the signal write failed) or, with commit=False, leaked into an
        # EARLIER signal's commit in the same per-coin cycle.
        cooldown_module = signal.get('cooldown_module')
        if cooldown_module:
            update_cooldown(conn, cooldown_module, signal['coin'], signal['direction'], commit=False)

        conn.commit()
    except Exception:
        conn.rollback()
        raise


def run_detectors_for_timeframe(timeframe):
    # --- NEU: DIESER BLOCK MUSS GENAU HIER REIN! ---
    import warnings

    warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

    conn = get_db_connection()
    try:
        with open('coins.json') as f:
            all_symbols = json.load(f)

        logger.info(f"🔍 Starting strategy scans for timeframe: {timeframe}...")

        # P2.44: one batch price fetch per cycle instead of one HTTP call per coin.
        price_map = get_live_prices_batch()

        for symbol in all_symbols:
            try:
                # Load the newest 480 CLOSED indicator rows (R1, T-2026-CU-9050-108):
                # core.candles excludes the forming candle and returns ASC. All five
                # classic strategies index this frame DESC (iloc[0] = newest bar), so
                # it is flipped back to DESC to keep their contract byte-identical — the
                # only behavioural change is that iloc[0] is now the newest CLOSED bar
                # instead of the forming one. A raw ASC hand-off would silently make
                # iloc[0] the OLDEST bar (docs/OPUS-HANDOFF.md Falle 1).
                df = (
                    read_indicators(conn, symbol, timeframe, limit=480, include_forming=False)
                    .iloc[::-1]
                    .reset_index(drop=True)
                )
                df_indexed = df.set_index('open_time')  # Vorbereiten für die komplexen Bots
            except Exception:
                continue

            if df.empty:
                continue

            # Prefer the batched price; fall back to the per-symbol HTTP→DB path
            # only for symbols missing from the batch (or if the batch failed).
            live_price = price_map.get(symbol)
            if not live_price:
                live_price = get_live_price(symbol, conn)
            if not live_price:
                continue

            signals = []

            # P1.15: Per-Coin-Isolation. Vorher konnte ein einziger schlechter
            # Coin (kaputte Indikator-Row, unerwarteter dtype) eine Strategie
            # crashen → Exception lief bis main() durch (dort nur FileNotFoundError
            # gefangen) → ganzer Detector-Prozess tot, halbe Coin-Liste ungescannt.
            # Jetzt: rollback + ERROR-Log (Coin, Timeframe, Strategie via exc_info)
            # und weiter mit dem nächsten Coin.
            try:
                # --- 30m STRATEGIEN ---
                if timeframe == '30m':
                    # Fast In And Out auf expliziten Operator-Wunsch (04.07.) wieder
                    # aktiv — Audit Report 14/16 (Σ −25.843, Note F) bleibt als
                    # Kontext dokumentiert; Exit-/Tail-Redesign empfohlen.
                    s1 = analyze_fast(conn, symbol, df, live_price)
                    if s1:
                        signals.append(s1)

                    s2 = analyze_vol(conn, symbol, df_indexed, live_price)
                    if s2:
                        signals.append(s2)

                # --- 1h STRATEGIEN ---
                elif timeframe == '1h':
                    s3 = analyze_5_pct(conn, symbol, df, live_price)
                    if s3:
                        signals.append(s3)

                    s4 = analyze_sr(conn, symbol, df_indexed, live_price)
                    if s4:
                        signals.append(s4)

                    if symbol in MAIN_CHANNEL_COINS:
                        s5 = analyze_main(conn, symbol, df_indexed, live_price)
                        if s5:
                            signals.append(s5)

                for signal in signals:
                    logger.info(f"🚀 SIGNAL FOUND: [{signal['strategy']}] {signal['coin']} {signal['direction']}!")
                    # FIX: Atomarer Write statt zwei separate Commits (siehe write_signal_atomic)
                    write_signal_atomic(conn, signal)
            except Exception as e:
                # P1.15: aborted Txn säubern, damit der nächste Coin nicht mit einer
                # vergifteten Transaktion weiterläuft; exc_info zeigt die Strategie.
                conn.rollback()
                logger.error(f"❌ Coin {symbol} ({timeframe}) Strategie-Scan fehlgeschlagen: {e}", exc_info=True)
                continue

    finally:
        conn.close()


def main():
    logger.info("=== DETECTOR ENGINE STARTED ===")
    last_processed = {'30m': None, '1h': None}

    while True:
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)

            for tf in ['30m', '1h']:
                if tf in state and state[tf]['status'] == 'updated':
                    update_time = state[tf]['timestamp']
                    if update_time != last_processed[tf]:
                        logger.info(f"🟢 Neues {tf} Indikator-Update erkannt!")
                        run_detectors_for_timeframe(tf)
                        last_processed[tf] = update_time
                        logger.info(f"🏁 Scans für {tf} stopped.")
        except FileNotFoundError:
            pass
        except Exception as e:
            # P1.15: breiter Fang statt Prozess-Tod. Was auch immer im Scan-Pass
            # durchschlägt (DB-Reconnect, State-Datei korrupt), wird geloggt und
            # nach Backoff neu versucht — der Watchdog muss den Detector nicht neu starten.
            logger.error(f"❌ Detector-Loop-Fehler, Backoff 30s: {e}", exc_info=True)
            time.sleep(30)

        # Polling-Intervall: 30s. Die Indicator-Engine schreibt Updates nur zu Candle-Close-Zeiten
        # (alle 30/60 Min), aber wir wollen signals nicht bis zu 10 Min after dem Close verzögern.
        time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")
