import os

import joblib
import xgboost as xgb


def update_model(filename):
    if not os.path.exists(filename):
        print(f"❌ File not found: {filename}")
        return

    # FIX (#85): threshold files contain only a float, no ML model.
    # Previously joblib.load succeeded, but `model.save_model(...)` crashed with
    # "AttributeError: 'float' object has no attribute 'save_model'" — caught by
    # the except, but the error looked like a model problem. Now: explicitly
    # skip (threshold files are named "threshold_*.pkl").
    basename = os.path.basename(filename)
    if basename.startswith("threshold_"):
        print(f"⏭️  Skipping {filename} (threshold file, not an ML model)")
        return

    print(f"🔄 Processing {filename}...")

    try:
        # 1. Load the old model (via joblib/pickle)
        model = joblib.load(filename)

        # Defensively check whether it is actually a model with save_model method
        if not hasattr(model, "save_model"):
            print(f"⚠️  {filename} does not contain an XGBoost model ({type(model).__name__}), skipping.")
            return

        # 2. Save the model in the new, native XGBoost format
        # The native format (.json or .ubm) is more version-independent
        # FIX (P1.35): replace(".model", ...) was a no-op for *_model.pkl/.joblib —
        # save_model() then overwrote the original artifact in-place. Now splitext
        # + hard refuse if target == source or the target already exists.
        root, ext = os.path.splitext(filename)
        new_filename = f"{root}_v2.json"
        if os.path.abspath(new_filename) == os.path.abspath(filename):
            print(f"🛑 Refusing to overwrite source artifact in-place: {filename}")
            return None
        if os.path.exists(new_filename):
            print(f"🛑 Refusing to overwrite existing artifact: {new_filename}")
            return None
        model.save_model(new_filename)

        # 3. Load it back as a test to verify success
        test_model = xgb.XGBClassifier()
        test_model.load_model(new_filename)

        print(f"✅ Success! New model saved as: {new_filename}")
        return new_filename
    except Exception as e:
        print(f"🔥 Error updating {filename}: {e}")
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
