# K11 · WSH1 — Wick-Reversal Stop-Hunt event study (T-2026-CU-9050-145)

_Generated 2026-07-17T00:35:16.602342+00:00 · read-only 15m event study · fee/side 0.0005 (round-trip 0.0010) · 4 coins · 1,644 all-events / 36 cascade-events_

**VERDICT: no-op/WSH1-falsified** · status: `partial (sampling cap)`

- grid cells: 24 (2 populations × k[1.5, 2.0, 3.0] × m[3.0, 5.0] × 2 dir) · val-positive: 9 · PASSING (val>0 AND test>0 at n_test≥50): **0**

- best cell selected on VAL: `all|k3.0|m3.0|LONG` → val 0.7624% (n=50) · **test -0.4613% (n=24, WR=0.75)**

## Acceptance Criteria (§K11, binary)

- ☐ Event grid `lower_wick ≥ k·ATR14` k∈{1.5,2,3} × `volume ≥ m·vol_sma20` m∈{3,5} × recovery ≥ 50%  
  _verify: K_GRID/M_GRID/RECOVERY_MIN; trailing_atr14/trailing_vol_sma20 (current-excluded); replay_coin trigger_
- ☐ Mirror upper wick → SHORT; entry = CLOSE of the CLOSED event candle; direction WITH the bounce  
  _verify: replay_coin loops LONG+SHORT; entry=c[i]; include_forming=False (Rule 5)_
- ☐ TWO populations: (a) all events, (b) events ≤60min after pump_dump_events (cascade ⊆ all)  
  _verify: fold_event → 'all' always, 'cascade' iff is_cascade(); spike_time TIMESTAMPTZ/UTC window [entry−60m, entry]_
- ☐ Labels = get_hvn_and_sr_levels(df=as-of) → geometry → simulate_exit, strictly as-of / no lookahead  
  _verify: geo_for(): as-of 15m frame ends at event candle, exit scan starts at i+1, forward-only_
- ☐ Report Rule-8 standard: per-cell n / WR / avg net PnL incl. fees; chrono val/test; selection on val ONLY  
  _verify: _stat_out (n/wr/avg_net_pct); simulate_exit nets round-trip fee; compute_split; derive_verdict picks on val_
- ☐ Stop-criterion: no val+test-positive cell ⇒ falsified (No-op-Done, not forced positive)  
  _verify: derive_verdict → 'no-op/WSH1-falsified' when passing==[]_
- ☐ PEX1 lesson stated: info is intraday; NO 1h fallback  
  _verify: this report §PEX1 + module docstring; all stages on 15m_
- ☐ Survivorship (Rule 9) + closed candles (Rule 5) + 15m sort order documented  
  _verify: §Population & caveats; load_15m sort_values ASC; include_forming=False_
- ☐ Resume/checkpoint machinery: streaming O(cells) accumulators, atomic temp+rename state in OS temp, --resume/--state-path/--checkpoint-every/--progress-every/--skip-cpu-check, RAM guard, peak-RSS, encoding-safe  
  _verify: save_state/load_state (os.replace); DEFAULT_STATE_PATH=tempdir; argparse; _avail_mb guard; _safe()_

_Note: this artifact is a SMOKE (sampling-capped); functional criteria are exercised end-to-end but the VERDICT is not universe-final until the full run._

**Reuse-vs-Build:** REUSE the tsmom_study.py label+resume machinery wholesale (get_hvn_and_sr_levels→hvn_sr_trade_geometry→ensure_min_tp_distance→simulate_exit, streaming O(cells) accumulators, atomic checkpoint/--resume); BUILD only the 15m wick-geometry + volume-climax event detector and the two-population (all vs cascade) fold.

## Full grid — geometry net PnL, chrono val/test split

| cell | all n | all avg% | all WR | val n | val avg% | test n | test avg% | test WR |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| all|k1.5|m3.0|LONG | 271 | -0.3075 | 0.7122 | 151 | -0.0462 | 120 | -0.6362 | 0.7 |
| all|k1.5|m3.0|SHORT | 240 | -0.2132 | 0.7708 | 119 | -0.6877 | 121 | 0.2534 | 0.8182 |
| all|k1.5|m5.0|LONG | 183 | -0.4056 | 0.6885 | 107 | 0.0765 | 76 | -1.0843 | 0.6447 |
| all|k1.5|m5.0|SHORT | 154 | -0.1053 | 0.8052 | 78 | -0.4679 | 76 | 0.2669 | 0.8553 |
| all|k2.0|m3.0|LONG | 168 | -0.0608 | 0.7143 | 96 | 0.3237 | 72 | -0.5734 | 0.7083 |
| all|k2.0|m3.0|SHORT | 152 | -0.2359 | 0.7829 | 75 | -1.1035 | 77 | 0.6092 | 0.8701 |
| all|k2.0|m5.0|LONG | 137 | -0.0535 | 0.708 | 80 | 0.2853 | 57 | -0.529 | 0.7193 |
| all|k2.0|m5.0|SHORT | 108 | -0.2474 | 0.787 | 55 | -0.9113 | 53 | 0.4415 | 0.8679 |
| all|k3.0|m3.0|LONG | 74 | 0.3655 | 0.7162 | 50 | 0.7624 | 24 | -0.4613 | 0.75 |
| all|k3.0|m3.0|SHORT | 45 | -0.6197 | 0.7556 | 24 | -1.3717 | 21 | 0.2398 | 0.8571 |
| all|k3.0|m5.0|LONG | 70 | 0.2734 | 0.7 | 47 | 0.695 | 23 | -0.5881 | 0.7391 |
| all|k3.0|m5.0|SHORT | 42 | -0.7796 | 0.7381 | 22 | -1.6583 | 20 | 0.187 | 0.85 |
| cascade|k1.5|m3.0|LONG | 4 | -0.0236 | 0.75 | 1 | 1.4347 | 3 | -0.5097 | 0.6667 |
| cascade|k1.5|m3.0|SHORT | 6 | -1.4919 | 0.5 | 0 | None | 6 | -1.4919 | 0.5 |
| cascade|k1.5|m5.0|LONG | 4 | -0.0236 | 0.75 | 1 | 1.4347 | 3 | -0.5097 | 0.6667 |
| cascade|k1.5|m5.0|SHORT | 5 | -2.0598 | 0.4 | 0 | None | 5 | -2.0598 | 0.4 |
| cascade|k2.0|m3.0|LONG | 2 | 2.2637 | 1.0 | 0 | None | 2 | 2.2637 | 1.0 |
| cascade|k2.0|m3.0|SHORT | 4 | 0.2673 | 0.75 | 0 | None | 4 | 0.2673 | 0.75 |
| cascade|k2.0|m5.0|LONG | 2 | 2.2637 | 1.0 | 0 | None | 2 | 2.2637 | 1.0 |
| cascade|k2.0|m5.0|SHORT | 3 | -0.0928 | 0.6667 | 0 | None | 3 | -0.0928 | 0.6667 |
| cascade|k3.0|m3.0|LONG | 1 | 1.0649 | 1.0 | 0 | None | 1 | 1.0649 | 1.0 |
| cascade|k3.0|m3.0|SHORT | 2 | 2.1236 | 1.0 | 1 | 2.7429 | 1 | 1.5043 | 1.0 |
| cascade|k3.0|m5.0|LONG | 1 | 1.0649 | 1.0 | 0 | None | 1 | 1.0649 | 1.0 |
| cascade|k3.0|m5.0|SHORT | 2 | 2.1236 | 1.0 | 1 | 2.7429 | 1 | 1.5043 | 1.0 |

## PEX1 lesson (§K11.5)

The information sits in the INTRADAY window around the event. Event detection, the as-of S/R frame AND the first-touch exit are ALL on 15m — we deliberately do NOT fall back to 1h context features (the falsified PEX1 path). If 15m proves too coarse here, the answer is waiting for ticker_10s / PEX2 maturity, NOT 1h.

## Population & caveats

- run status: partial (sampling cap) · coins done: 4 of 4 (universe 527)
- events: 1,644 all · 36 cascade (≤60min after a pump_dump_events row)
- peak process RSS: 134.2 MB (streaming accumulators, memory O(cells), not O(events))
- chrono val/test split epoch (UTC): 2026-01-13T13:45:00+00:00 — FIXED calendar midpoint of the BTCUSDT 15m window (longest-history proxy); val=earlier half, test=later half; selection on VAL only
- geometry exit: first-touch TP-vs-SL on 15m candles, 3 published TPs, scan capped 14d; as-of S/R frame = trailing 30d of 15m
- ATR14 & vol_sma20 are TRAILING and EXCLUDE the event candle (rolling.mean().shift(1)); TR = get_atr()'s fmax formula; recovery = (close−low)/(high−low)≥0.5 (range-half operationalization)
- fees (Rule 10): FEE_PER_SIDE=0.0005 netted inside simulate_exit (round-trip 0.001)
- **Survivorship bias (Rule 9)**: coins.json lists ACTIVE USDT-perps; delisted coins are absent → the population skews to survivors. Documented, not corrected.
- **Only closed candles (Rule 5)**: read_candles(include_forming=False); ATR/vol baselines are trailing/as-of (no lookahead); the exit scan starts strictly AFTER the entry candle.
- **15m sort order**: load_15m sorts ASC by open_time before array-izing; the exit scan and searchsorted assume ascending time — indexing was NOT 'simplified' without checking direction.
- **WR is not decisive (Rule 8)**: the verdict rests on net-PnL expectancy consistent across the chrono val/test halves.
- CPU-check override: --skip-cpu-check=True (VPS is CPU-saturated; the walkforward_sim guard would abort this read-only BELOW_NORMAL job).
- ⚠ SAMPLING CAP: --limit-symbols=4 (NOT a full run; VERDICT not universe-final).