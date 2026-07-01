import joblib
import xgboost as xgb
import os


def update_model(filename):
    if not os.path.exists(filename):
        print(f"❌ File not found: {filename}")
        return

    # FIX (#85): Threshold-Files enthalten nur einen float, kein ML-Modell.
    # Vorher lief joblib.load erfolgreich, aber `model.save_model(...)` crashte mit
    # "AttributeError: 'float' object has no attribute 'save_model'" — gefangen im
    # except, aber der Fehler sah aus wie ein Modell-Problem. Jetzt: explizit
    # skippen (Threshold-Files heißen "threshold_*.pkl").
    basename = os.path.basename(filename)
    if basename.startswith("threshold_"):
        print(f"⏭️  Skipping {filename} (threshold file, not an ML model)")
        return

    print(f"🔄 Processing {filename}...")

    try:
        # 1. Das alte Modell (via joblib/pickle) laden
        model = joblib.load(filename)

        # Defensively check whether it is actually a model with save_model method
        if not hasattr(model, "save_model"):
            print(f"⚠️  {filename} does not contain an XGBoost model ({type(model).__name__}), skipping.")
            return

        # 2. Das Modell im neuen, nativen XGBoost-Format speichern
        # The native format (.json or .ubm) is more version-independent
        new_filename = filename.replace(".model", "_v2.json")
        model.save_model(new_filename)

        # 3. Testweise wieder laden, um Erfolg zu prüfen
        test_model = xgb.XGBClassifier()
        test_model.load_model(new_filename)

        print(f"✅ Erfolg! Neues Modell gespeichert als: {new_filename}")
        return new_filename
    except Exception as e:
        print(f"🔥 Fehler beim Update von {filename}: {e}")
        return None


if __name__ == "__main__":
    update_model("trade_success_xgb_LONG_v1.model")
    update_model("trade_success_xgb_SHORT_v1.model")

    update_model("long_reversion_model.joblib")
    update_model("master_trade_model_xgboost_combined_signals.pkl")
    update_model("model_tsi_long_robust.pkl")
    update_model("model_tsi_short_robust.pkl")
    update_model("pump_dump_model.pkl")
    update_model("pump_model_8h_dump_final.pkl")
    update_model("pump_model_8h_pump_final.pkl")
    update_model("pump_model_24h_dump_final.pkl")
    update_model("pump_model_24h_pump_final.pkl")
    update_model("pump_model_72h_dump_final.pkl")
    update_model("pump_model_72h_pump_final.pkl")
    update_model("pump_model_168h_dump_final.pkl")
    update_model("pump_model_168h_pump_final.pkl")
    update_model("short_reversion_model.joblib")
    update_model("threshold_8h_dump_final.pkl")
    update_model("threshold_8h_pump_final.pkl")
    update_model("threshold_24h_dump_final.pkl")
    update_model("threshold_24h_pump_final.pkl")
    update_model("threshold_72h_dump_final.pkl")
    update_model("threshold_72h_pump_final.pkl")
    update_model("threshold_168h_dump_final.pkl")
    update_model("threshold_168h_pump_final.pkl")




