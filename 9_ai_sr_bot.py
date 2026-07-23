import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
import json
import logging
import time

import numpy as np
import pandas as pd

from core import config as _kcfg  # channel ids
from core import shadow_gate
from core.candles import read_indicators
from core.charting import generate_minichart_image

# --- CORE IMPORTE ---
from core.database import get_db_connection
from core.market_utils import check_cooldown, get_max_leverage, update_cooldown
from core.model_artifacts import load_artifact_json, maybe_reload
from core.signal_post import LEG_LIVE, LEG_SHADOW, has_open_ai_signal, post_ai_signal_gated, route_legacy_leg
from core.sra_features import SRA2_FEATURES, build_sra2_features
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_SR_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- MODELL-ARTEFAKTE (natives XGB-JSON + optionale Meta-/Calib-Sidecars) ---
#
# Die Dateinamen sind generationsfreie SLOTS — der Operator kopiert das
# promotete Artefakt hierher. Die Generation steht ausschliesslich in der
# Sidecar-Meta (`*_meta.json` → model_id), nie im Dateinamen und nie in einer
# Quellcode-Konstante (harte Regel 6, T-2026-CU-9050-042). Ein SRA3-Retrain
# postet damit als SRA3, statt still mit SRA2 in derselben Per-Bot-Statistik zu
# verschmelzen, auf der das Orchestrator-Gating entscheidet.
SRA_ARTIFACT_PATHS = {
    'LONG': "trade_success_xgb_LONG_v2.json",
    'SHORT': "trade_success_xgb_SHORT_v2.json",
}
# Der Tag, unter dem dieser Bot vor T-2026-CU-9050-042 BEIDE Richtungen postete.
# Zwei Rollen: default_tag fürs heutige Legacy-Artefakt (keine Meta ⇒ kein
# model_id) UND transitionaler Dedupe-Key — siehe check_cooldown/Duplikatprüfung.
SRA_LEGACY_TAG = 'SRA1'
# Posting-Schwelle des Legacy-Modells. Ein Artefakt mit Meta bringt seine eigene
# mit (optimal_threshold aus dem Validation-Slice) und überschreibt sie.
# 0.65→0.70 (T-2026-CU-9050-171): auf den realisierten SRA1-Trades (n=748,
# 03–07/2026) ist das Segment 0.65–0.70 netto negativ (Ø −0,10 %); ab 0.70
# bleiben 62 % der Trades mit MEHR Gesamt-PnL (302 vs. 274) und WR 52→55,5 %.
SRA_LEGACY_THRESHOLD = 0.70
# Shadow-Log-Untergrenze: alles darüber landet in ml_predictions_master.
SRA_SHADOW_THRESHOLD = 0.35

MODELS: dict[str, dict] = {}

# SRA2-Shadow (T-2026-CU-9050-125): der SRA2-Retrain war "nicht deploybar", WEIL
# die Label-Quelle closed_trades3 tot ist — ein reines TRAININGS-Problem. Shadow-
# Serving umgeht das: es scored den live S/R-Kandidatenstrom durch den geteilten
# core.sra_features-Builder + das staging-Artefakt und lässt den AI-Monitor die
# frischen Outcomes (closed_ai_signals) sammeln — genau die Labels, die der tote
# Tracker nicht mehr liefert. SRA1 bleibt unangetastet live.
SHADOW_SRA2: dict[str, object | None] = {"LONG": None, "SHORT": None}


def sra_expected_features() -> list[str]:
    """Die Feature-Namen, die dieser Bot bauen KANN — Legacy-Vektor ∪ SRA2-Vertrag.

    Der harte Feature-Vertrag (P0.12): verlangt ein Artefakt eine Spalte, die
    hier fehlt, wird es nicht geladen und der Bot idlet — nie fillna(0), denn
    das Modell hätte diese Spalte im Training nie als 0 gesehen. Die Liste wird
    aus den Buildern ABGELEITET, nicht danebengeschrieben (zwei Listen driften).
    """
    dummy: dict = dict.fromkeys(SRA_INDICATOR_COLS, 1.0)
    dummy['trend_direction'] = 'UP'
    legacy = create_feature_row('LONG', dummy) or {}
    return list(dict.fromkeys([*legacy.keys(), *SRA2_FEATURES]))


def build_serving_row(direction, indicators) -> dict | None:
    """Der Feature-Frame für ein Artefakt MIT Meta.

    Achtung, geteilte Spaltennamen mit UNTERSCHIEDLICHER Semantik: der alte
    Bot-Vektor rechnet ``pct_*`` gegen den Close, der SRA2-Builder gegen den
    Referenzwert (ema9, wma9, …). Der SRA2-Builder gewinnt deshalb bewusst
    (rechts im Merge) — er definiert die Semantik, auf der das Artefakt
    trainiert wurde. Der Legacy-Pfad unten fasst diesen Frame nie an.
    """
    legacy = create_feature_row(direction, indicators)
    if not legacy:
        return None
    return {**legacy, **build_sra2_features(indicators)}


def load_models() -> None:
    """Lädt beide Richtungs-Artefakte. Fehlt eines, idlet NUR diese Richtung
    (Falle 3: ein Bot ohne Artefakt startet und tut nichts, statt in den
    Watchdog-Restart-Loop zu laufen — vorher war das ein `exit(1)`)."""
    expected = sra_expected_features()
    for direction, path in SRA_ARTIFACT_PATHS.items():
        MODELS[direction] = load_artifact_json(path, expected, SRA_LEGACY_TAG, SRA_LEGACY_THRESHOLD)
    if not any(a['loaded'] for a in MODELS.values()):
        logger.error("❌ Kein SRA-Artefakt ladbar — Bot läuft im Idle-Modus bis zum Deploy.")

    # SRA2-Shadow-Modelle aus staging_models/ fail-soft nachladen.
    for direction in ("LONG", "SHORT"):
        SHADOW_SRA2[direction] = shadow_gate.load_shadow_artifact("SRA2", direction)
    if any(SHADOW_SRA2.values()):
        loaded = [d for d, m in SHADOW_SRA2.items() if m is not None]
        logger.info(f"👻 SRA2 Shadow-Modelle geladen: {', '.join(loaded)}")


def _emit_max2(conn, coin, prob, entry1, entry2, sl, targets) -> None:
    """MAX2 (T-2026-KYT-9050-020): der SRA2-LONG-Trade in den Main-Channel.

    MAX2 ist KEIN eigenes Modell und kein eigener Prozess — es ist ein Fork der
    SRA2-LONG-Emission (Aufruf aus _emit_sra2_shadow): feuert SRA2 LONG
    (prob>=threshold) für einen Coin aus config.MAIN_CHANNEL_COINS, wird DERSELBE
    Trade (gleiche prob + Entry/SL/Target-Geometrie) zusätzlich unter Tag "MAX2"
    nach CH_MAIN emittiert. Ersetzt den retireten klassischen "Main Channel"-
    Detektor (3_detectors.py), der auf genau dieser Coin-Whitelist lief — der
    einzige Filter ist die Whitelist, exakt wie zuvor (Operator-Entscheid Michi).

    LONG-only: SRA2 SHORT ist ein toter Shadow-Leg (threshold=None, Labels seit
    23.02 tot). MAX2 postet default-LIVE (leg_status("MAX2","LONG")=LIVE) — das
    ist kollisionsfrei mit dem SRA2-Post nach CH_AI_SR, WEIL CH_AI_SR NICHT
    Cornix-executed ist (informativ/Orchestrator, Operator-bestätigt); sonst wäre
    es ein Regel-4-Doppel-Trade auf den 37 Coins. Eigener Tag ⇒ eigener Cooldown-/
    Dedup-Namespace via has_open("MAX2") (Regel 6). Fehler bleiben gekapselt; der
    bereits committete SRA2-Post ist unberührt (eigene Txn, eigenes Rollback).
    """
    if not shadow_gate.shadow_posting_enabled() or shadow_gate.leg_status("MAX2", "LONG") not in (
        shadow_gate.LIVE,
        shadow_gate.SHADOW,
    ):
        return
    try:
        if has_open_ai_signal(conn, coin, "LONG", "MAX2"):
            return
        outcome = post_ai_signal_gated(
            conn,
            "MAX2",
            "LONG",
            _kcfg.CH_MAIN,
            coin,
            prob,
            entry1,
            entry2,
            sl,
            targets,
            source_desc="AI MAX2 (SRA2 S/R, Main-Channel-Filter)",
            n_show=3,
        )
        if outcome is not None:
            conn.commit()
    except Exception as e:
        logger.warning(f"MAX2 für {coin} LONG fehlgeschlagen: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


def _emit_sra2_shadow(conn, coin, direction, t_time, live_price) -> None:
    """SRA2-Emission über das shadow_gate-Routing (T-2026-CU-9050-125/185).

    Scored denselben S/R-Kandidaten wie der Live-SRA1-Pfad über den geteilten
    core.sra_features-Builder + das SRA2-Artefakt und emittiert bei prob>=threshold
    via post_ai_signal_gated: das LIVE-Bein SRA2 LONG (@0.6424, T-185, Artefakt im
    Repo-Root) emittiert eine Cornix-Format-Message an CH_AI_SR (koexistierend mit
    SRA1) — Cornix ist auf CH_AI_SR NICHT subscribed (informativ/Orchestrator),
    weshalb der MAX2-Fork denselben Trade kollisionsfrei nach CH_MAIN spiegeln kann
    (siehe _emit_max2). Das SHADOW-Bein SRA2 SHORT (threshold=None, staging) bleibt
    ein überwachter Shadow-Trade (kein Cornix). Geometrie = dieselbe HVN/S-R-Konstruktion wie process_ai_trade (bewusst
    dupliziert, um den Live-Pfad nicht anzufassen). Fehler bleiben gekapselt.
    """
    if not shadow_gate.shadow_posting_enabled() or shadow_gate.leg_status("SRA2", direction) not in (
        shadow_gate.LIVE,
        shadow_gate.SHADOW,
    ):
        return
    art = SHADOW_SRA2.get(direction)
    if art is None:
        return
    try:
        inds = get_indicators_at_time(conn, coin, t_time)
        if not inds:
            return
        serving = build_serving_row(direction, inds)
        if not serving:
            return
        prob = shadow_gate.score_artifact(art, serving)
        thr = shadow_gate.artifact_threshold(art)
        if thr is not None and prob < thr:
            return
        # Duplikat-Schutz für den LIVE-Leg (SRA2 LONG, T-185): post_ai_signal (live)
        # macht KEINEN has_open-Check — nur post_shadow_ai_signal tat das intern.
        # Deshalb hier explizit vor der teuren Geometrie prüfen (analog Bot 10).
        if has_open_ai_signal(conn, coin, direction, "SRA2"):
            return
        is_long = direction == "LONG"
        entry1 = float(live_price)
        entry2 = entry1 * 0.95 if is_long else entry1 * 1.05
        supps, resis = get_hvn_and_sr_levels(conn, coin, live_price)
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
        targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long, min_pct=0.05)
        if not targets:
            return
        outcome = post_ai_signal_gated(
            conn,
            "SRA2",
            direction,
            _kcfg.CH_AI_SR,  # LIVE-Leg SRA2 LONG → SRA-Channel (T-185); SHORT bleibt Shadow
            coin,
            prob,
            entry1,
            entry2,
            sl,
            targets,
            source_desc="AI SRA2 S/R Meta-Filter",
            n_show=3,
        )
        if outcome is not None:
            conn.commit()
        # MAX2 (T-2026-KYT-9050-020): denselben SRA2-LONG-Trade coin-gefiltert in
        # den Main-Channel forken — ersetzt den retireten klassischen Main-Channel-
        # Bot, der auf genau MAIN_CHANNEL_COINS lief. Nur LONG (SRA2 SHORT ist tot).
        if direction == "LONG" and coin in _kcfg.MAIN_CHANNEL_COINS:
            _emit_max2(conn, coin, prob, entry1, entry2, sl, targets)
    except Exception as e:
        logger.warning(f"SRA2 Shadow für {coin} {direction} fehlgeschlagen: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


# FEATURE & INDIKATOR HELFER


def get_indicators_at_time(conn, coin, timestamp):
    """Holt die 1h-Indikatoren zur letzten GESCHLOSSENEN Kerze <= timestamp.

    R1: include_forming=False — die Features/Erkennung laufen nie auf der forming
    Kerze. Feuerte ein Trade mitten in der laufenden Stunde, lieferte `<= timestamp`
    sonst die Partial-Indikatoren dieser Stunde.
    """
    try:
        df = read_indicators(conn, coin, "1h", limit=1, end=timestamp, include_forming=False)
        if df.empty:
            return None
        return df.iloc[-1].to_dict()
    except Exception as e:
        logger.debug(f"Indikatoren-DB-Fehler für {coin}: {e}")
        return None


# Roh-Indikatorspalten, die create_feature_row 1:1 übernimmt. Modul-Konstante,
# damit sra_expected_features() den Feature-Vertrag AUS dem Builder ableiten kann
# statt ihn danebenzuschreiben (zwei Listen driften auseinander).
SRA_INDICATOR_COLS = [
    'rsi_9',
    'rsi_14',
    'rsi_24',
    'macd_dif_fast_9_21_9',
    'macd_dea_fast_9_21_9',
    'tsi_fast_12_7_7',
    'tsi_fast_12_7_7_signal',
    'atr_14',
    'r_squared',
    'boll_upper_20',
    'boll_mid_20',
    'boll_lower_20',
    'donchian_upper_20',
    'donchian_lower_20',
    'donchian_mid_20',
    'support_price',
    'resistance_price',
    'ema_9',
    'ema_21',
    'wma_9',
    'wma_21',
    'kama_9',
    'kama_21',
    'close',
]


def create_feature_row(direction, indicators):
    """Erstellt das Feature-Dict für XGBoost basierend auf deiner Modell-Logik."""
    close = indicators.get('close', np.nan)
    if pd.isna(close) or close <= 0:
        return None

    features = {}
    base_cols = SRA_INDICATOR_COLS

    for col in base_cols:
        val = indicators.get(col)
        features[col] = float(val) if pd.notna(val) else np.nan

    trend_map = {'UP': 1.0, 'DOWN': -1.0, 'FLAT': 0.0, 'SIDEWAYS': 0.0}
    features['trend_direction_num'] = trend_map.get(str(indicators.get('trend_direction', '')).upper(), 0.0)

    def pct(a, b):
        return (a - b) / close * 100 if pd.notna(b) and close > 0 else np.nan

    features.update(
        {
            'pct_ema9': pct(close, indicators.get('ema_9')),
            'pct_ema21': pct(close, indicators.get('ema_21')),
            'pct_wma9': pct(close, indicators.get('wma_9')),
            'pct_kama9': pct(close, indicators.get('kama_9')),
            'pct_support': pct(close, indicators.get('support_price')),
            'pct_resist': pct(indicators.get('resistance_price'), close),
            'pct_boll_mid': pct(close, indicators.get('boll_mid_20')),
            'ema9_ema21_pct': pct(indicators.get('ema_9'), indicators.get('ema_21')),
            'kama9_kama21_pct': pct(indicators.get('kama_9'), indicators.get('kama_21')),
        }
    )

    atr = indicators.get('atr_14', np.nan)
    # FIX P1.20: ATR-Features IMMER emittieren — fehlt ATR, hatte der
    # Feature-Vektor 35 statt 38 Spalten, predict_proba warf und die ganze
    # Scan-Iteration brach ab. XGBoost kann mit NaN nativ umgehen.
    if pd.notna(atr) and atr > 0:
        features.update(
            {
                'support_atr': (close - indicators.get('support_price', np.nan)) / atr,
                'resist_atr': (indicators.get('resistance_price', np.nan) - close) / atr,
                'boll_width_atr': ((indicators.get('boll_upper_20', 0) - indicators.get('boll_lower_20', 0)) / atr),
            }
        )
    else:
        features.update({'support_atr': np.nan, 'resist_atr': np.nan, 'boll_width_atr': np.nan})

    features['is_long'] = 1.0 if direction.upper() == 'LONG' else 0.0
    return features


# TARGET CALCULATOR

# POSTING LOGIK


def process_ai_trade(conn, symbol, direction, module, live_price, confidence, chart_path=None) -> bool:
    """Calculates trade details, writes to outbox and monitor.

    Returns True wenn der Trade wirklich gepostet wurde, False wenn der
    interne Cooldown den Post unterdrückt hat (P2.30: der Caller schrieb
    vorher posted=True in ml_predictions_master, obwohl nie gepostet wurde).
    """
    target_channel = _kcfg.CH_AI_SR  # Dein Ziel-Kanal

    # FIX: Vorher eigener Cooldown-Check mit `pd.Timestamp.utcnow().tz_localize(None)`
    # → crashes in newer pandas versions (utcnow is tz-aware there) and mixes
    # tz-aware/tz-naive Vergleiche. Jetzt: saubere Version aus market_utils.
    #
    # Transitionaler Dedup (T-2026-CU-9050-042): der Cooldown-Key IST der Tag, und
    # der Tag wechselt beim Retrain-Rollout (SRA1 → SRA2). Eine frische
    # SRA1-Cooldown-Row würde ein SRA2-Signal auf demselben Coin dann nicht mehr
    # sperren, und Cornix öffnete eine zweite Live-Position neben der ersten.
    # Also zusätzlich gegen den Alt-Tag prüfen; solange die Tags gleich sind
    # (heute, Legacy-Artefakt ohne Meta), fällt der zweite Query weg.
    cooldown_tags = [module] if module == SRA_LEGACY_TAG else [module, SRA_LEGACY_TAG]
    if any(check_cooldown(conn, t, symbol, direction, 4) for t in cooldown_tags):
        return False

    # 2. Level & Targets
    is_long = direction == "LONG"
    entry1 = float(live_price)
    entry2 = entry1 * 0.95 if is_long else entry1 * 1.05
    supps, resis = get_hvn_and_sr_levels(conn, symbol, live_price)

    if is_long:
        sl = max([x for x in supps if x < entry2 * 0.99]) if any(x < entry2 * 0.99 for x in supps) else entry2 * 0.975
        t_cands = sorted([x for x in resis if x > (entry1 * 1.01)])
    else:
        sl = min([x for x in resis if x > entry2 * 1.01]) if any(x > entry2 * 1.01 for x in resis) else entry2 * 1.025
        t_cands = sorted([x for x in supps if x > 0 and x < (entry1 * 0.99)], reverse=True)

    # FIX: echte Zonen + ggf. 5%-Target wenn letzte Zone zu nah
    targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long, min_pct=0.05)
    # P2.31: publish AND track exactly the same targets. The Cornix block below
    # shows the first n_show TPs; the AI monitor (8_ai_trade_monitor) scores
    # whatever is stored in ai_signals.targets. Storing the full 20-zone list made
    # the monitor score phantom TPs the subscriber never saw. Single source for both.
    n_show = 3
    lev = get_max_leverage(symbol, 20)
    # 3. Cornix & Telegram
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
    lines += [f"💸 Stop Loss: $ {sl:.8f}", f"🧠 Trade idea generated by AI module {module} V3"]
    cornix_msg = "\n".join(lines)

    # FIX Doppel-Post (2026-07-06, Flotten-Sweep): Caption ohne eingebetteten
    # Cornix-Block — Cornix parste sonst beide Nachrichten als Signale.
    html_caption = f"<b>💥 AI {module} {direction} SIGNAL</b>\n<b>{symbol.replace('USDT', '')}/USDT</b>\n→ Direction: {direction}\n→ ML Confidence: <b>{confidence:.1%}</b>\n→ Time: {datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M')} UTC"

    # T-2026-KYT-9050-033 (Audit T-032): Fleet-Lifecycle-Gate. Default LIVE ⇒ keine
    # Verhaltensänderung. SRA1 ist in beiden Richtungen geparkt → SHADOW (überwachter
    # Trade statt Cornix); SRA2 ist der Live-Nachfolger (s. shadow_gate). Rein additiv
    # (Regel 4). Rückgabe False ⇒ der Caller loggt ml_predictions_master posted=False
    # (wie bei Cooldown-Suppression). Hinweis: post_shadow_ai_signal loggt zusätzlich
    # EINE Shadow-Prediction (trade_id=0) — bewusst in Kauf genommen; der monitored
    # ai_signals-Trade (Audit-Datenquelle) bleibt via has_open singulär.
    _route = route_legacy_leg(conn, module, direction, symbol, confidence, entry1, entry2, sl, targets, n_show=n_show)
    if _route != LEG_LIVE:
        if _route == LEG_SHADOW:
            conn.commit()
        return False

    with conn.cursor() as cur:
        # Cornix Text
        cur.execute("INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (target_channel, cornix_msg))
        # Chart Image
        if chart_path:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                (target_channel, html_caption, chart_path),
            )
        # Monitor

        cur.execute(
            """
                        INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
            (
                symbol,
                float(entry1),
                module,
                direction,
                float(confidence),
                float(entry1),
                float(entry2),
                float(sl),
                json.dumps(targets[:n_show]),
            ),
        )
    # FIX (Review Batch 4): Cooldown in DERSELBEN Transaktion wie Outbox +
    # ai_signals setzen. Vorher lief update_cooldown NACH conn.commit() —
    # warf der Cooldown-Upsert (z.B. lock_timeout), blieb der Post committed,
    # aber ohne Cooldown und ohne master-Log → der nächste Scan-Pass hat
    # denselben Trade erneut gepostet (Doppel-Exposure bei Cornix).
    update_cooldown(conn, module, symbol, direction, commit=False)
    conn.commit()
    logger.info(f"🚀 {module} Trade für {symbol} erfolgreich abgefeuert!")
    return True


# MAIN LOOP


def main():
    logger.info("=== 🧠 ML SR BOT AKTIVIERT ===")
    load_models()

    while True:
        # Tägliches Reload nimmt einen Artefakt-Deploy ohne Restart auf; ein
        # fehlgeschlagener Reload verwirft ein geladenes Artefakt nicht.
        expected = sra_expected_features()
        for direction in SRA_ARTIFACT_PATHS:
            MODELS[direction] = maybe_reload(MODELS[direction], expected)

        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # 1. Frische S&R Trades aus der Master-Tabelle suchen
                cur.execute("""
                    SELECT id, time, coin, direction, entry
                    FROM active_trades_master
                    WHERE strategy = 'Support Resistance'
                    AND posted >= NOW() - INTERVAL '60 minutes'
                """)
                fresh_trades = cur.fetchall()

                for trade in fresh_trades:
                    t_id, t_time, coin, direction, entry = trade

                    # FIX P1.20: per-Trade-Isolation. Vorher riss EIN kaputter
                    # Trade (z.B. predict-Fehler) die ganze Iteration ab und der
                    # Pass-Rollback verwarf auch die Shadow-Inserts aller schon
                    # verarbeiteten Trades. Jetzt: commit pro Trade, rollback
                    # betrifft nur den einen.
                    try:
                        # SRA2-Shadow (T-2026-CU-9050-125): denselben Kandidaten
                        # unabhängig vom SRA1-Live-Pfad scoren + überwacht tracken.
                        _emit_sra2_shadow(conn, coin, direction, t_time, entry)

                        artifact = MODELS.get(direction)
                        if not artifact or not artifact['loaded']:
                            continue  # Idle-Modus dieser Richtung (Falle 3)
                        # Der Posting-Tag kommt aus der Artefakt-Meta (load_artifact_json
                        # setzt tag = meta.model_id); ohne Meta bleibt es der benannte
                        # Legacy-Tag. Er trägt ai_signals.model, ml_predictions_master
                        # .model_name und den Cooldown-Key — alle drei müssen dieselbe
                        # Generation nennen, sonst mischt die Per-Bot-Statistik zwei
                        # Modelle (Regel 6).
                        module_name = artifact['tag']

                        # 2. Duplikatprüfung in Master-Log
                        #    Transitional: auch gegen den Alt-Tag prüfen. Ohne ihn hielte
                        #    ein SRA2-Rollout jeden bereits verarbeiteten Trade für neu und
                        #    postete ihn ein zweites Mal.
                        cur.execute(
                            "SELECT 1 FROM ml_predictions_master WHERE trade_id = %s AND model_name IN (%s, %s)",
                            (t_id, module_name, SRA_LEGACY_TAG),
                        )
                        if cur.fetchone():
                            continue

                        # 3. Aktiver Trade Check (T-2026-CU-9050-055) — prüft, ob für
                        #    genau dieses Modul/Coin/Richtung bereits ein nicht-
                        #    geschlossener Trade läuft. Der 4h-Cooldown im Post-Pfad ist
                        #    eine FREQUENZ-Sperre, kein Positions-Guard: ein SRA-Trade
                        #    läuft regelmässig länger, und ohne diesen Check öffnete das
                        #    Folgesignal eine ZWEITE Live-Position neben der ersten
                        #    (dieselbe Lektion wie RUB, T-2026-CU-9050-043).
                        #    Muster: 11_ai_mis_bot.py:318. Die Duplikatprüfung darüber
                        #    schützt nur gegen denselben trade_id, nicht gegen einen
                        #    NEUEN Setup-Trade auf einem Coin, der schon offen ist.
                        #
                        #    Der Tag ist zugleich der Dedupe-Key und kippt beim
                        #    SRA2-Rollout; ohne den Alt-Tag im IN blockte eine offene
                        #    SRA1-Position das SRA2-Signal nicht mehr. Gleiche Tags
                        #    (heute) ⇒ No-op.
                        cur.execute(
                            "SELECT 1 FROM ai_signals WHERE symbol = %s AND direction = %s AND model IN (%s, %s)",
                            (coin, direction, module_name, SRA_LEGACY_TAG),
                        )
                        if cur.fetchone():
                            continue  # Trade läuft live im AI Monitor

                        # 4. Indikatoren & Features
                        inds = get_indicators_at_time(conn, coin, t_time)
                        if not inds:
                            continue

                        features = create_feature_row(direction, inds)
                        if not features:
                            continue

                        # 5. XGBoost Vorhersage
                        #    Artefakt MIT Meta: Frame aus dem geteilten SRA2-Builder,
                        #    ausgerichtet auf den Vertrag — Auswahl UND Reihenfolge. Keine
                        #    fillna über Spalten: load_artifact_json hat die Namen hart
                        #    validiert (P0.12); fehlende WERTE bleiben NaN, das kennt das
                        #    Modell aus dem Training.
                        #    Artefakt OHNE Meta: der Legacy-Vektor, unverändert — er ist
                        #    der Vertrag des heute deployten SRA1-Modells.
                        if artifact['features']:
                            serving = build_serving_row(direction, inds)
                            if not serving:
                                continue
                            X = pd.DataFrame([serving])[artifact['features']]
                        else:
                            X = pd.DataFrame([features])
                        conf = float(artifact['model'].predict_proba(X)[0, 1])

                        # 6. Klassifizierung & Schatten-Log
                        posted = False
                        if conf >= artifact['threshold']:
                            # FIX (Review Batch 4): NaN-ATR-Vektoren nicht live posten.
                            # P1.20 lässt fehlende ATR-Features als NaN durch, damit der
                            # Scan nicht mehr crasht — aber das Modell hat im Training nie
                            # NaN in diesen Spalten gesehen, die Confidence darauf ist
                            # unkalibriert. Solche Rows nur shadow-loggen, kein Cornix-Post.
                            if pd.isna(features.get('support_atr', np.nan)):
                                logger.info(f"⚠️ {coin} {direction} conf {conf:.1%} — ATR fehlt, nur Shadow-Log.")
                            else:
                                logger.info(f"🎯 Treffer! {coin} {direction} hat {conf:.1%} Confidence.")
                                chart_p = generate_minichart_image(coin, minutes=240)
                                # FIX P2.30: posted aus dem Rückgabewert — False wenn der
                                # interne 4h-Cooldown den Post unterdrückt hat.
                                posted = process_ai_trade(conn, coin, direction, module_name, entry, conf, chart_p)

                        # Alles >= SRA_SHADOW_THRESHOLD in die Master-History loggen
                        if conf >= SRA_SHADOW_THRESHOLD:
                            cur.execute(
                                """
                                INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                                (t_id, module_name, t_time, coin, direction, entry, conf, posted),
                            )
                        else:
                            # Darunter nur als "erledigt" markieren (minimales Log)
                            cur.execute(
                                "INSERT INTO ml_predictions_master (trade_id, model_name, coin, confidence, posted) VALUES (%s, %s, %s, %s, False)",
                                (t_id, module_name, coin, conf),
                            )
                        conn.commit()
                    except Exception as trade_err:
                        logger.error(f"SRA1: Fehler bei Trade {t_id} ({coin} {direction}): {trade_err}")
                        # Rollback guarded — auf einer toten Connection (DB-Restart)
                        # wirft rollback() selbst und würde sonst bis aus main()
                        # durchschlagen und den Prozess killen.
                        try:
                            conn.rollback()
                        except Exception:
                            logger.error("SRA1: rollback fehlgeschlagen — Pass-Abbruch, Connection wird erneuert.")
                            break

            conn.commit()
        except Exception as e:
            logger.error(f"Fehler im Loop: {e}")
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass  # tote Connection — close() im finally gibt den Slot frei
        finally:
            if conn:
                conn.close()

        time.sleep(300)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")
