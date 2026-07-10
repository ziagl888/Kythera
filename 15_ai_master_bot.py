"""
15_ai_master_bot.py — AIM2 Master-Meta-Gate (ersetzt AIM1, docs/AIM2_DESIGN.md).

AIM1 wurde 2026-07-05 ad acta gelegt (Audit: verlässlich invertierte Kalibrierung,
Note F — audit_reports/dossiers/AIM1.md). AIM2 behält Slot, Channel und
Posting-Flow, aber Modell + Feature-Aufbau sind neu:

  * Features ausschliesslich über core.aim2_features.build_feature_row —
    derselbe Builder wie im Trainer (tools/aim2_build_dataset.py). Kein
    Train/Serve-Skew mehr (P0.13-Fehlermodus).
  * Kandidaten/Schwarm NUR posted=true und OHNE AIM1/AIM2 (F6-Selbst-Feedback-Fix).
  * ml_predictions_master/*_trades_master-Zeiten sind PG-Lokalzeit → UTC-Konvertierung
    (R07-AIM1-a-Fix); Kerzen-Join strikt auf die letzte GESCHLOSSENE 1h-Kerze.
  * Kalibrierte Wahrscheinlichkeit (Isotonic aus dem Artefakt), Threshold aus dem
    Artefakt (Val-Operating-Point), Parity-Guard gegen totes Vokabular.
  * SHADOW-FIRST: Ohne AIM2_LIVE_POSTING=1 in der Umgebung schreibt der Bot nur
    Shadow-Zeilen (posted=false) — Rollout-Gate 2 aus dem Design-Doc.
"""

import datetime
import json
import logging
import os
import time
import warnings
from typing import Any
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")
warnings.filterwarnings("ignore", category=UserWarning, module="xgboost")

from core import config as _kcfg  # channel ids
from core.aim2_features import (
    ATR_COLS,
    CONV_CONFIDENCE_MAPPING,
    MARKET_ABS_COLS,
    MARKET_PRICE_COLS,
    TRAIL_WIN_SQL,
    TRAIL_WINDOW_DAYS,
    build_feature_row,
    parity_nonzero_share,
)
from core.aim2_topn import MODEL_TAG as TOPN_TAG
from core.aim2_topn import TopNCandidate, select_topn
from core.aim2_topn import load_config as load_topn_config
from core.charting import generate_minichart_image
from core.database import get_db_connection
from core.market_utils import get_max_leverage
from core.signal_post import has_open_ai_signal, post_ai_signal
from core.time import utc_now_naive
from core.trade_utils import calculate_smart_targets

logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_MASTER_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG ---
AI_CHANNEL_ID = _kcfg.CH_MASTER
TOPN_CHANNEL_ID = _kcfg.CH_AIM2_TOPN  # eigener Kanal; 0 = ungesetzt → Shadow-only
MODEL_PATH = "master_meta_model_aim2.pkl"
MODEL_NAME = "AIM2"
LIVE_POSTING = os.getenv("AIM2_LIVE_POSTING", "0") == "1"  # Default: Shadow-only
SHADOW_FLOOR = 0.25  # darunter nicht mal Shadow loggen
MODEL_RELOAD_S = 24 * 3600  # R07-AIM1-b: Modell täglich neu laden
CANDIDATE_WINDOW_MIN = 30  # P2.35-Fix: Catch-up-fähiges Fenster, Dedup via processed-Tabelle
MAX_JOIN_STALENESS_H = 3
PARITY_MIN_NONZERO = 0.40  # OOD-Wache (P0.13): zu viele Null-Features → nicht handeln
LOCAL_TZ = ZoneInfo("Europe/Bucharest")

IND_COLS = MARKET_PRICE_COLS + MARKET_ABS_COLS + ATR_COLS + ["trend_direction"]

ARTIFACT: dict[str, Any] = {
    "model": None,
    "features": [],
    "threshold": 0.80,
    "calibrator": None,
    "vocab": set(),
    "loaded_at": 0.0,
}


def load_model() -> None:
    if not os.path.exists(MODEL_PATH):
        logger.warning(
            f"⚠️ AIM2-Artefakt '{MODEL_PATH}' nicht gefunden — Bot wartet "
            f"(Deploy aus staging_models ist eine Operator-Entscheidung)."
        )
        ARTIFACT["model"] = None
        return
    try:
        saved = joblib.load(MODEL_PATH)
        ARTIFACT["model"] = saved["model"]
        ARTIFACT["features"] = saved["features"]
        ARTIFACT["threshold"] = float(saved.get("threshold", 0.80))
        ARTIFACT["calibrator"] = saved.get("calibrator")
        ARTIFACT["vocab"] = set(saved.get("vocab_sources", []))
        ARTIFACT["loaded_at"] = time.time()
        logger.info(
            f"✅ AIM2-Artefakt geladen: {len(ARTIFACT['features'])} Features, "
            f"Threshold {ARTIFACT['threshold']}, Vokabular {len(ARTIFACT['vocab'])} Quellen, "
            f"Posting={'LIVE' if LIVE_POSTING else 'SHADOW-ONLY'}"
        )
    except Exception as e:
        logger.error(f"❌ Fehler beim Laden des AIM2-Artefakts: {e}")
        ARTIFACT["model"] = None


def to_utc_naive(series: pd.Series) -> pd.Series:
    """Naive PG-Lokalzeit (Europe/Bucharest) → naive UTC (vermessen 2026-07-05)."""
    s = pd.to_datetime(series, errors="coerce")
    s = s.dt.tz_localize(LOCAL_TZ, nonexistent="shift_forward", ambiguous="NaT")
    return s.dt.tz_convert("UTC").dt.tz_localize(None)


def df_from_query(conn, query, params=None):
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        columns = [desc[0] for desc in cur.description]
        return pd.DataFrame(rows, columns=columns)


def load_signal_stream(conn, since_utc_naive) -> pd.DataFrame:
    """Posted-AI- + Conv-Signale (Schwarm-Basis UND Kandidaten-Obermenge).
    Identische Definition wie im Trainer: posted=true, ohne AIM1/AIM2.

    Der AIM2-TOPN-Tag wird ebenfalls ausgeschlossen: die Top-N-Zeilen sind
    Meta-Gate-Ausgaben derselben Pipeline, kein Basissignal — sie als Kandidat
    oder in den Schwarm zurückzulassen wäre dieselbe F6-Selbst-Feedback-Schleife
    wie bei AIM1/AIM2 (T-2026-CU-9050-051)."""
    since_local = (
        pd.Timestamp(since_utc_naive).tz_localize("UTC").tz_convert(LOCAL_TZ).tz_localize(None)
    ).to_pydatetime()

    ai = df_from_query(
        conn,
        """
        SELECT id, model_name AS source, time, coin, direction, entry, confidence
        FROM ml_predictions_master
        WHERE posted = true AND model_name NOT IN ('AIM1', 'AIM2', %s) AND time > %s
        """,
        (TOPN_TAG, since_local),
    )
    if not ai.empty:
        ai["source_type"] = "ai"

    conv = df_from_query(
        conn,
        """
        SELECT id, strategy AS source, time, coin, direction, entry, NULL AS confidence
        FROM active_trades_master WHERE time > %s
        UNION ALL
        SELECT id, strategy AS source, time, coin, direction, entry, NULL AS confidence
        FROM closed_trades_master WHERE time > %s
        """,
        (since_local, since_local),
    )
    if not conv.empty:
        conv["source_type"] = "conv"

    stream = (
        pd.concat([d for d in (ai, conv) if not d.empty], ignore_index=True)
        if (not ai.empty or not conv.empty)
        else pd.DataFrame()
    )
    if stream.empty:
        return stream

    stream["ts"] = to_utc_naive(stream["time"])
    stream = stream.dropna(subset=["ts", "coin", "direction"])
    stream["symbol"] = stream["coin"].astype(str).str.upper().str.replace(r"_\d+[mhdwM]$", "", regex=True)
    stream = stream[stream["symbol"].str.endswith("USDT")]
    stream["direction"] = stream["direction"].astype(str).str.upper()
    stream = stream[stream["direction"].isin(["LONG", "SHORT"])]
    stream["entry"] = pd.to_numeric(stream["entry"], errors="coerce")
    stream["confidence"] = pd.to_numeric(stream["confidence"], errors="coerce")
    return stream.sort_values("ts").reset_index(drop=True)


def swarm_stats(stream: pd.DataFrame, symbol: str, ts, direction: str) -> dict:
    out = {
        "total_5d": 0,
        "long_5d": 0,
        "short_5d": 0,
        "latest_age_h": 120.0,
        "confl_same_dir_4h": 0,
        "distinct_src_same_dir_4h": 0,
    }
    if stream.empty:
        return out
    g = stream[(stream["symbol"] == symbol) & (stream["ts"] < ts) & (stream["ts"] >= ts - pd.Timedelta(days=5))]
    if g.empty:
        return out
    out["total_5d"] = len(g)
    out["long_5d"] = int((g["direction"] == "LONG").sum())
    out["short_5d"] = out["total_5d"] - out["long_5d"]
    out["latest_age_h"] = float((ts - g["ts"].max()).total_seconds() / 3600.0)
    g4 = g[(g["ts"] >= ts - pd.Timedelta(hours=4)) & (g["direction"] == direction)]
    out["confl_same_dir_4h"] = len(g4)
    out["distinct_src_same_dir_4h"] = int(g4["source"].nunique())
    return out


def load_trail_map(conn) -> dict:
    """Trailing-WR je AI-Quelle (30d, dedupliziert) — Semantik == Trainer (TRAIL_WIN_SQL)."""
    df = df_from_query(
        conn,
        f"""
        SELECT model, count(*) AS n, sum(CASE WHEN win THEN 1 ELSE 0 END) AS wins
        FROM (
            SELECT model, bool_or({TRAIL_WIN_SQL}) AS win
            FROM closed_ai_signals
            WHERE close_time > now()::timestamp - INTERVAL '{TRAIL_WINDOW_DAYS} days'
            GROUP BY model, symbol, direction, open_time, close_time
        ) d GROUP BY model
        """,
    )
    if df.empty:
        return {}
    return {r["model"]: (float(r["wins"]) / float(r["n"]) if r["n"] else 0.5, int(r["n"])) for _, r in df.iterrows()}


def load_market_row(conn, symbol: str, ts) -> tuple[dict, float] | None:
    """Letzte GESCHLOSSENE 1h-Kerze vor floor(ts) — Indikatoren + Close."""
    floor_utc = pd.Timestamp(ts).floor("h").tz_localize("UTC").to_pydatetime()
    cols = ", ".join(f't2."{c}"' for c in IND_COLS)
    try:
        df = df_from_query(
            conn,
            f'SELECT t1.open_time, t1.close, {cols} FROM "{symbol}_1h" t1 '
            f'LEFT JOIN "{symbol}_1h_indicators" t2 ON t1.open_time = t2.open_time '
            f"WHERE t1.open_time < %s ORDER BY t1.open_time DESC LIMIT 1",
            (floor_utc,),
        )
    except Exception:
        conn.rollback()
        return None
    if df.empty:
        return None
    row = df.iloc[0]
    open_time = pd.Timestamp(row["open_time"])
    if open_time.tzinfo is not None:
        open_time = open_time.tz_convert("UTC").tz_localize(None)
    if (pd.Timestamp(ts).floor("h") - open_time) > pd.Timedelta(hours=MAX_JOIN_STALENESS_H):
        return None  # Datenlücke — lieber kein Urteil als eines auf alten Kerzen
    close = float(row["close"]) if pd.notna(row["close"]) else 0.0
    if close <= 0:
        return None
    return {c: row[c] for c in IND_COLS}, close


def load_latest_regime(conn) -> tuple[dict | None, float]:
    df = df_from_query(
        conn,
        """
        SELECT ts, regime, alt_context, confidence, confidence_btc, confidence_alt,
               btc_return_1h, btc_return_4h, btc_atr_1h_pct, btc_atr_4h_pct, btcdom_return_24h
        FROM regime_history ORDER BY ts DESC LIMIT 1
        """,
    )
    if df.empty:
        return None, 360.0
    row = df.iloc[0].to_dict()
    now_utc = utc_now_naive()  # regime_history.ts ist naiv-UTC; utcnow() ist deprecated
    age_min = max(0.0, (now_utc - pd.Timestamp(row["ts"]).to_pydatetime()).total_seconds() / 60.0)
    return row, age_min


def count_topn_posts_24h(conn, now_utc_naive) -> int:
    """Rolling-24h-Zähler der AIM2-TOPN-Zeilen in ml_predictions_master.

    Zählt Shadow UND Live (jede Selektion schreibt eine Zeile), damit die
    harte Tages-Kappe im Shadow exakt so greift wie live — der Shadow ist so
    eine getreue Vorschau. `time` ist PG-Lokalzeit (Bucharest); der Cutoff wird
    identisch zu load_signal_stream nach lokal konvertiert (R3-Vertrag)."""
    since_local = (
        pd.Timestamp(now_utc_naive - pd.Timedelta(hours=24)).tz_localize("UTC").tz_convert(LOCAL_TZ).tz_localize(None)
    ).to_pydatetime()
    df = df_from_query(
        conn,
        "SELECT count(*) AS n FROM ml_predictions_master WHERE model_name = %s AND time > %s",
        (TOPN_TAG, since_local),
    )
    if df.empty:
        return 0
    return int(df.iloc[0]["n"])


def process_master_trades():
    if ARTIFACT["model"] is None:
        logger.warning("AIM2 skipped: Artefakt nicht geladen.")
        return

    logger.info("🔄 Starte Master-Meta-Analyse (AIM2)…")
    conn = get_db_connection()
    current_time = datetime.datetime.now(datetime.timezone.utc)
    now_utc_naive = pd.Timestamp(current_time).tz_convert("UTC").tz_localize(None)

    try:
        stream = load_signal_stream(conn, now_utc_naive - pd.Timedelta(days=5))
        if stream.empty:
            logger.info("ℹ️ Keine Signale im 5-Tage-Fenster.")
            return

        candidates = stream[stream["ts"] > now_utc_naive - pd.Timedelta(minutes=CANDIDATE_WINDOW_MIN)]
        if candidates.empty:
            logger.info("ℹ️ Keine neuen Signale im Kandidaten-Fenster.")
            return

        processed_df = df_from_query(
            conn,
            "SELECT signal_type, signal_id FROM master_ai_processed_signals WHERE processed_at > %s",
            ((current_time - datetime.timedelta(days=5)),),
        )
        processed = (
            set(zip(processed_df["signal_type"], processed_df["signal_id"], strict=True))
            if not processed_df.empty
            else set()
        )
        type_key = {"ai": "ai_signal", "conv": "conv_signal"}
        candidates = candidates[
            [
                (type_key[st], sid) not in processed
                for st, sid in zip(candidates["source_type"], candidates["id"], strict=True)
            ]
        ]
        if candidates.empty:
            logger.info("ℹ️ Alle Kandidaten bereits verarbeitet.")
            return

        logger.info(f"🔎 Analysiere {len(candidates)} neue Quellsignale…")
        trail_map = load_trail_map(conn)
        regime_row, regime_age = load_latest_regime(conn)
        market_cache: dict[str, tuple[dict, float] | None] = {}

        processed_inserts, shadow_inserts = [], []
        vocab_misses = 0

        # AIM2-TOPN (T-2026-CU-9050-051): eigener High-Conviction-Kanal hinter
        # default-off-Gate. Wir sammeln die starken, vertrauenswürdigen
        # Kandidaten dieses Zyklus und selektieren NACH der Schleife die Top-N
        # unter der rollierenden 24h-Kappe. topn_min: nie unter dem Basis-Gate.
        topn_cfg = load_topn_config()
        topn_min = max(topn_cfg.min_prob, ARTIFACT["threshold"])
        topn_pool: list[TopNCandidate] = []

        for signal in candidates.itertuples():
            coin, ts, direction = signal.symbol, signal.ts, signal.direction

            if coin not in market_cache:
                market_cache[coin] = load_market_row(conn, coin, ts)
            market = market_cache[coin]
            if market is None:
                continue
            market_row, close_price = market

            if signal.source_type == "ai":
                wr, n = trail_map.get(signal.source, (0.5, 0))
                conf = float(signal.confidence) if pd.notna(signal.confidence) else 0.5
            else:
                wr, n = 0.5, 0
                conf = CONV_CONFIDENCE_MAPPING.get(signal.source, 0.5)

            src_entry = float(signal.entry) if pd.notna(signal.entry) and signal.entry else 0.0
            drift = ((close_price - src_entry) / src_entry * 100.0) if src_entry > 0 else 0.0
            drift = max(-50.0, min(50.0, drift))

            feats = build_feature_row(
                market_row,
                close_price,
                regime_row,
                regime_age,
                swarm_stats(stream, coin, ts, direction),
                {
                    "name": signal.source,
                    "type": signal.source_type,
                    "conf": conf,
                    "trail_wr_30d": wr,
                    "trail_n_30d": n,
                    "entry_drift_pct": drift,
                    "direction": direction,
                },
            )

            if ARTIFACT["vocab"] and str(signal.source) not in ARTIFACT["vocab"]:
                vocab_misses += 1  # P0.13-Wache: neue Quelle, die das Modell nie sah

            X = (
                pd.DataFrame([feats])
                .reindex(columns=ARTIFACT["features"], fill_value=0.0)
                .apply(pd.to_numeric, errors="coerce")
                .fillna(0.0)
                .astype("float32")
            )
            raw = float(ARTIFACT["model"].predict_proba(X)[0][1])
            prob = (
                float(ARTIFACT["calibrator"].predict(np.array([raw]))[0]) if ARTIFACT["calibrator"] is not None else raw
            )

            parity = parity_nonzero_share(X.iloc[0].tolist(), ARTIFACT["features"])
            trusted = parity >= PARITY_MIN_NONZERO
            if not trusted:
                logger.warning(
                    f"⚠️ Parity-Guard {coin}/{signal.source}: nur {parity:.0%} "
                    f"Nicht-Null-Features → OOD-Verdacht, kein Posting."
                )

            processed_inserts.append((type_key[signal.source_type], int(signal.id), prob))

            if topn_cfg.enabled and trusted and prob >= topn_min:
                topn_pool.append(
                    TopNCandidate(
                        coin=coin,
                        direction=direction,
                        prob=prob,
                        trusted=trusted,
                        source=str(signal.source),
                        close_price=close_price,
                    )
                )

            if prob < SHADOW_FLOOR:
                continue

            wants_post = prob >= ARTIFACT["threshold"] and trusted
            if not (wants_post and LIVE_POSTING):
                shadow_inserts.append((MODEL_NAME, current_time, coin, direction, close_price, prob, False))
                if wants_post:
                    logger.info(
                        f"👻 SHADOW-Post {coin} {direction} (p={prob:.2f}, "
                        f"Quelle {signal.source}) — Live-Posting deaktiviert."
                    )
                continue

            # --- LIVE-POSTING (nur mit AIM2_LIVE_POSTING=1 nach der Shadow-Phase) ---
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM ai_signals WHERE symbol = %s AND direction = %s AND model = %s",
                    (coin, direction, MODEL_NAME),
                )
                if cur.fetchone():
                    logger.info(f"⏳ Skip {coin} {direction}: aktiver {MODEL_NAME}-Trade läuft.")
                    shadow_inserts.append((MODEL_NAME, current_time, coin, direction, close_price, prob, False))
                    continue

            trade_setup = calculate_smart_targets(conn, coin, direction, close_price)
            entry1, entry2 = trade_setup['entry1'], trade_setup['entry2']
            sl, targets = trade_setup['sl'], trade_setup['targets']
            lev = get_max_leverage(coin, 20)

            lines = [
                f"📈 Signal for {coin} 📈",
                f"🚨 Direction: {direction}",
                f"🚨 Leverage: {lev}",
                "🚨 Margin: Cross",
                f"🏦 CMP Entry: $ {entry1:.8f}",
                f"🏦 Entry 2: $ {entry2:.8f}",
            ]
            for i, t in enumerate(targets[:3], 1):
                lines.append(f"💰 TP{i}: $ {t:.8f}")
            lines += [f"💸 Stop Loss: $ {sl:.8f}", "🧠 Trade idea verified by Master AI module (AIM2) V1"]
            cornix_msg = "\n".join(lines)

            html_caption = (
                f"<pre><b>💎 MASTER AI TRADE (AIM2)</b>\n"
                f"<b>{coin.replace('USDT', '')}/USDT</b>\n"
                f"<b>→ Direction: <b>{direction}</b></b>\n"
                f"<b>→ Source: {signal.source} (Conf {conf:.2f})</b>\n"
                f"<b>→ Master Confidence (kalibriert): <b>{prob:.1%}</b></b>\n"
                f"<b>→ Time: {current_time.strftime('%H:%M')} UTC | Module: AIM2</b>\n\n"
                f"{cornix_msg}</pre>"
            )
            chart_buf = generate_minichart_image(coin, minutes=240)

            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (AI_CHANNEL_ID, cornix_msg)
                )
                if chart_buf:
                    cur.execute(
                        "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                        (AI_CHANNEL_ID, html_caption, chart_buf),
                    )
                else:
                    cur.execute(
                        "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                        (AI_CHANNEL_ID, html_caption),
                    )
                cur.execute(
                    """
                    INSERT INTO ai_signals (symbol, price, model, direction, confidence,
                                            entry1, entry2, sl, targets)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        coin,
                        entry1,
                        MODEL_NAME,
                        direction,
                        float(prob),
                        float(entry1),
                        float(entry2),
                        float(sl),
                        json.dumps(targets),
                    ),
                )
            shadow_inserts.append((MODEL_NAME, current_time, coin, direction, close_price, prob, True))
            logger.info(f"✅ AIM2 MASTER ALERT {coin} {direction} (p={prob:.1%}, Quelle {signal.source})")

        if vocab_misses:
            logger.warning(
                f"⚠️ {vocab_misses} Kandidaten von Quellen ausserhalb des "
                f"Trainings-Vokabulars — bei Häufung: Retrain (P0.13-Wache)."
            )

        # --- AIM2-TOPN: Top-N des Tages unter rollierender 24h-Kappe ---
        if topn_cfg.enabled and topn_pool:
            posts_24h = count_topn_posts_24h(conn, now_utc_naive)
            selected = select_topn(topn_pool, topn_cfg.n, topn_min, posts_24h)
            topn_live = topn_cfg.live and TOPN_CHANNEL_ID != 0
            if topn_cfg.live and TOPN_CHANNEL_ID == 0:
                logger.warning("AIM2-TOPN: Live-Gate an, aber CH_AIM2_TOPN ungesetzt → Shadow-only.")
            for cand in selected:
                # Läuft für Coin/Richtung bereits ein TOPN-Trade: Slot als Shadow
                # verbuchen (Kappe bleibt ehrlich), aber nicht doppelt posten.
                if topn_live and has_open_ai_signal(conn, cand.coin, cand.direction, TOPN_TAG):
                    shadow_inserts.append(
                        (TOPN_TAG, current_time, cand.coin, cand.direction, cand.close_price, cand.prob, False)
                    )
                    logger.info(f"⏳ AIM2-TOPN Skip {cand.coin} {cand.direction}: aktiver Trade läuft.")
                    continue
                if topn_live:
                    setup = calculate_smart_targets(conn, cand.coin, cand.direction, cand.close_price)
                    post_ai_signal(
                        conn,
                        TOPN_CHANNEL_ID,
                        TOPN_TAG,
                        cand.coin,
                        cand.direction,
                        cand.prob,
                        setup["entry1"],
                        setup["entry2"],
                        setup["sl"],
                        setup["targets"],
                        source_desc=f"AIM2 Top-{topn_cfg.n} des Tages · Quelle {cand.source}",
                        extra_info_lines=[f"Master-Confidence (kalibriert): {cand.prob:.1%}"],
                    )
                    shadow_inserts.append(
                        (TOPN_TAG, current_time, cand.coin, cand.direction, cand.close_price, cand.prob, True)
                    )
                    logger.info(
                        f"💎 AIM2-TOPN LIVE {cand.coin} {cand.direction} (p={cand.prob:.1%}, Quelle {cand.source})"
                    )
                else:
                    shadow_inserts.append(
                        (TOPN_TAG, current_time, cand.coin, cand.direction, cand.close_price, cand.prob, False)
                    )
                    logger.info(
                        f"👻 AIM2-TOPN SHADOW {cand.coin} {cand.direction} "
                        f"(p={cand.prob:.1%}, Quelle {cand.source}) — Live-Posting deaktiviert."
                    )

        with conn.cursor() as cur:
            if processed_inserts:
                cur.executemany(
                    """
                    INSERT INTO master_ai_processed_signals (signal_type, signal_id, ml_confidence, processed_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (signal_type, signal_id)
                    DO UPDATE SET processed_at = NOW(), ml_confidence = EXCLUDED.ml_confidence
                    """,
                    processed_inserts,
                )
            if shadow_inserts:
                cur.executemany(
                    """
                    INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                    VALUES (0, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    shadow_inserts,
                )
        conn.commit()

    except Exception as e:
        logger.error(f"Critical error im Master Task: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

    logger.info("🏁 AIM2 Master-Analyse fertig.")


def main():
    logger.info(f"=== 🧠 AI MASTER BOT (AIM2) GESTARTET — {'LIVE-POSTING' if LIVE_POSTING else 'SHADOW-ONLY'} ===")
    _topn = load_topn_config()
    if _topn.enabled:
        _topn_mode = "LIVE" if (_topn.live and TOPN_CHANNEL_ID != 0) else "SHADOW-ONLY"
        logger.info(f"    AIM2-TOPN aktiv — N={_topn.n}, min_prob={_topn.min_prob:.2f}, Posting={_topn_mode}")
    else:
        logger.info("    AIM2-TOPN deaktiviert (AIM2_TOPN_ENABLED≠1) — Basis-AIM2 unverändert.")

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS master_ai_processed_signals (
                signal_type TEXT NOT NULL,
                signal_id BIGINT NOT NULL,
                processed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                ml_confidence NUMERIC(5, 4),
                PRIMARY KEY (signal_type, signal_id)
            );
        """)
    conn.commit()
    conn.close()

    load_model()

    while True:
        if time.time() - ARTIFACT["loaded_at"] > MODEL_RELOAD_S:
            load_model()
        process_master_trades()
        time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell gestoppt (Strg+C). Fahre sauber herunter…")
