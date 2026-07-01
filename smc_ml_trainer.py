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

from core.database import get_db_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - SMC_ML_TRAINER - %(message)s')
logger = logging.getLogger(__name__)

# ==========================================
# 🛠️ CONFIGURATION
# ==========================================
COINS_FILE = "coins.json"
TIMEFRAMES = ['1h', '4h']
PIVOT_WINDOW = 10
RR_RATIO = 2.0
TRADE_MARGIN = 1000.0
LEVERAGE = 20
TAKER_FEE = 0.0004

PRICE_BASED_INDICATORS = [
    'ema_9', 'ema_21', 'ema_50', 'ema_200',
    'kama_21', 'wma_21',
    'donchian_upper_20', 'donchian_lower_20', 'donchian_mid_20',
    'boll_upper_20', 'boll_lower_20'
]
ABSOLUTE_INDICATORS = ['rsi_14', 'tsi_25_13_13', 'macd_dif_normal_12_26_9', 'macd_dea_normal_12_26_9']


# ==========================================
# 📊 DATA FETCHING & TRADE SIMULATION
# ==========================================
def load_coins():
    try:
        with open(COINS_FILE, 'r') as f:
            data = json.load(f)
            return [c.upper() for c in (data.get('coins', data) if isinstance(data, dict) else data) if
                    c.upper().endswith("USDT")]
    except:
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

        if df.empty or len(df) < 500: return pd.DataFrame()
        df.ffill(inplace=True)
        df.bfill(inplace=True)

        for c in df.columns:
            if c not in ['open_time', 'trend_direction']:
                df[c] = df[c].astype(float)
        return df.reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def simulate_and_extract_features(df, symbol):
    highs, lows, closes = df['high'].values, df['low'].values, df['close'].values
    rsis = df['rsi_14'].values

    peak_idx = scipy.signal.argrelextrema(highs, np.greater, order=PIVOT_WINDOW)[0]
    trough_idx = scipy.signal.argrelextrema(lows, np.less, order=PIVOT_WINDOW)[0]

    bb_trades = []
    td_trades = []

    def get_features(idx, direction):
        close_prev = closes[idx]
        features = {
            'dir_num': 1 if direction == 'LONG' else 0,
            'atr_14_pct': (df['atr_14'].iloc[idx] / close_prev) * 100
        }
        for ind in ABSOLUTE_INDICATORS:
            features[ind] = df[ind].iloc[idx]
        for ind in PRICE_BASED_INDICATORS:
            features[f"{ind}_dist_pct"] = ((df[ind].iloc[idx] - close_prev) / close_prev) * 100

        trend = str(df['trend_direction'].iloc[idx])
        features['trend_UP'] = 1 if trend == 'UP' else 0
        features['trend_DOWN'] = 1 if trend == 'DOWN' else 0
        features['trend_SIDEWAYS'] = 1 if trend == 'SIDEWAYS' else 0
        return features

    # --- 1a. THREE-DRIVE DIVERGENCE (BEARISH / SHORT) ---
    for i in range(2, len(peak_idx)):
        p1, p2, p3 = peak_idx[i - 2], peak_idx[i - 1], peak_idx[i]
        if p3 - p1 > 100: continue

        if highs[p1] < highs[p2] < highs[p3]:
            if rsis[p1] > rsis[p2] > rsis[p3]:
                entry = closes[p3]
                sl = highs[p3] * 1.005
                dist = sl - entry
                if dist <= 0: continue
                tp = entry - (dist * RR_RATIO)

                outcome = 0
                for j in range(p3 + 1, len(df)):
                    if highs[j] >= sl:
                        outcome = 0
                        break
                    elif lows[j] <= tp:
                        outcome = 1
                        break

                feats = get_features(p3, 'SHORT')
                feats['outcome'] = outcome
                feats['entry'] = entry
                feats['sl'] = sl
                feats['tp'] = tp
                td_trades.append(feats)

    # --- 1b. THREE-DRIVE DIVERGENCE (BULLISH / LONG) --- NEU!
    for i in range(2, len(trough_idx)):
        p1, p2, p3 = trough_idx[i - 2], trough_idx[i - 1], trough_idx[i]
        if p3 - p1 > 100: continue

        if lows[p1] > lows[p2] > lows[p3]:
            if rsis[p1] < rsis[p2] < rsis[p3]:
                entry = closes[p3]
                sl = lows[p3] * 0.995
                dist = entry - sl
                if dist <= 0: continue
                tp = entry + (dist * RR_RATIO)

                outcome = 0
                for j in range(p3 + 1, len(df)):
                    if lows[j] <= sl:
                        outcome = 0
                        break
                    elif highs[j] >= tp:
                        outcome = 1
                        break

                feats = get_features(p3, 'LONG')
                feats['outcome'] = outcome
                feats['entry'] = entry
                feats['sl'] = sl
                feats['tp'] = tp
                td_trades.append(feats)

    # --- 2. BREAKER BLOCK ---
    for p_idx in peak_idx:
        pivot_res = highs[p_idx]
        breakout_idx = -1
        for i in range(p_idx + PIVOT_WINDOW, min(p_idx + 60, len(df))):
            if closes[i] > pivot_res:
                breakout_idx = i
                break

        if breakout_idx != -1:
            for j in range(breakout_idx + 1, min(breakout_idx + 40, len(df))):
                if lows[j] <= pivot_res:
                    entry = pivot_res
                    sl = entry * 0.99
                    tp = entry * 1.02
                    outcome = 0
                    for k in range(j + 1, len(df)):
                        if lows[k] <= sl:
                            outcome = 0
                            break
                        elif highs[k] >= tp:
                            outcome = 1
                            break

                    feats = get_features(breakout_idx, 'LONG')  # Feature extraction at breakout moment
                    feats['outcome'] = outcome
                    feats['entry'] = entry
                    feats['sl'] = sl
                    feats['tp'] = tp
                    bb_trades.append(feats)
                    break

    for p_idx in trough_idx:
        pivot_sup = lows[p_idx]
        breakdown_idx = -1
        for i in range(p_idx + PIVOT_WINDOW, min(p_idx + 60, len(df))):
            if closes[i] < pivot_sup:
                breakdown_idx = i
                break

        if breakdown_idx != -1:
            for j in range(breakdown_idx + 1, min(breakdown_idx + 40, len(df))):
                if highs[j] >= pivot_sup:
                    entry = pivot_sup
                    sl = entry * 1.01
                    tp = entry * 0.98
                    outcome = 0
                    for k in range(j + 1, len(df)):
                        if highs[k] >= sl:
                            outcome = 0
                            break
                        elif lows[k] <= tp:
                            outcome = 1
                            break

                    feats = get_features(breakdown_idx, 'SHORT')  # Feature extraction at breakdown moment
                    feats['outcome'] = outcome
                    feats['entry'] = entry
                    feats['sl'] = sl
                    feats['tp'] = tp
                    bb_trades.append(feats)
                    break

    return bb_trades, td_trades


# ==========================================
# 🧠 ML TRAINING & EVALUATION
# ==========================================
def train_model(trades_df, pattern_name, tf):
    if trades_df.empty or len(trades_df) < 50:
        logger.warning(f"Insufficient data für {pattern_name} auf {tf}.")
        return

    logger.info(f"🚀 Starting ML Training für {pattern_name} ({tf}) mit {len(trades_df)} Trades...")

    feature_cols = [c for c in trades_df.columns if c not in ['outcome', 'entry', 'sl', 'tp']]
    X = trades_df[feature_cols].fillna(0)
    y = trades_df['outcome'].astype(int)

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    test_trades = trades_df.loc[X_test.index].copy()

    model = xgb.XGBClassifier(
        n_estimators=300, max_depth=5, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.8, random_state=42, eval_metric='logloss'
    )
    model.fit(X_train, y_train)

    probs = model.predict_proba(X_test)[:, 1]
    test_trades['prob'] = probs

    best_pnl = -float('inf')
    best_thresh = 0.0
    best_stats = {}

    print(f"\n--- 💰 THRESHOLD OPTIMIERUNG: {pattern_name} ({tf}) ---")
    thresholds = np.arange(0.30, 0.85, 0.05)
    for thresh in thresholds:
        taken = test_trades[test_trades['prob'] >= thresh]
        if len(taken) == 0: continue

        wins = len(taken[taken['outcome'] == 1])
        losses = len(taken[taken['outcome'] == 0])
        win_rate = (wins / len(taken)) * 100

        # PnL = (Wins * 2R) - (Losses * 1R)
        net_r = (wins * 2.0) - losses
        pnl = net_r * (TRADE_MARGIN * 0.1)  # Annahme: 1R = 10% Margin-Verlust

        print(
            f"Thresh: {thresh:.2f} | Trades: {len(taken):<4} | Win Rate: {win_rate:>5.1f}% | Net R: {net_r:+.1f} | PnL: ${pnl:+,.0f}")

        if pnl > best_pnl:
            best_pnl = pnl
            best_thresh = thresh
            best_stats = {'trades': len(taken), 'wr': win_rate, 'pnl': pnl, 'net_r': net_r}

    print(
        f"🎯 OPTIMAL: {best_thresh:.2f} -> {best_stats.get('net_r', 0)} R (Win Rate: {best_stats.get('wr', 0):.1f}%)\n")

    # Modell speichern
    prefix = "bb" if "Breaker" in pattern_name else "td"
    save_path = f"{prefix}_xgboost_model_{tf}.pkl"
    joblib.dump({'model': model, 'features': feature_cols, 'optimal_threshold': best_thresh}, save_path)
    logger.info(f"💾 Saved: {save_path}")


def main():
    coins = load_coins()
    if not coins: return

    for tf in TIMEFRAMES:
        all_bb = []
        all_td = []

        for idx, coin in enumerate(coins, 1):
            if idx % 50 == 0: logger.info(f"[{tf}] Loading Features: {idx}/{len(coins)}")
            df = fetch_merged_data(coin, tf)
            if df.empty: continue

            bb, td = simulate_and_extract_features(df, coin)
            all_bb.extend(bb)
            all_td.extend(td)

        train_model(pd.DataFrame(all_bb), "Breaker Block", tf)
        train_model(pd.DataFrame(all_td), "Three-Drive", tf)


if __name__ == "__main__":
    main()