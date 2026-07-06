# 33_ai_fif1_bot.py — FIF1 "FIFO-Filter-Modell" (Report 15, S11).
"""
Meta-Klassifier über den Fast-In-And-Out-Signalstrom: der Bot liest NEUE
FIFO-Signale aus active_trades_master (der Live-FIFO-Pfad in 3_detectors.py
bleibt unangetastet — sauberer A/B-Vergleich), schätzt mit dem Modell die
Gewinnwahrscheinlichkeit aus Entry-Zeitpunkt-Features (Regime, Richtung,
Markt-Kontext, Signal-Burst-Dichte) und postet nur die Top-Signale unter dem
Tag FIF1 in CH_NEW_IDEAS — mit der ORIGINAL-Geometrie des FIFO-Signals
(Entry/TP1/SL unverändert), damit die Selektion der einzige Unterschied ist.

Evidenz (Report 15 E6): FIFO hat 111k gelabelte Trades, Median +1,25%,
ø −0,13% — das Problem ist Selektion, nicht Tails. Trainer:
tools/fif1_build_dataset.py + tools/new_models_train.py --strategy fif1.

Nicht-geposteten Kandidaten werden als Shadow-Zeilen mitgeschrieben
(ml_predictions_master, posted=false) — das ist die A/B-Basis.

Watchdog: start_delay=215.
"""

import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
import logging
import os
import time

import numpy as np
import pandas as pd

from core import config as _kcfg
from core.database import get_db_connection
from core.model_artifacts import calibrated_confidence, load_artifact, maybe_reload
from core.research_features import (
    CONTEXT_MIN_CANDLES,
    FIF1_FEATURES,
    REGIME_FEATURES,
    assert_features_alive,
    build_fif1_row,
    fetch_context_frame,
)
from core.signal_post import has_open_ai_signal, log_prediction, post_ai_signal

logging.basicConfig(level=logging.INFO, format='%(asctime)s - FIF1_BOT - %(message)s')
logger = logging.getLogger(__name__)

MODEL_ID = "FIF1"
ARTIFACT_PATH = "fif1_model.pkl"
TARGET_CHANNEL_ID = _kcfg.CH_NEW_IDEAS
LIVE_POSTING = os.getenv("NEW_IDEAS_LIVE_POSTING", "1") == "1"
SIGNAL_MAX_AGE_MIN = 10  # Signale älter als das nie verarbeiten (Idle-Catch-up-Guard)
SOURCE_STRATEGY = "Fast In And Out"
ARTIFACT_RETRY_S = 1800

ARTIFACT = load_artifact(ARTIFACT_PATH, FIF1_FEATURES, MODEL_ID)


def ensure_artifact() -> None:
    global ARTIFACT
    if ARTIFACT["loaded"]:
        ARTIFACT = maybe_reload(ARTIFACT, FIF1_FEATURES)
    elif time.time() - ARTIFACT["loaded_at"] > ARTIFACT_RETRY_S:
        ARTIFACT = load_artifact(ARTIFACT_PATH, FIF1_FEATURES, MODEL_ID)


def fetch_latest_regime(conn) -> tuple[dict | None, float]:
    """Jüngste regime_history-Zeile + Alter in Minuten (ts = naive UTC)."""
    with conn.cursor() as cur:
        cur.execute("SELECT ts, regime, confidence FROM regime_history ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
    if row is None:
        return None, 360.0
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    ts = row[0]
    if ts.tzinfo is not None:
        ts = ts.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    age_min = max(0.0, (now - ts).total_seconds() / 60.0)
    return {"regime": row[1], "confidence": row[2]}, age_min


def fifo_burst_counts(conn, symbol: str, direction: str) -> tuple[int, int]:
    """Signal-Burst-Dichte aus BEIDEN Master-Tabellen (Trades wandern von
    active nach closed). Zeitvergleich DB-seitig — die time-Spalten tragen
    PG-Lokalzeit, NOW() castet konsistent in dieselbe Domäne."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM active_trades_master
                WHERE strategy = %(s)s AND coin = %(c)s AND direction = %(d)s
                  AND time > NOW() - INTERVAL '24 hours')
            + (SELECT COUNT(*) FROM closed_trades_master
                WHERE strategy = %(s)s AND coin = %(c)s AND direction = %(d)s
                  AND time > NOW() - INTERVAL '24 hours'),
              (SELECT COUNT(*) FROM active_trades_master
                WHERE strategy = %(s)s AND time > NOW() - INTERVAL '1 hour')
            + (SELECT COUNT(*) FROM closed_trades_master
                WHERE strategy = %(s)s AND time > NOW() - INTERVAL '1 hour')
            """,
            {"s": SOURCE_STRATEGY, "c": symbol, "d": direction},
        )
        row = cur.fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def startup_feature_selfcheck() -> None:
    """P0.12-Muster: Feature-Pipeline auf echten Daten von 3 Coins rechnen."""
    import json

    try:
        with open("coins.json") as f:
            coins = json.load(f)
    except Exception as e:
        logger.critical(f"Selbsttest: coins.json nicht ladbar: {e}")
        exit(1)

    conn = get_db_connection()
    try:
        regime_row, regime_age = fetch_latest_regime(conn)
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        rows, used = [], 0
        for symbol in coins[:15]:
            res = fetch_context_frame(conn, symbol)
            if res is None:
                continue
            df, idx = res
            for back in range(0, 8):
                if idx - back >= CONTEXT_MIN_CANDLES - 1:
                    rows.append(
                        build_fif1_row(
                            "LONG",
                            df,
                            idx - back,
                            regime_row,
                            regime_age,
                            fifo_same_dir_24h=back,
                            fifo_fleet_1h=back,
                            ts=now - datetime.timedelta(hours=back),
                        )
                    )
            used += 1
            if used >= 3:
                break
        assert_features_alive(
            rows,
            FIF1_FEATURES,
            binary_ok={"side_short", *REGIME_FEATURES},
            context=" (FIF1-Startup)",
        )
        logger.info(f"✅ Feature-Selbsttest bestanden ({len(rows)} Zeilen, {used} Coins).")
    except ValueError as e:
        logger.critical(f"❌ {e}")
        exit(1)
    finally:
        conn.close()


def signal_key(sig: dict) -> tuple:
    """Tabellen-agnostischer Dedupe-Key: ein Signal wandert vom Monitor binnen
    Sekunden von active_ nach closed_trades_master (mit NEUER Serial-id) — die
    id taugt deshalb nicht als Union-Watermark."""
    return (
        str(sig["coin"]).upper(),
        str(sig["direction"]).upper(),
        str(sig["time"]),
        float(sig["entry"] or 0),
    )


def fetch_recent_signals(conn) -> list[dict]:
    """FIFO-Signale der letzten SIGNAL_MAX_AGE_MIN Minuten aus BEIDEN
    Master-Tabellen (Review-Fixes 2026-07-06):
      * UNION mit closed: Fast-Resolver (SL/TP < 60s nach Insert) verschwinden
        sonst vor dem nächsten Poll aus active — genau die Verlierer, die der
        Filter lernen soll, würden live systematisch fehlen.
      * Zeitfenster statt id-Watermark: nach Idle-/Ausfall-Phasen wird kein
        Backlog tage-alter Signale mit verfallener Original-Geometrie gepostet
        (Analogon zum 30-min-Guard in Bot 30). Zeitvergleich DB-seitig — die
        time-Spalten tragen PG-Lokalzeit, NOW() castet in dieselbe Domäne."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, time, coin, direction, entry, target1, sl
            FROM active_trades_master
            WHERE strategy = %(s)s AND time > NOW() - INTERVAL '{SIGNAL_MAX_AGE_MIN} minutes'
            UNION ALL
            SELECT id, time, coin, direction, entry, target1, sl
            FROM closed_trades_master
            WHERE strategy = %(s)s AND time > NOW() - INTERVAL '{SIGNAL_MAX_AGE_MIN} minutes'
            ORDER BY time ASC
            """,
            {"s": SOURCE_STRATEGY},
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def process_signal(conn, sig: dict) -> None:
    symbol = str(sig["coin"]).upper()
    direction = str(sig["direction"]).upper()
    entry = float(sig["entry"] or 0)
    target1 = float(sig["target1"] or 0)
    sl = float(sig["sl"] or 0)
    if entry <= 0 or target1 <= 0 or sl <= 0 or direction not in ("LONG", "SHORT"):
        return

    res = fetch_context_frame(conn, symbol)
    if res is None:
        return
    df, idx = res
    regime_row, regime_age = fetch_latest_regime(conn)
    same_dir_24h, fleet_1h = fifo_burst_counts(conn, symbol, direction)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    # same_dir enthält das aktuelle Signal selbst (bereits in active_trades) →
    # −1, damit das Feature wie im Training "andere Signale davor" zählt.
    feature_row = build_fif1_row(
        direction,
        df,
        idx,
        regime_row,
        regime_age,
        fifo_same_dir_24h=max(0, same_dir_24h - 1),
        fifo_fleet_1h=max(0, fleet_1h - 1),
        ts=now,
    )
    missing = [c for c in ARTIFACT["features"] if c not in feature_row]
    if missing:
        raise ValueError(f"Feature-Vertrag verletzt — fehlend: {missing}")
    X = pd.DataFrame([{c: feature_row[c] for c in ARTIFACT["features"]}], dtype=float)
    X = X.replace([np.inf, -np.inf], np.nan).fillna(0)

    prob = float(ARTIFACT["model"].predict_proba(X)[0, 1])
    conf = calibrated_confidence(ARTIFACT, prob)

    logger.info(
        f"FIF1 Kandidat {symbol} {direction} (FIFO #{sig['id']}) | Prob {prob:.3f} (Gate {ARTIFACT['threshold']:.2f})"
    )

    if (
        prob >= ARTIFACT["threshold"]
        and LIVE_POSTING
        and not has_open_ai_signal(conn, symbol, direction, ARTIFACT["tag"])
    ):
        # ORIGINAL-FIFO-Geometrie durchreichen — die Selektion ist der einzige
        # Unterschied zum Quell-Signal (sonst misst der A/B-Vergleich nichts).
        post_ai_signal(
            conn,
            TARGET_CHANNEL_ID,
            ARTIFACT["tag"],
            symbol,
            direction,
            conf,
            entry1=entry,
            entry2=entry,
            sl=sl,
            targets=[target1],
            n_show=1,
            source_desc="AI FIFO Filter Model",
            extra_info_lines=[f"Quelle: Fast In And Out #{sig['id']}"],
        )
        log_prediction(conn, ARTIFACT["tag"], symbol, direction, entry, conf, posted=True, dedup_hours=0)
    else:
        if prob >= ARTIFACT["threshold"] and not LIVE_POSTING:
            logger.info(f"👻 SHADOW-Post {symbol} {direction} (p={prob:.2f}) — Live-Posting deaktiviert.")
        # dedup_hours=0: JEDER FIFO-Kandidat wird geloggt (A/B-Vollständigkeit) —
        # Dedupe übernimmt das Seen-Set des Zeitfenster-Pollings.
        log_prediction(conn, ARTIFACT["tag"], symbol, direction, entry, conf, posted=False, dedup_hours=0)
    conn.commit()


def main() -> None:
    global LIVE_POSTING
    logger.info("=== 🎛️ AI FIF1 BOT (FIFO-Filter, S11) GESTARTET ===")
    if TARGET_CHANNEL_ID == 0:
        logger.warning("CH_NEW_IDEAS nicht gesetzt — erzwinge Shadow-only-Modus.")
        LIVE_POSTING = False
    logger.info(f"Posting: {'LIVE' if LIVE_POSTING else 'SHADOW-ONLY'}")

    startup_feature_selfcheck()

    conn = get_db_connection()
    with conn.cursor() as cur:
        # Das Zeitfenster-Polling scannt beide Master-Tabellen jede Minute —
        # ohne (strategy, time)-Index wären das Seq-Scans über die größte
        # Trade-Tabelle der Fleet (closed: 111k+ FIFO-Zeilen). Gleicher Index
        # trägt auch fifo_burst_counts.
        cur.execute("CREATE INDEX IF NOT EXISTS idx_atm_strategy_time ON active_trades_master (strategy, time)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ctm_strategy_time ON closed_trades_master (strategy, time)")
    conn.commit()
    # Signale, die VOR dem Bot-Start liegen, nicht nachträglich verarbeiten:
    # aktuelles Fenster als gesehen markieren (verhindert auch Doppel-Posts
    # nach einem schnellen Bot-Restart innerhalb des Fensters).
    seen: dict[tuple, datetime.datetime] = {}
    now0 = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    for sig in fetch_recent_signals(conn):
        seen[signal_key(sig)] = now0
    conn.rollback()
    conn.close()
    logger.info(f"Start: {len(seen)} Signale im Fenster als gesehen markiert.")

    while True:
        ensure_artifact()
        if not ARTIFACT["loaded"]:
            time.sleep(60)
            continue

        conn = get_db_connection()
        try:
            signals = fetch_recent_signals(conn)
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            conn_dead = False
            for sig in signals:
                key = signal_key(sig)
                if key in seen:
                    continue
                seen[key] = now  # process-once, auch bei Fehler kein Retry-Loop
                try:
                    process_signal(conn, sig)
                except Exception as e:
                    logger.error(f"Error für {sig.get('coin')} (FIFO #{sig.get('id')}): {e}")
                finally:
                    try:
                        conn.rollback()  # P2.32-Muster; nach Commit ein No-op
                    except Exception:
                        logger.error("Rollback fehlgeschlagen (tote Connection) — Zyklus-Abbruch.")
                        conn_dead = True
                if conn_dead:
                    break
            # Seen-Set beschneiden (Fenster + Marge — bleibt konstant klein)
            cutoff = now - datetime.timedelta(minutes=SIGNAL_MAX_AGE_MIN * 3)
            seen = {k: v for k, v in seen.items() if v >= cutoff}
        except Exception as e:
            logger.error(f"FIF1-Poll-Fehler: {e}")
        finally:
            conn.close()
        time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")
