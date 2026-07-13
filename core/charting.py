import json
import logging
import os
import socket
import threading

# ⚠️  MUSS vor jedem anderen matplotlib-Import stehen — verhindert
#     "cannot connect to display"-Abstürze auf headless Servern.
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use('Agg')

import matplotlib.pyplot as plt
from matplotlib.dates import DateFormatter, MinuteLocator
from matplotlib.patches import Rectangle

logger = logging.getLogger(__name__)

# --- Font fallback chain for CJK coin names ---
# Some Binance Futures coins have Chinese names (z.B. 龙虾USDT, 币安人生)
# that are rendered in chart titles/axes. DejaVu Sans (matplotlib default)
# does not contain these glyphs → UserWarning spam in logs and boxes in charts.
#
# We build a fallback chain: first DejaVu (for Latin), then typical
# Windows-CJK-Fonts. Matplotlib nutzt den ersten Font der das Glyph enthält.
# Unbekannte Fonts werden still ignoriert, also unbedenklich wenn manche
# der genannten Fonts nicht installiert sind.
try:
    plt.rcParams['font.sans-serif'] = [
        'DejaVu Sans',  # Default, für Latin
        'Microsoft YaHei',  # Win10/11 default CJK
        'SimHei',  # Windows CJK fallback
        'Noto Sans CJK SC',  # Linux CJK
        'Arial Unicode MS',  # macOS CJK
        'sans-serif',  # ultimate fallback
    ]
    # axes.unicode_minus=False vermeidet zusätzliche Font-Warnings beim
    # Minus-Zeichen (Unicode U+2212 vs ASCII '-')
    plt.rcParams['axes.unicode_minus'] = False
    # Logger-Spam von matplotlib.font_manager bei fehlenden Glyphen
    # unterdrücken — after der Fallback-Kette ist das Rauschen.
    logging.getLogger('matplotlib.font_manager').setLevel(logging.ERROR)
except Exception as e:
    # Falls was schiefgeht: nicht fatal, wir bleiben beim Default
    logger.debug(f"CJK-Font-Setup fehlgeschlagen (non-fatal): {e}")

# Verhindert, dass mehrere Bots gleichzeitig matplotlib-State korrumpieren.
# plt ist NICHT thread-safe — ohne Lock gibt es sporadische Abstürze & kaputte Charts.
_CHART_LOCK = threading.Lock()

# ─── Chart Data Service Client ───────────────────────────────────────────────
# Fetcht 1min-Kerzen aus chart_data_service.py (Phase 2 Architektur).
# Kein fapi-Fallback mehr — wenn der Service tot ist, wird das Signal ohne
# Chart verschickt (Caller-Code prüft auf None).

CHART_SERVICE_HOST = os.getenv("CHART_SERVICE_HOST", "127.0.0.1")
CHART_SERVICE_PORT = int(os.getenv("CHART_SERVICE_PORT", "5555"))
_SERVICE_TIMEOUT = 3.0  # Sekunden


def _fetch_1m_from_service(symbol: str, minutes: int = 240) -> pd.DataFrame:
    """Holt 1min-Kerzen vom lokalen chart_data_service via TCP.

    Protokoll: line-based JSON (siehe chart_data_service.py).
    Bei Fehlern (Service nicht erreichbar, Symbol unbekannt, kein Buffer):
    leeres DataFrame zurück, Caller fällt dann ggf. auf kein-Chart-Verhalten.
    """
    request = json.dumps({"cmd": "get", "symbol": symbol, "minutes": minutes}) + "\n"

    try:
        with socket.create_connection((CHART_SERVICE_HOST, CHART_SERVICE_PORT), timeout=_SERVICE_TIMEOUT) as sock:
            sock.sendall(request.encode("utf-8"))
            # Antwort lesen bis Newline
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
        logger.warning(f"Chart-Service nicht erreichbar für {symbol}: {e}")
        return pd.DataFrame()
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"Chart-Service lieferte ungültige Antwort für {symbol}: {e}")
        return pd.DataFrame()
    except Exception as e:
        logger.warning(f"Chart-Service-Fehler für {symbol}: {e}")
        return pd.DataFrame()

    if "error" in response:
        logger.debug(f"Chart-Service für {symbol}: {response['error']}")
        return pd.DataFrame()

    candles = response.get("candles", [])
    if not candles:
        return pd.DataFrame()

    # Umwandeln in das erwartete Format (wie vormals Binance-Response)
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
    """Holt 5min-Kerzen aus der lokalen {symbol}_5m-Tabelle für das Candle-Overlay.

    Bei Fehlern (Tabelle fehlt, DB down, kein Connection-Pool):
    leeres DataFrame. Chart wird dann nur mit 1min-Linie gerendert (ohne candles).
    """
    # Wir brauchen ceil(minutes / 5) + kleiner Puffer
    n_candles = int(minutes / 5) + 2

    try:
        # Lazy import: verhindert zirkuläre Abhängigkeit beim Modul-Load
        from core.candles import read_candles
        from core.database import get_db_connection
    except Exception as e:
        logger.debug(f"DB-Import für 5m-Layer fehlgeschlagen: {e}")
        return pd.DataFrame()

    try:
        conn = get_db_connection()
        try:
            # Über core.candles: die neuesten n GESCHLOSSENEN 5m-Kerzen, ASC. Das
            # Overlay ist kosmetisch — die forming Kerze gehört nicht hinein (R1).
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
        logger.debug(f"5m-DB-Fetch für {symbol} fehlgeschlagen: {e}")
        return pd.DataFrame()


# Kompatibilitäts-Alias: Der alte Funktionsname bleibt, damit bestehende Aufrufer
# (alle 8 AI-Bots) keinen Code-Change brauchen. Intern delegiert das an den neuen
# Service. Das Fallback-Verhalten ist dasselbe wie bisher: leeres DF → Caller
# schickt das Signal ohne Chart.
def fetch_1m_data_binance(symbol: str, minutes: int = 240) -> pd.DataFrame:
    """Kompatibilitäts-Wrapper. Nutzt jetzt den Chart-Data-Service statt fapi.

    Alter Funktionsname + Signatur bleiben identisch, damit bestehende AI-Bots
    (EPD, SR, MIS, ATS, RUB, ATB, Master, ABR1) nicht angepasst werden müssen.
    """
    return _fetch_1m_from_service(symbol, minutes)


def generate_minichart_image(
    symbol: str, minutes: int = 240, spike_time=None, spike_start=None, spike_end=None
) -> str | None:
    """Erzeugt ein Mini-Chart-Bild für das gegebene Symbol.

    Args:
        symbol: z.B. "BTCUSDT"
        minutes: Chart-Range in Minuten (default 240 = 4h)
        spike_time: [Legacy] Optional datetime für eine einzelne vertikale
            Marker-Linie. Wird weiter unterstützt für Rückwärtskompatibilität
            mit bestehenden Aufrufern.
        spike_start: Optional datetime für den Beginn eines Pump/Dump-
            Bereichs. Wenn zusammen mit spike_end angegeben, wird eine
            schattierte Region zwischen beiden gezeichnet (orange für
            Pump, rot für Dump — Richtung wird aus den Preisen abgeleitet).
        spike_end: Optional datetime für das Ende des Bereichs.
    """
    with _CHART_LOCK:
        return _generate_chart_locked(symbol, minutes, spike_time, spike_start, spike_end)


def _generate_chart_locked(symbol: str, minutes: int, spike_time=None, spike_start=None, spike_end=None) -> str | None:
    """Interne Implementierung — nur innerhalb von _CHART_LOCK aufrufen."""
    fig = None
    try:
        df = fetch_1m_data_binance(symbol, minutes)

        if df.empty or len(df) < 5:
            logger.warning(f"Insufficient data für Chart: {symbol}")
            return None

        price = df['p']
        volume = df['v10s']
        actual_minutes = int((df.index[-1] - df.index[0]).total_seconds() / 60)

        # 5min-Kerzen für Candle-Overlay aus lokaler DB
        # (wenn DB not available, wird Chart ohne Kerzen gerendert — nur Linie)
        df_5m = _fetch_5m_from_db(symbol, minutes)

        # === SETUP ===
        fig = plt.figure(figsize=(16, 9), facecolor="#0d0d0d")
        gs = fig.add_gridspec(1, 2, width_ratios=[4, 1], wspace=0.05)
        ax_price = fig.add_subplot(gs[0, 0])
        ax_vol = ax_price.twinx()
        ax_vbp = fig.add_subplot(gs[0, 1])

        is_up = price.iloc[-1] >= price.iloc[0]

        # === VOLUMEN (1min granular) ===
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

        # === 5min-KERZEN (Overlay) ===
        # Werden UNTER die 1min-Linie gerendert, damit die Linie sichtbar bleibt.
        # Breite: 5 Minuten = 5/1440 Tage, 85% davon für leichten Abstand zwischen Kerzen.
        if not df_5m.empty and len(df_5m) >= 2:
            # Nur Kerzen im Zeitbereich des 1min-Charts anzeigen
            df_5m = df_5m[(df_5m.index >= df.index[0]) & (df_5m.index <= df.index[-1])]

            if len(df_5m) >= 1:
                for ts, row in df_5m.iterrows():
                    o, h, low, c = row['open'], row['high'], row['low'], row['close']
                    candle_is_up = c >= o
                    body_color = '#00ff88' if candle_is_up else '#ff3040'
                    body_alpha = 0.55
                    wick_alpha = 0.70

                    # Docht (High-Low) als dünne Linie
                    ax_price.plot([ts, ts], [low, h], color=body_color, linewidth=1.0, alpha=wick_alpha, zorder=2.3)

                    # Körper als Rectangle
                    body_low = min(o, c)
                    body_height = abs(c - o)
                    # Minimalhöhe damit doji-Kerzen sichtbar sind
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

        # === PREIS-LINIE (1min, Hauptfokus) ===
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

        # === SPIKE-MARKER (DEAKTIVIERT) ===
        # Wurde früher genutzt um after dem Fix der Bucket-Timestamp-Logik
        # visuell zu verifizieren dass die Spike-Zeitpunkte korrekt sind.
        # Da der Fix inzwischen validiert ist und in Produktion läuft,
        # brauchen wir die visuelle Bestätigung nicht mehr.
        #
        # Die Parameter spike_time/spike_start/spike_end bleiben in der
        # Signatur erhalten für Backwards-Kompatibilität mit bestehenden
        # Aufrufern — werden aber als no-op ignoriert.
        #
        # Falls die Linien doch nochmal gebraucht werden, siehe git history
        # für die ursprüngliche Implementation mit axvspan + zwei axvline.
        _ = (spike_start, spike_end, spike_time)  # explizit als ungenutzt markieren

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

        # === SPEICHERN ===
        # FIX (#67): Unique Filename mit Millisekunden-Timestamp.
        # Vorher fixer Pfad `charts/{symbol}_ai_chart.png` → Race Condition!
        # Wenn zwei Bots parallel für z.B. BTCUSDT einen Chart generieren, überschreiben
        # sie sich gegenseitig VOR dem Telegram-Versand. Der zweite Outbox-Eintrag
        # zeigt dann auf denselben Pfad, aber der Telegram-Bot löscht die Datei after
        # dem ersten Versand → FileNotFoundError beim zweiten ("Bild not found").
        import time as _t

        os.makedirs("charts", exist_ok=True)
        chart_path = f"charts/{symbol}_{int(_t.time() * 1000)}_ai_chart.png"
        plt.savefig(chart_path, format='png', dpi=150, facecolor="#0d0d0d", bbox_inches='tight')
        return chart_path

    except Exception as e:
        logger.error(f"Error for Chart-Generierung für {symbol}: {e}")
        return None

    finally:
        # Räumt alle Figure-Objekte auf — verhindert RAM-Leaks bei vielen Aufrufen.
        if fig is not None:
            plt.close(fig)
        plt.close('all')
