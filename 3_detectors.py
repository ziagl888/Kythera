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
from core.market_utils import DetectorCycle, update_cooldown
from strategies.strat_5_percent import analyze_coin as analyze_5_pct
from strategies.strat_fast_in_out import analyze_coin as analyze_fast

# --- IMPORT ALL STRATEGIES ---
from strategies.strat_main_channel import analyze_coin as analyze_main
from strategies.strat_support_resistance import analyze_coin as analyze_sr
from strategies.strat_volume_indicator import analyze_coin as analyze_vol

logging.basicConfig(level=logging.INFO, format='%(asctime)s - DETECTOR - %(message)s')
logger = logging.getLogger(__name__)

STATE_FILE = 'indicator_state.json'

# T-2026-CU-9050-172 (1): column projection for the per-coin indicator read.
# Union of every column the five classic strategies actually read (27 of ~120):
# REQUIRED_COLUMNS of strat_5_percent ⊇ strat_fast_in_out, plus the rsi/close/
# S/R/atr_14 reads of Support Resistance / Main Channel / Volume Indicator, plus
# open_time (frame index; read_indicators orders by it).
# P2.43 lesson: a MISSING column kills 5-Percent/Fast-In-Out signals SILENTLY
# via their `all(col in data.columns)` check — this projection is therefore
# pinned by backtest/test_detector_scan_optimization.py:
#   projection ⊇ each strategy's REQUIRED_COLUMNS and every AST-collected
#                per-row column read of all five strategies, and
#   projection ⊆ the engine DDL (a typo'd name would make the read itself fail
#                and silently skip the coin via the try/except below).
DETECTOR_INDICATOR_COLUMNS = (
    'open_time',
    'close',
    'rsi_9',
    'rsi_14',
    'tsi_fast_12_7_7',
    'tsi_fast_12_7_7_signal',
    'ema_9',
    'ema_12',
    'ema_21',
    'ema_26',
    'ema_55',
    'ema_89',
    'ema_200',
    'wma_9',
    'wma_12',
    'wma_21',
    'wma_26',
    'kama_9',
    'kama_12',
    'kama_21',
    'macd_dif_fast_9_21_9',
    'macd_dea_fast_9_21_9',
    'donchian_mid_4',
    'boll_mid_20',
    'atr_14',
    'support_price',
    'resistance_price',
)


def _strategies_for(timeframe, symbol):
    """Strategy roster (active_trades_master names) scanned for `symbol` in this
    timeframe — MUST mirror the dispatch in run_detectors_for_timeframe. The
    whole-coin prefilter may only skip a coin when every one of these
    strategies is WORKING in both directions."""
    if timeframe == '30m':
        return ('Fast In And Out', 'Volume Indicator')
    if timeframe == '1h':
        if symbol in MAIN_CHANNEL_COINS:
            return ('5 Percent', 'Support Resistance', 'Main Channel')
        return ('5 Percent', 'Support Resistance')
    return ()


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


# T-2026-CU-9050-172: index RECOMMENDATION only (execution is a VPS session,
# operator-gated — CLAUDE.md hard rule 1): the snapshot + the monitors' point
# lookups would profit from
#   CREATE INDEX ... ON active_trades_master (strategy, coin, direction) WHERE status = 'WORKING';
#   CREATE INDEX ... ON closed_trades_master (direction, posted);
#
# T-2026-CU-9050-172 (5): DB round-trips per scan cycle (N symbols ≈ 530),
# documented for the query-count acceptance criterion:
#   BEFORE: N × indicator read (SELECT * ≈ 120 cols); Volume Indicator issued
#           2 candle reads per coin (5d spike window + 10d baseline) plus, when
#           a spike fired, 1 active-trade + 1 cooldown point query and the 90d
#           HVN read; 5-Percent/Fast-In-Out issued up to 2 check_recent_trades
#           + 2 is_trade_already_active point queries per condition-passing
#           coin; SR/Main Channel 1 active-trade point query per S/R hit.
#           30m worst case ≈ N×3 reads + point queries ≈ 1.600+ queries/cycle.
#   AFTER:  1 active_trades_master snapshot per cycle (+ ≤1 trade_cooldowns
#           snapshot per module on first use) + ≤4 memoised check_recent_trades
#           (one per distinct (direction, hours, count)); (N − prefilter-skips)
#           × indicator read projected to 27 columns; Volume Indicator issues
#           1 bundled 15d candle read per coin (instead of 2) and the HVN read
#           only on the unchanged gated path.
#           30m ≈ N×2 reads + ~5 snapshot/memo queries per cycle.
def run_detectors_for_timeframe(timeframe):
    # --- NEU: DIESER BLOCK MUSS GENAU HIER REIN! ---
    import warnings

    warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

    conn = get_db_connection()
    try:
        with open('coins.json') as f:
            all_symbols = json.load(f)

        logger.info(f"🔍 Starting strategy scans for timeframe: {timeframe}...")

        cycle_started = time.perf_counter()

        # P2.44: one batch price fetch per cycle instead of one HTTP call per coin.
        price_map = get_live_prices_batch()

        # T-2026-CU-9050-172 (4): one guard snapshot per cycle. The snapshot
        # lives for exactly this cycle; own signal writes are mirrored into it
        # below so a later in-cycle lookup sees what a fresh DB read would see.
        t0 = time.perf_counter()
        cycle = DetectorCycle(conn)
        timings = {'snapshots': time.perf_counter() - t0, 'reads': 0.0, 'writes': 0.0}
        scan_timings: dict = {}
        coins_scanned = prefilter_skips = signals_found = 0

        def timed_scan(strategy_name, analyze_fn, frame, symbol, live_price):
            """Strategy call with per-strategy duration accounting (5)."""
            t_scan = time.perf_counter()
            try:
                return analyze_fn(conn, symbol, frame, live_price, cycle=cycle)
            finally:
                scan_timings[strategy_name] = scan_timings.get(strategy_name, 0.0) + (time.perf_counter() - t_scan)

        for symbol in all_symbols:
            # T-2026-CU-9050-172 (4b): whole-coin prefilter. Every emission path
            # of every strategy scanned in this timeframe requires its own
            # (strategy, direction) to have no WORKING row — if ALL of them are
            # occupied in BOTH directions, nothing can be emitted and the
            # indicator read + spike checks are skipped entirely. A partially
            # occupied coin is still scanned (the per-direction guards inside
            # the strategies decide, exactly as before).
            roster = _strategies_for(timeframe, symbol)
            if roster and cycle.all_directions_active(symbol, roster):
                prefilter_skips += 1
                continue

            t0 = time.perf_counter()
            try:
                # Load the newest 480 CLOSED indicator rows (R1, T-2026-CU-9050-108):
                # core.candles excludes the forming candle and returns ASC. All five
                # classic strategies index this frame DESC (iloc[0] = newest bar), so
                # it is flipped back to DESC to keep their contract byte-identical — the
                # only behavioural change is that iloc[0] is now the newest CLOSED bar
                # instead of the forming one. A raw ASC hand-off would silently make
                # iloc[0] the OLDEST bar (docs/OPUS-HANDOFF.md Falle 1).
                #
                # limit stays 480 for BOTH timeframes (T-172 deliverable 2
                # deliberately omitted): besides the SR first-hit scan, the
                # first_valid_index fallback on support_price (strat_5_percent,
                # strat_fast_in_out) may legitimately reach arbitrary frame depth
                # as long as pre-P1.12 broadcast rows survive in the DB (the
                # T-061 head-nulling recompute is incomplete) — a smaller frame
                # would change sr_row in exactly that edge and is therefore not
                # provably behaviour-invariant.
                df = (
                    read_indicators(
                        conn,
                        symbol,
                        timeframe,
                        limit=480,
                        include_forming=False,
                        columns=DETECTOR_INDICATOR_COLUMNS,
                    )
                    .iloc[::-1]
                    .reset_index(drop=True)
                )
                df_indexed = df.set_index('open_time')  # Vorbereiten für die komplexen Bots
            except Exception:
                continue
            finally:
                timings['reads'] += time.perf_counter() - t0

            if df.empty:
                continue

            # Prefer the batched price; fall back to the per-symbol HTTP→DB path
            # only for symbols missing from the batch (or if the batch failed).
            live_price = price_map.get(symbol)
            if not live_price:
                live_price = get_live_price(symbol, conn)
            if not live_price:
                continue

            coins_scanned += 1
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
                    s1 = timed_scan('Fast In And Out', analyze_fast, df, symbol, live_price)
                    if s1:
                        signals.append(s1)

                    s2 = timed_scan('Volume Indicator', analyze_vol, df_indexed, symbol, live_price)
                    if s2:
                        signals.append(s2)

                # --- 1h STRATEGIEN ---
                elif timeframe == '1h':
                    s3 = timed_scan('5 Percent', analyze_5_pct, df, symbol, live_price)
                    if s3:
                        signals.append(s3)

                    s4 = timed_scan('Support Resistance', analyze_sr, df_indexed, symbol, live_price)
                    if s4:
                        signals.append(s4)

                    if symbol in MAIN_CHANNEL_COINS:
                        s5 = timed_scan('Main Channel', analyze_main, df_indexed, symbol, live_price)
                        if s5:
                            signals.append(s5)

                for signal in signals:
                    logger.info(f"🚀 SIGNAL FOUND: [{signal['strategy']}] {signal['coin']} {signal['direction']}!")
                    # FIX: Atomarer Write statt zwei separate Commits (siehe write_signal_atomic)
                    t0 = time.perf_counter()
                    write_signal_atomic(conn, signal)
                    timings['writes'] += time.perf_counter() - t0
                    # (4b): mirror the own write into the cycle snapshot — the old
                    # per-signal DB reads saw this cycle's earlier commits.
                    cycle.note_signal_written(
                        signal['strategy'],
                        signal['coin'],
                        signal['direction'],
                        cooldown_module=signal.get('cooldown_module'),
                    )
                    signals_found += 1
            except Exception as e:
                # P1.15: aborted Txn säubern, damit der nächste Coin nicht mit einer
                # vergifteten Transaktion weiterläuft; exc_info zeigt die Strategie.
                conn.rollback()
                logger.error(f"❌ Coin {symbol} ({timeframe}) Strategie-Scan fehlgeschlagen: {e}", exc_info=True)
                continue

        # T-2026-CU-9050-172 (5): ONE aggregated timing line per cycle — no
        # per-coin spam. Strategy-scan durations include their internal candle
        # reads (spike/HVN/OHLCV/OBV) and guard lookups.
        per_strategy = ", ".join(f"{name} {secs:.2f}s" for name, secs in scan_timings.items())
        logger.info(
            f"⏱ {timeframe} cycle done in {time.perf_counter() - cycle_started:.2f}s — "
            f"{coins_scanned} coins scanned, {prefilter_skips} prefilter-skipped, {signals_found} signals | "
            f"snapshots {timings['snapshots']:.2f}s, indicator-reads {timings['reads']:.2f}s, "
            f"strategy-scans {sum(scan_timings.values()):.2f}s ({per_strategy or '—'}), "
            f"signal-writes {timings['writes']:.2f}s"
        )

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
