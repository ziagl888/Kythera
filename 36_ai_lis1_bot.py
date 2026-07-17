# 36_ai_lis1_bot.py — LIS1 "Post-Listing-Drift Fade" (Studie K5, SHADOW-ONLY).
"""
Fade-SHORT einer frisch gelisteten Coin am Tag 3 nach dem Binance-onboardDate.
Hypothese (Report K5 / listing_drift_study): neue Perp-Listings driften
markt-neutral nach unten (Median 7d −8% → 90d −34%, β kippt das Vorzeichen nie);
der handelbare Teil ist ein FRAGILER Fade-SHORT, materiell nur in der Tag-3-Zelle
(Tag 7 kippt negativ, Tag 14 ≈ 0; hohe WR ~0,70 aber tiefer Links-Tail p5 −27%).

**Reiner Shadow-Bot (kein Live-Post).** Es existiert KEIN Modell und KEIN
deploybarer Edge — der Bot validiert das Signal live über überwachte, aber nie
gepostete Trades (`post_shadow_ai_signal` → `ai_signals` OHNE `telegram_outbox`;
der AI-Monitor verfolgt Entry/TP/SL bis zum realisierten Close). Das
LONG-Blacklist-Ergebnis (Alter < 180d ⇒ kein LONG) ist eine Gating-/Risiko-
Entscheidung und wird hier bewusst NICHT umgesetzt (Operator-Gate, Michi).

Signal-Vertrag == Studie `tools/listing_drift_study.py::fade_events` (Zelle
d3|l0.0 = Market-Fill, n=152, Ø +1,07 %/Trade inkl. Funding, WR 0,70):
  * Trigger = reines Alters-Event: Coin erreicht Tag FADE_DAY nach onboardDate.
    Keine Drift-/Return-Schwelle (unbedingt, nur Daten-Guards).
  * onboardDate aus GET /fapi/v1/exchangeInfo (onboardDate ms, immutabel),
    Cache `staging_models/listing_onboard_dates.json`, Fallback erste 1h-Kerze.
  * Geometrie = geteilte `hvn_sr_trade_geometry` (== Studie): SHORT-SL aus
    Resistances über Entry*1,05*1,01, Targets aus Supports unter Entry*0,99,
    `ensure_min_tp_distance(min_pct=0.05)`, 3 veröffentlichte TPs.

Divergenz zur Studie (bewusst, dokumentiert): der Live-Fill ist der aktuelle
Close (Market) statt der exakten Anchor-Kerze — im Dauerbetrieb ≤1h nach dem
Tag-3-Anchor (stündlicher Scan + Fire-once-Dedup). Der Studien-Load-Floor von
120 1h-Zeilen (5d Voll-Historie) gilt NICHT für einen Live-Tag-3-Fill (die Coin
hat dann erst ~72h) — hier zählt der Geometrie-Floor (≥48 1h-Zeilen).

Läuft stündlich (Minute 23). Watchdog: start_delay=239.
"""

import datetime
import json
import logging
import time

from core import config as _kcfg
from core.candles import read_candles
from core.database import get_db_connection
from core.market_utils import check_cooldown, load_coins, update_cooldown
from core.shadow_gate import SHADOW, leg_status, shadow_posting_enabled
from core.signal_post import has_open_ai_signal, post_shadow_ai_signal
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels, hvn_sr_trade_geometry

logging.basicConfig(level=logging.INFO, format="%(asctime)s - LIS1_BOT - %(message)s")
logger = logging.getLogger(__name__)

MODEL_ID = "LIS1"
DIRECTION = "SHORT"  # nur der Fade-SHORT; das LONG-Blacklist-Gate ist Michi-Sache
FADE_DAY = 3  # nur Tag 3 ist materiell positiv (d7 kippt, d14 ≈ 0)
FADE_GRACE_DAYS = 1  # nur Coins im Fenster [3d, 4d) feuern → wirklich neue Coins
MIN_1H_ROWS = 48  # Geometrie-Floor (≥2d 1h), Tag-3-tauglich; NICHT der 120er-Studien-Load-Floor
COOLDOWN_HOURS = 24 * 14  # Tag-3 feuert EINMAL je Coin; langer Cooldown = Gürtel+Hosenträger
SHADOW_CONF = 0.5  # regelbasiert, kein Modell-Prob — neutraler Platzhalter
SCAN_MINUTE = 23  # eigene Minute (0/2/5/10/13/15/19/23 … belegt/frei)

EXCHANGE_INFO_URL = f"{_kcfg.BASE_URL}/fapi/v1/exchangeInfo"
ONBOARD_CACHE = "staging_models/listing_onboard_dates.json"


def fetch_onboard_map() -> dict[str, int]:
    """{SYMBOL: onboard_ms} aus exchangeInfo (frisch), sonst aus dem Cache.

    onboardDate ist je Symbol immutabel → ein veralteter Cache schadet nur bei
    ganz NEUEN, noch nicht gecachten Coins (die fallen dann auf den
    Erste-Kerze-Proxy in :func:`onboard_ts` zurück). Netz-Fehler ⇒ Cache ⇒ {}."""
    try:
        import requests

        r = requests.get(EXCHANGE_INFO_URL, timeout=25)
        r.raise_for_status()
        data = r.json()
        out: dict[str, int] = {}
        for s in data.get("symbols", []):
            ms = s.get("onboardDate")
            if ms and s.get("quoteAsset") == "USDT" and s.get("contractType") == "PERPETUAL":
                out[s["symbol"].upper()] = int(ms)
        if out:
            return out
    except Exception as e:
        logger.warning(f"exchangeInfo nicht abrufbar ({e}) — nutze Onboard-Cache.")
    try:
        with open(ONBOARD_CACHE, encoding="utf-8") as fh:
            blob = json.load(fh)
        return {k.upper(): int(v["onboard_ms"]) for k, v in blob.get("onboard", {}).items() if v.get("onboard_ms")}
    except Exception as e:
        logger.warning(f"Onboard-Cache nicht lesbar ({e}) — nur Erste-Kerze-Proxy.")
        return {}


def in_fade_window(onboard: datetime.datetime, now: datetime.datetime) -> bool:
    """True nur im Alters-Fenster [FADE_DAY, FADE_DAY+GRACE) — der Tag-3-Fade
    feuert genau für WIRKLICH neue Coins (nicht für längst gelistete). Pure
    Funktion, damit die Trigger-Grenze DB-frei testbar ist."""
    age_days = (now - onboard).total_seconds() / 86400.0
    return FADE_DAY <= age_days < FADE_DAY + FADE_GRACE_DAYS


def _onboard_from_ms(ms: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)


def onboard_ts(conn, symbol: str, onboard_map: dict[str, int], df) -> datetime.datetime | None:
    """onboardDate als tz-aware UTC. Primär exchangeInfo/Cache, Fallback die
    erste geschlossene 1h-Kerze (== Studien-Proxy `first_candle_proxy`)."""
    ms = onboard_map.get(symbol.upper())
    if ms:
        return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
    if df is not None and len(df):
        first = df["open_time"].iloc[0]
        ts = first.to_pydatetime() if hasattr(first, "to_pydatetime") else first
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return ts
    return None


def process_coin(conn, symbol: str, onboard_map: dict[str, int], now: datetime.datetime) -> None:
    # 1) Shadow-Gate zuerst (billig, kein DB/Netz): der Bot postet NIE live —
    #    ist das Bein nicht SHADOW, wird es stumm übersprungen (fail-safe zu
    #    Stille, nie zu einem Live-Post — die Regel hat keinen Edge).
    if not shadow_posting_enabled():
        return
    if leg_status(MODEL_ID, DIRECTION) != SHADOW:
        return

    # 2) Alters-Fenster [FADE_DAY, FADE_DAY+GRACE) — filtert ~alle Coins sofort
    #    ohne Kerzen-Read raus. onboard aus der Map (Proxy nur wenn nötig).
    ms = onboard_map.get(symbol.upper())
    if ms is not None and not in_fade_window(_onboard_from_ms(ms), now):
        return

    # 3) Dedup: EINMAL je Coin (offener Shadow-Trade ODER Cooldown aktiv).
    if check_cooldown(conn, MODEL_ID, symbol, DIRECTION, COOLDOWN_HOURS):
        return
    if has_open_ai_signal(conn, symbol, DIRECTION, MODEL_ID):
        return

    # 4) Kerzen + Alters-Check (inkl. Proxy für Coins ohne onboardDate).
    df = read_candles(conn, symbol, "1h", include_forming=False, columns=("open_time", "close"))
    if df is None or len(df) < MIN_1H_ROWS:
        return
    if ms is None:  # kein exchangeInfo-Eintrag → Proxy prüfen
        onboard = onboard_ts(conn, symbol, onboard_map, df)
        if onboard is None or not in_fade_window(onboard, now):
            return

    entry1 = float(df["close"].iloc[-1])
    if entry1 <= 0:
        return

    # 5) Geometrie == Studie: geteilte hvn_sr_trade_geometry (SHORT). Der Fill
    #    ist Market (entry1==entry2 → Zelle l0.0); entry2 der Geometrie (×1,05)
    #    formt nur den SL und wird für den Post verworfen.
    supps, resis = get_hvn_and_sr_levels(conn, symbol, entry1)
    _, sl, t_cands = hvn_sr_trade_geometry(entry1, False, supps, resis)
    targets = ensure_min_tp_distance(t_cands[:20], entry1, False, min_pct=0.05)
    if not targets:
        return

    if post_shadow_ai_signal(conn, MODEL_ID, symbol, DIRECTION, SHADOW_CONF, entry1, entry1, sl, targets, n_show=3):
        logger.info(
            f"👻 LIS1-Shadow SHORT {symbol} | Tag-3-Fade @ {entry1:g} (SL {sl:g}, {len(targets)} TP) — überwacht."
        )
    # Cooldown committet atomar (Shadow-Zeile + Cooldown) — Spiegel des
    # Fire-once-Dedups; nach einem No-op-Post ist es nur der Cooldown-Stempel.
    update_cooldown(conn, MODEL_ID, symbol, DIRECTION)


def run_scan() -> None:
    coins = load_coins("coins.json", usdt_only=True, uppercase=True)
    onboard_map = fetch_onboard_map()
    now = datetime.datetime.now(datetime.timezone.utc)
    # Vorfilter über die Map: nur Coins im Alters-Fenster ODER (selten) ohne
    # onboardDate müssen überhaupt in den teuren Pfad. Spart 527 Kerzen-Reads.
    candidates = [
        c
        for c in coins
        if (c.upper() not in onboard_map) or in_fade_window(_onboard_from_ms(onboard_map[c.upper()]), now)
    ]
    logger.info(
        f"🔍 LIS1-Scan: {len(coins)} Coins, {len(onboard_map)} onboard-Daten, {len(candidates)} Tag-3-Kandidaten."
    )

    conn = get_db_connection()
    conn_dead = False
    try:
        for symbol in candidates:
            try:
                process_coin(conn, symbol, onboard_map, now)
            except Exception as e:
                logger.error(f"Error für {symbol}: {e}")
            finally:
                try:
                    conn.rollback()  # P2.32-Muster; nach dem Cooldown-Commit ein No-op
                except Exception:
                    logger.error("Rollback fehlgeschlagen (tote Connection) — Scan-Abbruch.")
                    conn_dead = True
            if conn_dead:
                break
    finally:
        conn.close()
    logger.info("🏁 LIS1-Scan stopped.")


def main() -> None:
    logger.info("=== 🆕 AI LIS1 BOT (Post-Listing-Drift Fade, K5) GESTARTET — SHADOW-ONLY ===")
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_cooldowns (
                module VARCHAR(50), coin VARCHAR(20), direction VARCHAR(10),
                last_posted_at TIMESTAMP WITH TIME ZONE,
                PRIMARY KEY (module, coin, direction)
            );
        """)
    conn.commit()
    conn.close()

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        if now.minute == SCAN_MINUTE:
            run_scan()
            time.sleep(60)
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")
