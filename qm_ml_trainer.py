import warnings

warnings.filterwarnings("ignore")

import json
import logging
import os

import joblib
import numpy as np
import pandas as pd
import scipy.signal
import xgboost as xgb

# --- Eigene DB Connection importieren ---
from core.database import get_db_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - QM_ML_TRAINER - %(message)s')
logger = logging.getLogger(__name__)

# ==========================================
# 🛠️ CONFIGURATION
# ==========================================
COINS_FILE = "coins.json"

# Neue Artefakte gehen ausschließlich nach staging_models — NIE in-place über
# ein Produktions-pkl (P1.35-Regel). Rollout entscheidet der Operator.
STAGING_DIR = os.getenv("KYTHERA_STAGING_DIR", r"C:\Users\Michael\Documents\_X\staging_models")

# FIX (P1.31): unter dieser Coin-Abdeckung wird hart abgebrochen statt still
# auf einem trunkierten Universum zu trainieren.
MIN_COIN_COVERAGE = 0.80

# 💥 Wir trainieren beide Timeframes direkt hintereinander!
TIMEFRAMES_TO_TRAIN = ['1h', '4h']

TRADE_MARGIN = 5000.0
LEVERAGE = 20  # Standard-Evaluierung mit 20x Hebel
TAKER_FEE = 0.0004

PIVOT_WINDOW = 5
ORDER_EXPIRY = 50

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
# 📊 1. DATA FETCHING & TRADE SIMULATION
# ==========================================
def load_coins():
    try:
        with open(COINS_FILE) as f:
            data = json.load(f)
            coin_list = data.get('coins', data) if isinstance(data, dict) else data
            return [c.upper() for c in coin_list if c.upper().endswith("USDT")]
    except Exception as e:
        logger.error(f"Error loading von coins.json: {e}")
        return []


def fetch_merged_data(symbol, tf):
    # FIX (P1.31): Connection via try/finally schließen (vorher leakte jeder
    # Query-Fehler eine Pool-Connection) und Skips sichtbar loggen statt still
    # ein leeres DataFrame zurückzugeben.
    conn = None
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

        if df.empty:
            return pd.DataFrame()
        df.ffill(inplace=True)
        df.bfill(inplace=True)

        for c in df.columns:
            if c not in ['open_time', 'trend_direction']:
                df[c] = df[c].astype(float)
        return df.reset_index(drop=True)
    except Exception as e:
        logger.warning(f"[{tf}] {symbol} übersprungen: {e}")
        return pd.DataFrame()
    finally:
        if conn:
            conn.close()


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

            triggered, stopped_on_fill = False, False

            # FIX (P1.30): SL-Durchstich einer Pending-Order ist KEINE Invalidierung.
            # Da der SL jenseits des Entrys liegt, hat dieselbe Kerze zwingend auch
            # den Entry berührt → konservativ als fill-then-stop werten (Trade mit
            # outcome=0). Vorher wurden genau diese garantierten Verlierer aus dem
            # Datensatz gelöscht → Label-Verteilung nach oben verschoben.
            if order['direction'] == "LONG":
                if c_low <= order['sl']:
                    triggered, stopped_on_fill = True, True
                elif c_low <= order['entry']:
                    triggered = True
            else:
                if c_high >= order['sl']:
                    triggered, stopped_on_fill = True, True
                elif c_high >= order['entry']:
                    triggered = True

            if triggered:
                feature_idx = curr_idx - 1
                close_prev = df['close'].iloc[feature_idx]

                trade_data = {
                    'symbol': symbol,
                    'direction': order['direction'],
                    'entry': order['entry'],
                    'sl': order['sl'],
                    'tp': order['tp'],
                    # P1.30: Verlust sofort, wenn die Entry-Kerze auch den SL riss.
                    'outcome': 0 if stopped_on_fill else None,
                    # P1.29: Entry-Zeit für den chronologischen Split.
                    'entry_time': df['open_time'].iloc[curr_idx],
                    'atr_14_pct': (df['atr_14'].iloc[feature_idx] / close_prev) * 100,
                    'trend_direction': str(df['trend_direction'].iloc[feature_idx]),
                }

                for ind in ABSOLUTE_INDICATORS:
                    trade_data[ind] = df[ind].iloc[feature_idx]

                for ind in PRICE_BASED_INDICATORS:
                    trade_data[f"{ind}_dist_pct"] = ((df[ind].iloc[feature_idx] - close_prev) / close_prev) * 100

                order['trade_data'] = trade_data
                order['status'] = 'ACTIVE'
                order['entry_idx'] = curr_idx
                orders_to_remove.append(order)

        for o in orders_to_remove:
            pending_orders.remove(o)
            if o.get('status') == 'ACTIVE':
                completed_trades.append(o)

        for t in completed_trades:
            if t['trade_data']['outcome'] is not None:
                continue
            # FIX (P1.30): kein TP-Win auf der Entry-Kerze — ob TP oder SL zuerst
            # berührt wurde, ist intra-Kerze nicht feststellbar. SL-Hits auf der
            # Entry-Kerze sind oben bereits konservativ als Verlust gewertet;
            # TP-Bewertung beginnt erst mit der Folgekerze.
            if t.get('entry_idx') == curr_idx:
                continue
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
                                {'direction': 'SHORT', 'entry': H, 'sl': HH * 1.003, 'tp': LL, 'created_at': curr_idx}
                            )
                elif p1[1] == -1 and p2[1] == 1 and p3[1] == -1 and p4[1] == 1:
                    L, H, LL, HH = p1[2], p2[2], p3[2], p4[2]
                    if LL < L and HH > H:
                        processed_qm_ids.add(qm_id)
                        if c_price > L:
                            pending_orders.append(
                                {'direction': 'LONG', 'entry': L, 'sl': LL * 0.997, 'tp': HH, 'created_at': curr_idx}
                            )

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


def chronological_three_way_split(trades_df, tf):
    """FIX (P1.29): chronologischer Train/Val/Test-Split mit Purge-Gap.

    Vorher: random train_test_split über zeitlich überlappende Quasi-Duplikate
    (Kontamination) + Threshold-Wahl auf dem Test-Set (Maximum-Statistik).
    Jetzt: Split entlang der Entry-Zeit (70/15/15); zwischen den Slices wird
    eine Purge-Gap von ORDER_EXPIRY Bars freigelassen, weil ein Trade bis zu
    ORDER_EXPIRY Bars offen sein kann und sein Label sonst in den nächsten
    Slice hineinreicht.
    """
    tf_hours = {'1h': 1, '4h': 4}.get(tf, 1)
    gap = pd.Timedelta(hours=ORDER_EXPIRY * tf_hours)

    df = trades_df.copy()
    df['entry_time'] = pd.to_datetime(df['entry_time'], utc=True)
    df = df.sort_values('entry_time').reset_index(drop=True)

    t_train_end = df['entry_time'].quantile(0.70)
    t_val_end = df['entry_time'].quantile(0.85)

    train = df[df['entry_time'] <= t_train_end]
    val = df[(df['entry_time'] > t_train_end + gap) & (df['entry_time'] <= t_val_end)]
    test = df[df['entry_time'] > t_val_end + gap]
    return train, val, test


def train_and_optimize(trades_df, tf):
    logger.info(f"🚀 Starting ML Training für {tf} mit {len(trades_df)} completeden QM-Trades...")

    if 'trend_direction' in trades_df.columns:
        dummies = pd.get_dummies(trades_df['trend_direction'], prefix='trend')
        trades_df = pd.concat([trades_df, dummies], axis=1)
        trend_cols = list(dummies.columns)
    else:
        trend_cols = []

    feature_cols = (
        ABSOLUTE_INDICATORS + ['atr_14_pct'] + [f"{ind}_dist_pct" for ind in PRICE_BASED_INDICATORS] + trend_cols
    )

    trades_df['dir_num'] = (trades_df['direction'] == 'LONG').astype(int)
    feature_cols.append('dir_num')

    train_trades, val_trades, test_trades = chronological_three_way_split(trades_df, tf)
    logger.info(
        f"[{tf}] Chronologischer Split: train={len(train_trades)} val={len(val_trades)} test={len(test_trades)} "
        f"(Purge-Gap {ORDER_EXPIRY} Bars)"
    )
    if len(train_trades) < 100 or len(val_trades) < 30 or len(test_trades) < 30:
        logger.error(f"[{tf}] Zu wenig Trades für einen validen 3-Wege-Split — Abbruch.")
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

    importances = model.feature_importances_
    feat_imp = pd.DataFrame({'Feature': feature_cols, 'Importance': importances}).sort_values(
        by='Importance', ascending=False
    )
    print("\n" + "=" * 60)
    print(f"🏆 TOP INDIKATOREN FÜR QUASIMODO-ERFOLG ({tf} Chart)")
    print("=" * 60)
    for _idx, row in feat_imp.head(10).iterrows():
        print(f"🔹 {row['Feature']:<30}: {row['Importance']:.2%}")

    # FIX (P1.29): Threshold wird auf dem VALIDATION-Slice gewählt; das Test-Set
    # bleibt unangetastet und liefert danach die einzige ehrliche Zahl.
    print("\n" + "=" * 60)
    print(f"💰 THRESHOLD OPTIMIERUNG {tf} (Validation-Slice | Hebel {LEVERAGE}x)")
    print("=" * 60)

    val_trades = val_trades.copy()
    val_trades['prob'] = model.predict_proba(val_trades[feature_cols].fillna(0))[:, 1]

    best_pnl = -float('inf')
    best_thresh = 0.0

    thresholds = np.arange(0.30, 0.85, 0.05)
    for thresh in thresholds:
        taken_trades = val_trades[val_trades['prob'] >= thresh]
        if len(taken_trades) == 0:
            continue

        wins = len(taken_trades[taken_trades['outcome'] == 1])
        win_rate = (wins / len(taken_trades)) * 100
        pnl = sum(calculate_pnl(row, row['outcome'] == 1) for _, row in taken_trades.iterrows())

        print(
            f"Threshold >= {thresh:.2f} | Trades: {len(taken_trades):<5} | Win Rate: {win_rate:>5.1f}% | PnL: ${pnl:+,.2f}"
        )

        if pnl > best_pnl:
            best_pnl = pnl
            best_thresh = thresh

    # Ehrliche Out-of-Sample-Zahl: fixer Threshold aus Validation, angewandt auf Test.
    test_trades = test_trades.copy()
    test_trades['prob'] = model.predict_proba(test_trades[feature_cols].fillna(0))[:, 1]
    taken_test = test_trades[test_trades['prob'] >= best_thresh]
    if len(taken_test) > 0:
        test_wins = len(taken_test[taken_test['outcome'] == 1])
        test_wr = (test_wins / len(taken_test)) * 100
        test_pnl = sum(calculate_pnl(row, row['outcome'] == 1) for _, row in taken_test.iterrows())
        test_stats = {'trades': len(taken_test), 'wr': test_wr, 'pnl': test_pnl}
    else:
        test_stats = {'trades': 0, 'wr': 0.0, 'pnl': 0.0}

    print("=" * 60)
    print(f"🎯 OPTIMALER THRESHOLD ({tf}): {best_thresh:.2f} (gewählt auf Validation)")
    print(
        f"TEST (untouched, Threshold fix): Trades: {test_stats['trades']} | "
        f"Win Rate: {test_stats['wr']:.1f}% | PnL: ${test_stats['pnl']:+,.2f}"
    )
    print("=" * 60)

    # 💥 Saving das Modell dynamisch unter dem Namen des Timeframes!
    # Artefakte gehen nach STAGING_DIR — Produktions-pkls werden nie in-place
    # überschrieben, der Rollout ist eine bewusste Operator-Entscheidung.
    os.makedirs(STAGING_DIR, exist_ok=True)
    save_path = os.path.join(STAGING_DIR, f"qm_xgboost_model_{tf}.pkl")
    save_data = {
        'model': model,
        'features': feature_cols,
        'optimal_threshold': best_thresh,
        'meta': {
            'trainer': 'qm_ml_trainer.py',
            'xgboost_version': xgb.__version__,
            'split': 'chronological 70/15/15 + purge gap (P1.29)',
            'threshold_selected_on': 'validation',
            'test_stats': test_stats,
            'n_train': int(len(train_trades)),
            'n_val': int(len(val_trades)),
            'n_test': int(len(test_trades)),
        },
    }
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
        coins_with_data = 0
        skipped = []

        for idx, coin in enumerate(coins, 1):
            if idx % 50 == 0:
                logger.info(f"[{tf}] Processing Coin {idx}/{len(coins)}: {coin}...")

            df = fetch_merged_data(coin, tf)
            if len(df) < 200:
                skipped.append(coin)
                continue

            coins_with_data += 1
            trades = simulate_qm_trades(df, coin)
            all_trades.extend(trades)

        # FIX (P1.31): harter Abbruch statt still auf 0-8 Coins trainieren.
        coverage = coins_with_data / len(coins) if coins else 0.0
        if skipped:
            logger.warning(
                f"[{tf}] {len(skipped)} Coins ohne (ausreichende) Daten: {skipped[:20]}{'...' if len(skipped) > 20 else ''}"
            )
        if coverage < MIN_COIN_COVERAGE:
            raise SystemExit(
                f"[{tf}] ABBRUCH: nur {coins_with_data}/{len(coins)} Coins ({coverage:.0%}) lieferten Daten "
                f"(Minimum {MIN_COIN_COVERAGE:.0%})."
            )

        trades_df = pd.DataFrame(all_trades)

        if trades_df.empty:
            logger.warning(f"No QM trades in history for {tf} gefunden!")
            continue

        trades_df.dropna(inplace=True)
        train_and_optimize(trades_df, tf)

    logger.info("✅ TRAINING FOR ALL TIMEFRAMES COMPLETE!")


if __name__ == "__main__":
    main()
