import pandas as pd
import numpy as np
from xgboost import XGBClassifier
import joblib

# ---------------- CONFIG ----------------
FILE_NAME = "tsi_signals_uniform.csv" # NEUE DATEI
MODEL_LONG = "model_tsi_long_uniform.pkl"
MODEL_SHORT = "model_tsi_short_uniform.pkl"

# --- OPTIMALE THRESHOLDS HIER FESTLEGEN ---
# Werden nach dem ersten Lauf basierend auf den Ergebnissen angepasst
OPTIMAL_THRESHOLD_LONG = 0.75 
OPTIMAL_THRESHOLD_SHORT = 0.70 # Placeholder, wird neu gewählt
# ----------------------------------------

print("Lade Daten...")
df = pd.read_csv(FILE_NAME)
df['entry_time'] = pd.to_datetime(df['entry_time'])
df = df.sort_values('entry_time').reset_index(drop=True)

df["target"] = (df["outcome"] == "tp").astype(int)

features = [
    "rsi_14", "rsi_6", "macd_hist", "atr_pct", 
    "vol_ratio", "bb_width", "bb_pos", 
    "dist_ema200", "dist_ema9_21", 
    "rsi_ratio", "slope_norm", 
    "dist_supp", "dist_res",
    "dist_kama9", "dist_kama21", "dist_kama55", "dist_kama9_21",
    "dist_donch_up", "dist_donch_low",
    # NEUE FEATURES
    "macd_cross_bearish",
    "ema9_21_cross_bearish",
    "kama9_21_cross_bearish",
    "bollinger_lower_break",
    "close_below_ema50"
]

X_all = df[features].replace([np.inf, -np.inf], np.nan).fillna(0)
y_all = df["target"]

# ---------------- SPLIT DATEN LONG / SHORT ----------------
mask_long = df["direction"] == "long"
mask_short = df["direction"] == "short"

split_idx = int(len(df) * 0.8)
test_start_time = df.iloc[split_idx]["entry_time"]

print(f"Split Date: {test_start_time}")

df_train_long = df[(mask_long) & (df.index < split_idx)]
df_train_short = df[(mask_short) & (df.index < split_idx)]

df_test = df[df.index >= split_idx].copy()

print(f"Train Long: {len(df_train_long)}, Train Short: {len(df_train_short)}")
print(f"Test Total: {len(df_test)}")

# ---------------- BERECHNUNG separate scale_pos_weight ----------------
total_pos_long = df_train_long["target"].sum()
total_neg_long = len(df_train_long) - total_pos_long
scale_pos_weight_long = total_neg_long / total_pos_long if total_pos_long > 0 else 1.0 
print(f"Calculated scale_pos_weight for LONG: {scale_pos_weight_long:.2f}")

total_pos_short = df_train_short["target"].sum()
total_neg_short = len(df_train_short) - total_pos_short
scale_pos_weight_short = total_neg_short / total_pos_short if total_pos_short > 0 else 1.0
print(f"Calculated scale_pos_weight for SHORT: {scale_pos_weight_short:.2f}")

# ---------------- TRAINING LONG MODELL ----------------
print("\nTrainiere LONG Modell (uniform targets, scaled weights)...")
model_long = XGBClassifier(
    n_estimators=300, learning_rate=0.03, max_depth=5, 
    subsample=0.8, colsample_bytree=0.8, 
    scale_pos_weight=scale_pos_weight_long,
    random_state=42, n_jobs=-1
)
model_long.fit(df_train_long[features], df_train_long["target"])
joblib.dump(model_long, MODEL_LONG)

# ---------------- TRAINING SHORT MODELL ----------------
print("Trainiere SHORT Modell (uniform targets, scaled weights)...")
model_short = XGBClassifier(
    n_estimators=300, learning_rate=0.03, max_depth=5, 
    subsample=0.8, colsample_bytree=0.8, 
    scale_pos_weight=scale_pos_weight_short,
    random_state=42, n_jobs=-1
)
model_short.fit(df_train_short[features], df_train_short["target"])
joblib.dump(model_short, MODEL_SHORT)

# ---------------- PREDICTION AUF TEST SET ----------------
print("\nErstelle Vorhersagen...")
df_test["ml_score_long"] = model_long.predict_proba(df_test[features])[:, 1]
df_test["ml_score_short"] = model_short.predict_proba(df_test[features])[:, 1]

# --- Funktion für Backtest-Reporting ---
def print_backtest_report(data_subset, title, is_combined=False, tp_label="TP", sl_label="SL"):
    print(f"\n=== {title} ({tp_label} / {sl_label}) ===") # Angepasster Titel
    print(f"{'Threshold':<10} | {'Trades':<8} | {'WinRate%':<8} | {'Avg PnL%':<10} | {'Total PnL ($)':<15} | {'PF':<12}")
    print("-" * 85)

    for thresh in np.arange(0.5, 0.85, 0.05):
        if is_combined:
            subset = data_subset[( (data_subset["direction"] == "long") & (data_subset["ml_score_long"] >= thresh) ) |
                                 ( (data_subset["direction"] == "short") & (data_subset["ml_score_short"] >= thresh) )]
        elif "ml_score_long" in data_subset.columns: 
            subset = data_subset[data_subset["ml_score_long"] >= thresh]
        elif "ml_score_short" in data_subset.columns: 
            subset = data_subset[data_subset["ml_score_short"] >= thresh]
        else: 
            subset = pd.DataFrame()

        if len(subset) < 5: continue 
        
        count = len(subset)
        win_rate = (subset["outcome"] == "tp").mean() * 100
        avg_pnl = subset["pnl_pct"].mean()
        total_pnl = subset["pnl_$"].sum()
        
        wins = subset[subset["pnl_$"] > 0]["pnl_$"].sum()
        losses = abs(subset[subset["pnl_$"] < 0]["pnl_$"].sum())
        pf = wins / losses if losses > 0 else 100.0

        print(f"{thresh:.2f}       | {count:<8} | {win_rate:<8.1f} | {avg_pnl:<10.2f} | {total_pnl:<15.2f} | {pf:<12.2f}")


# ---------------- VISUALISIERUNG DER PERFORMANCE ----------------
print_backtest_report(df_test, "DUAL MODEL BACKTEST (General Combined Report)", is_combined=True, tp_label="2.5%", sl_label="1.5%") 

print_backtest_report(df_test[df_test["direction"] == "long"], "LONG TRADES PERFORMANCE", is_combined=False, tp_label="2.5%", sl_label="1.5%")
print_backtest_report(df_test[df_test["direction"] == "short"], "SHORT TRADES PERFORMANCE", is_combined=False, tp_label="2.5%", sl_label="1.5%")


# ---------------- FINALE, OPTIMIERTE KOMBINIERTE STRATEGIE ----------------
print(f"\n\n=== OPTIMIZED COMBINED STRATEGY (Long Thresh: {OPTIMAL_THRESHOLD_LONG:.2f}, Short Thresh: {OPTIMAL_THRESHOLD_SHORT:.2f}) ===")

filtered_long_trades = df_test[(df_test["direction"] == "long") & (df_test["ml_score_long"] >= OPTIMAL_THRESHOLD_LONG)]
filtered_short_trades = df_test[(df_test["direction"] == "short") & (df_test["ml_score_short"] >= OPTIMAL_THRESHOLD_SHORT)]

optimized_trades = pd.concat([filtered_long_trades, filtered_short_trades]).sort_values("entry_time")

if len(optimized_trades) > 0:
    total_trades = len(optimized_trades)
    win_rate = (optimized_trades["outcome"] == "tp").mean() * 100
    avg_pnl = optimized_trades["pnl_pct"].mean()
    total_pnl = optimized_trades["pnl_$"].sum()
    
    wins = optimized_trades[optimized_trades["pnl_$"] > 0]["pnl_$"].sum()
    losses = abs(optimized_trades[optimized_trades["pnl_$"] < 0]["pnl_$"].sum())
    pf = wins / losses if losses > 0 else 100.0

    print(f"Total Trades: {total_trades}")
    print(f"WinRate: {win_rate:.1f}%")
    print(f"Avg PnL%: {avg_pnl:.2f}")
    print(f"Total PnL ($): {total_pnl:.2f}")
    print(f"Profit Factor: {pf:.2f}")
    
    optimized_trades.to_csv("tsi_optimized_filtered_trades_uniform.csv", index=False)
    print("\nOptimierte Trades gespeichert in 'tsi_optimized_filtered_trades_uniform.csv'")
else:
    print("Keine Trades bei den gewählten optimalen Thresholds gefunden.")

print("\n--- Feature Importances (Long Model) ---")
fi_long = pd.DataFrame({'feature': features, 'importance': model_long.feature_importances_})
print(fi_long.sort_values('importance', ascending=False).head(10))

print("\n--- Feature Importances (Short Model) ---")
fi_short = pd.DataFrame({'feature': features, 'importance': model_short.feature_importances_})
print(fi_short.sort_values('importance', ascending=False).head(10))
