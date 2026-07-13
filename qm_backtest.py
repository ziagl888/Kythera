import warnings

warnings.filterwarnings("ignore")

import io
import logging
import sys
import time
from datetime import timedelta

import numpy as np
import pandas as pd
import scipy.signal

# Zwingt die Windows-Konsole, UTF-8 (Emojis) zu akzeptieren
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# --- Eigene DB Connection importieren ---
from core.candles import read_candles
from core.database import get_db_connection
from core.market_utils import load_coins as _core_load_coins
from core.time import utc_now

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# Known backtest limitations (P3.6): fees ARE applied (TAKER_FEE below) and a
# single running equity is tracked, but there is no concurrency/margin cap —
# every coin trades a full TRADE_MARGIN in parallel, so START_CAPITAL is not a
# real constraint. load_coins() also reads today's coins.json over ~2y, so
# delisted losers are absent (survivorship bias).

# ==========================================
# 🛠️ BACKTEST KONFIGURATION
# ==========================================
COINS_FILE = "coins.json"
OUTPUT_FILE = "qm_mass_results.txt"
TIMEFRAMES = ['1h', '4h']

START_CAPITAL = 100000.0
TRADE_MARGIN = 5000.0  # 5.000$ Einsatz pro Trade
LEVERAGE = 20  # 10x Hebel -> 50.000$ Positionsgröße
TAKER_FEE = 0.0004  # 0.04% Handelsgebühr pro Order (0.08% Total)

PIVOT_WINDOW = 5
ORDER_EXPIRY = 100


# ==========================================
# 📊 DATA & HELPERS
# ==========================================
def load_coins():
    # P3.1: read/dict-unwrap/USDT-filter/symbol-validation via the canon.
    return _core_load_coins(COINS_FILE, usdt_only=True, uppercase=True)


def fetch_db_data(symbol, tf):
    try:
        conn = get_db_connection()
        # Über core.candles: GESCHLOSSENE Kerzen, ASC (include_forming=False).
        df = read_candles(
            conn,
            symbol,
            tf,
            start=utc_now() - timedelta(days=730),
            include_forming=False,
            columns=('open_time', 'open', 'high', 'low', 'close'),
        )
        conn.close()

        for c in ['open', 'high', 'low', 'close']:
            df[c] = df[c].astype(float)
        df.dropna(inplace=True)
        return df.reset_index(drop=True)
    except Exception:
        # Tabelle existiert evtl. nicht, still skippingn
        return pd.DataFrame()


# ==========================================
# 🚀 BACKTEST ENGINE (CHRONOLOGISCH)
# ==========================================
def run_simulation(df, symbol, tf):
    capital = START_CAPITAL

    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values

    peak_idx = scipy.signal.argrelextrema(highs, np.greater, order=PIVOT_WINDOW)[0]
    trough_idx = scipy.signal.argrelextrema(lows, np.less, order=PIVOT_WINDOW)[0]

    raw_pivots = [(i, 1, highs[i]) for i in peak_idx] + [(i, -1, lows[i]) for i in trough_idx]
    raw_pivots.sort(key=lambda x: x[0])

    active_trades = []
    pending_orders = []

    wins = 0
    losses = 0
    max_capital = capital
    max_drawdown = 0.0

    live_alt_pivots = []
    raw_pivot_pointer = 0
    processed_qm_ids = set()

    for curr_idx in range(PIVOT_WINDOW * 2, len(df)):
        curr_high = highs[curr_idx]
        curr_low = lows[curr_idx]
        curr_price = closes[curr_idx]

        # A) AKTIVE TRADES PRÜFEN
        trades_to_remove = []
        for trade in active_trades:
            direction = trade['direction']
            entry = trade['entry']
            sl = trade['sl']
            tp = trade['tp']

            is_closed = False
            exit_price = 0.0

            if direction == "LONG":
                if curr_low <= sl:
                    is_closed, exit_price = True, sl
                elif curr_high >= tp:
                    is_closed, exit_price = True, tp
            elif direction == "SHORT":
                if curr_high >= sl:
                    is_closed, exit_price = True, sl
                elif curr_low <= tp:
                    is_closed, exit_price = True, tp

            if is_closed:
                nominal_size = TRADE_MARGIN * LEVERAGE
                qty = nominal_size / entry

                if direction == "LONG":
                    raw_pnl = (exit_price - entry) * qty
                else:
                    raw_pnl = (entry - exit_price) * qty

                fee = nominal_size * TAKER_FEE * 2
                net_pnl = raw_pnl - fee

                capital += net_pnl

                if net_pnl > 0:
                    wins += 1
                else:
                    losses += 1

                if capital > max_capital:
                    max_capital = capital
                drawdown = (max_capital - capital) / max_capital * 100
                if drawdown > max_drawdown:
                    max_drawdown = drawdown

                trades_to_remove.append(trade)

        for t in trades_to_remove:
            active_trades.remove(t)

        # B) PENDING ORDERS PRÜFEN
        orders_to_remove = []
        for order in pending_orders:
            if curr_idx - order['created_at'] > ORDER_EXPIRY:
                orders_to_remove.append(order)
                continue

            dir = order['direction']
            entry = order['entry']
            sl = order['sl']
            tp = order['tp']

            triggered = False
            stopped_on_fill = False

            # FIX (P1.30): SL-Durchstich ist KEINE Invalidierung — der SL liegt
            # jenseits des Entrys, dieselbe Kerze hat also zwingend auch den Entry
            # berührt. Konservativ als fill-then-stop werten (sofortiger Verlust)
            # statt den garantierten Verlierer aus der Statistik zu löschen.
            if dir == "LONG":
                if curr_low <= sl:
                    triggered, stopped_on_fill = True, True
                elif curr_low <= entry:
                    triggered = True
            elif dir == "SHORT":
                if curr_high >= sl:
                    triggered, stopped_on_fill = True, True
                elif curr_high >= entry:
                    triggered = True

            if triggered and stopped_on_fill:
                nominal_size = TRADE_MARGIN * LEVERAGE
                qty = nominal_size / entry
                raw_pnl = (sl - entry) * qty if dir == "LONG" else (entry - sl) * qty
                net_pnl = raw_pnl - nominal_size * TAKER_FEE * 2
                capital += net_pnl
                losses += 1
                if capital > max_capital:
                    max_capital = capital
                drawdown = (max_capital - capital) / max_capital * 100
                if drawdown > max_drawdown:
                    max_drawdown = drawdown
                orders_to_remove.append(order)
            elif triggered:
                active_trades.append({'direction': dir, 'entry': entry, 'sl': sl, 'tp': tp})
                orders_to_remove.append(order)

        for o in orders_to_remove:
            pending_orders.remove(o)

        # C) NEUE PIVOTS BESTÄTIGEN
        while raw_pivot_pointer < len(raw_pivots):
            p = raw_pivots[raw_pivot_pointer]
            if p[0] <= curr_idx - PIVOT_WINDOW:
                if not live_alt_pivots:
                    live_alt_pivots.append(p)
                else:
                    last_p = live_alt_pivots[-1]
                    if last_p[1] == p[1]:
                        if (p[1] == 1 and p[2] > last_p[2]) or (p[1] == -1 and p[2] < last_p[2]):
                            live_alt_pivots[-1] = p
                    else:
                        live_alt_pivots.append(p)
                raw_pivot_pointer += 1
            else:
                break

        # D) QM ERKENNUNG
        if len(live_alt_pivots) >= 4:
            p1, p2, p3, p4 = live_alt_pivots[-4], live_alt_pivots[-3], live_alt_pivots[-2], live_alt_pivots[-1]
            qm_id = p1[0]

            if qm_id not in processed_qm_ids:
                if p1[1] == 1 and p2[1] == -1 and p3[1] == 1 and p4[1] == -1:
                    H, L, HH, LL = p1[2], p2[2], p3[2], p4[2]
                    if HH > H and LL < L:
                        processed_qm_ids.add(qm_id)
                        entry = H
                        sl = HH * 1.003
                        tp = LL
                        if curr_price < entry:
                            pending_orders.append(
                                {'direction': 'SHORT', 'entry': entry, 'sl': sl, 'tp': tp, 'created_at': curr_idx}
                            )

                elif p1[1] == -1 and p2[1] == 1 and p3[1] == -1 and p4[1] == 1:
                    L, H, LL, HH = p1[2], p2[2], p3[2], p4[2]
                    if LL < L and HH > H:
                        processed_qm_ids.add(qm_id)
                        entry = L
                        sl = LL * 0.997
                        tp = HH
                        if curr_price > entry:
                            pending_orders.append(
                                {'direction': 'LONG', 'entry': entry, 'sl': sl, 'tp': tp, 'created_at': curr_idx}
                            )

    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
    net_pnl = capital - START_CAPITAL

    return {
        'symbol': symbol,
        'tf': tf,
        'trades': total_trades,
        'win_rate': win_rate,
        'pnl': net_pnl,
        'max_dd': max_drawdown,
    }


def main():
    coins = load_coins()
    if not coins:
        logger.error("No coins found in coins.json!")
        return

    print("=" * 85)
    print(f"🏛️ QM MASS-BACKTEST: {len(coins)} Coins | 1h, 4h, 1d | Letzte 2 Jahre")
    print(f"Margin: ${TRADE_MARGIN:,.0f} | Hebel: {LEVERAGE}x | P-Size: ${TRADE_MARGIN * LEVERAGE:,.0f}")
    print("=" * 85)

    # Datei initialisieren
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("=" * 85 + "\n")
        f.write(f"🏛️ QM MASS-BACKTEST ERGEBNISSE | Margin: ${TRADE_MARGIN:,.0f}\n")
        f.write("=" * 85 + "\n")
        f.write(f"{'Coin':<12} | {'TF':<5} | {'Trades':<8} | {'Win Rate':<10} | {'Max DD':<8} | {'Netto PnL':<12}\n")
        f.write("-" * 85 + "\n")

    results = []
    total_pnl = 0.0
    start_time = time.time()

    for idx, coin in enumerate(coins, 1):
        # Progress Info
        if idx % 10 == 0:
            print(f"⏳ Processing Coin {idx}/{len(coins)}: {coin}...")

        for tf in TIMEFRAMES:
            df = fetch_db_data(coin, tf)
            if df.empty or len(df) < 200:
                continue

            res = run_simulation(df, coin, tf)

            # Wir speichern nur, wenn der Coin überhaupt QM-Trades hatte
            if res['trades'] > 0:
                results.append(res)
                total_pnl += res['pnl']

                # In die TXT schreiben
                line = f"{res['symbol']:<12} | {res['tf']:<5} | {res['trades']:<8} | {res['win_rate']:>5.2f} %   | {res['max_dd']:>5.2f} % | ${res['pnl']:+,.2f}"
                with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")

                # Auch im Terminal ausgeben
                print(line)

        time.sleep(0.05)  # Kurze Pause für die Datenbank

    end_time = time.time()

    summary = "\n" + "=" * 85 + f"\n🏁 BACKTEST COMPLETE in {end_time - start_time:.2f} s\n"
    summary += f"💰 GESAMT PNL ÜBER ALLE TRADES: ${total_pnl:+,.2f}\n" + "=" * 85

    print(summary)
    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
        f.write(summary)


if __name__ == "__main__":
    main()
