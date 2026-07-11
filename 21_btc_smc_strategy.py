import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
import logging
import time

import pandas as pd

from core import config as _kcfg  # channel ids

# --- Eigene DB Connection importieren ---
from core.database import get_db_connection
from core.market_utils import calculate_pivots, check_cooldown, get_max_leverage, update_cooldown
from core.trade_utils import cap_leverage_to_sl

logging.basicConfig(level=logging.INFO, format='%(asctime)s - BTC_SNIPER - %(message)s')
logger = logging.getLogger(__name__)

# 🛠️ CONFIGURATION FÜR CORNIX & STRATEGIE
CHANNEL_ID = _kcfg.CH_BTC_SMC  # 🔴 HIER DEINEN NEUEN KANAL EINTRAGEN!
SYMBOL = 'BTCUSDT'
TIMEFRAME = '1h'

# Die Gewinner-Parameter aus dem Grid-Search Backtest
EMA_PERIOD = 21

# SL wird dynamisch berechnet: 1.0 × ATR(14) mit Floor von 0.4% und Cap von 1.2%.
# Damit bleibt die Strategie bei ruhigem Market eng (hohe R:R), passt sich aber
# bei Volatilität automatisch an (z.B. after CPI-Releases, Halving-Events).
SL_ATR_MULT = 1.0
SL_PCT_FLOOR = 0.004  # 0.4% — minimaler SL (historisch optimiertes Floor)
SL_PCT_CAP = 0.012  # 1.2% — Cap verhindert zu weite SLs in High-Vola-Phasen

# FIX P0.5 (Audit): 100x mit 0,4-1,2%-SL liquidierte bei ~-0,9% VOR dem SL —
# jeder Stop war -100% Margin. Jetzt 25x + zusätzlich cap_leverage_to_sl (R4).
DESIRED_LEVERAGE = 25  # wird gegen max_leverage.json gecapped

MIN_RR_RATIO = 1.25  # Minimum Risk-Reward
MAX_PIVOT_AGE = 120  # Keine Asbach-Uralt-Ziele
MAX_FVG_AGE = 48  # FVG muss innerhalb von 2 Tagen gefüllt werden

# Cooldown/Dedupe (P2.46): ohne diese Sperre feuert der Bot bei Gap-Filler-Lag
# dasselbe Setup eine 1h-Kerze später erneut. 12h ist der Fleet-Default für
# sub-daily Timeframes (P1.27-Muster, vgl. 16_smc_forex_metals COOLDOWN_HOURS.get(tf, 12))
# und liegt über der Kerzendauer (1h), damit das 1h-versetzte Doppelsignal sicher
# geblockt ist. Der Tag trägt kein Symbol — das liegt in der coin-Key-Spalte;
# "BTCSMC_1H" (9 Zeichen) passt in trade_cooldowns.module varchar(10) (T-024-Falle).
COOLDOWN_TAG = "BTCSMC_1H"
COOLDOWN_HOURS = 12


def calculate_atr(df, period=14):
    """Average True Range für dynamische SL-Berechnung."""
    high = df['high']
    low = df['low']
    close = df['close']
    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def calculate_dynamic_sl_pct(df, curr_close):
    """Liefert den SL-Abstand als Fraktion des Preises (z.B. 0.006 = 0.6%).
    Basis: ATR × SL_ATR_MULT, gecappt zwischen SL_PCT_FLOOR und SL_PCT_CAP.
    """
    atr_series = calculate_atr(df, 14)
    atr_val = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0
    if atr_val <= 0 or curr_close <= 0:
        return SL_PCT_FLOOR
    sl_pct = (atr_val * SL_ATR_MULT) / curr_close
    sl_pct = max(SL_PCT_FLOOR, min(SL_PCT_CAP, sl_pct))
    return sl_pct


# 📡 CORNIX SIGNAL GENERATOR
def send_cornix_signal(direction, entry, sl, tp, rr, lev):
    """Generiert ein sauberes Text-Signal, das Cornix zu 100% versteht.

    Returns True, wenn das Signal gepostet wurde, False, wenn der Cooldown es
    unterdrückt (P2.46) oder ein DB-Fehler auftrat. Der Cooldown-Upsert teilt sich
    die Transaktion des Outbox-Inserts (commit=False + ein einziges conn.commit),
    damit Signal und Dedupe-Marker atomar persistiert werden — ein Teil-Commit
    würde dasselbe Setup im nächsten Scan erneut posten lassen.
    """

    emoji = "🟢" if direction == "LONG" else "🔴"

    # Standard Cornix Parsing Format
    msg = f"""{emoji} <b>SMC Sniper Setup</b>
Symbol: {SYMBOL}
Direction: {direction}
Leverage: {lev}

Entry: {entry:.2f}
Take-Profit 1: {tp:.2f}
Stop-Loss: {sl:.2f}

<i>Risk/Reward: 1 : {rr:.2f} | Strategy: EMA21 + FVG Pivot Retest</i>"""

    try:
        with get_db_connection() as conn:
            # P2.46: bei Gap-Filler-Lag re-triggert dasselbe FVG-Setup den Bot eine
            # Kerze später. check_cooldown gibt True zurück, solange die Sperre aktiv
            # ist → dann nicht erneut posten.
            if check_cooldown(conn, COOLDOWN_TAG, SYMBOL, direction, COOLDOWN_HOURS):
                logger.info(f"⏳ Cooldown active für {SYMBOL} {direction}. Skip.")
                return False
            with conn.cursor() as cur:
                # Wir schicken es als reinen Text in die Outbox, HTML formatiert
                cur.execute("INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (CHANNEL_ID, msg))
            # Cooldown im selben Commit wie der Outbox-Insert setzen (Caller-Commit-Kontrakt).
            update_cooldown(conn, COOLDOWN_TAG, SYMBOL, direction, commit=False)
            conn.commit()
        logger.info(f"Cornix signal sent! {direction} @ {entry:.2f} (R:R {rr:.2f}, Lev {lev})")
        return True
    except Exception as e:
        logger.error(f"Error sending des Signals: {e}")
        return False


# 📊 DATA FETCHING (LOKALE DATENBANK)
def fetch_db_data():
    try:
        conn = get_db_connection()
        # FIX: Vorher ASC → lud Daten von 2020. Jetzt DESC LIMIT 500 +
        # Reverse, damit wir die NEUESTEN 500 Kerzen chronologisch sortiert bekommen.
        query = (
            f'SELECT open_time, open, high, low, close FROM "{SYMBOL}_{TIMEFRAME}" ORDER BY open_time DESC LIMIT 500'
        )
        df = pd.read_sql_query(query, conn)
        conn.close()

        if df.empty:
            return df

        # Chronologisch sortieren (älteste zuerst), damit alle Indizes und Pivots passen
        df = df.iloc[::-1].reset_index(drop=True)

        for c in ['open', 'high', 'low', 'close']:
            df[c] = df[c].astype(float)

        # Die laufende/aktuelle Kerze ignorieren, wir handeln nur harte Closes!
        if not df.empty:
            df = df.iloc[:-1].reset_index(drop=True)

        return df
    except Exception as e:
        logger.error(f"Error loading der DB-Daten: {e}")
        return pd.DataFrame()


# 🧠 SMC MATHEMATIK


def is_touching_pivot(price, pivots, max_idx, threshold=0.001):
    for p_idx, p_val in reversed(pivots):
        if p_idx > max_idx - 5:
            continue
        if p_idx < max_idx - MAX_PIVOT_AGE:
            break
        if abs(price - p_val) / p_val <= threshold:
            return True
    return False


# 🚀 CORE ENGINE
def analyze_market():
    logger.info("🔍 Analysing BTCUSDT 1h Chart auf Sniper-Setups...")
    df = fetch_db_data()

    if df.empty or len(df) < 200:
        return

    highs = df['high'].values
    lows = df['low'].values
    opens = df['open'].values
    closes = df['close'].values

    # EMA 21 für den Trend
    ema_values = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean().values

    supports, resistances = calculate_pivots(df, window=5)

    # Wir analysieren genau jetzt die ALLERLETZTE geschlossene Kerze
    curr_idx = len(df) - 1
    curr_low = lows[curr_idx]
    curr_high = highs[curr_idx]
    curr_price = closes[curr_idx]
    curr_ema = ema_values[curr_idx]

    # Wir suchen FVGs, die in den letzten 48 Kerzen entstanden sind
    search_start = max(2, curr_idx - MAX_FVG_AGE)

    # 🟢 1. LONG SETUPS PRÜFEN
    if curr_price > curr_ema:  # Trend Filter
        for i in range(search_start, curr_idx):
            # Ist es ein bullisches FVG?
            if highs[i - 2] < lows[i] and closes[i - 1] > opens[i - 1]:
                gap_bottom = highs[i - 2]
                candle_1_low = lows[i - 2]

                # Wurde es am Pivot started?
                if is_touching_pivot(candle_1_low, supports, i - 2):
                    # Wurde es VOR der aktuellen Kerze schon geschlossen?
                    was_closed_before = any(lows[j] <= gap_bottom for j in range(i + 1, curr_idx))

                    if not was_closed_before:
                        # Hat die AKTUELLE Kerze das FVG genau jetzt fully closed?
                        if curr_low <= gap_bottom:
                            # SETUP GEFUNDEN! Targets suchen...
                            valid_res = [
                                val
                                for p_idx, val in resistances
                                if curr_idx - MAX_PIVOT_AGE <= p_idx <= curr_idx - 5 and val > curr_price
                            ]

                            if valid_res:
                                target = min(valid_res)
                                sl_pct = calculate_dynamic_sl_pct(df, curr_price)
                                sl = curr_low * (1.0 - sl_pct)
                                risk = curr_price - sl
                                reward = target - curr_price
                                rr = reward / risk

                                if risk > 0 and rr >= MIN_RR_RATIO:
                                    lev = cap_leverage_to_sl(get_max_leverage(SYMBOL, DESIRED_LEVERAGE), curr_price, sl)
                                    if send_cornix_signal("LONG", curr_price, sl, target, rr, lev):
                                        logger.info(
                                            f"🎯 BINGO LONG! FVG fully closed bei {gap_bottom:.2f} | SL-Pct {sl_pct * 100:.2f}%"
                                        )
                                    return  # Verhindert, dass wir mehrere Setups im selben Durchlauf posten

    # 🔴 2. SHORT SETUPS PRÜFEN
    if curr_price < curr_ema:  # Trend Filter
        for i in range(search_start, curr_idx):
            # Ist es ein bärisches FVG?
            if lows[i - 2] > highs[i] and closes[i - 1] < opens[i - 1]:
                gap_top = lows[i - 2]
                candle_1_high = highs[i - 2]

                # Wurde es am Pivot started?
                if is_touching_pivot(candle_1_high, resistances, i - 2):
                    # Wurde es VOR der aktuellen Kerze schon geschlossen?
                    was_closed_before = any(highs[j] >= gap_top for j in range(i + 1, curr_idx))

                    if not was_closed_before:
                        # Hat die AKTUELLE Kerze das FVG genau jetzt fully closed?
                        if curr_high >= gap_top:
                            # SETUP GEFUNDEN! Targets suchen...
                            valid_sup = [
                                val
                                for p_idx, val in supports
                                if curr_idx - MAX_PIVOT_AGE <= p_idx <= curr_idx - 5 and val < curr_price
                            ]

                            if valid_sup:
                                target = max(valid_sup)
                                sl_pct = calculate_dynamic_sl_pct(df, curr_price)
                                sl = curr_high * (1.0 + sl_pct)
                                risk = sl - curr_price
                                reward = curr_price - target
                                rr = reward / risk

                                if risk > 0 and rr >= MIN_RR_RATIO:
                                    lev = cap_leverage_to_sl(get_max_leverage(SYMBOL, DESIRED_LEVERAGE), curr_price, sl)
                                    if send_cornix_signal("SHORT", curr_price, sl, target, rr, lev):
                                        logger.info(
                                            f"🎯 BINGO SHORT! FVG fully closed bei {gap_top:.2f} | SL-Pct {sl_pct * 100:.2f}%"
                                        )
                                    return


# ⏰ HAUPTSCHLEIFE
def main():
    logger.info("=== 🎯 BTC SNIPER BOT (CORNIX EDITION) GESTARTET ===")
    logger.info(
        f"Parameter: EMA {EMA_PERIOD} | SL dynamisch (ATR×{SL_ATR_MULT}, {SL_PCT_FLOOR * 100:.1f}%–{SL_PCT_CAP * 100:.1f}%) | Min R:R {MIN_RR_RATIO}"
    )

    while True:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)

            # Checking immer um Minute :01 (Wenn die 1h Kerze garantiert geschlossen und in der DB ist)
            if now.minute == 1:
                analyze_market()
                logger.info("🏁 Durchlauf stopped. Schlafe für 55 Minuten...")
                time.sleep(3300)  # Schlafe für 55 Minuten, um CPU zu sparen
            else:
                time.sleep(10)  # Kurzer Check jede 10 Sekunden

        except KeyboardInterrupt:
            logger.info("🛑 Bot wird stopped (STRG+C).")
            break
        except Exception as e:
            logger.error(f"Critical error im Main-Loop: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
