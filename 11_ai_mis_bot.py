import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
import json
import logging
import os
import time

import joblib
import numpy as np
import pandas as pd

from core import config as _kcfg  # channel ids
from core.candles import history_start, read_candles_with_indicators
from core.charting import generate_minichart_image
from core.database import get_db_connection
from core.live_price import get_live_price, get_live_prices_batch
from core.market_utils import check_cooldown, get_max_leverage, update_cooldown
from core.mis_features import (
    BINARY_FLAG_FEATURES,
    MIS_INDICATOR_COLUMNS,
    MIS_RENAME_MAP,
    add_advanced_features,
    assert_features_alive,
)
from core.trade_utils import calculate_smart_targets

logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_MIS_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG & CHANNELS (Dynamisches Routing) ---
# Hier trägst du die 4 unterschiedlichen Channel-IDs ein!
MIS_CHANNELS = {
    "8H": _kcfg.CH_MIS_8H,  # 👈 Channel für 8h
    "24H": _kcfg.CH_MIS_24H,  # 👈 Channel für 24h
    "72H": _kcfg.CH_MIS_72H,  # 👈 Channel für 72h
    "168H": _kcfg.CH_MIS_168H,  # 👈 Channel für 168h
}

# --- LOAD ML MODELS ---
# MIS2 (Operator-Entscheide 2026-07-06, docs/MODEL_INTENT.md §1):
#   * Move-Label-Modelle (±5%/8h, ±10%/24h, ±15%/72h, ±25%/168h) ersetzen die
#     alten MIS1-Modelle KOMPLETT — MIS1 ist abgeschaltet, kein Legacy-Fallback.
#   * NUR die Pump-Seite ist deploybar (alle 4 Horizonte mit Out-of-Time-Ertrag);
#     die Dump-Seite erkennt Dumps zwar gut, verdient aber mit der Short-Geometrie
#     nichts — sie wird separat überarbeitet (eigener Task).
#   * Basis-Mix nach Testlage: Close-Labels für 8h/24h/168h, Wick für 72h.
#   Artefakt = dict(model, features, optimal_threshold, calibrator_isotonic, meta)
#   aus tools/retrain_from_replay.py --label-mode move.
# MODEL_GENERATION ist NUR noch der laute Fallback-Tag, falls ein Artefakt keine
# meta.model_id trägt — der Posting-Tag kommt aus dem Artefakt (Versionierungs-
# Regel, T-2026-CU-9050-030). Die Dateinamen sind bewusst generationsfreie SLOTS
# (Operator-Entscheid 2026-07-09): ein MIS3-Rollout überschreibt mis2_model_*.pkl,
# und der Bot postet allein anhand der neuen meta.model_id als MIS3-72H.
MODEL_GENERATION = "MIS2"
PUMP_MODELS = {
    key: {
        "artifact_path": f"mis2_model_{key}.pkl",  # Slot-Name, KEINE Generations-Angabe
        "model": None,
        "threshold": 0.5,
        "features": None,
        "calibrator": None,
        "generation": MODEL_GENERATION,
        "loaded": False,
    }
    for key in ("8h_pump", "24h_pump", "72h_pump", "168h_pump", "8h_dump", "24h_dump", "72h_dump", "168h_dump")
}

# MIS2-SHORT-Regeln je Horizont (Operator-Entscheide 2026-07-06 abends +
# Geometrie-Studie V2, staging_models/mis2_dump_geometry_study_v2.json):
#   * Entry = LIMIT-Sell "bounce_pct" ÜBER dem Signalkurs — in den Aufwärts-
#     Zuck hinein verkaufen (der riss vorher die Stops; Fill-Quote 78-88 %).
#   * TP rechnet ab SIGNALKURS (die Move-Prognose zählt ab Signalzeitpunkt),
#     SL ab Entry. Ein einzelnes TP — exakt die simulierte Geometrie.
#   * Hebel: hartes 20x-Posting per Operator-Entscheid (Cross-Margin, kleine
#     Positionen auf großes Depot) — bewusst KEIN cap_leverage_to_sl, obwohl
#     SL 12-16 % über der Isolated-Liquidationsdistanz liegt.
#   * 8H ist laut Studie negativ (−0,24 %/Trade) — Operator will den
#     Live-Beweis ("evtl stimmen die modelle nicht 100%"), dokumentiert in
#     docs/MODEL_INTENT.md §1.
DUMP_RULES = {
    "8H": {"bounce_pct": 5.0, "tp_pct": 5.0, "sl_pct": 5.0},  # Studie: −0,24 %/Trade
    "24H": {"bounce_pct": 5.0, "tp_pct": 10.0, "sl_pct": 16.0},  # Studie: +0,49 %/Trade
    "72H": {"bounce_pct": 5.0, "tp_pct": 15.0, "sl_pct": 12.0},  # Studie: +0,72 %/Trade
    "168H": {"bounce_pct": 5.0, "tp_pct": 16.7, "sl_pct": 12.0},  # Studie: +0,27 %/Trade
}


def load_pump_models():
    """Lädt die MIS2-Move-Artefakte (kein Legacy-Fallback — MIS1 ist aus)."""
    loaded_count = 0
    for key, cfg in PUMP_MODELS.items():
        try:
            if os.path.exists(cfg["artifact_path"]):
                art = joblib.load(cfg["artifact_path"])
                cfg["model"] = art["model"]
                cfg["threshold"] = float(art["optimal_threshold"])
                cfg["features"] = list(art["features"])
                cfg["calibrator"] = art.get("calibrator_isotonic")
                # Posting-Tag aus der Artefakt-Meta (Versionierungs-Regel): ein
                # MIS3-Retrain im selben Slot muss als MIS3-* posten, sonst
                # verschmilzt seine Per-Bot-Statistik mit MIS2 und das
                # Orchestrator-Gating entscheidet über die neue Generation
                # anhand der Performance der alten (T-2026-CU-9050-030).
                model_id = str((art.get("meta") or {}).get("model_id") or "").strip()
                if model_id:
                    cfg["generation"] = model_id
                else:
                    logger.error(
                        f"⚠️ {cfg['artifact_path']}: meta.model_id fehlt — poste unter Fallback-Tag "
                        f"{MODEL_GENERATION}. Ein Retrain-Artefakt OHNE model_id taggt seine Trades falsch."
                    )
                    cfg["generation"] = MODEL_GENERATION
                cfg["loaded"] = True
                loaded_count += 1
            else:
                logger.warning(f"Modell fehlt: {cfg['artifact_path']}")
        except Exception as e:
            logger.error(f"Error loading von {key}: {e}")

    generations = sorted({cfg["generation"] for cfg in PUMP_MODELS.values() if cfg["loaded"]})
    logger.info(
        f"✅ {loaded_count}/{len(PUMP_MODELS)} Multi-Horizon Modelle ({'/'.join(generations)}) loaded successfully."
    )
    if len(generations) > 1:
        # Gemischter Rollout (z. B. 72H schon MIS3, Rest noch MIS2). Kein Fehler —
        # jedes Signal postet unter der Generation SEINES Modells —, aber sichtbar machen.
        logger.warning(f"Gemischte Modell-Generationen geladen: {generations}")

    # FIX: Thresholds explizit loggen, damit Drift zwischen Modell-File und
    # Threshold-File sofort auffällt.
    thresh_summary = ", ".join(f"{h}={cfg['threshold']:.2f}" for h, cfg in PUMP_MODELS.items() if cfg["loaded"])
    logger.info(f"{'/'.join(generations) or MODEL_GENERATION} Thresholds: {thresh_summary}")


def startup_feature_selfcheck():
    """P0.12-Muster (wie 18_ai_abr1_bot): Feature-Pipeline auf echten Daten von
    bis zu 3 Coins rechnen und hart abbrechen, wenn ein kontinuierliches Feature
    konstant ist oder ein geladenes Modell Features verlangt, die der (bereinigte)
    Builder nicht mehr liefert — Legacy-67-Feature-Modelle mit den Leakage-
    Spalten werden dabei entladen statt still mit fillna(0)-Nullen zu scoren."""
    try:
        with open('coins.json') as f:
            coins = json.load(f)
    except Exception as e:
        logger.critical(f"Selbsttest: coins.json nicht ladbar: {e}")
        exit(1)

    conn = get_db_connection()
    try:
        frames = []
        for symbol in coins[:10]:
            df = _fetch_mis_frame(conn, symbol)
            if df is None or len(df) < 30:
                continue
            frames.append(add_advanced_features(df))
            if len(frames) >= 3:
                break
        if not frames:
            logger.critical("❌ Feature-Selbsttest: keine verwertbaren Daten gefunden — Abbruch.")
            exit(1)
        sample = pd.concat(frames, ignore_index=True)
        try:
            assert_features_alive(sample, context=" (Bot-Startup)")
        except ValueError as e:
            logger.critical(f"❌ {e}")
            exit(1)
        constant_flags = [c for c in BINARY_FLAG_FEATURES if sample[c].nunique(dropna=False) <= 1]
        if constant_flags:
            logger.warning(
                f"Selbsttest: Binär-Flags konstant über die Stichprobe (kann legitim sein): {constant_flags}"
            )

        for key, cfg in PUMP_MODELS.items():
            if not cfg["loaded"]:
                continue
            missing = [c for c in (cfg["features"] or []) if c not in sample.columns]
            if missing:
                logger.critical(
                    f"❌ {key}: Modell verlangt Features, die der Builder nicht liefert "
                    f"(vermutlich Legacy-Leakage-Spalten, Report 13): {missing[:6]}… — Modell entladen."
                )
                cfg["loaded"] = False
                cfg["model"] = None
        if not any(cfg["loaded"] for cfg in PUMP_MODELS.values()):
            logger.critical("❌ Kein kompatibles MIS1-Modell übrig — Abbruch.")
            exit(1)
        n_ok = sum(1 for cfg in PUMP_MODELS.values() if cfg["loaded"])
        logger.info(
            f"✅ Feature-Selbsttest bestanden ({len(sample)} Zeilen, {len(frames)} Coins, {n_ok} Modelle kompatibel)."
        )
    finally:
        conn.close()


# 🛡️ COOLDOWN CHECK


def _fetch_mis_frame(conn, symbol):
    """Letzte 100 GESCHLOSSENE 1h-Kerzen + Indikator-Join — Spaltenkatalog kommt
    aus core.mis_features (eine Quelle für Bot, Trainer und Simulator).

    R1 (Block 4): liest geschlossene Kerzen via core.candles (ASC, forming bar
    dropped — kein manuelles reverse mehr). Die API liefert die ROHEN
    Indikatornamen; MIS_RENAME_MAP reproduziert danach die drei tsi/macd-Aliase,
    damit der Frame byte-gleich zur geteilten MIS_INDICATOR_COLUMNS-Liste (und
    damit zu tools/walkforward_sim.py) bleibt und add_advanced_features seine
    REQUIRED_INPUT_COLS findet (harte Regel 7, EINE Quelle in core.mis_features)."""
    df = read_candles_with_indicators(
        conn,
        symbol,
        "1h",
        limit=100,
        # TimescaleDB chunk-exclusion hint (T-2026-CU-9050-180): bound the read to
        # a window that comfortably holds the newest 100 closed 1h candles so the
        # returned rows are unchanged while ~120 of 126 chunks are pruned.
        start=history_start("1h", 100),
        include_forming=False,
        candle_columns=("open_time", "close", "volume"),
        indicator_columns=MIS_INDICATOR_COLUMNS,
    )
    if len(df) < 10:
        return None
    return df.rename(columns=MIS_RENAME_MAP)


def check_mis_models():
    # FIX P2.32: kein autocommit mehr — Outbox-Post, ai_signals-Insert und
    # master-Log gehören pro Signal in EINE Transaktion (Commit übernimmt
    # update_cooldown bzw. der explizite Commit im Shadow-Pfad). Vorher
    # konnte ein Crash mittendrin einen gePOSTeten Trade ohne Tracking
    # hinterlassen.
    conn = get_db_connection()

    try:
        with open('coins.json') as f:
            coins = json.load(f)
    except Exception as e:
        logger.error(f"Could not load coins.json: {e}")
        conn.close()  # Pool-Slot freigeben (Review Batch 4)
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    logger.info(f"🔍 Starting MIS1 Model Check für {len(coins)} Coins...")

    # FIX: Einmalig VOR der Coin-Schleife prüfen ob überhaupt ein Modell geladen ist.
    # Vorher stand der Check in der Schleife mit `return` → der ganze Scan brach
    # ab sobald ein einziger Coin kein Modell fand.
    if not any(cfg["loaded"] for cfg in PUMP_MODELS.values()):
        logger.error("No MIS1 model loaded. Scan aborted.")
        conn.close()  # Pool-Slot freigeben (Review Batch 4)
        return

    # R1: live entry price via batch ticker (1 call/cycle), per-coin HTTP→DB fallback.
    price_map = get_live_prices_batch()

    conn_dead = False
    for symbol in coins:
        try:
            df = _fetch_mis_frame(conn, symbol)
            if df is None:
                continue

            df_features = add_advanced_features(df)
            # FIX P1.17 + R1: model features from the last CLOSED candle. The frame now
            # holds only closed candles (include_forming=False), so that is iloc[-1]
            # (was iloc[-2] when the forming bar was still the last row) — the forming
            # bar's stale/partial volume+indicator values are gone by construction.
            # Kept as a 1-row DataFrame for sklearn name validation, not a Series.
            # The entry price stays LIVE — batch ticker, not the candle close.
            df_current = df_features.iloc[-1:]
            live_price = price_map.get(symbol) or get_live_price(symbol, conn)
            if not live_price:
                continue
            current_price = float(live_price)

            # Alle Modelle für diesen Coin testen — Feature-Auswahl je Modell
            # NAMENSBASIERT über das DataFrame (P1.18: `.values` hatte die
            # sklearn-Namensvalidierung deaktiviert; Kompatibilität wurde beim
            # Startup-Selbsttest bereits hart geprüft).
            candidates = []
            for horizon, cfg in PUMP_MODELS.items():
                if not cfg["loaded"]:
                    continue

                try:
                    X_current = df_current[cfg["features"]]
                    prob = float(cfg["model"].predict_proba(X_current)[0, 1])
                    if prob >= 0.25:
                        direction = "LONG" if "pump" in horizon.lower() else "SHORT"
                        clean_horizon = horizon.upper().replace("_PUMP", "").replace("_DUMP", "")
                        # Kalibrierte Confidence (Isotonic aus dem Retrain-Artefakt)
                        # für Anzeige/Logging; das GATING läuft weiter über die rohe
                        # Probability, denn der Threshold wurde auf rohen Val-Probs
                        # gewählt (tools/retrain_from_replay.py).
                        if cfg["calibrator"] is not None:
                            conf = float(np.clip(cfg["calibrator"].predict([prob])[0], 0.0, 1.0))
                        else:
                            conf = prob
                        candidates.append((prob, clean_horizon, direction, cfg["threshold"], conf, cfg["generation"]))
                except Exception as e:
                    logger.error(f"{symbol} {horizon}: predict fehlgeschlagen: {e}")

            if not candidates:
                continue

            # FIX P2.33: nach Abstand zur MODELL-EIGENEN Schwelle ranken, nicht
            # nach roher Probability — die 8 Modelle sind unterschiedlich
            # kalibriert, ein 0.55er unter-Schwelle-Kandidat verdrängte sonst
            # ein 0.52er über-Schwelle-Signal.
            candidates.sort(reverse=True, key=lambda x: x[0] - x[3])
            best_prob, best_horizon, best_direction, best_threshold, best_conf, best_generation = candidates[0]
            # Generation kommt aus der Artefakt-Meta des GEWINNER-Modells, nie aus
            # einer Quellcode-Konstante (T-2026-CU-9050-030): mis2_model_*.pkl ist ein
            # Slot-Name, nur meta.model_id kennt die Generation. z. B. MIS2-72H → MIS3-72H.
            module_tag = f"{best_generation}-{best_horizon}"

            # 1. Aktiver Trade Check — prüft ob ein nicht-geschlossener Trade für
            #    genau dieses Modul/Coin/Richtung läuft. Der Cooldown-Check weiter
            #    unten verhindert zusätzlich zu schnelle Folgesignale im Horizon-Fenster.
            #
            #    Der Check läuft über den Tag — und der Tag wechselt beim Retrain-Rollout
            #    (MIS2-72H → MIS3-72H). Ohne den Alt-Tag im IN würde eine offene
            #    MIS2-Position denselben Coin/Direction nicht mehr blocken und der
            #    MIS3-Lauf öffnete eine ZWEITE Live-Position daneben. legacy_tag ist
            #    exakt das Tag, das dieser Bot vor T-2026-CU-9050-030 gepostet hätte;
            #    solange Konstante und Artefakt-Generation übereinstimmen, ist das IN
            #    ein No-op. Muster: 25_smc_ml_sniper.py (T-2026-CU-9050-026).
            legacy_tag = f"{MODEL_GENERATION}-{best_horizon}"
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM ai_signals
                    WHERE symbol = %s AND direction = %s AND model IN (%s, %s)
                """,
                    (symbol, best_direction, module_tag, legacy_tag),
                )
                trade_exists = cur.fetchone()

            if trade_exists:
                continue  # Skippingn, Trade läuft live im AI Monitor

            # --- LOGIK ANWENDEN ---
            if best_prob < 0.25:
                pass
            elif 0.25 <= best_prob < best_threshold:
                # Shadow Mode
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT 1 FROM ml_predictions_master
                        WHERE coin = %s AND direction = %s AND model_name = %s AND time > NOW() - INTERVAL '4 hours'
                    """,
                        (symbol, best_direction, module_tag),
                    )
                    if not cur.fetchone():
                        cur.execute(
                            """
                            INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                            VALUES (0, %s, %s, %s, %s, %s, %s, False)
                        """,
                            (module_tag, now, symbol, best_direction, float(current_price), float(best_conf)),
                        )
                conn.commit()  # P2.32: Shadow-Insert explizit committen (autocommit ist aus)
            elif best_prob >= best_threshold:
                # 💥 Hard Cooldown Check (8h, 24h, 72h, 168h Sperre je after Modell)
                # check_cooldown returned True wenn Cooldown NOCH AKTIV ist → dann skippen.
                cd_hours = int(best_horizon.replace("H", ""))
                if check_cooldown(conn, module_tag, symbol, best_direction, cd_hours):
                    continue

                logger.info(
                    f"🚀 {best_generation} Trade gefunden: {symbol} {best_direction} | {module_tag} (raw {best_prob:.3f} / kalibriert {best_conf:.1%})"
                )

                is_long = best_direction == "LONG"
                if is_long:
                    # 💥 SMARTE TARGETS (Pump-Seite: wie gehabt)
                    trade_setup = calculate_smart_targets(conn, symbol, best_direction, current_price)
                    entry1 = trade_setup['entry1']
                    entry2 = trade_setup['entry2']
                    sl = trade_setup['sl']
                    targets = trade_setup['targets']
                else:
                    # Dump-Seite: studien-validierte Bracket-Geometrie je Horizont
                    # (s. DUMP_RULES oben) statt Smart-Targets — die verloren
                    # nachweislich (Studie V1: −0,3 bis −0,8 %/Trade).
                    rules = DUMP_RULES[best_horizon]
                    entry1 = current_price * (1 + rules["bounce_pct"] / 100.0)  # Limit-Sell in den Bounce
                    entry2 = entry1  # Einzel-Entry — exakt die simulierte Geometrie
                    sl = entry1 * (1 + rules["sl_pct"] / 100.0)
                    targets = [current_price * (1 - rules["tp_pct"] / 100.0)]  # TP ab Signalkurs
                lev = get_max_leverage(symbol, 20)
                emoji = "🚀 PUMP SIGNAL (MIS)" if is_long else "💥 DUMP SIGNAL (MIS)"
                strength = "STRONG" if best_prob >= best_threshold + 0.1 else "MODERATE"

                # RRR (Risk Reward Ratio) Berechnung
                avg_entry = (entry1 + entry2) / 2
                risk_pct = abs((sl - avg_entry) / avg_entry)
                reward_pct = abs((targets[0] - avg_entry) / avg_entry) if targets else 0.01
                rrr = reward_pct / risk_pct if risk_pct > 0 else 0.01

                # P2.31: publish AND track exactly the same targets. The Cornix block
                # shows the first n_show TPs; the AI monitor (8_ai_trade_monitor) scores
                # whatever is stored in ai_signals.targets. Storing the full target list
                # made the monitor score phantom TPs the subscriber never saw.
                n_show = 5

                # Cornix Text
                cornix_msg = f"""📈 Signal for {symbol} 📈
🚨 Direction: {best_direction}
🚨 Leverage: {lev}
🚨 Margin: Cross
🏦 CMP Entry: $ {entry1:.8f}
🏦 Entry 2: $ {entry2:.8f}"""

                for i, t in enumerate(targets[:n_show], 1):
                    cornix_msg += f"\n💰 TP{i}: $ {t:.8f}"

                cornix_msg += (
                    f"\n💸 Stop Loss: $ {sl:.8f}\n🧠 AI Confidence: {best_conf * 100:.1f}% ({module_tag} Filter)"
                )

                # HTML Visualisierung
                html_caption = f"""<pre>
<b>{emoji}</b>
<b>├─ Coin:</b> <b>{symbol}</b>
<b>├─ Action:</b> <b>{best_direction}</b>
<b>├─ Horizon:</b> <b>{best_horizon}</b>
<b>├─ RRR (T1):</b> <b>1:{rrr:.2f}</b>
<b>└─ ML Confidence:</b> <b>{strength} – {best_conf:.1%}</b>

<b>├─ Entry 1:</b> <b>${entry1:,.8f}</b>
<b>└─ Entry 2:</b> <b>${entry2:,.8f}</b>

<b>├─ Take Profits:</b>
"""
                for i, t in enumerate(targets[:n_show], 1):
                    pct = abs((t - entry1) / entry1 * 100) * int(lev.replace('x', ''))
                    t_col = "#00ff88" if i <= 2 else "#88ff88"
                    html_caption += f"<b style=\"color:{t_col};\">   T{i}:</b> <b>${t:,.8f}</b> → <b style=\"color:lime;\">+{pct:.1f}%</b>\n"

                sl_loss = risk_pct * 100 * int(lev.replace('x', ''))
                # FIX Doppel-Post (2026-07-06, Flotten-Sweep): Caption ohne
                # eingebetteten Cornix-Block — Cornix parste beide Nachrichten.
                html_caption += f"""<b>└─ Stop Loss:</b> <b>${sl:,.8f}</b> → <b>-{sl_loss:.1f}%</b></pre>"""

                # Target Channel Routing
                target_channel = MIS_CHANNELS.get(best_horizon, _kcfg.CH_MIS_8H)  # Fallback

                chart_buf = generate_minichart_image(symbol, minutes=240)

                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                        (target_channel, cornix_msg),
                    )

                    if chart_buf:
                        cur.execute(
                            "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                            (target_channel, html_caption, chart_buf),
                        )
                    else:
                        cur.execute(
                            "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                            (target_channel, html_caption),
                        )

                    # SHORT = Limit-Entry (+5 % über Markt): entry_filled=FALSE
                    # bis der Monitor den Fill sieht; expiry_hours = Horizont
                    # (Verfall + Timeout-Exit — Teil der validierten Geometrie).
                    cur.execute(
                        """
                        INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets, entry_filled, expiry_hours)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                        (
                            symbol,
                            float(entry1),
                            module_tag,
                            best_direction,
                            float(best_conf),
                            float(entry1),
                            float(entry2),
                            float(sl),
                            json.dumps(targets[:n_show]),
                            is_long,  # LONG = CMP-Entry (sofort gefüllt), SHORT = Limit
                            None if is_long else int(best_horizon.replace("H", "")),
                        ),
                    )

                    cur.execute(
                        """
                        INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                        VALUES (0, %s, %s, %s, %s, %s, %s, True)
                    """,
                        (module_tag, now, symbol, best_direction, float(current_price), float(best_conf)),
                    )

                # Cooldown setzen damit der gleiche Coin/Direction nicht sofort wieder feuert.
                # P2.32: update_cooldown committed (default commit=True) und schließt damit
                # die EINE Transaktion aus Outbox-Posts + ai_signals + master-Log atomar ab.
                update_cooldown(conn, module_tag, symbol, best_direction)

        except Exception as e:
            logger.error(f"Error for {symbol} in MIS1: {e}")
        finally:
            # P2.32 + Review Batch 4: Transaktion pro Coin IMMER schließen.
            # (a) Eine aborted Transaktion würde sonst alle folgenden Coins
            #     vergiften ("current transaction is aborted", vgl. P1.23).
            # (b) Eine offene Read-Transaktion über den ganzen 538-Coin-Scan
            #     friert NOW() (= transaction_timestamp) auf den Scan-Start ein
            #     → telegram_outbox.created_at rückdatiert (Orchestrator-
            #     Staleness-Filter verwirft die Signale still) und Cooldowns
            #     werden um die Scan-Dauer verkürzt.
            # Nach einem Commit-Pfad ist der rollback ein No-op.
            try:
                conn.rollback()
            except Exception:
                logger.error("MIS1: rollback fehlgeschlagen (tote Connection) — Scan-Abbruch.")
                conn_dead = True
        if conn_dead:
            break

    if conn:
        conn.close()
    logger.info("🏁 MIS1 Model Check stopped.")


def main():
    logger.info("=== 🧠 AI MIS BOT (Multi-Horizon) GESTARTET ===")

    # Tabellen Setup für Cooldown
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_cooldowns (
                module VARCHAR(50),
                coin VARCHAR(20),
                direction VARCHAR(10),
                last_posted_at TIMESTAMP WITH TIME ZONE,
                PRIMARY KEY (module, coin, direction)
            );
        """)
    conn.commit()
    conn.close()

    load_pump_models()
    # P0.12-Muster: Feature-Pipeline + Modell-Kompatibilität hart prüfen,
    # BEVOR der Scan-Loop startet (inkompatible Legacy-Modelle werden entladen).
    startup_feature_selfcheck()

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)

        if now.minute == 11:
            check_mis_models()
            time.sleep(60)
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")
