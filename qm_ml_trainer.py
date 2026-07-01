import warnings

warnings.filterwarnings("ignore")

import time
import json
import logging
import pandas as pd
import numpy as np
import scipy.signal
import xgboost as xgb
from sklearn.model_selection import train_test_split
import joblib

# --- Eigene DB Connection importieren ---
from core.database import get_db_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - QM_ML_TRAINER - %(message)s')
logger = logging.getLogger(__name__)

# ==========================================
# 🛠️ CONFIGURATION
# ==========================================
COINS_FILE = "coins.json"

# 💥 Wir trainieren beide Timeframes direkt hintereinander!
TIMEFRAMES_TO_TRAIN = ['1h', '4h']

TRADE_MARGIN = 5000.0
LEVERAGE = 20  # Standard-Evaluierung mit 20x Hebel
TAKER_FEE = 0.0004

PIVOT_WINDOW = 5
ORDER_EXPIRY = 50

PRICE_BASED_INDICATORS = [
    'ema_9', 'ema_21', 'ema_50', 'ema_200',
    'kama_21', 'wma_21',
    'donchian_upper_20', 'donchian_lower_20', 'donchian_mid_20',
    'boll_upper_20', 'boll_lower_20'
]

ABSOLUTE_INDICATORS = [
    'rsi_14', 'tsi_25_13_13',
    'macd_dif_normal_12_26_9', 'macd_dea_normal_12_26_9'
]


# ==========================================
# 📊 1. DATA FETCHING & TRADE SIMULATION
# ==========================================
def load_coins():
    try:
        with open(COINS_FILE, 'r') as f:
            data = json.load(f)
            coin_list = data.get('coins', data) if isinstance(data, dict) else data
            return [c.upper() for c in coin_list if c.upper().endswith("USDT")]
    except Exception as e:
        logger.error(f"Error loading von coins.json: {e}")
        return []


def fetch_merged_data(symbol, tf):
    try:
        conn = get_db_connection()
        fields = ["t1.open_time", "t1.open", "t1.high", "t1.low", "t1.close"]
        for ind in PRICE_BASED_INDICATORS + ABSOLUTE_INDICATORS + ['atr_14', 'trend_direction']:
            fields.append(f"t2.{ind}")

        query = f"""
            SELECT {', '.join(fields)}
            FROM "{symbol}_{tf}" t1
            LEFT JOIN "{symbol}_{tf}_indicators" t2 ON t1.open_time = t2.open_time
            WHERE t1.open_time >= NOW() - INTERVAL '2 years'
            ORDER BY t1.open_time ASC
        """
        df = pd.read_sql_query(query, conn)
        conn.close()

        if df.empty: return pd.DataFrame()
        df.ffill(inplace=True)
        df.bfill(inplace=True)

        for c in df.columns:
            if c not in ['open_time', 'trend_direction']:
                df[c] = df[c].astype(float)
        return df.reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def simulate_qm_trades(df, symbol):
    highs, lows, closes = df['high'].values, df['low'].values, df['close'].values

    peak_idx = scipy.signal.argrelextrema(highs, np.greater, order=PIVOT_WINDOW)[0]
    trough_idx = scipy.signal.argrelextrema(lows, np.less, order=PIVOT_WINDOW)[0]

    raw_pivots = [(i, 1, highs[i]) for i in peak_idx] + [(i, -1, lows[i]) for i in trough_idx]
    raw_pivots.sort(key=lambda x: x[0])

    pending_orders = []
    completed_trades = []
    live_alt_pivots = []
    raw_pivot_pointer = 0
    processed_qm_ids = set()

    for curr_idx in range(PIVOT_WINDOW * 2, len(df)):
        c_high, c_low, c_price = highs[curr_idx], lows[curr_idx], closes[curr_idx]

        orders_to_remove = []
        for order in pending_orders:
            if curr_idx - order['created_at'] > ORDER_EXPIRY:
                orders_to_remove.append(order)
                continue

            triggered, invalidated = False, False

            if order['direction'] == "LONG":
                if c_low <= order['sl']:
                    invalidated = True
                elif c_low <= order['entry']:
                    triggered = True
            else:
                if c_high >= order['sl']:
                    invalidated = True
                elif c_high >= order['entry']:
                    triggered = True

            if invalidated:
                orders_to_remove.append(order)
            elif triggered:
                feature_idx = curr_idx - 1
                close_prev = df['close'].iloc[feature_idx]

                trade_data = {
                    'symbol': symbol,
                    'direction': order['direction'],
                    'entry': order['entry'],
                    'sl': order['sl'],
                    'tp': order['tp'],
                    'outcome': None,
                    'atr_14_pct': (df['atr_14'].iloc[feature_idx] / close_prev) * 100,
                    'trend_direction': str(df['trend_direction'].iloc[feature_idx])
                }

                for ind in ABSOLUTE_INDICATORS:
                    trade_data[ind] = df[ind].iloc[feature_idx]

                for ind in PRICE_BASED_INDICATORS:
                    trade_data[f"{ind}_dist_pct"] = ((df[ind].iloc[feature_idx] - close_prev) / close_prev) * 100

                order['trade_data'] = trade_data
                order['status'] = 'ACTIVE'
                orders_to_remove.append(order)

        for o in orders_to_remove:
            pending_orders.remove(o)
            if o.get('status') == 'ACTIVE':
                completed_trades.append(o)

        for t in completed_trades:
            if t['trade_data']['outcome'] is not None: continue
            d, sl, tp = t['trade_data']['direction'], t['trade_data']['sl'], t['trade_data']['tp']
            if d == "LONG":
                if c_low <= sl:
                    t['trade_data']['outcome'] = 0
                elif c_high >= tp:
                    t['trade_data']['outcome'] = 1
            else:
                if c_high >= sl:
                    t['trade_data']['outcome'] = 0
                elif c_low <= tp:
                    t['trade_data']['outcome'] = 1

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

        if len(live_alt_pivots) >= 4:
            p1, p2, p3, p4 = live_alt_pivots[-4], live_alt_pivots[-3], live_alt_pivots[-2], live_alt_pivots[-1]
            qm_id = p1[0]
            if qm_id not in processed_qm_ids:
                if p1[1] == 1 and p2[1] == -1 and p3[1] == 1 and p4[1] == -1:
                    H, L, HH, LL = p1[2], p2[2], p3[2], p4[2]
                    if HH > H and LL < L:
                        processed_qm_ids.add(qm_id)
                        if c_price < H:
                            pending_orders.append(
                                {'direction': 'SHORT', 'entry': H, 'sl': HH * 1.003, 'tp': LL, 'created_at': curr_idx})
                elif p1[1] == -1 and p2[1] == 1 and p3[1] == -1 and p4[1] == 1:
                    L, H, LL, HH = p1[2], p2[2], p3[2], p4[2]
                    if LL < L and HH > H:
                        processed_qm_ids.add(qm_id)
                        if c_price > L:
                            pending_orders.append(
                                {'direction': 'LONG', 'entry': L, 'sl': LL * 0.997, 'tp': HH, 'created_at': curr_idx})

    return [t['trade_data'] for t in completed_trades if t['trade_data']['outcome'] is not None]


# ==========================================
# 🧠 2. ML TRAINING & THRESHOLD OPTIMIZER
# ==========================================
def calculate_pnl(row, is_win):
    nominal = TRADE_MARGIN * LEVERAGE
    qty = nominal / row['entry']
    fee = nominal * TAKER_FEE * 2
    if is_win:
        if row['direction'] == 'LONG':
            raw_pnl = (row['tp'] - row['entry']) * qty
        else:
            raw_pnl = (row['entry'] - row['tp']) * qty
    else:
        if row['direction'] == 'LONG':
            raw_pnl = (row['sl'] - row['entry']) * qty
        else:
            raw_pnl = (row['entry'] - row['sl']) * qty
    return raw_pnl - fee


def train_and_optimize(trades_df, tf):
    logger.info(f"🚀 Starting ML Training für {tf} mit {len(trades_df)} completeden QM-Trades...")

    if 'trend_direction' in trades_df.columns:
        dummies = pd.get_dummies(trades_df['trend_direction'], prefix='trend')
        trades_df = pd.concat([trades_df, dummies], axis=1)
        trend_cols = list(dummies.columns)
    else:
        trend_cols = []

    feature_cols = ABSOLUTE_INDICATORS + ['atr_14_pct'] + [f"{ind}_dist_pct" for ind in
                                                           PRICE_BASED_INDICATORS] + trend_cols

    trades_df['dir_num'] = (trades_df['direction'] == 'LONG').astype(int)
    feature_cols.append('dir_num')

    X = trades_df[feature_cols].fillna(0)
    y = trades_df['outcome'].astype(int)

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    test_trades = trades_df.loc[X_test.index].copy()

    model = xgb.XGBClassifier(
        n_estimators=300,
        max_depth=5,
        learning_rate=0.03,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        eval_metric='logloss'
    )

    model.fit(X_train, y_train)

    importances = model.feature_importances_
    feat_imp = pd.DataFrame({'Feature': feature_cols, 'Importance': importances}).sort_values(by='Importance',
                                                                                              ascending=False)
    print("\n" + "=" * 60)
    print(f"🏆 TOP INDIKATOREN FÜR QUASIMODO-ERFOLG ({tf} Chart)")
    print("=" * 60)
    for idx, row in feat_imp.head(10).iterrows():
        print(f"🔹 {row['Feature']:<30}: {row['Importance']:.2%}")

    print("\n" + "=" * 60)
    print(f"💰 THRESHOLD OPTIMIERUNG {tf} (Out-of-Sample PnL | Hebel {LEVERAGE}x)")
    print("=" * 60)

    probs = model.predict_proba(X_test)[:, 1]
    test_trades['prob'] = probs

    best_pnl = -float('inf')
    best_thresh = 0.0
    best_stats = {}

    thresholds = np.arange(0.30, 0.85, 0.05)
    for thresh in thresholds:
        taken_trades = test_trades[test_trades['prob'] >= thresh]
        if len(taken_trades) == 0: continue

        wins = len(taken_trades[taken_trades['outcome'] == 1])
        win_rate = (wins / len(taken_trades)) * 100
        pnl = sum(calculate_pnl(row, row['outcome'] == 1) for _, row in taken_trades.iterrows())

        print(
            f"Threshold >= {thresh:.2f} | Trades: {len(taken_trades):<5} | Win Rate: {win_rate:>5.1f}% | PnL: ${pnl:+,.2f}")

        if pnl > best_pnl:
            best_pnl = pnl
            best_thresh = thresh
            best_stats = {'trades': len(taken_trades), 'wr': win_rate, 'pnl': pnl}

    print("=" * 60)
    print(f"🎯 OPTIMALER THRESHOLD ({tf}): {best_thresh:.2f}")
    if best_stats:
        print(f"Wir machen ${best_stats['pnl']:+,.2f} mit {best_stats['trades']} Trades (Out-of-Sample!)")
    print("=" * 60)

    # 💥 Saving das Modell dynamisch unter dem Namen des Timeframes!
    save_path = f"qm_xgboost_model_{tf}.pkl"
    save_data = {'model': model, 'features': feature_cols, 'optimal_threshold': best_thresh}
    joblib.dump(save_data, save_path)
    logger.info(f"💾 Model and features saved successfully to: {save_path}\n")


def main():
    coins = load_coins()
    if not coins:
        logger.error("No coins found!")
        return

    # Wir iterieren durch alle Timeframes, die wir in der Config definiert haben
    for tf in TIMEFRAMES_TO_TRAIN:
        logger.info(f"=== 🔄 STARTE VERARBEITUNG FÜR TIMEFRAME: {tf} ===")
        all_trades = []

        for idx, coin in enumerate(coins, 1):
            if idx % 50 == 0: logger.info(f"[{tf}] Processing Coin {idx}/{len(coins)}: {coin}...")

            df = fetch_merged_data(coin, tf)
            if len(df) < 200: continue

            trades = simulate_qm_trades(df, coin)
            all_trades.extend(trades)

        trades_df = pd.DataFrame(all_trades)

        if trades_df.empty:
            logger.warning(f"No QM trades in history for {tf} gefunden!")
            continue

        trades_df.dropna(inplace=True)
        train_and_optimize(trades_df, tf)

    logger.info("✅ TRAINING FOR ALL TIMEFRAMES COMPLETE!")


if __name__ == "__main__":
    main()