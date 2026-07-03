import os
# Step-2 remaining checks (15, 19, 20, gap census, RSI check)
import json, os, glob, warnings
import psycopg2
import pandas as pd
warnings.filterwarnings("ignore")

LIVE = r"c:\Users\Michael\PycharmProjects\crypto_trading_bot_v2"
conn = psycopg2.connect(dbname=os.environ.get("DB_NAME", "cryptodata"), user=os.environ.get("DB_USER", "dbfiller"),
                        password=os.environ["DB_PASSWORD"], host="localhost", port=5432)
conn.set_session(readonly=True, autocommit=True)
q = lambda sql: pd.read_sql(sql, conn)

def sec(t): print("\n" + "=" * 70 + "\n## " + t)

sec("15: whole-window broadcast (P1.12) — poc/trendline_price distinct on old rows")
poc = q('''SELECT count(*) n, count(DISTINCT poc) d_poc, count(DISTINCT trendline_price) d_trend,
                  count(DISTINCT support_price) d_support
           FROM (SELECT poc, trendline_price, support_price FROM "BTCUSDT_1h_indicators"
                 WHERE open_time < NOW() - interval '30 days'
                 ORDER BY open_time DESC LIMIT 5000) x''')
print(poc.to_string(index=False))

sec("P2.12: RSI formula check — stored rsi_14 vs Wilder vs ewm(span)")
d = q('SELECT open_time, close, rsi_14 FROM "BTCUSDT_1h_indicators" WHERE open_time > NOW() - interval \'60 days\' ORDER BY open_time')
c = d.close.astype(float)
delta = c.diff()
up, dn = delta.clip(lower=0), -delta.clip(upper=0)
wil_up = up.ewm(alpha=1/14, adjust=False).mean(); wil_dn = dn.ewm(alpha=1/14, adjust=False).mean()
rsi_wilder = 100 - 100/(1 + wil_up/wil_dn)
sp_up = up.ewm(span=14, adjust=False).mean(); sp_dn = dn.ewm(span=14, adjust=False).mean()
rsi_span = 100 - 100/(1 + sp_up/sp_dn)
tail = slice(-500, None)
err_w = (d.rsi_14[tail] - rsi_wilder[tail]).abs().mean()
err_s = (d.rsi_14[tail] - rsi_span[tail]).abs().mean()
print(f"mean|stored - wilder| = {err_w:.3f}   mean|stored - ewm(span=14)| = {err_s:.3f}  (last 500 rows)")

sec("20: coins.json coverage (P1.31) + metals (P2.45)")
coins = json.load(open(os.path.join(LIVE, "coins.json")))
if isinstance(coins, dict): coins = coins.get("coins", list(coins.keys()))
tables = set(q("SELECT table_name FROM information_schema.tables WHERE table_schema='public'").table_name)
missing_1h = [x for x in coins if f"{x}_1h" not in tables]
missing_ind = [x for x in coins if f"{x}_1h_indicators" not in tables]
missing_4h_ind = [x for x in coins if f"{x}_4h_indicators" not in tables]
print(f"coins.json: {len(coins)} | missing _1h: {len(missing_1h)} | missing _1h_indicators: {len(missing_ind)} | missing _4h_indicators: {len(missing_4h_ind)}")
print("missing_1h:", missing_1h[:20])
print("missing_1h_indicators:", missing_ind[:20])
metals = sorted(t for t in tables if "XAU" in t.upper() or "XAG" in t.upper() or "GOLD" in t.upper())
print("metals tables:", metals if metals else "NONE")

sec("19: whale coverage (P1.42) — distinct symbols in last 3 whale files")
symbols = set()
for fp in sorted(glob.glob(os.path.join(LIVE, "whale_data", "whale_trades_*.json")))[-3:]:
    try:
        data = json.load(open(fp, encoding="utf-8"))
        rows = data if isinstance(data, list) else data.get("trades", [])
        if isinstance(rows, list):
            for r0 in rows:
                s = (r0.get("symbol") or r0.get("s")) if isinstance(r0, dict) else None
                if s: symbols.add(s)
        if isinstance(data, dict) and not rows:
            symbols.update(k for k in data.keys() if k.endswith("USDT"))
        print(f"  {os.path.basename(fp)}: cumulative {len(symbols)}")
    except Exception as e:
        print("  parse error", os.path.basename(fp), type(e).__name__, e)
print(f"distinct whale symbols: {len(symbols)} vs coins.json {len(coins)}")

sec("15b: 1h gap census across all coins (30d, internal gaps)")
gap_syms, tot, worst = 0, 0, []
for x in coins:
    t = f"{x}_1h"
    if t not in tables: continue
    r = q(f'''WITH b AS (SELECT open_time FROM "{t}" WHERE open_time > NOW()-interval '30 days')
              SELECT count(*) m FROM generate_series(
                (SELECT min(open_time) FROM b),(SELECT max(open_time) FROM b), interval '1 hour') g(ts)
              LEFT JOIN b ON b.open_time=g.ts WHERE b.open_time IS NULL''')
    m = int(r.m[0] or 0)
    if m: gap_syms += 1; tot += m; worst.append((x, m))
worst.sort(key=lambda y: -y[1])
print(f"symbols with internal 1h gaps: {gap_syms}/{len(coins)}, total missing hours: {tot}")
print("worst:", worst[:15])

sec("P1.41/P1.40: pump_dump_events + shadow flood rates")
try:
    r = q("SELECT count(*) n, min(detected_at) mn, max(detected_at) mx FROM pump_dump_events")
    print("pump_dump_events:", r.to_string(index=False))
except Exception as e:
    print("pump_dump_events schema differs:", e)
    r = q("SELECT column_name FROM information_schema.columns WHERE table_name='pump_dump_events'")
    print(list(r.column_name))
r = q("""SELECT model_name, count(*) n FROM ml_predictions_master
         WHERE created_at > NOW() - interval '7 days' AND posted=FALSE GROUP BY 1 ORDER BY 2 DESC LIMIT 8""")
print("shadow rows last 7d:")
print(r.to_string(index=False))

conn.close()
print("\nDONE")
