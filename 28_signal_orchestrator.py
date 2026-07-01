# 28_signal_orchestrator.py
"""
Signal-Orchestrator — Phase 3 des Regime-Orchestrators.

Läuft alle 500ms und:
  1. Prüft telegram_outbox auf neue Bot-signals
  2. Identifiziert Bot, parst Coin + Direction
  3. Prüft Whitelist (4D oder Overall-Fallback bei Detektor-Ausfall)
  4. Reicht whitelisted signals in REGIME_TRADING_CHANNEL_ID durch
  5. Tracked Trades als ROM1 in ai_signals + orchestrator_open_trades
  6. Prüft bei Regime-Wechsel auf zu schließende Trades → Close-Commands

Watchdog: start_delay=175
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from core.database import get_db_connection
from core.logging_setup import setup_logging
from core.market_utils import check_cooldown, get_max_leverage, send_telegram, update_cooldown
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels
from core import config as _kcfg  # channel ids

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
LOOP_INTERVAL_MS = 500
AUTO_CLOSE_ON_REGIME_CHANGE = True
NEW_SIGNAL_DETECTION_WINDOW_SEC = 60
ORCHESTRATOR_MODULE_NAME = "ROM1"
ORCHESTRATOR_COOLDOWN_HOURS = 4
FALLBACK_MAX_DISTINCT_REGIMES_2H = 3
FALLBACK_UNSTABLE_LOOKBACK_HOURS = 2
REFERENCE_WINDOW_DAYS = 30
MIN_TRADES_FOR_DECISION = 30
LIFECYCLE_SYNC_INTERVAL_SEC = 30
FALLBACK_MIN_WR = 50.0

# Outcome-Klassifikation im Lifecycle-Sync: siehe Erläuterung in
# 27_bot_regime_analyzer.py — gleiche Logik damit Win/Loss-Bestimmung konsistent ist.
OUTCOME_MIN_PNL_PCT = 0.1       # |pnl| <= 0.1% → neutral
OUTCOME_MAX_ABS_PNL_PCT = 100.0  # |pnl| > 100% → neutral (Daten-Bug)

BOT_IDENTIFICATION_PATTERNS = [
    r"\b(MIS1-\d+[Hh]_(?:pump|dump))\b",   # MIS1-8h_pump, most specific first
    r"\b(MIS1-\d+[Hh])\b",                  # MIS1-8H, MIS1-24H
    r"\b(MIS1|ATS1|RUB1|ATB1|AIM1|ABR1|EPD1|SRA1)\b",
    # Quasimodo (24_quasimodo_bot.py): f"QM_{tf.upper()}" → QM_1H, QM_4H
    # SMC-ML-Sniper (25_smc_ml_sniper.py): f"{BB|TD}_{tf.upper()}" → BB_1H, BB_4H, TD_1H, TD_4H
    # Pattern Detector (7_pattern_detector.py): f"BR{tf.upper()}" → BR1H, BR2H, BR4H, BR1D
    r"\b(QM_\d+[HhDd]|BB_\d+[HhDd]|TD_\d+[HhDd]|BR\d+[HhDd])\b",
    # Legacy-Fallback (alte QM/BB/TD Varianten, falls historische Outbox-entries
    # noch existieren) — die aktuellen Bots nutzen diese Tags nicht mehr.
    r"\b(QM_BULL|QM_BEAR|BB_BULL|BB_BEAR|TD_LONG|TD_SHORT)\b",
    r"🧠\s*([A-Za-z0-9 ]+?)\s+Strategy",
]

CHANNEL_TO_BOT_FALLBACK: dict[int, str] = {
    _kcfg.CH_FAST_IN_OUT: "Fast In And Out",
    _kcfg.CH_5_PERCENT: "5 Percent",
    _kcfg.CH_SUPPORT_RESISTANCE: "Support Resistance",
    _kcfg.CH_VOLUME_INDICATOR: "Volume Indicator",
    _kcfg.CH_PATTERN_DETECTOR: "Pattern Detector",
}

# ─────────────────────────────────────────────────────────────────────────────
logger = setup_logging("SIGNAL_ORCHESTRATOR")

# Module-level state
_last_known_regime: str | None = None
_last_known_alt_context: str | None = None
_last_seen_outbox_id: int = 0
_last_lifecycle_sync: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# PARSING
# ─────────────────────────────────────────────────────────────────────────────

def parse_cornix_signal(msg: str) -> dict | None:
    """
    Parses a Cornix-format signal message.

    Returns dict or None if format doesn't match:
        {
            'coin': str, 'direction': 'LONG'|'SHORT',
            'entry': float, 'sl': float,
            'targets': [float, ...],
            'strategy_footer': str | None,
        }

    Rejects HTML messages (chart captions contain <pre>/<b> tags).
    """
    if not msg:
        return None

    # Must be plain-text Cornix, not HTML chart caption
    if "<pre>" in msg or "<b>" in msg or "<i>" in msg:
        return None

    # Must contain the key markers
    if "📈 Signal for" not in msg:
        return None
    if "Direction:" not in msg or "Stop Loss:" not in msg:
        return None

    coin_m = re.search(r"Signal for\s+(\S+)", msg)
    dir_m = re.search(r"Direction:\s*(LONG|SHORT)", msg)
    entry_m = re.search(r"CMP Entry:\s*\$\s*([0-9.]+)", msg)
    sl_m = re.search(r"Stop Loss:\s*\$\s*([0-9.]+)", msg)
    targets = [float(m) for m in re.findall(r"TP\d+:\s*\$\s*([0-9.]+)", msg)]
    footer_m = re.search(r"🧠\s*(.+?)(?:\s+Strategy)?(?:\s*-?\s*V\d+)?\s*$", msg, re.M)

    if not (coin_m and dir_m and entry_m and sl_m):
        return None

    return {
        "coin": coin_m.group(1).strip(),
        "direction": dir_m.group(1).strip(),
        "entry": float(entry_m.group(1)),
        "sl": float(sl_m.group(1)),
        "targets": targets,
        "strategy_footer": footer_m.group(1).strip() if footer_m else None,
    }


def identify_bot(msg: str, channel_id: int | None) -> str | None:
    """
    Extracts the bot name from signal message text.
    Falls back to channel-ID mapping.
    Returns None if unidentifiable.
    """
    for pattern in BOT_IDENTIFICATION_PATTERNS:
        m = re.search(pattern, msg, re.IGNORECASE)
        if m:
            return m.group(1).strip()

    if channel_id is not None:
        return CHANNEL_TO_BOT_FALLBACK.get(channel_id)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# DETEKTOR-ZUVERLÄSSIGKEIT
# ─────────────────────────────────────────────────────────────────────────────

def is_regime_detector_reliable(conn) -> tuple[bool, str]:
    """
    Returns (True, 'reliable') if the regime detector is trustworthy,
    or (False, reason) for fallback mode.

    Fallback triggered by:
      - 'no_regime': regime_current is empty
      - 'regime_is_transition': current regime is TRANSITION
      - 'regime_unstable': ≥3 distinct regimes in last 2h
    """
    with conn.cursor() as cur:
        cur.execute("SELECT regime FROM regime_current WHERE id = 1")
        row = cur.fetchone()

    if row is None:
        return (False, "no_regime")

    regime = row[0]
    if regime == "TRANSITION":
        return (False, "regime_is_transition")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(DISTINCT regime) FROM regime_history
            WHERE ts >= NOW() AT TIME ZONE 'UTC'
                  - INTERVAL '%s hours'
            """,
            (FALLBACK_UNSTABLE_LOOKBACK_HOURS,),
        )
        distinct = cur.fetchone()[0]

    if distinct >= FALLBACK_MAX_DISTINCT_REGIMES_2H:
        return (False, "regime_unstable")

    return (True, "reliable")


def is_whitelisted_fallback(conn, bot_name: str, direction: str) -> tuple[bool, str]:
    """
    Fallback whitelist without regime filter.
    Uses overall (ALL × ALL) performance vs fixed 50% threshold.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT n_trades, win_rate FROM bot_regime_performance
            WHERE bot_name = %s AND regime = 'ALL' AND alt_context = 'ALL'
              AND direction = %s AND window_days = %s
            """,
            (bot_name, direction, REFERENCE_WINDOW_DAYS),
        )
        row = cur.fetchone()

    if row is None or (row[0] or 0) < MIN_TRADES_FOR_DECISION:
        return (True, "fallback_insufficient_data")

    wr = row[1] or 0.0
    if wr >= FALLBACK_MIN_WR:
        return (True, "fallback_wr_above_50")
    return (False, "fallback_wr_below_50")


def get_whitelist_decision(
    conn, bot_name: str, direction: str
) -> tuple[bool, str]:
    """
    Main whitelist entry point. Chooses between:
      - Normal 4D-lookup (reliable detector)
      - Overall-fallback (unreliable detector)
    """
    reliable, status = is_regime_detector_reliable(conn)

    if reliable:
        with conn.cursor() as cur:
            cur.execute("SELECT regime, alt_context FROM regime_current WHERE id = 1")
            regime_row = cur.fetchone()

        if regime_row is None:
            return (True, "no_whitelist_entry")

        regime, alt_context = regime_row

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT whitelisted, reason FROM bot_regime_whitelist
                WHERE bot_name = %s AND regime = %s
                  AND alt_context = %s AND direction = %s
                """,
                (bot_name, regime, alt_context, direction),
            )
            wl_row = cur.fetchone()

        if wl_row is None:
            return (True, "no_whitelist_entry")

        return (bool(wl_row[0]), wl_row[1] or "unknown")

    # Fallback path
    logger.info(
        f"⚠️ Regime detector unreliable ({status}) — "
        f"Overall fallback for {bot_name} {direction}"
    )
    whitelisted, fallback_reason = is_whitelisted_fallback(conn, bot_name, direction)
    return (whitelisted, f"{status}:{fallback_reason}")


def get_current_regime_full(conn) -> tuple[str, str] | None:
    """Returns (regime, alt_context) from regime_current, or None."""
    with conn.cursor() as cur:
        cur.execute("SELECT regime, alt_context FROM regime_current WHERE id = 1")
        row = cur.fetchone()
    return (row[0], row[1]) if row else None


# ─────────────────────────────────────────────────────────────────────────────
# OPPOSITE-DIRECTION CHECK
# ─────────────────────────────────────────────────────────────────────────────

def is_opposite_direction_open(conn, coin: str, new_direction: str) -> bool:
    """True if an open orchestrator trade exists for this coin in opposite direction."""
    opposite = "SHORT" if new_direction == "LONG" else "LONG"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM orchestrator_open_trades
            WHERE coin = %s AND direction = %s AND status = 'OPEN'
            LIMIT 1
            """,
            (coin, opposite),
        )
        return cur.fetchone() is not None


# ─────────────────────────────────────────────────────────────────────────────
# ROM1-TRADE-PARAMS (eigene Entry/SL/Target-Berechnung)
# ─────────────────────────────────────────────────────────────────────────────
# Der Orchestrator übernimmt nicht mehr die Werte des originalen Bot-Signals,
# sondern berechnet Entry/SL/Targets selbst — mit derselben Logik wie die
# AI-Bots (ATS1, ATB1, RUB1 etc.). Das entkoppelt ROM1 von den Original-Bots
# und macht die Trades zu echten eigenen ROM1-Trades.
#
# Die Logik ist direkt aus 12_ai_ats_bot.py (Zeile 242-257) übernommen und
# nutzt get_hvn_and_sr_levels() + ensure_min_tp_distance() aus core/trade_utils.
# Siehe auch 14_ai_atb_bot.py, 13_ai_rub_bot.py — alle nutzen dasselbe Muster.

ROM1_DESIRED_LEVERAGE = 20              # Gleicher Standard wie die AI-Bots
ROM1_ENTRY2_OFFSET_PCT = 0.05           # 2. Entry 5% entfernt (AI-Bot-Standard)
ROM1_SL_FALLBACK_OFFSET_PCT = 0.025     # Fallback-SL wenn keine echte Zone verfügbar
ROM1_TP_MIN_DISTANCE_PCT = 0.05         # Mindestabstand letztes TP zum Entry


def _get_latest_price(conn, coin: str) -> float | None:
    """Holt den letzten Close-Preis aus der 5m-Tabelle des Coins.

    Gibt None zurück falls no data verfügbar sind (z.B. neu gelistetes Symbol).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT close FROM "{coin}_5m" ORDER BY open_time DESC LIMIT 1'
            )
            row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])
    except Exception as e:
        logger.warning(f"Konnte Preis für {coin} nicht laden: {e}")
        return None


def compute_rom1_trade_params(conn, coin: str, direction: str) -> dict | None:
    """Berechnet Entry/SL/Targets für einen ROM1-Trade — unabhängig vom
    ursprünglichen Bot-Signal.

    Nutzt dieselbe Logik wie die AI-Bots (siehe 12_ai_ats_bot.py):
      - Entry1 = aktueller Marktpreis (letzter 5m-Close)
      - Entry2 = 5% vom Entry1 entfernt (weg vom Trade für Pullback-Entry)
      - SL = nächstliegende S/R-Zone außerhalb Entry2, sonst Entry2 × (1 ∓ 2.5%)
      - Targets = echte S/R-Zonen jenseits Entry1, gecappt durch ensure_min_tp_distance

    Returns None falls Preis not available ist oder Zonen-Lookup versagt.
    Sonst dict mit:
      entry1, entry2, sl, targets (list), leverage (str, z.B. "20x")
    """
    current_price = _get_latest_price(conn, coin)
    if current_price is None or current_price <= 0:
        logger.warning(f"ROM1: Kein Preis für {coin}, skipping Trade-Berechnung")
        return None

    is_long = (direction == "LONG")
    entry1 = current_price

    if is_long:
        entry2 = entry1 * (1 - ROM1_ENTRY2_OFFSET_PCT)
    else:
        entry2 = entry1 * (1 + ROM1_ENTRY2_OFFSET_PCT)

    try:
        supps, resis = get_hvn_and_sr_levels(conn, coin, current_price)
    except Exception as e:
        logger.warning(f"ROM1: S/R-Lookup für {coin} fehlgeschlagen: {e}")
        return None

    # SL- und Target-Kandidaten exakt after ATS1-Muster
    if is_long:
        below_entry2 = [x for x in supps if x < entry2 * 0.99]
        sl = max(below_entry2) if below_entry2 else entry2 * (1 - ROM1_SL_FALLBACK_OFFSET_PCT)
        t_cands = sorted([x for x in resis if x > (entry1 * 1.01)])
    else:
        above_entry2 = [x for x in resis if x > entry2 * 1.01]
        sl = min(above_entry2) if above_entry2 else entry2 * (1 + ROM1_SL_FALLBACK_OFFSET_PCT)
        t_cands = sorted(
            [x for x in supps if 0 < x < (entry1 * 0.99)],
            reverse=True,
        )

    # Bis zu 20 Targets (wie die AI-Bots), gecappt durch ensure_min_tp_distance
    targets = ensure_min_tp_distance(
        t_cands[:20], entry1, is_long, min_pct=ROM1_TP_MIN_DISTANCE_PCT
    )

    if not targets:
        logger.warning(f"ROM1: Keine validen Targets für {coin} {direction}")
        return None

    leverage = get_max_leverage(coin, ROM1_DESIRED_LEVERAGE)

    return {
        "entry1": float(entry1),
        "entry2": float(entry2),
        "sl": float(sl),
        "targets": [float(t) for t in targets],
        "leverage": leverage,
    }


def build_rom1_cornix_message(
    coin: str,
    direction: str,
    params: dict,
    trigger_bot: str | None = None,
) -> str:
    """Baut die exakte Cornix-Plaintext-Message aus ROM1-Parametern.

    Format exakt wie bei den AI-Bots (siehe 12_ai_ats_bot.py Zeile 261-265),
    damit Cornix die Message beim Parsen genauso versteht wie die Original-signals.

    Args:
        trigger_bot: Optional, Name des Bots der den Trade ausgelöst hat
            (z.B. "MIS1-8h"). Wird in einer separaten Zeile after dem Footer
            ergänzt damit die Info bei Downstream-Tools und Lesern erhalten
            bleibt. Cornix parsed das Feld nicht und ignoriert es.
    """
    lines = [
        f"📈 Signal for {coin} 📈",
        f"🚨 Direction: {direction}",
        f"🚨 Leverage: {params['leverage']}",
        f"🚨 Margin: Cross",
        f"🏦 CMP Entry: $ {params['entry1']:.8f}",
        f"🏦 Entry 2: $ {params['entry2']:.8f}",
    ]
    # Cornix nimmt nur die ersten 3 TPs wahr (Standard) — wir posten auch nur 3.
    # Die restlichen Targets (bis zu 20) stehen in ai_signals für den Monitor.
    for i, t in enumerate(params["targets"][:3], start=1):
        lines.append(f"💰 TP{i}: $ {t:.8f}")
    lines.append(f"💸 Stop Loss: $ {params['sl']:.8f}")
    lines.append(f"🧠 Trade idea generated by AI module ROM1 V1")
    if trigger_bot:
        # Trigger-Info als eigene Zeile — bleibt für Leser sichtbar und
        # wird auch in orchestrator_open_trades unter bot_name saved.
        lines.append(f"📡 Triggered by: {trigger_bot}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# ROM1-TRACKING
# ─────────────────────────────────────────────────────────────────────────────

def insert_rom1_signal(conn, coin: str, direction: str, params: dict) -> None:
    """Inserts a ROM1 entry into ai_signals for lifecycle tracking.

    Nimmt jetzt die ROM1-eigenen Trade-Params (aus compute_rom1_trade_params),
    nicht mehr die Werte des Original-Signals.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ai_signals
                (symbol, price, model, direction, confidence,
                 entry1, entry2, sl, targets)
            VALUES (%s, %s, 'ROM1', %s, 1.0, %s, %s, %s, %s)
            """,
            (
                coin,
                params["entry1"],
                direction,
                params["entry1"],
                params["entry2"],
                params["sl"],
                json.dumps(params["targets"]),
            ),
        )
    conn.commit()


def insert_orchestrator_open_trade(
    conn,
    coin: str,
    direction: str,
    bot_name: str,
    entry: float,
    outbox_id: int | None,
    regime: str | None,
    alt_context: str | None,
) -> None:
    """Records a forwarded trade in orchestrator_open_trades."""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orchestrator_open_trades
                (coin, direction, bot_name, entry_price, opened_at,
                 regime_at_open, alt_context_at_open,
                 original_outbox_id, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'OPEN')
            """,
            (coin, direction, bot_name, entry, now_utc,
             regime, alt_context, outbox_id),
        )
    conn.commit()


def log_suppressed(
    conn,
    bot_name: str | None,
    coin: str | None,
    direction: str | None,
    reason: str,
    outbox_id: int | None,
) -> None:
    """Logs a suppressed (not forwarded) signal."""
    regime_str = None
    state = get_current_regime_full(conn)
    if state:
        regime_str = f"{state[0]}/{state[1]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orchestrator_suppressed_signals
                (bot_name, coin, direction, regime_at_signal, reason, original_outbox_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (bot_name, coin, direction, regime_str, reason, outbox_id),
        )
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL-GATING
# ─────────────────────────────────────────────────────────────────────────────

async def signal_gating_pass(conn) -> None:
    """
    Scans telegram_outbox for new bot signals and forwards whitelisted ones
    to REGIME_TRADING_CHANNEL_ID.
    """
    global _last_seen_outbox_id

    from core.config import REGIME_TRADING_CHANNEL_ID

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - timedelta(seconds=NEW_SIGNAL_DETECTION_WINDOW_SEC)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, channel_id, message
            FROM telegram_outbox
            WHERE id > %s
              AND created_at >= %s
              AND sent = FALSE
              AND failed = FALSE
            ORDER BY id ASC
            LIMIT 100
            """,
            (_last_seen_outbox_id, cutoff),
        )
        rows = cur.fetchall()

    if not rows:
        return

    last_id = _last_seen_outbox_id
    for (outbox_id, channel_id, message) in rows:
        last_id = max(last_id, outbox_id)

        # ── Parse signal ──────────────────────────────────────────────────────
        parsed = parse_cornix_signal(message or "")
        if parsed is None:
            continue  # Not a Cornix signal

        coin = parsed["coin"]
        direction = parsed["direction"]

        # ── Identify bot ──────────────────────────────────────────────────────
        bot_name = identify_bot(message, channel_id)
        if bot_name is None:
            logger.warning(
                f"Bot nicht identifizierbar für Outbox #{outbox_id}: {message[:80]!r}"
            )
            log_suppressed(conn, None, coin, direction, "bot_unidentified", outbox_id)
            continue

        # ── Whitelist check ───────────────────────────────────────────────────
        whitelisted, wl_reason = get_whitelist_decision(conn, bot_name, direction)
        if not whitelisted:
            logger.info(
                f"⛔ Signal filtered: {bot_name} {coin} {direction} "
                f"(reason: {wl_reason})"
            )
            log_suppressed(
                conn, bot_name, coin, direction, f"bot_not_whitelisted:{wl_reason}", outbox_id
            )
            continue

        # ── Cooldown check ────────────────────────────────────────────────────
        if check_cooldown(conn, ORCHESTRATOR_MODULE_NAME, coin, direction,
                          ORCHESTRATOR_COOLDOWN_HOURS):
            log_suppressed(
                conn, bot_name, coin, direction, "orchestrator_cooldown", outbox_id
            )
            continue

        # ── Cross-direction check ─────────────────────────────────────────────
        if is_opposite_direction_open(conn, coin, direction):
            log_suppressed(
                conn, bot_name, coin, direction, "opposite_direction_open", outbox_id
            )
            continue

        # ── ALL CHECKS PASSED → ROM1 BERECHNET EIGENEN TRADE ────────────────
        # Der Orchestrator übernimmt NICHT mehr die Werte des Original-Signals.
        # Stattdessen nutzt er dieselbe Logik wie die AI-Bots und berechnet
        # Entry/SL/Targets selbst — basierend auf aktuellem Preis und echten
        # S/R-Zonen aus get_hvn_and_sr_levels().
        rom1_params = compute_rom1_trade_params(conn, coin, direction)
        if rom1_params is None:
            # Preis not available oder S/R-Lookup fehlgeschlagen → nicht posten
            log_suppressed(
                conn, bot_name, coin, direction, "rom1_params_unavailable", outbox_id
            )
            continue

        state = get_current_regime_full(conn)
        cur_regime = state[0] if state else None
        cur_alt = state[1] if state else None

        # ROM1 baut seine eigene Cornix-Message aus den berechneten Params.
        # trigger_bot zeigt im Signal-Text welcher Original-Bot den Trade
        # ausgelöst hat — Info bleibt für Leser und Downstream-Analytics erhalten.
        rom1_message = build_rom1_cornix_message(
            coin, direction, rom1_params, trigger_bot=bot_name
        )

        # Post ins Trading-Channel
        send_telegram(rom1_message, REGIME_TRADING_CHANNEL_ID)

        # ROM1-Tracking in ai_signals (wird vom 8_ai_trade_monitor aufgegriffen)
        insert_rom1_signal(conn, coin, direction, rom1_params)

        # Orchestrator open trade — nutzt jetzt auch den ROM1-Entry, nicht mehr den
        # originalen Entry aus dem parsed Signal
        insert_orchestrator_open_trade(
            conn, coin, direction, bot_name,
            rom1_params["entry1"], outbox_id, cur_regime, cur_alt,
        )

        # Update cooldown
        update_cooldown(conn, ORCHESTRATOR_MODULE_NAME, coin, direction)

        logger.info(
            f"✅ ROM1-Trade gepostet: {coin} {direction} @ {rom1_params['entry1']:.6f} "
            f"(SL {rom1_params['sl']:.6f}, {len(rom1_params['targets'])} TPs) "
            f"[Trigger: {bot_name}, Regime: {cur_regime}/{cur_alt}]"
        )

    _last_seen_outbox_id = last_id


# ─────────────────────────────────────────────────────────────────────────────
# LIFECYCLE SYNC
# ─────────────────────────────────────────────────────────────────────────────

def mark_orchestrator_trade_closed(
    conn, trade_id: int, status: str, reason: str
) -> None:
    """Updates an orchestrator_open_trades row to closed."""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE orchestrator_open_trades
            SET status = %s, closed_at = %s, close_reason = %s
            WHERE id = %s
            """,
            (status, now_utc, reason, trade_id),
        )
    conn.commit()


def _get_last_close_price(conn, coin: str, fallback: float | None = None) -> float | None:
    """Holt den letzten 5m-Close-Preis des Coins, mit Entry-Fallback.

    Wird beim Regime-Wechsel-Close genutzt um einen sinnvollen Close-Preis
    zu haben, auch wenn der Trade gerade nicht an einem SL/TP-Level steht.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT sp_get_close")
            try:
                cur.execute(
                    f'SELECT close FROM "{coin}_5m" '
                    f'ORDER BY open_time DESC LIMIT 1'
                )
                row = cur.fetchone()
                cur.execute("RELEASE SAVEPOINT sp_get_close")
                if row and row[0] is not None:
                    return float(row[0])
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_get_close")
    except Exception:
        pass
    return fallback


def force_close_trades_for_regime_change(conn, coin: str, direction: str) -> dict:
    """Schließt alle offenen Trades für coin+direction wegen Regime-Wechsel.

    Findet offene entries in ai_signals und active_trades_master für das
    angegebene coin+direction und verschiebt sie in die closed_*-Tabellen
    mit status-Marker "CLOSED_REGIME_CHANGE". Wird vom Regime-Wechsel-
    Handler aufgerufen afterdem der Close-Command an Cornix gegangen ist.

    Warum nicht erst den Monitor warten lassen?
    - Cornix schließt die Binance-Position JETZT
    - Der Monitor würde den Trade erst schließen wenn sein eigener
      SL/TP erreicht wird — das kann Stunden/Tage dauern
    - In der Zwischenzeit würde der Trade als "still open" in der
      Statistik erscheinen und die WR verzerren

    Classification: Der "CLOSED_REGIME_CHANGE"-Marker wird von Market-
    Tracker, Analyzer und Orchestrator-Outcome-Classifier als NEUTRAL
    behandelt (nicht als Win/Loss) — der Trade wurde aus externen
    Gründen geschlossen, nicht wegen des Bot-Signals.

    Returns:
        dict mit 'ai_closed' und 'classic_closed' (Anzahl der geschlossenen)
    """
    result = {"ai_closed": 0, "classic_closed": 0}
    now = datetime.now(timezone.utc)

    # ── AI-Trades (ai_signals) ─────────────────────────────────────────────
    # Alle offenen AI-Trades auf diesem Coin+Direction finden — auch die
    # ROM1-Kopie des Orchestrators selbst.
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, symbol, model, direction, entry1, price,
                       current_target_hit, open_time
                FROM ai_signals
                WHERE symbol = %s AND direction = %s
                """,
                (coin, direction),
            )
            ai_rows = cur.fetchall()
    except Exception as e:
        logger.warning(f"Regime-Close: ai_signals-Query fehlgeschlagen ({coin}): {e}")
        conn.rollback()
        ai_rows = []

    for row in ai_rows:
        tid, symbol, model, trade_dir, entry1, price, targets_hit, open_time = row
        entry = float(entry1) if entry1 is not None else (
            float(price) if price is not None else 0.0
        )
        close_price = _get_last_close_price(conn, coin, fallback=entry)
        if close_price is None:
            close_price = entry  # letzter Fallback

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO closed_ai_signals (
                        symbol, model, direction, entry, close_price,
                        targets_hit, open_time, close_time, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        symbol, model, trade_dir, entry, close_price,
                        int(targets_hit or 0), open_time, now,
                        "CLOSED_REGIME_CHANGE",
                    ),
                )
                cur.execute("DELETE FROM ai_signals WHERE id = %s", (tid,))
            conn.commit()
            result["ai_closed"] += 1
        except Exception as e:
            logger.warning(
                f"Regime-Close: AI-Trade {tid} ({symbol} {model}) "
                f"konnte nicht geschlossen werden: {e}"
            )
            conn.rollback()

    # ── Klassische Trades (active_trades_master) ───────────────────────────
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, strategy, time, coin, direction, lev, entry,
                       target1, target2, target3, target4, sl
                FROM active_trades_master
                WHERE coin = %s AND direction = %s
                """,
                (coin, direction),
            )
            classic_cols = [d[0] for d in cur.description]
            classic_rows = [dict(zip(classic_cols, r)) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(
            f"Regime-Close: active_trades_master-Query fehlgeschlagen ({coin}): {e}"
        )
        conn.rollback()
        classic_rows = []

    for trade in classic_rows:
        entry = float(trade['entry']) if trade['entry'] else 0.0
        close_price = _get_last_close_price(conn, coin, fallback=entry)
        if close_price is None:
            close_price = entry

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO closed_trades_master (
                        strategy, time, coin, direction, lev, entry,
                        target1, target2, target3, target4, sl,
                        close_price, posted, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        trade['strategy'], trade['time'], trade['coin'],
                        trade['direction'], trade['lev'], entry,
                        trade['target1'], trade['target2'],
                        trade['target3'], trade['target4'], trade['sl'],
                        close_price, now, "CLOSED_REGIME_CHANGE",
                    ),
                )
                cur.execute(
                    "DELETE FROM active_trades_master WHERE id = %s",
                    (trade['id'],),
                )
            conn.commit()
            result["classic_closed"] += 1
        except Exception as e:
            logger.warning(
                f"Regime-Close: Classic-Trade {trade['id']} "
                f"({trade['coin']}) konnte nicht geschlossen werden: {e}"
            )
            conn.rollback()

    return result


def _classify_outcome_by_pnl(direction: str, entry: float | None,
                              close_price: float | None,
                              close_reason: str | None) -> str:
    """Klassifiziert einen geschlossenen Trade anhand des realen PnL.

    Spiegelt die Logik aus 27_bot_regime_analyzer.py wider — gleiche
    Konstanten, gleiche Semantik. Nur die Return-Werte sind anders, weil
    der Orchestrator andere Status-Strings schreibt.

    Returns einen der Status-Strings:
      'CLOSED_TP'      → Trade war profitabel (echter Win)
      'CLOSED_SL'      → Trade war negativ (echter Loss)
      'CLOSED_NEUTRAL' → Delisting, Housekeeping, Ausreißer oder Micro-PnL
                         (≠ Win und ≠ Loss, soll nicht in Perf-Stats zählen)
    """
    reason = (close_reason or "").upper()
    # Neutral-Marker: extern verursachte Closes die nicht vom Bot-Signal kommen
    # → nicht in Win/Loss-Statistik zählen
    if ("DELISTED" in reason or "CLEANUP" in reason or "ORPHAN" in reason
            or "REGIME_CHANGE" in reason):
        return "CLOSED_NEUTRAL"
    if entry is None or close_price is None:
        return "CLOSED_NEUTRAL"
    try:
        e = float(entry)
        c = float(close_price)
    except (TypeError, ValueError):
        return "CLOSED_NEUTRAL"
    if e <= 0:
        return "CLOSED_NEUTRAL"
    raw = (c - e) / e * 100.0
    pnl = raw if direction == "LONG" else -raw
    if abs(pnl) > OUTCOME_MAX_ABS_PNL_PCT:
        return "CLOSED_NEUTRAL"
    if abs(pnl) <= OUTCOME_MIN_PNL_PCT:
        return "CLOSED_NEUTRAL"
    return "CLOSED_TP" if pnl > 0 else "CLOSED_SL"


async def sync_closed_trades(conn) -> None:
    """
    Checks if any open orchestrator trades have been closed in the master tables.
    Updates orchestrator_open_trades accordingly.
    Runs every LIFECYCLE_SYNC_INTERVAL_SEC seconds.

    Klassifiziert das Outcome PnL-basiert (statt targets_hit/status-basiert),
    um die bekannten Bugs im 8_ai_trade_monitor zu umgehen (LEGACY TARGET HIT
    schreibt targets_hit=0 etc.). DELISTED/CLEANUP-Trades bekommen den Status
    CLOSED_NEUTRAL — die Orchestrator-Performance wird dadurch nicht verzerrt.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, coin, direction, bot_name, opened_at
            FROM orchestrator_open_trades
            WHERE status = 'OPEN'
            """
        )
        open_trades = cur.fetchall()

    for (trade_id, coin, direction, bot_name, opened_at) in open_trades:
        opened_at_naive = opened_at if not (hasattr(opened_at, "tzinfo") and opened_at.tzinfo) \
            else opened_at.replace(tzinfo=None)
        window_start = opened_at_naive - timedelta(seconds=60)
        window_end = opened_at_naive + timedelta(hours=720)  # 30 days max

        # Check closed_trades_master
        # status ist ein String "0"..."4" (0=SL, 1..4=TP-Level)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entry, close_price, status FROM closed_trades_master
                WHERE coin = %s AND direction = %s
                  AND time >= %s AND time <= %s
                LIMIT 1
                """,
                (coin, direction, window_start, window_end),
            )
            row = cur.fetchone()

        if row:
            entry, close_price, _status = row
            # Klassische Trades haben keinen close_reason — nur PnL zählt.
            new_status = _classify_outcome_by_pnl(
                direction, entry, close_price, close_reason=None
            )
            mark_orchestrator_trade_closed(conn, trade_id, new_status, "lifecycle_sync")
            continue

        # Check closed_ai_signals
        # `status` in closed_ai_signals enthält tatsächlich den close_reason
        # (z.B. "LEGACY TARGET HIT (+2.5%)", "DELISTED / CLEANUP").
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entry, close_price, status FROM closed_ai_signals
                WHERE symbol = %s AND direction = %s
                  AND open_time >= %s AND open_time <= %s
                LIMIT 1
                """,
                (coin, direction, window_start, window_end),
            )
            row = cur.fetchone()

        if row:
            entry, close_price, close_reason = row
            new_status = _classify_outcome_by_pnl(
                direction, entry, close_price, close_reason
            )
            mark_orchestrator_trade_closed(conn, trade_id, new_status, "lifecycle_sync")


# ─────────────────────────────────────────────────────────────────────────────
# REGIME-CHANGE → CLOSE-COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def check_regime_change_and_close(conn) -> None:
    """
    Polls regime_current for changes. If regime or alt_context changed:
      - Checks all open orchestrator trades against new 4D whitelist
      - Posts Close-Commands for non-whitelisted trades
      - Posts summary to REGIME_STATUS_CHANNEL_ID
    """
    global _last_known_regime, _last_known_alt_context

    state = get_current_regime_full(conn)
    if state is None:
        return

    new_regime, new_alt_context = state

    # Init on first run
    if _last_known_regime is None:
        _last_known_regime = new_regime
        _last_known_alt_context = new_alt_context
        return

    btc_changed = new_regime != _last_known_regime
    alt_changed = new_alt_context != _last_known_alt_context

    if not (btc_changed or alt_changed):
        return

    old_regime = _last_known_regime
    old_alt = _last_known_alt_context
    _last_known_regime = new_regime
    _last_known_alt_context = new_alt_context

    changes = []
    if btc_changed:
        changes.append(f"BTC-Regime {old_regime} → {new_regime}")
    if alt_changed:
        changes.append(f"Alt-Context {old_alt} → {new_alt_context}")
    logger.info(f"🔄 Orchestrator detected change: {', '.join(changes)}")

    if not AUTO_CLOSE_ON_REGIME_CHANGE:
        return

    from core.config import REGIME_STATUS_CHANNEL_ID, REGIME_TRADING_CHANNEL_ID

    # Load all open trades
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, coin, direction, bot_name "
            "FROM orchestrator_open_trades WHERE status = 'OPEN'"
        )
        open_trades = cur.fetchall()

    closes = []
    keeps = []

    for (trade_id, coin, direction, bot_name) in open_trades:
        whitelisted, reason = get_whitelist_decision(conn, bot_name, direction)
        if not whitelisted:
            closes.append((trade_id, coin, direction, bot_name, reason))
        else:
            keeps.append((coin, direction, bot_name))

    # Post Close-Commands to trading channel and move trades to closed tables
    total_ai = 0
    total_classic = 0
    for (trade_id, coin, direction, bot_name, reason) in closes:
        close_cmd = f"Close {coin}"
        send_telegram(close_cmd, REGIME_TRADING_CHANNEL_ID)

        # Mark orchestrator_open_trades row as closed
        mark_orchestrator_trade_closed(
            conn, trade_id, "CLOSED_REGIME_CHANGE",
            f"REGIME_CHANGE:{reason}",
        )

        # Fix: Verschiebe auch die Original-Trades aus ai_signals /
        # active_trades_master in ihre closed_*-Tabellen, damit sie nicht
        # als "still open" in den Market-Tracker-Reports erscheinen bis
        # der Monitor irgendwann ihren eigenen SL/TP trifft (kann Tage
        # dauern). Der Marker "CLOSED_REGIME_CHANGE" wird downstream als
        # neutraler Close klassifiziert (nicht Win/nicht Loss) — der Trade
        # wurde aus externen Gründen geschlossen, nicht weil das Bot-Signal
        # falsch war.
        close_stats = force_close_trades_for_regime_change(conn, coin, direction)
        total_ai += close_stats["ai_closed"]
        total_classic += close_stats["classic_closed"]

        logger.info(
            f"🛑 Close command posted: {coin} {direction} "
            f"(bot={bot_name}, reason={reason}) — "
            f"DB: {close_stats['ai_closed']} AI + "
            f"{close_stats['classic_closed']} classic force-closed"
        )

    # Summary to status channel
    change_line = "\n".join(changes)
    close_lines = "\n".join(
        f"  {c[1]} {c[2]} ({c[3]}) — closed 🛑" for c in closes
    )
    keep_lines = "\n".join(
        f"  {k[0]} {k[1]} ({k[2]}) — kept open (whitelisted)" for k in keeps
    )
    summary = (
        f"🔄 REGIME CHANGE & AUTO-CLOSE\n\n"
        f"{change_line}\n\n"
        f"Open trades before change: {len(open_trades)}\n"
    )
    if closes:
        summary += close_lines + "\n"
    if keeps:
        summary += keep_lines + "\n"
    summary += f"\n{len(closes)} close command(s) posted to trading channel."
    if total_ai or total_classic:
        summary += (
            f"\n{total_ai} AI + {total_classic} classic trade(s) moved to "
            f"closed tables with status CLOSED_REGIME_CHANGE."
        )

    try:
        send_telegram(summary, REGIME_STATUS_CHANNEL_ID)
    except Exception as e:
        logger.error(f"Error sending des Regime-Change-Summary: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# HAUPT-LOOP
# ─────────────────────────────────────────────────────────────────────────────

def should_run_lifecycle_sync() -> bool:
    global _last_lifecycle_sync
    now = time.monotonic()
    if now - _last_lifecycle_sync >= LIFECYCLE_SYNC_INTERVAL_SEC:
        _last_lifecycle_sync = now
        return True
    return False


async def main_loop() -> None:
    logger.info("=== 🎯 SIGNAL ORCHESTRATOR STARTED ===")
    while True:
        conn = None
        try:
            conn = get_db_connection()
            await check_regime_change_and_close(conn)
            await signal_gating_pass(conn)
            if should_run_lifecycle_sync():
                await sync_closed_trades(conn)
        except Exception as e:
            logger.error(f"Orchestrator-Loop-Error: {e}", exc_info=True)
        finally:
            if conn:
                conn.close()
        await asyncio.sleep(LOOP_INTERVAL_MS / 1000.0)


if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("Signal Orchestrator manuell stopped (Strg+C).")
