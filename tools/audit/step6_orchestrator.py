import os
# Step 6: regime orchestrator empirical analysis (read-only)
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

sec("Throughput: forwards vs suppressions over time (month)")
f = q("SELECT date_trunc('month', opened_at) m, count(*) fwd FROM orchestrator_open_trades GROUP BY 1 ORDER BY 1")
sup = q("SELECT date_trunc('month', ts) m, count(*) sup FROM orchestrator_suppressed_signals GROUP BY 1 ORDER BY 1")
tp = f.merge(sup, on="m", how="outer").fillna(0)
tp["gate_rate_%"] = (100 * tp.sup / (tp.sup + tp.fwd)).round(1)
print(tp.to_string(index=False))

sec("Suppression reasons over time")
sr = q("""SELECT date_trunc('month', ts) m, split_part(reason, ':', 1) r, count(*)
          FROM orchestrator_suppressed_signals GROUP BY 1,2 ORDER BY 1,3 DESC""")
print(sr.pivot_table(index="m", columns="r", values="count", fill_value=0).to_string())

sec("Whitelist quality: reason/whitelisted distribution of FRESH rows (pretty names)")
w = q("""SELECT whitelisted, reason, count(*) FROM bot_regime_whitelist
         WHERE computed_at > NOW() - interval '2 days' GROUP BY 1,2 ORDER BY 3 DESC LIMIT 12""")
print(w.to_string(index=False))
w2 = q("""SELECT regime, alt_context, count(*) FILTER (WHERE whitelisted) wl_true, count(*) n
          FROM bot_regime_whitelist WHERE computed_at > NOW() - interval '2 days'
          GROUP BY 1,2 ORDER BY 1,2""")
print(w2.to_string(index=False))

sec("bot_regime_performance: cell occupancy (data basis of the whitelist)")
try:
    bp = q("SELECT * FROM bot_regime_performance LIMIT 3")
    print("Columns:", list(bp.columns))
    bp2 = q("""SELECT count(*) cells,
                      count(*) FILTER (WHERE total_trades < 20) under20,
                      count(*) FILTER (WHERE total_trades < 50) under50,
                      percentile_disc(0.5) WITHIN GROUP (ORDER BY total_trades) med_trades,
                      max(computed_at) freshest
               FROM bot_regime_performance""")
    print(bp2.to_string(index=False))
except Exception as e:
    print("Schema different:", e)

sec("Regime durations (debounced would be regime_current; here: raw regime_history episodes)")
rh = q("SELECT ts, regime, alt_context, confidence FROM regime_history ORDER BY ts")
rh["ep"] = (rh.regime != rh.regime.shift()).cumsum()
ep = rh.groupby("ep").agg(regime=("regime", "first"), start=("ts", "first"), end=("ts", "last"), n=("ts", "size"))
ep["dur_h"] = (ep.end - ep.start).dt.total_seconds() / 3600
print(ep.groupby("regime").agg(episodes=("dur_h", "size"),
                               med_dur_h=("dur_h", lambda x: round(x.median(), 1)),
                               p90_dur_h=("dur_h", lambda x: round(x.quantile(.9), 1))).to_string())
short = ep[ep.dur_h < 1]
print(f"Episodes <1h (flaps): {len(short)}/{len(ep)} ({100*len(short)/len(ep):.0f}%)")
print("Time range regime_history:", rh.ts.min(), "-", rh.ts.max())
print("confidence: med", rh.confidence.median(), "p10", rh.confidence.quantile(.1).round(2))

sec("Auto-close evaluation: PnL at the close time of REGIME_CHANGE closes")
rc = q("""SELECT model, direction, entry, close_price, targets_hit FROM closed_ai_signals
          WHERE status ILIKE '%%REGIME%%' AND entry>0 AND close_price>0""")
rc["dirsign"] = np.where(rc.direction.str.upper().str.startswith("L"), 1, -1)
rc["pnl"] = (rc.close_price - rc.entry) / rc.entry * 100 * rc.dirsign
print(f"n={len(rc)}, avg PnL at auto-close: {rc.pnl.mean():.2f}%, median {rc.pnl.median():.2f}%, "
      f"closed in profit: {100*(rc.pnl>0).mean():.1f}%, targets_hit>=1: {100*(rc.targets_hit.fillna(0)>=1).mean():.1f}%")
print(rc.groupby("model").agg(n=("pnl", "size"), avg=("pnl", lambda x: round(x.mean(), 2))).sort_values("n", ascending=False).head(8).to_string())

sec("ROM1 lifecycle: open/closed orchestrator trades + age")
oo = q("""SELECT status, count(*), min(opened_at)::date, max(opened_at)::date FROM orchestrator_open_trades GROUP BY 1""")
print(oo.to_string(index=False))
stale = q("""SELECT count(*) FROM orchestrator_open_trades WHERE status='OPEN' AND opened_at < NOW() - interval '7 days'""")
print("OPEN older than 7 days:", int(stale.iloc[0, 0]))

sec("bot_unidentified: which channels slip through identify_bot?")
bu = q("""SELECT o.channel_id, count(*) FROM orchestrator_suppressed_signals s
          JOIN telegram_outbox o ON o.id=s.original_outbox_id
          WHERE s.reason='bot_unidentified' GROUP BY 1 ORDER BY 2 DESC LIMIT 10""")
print(bu.to_string(index=False))

conn.close()
print("\nDONE")
