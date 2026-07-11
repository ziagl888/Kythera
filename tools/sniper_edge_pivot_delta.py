"""Signal-rate delta for T-2026-CU-9050-093: unconfirmed right-edge pivots vs not.

Replays the geometry gate of 25_smc_ml_sniper.scan_market over the DB-free
regression-guard fixtures (4 symbols x {1h,4h}, 600 closed candles each). Every
scan point feeds a 150-row window whose LAST row plays the forming candle
(exactly the frame the bot sees live). Counted is the pre-ML pattern trigger,
i.e. the point where evaluate_and_trade() would be called.

This isolates the edge-pivot policy change ONLY: both the OLD and the NEW arm
already drop the forming candle (P1.46) and use find_breaker_setup for BB
(P2.39/T-089). The single difference is the CONFIRM filter on the pivot arrays:
CONFIRM=0 is the current merged behaviour, CONFIRM=PIVOT_WINDOW//2 is the shipped
policy (option B). Pass a value on argv to probe other confirm windows, e.g.
CONFIRM=PIVOT_WINDOW (option A, full Bot-24-style confirmation).

The fixture rows are CLOSED candles, so the stand-in forming row carries its
final high/low. The replay therefore measures exactly the code delta; the live
repaint is strictly larger, because there the forming candle is only partially
built and the kept edge pivots additionally move. rsi_14 is recomputed with the
engine's own formula instead of read from the indicator table.

Run from the repo root: python tools/sniper_edge_pivot_delta.py [confirm]
"""

import sys

import numpy as np
import pandas as pd
import scipy.signal

PIVOT_WINDOW = 10
MAX_TD_SPAN = 50
MAX_BB_AGE = 20
LIMIT = 150


def calculate_rsi(series, period=14):  # verbatim from 2_indicator_engine.py
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    roll_up = up.ewm(span=period, adjust=False).mean()
    roll_down = down.ewm(span=period, adjust=False).mean()
    rs = roll_up / roll_down
    return (100.0 - (100.0 / (1.0 + rs))).fillna(50)


def find_breaker_setup(pivot_indices, level_arr, extreme_arr, closes, n_closed, current_price, direction):
    """Inlined verbatim from 25_smc_ml_sniper.find_breaker_setup so the harness
    stays DB-free and import-free (the bot exit(1)s on model load)."""
    is_long = direction == 'LONG'
    band, break_margin, max_age = 0.005, 0.003, MAX_BB_AGE
    for p in reversed([int(x) for x in pivot_indices]):
        level = float(level_arr[p])
        if level <= 0:
            continue
        if not (level * (1 - band) <= current_price <= level * (1 + band)):
            continue
        breakout_idx = -1
        for i in range(p + 1, n_closed):
            if (closes[i] > level) if is_long else (closes[i] < level):
                breakout_idx = i
                break
        if breakout_idx == -1 or (n_closed - 1 - breakout_idx) > max_age:
            continue
        window = extreme_arr[breakout_idx:n_closed]
        if len(window) == 0:
            continue
        if is_long:
            if window.max() > level * (1 + break_margin):
                return p, breakout_idx
        else:
            if window.min() < level * (1 - break_margin):
                return p, breakout_idx
    return None


def triggers(highs, lows, closes, rsis, tf, confirm):
    n = len(closes)                     # == len(df), forming candle is the last row
    n_closed = n - 1
    last_closed = n - 2
    current_price = closes[-1]          # live CMP (R1/P1.46: stays live)
    c_highs, c_lows = highs[:-1], lows[:-1]
    peak_idx = scipy.signal.argrelextrema(c_highs, np.greater, order=PIVOT_WINDOW)[0]
    trough_idx = scipy.signal.argrelextrema(c_lows, np.less, order=PIVOT_WINDOW)[0]
    # T-093 edge-pivot filter (confirm=0 → current merged behaviour)
    peak_idx = peak_idx[peak_idx <= last_closed - confirm]
    trough_idx = trough_idx[trough_idx <= last_closed - confirm]

    out = set()
    if len(peak_idx) < 3 or len(trough_idx) < 3:
        return out

    # 1a TD short
    if n - peak_idx[-1] <= PIVOT_WINDOW + 2:
        p1, p2, p3 = peak_idx[-3], peak_idx[-2], peak_idx[-1]
        if (p3 - p1) <= MAX_TD_SPAN and highs[p1] < highs[p2] < highs[p3] and rsis[p1] > rsis[p2] > rsis[p3]:
            out.add(('td', 'SHORT'))
    # 1b TD long
    if n - trough_idx[-1] <= PIVOT_WINDOW + 2:
        p1, p2, p3 = trough_idx[-3], trough_idx[-2], trough_idx[-1]
        if (p3 - p1) <= MAX_TD_SPAN and lows[p1] > lows[p2] > lows[p3] and rsis[p1] < rsis[p2] < rsis[p3]:
            out.add(('td', 'LONG'))

    if tf == '1h':  # BB_1H parked on both sides
        return out

    # 2a/2b BB via the P2.39/T-089 selector
    if find_breaker_setup(peak_idx, highs, highs, closes, n_closed, current_price, 'LONG') is not None:
        out.add(('bb', 'LONG'))
    if find_breaker_setup(trough_idx, lows, lows, closes, n_closed, current_price, 'SHORT') is not None:
        out.add(('bb', 'SHORT'))
    return out


def main():
    confirm_new = int(sys.argv[1]) if len(sys.argv) > 1 else PIVOT_WINDOW // 2
    rows = []
    scan_points = 0
    frames = 0
    for sym in ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'DOGEUSDT']:
        for tf in ['1h', '4h']:
            frames += 1
            d = np.load(f'tools/regression_guard/fixtures/{sym}__{tf}.npz', allow_pickle=True)
            cols = list(d['__columns__'])
            df = pd.DataFrame({c: d[f'c{i}'] for i, c in enumerate(cols)})
            df = df.sort_values('open_time').reset_index(drop=True)
            df['rsi_14'] = calculate_rsi(df['close'].astype(float))
            H, L, C, R = (df[c].astype(float).values for c in ['high', 'low', 'close', 'rsi_14'])
            for end in range(LIMIT, len(df) + 1):
                scan_points += 1
                s = slice(end - LIMIT, end)
                old = triggers(H[s], L[s], C[s], R[s], tf, confirm=0)
                new = triggers(H[s], L[s], C[s], R[s], tf, confirm=confirm_new)
                for kind in old | new:
                    rows.append((sym, tf, f'{kind[0]}_{kind[1]}', kind in old, kind in new))

    t = pd.DataFrame(rows, columns=['sym', 'tf', 'pattern', 'old', 'new'])
    print(f'scan points: {scan_points}  ({frames} frames)   confirm(new)={confirm_new}')
    print()
    g = t.groupby('pattern').agg(old=('old', 'sum'), new=('new', 'sum'))
    g['both'] = t[t.old & t.new].groupby('pattern').size().reindex(g.index).fillna(0).astype(int)
    g['only_old'] = g.old - g.both
    g['only_new'] = g.new - g.both
    g['delta_pct'] = ((g.new - g.old) / g.old.replace(0, np.nan) * 100).round(1)
    print(g.to_string())
    print()
    tot_old, tot_new = int(g.old.sum()), int(g.new.sum())
    delta = round((tot_new - tot_old) / tot_old * 100, 1) if tot_old else 0.0
    print('TOTAL', dict(old=tot_old, new=tot_new, both=int(g.both.sum()), delta_pct=delta))


if __name__ == '__main__':
    main()
