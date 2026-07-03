# tsi_ml_relative_features.py – ML auf relativen Features

import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
import joblib

# 1. Daten laden
df = pd.read_csv("tsi_signals_with_relative_features.csv")

print(f"Geladene Signale: {len(df)}")
print("Spalten:", df.columns.tolist())

# 2. Target
df["profitable"] = (df["pnl_$"] > 0).astype(int)

# 3. Features (relativ + basis)
feature_cols = [
    "rsi_14",
    "volume_ratio",
    "close_to_ema200_pct",
    "close_to_kama_pct",
    "ema9_to_ema21_pct",
    "ema9_to_ema200_pct",
    "atr_pct",
    "macd_hist",
    "macd_positive"
]

# Nur vorhandene Spalten
feature_cols = [col for col in feature_cols if col in df.columns]
print("Verwendete Features:", feature_cols)

# NaN behandeln (Median imputieren)
df_features = df[feature_cols].copy()
df_features = df_features.fillna(df_features.median())

# 4. Train/Test Split
X_train, X_test, y_train, y_test = train_test_split(
    df_features, df["profitable"], test_size=0.2, random_state=42, stratify=df["profitable"]
)

# 5. Modell trainieren
model = RandomForestClassifier(
    n_estimators=1000,
    max_depth=15,
    min_samples_leaf=5,
    class_weight="balanced",
    random_state=42,
    n_jobs=-1
)
model.fit(X_train, y_train)

# 6. Evaluation
y_pred = model.predict(X_test)
y_prob = model.predict_proba(X_test)[:, 1]

print("\n=== Basis-Performance (alle Test-Trades) ===")
print(classification_report(y_test, y_pred))

# 7. Feature Importance
importances = model.feature_importances_
feat_importance = pd.DataFrame({
    "feature": feature_cols,
    "importance": importances
}).sort_values("importance", ascending=False)

print("\n=== Top Features (relativ) ===")
print(feat_importance.to_string(index=False))

# 8. Modell speichern
joblib.dump(model, "tsi_profit_predictor_relative.pkl")
print("\nModell gespeichert als 'tsi_profit_predictor_relative.pkl'")

# 9. Gefilterte Backtests mit verschiedenen Thresholds
df_test = df.iloc[X_test.index].copy().reset_index(drop=True)
df_test["prob_profit"] = y_prob

print("\n=== Gefilterte Performance ===")
for threshold in [0.5, 0.6, 0.65, 0.7, 0.75, 0.8]:
    filtered = df_test[df_test["prob_profit"] > threshold]
    if len(filtered) == 0:
        continue
    trades_reduction = (1 - len(filtered)/len(df_test)) * 100
    win_rate = filtered["profitable"].mean() * 100
    pnl_total = filtered["pnl_$"].sum()
    pnl_per_trade = filtered["pnl_$"].mean()
    print(f"Threshold >{threshold:.2f}:")
    print(f"  Trades: {len(filtered)} ({trades_reduction:.1f}% Reduktion)")
    print(f"  Win-Rate: {win_rate:.1f}%")
    print(f"  Gesamt PnL: {pnl_total:,.2f} $")
    print(f"  PnL pro Trade: {pnl_per_trade:,.2f} $")
    print("---")

# Optional: Beste Threshold speichern
best_threshold = 0.7  # anpassen nach Ergebnis
best_filtered = df_test[df_test["prob_profit"] > best_threshold]
best_filtered.to_csv(f"tsi_filtered_best_threshold_{best_threshold}.csv", index=False)
print(f"\nBeste gefilterte Trades in 'tsi_filtered_best_threshold_{best_threshold}.csv' gespeichert.")