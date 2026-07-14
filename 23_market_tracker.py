import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")
warnings.filterwarnings("ignore", category=UserWarning, module="pandas_ta")

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from core import config as _kcfg  # channel ids
from core import shadow_gate
from core.bot_catalog import active_scripts, script_for_tag
from core.bot_naming import pretty_name

# --- Eigene DB Connection importieren ---
from core.database import get_db_connection
from core.market_utils import send_telegram
from core.realized_pnl import realized_pnl_pct

# 🛠️ CONFIGURATION
logging.basicConfig(level=logging.INFO, format='%(asctime)s - MARKET_TRACKER - %(message)s')
logger = logging.getLogger(__name__)

# 🔴 HIER DEINEN CHANNEL EINTRAGEN
TELEGRAM_CHANNEL_ID = _kcfg.CH_MARKET_DATA

EXCLUDED_COINS_FOR_TOTAL = ['BTCUSDT', 'XAUUSDT', 'XAGUSDT', 'PAXGUSDT', 'BTCDOMUSDT']
COINS_FILE = 'coins.json'

# ── Dedup fragments for the closed-trade tables (report-14 unique-index key) ──
# Single source for all four hourly queries — key and survivor order must stay
# in sync between job_signal_summary and job_per_bot_performance
# (T-2026-CU-9050-025). closed_ai_signals has no unique index and carries
# ~357k migration / LEGACY re-close rows (re-closes of the SAME trade with a
# different close_time/close_price); closed_trades_master ~11k (all verified
# identical-entry re-closes, no laddered trades). Survivor per group = the
# EARLIEST close (the original outcome; re-close artifacts came later); the
# remaining columns only break ties between rows with identical close times.
# Direction is uppercased inside the key — historical rows are not uniformly
# uppercase and all downstream comparisons are exact 'LONG'/'SHORT'.
#
# NOTE: open_time/time come from now() column defaults, and Postgres freezes
# now() per transaction — two same-key signals written in one commit would
# merge here (and would equally violate the planned report-14 unique index).
# No current writer does that (per-coin/direction cooldowns + dedupe).
AI_DEDUP_KEY = "symbol, model, upper(btrim(direction)), open_time"
AI_DEDUP_ORDER = f"{AI_DEDUP_KEY}, close_time ASC NULLS LAST, targets_hit DESC NULLS LAST, status ASC NULLS LAST"
CLS_DEDUP_KEY = "coin, strategy, upper(btrim(direction)), time"
CLS_DEDUP_ORDER = f"{CLS_DEDUP_KEY}, posted ASC NULLS LAST, status DESC NULLS LAST"


# ── Open AI signals ──────────────────────────────────────────────────────────
# P1.44: the OPEN side of the AI signals must come from `ai_signals`, never from
# `ml_predictions_master`. Two reasons, both of which silently inflated the
# "Opened" counts:
#
#   1. ml_predictions_master is an append-only prediction log — nothing ever
#      DELETEs from it. closed_ai_signals holds the same signals after they
#      close. Counting both meant every AI signal that opened AND closed inside
#      the window was counted TWICE as "opened".
#   2. It also carries shadow rows (posted=False, e.g. EPD2's 0.25..0.60 band,
#      see P1.41) that were never traded. Those are not opened signals.
#
# ai_signals and closed_ai_signals are per-signal and disjoint — the monitors
# DELETE from ai_signals on close (8_ai_trade_monitor.py:360) and INSERT into
# closed_ai_signals. That mirrors the classic path exactly
# (active_trades_master / closed_trades_master), and it is the shape
# job_per_bot_performance already used. Both posts now share this one query so
# they cannot drift apart.
#
# ai_signals has no open-time column; ml_predictions_master does. The LEFT JOIN
# recovers created_at and is intentionally fuzzy (coin/model/direction within
# 30 days) — a still-open signal whose prediction row was deduped away lands on
# a slightly older sibling row, and one with no row at all falls back to NOW().
# The alternative (ml_predictions_master as the opens source) is worse:
# log_prediction dedupes 4h per module/coin/direction, so it is a DEDUPED log,
# not a faithful one, and would undercount legitimate re-posts.
#
# `m.posted = TRUE` keeps a shadow row from supplying the open timestamp. Two
# groups therefore always take the NOW() fallback, and their still-open rows read
# as perpetually fresh in the Opened buckets. Display only — Kelly and WR consume
# closed_ai_signals.open_time, never this JOIN:
#   - bots that never write to ml_predictions_master at all (7, 18, 29, ROM1);
#     unchanged, they always fell back.
#   - ATB1 (14_ai_atb_bot.py:720), which writes posted=False even on its LIVE
#     branch — before the filter it accidentally matched its own shadow row.
#     ATB1 is parked, so this is bounded to legacy open rows. Root cause tracked
#     as P1.47; it must be fixed there, not by weakening this filter.
OPEN_AI_SIGNALS_QUERY = """
    SELECT a.model as strategy, a.direction, a.entry1 as entry,
           COALESCE(m.time, NOW() AT TIME ZONE 'UTC') as created_at
    FROM ai_signals a
    LEFT JOIN ml_predictions_master m
      ON m.coin = a.symbol AND m.model_name = a.model
     AND m.direction = a.direction
     AND m.posted = TRUE
     AND m.time >= NOW() AT TIME ZONE 'UTC' - INTERVAL '30 days'
    WHERE a.entry1 IS NOT NULL
"""

OPEN_AI_SIGNALS_FALLBACK_QUERY = """
    SELECT model as strategy, direction, entry1 as entry,
           NOW() AT TIME ZONE 'UTC' as created_at
    FROM ai_signals
    WHERE entry1 IS NOT NULL
"""


def _load_open_ai_signals(conn) -> pd.DataFrame:
    """Open AI signals with a best-effort created_at. Never raises on the JOIN."""
    try:
        df = pd.read_sql_query(OPEN_AI_SIGNALS_QUERY, conn)
        # Der JOIN kann pro Signal mehrere ml-Zeilen treffen — neueste gewinnt.
        if not df.empty and 'created_at' in df.columns:
            df = df.sort_values('created_at').drop_duplicates(subset=['strategy', 'direction', 'entry'], keep='last')
        return df
    except Exception as e:
        logger.debug(f"ai_signals JOIN fehlgeschlagen: {e} — nutze Fallback")
        # Postgres aborts the whole transaction on a failed statement; without
        # this rollback the fallback dies with InFailedSqlTransaction — i.e. the
        # fallback never actually fell back.
        conn.rollback()
        return pd.read_sql_query(OPEN_AI_SIGNALS_FALLBACK_QUERY, conn)


# 📡 DATABASE & HELPERS


def load_all_altcoins():
    try:
        with open(COINS_FILE) as f:
            data = json.load(f)
            coin_list = data.get('coins', data) if isinstance(data, dict) else data
            return [
                c.upper() for c in coin_list if c.upper().endswith("USDT") and c.upper() not in EXCLUDED_COINS_FOR_TOTAL
            ]
    except Exception as e:
        logger.error(f"Error loading von {COINS_FILE}: {e}")
        return []


def format_money(val):
    if val >= 1e9:
        return f"${val / 1e9:.2f}B"
    if val >= 1e6:
        return f"${val / 1e6:.2f}M"
    if val >= 1e3:
        return f"${val / 1e3:.0f}K"
    return f"${val:,.0f}"


def get_color(val, reverse=False):
    if reverse:
        return "lime" if val < 0 else "red"
    return "lime" if val >= 0 else "red"


# 🚀 1. MAIN VOLUME REPORT (BTC, ETH, TOTAL)
async def get_volume_data(symbols, hours_ago):
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours_ago)
    usd_totals, buy_totals, sell_totals = 0.0, 0.0, 0.0

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                for sym in symbols:
                    # FIX (#72): `volume * close` war eine Näherung (volume ist Coin-Menge,
                    # close ist End-Preis). Binance liefert quote_volume direkt, aber das
                    # ist nicht in unserer DB saved. Als bessere Näherung nutzen wir
                    # jetzt den MID-Preis (open + close)/2 statt nur close — reduziert
                    # Error for Kerzen mit großer Intra-Candle-Bewegung.
                    query = f"""
                        SELECT
                            COALESCE(SUM(volume * (open + close) / 2), 0),
                            COALESCE(SUM(CASE WHEN close >= open THEN volume * (open + close) / 2 ELSE 0 END), 0),
                            COALESCE(SUM(CASE WHEN close < open THEN volume * (open + close) / 2 ELSE 0 END), 0)
                        FROM "{sym}_30m" WHERE open_time >= %s AND open_time <= %s
                    """
                    try:
                        cur.execute(query, (start, now))
                        row = cur.fetchone()
                        if row:
                            usd_totals += float(row[0])
                            buy_totals += float(row[1])
                            sell_totals += float(row[2])
                    except Exception:
                        conn.rollback()
    except Exception:
        pass
    return usd_totals, buy_totals, sell_totals


async def get_price_change(symbols, hours_ago):
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours_ago)
    total_change, valid_coins = 0.0, 0

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                for sym in symbols:
                    try:
                        cur.execute(
                            f'SELECT close FROM "{sym}_30m" WHERE open_time >= %s ORDER BY open_time ASC LIMIT 1',
                            (start,),
                        )
                        r_old = cur.fetchone()
                        cur.execute(f'SELECT close FROM "{sym}_30m" ORDER BY open_time DESC LIMIT 1')
                        r_new = cur.fetchone()
                        if r_old and r_new and float(r_old[0]) > 0:
                            total_change += ((float(r_new[0]) - float(r_old[0])) / float(r_old[0])) * 100
                            valid_coins += 1
                    except Exception:
                        conn.rollback()
    except Exception:
        pass
    return (total_change / valid_coins) if valid_coins > 0 else 0.0


async def generate_main_report(target_name):
    is_total = target_name == "TOTAL ALT MARKET"
    symbols = load_all_altcoins() if is_total else [target_name]

    ch_1h = await get_price_change(symbols, 1)
    ch_4h = await get_price_change(symbols, 4)
    ch_24h = await get_price_change(symbols, 24)
    ch_7d = await get_price_change(symbols, 7 * 24)

    periods = {'1h': 1, '4h': 4, '24h': 24, '7d': 7 * 24, '30d': 30 * 24}
    v_data, usd_30d = {}, 0

    for name, h in periods.items():
        u, b, s = await get_volume_data(symbols, h)
        v_data[name] = {'usd': u, 'diff': b - s}
        if name == '30d':
            usd_30d = u

    avg_daily = usd_30d / 30 if usd_30d > 0 else 1
    for name, data in v_data.items():
        exp = avg_daily * (periods[name] / 24)
        data['pct'] = ((data['usd'] / exp) - 1) * 100 if exp > 0 else 0

    def f_p(x):
        return f"{x:+.2f}%"

    emoji = "💎" if target_name == "BTCUSDT" else "💠" if target_name == "ETHUSDT" else "🔥"

    html = f"""<pre>
{emoji} <b>VOL & PRICE: {target_name}</b>

<b>PRICE CHANGES (AVG)</b>
 1h: <b>{f_p(ch_1h)}</b>  |  4h: <b>{f_p(ch_4h)}</b>
24h: <b>{f_p(ch_24h)}</b>  |  7d: <b>{f_p(ch_7d)}</b>

<b>VOLUME ACTIVITY</b>
 1h: <b>{format_money(v_data['1h']['usd'])}</b> (<b>{f_p(v_data['1h']['pct'])}</b>)
 4h: <b>{format_money(v_data['4h']['usd'])}</b> (<b>{f_p(v_data['4h']['pct'])}</b>)
24h: <b>{format_money(v_data['24h']['usd'])}</b> (<b>{f_p(v_data['24h']['pct'])}</b>)

<b>BUY vs SELL (NET DIFFERENCE)</b>
 1h: <b>{format_money(v_data['1h']['diff'])}</b>
 4h: <b>{format_money(v_data['4h']['diff'])}</b>
24h: <b>{format_money(v_data['24h']['diff'])}</b>
 7d: <b>{format_money(v_data['7d']['diff'])}</b>
</pre>"""
    send_telegram(html.strip(), TELEGRAM_CHANNEL_ID)


async def job_main_reports():
    logger.info("Generiere Main Reports...")
    await generate_main_report("BTCUSDT")
    await asyncio.sleep(1)
    await generate_main_report("ETHUSDT")
    await asyncio.sleep(1)
    await generate_main_report("TOTAL ALT MARKET")
    await asyncio.sleep(1)


# 🚀 2. TOP GAINERS & LOSERS
async def job_gainers_losers():
    logger.info("Generiere Gainers/Losers...")
    coins = load_all_altcoins()
    now = datetime.now(timezone.utc)
    t1, t4, t24 = now - timedelta(hours=1), now - timedelta(hours=4), now - timedelta(hours=24)
    stats = []

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                for sym in coins:
                    try:
                        # Holt die gesamten letzten 24h für den Coin in einem Rutsch
                        cur.execute(
                            f'SELECT open_time, close FROM "{sym}_30m" WHERE open_time >= %s ORDER BY open_time ASC',
                            (t24,),
                        )
                        rows = cur.fetchall()
                        if not rows:
                            continue
                        df = pd.DataFrame(rows, columns=['ot', 'c'])

                        curr_p = df['c'].iloc[-1]
                        p24 = df['c'].iloc[0]

                        df_4h = df[df['ot'] >= t4]
                        p4 = df_4h['c'].iloc[0] if not df_4h.empty else curr_p

                        df_1h = df[df['ot'] >= t1]
                        p1 = df_1h['c'].iloc[0] if not df_1h.empty else curr_p

                        c1 = ((curr_p - p1) / p1) * 100 if p1 > 0 else 0
                        c4 = ((curr_p - p4) / p4) * 100 if p4 > 0 else 0
                        c24 = ((curr_p - p24) / p24) * 100 if p24 > 0 else 0

                        stats.append({'sym': sym.replace("USDT", ""), '1h': c1, '4h': c4, '24h': c24})
                    except Exception:
                        conn.rollback()
    except Exception:
        pass

    if not stats:
        return

    def build_list(tf, is_gain):
        s = sorted(stats, key=lambda x: x[tf], reverse=is_gain)[:10]
        lines = []
        for i, c in enumerate(s, 1):
            val = c[tf]
            sign = "+" if val > 0 else ""
            lines.append(f"<b>{i:02d}</b> <b>{c['sym']:<9}</b> <b>{sign}{val:.2f}%</b>")
        return "\n".join(lines)

    # GAINERS MSG
    msg_g = f"""<pre>
🚀 <b>TOP 10 GAINERS</b> 🚀

<b>⏱️ LAST 1 HOUR</b>
{build_list('1h', True)}

<b>⏱️ LAST 4 HOURS</b>
{build_list('4h', True)}

<b>⏱️ LAST 24 HOURS</b>
{build_list('24h', True)}
</pre>"""
    send_telegram(msg_g.strip(), TELEGRAM_CHANNEL_ID)
    await asyncio.sleep(1)

    # LOSERS MSG
    msg_l = f"""<pre>
💥 <b>TOP 10 LOSERS</b> 💥

<b>⏱️ LAST 1 HOUR</b>
{build_list('1h', False)}

<b>⏱️ LAST 4 HOURS</b>
{build_list('4h', False)}

<b>⏱️ LAST 24 HOURS</b>
{build_list('24h', False)}
</pre>"""
    send_telegram(msg_l.strip(), TELEGRAM_CHANNEL_ID)
    await asyncio.sleep(1)


# 🚀 3. VOLUME SPIKES (4h vs 7d Avg)
async def job_volume_spikes():
    logger.info("Generiere Volume Spikes...")
    now = datetime.now(timezone.utc)
    t4 = now - timedelta(hours=4)
    t7 = now - timedelta(days=7)
    coins = load_all_altcoins()
    spikes = []

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                for sym in coins:
                    try:
                        cur.execute(
                            f'SELECT SUM(volume), (SELECT close FROM "{sym}_30m" ORDER BY open_time DESC LIMIT 1) FROM "{sym}_30m" WHERE open_time >= %s',
                            (t4,),
                        )
                        r = cur.fetchone()
                        if not r or not r[0]:
                            continue

                        usd_4h = float(r[0]) * float(r[1])
                        if usd_4h < 250000:
                            continue  # Mindestens 250k Volumen

                        cur.execute(
                            f'SELECT SUM(volume) FROM "{sym}_30m" WHERE open_time >= %s AND open_time < %s', (t7, t4)
                        )
                        r7 = cur.fetchone()
                        if not r7 or not r7[0]:
                            continue

                        avg_4h_vol_over_7d = float(r7[0]) / 42.0  # 7 Tage = 42x 4h-Perioden
                        if avg_4h_vol_over_7d <= 0:
                            continue

                        ratio = float(r[0]) / avg_4h_vol_over_7d
                        if ratio >= 2.5:  # Zeigt alles ab 2.5x
                            spikes.append({'sym': sym.replace("USDT", ""), 'rat': ratio, 'usd': usd_4h})
                    except Exception:
                        conn.rollback()
    except Exception:
        pass

    if not spikes:
        return
    spikes = sorted(spikes, key=lambda x: x['rat'], reverse=True)[:10]

    lines = []
    for i, s in enumerate(spikes, 1):
        lines.append(f"<b>{i:02d}</b> <b>{s['sym']:<8}</b> <b>{s['rat']:>5.1f}x</b> {format_money(s['usd']):>7}")

    msg = f"""<pre>
🌊 <b>TOP 10 VOLUME SPIKES</b> 🌊
<b>Last 4h vs 7d Average</b>

<b>#  COIN      SPIKE    4h USD</b>
{chr(10).join(lines)}
</pre>"""
    send_telegram(msg.strip(), TELEGRAM_CHANNEL_ID)
    await asyncio.sleep(1)


# 🚀 4. VOLATILE COINS (Last 4h)
async def job_volatile_coins():
    logger.info("Generiere Volatile Coins...")
    now = datetime.now(timezone.utc)
    t4 = now - timedelta(hours=4)
    coins = load_all_altcoins()
    volatile = []

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                for sym in coins:
                    try:
                        cur.execute(
                            f'SELECT MAX(high), MIN(low), (SELECT close FROM "{sym}_30m" ORDER BY open_time DESC LIMIT 1) FROM "{sym}_30m" WHERE open_time >= %s',
                            (t4,),
                        )
                        r = cur.fetchone()
                        if not r or not r[0] or not r[1]:
                            continue

                        h, low, c = float(r[0]), float(r[1]), float(r[2])
                        if low <= 0:
                            continue

                        range_pct = ((h - low) / low) * 100
                        if range_pct >= 5.0:  # Zeigt alles über 5% Range
                            volatile.append({'sym': sym.replace("USDT", ""), 'r': range_pct, 'h': h, 'l': low, 'c': c})
                    except Exception:
                        conn.rollback()
    except Exception:
        pass

    if not volatile:
        return
    volatile = sorted(volatile, key=lambda x: x['r'], reverse=True)[:15]

    lines = []
    for i, v in enumerate(volatile, 1):
        trend = "UP" if v['c'] > v['l'] * 1.02 else "DOWN"
        lines.append(f"<b>{i:02d}</b> <b>{v['sym']:<8}</b> <b>{v['r']:>5.1f}%</b> <b>{trend}</b>")

    msg = f"""<pre>
⚡ <b>TOP 15 VOLATILE COINS</b> ⚡
<b>Last 4h Price Range</b>

<b>#  COIN      RANGE    TREND</b>
{chr(10).join(lines)}
</pre>"""
    send_telegram(msg.strip(), TELEGRAM_CHANNEL_ID)
    await asyncio.sleep(1)


# 🚀 5. HOURLY SIGNAL SUMMARY (Alle Bots)
async def job_signal_summary():
    logger.info("Generiere stündliche Bot-signals Zusammenfassung...")
    now = datetime.now(timezone.utc)
    t24 = now - timedelta(hours=24)

    try:
        # `with` (not a bare close() at the end of the try): PooledConnection
        # returns the connection to the pool on __exit__, so a raising query no
        # longer burns a pool slot. Pool max is 8 per process — a leak per failed
        # run silently starves the tracker after a handful of DB hiccups.
        with get_db_connection() as conn:
            # 1. OFFENE TRADES HOLEN (Hier gibt es naturgemäß nur den Eröffnungszeitpunkt)
            query_act_trades = (
                "SELECT strategy, direction, time as created_at FROM active_trades_master WHERE time >= %s"
            )
            df_act_trades = pd.read_sql_query(query_act_trades, conn, params=(t24,))

            # P1.44: open AI signals come from ai_signals, NOT from
            # ml_predictions_master (append-only + shadow rows → double-counted
            # and inflated "Opened"). See OPEN_AI_SIGNALS_QUERY at module top.
            # No 24h filter needed: ai_signals only ever holds the still-open
            # set, and get_o_stats windows on created_at client-side anyway.
            df_act_ai = _load_open_ai_signals(conn)

            # 2. GESCHLOSSENE TRADES HOLEN
            # Wir fragen alles ab, was entweder in den letzten 24h geschlossen ODER eröffnet wurde
            # Queries erweitert um entry/close_price für PnL-basierte is_win-Klassifikation
            # Dedupe on the report-14 key (see AI_DEDUP_KEY/CLS_DEDUP_KEY at module
            # top). Structure: dedup runs over the FULL table first, filters come
            # OUTSIDE — so the survivor pick (earliest close = original outcome)
            # is independent of the 24h window and the price-validity filter. A
            # window-first query would let a future re-close event resurface
            # months-old trades as freshly closed (only the artifact falls inside
            # the window). entry/close_price > 0 matches job_per_bot_performance —
            # v1-era rows carry close_price=0 and would otherwise score SHORTs as
            # +100% wins. Live-verified 2026-07-09: with current data, filter
            # placement does not change a single surviving row.
            query_cls_trades = f"""
                SELECT * FROM (
                    SELECT DISTINCT ON ({CLS_DEDUP_KEY})
                           strategy, upper(btrim(direction)) as direction, entry, close_price,
                           time as created_at, posted as closed_at, status
                    FROM closed_trades_master
                    ORDER BY {CLS_DEDUP_ORDER}
                ) d
                WHERE (d.closed_at >= %s OR d.created_at >= %s)
                  AND d.entry > 0 AND d.close_price > 0
            """
            df_cls_trades = pd.read_sql_query(query_cls_trades, conn, params=(t24, t24))

            query_cls_ai = f"""
                SELECT * FROM (
                    SELECT DISTINCT ON ({AI_DEDUP_KEY})
                           model as strategy, upper(btrim(direction)) as direction, entry, close_price,
                           open_time as created_at, close_time as closed_at, targets_hit,
                           status as close_reason
                    FROM closed_ai_signals
                    WHERE status IS DISTINCT FROM 'ENTRY_NOT_FILLED'
                    ORDER BY {AI_DEDUP_ORDER}
                ) d
                WHERE (d.closed_at >= %s OR d.created_at >= %s)
                  AND d.entry > 0 AND d.close_price > 0
            """
            df_cls_ai = pd.read_sql_query(query_cls_ai, conn, params=(t24, t24))
    except Exception as e:
        logger.error(f"Error loading der Signal-Daten: {e}")
        return

    # Belt-and-braces for the frames whose direction is not normalised in SQL
    # (active_trades_master / ml_predictions_master).
    for df in [df_act_trades, df_act_ai]:
        if not df.empty and 'direction' in df.columns:
            df['direction'] = df['direction'].astype(str).str.strip().str.upper()

    # --- DATEN AUFBEREITEN & GEWINNE ERMITTELN ---

    # PnL-basierte is_win-Klassifikation (statt direkt aus status/targets_hit).
    # Das umgeht die bekannten Bugs:
    #   - LEGACY TARGET HIT (+2.5%) schreibt targets_hit=0 → wurde als Loss gewertet
    #   - DELISTED/CLEANUP zählt nicht als Loss, sondern als neutral
    #   - Ausreißer mit |pnl| > 100% sind Daten-Bugs und werden neutral
    #   - |pnl| <= 0.1% sind Housekeeping-Closes, neutral
    OUTCOME_MIN_PNL_PCT = 0.1
    OUTCOME_MAX_ABS_PNL_PCT = 100.0

    def _compute_outcome_flags(df, has_close_reason: bool):
        """Setzt df['is_win'] und df['is_decisive'] (= Win ODER Loss, keine Neutrale).
        Nutzt pnl_pct wenn entry+close_price vorhanden, sonst Fallback auf
        status/targets_hit wie vorher."""
        if df.empty:
            return
        # PnL berechnen falls möglich
        if 'entry' in df.columns and 'close_price' in df.columns:
            entry = pd.to_numeric(df['entry'], errors='coerce')
            close = pd.to_numeric(df['close_price'], errors='coerce')
            priced = entry.notna() & close.notna()
            # close > 0 in addition to entry > 0: v1-era rows (pre-2026-03)
            # carry close_price=0 — without the guard a SHORT there scores as
            # a +100% win (a LONG as a -100% loss). Rows whose price is
            # present but unusable are NEUTRAL, not fallback material: the
            # status/targets fallback would resurrect exactly the writer bugs
            # the PnL-based path was built to bypass.
            valid = priced & (entry > 0) & (close > 0)
            bad_price = priced & ~valid
            pct = (close - entry) / entry * 100
            is_short = df['direction'] == 'SHORT'
            pnl_pct = pct.where(~is_short, -pct)
            pnl_pct = pnl_pct.where(valid, other=pd.NA)
            df['pnl_pct'] = pnl_pct
        else:
            df['pnl_pct'] = pd.NA
            bad_price = pd.Series(False, index=df.index)

        # close_reason-Spalte (leer falls not loaded)
        if has_close_reason and 'close_reason' in df.columns:
            reason_upper = df['close_reason'].fillna('').astype(str).str.upper()
        else:
            reason_upper = pd.Series([''] * len(df), index=df.index)

        # B9-Zensur-Korrektur (T-2026-CU-9050-048): REGIME_CHANGE raus — ein
        # Auto-Close realisiert einen echten PnL und zählt als Win/Loss (nur
        # near-0%-Closes bleiben über den Micro-PnL-Filter neutral). Ohne das
        # divergierte die Report-WR von der (jetzt korrigierten) Whitelist-WR.
        is_housekeeping = reason_upper.str.contains('DELISTED|CLEANUP|ORPHAN', regex=True, na=False)

        # PnL-basierte Klassifikation (mit Fallback)
        pnl_num = pd.to_numeric(df['pnl_pct'], errors='coerce')
        has_pnl = pnl_num.notna()
        abs_pnl = pnl_num.abs()

        is_outlier = has_pnl & (abs_pnl > OUTCOME_MAX_ABS_PNL_PCT)
        is_micro = has_pnl & (abs_pnl <= OUTCOME_MIN_PNL_PCT)
        is_neutral = is_housekeeping | is_outlier | is_micro

        # Win/Loss nur für nicht-neutrale Trades
        is_win_by_pnl = has_pnl & (~is_neutral) & (pnl_num > 0)
        is_loss_by_pnl = has_pnl & (~is_neutral) & (pnl_num < 0)

        # Fallback für Zeilen ohne PnL (entry/close_price fehlen):
        # Nutze die alte status/targets_hit-Logik, aber immer noch DELISTED
        # als neutral behandeln.
        if 'status' in df.columns:
            fallback_win = pd.to_numeric(df['status'], errors='coerce').fillna(0) > 0
        elif 'targets_hit' in df.columns:
            fallback_win = pd.to_numeric(df['targets_hit'], errors='coerce').fillna(0) > 0
        else:
            fallback_win = pd.Series([False] * len(df), index=df.index)

        # Kombiniere: PnL wenn vorhanden, sonst Fallback (außer housekeeping
        # und bad_price — letztere sind neutral, siehe oben)
        is_win = is_win_by_pnl.copy()
        no_pnl = ~has_pnl & ~is_housekeeping & ~bad_price
        is_win = is_win | (no_pnl & fallback_win)

        df['is_win'] = is_win
        df['is_decisive'] = is_win_by_pnl | is_loss_by_pnl | (no_pnl & ~is_housekeeping)

    if not df_cls_trades.empty:
        _compute_outcome_flags(df_cls_trades, has_close_reason=False)
    if not df_cls_ai.empty:
        _compute_outcome_flags(df_cls_ai, has_close_reason=True)

    # Zeitstempel strikt in UTC umwandeln
    for df in [df_act_trades, df_act_ai, df_cls_trades, df_cls_ai]:
        if not df.empty and 'created_at' in df.columns:
            df['created_at'] = pd.to_datetime(df['created_at'], utc=True)

    for df in [df_cls_trades, df_cls_ai]:
        if not df.empty and 'closed_at' in df.columns:
            df['closed_at'] = pd.to_datetime(df['closed_at'], utc=True)
    # Opens = still-open ∪ closed, per Signal-Typ. Beide Paare sind disjunkt,
    # weil die Monitore beim Schliessen aus active_trades_master bzw. ai_signals
    # DELETEn und in die closed_*-Tabelle INSERTen. Vor P1.44 stand hier auf der
    # AI-Seite ml_predictions_master (append-only) statt ai_signals — jedes
    # AI-Signal, das im Fenster öffnete UND schloss, zählte doppelt.
    df_all_created = pd.concat([df_act_trades, df_act_ai, df_cls_trades, df_cls_ai], ignore_index=True)
    df_all_closed = pd.concat([df_cls_trades, df_cls_ai], ignore_index=True)

    # --- KATEGORIEN ZUWEISEN ---
    def get_category(strategy):
        """FIX (#71/#73): Kategorisierung war inkonsistent.
        - TD_* (Three-Drive aus SMC Sniper) wurde fälschlich als INDICATOR eingeordnet
          obwohl es ein Pattern ist.
        - BB_* (Breaker Block) und QM_* (Quasimodo) waren als VOLUME klassifiziert
          obwohl sie struktur-/pattern-basiert sind.
        Jetzt saubere Zuordnung after Signal-Typ.
        """
        s = str(strategy).upper()
        # Versionierungs-Regel (Operator 2026-07-06): Retrain-Generationen posten
        # unter neuem Tag (MIS2, ABR2, ATS2, ...) — deshalb Präfix-Matching statt
        # Exakt-Listen, damit neue Generationen automatisch kategorisiert werden.
        # INDICATOR = klassische Oszillator-/Crossover-basierte signals
        if s in ["5 PERCENT", "FAST IN AND OUT"] or s.startswith(("MIS", "ATS")):
            return "INDICATOR"
        # VOLUME = rein volumen-basierte signals
        if s == "VOLUME INDICATOR" or s.startswith("EPD"):
            return "VOLUME"
        # LEVEL = Support/Resistance & Reversion an Zonen
        if s == "SUPPORT RESISTANCE" or s.startswith(("ABR", "RUB", "SRA")):
            return "LEVEL"
        # PATTERN = SMC-Patterns, Chart-Patterns, Trendline
        if s.startswith(("AIM", "ATB", "BR", "TD", "BB", "QM", "SMC")):
            return "PATTERN"
        return "OTHER"

    if not df_all_created.empty:
        df_all_created['category'] = df_all_created['strategy'].apply(get_category)
    if not df_all_closed.empty:
        df_all_closed['category'] = df_all_closed['strategy'].apply(get_category)

    # --- STATISTIKEN BERECHNEN ---
    def calc_stats(cat_name):
        cat_created = (
            df_all_created[df_all_created['category'] == cat_name] if not df_all_created.empty else pd.DataFrame()
        )
        cat_closed = df_all_closed[df_all_closed['category'] == cat_name] if not df_all_closed.empty else pd.DataFrame()

        def get_o_stats(hours):
            if cat_created.empty:
                return 0, 0, "0.0"
            t_limit = now - timedelta(hours=hours)
            sub = cat_created[cat_created['created_at'] >= t_limit]
            n_long = len(sub[sub['direction'] == 'LONG'])
            s = len(sub[sub['direction'] == 'SHORT'])
            ratio = "∞" if s == 0 and n_long > 0 else "0.0" if s == 0 else f"{n_long / s:.1f}"
            return n_long, s, ratio

        def get_c_stats(hours):
            if cat_closed.empty:
                return "0.0", "0.0"
            t_limit = now - timedelta(hours=hours)
            sub = cat_closed[cat_closed['closed_at'] >= t_limit]

            l_sub = sub[sub['direction'] == 'LONG']
            s_sub = sub[sub['direction'] == 'SHORT']

            # WR nur aus "entschiedenen" Trades (Wins+Losses), Neutrale (DELISTED,
            # Housekeeping, Ausreißer) ausgeschlossen. Sonst bekommen Bots mit
            # viel Cleanup-Traffic irreführend niedrige WR.
            l_dec = l_sub[l_sub['is_decisive']] if 'is_decisive' in l_sub.columns else l_sub
            s_dec = s_sub[s_sub['is_decisive']] if 'is_decisive' in s_sub.columns else s_sub

            l_win = (len(l_dec[l_dec['is_win']]) / len(l_dec) * 100) if len(l_dec) > 0 else 0.0
            s_win = (len(s_dec[s_dec['is_win']]) / len(s_dec) * 100) if len(s_dec) > 0 else 0.0
            return f"{l_win:.1f}", f"{s_win:.1f}"

        o1_l, o1_s, o1_r = get_o_stats(1)
        o4_l, o4_s, o4_r = get_o_stats(4)
        o24_l, o24_s, o24_r = get_o_stats(24)

        c1_l, c1_s = get_c_stats(1)
        c4_l, c4_s = get_c_stats(4)
        c24_l, c24_s = get_c_stats(24)

        return f"""<b>Opened:</b>
1h : 🟢 {o1_l}L / 🔴 {o1_s}S (Ratio: {o1_r})
4h : 🟢 {o4_l}L / 🔴 {o4_s}S (Ratio: {o4_r})
24h: 🟢 {o24_l}L / 🔴 {o24_s}S (Ratio: {o24_r})
<b>Closed:</b>
1h : 🟢 L: {c1_l}% Hit / 🔴 S: {c1_s}% Hit
4h : 🟢 L: {c4_l}% Hit / 🔴 S: {c4_s}% Hit
24h: 🟢 L: {c24_l}% Hit / 🔴 S: {c24_s}% Hit"""

    # --- NACHRICHT ZUSAMMENBAUEN ---
    msg = f"""<pre>
📊 <b>BOT SIGNAL SUMMARY</b> 📊

⚙️ <b>INDICATOR BASED</b>
{calc_stats("INDICATOR")}
────────────────────────
🌊 <b>VOLUME BASED</b>
{calc_stats("VOLUME")}
────────────────────────
🧱 <b>LEVEL BASED</b>
{calc_stats("LEVEL")}
────────────────────────
📐 <b>PATTERN BASED</b>
{calc_stats("PATTERN")}
</pre>"""

    send_telegram(msg, TELEGRAM_CHANNEL_ID)
    logger.info("✅ Stündliche Signal-Zusammenfassung sent successfully.")
    await asyncio.sleep(1)


# ═══════════════════════════════════════════════════════════════════════════
# PER-BOT PERFORMANCE DETAIL POST
# ═══════════════════════════════════════════════════════════════════════════
#
# Ergänzt die bestehende kategorie-basierte Summary um eine Tabelle pro
# einzelnem Bot (Strategy). Zeigt Win-Rate über 5 Zeitfenster
# (1h/4h/24h/7d/All) sowie durchschnittliche PnL in %.
#
# Design-Entscheidungen:
# - Min. 3 Trades pro Zeitfenster damit eine Zahl angezeigt wird (sonst "---").
# - Trend-Pfeile: 1h-Wert vs. All-Zeit-Wert ≥ 10 Prozentpunkte Abweichung →
#   ↑ (heiß) oder ↓ (kalt).
# - Alle Bots in einer Tabelle, sortiert after Gesamt-Trade-Zahl (Bots mit mehr
#   Historie zuerst — aussagekräftiger).
# - Wird 5 Sekunden after der Kategorie-Summary gesendet damit der Telegram-
#   Worker Luft bekommt und nicht 2 Posts im selben Takt in den gleichen Channel.


def _get_regime_fit_label(conn, bot_name: str) -> str:
    """
    Returns a human-readable regime fit label for a bot in the current BTC-regime.
    Graceful degradation: returns '---' if the regime orchestrator is not deployed
    or tables don't exist yet. Never raises — market tracker must not crash.

    Examples:
        'CHOP 58% (n=145), Overall 59% → NEUTRAL'
        'TREND_UP 72% (n=80), Overall 61% → STRONG'
        '--- (insufficient data)'
        '---'
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT regime FROM regime_current WHERE id = 1")
            row = cur.fetchone()
        if row is None:
            return "---"
        cur_regime = row[0]

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT n_trades, win_rate FROM bot_regime_performance
                WHERE bot_name = %s AND regime = %s
                  AND alt_context = 'ALL' AND direction = 'BOTH'
                  AND window_days = 30
                """,
                (bot_name, cur_regime),
            )
            regime_row = cur.fetchone()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT n_trades, win_rate FROM bot_regime_performance
                WHERE bot_name = %s AND regime = 'ALL'
                  AND alt_context = 'ALL' AND direction = 'BOTH'
                  AND window_days = 30
                """,
                (bot_name,),
            )
            overall_row = cur.fetchone()

        if regime_row is None or overall_row is None:
            return "---"

        n_regime, wr_regime = regime_row
        _, wr_overall = overall_row
        if wr_regime is None or wr_overall is None:
            return "---"

        if n_regime < 30:
            return f"{cur_regime} n={n_regime} → --- (insufficient data)"

        diff = wr_regime - wr_overall
        if diff >= 10.0:
            label = "STRONG ↑"
        elif diff <= -10.0:
            label = "WEAK ↓"
        else:
            label = "NEUTRAL"

        return f"{cur_regime} {wr_regime:.0f}% (n={n_regime}), Overall {wr_overall:.0f}% → {label}"

    except Exception:
        # The caller reuses ONE connection for all bots. A failed statement
        # leaves the transaction aborted, so without this rollback every
        # subsequent bot's lookup dies with InFailedSqlTransaction and the
        # whole column degrades to '---' after the first hiccup.
        try:
            conn.rollback()
        except Exception:
            pass
        return "---"


# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM MESSAGE CHUNKING (shared by the per-bot performance post)
# ═══════════════════════════════════════════════════════════════════════════
#
# Telegram rejects any message over 4096 chars, and send_telegram only queues
# into telegram_outbox — the dispatcher (4_telegram_bot) drops an over-limit
# message silently. The per-bot post is therefore split into several messages
# on ENTRY boundaries so a bot/table entry is never torn across two messages.
#
# These were nested inside job_per_bot_performance; lifted to module scope so
# the split logic is unit-testable without a DB (backtest/test_market_tracker_
# chunker.py). They are pure — no closure over job-local state.

TELEGRAM_TEXT_LIMIT = 4096
SAFETY_BUFFER = 200  # headroom for HTML tags + unforeseen characters


def _group_bot_entries(src_lines: list[str]) -> list[str]:
    """Gruppiert Kelly-Zeilen zu Bot-Blöcken (getrennt durch Leerzeilen).

    Ein Bot-Eintrag darf NIEMALS über Chunks gesplittet werden — sonst
    endet der Post mit einem halben Eintrag.
    """
    blocks = []
    current = []
    for ln in src_lines:
        if ln == "":
            if current:
                blocks.append("\n".join(current))
                current = []
        else:
            current.append(ln)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _group_table_entries(src_lines: list[str]) -> list[str]:
    """Gruppiert Tabellen-Zeilen zu Bot-Blöcken.

    Die Tabelle ist ähnlich aufgebaut wie der Kelly-Block: jede
    Bot-Zeile (plus optional 1-3 Detail-Zeilen) endet mit einer
    Leerzeile. Die ersten beiden Zeilen (Header + Separator) müssen
    in JEDEM Chunk dabei sein — Tabellen ohne Header wären unlesbar.
    """
    if len(src_lines) < 2:
        return []
    # Erste zwei Zeilen sind Header + Separator → separat halten,
    # die packen wir in jedes Chunk-Header mit rein.
    blocks = []
    current = []
    for ln in src_lines[2:]:
        if ln == "":
            if current:
                blocks.append("\n".join(current))
                current = []
        else:
            current.append(ln)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _hard_split_block(block: str, budget: int) -> list[str]:
    """Last-resort split of a SINGLE oversized entry so no chunk exceeds the
    Telegram limit.

    Normal bot/table entries sit far under budget; _build_chunks only reaches
    for this on a pathological entry (e.g. a corrupted strategy name or a bot
    with an abnormal detail block). Without it, one over-budget block is emitted
    as a single >4096-char chunk and Telegram drops the whole message silently.
    Splits on line boundaries first, then on hard character boundaries for a
    single over-long line — the post degrades into several messages instead of
    vanishing. Every returned piece is guaranteed <= budget.
    """
    if budget <= 0 or len(block) <= budget:
        return [block]
    pieces: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in block.split("\n"):
        if len(line) > budget:
            # A single line longer than the budget — flush, then char-split it.
            if current:
                pieces.append("\n".join(current))
                current = []
                current_len = 0
            for i in range(0, len(line), budget):
                pieces.append(line[i : i + budget])
            continue
        add = len(line) + (1 if current else 0)  # +1 for the rejoining "\n"
        if current and current_len + add > budget:
            pieces.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += add
    if current:
        pieces.append("\n".join(current))
    return pieces


def _build_chunks(
    blocks: list[str],
    header_first: str,
    header_continued: str,
    footer: str,
    separator: str = "\n\n",
) -> list[str]:
    """Packt Bot-Blöcke in mehrere Chunks unter dem 4096-Zeichen-Limit.

    - header_first: Header für den allerersten Chunk (volle Überschrift)
    - header_continued: Header für Folge-Chunks ("continued" Markierung)
    - footer: Footer für jeden Chunk (Legende etc.)
    - separator: Trenner zwischen Blöcken ("\\n\\n" für Bot-Blöcke mit
      Leerzeile, "\\n" für einzeilige Tabellen-Rows)
    """
    if not blocks:
        return []

    # A single block that alone exceeds the per-chunk body budget would be
    # emitted as one over-limit chunk and then silently dropped by Telegram
    # (>4096 chars). Normal entries sit far under budget; this only trips on a
    # pathological one. Split such a block on line/char boundaries FIRST so
    # every emitted chunk stays legal. Budget uses the larger of the two headers
    # so a split piece fits whether it lands in the first or a continued chunk.
    body_budget = TELEGRAM_TEXT_LIMIT - SAFETY_BUFFER - max(len(header_first), len(header_continued)) - len(footer)
    normalized: list[str] = []
    for block in blocks:
        if len(block) > body_budget:
            normalized.extend(_hard_split_block(block, body_budget))
        else:
            normalized.append(block)
    blocks = normalized

    chunks = []
    # Erster Chunk: header_first + blocks + footer
    # Folge-Chunks: header_continued + blocks + footer
    current_hdr = header_first
    current_body = []
    current_size = len(current_hdr) + len(footer)

    separator_size = len(separator)

    for block in blocks:
        needed = len(block) + (separator_size if current_body else 0)
        if current_size + needed > TELEGRAM_TEXT_LIMIT - SAFETY_BUFFER and current_body:
            # Chunk abschließen
            chunks.append(current_hdr + separator.join(current_body) + footer)
            # Neuer Chunk mit "continued" Header
            current_hdr = header_continued
            current_body = [block]
            current_size = len(current_hdr) + len(footer) + len(block)
        else:
            current_body.append(block)
            current_size += needed

    if current_body:
        chunks.append(current_hdr + separator.join(current_body) + footer)

    return chunks


async def job_per_bot_performance() -> None:
    """Sendet eine detaillierte Performance-Tabelle pro einzelnem Bot/Strategy.

    Zieht Daten aus vier Tabellen:
      - closed_trades_master: klassische Trades, geschlossen
      - active_trades_master: klassische Trades, offen
      - closed_ai_signals:    AI-signals, geschlossen
      - ai_signals:           AI-signals, offen

    Neu (April 2026):
      - MIS1-<N>h_pump + MIS1-<N>h_dump werden zu MIS1-<N>h konsolidiert
      - Typo-Fix: MSI1-* → MIS1-*
      - Zeitfenster-Filter after ERÖFFNUNGSZEIT (created_at) statt closed_at.
        Zeigt Bot-Entscheidungen im Zeitraum X, nicht Close-Events.
      - WR-Berechnung nur aus geschlossenen Trades innerhalb des Fensters
        (offene Trades werden separat gezählt aber nicht in WR einbezogen)
      - Detail-Zeile mit 4h Target-Staffelung (TP1+/TP2+/TP3+/TP4/SL)
        plus LONG vs SHORT Split

    Datenmodell after Vereinheitlichung (df_all):
      strategy_short: str         — Anzeige-Name after Aliasing + Konsolidierung
      direction:      "LONG"|"SHORT"
      entry:          float
      close_price:    float | NaN (bei offenen)
      created_at:     datetime    — Eröffnungszeit
      closed_at:      datetime|NaN — Schließzeit (NaN = offen)
      is_closed:      bool
      status_num:     int         — 0=SL, 1=TP1, 2=TP2, 3=TP3, 4=TP4 (NaN=offen)
      is_win:         bool        — True wenn status_num >= 1
      pnl_pct:        float|NaN   — Richtungs-korrigierter PnL-% (NaN bei offen)
    """
    logger.info("Generiere Per-Bot Performance-Detail-Post...")
    now = datetime.now(timezone.utc)

    try:
        # `with`: see job_signal_summary — the connection must go back to the
        # pool even when one of the five queries below raises.
        #
        # KNOWN RISK (P2.41, T-2026-CU-9050-081) — full-history load per hour.
        # The two closed-table queries dedupe (DISTINCT ON) over the ENTIRE
        # closed_trades_master / closed_ai_signals every hour (no time bound).
        # This is INTENTIONAL and must not be "optimised" with a window filter:
        #   1. The report carries an all-time column (stats['All']) plus all-time
        #      avg-PnL and Kelly — those need the full history by definition.
        #   2. The survivor pick (earliest close = original outcome) must run
        #      over the full table; a window-first query lets a re-close artifact
        #      resurface a months-old trade as freshly closed (same reason the
        #      summary queries filter OUTSIDE the dedup).
        # Measured cost 2026-07-09: ~439k raw AI rows collapse server-side to
        # ~81.8k under the dedup key, so pandas receives the deduped set, not the
        # raw table. Fine today; it grows with trade history. If it ever bites,
        # the fix is a materialised all-time aggregate refreshed less often (a
        # behaviour-changing redesign, an Operator decision), NOT a silent window
        # here. Documented as a risk rather than optimised (Ledger-Geist).
        with get_db_connection() as conn:
            # ═══ GESCHLOSSENE TRADES ═══

            # Klassische Trades: time=created, posted=closed, status=0..4 (string)
            #
            # Both closed queries dedupe on the report-14 key (AI_DEDUP_KEY /
            # CLS_DEDUP_KEY at module top, rationale there). Live measurement
            # 2026-07-09: 439k raw AI rows → 81.8k real trades under this key
            # (old #13 key left 360k); outside Feb/Mar 2026 the key is unique in
            # practice (raw == distinct every month). Same structure as the
            # summary queries: dedup over the full table FIRST, validity filter
            # outside — the survivor pick must not depend on the filter.
            #
            # entry > 0 AND close_price > 0: v1-era rows (pre-2026-03) carry
            # close_price=0 — the pnl formula would score such a SHORT as a +100%
            # win (a LONG as a -100% loss), both inside the 100% outlier bound.
            df_cls_closed = pd.read_sql_query(
                f"""
                SELECT * FROM (
                    SELECT DISTINCT ON ({CLS_DEDUP_KEY})
                           strategy, upper(btrim(direction)) as direction, entry, close_price,
                           time as created_at, posted as closed_at, status
                    FROM closed_trades_master
                    ORDER BY {CLS_DEDUP_ORDER}
                ) d
                WHERE d.entry > 0 AND d.close_price > 0
                """,
                conn,
            )

            # AI-signals: open_time=created, close_time=closed, targets_hit=0..19 (int)
            # close_reason wird aus der status-Spalte geladen (vom 8_ai_trade_monitor gesetzt)
            df_ai_closed = pd.read_sql_query(
                f"""
                SELECT * FROM (
                    SELECT DISTINCT ON ({AI_DEDUP_KEY})
                           model as strategy, upper(btrim(direction)) as direction, entry, close_price,
                           open_time as created_at, close_time as closed_at, targets_hit,
                           status as close_reason
                    FROM closed_ai_signals
                    WHERE status IS DISTINCT FROM 'ENTRY_NOT_FILLED'
                    ORDER BY {AI_DEDUP_ORDER}
                ) d
                WHERE d.entry > 0 AND d.close_price > 0
                """,
                conn,
            )

            # ═══ OFFENE TRADES ═══

            # active_trades_master: time=created; keine close_price/status
            df_cls_open = pd.read_sql_query(
                """
                SELECT strategy, direction, entry, time as created_at
                FROM active_trades_master
                WHERE entry IS NOT NULL
                """,
                conn,
            )

            # ai_signals hat keine Spalte für die Eröffnungszeit —
            # _load_open_ai_signals holt sie per JOIN aus ml_predictions_master
            # (Rationale + Fallback: OPEN_AI_SIGNALS_QUERY am Modul-Kopf).
            # Geteilt mit job_signal_summary, damit die beiden Posts nicht
            # auseinanderdriften (P1.44).
            df_ai_open = _load_open_ai_signals(conn)
    except Exception as e:
        logger.error(f"Error loading der Per-Bot Performance-Daten: {e}", exc_info=True)
        return

    # ─── Datenmodell vereinheitlichen ───

    # WICHTIG: is_win wird jetzt BASIEREND AUF DEM TATSÄCHLICHEN PnL ermittelt,
    # nicht mehr aus targets_hit bzw. status. Das umgeht mehrere historische Bugs:
    #
    #   1. 8_ai_trade_monitor schreibt bei "LEGACY TARGET HIT (+2.5%)" als Win,
    #      setzt aber new_targets_hit NICHT (bleibt bei 0). Das ist der Haupt-
    #      grund für die fast 100%-WR-Verluste-Anzeige bei Bots wie EPD1/ATS1.
    #   2. Trades mit close_reason='DELISTED / CLEANUP' sind weder Win noch
    #      Loss — sie werden jetzt als neutral behandelt und aus Kelly
    #      ausgeschlossen.
    #   3. Extreme Ausreißer (|pnl| > 100%) sind meistens Daten-Bugs und
    #      verzerren avg_win/avg_loss massiv — werden gefiltert.
    #   4. Trades mit |pnl| ≈ 0% (Housekeeping-Closes) sind ebenfalls neutral.
    #
    # Die klassifikation erfolgt after der PnL-Berechnung weiter unten.

    # Geschlossene klassische Trades
    if not df_cls_closed.empty:
        df_cls_closed['status_num'] = pd.to_numeric(df_cls_closed['status'], errors='coerce').fillna(0).astype(int)
        # NEU: close_reason-Spalte gibt es für klassische Trades nicht — leer setzen
        df_cls_closed['close_reason'] = ''
        df_cls_closed['is_closed'] = True
        df_cls_closed = df_cls_closed[
            [
                'strategy',
                'direction',
                'entry',
                'close_price',
                'created_at',
                'closed_at',
                'is_closed',
                'status_num',
                'close_reason',
            ]
        ]

    # Geschlossene AI-Trades
    if not df_ai_closed.empty:
        df_ai_closed['status_num'] = pd.to_numeric(df_ai_closed['targets_hit'], errors='coerce').fillna(0).astype(int)
        # close_reason wurde in der Query geladen, ggf. NaN → ''
        df_ai_closed['close_reason'] = df_ai_closed['close_reason'].fillna('').astype(str)
        df_ai_closed['is_closed'] = True
        df_ai_closed = df_ai_closed[
            [
                'strategy',
                'direction',
                'entry',
                'close_price',
                'created_at',
                'closed_at',
                'is_closed',
                'status_num',
                'close_reason',
            ]
        ]

    # Offene Trades — haben kein close, keinen status
    for df_open in (df_cls_open, df_ai_open):
        if not df_open.empty:
            df_open['close_price'] = pd.NA
            df_open['closed_at'] = pd.NaT
            df_open['is_closed'] = False
            df_open['status_num'] = pd.NA
            df_open['close_reason'] = ''

    # Alle Teile zusammenführen
    parts = []
    for df_part in (df_cls_closed, df_ai_closed, df_cls_open, df_ai_open):
        if not df_part.empty:
            parts.append(df_part)

    if not parts:
        logger.info("Keine Trade-Historie vorhanden — Per-Bot Post skipped.")
        return

    df_all = pd.concat(parts, ignore_index=True)

    # Timestamps normalisieren
    df_all['created_at'] = pd.to_datetime(df_all['created_at'], utc=True, errors='coerce')
    df_all['closed_at'] = pd.to_datetime(df_all['closed_at'], utc=True, errors='coerce')
    df_all = df_all.dropna(subset=['created_at'])

    # Entry/Close numerisch, nur sinnvolle entries
    df_all['entry'] = pd.to_numeric(df_all['entry'], errors='coerce')
    df_all['close_price'] = pd.to_numeric(df_all['close_price'], errors='coerce')
    df_all = df_all.dropna(subset=['entry'])
    df_all = df_all[df_all['entry'] > 0]

    # Belt-and-braces direction normalisation: the closed queries already
    # uppercase in SQL, but the open-trade frames (active_trades_master /
    # ai_signals) do not. A lowercase 'short' would otherwise get the LONG
    # sign in pnl_pct and drop out of the direction splits.
    df_all['direction'] = df_all['direction'].astype(str).str.strip().str.upper()

    # PnL nur für geschlossene Trades berechnen
    pct = (df_all['close_price'] - df_all['entry']) / df_all['entry'] * 100
    is_short = df_all['direction'] == 'SHORT'
    df_all['pnl_pct'] = pct.where(~is_short, -pct)
    # Offene Trades haben kein pnl_pct
    df_all.loc[~df_all['is_closed'], 'pnl_pct'] = pd.NA

    # ─── TRADE-OUTCOME-KLASSIFIKATION (NEU) ───
    # Klassifiziert jeden geschlossenen Trade als 'win', 'loss' oder 'neutral'.
    # 'neutral' wird aus Kelly ausgeschlossen (zählt weder als Win noch als Loss).
    #
    # Konstanten:
    #   OUTCOME_MIN_PNL_PCT: Trades mit |pnl_pct| <= diesem Wert gelten als
    #                       neutral (meist Housekeeping-Closes bei ~0%).
    #   OUTCOME_MAX_ABS_PNL_PCT: Trades mit |pnl_pct| > diesem Wert gelten
    #                           als Ausreißer und werden ignoriert (Daten-Bugs).
    OUTCOME_MIN_PNL_PCT = 0.1
    OUTCOME_MAX_ABS_PNL_PCT = 100.0

    def _classify_outcome(row) -> str:
        """Returns 'win', 'loss', 'neutral', oder '' (für offene Trades)."""
        if not row['is_closed']:
            return ''
        reason = (row['close_reason'] or '').upper()
        # Housekeeping-Closes: weder Win noch Loss (extern verursacht).
        # B9-Zensur-Korrektur (T-2026-CU-9050-048): REGIME_CHANGE zählt jetzt
        # mit realem PnL als Win/Loss (near-0% fängt der Micro-PnL-Filter).
        if 'DELISTED' in reason or 'CLEANUP' in reason or 'ORPHAN' in reason:
            return 'neutral'
        pnl = row['pnl_pct']
        if pd.isna(pnl):
            return 'neutral'
        pnl_f = float(pnl)
        # Ausreißer-Filter (wahrscheinlich Daten-Bug)
        if abs(pnl_f) > OUTCOME_MAX_ABS_PNL_PCT:
            return 'neutral'
        # Neutrale Micro-Bewegungen
        if abs(pnl_f) <= OUTCOME_MIN_PNL_PCT:
            return 'neutral'
        return 'win' if pnl_f > 0 else 'loss'

    df_all['outcome'] = df_all.apply(_classify_outcome, axis=1)
    df_all['is_win'] = df_all['outcome'] == 'win'
    df_all['is_loss'] = df_all['outcome'] == 'loss'
    df_all['is_neutral'] = df_all['outcome'] == 'neutral'

    # ─── Strategy-Namen normalisieren ───
    # pretty_name kommt jetzt zentral aus core/bot_naming.py damit
    # Market-Tracker und 27_bot_regime_analyzer identisch normalisieren.
    # Das behebt den "Regime Fit: ---" Bug bei FastInOut, MIS1-*, SR etc.
    # (Analyzer schrieb historisch mit Rohnamen "Fast In And Out",
    # Market-Tracker fragte mit "FastInOut" an → kein Match.)

    df_all['strategy_short'] = df_all['strategy'].apply(pretty_name)

    # ─── Pro Strategie & Zeitfenster Stats berechnen ───
    #
    # Zeitfenster-Semantik (neu April 2026):
    #   "1h" = Trades die in der letzten Stunde ERÖFFNET wurden.
    #   Die Win-Rate bezieht sich NUR auf bereits geschlossene davon —
    #   noch offene Trades zählen nicht in die WR-Berechnung (weil Ergebnis
    #   unklar), werden aber separat als "open" in der Detail-Zeile angezeigt.
    #
    # Warum created_at-basiert?
    #   Die Frage "Wie performt Bot X gerade?" hängt von den Marktbedingungen
    #   zur Eröffnung ab. Ein 168h-MIS1-Signal das heute schließt wurde vor
    #   einer Woche eröffnet — das sollte nicht die "1h"-Spalte beeinflussen.
    WINDOWS = [
        ("1h", timedelta(hours=1)),
        ("4h", timedelta(hours=4)),
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
        ("All", None),
    ]
    MIN_TRADES = 3

    # --- Kelly-Konstanten ---
    LEVERAGE = 20
    KELLY_FRACTION = 0.5
    KELLY_MIN_WINS = 10
    KELLY_MIN_LOSSES = 10

    def compute_kelly(sub_df) -> dict:
        """Berechnet Kelly-Stats. Basiert nur auf geschlossenen, nicht-neutralen Trades.

        WICHTIG: Neutrale Trades (Housekeeping-Closes, Ausreißer, DELISTED) werden
        komplett ausgeschlossen — sie sind weder Win noch Loss und würden die
        Statistik verzerren.
        """
        sub_closed = sub_df[sub_df['is_closed']]
        wins_pct = sub_closed[sub_closed['outcome'] == 'win']['pnl_pct']
        losses_pct = sub_closed[sub_closed['outcome'] == 'loss']['pnl_pct']

        if len(wins_pct) < KELLY_MIN_WINS or len(losses_pct) < KELLY_MIN_LOSSES:
            return {'status': 'insufficient_data'}

        avg_win = float(wins_pct.mean())
        avg_loss = abs(float(losses_pct.mean()))

        if avg_loss < 0.01 or avg_win <= 0:
            return {'status': 'insufficient_data'}

        b = avg_win / avg_loss
        p = len(wins_pct) / (len(wins_pct) + len(losses_pct))
        q = 1.0 - p

        kelly_f = (b * p - q) / b

        if kelly_f <= 0:
            return {'status': 'neg_edge'}

        half_kelly = kelly_f * KELLY_FRACTION
        half_kelly_pct = half_kelly * 100
        margin_safe_pct = half_kelly_pct / LEVERAGE
        margin_pure_pct = (half_kelly_pct * 100.0) / (LEVERAGE * avg_loss)

        return {
            'status': 'ok',
            'half_kelly_pct': half_kelly_pct,
            'margin_safe_pct': margin_safe_pct,
            'margin_pure_pct': margin_pure_pct,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'n_wins': len(wins_pct),
            'n_losses': len(losses_pct),
        }

    rows_per_strategy: dict[str, dict] = {}

    for strategy in sorted(df_all['strategy_short'].unique()):
        sub_full = df_all[df_all['strategy_short'] == strategy]

        # ─── Stats pro Zeitfenster ───
        # Neue Semantik: Filter auf created_at (Eröffnungszeit).
        # WR nur aus geschlossenen Trades innerhalb des Fensters.
        stats: dict[str, Any] = {'total': len(sub_full)}

        for win_name, delta in WINDOWS:
            if delta is None:
                sub_window = sub_full
            else:
                sub_window = sub_full[sub_full['created_at'] >= now - delta]

            # WR nur aus geschlossenen Trades in diesem Zeitfenster
            # WICHTIG: neutrale Trades (DELISTED, Housekeeping) zählen NICHT als
            # Loss in der WR — sonst bekommen Bots wie EPD1 irreführende ~0% WR
            # obwohl sie eigentlich bei 57% liegen.
            sub_closed = sub_window[sub_window['is_closed']]
            # Nur echte Wins/Losses (neutrale ausschließen)
            sub_decisive = sub_closed[sub_closed['outcome'].isin(['win', 'loss'])]
            n_closed = len(sub_closed)
            n_decisive = len(sub_decisive)

            if n_decisive < MIN_TRADES:
                stats[win_name] = ("---", None, n_closed)
            else:
                wr = sub_decisive['is_win'].sum() / n_decisive * 100
                stats[win_name] = (f"{wr:.0f}%", wr, n_closed)

        # Avg-PnL über alle ENTSCHIEDENEN (Wins+Losses) geschlossenen Trades all-time.
        # Neutrale (Housekeeping, Outlier) werden ausgeschlossen damit der Avg
        # nicht durch 0%-Trades oder Datenbug-Ausreißer verwässert wird.
        sub_closed_all = sub_full[sub_full['is_closed']]
        sub_decisive_all = sub_closed_all[sub_closed_all['outcome'].isin(['win', 'loss'])]
        if len(sub_decisive_all) > 0:
            stats['avg_pnl_all'] = float(sub_decisive_all['pnl_pct'].mean())
        else:
            stats['avg_pnl_all'] = None

        # Total-Counts für Header: neutrale separat ausweisen damit klar wird
        # dass sie existieren aber nicht in die Statistik einfließen.
        stats['n_closed_total'] = len(sub_closed_all)
        stats['n_decisive_total'] = len(sub_decisive_all)
        stats['n_neutral_total'] = len(sub_closed_all) - len(sub_decisive_all)

        # ─── Detail-Zeile für 4h-Fenster ───
        # Zeigt Target-Staffelung + LONG/SHORT-Split der letzten 4h.
        sub_4h = sub_full[sub_full['created_at'] >= now - timedelta(hours=4)]
        sub_4h_closed = sub_4h[sub_4h['is_closed']]
        sub_4h_open = sub_4h[~sub_4h['is_closed']]

        n_4h_opened = len(sub_4h)
        n_4h_closed = len(sub_4h_closed)
        n_4h_open = len(sub_4h_open)

        # Target-Staffelung: TP1+, TP2+, TP3+, TP4, SL
        # status_num ist 0..4 bei geschlossenen
        if n_4h_closed > 0:
            status_vals = sub_4h_closed['status_num'].astype(int)
            n_tp1_plus = int((status_vals >= 1).sum())
            n_tp2_plus = int((status_vals >= 2).sum())
            n_tp3_plus = int((status_vals >= 3).sum())
            n_tp4 = int((status_vals == 4).sum())
            n_sl = int((status_vals == 0).sum())

            # LONG/SHORT-Split — nur entschiedene Trades (ohne Neutrale) für WR
            long_closed = sub_4h_closed[sub_4h_closed['direction'] == 'LONG']
            short_closed = sub_4h_closed[sub_4h_closed['direction'] == 'SHORT']
            long_decisive = long_closed[long_closed['outcome'].isin(['win', 'loss'])]
            short_decisive = short_closed[short_closed['outcome'].isin(['win', 'loss'])]
            l_n = len(long_decisive)
            s_n = len(short_decisive)
            l_wins = int(long_decisive['is_win'].sum()) if l_n > 0 else 0
            s_wins = int(short_decisive['is_win'].sum()) if s_n > 0 else 0
        else:
            n_tp1_plus = n_tp2_plus = n_tp3_plus = n_tp4 = n_sl = 0
            l_n = s_n = l_wins = s_wins = 0

        stats['detail_4h'] = {
            'opened': n_4h_opened,
            'closed': n_4h_closed,
            'open': n_4h_open,
            'tp1_plus': n_tp1_plus,
            'tp2_plus': n_tp2_plus,
            'tp3_plus': n_tp3_plus,
            'tp4': n_tp4,
            'sl': n_sl,
            'long_n': l_n,
            'long_wins': l_wins,
            'short_n': s_n,
            'short_wins': s_wins,
        }

        # Kelly basiert auf ALLEN geschlossenen Trades dieser Strategy
        stats['kelly'] = compute_kelly(sub_full)

        rows_per_strategy[strategy] = stats

    # --- Sortierung: after Anzahl geschlossener Trades (aussagekräftigste zuerst) ---
    sorted_strategies = sorted(
        rows_per_strategy.items(),
        key=lambda kv: kv[1]['n_closed_total'],
        reverse=True,
    )

    # --- Trend-Marker: 1h vs. All ≥ 10 Prozentpunkte Abweichung ---
    def trend_marker(cell_1h, cell_all) -> str:
        """Gibt ↑ oder ↓ oder '' zurück — nur wenn beide Werte numerisch sind."""
        wr_1h = cell_1h[1]
        wr_all = cell_all[1]
        if wr_1h is None or wr_all is None:
            return " "
        diff = wr_1h - wr_all
        if diff >= 10:
            return "↑"
        if diff <= -10:
            return "↓"
        return " "

    # --- Haupttabelle bauen (monospace, passt in Telegram-<pre>) ---
    # Zielformat:
    #   MIS1-8h      │ 58%↑ │ 61% │ 63% │ 70% │ 62%   (n=7664, +1.34%)
    #     4h: 12 opened → 8 closed, 4 still open
    #       TP1+:7 TP2+:5 TP3+:2 TP4:0 | SL:1
    #       LONG: 6/7 win | SHORT: 1/1 win
    header = "Bot          │ 1h    │ 4h   │ 24h  │ 7d   │ All"
    separator = "─" * len(header)

    lines = [header, separator]

    for strategy, stats in sorted_strategies:
        cell_1h_str = stats["1h"][0]
        cell_4h_str = stats["4h"][0]
        cell_24h_str = stats["24h"][0]
        cell_7d_str = stats["7d"][0]
        cell_all_str = stats["All"][0]

        trend = trend_marker(stats["1h"], stats["All"])

        # n=X in der Haupt-Zeile ist jetzt n_closed_total (nur geschlossene)
        avg_pnl = stats.get('avg_pnl_all')
        n_closed = stats['n_closed_total']
        if avg_pnl is not None:
            pnl_str = f"n={n_closed}, {avg_pnl:+.2f}%"
        else:
            pnl_str = f"n={n_closed}"

        line = (
            f"{strategy:<12} │ {cell_1h_str:>4}{trend} │ "
            f"{cell_4h_str:>4} │ {cell_24h_str:>4} │ "
            f"{cell_7d_str:>4} │ {cell_all_str:>4}   ({pnl_str})"
        )
        lines.append(line)

        # Detail-Zeile für 4h (nur wenn es überhaupt Aktivität gab)
        d = stats.get('detail_4h', {})
        if d.get('opened', 0) > 0:
            if d['closed'] > 0:
                # Voller Detail-Block mit TP-Staffelung + Direction-Split
                detail1 = f"  4h: {d['opened']} opened → {d['closed']} closed, {d['open']} still open"
                lines.append(detail1)

                detail2 = (
                    f"    TP1+:{d['tp1_plus']} TP2+:{d['tp2_plus']} TP3+:{d['tp3_plus']} TP4:{d['tp4']} | SL:{d['sl']}"
                )
                lines.append(detail2)

                # LONG/SHORT-Split nur zeigen wenn beide Richtungen vorhanden
                parts = []
                if d['long_n'] > 0:
                    parts.append(f"LONG: {d['long_wins']}/{d['long_n']} win")
                if d['short_n'] > 0:
                    parts.append(f"SHORT: {d['short_wins']}/{d['short_n']} win")
                if parts:
                    lines.append(f"    {' | '.join(parts)}")

                lines.append("")  # Leerzeile zwischen Bots für Lesbarkeit
            else:
                # Nur Aktivität ohne Close → Kompakt-Variante (1 Zeile + Leerzeile)
                # Vermeidet den leeren Detail-Block, Layout bleibt ruhig.
                lines.append(f"  4h: {d['opened']} opened, {d['open']} still open")
                lines.append("")

    if len(lines) <= 2:
        logger.info("Per-Bot Post: keine Strategie mit Daten — skipped.")
        return

    # --- Kelly-Block bauen (pro Bot ein mobile-freundlicher Eintrag) ---
    # Layout:
    #   BOTNAME
    #     Half-Kelly:   13.2% of account
    #     Safe Margin:   0.66%  (Half-Kelly / Leverage)
    #     Pure Margin:  43.9%  (Half-Kelly / (avg_loss × Leverage))
    #
    # Oder bei negativem Edge:
    #   BOTNAME
    #     ⛔ NEGATIVE EDGE — do not trade
    #
    # Gleiche Reihenfolge wie Haupttabelle (after Trade-Count sortiert).
    #
    # WICHTIG: Telegram-HTML ist restriktiv bei Tags AUSSERHALB von <pre>-Blöcken.
    # Alle anderen funktionierenden Posts im Market-Tracker (Gainers, Losers,
    # Volume Spikes, Volatile Coins, Signal Summary) packen ALLES in einen
    # einzigen <pre>-Block. Wir folgen dem gleichen Muster — das ist die
    # zuverlässige Variante.
    kelly_lines = []

    # Eine Connection für alle Regime-Fit-Lookups statt pro Bot eine neue
    # aufzubauen. Bei 25+ Bots spart das 25+ TCP-Handshakes + DB-Auth.
    _regime_conn = None
    try:
        _regime_conn = get_db_connection()
    except Exception:
        _regime_conn = None  # Graceful: alle Regime-Fits zeigen dann "---"

    # try/finally: the loop below indexes into the kelly dict; anything raising
    # in there used to skip the close() and leak the pool slot.
    try:
        for strategy, stats in sorted_strategies:
            k = stats.get('kelly', {})
            status = k.get('status')

            kelly_lines.append(f"<b>{strategy}</b>")

            if status == 'insufficient_data':
                kelly_lines.append("  --- insufficient data (need ≥10 wins & losses)")
            elif status == 'neg_edge':
                kelly_lines.append("  ⛔ NEGATIVE EDGE — do not trade")
            elif status == 'ok':
                hk = k['half_kelly_pct']
                ms = k['margin_safe_pct']
                mp = k['margin_pure_pct']
                kelly_lines.append(f"  Half-Kelly:   {hk:>5.1f}% of account")
                kelly_lines.append(f"  Safe Margin:  {ms:>5.2f}%  (Half-Kelly / Lev)")
                kelly_lines.append(f"  Pure Margin:  {mp:>5.1f}%  (Half-Kelly / (avg_loss × Lev))")
            else:
                kelly_lines.append("  ---")

            # Regime Fit — Graceful Degradation: zeigt '---' wenn Orchestrator
            # nicht deployt oder Connection tot ist.
            if _regime_conn is not None:
                try:
                    fit_label = _get_regime_fit_label(_regime_conn, strategy)
                except Exception:
                    fit_label = "---"
                kelly_lines.append(f"  Regime Fit:   {fit_label}")
            else:
                kelly_lines.append("  Regime Fit:   ---")

            kelly_lines.append("")  # Leerzeile als Abtrennung zwischen Bots
    finally:
        # Regime-Connection sauber schließen, nachdem alle Bots durch sind
        if _regime_conn is not None:
            try:
                _regime_conn.close()
            except Exception:
                pass

    # --- Zusammenbau: ALLES in EINEN <pre>-Block, ohne style-Attribute ---
    # WICHTIG — Telegram-HTML-Regeln (Bot API Dokumentation):
    #   - Erlaubte Tags: <b>, <i>, <u>, <s>, <code>, <pre>, <a href="...">,
    #     <span class="tg-spoiler">
    #   - Alle Attribute außer `href` (bei <a>) und `class="tg-spoiler"` sind
    #     offiziell NICHT erlaubt.
    #   - ist NIE erlaubt — wird von manchen Clients toleriert,
    #     von anderen (insbesondere Mobile) verworfen → Parse-Fehler → Message
    #     wird still ge-failed.
    # Andere Posts im Market-Tracker nutzen `<pre>` und funktionieren
    # zufällig, weil Telegram das style-Attribut ignoriert. Aber bei komplexen
    # Messages mit vielen verschachtelten Tags triggert das trotzdem Parser-
    # Probleme. Wir bleiben hier auf der sicheren Seite: minimales API-konformes
    # HTML, KEINE style-Attribute.

    # --- Telegram-Message-Splitting ---
    # Bei vielen Strategien übersteigen die Posts das 4096-Zeichen-Limit.
    # Der Post hat zwei Teile: eine Tabelle und einen Kelly-Block.
    # Beide können einzeln zu lang werden, daher splitten wir beide
    # mit derselben Helper-Funktion in zuverlässige Chunks — auf
    # Zeilengrenzen, ohne Bot-entries oder Tabellen-Zeilen zu zerreißen.

    # Telegram-Chunking-Helper leben jetzt auf Modulebene (Über-Block-Split +
    # DB-freie Tests, P2.41) — _group_table_entries / _group_bot_entries /
    # _build_chunks.

    # ── Tabelle splitten ─────────────────────────────────────────────────
    table_header_line = lines[0] if len(lines) > 0 else ""
    table_separator = lines[1] if len(lines) > 1 else ""
    table_column_hdr = f"{table_header_line}\n{table_separator}\n"

    table_header_first = '<pre>📊 <b>PER-BOT PERFORMANCE</b> 📊\n\n' + table_column_hdr
    table_header_continued = '<pre>📊 <b>PER-BOT PERFORMANCE</b> (continued) 📊\n\n' + table_column_hdr
    table_footer = (
        '\n\n<b>Legend:</b>\n'
        '  ↑ 1h WR ≥10pp above All | ↓ 1h WR ≥10pp below All\n'
        '  --- = fewer than 3 trades in window\n'
        '  +X% = avg PnL/trade over all-time'
        '</pre>'
    )

    table_blocks = _group_table_entries(lines)
    table_chunks = _build_chunks(
        table_blocks,
        table_header_first,
        table_header_continued,
        table_footer,
    )

    # Fallback falls unerwartet keine Bot-Blöcke generiert wurden
    if not table_chunks:
        table_chunks = [table_header_first + table_footer]

    # ── Kelly-Block splitten ─────────────────────────────────────────────
    kelly_header_first = (
        '<pre>💰 <b>HALF-KELLY POSITION SIZING</b> 💰\n<i>20x Cross Leverage, Half-Kelly based on all-time data</i>\n\n'
    )
    kelly_header_continued = (
        '<pre>'
        '💰 <b>HALF-KELLY POSITION SIZING</b> (continued) 💰\n'
        '<i>20x Cross Leverage, Half-Kelly based on all-time data</i>\n\n'
    )
    kelly_footer = (
        '\n\n<i>Safe Margin: conservative, Half-Kelly as exposure target</i>\n'
        '<i>Pure Margin: classical Kelly calculation with avg_loss</i>\n'
        '<i>⚠ With N parallel correlated trades: margin ÷ N !</i>'
        '</pre>'
    )

    kelly_blocks = _group_bot_entries(kelly_lines)
    kelly_chunks = _build_chunks(
        kelly_blocks,
        kelly_header_first,
        kelly_header_continued,
        kelly_footer,
    )

    if not kelly_chunks:
        kelly_chunks = [kelly_header_first + kelly_footer]

    # ── Compact post: one line per model, alphanumerically sorted ────────
    # Complements the main table (sorted by trade count) with an A–Z view:
    # model generations (ABR1/ABR2, RUB1/RUB2, ...) sit directly under each
    # other and compare at a glance. Deliberately only 24h/7d/All + avg PnL
    # + n — no detail block, no Kelly.
    compact_rows = []
    for strategy, stats in sorted(rows_per_strategy.items(), key=lambda kv: kv[0].casefold()):
        avg_pnl = stats.get('avg_pnl_all')
        pnl_cell = f"{avg_pnl:+.2f}%" if avg_pnl is not None else "---"
        compact_rows.append(
            f"{strategy:<12} │ {stats['24h'][0]:>4} │ {stats['7d'][0]:>4} │ {stats['All'][0]:>4} │ "
            f"{pnl_cell} n={stats['n_decisive_total']}"
        )

    compact_col_hdr = "Bot          │ 24h  │ 7d   │ All  │ avgPnL n"
    compact_table_hdr = f"{compact_col_hdr}\n{'─' * len(compact_col_hdr)}\n"
    compact_header_first = '<pre>🔡 <b>MODELS A–Z</b> (compact)\n\n' + compact_table_hdr
    compact_header_continued = '<pre>🔡 <b>MODELS A–Z</b> (continued)\n\n' + compact_table_hdr
    compact_footer = (
        '\n\n<b>Legend:</b>\n'
        '  WR from decisive closes (window = open time)\n'
        '  --- = fewer than 3 decisive trades\n'
        '  avgPnL / n = all-time, neutrals excluded'
        '</pre>'
    )

    compact_chunks = _build_chunks(
        compact_rows,
        compact_header_first,
        compact_header_continued,
        compact_footer,
        separator="\n",
    )

    # --- Send: table chunks, then compact A-Z post, then Kelly chunks ---
    # Short delay between messages so Telegram keeps the ordering.
    for tchunk in table_chunks:
        send_telegram(tchunk, TELEGRAM_CHANNEL_ID)
        await asyncio.sleep(1)
    for cchunk in compact_chunks:
        send_telegram(cchunk, TELEGRAM_CHANNEL_ID)
        await asyncio.sleep(1)
    for kchunk in kelly_chunks:
        send_telegram(kchunk, TELEGRAM_CHANNEL_ID)
        await asyncio.sleep(1)

    logger.info(
        f"✅ Per-Bot Performance-Post gesendet ({len(sorted_strategies)} Strategien, {len(df_all)} Trades total)."
    )
    await asyncio.sleep(1)


# ═══════════════════════════════════════════════════════════════════════════
# REALIZED PnL REPORT (T-2026-CU-9050-115)
# ═══════════════════════════════════════════════════════════════════════════
#
# Every 4 hours: per ACTIVE bot the actually realised, leveraged PnL of the
# closed trades — target-weighted (stake split equally across the published
# targets, each hit realises 1/N at the target price, the rest closes at
# close_price) × posted leverage. Windows are keyed on CLOSE time (realised
# when closed), unlike job_per_bot_performance which windows on open time.
#
# Exact-only (operator decision 2026-07-13): AI rows count ONLY when the
# close persisted targets + lev (8_ai_trade_monitor writes them since this
# task); historical NULL rows are excluded, so the AI windows fill up
# gradually after deploy. Classic rows (closed_trades_master) always carried
# target1-4 + lev and are exact over the full history.

REALIZED_WINDOWS: tuple[tuple[str, float], ...] = (
    ("8h", 8.0),
    ("24h", 24.0),
    ("3d", 72.0),
    ("7d", 168.0),
    ("30d", 720.0),
)

# Housekeeping-style closes are externally caused — neither a bot win nor a
# bot loss (same rationale as _classify_outcome in job_per_bot_performance).
REALIZED_NEUTRAL_FRAGMENTS = ("DELISTED", "CLEANUP", "ORPHAN")


def _is_neutral_close(reason: object) -> bool:
    """True für Housekeeping-Closes (DELISTED/CLEANUP/ORPHAN).

    Gilt für BEIDE Quellen: closed_ai_signals.status trägt den close_reason,
    und 6_housekeeping schreibt dieselben Marker auch in
    closed_trades_master.status (statt der üblichen "0".."4") — ohne diesen
    Filter würde ein Delisting-Close als voller Entry→Letztkurs-Move × Hebel
    in die Bot-Summe laufen, obwohl die Legende Housekeeping ausschließt.
    """
    text = str(reason or "").upper()
    return any(frag in text for frag in REALIZED_NEUTRAL_FRAGMENTS)


def _parse_targets(value: Any) -> list | None:
    """closed_ai_signals.targets → Liste. Der json-Spaltenwert kommt je nach
    Treiber-Pfad als geparste Liste ODER als String an; alles andere → None
    (Row fällt exact-only aus dem Report)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, list) else None


def _classic_targets(t1: Any, t2: Any, t3: Any, t4: Any) -> list[float]:
    """N aus den non-null target1-4-Spalten ableiten. 3_detectors schreibt 0
    für nicht vergebene Targets; REAL-NULLs kommen aus pandas als NaN an —
    beides fällt raus (NaN > 0 ist False)."""
    out = []
    for t in (t1, t2, t3, t4):
        if t is None:
            continue
        try:
            f = float(t)
        except (TypeError, ValueError):
            continue
        if f > 0:
            out.append(f)
    return out


def _parse_hits(status: Any) -> int:
    """Classic status ("0".."4", tolerant gegen Junk) → Anzahl getroffener
    Targets. Nicht-numerisch = 0 (Housekeeping-Marker filtert der Caller
    vorher über _is_neutral_close)."""
    try:
        return int(float(status))
    except (TypeError, ValueError):
        return 0


def _aggregate_realized_pnl(rows: list[tuple[str, float, float]]) -> dict[str, dict[str, dict[str, float]]]:
    """(bot, age_hours, pnl_pct)-Zeilen in per-Bot/per-Fenster-Stats falten.

    Returns {bot: {window: {'sum': x, 'n': k, 'avg': x/k}}} — windows without
    trades are absent. Pure + module-scope so backtest/test_market_tracker_
    realized.py can drive it without a DB (same pattern as _build_chunks).
    """
    stats: dict[str, dict[str, dict[str, float]]] = {}
    for bot, age_h, pnl in rows:
        if age_h < 0:
            continue  # clock skew artefact — a close "in the future" fits no window
        per_bot = stats.setdefault(bot, {})
        for win_name, hours in REALIZED_WINDOWS:
            if age_h <= hours:
                w = per_bot.setdefault(win_name, {'sum': 0.0, 'n': 0.0})
                w['sum'] += pnl
                w['n'] += 1
    for per_bot in stats.values():
        for w in per_bot.values():
            w['avg'] = w['sum'] / w['n']
    return stats


def _format_realized_pnl_blocks(stats: dict[str, dict[str, dict[str, float]]]) -> list[str]:
    """Ein Telegram-<pre>-Block pro Bot, sortiert nach 30d-Summe (absteigend)."""

    def sort_key(item: tuple[str, dict]) -> tuple[float, str]:
        w30 = item[1].get("30d")
        return (-(w30['sum'] if w30 else float('-inf')), item[0].casefold())

    blocks = []
    for bot, per_bot in sorted(stats.items(), key=sort_key):
        lines = [f"<b>{bot}</b>"]
        for win_name, _hours in REALIZED_WINDOWS:
            w = per_bot.get(win_name)
            if w:
                lines.append(f"  {win_name:<3}: Σ {w['sum']:>+8.1f}% │ Ø {w['avg']:>+7.2f}% │ n={int(w['n'])}")
            else:
                lines.append(f"  {win_name:<3}: —")
        blocks.append("\n".join(lines))
    return blocks


def realized_lifecycle_bucket(tag: str, direction: str, active_scripts_set: set[str]) -> str:
    """Report-Bucket eines (tag, direction)-Beins (T-2026-CU-9050-125).

    Returns 'active' | 'shadow' | 'retired' | 'inactive' | 'unmapped'. Quelle der
    Lifecycle-Wahrheit ist core.shadow_gate; nur LIVE-Beine unterliegen zusätzlich
    dem Läuft-das-Skript-Gate (active_scripts). Pure + module-scope → testbar ohne
    DB (backtest/test_market_tracker_lifecycle.py)."""
    status = shadow_gate.leg_status(tag, direction)
    if status == shadow_gate.RETIRED:
        return "retired"
    if status == shadow_gate.SHADOW:
        return "shadow"
    script = script_for_tag(tag)
    if script is None:
        return "unmapped"
    if script not in active_scripts_set:
        return "inactive"
    return "active"


async def job_realized_pnl_report() -> None:
    """Postet den Leveraged-Realized-PnL-Report für aktive Bots (alle 4h)."""
    # Der Scheduler tickt stündlich (minutes-Liste) — die 4h-Cadence lebt hier.
    now = datetime.now(timezone.utc)
    if now.hour % 4 != 0:
        return

    logger.info("Generiere Realized-PnL-Report (aktive Bots)...")

    try:
        with get_db_connection() as conn:
            # ── AI closes ────────────────────────────────────────────────
            # Dedup over the FULL table first, filters outside (report-14
            # survivor rule, see AI_DEDUP_KEY comment at module top).
            #
            # age_h statt Timestamps (docs/UTC_POLICY.md §2/§3, Falle 9):
            # close_time wird von Bot 8 per NOW() geschrieben. Ob die Spalte
            # naiv ist (schema.sql) oder timestamptz (Bot-8-Bootstrap-ALTER,
            # r3-Migrationsdoc) — LOCALTIMESTAMP - close_time ist in BEIDEN
            # Domänen writer-konsistent: naiv-lokal minus naiv-lokal, bzw.
            # LOCALTIMESTAMP castet gegen timestamptz implizit auf now().
            # Ein UTC-now aus Python läge im naiven Fall 3h daneben (P1.8).
            # Deterministischer Spalten-Probe statt Exception-String-Match:
            # eine pandas.DatabaseError-Message enthält den vollen SQL-Text
            # (und damit immer "targets"/"lev") — ein Match darauf würde JEDEN
            # Query-Fehler (Lock, Timeout, Connection) als "Migration
            # ausstehend" maskieren. information_schema sagt es direkt.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'closed_ai_signals' AND column_name IN ('targets', 'lev')
                    """
                )
                have_cols = {row[0] for row in cur.fetchall()}

            if {"targets", "lev"} <= have_cols:
                df_ai = pd.read_sql_query(
                    f"""
                    SELECT * FROM (
                        SELECT DISTINCT ON ({AI_DEDUP_KEY})
                               model AS strategy, upper(btrim(direction)) AS direction,
                               entry, close_price, targets_hit, targets, lev,
                               status AS close_reason,
                               EXTRACT(EPOCH FROM (LOCALTIMESTAMP - close_time)) / 3600.0 AS age_h
                        FROM closed_ai_signals
                        ORDER BY {AI_DEDUP_ORDER}
                    ) d
                    WHERE d.entry > 0 AND d.close_price > 0
                      AND d.targets IS NOT NULL AND d.lev IS NOT NULL
                      AND d.age_h <= 720.0
                      AND d.close_reason IS DISTINCT FROM 'ENTRY_NOT_FILLED'
                    """,
                    conn,
                )
            else:
                # Erwartbar bis Bot 8 nach dem Deploy einmal lief und die
                # Spalten targets/lev angelegt hat (Schema-Sicherung dort).
                logger.warning(
                    "Realized-PnL: closed_ai_signals hat targets/lev noch nicht (Bot-8-Migration ausstehend) — AI-Teil übersprungen."
                )
                df_ai = pd.DataFrame()

            # ── Classic closes ───────────────────────────────────────────
            # posted (= Close-Zeit): 5_trade_monitor übergibt zwar aware-UTC,
            # aber der Cast in die NAIVE Spalte läuft über die Session-TZ und
            # landet als LOKALZEIT (docs/UTC_POLICY.md §3, P2.6 offen bis zum
            # R3-Pool-Flip). Dieselbe Uhr wie beim AI-Teil: LOCALTIMESTAMP.
            # NOW() AT TIME ZONE 'UTC' läge 3h daneben und würde jeden in den
            # letzten ~3h geschlossenen Trade als negative Age still droppen.
            df_cls = pd.read_sql_query(
                f"""
                SELECT * FROM (
                    SELECT DISTINCT ON ({CLS_DEDUP_KEY})
                           strategy, upper(btrim(direction)) AS direction,
                           entry, close_price, lev,
                           target1, target2, target3, target4, status,
                           EXTRACT(EPOCH FROM (LOCALTIMESTAMP - posted)) / 3600.0 AS age_h
                    FROM closed_trades_master
                    ORDER BY {CLS_DEDUP_ORDER}
                ) d
                WHERE d.entry > 0 AND d.close_price > 0 AND d.age_h <= 720.0
                """,
                conn,
            )
    except Exception as e:
        logger.error(f"Error loading der Realized-PnL-Daten: {e}", exc_info=True)
        return

    active = active_scripts()
    # T-2026-CU-9050-125: drei Lifecycle-Blöcke je (tag, direction) statt eines
    # flachen ACTIVE-Blocks — aktiv (live postend) / shadow (getrackt, nie live) /
    # retired (alte Generation). Quelle der Wahrheit: core.shadow_gate.leg_status.
    active_rows: list[tuple[str, float, float]] = []
    shadow_rows: list[tuple[str, float, float]] = []
    retired_rows: list[tuple[str, float, float]] = []
    n_neutral = 0
    n_invalid = 0
    n_inactive = 0
    n_future = 0
    unmapped: set[str] = set()

    def add_row(tag: str, direction: str, age_h: float, pnl: float | None) -> None:
        nonlocal n_invalid, n_inactive, n_future
        if pnl is None:
            n_invalid += 1
            return
        if age_h < 0:
            # Close "in der Zukunft" = Uhren-Mismatch zwischen Writer-Spalte
            # und Report-Query (Falle 9). Zählen statt still droppen — ein
            # wachsender Wert hier heißt: die Clock-Paarung oben ist kaputt.
            n_future += 1
            return
        label = pretty_name(tag)
        bucket = realized_lifecycle_bucket(tag, direction, active)
        if bucket == "retired":
            # Alte Generation: historisch zeigen, unabhängig vom Live-Zustand.
            retired_rows.append((label, float(age_h), pnl))
        elif bucket == "shadow":
            # Getrackt, aber nie live gepostet (staging/neuer Tag oder geparktes Bein).
            shadow_rows.append((label, float(age_h), pnl))
        elif bucket == "active":
            active_rows.append((label, float(age_h), pnl))
        elif bucket == "unmapped":
            unmapped.add(label)
        else:  # "inactive": LIVE-Bein, aber Skript geparkt (wie bisher gedroppt)
            n_inactive += 1

    for r in df_ai.itertuples(index=False):
        if _is_neutral_close(r.close_reason):
            n_neutral += 1
            continue
        targets = _parse_targets(r.targets)
        pnl = realized_pnl_pct(r.direction, r.entry, r.close_price, targets or [], r.targets_hit, r.lev)
        add_row(str(r.strategy), str(r.direction), r.age_h, pnl)

    for r in df_cls.itertuples(index=False):
        # 6_housekeeping schreibt "DELISTED"-Marker auch in die classic
        # status-Spalte (sonst "0".."4") — Housekeeping-Closes sind neutral.
        if _is_neutral_close(r.status):
            n_neutral += 1
            continue
        targets = _classic_targets(r.target1, r.target2, r.target3, r.target4)
        pnl = realized_pnl_pct(r.direction, r.entry, r.close_price, targets, _parse_hits(r.status), r.lev)
        add_row(str(r.strategy), str(r.direction), r.age_h, pnl)

    if unmapped:
        # No silent drops: an unmapped tag means core/bot_catalog.py lacks the
        # family of a new/renamed model — surface it instead of hiding trades.
        logger.warning(f"Realized-PnL: {len(unmapped)} Tag(s) ohne Bot-Zuordnung ausgelassen: {sorted(unmapped)}")

    active_stats = _aggregate_realized_pnl(active_rows)
    shadow_stats = _aggregate_realized_pnl(shadow_rows)
    retired_stats = _aggregate_realized_pnl(retired_rows)
    if not (active_stats or shadow_stats or retired_stats):
        logger.info("Realized-PnL: keine berechenbaren Trades im 30d-Fenster — Post skipped.")
        return

    unmapped_note = f'\n  {len(unmapped)} unmapped tag(s) skipped (see bot log)' if unmapped else ''
    legend = (
        '\n\n<b>Legend:</b>\n'
        '  Σ = sum of realized % per window | Ø = avg per trade\n'
        '  ACTIVE = live posting · SHADOW = tracked, not posted · RETIRED = old tag\n'
        '  stake split equally across targets, rest closes at exit\n'
        '  AI trades: only closes with persisted targets+lev (exact-only)\n'
        '  excluded: unfilled entries, housekeeping closes'
        f'{unmapped_note}'
    )

    sections = [
        ("💵 <b>REALIZED PnL — ACTIVE</b> (live posting) 💵", active_stats),
        ("👻 <b>REALIZED PnL — SHADOW</b> (tracked, not live) 👻", shadow_stats),
        ("🗄 <b>REALIZED PnL — RETIRED</b> (old model versions) 🗄", retired_stats),
    ]
    sections = [(title, st) for title, st in sections if st]

    for idx, (title, st) in enumerate(sections):
        is_last = idx == len(sections) - 1
        header_first = f'<pre>{title}\n<i>leveraged, target-weighted, windows by CLOSE time</i>\n\n'
        header_continued = f'<pre>{title} (continued)\n\n'
        footer = (legend if is_last else '') + '</pre>'
        chunks = _build_chunks(_format_realized_pnl_blocks(st), header_first, header_continued, footer)
        for chunk in chunks:
            send_telegram(chunk, TELEGRAM_CHANNEL_ID)
            await asyncio.sleep(1)

    if n_future:
        logger.warning(
            f"Realized-PnL: {n_future} Close(s) mit negativer Age gedroppt — Writer-/Query-Uhr prüfen (Falle 9)."
        )
    n_total = len(active_rows) + len(shadow_rows) + len(retired_rows)
    logger.info(
        f"✅ Realized-PnL-Post gesendet (active={len(active_stats)}, shadow={len(shadow_stats)}, "
        f"retired={len(retired_stats)} Bots, {n_total} Trades im 30d-Fenster; "
        f"skipped: {n_neutral} neutral, {n_invalid} invalid, {n_inactive} inactive, {n_future} future-age)."
    )


# ⏰ SCHEDULER ENGINE
#
# KNOWN RISK (P2.41, T-2026-CU-9050-081) — the jobs are `async def` but every
# one does BLOCKING synchronous DB I/O (pd.read_sql_query / cur.execute). The
# `async` is effectively cosmetic: on the single event loop a running job blocks
# every other job's timer until it returns. This is tolerated by design, NOT a
# concurrency bug, and is deliberately not "fixed" here:
#   - The six jobs are staggered across distinct minute/second offsets (see
#     main()), so in normal operation they do not overlap.
#   - A true-async conversion (asyncio.to_thread around each query) would let
#     jobs run concurrently and open several pooled connections at once — the
#     pool max is 8 per process, so it would trade a benign scheduling delay for
#     a real starvation risk. That is a rewrite, out of this task's scope.
# The one real exposure: if a single job HANGS on a slow query it stalls the
# others too. Mitigation already in place is the time-staggering; a hard
# per-job timeout would be the minimal future hardening if it ever bites.
async def schedule_job(minutes, second, job_func, name):
    """Führt einen Task exakt zu den definierten Minuten & Sekunden aus."""
    logger.info(f"Task '{name}' registriert für Min: {minutes}, Sek: {second}.")
    while True:
        now = datetime.now(timezone.utc)
        next_run = None

        # Searching die nächste Minute im Array
        for m in minutes:
            cand = now.replace(minute=m, second=second, microsecond=0)
            if cand > now:
                if next_run is None or cand < next_run:
                    next_run = cand

        # Falls in dieser Stunde keine Minute mehr passt -> nächste Stunde!
        if next_run is None:
            next_run = (now + timedelta(hours=1)).replace(minute=minutes[0], second=second, microsecond=0)

        sleep_sec = (next_run - now).total_seconds()
        await asyncio.sleep(sleep_sec)

        try:
            await job_func()
        except Exception as e:
            logger.error(f"Error for {name}: {e}", exc_info=True)

        # 💥 FIX FÜR DOPPELTE NACHRICHTEN:
        # Zwingt die Schleife, in die nächste Sekunde zu springen,
        # damit "next_run" bei der nächsten Iteration definitiv in der Zukunft liegt.
        await asyncio.sleep(1)


async def main():
    logger.info("=== 🌐 MARKET TRACKER GESTARTET ===")

    tasks = [
        # 1. Main Vol Report: Volle Stunde + 15 Sek [XX:00:15]
        asyncio.create_task(schedule_job([0], 15, job_main_reports, "Main_Volume_Report")),
        # 2. Gainers & Losers: Volle Stunde + 1 Min (60 Sek) [XX:01:00]
        asyncio.create_task(schedule_job([1], 0, job_gainers_losers, "Gainers_Losers")),
        # 3. Volume Spikes: Volle Stunde + 15 Sek UND Halbe Stunde + 15 Sek [XX:00:15 & XX:30:15]
        asyncio.create_task(schedule_job([0, 30], 30, job_volume_spikes, "Volume_Spikes")),
        # 4. Volatile Coins: Volle Stunde + 25 Sek UND Halbe Stunde + 25 Sek [XX:00:25 & XX:30:25]
        asyncio.create_task(schedule_job([0, 30], 45, job_volatile_coins, "Volatile_Coins")),
        # 5. NEU: Signal Summary: Volle Stunde + 1 Sek [XX:00:01]
        asyncio.create_task(schedule_job([0], 1, job_signal_summary, "Signal_Summary")),
        # 6. Per-Bot Performance-Detail: Volle Stunde + 30 Sek [XX:00:30]
        # Läuft 30s after der Signal-Summary damit der Telegram-Worker nicht
        # zwei große Posts im gleichen Takt in denselben Channel drückt.
        asyncio.create_task(schedule_job([0], 30, job_per_bot_performance, "Per_Bot_Performance")),
        # 7. Realized PnL (T-2026-CU-9050-115): stündlich getriggert [XX:02:30],
        # der Job selbst postet nur alle 4h (hour % 4 == 0). Minute 2 hält den
        # XX:00/XX:01-Takt der bestehenden Posts frei.
        asyncio.create_task(schedule_job([2], 30, job_realized_pnl_report, "Realized_PnL")),
    ]

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C).")
