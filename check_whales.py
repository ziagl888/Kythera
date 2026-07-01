import os
import glob
import json
from collections import Counter

# Directory containing the files
DATA_DIR = "whale_data"


def check_latest_whale_data():
    # 1. Check if directory exists
    if not os.path.exists(DATA_DIR):
        print(f"Directory '{DATA_DIR}' does not exist.")
        return

    # 2. Find all JSON files in the directory
    search_pattern = os.path.join(DATA_DIR, "*.json")
    files = glob.glob(search_pattern)

    if not files:
        print(f"No JSON files found in '{DATA_DIR}'.")
        return

    # 3. Find the most recent file by modification date
    latest_file = max(files, key=os.path.getmtime)

    print(f"Reading data from the most recent file: {latest_file}...\n")

    # 4. Datei laden und auswerten
    try:
        with open(latest_file, "r", encoding="utf-8") as f:
            trades = json.load(f)

        if not trades:
            print("File is empty.")
            return

        # Zählt, wie oft jedes 'sym' (Symbol) in der Liste vorkommt
        coin_counts = Counter(trade["sym"] for trade in trades)

        # 5. Ausgabe formatieren
        print("=" * 40)
        print(f"🐳 WHALE TRADE STATISTICS (File: {os.path.basename(latest_file)})")
        print(f"Gesamtanzahl Trades (> 100k USD): {len(trades)}")
        print("=" * 40)

        # Sortiert after Häufigkeit absteigend ausgeben
        for coin, count in coin_counts.most_common():
            print(f"{coin:<15} : {count:>5} Trades")

        print("=" * 40)

    except Exception as e:
        print(f"Error reading der File: {e}")


if __name__ == "__main__":
    check_latest_whale_data()