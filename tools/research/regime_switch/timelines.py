"""timelines.py — build four regime timelines over one identical feature frame.

  A · RAW   : ``classify_regime`` per candle, prev_regime=None (enter-only, no
              hysteresis, no debounce). The whipsaw upper bound.
  B · RULE  : the faithful LIVE loop — 5-min cadence (features are piecewise
              constant per 15m candle → CHECKS_PER_CANDLE identical checks),
              §22 mid-band hysteresis fed back into classification, then the
              debounce state machine. This is the live baseline.
  C · HMM   : 3-state GaussianHMM(ret_4h, atr_4h_pct), walk-forward, CAUSAL
              Viterbi decode on a trailing window only (no intra-block look-
              ahead — that would hand the HMM an unfair smoothing edge and bias
              the study toward the thread's claim).
  D · SOFT  : the "soft switching" idea grafted onto our own classifier — an
              EMA-smoothed per-regime confidence vector; effective = argmax.
              The continuous analog of debounce, no ML.

RAW/RULE/SOFT emit the native 5-regime vocabulary; HMM emits BULL/NEUTRAL/BEAR.
Whipsaw / dwell / separation metrics are vocabulary-agnostic, so the four are
comparable even though C's alphabet differs (documented in the report).

The debounce port below mirrors ``core.regime_logic.apply_debounce`` /
``hysteresis_prev_regime`` exactly (the DB read/persist is replaced by an in-
memory dataclass). It MUST stay in sync with that module — pinned by
``backtest/test_regime_switch_study.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.regime_logic import (
    REGIME_DEBOUNCE_COUNT,
    TREND_DEBOUNCE_COUNT,
    classify_alt_context,
    classify_btc_regime,
    classify_regime,
)

from .features import feature_row

# 5-min live cadence over 15m piecewise-constant features → 15 / 5 checks/candle.
CHECKS_PER_CANDLE = 3


# ─────────────────────────────────────────────────────────────────────────────
# A · RAW
# ─────────────────────────────────────────────────────────────────────────────
def build_raw_timeline(feat: pd.DataFrame) -> pd.Series:
    """Raw btc-regime per candle, no hysteresis/debounce. NaN percentile → skip."""
    labels: list[str | None] = []
    for _, r in feat.iterrows():
        p75, p40 = r.get("vola_p75"), r.get("vola_p40")
        if p75 is None or p40 is None or np.isnan(p75) or np.isnan(p40):
            labels.append(None)
            continue
        reg, _ = classify_btc_regime(feature_row(r), float(p75), float(p40), prev_regime=None)
        labels.append(reg)
    return pd.Series(labels, index=feat.index, name="raw")


# ─────────────────────────────────────────────────────────────────────────────
# B · RULE — in-memory port of the live debounce + §22 hysteresis
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class _DebounceState:
    regime: str | None = None
    alt: str | None = None
    pending_regime: str | None = None
    pending_count: int = 0
    pending_alt: str | None = None
    pending_alt_count: int = 0


def _hysteresis_prev(st: _DebounceState) -> str | None:
    """Port of core.regime_logic.hysteresis_prev_regime: effective TREND wins over
    a pending TREND; otherwise the effective regime; None on cold start."""
    if st.regime is not None and str(st.regime).startswith("TREND"):
        return str(st.regime)
    if st.pending_regime is not None and str(st.pending_regime).startswith("TREND"):
        return str(st.pending_regime)
    return st.regime


def _step_debounce(st: _DebounceState, raw_regime: str, raw_alt: str) -> None:
    """Port of core.regime_logic.apply_debounce decision branches (mutates st).

    TREND targets need TREND_DEBOUNCE_COUNT consecutive checks; everything else
    REGIME_DEBOUNCE_COUNT. Cold start initialises both axes to the raw values.
    """
    if st.regime is None:  # cold start
        st.regime, st.alt = raw_regime, raw_alt
        return

    needed = TREND_DEBOUNCE_COUNT if str(raw_regime).startswith("TREND") else REGIME_DEBOUNCE_COUNT
    if raw_regime == st.regime:
        st.pending_regime, st.pending_count = None, 0
    elif st.pending_regime == raw_regime:
        st.pending_count += 1
        if st.pending_count >= needed:
            st.regime, st.pending_regime, st.pending_count = raw_regime, None, 0
    else:
        st.pending_regime, st.pending_count = raw_regime, 1

    if raw_alt == st.alt:
        st.pending_alt, st.pending_alt_count = None, 0
    elif st.pending_alt == raw_alt:
        st.pending_alt_count += 1
        if st.pending_alt_count >= REGIME_DEBOUNCE_COUNT:
            st.alt, st.pending_alt, st.pending_alt_count = raw_alt, None, 0
    else:
        st.pending_alt, st.pending_alt_count = raw_alt, 1


def build_rule_timeline(feat: pd.DataFrame, checks_per_candle: int = CHECKS_PER_CANDLE) -> pd.Series:
    """Live-faithful effective btc-regime per candle: hysteresis-fed classification
    + debounce, replayed ``checks_per_candle`` times per candle (5-min cadence)."""
    st = _DebounceState()
    labels: list[str | None] = []
    for _, r in feat.iterrows():
        p75, p40 = r.get("vola_p75"), r.get("vola_p40")
        if p75 is None or p40 is None or np.isnan(p75) or np.isnan(p40):
            labels.append(st.regime)  # carry last effective through warmup gaps
            continue
        feats = feature_row(r)
        for _ in range(checks_per_candle):
            prev = _hysteresis_prev(st)
            raw_btc, _ = classify_btc_regime(feats, float(p75), float(p40), prev_regime=prev)
            raw_alt, _ = classify_alt_context(feats)
            _step_debounce(st, raw_btc, raw_alt)
        labels.append(st.regime)
    return pd.Series(labels, index=feat.index, name="rule")


# ─────────────────────────────────────────────────────────────────────────────
# D · SOFT — EMA-smoothed confidence vector, effective = argmax
# ─────────────────────────────────────────────────────────────────────────────
_REGIMES = ("TREND_UP", "TREND_DOWN", "CHOP", "HIGH_VOLA", "TRANSITION")


def build_soft_timeline(feat: pd.DataFrame, half_life_candles: float = 4.0) -> pd.Series:
    """Soft switching on our own classifier: each candle adds the raw regime's
    confidence to its score, all scores decay by the half-life, effective =
    argmax. A hard flip needs the leader to actually change — the continuous
    analog of the debounce, tunable by ``half_life_candles``."""
    decay = 0.5 ** (1.0 / half_life_candles)
    score = dict.fromkeys(_REGIMES, 0.0)
    labels: list[str | None] = []
    for _, r in feat.iterrows():
        p75, p40 = r.get("vola_p75"), r.get("vola_p40")
        if p75 is None or p40 is None or np.isnan(p75) or np.isnan(p40):
            labels.append(None)
            continue
        res = classify_regime(feature_row(r), float(p75), float(p40), prev_regime=None)
        for k in score:
            score[k] *= decay
        reg = res["regime"]
        if reg in score:
            score[reg] += res["confidence_btc"]
        labels.append(max(score, key=score.get))
    return pd.Series(labels, index=feat.index, name="soft")


# ─────────────────────────────────────────────────────────────────────────────
# C · HMM — walk-forward, causal trailing-window Viterbi decode
# ─────────────────────────────────────────────────────────────────────────────
def _forward_filter_states(model, Xs: np.ndarray) -> np.ndarray:
    """Causal filtered state sequence: argmax_j P(state_t=j | obs_1..t).

    One forward pass over log-emission likelihoods — no backward smoothing, so
    candle t's state uses only observations up to t (unlike model.predict /
    predict_proba, which are Viterbi/forward-backward and peek at the whole
    block). This is what keeps the HMM's whipsaw honest.
    """
    from scipy.special import logsumexp

    log_b = model._compute_log_likelihood(Xs)  # (T, K) log emission probs
    log_t = np.log(model.transmat_ + 1e-300)
    log_start = np.log(model.startprob_ + 1e-300)
    n_t = log_b.shape[0]
    la = log_start + log_b[0]
    states = np.empty(n_t, dtype=int)
    states[0] = int(la.argmax())
    for t in range(1, n_t):
        la = logsumexp(la[:, None] + log_t, axis=0) + log_b[t]
        states[t] = int(la.argmax())
    return states


def build_hmm_timeline(
    feat: pd.DataFrame,
    train_window: int = 90 * 96,
    refit_every: int = 30 * 96,
    warmup: int = 2 * 96,
    n_states: int = 3,
    n_iter: int = 100,
    verbose: bool = False,
) -> pd.Series:
    """3-state GaussianHMM over (ret_4h, atr_4h_pct), walk-forward + CAUSAL filter.

    Refit every ``refit_every`` candles (default 30d — the thread's own "retrain
    monthly", and it keeps the ~16s/full-cov-fit cost to ~12 fits) on the trailing
    ``train_window``; for the block until the next refit, decode via a forward
    filter over [block_start - warmup, block_end) using only that model — causal
    (no intra-block look-ahead). States are relabelled per fit by mean ret_4h
    (BEAR<NEUTRAL<BULL) so labels stay stable across refits. A failed/degenerate
    fit → NEUTRAL block.
    """
    from hmmlearn import hmm
    from sklearn.preprocessing import StandardScaler

    X = feat[["btc_return_4h", "btc_atr_4h_pct"]].to_numpy(dtype=float)
    ret = feat["btc_return_4h"].to_numpy(dtype=float)
    n = len(feat)
    labels: list[str | None] = [None] * n
    order_names = ["BEAR", "NEUTRAL", "BULL"] if n_states == 3 else [f"S{i}" for i in range(n_states)]
    n_blocks = len(range(train_window, n, refit_every))

    for bi, f in enumerate(range(train_window, n, refit_every), 1):
        if verbose:
            import sys as _sys
            print(f"    HMM fit block {bi}/{n_blocks} @ candle {f}…", flush=True, file=_sys.stderr)
        block_end = min(f + refit_every, n)
        # ── fit on the trailing train window ending at f ──
        tr = X[f - train_window : f]
        ok_tr = np.isfinite(tr).all(axis=1)
        model = scaler = None
        state_label: dict[int, str] = {}
        try:
            trc = tr[ok_tr]
            if len(trc) < n_states * 20:
                raise ValueError("train window too thin")
            scaler = StandardScaler().fit(trc)
            model = hmm.GaussianHMM(n_components=n_states, covariance_type="full", n_iter=n_iter, random_state=42)
            model.fit(scaler.transform(trc))
            pred_tr = model.predict(scaler.transform(trc))
            tr_ret = ret[f - train_window : f][ok_tr]
            means = [tr_ret[pred_tr == s].mean() if (pred_tr == s).any() else 0.0 for s in range(n_states)]
            order = np.argsort(means)  # low→high mean return
            state_label = {int(order[i]): order_names[i] for i in range(n_states)}
        except Exception:
            model = None

        # ── causal forward-filter decode of [f, block_end) ──
        if model is None:
            for k in range(f, block_end):
                labels[k] = "NEUTRAL"
            continue
        seg_start = max(0, f - warmup)
        seg = X[seg_start:block_end]
        ok = np.isfinite(seg).all(axis=1)
        seg_states = np.full(len(seg), -1, dtype=int)
        if ok.sum() >= 2:
            try:
                filt = _forward_filter_states(model, scaler.transform(seg[ok]))
                seg_states[ok] = filt
            except Exception:
                pass
        for k in range(f, block_end):
            s = seg_states[k - seg_start]
            labels[k] = state_label.get(int(s), "NEUTRAL") if s >= 0 else "NEUTRAL"

    return pd.Series(labels, index=feat.index, name="hmm")
