import json
import logging
import os
import socket
import threading

# ⚠️  MUST come before any other matplotlib import — prevents
#     "cannot connect to display" crashes on headless servers.
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use('Agg')

import matplotlib.pyplot as plt
from matplotlib.dates import DateFormatter, MinuteLocator
from matplotlib.patches import Rectangle

logger = logging.getLogger(__name__)

# --- Font fallback chain for CJK coin names ---
# Some Binance Futures coins have Chinese names (e.g. 龙虾USDT, 币安人生)
# that are rendered in chart titles/axes. DejaVu Sans (matplotlib default)
# does not contain these glyphs → UserWarning spam in logs and boxes in charts.
#
# We build a fallback chain: first DejaVu (for Latin), then typical
# Windows CJK fonts. Matplotlib uses the first font that contains the glyph.
# Unknown fonts are silently ignored, so it's harmless if some
# of the listed fonts are not installed.
try:
    plt.rcParams['font.sans-serif'] = [
        'DejaVu Sans',  # default, for Latin
        'Microsoft YaHei',  # Win10/11 default CJK
        'SimHei',  # Windows CJK fallback
        'Noto Sans CJK SC',  # Linux CJK
        'Arial Unicode MS',  # macOS CJK
        'sans-serif',  # ultimate fallback
    ]
    # axes.unicode_minus=False avoids extra font warnings for the
    # minus sign (Unicode U+2212 vs ASCII '-')
    plt.rcParams['axes.unicode_minus'] = False
    # Suppress logger spam from matplotlib.font_manager for missing
    # glyphs — with the fallback chain in place, that's just noise.
    logging.getLogger('matplotlib.font_manager').setLevel(logging.ERROR)
except Exception as e:
    # If something goes wrong: not fatal, we stay on the default
    logger.debug(f"CJK font setup failed (non-fatal): {e}")

# Prevents multiple bots from corrupting matplotlib state simultaneously.
# plt is NOT thread-safe — without a lock there are sporadic crashes & broken charts.
_CHART_LOCK = threading.Lock()

# ─── Chart Data Service Client ───────────────────────────────────────────────
# Fetches 1min candles from chart_data_service.py (phase 2 architecture).
# No more fapi fallback — if the service is dead, the signal is sent without
# a chart (caller code checks for None).

CHART_SERVICE_HOST = os.getenv("CHART_SERVICE_HOST", "127.0.0.1")
CHART_SERVICE_PORT = int(os.getenv("CHART_SERVICE_PORT", "5555"))
_SERVICE_TIMEOUT = 3.0  # seconds


def _fetch_1m_from_service(symbol: str, minutes: int = 240) -> pd.DataFrame:
    """Fetches 1min candles from the local chart_data_service via TCP.

    Protocol: line-based JSON (see chart_data_service.py).
    On errors (service unreachable, symbol unknown, no buffer):
    returns an empty DataFrame, the caller then falls back to no-chart behaviour.
    """
    request = json.dumps({"cmd": "get", "symbol": symbol, "minutes": minutes}) + "\n"

    try:
        with socket.create_connection((CHART_SERVICE_HOST, CHART_SERVICE_PORT), timeout=_SERVICE_TIMEOUT) as sock:
            sock.sendall(request.encode("utf-8"))
            # Read response until newline
            buf = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    break

        line = buf.split(b"\n", 1)[0].decode("utf-8")
        response = json.loads(line)

    except (TimeoutError, ConnectionRefusedError, OSError) as e:
        logger.warning(f"Chart service unreachable for {symbol}: {e}")
        return pd.DataFrame()
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"Chart service returned an invalid response for {symbol}: {e}")
        return pd.DataFrame()
    except Exception as e:
        logger.warning(f"Chart service error for {symbol}: {e}")
        return pd.DataFrame()

    if "error" in response:
        logger.debug(f"Chart service for {symbol}: {response['error']}")
        return pd.DataFrame()

    candles = response.get("candles", [])
    if not candles:
        return pd.DataFrame()

    # Convert into the expected format (like the former Binance response)
    # [open_time_ms, open, high, low, close, volume] → ['t', 'o', 'h', 'l', 'p', 'v10s']
    df = pd.DataFrame(candles, columns=['t', 'o', 'h', 'l', 'p', 'v10s'])
    df['t'] = pd.to_datetime(df['t'], unit='ms', utc=True)
    df['o'] = df['o'].astype(float)
    df['h'] = df['h'].astype(float)
    df['l'] = df['l'].astype(float)
    df['p'] = df['p'].astype(float)
    df['v10s'] = df['v10s'].astype(float)
    df = df.sort_values('t').set_index('t')
    return df


def _fetch_5m_from_db(symbol: str, minutes: int = 240) -> pd.DataFrame:
    """Fetches 5min candles from the local {symbol}_5m table for the candle overlay.

    On errors (table missing, DB down, no connection pool):
    empty DataFrame. The chart is then rendered with only the 1min line (no candles).
    """
    # We need ceil(minutes / 5) + a small buffer
    n_candles = int(minutes / 5) + 2

    try:
        # Lazy import: prevents a circular dependency at module load
        from core.candles import read_candles
        from core.database import get_db_connection
    except Exception as e:
        logger.debug(f"DB import for the 5m layer failed: {e}")
        return pd.DataFrame()

    try:
        conn = get_db_connection()
        try:
            # Via core.candles: the newest n CLOSED 5m candles, ASC. The
            # overlay is cosmetic — the forming candle does not belong in it (R1).
            df = read_candles(
                conn,
                symbol,
                "5m",
                limit=n_candles,
                include_forming=False,
                columns=("open_time", "open", "high", "low", "close", "volume"),
            )
        finally:
            conn.close()

        if df.empty:
            return pd.DataFrame()

        df['open_time'] = pd.to_datetime(df['open_time'], utc=True)
        df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
        df = df.sort_values('open_time').set_index('open_time')
        return df

    except Exception as e:
        logger.debug(f"5m DB fetch for {symbol} failed: {e}")
        return pd.DataFrame()


# Compatibility alias: the old function name stays, so existing callers
# (all 8 AI bots) need no code change. Internally this delegates to the new
# service. The fallback behaviour is the same as before: empty DF → caller
# sends the signal without a chart.
def fetch_1m_data_binance(symbol: str, minutes: int = 240) -> pd.DataFrame:
    """Compatibility wrapper. Now uses the chart data service instead of fapi.

    Old function name + signature stay identical, so existing AI bots
    (EPD, SR, MIS, ATS, RUB, ATB, Master, ABR1) don't need to be adjusted.
    """
    return _fetch_1m_from_service(symbol, minutes)


def generate_minichart_image(
    symbol: str, minutes: int = 240, spike_time=None, spike_start=None, spike_end=None
) -> str | None:
    """Generates a mini chart image for the given symbol.

    Args:
        symbol: e.g. "BTCUSDT"
        minutes: chart range in minutes (default 240 = 4h)
        spike_time: [legacy] optional datetime for a single vertical
            marker line. Still supported for backwards compatibility
            with existing callers.
        spike_start: optional datetime for the start of a pump/dump
            range. If given together with spike_end, a shaded region
            is drawn between the two (orange for
            pump, red for dump — direction is derived from the prices).
        spike_end: optional datetime for the end of the range.
    """
    with _CHART_LOCK:
        return _generate_chart_locked(symbol, minutes, spike_time, spike_start, spike_end)


def _generate_chart_locked(symbol: str, minutes: int, spike_time=None, spike_start=None, spike_end=None) -> str | None:
    """Internal implementation — only call from within _CHART_LOCK."""
    fig = None
    try:
        df = fetch_1m_data_binance(symbol, minutes)

        if df.empty or len(df) < 5:
            logger.warning(f"Insufficient data for chart: {symbol}")
            return None

        price = df['p']
        volume = df['v10s']
        actual_minutes = int((df.index[-1] - df.index[0]).total_seconds() / 60)

        # 5min candles for the candle overlay from the local DB
        # (if DB not available, the chart is rendered without candles — line only)
        df_5m = _fetch_5m_from_db(symbol, minutes)

        # === SETUP ===
        fig = plt.figure(figsize=(16, 9), facecolor="#0d0d0d")
        gs = fig.add_gridspec(1, 2, width_ratios=[4, 1], wspace=0.05)
        ax_price = fig.add_subplot(gs[0, 0])
        ax_vol = ax_price.twinx()
        ax_vbp = fig.add_subplot(gs[0, 1])

        is_up = price.iloc[-1] >= price.iloc[0]

        # === VOLUME (1min granular) ===
        vol_max_scale = volume.quantile(0.99) if (len(volume) > 0 and volume.max() > 0) else 1
        if vol_max_scale == 0:
            vol_max_scale = volume.max() or 1

        vol_colors = [
            '#00ff88' if i == 0 or price.iloc[i] >= price.iloc[i - 1] else '#ff3040' for i in range(len(price))
        ]
        time_diffs = df.index.to_series().diff().dt.total_seconds().median()
        if pd.isna(time_diffs) or time_diffs == 0:
            time_diffs = 60
        width_days = (time_diffs / 86400) * 0.9

        ax_vol.bar(price.index, volume, color=vol_colors, width=width_days, alpha=0.5, align='center', zorder=1)
        ax_vol.set_ylim(0, vol_max_scale * 4.0)
        ax_vol.axis('off')

        # === 5min CANDLES (overlay) ===
        # Rendered UNDER the 1min line, so the line stays visible.
        # Width: 5 minutes = 5/1440 days, 85% of that for a slight gap between candles.
        if not df_5m.empty and len(df_5m) >= 2:
            # Only show candles within the time range of the 1min chart
            df_5m = df_5m[(df_5m.index >= df.index[0]) & (df_5m.index <= df.index[-1])]

            if len(df_5m) >= 1:
                for ts, row in df_5m.iterrows():
                    o, h, low, c = row['open'], row['high'], row['low'], row['close']
                    candle_is_up = c >= o
                    body_color = '#00ff88' if candle_is_up else '#ff3040'
                    body_alpha = 0.55
                    wick_alpha = 0.70

                    # Wick (high-low) as a thin line
                    ax_price.plot([ts, ts], [low, h], color=body_color, linewidth=1.0, alpha=wick_alpha, zorder=2.3)

                    # Body as a rectangle
                    body_low = min(o, c)
                    body_height = abs(c - o)
                    # Minimum height so doji candles are visible
                    if body_height < (h - low) * 0.02 and (h - low) > 0:
                        body_height = (h - low) * 0.02
                    rect = Rectangle(
                        (ts - pd.Timedelta(seconds=150 * 0.85), body_low),  # x-offset, y
                        pd.Timedelta(seconds=300 * 0.85),
                        body_height,  # width, height
                        facecolor=body_color,
                        edgecolor=body_color,
                        alpha=body_alpha,
                        zorder=2.4,
                        linewidth=0.7,
                    )
                    ax_price.add_patch(rect)

        # === PRICE LINE (1min, main focus) ===
        fill_color = "#00ff88" if is_up else "#ff3040"
        ax_price.fill_between(price.index, price, price.min(), color=fill_color, alpha=0.12, zorder=2)
        ax_price.plot(price.index, price, color="#00ffff", linewidth=2.0, zorder=3)
        ax_price.axhline(price.iloc[-1], color="white", linewidth=1, linestyle="--", alpha=0.5, zorder=3.5)
        ax_price.text(
            0.05,
            price.iloc[-1],
            f"{price.iloc[-1]:,.4f}",
            transform=ax_price.get_yaxis_transform(),
            color="white",
            fontsize=10,
            fontweight='bold',
            va='center',
            bbox=dict(facecolor='#1e1e1e', edgecolor='none', pad=5),
            zorder=4,
        )

        # === SPIKE MARKER (DISABLED) ===
        # Used to be used after the fix of the bucket-timestamp logic to
        # visually verify that the spike timestamps are correct.
        # Since the fix has since been validated and is running in production,
        # we no longer need the visual confirmation.
        #
        # The parameters spike_time/spike_start/spike_end remain in the
        # signature for backwards compatibility with existing
        # callers — but are ignored as a no-op.
        #
        # If the lines are ever needed again, see git history
        # for the original implementation with axvspan + two axvline.
        _ = (spike_start, spike_end, spike_time)  # explicitly marked as unused

        # === VOLUME PROFILE (VBP) ===
        ax_vbp.set_facecolor("#0d0d0d")
        bins = np.linspace(price.min() * 0.995, price.max() * 1.005, 45)
        hist, _ = np.histogram(price, bins=bins, weights=volume)
        centers = (bins[:-1] + bins[1:]) / 2
        bar_height = (bins[1] - bins[0]) * 0.88

        ax_vbp.barh(centers, hist, height=bar_height, color='#ff69b4', alpha=0.75, edgecolor='#ff1493', linewidth=0.6)
        max_idx = np.argmax(hist)
        ax_vbp.barh(
            centers[max_idx],
            hist[max_idx],
            height=(bins[1] - bins[0]),
            color='#00ffff',
            alpha=0.9,
            edgecolor='#ff1493',
            linewidth=0.6,
        )

        ax_vbp.set_ylim(ax_price.get_ylim())
        ax_vbp.invert_xaxis()
        ax_vbp.set_xlabel('Vol', color='#ff69b4', fontsize=10)
        ax_vbp.tick_params(colors='#ff69b4', labelsize=8)
        ax_vbp.spines[['top', 'right', 'left', 'bottom']].set_visible(False)

        # === STYLING ===
        coin_str = symbol.replace("USDT", "")
        title_time = f"{actual_minutes}min" if actual_minutes > 0 else f"{minutes}min"
        ax_price.set_title(
            f"{coin_str} • {title_time} • ${price.iloc[-1]:,.8f}",
            color="white",
            fontsize=20,
            fontweight='bold',
            loc='center',
            pad=10,
        )
        ax_price.grid(True, color='#333333', alpha=0.3, linestyle='--')
        ax_price.set_facecolor("#0d0d0d")
        ax_price.spines[['top', 'right', 'left', 'bottom']].set_visible(False)
        ax_price.tick_params(axis='x', colors='#888888', labelsize=10)
        ax_price.tick_params(axis='y', colors='#888888', labelsize=10)

        locator = MinuteLocator(interval=max(1, int(actual_minutes / 6)))
        ax_price.xaxis.set_major_locator(locator)
        ax_price.xaxis.set_major_formatter(DateFormatter('%H:%M'))
        ax_price.set_xlim(df.index[0], df.index[-1])
        plt.subplots_adjust(left=0.05, right=0.9, top=0.9, bottom=0.1)

        # === SAVING ===
        # FIX (#67): unique filename with millisecond timestamp.
        # Previously a fixed path `charts/{symbol}_ai_chart.png` → race condition!
        # If two bots generate a chart for e.g. BTCUSDT in parallel, they overwrite
        # each other BEFORE the Telegram send. The second outbox entry
        # then points to the same path, but the Telegram bot deletes the file after
        # the first send → FileNotFoundError on the second ("image not found").
        import time as _t

        os.makedirs("charts", exist_ok=True)
        chart_path = f"charts/{symbol}_{int(_t.time() * 1000)}_ai_chart.png"
        plt.savefig(chart_path, format='png', dpi=150, facecolor="#0d0d0d", bbox_inches='tight')
        return chart_path

    except Exception as e:
        logger.error(f"Error generating chart for {symbol}: {e}")
        return None

    finally:
        # Cleans up all figure objects — prevents RAM leaks over many calls.
        if fig is not None:
            plt.close(fig)
        plt.close('all')
