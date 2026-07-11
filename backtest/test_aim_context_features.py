"""Standalone (DB-free) guards für die AIM2-Serving-Fixes aus P2.35
(T-2026-CU-9050-090):

  (a) Kandidaten-Fenster ist catch-up-fähig (CANDIDATE_WINDOW_MIN) UND durch eine
      persistente processed-Tabelle gegen Doppel-Processing abgesichert.
  (b) Schwarm-/Kontext-Aggregate zählen den Kandidaten selbst NICHT mit
      (strikt < ts). Die AIM1/AIM2/AIM2-TOPN-Selbstexklusion liegt auf
      Stream-Ebene und wird von test_aim2_event_source_symmetry gepinnt.
  (c) Der conv-Dedup-Key ist tabellen-AGNOSTISCH: ein conv-Signal wandert binnen
      Sekunden von active_ nach closed_trades_master mit NEUER Serial-id bei
      unveränderter Open-`time` (5_trade_monitor.close_trade). Der per-Tabelle
      `id` taugt deshalb nicht als Dedup-Schlüssel — sonst würde die closed-Form
      als frischer Kandidat re-gescored (Doppel-Post) und unbeteiligte
      active/closed-Rows mit gleicher id verdrängten sich gegenseitig (die
      ursprüngliche P2.35-Kollision). Diagnose wie 33_ai_fif1_bot.signal_key.

Regel 7 (Trainer==Serving Feature-Parität) ist hier NICHT berührt: der
Dedup-Key steuert nur, WELCHE Kandidaten das Serving scored/postet — er ist kein
Modell-Input-Feature, und der Trainer (aim2_build_dataset.py) dedupliziert gar
nicht. Die Kontext-Selbstexklusion (b) ist auf beiden Seiten bereits identisch
implementiert (strikt < ts), es fällt daher KEINE Retrain-Kopplung an.

Der Bot heißt `15_ai_master_bot.py` (Ziffern-Präfix → nicht importierbar); wir
laden ihn per importlib. `core.config` validiert zwei Pflicht-Env-Vars beim
Import — hier mit Dummies gesetzt, es wird nie eine DB-Verbindung geöffnet.

Run: python backtest/test_aim_context_features.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pandas as pd  # vor dem Modul-Load importieren (Loader-Konvention)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_PASSWORD", "test-stub")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-stub")

_spec = importlib.util.spec_from_file_location("ai_master_bot", str(ROOT / "15_ai_master_bot.py"))
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

TS = pd.Timestamp("2026-07-11T10:00:00")
BIGINT_MAX = (1 << 63) - 1


# ── (c) conv-Dedup-Key: tabellen-agnostisch ──────────────────────────────────
def test_conv_same_signal_survives_active_to_closed_migration():
    """Gleiches Signal, active(id=500) → closed(id=777): NEUE id, gleiche
    Identität → identischer Key (sonst Doppel-Post der closed-Form)."""
    active = bot.dedup_key("conv", 500, "Fast In And Out", "BTCUSDT", "LONG", TS, 65000.0)
    closed = bot.dedup_key("conv", 777, "Fast In And Out", "BTCUSDT", "LONG", TS, 65000.0)
    assert active == closed, "conv-Key hängt an der per-Tabelle id → active/closed re-gescored (Doppel-Post)"
    assert active[0] == "conv"


def test_conv_unrelated_rows_sharing_a_per_table_id_are_distinct():
    """Die eigentliche P2.35-Kollision: zwei UNBETEILIGTE conv-Signale mit
    zufällig gleicher per-Tabelle id dürfen sich nicht deduplizieren."""
    a = bot.dedup_key("conv", 500, "Fast In And Out", "BTCUSDT", "LONG", TS, 65000.0)
    b = bot.dedup_key("conv", 500, "Volume Indicator", "ETHUSDT", "SHORT", TS, 3000.0)
    assert a != b, "unbeteiligte conv-Rows mit gleicher id kollidieren → eines wird still verworfen"


def test_conv_identity_separates_coin_direction_time_source():
    base = dict(source="Volume Indicator", symbol="ETHUSDT", direction="LONG", ts=TS, entry=3000.0)
    k = bot.conv_signal_identity(**base)
    assert k != bot.conv_signal_identity(**{**base, "symbol": "BTCUSDT"})
    assert k != bot.conv_signal_identity(**{**base, "direction": "SHORT"})
    assert k != bot.conv_signal_identity(**{**base, "ts": TS + pd.Timedelta(hours=1)})
    assert k != bot.conv_signal_identity(**{**base, "source": "Fast In And Out"})
    assert k != bot.conv_signal_identity(**{**base, "entry": 3000.5})


def test_conv_identity_is_bigint_safe_and_deterministic():
    k = bot.conv_signal_identity("Fast In And Out", "BTCUSDT", "LONG", TS, 65000.0)
    assert 0 <= k <= BIGINT_MAX, "signal_id muss in den signierten 64-bit-Raum (BIGINT) passen"
    assert k == bot.conv_signal_identity("Fast In And Out", "btcusdt", "long", TS, 65000.0), "muss case-stabil sein"


def test_ai_key_keeps_the_stable_prediction_id():
    """ai: ml_predictions_master.id ist stabil (posted-Rows migrieren nie) →
    direkte id, getrennter Namensraum von conv."""
    assert bot.dedup_key("ai", 123, "MIS1-24H", "BTCUSDT", "LONG", TS, 65000.0) == ("ai_signal", 123)


def test_ai_and_conv_namespaces_do_not_collide():
    ai = bot.dedup_key("ai", 500, "MIS1-24H", "BTCUSDT", "LONG", TS, 65000.0)
    conv = bot.dedup_key("conv", 500, "Fast In And Out", "BTCUSDT", "LONG", TS, 65000.0)
    assert ai[0] != conv[0]


# ── (b) Kontext/Schwarm: Kandidat zählt sich nicht selbst ─────────────────────
def _stream(rows):
    return pd.DataFrame(rows, columns=["symbol", "ts", "direction", "source"])


def test_swarm_excludes_the_candidate_itself():
    """Eine Row exakt bei ts (der Kandidat) darf NICHT in die 5d-Aggregate."""
    s = _stream(
        [
            ("BTCUSDT", TS, "LONG", "CANDIDATE"),               # der Kandidat selbst
            ("BTCUSDT", TS - pd.Timedelta(hours=1), "LONG", "X"),
            ("BTCUSDT", TS - pd.Timedelta(hours=2), "SHORT", "Y"),
        ]
    )
    out = bot.swarm_stats(s, "BTCUSDT", TS, "LONG")
    assert out["total_5d"] == 2, "Kandidat (ts == ts) wurde mitgezählt — Selbstzählung"
    assert out["long_5d"] == 1 and out["short_5d"] == 1


def test_swarm_confluence_counts_only_prior_same_direction():
    s = _stream(
        [
            ("BTCUSDT", TS, "LONG", "CANDIDATE"),
            ("BTCUSDT", TS - pd.Timedelta(hours=1), "LONG", "X"),
            ("BTCUSDT", TS - pd.Timedelta(hours=3), "LONG", "Z"),
            ("BTCUSDT", TS - pd.Timedelta(hours=6), "LONG", "OLD"),  # >4h → nicht in Konfluenz
        ]
    )
    out = bot.swarm_stats(s, "BTCUSDT", TS, "LONG")
    assert out["confl_same_dir_4h"] == 2
    assert out["distinct_src_same_dir_4h"] == 2


# ── (a) Fenster + processed-Tabelle ───────────────────────────────────────────
def test_candidate_window_is_catch_up_sized():
    assert bot.CANDIDATE_WINDOW_MIN >= 60, "Catch-up-Fenster nach Downtime (P2.35)"


def test_processed_dedup_table_exists_and_is_keyed_on_signal_identity():
    src = (ROOT / "15_ai_master_bot.py").read_text(encoding="utf-8")
    assert "master_ai_processed_signals" in src
    assert "PRIMARY KEY (signal_type, signal_id)" in src
    # der Insert muss den agnostischen Key aus dem Kandidaten nehmen, nicht die roh-id
    assert "signal.dkey" in src


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
