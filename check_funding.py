import glob
import json
import os

from core.time import from_unix_ts

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
        with open(latest_file, encoding="utf-8") as f:
            data = json.load(f)

        if not data:
            print("File is empty.")
            return

        print("=" * 40)
        print("💰 FUNDING RATES STATISTIK")
        print(f"Records in file: {len(data)}")

        # Zeige die 5 aktuellsten entries als Beispiel
        print("\nLetzte 5 erfasste Raten:")
        for d in data[-5:]:
            # `ts` ist eine UTC-Epoche (20_funding_logger_bot) — vorher als
            # Serverlokalzeit gerendert, jetzt als UTC wie überall sonst.
            time_str = from_unix_ts(d['ts']).strftime('%H:%M:%S')
            print(f"[{time_str}] {d['sym']:<12} : {d['rate'] * 100:+.4f}%")

        print("=" * 40)

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    check_latest_funding_data()
