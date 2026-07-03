import os
# Step 4: per-bot/strategy realized results + integrity checks (read-only)
import warnings
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

FEE_RT = 0.10  # taker round-trip %, assumption

# ============ AI BOTS ============
ai = q("""SELECT model, direction, status, targets_hit, entry, close_price, open_time, close_time
          FROM closed_ai_signals""")
ai["dirsign"] = np.where(ai.direction.str.upper().str.startswith("L"), 1, -1)
ai["pnl_pct"] = (ai.close_price - ai.entry) / ai.entry * 100 * ai.dirsign
s = ai.status.fillna("").str.upper()
ai["cls"] = "other"
ai.loc[s.str.contains("DELISTED|CLEANUP|REGIME|EXPIRED"), "cls"] = "censored"
ai.loc[s.str.startswith("LEGACY"), "cls"] = "legacy"
ai.loc[(ai.cls == "other") & (s.str.contains("ALL TARGETS") | (ai.targets_hit.fillna(0) >= 1)), "cls"] = "win"
ai.loc[(ai.cls == "other") & s.str.startswith("SL"), "cls"] = "loss"

sec("AI-Anomalien / Integrität (closed_ai_signals)")
anom = {
    "rows_total": len(ai),
    "entry<=0_or_null": int(((ai.entry <= 0) | ai.entry.isna()).sum()),
    "close<=0_or_null": int(((ai.close_price <= 0) | ai.close_price.isna()).sum()),
    "abs_pnl>50%": int((ai.pnl_pct.abs() > 50).sum()),
    "abs_pnl>90%": int((ai.pnl_pct.abs() > 90).sum()),
}
print(anom)
dup = q("""SELECT count(*) FROM (SELECT symbol, model, direction, open_time, count(*) c
           FROM closed_ai_signals GROUP BY 1,2,3,4 HAVING count(*)>1) d""")
print("duplicate close-groups (symbol+model+dir+open_time, P2.8):", int(dup.iloc[0, 0]))
dup2 = q("""SELECT symbol, model, direction, open_time, count(*) c FROM closed_ai_signals
            GROUP BY 1,2,3,4 HAVING count(*)>1 ORDER BY c DESC LIMIT 5""")
print(dup2.to_string(index=False))

valid = ai[(ai.entry > 0) & (ai.close_price > 0)]
sec("AI-Bots: realisierte Ergebnisse pro Modell (aktive Ära, ohne LEGACY; win = >=TP1)")
cur = valid[valid.cls.isin(["win", "loss"])]
g = cur.groupby("model").agg(
    n=("pnl_pct", "size"),
    wr=("cls", lambda x: round(100 * (x == "win").mean(), 1)),
    avg_pnl=("pnl_pct", lambda x: round(x.mean(), 2)),
    med_pnl=("pnl_pct", lambda x: round(x.median(), 2)),
    p5=("pnl_pct", lambda x: round(x.quantile(.05), 1)),
    p95=("pnl_pct", lambda x: round(x.quantile(.95), 1)),
    sum_pnl=("pnl_pct", lambda x: round(x.sum(), 0)),
)
g["avg_net"] = (g.avg_pnl - FEE_RT).round(2)
g["sum_net"] = (g.sum_pnl - FEE_RT * g.n).round(0)
g = g.sort_values("sum_net", ascending=False)
print(g.to_string())
tot = cur.pnl_pct
print(f"\nTOTAL aktive Ära: n={len(cur)}, WR={100*(cur.cls=='win').mean():.1f}%, "
      f"avg {tot.mean():.2f}%/Trade (netto {tot.mean()-FEE_RT:.2f}%), Summe {tot.sum():.0f}% (netto {tot.sum()-FEE_RT*len(cur):.0f}%)")

sec("AI: Zensur-Anteile pro Modell (REGIME/DELISTED etc. — P1.9-Bias)")
cz = valid[valid.cls.isin(["win", "loss", "censored"])]
z = cz.groupby("model").agg(n=("cls", "size"),
                            censored_pct=("cls", lambda x: round(100 * (x == "censored").mean(), 1)),
                            cens_avg_pnl=("pnl_pct", lambda x: round(x[cz.loc[x.index, "cls"] == "censored"].mean(), 2)))
print(z.sort_values("censored_pct", ascending=False).head(12).to_string())

sec("AI: Richtungssplit (LONG vs SHORT WR) — Top-Modelle")
d = cur.groupby(["model", "direction"]).agg(n=("cls", "size"), wr=("cls", lambda x: round(100 * (x == "win").mean(), 1)))
d = d.unstack("direction")
d.columns = [f"{a}_{b}" for a, b in d.columns]
print(d.loc[cur.model.value_counts().head(10).index].to_string())

sec("AI: Monatstrend (WR% | sum_pnl%) — Top-8-Modelle")
cur2 = cur.copy()
cur2["month"] = pd.to_datetime(cur2.close_time).dt.to_period("M").astype(str)
top8 = cur.model.value_counts().head(8).index
mt = cur2[cur2.model.isin(top8)].groupby(["model", "month"]).agg(
    n=("cls", "size"), wr=("cls", lambda x: round(100 * (x == "win").mean(), 0)),
    pnl=("pnl_pct", lambda x: round(x.sum(), 0)))
print(mt.to_string())

sec("AI: LEGACY-Ära separat (Scoring aus Status-Text)")
leg = valid[valid.cls == "legacy"]
legwin = leg.status.str.upper().str.contains("TARGET")
print(f"LEGACY rows: {len(leg)}, 'TARGET HIT': {legwin.sum()} ({100*legwin.mean():.1f}%), "
      f"avg pnl {leg.pnl_pct.mean():.2f}%, Summe {leg.pnl_pct.sum():.0f}%")
print("Zeitraum:", leg.close_time.min(), "→", leg.close_time.max())

sec("ROM1 vs. gespiegelte Bots (Orchestrator-Mehrwert)")
rom = cur[cur.model == "ROM1"]
print(f"ROM1: n={len(rom)}, WR={100*(rom.cls=='win').mean():.1f}%, avg {rom.pnl_pct.mean():.2f}%, sum {rom.pnl_pct.sum():.0f}%")

# ============ CLASSIC ============
ct = q("""SELECT strategy, direction, status, entry, close_price, sl, time, posted
          FROM closed_trades_master""")
ct["dirsign"] = np.where(ct.direction.str.upper().str.startswith("L"), 1, -1)
ct["pnl_pct"] = (ct.close_price - ct.entry) / ct.entry * 100 * ct.dirsign

sec("Classic-Anomalien / Integrität (closed_trades_master)")
anom2 = {
    "rows_total": len(ct),
    "entry<=0_or_null": int(((ct.entry <= 0) | ct.entry.isna()).sum()),
    "close<=0_or_null": int(((ct.close_price <= 0) | ct.close_price.isna()).sum()),
    "abs_pnl>50%": int((ct.pnl_pct.abs() > 50).sum()),
    "close_at_exactly_0": int((ct.close_price == 0).sum()),
}
print(anom2)
dupc = q("""SELECT count(*) FROM (SELECT strategy, coin, direction, time, count(*) c
            FROM closed_trades_master GROUP BY 1,2,3,4 HAVING count(*)>1) d""")
print("duplicate close-groups (strategy+coin+dir+time, P2.8):", int(dupc.iloc[0, 0]))

validc = ct[(ct.entry > 0) & (ct.close_price > 0)]
su = validc.status.fillna("").str.upper()
validc = validc.assign(cls=np.select(
    [su.str.contains("DELISTED|REGIME|FORCE"),
     su.isin(["1", "2", "3", "4", "SL1", "SL2", "SL3"]),
     su.isin(["0", "SL0"])],
    ["censored", "win", "loss"], default="other"))

sec("Classic: Ergebnisse pro Strategie (win = >=TP1 [status 1-4/SL1-3], loss = 0/SL0)")
cc = validc[validc.cls.isin(["win", "loss"])]
gc = cc.groupby("strategy").agg(
    n=("pnl_pct", "size"),
    wr=("cls", lambda x: round(100 * (x == "win").mean(), 1)),
    avg_pnl=("pnl_pct", lambda x: round(x.mean(), 2)),
    med_pnl=("pnl_pct", lambda x: round(x.median(), 2)),
    p5=("pnl_pct", lambda x: round(x.quantile(.05), 1)),
    p95=("pnl_pct", lambda x: round(x.quantile(.95), 1)),
    sum_pnl=("pnl_pct", lambda x: round(x.sum(), 0)))
gc["avg_net"] = (gc.avg_pnl - FEE_RT).round(2)
gc["sum_net"] = (gc.sum_pnl - FEE_RT * gc.n).round(0)
print(gc.sort_values("sum_net", ascending=False).to_string())

sec("Classic: Konsistenz Status vs. realisierte PnL (Misklassifikations-Check)")
for lab, sub in (("status=win aber pnl<0", cc[(cc.cls == "win") & (cc.pnl_pct < -0.5)]),
                 ("status=loss aber pnl>0", cc[(cc.cls == "loss") & (cc.pnl_pct > 0.5)])):
    print(f"{lab}: {len(sub)} rows ({100*len(sub)/max(len(cc),1):.1f}%)  avg_pnl={sub.pnl_pct.mean():.2f}%")
    if len(sub):
        print(sub.groupby("strategy").size().sort_values(ascending=False).head(5).to_dict())

sec("Classic: Monatstrend (n | WR% | sum_pnl%)")
cc2 = cc.copy()
cc2["month"] = pd.to_datetime(cc2.time).dt.to_period("M").astype(str)
mtc = cc2.groupby(["strategy", "month"]).agg(n=("cls", "size"),
                                             wr=("cls", lambda x: round(100 * (x == "win").mean(), 0)),
                                             pnl=("pnl_pct", lambda x: round(x.sum(), 0)))
print(mtc.to_string())

sec("Classic: Zensur/FORCE_CLOSED-Anteile")
zc = validc.groupby("strategy").cls.value_counts(normalize=True).mul(100).round(1).unstack(fill_value=0)
print(zc.to_string())

sec("Classic: sl<=0-Ära vs. danach (P2.9-Kontext)")
ct["has_sl"] = ct.sl > 0
per = ct.groupby("has_sl").agg(n=("pnl_pct", "size"), tmin=("time", "min"), tmax=("time", "max"))
print(per.to_string())

conn.close()
print("\nDONE")
