import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone

import websockets

from core import config as _kcfg  # channel ids

# DB Connection importieren (für Telegram)
from core.market_utils import load_coins, send_telegram
from core.trade_utils import format_price
from core.ws_utils import apply_keepalive as _apply_keepalive

# 🛠️ CONFIGURATION
logging.basicConfig(level=logging.INFO, format='%(asctime)s - WHALE_LOGGER - %(message)s')
logger = logging.getLogger(__name__)

COINS_FILE = "coins.json"
DATA_DIR = "whale_data"

# Mindest-USD-Volumen, ab dem ein Trade als "Whale-Trade" getracked wird.
# Dokumentation, Log-Ausgabe und dieser Wert müssen übereinstimmen!
MIN_USD_VALUE = 25_000.0
SAVE_INTERVAL_SEC = 300  # Alle 5 Minuten speichern
MAX_AGE_SEC = 3 * 24 * 3600  # 3 Tage im RAM

TELEGRAM_CHANNEL_ID = _kcfg.CH_MARKET_DATA
UPDATE_INTERVAL_SEC = 1800  # 30 Minuten

TOP20_WHALE_COINS = [
    "BTCUSDT",
    "ETHUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "TRXUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "BCHUSDT",
    "HYPEUSDT",
    "LINKUSDT",
    "ZECUSDT",
    "XLMUSDT",
    "LTCUSDT",
    "HBARUSDT",
    "AVAXUSDT",
    "SUIUSDT",
    "UNIUSDT",
    "TONUSDT",
    "DOTUSDT",
]

# Globaler Arbeitsspeicher
WHALE_TRADES = []
os.makedirs(DATA_DIR, exist_ok=True)


def format_usd(val):
    """Formatiert große Summen kompakt für Mobilgeräte (z.B. 1.5M, 500K).

    FIX (#81): Vorher fielen negative Werte durch ins default-format
    (z.B. -1_500_000 → $-1500000). Jetzt: absoluten Wert formatieren und
    Sign getrennt voranstellen.
    """
    sign = "-" if val < 0 else ""
    abs_val = abs(val)
    if abs_val >= 1_000_000:
        return f"{sign}${abs_val / 1_000_000:.1f}M"
    if abs_val >= 1_000:
        return f"{sign}${abs_val / 1_000:.0f}K"
    return f"{sign}${abs_val:.0f}"


# 📡 TELEGRAM HELPER


def get_stats(trades, start_ts, end_ts, symbols=None, exclude=None):
    """Counts trades and volume for a specific time window and coin set."""
    l_c, l_v, s_c, s_v = 0, 0.0, 0, 0.0
    for t in trades:
        if start_ts <= t['ts'] < end_ts:
            sym = t['sym']
            if symbols and sym not in symbols:
                continue
            if exclude and sym in exclude:
                continue

            if t['dir'] == 'LONG':
                l_c += 1
                l_v += t['usd']
            else:
                s_c += 1
                s_v += t['usd']
    return l_c, l_v, s_c, s_v


def get_ratio(long_vol, short_vol):
    """Calculates the L/S ratio."""
    if short_vol == 0:
        return "∞" if long_vol > 0 else "0.0"
    return f"{long_vol / short_vol:.2f}"


def build_asset_block(name, trades, now_ts, symbols=None):
    """Builds the text block for BTC, ETH or top-20."""
    t1 = now_ts - 3600  # 1h
    t2 = now_ts - 7200  # 2h (für prev 1h)
    t4 = now_ts - 14400  # 4h
    t24 = now_ts - 86400  # 24h

    # Stats für die verschiedenen Zeitfenster
    l1_c, l1_v, s1_c, s1_v = get_stats(trades, t1, now_ts, symbols)
    _, pl1_v, _, ps1_v = get_stats(trades, t2, t1, symbols)  # Prev 1h
    _, l4_v, _, s4_v = get_stats(trades, t4, now_ts, symbols)  # Last 4h
    _, l24_v, _, s24_v = get_stats(trades, t24, now_ts, symbols)  # Last 24h

    r1 = get_ratio(l1_v, s1_v)
    r_p1 = get_ratio(pl1_v, ps1_v)
    r4 = get_ratio(l4_v, s4_v)
    r24 = get_ratio(l24_v, s24_v)

    return f"""<b>{name}</b>
🟢 L: {l1_c} ({format_usd(l1_v)})
🔴 S: {s1_c} ({format_usd(s1_v)})
⚖️ Ratio (Vol): <b>{r1}</b>
⏳ Prev Ratios: 1h:{r_p1} | 4h:{r4} | 24h:{r24}"""


# 📊 WHALE EVALUATOR LOOP (Exakt :00:05 und :30:05)
async def evaluate_whales_loop():
    """Background task: posts exactly at the half and full hour + 5 seconds."""
    logger.info("Whale evaluator started (synced to :00:05 and :30:05).")

    while True:
        # 1. Zeitzonen-Berechnung für den exakten Trigger
        now = datetime.now(timezone.utc)

        if now.minute < 30 or (now.minute == 30 and now.second < 5):
            # Ziel ist in DIESER Stunde um xx:30:05
            target = now.replace(minute=30, second=5, microsecond=0)
        else:
            # Ziel ist in der NÄCHSTEN Stunde um xx:00:05
            target = (now + timedelta(hours=1)).replace(minute=0, second=5, microsecond=0)

        # 2. Exakt so lange schlafen
        sleep_sec = (target - now).total_seconds()
        await asyncio.sleep(sleep_sec)

        # 3. Code ausführen
        try:
            now_ts = time.time()
            t1 = now_ts - 3600

            # Alle Trades der letzten 1 Stunde kopieren für schnelle Analyse
            last_1h_trades = [t for t in WHALE_TRADES if t['ts'] >= t1]

            # 1. Standard Blöcke (BTC, ETH, TOP20)
            btc_block = build_asset_block("BTC Trades", WHALE_TRADES, now_ts, ["BTCUSDT"])
            eth_block = build_asset_block("ETH Trades", WHALE_TRADES, now_ts, ["ETHUSDT"])
            top20_block = build_asset_block("TOP 20 Coins", WHALE_TRADES, now_ts, TOP20_WHALE_COINS)

            # 2. Top 5 Altcoins after Whale Volumen (ohne BTC/ETH)
            altcoin_vols = {}
            for t in last_1h_trades:
                sym = t['sym']
                if sym in ["BTCUSDT", "ETHUSDT"]:
                    continue

                if sym not in altcoin_vols:
                    altcoin_vols[sym] = {'l_c': 0, 'l_v': 0.0, 's_c': 0, 's_v': 0.0}

                if t['dir'] == 'LONG':
                    altcoin_vols[sym]['l_c'] += 1
                    altcoin_vols[sym]['l_v'] += t['usd']
                else:
                    altcoin_vols[sym]['s_c'] += 1
                    altcoin_vols[sym]['s_v'] += t['usd']

            # Sortieren after Volumen
            top_long_coins = sorted(altcoin_vols.items(), key=lambda x: x[1]['l_v'], reverse=True)[:5]
            top_short_coins = sorted(altcoin_vols.items(), key=lambda x: x[1]['s_v'], reverse=True)[:5]

            dev_block = "<b>Development (Last 1h, ex. BTC/ETH):</b>\n"
            dev_block += "🟢 <b>Top 5 Longs:</b>\n"
            for sym, data in top_long_coins:
                if data['l_v'] > 0:
                    dev_block += f"{sym:<10} {data['l_c']:>2}x {format_usd(data['l_v']):>8}\n"

            dev_block += "\n🔴 <b>Top 5 Shorts:</b>\n"
            for sym, data in top_short_coins:
                if data['s_v'] > 0:
                    dev_block += f"{sym:<10} {data['s_c']:>2}x {format_usd(data['s_v']):>8}\n"

            # 3. Top 5 Einzel-Trades (Die wahren Wale)
            long_trades = sorted(
                [t for t in last_1h_trades if t['dir'] == 'LONG'], key=lambda x: x['usd'], reverse=True
            )[:5]
            short_trades = sorted(
                [t for t in last_1h_trades if t['dir'] == 'SHORT'], key=lambda x: x['usd'], reverse=True
            )[:5]

            trade_block = "<b>Top 5 Whale Trades Long (1h):</b>\n"
            for t in long_trades:
                dt_str = datetime.fromtimestamp(t['ts'], tz=timezone.utc).strftime('%H:%M')
                # P3.5: significant-digit price so sub-cent coins don't all show "$0.00".
                trade_block += f"{t['sym']:<9} {format_usd(t['usd']):>6} @ {format_price(t['prc'])} ({dt_str})\n"

            trade_block += "\n<b>Top 5 Whale Trades Short (1h):</b>\n"
            for t in short_trades:
                dt_str = datetime.fromtimestamp(t['ts'], tz=timezone.utc).strftime('%H:%M')
                # P3.5: significant-digit price so sub-cent coins don't all show "$0.00".
                trade_block += f"{t['sym']:<9} {format_usd(t['usd']):>6} @ {format_price(t['prc'])} ({dt_str})\n"

            # 4. Zusammenbauen der fertigen Nachricht
            msg = f"""<pre>
🐋 <b>WHALE ACTIVITY UPDATE</b> 🐋

{btc_block}

{eth_block}

{top20_block}

-------------------------------
{dev_block}
-------------------------------
{trade_block}</pre>"""

            send_telegram(msg, TELEGRAM_CHANNEL_ID)
            logger.info("✅ Pünktliches Whale Activity Update gesendet.")

        except Exception as e:
            logger.error(f"Fehler im Whale Evaluator: {e}", exc_info=True)


# ⚙️ DATENSAMMLER CORE SYSTEM


def load_existing_whales():
    global WHALE_TRADES
    WHALE_TRADES = []
    now = datetime.now(timezone.utc)

    for i in range(4):
        check_date = now - timedelta(days=i)
        date_str = check_date.strftime('%Y-%m-%d')
        filepath = os.path.join(DATA_DIR, f"whale_trades_{date_str}.json")

        if os.path.exists(filepath):
            try:
                with open(filepath, encoding="utf-8") as f:
                    WHALE_TRADES.extend(json.load(f))
            except Exception as e:
                logger.error(f"Error loading von {filepath}: {e}")

    cutoff = time.time() - MAX_AGE_SEC
    WHALE_TRADES = [t for t in WHALE_TRADES if t["ts"] >= cutoff]
    WHALE_TRADES.sort(key=lambda x: x["ts"])
    logger.info(f"Historie geladen: {len(WHALE_TRADES)} Trades aktiv im RAM.")


def sync_group_and_save(trades_copy):
    daily_groups = {}
    for t in trades_copy:
        dt = datetime.fromtimestamp(t["ts"], tz=timezone.utc)
        date_str = dt.strftime('%Y-%m-%d')
        if date_str not in daily_groups:
            daily_groups[date_str] = []
        daily_groups[date_str].append(t)

    for date_str, trades in daily_groups.items():
        filepath = os.path.join(DATA_DIR, f"whale_trades_{date_str}.json")
        tmppath = os.path.join(DATA_DIR, f"whale_trades_{date_str}.tmp")
        try:
            with open(tmppath, "w", encoding="utf-8") as f:
                json.dump(trades, f, separators=(',', ':'))
            os.replace(tmppath, filepath)
        except Exception as e:
            logger.error(f"Schreibfehler bei Datei {filepath}: {e}")


async def save_whales_loop():
    global WHALE_TRADES
    logger.info("Speicher-Job started.")
    while True:
        await asyncio.sleep(SAVE_INTERVAL_SEC)
        try:
            cutoff = time.time() - MAX_AGE_SEC
            WHALE_TRADES = [t for t in WHALE_TRADES if t["ts"] >= cutoff]
            trades_copy = list(WHALE_TRADES)
            await asyncio.to_thread(sync_group_and_save, trades_copy)
            logger.debug(f"💾 Historie gesichert. Im RAM: {len(WHALE_TRADES)}")
        except Exception as e:
            logger.error(f"Fehler im Speicher-Loop: {e}")


# ── P1.42: WS-Sharding-Konfiguration ─────────────────────────────────────
# Binance-Futures (fapi) liefert Combined-Streams mit vielen Streams pro
# Connection nur bis ~200 zuverlässig — mit allen 538 aggTrade-Streams auf
# EINER Connection kamen nur 49/529 Symbole an bzw. die Connection wurde
# komplett abgelehnt (Logger schrieb seit 18.04. gar keine Files mehr).
# Fix: Streams in Chunks à ≤180 auf mehrere Connections sharden
# (538 Symbole → 3 Connections), Muster übernommen aus 1_data_ingestion.py
# (WEBSOCKET-FLEET: Chunking, staggered Startup, Backoff mit Jitter,
# ein asyncio-Task pro Connection).
WHALE_STREAMS_PER_CONN = 180
WHALE_WS_STAGGER_SEC = 3.0  # Versatz zwischen den Connection-Starts
WHALE_RECONNECT_MIN_SEC = 5.0
WHALE_RECONNECT_MAX_SEC = 300.0


async def whale_ws_worker(worker_id: int, streams: list, startup_delay: float = 0.0):
    """Eine WS-Connection für einen Stream-Shard mit eigener Reconnect-Schleife (P1.42).

    Pong-Keepalive und 45s-Watchdog laufen pro Connection — jeder Shard
    überwacht und reconnected sich selbst, unabhängig von den anderen.
    """
    global WHALE_TRADES

    if startup_delay > 0:
        # Staggered Start: Connects zeitlich spreizen (Binance-Connect-Limit
        # + kein gemeinsamer 180s-Ping-Zyklus aller Shards)
        logger.info(f"⏳ Whale WS {worker_id} wartet {startup_delay:.0f}s für staggered start...")
        await asyncio.sleep(startup_delay)

    # URL-encoded combined stream — avoids SUBSCRIBE which Binance drops at ~150s with many streams
    # Binance-Migration 23.04.2026: aggTrade ist ein /market-Stream — die alte
    # ungeroutete URL pushte seitdem nichts mehr (deshalb war der Logger "tot").
    url = "wss://fstream.binance.com/market/stream?streams=" + "/".join(streams)

    _whale_backoff = WHALE_RECONNECT_MIN_SEC
    while True:
        try:
            async with websockets.connect(
                url, ping_interval=None, ping_timeout=None, open_timeout=30, max_size=2**22
            ) as ws:
                _apply_keepalive(ws)
                logger.info(f"🟢 Whale WS {worker_id} connected ({len(streams)} aggTrade streams, URL-encoded)")

                # Backoff nach erfolgreichem Connect zurücksetzen (Audit 09-W2:
                # vorher resettete er nie → dauerhaft gecappte 300s-Reconnect-Waits)
                _whale_backoff = WHALE_RECONNECT_MIN_SEC

                # Unsolicited pong every 120s — keepalive safety net (per Connection)
                async def _whale_pong_task():
                    while True:
                        await asyncio.sleep(120)
                        try:
                            await ws.pong()
                        except Exception:
                            break

                pong_task = asyncio.create_task(_whale_pong_task())
                try:
                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=45)
                        except asyncio.TimeoutError:
                            # No data for 45s — send pong as keepalive (not ping)
                            try:
                                await ws.pong()
                            except Exception:
                                break
                            continue

                        try:
                            payload = json.loads(msg)
                        except Exception:
                            continue

                        if "data" not in payload:
                            continue
                        data = payload["data"]
                        if data.get("e") != "aggTrade":
                            continue

                        qty, price = float(data["q"]), float(data["p"])
                        notional = qty * price
                        if notional < MIN_USD_VALUE:
                            continue

                        direction = "SHORT" if data["m"] else "LONG"
                        trade_record = {
                            "ts": data["T"] / 1000.0,
                            "sym": data["s"],
                            "dir": direction,
                            "usd": round(notional, 2),
                            "prc": price,
                        }
                        WHALE_TRADES.append(trade_record)

                finally:
                    pong_task.cancel()
                    try:
                        await pong_task
                    except (asyncio.CancelledError, Exception):
                        pass

        except Exception as e:
            # Jitter + Worker-Spread, damit die Shards nicht synchron reconnecten
            # (Muster aus 1_data_ingestion.py binance_ws_worker)
            jitter = random.uniform(0.8, 1.2)
            spread_sec = (worker_id - 1) * 2.0
            wait_sec = min(_whale_backoff * jitter, WHALE_RECONNECT_MAX_SEC) + spread_sec
            logger.warning(f"🔴 Whale WS {worker_id} disconnected ({e}). Reconnecting in {wait_sec:.0f}s...")
            await asyncio.sleep(wait_sec)
            _whale_backoff = min(_whale_backoff * 2.0, WHALE_RECONNECT_MAX_SEC)
            continue
        _whale_backoff = WHALE_RECONNECT_MIN_SEC  # reset backoff on clean exit of inner loop


async def whale_ws_listener():
    """Startet die Whale-WS-Fleet: Streams in Shards à ≤180 auf mehrere Connections (P1.42)."""
    coins = load_coins()
    if not coins:
        logger.error("Keine Coins gefunden. Beende Listener.")
        return

    streams = [f"{c.lower()}@aggTrade" for c in coins]

    # P1.42: in Chunks à ≤ WHALE_STREAMS_PER_CONN sharden (538 Symbole → 3 Connections)
    stream_chunks = [streams[i : i + WHALE_STREAMS_PER_CONN] for i in range(0, len(streams), WHALE_STREAMS_PER_CONN)]

    logger.info(
        f"🚀 Whale-WS-Fleet: {len(stream_chunks)} Connections für {len(streams)} Streams "
        f"(≤{WHALE_STREAMS_PER_CONN}/Conn, Stagger {WHALE_WS_STAGGER_SEC:.0f}s)"
    )
    logger.info(f"📡 Whale-Radar lauscht auf Trades > ${MIN_USD_VALUE / 1000:.0f}k...")

    # Ein Task pro Connection, gestaffelter Start
    await asyncio.gather(
        *(
            whale_ws_worker(i + 1, chunk, startup_delay=i * WHALE_WS_STAGGER_SEC)
            for i, chunk in enumerate(stream_chunks)
        )
    )


async def main():
    logger.info("=== 🐳 WHALE LOGGER & EVALUATOR START ===")
    load_existing_whales()

    # Startet das Speichern (alle 5 Min)
    asyncio.create_task(save_whales_loop())

    # Startet die Auswertung (alle 30 Min)
    asyncio.create_task(evaluate_whales_loop())

    # Startet den WebSocket Listener (endlos)
    await whale_ws_listener()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Whale Logger manuell stopped (Strg+C). Rette Daten...")
        trades_copy = list(WHALE_TRADES)
        sync_group_and_save(trades_copy)
        logger.info("✅ Daten erfolgreich gerettet. Shutdown stopped.")
