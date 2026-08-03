# backtest/test_research_bots_live_price.py
"""DB-free tests for entry price anchor of research bots 30/31/32
(T-2026-KYT-9050-011, follow-up from block 5 / T-2026-CU-9050-112).

Since block 5 ``core.research_features.fetch_context_frame`` reads with
``include_forming=False`` — ``df["close"].iloc[-1]`` is thus the last CLOSED
1h candle and as entry anchor up to ~59 min stale. The three bots now fetch
the anchor via ``core.live_price.get_live_price`` (core.candles contract 2:
detection on closed candles, price separately).

Pinned is exactly what depends on the money path:
  1. Posted geometry AND prediction log are based on LIVE price, not the
     (deliberately set differently) last closed close of the context frame.
  2. If ``get_live_price`` returns None (Binance REST dead AND DB fallback empty),
     signal is SKIPPED — no post, no ``log_prediction`` with price=None.
  3. Cooldown semantics remain unchanged: 30/31 mirror the unconditional training
     dedup and set cooldown also on None path; 32 still sets it only on post path.
  4. Freshness guard of bot 32 (``fetch_context_frame`` → None) holds even though
     the frame no longer provides price there.
  5. Source pin: no bot takes the anchor again from the candle frame.

Run: pytest backtest/test_research_bots_live_price.py -v
     python backtest/test_research_bots_live_price.py
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import sys
import time

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# core.config requires secrets; build machine provides empty .env.
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

UTC = datetime.timezone.utc

LIVE_PRICE = 123.45  # what get_live_price returns
STALE_CLOSE = 999.0  # last CLOSED 1h close in context frame

BOT_FILES = ("30_ai_pex1_bot.py", "31_ai_fmr1_bot.py", "32_ai_trm1_bot.py")


def _import_bot(filename: str, module_name: str):
    """Numerically named bot modules are not importable — load by path.
    The module import is DB-free: ``load_artifact`` finds no pkl and returns
    the idle contract (pitfall 3)."""
    path = os.path.join(REPO_ROOT, filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


pex1 = _import_bot("30_ai_pex1_bot.py", "pex1_bot_under_test")
fmr1 = _import_bot("31_ai_fmr1_bot.py", "fmr1_bot_under_test")
trm1 = _import_bot("32_ai_trm1_bot.py", "trm1_bot_under_test")


# ── DB-free fakes ───────────────────────────────────────────────────────────
class _Cur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FakeConn:
    def cursor(self, *a, **k):
        return _Cur()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _Model:
    """predict_proba like sklearn/xgboost: 2D ndarray, one row."""

    def __init__(self, proba):
        self._proba = np.asarray([proba], dtype=float)

    def predict_proba(self, X):
        return self._proba


def _artifact(tag: str, proba, threshold: float = 0.5) -> dict:
    return {
        "loaded": True,
        "model": _Model(proba),
        "features": ["f"],
        "threshold": threshold,
        "calibrator": None,
        "tag": tag,
        "meta": {},
        "loaded_at": time.time(),
        "path": "",
        "default_tag": tag,
        "default_threshold": 1.0,
    }


def _ctx_frame(n: int = 40):
    """(df, idx) like fetch_context_frame — the last close is deliberately the
    STALE_CLOSE marker: if it shows up in the geometry, the anchor came from the
    candle frame instead of from get_live_price."""
    df = pd.DataFrame(
        {
            # naive like fetch_context_frame (tz_localize(None) after the read)
            "open_time": pd.date_range("2026-08-01", periods=n, freq="h"),
            "close": [STALE_CLOSE] * n,
            "volume": [1000.0] * n,
        }
    )
    return df, n - 1


class _Rec:
    def __init__(self):
        self.posts: list[dict] = []
        self.preds: list[tuple] = []
        self.cooldowns: list[tuple] = []
        self.shadows: list[tuple] = []


def _targets(conn, symbol, direction, live_price, **k):
    """Echo mock of calculate_smart_targets: geometry depends on anchor."""
    p = float(live_price)
    return {"entry1": p, "entry2": p, "sl": p * 1.05, "targets": [p * 0.98, p * 0.96]}


def _post(rec: _Rec):
    def f(conn, channel_id, tag, symbol, direction, conf, entry1, entry2, sl, targets, **k):
        rec.posts.append({"tag": tag, "symbol": symbol, "direction": direction, "entry1": entry1, "sl": sl})

    return f


def _pred(rec: _Rec):
    def f(conn, tag, symbol, direction, entry_price, confidence, posted=False, **k):
        rec.preds.append((tag, symbol, direction, entry_price, posted))

    return f


def _cooldown(rec: _Rec):
    def f(conn, module, symbol, direction, *a, **k):
        rec.cooldowns.append((module, symbol, direction))

    return f


# ── Bot 30 / PEX1 ────────────────────────────────────────────────────────────
def _event(symbol: str = "AAAUSDT") -> dict:
    now = datetime.datetime.now(UTC).replace(tzinfo=None)
    return {
        "symbol": symbol,
        "spike_time": now - datetime.timedelta(minutes=5),  # fresh (< 30 min)
        "volume_ratio": 7.0,
        "price_change_60s": 2.0,
        "buy_pressure": 0.8,
        "volatility": 0.01,
    }


def _wire_pex1(mp, price):
    rec = _Rec()
    mp.setattr(pex1, "ARTIFACT", _artifact("PEX1", [0.1, 0.9]))
    mp.setattr(pex1, "LIVE_POSTING", True)
    mp.setattr(pex1, "check_cooldown", lambda *a, **k: False)
    mp.setattr(pex1, "has_open_ai_signal", lambda *a, **k: False)
    mp.setattr(pex1, "fetch_context_frame", lambda *a, **k: _ctx_frame())
    mp.setattr(pex1, "build_pex1_row", lambda *a, **k: {"f": 1.0})
    mp.setattr(pex1, "get_live_price", lambda *a, **k: price)
    mp.setattr(pex1, "calculate_smart_targets", _targets)
    mp.setattr(pex1, "post_ai_signal", _post(rec))
    mp.setattr(pex1, "log_prediction", _pred(rec))
    mp.setattr(pex1, "update_cooldown", _cooldown(rec))
    return rec


def test_pex1_entry_anchor_is_the_live_price(monkeypatch):
    rec = _wire_pex1(monkeypatch, LIVE_PRICE)
    pex1.process_event(_FakeConn(), _event(), 0)

    assert len(rec.posts) == 1, "PEX1 posted no signal despite prob > threshold"
    assert rec.posts[0]["entry1"] == LIVE_PRICE, "Entry anchor does not come from get_live_price"
    assert rec.posts[0]["entry1"] != STALE_CLOSE, "Entry anchor is the closed 1h close (stale)"
    assert rec.preds == [("PEX1", "AAAUSDT", "SHORT", LIVE_PRICE, True)]
    assert rec.cooldowns == [("PEX1", "AAAUSDT", "SHORT")]


def test_pex1_skips_signal_when_live_price_is_none(monkeypatch):
    rec = _wire_pex1(monkeypatch, None)
    pex1.process_event(_FakeConn(), _event(), 0)

    assert rec.posts == [], "PEX1 posted without a live price"
    assert rec.preds == [], "PEX1 wrote a prediction log without an entry price"
    # Cooldown mirrors the unconditional 4h training dedup — it depends on
    # scoring, not posting, and thus remains unchanged.
    assert rec.cooldowns == [("PEX1", "AAAUSDT", "SHORT")]


# ── Bot 31 / FMR1 ────────────────────────────────────────────────────────────
def _wire_fmr1(mp, price):
    rec = _Rec()
    mp.setattr(fmr1, "ARTIFACT", _artifact("FMR1", [0.1, 0.9]))
    mp.setattr(fmr1, "LIVE_POSTING", True)
    mp.setattr(fmr1, "check_cooldown", lambda *a, **k: False)
    mp.setattr(fmr1, "has_open_ai_signal", lambda *a, **k: False)
    mp.setattr(fmr1, "fetch_funding_history", lambda *a, **k: [1e-4 * i for i in range(1, 21)])
    mp.setattr(fmr1, "fetch_context_frame", lambda *a, **k: _ctx_frame())
    mp.setattr(fmr1, "build_fmr1_row", lambda *a, **k: {"f": 1.0})
    mp.setattr(fmr1, "get_live_price", lambda *a, **k: price)
    mp.setattr(fmr1, "calculate_smart_targets", _targets)
    mp.setattr(fmr1, "post_ai_signal", _post(rec))
    mp.setattr(fmr1, "log_prediction", _pred(rec))
    mp.setattr(fmr1, "update_cooldown", _cooldown(rec))
    mp.setattr(
        fmr1,
        "_emit_fmr2_shadow",
        lambda conn, symbol, direction, feature_row, live_price: rec.shadows.append((symbol, direction, live_price)),
    )
    return rec


def test_fmr1_entry_anchor_is_the_live_price(monkeypatch):
    rec = _wire_fmr1(monkeypatch, LIVE_PRICE)
    fmr1.process_candidate(_FakeConn(), "BBBUSDT", "SHORT", 0.0012, 0.97)

    assert len(rec.posts) == 1, "FMR1 posted no signal despite prob > threshold"
    assert rec.posts[0]["entry1"] == LIVE_PRICE, "Entry anchor does not come from get_live_price"
    assert rec.posts[0]["entry1"] != STALE_CLOSE, "Entry anchor is the closed 1h close (stale)"
    assert rec.preds == [("FMR1", "BBBUSDT", "SHORT", LIVE_PRICE, True)]
    # FMR2 shadow leg depends on the SAME anchor.
    assert rec.shadows == [("BBBUSDT", "SHORT", LIVE_PRICE)]
    assert rec.cooldowns == [("FMR1", "BBBUSDT", "SHORT")]


def test_fmr1_skips_signal_and_shadow_when_live_price_is_none(monkeypatch):
    rec = _wire_fmr1(monkeypatch, None)
    fmr1.process_candidate(_FakeConn(), "BBBUSDT", "SHORT", 0.0012, 0.97)

    assert rec.posts == [], "FMR1 posted without a live price"
    assert rec.preds == [], "FMR1 wrote a prediction log without an entry price"
    assert rec.shadows == [], "FMR2 shadow leg ran without an entry price"
    assert rec.cooldowns == [("FMR1", "BBBUSDT", "SHORT")]  # 24h dedup mirror unchanged


# ── Bot 32 / TRM1 ────────────────────────────────────────────────────────────
def _wire_trm1(mp, price, ctx=_ctx_frame):
    rec = _Rec()
    mp.setattr(trm1, "ARTIFACT", _artifact("TRM1", [0.1, 0.8, 0.1]))  # class 1 = TREND_UP → LONG
    mp.setattr(trm1, "LIVE_POSTING", True)
    mp.setattr(trm1, "get_db_connection", lambda *a, **k: _FakeConn())
    mp.setattr(trm1, "fetch_regime_state", lambda conn: ("TRANSITION", 30.0))
    mp.setattr(trm1, "fetch_regime_window", lambda *a, **k: [{"regime": "TRANSITION"}, {"regime": "TRANSITION"}])
    mp.setattr(trm1, "build_trm1_row", lambda *a, **k: {"f": 1.0})
    mp.setattr(trm1, "check_cooldown", lambda *a, **k: False)
    mp.setattr(trm1, "has_open_ai_signal", lambda *a, **k: False)
    mp.setattr(trm1, "fetch_context_frame", lambda *a, **k: ctx() if ctx is not None else None)
    mp.setattr(trm1, "get_live_price", lambda *a, **k: price)
    mp.setattr(trm1, "calculate_smart_targets", _targets)
    mp.setattr(trm1, "post_ai_signal", _post(rec))
    mp.setattr(trm1, "log_prediction", _pred(rec))
    mp.setattr(trm1, "update_cooldown", _cooldown(rec))
    return rec


def test_trm1_entry_anchor_is_the_live_price(monkeypatch):
    rec = _wire_trm1(monkeypatch, LIVE_PRICE)
    trm1.run_check()

    assert len(rec.posts) == 1, "TRM1 posted no signal despite prob > threshold"
    assert rec.posts[0]["symbol"] == "BTCUSDT" and rec.posts[0]["direction"] == "LONG"
    assert rec.posts[0]["entry1"] == LIVE_PRICE, "Entry anchor does not come from get_live_price"
    assert rec.posts[0]["entry1"] != STALE_CLOSE, "Entry anchor is the closed 1h close (stale)"
    assert rec.preds == [("TRM1", "BTCUSDT", "LONG", LIVE_PRICE, True)]
    assert rec.cooldowns == [("TRM1", "BTCUSDT", "LONG")]


def test_trm1_skips_check_when_live_price_is_none(monkeypatch):
    rec = _wire_trm1(monkeypatch, None)
    trm1.run_check()

    assert rec.posts == [], "TRM1 posted without a live price"
    assert rec.preds == [], "TRM1 wrote a prediction log without an entry price"
    # Bot 32 sets cooldown by design ONLY on post path — unchanged.
    assert rec.cooldowns == []


def test_trm1_keeps_the_context_frame_freshness_guard(monkeypatch):
    """The frame no longer supplies a price, but the gate 'BTCUSDT-1h-join
    present and not staler than CONTEXT_MAX_STALENESS_H' still holds."""
    rec = _wire_trm1(monkeypatch, LIVE_PRICE, ctx=None)
    trm1.run_check()

    assert rec.posts == [] and rec.preds == [], "Freshness guard no longer applies (stale data → signal)"


# ── Quell-Pin ────────────────────────────────────────────────────────────────
def test_no_research_bot_anchors_on_the_closed_candle_frame():
    """Regression pin against rollback: no bot may take the entry anchor again
    from the (closed-only since block 5) context frame."""
    for filename in BOT_FILES:
        with open(os.path.join(REPO_ROOT, filename), encoding="utf-8") as fh:
            src = fh.read()
        assert 'live_price = float(df["close"]' not in src, f"{filename}: anchor taken again from the candle frame"
        assert "from core.live_price import get_live_price" in src, f"{filename}: get_live_price not imported"


if __name__ == "__main__":
    import traceback

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # cp1252 console (Windows)
    except Exception:
        pass
    from _pytest.monkeypatch import MonkeyPatch

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        mp = MonkeyPatch()
        try:
            if fn.__code__.co_argcount:
                fn(mp)
            else:
                fn()
            print(f"  ok  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
        finally:
            mp.undo()
    print(f"\n{len(fns) - failed}/{len(fns)} tests passed")
    sys.exit(1 if failed else 0)
