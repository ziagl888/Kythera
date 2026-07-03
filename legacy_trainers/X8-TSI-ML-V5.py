import pandas as pd
import numpy as np
from xgboost import XGBClassifier
import joblib
import multiprocessing

# --- KONFIGURATION ----------------
FILE_NAME = "tsi_signals_short_only.csv" 
MODEL_FILENAME = "model_tsi_short_robust.pkl"
# ----------------------------------

def main():
    print("Lade Short-Daten...")
    df = pd.read_csv(FILE_NAME)
    df['entry_time'] = pd.to_datetime(df['entry_time'])
    df = df.sort_values('entry_time').reset_index(drop=True)

    # Standard Target (TP Hit)
    df["target"] = (df["outcome"] == "tp").astype(int)

    features = [
        "rsi_14", "rsi_6", "macd_hist", "atr_pct", 
        "vol_ratio", "bb_width", "bb_pos", 
        "dist_ema200", "dist_ema9_21", 
        "rsi_ratio", "slope_norm", 
        "dist_supp", "dist_res",
        "dist_kama9", "dist_kama21", "dist_kama55", "dist_kama9_21",
        "dist_donch_up", "dist_donch_low",
        "macd_cross_bearish",
        "ema9_21_cross_bearish",
        "kama9_21_cross_bearish",
        "bollinger_lower_break",
        "close_below_ema50",
        "obv_ratio",
        "close_to_vwap_pct",
        "obv_val",
        "volume_spike",
        "volume_trend_up"
    ]

    X_all = df[features].replace([np.inf, -np.inf], np.nan).fillna(0)
    y_all = df["target"]

    # Split
    split_idx = int(len(df) * 0.8)
    print(f"Split Date: {df.iloc[split_idx]['entry_time']}")

    X_train, X_test = X_all.iloc[:split_idx], X_all.iloc[split_idx:]
    y_train, y_test = y_all.iloc[:split_idx], y_all.iloc[split_idx:]
    df_test = df.iloc[split_idx:].copy()

    # Scale Pos Weight
    total_pos_train = y_train.sum()
    total_neg_train = len(y_train) - total_pos_train
    scale_pos_weight_value = total_neg_train / total_pos_train if total_pos_train > 0 else 1.0 
    print(f"Calculated scale_pos_weight: {scale_pos_weight_value:.2f}")

    print("\nTrainiere robustes XGBoost Modell (Short)...")
    # Gleiche robuste Parameter wie beim Long-Modell
    model = XGBClassifier(
        n_estimators=500,        
        learning_rate=0.03,      
        max_depth=6,             
        subsample=0.8,           
        colsample_bytree=0.8,    
        scale_pos_weight=scale_pos_weight_value,
        random_state=42,
        n_jobs=-1
    )

    model.fit(X_train, y_train)
    joblib.dump(model, MODEL_FILENAME)

    print("\nErstelle Vorhersagen...")
    df_test["ml_score"] = model.predict_proba(X_test)[:, 1]

    print(f"\n=== SHORT ONLY ROBUST PERFORMANCE (2.5% TP / 1.5% SL) ===") 
    print(f"{'Threshold':<10} | {'Trades':<8} | {'WinRate%':<8} | {'Avg PnL%':<10} | {'Total PnL ($)':<15} | {'PF':<12}")
    print("-" * 85)

    best_pf = -np.inf
    best_thresh = 0.5

    for thresh in np.arange(0.5, 0.95, 0.05):
        subset = df_test[df_test["ml_score"] >= thresh]
        if len(subset) < 5: continue 
        
        count = len(subset)
        win_rate = (subset["outcome"] == "tp").mean() * 100
        avg_pnl = subset["pnl_pct"].mean()
        total_pnl = subset["pnl_$"].sum()
        
        wins = subset[subset["pnl_$"] > 0]["pnl_$"].sum()
        losses = abs(subset[subset["pnl_$"] < 0]["pnl_$"].sum())
        pf = wins / losses if losses > 0 else 100.0

        print(f"{thresh:.2f}       | {count:<8} | {win_rate:<8.1f} | {avg_pnl:<10.2f} | {total_pnl:<15.2f} | {pf:<12.2f}")

        if pf > best_pf and count > 20: 
            best_pf = pf
            best_thresh = thresh

    print(f"\nEmpfohlener Threshold: {best_thresh:.2f} (PF: {best_pf:.2f})")
    
    best_trades = df_test[df_test["ml_score"] >= best_thresh]
    best_trades.to_csv("tsi_short_robust_trades.csv", index=False)

    print("\n--- Feature Importances (Short) ---")
    fi = pd.DataFrame({'feature': features, 'importance': model.feature_importances_})
    print(fi.sort_values('importance', ascending=False).head(15))

if __name__ == '__main__':
    multiprocessing.freeze_support() 
    main()
