import os
# Step 4b: dedup impact + classic part (read-only)
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
FEE_RT = 0.10

ai = q("""SELECT symbol, model, direction, status, targets_hit, entry, close_price, open_time, close_time
          FROM closed_ai_signals""")
ai["dirsign"] = np.where(ai.direction.str.upper().str.startswith("L"), 1, -1)
ai["pnl_pct"] = (ai.close_price - ai.entry) / ai.entry * 100 * ai.dirsign
s = ai.status.fillna("").str.upper()
ai["cls"] = "other"
ai.loc[s.str.contains("DELISTED|CLEANUP|REGIME|EXPIRED"), "cls"] = "censored"
ai.loc[s.str.startswith("LEGACY"), "cls"] = "legacy"
ai.loc[(ai.cls == "other") & (s.str.contains("ALL TARGETS") | (ai.targets_hit.fillna(0) >= 1)), "cls"] = "win"
ai.loc[(ai.cls == "other") & s.str.startswith("SL"), "cls"] = "loss"

sec("Dedup-Analyse: wer erzeugt die Duplikate?")
grp = ai.groupby(["symbol", "model", "direction", "open_time"]).size()
dups = grp[grp > 1]
print(f"Gruppen gesamt: {len(grp)}, Duplikat-Gruppen: {len(dups)}, überzählige Rows: {int((dups-1).sum())} von {len(ai)}")
dd = dups.reset_index(name="c").groupby("model").agg(groups=("c", "size"), extra_rows=("c", lambda x: int((x - 1).sum())))
print(dd.sort_values("extra_rows", ascending=False).head(12).to_string())
sent = ai[ai.open_time == pd.Timestamp("2026-02-24 12:43:59.650539")]
print(f"\nRows mit Sentinel-open_time 2026-02-24 12:43:59.65: {len(sent)} (Migrations-/Default-Zeitstempel?)")

sec("AI-Ergebnisse DEDUPLIZIERT (erste Close-Row je Gruppe; aktive Ära, win/loss)")
ai_sorted = ai.sort_values("close_time")
dedup = ai_sorted.drop_duplicates(subset=["symbol", "model", "direction", "open_time"], keep="first")
cur = dedup[(dedup.entry > 0) & (dedup.close_price > 0) & dedup.cls.isin(["win", "loss"])]
g = cur.groupby("model").agg(
    n=("pnl_pct", "size"),
    wr=("cls", lambda x: round(100 * (x == "win").mean(), 1)),
    avg_pnl=("pnl_pct", lambda x: round(x.mean(), 2)),
    med_pnl=("pnl_pct", lambda x: round(x.median(), 2)),
    sum_pnl=("pnl_pct", lambda x: round(x.sum(), 0)))
g["sum_net"] = (g.sum_pnl - FEE_RT * g.n).round(0)
print(g.sort_values("sum_net", ascending=False).to_string())
print(f"\nTOTAL dedup: n={len(cur)}, WR={100*(cur.cls=='win').mean():.1f}%, avg {cur.pnl_pct.mean():.2f}%, "
      f"Summe {cur.pnl_pct.sum():.0f}% (netto {cur.pnl_pct.sum()-FEE_RT*len(cur):.0f}%)")

leg = dedup[(dedup.cls == "legacy") & (dedup.entry > 0) & (dedup.close_price > 0)]
legwin = leg.status.str.upper().str.contains("TARGET")
print(f"\nLEGACY dedup: n={len(leg)}, TARGET-HIT-Quote {100*legwin.mean():.1f}%, avg {leg.pnl_pct.mean():.2f}%, Summe {leg.pnl_pct.sum():.0f}%")
print("LEGACY Zeitraum:", str(leg.close_time.min())[:10], "bis", str(leg.close_time.max())[:10])
legm = leg.copy(); legm["month"] = pd.to_datetime(legm.close_time).dt.to_period("M").astype(str)
print(legm.groupby("month").agg(n=("pnl_pct", "size"), avg=("pnl_pct", lambda x: round(x.mean(), 2)), sum=("pnl_pct", lambda x: round(x.sum(), 0))).to_string())

# ============ CLASSIC ============
ct = q("""SELECT strategy, coin, direction, status, entry, close_price, sl, time, posted
          FROM closed_trades_master""")
ct["dirsign"] = np.where(ct.direction.str.upper().str.startswith("L"), 1, -1)
ct["pnl_pct"] = (ct.close_price - ct.entry) / ct.entry * 100 * ct.dirsign

sec("Classic-Anomalien / Integrität (closed_trades_master)")
print({
    "rows_total": len(ct),
    "entry<=0_or_null": int(((ct.entry <= 0) | ct.entry.isna()).sum()),
    "close<=0_or_null": int(((ct.close_price <= 0) | ct.close_price.isna()).sum()),
    "abs_pnl>50%": int((ct.pnl_pct.abs() > 50).sum()),
})
grpc = ct.groupby(["strategy", "coin", "direction", "time"]).size()
dupc = grpc[grpc > 1]
print(f"Duplikat-Gruppen: {len(dupc)}, überzählige Rows: {int((dupc-1).sum())}")

ctd = ct.sort_values("posted").drop_duplicates(subset=["strategy", "coin", "direction", "time"], keep="first")
validc = ctd[(ctd.entry > 0) & (ctd.close_price > 0)].copy()
su = validc.status.fillna("").str.upper()
validc["cls"] = np.select(
    [su.str.contains("DELISTED|REGIME|FORCE"),
     su.isin(["1", "2", "3", "4", "SL1", "SL2", "SL3"]),
     su.isin(["0", "SL0"])],
    ["censored", "win", "loss"], default="other")

sec("Classic DEDUP: Ergebnisse pro Strategie (win = >=TP1)")
cc = validc[validc.cls.isin(["win", "loss"])]
gc = cc.groupby("strategy").agg(
    n=("pnl_pct", "size"),
    wr=("cls", lambda x: round(100 * (x == "win").mean(), 1)),
    avg_pnl=("pnl_pct", lambda x: round(x.mean(), 2)),
    med_pnl=("pnl_pct", lambda x: round(x.median(), 2)),
    p5=("pnl_pct", lambda x: round(x.quantile(.05), 1)),
    p95=("pnl_pct", lambda x: round(x.quantile(.95), 1)),
    sum_pnl=("pnl_pct", lambda x: round(x.sum(), 0)))
gc["sum_net"] = (gc.sum_pnl - FEE_RT * gc.n).round(0)
print(gc.sort_values("sum_net", ascending=False).to_string())
print(f"\nTOTAL Classic dedup: n={len(cc)}, WR={100*(cc.cls=='win').mean():.1f}%, avg {cc.pnl_pct.mean():.2f}%, Summe {cc.pnl_pct.sum():.0f}%")

sec("Classic: Status-vs-PnL-Konsistenz")
for lab, sub in (("status=win aber pnl<-0.5%", cc[(cc.cls == "win") & (cc.pnl_pct < -0.5)]),
                 ("status=loss aber pnl>+0.5%", cc[(cc.cls == "loss") & (cc.pnl_pct > 0.5)])):
    print(f"{lab}: {len(sub)} ({100*len(sub)/max(len(cc),1):.1f}%), avg={sub.pnl_pct.mean():.2f}%")

sec("Classic: Monatstrend (n | WR% | sum_pnl%)")
cc2 = cc.copy(); cc2["month"] = pd.to_datetime(cc2.time).dt.to_period("M").astype(str)
print(cc2.groupby(["strategy", "month"]).agg(n=("cls", "size"),
    wr=("cls", lambda x: round(100 * (x == "win").mean(), 0)),
    pnl=("pnl_pct", lambda x: round(x.sum(), 0))).to_string())

sec("Classic: Direction-Split")
dc = cc.groupby(["strategy", "direction"]).agg(n=("cls", "size"), wr=("cls", lambda x: round(100 * (x == "win").mean(), 1)),
                                               avg=("pnl_pct", lambda x: round(x.mean(), 2)))
print(dc.to_string())

sec("Classic: Zensur-Anteile (censored inkl. FORCE_CLOSED)")
print(validc.groupby("strategy").cls.value_counts(normalize=True).mul(100).round(1).unstack(fill_value=0).to_string())

conn.close()
print("\nDONE")
