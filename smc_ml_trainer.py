import warnings

warnings.filterwarnings("ignore")

import logging
import os
from datetime import timedelta

import joblib
import numpy as np
import pandas as pd
import scipy.signal
import xgboost as xgb

from core.candles import read_candles_with_indicators
from core.database import get_db_connection
from core.market_utils import load_coins as _core_load_coins
from core.staging_guard import assert_no_foreign_overwrite
from core.time import utc_now

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

# New artefacts go exclusively to staging_models — NEVER in-place over
# a production pkl (P1.35 rule). Rollout is decided by the operator.
STAGING_DIR = os.getenv("KYTHERA_STAGING_DIR", r"C:\Users\Michael\Documents\_X\staging_models")

# FIX (P1.31): below this coin coverage training aborts hard instead of silently
# training on a truncated universe.
MIN_COIN_COVERAGE = 0.80

# FIX (P1.29): purge gap between chronological slices. TD patterns span
# up to 100 bars, BB setups up to 100 (60 breakout + 40 retest) — labels that
# run past the slice end must not leak into the next slice.
PURGE_GAP_BARS = 100

PRICE_BASED_INDICATORS = [
    'ema_9',
    'ema_21',
    'ema_50',
    'ema_200',
    'kama_21',
    'wma_21',
    'donchian_upper_20',
    'donchian_lower_20',
    'donchian_mid_20',
    'boll_upper_20',
    'boll_lower_20',
]
ABSOLUTE_INDICATORS = ['rsi_14', 'tsi_25_13_13', 'macd_dif_normal_12_26_9', 'macd_dea_normal_12_26_9']


# ==========================================
# 📊 DATA FETCHING & TRADE SIMULATION
# ==========================================
def load_coins():
    # P3.1: read/dict-unwrap/USDT-filter/symbol-validation via the canon.
    return _core_load_coins(COINS_FILE, usdt_only=True, uppercase=True)


def fetch_merged_data(symbol, tf):
    # FIX (P1.31): close connection via try/finally (previously every
    # query error leaked a pool connection) and log skips visibly instead of silently
    # returning an empty DataFrame.
    conn = None
    try:
        conn = get_db_connection()
        ind_cols = PRICE_BASED_INDICATORS + ABSOLUTE_INDICATORS + ['atr_14', 'trend_direction']
        # Via core.candles: CLOSED candles + indicator join, ASC
        # (include_forming=False). The 2-year window previously had no upper
        # bound and trained the forming candle too — the same R1 lookahead
        # class that the walk-forward sim lost in T-037.
        df = read_candles_with_indicators(
            conn,
            symbol,
            tf,
            start=utc_now() - timedelta(days=730),
            include_forming=False,
            candle_columns=('open_time', 'open', 'high', 'low', 'close'),
            indicator_columns=ind_cols,
        )

        if df.empty or len(df) < 500:
            return pd.DataFrame()
        df.ffill(inplace=True)
        # P3.6 (known limitation): bfill fills leading NaNs with the FIRST future
        # value — a backward look-ahead. Harmless only where it touches the warmup
        # head before any label window; documented, not changed (no logic edit).
        df.bfill(inplace=True)

        for c in df.columns:
            if c not in ['open_time', 'trend_direction']:
                df[c] = df[c].astype(float)
        return df.reset_index(drop=True)
    except Exception as e:
        logger.warning(f"[{tf}] {symbol} skipped: {e}")
        return pd.DataFrame()
    finally:
        if conn:
            conn.close()


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
            'atr_14_pct': (df['atr_14'].iloc[idx] / close_prev) * 100,
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
        if p3 - p1 > 100:
            continue

        if highs[p1] < highs[p2] < highs[p3]:
            if rsis[p1] > rsis[p2] > rsis[p3]:
                entry = closes[p3]
                sl = highs[p3] * 1.005
                dist = sl - entry
                if dist <= 0:
                    continue
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
                feats['entry_time'] = df['open_time'].iloc[p3]  # P1.29: for chronological split
                td_trades.append(feats)

    # --- 1b. THREE-DRIVE DIVERGENCE (BULLISH / LONG) --- NEW!
    for i in range(2, len(trough_idx)):
        p1, p2, p3 = trough_idx[i - 2], trough_idx[i - 1], trough_idx[i]
        if p3 - p1 > 100:
            continue

        if lows[p1] > lows[p2] > lows[p3]:
            if rsis[p1] < rsis[p2] < rsis[p3]:
                entry = closes[p3]
                sl = lows[p3] * 0.995
                dist = entry - sl
                if dist <= 0:
                    continue
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
                feats['entry_time'] = df['open_time'].iloc[p3]  # P1.29: for chronological split
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

                    feats = get_features(breakout_idx, 'LONG')  # feature extraction at breakout moment
                    feats['outcome'] = outcome
                    feats['entry'] = entry
                    feats['sl'] = sl
                    feats['tp'] = tp
                    feats['entry_time'] = df['open_time'].iloc[j]  # P1.29: retest candle = entry time
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

                    feats = get_features(breakdown_idx, 'SHORT')  # feature extraction at breakdown moment
                    feats['outcome'] = outcome
                    feats['entry'] = entry
                    feats['sl'] = sl
                    feats['tp'] = tp
                    feats['entry_time'] = df['open_time'].iloc[j]  # P1.29: retest candle = entry time
                    bb_trades.append(feats)
                    break

    return bb_trades, td_trades


# ==========================================
# 🧠 ML TRAINING & EVALUATION
# ==========================================
def chronological_three_way_split(trades_df, tf):
    """FIX (P1.29): chronological train/val/test split (70/15/15) with purge gap.

    Before: random train_test_split over temporally overlapping quasi-duplicates
    (contamination) + threshold selection on the test set (maximum statistic).
    """
    tf_hours = {'1h': 1, '4h': 4}.get(tf, 1)
    gap = pd.Timedelta(hours=PURGE_GAP_BARS * tf_hours)

    df = trades_df.copy()
    df['entry_time'] = pd.to_datetime(df['entry_time'], utc=True)
    df = df.sort_values('entry_time').reset_index(drop=True)

    t_train_end = df['entry_time'].quantile(0.70)
    t_val_end = df['entry_time'].quantile(0.85)

    train = df[df['entry_time'] <= t_train_end]
    val = df[(df['entry_time'] > t_train_end + gap) & (df['entry_time'] <= t_val_end)]
    test = df[df['entry_time'] > t_val_end + gap]
    return train, val, test


def _threshold_scan(trades, thresh):
    taken = trades[trades['prob'] >= thresh]
    if len(taken) == 0:
        return None
    wins = len(taken[taken['outcome'] == 1])
    losses = len(taken[taken['outcome'] == 0])
    win_rate = (wins / len(taken)) * 100
    # PnL = (Wins * 2R) - (Losses * 1R)
    net_r = (wins * 2.0) - losses
    pnl = net_r * (TRADE_MARGIN * 0.1)  # assumption: 1R = 10% margin loss
    return {'trades': len(taken), 'wr': win_rate, 'pnl': pnl, 'net_r': net_r}


def train_model(trades_df, pattern_name, tf):
    if trades_df.empty or len(trades_df) < 50:
        logger.warning(f"Insufficient data for {pattern_name} on {tf}.")
        return

    logger.info(f"🚀 Starting ML training for {pattern_name} ({tf}) with {len(trades_df)} trades...")

    feature_cols = [c for c in trades_df.columns if c not in ['outcome', 'entry', 'sl', 'tp', 'entry_time']]

    train_trades, val_trades, test_trades = chronological_three_way_split(trades_df, tf)
    logger.info(
        f"[{tf}] {pattern_name}: chronological split train={len(train_trades)} "
        f"val={len(val_trades)} test={len(test_trades)} (purge gap {PURGE_GAP_BARS} bars)"
    )
    if len(train_trades) < 100 or len(val_trades) < 30 or len(test_trades) < 30:
        logger.error(f"[{tf}] {pattern_name}: too few trades for a valid 3-way split — aborting.")
        return

    X_train = train_trades[feature_cols].fillna(0)
    y_train = train_trades['outcome'].astype(int)

    model = xgb.XGBClassifier(
        n_estimators=300,
        max_depth=5,
        learning_rate=0.03,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        eval_metric='logloss',
    )
    model.fit(X_train, y_train)

    # FIX (P1.29): select threshold on VALIDATION slice; test remains untouched.
    val_trades = val_trades.copy()
    val_trades['prob'] = model.predict_proba(val_trades[feature_cols].fillna(0))[:, 1]

    best_pnl = -float('inf')
    best_thresh = 0.0

    print(f"\n--- 💰 THRESHOLD OPTIMISATION (validation): {pattern_name} ({tf}) ---")
    thresholds = np.arange(0.30, 0.85, 0.05)
    for thresh in thresholds:
        stats = _threshold_scan(val_trades, thresh)
        if stats is None:
            continue
        print(
            f"Thresh: {thresh:.2f} | Trades: {stats['trades']:<4} | Win Rate: {stats['wr']:>5.1f}% "
            f"| Net R: {stats['net_r']:+.1f} | PnL: ${stats['pnl']:+,.0f}"
        )
        if stats['pnl'] > best_pnl:
            best_pnl = stats['pnl']
            best_thresh = thresh

    # Honest out-of-sample number: fixed threshold from validation, applied to test.
    test_trades = test_trades.copy()
    test_trades['prob'] = model.predict_proba(test_trades[feature_cols].fillna(0))[:, 1]
    test_stats = _threshold_scan(test_trades, best_thresh) or {'trades': 0, 'wr': 0.0, 'pnl': 0.0, 'net_r': 0.0}

    print(
        f"🎯 OPTIMAL: {best_thresh:.2f} (validation) | TEST: {test_stats['trades']} trades, "
        f"{test_stats['wr']:.1f}% WR, {test_stats['net_r']:+.1f} R\n"
    )

    # Save model — only to STAGING_DIR, never in-place over production pkls.
    prefix = "bb" if "Breaker" in pattern_name else "td"
    os.makedirs(STAGING_DIR, exist_ok=True)
    save_path = os.path.join(STAGING_DIR, f"{prefix}_xgboost_model_{tf}.pkl")
    # Same filename as tools/retrain_from_replay.py — this trainer on
    # 2026-07-14 overwrote four finished replay artefacts (T-2026-KYT-9050-006).
    assert_no_foreign_overwrite(save_path, 'smc_ml_trainer.py')
    joblib.dump(
        {
            'model': model,
            'features': feature_cols,
            'optimal_threshold': best_thresh,
            'meta': {
                'trainer': 'smc_ml_trainer.py',
                'xgboost_version': xgb.__version__,
                'split': 'chronological 70/15/15 + purge gap (P1.29)',
                'threshold_selected_on': 'validation',
                'test_stats': test_stats,
                'n_train': int(len(train_trades)),
                'n_val': int(len(val_trades)),
                'n_test': int(len(test_trades)),
            },
        },
        save_path,
    )
    logger.info(f"💾 Saved: {save_path}")


def main():
    coins = load_coins()
    if not coins:
        return

    for tf in TIMEFRAMES:
        all_bb = []
        all_td = []
        coins_with_data = 0
        skipped = []

        for idx, coin in enumerate(coins, 1):
            if idx % 50 == 0:
                logger.info(f"[{tf}] loading features: {idx}/{len(coins)}")
            df = fetch_merged_data(coin, tf)
            if df.empty:
                skipped.append(coin)
                continue

            coins_with_data += 1
            bb, td = simulate_and_extract_features(df, coin)
            all_bb.extend(bb)
            all_td.extend(td)

        # FIX (P1.31): hard abort instead of silently training on 0-8 coins.
        coverage = coins_with_data / len(coins) if coins else 0.0
        if skipped:
            logger.warning(
                f"[{tf}] {len(skipped)} coins with insufficient data: "
                f"{skipped[:20]}{'...' if len(skipped) > 20 else ''}"
            )
        if coverage < MIN_COIN_COVERAGE:
            raise SystemExit(
                f"[{tf}] ABORT: only {coins_with_data}/{len(coins)} coins ({coverage:.0%}) provided data "
                f"(minimum {MIN_COIN_COVERAGE:.0%})."
            )

        train_model(pd.DataFrame(all_bb), "Breaker Block", tf)
        train_model(pd.DataFrame(all_td), "Three-Drive", tf)


if __name__ == "__main__":
    main()
