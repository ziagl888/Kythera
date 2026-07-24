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
from core import shadow_gate
from core.candles import read_candles, read_indicators
from core.charting import generate_minichart_image
from core.database import get_db_connection
from core.funding_features import funding_features_asof, load_funding
from core.market_utils import check_cooldown, get_max_leverage, update_cooldown
from core.rub_features import build_rub_features, rub_event_type, rub_trend
from core.signal_post import LEG_LIVE, LEG_SHADOW, post_shadow_ai_signal, route_legacy_leg
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels

logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_RUB_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG & CHANNELS ---
# Hier kannst du den speziellen Rubberband-Kanal setzen
RUBBERBAND_CHANNEL_ID = _kcfg.CH_RUBBERBAND

# --- LOAD ML MODELS ---
# RUB1-Revive (T-2026-KYT-9050-037, Operator-Entscheid Michi aus bot_results.xlsx):
# Bot 13 fährt BEIDE Richtungen wieder auf den ORIGINALEN Legacy-Reversion-Modellen
# und postet sie live unter dem Original-Tag RUB1 (LONG 2.48 % / SHORT 0.78 %,
# historisch beide positiv). Das revertiert (a) den T-030-LONG-Tag-Rename (→ RUB2)
# und (b) den PR-#9-Removal des Legacy-SHORT-Zweigs (rub2_model_SHORT-Retrain).
# Der RUB2-Retrain wird gebencht: RUB2 bleibt im shadow_gate-Register SHADOW (beide
# Richtungen, block E); der RUB3/RUB4-LONG-Challenger läuft unverändert als Shadow.
MODEL_LONG_PATH = 'long_reversion_model.joblib'
MODEL_SHORT_PATH = 'short_reversion_model.joblib'
# Original-RUB1-Thresholds auf der ROHEN predict_proba der Legacy-Modelle (9 rub-
# Features, KEIN Funding — Parität zur Vor-PR-#9-Logik, git 07c8874^). Bewusst nicht
# neu erfunden.
REVERSION_THRESH_LONG = 0.75
REVERSION_THRESH_SHORT = 0.85
# Posting-Tag BEIDER Richtungen: das Original-RUB1. Die Legacy-Modelle tragen keine
# Artefakt-Meta, also eine benannte Konstante (kein meta.model_id-Lookup mehr).
RUB_TAG = "RUB1"
# Der Tag, unter dem dieser Bot ZULETZT (RUB2-Generation, T-030…T-033) postete und
# unter dem noch offene Trades/Cooldowns liegen können. Nur für den transitionalen
# Dedup über den Tag-Wechsel RUB2 → RUB1 (Active-Trade-Check + Cooldown, Regel 4) —
# damit kein Doppel-Post entsteht, solange alte RUB2-Positionen noch offen sind.
RUB_LEGACY_TAG = "RUB2"

MODEL_LONG = None
MODEL_SHORT = None

# RUB3-Shadow (T-2026-CU-9050-125): der rub2_model_LONG-Retrain war "nicht
# deploybar" (kein positiver LONG-Operating-Point). Das LIVE-LONG-Bein fährt
# weiter das Legacy-Modell und postet unter Tag "RUB2"; der Retrain-Shadow läuft
# deshalb PARALLEL unter dem eigenen Generations-Tag "RUB3" (Operator-Entscheid
# Michi, Regel 6) — nie live, nur überwachte Shadow-Trades, damit sich zeigt, ob
# der saubere Retrain das Legacy-LONG schlägt (Regime-Frage §8/Teil 3). Der Tag
# unterscheidet sich per RICHTUNG von einem etwaigen künftigen RUB3-SHORT.
SHADOW_RUB3_LONG = None

# RUB4 (T-2026-CU-9050-164): funding-gegatetes RUB-LONG als SHADOW-Experiment.
# Retrospektiv (123 geschlossene RUB-LONG-Trades) dreht das ABR1-Funding-Gate das
# Aggregat von −2,9 %/Trade ins Plus (+1,6 %), aber nur 6/123 Trades passieren es
# → dünn, muss forward-validiert werden. RUB4 emittiert DENSELBEN RUB3-Kandidaten,
# aber NUR wenn fund_24h > +3 bps (ABR1-LONG-Schwelle) — eigener Tag, damit der
# Report gegatet (RUB4) vs. ungegatet (RUB3) vergleicht. Rein additiv, nie live.
FUNDING_GATE_LONG_BPS = 3.0
RUB4_GATED_LONG_TAG = "RUB4"


def funding_gate_open(fund_24h_bps) -> bool:
    """True, wenn das ABR1-Funding-Gate offen ist (fund_24h > +3 bps). Pure →
    DB-frei testbar. None (keine Funding-Daten) ⇒ Gate ZU (kein RUB4-Post)."""
    return fund_24h_bps is not None and fund_24h_bps > FUNDING_GATE_LONG_BPS


def load_models():
    """Loads the Mean Reversion models (RUB1 Legacy, beide Richtungen)."""
    global MODEL_LONG, MODEL_SHORT
    try:
        if os.path.exists(MODEL_LONG_PATH):
            MODEL_LONG = joblib.load(MODEL_LONG_PATH)
            logger.info("✅ Rubberband LONG-Modell (Legacy RUB1) loaded successfully.")
        else:
            logger.warning(f"Modell fehlt: {MODEL_LONG_PATH} — LONG-Seite aus.")
    except Exception as e:
        logger.error(f"❌ Error loading LONG-Modell: {e} — LONG-Seite aus.")

    try:
        if os.path.exists(MODEL_SHORT_PATH):
            MODEL_SHORT = joblib.load(MODEL_SHORT_PATH)
            logger.info("✅ Rubberband SHORT-Modell (Legacy RUB1) loaded successfully.")
        else:
            logger.warning(f"Modell fehlt: {MODEL_SHORT_PATH} — SHORT-Seite aus.")
    except Exception as e:
        logger.error(f"❌ Error loading SHORT-Modell: {e} — SHORT-Seite aus.")

    global SHADOW_RUB3_LONG
    SHADOW_RUB3_LONG = shadow_gate.load_shadow_artifact("RUB3", "LONG")
    if SHADOW_RUB3_LONG is not None:
        logger.info("👻 RUB3 (rub2_model_LONG) Shadow-Modell geladen.")


def _emit_rub3_shadow(conn, symbol, curr_close, base_features, now):
    """RUB3-Shadow-Emission (T-2026-CU-9050-125) — rein additiv, nie live.

    Scored denselben LONG-Vorfilter-Kandidaten wie der Live-Legacy-LONG-Pfad, aber
    mit dem sauberen rub2_model_LONG-Retrain (15 Features = 9 rub + 6 Funding,
    Funding as-of zum Candle-Close wie die SHORT-Seite/der Replay). Threshold ist
    null (kein deploybarer Operating-Point) → jeder Kandidat wird als überwachter
    Shadow-Trade unter Tag ``RUB3`` getrackt (kein Cornix). Geometrie = dieselbe
    LONG-HVN/S-R-Konstruktion wie der Live-Pfad (bewusst dupliziert). Fehler
    bleiben gekapselt — der Live-RUB-Pfad darf nie betroffen sein.
    """
    if not shadow_gate.shadow_posting_enabled() or not shadow_gate.is_shadow("RUB3", "LONG"):
        return
    if SHADOW_RUB3_LONG is None:
        return
    try:
        feats = dict(base_features)
        ts_decision = now.replace(minute=0, second=0, microsecond=0)
        fund_by_sym = load_funding(conn, [symbol], since=now - datetime.timedelta(days=95))
        feats.update(funding_features_asof(fund_by_sym, symbol, ts_decision))
        prob = shadow_gate.score_artifact(SHADOW_RUB3_LONG, feats)
        thr = shadow_gate.artifact_threshold(SHADOW_RUB3_LONG)
        if thr is not None and prob < thr:
            return
        entry1 = curr_close
        entry2 = entry1 * 0.95
        supps, resis = get_hvn_and_sr_levels(conn, symbol, curr_close)
        sl = max([x for x in supps if x < entry2 * 0.99]) if any(x < entry2 * 0.99 for x in supps) else entry2 * 0.975
        t_cands = sorted([x for x in resis if x > (entry1 * 1.01)])
        targets = ensure_min_tp_distance(t_cands[:20], entry1, True, min_pct=0.05)
        if not targets:
            return
        wrote = post_shadow_ai_signal(conn, "RUB3", symbol, "LONG", prob, entry1, entry2, sl, targets, n_show=3)
        # RUB4-Funding-Gate-Variante: dasselbe Setup, aber NUR wenn fund_24h > +3 bps
        # (feats["fund_24h"] ist bereits berechnet). Testet, ob das Gate die
        # RUB-LONG-Seite rettet. Eigener Tag, fail-safe zu Stille wenn nicht SHADOW.
        if funding_gate_open(feats.get("fund_24h")) and shadow_gate.is_shadow(RUB4_GATED_LONG_TAG, "LONG"):
            if post_shadow_ai_signal(
                conn, RUB4_GATED_LONG_TAG, symbol, "LONG", prob, entry1, entry2, sl, targets, n_show=3
            ):
                wrote = True
        if wrote:
            conn.commit()
    except Exception as e:
        logger.warning(f"RUB3 Shadow für {symbol} fehlgeschlagen: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


# --- HAUPT CHECKER FUNKTION ---
def check_rubberband_conditions():
    # Entkoppelter Guard (PR-#9-Muster, bewahrt): ein fehlendes Legacy-Modell einer
    # Richtung darf die andere nicht mit abschalten. Die Richtungs-Guards im Loop
    # überspringen die nicht ladbare Seite einzeln (MODEL_LONG / MODEL_SHORT is None).
    if not (MODEL_LONG or MODEL_SHORT):
        logger.error("Modelle not loaded. Skipping Scan.")
        return

    conn = get_db_connection()
    try:
        with open('coins.json') as f:
            coins = json.load(f)
    except Exception as e:
        logger.error(f"Could not load coins.json: {e}")
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    logger.info(f"🔍 Starting Rubberband (RUB1) Scan für {len(coins)} Coins...")

    for symbol in coins:
        try:
            # USDT Filter (Verhindert Error for USDC Paaren)
            if 'USDT_' in symbol:
                continue

            # 1. 90 Tage Daten für die Trendberechnung holen — Erkennung läuft auf
            # GESCHLOSSENEN Kerzen (R1). core.candles mit include_forming=False ersetzt
            # den bisherigen `open_time < date_trunc('hour', NOW())`-Filter (P1.19):
            # der zentrale Closed-Cutoff ist für 1h identisch (period_start = Stunden-Floor).
            df_90d = read_candles(
                conn,
                symbol,
                "1h",
                start=now - datetime.timedelta(days=95),
                include_forming=False,
                columns=("open_time", "close"),
            )
            if len(df_90d) < 50:
                continue

            # 2. Letzte GESCHLOSSENE Indikator-Kerze — close mitziehen, damit curr_close
            # aus DERSELBEN Kerze stammt wie die Indikatoren (P1.19). include_forming=False
            # ersetzt den closed-candle-Filter; open_time nur fürs Ordering, danach raus.
            df_ind = read_indicators(
                conn,
                symbol,
                "1h",
                limit=1,
                include_forming=False,
                columns=(
                    "open_time",
                    "close",
                    "rsi_14",
                    "tsi_fast_12_7_7",
                    "tsi_fast_12_7_7_signal",
                    "macd_dif_normal_12_26_9",
                    "macd_dea_normal_12_26_9",
                    "atr_14",
                    "ema_200",
                    "donchian_lower_20",
                    "donchian_upper_20",
                ),
            )
            if df_ind.empty:
                continue
            ind = df_ind.iloc[-1].drop("open_time").to_dict()

            # --- TRENDBERECHNUNG ---
            # Regression + Vorfilter + Feature-Bau leben seit dem RUB2-Adapter
            # (2026-07-06) in core/rub_features — EINE Quelle für Bot UND
            # Walkforward-Replay (X-R1-Regel), wie find_break_retest_setups bei ABR.
            df_90d['ts'] = pd.to_datetime(df_90d['open_time'], utc=True).apply(lambda x: x.timestamp())
            ts_values = df_90d['ts'].values
            close_values = df_90d['close'].values.astype(float)

            # P1.19: curr_close aus der geschlossenen Indikator-Kerze (ind['close']),
            # nicht aus dem 90d-Preis-Array — so mischen dist_to_trend + alle ML-Features
            # nicht mehr Live-Preis mit Partial-Indikatoren. Fallback auf die (nun
            # ebenfalls geschlossene) letzte 90d-Kerze, falls close NaN/fehlt.
            try:
                curr_close = float(ind['close'])
                if not np.isfinite(curr_close):
                    curr_close = float(close_values[-1])
            except (TypeError, ValueError, KeyError):
                curr_close = float(close_values[-1])

            dist_to_trend_pct, slope_pct_per_day = rub_trend(ts_values, close_values, curr_close)

            # --- INDIKATOREN AUSLESEN ---
            def get_f(key, default=0.0, ind=ind):
                val = ind.get(key)
                # FIX: Vorher wurde nur auf `None` geprüft. pandas/postgres können aber
                # NaN/Inf liefern (insbesondere bei frischen Coins mit wenig Historie).
                # Wenn diese in die ML-Features fließen, crasht predict_proba oder
                # liefert unbrauchbare Werte. Jetzt: auch NaN/Inf → default.
                try:
                    if val is None:
                        return default
                    fv = float(val)
                    if not np.isfinite(fv):
                        return default
                    return fv
                except (TypeError, ValueError):
                    return default

            rsi = get_f('rsi_14', 50)
            tsi_line = get_f('tsi_fast_12_7_7')
            tsi_signal = get_f('tsi_fast_12_7_7_signal')
            macd_line = get_f('macd_dif_normal_12_26_9')
            macd_signal = get_f('macd_dea_normal_12_26_9')
            atr_14 = get_f('atr_14')
            ema_200 = get_f('ema_200', curr_close)
            dc_lower = get_f('donchian_lower_20', curr_close)
            dc_upper = get_f('donchian_upper_20', curr_close)

            # --- VORFILTERUNG (RUBBERBAND BEDINGUNGEN) — geteilte Quelle ---
            event_type = rub_event_type(dist_to_trend_pct, rsi, tsi_line, curr_close, dc_lower, dc_upper)
            if not event_type:
                continue

            # --- ML FEATURES BERECHNEN — geteilte Quelle ---
            base_features = build_rub_features(
                dist_to_trend_pct,
                slope_pct_per_day,
                curr_close,
                rsi,
                tsi_line,
                tsi_signal,
                macd_line,
                macd_signal,
                atr_14,
                ema_200,
            )

            is_long = event_type == "REVERSION_UP"
            direction = "LONG" if is_long else "SHORT"
            # Posting-Tag BEIDER Richtungen: das Original-RUB1 (T-2026-KYT-9050-037
            # Revive). Beide Seiten fahren wieder das Legacy-Reversion-Modell ohne
            # Artefakt-Meta, also eine einzige benannte Konstante — kein richtungs-
            # abhängiger meta.model_id-Lookup mehr (der galt der RUB2-SHORT-Generation,
            # jetzt gebencht). Der RUB3/RUB4-LONG-Challenger postet weiter unter eigenem
            # Tag (siehe _emit_rub3_shadow) und kollidiert damit nicht mit RUB1.
            module_tag = RUB_TAG

            # 1. Aktiver Trade Check (T-2026-CU-9050-043) — prüft, ob für genau dieses
            #    Modul/Coin/Richtung bereits ein nicht-geschlossener Trade läuft.
            #    Der Cooldown darunter ist eine FREQUENZ-Sperre (4h), kein Positions-
            #    Guard: ein RUB-Trade läuft bei Mean-Reversion regelmäßig länger als
            #    sein Cooldown, und ohne diesen Check öffnete das Folgesignal eine
            #    ZWEITE Live-Position neben der ersten. Muster: 11_ai_mis_bot.py.
            #
            #    Der Check läuft über den Tag, und der Tag wechselt mit dem RUB1-Revive
            #    (RUB2 → RUB1, T-037). Ohne den Alt-Tag im IN würde eine noch offene
            #    RUB2-Position denselben Coin/Direction nicht mehr blocken → möglicher
            #    Doppel-Post über den Tag-Wechsel (Regel 4).
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM ai_signals
                    WHERE symbol = %s AND direction = %s AND model IN (%s, %s)
                """,
                    (symbol, direction, module_tag, RUB_LEGACY_TAG),
                )
                trade_exists = cur.fetchone()

            if trade_exists:
                continue  # Trade läuft live im AI Monitor

            # 2. FIX: Cooldown-Check VOR der teuren ML-Prediction.
            # Vorher lief predict_proba auch dann, wenn der Coin noch im Cooldown war
            # (bei 500 Coins × mehreren Event-Typen = viel verschwendete CPU).
            # Der Shadow-Log unterhalb bleibt erhalten — er dokumentiert alle
            # potenziellen Trades, auch die abgelehnten. Beim Skip durch Cooldown
            # loggen wir weiterhin fürs Monitoring.
            # Transitionaler Dedup (T-037 Revive): der Cooldown-Key ist der Tag, und der
            # wechselt mit dem RUB1-Revive (RUB2 → RUB1). Eine frische RUB2-Cooldown-Row
            # würde ein RUB1-Signal auf demselben Coin sonst nicht mehr sperren. Also
            # zusätzlich gegen den Alt-Tag prüfen. Dieselbe Transitional-Logik trägt
            # der Active-Trade-Check oben — beide Sperren müssen den Generationswechsel
            # überstehen, sonst reißt der Schutz an der jeweils anderen Stelle auf.
            cooldown_tags = [module_tag] if module_tag == RUB_LEGACY_TAG else [module_tag, RUB_LEGACY_TAG]
            if any(check_cooldown(conn, t, symbol, direction, 4) for t in cooldown_tags):
                logger.debug(f"RUB1 Prediction für {symbol} {direction} im Cooldown — skip.")
                continue

            # Prediction (teuer, erst after Cooldown-Check). Beide Richtungen fahren das
            # ORIGINALE Legacy-Reversion-Modell auf den 9 rub-Features (KEIN Funding) mit
            # ihrem Original-Threshold — Parität zur Vor-PR-#9-RUB1-Logik (git 07c8874^).
            if is_long:
                if MODEL_LONG is None:
                    continue
                threshold = REVERSION_THRESH_LONG
                prob = MODEL_LONG.predict_proba(pd.DataFrame([base_features]))[0, 1]
            else:
                if MODEL_SHORT is None:
                    continue
                threshold = REVERSION_THRESH_SHORT
                prob = MODEL_SHORT.predict_proba(pd.DataFrame([base_features]))[0, 1]

            logger.info(f"RUB1 Trigger: {symbol} {direction} | ML-Conf: {prob:.1%} (Thresh: {threshold:.2f})")

            # RUB3-Shadow (T-2026-CU-9050-125): denselben LONG-Kandidaten mit dem
            # sauberen Retrain scoren + überwacht tracken, unabhängig vom Live-Pfad.
            if is_long:
                _emit_rub3_shadow(conn, symbol, curr_close, base_features, now)

            # --- SHADOW MODE LOGGING ---
            # Direction-Gate ENTFERNT (Operator 2026-07-06): LONG handelt wieder
            # (Audit-Batch hatte LONG nach Report 14 D.5 in den Shadow gelegt).
            if prob < threshold:
                # Ablegen in Master Tabelle (als abgelehnter Trade)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                        VALUES (0, %s, %s, %s, %s, %s, %s, False)
                    """,
                        (module_tag, now, symbol, direction, float(curr_close), float(prob)),
                    )
                conn.commit()
                continue

            # 🔥 TRADE AUSFÜHREN
            logger.info(f"🔥 RUB1 TRADE EXECUTE: {symbol} {direction} (ML {prob:.1%})")

            entry1 = curr_close
            entry2 = entry1 * 0.95 if is_long else entry1 * 1.05
            supps, resis = get_hvn_and_sr_levels(conn, symbol, curr_close)

            if is_long:
                sl = (
                    max([x for x in supps if x < entry2 * 0.99])
                    if any(x < entry2 * 0.99 for x in supps)
                    else entry2 * 0.975
                )
                t_cands = sorted([x for x in resis if x > (entry1 * 1.01)])
            else:
                sl = (
                    min([x for x in resis if x > entry2 * 1.01])
                    if any(x > entry2 * 1.01 for x in resis)
                    else entry2 * 1.025
                )
                t_cands = sorted([x for x in supps if x > 0 and x < (entry1 * 0.99)], reverse=True)

            # FIX: Vorher `while len(targets) < 20: append last*1.02` → extrapolierte
            # bis +48% über Entry, bei Mean-Reversion-Bots absurd. Jetzt nur noch:
            # echte Zonen nehmen, und ggf. EIN 5%-Target anhängen wenn das letzte
            # zu nah am Entry liegt.
            targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long, min_pct=0.05)
            # P2.31: publish AND track exactly the same targets. The Cornix block
            # shows the first n_show TPs; the AI monitor (8_ai_trade_monitor) scores
            # whatever is stored in ai_signals.targets. Storing the full 20-zone list
            # made the monitor score phantom TPs the subscriber never saw.
            n_show = 3

            # Fleet-Lifecycle-Gate (T-2026-KYT-9050-033) an der Emissions-Stelle.
            # module_tag == RUB1 ist im Register explizit LIVE (T-037, Defense-in-Depth)
            # ⇒ route_legacy_leg gibt LEG_LIVE zurück und der Bot postet wie unten (Cornix
            # + ai_signals). Die gebenchte RUB2-Generation bleibt daneben SHADOW; der
            # RUB3/RUB4-LONG-Challenger unverändert Shadow (oben, _emit_rub3_shadow).
            # Rein additiv (Regel 4).
            _route = route_legacy_leg(
                conn, module_tag, direction, symbol, prob, entry1, entry2, sl, targets, n_show=n_show
            )
            if _route != LEG_LIVE:
                if _route == LEG_SHADOW:
                    conn.commit()
                continue

            lev = get_max_leverage(symbol, 20)

            # Cornix Text
            lines = [
                f"📈 Signal for {symbol} 📈",
                f"🚨 Direction: {direction}",
                f"🚨 Leverage: {lev}",
                "🚨 Margin: Cross",
                f"🏦 CMP Entry: $ {entry1:.8f}",
                f"🏦 Entry 2: $ {entry2:.8f}",
            ]
            for i, t in enumerate(targets[:n_show], 1):
                lines.append(f"💰 TP{i}: $ {t:.8f}")
            lines += [f"💸 Stop Loss: $ {sl:.8f}", f"🧠 Trade idea generated by AI module {module_tag}"]
            cornix_msg = "\n".join(lines)

            # HTML für Chart
            emoji = "🚀 RUBBERBAND MEAN REVERSION LONG" if is_long else "💥 RUBBERBAND MEAN REVERSION SHORT"
            dist_str = f"{dist_to_trend_pct * 100:+.2f}%"

            # FIX Doppel-Post (2026-07-06, gleiche Fehlerklasse wie Bot 18/7):
            # Chart-Caption ohne eingebetteten Cornix-Block.
            html_caption = f"""<pre><b>{emoji}</b>\n<b>{symbol.replace('USDT', '')}/USDT</b>\n<b>→ Direction: {direction}</b>\n<b>→ Confidence: <b>{prob:.1%}</b> (Thresh {threshold})</b>\n<b>→ Price: {curr_close:.4f}</b>\n<b>→ Trend Distance: <b>{dist_str}</b></b>\n<b>→ Time: {now.strftime('%H:%M')} UTC | Modul: {module_tag}</b></pre>"""

            chart_buf = generate_minichart_image(symbol, minutes=240)
            with conn.cursor() as cur:
                # Cornix Channel (Hier nutzt er den speziellen Rubberband Channel!)
                cur.execute(
                    "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                    (RUBBERBAND_CHANNEL_ID, cornix_msg),
                )
                # Chart Channel
                if chart_buf:
                    cur.execute(
                        "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                        (RUBBERBAND_CHANNEL_ID, html_caption, chart_buf),
                    )
                else:
                    cur.execute(
                        "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                        (RUBBERBAND_CHANNEL_ID, html_caption),
                    )

                # AI Signal Monitor

                cur.execute(
                    """
                                INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                    (
                        symbol,
                        float(entry1),
                        module_tag,
                        direction,
                        float(prob),
                        float(entry1),
                        float(entry2),
                        float(sl),
                        json.dumps(targets[:n_show]),
                    ),
                )
                # Master Log
                cur.execute(
                    """INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted) VALUES (0, %s, %s, %s, %s, %s, %s, True)""",
                    (module_tag, now, symbol, direction, float(curr_close), float(prob)),
                )

            conn.commit()
            update_cooldown(conn, module_tag, symbol, direction)

        except Exception as e:
            logger.error(f"Error for {symbol} in RUB1: {e}")
            if conn:
                conn.rollback()

    if conn:
        conn.close()
    logger.info("🏁 RUB1 Model Check stopped.")


def main():
    logger.info("=== 🎯 AI RUBBERBAND BOT (RUB1) GESTARTET ===")

    # Modelle laden
    load_models()

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)

        # P3.10: comments corrected to match code — fires at minute 10 (not 12).
        if now.minute == 10:
            check_rubberband_conditions()
            # Schlafen, damit er nicht mehrfach in Minute 10 triggert
            time.sleep(60)
        else:
            # Checkt alle 10 Sekunden, ob Minute 10 erreicht ist
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")
