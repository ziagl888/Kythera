import os
# Step 5: hypothesis tests for new-strategy proposals (read-only)
import sys, io, warnings
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import psycopg2
import pandas as pd
import numpy as np
warnings.filterwarnings("ignore")
pd.set_option("display.width", 200)

conn = psycopg2.connect(dbname=os.environ.get("DB_NAME", "cryptodata"), user=os.environ.get("DB_USER", "dbfiller"),
                        password=os.environ["DB_PASSWORD"], host="localhost", port=5432)
conn.set_session(readonly=True, autocommit=True)
q = lambda sql: pd.read_sql(sql, conn)
def sec(t): print("\n" + "=" * 78 + "\n## " + t)

ai = q("""SELECT symbol, model, direction, status, targets_hit, entry, close_price, open_time
          FROM closed_ai_signals WHERE status NOT ILIKE 'LEGACY%%'""")
ai = ai.sort_values("open_time").drop_duplicates(subset=["symbol", "model", "direction", "open_time"], keep="first")
s = ai.status.fillna("").str.upper()
ai["cls"] = "other"
ai.loc[s.str.contains("DELISTED|CLEANUP|REGIME|EXPIRED"), "cls"] = "censored"
ai.loc[(ai.cls == "other") & (s.str.contains("ALL TARGETS") | (ai.targets_hit.fillna(0) >= 1)), "cls"] = "win"
ai.loc[(ai.cls == "other") & s.str.startswith("SL"), "cls"] = "loss"
ai["dirsign"] = np.where(ai.direction.str.upper().str.startswith("L"), 1, -1)
ai["pnl"] = (ai.close_price - ai.entry) / ai.entry * 100 * ai.dirsign
wl = ai[ai.cls.isin(["win", "loss"]) & (ai.entry > 0) & (ai.close_price > 0)].copy()
wl = wl[wl.open_time > "2026-02-25"]

sec("H1: Konfluenz — n unabhängige Modelle auf (symbol, direction) binnen 4h")
wl = wl.sort_values(["symbol", "direction", "open_time"])
res = []
for (sym, d), grp in wl.groupby(["symbol", "direction"]):
    t = grp.open_time.values.astype("datetime64[s]").astype(np.int64)
    models = grp.model.values
    for i in range(len(grp)):
        lo, hi = np.searchsorted(t, t[i] - 4 * 3600), np.searchsorted(t, t[i] + 4 * 3600, side="right")
        res.append(len(set(models[lo:hi])))
wl["n_models_4h"] = res
wl["conf_bucket"] = pd.cut(wl.n_models_4h, [0, 1, 2, 3, 99], labels=["1", "2", "3", "4+"])
h1 = wl.groupby("conf_bucket", observed=True).agg(
    n=("cls", "size"), wr=("cls", lambda x: round(100 * (x == "win").mean(), 1)),
    avg_pnl=("pnl", lambda x: round(x.mean(), 2)), med=("pnl", lambda x: round(x.median(), 2)))
print(h1.to_string())
# same but excluding the meta/echo models (AIM1 mirrors others, ROM1 mirrors)
wl2 = wl[~wl.model.isin(["AIM1", "ROM1"])]
h1b = wl2.groupby("conf_bucket", observed=True).agg(
    n=("cls", "size"), wr=("cls", lambda x: round(100 * (x == "win").mean(), 1)),
    avg_pnl=("pnl", lambda x: round(x.mean(), 2)))
print("\nohne AIM1/ROM1:")
print(h1b.to_string())

sec("H1b: Konfluenz je Richtung")
h1c = wl2.groupby(["direction", "conf_bucket"], observed=True).agg(
    n=("cls", "size"), wr=("cls", lambda x: round(100 * (x == "win").mean(), 1)),
    avg_pnl=("pnl", lambda x: round(x.mean(), 2)))
print(h1c.to_string())

sec("H2: Regime-konditionale Performance (orchestrator_open_trades.regime_at_open)")
oo = q("""SELECT o.bot_name, o.coin, o.direction, o.regime_at_open, o.alt_context_at_open,
                 o.status, o.close_reason, o.opened_at
          FROM orchestrator_open_trades o WHERE o.status <> 'OPEN'""")
print("close_reason-Verteilung:", oo.close_reason.value_counts().head(8).to_dict())
# outcome via join to closed_ai_signals/closed_trades? close_reason may encode result for ROM1 only.
# instead: join wl outcomes by (coin, direction, opened_at ~ open_time +-2h)
wl_j = wl2[["symbol", "direction", "open_time", "cls", "pnl", "model"]].copy()
oo["opened_at"] = pd.to_datetime(oo.opened_at)
merged = oo.merge(wl_j, left_on=["coin", "direction"], right_on=["symbol", "direction"])
merged["dt"] = (merged.open_time - merged.opened_at).abs()
merged = merged[merged.dt < pd.Timedelta("4h")].sort_values("dt").drop_duplicates(
    subset=["coin", "direction", "opened_at"], keep="first")
print(f"orchestrator-Trades mit Outcome-Match: {len(merged)}")
h2 = merged.groupby(["regime_at_open", "direction"]).agg(
    n=("cls", "size"), wr=("cls", lambda x: round(100 * (x == "win").mean(), 1)),
    avg_pnl=("pnl", lambda x: round(x.mean(), 2)))
print(h2.to_string())
h2b = merged.groupby(["alt_context_at_open", "direction"]).agg(
    n=("cls", "size"), wr=("cls", lambda x: round(100 * (x == "win").mean(), 1)),
    avg_pnl=("pnl", lambda x: round(x.mean(), 2)))
print(h2b.to_string())

sec("H3: AIM1-Inversions-Fade (Shadow-Hypothese) — conf>0.85-AIM1-Signale kontern")
aim = q("""SELECT p.coin, p.direction, p.confidence, p.time, c.status, c.targets_hit,
                  c.entry, c.close_price
           FROM ml_predictions_master p
           JOIN closed_ai_signals c ON c.symbol=p.coin AND c.direction=p.direction AND c.model='AIM1'
             AND c.open_time BETWEEN p.time - interval '2 hours' AND p.time + interval '2 hours'
           WHERE p.model_name='AIM1' AND p.posted=TRUE""")
su = aim.status.fillna("").str.upper()
aim["win"] = (su.str.contains("ALL TARGETS") | (aim.targets_hit.fillna(0) >= 1))
aim["loss"] = su.str.startswith("SL") & ~aim.win
aim = aim[aim.win | aim.loss]
aim["dirsign"] = np.where(aim.direction.str.upper().str.startswith("L"), 1, -1)
aim["pnl"] = (aim.close_price - aim.entry) / aim.entry * 100 * aim.dirsign
hb = aim.groupby(pd.cut(aim.confidence, [0.8, 0.85, 0.9, 0.95, 1.0]), observed=True).agg(
    n=("win", "size"), wr=("win", lambda x: round(100 * x.mean(), 1)),
    avg_pnl=("pnl", lambda x: round(x.mean(), 2)))
print(hb.to_string())
print("→ Fade-PnL wäre ~ -avg_pnl (vor Kosten), Stabilität unklar (OOD-Artefakt)")

sec("H4: Tail-Anatomie — was wäre FIFO mit Time-Stop/Loss-Cap gewesen?")
ct = q("""SELECT strategy, direction, status, entry, close_price, time
          FROM closed_trades_master WHERE strategy='Fast In And Out' AND close_price>0 AND entry>0""")
ct["dirsign"] = np.where(ct.direction.str.upper().str.startswith("L"), 1, -1)
ct["pnl"] = (ct.close_price - ct.entry) / ct.entry * 100 * ct.dirsign
su = ct.status.fillna("").str.upper()
ct = ct[su.isin(["0", "1", "SL0", "SL1"])]
print(f"FIFO n={len(ct)}, avg={ct.pnl.mean():.2f}%, med={ct.pnl.median():.2f}%")
for cap in (-3, -5, -8):
    capped = ct.pnl.clip(lower=cap)
    print(f"  Loss-Cap bei {cap}%: avg={capped.mean():.2f}%  (Anteil gecappt: {100*(ct.pnl<cap).mean():.1f}%)")

conn.close()
print("\nDONE")
