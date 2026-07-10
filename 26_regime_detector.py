# 26_regime_detector.py
"""
Regime Detector — Phase 1 des Regime-Orchestrators.

Läuft alle 5 Minuten und:
  1. Berechnet Features (BTC + BTCDOM)
  2. Klassifiziert BTC-Regime + Alt-Context (zwei Achsen)
  3. Schreibt jeden Check after regime_history
  4. Debounced beide Achsen unabhängig → regime_current
  5. Bei bestätigtem Wechsel: Regime-Change-Alert in REGIME_STATUS_CHANNEL_ID

Watchdog: start_delay=160
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from core.database import get_db_connection
from core.logging_setup import setup_logging
from core.regime_logic import (
    apply_debounce,
    classify_regime,
    compute_features,
    hysteresis_prev_regime,
    read_regime_state,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
CHECK_INTERVAL_SECONDS = 300  # alle 5 Minuten
TREND_RETURN_THRESHOLD_4H_PCT = 1.5  # Referenz für Status-Posts
VOLA_HIGH_PERCENTILE = 75
VOLA_LOW_PERCENTILE = 40
ALT_CONTEXT_THRESHOLD_PCT = 1.5
# Share of forwarded ROM1 trades that bypassed a real 4D whitelist cell
# (default-open or fallback) above which the hourly status flags the gate.
# P0.4 ran undetected for months precisely because a silently-open gate looks
# exactly like a permissive one from the outside.
GATE_DEFAULT_OPEN_ALARM_PCT = 20.0
GATE_STATS_LOOKBACK_HOURS = 24

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logger = setup_logging("REGIME_DETECTOR")


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA
# ─────────────────────────────────────────────────────────────────────────────


def ensure_regime_schema(conn) -> None:
    """
    Creates all regime-orchestrator tables if they don't exist.
    Idempotent — safe to call on every startup.
    """
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS regime_history (
                id SERIAL PRIMARY KEY,
                ts TIMESTAMP WITHOUT TIME ZONE NOT NULL UNIQUE,
                regime TEXT NOT NULL,
                alt_context TEXT NOT NULL,
                btc_price REAL,
                btc_return_1h REAL,
                btc_return_4h REAL,
                btc_atr_1h_pct REAL,
                btc_atr_4h_pct REAL,
                btcdom_value REAL,
                btcdom_return_24h REAL,
                confidence REAL,
                confidence_btc REAL,
                confidence_alt REAL,
                raw_features JSON
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_regime_history_ts_desc
            ON regime_history (ts DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_regime_history_regime
            ON regime_history (regime)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_regime_history_alt_context
            ON regime_history (alt_context)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS regime_current (
                id INT PRIMARY KEY DEFAULT 1,
                regime TEXT NOT NULL,
                alt_context TEXT NOT NULL,
                since TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                alt_context_since TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                confidence REAL,
                last_raw_regime TEXT,
                last_raw_alt_context TEXT,
                last_raw_ts TIMESTAMP WITHOUT TIME ZONE,
                pending_regime TEXT,
                pending_count INT DEFAULT 0,
                pending_alt_context TEXT,
                pending_alt_count INT DEFAULT 0,
                CONSTRAINT singleton_check CHECK (id = 1)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_regime_performance (
                id SERIAL PRIMARY KEY,
                bot_name TEXT NOT NULL,
                regime TEXT NOT NULL,
                alt_context TEXT NOT NULL,
                direction TEXT NOT NULL,
                window_days INT NOT NULL,
                n_trades INT NOT NULL,
                win_rate REAL,
                avg_pnl_pct REAL,
                median_pnl_pct REAL,
                pnl_stddev REAL,
                sharpe_like REAL,
                worst_trade_pct REAL,
                best_trade_pct REAL,
                last_computed TIMESTAMP WITHOUT TIME ZONE
                    DEFAULT (NOW() AT TIME ZONE 'UTC'),
                UNIQUE (bot_name, regime, alt_context, direction, window_days)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_brp_bot_name
            ON bot_regime_performance (bot_name)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_brp_regime
            ON bot_regime_performance (regime)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_brp_alt_context
            ON bot_regime_performance (alt_context)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_regime_whitelist (
                bot_name TEXT NOT NULL,
                regime TEXT NOT NULL,
                alt_context TEXT NOT NULL,
                direction TEXT NOT NULL,
                whitelisted BOOLEAN NOT NULL,
                reason TEXT,
                computed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                PRIMARY KEY (bot_name, regime, alt_context, direction)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS orchestrator_open_trades (
                id SERIAL PRIMARY KEY,
                coin TEXT NOT NULL,
                direction TEXT NOT NULL,
                bot_name TEXT NOT NULL,
                entry_price REAL,
                opened_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                regime_at_open TEXT,
                alt_context_at_open TEXT,
                original_outbox_id BIGINT,
                wl_reason TEXT,
                status TEXT DEFAULT 'OPEN',
                closed_at TIMESTAMP WITHOUT TIME ZONE,
                close_reason TEXT
            )
        """)
        # B8: additive for DBs created before the column existed. Rows written
        # by the pre-B8 orchestrator keep wl_reason NULL — the gate-quality
        # stats below count them separately instead of guessing a path.
        cur.execute("""
            ALTER TABLE orchestrator_open_trades
            ADD COLUMN IF NOT EXISTS wl_reason TEXT
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_oot_status
            ON orchestrator_open_trades (status)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_oot_coin_dir
            ON orchestrator_open_trades (coin, direction)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS orchestrator_suppressed_signals (
                id SERIAL PRIMARY KEY,
                ts TIMESTAMP WITHOUT TIME ZONE
                    DEFAULT (NOW() AT TIME ZONE 'UTC'),
                bot_name TEXT,
                coin TEXT,
                direction TEXT,
                regime_at_signal TEXT,
                reason TEXT,
                original_outbox_id BIGINT
            )
        """)

        # trade_cooldowns may already exist (used by all bots)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_cooldowns (
                module TEXT NOT NULL,
                coin TEXT NOT NULL,
                direction TEXT NOT NULL,
                last_posted_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                PRIMARY KEY (module, coin, direction)
            )
        """)

    conn.commit()
    logger.info("✅ Regime schema ensured (all tables present).")


# ─────────────────────────────────────────────────────────────────────────────
# REGIME HISTORY INSERT
# ─────────────────────────────────────────────────────────────────────────────


def insert_regime_history(conn, result: dict, features: dict) -> None:
    """Inserts one row into regime_history for the current check."""
    ts_now = datetime.now(timezone.utc).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO regime_history
                (ts, regime, alt_context,
                 btc_price, btc_return_1h, btc_return_4h,
                 btc_atr_1h_pct, btc_atr_4h_pct,
                 btcdom_value, btcdom_return_24h,
                 confidence, confidence_btc, confidence_alt,
                 raw_features)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ts) DO NOTHING
            """,
            (
                ts_now,
                result["regime"],
                result["alt_context"],
                features.get("btc_price"),
                features.get("btc_return_1h"),
                features.get("btc_return_4h"),
                features.get("btc_atr_1h_pct"),
                features.get("btc_atr_4h_pct"),
                features.get("btcdom_value"),
                features.get("btcdom_return_24h"),
                result["confidence"],
                result["confidence_btc"],
                result["confidence_alt"],
                json.dumps({k: v for k, v in features.items() if isinstance(v, (int, float, type(None)))}),
            ),
        )
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# REGIME-CHANGE HANDLER
# ─────────────────────────────────────────────────────────────────────────────


def handle_regime_change(
    conn,
    new_regime: str,
    new_alt_context: str,
    btc_changed: bool,
    alt_changed: bool,
    features: dict,
) -> None:
    """
    Called after a confirmed debounced regime change on one or both axes.

    Posts a regime-change alert to REGIME_STATUS_CHANNEL_ID.
    The actual Close-Commands are generated by 28_signal_orchestrator.py
    (it polls regime_current and handles close logic itself).
    """
    # Import here to avoid circular dependency at module level
    from core.config import REGIME_STATUS_CHANNEL_ID
    from core.market_utils import send_telegram

    change_parts = []
    if btc_changed:
        change_parts.append(f"BTC Regime → <b>{new_regime}</b>")
    if alt_changed:
        change_parts.append(f"Alt Context → <b>{new_alt_context}</b>")

    btc_price = features.get("btc_price", 0)
    btc_ret_4h = features.get("btc_return_4h", 0.0)
    btcdom_val = features.get("btcdom_value")
    btcdom_ret = features.get("btcdom_return_24h")
    # Confidence separat anzeigen statt min(btc, alt) — der alte Code zeigte
    # 0% wenn eine der beiden Axes unsicher war, was alarmistisch wirkt
    # obwohl die betroffene Axis durchaus confident sein kann.
    conf_btc = features.get("confidence_btc", features.get("confidence", 0.0))
    conf_alt = features.get("confidence_alt", 0.0)

    btcdom_line = ""
    if btcdom_val is not None:
        # BTCDOM ist ein Punkt-Index (Base 1000), kein Prozentsatz.
        # Früher mit '%' suffixiert, das war irreführend.
        btcdom_str = f"{btcdom_val:.1f}"
        if btcdom_ret is not None:
            sign = "+" if btcdom_ret >= 0 else ""
            btcdom_str += f" ({sign}{btcdom_ret:.2f}% 24h)"
        btcdom_line = f"\nBTCDOM: {btcdom_str}"

    msg = (
        f"🔄 REGIME CHANGE\n\n"
        f"{chr(10).join(change_parts)}\n"
        f"Confidence: BTC {conf_btc * 100:.0f}% | Alt {conf_alt * 100:.0f}%\n"
        f"\nBTC: ${btc_price:,.0f} ({btc_ret_4h:+.2f}% in 4h)"
        f"{btcdom_line}\n"
        f"\n<i>Close commands will follow via trading channel if needed.</i>"
    )

    try:
        send_telegram(msg, REGIME_STATUS_CHANNEL_ID)
    except Exception as e:
        logger.error(f"Error sending des Regime-Change-Alerts: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# STÜNDLICHER STATUS-POST
# ─────────────────────────────────────────────────────────────────────────────


async def post_hourly_status() -> None:
    """Posts an hourly regime status to REGIME_STATUS_CHANNEL_ID."""
    from core.config import REGIME_STATUS_CHANNEL_ID
    from core.market_utils import send_telegram

    conn = None
    try:
        conn = get_db_connection()

        # Aktuelles Regime
        with conn.cursor() as cur:
            cur.execute(
                "SELECT regime, alt_context, since, alt_context_since, confidence FROM regime_current WHERE id = 1"
            )
            row = cur.fetchone()

        if row is None:
            send_telegram(
                "🌡️ REGIME STATUS\n\n⚠️ No regime computed yet.",
                REGIME_STATUS_CHANNEL_ID,
            )
            return

        cur_regime, cur_alt, since, alt_since, conf = row
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

        def _since_str(ts) -> str:
            if ts is None:
                return "unknown"
            delta = now_utc - ts
            h = int(delta.total_seconds() // 3600)
            m = int((delta.total_seconds() % 3600) // 60)
            return f"{ts.strftime('%Y-%m-%d %H:%M')} UTC ({h}h {m}min)"

        # Letzte 24h Regime-Verteilung
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT regime, COUNT(*) as n FROM regime_history
                WHERE ts >= NOW() AT TIME ZONE 'UTC' - INTERVAL '24 hours'
                GROUP BY regime ORDER BY n DESC
                """
            )
            dist_rows = cur.fetchall()

        total_checks = sum(r[1] for r in dist_rows) or 1
        dist_lines = "\n".join(f"  {r[0]:<12} {r[1] / total_checks * 100:.0f}%" for r in dist_rows)

        # Aktuelle BTC-Features
        features = compute_features(conn)
        btc_line = "  no data"
        atr_line = "  no data"
        btcdom_line = "  ---"
        if features:
            btc_line = (
                f"  ${features['btc_price']:>10,.0f} | "
                f"1h {features['btc_return_1h']:+.2f}% | "
                f"4h {features['btc_return_4h']:+.2f}%"
            )
            atr_line = f"  1h {features['btc_atr_1h_pct']:.2f}% | 4h {features['btc_atr_4h_pct']:.2f}%"
            if features.get("btcdom_value") is not None:
                # BTCDOM ist Punkt-Index (Base 1000), kein Prozentsatz.
                sign = "+" if (features.get("btcdom_return_24h") or 0) >= 0 else ""
                ret24h = features.get("btcdom_return_24h", 0.0) or 0.0
                btcdom_line = f"  {features['btcdom_value']:.1f} | 24h {sign}{ret24h:.2f}%"

        # Offene Orchestrator-Trades
        with conn.cursor() as cur:
            cur.execute(
                "SELECT coin, direction, bot_name FROM orchestrator_open_trades "
                "WHERE status = 'OPEN' ORDER BY opened_at DESC"
            )
            open_trades = cur.fetchall()

        open_count = len(open_trades)
        open_lines = "\n".join(f"  {t[0]} {t[1]} ({t[2]})" for t in open_trades[:8])
        if open_count > 8:
            open_lines += f"\n  ... and {open_count - 8} more"

        # Whitelist-Summary im aktuellen Regime
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT bot_name, direction, whitelisted FROM bot_regime_whitelist
                WHERE regime = %s AND alt_context = %s
                ORDER BY bot_name, direction
                """,
                (cur_regime, cur_alt),
            )
            wl_rows = cur.fetchall()

        wl_pass = [f"{r[0]} {r[1]}" for r in wl_rows if r[2]]
        wl_block = [f"{r[0]} {r[1]}" for r in wl_rows if not r[2]]
        wl_lines = ""
        if wl_pass:
            wl_lines += f"\n  ↑ {', '.join(wl_pass[:6])}"
        if wl_block:
            wl_lines += f"\n  ↓ {', '.join(wl_block[:6])}"
        if not wl_rows:
            wl_lines = "\n  (whitelist not yet computed)"

        # Gate-Qualität: wieviele Forwards der letzten 24h liefen über eine
        # echte 4D-Zelle, wieviele über default-open bzw. Fallback (B8).
        # wl_reason-Format: '4D-Grund' | 'no_whitelist_entry' |
        # '<status>:<fallback_reason>' (Fallback- und Staleness-Pfad).
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE wl_reason = 'no_whitelist_entry'),
                    COUNT(*) FILTER (WHERE POSITION(':' IN wl_reason) > 0),
                    COUNT(*) FILTER (WHERE wl_reason IS NOT NULL),
                    COUNT(*) FILTER (WHERE wl_reason IS NULL)
                FROM orchestrator_open_trades
                WHERE opened_at >= (NOW() AT TIME ZONE 'UTC')
                      - INTERVAL '%s hours'
                """,
                (GATE_STATS_LOOKBACK_HOURS,),
            )
            n_default_open, n_fallback, n_known, n_unknown = cur.fetchone()

        if n_known:
            pct_default_open = n_default_open / n_known * 100
            pct_fallback = n_fallback / n_known * 100
            pct_4d = 100 - pct_default_open - pct_fallback
            bypass_flag = "⚠️ " if pct_default_open + pct_fallback >= GATE_DEFAULT_OPEN_ALARM_PCT else ""
            gate_lines = (
                f"  {bypass_flag}default-open {pct_default_open:.0f}% | "
                f"fallback {pct_fallback:.0f}% | 4D {pct_4d:.0f}% ({n_known} forwards)"
            )
            if n_unknown:
                gate_lines += f"\n  ({n_unknown} pre-B8 forwards without reason)"
        else:
            gate_lines = f"  no forwards in {GATE_STATS_LOOKBACK_HOURS}h"

        ts_str = now_utc.strftime("%Y-%m-%d %H:%M")
        msg = (
            f"🌡️ REGIME STATUS — {ts_str} UTC\n\n"
            f"BTC Regime: <b>{cur_regime}</b> (conf {(conf or 0) * 100:.0f}%)\n"
            f"Since: {_since_str(since)}\n"
            f"Alt Context: <b>{cur_alt}</b>\n"
            f"Alt since: {_since_str(alt_since)}\n\n"
            f"BTC:\n{btc_line}\n"
            f"ATR:\n{atr_line}\n"
            f"BTCDOM:\n{btcdom_line}\n\n"
            f"Last 24h regime distribution:\n{dist_lines}\n\n"
            f"Whitelist ({cur_regime} × {cur_alt}):{wl_lines}\n\n"
            f"Gate path (last {GATE_STATS_LOOKBACK_HOURS}h):\n{gate_lines}\n\n"
            f"Open trades in trading channel: {open_count}\n"
            f"{open_lines}"
        )

        send_telegram(msg, REGIME_STATUS_CHANNEL_ID)
        logger.info(f"✅ Hourly status post sent ({cur_regime}/{cur_alt})")

    except Exception as e:
        logger.error(f"Error for Stunden-Status-Post: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# HAUPT-LOOP
# ─────────────────────────────────────────────────────────────────────────────


async def regime_check_loop() -> None:
    """
    Runs every 5 minutes. Computes features, classifies, writes history,
    applies debounce, and triggers change handler if needed.
    """
    conn = None
    try:
        conn = get_db_connection()
        ensure_regime_schema(conn)

        features = compute_features(conn)
        if features is None:
            logger.warning("Incomplete data — Regime-Check skipped")
            return

        # Regime-State EINMAL je Check lesen: liefert die Hysterese-Referenz
        # (§22 — bestehender ODER pendender TREND hält bis unter die
        # Exit-Schwelle) und wird unten an apply_debounce durchgereicht.
        state_row = read_regime_state(conn)
        prev_regime = hysteresis_prev_regime(state_row)

        result = classify_regime(features, features["vola_p75"], features["vola_p40"], prev_regime=prev_regime)
        logger.info(
            f"Regime-Check: BTC={result['regime']} (conf={result['confidence_btc']:.2f}) "
            f"Alt={result['alt_context']} (conf={result['confidence_alt']:.2f})"
        )

        insert_regime_history(conn, result, features)

        debounced = apply_debounce(
            conn,
            raw_regime=result["regime"],
            raw_alt_context=result["alt_context"],
            raw_confidence=result["confidence"],
            raw_ts=datetime.now(timezone.utc),
            state_row=state_row,
        )

        if debounced["btc_regime_changed"] or debounced["alt_context_changed"]:
            # Confidences aus classify_regime in features mergen damit die
            # Alert-Message beide Werte getrennt anzeigen kann (vorher zeigte
            # der Report min(btc, alt) → 0% wenn nur eine Axis unsicher war,
            # wirkte alarmistisch obwohl die andere Axis valide war).
            features_with_conf = dict(features)
            features_with_conf["confidence_btc"] = result["confidence_btc"]
            features_with_conf["confidence_alt"] = result["confidence_alt"]
            features_with_conf["confidence"] = result["confidence"]

            handle_regime_change(
                conn,
                new_regime=debounced["effective_regime"],
                new_alt_context=debounced["effective_alt_context"],
                btc_changed=debounced["btc_regime_changed"],
                alt_changed=debounced["alt_context_changed"],
                features=features_with_conf,
            )

    except Exception as e:
        logger.error(f"Regime-Check fehlgeschlagen: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()


async def schedule_regime_checks() -> None:
    """Aligns checks to 5-minute boundaries (XX:00, XX:05, ...)."""
    while True:
        now = datetime.now(timezone.utc)
        minutes_to_next = 5 - (now.minute % 5)
        next_run = (now + timedelta(minutes=minutes_to_next)).replace(second=0, microsecond=0)
        sleep_sec = (next_run - now).total_seconds()
        await asyncio.sleep(max(sleep_sec, 1))
        await regime_check_loop()
        await asyncio.sleep(1)  # Prevent double-run in same second


async def schedule_hourly_status() -> None:
    """Posts status at XX:00:50 every hour."""
    while True:
        now = datetime.now(timezone.utc)
        next_run = (now + timedelta(hours=1)).replace(minute=0, second=50, microsecond=0)
        sleep_sec = (next_run - now).total_seconds()
        await asyncio.sleep(max(sleep_sec, 1))
        await post_hourly_status()
        await asyncio.sleep(1)


async def main() -> None:
    logger.info("=== 🌐 REGIME DETECTOR STARTED ===")

    # Schema beim Start sicherstellen
    conn = None
    try:
        conn = get_db_connection()
        ensure_regime_schema(conn)
    finally:
        if conn:
            conn.close()

    # Ersten Check sofort, dann alle 5 Minuten
    await regime_check_loop()

    await asyncio.gather(
        schedule_regime_checks(),
        schedule_hourly_status(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Regime Detector manuell stopped (Strg+C).")
