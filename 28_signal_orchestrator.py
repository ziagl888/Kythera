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
import os
import re
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from core import config as _kcfg  # channel ids
from core.bot_naming import pretty_name
from core.database import get_db_connection
from core.logging_setup import setup_logging
from core.market_utils import check_cooldown, get_max_leverage, send_telegram, update_cooldown
from core.time import LEGACY_WRITER_TZ, utc_now_naive
from core.trade_utils import cap_leverage_to_sl, ensure_min_tp_distance, get_hvn_and_sr_levels

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
LOOP_INTERVAL_MS = 500
AUTO_CLOSE_ON_REGIME_CHANGE = True
# T-2026-CU-9050-049: winner-differentiated regime auto-close (A/B). Default OFF.
# When True, an open trade that is IN PROFIT at a regime change is NOT market-
# closed — its stop-loss is trailed (break-even, or the last reached TP level)
# via a Cornix SL-update command and the trade keeps running; losers are still
# closed. This changes live money-path behavior and starts an A/B experiment
# (orchestrator_open_trades.regime_close_action REGIME_CHANGE_CLOSED vs
# REGIME_CHANGE_TRAILED). Flipping it True is an operator decision
# (OPUS-HANDOFF §6; Kythera doctrine "Default-off für Unbewiesenes").
TRAIL_WINNERS_ON_REGIME_CHANGE = os.getenv("KYTHERA_REGIME_TRAIL_WINNERS", "0") == "1"
# Minimum profit (direction-aware price move %) for a trade to count as a
# "winner" worth protecting rather than closing. Mirrors the round-trip taker
# fee floor (OUTCOME_MIN_PNL_PCT / 27_bot_regime_analyzer.V2_BREAK_EVEN_PNL_PCT):
# below it a break-even SL would not lock a real non-loss, so we close instead.
TRAIL_MIN_PROFIT_PCT = 0.1
# FIX P2.28: 60s → 300s. Neuheit garantiert der id-Cursor (inkl. MAX(id)-Init
# beim Start) — das Fenster ist nur noch die Staleness-Grenze. Bei 60s fielen
# Signale schon raus, wenn ein einzelner Gating-Pass (S/R-Berechnung über
# mehrere Rows) länger als eine Minute hing.
NEW_SIGNAL_DETECTION_WINDOW_SEC = 300
ORCHESTRATOR_MODULE_NAME = "ROM1"
# Footer-Marker der eigenen ROM1-Messages — Single Source für den Message-
# Builder UND die Self-Echo-Barriere, damit die beiden nie auseinanderdriften.
ROM1_SIGNATURE = "AI module ROM1"
ORCHESTRATOR_COOLDOWN_HOURS = 4
FALLBACK_MAX_DISTINCT_REGIMES_2H = 3
FALLBACK_UNSTABLE_LOOKBACK_HOURS = 2
REFERENCE_WINDOW_DAYS = 30
MIN_TRADES_FOR_DECISION = 30
# Max age of a bot_regime_whitelist cell before the 4D lookup is distrusted
# (P0.4/P2.25): 27_bot_regime_analyzer recomputes the whitelist daily, so a
# cell older than two analyzer cycles means the analyzer is dead or writes a
# key the orchestrator never reads. Gating on such a cell decides today's
# money on stats frozen months ago — fall back to the overall window instead.
WHITELIST_MAX_AGE_HOURS = 48
LIFECYCLE_SYNC_INTERVAL_SEC = 30
# Half-width of the row anchor used by the lifecycle sync, the corpse reaper's
# twin check and its anti-censor guard. ONE constant on purpose: if the match
# loop and the reaper ever disagree on the window, the reaper can censor an
# outcome the match loop would still have classified.
LIFECYCLE_SYNC_WINDOW_SEC = 60
# Historical session TZ under which the pre-T-052 DB default stamped
# ai_signals.open_time (canonical constant lives in core/time.py: pinned, NOT
# current_setting('TimeZone') — a future R3 session-TZ flip must not un-shield
# live legacy positions; AT TIME ZONE handles their DST per-timestamp). Only
# used for the legacy second window; new rows carry naive UTC and match the
# first window. Collision-free because the 4h per-coin+direction cooldown makes
# two same-direction trades 3h±window apart impossible (pinned by a test).
LEGACY_SESSION_TZ = LEGACY_WRITER_TZ


def _anchor_window_predicate(col: str, anchor: str) -> str:
    """SQL predicate: `col` lies within ±LIFECYCLE_SYNC_WINDOW_SEC of `anchor`,
    either directly (naive-UTC rows) or after converting a legacy
    session-local timestamp to UTC.

    ONE builder for the match loop, the reaper's twin check and its
    anti-censor guard — the three must never disagree on the window shape, or
    the reaper censors outcomes the match loop would still classify (the
    round-3 asymmetry class). Interpolations are compile-time constants only.
    """
    w = f"INTERVAL '{LIFECYCLE_SYNC_WINDOW_SEC} seconds'"
    return (
        f"({col} BETWEEN {anchor} - {w} AND {anchor} + {w} "
        f"OR ({col} AT TIME ZONE '{LEGACY_SESSION_TZ}' AT TIME ZONE 'UTC') "
        f"BETWEEN {anchor} - {w} AND {anchor} + {w})"
    )


FALLBACK_MIN_WR = 50.0

# Outcome-Klassifikation im Lifecycle-Sync: siehe Erläuterung in
# 27_bot_regime_analyzer.py — gleiche Logik damit Win/Loss-Bestimmung konsistent ist.
OUTCOME_MIN_PNL_PCT = 0.1  # |pnl| <= 0.1% → neutral
OUTCOME_MAX_ABS_PNL_PCT = 100.0  # |pnl| > 100% → neutral (Daten-Bug)

BOT_IDENTIFICATION_PATTERNS = [
    # Versionierungs-Regel (Operator 2026-07-06): Retrain-Generationen posten
    # unter neuem Tag (MIS2, RUB2, BB2_4H, ...) — deshalb generationsoffene
    # Patterns (\d+ statt literal 1), analog get_category im Market-Tracker.
    # Ein Tag, das hier nicht matcht, wird als bot_unidentified HART
    # unterdrückt (T-2026-CU-9050-026: BB2_4H wäre nie beim Whitelist-Check
    # angekommen; gleiche Wurzel wie das offene RUB2-Attributions-Finding
    # aus PR #9).
    r"\b(MIS\d+-\d+[Hh]_(?:pump|dump))\b",  # MIS1-8h_pump, most specific first
    r"\b(MIS\d+-\d+[Hh])\b",  # MIS1-8H, MIS2-72H
    r"\b((?:MIS|ATS|RUB|ATB|AIM|ABR|EPD|SRA)\d+)\b",  # RUB2, ABR2, ...
    # Quasimodo (24_quasimodo_bot.py): f"QM_{tf.upper()}" → QM_1H, QM_4H
    # SMC-ML-Sniper (25_smc_ml_sniper.py): Artefakt-model_id → BB_1H, BB2_4H, TD2_4H
    # Pattern Detector (7_pattern_detector.py): f"BR{tf.upper()}" → BR1H, BR2H, BR4H, BR1D
    r"\b(QM\d*_\d+[HhDd]|BB\d*_\d+[HhDd]|TD\d*_\d+[HhDd]|BR\d+[HhDd])\b",
    # Legacy-Fallback (alte QM/BB/TD Varianten, falls historische Outbox-entries
    # noch existieren) — die aktuellen Bots nutzen diese Tags nicht mehr.
    r"\b(QM_BULL|QM_BEAR|BB_BULL|BB_BEAR|TD_LONG|TD_SHORT)\b",
    r"🧠\s*([A-Za-z0-9 ]+?)\s+Strategy",
]


def _build_channel_fallback(pairs: Iterable[tuple[int, str]]) -> dict[int, str]:
    """Maps channel id → bot name, dropping unset channels.

    core.config._ch() returns 0 for an unset channel id. Without the filter every
    unset channel collapses onto the key 0 and the last entry silently wins, so a
    lookup for a disabled bot would resolve to an unrelated bot name.
    """
    return {cid: name for cid, name in pairs if cid}


CHANNEL_TO_BOT_FALLBACK: dict[int, str] = _build_channel_fallback(
    (
        (_kcfg.CH_FAST_IN_OUT, "Fast In And Out"),
        (_kcfg.CH_5_PERCENT, "5 Percent"),
        (_kcfg.CH_SUPPORT_RESISTANCE, "Support Resistance"),
        (_kcfg.CH_VOLUME_INDICATOR, "Volume Indicator"),
        (_kcfg.CH_PATTERN_DETECTOR, "Pattern Detector"),
    )
)

# ─────────────────────────────────────────────────────────────────────────────
logger = setup_logging("SIGNAL_ORCHESTRATOR")

# Module-level state
_last_known_regime: str | None = None
_last_known_alt_context: str | None = None
_last_seen_outbox_id: int = 0
_outbox_cursor_initialized: bool = False
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


def get_whitelist_decision(conn, bot_name: str, direction: str) -> tuple[bool, str]:
    """
    Main whitelist entry point. Chooses between:
      - Normal 4D-lookup (reliable detector, fresh cell)
      - Overall-fallback (unreliable detector, or 4D cell older than
        WHITELIST_MAX_AGE_HOURS — P0.4/P2.25 staleness gate)

    computed_at is naive UTC (27_bot_regime_analyzer writes utc_now naive),
    so it compares directly against utc_now_naive().
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
                SELECT whitelisted, reason, computed_at FROM bot_regime_whitelist
                WHERE bot_name = %s AND regime = %s
                  AND alt_context = %s AND direction = %s
                """,
                (bot_name, regime, alt_context, direction),
            )
            wl_row = cur.fetchone()

        if wl_row is None:
            return (True, "no_whitelist_entry")

        computed_at = wl_row[2]
        stale = computed_at is None or (utc_now_naive() - computed_at) > timedelta(hours=WHITELIST_MAX_AGE_HOURS)
        if stale:
            age_str = (
                "unknown" if computed_at is None else f"{(utc_now_naive() - computed_at).total_seconds() / 3600:.0f}h"
            )
            logger.warning(
                f"⚠️ Stale whitelist cell ({regime}/{alt_context}, age {age_str}) — "
                f"Overall fallback for {bot_name} {direction}"
            )
            whitelisted, fallback_reason = is_whitelisted_fallback(conn, bot_name, direction)
            return (whitelisted, f"whitelist_stale:{fallback_reason}")

        return (bool(wl_row[0]), wl_row[1] or "unknown")

    # Fallback path
    logger.info(f"⚠️ Regime detector unreliable ({status}) — Overall fallback for {bot_name} {direction}")
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
    """True if an open orchestrator trade exists for this coin in opposite direction.

    Deliberately NO age bound here (T-2026-CU-9050-052): a genuinely live ROM1
    position can stay open well past 72h (expiry_hours is never set for ROM1),
    and dropping the block by age would let ROM1 post the opposite direction
    against a live position (flip/double exposure — the exact P1.8 risk).
    Corpse rows that would otherwise block forever are closed by the corpse
    reaper in sync_closed_trades instead: a row is only OPEN while its
    ai_signals twin still exists or until the reaper transitions it.
    """
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


def is_same_direction_open(conn, coin: str, direction: str) -> bool:
    """FIX P2.26: True wenn bereits ein OPEN ROM1-Trade auf coin+direction läuft.

    Ohne diesen Check stapelte ROM1 nach Ablauf des 4h-Cooldowns weitere
    Positionen auf denselben Coin in dieselbe Richtung (Doppel-Exposure).

    The former 72h age bound is gone (T-2026-CU-9050-052): it was meant to
    decay corpse rows, but it also un-blocked genuinely live positions older
    than 72h and re-enabled the stacking this check exists to prevent. Corpse
    decay is now the corpse reaper's job (sync_closed_trades): an OPEN row
    whose ai_signals twin is gone is transitioned out after 72h, so it can
    never block this coin+direction forever.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM orchestrator_open_trades
            WHERE coin = %s AND direction = %s AND status = 'OPEN'
            LIMIT 1
            """,
            (coin, direction),
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

ROM1_DESIRED_LEVERAGE = 20  # Gleicher Standard wie die AI-Bots
ROM1_ENTRY2_OFFSET_PCT = 0.05  # 2. Entry 5% entfernt (AI-Bot-Standard)
ROM1_SL_FALLBACK_OFFSET_PCT = 0.025  # Fallback-SL wenn keine echte Zone verfügbar
ROM1_TP_MIN_DISTANCE_PCT = 0.05  # Mindestabstand letztes TP zum Entry
ROM1_PUBLISHED_TARGETS = 3  # build_rom1_cornix_message postet TP1..TP3 (Cornix-Standard)


def _get_latest_price(conn, coin: str) -> float | None:
    """Holt den letzten Close-Preis aus der 5m-Tabelle des Coins.

    Gibt None zurück falls no data verfügbar sind (z.B. neu gelistetes Symbol).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT close FROM "{coin}_5m" ORDER BY open_time DESC LIMIT 1')
            row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])
    except Exception as e:
        logger.warning(f"Konnte Preis für {coin} nicht laden: {e}")
        return None


def compute_rom1_trade_params(conn, coin: str, direction: str, price=None, df=None) -> dict | None:
    """Berechnet Entry/SL/Targets für einen ROM1-Trade — unabhängig vom
    ursprünglichen Bot-Signal.

    Nutzt dieselbe Logik wie die AI-Bots (siehe 12_ai_ats_bot.py):
      - Entry1 = aktueller Marktpreis (letzter 5m-Close)
      - Entry2 = 5% vom Entry1 entfernt (weg vom Trade für Pullback-Entry)
      - SL = nächstliegende S/R-Zone außerhalb Entry2, sonst Entry2 × (1 ∓ 2.5%)
      - Targets = echte S/R-Zonen jenseits Entry1, gecappt durch ensure_min_tp_distance

    `price`/`df` (optional, beide zusammen): As-of-Muster wie
    `get_hvn_and_sr_levels(df=...)` und `calculate_smart_targets(df=...)` (P0.10).
    Werden sie übergeben, findet KEIN DB-Zugriff statt: `price` ersetzt den
    Live-CMP, `df` ist das chronologisch aufsteigende 1h-Fenster (~95 Tage,
    high/low/close) BIS zur Entscheidungskerze. Der ROM1-Counterfactual-Scorer
    (tools/rom1_counterfactual.py) spielt so exakt diese Geometrie auf
    historischen Fenstern ab — eine Quelle, kein Copy-Paste-Skew (X-R1).

    Returns None falls Preis not available ist oder Zonen-Lookup versagt.
    Sonst dict mit:
      entry1, entry2, sl, targets (list), leverage (str, z.B. "20x")
    """
    current_price = price if price is not None else _get_latest_price(conn, coin)
    if current_price is None or current_price <= 0:
        logger.warning(f"ROM1: Kein Preis für {coin}, skipping Trade-Berechnung")
        return None

    is_long = direction == "LONG"
    entry1 = float(current_price)

    if is_long:
        entry2 = entry1 * (1 - ROM1_ENTRY2_OFFSET_PCT)
    else:
        entry2 = entry1 * (1 + ROM1_ENTRY2_OFFSET_PCT)

    try:
        supps, resis = get_hvn_and_sr_levels(conn, coin, current_price, df=df)
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

    # FIX P2.27 (Audit, Step 2 belegt: SL-Distanz p90=17,9%, max 65%): Die nächste
    # S/R-Zone kann beliebig weit weg liegen — bei 20x jenseits der Liquidation.
    # Gleicher 15%-Cap wie calculate_smart_targets.
    ROM1_MAX_SL_DIST_PCT = 0.15
    if is_long:
        sl = max(sl, entry2 * (1 - ROM1_MAX_SL_DIST_PCT))
    else:
        sl = min(sl, entry2 * (1 + ROM1_MAX_SL_DIST_PCT))

    # Bis zu 20 Targets (wie die AI-Bots), gecappt durch ensure_min_tp_distance
    targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long, min_pct=ROM1_TP_MIN_DISTANCE_PCT)

    if not targets:
        logger.warning(f"ROM1: Keine validen Targets für {coin} {direction}")
        return None

    # R4 (Audit): Hebel gegen die SL-Distanz cappen — Liquidation darf nie vor
    # dem SL liegen. Konservativ gegen entry1 (CMP) gerechnet, dort ist die
    # SL-Distanz am größten.
    leverage = cap_leverage_to_sl(get_max_leverage(coin, ROM1_DESIRED_LEVERAGE), entry1, sl)

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
        "🚨 Margin: Cross",
        f"🏦 CMP Entry: $ {params['entry1']:.8f}",
        f"🏦 Entry 2: $ {params['entry2']:.8f}",
    ]
    # Cornix nimmt nur die ersten 3 TPs wahr (Standard) — wir posten auch nur 3.
    # Die restlichen Targets (bis zu 20) stehen in ai_signals für den Monitor.
    for i, t in enumerate(params["targets"][:ROM1_PUBLISHED_TARGETS], start=1):
        lines.append(f"💰 TP{i}: $ {t:.8f}")
    lines.append(f"💸 Stop Loss: $ {params['sl']:.8f}")
    lines.append(f"🧠 Trade idea generated by {ROM1_SIGNATURE} V1")
    if trigger_bot:
        # Trigger-Info als eigene Zeile — bleibt für Leser sichtbar und
        # wird auch in orchestrator_open_trades unter bot_name saved.
        lines.append(f"📡 Triggered by: {trigger_bot}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# ROM1-TRACKING
# ─────────────────────────────────────────────────────────────────────────────


def insert_rom1_signal(conn, coin: str, direction: str, params: dict, commit: bool = True) -> None:
    """Inserts a ROM1 entry into ai_signals for lifecycle tracking.

    Nimmt jetzt die ROM1-eigenen Trade-Params (aus compute_rom1_trade_params),
    nicht mehr die Werte des Original-Signals.

    commit=False lässt den Insert in der offenen Transaktion des Callers —
    signal_gating_pass committed Tracking + Outbox-Post atomar zusammen.

    P1.8 follow-up (T-2026-CU-9050-052): open_time is set explicitly as naive
    UTC. The DB default now() stamps session-local time (Europe/Bucharest)
    into the naive timestamp column — a constant +3h offset against the
    naive-UTC opened_at of the orchestrator_open_trades twin row, so the
    ±60s window in sync_closed_trades could never match (sync silently dead
    since 2026-07-04; evidence: T-2026-CU-9050-044). Monitor 8 already treats
    open_time as UTC (ot_aware) — for ROM1 rows that assumption now holds.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ai_signals
                (symbol, price, model, direction, confidence,
                 entry1, entry2, sl, targets, open_time)
            VALUES (%s, %s, 'ROM1', %s, 1.0, %s, %s, %s, %s, %s)
            """,
            (
                coin,
                params["entry1"],
                direction,
                params["entry1"],
                params["entry2"],
                params["sl"],
                json.dumps(params["targets"]),
                utc_now_naive(),
            ),
        )
    if commit:
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
    wl_reason: str | None = None,
    commit: bool = True,
) -> None:
    """Records a forwarded trade in orchestrator_open_trades.

    commit=False: siehe insert_rom1_signal — atomarer Forward-Commit im Caller.

    wl_reason (B8) persists WHICH gate path let the signal through — a real 4D
    cell, `no_whitelist_entry`, or one of the fallback paths. Without it the
    forwarded side of the gate is unauditable (suppressed_signals only records
    the blocked side); 26_regime_detector.post_hourly_status reads it for the
    default-open rate.
    """
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orchestrator_open_trades
                (coin, direction, bot_name, entry_price, opened_at,
                 regime_at_open, alt_context_at_open,
                 original_outbox_id, wl_reason, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN')
            """,
            (coin, direction, bot_name, entry, now_utc, regime, alt_context, outbox_id, wl_reason),
        )
    if commit:
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
    global _last_seen_outbox_id, _outbox_cursor_initialized

    from core.config import REGIME_TRADING_CHANNEL_ID

    # Cursor beim Prozessstart auf MAX(id) setzen — ohne das würde nach einem
    # Restart (Cursor = 0) das ganze Detection-Fenster erneut durchlaufen und
    # bereits gegatete Rows bekämen Duplikat-Einträge in orchestrator_suppressed_signals
    # (verzerrt die Suppression-Statistik). Signale aus der Downtime sind damit
    # bewusst übersprungen — wie vorher auch (sent=TRUE-Filter bzw. P2.28).
    if not _outbox_cursor_initialized:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM telegram_outbox")
            _last_seen_outbox_id = cur.fetchone()[0]
        _outbox_cursor_initialized = True
        return

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - timedelta(seconds=NEW_SIGNAL_DETECTION_WINDOW_SEC)

    # FIX P0.3: channel_id-Filter — eigene ROM1-Posts landen im selben Outbox-
    # Table und matchen die Bot-Patterns ("Triggered by: MIS1-8h") → Self-Echo
    # durch die ganze Pipeline. Der Trading-Channel wird deshalb hart
    # ausgeschlossen.
    # FIX P1.6: kein sent/failed-Filter mehr — der Dispatcher racet gegen diesen
    # Scan und markiert Signale zwischen zwei Pässen als sent → sie fielen aus
    # dem SELECT und wurden nie gegated. Neuheit garantiert der id-Cursor.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, channel_id, message
            FROM telegram_outbox
            WHERE id > %s
              AND created_at >= %s
              AND channel_id IS DISTINCT FROM %s
            ORDER BY id ASC
            LIMIT 100
            """,
            (_last_seen_outbox_id, cutoff, REGIME_TRADING_CHANNEL_ID),
        )
        rows = cur.fetchall()

    if not rows:
        return

    for outbox_id, channel_id, message in rows:
        # Per-Row-Isolation: eine kaputte Row darf weder die restlichen Rows
        # des Passes abreissen noch (weil der Cursor sonst nie advanced) alle
        # 500ms erneut crashen und den Signalstrom blockieren.
        try:
            _gate_and_forward_row(conn, outbox_id, channel_id, message, REGIME_TRADING_CHANNEL_ID)
        except Exception as row_err:
            logger.error(f"Gating-Fehler bei Outbox #{outbox_id}: {row_err}", exc_info=True)
            try:
                conn.rollback()
            except Exception:
                break  # Connection unbrauchbar — Pass abbrechen, nächster Loop-Tick holt frische
        finally:
            _last_seen_outbox_id = max(_last_seen_outbox_id, outbox_id)


def _gate_and_forward_row(
    conn, outbox_id: int, channel_id: int | None, message: str, regime_trading_channel_id: int
) -> None:
    """Gated eine einzelne Outbox-Row und forwarded sie ggf. als ROM1-Trade.

    Wird pro Row in einem eigenen try/except aufgerufen (siehe signal_gating_pass).
    """
    # ── Parse signal ──────────────────────────────────────────────────────
    parsed = parse_cornix_signal(message or "")
    if parsed is None:
        return  # Not a Cornix signal

    coin = parsed["coin"]
    direction = parsed["direction"]

    # FIX P0.3 (zweite Barriere zum Channel-Filter im SELECT): eigene
    # ROM1-Messages hart verwerfen, egal aus welchem Channel sie kommen.
    if ROM1_SIGNATURE in message:
        return

    # ── Identify bot ──────────────────────────────────────────────────────
    bot_name = identify_bot(message, channel_id)
    if bot_name is None:
        logger.warning(f"Bot nicht identifizierbar für Outbox #{outbox_id}: {message[:80]!r}")
        log_suppressed(conn, None, coin, direction, "bot_unidentified", outbox_id)
        return

    # FIX P0.4: Whitelist-/Performance-Keys sind pretty_name()-normalisiert
    # (27_bot_regime_analyzer schreibt "MIS1-8h"/"FastInOut"), identify_bot
    # liefert Roh-Namen ("MIS1-8H"/"Fast In And Out") → ohne Normalisierung
    # fand der case-sensitive Lookup NIE etwas und jedes Signal lief als
    # "no_whitelist_entry" ungefiltert durch. pretty_name ist idempotent.
    bot_name = pretty_name(bot_name)

    # ── Whitelist check ───────────────────────────────────────────────────
    whitelisted, wl_reason = get_whitelist_decision(conn, bot_name, direction)
    if not whitelisted:
        logger.info(f"⛔ Signal filtered: {bot_name} {coin} {direction} (reason: {wl_reason})")
        log_suppressed(conn, bot_name, coin, direction, f"bot_not_whitelisted:{wl_reason}", outbox_id)
        return

    # ── Cooldown check ────────────────────────────────────────────────────
    if check_cooldown(conn, ORCHESTRATOR_MODULE_NAME, coin, direction, ORCHESTRATOR_COOLDOWN_HOURS):
        log_suppressed(conn, bot_name, coin, direction, "orchestrator_cooldown", outbox_id)
        return

    # ── Cross-direction check ─────────────────────────────────────────────
    if is_opposite_direction_open(conn, coin, direction):
        log_suppressed(conn, bot_name, coin, direction, "opposite_direction_open", outbox_id)
        return

    # ── Same-direction check (FIX P2.26) ──────────────────────────────────
    if is_same_direction_open(conn, coin, direction):
        log_suppressed(conn, bot_name, coin, direction, "same_direction_open", outbox_id)
        return

    # ── ALL CHECKS PASSED → ROM1 BERECHNET EIGENEN TRADE ────────────────
    # Der Orchestrator übernimmt NICHT mehr die Werte des Original-Signals.
    # Stattdessen nutzt er dieselbe Logik wie die AI-Bots und berechnet
    # Entry/SL/Targets selbst — basierend auf aktuellem Preis und echten
    # S/R-Zonen aus get_hvn_and_sr_levels().
    rom1_params = compute_rom1_trade_params(conn, coin, direction)
    if rom1_params is None:
        # Preis not available oder S/R-Lookup fehlgeschlagen → nicht posten
        log_suppressed(conn, bot_name, coin, direction, "rom1_params_unavailable", outbox_id)
        return

    state = get_current_regime_full(conn)
    cur_regime = state[0] if state else None
    cur_alt = state[1] if state else None

    # ROM1 baut seine eigene Cornix-Message aus den berechneten Params.
    # trigger_bot zeigt im Signal-Text welcher Original-Bot den Trade
    # ausgelöst hat — Info bleibt für Leser und Downstream-Analytics erhalten.
    rom1_message = build_rom1_cornix_message(coin, direction, rom1_params, trigger_bot=bot_name)

    # FIX P0.3/P1.7: Cooldown, Tracking (ai_signals + orchestrator_open_trades)
    # und der Outbox-Post in EINER Transaktion auf derselben Connection, mit
    # dem Outbox-Insert als letztem Write — damit gibt es weder "Cornix-Trade
    # ohne Tracking" (alter Zustand, Send zuerst) noch "Phantom-Tracking/
    # Cooldown ohne Post". Ein Doppel-Post bei Rollback droht nicht: der
    # Cursor advanced pro Row im finally (signal_gating_pass) und wird beim
    # Prozess-Restart auf MAX(id) initialisiert.
    update_cooldown(conn, ORCHESTRATOR_MODULE_NAME, coin, direction, commit=False)

    # ROM1-Tracking in ai_signals (wird vom 8_ai_trade_monitor aufgegriffen)
    insert_rom1_signal(conn, coin, direction, rom1_params, commit=False)

    # Orchestrator open trade — nutzt jetzt auch den ROM1-Entry, nicht mehr den
    # originalen Entry aus dem parsed Signal
    insert_orchestrator_open_trade(
        conn,
        coin,
        direction,
        bot_name,
        rom1_params["entry1"],
        outbox_id,
        cur_regime,
        cur_alt,
        wl_reason=wl_reason,
        commit=False,
    )

    # Outbox-Insert bewusst auf `conn` statt via send_telegram() (das eine
    # eigene Connection + eigenen Commit nutzt) — nur so ist der Post Teil
    # derselben Transaktion wie das Tracking.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
            (regime_trading_channel_id, rom1_message),
        )
    conn.commit()

    logger.info(
        f"✅ ROM1-Trade gepostet: {coin} {direction} @ {rom1_params['entry1']:.6f} "
        f"(SL {rom1_params['sl']:.6f}, {len(rom1_params['targets'])} TPs) "
        f"[Trigger: {bot_name}, Regime: {cur_regime}/{cur_alt}]"
    )


# ─────────────────────────────────────────────────────────────────────────────
# LIFECYCLE SYNC
# ─────────────────────────────────────────────────────────────────────────────


def mark_orchestrator_trade_closed(
    conn,
    trade_id: int,
    status: str,
    reason: str,
    regime_close_action: str | None = None,
) -> None:
    """Updates an orchestrator_open_trades row to closed.

    regime_close_action (T-2026-CU-9050-049): optional A/B arm tag written
    atomically with the close ('REGIME_CHANGE_CLOSED' for a regime-change
    market-close). When None the column is left untouched — so the lifecycle-
    sync's final close of a previously-TRAILED trade does NOT overwrite its
    'REGIME_CHANGE_TRAILED' tag (the cohort marker must survive to the real exit).
    """
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    with conn.cursor() as cur:
        if regime_close_action is not None:
            cur.execute(
                """
                UPDATE orchestrator_open_trades
                SET status = %s, closed_at = %s, close_reason = %s,
                    regime_close_action = %s, regime_action_at = %s
                WHERE id = %s
                """,
                (status, now_utc, reason, regime_close_action, now_utc, trade_id),
            )
        else:
            cur.execute(
                """
                UPDATE orchestrator_open_trades
                SET status = %s, closed_at = %s, close_reason = %s
                WHERE id = %s
                """,
                (status, now_utc, reason, trade_id),
            )
    conn.commit()


def mark_orchestrator_trade_trailed(conn, trade_id: int, new_sl: float) -> None:
    """Tags an OPEN orchestrator trade as trailed at a regime change (A/B arm).

    T-2026-CU-9050-049. Keeps status='OPEN' — the trade keeps running with its
    moved SL and closes later via the monitor/lifecycle-sync with its real PnL.
    regime_close_action='REGIME_CHANGE_TRAILED' survives that final close
    (mark_orchestrator_trade_closed leaves it untouched), so the TRAILED cohort
    stays identifiable for the 4-6 week live comparison. It also excludes the
    trade from future regime-close passes (the candidate query filters
    regime_close_action IS NULL) so we never re-trail or spam SL-update messages.
    """
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE orchestrator_open_trades
            SET regime_close_action = 'REGIME_CHANGE_TRAILED', regime_action_at = %s
            WHERE id = %s
            """,
            (now_utc, trade_id),
        )
    conn.commit()


def build_rom1_sl_update_message(coin: str, new_sl: float) -> str:
    """Builds the Cornix SL-update command for an open ROM1 position.

    T-2026-CU-9050-049. Cornix here is configured for symbol-addressed channel
    commands — the same family as the existing `Close <SYMBOL>` command (see
    core.config:115). Like Close, this moves the stop for ALL open trades of the
    symbol in the trading channel.

    HARD RULE 4 (no double-parse): this MUST NOT be parseable as a new Cornix
    signal. parse_cornix_signal requires the "Signal for …/Direction/Entry"
    block, which this single-line command never carries — test_signal_orchestrator
    asserts parse_cornix_signal() returns None for this message.

    The exact keyword is operator-owned (Michi, T-2026-CU-9050-049: symbol-
    addressed, like Close). Kept as a single builder so the wire format has one
    place to change if the Cornix config is retuned.
    """
    return f"SL {coin} {new_sl:.8f}"


def _get_rom1_trade_levels(conn, coin: str, direction: str) -> tuple[int, list[float]]:
    """Best-effort (targets_hit, targets) for the open ROM1 twin of coin+direction.

    Returns (0, []) on any miss — the caller then trails to break-even, which is
    always safe for a winner. Matches the same coin+direction+model='ROM1' as
    force_close_trades_for_regime_change; on multiple opens the most recent wins.
    Wrapped in a SAVEPOINT so a bad row can't poison the caller's transaction.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT sp_rom1_levels")
            try:
                cur.execute(
                    """
                    SELECT current_target_hit, targets
                    FROM ai_signals
                    WHERE symbol = %s AND direction = %s AND model = 'ROM1'
                    ORDER BY open_time DESC
                    LIMIT 1
                    """,
                    (coin, direction),
                )
                row = cur.fetchone()
                cur.execute("RELEASE SAVEPOINT sp_rom1_levels")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_rom1_levels")
                return (0, [])
    except Exception:
        return (0, [])
    if not row:
        return (0, [])
    targets_hit, targets_raw = row
    tlist: list[float] = []
    if targets_raw:
        try:
            parsed = targets_raw if isinstance(targets_raw, list) else json.loads(targets_raw)
            tlist = [float(t) for t in parsed]
        except (ValueError, TypeError):
            tlist = []
    return (int(targets_hit or 0), tlist)


def _compute_trailed_sl(
    direction: str,
    entry: float,
    current_price: float,
    targets_hit: int,
    targets: list[float] | None,
) -> float | None:
    """SL level to trail a winning trade to, or None if it is not a protectable
    winner (→ caller closes it instead).

    T-2026-CU-9050-049. Winner = current price beyond entry in the trade
    direction by more than TRAIL_MIN_PROFIT_PCT. The trailed SL is the last
    REACHED TP level when at least one TP was hit, else break-even (entry). A TP
    level is only used when it still sits on the protective side of the current
    price (below for LONG, above for SHORT); if price has retraced through it we
    fall back to break-even, which for a winner is always on the safe side. So
    the returned SL can never instantly stop the trade out. Returns None on
    invalid entry/price or when the trade is not in meaningful profit.
    """
    is_long = direction == "LONG"
    if entry <= 0 or current_price <= 0:
        return None
    raw = (current_price - entry) / entry * 100.0
    pnl = raw if is_long else -raw
    if pnl <= TRAIL_MIN_PROFIT_PCT:
        return None  # not a winner → close
    level = entry  # break-even default (always protective for a winner)
    if targets_hit and targets:
        idx = min(int(targets_hit), len(targets)) - 1
        if 0 <= idx < len(targets):
            tp = float(targets[idx])
            if (is_long and tp < current_price) or (not is_long and tp > current_price):
                level = tp
    # Final guard: SL must sit strictly on the protective side of current price.
    if is_long and level >= current_price:
        return None
    if not is_long and level <= current_price:
        return None
    return level


def _get_last_close_price(conn, coin: str, fallback: float | None = None) -> float | None:
    """Holt den letzten 5m-Close-Preis des Coins, mit Entry-Fallback.

    Wird beim Regime-Wechsel-Close genutzt um einen sinnvollen Close-Preis
    zu haben, auch wenn der Trade gerade nicht an einem SL/TP-Level steht.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT sp_get_close")
            try:
                cur.execute(f'SELECT close FROM "{coin}_5m" ORDER BY open_time DESC LIMIT 1')
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
    """Schließt die offenen ROM1-Trades für coin+direction wegen Regime-Wechsel.

    Findet offene ROM1-entries in ai_signals für das angegebene
    coin+direction und verschiebt sie in closed_ai_signals mit
    status-Marker "CLOSED_REGIME_CHANGE". Wird vom Regime-Wechsel-
    Handler aufgerufen afterdem der Close-Command an Cornix gegangen ist.

    FIX P1.9: Vorher wurden ALLE offenen Trades aller Bots auf coin+direction
    geschlossen (kein model-/strategy-Filter) — fremde Verluste wurden als
    neutral zensiert und die Whitelist-Winrates nach oben gebiast. Der
    Close-Command geht nur an den ROM1-Trading-Channel, also dürfen auch nur
    die orchestrator-eigenen Rows (model='ROM1') geschlossen werden. Der
    active_trades_master-Block ist raus — ROM1 schreibt nie dorthin, dort
    konnten nur Fremd-Trades getroffen werden.

    Warum nicht erst den Monitor warten lassen?
    - Cornix schließt die Binance-Position JETZT
    - Der Monitor würde den Trade erst schließen wenn sein eigener
      SL/TP erreicht wird — das kann Stunden/Tage dauern
    - In der Zwischenzeit würde der Trade als "still open" in der
      Statistik erscheinen und die WR verzerren

    Classification (B9-Zensur-Korrektur, T-2026-CU-9050-048): Der
    "CLOSED_REGIME_CHANGE"-Marker wird von Market-Tracker, Analyzer und
    Orchestrator-Outcome-Classifier mit seinem REALEN PnL zum Close-Zeitpunkt
    als Win/Loss gewertet (vorher pauschal NEUTRAL — das zensierte genau die
    per Auto-Close realisierten Verluste und biaste die ROM1-WR nach oben,
    Report 16 B9). Der Auto-Close ist der Exit des Trades, nicht ein externes
    Housekeeping-Event.

    Returns:
        dict mit 'ai_closed' und 'classic_closed' (Anzahl der geschlossenen)
    """
    result = {"ai_closed": 0, "classic_closed": 0}
    now = datetime.now(timezone.utc)

    # ── AI-Trades (ai_signals) ─────────────────────────────────────────────
    # FIX P1.9: nur die ROM1-Kopie des Orchestrators selbst — fremde
    # AI-Trades bleiben offen und werden von ihren eigenen Monitoren bewertet.
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, symbol, model, direction, entry1, price,
                       current_target_hit, open_time
                FROM ai_signals
                WHERE symbol = %s AND direction = %s AND model = 'ROM1'
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
        entry = float(entry1) if entry1 is not None else (float(price) if price is not None else 0.0)
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
                        symbol,
                        model,
                        trade_dir,
                        entry,
                        close_price,
                        int(targets_hit or 0),
                        open_time,
                        now,
                        "CLOSED_REGIME_CHANGE",
                    ),
                )
                cur.execute("DELETE FROM ai_signals WHERE id = %s", (tid,))
            conn.commit()
            result["ai_closed"] += 1
        except Exception as e:
            logger.warning(f"Regime-Close: AI-Trade {tid} ({symbol} {model}) konnte nicht geschlossen werden: {e}")
            conn.rollback()

    # FIX P1.9: kein active_trades_master-Close mehr — 'classic_closed' bleibt
    # für die Caller-Summary im Result, ist aber immer 0.
    return result


def _classify_outcome_by_pnl(
    direction: str, entry: float | None, close_price: float | None, close_reason: str | None
) -> str:
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
    # → nicht in Win/Loss-Statistik zählen.
    #
    # B9-Zensur-Korrektur (T-2026-CU-9050-048): REGIME_CHANGE ist NICHT mehr
    # neutral — ein Auto-Close bei Regime-Wechsel realisiert einen echten PnL,
    # der als Win/Loss zählen muss (near-0%-Closes fängt der Micro-PnL-Filter
    # weiter neutral ab). Spiegelt _classify_outcome in 27_bot_regime_analyzer.
    if "DELISTED" in reason or "CLEANUP" in reason or "ORPHAN" in reason:
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
    Checks if any open orchestrator trades have been closed in closed_ai_signals
    (model='ROM1' — die eigene Tracking-Kopie, siehe FIX P1.8 unten).
    Updates orchestrator_open_trades accordingly.
    Runs every LIFECYCLE_SYNC_INTERVAL_SEC seconds.

    Klassifiziert das Outcome PnL-basiert (statt targets_hit/status-basiert),
    um die bekannten Bugs im 8_ai_trade_monitor zu umgehen (LEGACY TARGET HIT
    schreibt targets_hit=0 etc.). DELISTED/CLEANUP-Trades bekommen den Status
    CLOSED_NEUTRAL — die Orchestrator-Performance wird dadurch nicht verzerrt.
    """
    # Reaper FIRST: corpse decay must not depend on the health of the per-row
    # match loop below (a poison row there would silently disable decay while
    # the unbounded direction blocks stay active). Safe to run first: the
    # reaper skips anything the match loop could still classify.
    await reap_corpse_trades(conn)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, coin, direction, bot_name, opened_at
            FROM orchestrator_open_trades
            WHERE status = 'OPEN'
            """
        )
        open_trades = cur.fetchall()

    for trade_id, coin, direction, _bot_name, opened_at in open_trades:
        opened_at_naive = (
            opened_at if not (hasattr(opened_at, "tzinfo") and opened_at.tzinfo) else opened_at.replace(tzinfo=None)
        )
        # FIX P1.8: Vorher matchte der Sync per Zufall fremde Trades — kein
        # model-Filter, kein ORDER BY, 720h-Fenster (Report 18 K1: Outcome
        # nichtdeterministisch, Opposite-Schutz fiel vorzeitig weg). Jetzt:
        # nur model='ROM1', open_time als Zeitanker ±60s gegen opened_at
        # (Tracking + ai_signals entstehen in derselben Transaktion), bei
        # mehreren Kandidaten gewinnt die kleinste Zeitdifferenz. Der
        # closed_trades_master-Check ist raus — ROM1 schreibt nie nach
        # active_trades_master, dort konnte nur ein Fremd-Trade matchen.
        #
        # T-2026-CU-9050-052: second window through LEGACY_SESSION_TZ — rows
        # written before this fix carry session-local open_time (+3h), and
        # their closes (copied verbatim by the monitor) would otherwise never
        # match; a legacy-era trade that closes AFTER deploy would lose its
        # real WIN/LOSS to the corpse reaper. Safe against cross-matching a
        # different trade: the 4h per-coin+direction cooldown means no two
        # same-direction trades can sit 3h±window apart.
        # `status` in closed_ai_signals enthält tatsächlich den close_reason
        # (z.B. "LEGACY TARGET HIT (+2.5%)", "DELISTED / CLEANUP").
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT entry, close_price, status FROM closed_ai_signals
                WHERE symbol = %s AND direction = %s AND model = 'ROM1'
                  AND {_anchor_window_predicate("open_time", "%s::timestamp")}
                ORDER BY LEAST(
                    ABS(EXTRACT(EPOCH FROM (open_time - %s))),
                    ABS(EXTRACT(EPOCH FROM (
                        (open_time AT TIME ZONE '{LEGACY_SESSION_TZ}' AT TIME ZONE 'UTC') - %s
                    )))
                )
                LIMIT 1
                """,
                (coin, direction) + (opened_at_naive,) * 6,
            )
            row = cur.fetchone()

        if row:
            entry, close_price, close_reason = row
            new_status = _classify_outcome_by_pnl(direction, entry, close_price, close_reason)
            mark_orchestrator_trade_closed(conn, trade_id, new_status, "lifecycle_sync")


async def reap_corpse_trades(conn) -> None:
    """Closes OPEN tracking rows whose ai_signals twin is gone (corpse reaper).

    T-2026-CU-9050-052: a live ROM1 trade always has its ai_signals twin (both
    rows are written in the same transaction; the monitor deletes the twin on
    close). An OPEN row WITHOUT a twin is a trade that closed but was never
    synced — either a legacy corpse from the dead-sync era (open_time stamped
    +3h by the session-TZ DB default, the ±60s sync window can never match:
    395 rows as of 2026-07-10) or a pathological case (manually deleted
    ai_signals row). Left OPEN, such rows block the direction checks forever,
    feed spurious Close commands into every regime-change pass, and get
    re-scanned by the sync loop on every pass.

    Guards, in order:
    - 72h minimum age: never touches fresh rows.
    - Twin check is ROW-anchored (±window around opened_at, both rows are
      written in one transaction), NOT just coin+direction — a live trade's
      twin must not shield a corpse that shares coin+direction (stacking era),
      because that corpse would keep feeding Close commands that flatten the
      LIVE position on the next regime flip. Legacy twins sit at
      LEGACY_SESSION_TZ local time, hence the second window.
    - Never censors a classifiable outcome: if a closed_ai_signals row exists
      in either sync window (same two windows as the match loop — asymmetry
      here would censor legacy-era closes), the regular match loop will
      classify the real WIN/LOSS — the reaper skips (closes the
      monitor-commit race for >72h trades; the monitor deletes the twin and
      writes the close atomically).

    Reaped rows get CLOSED_NEUTRAL (no PnL/WR distortion, same convention as
    DELISTED/CLEANUP) and closed_at = reap time — NOT the real close time,
    which is unknowable for corpses. Duration stats must exclude
    close_reason='corpse_reaper'. No Telegram post — bookkeeping, not a trade
    action. A twin that is stuck (monitor cannot process the coin) keeps its
    row OPEN by design — protection over availability; the decay path for
    that case is housekeeping's DELISTED cleanup deleting the twin.
    """
    now_naive = utc_now_naive()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE orchestrator_open_trades o
            SET status = 'CLOSED_NEUTRAL', closed_at = %s, close_reason = 'corpse_reaper'
            WHERE o.status = 'OPEN'
              AND o.opened_at < %s
              AND NOT EXISTS (
                  SELECT 1 FROM ai_signals a
                  WHERE a.model = 'ROM1' AND a.symbol = o.coin AND a.direction = o.direction
                    AND {_anchor_window_predicate("a.open_time", "o.opened_at")}
              )
              AND NOT EXISTS (
                  SELECT 1 FROM closed_ai_signals c
                  WHERE c.model = 'ROM1' AND c.symbol = o.coin AND c.direction = o.direction
                    AND {_anchor_window_predicate("c.open_time", "o.opened_at")}
              )
            """,
            (now_naive, now_naive - timedelta(hours=72)),
        )
        reaped = cur.rowcount
    conn.commit()
    if reaped:
        logger.info(f"🧹 Corpse-Reaper: {reaped} verwaiste OPEN-Rows ohne ai_signals-Twin neutral geschlossen.")


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

    _close_non_whitelisted_open_trades(
        conn,
        changes=changes,
        title="🔄 REGIME CHANGE & AUTO-CLOSE",
        count_label="Open trades before change",
    )


def _close_non_whitelisted_open_trades(
    conn,
    changes: list[str],
    title: str,
    count_label: str,
    always_announce: bool = True,
) -> None:
    """Re-judges every OPEN orchestrator trade against the CURRENT 4D whitelist
    and posts Close (loser) / SL-trail (winner, A/B gate) commands for the ones
    the current regime no longer whitelists, then posts a summary.

    Shared by two triggers:
      - check_regime_change_and_close: an OBSERVED in-memory regime flip.
      - run_startup_reconciliation (P2.24): a flip that happened while the
        orchestrator was DOWN is never observed as a flip, so we reconcile
        against the current whitelist at startup instead of a remembered regime.

    Money-path safety: only ROM1's own tracked rows live in
    orchestrator_open_trades, and the DB-side force-close is model='ROM1'
    filtered (P1.9) — foreign bots' trades are never touched. No new close
    mechanism: reuses the existing Close-command + mark/force-close path.
    """
    from core.config import REGIME_STATUS_CHANNEL_ID, REGIME_TRADING_CHANNEL_ID

    # Load all open trades that have not already been actioned by a prior
    # regime pass. T-2026-CU-9050-049: a TRAILED winner stays status='OPEN' but
    # carries regime_close_action — excluding it here prevents re-trailing / a
    # second SL-update on every subsequent regime change. (Closed rows are
    # already excluded by status <> 'OPEN'.)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, coin, direction, bot_name, entry_price "
            "FROM orchestrator_open_trades "
            "WHERE status = 'OPEN' AND regime_close_action IS NULL"
        )
        open_trades = cur.fetchall()

    # Each candidate carries trail_sl: a float SL level → trail the winner, or
    # None → close the loser. trail_sl stays None entirely while the gate is off.
    closes = []
    keeps = []

    for trade_id, coin, direction, bot_name, entry_price in open_trades:
        whitelisted, reason = get_whitelist_decision(conn, bot_name, direction)
        if whitelisted:
            keeps.append((coin, direction, bot_name))
            continue
        trail_sl = None
        if TRAIL_WINNERS_ON_REGIME_CHANGE:
            entry = float(entry_price) if entry_price is not None else 0.0
            current_price = _get_last_close_price(conn, coin, fallback=entry) or entry
            targets_hit, targets = _get_rom1_trade_levels(conn, coin, direction)
            trail_sl = _compute_trailed_sl(direction, entry, current_price, targets_hit, targets)
        closes.append((trade_id, coin, direction, bot_name, reason, trail_sl))

    # Pass 1: trail winners (SL-update, keep running). Done FIRST so Pass 2 can
    # avoid flattening a just-trailed winner with the symbol-wide `Close <coin>`.
    trailed_coins: set[str] = set()
    n_trailed = 0
    for trade_id, coin, direction, bot_name, reason, trail_sl in closes:
        if trail_sl is None:
            continue
        # SL-update, NOT a second Cornix signal (hard rule 4). Symbol-addressed
        # like Close (core.config:115) — one command per winning coin.
        send_telegram(build_rom1_sl_update_message(coin, trail_sl), REGIME_TRADING_CHANNEL_ID)
        mark_orchestrator_trade_trailed(conn, trade_id, trail_sl)
        trailed_coins.add(coin)
        n_trailed += 1
        logger.info(
            f"🎯 Regime-Change TRAIL: {coin} {direction} SL→{trail_sl:.8f} "
            f"(bot={bot_name}, reason={reason}) — trade kept open (A/B: TRAILED)"
        )

    # Pass 2: close losers. Post Close-Commands and move trades to closed tables.
    total_ai = 0
    total_classic = 0
    n_closed = 0
    for trade_id, coin, direction, bot_name, reason, trail_sl in closes:
        if trail_sl is not None:
            continue
        if coin in trailed_coins:
            # Cornix `Close <coin>` is symbol-wide (core.config:115) and would
            # flatten the trailed winner on the same symbol. Skip and leave this
            # row OPEN for the next regime pass to re-judge once the winner exits.
            logger.warning(
                f"Regime-Close: defer Close {coin} {direction} — same symbol has a "
                f"trailed winner this pass; symbol-wide Close would flatten it."
            )
            continue
        send_telegram(f"Close {coin}", REGIME_TRADING_CHANNEL_ID)

        # Mark orchestrator_open_trades row as closed (A/B arm: CLOSED).
        mark_orchestrator_trade_closed(
            conn,
            trade_id,
            "CLOSED_REGIME_CHANGE",
            f"REGIME_CHANGE:{reason}",
            regime_close_action="REGIME_CHANGE_CLOSED",
        )

        # Verschiebe die ROM1-Tracking-Kopie aus ai_signals nach
        # closed_ai_signals, damit sie nicht als "still open" in den
        # Market-Tracker-Reports erscheint bis der Monitor irgendwann
        # ihren eigenen SL/TP trifft (kann Tage dauern). Der Marker
        # "CLOSED_REGIME_CHANGE" wird downstream mit seinem realen PnL als
        # Win/Loss klassifiziert (B9-Zensur-Korrektur, T-2026-CU-9050-048).
        # FIX P1.9: fremde Trades (andere Modelle, active_trades_master)
        # bleiben unangetastet.
        close_stats = force_close_trades_for_regime_change(conn, coin, direction)
        total_ai += close_stats["ai_closed"]
        total_classic += close_stats["classic_closed"]
        n_closed += 1

        logger.info(
            f"🛑 Close command posted: {coin} {direction} "
            f"(bot={bot_name}, reason={reason}) — "
            f"DB: {close_stats['ai_closed']} AI + "
            f"{close_stats['classic_closed']} classic force-closed"
        )

    # Summary to status channel. always_announce=True (regime-change caller)
    # always posts — an observed flip is worth reporting even if every trade was
    # kept. The startup caller passes False: it stays silent unless it actually
    # closed or trailed something, so a routine watchdog restart with N healthy
    # open trades does not spam the status channel on every boot.
    if not (always_announce or n_closed or n_trailed):
        return

    change_line = "\n".join(changes)
    close_lines = "\n".join(
        f"  {c[1]} {c[2]} ({c[3]}) — {'trailed 🎯' if c[5] is not None else 'closed 🛑'}" for c in closes
    )
    keep_lines = "\n".join(f"  {k[0]} {k[1]} ({k[2]}) — kept open (whitelisted)" for k in keeps)
    summary = f"{title}\n\n{change_line}\n\n{count_label}: {len(open_trades)}\n"
    if closes:
        summary += close_lines + "\n"
    if keeps:
        summary += keep_lines + "\n"
    summary += f"\n{n_closed} close + {n_trailed} trail command(s) posted to trading channel."
    if total_ai or total_classic:
        summary += (
            f"\n{total_ai} AI + {total_classic} classic trade(s) moved to "
            f"closed tables with status CLOSED_REGIME_CHANGE."
        )

    try:
        send_telegram(summary, REGIME_STATUS_CHANNEL_ID)
    except Exception as e:
        logger.error(f"Error sending des Regime-Change-Summary: {e}")


async def run_startup_reconciliation(conn) -> None:
    """P2.24: one-shot startup catch-up for regime changes missed during downtime.

    check_regime_change_and_close only acts on an OBSERVED in-memory regime flip
    (regime_current now differs from the value remembered at the last poll). On a
    fresh start that baseline (_last_known_regime) is empty, so the first poll
    only SEEDS it and returns — a regime flip that happened while the orchestrator
    was down is therefore never acted on, and every open trade keeps running under
    a regime that may no longer whitelist it (the P2.24 gap: In-Memory-State,
    "Regime-Wechsel während Orchestrator-Downtime nie nachgeholt").

    This reconciles every OPEN trade against the CURRENT whitelist at startup — no
    remembered regime needed — and seeds the in-memory baseline so the periodic
    check starts clean (and does not re-fire on the current state). It reuses the
    same ROM1-only close/trail path as the regime-change handler, so no new close
    mechanism and no foreign-bot trades touched (P1.9).
    """
    global _last_known_regime, _last_known_alt_context

    # Seed the in-memory baseline from the current regime so the first periodic
    # check_regime_change_and_close does not treat the boot state as a change.
    state = get_current_regime_full(conn)
    if state is not None:
        _last_known_regime, _last_known_alt_context = state

    if not AUTO_CLOSE_ON_REGIME_CHANGE:
        return

    logger.info("🚀 Startup reconciliation: prüfe offene ROM1-Trades gegen die aktuelle Whitelist (P2.24).")
    _close_non_whitelisted_open_trades(
        conn,
        changes=["Startup reconciliation — offene Trades gegen aktuelle Whitelist geprüft (P2.24)"],
        title="🚀 ORCHESTRATOR STARTUP — WHITELIST RECONCILIATION",
        count_label="Open trades at startup",
        always_announce=False,
    )


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


async def _run_stage(conn, stage_name: str, coro) -> bool:
    """Runs one main-loop stage; an exception is logged and the connection is
    rolled back so the NEXT stage does not inherit an aborted transaction.
    Returns False on failure so the caller can keep money-path couplings.

    T-2026-CU-9050-052: the stages were one try block before — a persistent
    poison row in the regime check or the gating pass would then silently
    starve the lifecycle sync (and with it the corpse reaper, the only decay
    path for stuck OPEN rows) forever.
    """
    try:
        await coro
        return True
    except Exception as e:
        logger.error(f"Orchestrator-Loop-Error [{stage_name}]: {e}", exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
        return False


async def main_loop() -> None:
    logger.info("=== 🎯 SIGNAL ORCHESTRATOR STARTED ===")

    # P2.24: reconcile open trades against the current whitelist ONCE at startup,
    # before the periodic loop begins. This closes the downtime gap that the
    # in-memory regime tracker cannot see (see run_startup_reconciliation). Runs
    # on its own short-lived connection and is fully fail-safe: a failure here
    # must never prevent the orchestrator from starting its normal loop.
    startup_conn = None
    try:
        startup_conn = get_db_connection()
        await run_startup_reconciliation(startup_conn)
    except Exception as e:
        logger.error(f"Orchestrator startup reconciliation failed: {e}", exc_info=True)
        if startup_conn is not None:
            try:
                startup_conn.rollback()
            except Exception:
                pass
    finally:
        if startup_conn is not None:
            startup_conn.close()

    while True:
        conn = None
        try:
            conn = get_db_connection()
            regime_ok = await _run_stage(conn, "regime_change", check_regime_change_and_close(conn))
            if regime_ok:
                await _run_stage(conn, "gating", signal_gating_pass(conn))
            else:
                # Fail-closed on the money path: while regime-flip auto-closes
                # are broken, do NOT open new exposure via the gating pass.
                # Only the lifecycle sync below stays independent.
                logger.warning("Gating-Pass übersprungen: Regime-Stage fehlgeschlagen (fail-closed).")
            if should_run_lifecycle_sync():
                await _run_stage(conn, "lifecycle_sync", sync_closed_trades(conn))
        except Exception as e:
            # Backstop: nothing here may ever kill the process (no gating, no
            # auto-closes, no reaping on the live fleet).
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
