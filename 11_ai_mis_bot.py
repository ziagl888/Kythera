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
from core.signal_post import LEG_LIVE, LEG_SHADOW, route_legacy_leg
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


# --- LOAD MIS1 MODELS (Revive, T-2026-KYT-9050-034) ---
# Operator-Entscheid (Michi): die MIS1-Generation (Audit T-032: MIS1-24H/72H/168H
# LONG + MIS1-8H SHORT realisierten BESSER als die neue MIS2-Move-Generation)
# EXAKT wiederherstellen — kein Retrain. Die Artefakte liegen unverändert im Repo-
# Root: pump_model_{key}_final.pkl (nackte 67-Feature-XGBClassifier) + threshold_
# {key}_final.pkl. Sie werden mit dem GETEILTEN Builder gefüttert, aber im
# include_legacy=True-Modus — der reproduziert die 8 LEGACY_ONLY_COLS, die diese
# Modelle zusätzlich zu den 63 sauberen Features erwarten (verifiziert: 0 missing
# über alle 8 Modelle). MIS1 läuft PARALLEL zu MIS2 unter eigenen Tags MIS1-* →
# kollisionsfrei (eigener Active-Trade-Check + Cooldown je Tag). Welche MIS1-Beine
# live posten, steuert AUSSCHLIESSLICH das shadow_gate-Register (core/shadow_gate.py):
# die guten Beine sind Default-LIVE, die schwachen dort auf SHADOW geparkt.
MIS1_GENERATION = "MIS1"
MIS1_MODELS = {
    key: {
        "model_path": f"pump_model_{key}_final.pkl",
        "threshold_path": f"threshold_{key}_final.pkl",
        "model": None,
        "threshold": 0.5,
        "features": None,
        "generation": MIS1_GENERATION,
        "loaded": False,
    }
    for key in ("8h_pump", "24h_pump", "72h_pump", "168h_pump", "8h_dump", "24h_dump", "72h_dump", "168h_dump")
}


def load_mis1_models():
    """Lädt die MIS1-Legacy-Artefakte (nackter XGBClassifier + separates Threshold-
    pkl) — exakte Restauration des Pfads, der die vom Audit gemessene Live-
    Erfolgsrate produziert hat (99e9de3^). Der Feature-Vertrag kommt aus
    feature_names_in_ (67 Features); der Selfcheck prüft die Kompatibilität gegen
    den include_legacy-Builder hart, bevor der Scan startet."""
    loaded_count = 0
    for key, cfg in MIS1_MODELS.items():
        try:
            if not os.path.exists(cfg["model_path"]):
                logger.warning(f"MIS1-Modell fehlt: {cfg['model_path']}")
                continue
            cfg["model"] = joblib.load(cfg["model_path"])
            if os.path.exists(cfg["threshold_path"]):
                cfg["threshold"] = float(joblib.load(cfg["threshold_path"]))
            else:
                # Kein separates Threshold-File → konservativer Default (wie 99e9de3^).
                cfg["threshold"] = 0.60
                logger.warning(f"MIS1-Threshold fehlt ({cfg['threshold_path']}) → Default 0.60")
            cfg["features"] = list(getattr(cfg["model"], "feature_names_in_", []))
            cfg["loaded"] = True
            loaded_count += 1
        except Exception as e:
            logger.error(f"Error loading MIS1 {key}: {e}")
    thresh_summary = ", ".join(f"{h}={cfg['threshold']:.2f}" for h, cfg in MIS1_MODELS.items() if cfg["loaded"])
    logger.info(f"✅ {loaded_count}/{len(MIS1_MODELS)} MIS1-Modelle (Revive) loaded. Thresholds: {thresh_summary}")


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
            # include_legacy=True: SUPERSET (71 Spalten) — enthält die 63 sauberen
            # MIS2-Features UND die 8 LEGACY_ONLY_COLS, die die MIS1-Revive-Modelle
            # zusätzlich brauchen (T-2026-KYT-9050-034). Für MIS2 additiv-neutral
            # (die 8 Extra-Spalten werden nie selektiert); die Feature-Alive-
            # Assertion prüft weiterhin die 63 sauberen FEATURE_COLS.
            frames.append(add_advanced_features(df, include_legacy=True))
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
            logger.critical("❌ Kein kompatibles MIS2-Modell übrig — Abbruch.")
            exit(1)

        # MIS1-Revive-Modelle (T-2026-KYT-9050-034) gegen den include_legacy-Superset
        # prüfen. Additiv: schlägt ein MIS1-Modell fehl, wird NUR es entladen — der
        # Bot läuft mit MIS2 (und den übrigen MIS1-Beinen) weiter, KEIN harter Abbruch.
        for key, cfg in MIS1_MODELS.items():
            if not cfg["loaded"]:
                continue
            missing = [c for c in (cfg["features"] or []) if c not in sample.columns]
            if missing:
                logger.error(
                    f"❌ MIS1 {key}: Modell verlangt Features, die auch der include_legacy-"
                    f"Builder nicht liefert: {missing[:6]}… — MIS1-Modell entladen (MIS2 unberührt)."
                )
                cfg["loaded"] = False
                cfg["model"] = None

        n_ok = sum(1 for cfg in PUMP_MODELS.values() if cfg["loaded"])
        n_ok_mis1 = sum(1 for cfg in MIS1_MODELS.values() if cfg["loaded"])
        logger.info(
            f"✅ Feature-Selbsttest bestanden ({len(sample)} Zeilen, {len(frames)} Coins, "
            f"{n_ok} MIS2 + {n_ok_mis1} MIS1 Modelle kompatibel)."
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


def _score_models_batched(collected_frames, models):
    """Batched MIS2 inference (T-2026-CU-9050-186).

    ``collected_frames``: list of the per-coin 1-row feature DataFrames
    (``df_features.iloc[-1:]``) in scan order — already built by
    ``add_advanced_features`` PER COIN, never concatenated before feature
    building (rolling windows must not cross coin boundaries).
    ``models``: mapping horizon-key -> cfg for the LOADED models.

    Returns ``{key: np.ndarray}`` where the array holds ``predict_proba[:, 1]``
    for every collected coin IN THE SAME ORDER. ``NaN`` marks a coin/model whose
    inference failed — the caller skips exactly that (coin, model), reproducing
    the old per-coin ``try/except`` that dropped a single failing prediction.

    Why this is behaviour-neutral: XGBoost scores each row independently, so one
    ``predict_proba`` over the stacked matrix yields the identical per-row
    probability as scoring each row alone — it only amortises the ~66ms per-call
    overhead (sklearn name-validation + DMatrix build) across all coins (527x8 =
    4216 calls/scan -> 8). The per-model column selection ``cfg["features"]``
    fixes column order identically to the single-row path.

    Fast path = one batched call per model. On ANY batch exception (e.g. a single
    corrupt row poisoning the concat/predict) it falls back to per-row scoring
    for THAT model only, so the failure semantics stay exactly as before: a bad
    coin loses just its own prediction, every other coin still scores.
    """
    n = len(collected_frames)
    probs_by_model = {}
    for key, cfg in models.items():
        feats = cfg["features"]
        try:
            X_all = pd.concat([f[feats] for f in collected_frames], axis=0)
            probs_by_model[key] = np.asarray(cfg["model"].predict_proba(X_all)[:, 1], dtype=float)
        except Exception as e:
            logger.error(f"MIS batch predict {key} fehlgeschlagen — per-Coin-Fallback: {e}")
            arr = np.full(n, np.nan, dtype=float)
            for i, f in enumerate(collected_frames):
                try:
                    arr[i] = float(cfg["model"].predict_proba(f[feats])[0, 1])
                except Exception as e2:
                    logger.error(f"{key} row {i}: predict fehlgeschlagen: {e2}")
            probs_by_model[key] = arr
    return probs_by_model


def _mis_geometry(conn, generation, symbol, direction, horizon, current_price):
    """Trade-Geometrie je Generation → (entry1, entry2, sl, targets, entry_filled, expiry_hours).

    * MIS1-Revive (T-2026-KYT-9050-034): EXAKT der alte Pfad (99e9de3^) —
      ``calculate_smart_targets`` für BEIDE Richtungen, immediate CMP-Entry
      (entry_filled=True, kein expiry). Das ist die Geometrie, die die vom Audit
      T-032 gemessene MIS1-Erfolgsrate produziert hat.
    * MIS2/MIS3: LONG = Smart-Targets, SHORT = studien-validierte DUMP_RULES-
      Bracket-Geometrie (Limit-Entry, entry_filled=False, expiry=Horizont) —
      unverändert.
    """
    is_long = direction == "LONG"
    if generation == MIS1_GENERATION or is_long:
        s = calculate_smart_targets(conn, symbol, direction, current_price)
        return s["entry1"], s["entry2"], s["sl"], s["targets"], True, None
    rules = DUMP_RULES[horizon]
    entry1 = current_price * (1 + rules["bounce_pct"] / 100.0)  # Limit-Sell in den Bounce
    entry2 = entry1  # Einzel-Entry — exakt die simulierte Geometrie
    sl = entry1 * (1 + rules["sl_pct"] / 100.0)
    targets = [current_price * (1 - rules["tp_pct"] / 100.0)]  # TP ab Signalkurs
    return entry1, entry2, sl, targets, False, int(horizon.replace("H", ""))


def _post_mis_live_leg(
    conn,
    module_tag,
    best_horizon,
    best_direction,
    best_prob,
    best_threshold,
    best_conf,
    symbol,
    current_price,
    now,
    entry1,
    entry2,
    sl,
    targets,
    *,
    entry_filled,
    expiry_hours,
):
    """Geteilter LIVE-Post-Body für MIS-Signale (MIS2 + MIS1-Revive, T-034).

    Baut Cornix + HTML-Visualisierung (OHNE eingebetteten Cornix-Block — Regel 4,
    Doppel-Trade-Fix 2026-07-06), schreibt telegram_outbox + ai_signals +
    ml_predictions_master und setzt den Cooldown (update_cooldown committet die EINE
    Transaktion). ``entry_filled``/``expiry_hours`` tragen die Entry-Semantik der
    jeweiligen Geometrie (MIS2-SHORT = Limit; MIS1 + MIS2-LONG = CMP sofort)."""
    is_long = best_direction == "LONG"
    lev = get_max_leverage(symbol, 20)
    emoji = "🚀 PUMP SIGNAL (MIS)" if is_long else "💥 DUMP SIGNAL (MIS)"
    strength = "STRONG" if best_prob >= best_threshold + 0.1 else "MODERATE"

    # RRR (Risk Reward Ratio) Berechnung
    avg_entry = (entry1 + entry2) / 2
    risk_pct = abs((sl - avg_entry) / avg_entry)
    reward_pct = abs((targets[0] - avg_entry) / avg_entry) if targets else 0.01
    rrr = reward_pct / risk_pct if risk_pct > 0 else 0.01

    # P2.31: publish AND track exactly the same targets. The Cornix block shows the
    # first n_show TPs; the AI monitor (8_ai_trade_monitor) scores whatever is stored
    # in ai_signals.targets. Storing the full target list made the monitor score
    # phantom TPs the subscriber never saw.
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

    cornix_msg += f"\n💸 Stop Loss: $ {sl:.8f}\n🧠 AI Confidence: {best_conf * 100:.1f}% ({module_tag} Filter)"

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
        html_caption += (
            f"<b style=\"color:{t_col};\">   T{i}:</b> <b>${t:,.8f}</b> → <b style=\"color:lime;\">+{pct:.1f}%</b>\n"
        )

    sl_loss = risk_pct * 100 * int(lev.replace('x', ''))
    # FIX Doppel-Post (2026-07-06, Flotten-Sweep): Caption ohne eingebetteten
    # Cornix-Block — Cornix parste sonst beide Nachrichten (Regel 4).
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

        # entry_filled/expiry_hours kommen aus der Geometrie: MIS2-SHORT = Limit-Entry
        # (+5 % über Markt, entry_filled=FALSE bis der Monitor den Fill sieht,
        # expiry_hours=Horizont); MIS1 + LONG = CMP-Entry (sofort gefüllt, kein Verfall).
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
                entry_filled,
                expiry_hours,
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
    # P2.32: update_cooldown committed (default commit=True) und schließt damit die
    # EINE Transaktion aus Outbox-Posts + ai_signals + master-Log atomar ab.
    update_cooldown(conn, module_tag, symbol, best_direction)


def _process_mis_candidates(conn, idx, symbol, current_price, now, models, probs, legacy_generation):
    """Per-Coin-Kandidatenwahl + Emission für EINE Generation (MIS2 oder MIS1-Revive).

    Baut aus den gebatchten Probabilities (``probs``) die Kandidaten je geladenem
    Modell, rankt nach Abstand zur modell-eigenen Schwelle (P2.33), prüft den
    Active-Trade-Check + Cooldown, holt die Geometrie generations-abhängig
    (:func:`_mis_geometry`) und routet über das shadow_gate-Register
    (:func:`route_legacy_leg`). ``legacy_generation`` ist die Konstanten-Generation
    für den Active-Trade-Alt-Tag (MIS2→MIS3-Rename-Schutz; für MIS1 ein No-op)."""
    candidates = []
    for horizon, cfg in models.items():
        if not cfg["loaded"]:
            continue
        arr = probs.get(horizon)
        if arr is None:
            continue
        prob = arr[idx]
        # NaN = Inferenz dieses (Coin, Modell) fehlgeschlagen (Batch-Fallback);
        # exakt wie das alte per-Coin-try/except diese eine Prediction fallen ließ.
        if not np.isfinite(prob):
            continue
        prob = float(prob)
        if prob >= 0.25:
            direction = "LONG" if "pump" in horizon.lower() else "SHORT"
            clean_horizon = horizon.upper().replace("_PUMP", "").replace("_DUMP", "")
            # Kalibrierte Confidence (Isotonic aus dem Retrain-Artefakt) für Anzeige/
            # Logging; das GATING läuft weiter über die rohe Probability, denn der
            # Threshold wurde auf rohen Val-Probs gewählt. MIS1-Modelle tragen keinen
            # Kalibrator (nackter XGBClassifier) → conf = rohe Probability.
            if cfg.get("calibrator") is not None:
                conf = float(np.clip(cfg["calibrator"].predict([prob])[0], 0.0, 1.0))
            else:
                conf = prob
            candidates.append((prob, clean_horizon, direction, cfg["threshold"], conf, cfg["generation"]))

    if not candidates:
        return

    # FIX P2.33: nach Abstand zur MODELL-EIGENEN Schwelle ranken, nicht nach roher
    # Probability — die 8 Modelle sind unterschiedlich kalibriert.
    candidates.sort(reverse=True, key=lambda x: x[0] - x[3])
    best_prob, best_horizon, best_direction, best_threshold, best_conf, best_generation = candidates[0]
    # Generation kommt aus der Artefakt-Meta des GEWINNER-Modells (T-2026-CU-9050-030);
    # für MIS1-Revive ist sie die Konstante "MIS1".
    module_tag = f"{best_generation}-{best_horizon}"

    # 1. Aktiver Trade Check — läuft über den Tag. legacy_tag fängt den MIS2→MIS3-
    #    Rename (offene Alt-Position blockt weiter); für MIS1 ist legacy_tag ==
    #    module_tag → das IN ist ein No-op.
    legacy_tag = f"{legacy_generation}-{best_horizon}"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM ai_signals WHERE symbol = %s AND direction = %s AND model IN (%s, %s)",
            (symbol, best_direction, module_tag, legacy_tag),
        )
        if cur.fetchone():
            return  # Trade läuft live im AI Monitor

    # --- LOGIK ANWENDEN ---
    if best_prob < 0.25:
        return
    if best_prob < best_threshold:
        # Shadow Mode (Sub-Threshold-Prediction protokollieren, kein Trade).
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
        return

    # best_prob >= best_threshold → Trade-Kandidat.
    # 💥 Hard Cooldown Check (Horizont-Sperre je Modell). True = Cooldown aktiv → skip.
    cd_hours = int(best_horizon.replace("H", ""))
    if check_cooldown(conn, module_tag, symbol, best_direction, cd_hours):
        return

    logger.info(
        f"🚀 {best_generation} Trade gefunden: {symbol} {best_direction} | {module_tag} "
        f"(raw {best_prob:.3f} / kalibriert {best_conf:.1%})"
    )

    entry1, entry2, sl, targets, entry_filled, expiry_hours = _mis_geometry(
        conn, best_generation, symbol, best_direction, best_horizon, current_price
    )

    # Fleet-Lifecycle-Gate (T-2026-KYT-9050-033/034). Default LIVE ⇒ keine
    # Verhaltensänderung. Das shadow_gate-Register steuert, welche (tag, direction)-
    # Beine live posten (Cornix) bzw. als überwachter Shadow-Trade laufen — für MIS2
    # UND die MIS1-Revive-Beine. Rein additiv am Post-Zweig (Regel 4).
    _route = route_legacy_leg(
        conn, module_tag, best_direction, symbol, float(best_conf), entry1, entry2, sl, targets, n_show=5
    )
    if _route != LEG_LIVE:
        if _route == LEG_SHADOW:
            conn.commit()
        return

    _post_mis_live_leg(
        conn,
        module_tag,
        best_horizon,
        best_direction,
        best_prob,
        best_threshold,
        best_conf,
        symbol,
        current_price,
        now,
        entry1,
        entry2,
        sl,
        targets,
        entry_filled=entry_filled,
        expiry_hours=expiry_hours,
    )


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

    # === PHASE A: per-coin feature build (UNCHANGED math), collect for batched scoring ===
    # add_advanced_features stays PER COIN — its rolling windows must never span
    # coin boundaries. We only collect the finished 1-row feature frames so the
    # 8 models can be scored in one batched predict_proba each below.
    collected = []  # list of (symbol, df_current, current_price) in scan order
    for symbol in coins:
        try:
            df = _fetch_mis_frame(conn, symbol)
            if df is None:
                continue

            # include_legacy=True (T-2026-KYT-9050-034): SUPERSET-Frame (71 Spalten),
            # der BEIDE Generationen bedient — MIS2 selektiert seine 63 sauberen
            # Features namensbasiert (additiv-neutral, die 8 Extra-Spalten werden nie
            # gewählt), die MIS1-Revive-Modelle ihre 67 (63 sauber + 8 LEGACY_ONLY_COLS).
            # EIN Feature-Build pro Coin für beide Generationen — kein doppelter DB-Read.
            df_features = add_advanced_features(df, include_legacy=True)
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
            collected.append((symbol, df_current, float(live_price)))
        except Exception as e:
            logger.error(f"Error building MIS features for {symbol}: {e}")
        finally:
            # Keep the read transaction clean between coins (P2.32): an aborted read
            # would poison every following coin, and a single open read transaction
            # across the whole scan would freeze NOW() on the scan start.
            try:
                conn.rollback()
            except Exception:
                logger.error("MIS1: rollback fehlgeschlagen (tote Connection) — Scan-Abbruch.")
                conn_dead = True
        if conn_dead:
            break

    if conn_dead or not collected:
        if conn:
            conn.close()
        logger.info("🏁 MIS1 Model Check stopped.")
        return

    # === PHASE B: ONE predict_proba per model over ALL collected coins ===
    # Was 527 coins x 8 models = 4216 single-row calls per scan; now 8 batched
    # calls. Row-independent XGBoost => byte-identical per-coin probabilities
    # (T-2026-CU-9050-186). Order of `probs[key]` matches `collected`.
    frames_for_score = [dc for (_, dc, _) in collected]
    loaded_models = {h: cfg for h, cfg in PUMP_MODELS.items() if cfg["loaded"]}
    probs_by_model = _score_models_batched(frames_for_score, loaded_models)

    # MIS1-Revive (T-2026-KYT-9050-034): dieselbe gebatchte Inferenz über die MIS1-
    # Modelle. Die Feature-Auswahl in _score_models_batched ist namensbasiert
    # (cfg["features"] = 67 MIS1-Features), der Superset-Frame liefert sie alle.
    loaded_mis1 = {h: cfg for h, cfg in MIS1_MODELS.items() if cfg["loaded"]}
    probs_by_mis1 = _score_models_batched(frames_for_score, loaded_mis1) if loaded_mis1 else {}

    # === PHASE C: per-coin candidate build + posting (identical logic, per-coin txn) ===
    for idx, (symbol, _df_current, current_price) in enumerate(collected):
        try:
            # MIS2/MIS3 (bestehende Generation): unveränderte Kandidatenwahl + Emit,
            # jetzt über den geteilten Prozessor. legacy_generation=MODEL_GENERATION
            # fängt den MIS2→MIS3-Rename im Active-Trade-Check.
            _process_mis_candidates(
                conn, idx, symbol, current_price, now, PUMP_MODELS, probs_by_model, MODEL_GENERATION
            )
            # MIS1-Revive (T-2026-KYT-9050-034): parallele Generation unter eigenen
            # Tags MIS1-*, EXAKT dieselbe Verarbeitung — nur MIS1-Modelle + MIS1-
            # Geometrie (calculate_smart_targets beide Richtungen, s. _mis_geometry).
            # Kollisionsfrei: eigener Active-Trade-Check + Cooldown je Tag; das
            # shadow_gate-Register steuert, welche MIS1-Beine live posten.
            if loaded_mis1:
                _process_mis_candidates(
                    conn, idx, symbol, current_price, now, MIS1_MODELS, probs_by_mis1, MIS1_GENERATION
                )

        except Exception as e:
            logger.error(f"Error for {symbol} in MIS check: {e}")
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
    load_mis1_models()  # MIS1-Revive (T-2026-KYT-9050-034): Legacy-Artefakte parallel laden
    # P0.12-Muster: Feature-Pipeline + Modell-Kompatibilität hart prüfen,
    # BEVOR der Scan-Loop startet (inkompatible Legacy-Modelle werden entladen).
    # Prüft beide Generationen gegen den include_legacy-Superset.
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
