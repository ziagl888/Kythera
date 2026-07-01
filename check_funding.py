import os
import glob
import json
from datetime import datetime

DATA_DIR = "funding_data"


def check_latest_funding_data():
    if not os.path.exists(DATA_DIR):
        print(f"Directory '{DATA_DIR}' does not exist.")
        return

    files = glob.glob(os.path.join(DATA_DIR, "*.json"))
    if not files:
        print("No JSON files found.")
        return

    latest_file = max(files, key=os.path.getmtime)
    print(f"Reading Daten aus: {latest_file}...\n")

    try:
        with open(latest_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not data:
            print("File is empty.")
            return

        print("=" * 40)
        print(f"💰 FUNDING RATES STATISTIK")
        print(f"Records in file: {len(data)}")

        # Zeige die 5 aktuellsten entries als Beispiel
        print("\nLetzte 5 erfasste Raten:")
        for d in data[-5:]:
            time_str = datetime.fromtimestamp(d['ts']).strftime('%H:%M:%S')
            print(f"[{time_str}] {d['sym']:<12} : {d['rate'] * 100:+.4f}%")

        print("=" * 40)

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    check_latest_funding_data()