"""Signal-rate delta for T-2026-CU-9050-036: pivots on the forming candle vs not.

Replays the geometry gate of 25_smc_ml_sniper.scan_market over the DB-free
regression-guard fixtures (4 symbols x {1h,4h}, 600 closed candles each).
Every scan point feeds a 150-row window whose LAST row plays the forming candle
(exactly the frame the bot sees live). Counted is the pre-ML pattern trigger,
i.e. the point where evaluate_and_trade() would be called.

The fixture rows are CLOSED candles, so the stand-in forming row carries its
final high/low. The replay therefore measures exactly the code delta (last row
in vs. out); the live repaint is strictly larger, because there the forming
candle is only partially built. rsi_14 is recomputed with the engine's own
formula instead of read from the indicator table.

Run from the repo root: py -3.13 tools/sniper_forming_delta.py
"""

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


def triggers(highs, lows, closes, rsis, tf, drop_forming):
    n = len(closes)
    current_price = closes[-1]
    ph, pl = (highs[:-1], lows[:-1]) if drop_forming else (highs, lows)
    peak_idx = scipy.signal.argrelextrema(ph, np.greater, order=PIVOT_WINDOW)[0]
    trough_idx = scipy.signal.argrelextrema(pl, np.less, order=PIVOT_WINDOW)[0]
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

    # 2a BB long
    p_res = peak_idx[-2]
    pivot_res = highs[p_res]
    if pivot_res * 0.995 <= current_price <= pivot_res * 1.005:
        breakout_idx = -1
        for i in range(p_res + 1, n - 1):
            if closes[i] > pivot_res:
                breakout_idx = i
                break
        if breakout_idx != -1 and (n - 1 - breakout_idx) <= MAX_BB_AGE:
            if max(highs[breakout_idx : n - 1]) > pivot_res * 1.003:
                out.add(('bb', 'LONG'))
    # 2b BB short
    p_sup = trough_idx[-2]
    pivot_sup = lows[p_sup]
    if pivot_sup * 0.995 <= current_price <= pivot_sup * 1.005:
        breakdown_idx = -1
        for i in range(p_sup + 1, n - 1):
            if closes[i] < pivot_sup:
                breakdown_idx = i
                break
        if breakdown_idx != -1 and (n - 1 - breakdown_idx) <= MAX_BB_AGE:
            if min(lows[breakdown_idx : n - 1]) < pivot_sup * 0.997:
                out.add(('bb', 'SHORT'))
    return out


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
            old = triggers(H[s], L[s], C[s], R[s], tf, drop_forming=False)
            new = triggers(H[s], L[s], C[s], R[s], tf, drop_forming=True)
            for kind in old | new:
                rows.append((sym, tf, f'{kind[0]}_{kind[1]}', kind in old, kind in new))

t = pd.DataFrame(rows, columns=['sym', 'tf', 'pattern', 'old', 'new'])
print(f'scan points: {scan_points}  ({frames} frames)')
print()
g = t.groupby('pattern').agg(old=('old', 'sum'), new=('new', 'sum'))
g['both'] = t[t.old & t.new].groupby('pattern').size().reindex(g.index).fillna(0).astype(int)
g['only_old'] = g.old - g.both
g['only_new'] = g.new - g.both
g['delta_pct'] = ((g.new - g.old) / g.old.replace(0, np.nan) * 100).round(1)
print(g.to_string())
print()
print('TOTAL', dict(old=int(g.old.sum()), new=int(g.new.sum()), both=int(g.both.sum())))
print()
print(t.groupby(['tf', 'pattern']).agg(old=('old', 'sum'), new=('new', 'sum')).to_string())
