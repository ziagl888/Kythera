# K11 · WSH1 — Wick-Reversal Stop-Hunt event study (T-2026-CU-9050-145)

_Generated 2026-07-17T03:07:11.395180+00:00 · read-only 15m event study · fee/side 0.0005 (round-trip 0.0010) · 527 coins · 231,319 all-events / 35,893 cascade-events_

**VERDICT: no-op/WSH1-falsified** · status: `complete`

- grid cells: 24 (2 populations × k[1.5, 2.0, 3.0] × m[3.0, 5.0] × 2 dir) · val-positive: 3 · PASSING (val>0 AND test>0 at n_test≥50): **0**

- best cell selected on VAL: `cascade|k3.0|m5.0|LONG` → val 0.3524% (n=89) · **test -0.2762% (n=872, WR=0.6239)**

## Acceptance Criteria (§K11, binary)

- ✅ Event grid `lower_wick ≥ k·ATR14` k∈{1.5,2,3} × `volume ≥ m·vol_sma20` m∈{3,5} × recovery ≥ 50%  
  _verify: K_GRID/M_GRID/RECOVERY_MIN; trailing_atr14/trailing_vol_sma20 (current-excluded); replay_coin trigger_
- ✅ Mirror upper wick → SHORT; entry = CLOSE of the CLOSED event candle; direction WITH the bounce  
  _verify: replay_coin loops LONG+SHORT; entry=c[i]; include_forming=False (Rule 5)_
- ✅ TWO populations: (a) all events, (b) events ≤60min after pump_dump_events (cascade ⊆ all)  
  _verify: fold_event → 'all' always, 'cascade' iff is_cascade(); spike_time TIMESTAMPTZ/UTC window [entry−60m, entry]_
- ✅ Labels = get_hvn_and_sr_levels(df=as-of) → geometry → simulate_exit, strictly as-of / no lookahead  
  _verify: geo_for(): as-of 15m frame ends at event candle, exit scan starts at i+1, forward-only_
- ✅ Report Rule-8 standard: per-cell n / WR / avg net PnL incl. fees; chrono val/test; selection on val ONLY  
  _verify: _stat_out (n/wr/avg_net_pct); simulate_exit nets round-trip fee; compute_split; derive_verdict picks on val_
- ✅ Stop-criterion: no val+test-positive cell ⇒ falsified (No-op-Done, not forced positive)  
  _verify: derive_verdict → 'no-op/WSH1-falsified' when passing==[]_
- ✅ PEX1 lesson stated: info is intraday; NO 1h fallback  
  _verify: this report §PEX1 + module docstring; all stages on 15m_
- ✅ Survivorship (Rule 9) + closed candles (Rule 5) + 15m sort order documented  
  _verify: §Population & caveats; load_15m sort_values ASC; include_forming=False_
- ✅ Resume/checkpoint machinery: streaming O(cells) accumulators, atomic temp+rename state in OS temp, --resume/--state-path/--checkpoint-every/--progress-every/--skip-cpu-check, RAM guard, peak-RSS, encoding-safe  
  _verify: save_state/load_state (os.replace); DEFAULT_STATE_PATH=tempdir; argparse; _avail_mb guard; _safe()_

**Reuse-vs-Build:** REUSE the tsmom_study.py label+resume machinery wholesale (get_hvn_and_sr_levels→hvn_sr_trade_geometry→ensure_min_tp_distance→simulate_exit, streaming O(cells) accumulators, atomic checkpoint/--resume); BUILD only the 15m wick-geometry + volume-climax event detector and the two-population (all vs cascade) fold.

## Full grid — geometry net PnL, chrono val/test split

| cell | all n | all avg% | all WR | val n | val avg% | test n | test avg% | test WR |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| all|k1.5|m3.0|LONG | 35823 | -0.1688 | 0.6556 | 16408 | -0.3323 | 19415 | -0.0307 | 0.6765 |
| all|k1.5|m3.0|SHORT | 37446 | -0.164 | 0.7387 | 16446 | -0.4311 | 21000 | 0.0451 | 0.7555 |
| all|k1.5|m5.0|LONG | 23648 | -0.149 | 0.6505 | 10706 | -0.4254 | 12942 | 0.0796 | 0.684 |
| all|k1.5|m5.0|SHORT | 25753 | -0.1575 | 0.7413 | 11391 | -0.4231 | 14362 | 0.0531 | 0.7562 |
| all|k2.0|m3.0|LONG | 20448 | -0.2501 | 0.6427 | 9601 | -0.5633 | 10847 | 0.0272 | 0.6871 |
| all|k2.0|m3.0|SHORT | 22532 | -0.2713 | 0.731 | 10047 | -0.6612 | 12485 | 0.0424 | 0.7575 |
| all|k2.0|m5.0|LONG | 15940 | -0.2153 | 0.6414 | 7491 | -0.5014 | 8449 | 0.0383 | 0.6895 |
| all|k2.0|m5.0|SHORT | 18017 | -0.2151 | 0.7381 | 8072 | -0.5766 | 9945 | 0.0784 | 0.7605 |
| all|k3.0|m3.0|LONG | 7459 | -0.0517 | 0.6328 | 3845 | -0.3822 | 3614 | 0.2999 | 0.7072 |
| all|k3.0|m3.0|SHORT | 8984 | -0.3048 | 0.7345 | 3986 | -0.6929 | 4998 | 0.0047 | 0.7557 |
| all|k3.0|m5.0|LONG | 6952 | -0.0351 | 0.632 | 3614 | -0.3343 | 3338 | 0.2889 | 0.7076 |
| all|k3.0|m5.0|SHORT | 8317 | -0.2777 | 0.738 | 3672 | -0.6699 | 4645 | 0.0323 | 0.7576 |
| cascade|k1.5|m3.0|LONG | 3883 | -0.248 | 0.5918 | 389 | -0.2194 | 3494 | -0.2512 | 0.5939 |
| cascade|k1.5|m3.0|SHORT | 5820 | -0.0417 | 0.7273 | 817 | -0.1321 | 5003 | -0.027 | 0.733 |
| cascade|k1.5|m5.0|LONG | 3056 | -0.234 | 0.5923 | 301 | 0.014 | 2755 | -0.2611 | 0.5942 |
| cascade|k1.5|m5.0|SHORT | 4656 | 0.0503 | 0.7337 | 704 | -0.1773 | 3952 | 0.0909 | 0.7409 |
| cascade|k2.0|m3.0|LONG | 2467 | -0.2789 | 0.5971 | 227 | -0.364 | 2240 | -0.2702 | 0.6013 |
| cascade|k2.0|m3.0|SHORT | 4063 | -0.0292 | 0.7325 | 638 | -0.3837 | 3425 | 0.0369 | 0.7431 |
| cascade|k2.0|m5.0|LONG | 2186 | -0.3225 | 0.5947 | 193 | -0.1674 | 1993 | -0.3375 | 0.5981 |
| cascade|k2.0|m5.0|SHORT | 3592 | 0.0955 | 0.7416 | 593 | -0.3976 | 2999 | 0.193 | 0.7536 |
| cascade|k3.0|m3.0|LONG | 1003 | -0.1958 | 0.6271 | 94 | 0.2927 | 909 | -0.2463 | 0.6282 |
| cascade|k3.0|m3.0|SHORT | 2140 | -0.0382 | 0.7379 | 394 | -0.5213 | 1746 | 0.0708 | 0.7503 |
| cascade|k3.0|m5.0|LONG | 961 | -0.218 | 0.6233 | 89 | 0.3524 | 872 | -0.2762 | 0.6239 |
| cascade|k3.0|m5.0|SHORT | 2066 | 0.0583 | 0.742 | 383 | -0.4701 | 1683 | 0.1785 | 0.7546 |

## PEX1 lesson (§K11.5)

The information sits in the INTRADAY window around the event. Event detection, the as-of S/R frame AND the first-touch exit are ALL on 15m — we deliberately do NOT fall back to 1h context features (the falsified PEX1 path). If 15m proves too coarse here, the answer is waiting for ticker_10s / PEX2 maturity, NOT 1h.

## Population & caveats

- run status: complete · coins done: 527 of 527 (universe 527)
- events: 231,319 all · 35,893 cascade (≤60min after a pump_dump_events row)
- peak process RSS: 145.8 MB (streaming accumulators, memory O(cells), not O(events))
- chrono val/test split epoch (UTC): 2026-01-13T14:07:30+00:00 — FIXED calendar midpoint of the BTCUSDT 15m window (longest-history proxy); val=earlier half, test=later half; selection on VAL only
- geometry exit: first-touch TP-vs-SL on 15m candles, 3 published TPs, scan capped 14d; as-of S/R frame = trailing 30d of 15m
- ATR14 & vol_sma20 are TRAILING and EXCLUDE the event candle (rolling.mean().shift(1)); TR = get_atr()'s fmax formula; recovery = (close−low)/(high−low)≥0.5 (range-half operationalization)
- fees (Rule 10): FEE_PER_SIDE=0.0005 netted inside simulate_exit (round-trip 0.001)
- **Survivorship bias (Rule 9)**: coins.json lists ACTIVE USDT-perps; delisted coins are absent → the population skews to survivors. Documented, not corrected.
- **Only closed candles (Rule 5)**: read_candles(include_forming=False); ATR/vol baselines are trailing/as-of (no lookahead); the exit scan starts strictly AFTER the entry candle.
- **15m sort order**: load_15m sorts ASC by open_time before array-izing; the exit scan and searchsorted assume ascending time — indexing was NOT 'simplified' without checking direction.
- **WR is not decisive (Rule 8)**: the verdict rests on net-PnL expectancy consistent across the chrono val/test halves.
- CPU-check override: --skip-cpu-check=True (VPS is CPU-saturated; the walkforward_sim guard would abort this read-only BELOW_NORMAL job).