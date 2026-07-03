import pandas as pd
import numpy as np
import joblib # Zum Speichern/Laden des Modells
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, roc_auc_score, confusion_matrix
import xgboost as xgb
import matplotlib.pyplot as plt
import seaborn as sns

# ========================= KONFIGURATION =========================
DATA_FILE = 'ml_training_data.csv' 
MODEL_FILE = 'trend_prediction_model.joblib' 

def train_and_evaluate_model():
    print("--- Starte ML Modell Training mit erweiterten Indikatoren ---")

    # 1. Daten laden
    try:
        df = pd.read_csv(DATA_FILE)
        print(f"Daten erfolgreich aus {DATA_FILE} geladen. {len(df)} Zeilen gefunden.")
    except FileNotFoundError:
        print(f"Fehler: {DATA_FILE} nicht gefunden. Bitte zuerst das Daten-Sammel-Skript ausführen.")
        return

    # 2. Daten vorbereiten (Feature Engineering & Label Definition)
    # Konvertiere 'event_type' in numerische Werte (für XGBoost)
    df['event_type'] = df['event_type'].map({'UP': 1, 'DOWN': 0})
    
    # Definiere die Features (X) und die Zielvariable (y)
    # NEU: Die Feature-Liste wurde um die neuen Indikatoren erweitert
    features = [
        'event_type',               # 0 für DOWN, 1 für UP
        'vol_ratio',                # Volumen-Ratio zum Durchschnitt
        'rsi',                      # RSI(14)
        'atr_pct',                  # ATR als % des Preises
        'dist_ema200',              # Distanz zum EMA200
        'slope_trend',              # Steigung der Trendlinie
        'hour_of_day',              # Stunde des Tages

        # NEUE INDIKATOR-FEATURES
        'dist_close_ema9_pct',      # Abstand von Close zu EMA9
        'dist_ema9_ema21_pct',      # Abstand von EMA9 zu EMA21
        'dist_close_kama9_pct',     # Abstand von Close zu KAMA9
        'MACD_Line',                # MACD Linie
        'MACD_Signal',              # MACD Signal Linie
        'TSI_Line',                 # TSI Linie
        'TSI_Signal',               # TSI Signal Linie
        'dist_close_bb_lower_pct',  # Abstand zu unterem Bollinger Band
        'dist_close_bb_upper_pct',  # Abstand zu oberem Bollinger Band
        'bb_position_relative',     # Relative Position im Bollinger Band (0=unten, 1=oben)
        'dist_close_dc_lower_pct',  # Abstand zu unterem Donchian Channel
        'dist_close_dc_upper_pct',  # Abstand zu oberem Donchian Channel
        'dc_position_relative'      # Relative Position im Donchian Channel (0=unten, 1=oben)
    ]
    X = df[features]
    y = df['label_success'] 

    # Überprüfen auf NaN-Werte (falls Indikatoren am Anfang oder bei Lücken keine Werte hatten)
    initial_rows = len(X)
    combined_df = pd.concat([X, y], axis=1).dropna()
    X = combined_df[features]
    y = combined_df['label_success']
    
    if len(X) < initial_rows:
        print(f"Achtung: {initial_rows - len(X)} Zeilen mit NaN-Werten entfernt. Verbleibende Samples: {len(X)}")
        
    if len(X) == 0:
        print("Keine gültigen Daten nach NaN-Entfernung übrig. Training abgebrochen.")
        return

    print(f"Anzahl verwendeter Features: {len(features)}")
    print(f"Verwendete Features: {features}")
    print(f"Anzahl Samples nach Bereinigung: {len(X)}")

    # 3. Daten in Trainings- und Testsets aufteilen
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

    print(f"Trainingsset Größe: {len(X_train)} | Testset Größe: {len(X_test)}")
    print(f"Verteilung der Erfolg-Labels im Trainingsset:\n{y_train.value_counts(normalize=True)}")
    print(f"Verteilung der Erfolg-Labels im Testset:\n{y_test.value_counts(normalize=True)}")

    # 4. Modell trainieren (XGBoost Classifier)
    scale_pos_weight_value = len(y_train[y_train == 0]) / len(y_train[y_train == 1])
    
    model = xgb.XGBClassifier(
        objective='binary:logistic',
        eval_metric='logloss',
        # use_label_encoder=False, # Deprecated warning umgehen, in neueren Versionen nicht mehr nötig
        random_state=42,         
        n_estimators=500,        
        learning_rate=0.05,      
        max_depth=5,             
        subsample=0.7,           
        colsample_bytree=0.7,    
        scale_pos_weight=scale_pos_weight_value 
    )
    
    print("\nStarte Modelltraining...")
    model.fit(X_train, y_train)
    print("Modelltraining abgeschlossen.")

    # 5. Modell evaluieren
    print("\n--- Modell Evaluation auf dem Testset ---")
    y_pred = model.predict(X_test) 
    y_proba = model.predict_proba(X_test)[:, 1] 

    print("\nKlassifikationsbericht (Precision, Recall, F1-Score):")
    print(classification_report(y_test, y_pred))

    roc_auc = roc_auc_score(y_test, y_proba)
    print(f"ROC AUC Score (Maß für Trennschärfe des Modells): {roc_auc:.4f}")
    
    cm = confusion_matrix(y_test, y_pred)
    print("\nKonfusionsmatrix:")
    print(cm)
    
    print("\nInterpretation der Konfusionsmatrix:")
    print(f"  True Positives (TP): {cm[1,1]} -> Modell sagt Erfolg voraus, und es war ein Erfolg.")
    print(f"  False Positives (FP): {cm[0,1]} -> Modell sagt Erfolg voraus, aber es war ein Misserfolg (Fehlalarm).")
    print(f"  True Negatives (TN): {cm[0,0]} -> Modell sagt Misserfolg voraus, und es war ein Misserfolg.")
    print(f"  False Negatives (FN): {cm[1,0]} -> Modell sagt Misserfolg voraus, aber es war ein Erfolg (verpasste Chance).")

    # 6. Feature Importance anzeigen
    print("\n--- Feature Importance (Wichtigkeit der Indikatoren) ---")
    feature_importances = pd.Series(model.feature_importances_, index=X_train.columns)
    
    top_features = feature_importances.sort_values(ascending=False)
    print(top_features)

    plt.figure(figsize=(12, 9)) # Angepasste Größe für mehr Features
    sns.barplot(x=top_features.values, y=top_features.index, palette='viridis')
    plt.title('XGBoost Feature Importance (mit erweiterten Indikatoren)')
    plt.xlabel('Wichtigkeit')
    plt.ylabel('Features')
    plt.tight_layout()
    plt.show()

    # 7. Modell speichern
    joblib.dump(model, MODEL_FILE)
    print(f"\nModell erfolgreich gespeichert als '{MODEL_FILE}'")
    print("\n--- ML Modell Training Abgeschlossen ---")

if __name__ == "__main__":
    train_and_evaluate_model()
