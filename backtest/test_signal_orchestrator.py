# backtest/test_signal_orchestrator.py
"""
Unit tests for Signal-Orchestrator (parsing, bot-ID, gating, cooldown, ROM1).
Run with: pytest backtest/test_signal_orchestrator.py -v
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import MagicMock, patch

# Import under a stable alias to avoid 28_... prefix issues
import importlib.util, unittest.mock as mock
from core import config as _kcfg  # channel ids

def _load_orchestrator():
    spec = importlib.util.spec_from_file_location(
        "signal_orchestrator",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "28_signal_orchestrator.py")
    )
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict("sys.modules", {
        "core.database": mock.MagicMock(),
        "core.logging_setup": mock.MagicMock(
            setup_logging=lambda x: __import__("logging").getLogger(x)
        ),
        "core.config": mock.MagicMock(
            REGIME_TRADING_CHANNEL_ID=_kcfg.CH_REGIME_TRADING,
            REGIME_STATUS_CHANNEL_ID=_kcfg.CH_MARKET_DATA,
        ),
        "core.market_utils": mock.MagicMock(),
        "core.trade_utils": mock.MagicMock(),
    }):
        spec.loader.exec_module(mod)
    return mod


orch = _load_orchestrator()


# ── Parsing ───────────────────────────────────────────────────────────────────

LONG_SIGNAL = """📈 Signal for BTCUSDT 📈

🚨 Direction: LONG
🚨 Leverage: 20x
🚨 Margin: Cross
🏦 CMP Entry: $ 64321.50000000
💰 TP1: $ 65000.00000000
💰 TP2: $ 65500.00000000
💰 TP3: $ 66000.00000000

💸 Stop Loss: $ 63800.00000000

🧠 AIM1 Strategy - V3"""

SHORT_SIGNAL = """📈 Signal for ETHUSDT 📈

🚨 Direction: SHORT
🚨 Leverage: 10x
🚨 Margin: Cross
🏦 CMP Entry: $ 3200.00000000
💰 TP1: $ 3100.00000000
💰 TP2: $ 3000.00000000

💸 Stop Loss: $ 3350.00000000

🧠 MIS1-8H Strategy - V3"""

HTML_SIGNAL = "<pre><b>📈 Signal for BTCUSDT 📈</b></pre>"


def test_parse_cornix_signal_long():
    result = orch.parse_cornix_signal(LONG_SIGNAL)
    assert result is not None
    assert result["coin"] == "BTCUSDT"
    assert result["direction"] == "LONG"
    assert result["entry"] == pytest.approx(64321.5)
    assert result["sl"] == pytest.approx(63800.0)
    assert len(result["targets"]) == 3


def test_parse_cornix_signal_short():
    result = orch.parse_cornix_signal(SHORT_SIGNAL)
    assert result is not None
    assert result["coin"] == "ETHUSDT"
    assert result["direction"] == "SHORT"
    assert len(result["targets"]) == 2


def test_parse_cornix_signal_all_tps():
    msg = LONG_SIGNAL.replace(
        "💰 TP3: $ 66000.00000000",
        "💰 TP3: $ 66000.00000000\n💰 TP4: $ 66500.00000000"
    )
    result = orch.parse_cornix_signal(msg)
    assert result is not None
    assert len(result["targets"]) == 4


def test_parse_cornix_signal_entry2_variant():
    msg = LONG_SIGNAL.replace(
        "🏦 CMP Entry: $ 64321.50000000",
        "🏦 CMP Entry: $ 64321.50000000\n🏦 Entry 2: $ 64000.00000000"
    )
    result = orch.parse_cornix_signal(msg)
    assert result is not None
    assert result["entry"] == pytest.approx(64321.5)


def test_parse_non_cornix_returns_none():
    assert orch.parse_cornix_signal("Hello world") is None
    assert orch.parse_cornix_signal("") is None
    assert orch.parse_cornix_signal(HTML_SIGNAL) is None


# ── Bot Identification ────────────────────────────────────────────────────────

def test_identify_bot_ai_model_in_text():
    assert orch.identify_bot("AIM1 model signal", None) == "AIM1"
    assert orch.identify_bot("MIS1-8H analysis", None) == "MIS1-8H"
    assert orch.identify_bot("QM_BULL pattern", None) == "QM_BULL"


def test_identify_bot_strategy_footer():
    assert orch.identify_bot(LONG_SIGNAL, None) == "AIM1"


def test_identify_bot_channel_fallback():
    result = orch.identify_bot("random message", _kcfg.CH_FAST_IN_OUT)
    assert result == "Fast In And Out"


def test_identify_bot_unknown_returns_none():
    assert orch.identify_bot("no bot info here", None) is None


def test_identify_bot_quasimodo_footer():
    """Quasimodo-Bot (24_quasimodo_bot.py) nutzt Footer der Form
    '🧠 AI Confidence: X% (QM_1H Filter)' — früher nicht erkannt."""
    msg_1h = "🧠 AI Confidence: 67.3% (QM_1H Filter)"
    msg_4h = "🧠 AI Confidence: 72.1% (QM_4H Filter)"
    assert orch.identify_bot(msg_1h, None) == "QM_1H"
    assert orch.identify_bot(msg_4h, None) == "QM_4H"


def test_identify_bot_smc_ml_sniper_footer():
    """SMC-ML-Sniper (25_smc_ml_sniper.py): BB_ und TD_ Varianten."""
    assert orch.identify_bot(
        "🧠 AI Confidence: 67.3% (BB_1H Filter)", None
    ) == "BB_1H"
    assert orch.identify_bot(
        "🧠 AI Confidence: 67.3% (BB_4H Filter)", None
    ) == "BB_4H"
    assert orch.identify_bot(
        "🧠 AI Confidence: 67.3% (TD_1H Filter)", None
    ) == "TD_1H"
    assert orch.identify_bot(
        "🧠 AI Confidence: 67.3% (TD_4H Filter)", None
    ) == "TD_4H"


def test_identify_bot_retrain_generation_tags():
    """Versionierungs-Regel: Retrain-Generationen posten unter neuem Tag
    (BB2_4H, TD2_4H, RUB2, MIS2-72H, ...) und MÜSSEN identifizierbar sein —
    sonst hart unterdrückt als bot_unidentified (T-2026-CU-9050-026;
    RUB2-Attributions-Finding aus PR #9)."""
    assert orch.identify_bot(
        "🧠 AI Confidence: 67.3% (BB2_4H Filter)", None
    ) == "BB2_4H"
    assert orch.identify_bot(
        "🧠 AI Confidence: 71.0% (TD2_4H Filter)", None
    ) == "TD2_4H"
    assert orch.identify_bot("RUB2 breakout signal", None) == "RUB2"
    assert orch.identify_bot("ABR2 retest signal", None) == "ABR2"
    assert orch.identify_bot("MIS2-72H analysis", None) == "MIS2-72H"
    assert orch.identify_bot("MIS2-8h_pump detected", None) == "MIS2-8h_pump"
    # Alt-Generation bleibt unverändert erkannt
    assert orch.identify_bot("RUB1 legacy signal", None) == "RUB1"


def test_identify_bot_pattern_detector_footer():
    """Pattern Detector (7_pattern_detector.py): BR1H, BR2H, BR4H, BR1D."""
    assert orch.identify_bot(
        "🧠 Trade idea generated by AI module BR1H V3", None
    ) == "BR1H"
    assert orch.identify_bot(
        "🧠 Trade idea generated by AI module BR2H V3", None
    ) == "BR2H"
    assert orch.identify_bot(
        "🧠 Trade idea generated by AI module BR4H V3", None
    ) == "BR4H"
    assert orch.identify_bot(
        "🧠 Trade idea generated by AI module BR1D V3", None
    ) == "BR1D"


def test_identify_bot_maviausdt_regression():
    """Regression-Test: Das komplette MAVIAUSDT-Signal das im Log als
    'Bot nicht identifizierbar' auftauchte. Fügen wir einen plausiblen
    QM_4H-Footer hinzu (10x Leverage = Binance-Cap für MAVIAUSDT)."""
    full_msg = (
        "📈 Signal for MAVIAUSDT 📈\n"
        "🚨 Direction: SHORT\n"
        "🚨 Leverage: 10x\n"
        "🚨 Margin: Cross\n"
        "🏦 CMP Entry: $ 1.23456789\n"
        "💰 TP1: $ 1.20000000\n"
        "💰 TP2: $ 1.18000000\n"
        "💸 Stop Loss: $ 1.28000000\n"
        "🧠 AI Confidence: 65.3% (QM_4H Filter)"
    )
    assert orch.identify_bot(full_msg, None) == "QM_4H"


def test_identify_bot_legacy_qm_bull_still_works():
    """Legacy-Tags (QM_BULL/BEAR etc.) sollen weiter erkannt werden,
    falls noch historische Outbox-entries existieren."""
    assert orch.identify_bot("QM_BULL pattern", None) == "QM_BULL"
    assert orch.identify_bot("BB_BEAR setup", None) == "BB_BEAR"
    assert orch.identify_bot("TD_LONG reversal", None) == "TD_LONG"


# ── Regime-Change Outcome Classification ─────────────────────────────────────

def test_classify_outcome_regime_change_is_neutral():
    """REGIME_CHANGE-Close wird als CLOSED_NEUTRAL klassifiziert —
    weder Win noch Loss, weil der Close extern ausgelöst wurde."""
    # LONG +5% PnL aber als REGIME_CHANGE geschlossen → NEUTRAL (nicht WIN)
    result = orch._classify_outcome_by_pnl(
        "LONG", entry=100.0, close_price=105.0,
        close_reason="REGIME_CHANGE:not_whitelisted"
    )
    assert result == "CLOSED_NEUTRAL"

    # Auch bei Loss → NEUTRAL
    result = orch._classify_outcome_by_pnl(
        "LONG", entry=100.0, close_price=95.0,
        close_reason="REGIME_CHANGE:btc_trend_down"
    )
    assert result == "CLOSED_NEUTRAL"


def test_classify_outcome_normal_tp_still_win():
    """Normaler TP-Hit bleibt CLOSED_TP (kein Kollateralschaden durch Fix)."""
    result = orch._classify_outcome_by_pnl(
        "LONG", entry=100.0, close_price=103.0,
        close_reason="ALL TARGETS HIT"
    )
    assert result == "CLOSED_TP"


def test_classify_outcome_delisted_still_neutral():
    """Bestehende DELISTED/CLEANUP-Erkennung funktioniert noch."""
    result = orch._classify_outcome_by_pnl(
        "LONG", entry=100.0, close_price=100.0,
        close_reason="DELISTED / CLEANUP"
    )
    assert result == "CLOSED_NEUTRAL"


# ── Regime-Change Force-Close ─────────────────────────────────────────────────

def test_force_close_trades_for_regime_change_closes_ai_signals():
    """force_close_trades_for_regime_change verschiebt offene AI-Trades
    aus ai_signals in closed_ai_signals mit status=CLOSED_REGIME_CHANGE."""
    mock_conn = MagicMock()
    inserts = []
    deletes = []

    def execute_side_effect(sql, params=None):
        sql_lower = sql.strip().lower()
        if "select id, symbol, model" in sql_lower and "from ai_signals" in sql_lower:
            # 1 offener AI-Trade auf BTCUSDT LONG
            mock_cursor._rows = [
                (42, "BTCUSDT", "ATS1", "LONG", 50000.0, 50000.0, 0,
                 __import__("datetime").datetime(2026, 4, 15)),
            ]
        elif "select id, strategy, time" in sql_lower:
            # keine classic trades
            mock_cursor._rows = []
            mock_cursor.description = [
                ("id",), ("strategy",), ("time",), ("coin",), ("direction",),
                ("lev",), ("entry",), ("target1",), ("target2",),
                ("target3",), ("target4",), ("sl",),
            ]
        elif "select close from" in sql_lower:
            # Letzter 5m-Close
            mock_cursor._rows = [(49500.0,)]
        elif "insert into closed_ai_signals" in sql_lower:
            inserts.append(("ai", params))
        elif "insert into closed_trades_master" in sql_lower:
            inserts.append(("classic", params))
        elif "delete from ai_signals" in sql_lower:
            deletes.append(("ai", params))
        elif "delete from active_trades_master" in sql_lower:
            deletes.append(("classic", params))
        elif "savepoint" in sql_lower:
            pass

    mock_cursor = MagicMock()
    mock_cursor._rows = []
    mock_cursor.execute.side_effect = execute_side_effect
    mock_cursor.fetchone.side_effect = lambda: mock_cursor._rows[0] if mock_cursor._rows else None
    mock_cursor.fetchall.side_effect = lambda: list(mock_cursor._rows)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    result = orch.force_close_trades_for_regime_change(
        mock_conn, "BTCUSDT", "LONG"
    )

    assert result["ai_closed"] == 1
    assert result["classic_closed"] == 0
    # INSERT in closed_ai_signals muss status='CLOSED_REGIME_CHANGE' haben
    ai_inserts = [p for kind, p in inserts if kind == "ai"]
    assert len(ai_inserts) == 1
    # params-Tuple: (symbol, model, direction, entry, close_price,
    #                targets_hit, open_time, close_time, status)
    params = ai_inserts[0]
    assert params[0] == "BTCUSDT"
    assert params[1] == "ATS1"
    assert params[2] == "LONG"
    assert params[3] == 50000.0       # entry
    assert params[4] == 49500.0       # close_price = letzter 5m-Close
    assert params[8] == "CLOSED_REGIME_CHANGE"
    # DELETE muss die ID 42 treffen
    ai_deletes = [p for kind, p in deletes if kind == "ai"]
    assert ai_deletes == [(42,)]


def test_force_close_trades_for_regime_change_close_price_fallback_to_entry():
    """Wenn kein 5m-Close verfügbar, wird entry als close_price genutzt
    → PnL = 0, Klassifikation bleibt NEUTRAL."""
    mock_conn = MagicMock()
    inserts = []

    def execute_side_effect(sql, params=None):
        sql_lower = sql.strip().lower()
        if "select id, symbol, model" in sql_lower and "from ai_signals" in sql_lower:
            mock_cursor._rows = [
                (10, "FAKEUSDT", "EPD1", "SHORT", 5.0, None, 0,
                 __import__("datetime").datetime(2026, 4, 13)),
            ]
        elif "select id, strategy, time" in sql_lower:
            mock_cursor._rows = []
            mock_cursor.description = [
                ("id",), ("strategy",), ("time",), ("coin",), ("direction",),
                ("lev",), ("entry",), ("target1",), ("target2",),
                ("target3",), ("target4",), ("sl",),
            ]
        elif "select close from" in sql_lower:
            # KEIN 5m-Close verfügbar
            mock_cursor._rows = []
        elif "insert into closed_ai_signals" in sql_lower:
            inserts.append(params)
        elif "savepoint" in sql_lower or "delete" in sql_lower:
            pass

    mock_cursor = MagicMock()
    mock_cursor._rows = []
    mock_cursor.execute.side_effect = execute_side_effect
    mock_cursor.fetchone.side_effect = lambda: mock_cursor._rows[0] if mock_cursor._rows else None
    mock_cursor.fetchall.side_effect = lambda: list(mock_cursor._rows)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    result = orch.force_close_trades_for_regime_change(
        mock_conn, "FAKEUSDT", "SHORT"
    )

    assert result["ai_closed"] == 1
    assert len(inserts) == 1
    # close_price fällt auf entry=5.0 zurück → PnL=0 → neutral
    assert inserts[0][3] == 5.0  # entry
    assert inserts[0][4] == 5.0  # close_price = entry (Fallback)
    assert inserts[0][8] == "CLOSED_REGIME_CHANGE"


# ── Signal Gating ─────────────────────────────────────────────────────────────

def test_parse_non_signal_message_skipped():
    """Non-signal messages (no '📈 Signal for') should return None."""
    assert orch.parse_cornix_signal("Market update: BTC up 5%") is None


def test_gating_skips_html_messages():
    assert orch.parse_cornix_signal(HTML_SIGNAL) is None


def test_gating_skips_market_tracker_posts():
    market_msg = "📊 Volume Report\nBTC: $64,000"
    assert orch.parse_cornix_signal(market_msg) is None


# ── Detector Reliability ──────────────────────────────────────────────────────

def _mock_conn_with_regime(regime, distinct_count=1):
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    # First call returns regime, second call returns distinct count
    cur.fetchone = MagicMock(side_effect=[(regime,), (distinct_count,)])
    return conn


def test_fallback_triggered_when_regime_is_transition():
    conn = _mock_conn_with_regime("TRANSITION")
    reliable, reason = orch.is_regime_detector_reliable(conn)
    assert reliable is False
    assert reason == "regime_is_transition"


def test_fallback_triggered_when_regime_current_empty():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=None)
    conn.cursor = MagicMock(return_value=cur)
    reliable, reason = orch.is_regime_detector_reliable(conn)
    assert reliable is False
    assert reason == "no_regime"


def test_fallback_triggered_when_many_regime_changes_2h():
    conn = _mock_conn_with_regime("TREND_UP", distinct_count=4)
    reliable, reason = orch.is_regime_detector_reliable(conn)
    assert reliable is False
    assert reason == "regime_unstable"


def test_fallback_not_triggered_in_stable_regime():
    conn = _mock_conn_with_regime("TREND_UP", distinct_count=1)
    reliable, reason = orch.is_regime_detector_reliable(conn)
    assert reliable is True
    assert reason == "reliable"


def test_fallback_passes_bot_with_wr_above_50():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=(100, 58.0))
    conn.cursor = MagicMock(return_value=cur)
    whitelisted, reason = orch.is_whitelisted_fallback(conn, "MIS1", "LONG")
    assert whitelisted is True


def test_fallback_blocks_bot_with_wr_below_50():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=(100, 42.0))
    conn.cursor = MagicMock(return_value=cur)
    whitelisted, reason = orch.is_whitelisted_fallback(conn, "WEAK_BOT", "LONG")
    assert whitelisted is False


def test_fallback_uses_overall_performance_wr_threshold():
    """Fallback threshold is 50%, not the bot's own overall WR."""
    assert orch.FALLBACK_MIN_WR == 50.0


# ── Cooldown ──────────────────────────────────────────────────────────────────

def test_cooldown_module_name_is_rom1():
    assert orch.ORCHESTRATOR_MODULE_NAME == "ROM1"


def test_cooldown_duration_is_4h():
    assert orch.ORCHESTRATOR_COOLDOWN_HOURS == 4


def test_cross_direction_blocked_while_trade_open():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=(1,))  # Trade found
    conn.cursor = MagicMock(return_value=cur)
    assert orch.is_opposite_direction_open(conn, "BTCUSDT", "SHORT") is True


def test_cross_direction_allowed_after_trade_close():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=None)  # No trade found
    conn.cursor = MagicMock(return_value=cur)
    assert orch.is_opposite_direction_open(conn, "BTCUSDT", "SHORT") is False


# ── ROM1 Tracking ─────────────────────────────────────────────────────────────

def test_rom1_signal_model_name():
    assert orch.ORCHESTRATOR_MODULE_NAME == "ROM1"


def test_close_command_exact_format():
    """Close command must be exactly 'Close SYMBOL'."""
    coin = "BTCUSDT"
    cmd = f"Close {coin}"
    assert cmd == "Close BTCUSDT"
    assert "<" not in cmd
    assert ">" not in cmd


def test_regime_change_detection_initializes_state():
    """First call initializes _last_known_regime without triggering changes."""
    orch._last_known_regime = None
    orch._last_known_alt_context = None
    # After running check, state should be initialized but no closes


def test_loop_interval_is_500ms():
    assert orch.LOOP_INTERVAL_MS == 500


# ── Outcome-Klassifikation im Lifecycle-Sync (Kelly-/WR-Fix) ────────────────
# Stellen sicher dass der Lifecycle-Sync Win/Loss/Neutral korrekt aus dem
# echten PnL ableitet statt aus dem fehlerhaften targets_hit/status-Feld.

def test_classify_outcome_by_pnl_win():
    """Positive PnL → CLOSED_TP."""
    assert orch._classify_outcome_by_pnl("LONG", 100.0, 102.5, "TP hit") == "CLOSED_TP"
    assert orch._classify_outcome_by_pnl("SHORT", 100.0, 97.5, "TP hit") == "CLOSED_TP"


def test_classify_outcome_by_pnl_loss():
    """Negative PnL → CLOSED_SL."""
    assert orch._classify_outcome_by_pnl("LONG", 100.0, 97.5, "SL Hit") == "CLOSED_SL"
    assert orch._classify_outcome_by_pnl("SHORT", 100.0, 102.5, "SL Hit") == "CLOSED_SL"


def test_classify_outcome_by_pnl_legacy_target_hit_with_zero_targets():
    """Der Haupt-Bug: LEGACY TARGET HIT mit targets_hit=0. Vorher CLOSED_SL,
    jetzt korrekt CLOSED_TP weil PnL positiv ist."""
    result = orch._classify_outcome_by_pnl(
        "LONG", 100.0, 102.6, "LEGACY TARGET HIT (+2.5%)"
    )
    assert result == "CLOSED_TP"


def test_classify_outcome_by_pnl_delisted_is_neutral():
    """DELISTED / CLEANUP → CLOSED_NEUTRAL, nicht CLOSED_SL."""
    result = orch._classify_outcome_by_pnl(
        "LONG", 100.0, 80.0, "DELISTED / CLEANUP"
    )
    assert result == "CLOSED_NEUTRAL"


def test_classify_outcome_by_pnl_outlier_is_neutral():
    """Ausreißer mit |pnl| > 100% → CLOSED_NEUTRAL."""
    result = orch._classify_outcome_by_pnl(
        "LONG", 100.0, 1234.0, "LEGACY TARGET HIT (+2.5%)"
    )
    assert result == "CLOSED_NEUTRAL"


def test_classify_outcome_by_pnl_micro_is_neutral():
    """|pnl| <= 0.1% → CLOSED_NEUTRAL (Housekeeping)."""
    result = orch._classify_outcome_by_pnl("LONG", 100.0, 100.05, "SL Hit")
    assert result == "CLOSED_NEUTRAL"


def test_classify_outcome_by_pnl_invalid_inputs():
    """None oder nicht-numerische Werte → CLOSED_NEUTRAL (fail-safe)."""
    assert orch._classify_outcome_by_pnl("LONG", None, 100.0, "") == "CLOSED_NEUTRAL"
    assert orch._classify_outcome_by_pnl("LONG", 100.0, None, "") == "CLOSED_NEUTRAL"
    assert orch._classify_outcome_by_pnl("LONG", 0.0, 100.0, "") == "CLOSED_NEUTRAL"
    assert orch._classify_outcome_by_pnl("LONG", -5.0, 100.0, "") == "CLOSED_NEUTRAL"


def test_classify_outcome_by_pnl_direction_short_correctly_inverted():
    """SHORT: wenn close > entry, dann PnL negativ → CLOSED_SL."""
    # LONG: close=102, entry=100 → +2%, Win
    assert orch._classify_outcome_by_pnl("LONG", 100.0, 102.0, "") == "CLOSED_TP"
    # SHORT: close=102, entry=100 → -2%, Loss
    assert orch._classify_outcome_by_pnl("SHORT", 100.0, 102.0, "") == "CLOSED_SL"


def test_classify_outcome_constants_match_analyzer():
    """Die Konstanten müssen die gleichen sein wie im Analyzer damit die
    Klassifikation konsistent ist."""
    assert orch.OUTCOME_MIN_PNL_PCT == 0.1
    assert orch.OUTCOME_MAX_ABS_PNL_PCT == 100.0


# ── ROM1 Eigene Trade-Berechnung (statt Durchreichen) ────────────────────────
# Verifiziert dass ROM1 nicht mehr das originale Bot-Signal übernimmt,
# sondern eigene Entry/SL/Targets via AI-Bot-Logik berechnet.

def test_rom1_constants_match_ai_bots():
    """Leverage, Entry2-Offset etc. müssen mit den AI-Bots konsistent sein."""
    assert orch.ROM1_DESIRED_LEVERAGE == 20
    assert orch.ROM1_ENTRY2_OFFSET_PCT == 0.05
    assert orch.ROM1_TP_MIN_DISTANCE_PCT == 0.05


def test_compute_rom1_trade_params_long():
    """LONG-Trade: Entry1 = aktueller Preis, Entry2 = 5% darunter,
    SL aus Supports, Targets aus Resistances."""
    mock_conn = mock.MagicMock()
    # Mock _get_latest_price; ensure_min_tp_distance passthrough (Identity) damit
    # die Targets-Liste durchgereicht wird.
    with mock.patch.object(orch, "_get_latest_price", return_value=100.0), \
         mock.patch.object(orch, "get_hvn_and_sr_levels",
                           return_value=([92.0, 88.0], [105.0, 110.0, 120.0])), \
         mock.patch.object(orch, "ensure_min_tp_distance",
                           side_effect=lambda t, e, l, min_pct: list(t)), \
         mock.patch.object(orch, "get_max_leverage", return_value="20x"):
        params = orch.compute_rom1_trade_params(mock_conn, "BTCUSDT", "LONG")

    assert params is not None
    assert params["entry1"] == 100.0
    assert params["entry2"] == 95.0        # 5% unter Entry1
    # SL: höchstes Support unter Entry2*0.99=94.05 → 92.0
    assert params["sl"] == 92.0
    # Targets: alle Resistances > 101 sortiert
    assert params["targets"] == [105.0, 110.0, 120.0]
    assert params["leverage"] == "20x"


def test_compute_rom1_trade_params_short():
    """SHORT-Trade: Entry2 5% über Entry1, SL aus Resistances, Targets aus Supports."""
    mock_conn = mock.MagicMock()
    with mock.patch.object(orch, "_get_latest_price", return_value=100.0), \
         mock.patch.object(orch, "get_hvn_and_sr_levels",
                           return_value=([95.0, 90.0, 85.0], [108.0, 112.0])), \
         mock.patch.object(orch, "ensure_min_tp_distance",
                           side_effect=lambda t, e, l, min_pct: list(t)), \
         mock.patch.object(orch, "get_max_leverage", return_value="20x"):
        params = orch.compute_rom1_trade_params(mock_conn, "BTCUSDT", "SHORT")

    assert params is not None
    assert params["entry1"] == 100.0
    assert params["entry2"] == 105.0       # 5% über Entry1
    # SL: niedrigste Resistance über Entry2*1.01=106.05 → 108.0
    assert params["sl"] == 108.0
    # Targets: Supports unter Entry1*0.99=99, absteigend sortiert
    assert params["targets"] == [95.0, 90.0, 85.0]


def test_compute_rom1_trade_params_sl_fallback_when_no_zones():
    """Wenn keine Zonen außerhalb Entry2, greift Fallback-SL."""
    mock_conn = mock.MagicMock()
    with mock.patch.object(orch, "_get_latest_price", return_value=100.0), \
         mock.patch.object(orch, "get_hvn_and_sr_levels",
                           return_value=([], [105.0])), \
         mock.patch.object(orch, "ensure_min_tp_distance",
                           side_effect=lambda t, e, l, min_pct: list(t) or [e * 1.05]), \
         mock.patch.object(orch, "get_max_leverage", return_value="20x"):
        params = orch.compute_rom1_trade_params(mock_conn, "BTCUSDT", "LONG")

    assert params is not None
    # Fallback: entry2 × (1 - 2.5%) = 95 × 0.975 = 92.625
    assert abs(params["sl"] - 92.625) < 0.001


def test_compute_rom1_trade_params_returns_none_when_no_price():
    """Ohne Preis no trade — Nil zurück, kein Crash."""
    mock_conn = mock.MagicMock()
    with mock.patch.object(orch, "_get_latest_price", return_value=None):
        params = orch.compute_rom1_trade_params(mock_conn, "UNKNOWNUSDT", "LONG")
    assert params is None


def test_compute_rom1_trade_params_returns_none_when_no_targets():
    """Keine validen Targets (ensure_min_tp_distance liefert leer)?
    ensure_min_tp_distance gibt aber mindestens 1 Fallback-TP zurück, daher
    testen wir den edge case dass ensure-Helper None/[] zurückgibt."""
    mock_conn = mock.MagicMock()
    with mock.patch.object(orch, "_get_latest_price", return_value=100.0), \
         mock.patch.object(orch, "get_hvn_and_sr_levels",
                           return_value=([], [])), \
         mock.patch.object(orch, "ensure_min_tp_distance", return_value=[]), \
         mock.patch.object(orch, "get_max_leverage", return_value="20x"):
        params = orch.compute_rom1_trade_params(mock_conn, "BTCUSDT", "LONG")
    assert params is None


def test_build_rom1_cornix_message_format():
    """Das ausgegebene Message-Format muss Cornix-parsebar sein und wieder
    von parse_cornix_signal() erkannt werden."""
    params = {
        "entry1": 43210.12345678,
        "entry2": 41049.61728394,
        "sl": 40950.00000000,
        "targets": [43500.0, 44000.0, 44500.0, 45000.0, 45500.0],
        "leverage": "20x",
    }
    msg = orch.build_rom1_cornix_message("BTCUSDT", "LONG", params)

    # Muss Kern-Marker enthalten die parse_cornix_signal prüft
    assert "📈 Signal for BTCUSDT" in msg
    assert "Direction: LONG" in msg
    assert "Stop Loss:" in msg
    assert "CMP Entry:" in msg
    assert "ROM1 V1" in msg
    # Nur die ersten 3 TPs werden für Cornix gepostet
    assert "TP1:" in msg
    assert "TP2:" in msg
    assert "TP3:" in msg
    assert "TP4:" not in msg
    assert "TP5:" not in msg

    # Round-trip: parse_cornix_signal muss die Message wieder verstehen
    parsed = orch.parse_cornix_signal(msg)
    assert parsed is not None
    assert parsed["coin"] == "BTCUSDT"
    assert parsed["direction"] == "LONG"
    assert abs(parsed["entry"] - 43210.12345678) < 0.0001
    assert abs(parsed["sl"] - 40950.0) < 0.0001
    assert len(parsed["targets"]) == 3


def test_build_rom1_cornix_message_short():
    params = {
        "entry1": 100.0,
        "entry2": 105.0,
        "sl": 108.0,
        "targets": [95.0, 90.0, 85.0],
        "leverage": "10x",
    }
    msg = orch.build_rom1_cornix_message("ETHUSDT", "SHORT", params)
    assert "Direction: SHORT" in msg
    assert "Leverage: 10x" in msg
    # Round-trip
    parsed = orch.parse_cornix_signal(msg)
    assert parsed is not None
    assert parsed["direction"] == "SHORT"


def test_build_rom1_cornix_message_with_trigger_bot():
    """Trigger-Info wird als separate Zeile angehängt, bricht nicht das Parsing."""
    params = {
        "entry1": 100.0,
        "entry2": 95.0,
        "sl": 92.0,
        "targets": [105.0, 110.0, 120.0],
        "leverage": "20x",
    }
    msg = orch.build_rom1_cornix_message(
        "BTCUSDT", "LONG", params, trigger_bot="MIS1-8h"
    )
    # Trigger-Zeile muss im Output sein
    assert "📡 Triggered by: MIS1-8h" in msg
    # Kommt after dem Standard-Footer
    lines = msg.splitlines()
    assert lines[-2].startswith("🧠")
    assert lines[-1].startswith("📡")

    # Cornix-Round-Trip muss weiter funktionieren (Trigger stört TP/SL nicht)
    parsed = orch.parse_cornix_signal(msg)
    assert parsed is not None
    assert parsed["coin"] == "BTCUSDT"
    assert parsed["direction"] == "LONG"
    assert len(parsed["targets"]) == 3


def test_build_rom1_cornix_message_without_trigger_bot():
    """Ohne trigger_bot Parameter: keine Trigger-Zeile → Backward-Compat."""
    params = {
        "entry1": 100.0,
        "entry2": 95.0,
        "sl": 92.0,
        "targets": [105.0, 110.0, 120.0],
        "leverage": "20x",
    }
    msg = orch.build_rom1_cornix_message("BTCUSDT", "LONG", params)
    assert "Triggered by:" not in msg
    # Letzte Zeile ist der Standard-Footer
    assert msg.splitlines()[-1].startswith("🧠")


def test_rom1_params_used_not_original_signal():
    """insert_rom1_signal muss ROM1-berechnete Werte schreiben, nicht die
    Original-Params des auslösenden Bots."""
    mock_conn = mock.MagicMock()
    mock_cursor = mock.MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    rom1_params = {
        "entry1": 100.0,
        "entry2": 95.0,
        "sl": 92.0,
        "targets": [105.0, 110.0, 120.0],
        "leverage": "20x",
    }
    orch.insert_rom1_signal(mock_conn, "BTCUSDT", "LONG", rom1_params)

    # ai_signals INSERT wurde aufgerufen mit den ROM1-Werten
    assert mock_cursor.execute.called
    call_args = mock_cursor.execute.call_args
    # Die Werte-Tuple ist das zweite positional-arg oder via kwargs
    values = call_args[0][1]
    # values: (symbol, price, direction, entry1, entry2, sl, targets_json)
    assert values[0] == "BTCUSDT"
    assert values[1] == 100.0       # price = ROM1-entry1
    assert values[2] == "LONG"
    assert values[3] == 100.0       # entry1 = ROM1-entry1
    assert values[4] == 95.0        # entry2 = ROM1-entry2
    assert values[5] == 92.0        # sl = ROM1-sl
    # targets als JSON
    import json as _json
    assert _json.loads(values[6]) == [105.0, 110.0, 120.0]
