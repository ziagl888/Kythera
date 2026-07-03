import os
# Step-2 DB verification battery (read-only)
import json, os, re, sys, glob, pickle, warnings
import psycopg2
import pandas as pd
warnings.filterwarnings("ignore")

LIVE = r"c:\Users\Michael\PycharmProjects\crypto_trading_bot_v2"
conn = psycopg2.connect(dbname=os.environ.get("DB_NAME", "cryptodata"), user=os.environ.get("DB_USER", "dbfiller"),
                        password=os.environ["DB_PASSWORD"], host="localhost", port=5432)
conn.set_session(readonly=True, autocommit=True)

def q(sql, params=None):
    return pd.read_sql(sql, conn, params=params)

def sec(title):
    print("\n" + "=" * 70)
    print("## " + title)

def classify(status, targets_hit):
    s = (status or "").upper()
    if "DELISTED" in s or "CLEANUP" in s or "REGIME" in s or "EXPIRED" in s:
        return "neutral"
    if "ALL TARGETS" in s:
        return "win"
    if s.startswith("SL HIT") or s.startswith("SL_HIT") or "STOP" in s:
        return "win" if (targets_hit or 0) >= 1 else "loss"
    if (targets_hit or 0) >= 1 or s.startswith("TP"):
        return "win"
    return "other:" + s[:20]

# ---------- 13/14: per-model realized outcomes ----------
sec("13/14: closed_ai_signals — per-model realized outcomes (win = >=TP1)")
df = q("SELECT model, direction, status, targets_hit, entry, close_price, open_time, close_time FROM closed_ai_signals")
df["res"] = [classify(s, t) for s, t in zip(df.status, df.targets_hit)]
other = df[df.res.str.startswith("other")]
if len(other):
    print("unclassified statuses:", other.res.value_counts().head(10).to_dict())
d2 = df[df.res.isin(["win", "loss"])]
g = d2.groupby("model").agg(n=("res", "size"), wins=("res", lambda x: (x == "win").sum()))
g["wr%"] = (100 * g.wins / g.n).round(1)
g = g.sort_values("n", ascending=False)
print(g.to_string())
overall = 100 * (d2.res == "win").mean()
rom = g.loc["ROM1"] if "ROM1" in g.index else None
print(f"\nOVERALL WR: {overall:.1f}%  | ROM1: {rom['wr%'] if rom is not None else 'n/a'}% (n={int(rom['n']) if rom is not None else 0})")

sec("14b: targets_hit distribution (P2.31) — top models")
th = df[df.res.isin(["win", "loss"])].groupby(["model", "targets_hit"]).size().unstack(fill_value=0)
print(th.loc[th.sum(axis=1).sort_values(ascending=False).head(12).index].to_string())

# ---------- 12: calibration confidence vs outcome ----------
sec("12: calibration — posted predictions joined to closed outcomes (+-2h)")
cal = q("""
  SELECT p.model_name, p.confidence, c.status, c.targets_hit
  FROM ml_predictions_master p
  JOIN closed_ai_signals c
    ON c.symbol = p.coin AND c.direction = p.direction AND c.model = p.model_name
   AND c.open_time BETWEEN p.time - interval '2 hours' AND p.time + interval '2 hours'
  WHERE p.posted = TRUE
""")
cal["res"] = [classify(s, t) for s, t in zip(cal.status, cal.targets_hit)]
cal = cal[cal.res.isin(["win", "loss"])]
print(f"joined rows: {len(cal)}")
cal["bucket"] = pd.cut(cal.confidence, [0, .4, .5, .6, .7, .8, .9, 1.0])
for m, sub in cal.groupby("model_name"):
    if len(sub) < 30:
        continue
    bt = sub.groupby("bucket", observed=True).agg(n=("res", "size"), wr=("res", lambda x: round(100 * (x == "win").mean(), 1)))
    bt = bt[bt.n >= 5]
    if len(bt) >= 2:
        corr = sub.confidence.corr((sub.res == "win").astype(float))
        print(f"\n{m} (n={len(sub)}, conf-vs-win corr={corr:.3f})")
        print(bt.to_string())

# ---------- 12b: ABR1 long vs short ----------
sec("12b: ABR1 LONG vs SHORT (P2.38 SUCCESS_CLASS_IDX test)")
abr = df[(df.model == "ABR1") & df.res.isin(["win", "loss"])]
print(abr.groupby("direction").agg(n=("res", "size"), wr=("res", lambda x: round(100 * (x == "win").mean(), 1))).to_string())

# ---------- 13b: ROM1 SL distance (P2.27) ----------
sec("13b: ROM1 SL distance |sl-entry1|/entry1 from ai_signals (P2.27)")
rom1 = q("SELECT entry1, sl FROM ai_signals WHERE model='ROM1' AND entry1 IS NOT NULL AND sl IS NOT NULL AND entry1>0")
if len(rom1):
    dist = (rom1.sl - rom1.entry1).abs() / rom1.entry1 * 100
    print(dist.describe(percentiles=[.5, .75, .9, .95, .99]).round(2).to_string())
    print(f">15% SL distance: {(dist > 15).sum()}/{len(dist)}  >30%: {(dist > 30).sum()}")
else:
    print("no ROM1 rows in ai_signals")

# ---------- 14c: classic strategies from closed_trades_master ----------
sec("14c: classic strategies — closed_trades_master")
ct = q("SELECT strategy, status, sl, count(*) n FROM (SELECT strategy, status, sl FROM closed_trades_master) x GROUP BY 1,2,3 LIMIT 0")  # placeholder
ct = q("SELECT strategy, status, count(*) n FROM closed_trades_master GROUP BY 1,2")
piv = ct.pivot_table(index="strategy", columns="status", values="n", fill_value=0, aggfunc="sum")
print(piv.to_string())
sl0 = q("SELECT strategy, count(*) FILTER (WHERE sl<=0 OR sl IS NULL) sl_bad, count(*) n FROM closed_trades_master GROUP BY 1 ORDER BY 2 DESC")
print("\nsl<=0/NULL by strategy (P2.9):")
print(sl0.to_string(index=False))

# ---------- 18: vocabulary check master model ----------
sec("18: AIM1 master-model dummy vocabulary vs live labels (P0.13)")
try:
    with open(os.path.join(LIVE, "master_trade_model_xgboost_combined_signals.pkl"), "rb") as f:
        mdl = pickle.load(f)
    feats = None
    if hasattr(mdl, "feature_names_in_"):
        feats = list(mdl.feature_names_in_)
    elif isinstance(mdl, dict):
        for k in ("features", "feature_names", "columns"):
            if k in mdl:
                feats = list(mdl[k]); break
        if feats is None and "model" in mdl and hasattr(mdl["model"], "feature_names_in_"):
            feats = list(mdl["model"].feature_names_in_)
    if feats is None:
        print("could not extract features; type:", type(mdl))
    else:
        model_dummies = sorted(f for f in feats if f.startswith("ai_model_"))
        conv_dummies = sorted(f for f in feats if f.startswith("conv_bot_"))
        print(f"{len(feats)} features; {len(model_dummies)} ai_model_*, {len(conv_dummies)} conv_bot_*")
        print("ai_model_ dummies:", model_dummies)
        print("conv_bot_ dummies:", conv_dummies)
        live_models = set(q("SELECT DISTINCT model FROM ai_signals").model)
        live_conv = set(q("SELECT DISTINCT strategy FROM active_trades_master UNION SELECT DISTINCT strategy FROM closed_trades_master").strategy.dropna())
        mm = {d[len("ai_model_"):] for d in model_dummies}
        cc = {d[len("conv_bot_"):] for d in conv_dummies}
        print("\nlive ai_signals.model values:", sorted(live_models))
        print("overlap model dummies vs live:", sorted(mm & live_models))
        print("live conv strategies:", sorted(live_conv))
        print("overlap conv dummies vs live:", sorted(cc & live_conv))
except Exception as e:
    print("ERROR loading master pkl:", e)

# ---------- 11: regime flaps ----------
sec("11: regime_history — raw flaps & TRANSITION share, 30 days (P2.23)")
rh = q("SELECT ts, regime FROM regime_history WHERE ts > NOW() AT TIME ZONE 'UTC' - interval '30 days' ORDER BY ts")
if len(rh):
    rh["win2"] = rh.ts.dt.floor("2h")
    flaps = rh.groupby("win2").regime.nunique()
    print(f"rows: {len(rh)}, 2h windows: {len(flaps)}, windows with >=2 distinct regimes: {(flaps >= 2).sum()} ({100 * (flaps >= 2).mean():.1f}%)  (>=3: {(flaps >= 3).sum()})")
    print("regime share:", (rh.regime.value_counts(normalize=True) * 100).round(1).to_dict())
    print("raw regime changes/day:", round((rh.regime != rh.regime.shift()).sum() / 30, 1))
else:
    print("no regime_history rows in window")

# ---------- 15: POC broadcast check (P1.12) ----------
sec("15: whole-window indicator broadcast (P1.12) — BTCUSDT_1h_indicators")
poc = q("""SELECT count(*) n, count(DISTINCT poc) d_poc, count(DISTINCT trendline_value) d_trend
           FROM (SELECT poc, trendline_value FROM "BTCUSDT_1h_indicators"
                 WHERE open_time < NOW() - interval '30 days' LIMIT 5000) x""")
print(poc.to_string(index=False))

# ---------- 20: missing tables per coins.json ----------
sec("20: coins.json coverage — missing candle/indicator tables (P1.31)")
coins = json.load(open(os.path.join(LIVE, "coins.json")))
if isinstance(coins, dict):
    coins = coins.get("coins", list(coins.keys()))
tables = set(q("SELECT table_name FROM information_schema.tables WHERE table_schema='public'").table_name)
missing_1h = [c for c in coins if f"{c}_1h" not in tables]
missing_ind = [c for c in coins if f"{c}_1h_indicators" not in tables]
print(f"coins.json: {len(coins)} symbols; missing _1h: {len(missing_1h)}; missing _1h_indicators: {len(missing_ind)}")
print("missing_1h sample:", missing_1h[:15])
print("missing_ind sample:", missing_ind[:15])
metals = [t for t in tables if "XAU" in t or "XAG" in t or "GC=" in t]
print("metals tables:", metals[:10] if metals else "NONE")

# ---------- 19: whale coverage ----------
sec("19: whale-logger coverage (P1.42)")
symbols = set()
files = sorted(glob.glob(os.path.join(LIVE, "whale_data", "whale_trades_*.json")))[-3:]
for fp in files:
    try:
        data = json.load(open(fp, encoding="utf-8"))
        if isinstance(data, list):
            for row in data:
                s = row.get("symbol") or row.get("s")
                if s: symbols.add(s)
        elif isinstance(data, dict):
            symbols.update(data.keys())
    except Exception as e:
        print("  parse error", os.path.basename(fp), e)
print(f"last-3-file distinct symbols: {len(symbols)} vs coins.json {len(coins)}")

# ---------- gap census across all symbols (last 30d, excluding post-shutdown) ----------
sec("15b: gap census 1h — all coins.json symbols, 30d window (R1/P2.13/P0.9)")
gap_syms = 0; tot_missing = 0; worst = []
for c in coins:
    t = f"{c}_1h"
    if t not in tables: continue
    r = q(f'''WITH b AS (SELECT open_time FROM "{t}" WHERE open_time > NOW()-interval '30 days')
              SELECT count(*) m FROM generate_series(
                (SELECT date_trunc('hour', min(open_time)) FROM b),
                (SELECT date_trunc('hour', max(open_time)) FROM b), interval '1 hour') g(ts)
              LEFT JOIN b ON b.open_time=g.ts WHERE b.open_time IS NULL''')
    m = int(r.m[0] or 0)
    if m > 0:
        gap_syms += 1; tot_missing += m; worst.append((c, m))
worst.sort(key=lambda x: -x[1])
print(f"symbols with 1h gaps (within own min-max, 30d): {gap_syms}, total missing hours: {tot_missing}")
print("worst:", worst[:15])

conn.close()
print("\nDONE")
