# tools/regime_rules_study.py
"""Detector-Rework-Studie (MODEL_INTENT §22, T-2026-CU-9050-016 Folge):
Regelvarianten für classify_btc_regime über die volle BTCUSDT_15m-Historie
nachspielen und zweifach bewerten:

  1. STRUKTUR — kommt TREND überhaupt vor, und wie stabil? (Episodenzahl,
     Mediandauer, Flap-Quote <1h, Zeitanteile je Regime). Step-6-Befund:
     die Ist-Regel verlangt ATR<P40 UND |ret_4h|>1,5 % — ein 1,5 %-Move
     hebt ATR aber fast immer über P40 → 7 TREND-Episoden in 5,5 Monaten.
  2. ÖKONOMIE — trennt der Zustand die Monatsergebnisse der bekannten
     regimeabhängigen LONG-Setups? Overlay der Replay-Events (RUB LONG,
     ABR1 LONG) auf den nachgespielten Zustand: Ø-PnL je Regime.
     (Das ist der Use-Case: Regime-Gate statt Event-Gate, §8.)

Varianten:
  V0            — Ist-Zustand (core/regime_logic.classify_btc_regime).
  V1_fix_<X>    — Mid-Band (P40..P75) bekommt eigene Trend-Regel:
                  |ret_4h| ≥ X  (X aus Grid) → TREND_UP/DOWN, sonst TRANSITION.
  V2_atr_<K>    — Mid-Band vol-skaliert: |ret_4h| ≥ K × atr_4h_pct.

Alle Varianten lassen Low-Vola-Regeln (TREND/CHOP) und HIGH_VOLA unverändert —
der Operator-Auftrag betrifft NUR die TRANSITION-Restklasse.

Debounce-Näherung: Zustandswechsel zählt erst nach 2 aufeinanderfolgenden
15m-Bars (~ live: 2×5-min-Checks). Identisch über alle Varianten → fairer
Vergleich; Absolutwerte sind Näherung.

Read-only; Ergebnis-JSON nach staging_models.
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import timedelta  # noqa: E402

from core.candles import read_candles  # noqa: E402
from core.database import get_db_connection  # noqa: E402
from core.regime_logic import (  # noqa: E402
    CHOP_RETURN_THRESHOLD_4H_PCT,
    TREND_RETURN_THRESHOLD_4H_PCT,
    VOLA_LOOKBACK_DAYS,
)
from core.time import utc_now  # noqa: E402

from tools.research_dataset_common import (  # noqa: E402
    REPLAY_DIR,
    STAGING_DIR,
    log,
)

DAYS = 430
DEBOUNCE_BARS = 2  # ≈ 2 Checks à 5 min live
BARS_PER_DAY = 96  # 15m
FIX_GRID = (1.5, 2.0, 2.5)
ATR_GRID = (0.75, 1.0, 1.5)


def load_btc(conn) -> pd.DataFrame:
    # Über core.candles: GESCHLOSSENE 15m-Kerzen, ASC. Die Regime-Studie darf
    # nicht auf der forming 15m-Kerze rechnen (dieselbe R1-Disziplin, die live
    # core/regime_logic bekommt) — include_forming=False schneidet DB-seitig.
    df = read_candles(
        conn,
        "BTCUSDT",
        "15m",
        start=utc_now() - timedelta(days=DAYS + VOLA_LOOKBACK_DAYS + 2),
        include_forming=False,
        columns=("open_time", "high", "low", "close"),
    )
    # timestamptz über DST-Grenzen ⇒ gemischte Offsets ⇒ utc=True Pflicht
    # (gleiche Falle wie spike_time_to_utc, Fix f95f092); Events sind naive UTC.
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True).dt.tz_localize(None)
    return df.reset_index(drop=True)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Vektorisierte Replikation von core/regime_logic.compute_features."""
    close, high, low = df["close"].astype(float), df["high"].astype(float), df["low"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr_4h = tr.ewm(span=16, adjust=False).mean()

    out = pd.DataFrame(
        {
            # Zustand auf den BAR-CLOSE stempeln (open_time + 15m): ret_4h/ATR
            # dieser Zeile sind erst mit dem Close bekannt — mit open_time hätte
            # der as-of-Merge in economics() bis zu 15 min Lookahead und die
            # Regime-Ökonomie wäre optimistisch verzerrt (Review PR #9).
            "ts": df["open_time"] + pd.Timedelta(minutes=15),
            "ret_4h": close.pct_change(16) * 100,
            "atr_4h_pct": atr_4h / close * 100,
        }
    )
    win = VOLA_LOOKBACK_DAYS * BARS_PER_DAY
    vola = out["atr_4h_pct"]
    out["p75"] = vola.rolling(win, min_periods=win // 2).quantile(0.75)
    out["p40"] = vola.rolling(win, min_periods=win // 2).quantile(0.40)
    return out.dropna().reset_index(drop=True)


def classify(feat: pd.DataFrame, variant: str, param: float | None) -> np.ndarray:
    """Regelwerk je Bar. Low-Vola- und HIGH_VOLA-Zweige == Ist-Zustand."""
    ret, atr, p75, p40 = (feat[c].values for c in ("ret_4h", "atr_4h_pct", "p75", "p40"))
    regime = np.full(len(feat), "TRANSITION", dtype=object)

    high_vola = atr > p75
    low_vola = atr < p40
    mid = ~high_vola & ~low_vola

    regime[high_vola] = "HIGH_VOLA"
    regime[low_vola & (ret > TREND_RETURN_THRESHOLD_4H_PCT)] = "TREND_UP"
    regime[low_vola & (ret < -TREND_RETURN_THRESHOLD_4H_PCT)] = "TREND_DOWN"
    regime[low_vola & (np.abs(ret) < CHOP_RETURN_THRESHOLD_4H_PCT)] = "CHOP"

    if variant == "V1_fix":
        regime[mid & (ret >= param)] = "TREND_UP"
        regime[mid & (ret <= -param)] = "TREND_DOWN"
    elif variant == "V2_atr":
        regime[mid & (ret >= param * atr)] = "TREND_UP"
        regime[mid & (ret <= -param * atr)] = "TREND_DOWN"
    # V0: mid bleibt TRANSITION
    return regime


def debounce(raw: np.ndarray) -> np.ndarray:
    """Wechsel erst nach DEBOUNCE_BARS gleichen Raw-Werten wirksam."""
    eff = np.empty_like(raw)
    cur = raw[0]
    pend, pend_n = None, 0
    for i, r in enumerate(raw):
        if r == cur:
            pend, pend_n = None, 0
        elif r == pend:
            pend_n += 1
            if pend_n >= DEBOUNCE_BARS:
                cur, pend, pend_n = r, None, 0
        else:
            pend, pend_n = r, 1
        eff[i] = cur
    return eff


def episode_stats(eff: np.ndarray, ts: pd.Series) -> dict:
    """Zeitanteile, Episodenzahl, Mediandauer (h), Flap-Quote (<1h) je Regime."""
    df = pd.DataFrame({"regime": eff, "ts": ts.values})
    df["ep"] = (df["regime"] != df["regime"].shift(1)).cumsum()
    eps = df.groupby("ep").agg(regime=("regime", "first"), n=("regime", "size"))
    eps["hours"] = eps["n"] * 0.25
    share = df["regime"].value_counts(normalize=True) * 100

    out = {}
    for reg in ("TREND_UP", "TREND_DOWN", "CHOP", "HIGH_VOLA", "TRANSITION"):
        e = eps[eps["regime"] == reg]
        out[reg] = {
            "share_pct": round(float(share.get(reg, 0.0)), 1),
            "episodes": int(len(e)),
            "median_h": round(float(e["hours"].median()), 1) if len(e) else None,
            "flap_lt1h_pct": round(float((e["hours"] < 1).mean() * 100), 0) if len(e) else None,
        }
    return out


def load_replay_events(path: str, direction: str) -> pd.DataFrame | None:
    if not os.path.exists(path):
        return None
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            if t.get("direction") != direction or t.get("net_pnl_pct") is None:
                continue
            rows.append({"ts": t["signal_time"], "pnl": float(t["net_pnl_pct"])})
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], format="mixed", utc=True).dt.tz_localize(None)
    return df.sort_values("ts").reset_index(drop=True)


def economics(eff: np.ndarray, ts: pd.Series, events: pd.DataFrame) -> dict:
    """Ø-PnL der Replay-Events je nachgespieltem Regime-Zustand (as-of merge)."""
    state = pd.DataFrame({"ts": ts.values, "regime": eff})
    merged = pd.merge_asof(events, state, on="ts", direction="backward")
    g = merged.groupby("regime")["pnl"].agg(["size", "mean", "median"])
    return {
        reg: {"n": int(r["size"]), "avg_pnl": round(float(r["mean"]), 2), "med_pnl": round(float(r["median"]), 2)}
        for reg, r in g.iterrows()
    }


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    conn = get_db_connection()
    log("Lade BTCUSDT_15m …")
    btc = load_btc(conn)
    conn.close()
    log(f"{len(btc)} Bars geladen ({btc['open_time'].min()} → {btc['open_time'].max()})")

    feat = build_features(btc)
    log(f"Features: {len(feat)} Bars nach Warmup")

    rub = load_replay_events(os.path.join(REPLAY_DIR, "rub_replay_365d.jsonl"), "LONG")
    abr = load_replay_events(os.path.join(REPLAY_DIR, "detector_fix", "abr1_replay_365d.jsonl"), "LONG")
    log(
        f"Ökonomie-Overlay: RUB-LONG={0 if rub is None else len(rub)} Events, "
        f"ABR1-LONG={0 if abr is None else len(abr)} Events"
    )

    variants: list[tuple[str, str, float | None]] = [("V0_ist", "V0", None)]
    variants += [(f"V1_fix_{x}", "V1_fix", x) for x in FIX_GRID]
    variants += [(f"V2_atr_{k}", "V2_atr", k) for k in ATR_GRID]

    results = {}
    for name, kind, param in variants:
        raw = classify(feat, kind, param)
        eff = debounce(raw)
        res = {"structure": episode_stats(eff, feat["ts"])}
        if rub is not None:
            res["rub_long_by_regime"] = economics(eff, feat["ts"], rub)
        if abr is not None:
            res["abr1_long_by_regime"] = economics(eff, feat["ts"], abr)
        results[name] = res

        s = res["structure"]
        log(
            f"{name}: TREND_UP {s['TREND_UP']['share_pct']}% ({s['TREND_UP']['episodes']} Ep, "
            f"med {s['TREND_UP']['median_h']}h) | TREND_DOWN {s['TREND_DOWN']['share_pct']}% "
            f"({s['TREND_DOWN']['episodes']} Ep) | TRANSITION {s['TRANSITION']['share_pct']}%"
        )
        if rub is not None and "TREND_UP" in res.get("rub_long_by_regime", {}):
            r = res["rub_long_by_regime"]["TREND_UP"]
            log(f"   RUB-LONG in TREND_UP: n={r['n']}, Ø {r['avg_pnl']:+.2f}%/Trade")

    out = os.path.join(STAGING_DIR, "regime_rules_study.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"days": DAYS, "debounce_bars": DEBOUNCE_BARS, "results": results}, fh, indent=2)
    log(f"FERTIG -> {out}")


if __name__ == "__main__":
    main()
