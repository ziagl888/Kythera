#!/usr/bin/env py -3.13
# tools/mps1_liq_heatmap.py — MPS1 liquidation-heatmap engine (T-2026-KYT-9050-073)
"""DB-free estimator of Binance-futures liquidation clusters ("futures max
pain") from 5m open-interest deltas — the Coinglass-style heuristic behind
MartyParty's "trade the spread between 24h Max Pains" chart.

We cannot observe trader positions. The standard estimation (same class of
heuristic Coinglass documents for its liquidation heatmap) is:

  * SEED — when open interest RISES over a closed 5m bar, new positions were
    opened during that bar at ~its typical price p (hlc3). For assumed leverage
    tiers L the implied liquidation prices are
        long  ≈ p · (1 − 1/L + mmr)        short ≈ p · (1 + 1/L − mmr)
    (isolated USDT-M approximation; mmr pulls the level slightly toward entry).
    The ΔOI notional (USDT) is split 50/50 long/short (the side mix of NEW
    contracts is unobservable; OI counts matched pairs) and across tiers by a
    fixed weight vector (lower leverage carries more notional).
  * CONSUME — when a later bar's [low, high] range crosses a level, that
    liquidity is treated as taken (liquidated or stopped) and the level dies.
    Consumption runs BEFORE this bar's seeding: levels born from a bar are
    never consumed by the same bar's range (they were opened inside it).
  * DECAY — when OI FALLS over a bar, positions were closed voluntarily;
    all alive levels shrink by the relative OI drop (uniform, both sides).
    Part of a drop may actually be liquidations already handled by CONSUME —
    accepted double-shrink, documented; it biases densities DOWN (conservative).
  * EXPIRE — MartyParty's bands are "24hr Max Pains": only levels seeded
    within the trailing `window_bars` (default 288 = 24h) count. Older seeds
    die regardless of price action.
  * BANDS — at each bar close, alive levels are histogrammed in log-space
    around the close and sided by their CURRENT position relative to spot:
    the densest smoothed cluster above spot is the upper band (in practice
    short-liquidation magnets — a level between spot and its seed would have
    been consumed on the cross, gaps through levels are the rare exception),
    the densest below is the lower band. Bins further than
    `max_band_distance` from spot are ignored (irrelevant magnets), bins
    closer than `min_band_distance` too (noise riding on spot).

No look-ahead by construction: bar t consumes only OI points and candles up
to and including bar t; truncating the inputs reproduces the prefix exactly
(pinned in backtest/test_mps1_liq_heatmap.py).

Honest limits: the leverage mix and the long/short split are ASSUMPTIONS —
the output is an estimate of where liquidity clusters, not ground truth. We
collect no liquidation feed (yet) to calibrate against; the event study
(tools/mps1_event_study.py) therefore tests the bands' PREDICTIVE value
directly instead of their literal accuracy.

Only closed candles enter (Rule 5) — `oi_5m` itself only carries closed 5m
periods (collector contract, 35_oi_collector.py).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

BAR_SECONDS = 300  # the engine is bound to the 5m grid of oi_5m

# Output columns of build_heatmap (one row per input candle, same order).
HEATMAP_COLUMNS = [
    "open_time",
    "close",
    "oi_value_usdt",
    "d_oi_usdt",
    "upper_band",
    "upper_density_usdt",
    "upper_density_frac",
    "lower_band",
    "lower_density_usdt",
    "lower_density_frac",
    "alive_usdt_above",
    "alive_usdt_below",
]


@dataclass(frozen=True)
class HeatmapConfig:
    """Tunables of the estimator. Defaults mirror the public heatmap folklore:
    leverage tiers 10/25/50/100 with notional concentrated on the low tiers."""

    leverage_tiers: tuple[float, ...] = (10.0, 25.0, 50.0, 100.0)
    tier_weights: tuple[float, ...] = (0.4, 0.3, 0.2, 0.1)
    maintenance_margin: float = 0.005
    window_bars: int = 288  # 24h of 5m bars ("24hr Max Pain")
    bin_bp: float = 25.0  # histogram bin width, basis points (log-space)
    smooth_bins: int = 3  # cluster density = centered sum over this many bins
    min_band_distance: float = 0.001  # ignore levels within 0.1% of spot
    max_band_distance: float = 0.25  # ignore levels further than 25% from spot

    def __post_init__(self) -> None:
        if len(self.leverage_tiers) != len(self.tier_weights):
            raise ValueError("leverage_tiers and tier_weights must have equal length")
        if abs(sum(self.tier_weights) - 1.0) > 1e-9:
            raise ValueError("tier_weights must sum to 1")
        if min(self.leverage_tiers) <= 1.0:
            raise ValueError("leverage tiers must be > 1")
        if self.smooth_bins < 1 or self.smooth_bins % 2 == 0:
            raise ValueError("smooth_bins must be odd and >= 1")


def liq_prices(price: float, cfg: HeatmapConfig) -> tuple[np.ndarray, np.ndarray]:
    """Implied (long, short) liquidation prices per leverage tier for an entry
    at `price`. Long liqs sit below entry, short liqs above."""
    tiers = np.asarray(cfg.leverage_tiers)
    long_liq = price * (1.0 - 1.0 / tiers + cfg.maintenance_margin)
    short_liq = price * (1.0 + 1.0 / tiers - cfg.maintenance_margin)
    return long_liq, short_liq


def align_oi_to_candles(candles: pd.DataFrame, oi: pd.DataFrame) -> pd.DataFrame:
    """Join `oi_5m` points onto 5m candles: the OI snapshot stamped at grid
    time T is the state at the CLOSE of the bar with open_time = T − 5m
    (collector fetches the freshly closed point just after the mark).

    Returns the candle frame plus `oi_value_usdt` (NaN where the sweep is
    missing) and `d_oi_usdt` — the diff over AVAILABLE points, attributed
    entirely to the bar ending at the later point (a gap's net delta lands on
    the first bar after the gap; documented approximation).
    """
    out = candles.reset_index(drop=True).copy()
    close_time = out["open_time"] + pd.Timedelta(seconds=BAR_SECONDS)
    oi_series = oi.set_index("ts")["oi_value_usdt"]
    oi_series = oi_series[~oi_series.index.duplicated(keep="first")]
    out["oi_value_usdt"] = close_time.map(oi_series).astype(float)
    avail = out["oi_value_usdt"].dropna()
    out["d_oi_usdt"] = avail.diff().reindex(out.index)
    return out


def build_heatmap(candles: pd.DataFrame, oi: pd.DataFrame, cfg: HeatmapConfig | None = None) -> pd.DataFrame:
    """Single forward pass over closed 5m candles + oi_5m points of ONE symbol.

    `candles`: ascending, columns open_time/open/high/low/close (closed bars).
    `oi`: ascending, columns ts/oi_value_usdt.
    Returns one row per candle (HEATMAP_COLUMNS); band columns are NaN while
    no qualifying cluster exists (e.g. during warm-up).

    Implementation notes (performance — a naive per-bar histogram costs ~15 s
    per symbol, prohibitive over 528 symbols):
      * Levels live on an ABSOLUTE log-price bin grid (anchor price 1.0), so
        the histogram is maintained INCREMENTALLY: seeds add to their bin,
        consumption/expiry subtract, an OI drop scales the whole vector once.
        Band prices are therefore quantized to the grid (±½ bin ≈ ±bin_bp/2),
        not to a spot-anchored grid; side totals count bins strictly above/
        below the close's own bin (sub-bin difference to an exact price
        split — irrelevant at 25 bp resolution).
      * Uniform decay is applied lazily via a running product S: a level's
        current weight is raw · S/S_birth (S is renormalized before it can
        underflow), and the hist vector is scaled in the same step, keeping
        both bookkeeping views consistent.
      * The hot loop stays in Python scalars (numpy per-call overhead is the
        actual cost driver); per-bar hist snapshots are collected and the
        band/argmax extraction runs VECTORIZED over all bars afterwards, in
        row chunks to bound memory.
    """
    cfg = cfg or HeatmapConfig()
    n = len(candles)
    merged = align_oi_to_candles(candles, oi)
    if n == 0:
        empty = merged.assign(**{c: pd.Series(dtype=float) for c in HEATMAP_COLUMNS if c not in merged.columns})
        return empty[HEATMAP_COLUMNS]
    high_l = merged["high"].tolist()
    low_l = merged["low"].tolist()
    close_l = merged["close"].tolist()
    oi_l = merged["oi_value_usdt"].tolist()
    d_l = merged["d_oi_usdt"].tolist()

    w = cfg.bin_bp * 1e-4  # log-space bin width
    min_off = max(1, int(math.ceil(math.log1p(cfg.min_band_distance) / w)))
    max_off = int(math.ceil(math.log1p(cfg.max_band_distance) / w))
    half_smooth = cfg.smooth_bins // 2

    # Absolute grid bounds: candle extremes plus room for the widest seed
    # excursion (lowest tier) and the band search window, so no level or
    # search column can ever fall off the grid.
    margin = math.log1p(1.0 / min(cfg.leverage_tiers)) + math.log1p(cfg.max_band_distance) + 0.05
    lo_log = math.log(min(low_l)) - margin
    offset = int(math.floor(lo_log / w))
    nbins = int(math.ceil((math.log(max(high_l)) + margin) / w)) - offset + 1

    hist = np.zeros(nbins)
    hist_snap = np.zeros((n, nbins), dtype=np.float32)  # per-bar state for the band post-pass
    cb_arr = np.zeros(n, dtype=np.int64)
    n_alive_arr = np.zeros(n, dtype=np.int64)

    # Level records in parallel Python lists (hot-loop scalar access), plus a
    # bin → level-indices map so consumption only walks the bins a bar spans.
    lv_price: list[float] = []
    lv_raw: list[float] = []
    lv_sb: list[float] = []
    lv_bin: list[int] = []
    lv_birth: list[int] = []
    lv_alive: list[bool] = []
    bin_map: dict[int, list[int]] = {}
    head = 0
    n_alive = 0
    S = 1.0  # running decay product (lazy uniform decay)

    tiers = list(cfg.leverage_tiers)
    frac_long = [1.0 - 1.0 / L + cfg.maintenance_margin for L in tiers]
    frac_short = [1.0 + 1.0 / L - cfg.maintenance_margin for L in tiers]
    tier_w = list(cfg.tier_weights)

    for t in range(n):
        low_t, high_t, close_t = low_l[t], high_l[t], close_l[t]

        # 1) EXPIRE — advance the frontier past levels older than the window.
        expiry_cut = t - cfg.window_bars
        while head < len(lv_birth) and lv_birth[head] <= expiry_cut:
            if lv_alive[head]:
                hist[lv_bin[head]] -= lv_raw[head] * (S / lv_sb[head])
                lv_alive[head] = False
                n_alive -= 1
            head += 1

        # 2) DECAY — uniform shrink by the relative OI drop of this bar.
        d = d_l[t]
        if d == d and d < 0:  # NaN-safe
            prev_oi = oi_l[t] - d
            if prev_oi > 0:
                f = max(1e-12, 1.0 + d / prev_oi)
                S *= f
                if n_alive:
                    hist *= f
                if S < 1e-100:  # renormalize before S can underflow to 0
                    for i in range(head, len(lv_sb)):
                        lv_sb[i] /= S
                    S = 1.0

        # 3) CONSUME — this bar's range takes out every level it crossed.
        if n_alive:
            b_lo = int(math.floor(math.log(low_t) / w)) - offset
            b_hi = int(math.floor(math.log(high_t) / w)) - offset
            for b in range(b_lo, b_hi + 1):
                lst = bin_map.get(b)
                if not lst:
                    continue
                keep = []
                for i in lst:
                    if not lv_alive[i]:
                        continue  # lazy cleanup of expired entries
                    if low_t <= lv_price[i] <= high_t:
                        hist[b] -= lv_raw[i] * (S / lv_sb[i])
                        lv_alive[i] = False
                        n_alive -= 1
                    else:
                        keep.append(i)
                if keep:
                    bin_map[b] = keep
                else:
                    del bin_map[b]

        # 4) SEED — OI growth opens new positions at ~hlc3 of this bar.
        if d == d and d > 0:
            p = (high_t + low_t + close_t) / 3.0
            base = 0.5 * d
            for k in range(len(tiers)):
                wgt = base * tier_w[k]
                for price in (p * frac_long[k], p * frac_short[k]):
                    b = int(math.floor(math.log(price) / w)) - offset
                    idx = len(lv_price)
                    lv_price.append(price)
                    lv_raw.append(wgt)
                    lv_sb.append(S)
                    lv_bin.append(b)
                    lv_birth.append(t)
                    lv_alive.append(True)
                    bin_map.setdefault(b, []).append(idx)
                    hist[b] += wgt
                    n_alive += 1

        # 5) snapshot for the vectorized band post-pass.
        n_alive_arr[t] = n_alive
        cb_arr[t] = int(math.floor(math.log(close_t) / w)) - offset
        if n_alive:
            hist_snap[t] = hist

    # ── BANDS (vectorized, chunked): densest smoothed cluster per side ───────
    out = np.full((n, 8), np.nan)  # 0..2 upper band/density/frac · 3..5 lower · 6/7 totals
    offs = np.arange(min_off, max_off + 1)
    chunk = 4096
    for c0 in range(0, n, chunk):
        c1 = min(n, c0 + chunk)
        m = c1 - c0
        hc = hist_snap[c0:c1].astype(np.float64)
        # Centered sliding sum of width smooth_bins (zero-padded edges).
        padded = np.zeros((m, nbins + 2 * half_smooth))
        padded[:, half_smooth : half_smooth + nbins] = hc
        cs = np.concatenate([np.zeros((m, 1)), np.cumsum(padded, axis=1)], axis=1)
        smoothed = cs[:, cfg.smooth_bins :] - cs[:, :nbins]
        # Prefix sums of the raw hist for the strictly-above/below totals.
        ct = np.concatenate([np.zeros((m, 1)), np.cumsum(hc, axis=1)], axis=1)
        total = ct[:, -1]
        cbc = cb_arr[c0:c1]
        ridx = np.arange(m)
        above = total - ct[ridx, np.minimum(cbc + 1, nbins)]
        below = ct[ridx, np.clip(cbc, 0, nbins)]
        valid = n_alive_arr[c0:c1] > 0
        out_c = out[c0:c1]
        out_c[valid, 6] = above[valid]
        out_c[valid, 7] = below[valid]
        for sign, base_col, side_total in ((1, 0, above), (-1, 3, below)):
            cols = np.clip(cbc[:, None] + sign * offs[None, :], 0, nbins - 1)
            window = smoothed[ridx[:, None], cols]
            j = np.argmax(window, axis=1)
            dens = window[ridx, j]
            band = np.exp((cbc + sign * (min_off + j) + offset + 0.5) * w)
            ok = valid & (dens > 0) & (side_total > 0)
            out_c[ok, base_col + 0] = band[ok]
            out_c[ok, base_col + 1] = dens[ok]
            out_c[ok, base_col + 2] = dens[ok] / side_total[ok]

    close = merged["close"].to_numpy()
    oi_val = merged["oi_value_usdt"].to_numpy()
    d_oi = merged["d_oi_usdt"].to_numpy()

    return pd.DataFrame(
        {
            "open_time": merged["open_time"],
            "close": close,
            "oi_value_usdt": oi_val,
            "d_oi_usdt": d_oi,
            "upper_band": out[:, 0],
            "upper_density_usdt": out[:, 1],
            "upper_density_frac": out[:, 2],
            "lower_band": out[:, 3],
            "lower_density_usdt": out[:, 4],
            "lower_density_frac": out[:, 5],
            "alive_usdt_above": out[:, 6],
            "alive_usdt_below": out[:, 7],
        }
    )
