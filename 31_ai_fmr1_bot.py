# 31_ai_fmr1_bot.py — FMR1 "Funding-Extreme Mean-Reversion" (Report 15, S8).
"""
Cross-sectional Funding-Bot: Coins im obersten Funding-Perzentil (überhitzte
Longs zahlen) sind SHORT-Kandidaten, Coins im untersten Perzentil LONG —
klassische Carry-/Crowding-Unwind-Edge, orthogonal zur restlichen Flotte.
Ein Binär-Modell (tools/fmr1_build_dataset.py + tools/new_models_train.py
--strategy fmr1) gated die Kandidaten auf TP1-vor-SL.

Datenpfade:
  * LIVE: aktuelle Rates cross-sectional aus EINEM REST-Call
    (GET /fapi/v1/premiumIndex, lastFundingRate); Settlement-Historie je
    Kandidat aus GET /fapi/v1/fundingRate — der Bot ist damit unabhängig vom
    Backfill-Zustand der funding_rates-Tabelle.
  * TRAINING: funding_rates-Tabelle (tools/backfill_funding_rates.py) — gleiche
    Quelle (Binance-Settlements), gleiche Statistik-Features (core/research_features).
    Bekannter, dokumentierter Rest-Skew: live gated die *laufende* Rate, im
    Training die *gesettelte* — Details docs/NEW_IDEAS_BOTS.md.

Läuft stündlich (Minute 19). Watchdog: start_delay=199.
"""

import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
import json
import logging
import os
import time

import numpy as np
import pandas as pd
import requests

from core import config as _kcfg
from core.database import get_db_connection
from core.market_utils import check_cooldown, update_cooldown
from core.model_artifacts import calibrated_confidence, load_artifact, maybe_reload
from core.research_features import (
    CONTEXT_MIN_CANDLES,
    FMR1_FEATURES,
    FMR1_LONG_PCTL,
    FMR1_SHORT_PCTL,
    assert_features_alive,
    build_fmr1_row,
    fetch_context_frame,
    funding_stats,
)
from core.signal_post import has_open_ai_signal, log_prediction, post_ai_signal
from core.trade_utils import calculate_smart_targets

logging.basicConfig(level=logging.INFO, format='%(asctime)s - FMR1_BOT - %(message)s')
logger = logging.getLogger(__name__)

MODEL_ID = "FMR1"
ARTIFACT_PATH = "fmr1_model.pkl"
TARGET_CHANNEL_ID = _kcfg.CH_FMR1  # per-Bot-Override, Fallback CH_NEW_IDEAS
LIVE_POSTING = os.getenv("NEW_IDEAS_LIVE_POSTING", "1") == "1"
SHADOW_FLOOR = 0.25
COOLDOWN_HOURS = 24  # Funding-Trades sind langsam (Halten bis Normalisierung)
SCAN_MINUTE = 19  # eigene Minute (2/3/10/11/13 etc. sind belegt)
ARTIFACT_RETRY_S = 1800
MAX_CANDIDATES_PER_SIDE = 40  # Sicherheits-Kappe (5% von ~540 Coins ≈ 27)

PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
FUNDING_HISTORY_URL = "https://fapi.binance.com/fapi/v1/fundingRate"

ARTIFACT = load_artifact(ARTIFACT_PATH, FMR1_FEATURES, MODEL_ID)


def ensure_artifact() -> None:
    global ARTIFACT
    if ARTIFACT["loaded"]:
        ARTIFACT = maybe_reload(ARTIFACT, FMR1_FEATURES)
    elif time.time() - ARTIFACT["loaded_at"] > ARTIFACT_RETRY_S:
        ARTIFACT = load_artifact(ARTIFACT_PATH, FMR1_FEATURES, MODEL_ID)


def load_coin_set() -> set[str]:
    with open("coins.json") as f:
        data = json.load(f)
    coins = data.get("coins", data) if isinstance(data, dict) else data
    return {c.upper() for c in coins if c.upper().endswith("USDT")}


def fetch_cross_section(coin_set: set[str]) -> pd.DataFrame | None:
    """Aktuelle Funding-Rates aller Coins (ein Request) + Perzentil-Rang."""
    try:
        resp = requests.get(PREMIUM_INDEX_URL, timeout=15)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as e:
        logger.error(f"premiumIndex-Fetch fehlgeschlagen: {e}")
        return None
    df = pd.DataFrame(
        [
            {"symbol": r["symbol"], "rate": float(r.get("lastFundingRate") or 0.0)}
            for r in rows
            if r.get("symbol") in coin_set
        ]
    )
    if len(df) < 50:
        logger.error(f"Cross-Section zu dünn ({len(df)} Coins) — Scan übersprungen.")
        return None
    df["pctl"] = df["rate"].rank(pct=True)
    return df


def fetch_funding_history(symbol: str) -> list[float] | None:
    """Settlement-Historie (ASC) für die Statistik-Features — REST, damit der
    Live-Pfad nicht am Backfill-Cron hängt."""
    try:
        resp = requests.get(FUNDING_HISTORY_URL, params={"symbol": symbol, "limit": 100}, timeout=15)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as e:
        logger.warning(f"{symbol}: fundingRate-Historie nicht ladbar: {e}")
        return None
    rates = [float(r["fundingRate"]) for r in sorted(rows, key=lambda r: r["fundingTime"])]
    return rates if len(rates) >= 10 else None


def startup_feature_selfcheck() -> None:
    """P0.12-Muster: Kontext-Features auf echten Daten + REST-Erreichbarkeit."""
    coin_set = load_coin_set()
    cs = fetch_cross_section(coin_set)
    if cs is None:
        # KEIN exit(1): ein transienter Binance-/Netz-Ausfall beim Boot würde
        # sonst einen Watchdog-Restart-Loop erzeugen (Review-Fix 2026-07-06).
        # Der Scan skippt bei fetch_cross_section()==None ohnehin sauber.
        logger.warning("Selbsttest: Funding-Cross-Section nicht abrufbar — Scan skippt, bis Binance antwortet.")

    conn = get_db_connection()
    try:
        rows, used = [], 0
        dummy_stats = {
            "funding_rate_bps": 12.0,
            "funding_z_30d": 2.5,
            "funding_delta_8h_bps": 1.0,
            "funding_sum_3d_bps": 30.0,
        }
        for symbol in sorted(coin_set)[:15]:
            res = fetch_context_frame(conn, symbol)
            if res is None:
                continue
            df, idx = res
            for back in range(0, 8):
                if idx - back >= CONTEXT_MIN_CANDLES - 1:
                    rows.append(build_fmr1_row(dummy_stats, 0.97, "SHORT", df, idx - back))
            used += 1
            if used >= 3:
                break
        assert_features_alive(
            rows,
            FMR1_FEATURES,
            binary_ok={
                "funding_rate_bps",
                "funding_cs_pctl",
                "funding_z_30d",
                "funding_delta_8h_bps",
                "funding_sum_3d_bps",
                "side_short",
            },
            context=" (FMR1-Startup)",
        )
        cs_note = f"{len(cs)} Coins in der Cross-Section" if cs is not None else "Cross-Section ausstehend"
        logger.info(f"✅ Feature-Selbsttest bestanden ({len(rows)} Zeilen, {used} Coins, {cs_note}).")
    except ValueError as e:
        logger.critical(f"❌ {e}")
        exit(1)
    finally:
        conn.close()


def process_candidate(conn, symbol: str, direction: str, rate: float, pctl: float) -> None:
    if check_cooldown(conn, MODEL_ID, symbol, direction, COOLDOWN_HOURS):
        return
    if has_open_ai_signal(conn, symbol, direction, ARTIFACT["tag"]):
        return

    rates = fetch_funding_history(symbol)
    if rates is None:
        return
    # Cross-Section liefert die LAUFENDE Rate — sie ersetzt das letzte Element
    # der Settlement-Historie NICHT, sondern wird angehängt (aktuellster Stand).
    stats = funding_stats(rates + [rate])

    res = fetch_context_frame(conn, symbol)
    if res is None:
        return
    df, idx = res

    feature_row = build_fmr1_row(stats, pctl, direction, df, idx)
    missing = [c for c in ARTIFACT["features"] if c not in feature_row]
    if missing:
        raise ValueError(f"Feature-Vertrag verletzt — fehlend: {missing}")
    X = pd.DataFrame([{c: feature_row[c] for c in ARTIFACT["features"]}], dtype=float)
    X = X.replace([np.inf, -np.inf], np.nan).fillna(0)

    prob = float(ARTIFACT["model"].predict_proba(X)[0, 1])
    conf = calibrated_confidence(ARTIFACT, prob)
    live_price = float(df["close"].iloc[-1])

    logger.info(
        f"FMR1 Funding-Extrem {symbol} {direction} | Rate {rate * 1e4:+.1f} bps "
        f"(Pctl {pctl:.2f}) | Prob {prob:.3f} (Gate {ARTIFACT['threshold']:.2f})"
    )

    if prob >= ARTIFACT["threshold"] and LIVE_POSTING:
        setup = calculate_smart_targets(conn, symbol, direction, live_price)
        post_ai_signal(
            conn,
            TARGET_CHANNEL_ID,
            ARTIFACT["tag"],
            symbol,
            direction,
            conf,
            setup["entry1"],
            setup["entry2"],
            setup["sl"],
            setup["targets"],
            source_desc="AI Funding Mean-Reversion Model",
            extra_info_lines=[f"Funding: {rate * 1e4:+.1f} bps, Perzentil {pctl:.0%}"],
        )
        log_prediction(conn, ARTIFACT["tag"], symbol, direction, live_price, conf, posted=True)
    else:
        if prob >= ARTIFACT["threshold"]:
            logger.info(f"👻 SHADOW-Post {symbol} {direction} (p={prob:.2f}) — Live-Posting deaktiviert.")
        if prob >= SHADOW_FLOOR:
            log_prediction(conn, ARTIFACT["tag"], symbol, direction, live_price, conf, posted=False)
    # Cooldown auf JEDEM gescorten Kandidaten — Spiegel des unbedingten
    # 24h-Dedups im Training (Review-Fix 2026-07-06); committet atomar.
    update_cooldown(conn, MODEL_ID, symbol, direction)


def run_scan() -> None:
    coin_set = load_coin_set()
    cs = fetch_cross_section(coin_set)
    if cs is None:
        return

    shorts = cs[cs["pctl"] >= FMR1_SHORT_PCTL].nlargest(MAX_CANDIDATES_PER_SIDE, "rate")
    longs = cs[cs["pctl"] <= FMR1_LONG_PCTL].nsmallest(MAX_CANDIDATES_PER_SIDE, "rate")
    logger.info(f"🔍 FMR1-Scan: {len(cs)} Coins | {len(shorts)} SHORT- / {len(longs)} LONG-Kandidaten.")

    conn = get_db_connection()
    conn_dead = False
    try:
        for frame, direction in ((shorts, "SHORT"), (longs, "LONG")):
            for row in frame.itertuples():
                try:
                    process_candidate(conn, row.symbol, direction, float(row.rate), float(row.pctl))
                except Exception as e:
                    logger.error(f"Error für {row.symbol}: {e}")
                finally:
                    try:
                        conn.rollback()  # P2.32-Muster; nach Commit ein No-op
                    except Exception:
                        logger.error("Rollback fehlgeschlagen (tote Connection) — Scan-Abbruch.")
                        conn_dead = True
                if conn_dead:
                    break
            if conn_dead:
                break
    finally:
        conn.close()
    logger.info("🏁 FMR1-Scan stopped.")


def main() -> None:
    global LIVE_POSTING
    logger.info("=== 💸 AI FMR1 BOT (Funding-Extreme Mean-Reversion, S8) GESTARTET ===")
    if TARGET_CHANNEL_ID == 0:
        logger.warning("Weder CH_FMR1 noch CH_NEW_IDEAS gesetzt — erzwinge Shadow-only-Modus.")
        LIVE_POSTING = False
    logger.info(f"Posting: {'LIVE' if LIVE_POSTING else 'SHADOW-ONLY'}")

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

    startup_feature_selfcheck()

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        if now.minute == SCAN_MINUTE:
            ensure_artifact()
            if ARTIFACT["loaded"]:
                run_scan()
            else:
                logger.info("Kein FMR1-Artefakt — Scan übersprungen (Idle-Modus).")
            time.sleep(60)
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")
