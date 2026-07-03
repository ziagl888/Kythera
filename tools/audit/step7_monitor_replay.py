import os
# Step 7: monitor scoring replay against 5m candles (read-only)
import sys, io, warnings
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import psycopg2
import pandas as pd
import numpy as np
warnings.filterwarnings("ignore")

conn = psycopg2.connect(dbname=os.environ.get("DB_NAME", "cryptodata"), user=os.environ.get("DB_USER", "dbfiller"),
                        password=os.environ["DB_PASSWORD"], host="localhost", port=5432)
conn.set_session(readonly=True, autocommit=True)
q = lambda sql, p=None: pd.read_sql(sql, conn, params=p)
def sec(t): print("\n" + "=" * 78 + "\n## " + t)

trades = q("""SELECT id, strategy, coin, direction, entry, target1, sl, time, posted, status, close_price
              FROM closed_trades_master
              WHERE time > '2026-06-05' AND close_price > 0 AND entry > 0 AND target1 > 0 AND sl > 0
                AND status IN ('0','1','2','SL0','SL1','SL2')
              ORDER BY random() LIMIT 400""")
print(f"Sample: {len(trades)} Trades, Strategien: {trades.strategy.value_counts().to_dict()}")

tables = set(q("SELECT table_name FROM information_schema.tables WHERE table_schema='public'").table_name)

results = []
for _, t in trades.iterrows():
    tab = f"{t.coin}_5m"
    if tab not in tables:
        continue
    is_long = t.direction.upper().startswith("L")
    # load candles around trade window (naive time may be UTC or UTC+3 → widen)
    c = q(f'''SELECT open_time AT TIME ZONE 'UTC' AS ot, high, low, close FROM "{tab}"
              WHERE open_time BETWEEN (%s::timestamp AT TIME ZONE 'UTC') - interval '4 hours'
                                  AND (%s::timestamp AT TIME ZONE 'UTC') + interval '4 hours'
              ORDER BY open_time''', (str(t.time), str(t.posted)))
    if len(c) < 5:
        continue
    # TZ alignment: find shift (0 or -3h on naive stamps => +0/+3 on window) where close_price
    # falls inside the candle at 'posted'
    best_shift, best_err = None, 1e18
    for shift in (0, 3):
        ts_close = pd.Timestamp(t.posted) - pd.Timedelta(hours=shift)
        row = c[(c.ot <= ts_close)].tail(1)
        if not len(row):
            continue
        lo, hi = float(row.low.iloc[0]), float(row.high.iloc[0])
        err = 0 if lo <= t.close_price <= hi else min(abs(t.close_price - lo), abs(t.close_price - hi)) / t.close_price
        if err < best_err:
            best_err, best_shift = err, shift
    if best_shift is None:
        continue
    ts_open = pd.Timestamp(t.time) - pd.Timedelta(hours=best_shift)
    ts_close = pd.Timestamp(t.posted) - pd.Timedelta(hours=best_shift)
    win = c[(c.ot > ts_open) & (c.ot <= ts_close)]
    if not len(win):
        continue
    if is_long:
        tp_hit = win[win.high >= t.target1]
        sl_hit = win[win.low <= t.sl]
    else:
        tp_hit = win[win.low <= t.target1]
        sl_hit = win[win.high >= t.sl]
    first_tp = tp_hit.ot.iloc[0] if len(tp_hit) else None
    first_sl = sl_hit.ot.iloc[0] if len(sl_hit) else None
    same_candle = (first_tp is not None and first_sl is not None and first_tp == first_sl)
    monitor_says_tp1 = t.status in ("1", "2", "SL1", "SL2")
    replay_says_tp1 = first_tp is not None and (first_sl is None or first_tp <= first_sl)
    results.append(dict(strategy=t.strategy, status=t.status, close_plausible=best_err == 0,
                        err=best_err, monitor_tp1=monitor_says_tp1, replay_tp1=replay_says_tp1,
                        same_candle=same_candle, shift=best_shift,
                        missed_tp=(not monitor_says_tp1 and replay_says_tp1),
                        missed_sl=(t.status in ("1", "2") and first_sl is not None and
                                   (first_tp is None or first_sl < first_tp))))
r = pd.DataFrame(results)
sec("Replay-Ergebnis")
print(f"replayed: {len(r)}")
print(f"close_price innerhalb der Close-Kerze: {100*r.close_plausible.mean():.1f}%  (median err sonst: {r[~r.close_plausible].err.median()*100 if (~r.close_plausible).any() else 0:.2f}%)")
print(f"TZ-Shift-Verteilung (0h=UTC, 3h=Lokal geschrieben): {r['shift'].value_counts().to_dict()}")
print(f"Monitor sagt >=TP1: {100*r.monitor_tp1.mean():.1f}%  | Replay sagt >=TP1 (first-touch): {100*r.replay_tp1.mean():.1f}%")
print(f"Übereinstimmung Monitor vs Replay: {100*(r.monitor_tp1==r.replay_tp1).mean():.1f}%")
print(f"  Monitor VERPASSTE TP1 (Replay ja, Monitor nein): {r.missed_tp.sum()} ({100*r.missed_tp.mean():.1f}%)")
print(f"  Monitor vergab TP1, aber SL kam laut Replay ZUERST: {r.missed_sl.sum()} ({100*r.missed_sl.mean():.1f}%)")
print(f"  TP+SL in derselben 5m-Kerze (Ambiguität): {r.same_candle.sum()} ({100*r.same_candle.mean():.1f}%)")
print("\nnach Strategie (n | agree%):")
print(r.groupby("strategy").apply(lambda x: f"n={len(x)} agree={100*(x.monitor_tp1==x.replay_tp1).mean():.0f}% missedTP={x.missed_tp.sum()} slFirst={x.missed_sl.sum()}").to_string())

sec("5m-Gap-Census (10 Sample-Coins, 25 Tage)")
import random
random.seed(1)
coins = [x[:-3] for x in tables if x.endswith("_5m") and "USDT" in x]
for coin in random.sample(coins, 10):
    g = q(f'''WITH b AS (SELECT open_time FROM "{coin}_5m" WHERE open_time > NOW()-interval '26 days' AND open_time < NOW()-interval '10 hours')
              SELECT count(*) m FROM generate_series((SELECT min(open_time) FROM b),(SELECT max(open_time) FROM b), interval '5 min') s(ts)
              LEFT JOIN b ON b.open_time=s.ts WHERE b.open_time IS NULL''')
    print(f"  {coin}: fehlende 5m-Kerzen: {int(g.m[0] or 0)}")

sec("Outbox-Failures (800 failed)")
f = q("""SELECT channel_id, count(*) n, left(max(last_error),60) sample_err FROM telegram_outbox
         WHERE failed GROUP BY 1 ORDER BY 2 DESC LIMIT 8""")
print(f.to_string(index=False))

conn.close()
print("\nDONE")
