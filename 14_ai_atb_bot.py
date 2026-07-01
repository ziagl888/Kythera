import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")
warnings.filterwarnings("ignore", category=UserWarning, module="pandas_ta")

import matplotlib

matplotlib.use('Agg')
import datetime
import json
import logging
import os
import time
import uuid

import joblib
import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import pandas_ta as ta
import scipy.signal
import scipy.stats as stats

from core import config as _kcfg  # channel ids
from core.charting import generate_minichart_image
from core.database import get_db_connection
from core.market_utils import check_cooldown, get_max_leverage, update_cooldown
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels

logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_ATB_BOT - %(message)s')
logger = logging.getLogger(__name__)

# 🛠️ CONFIGURATION & FILTER-TUNING
MODEL_ID = 'ATB1'
TARGET_CHANNEL_ID = _kcfg.CH_ATB_TARGET  # Dein AI / Cornix Channel
TRENDBREAKER_CHANNEL_ID = _kcfg.CH_ATB_INFO  # Info-Channel

# --- FILTER ---
TREND_MIN_R_VALUE = 0.2  # 0.0 = Akzeptiert alle Trends (altes Verhalten)
MAX_DISTANCE_PCT = 0.05  # 12% Toleranz-Radar

TL_MODEL_LONG_PATH = 'long_trend_prediction_model.joblib'
TL_MODEL_SHORT_PATH = 'short_trend_prediction_model.joblib'
TL_THRESH_LONG = 0.80
TL_THRESH_SHORT = 0.75

MODELS = {'LONG': None, 'SHORT': None}
TRENDLINE_STATE = {}

# FIX: Persistenz für TRENDLINE_STATE. Vorher war der State nur in-memory →
# after jedem Restart war prev_relation="unknown" für ALLE Coins → der
# Break-Check `prev in ["below","near","unknown"] and curr == "above"` feuerte
# sofort für jeden Coin, der aktuell über seiner Trendlinie liegt (= massenhaft).
TRENDLINE_STATE_FILE = "trendline_state.json"


def load_trendline_state():
    global TRENDLINE_STATE
    if not os.path.exists(TRENDLINE_STATE_FILE):
        TRENDLINE_STATE = {}
        logger.info("📂 No trendline_state.json found → starting fresh.")
        return
    try:
        with open(TRENDLINE_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        # last_alert wieder in datetime umwandeln
        TRENDLINE_STATE = {}
        for sym, info in data.items():
            info_copy = dict(info)
            if "last_alert" in info_copy and isinstance(info_copy["last_alert"], str):
                info_copy["last_alert"] = datetime.datetime.fromisoformat(info_copy["last_alert"])
            TRENDLINE_STATE[sym] = info_copy
        logger.info(f"✅ {len(TRENDLINE_STATE)} trendline states loaded.")
    except Exception as e:
        logger.error(f"Error loading von {TRENDLINE_STATE_FILE}: {e}")
        TRENDLINE_STATE = {}


def save_trendline_state():
    try:
        serializable = {}
        for sym, info in TRENDLINE_STATE.items():
            info_copy = dict(info)
            if "last_alert" in info_copy and isinstance(info_copy["last_alert"], datetime.datetime):
                info_copy["last_alert"] = info_copy["last_alert"].isoformat()
            serializable[sym] = info_copy
        tmp = TRENDLINE_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, TRENDLINE_STATE_FILE)
    except Exception as e:
        logger.error(f"Error saving von {TRENDLINE_STATE_FILE}: {e}")


CHART_DIR = "generated_charts"
os.makedirs(CHART_DIR, exist_ok=True)


def load_models_and_coins():
    try:
        if os.path.exists(TL_MODEL_LONG_PATH):
            MODELS['LONG'] = joblib.load(TL_MODEL_LONG_PATH)
        if os.path.exists(TL_MODEL_SHORT_PATH):
            MODELS['SHORT'] = joblib.load(TL_MODEL_SHORT_PATH)
        logger.info("✅ ML Modelle für Trendline Break (ATB1) loaded successfully.")
    except Exception as e:
        logger.error(f"❌ Error loading der ATB1 Modelle: {e}")

    try:
        with open('coins.json') as f:
            data = json.load(f)
            return data.get('coins', data) if isinstance(data, dict) else data
    except Exception:
        return []


# 🧠 BERECHNUNGS-LOGIKEN (NEU: Index-Basiert!)
def detect_trend(df):
    if len(df) < 50:
        return 'UNDECIDED', None

    highs, lows = df['high'].values, df['low'].values
    high_pivots = scipy.signal.find_peaks(highs, distance=8)[0]
    low_pivots = scipy.signal.find_peaks(-lows, distance=8)[0]

    def calc_line(pivots, is_high):
        if len(pivots) < 2:
            return None, None

        # 💥 DER FIX: Wir nutzen den einfachen Kerzen-Index statt riesiger Timestamps!
        x = pivots
        y = highs[pivots] if is_high else lows[pivots]

        slope, intercept, r_value, _, _ = stats.linregress(x, y)
        if abs(r_value) < TREND_MIN_R_VALUE:
            return None, None
        return float(slope), float(intercept)

    down_slope, down_intercept = calc_line(high_pivots, True)
    up_slope, up_intercept = calc_line(low_pivots, False)

    if down_slope is not None and down_slope < 0:
        return 'DOWN', (down_slope, down_intercept)
    elif up_slope is not None and up_slope > 0:
        return 'UP', (up_slope, up_intercept)
    return 'UNDECIDED', None


def find_pivots(df, distance=8):
    high_peaks, _ = scipy.signal.find_peaks(df['high'], distance=distance)
    low_peaks, _ = scipy.signal.find_peaks(-df['low'], distance=distance)
    return high_peaks, low_peaks


def get_ml_prediction(df_raw, event_type_str, slope, current_close_price):
    is_long = "UP" in event_type_str
    model_to_use = MODELS['LONG'] if is_long else MODELS['SHORT']
    current_ml_threshold = TL_THRESH_LONG if is_long else TL_THRESH_SHORT

    if model_to_use is None:
        return 0.0, current_ml_threshold

    try:
        df = df_raw.copy()
        df.columns = df.columns.str.lower()
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df.dropna(subset=['open', 'high', 'low', 'close', 'volume'], inplace=True)
        if df.empty:
            return 0.0, current_ml_threshold

        df['vol_avg_20'] = df['volume'].rolling(window=20).mean()
        df['rsi'] = ta.rsi(df['close'], length=14)
        df['ema_9'] = ta.ema(df['close'], length=9)
        df['ema_21'] = ta.ema(df['close'], length=21)
        df['ema_50'] = ta.ema(df['close'], length=50)
        df['ema_200'] = ta.ema(df['close'], length=200)
        df['dist_close_ema9_pct'] = (df['close'] - df['ema_9']) / df['ema_9']
        df['dist_ema9_ema21_pct'] = (df['ema_9'] - df['ema_21']) / df['ema_21']
        df['kama_9'] = ta.kama(df['close'], length=9)
        df['dist_close_kama9_pct'] = (df['close'] - df['kama_9']) / df['kama_9']

        macd = ta.macd(df['close'], fast=9, slow=21, signal=9)
        df['MACD_Line'] = macd['MACD_9_21_9'] if macd is not None else 0
        df['MACD_Signal'] = macd['MACDs_9_21_9'] if macd is not None else 0

        tsi = ta.tsi(df['close'], fast=12, slow=7, signal=7)
        df['TSI_Line'] = tsi['TSI_7_12_7'] if tsi is not None else 0
        df['TSI_Signal'] = tsi['TSIs_7_12_7'] if tsi is not None else 0

        bbands = ta.bbands(df['close'], length=20, std=2.0)
        if bbands is not None:
            bb_lower_col = next((col for col in bbands.columns if col.startswith('BBL_')), None)
            bb_upper_col = next((col for col in bbands.columns if col.startswith('BBU_')), None)
            df['BB_Lower'] = bbands[bb_lower_col]
            df['BB_Upper'] = bbands[bb_upper_col]
            df['dist_close_bb_lower_pct'] = (df['close'] - df['BB_Lower']) / df['close']
            df['dist_close_bb_upper_pct'] = (df['close'] - df['BB_Upper']) / df['close']
            diff_bb = df['BB_Upper'] - df['BB_Lower']
            df['bb_position_relative'] = np.where(diff_bb != 0, (df['close'] - df['BB_Lower']) / diff_bb, 0)
        else:
            return 0.0, current_ml_threshold

        donchian = ta.donchian(df['high'], df['low'], length=20)
        if donchian is not None:
            dc_lower_col = next((col for col in donchian.columns if col.startswith('DCL_')), None)
            dc_upper_col = next((col for col in donchian.columns if col.startswith('DCU_')), None)
            df['DC_Lower'] = donchian[dc_lower_col]
            df['DC_Upper'] = donchian[dc_upper_col]
            df['dist_close_dc_lower_pct'] = (df['close'] - df['DC_Lower']) / df['close']
            df['dist_close_dc_upper_pct'] = (df['close'] - df['DC_Upper']) / df['close']
            diff_dc = df['DC_Upper'] - df['DC_Lower']
            df['dc_position_relative'] = np.where(diff_dc != 0, (df['close'] - df['DC_Lower']) / diff_dc, 0)
        else:
            return 0.0, current_ml_threshold

        df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['ATR_PCT'] = df['ATR'] / df['close']

        df.dropna(inplace=True)
        if df.empty:
            return 0.0, current_ml_threshold

        row = df.iloc[-1]
        vol_ratio = (row['volume'] / row['vol_avg_20']) if row['vol_avg_20'] > 0 else 0
        dist_ema200 = (row['close'] - row['ema_200']) / row['ema_200'] if row['ema_200'] else 0
        slope_pct_per_day = (slope * 24) / current_close_price if current_close_price else 0
        hour_of_day = pd.to_datetime(row['open_time']).hour

        features_dict = {
            'vol_ratio': [vol_ratio],
            'rsi': [row['rsi']],
            'atr_pct': [row['ATR_PCT']],
            'dist_ema200': [dist_ema200],
            'slope_trend': [slope_pct_per_day],
            'hour_of_day': [hour_of_day],
            'dist_close_ema9_pct': [row['dist_close_ema9_pct']],
            'dist_ema9_ema21_pct': [row['dist_ema9_ema21_pct']],
            'dist_close_kama9_pct': [row['dist_close_kama9_pct']],
            'MACD_Line': [row['MACD_Line']],
            'MACD_Signal': [row['MACD_Signal']],
            'TSI_Line': [row['TSI_Line']],
            'TSI_Signal': [row['TSI_Signal']],
            'dist_close_bb_lower_pct': [row['dist_close_bb_lower_pct']],
            'dist_close_bb_upper_pct': [row['dist_close_bb_upper_pct']],
            'bb_position_relative': [row['bb_position_relative']],
            'dist_close_dc_lower_pct': [row['dist_close_dc_lower_pct']],
            'dist_close_dc_upper_pct': [row['dist_close_dc_upper_pct']],
            'dc_position_relative': [row['dc_position_relative']],
        }
        # FIX: Zusätzlicher Schutz gegen NaN/Inf in den Features.
        # Hinweis: Die Indikatoren werden hier live via pandas_ta neu berechnet,
        # statt sie aus der DB zu lesen. Damit besteht prinzipiell Train/Live-Drift
        # falls das Modell auf Engine-Indikatoren trainiert wurde. Da aber das
        # ML-Modell bereits deployed ist, kann die Feature-Semantik nicht ohne
        # Re-Training geändert werden — daher bleibt pandas_ta-Berechnung als
        # Status quo, mit robusterem Clean-up der Werte.
        X_live = pd.DataFrame(features_dict).astype(float)
        X_live = X_live.replace([np.inf, -np.inf], np.nan).fillna(0.0)
        proba = model_to_use.predict_proba(X_live)[0][1]
        return float(proba), current_ml_threshold
    except Exception as e:
        logger.error(f"ML-Fehler während der ATB Vorhersage: {e}", exc_info=True)
        return 0.0, current_ml_threshold


# 🎨 MEGAGEILER INFO-CHART FUNKTION
def generate_megageil_chart(conn, symbol, trend_direction, slope, intercept):
    try:
        df_7d = pd.read_sql_query(
            f'SELECT * FROM "{symbol}_1h" WHERE open_time >= NOW() - INTERVAL \'8 days\' ORDER BY open_time ASC',
            conn,
            parse_dates=['open_time'],
        )
        df_ind = pd.read_sql_query(
            f'SELECT * FROM "{symbol}_1h_indicators" WHERE open_time >= NOW() - INTERVAL \'8 days\' ORDER BY open_time ASC',
            conn,
            parse_dates=['open_time'],
        )
        df_90d = pd.read_sql_query(
            f'SELECT * FROM "{symbol}_1h" WHERE open_time >= NOW() - INTERVAL \'95 days\' ORDER BY open_time ASC',
            conn,
            parse_dates=['open_time'],
        )

        if df_7d.empty or df_ind.empty:
            return None

        df_7d.columns = [c.upper() for c in df_7d.columns]
        df_ind.columns = [c.upper() for c in df_ind.columns]
        df_90d.columns = [c.lower() for c in df_90d.columns]

        df_7d['OPEN_TIME'] = pd.to_datetime(df_7d['OPEN_TIME']).dt.tz_localize(None)
        df_ind['OPEN_TIME'] = pd.to_datetime(df_ind['OPEN_TIME']).dt.tz_localize(None)
        df_90d['open_time'] = pd.to_datetime(df_90d['open_time']).dt.tz_localize(None)

        cols = [c for c in df_ind.columns if c not in df_7d.columns or c == 'OPEN_TIME']
        df_plot = df_7d.merge(df_ind[cols], on='OPEN_TIME', how='left')
        df_plot = df_plot.ffill().bfill()

        for c in ['OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME']:
            if c in df_plot.columns:
                df_plot[c] = pd.to_numeric(df_plot[c], errors='coerce')

        bg = '#1e1e1e'
        fg = 'white'
        fig = plt.figure(figsize=(22, 15), facecolor=bg)
        gs = gridspec.GridSpec(4, 2, width_ratios=[6, 1], height_ratios=[5, 1, 1.2, 0.8], hspace=0.4, wspace=0.05)
        ax1 = fig.add_subplot(gs[0, 0])
        ax1.set_facecolor(bg)

        # x_vals = np.arange(len(df_plot))
        # o, c, h, l = df_plot['OPEN'].values, df_plot['CLOSE'].values, df_plot['HIGH'].values, df_plot['LOW'].values
        # up, down = c >= o, ~up
        # col_up, col_down = '#44ff44', '#ff4444'

        x_vals = np.arange(len(df_plot))
        o = df_plot['OPEN'].values
        c = df_plot['CLOSE'].values
        h = df_plot['HIGH'].values
        low = df_plot['LOW'].values
        up = c >= o
        down = c < o
        col_up = '#44ff44'
        col_down = '#ff4444'

        ax1.vlines(x_vals[up], low[up], h[up], color=col_up, linewidth=1.2, zorder=3)
        ax1.vlines(x_vals[down], low[down], h[down], color=col_down, linewidth=1.2, zorder=3)
        body_h = np.maximum(np.abs(c - o), (h.max() - low.min()) * 0.002)
        body_b = np.minimum(o, c)
        ax1.bar(x_vals[up], body_h[up], bottom=body_b[up], width=0.6, color=col_up, linewidth=0, zorder=4)
        ax1.bar(x_vals[down], body_h[down], bottom=body_b[down], width=0.6, color=col_down, linewidth=0, zorder=4)

        if all(col in df_plot.columns for col in ['DONCHIAN_UPPER_20', 'DONCHIAN_MID_20', 'DONCHIAN_LOWER_20']):
            ax1.plot(x_vals, df_plot['DONCHIAN_UPPER_20'], color='#00ffff', linewidth=1.5, label='Don Upper 20')
            ax1.plot(x_vals, df_plot['DONCHIAN_MID_20'], color='#00ffff', linewidth=1.0, alpha=0.8, label='Don Mid 20')
            ax1.plot(x_vals, df_plot['DONCHIAN_LOWER_20'], color='#00ffff', linewidth=1.5, label='Don Lower 20')
            ax1.fill_between(
                x_vals, df_plot['DONCHIAN_LOWER_20'], df_plot['DONCHIAN_UPPER_20'], color='#00ffff', alpha=0.06
            )

        if 'EMA_9' in df_plot.columns:
            ax1.plot(x_vals, df_plot['EMA_9'], color='yellow', linewidth=1.1, label='EMA9')
        if 'EMA_21' in df_plot.columns:
            ax1.plot(x_vals, df_plot['EMA_21'], color='#32CD32', linewidth=1.1, label='EMA21')
        if 'WMA_55' in df_plot.columns:
            ax1.plot(x_vals, df_plot['WMA_55'], color='#FFB300', linewidth=1.1, label='WMA55')
        if 'EMA_200' in df_plot.columns:
            ax1.plot(x_vals, df_plot['EMA_200'], color='#E53935', linewidth=1.1, label='EMA200')
        if 'KAMA_9' in df_plot.columns:
            ax1.plot(x_vals, df_plot['KAMA_9'], color='#9C27B0', linewidth=1.1, label='KAMA9')

        ax1.set_title(f"{symbol} Trendline Break", color=fg, fontsize=19, pad=25, weight='bold')
        ax1.legend(facecolor=bg, labelcolor=fg, fontsize=12, loc='upper left')
        ax1.grid(True, alpha=0.25, color=fg, linewidth=0.5)
        ax1.tick_params(colors=fg, labelsize=11)

        margin = (h.max() - low.min()) * 0.05
        ax1.set_ylim(low.min() - margin, h.max() + margin)

        ax_vol = ax1.twinx()
        vol_max = df_plot['VOLUME'].max()
        vol_min_display = vol_max * 0.25
        vol_colors = np.where(up, col_up, col_down)
        display_volume = df_plot['VOLUME'].copy()
        display_volume[display_volume < vol_min_display] = vol_min_display
        ax_vol.bar(
            x_vals,
            display_volume,
            width=0.6,
            color=vol_colors,
            alpha=0.5,
            edgecolor='#ffffff44',
            linewidth=0.25,
            align='center',
        )
        ax_vol.set_ylim(0, vol_max * 2.5)
        ax_vol.yaxis.set_label_position("right")
        ax_vol.yaxis.tick_right()
        ax_vol.set_ylabel('Volume', color=fg, fontsize=13, weight='bold')
        ax_vol.tick_params(colors=fg, labelsize=10)
        ax_vol.grid(True, alpha=0.15, color=fg, linewidth=0.4, linestyle='-', axis='y')

        ax4 = ax1.twinx()
        ax4.fill_between(x_vals, df_plot['VOLUME'], color='gray', alpha=0.4)
        ax4.plot(x_vals, df_plot['VOLUME'], color='gray', linewidth=1)
        ax4.set_ylim(0, vol_max * 2.5)
        ax4.axis('off')

        ax_vbp = fig.add_subplot(gs[0, 1])
        ax_vbp.set_facecolor('#1e1e1e')
        price_bins = np.linspace(low.min(), h.max(), 40)
        vol_by_price = np.zeros(len(price_bins) - 1)
        for _, row in df_7d.iterrows():
            idx = np.searchsorted(price_bins, [row['LOW'], row['HIGH']])
            idx = np.clip(idx, 0, len(vol_by_price) - 1)
            if idx[0] == idx[1]:
                vol_by_price[idx[0]] += row['VOLUME']
            else:
                vol_by_price[idx[0] : idx[1]] += row['VOLUME'] / (idx[1] - idx[0])
        ax_vbp.barh(
            (price_bins[:-1] + price_bins[1:]) / 2,
            vol_by_price,
            height=(price_bins[1] - price_bins[0]) * 0.8,
            color='#ff69b4',
            alpha=0.6,
        )
        ax_vbp.set_ylim(ax1.get_ylim())
        ax_vbp.invert_xaxis()
        ax_vbp.tick_params(colors='white')

        if slope is not None and intercept is not None:
            # FIX: Index-basiertes Mapping für die Trendlinie
            offset = len(df_90d) - len(df_plot)
            trend_y = slope * (x_vals + offset) + intercept
            ax1.plot(x_vals, trend_y, color='orange', linewidth=5.0, alpha=0.98, label=f'90d Trend: {trend_direction}')
            ax1.legend(facecolor=bg, labelcolor=fg, fontsize=12, loc='upper left')

            h_peaks, l_peaks = find_pivots(df_90d, distance=8)
            pivots = h_peaks if trend_direction == 'DOWN' else l_peaks

            pivot_times = df_90d['open_time'].iloc[pivots]
            pivot_prices = df_90d['high' if trend_direction == 'DOWN' else 'low'].iloc[pivots]

            t_map = {t: i for i, t in enumerate(df_plot['OPEN_TIME'])}
            px, py = [], []
            for t, p in zip(pivot_times, pivot_prices, strict=False):
                if t in t_map:
                    px.append(t_map[t])
                    py.append(p)
            color_pivot = '#ff4444' if trend_direction == 'DOWN' else '#44ff44'
            if px:
                ax1.scatter(px, py, color=color_pivot, s=180, zorder=6, edgecolors='white', linewidth=3.0, marker='o')

        ax2 = fig.add_subplot(gs[1, 0], sharex=ax1)
        ax2.set_facecolor(bg)
        if 'RSI_9' in df_plot.columns:
            ax2.plot(x_vals, df_plot['RSI_9'], color='yellow', linewidth=1.1)
        if 'RSI_14' in df_plot.columns:
            ax2.plot(x_vals, df_plot['RSI_14'], color='orange', linewidth=1.1)
        ax2.axhline(75, color='red', linestyle='--', alpha=0.8)
        ax2.axhline(25, color='green', linestyle='--', alpha=0.8)
        ax2.set_ylim(0, 100)
        ax2.set_ylabel('RSI', color=fg)
        ax2.grid(True, alpha=0.15, color=fg)

        ax3 = fig.add_subplot(gs[2, 0], sharex=ax1)
        ax3.set_facecolor(bg)
        if 'TSI_FAST_12_7_7' in df_plot.columns:
            ax3.plot(x_vals, df_plot['TSI_FAST_12_7_7'], color='#00ff00', linewidth=1.8)
        if 'TSI_FAST_12_7_7_SIGNAL' in df_plot.columns:
            ax3.plot(x_vals, df_plot['TSI_FAST_12_7_7_SIGNAL'], color='red', linewidth=1.4)
        ax3.axhline(0, color=fg, linestyle='-', alpha=0.3)
        ax3.set_ylim(-100, 100)
        ax3.set_ylabel('TSI', color=fg)
        ax3.grid(True, alpha=0.15, color=fg)

        def format_date(x, pos=None):
            idx = int(x + 0.5)
            if 0 <= idx < len(df_plot):
                return df_plot['OPEN_TIME'].iloc[idx].strftime('%d.%m %H:%M')
            return ''

        ax1.xaxis.set_major_formatter(mticker.FuncFormatter(format_date))
        ax1.xaxis.set_major_locator(mticker.MaxNLocator(nbins=10))

        for ax in [ax1, ax2, ax3, ax_vol]:
            ax.tick_params(colors=fg, labelsize=10)
            for label in ax.get_xticklabels() + ax.get_yticklabels():
                label.set_color(fg)

        filename = f"{CHART_DIR}/ATB1_BIG_{symbol}_{uuid.uuid4().hex[:8]}.png"
        fig.savefig(filename, format='png', dpi=150, facecolor=bg, bbox_inches='tight', pad_inches=0.4)
        plt.close(fig)
        return filename
    except Exception as e:
        logger.error(f"Error for Mega-Chart Generierung für {symbol}: {e}", exc_info=True)
        return None


# 🚀 HAUPT ENGINE

# FIX (#51): Eigene is_cooled_down-Funktion entfernt und durch check_cooldown
# aus core.market_utils ersetzt. Die alte Version hatte:
#   - Eigenständige (aber korrekte) Timezone-Logik
#   - `except: return True` Bug (Batch 1 bereits behoben)
#   - aber immer noch Code-Duplikation mit market_utils
# Jetzt: zentraler Helper + update_cooldown explizit after erfolgreichem Send.


# FIX (#51): Eigene set_cooldown-Funktion entfernt und durch update_cooldown
# aus core.market_utils ersetzt (beide haben identische Semantik).


def save_minichart_to_disk(symbol: str) -> str:
    """Holt den fertigen Minichart-Pfad aus der Core-Engine."""
    try:
        # Die Core-Funktion generiert den Chart, speichert ihn in 'charts/'
        # und gibt uns den direkten Dateipfad als String zurück.
        chart_path = generate_minichart_image(symbol, minutes=240)

        # Sicherheitsprüfung: Wurde ein Pfad zurückgegeben und existiert die Datei wirklich?
        if chart_path and isinstance(chart_path, str) and os.path.exists(chart_path):
            return chart_path
        else:
            logger.warning(f"⚠️ Minichart für {symbol} konnte not found/generiert werden.")
            return None

    except Exception as e:
        logger.error(f"❌ Fehler beim Abrufen des Minicharts für {symbol}: {e}")
        return None


def send_signal(conn, symbol, direction, prob, close_price, event_name, trend_direction, pic_path):
    # FIX: check_cooldown returned True wenn Cooldown AKTIV ist → skip.
    if check_cooldown(conn, MODEL_ID, symbol, direction, 4):
        logger.info(f"⏳ Cooldown active für {symbol} ({direction}).")
        return

    entry1 = float(close_price)
    entry2 = entry1 * 0.96 if direction == "LONG" else entry1 * 1.04
    supps, resis = get_hvn_and_sr_levels(conn, symbol, entry1)

    if direction == "LONG":
        sl = max([x for x in supps if x < entry2 * 0.99]) if any(x < entry2 * 0.99 for x in supps) else entry2 * 0.95
        t_cands = sorted([x for x in resis if x > (entry1 * 1.01)])
    else:
        sl = min([x for x in resis if x > entry2 * 1.01]) if any(x > entry2 * 1.01 for x in resis) else entry2 * 1.05
        t_cands = sorted([x for x in supps if x > 0 and x < (entry1 * 0.99)], reverse=True)

    # FIX: echte Zonen + ggf. 5%-Target wenn letzte Zone zu nah
    targets = ensure_min_tp_distance(t_cands[:20], entry1, direction == "LONG", min_pct=0.05)

    lev = get_max_leverage(symbol, 20)

    lines = [
        f"📈 Signal for {symbol} 📈",
        f"🚨 Direction: {direction}",
        f"🚨 Leverage: {lev}",
        "🚨 Margin: Cross",
        f"🏦 CMP Entry: $ {entry1:.5f}",
        f"🏦 Entry 2: $ {entry2:.5f}",
    ]
    for i, t in enumerate(targets[:3], 1):
        lines.append(f"💰 TP{i}: $ {t:.5f}")
    lines += [f"💸 Stop Loss: $ {sl:.5f}", f"🧠 Trade idea generated by AI module {MODEL_ID}"]
    cornix_msg = "\n".join(lines)

    emoji = "🚀 AI ATB1 TRENDLINE LONG" if direction == "LONG" else "💥 AI ATB1 TRENDLINE SHORT"

    html_caption = f"""<pre><b>{emoji}</b>\n<b>{symbol}</b>\n<b>→ Direction: {direction}</b>\n<b>→ Event: {event_name}</b>\n<b>→ 90d Trend: {trend_direction}</b>\n<b>→ ML Confidence: <b>{prob:.1%}</b></b>\n<b>→ Time: {datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M')} UTC | Modul: {MODEL_ID}</b></pre>"""

    # chart_path = save_minichart_to_disk(symbol)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (TARGET_CHANNEL_ID, cornix_msg)
        )
        if pic_path:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                (TARGET_CHANNEL_ID, html_caption, pic_path),
            )
        else:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (TARGET_CHANNEL_ID, html_caption)
            )
        cur.execute(
            """INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                symbol,
                float(entry1),
                MODEL_ID,
                direction,
                float(prob),
                float(entry1),
                float(entry2),
                float(sl),
                json.dumps(targets),
            ),
        )
    conn.commit()
    logger.info(f"✅ {MODEL_ID} Trade Signal für {symbol} in Outbox gelegt!")
    update_cooldown(conn, MODEL_ID, symbol, direction)


def run_trendline_detector():
    logger.info("🔍 Starting Trendline Break/Bounce Scan (ATB1)...")
    conn = get_db_connection()
    coins = load_models_and_coins()
    now = datetime.datetime.now(datetime.timezone.utc)

    stats_dict = {"total": 0, "no_data": 0, "no_trend": 0, "too_far": 0, "events": 0}
    distance_logs = []

    for symbol in coins:
        if 'USDT_' in symbol:
            continue
        stats_dict["total"] += 1

        state = TRENDLINE_STATE.get(
            symbol,
            {"last_alert": datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc), "prev_relation": "unknown"},
        )
        if (now - state["last_alert"]).total_seconds() < 3600:
            continue

        try:
            query = f"""SELECT open_time, open, high, low, close, volume FROM "{symbol}_1h" WHERE open_time >= NOW() - INTERVAL '95 days' ORDER BY open_time ASC"""
            df_90d = pd.read_sql_query(query, conn, parse_dates=['open_time'])
            if len(df_90d) < 50:
                stats_dict["no_data"] += 1
                continue

            df_90d['open_time'] = pd.to_datetime(df_90d['open_time'], utc=True)
            df_recent = df_90d.tail(4).copy()
            if len(df_recent) < 3:
                stats_dict["no_data"] += 1
                continue

            last_close = float(df_recent['close'].iloc[-1])
            prev_close = float(df_recent['close'].iloc[-2])

            trend_direction, trend_data = detect_trend(df_90d)
            if not trend_data:
                stats_dict["no_trend"] += 1
                continue

            slope, intercept = trend_data

            # FIX: Wir berechnen die Linie nun über den Kerzen-Index!
            last_idx = len(df_90d) - 1
            trend_value_last = slope * last_idx + intercept

            # FIX: Standard-Prozent-Rechnung (verhindert das -100% Problem!)
            rel_distance = abs(last_close - trend_value_last) / last_close
            distance_logs.append((symbol, rel_distance * 100))

            if rel_distance > MAX_DISTANCE_PCT:
                stats_dict["too_far"] += 1
                continue

            tolerance = last_close * 0.008
            distance = last_close - trend_value_last

            if abs(distance) <= tolerance:
                current_relation = "near"
            elif distance > 0:
                current_relation = "above"
            else:
                current_relation = "below"

            prev_relation = state["prev_relation"]
            event = None

            # HIER IST DER BUG AUS DEINEM ALTEN BOT WIEDER AKTIV!
            # Erlaubt dem Bot wieder am Fließband zu triggern, wenn Coins ins Radar kommen
            if prev_relation in ["below", "near", "unknown"] and current_relation == "above" and distance > tolerance:
                event = "TRENDLINE BREAK UP"
            elif (
                prev_relation in ["above", "near", "unknown"] and current_relation == "below" and distance < -tolerance
            ):
                event = "TRENDLINE BREAK DOWN"
            elif prev_relation in ["above", "unknown"] and current_relation == "near":
                if min(df_recent['low'].iloc[-3:]) >= trend_value_last - tolerance and last_close > prev_close:
                    event = "BOUNCE UP FROM TRENDLINE"
            elif prev_relation in ["below", "unknown"] and current_relation == "near":
                if max(df_recent['high'].iloc[-3:]) <= trend_value_last + tolerance and last_close < prev_close:
                    event = "BOUNCE DOWN FROM TRENDLINE"

            if event:
                stats_dict["events"] += 1
                ml_prob, threshold = get_ml_prediction(df_90d, event, slope, last_close)
                logger.info(f"Signal: {symbol} {event} | ML Score: {ml_prob:.2f} (Thresh: {threshold:.2f})")

                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """CREATE TABLE IF NOT EXISTS trendmeet_rawdata (id SERIAL PRIMARY KEY, detection_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP, coin TEXT, event_type TEXT, trend_direction TEXT, close_price NUMERIC, trend_value NUMERIC, rel_distance_pct NUMERIC, abs_distance NUMERIC)"""
                        )
                        cur.execute(
                            """INSERT INTO trendmeet_rawdata (coin, event_type, trend_direction, close_price, trend_value, rel_distance_pct, abs_distance) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                            (
                                symbol,
                                event,
                                trend_direction,
                                float(last_close),
                                float(trend_value_last),
                                float(rel_distance) * 100,
                                float(distance),
                            ),
                        )

                        if ml_prob >= 0.25:
                            direction = "LONG" if "UP" in event else "SHORT"
                            cur.execute(
                                """CREATE TABLE IF NOT EXISTS ML_TREND_TRADES (id SERIAL PRIMARY KEY, symbol TEXT, direction TEXT, ml_probability NUMERIC, close_price NUMERIC, event_type TEXT, trend_direction TEXT, created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP)"""
                            )
                            cur.execute(
                                """INSERT INTO ML_TREND_TRADES (symbol, direction, ml_probability, close_price, event_type, trend_direction) VALUES (%s, %s, %s, %s, %s, %s)""",
                                (
                                    symbol,
                                    direction,
                                    float(ml_prob),
                                    float(last_close),
                                    f"{event} (ML: {ml_prob:.0%})",
                                    trend_direction,
                                ),
                            )

                            cur.execute(
                                """INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted) VALUES (0, %s, %s, %s, %s, %s, %s, False)""",
                                ("ATB1", now, symbol, direction, float(last_close), float(ml_prob)),
                            )
                    conn.commit()
                except Exception as e:
                    logger.error(f"DB Error bei Trendline: {e}")
                    conn.rollback()

                emoji = "🚀" if "UP" in event else "💥"
                trade_status = "(Trade Triggered ✅)" if ml_prob >= threshold else "(No Trade ❌)"

                # Formatierte Prozentzahl für die Telegram Ausgabe
                dist_str = (distance / trend_value_last) * 100

                info_html = f"""<pre><b>{emoji} {event}</b>\n<b>{symbol.replace('USDT', '')}/USDT</b>\n<b>→ 90d Trend: <b>{trend_direction}</b></b>\n<b>→ Close: <code>${last_close:,.8f}</code> | Trend: <code>${trend_value_last:,.8f}</code></b>\n<b>→ Distance: {dist_str:+.2f}%</b>\n<b>→ ML Confidence: {ml_prob:.1%} {trade_status}</b>\n<b>→ Time: {now.strftime('%H:%M')} UTC</b></pre>"""

                info_chart_path = generate_megageil_chart(conn, symbol, trend_direction, slope, intercept)

                try:
                    with conn.cursor() as cur:
                        if info_chart_path:
                            cur.execute(
                                "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                                (TRENDBREAKER_CHANNEL_ID, info_html, info_chart_path),
                            )
                        else:
                            cur.execute(
                                "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                                (TRENDBREAKER_CHANNEL_ID, info_html),
                            )
                    conn.commit()
                except Exception as e:
                    logger.error(f"Error sending in Trendbreaker Channel: {e}")
                    conn.rollback()

                if ml_prob >= threshold:
                    direction = "LONG" if "UP" in event else "SHORT"
                    logger.info(f"🔥 ATB1 TRADE EXECUTE: {symbol} {direction}")
                    send_signal(conn, symbol, direction, ml_prob, last_close, event, trend_direction, info_chart_path)

                state["last_alert"] = now

            state["prev_relation"] = current_relation
            TRENDLINE_STATE[symbol] = state

        except Exception as e:
            logger.error(f"Error for {symbol} in ATB1 Detector: {e}", exc_info=True)

    conn.close()

    # FIX: State after jedem Scan persistieren, damit der Bot bei Restart weiß,
    # welche Coins schon über/unter ihrer Trendlinie waren (verhindert Massen-Alerts).
    save_trendline_state()

    distance_logs.sort(key=lambda x: x[1])
    top_3 = ", ".join([f"{s} ({d:.1f}%)" for s, d in distance_logs[:5]])

    logger.info(
        f"🏁 ATB1 Trendline Scan stopped. "
        f"Geprüft: {stats_dict['total']} | "
        f"Kein klarer Trend (R<{TREND_MIN_R_VALUE}): {stats_dict['no_trend']} | "
        f"Zu weit von Trendline entfernt (>{MAX_DISTANCE_PCT * 100}%): {stats_dict['too_far']} | "
        f"Breakouts gefunden: {stats_dict['events']}"
    )
    logger.info(f"🔍 Top 5 nächste Coins zur Trendlinie aktuell: {top_3}")


def main():
    logger.info("=== 📐 AI ATB1 (Trendline Break/Bounce Sniper) GESTARTET ===")
    load_models_and_coins()
    # FIX: Bekannten Trendline-State beim Start laden.
    load_trendline_state()

    while True:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            if now.minute == 3:
                run_trendline_detector()
                time.sleep(60)
            else:
                time.sleep(10)
        except KeyboardInterrupt:
            logger.info("ATB1 Bot manuell stopped (Strg+C).")
            break


if __name__ == "__main__":
    main()
