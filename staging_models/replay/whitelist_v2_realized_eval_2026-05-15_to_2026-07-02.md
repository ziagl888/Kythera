# whitelist_v2 Flip — realized decision basis (T-2026-KYT-9050-007)

**Window:** 2026-05-15 00:00:00 → 2026-08-01 22:31:57.404259+00:00 (UTC)
**Snapshot:** 1590 cells, v2 coverage 100.0%, age 0.38h
(analyzer alive)

## 1. Cell divergence (today's snapshot)

| Class | Cells | Share |
|---|---:|---:|
| both_open | 94 | 5.9% |
| both_block | 98 | 6.2% |
| v2_would_block | 1395 | 87.7% |
| v2_would_open | 3 | 0.2% |
| v2_missing | 0 | 0.0% |

## 2. Actual gate traffic

- Events total: **4027**, of which cell-decided (flip-relevant): **2016**
- Gate rate open: v1 **0.0%** → v2 **9.42%**
- ROM1 forwards/day: v1 **40.52** → v2 (forecast) **44.48**
- v1 drift of the snapshot approximation: 1570/2016 = **77.88%** agreement

## 3. What the divergent signals REALIZED

### 3a. Trigger leg (source bot's own trade — symmetric, both sides)

| Class | Events | with leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| v2_would_open | 190 | 190 | 22 | 168 | 52.4 | -349.3 | -2.179 | -56.6 (8) |
| both_block | 1826 | 1823 | 169 | 1654 | 62.9 | 1802.0 | 0.991 | 7596.9 (1149) |
| unaffected | 2011 | 2007 | 794 | 1213 | 56.7 | 361.5 | 0.199 | -3378.3 (906) |

**Flip balance on the trigger leg:** v2 removes Σ 0.0% (0 decided trades), v2 unblocks Σ -349.3% (168) → **Δ -349.3%** (unlevered move).

### 3b. ROM1 leg (the real money — exists only on the forwarded side)

| Class | Events | with ROM1 leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| unaffected | 2011 | 1942 | 849 | 1093 | 65.6 | 1811.1 | 1.560 | 146.4 (1) |

> `v2_would_open` structurally has NO ROM1 leg: these signals were never forwarded, hence never traded as ROM1. The additionally unblocked side is fundamentally not measurable in ROM1 money — only in the trigger leg (3a), and that carries a different geometry (P1.10).

## 3c. Clean vs. drift-contaminated (the reliable subset)

The flip class compares the RECORDED v1 decision with TODAY'S v2 cell. Where today's v1 cell no longer matches the recorded decision, the cell has since moved — then the class compares two different cell states, not v1 against v2. Only `v1_agree` is a clean v1-vs-v2 reading.

| Class | Subset | Events | with leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| v2_would_open | v1_drifted | 190 | 190 | 22 | 168 | 52.4 | -349.3 | -2.179 | -56.6 (8) |

## 4. Which v1 path did the divergent traffic come through?

`insufficient_data` is v1's default-open crutch (n < 30 in the cell), `wr_above_overall` / `counter_trend_specialist` are v1 decisions ON MERIT. The cell matrix and the traffic answer this differently.

### v2_would_open — trigger leg by v1 path

| v1 path | Events | with leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| wr_below_overall | 190 | 190 | 22 | 168 | 52.4 | -349.3 | -2.179 | -56.6 (8) |

## 5. Breakdown by bot × direction

### v2_would_block — trigger leg

_no events in this class._

### v2_would_open — trigger leg

| Bot | Dir | Events | with leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| EPD1 | SHORT | 190 | 190 | 22 | 168 | 52.4 | -349.3 | -2.179 | -56.6 (8) |

## 6. Measurement limits (measured, not assumed)

- Snapshot approximation: `bot_regime_whitelist` is UPSERT-only with no history — the per-event v2 verdict comes from today's snapshot (2026-08-01 22:08:40.703564), not from the state at signal time. The v1 drift (77.88% agreement over 2016 events) measures this error on the only axis where both states are known.
- The historical whitelist is therefore still NOT reconstructable (confirms the T-031 finding): neither `bot_regime_whitelist` nor `bot_regime_performance` keep a history, and bot 28 logs only the v1 path per signal, never the v2 verdict.
- `v2_would_open` has no ROM1 leg — these signals were never traded. The unblocked side is only measurable via the source bot's trade, which carries a DIFFERENT geometry than ROM1 (docs/REGIME_ORCHESTRATOR.md, P1.10).
- Trigger-leg coverage < 100%: unmatched events are counted as `no_twin`, not scored as 0. Causes: signal still open, trade never opened, monitor gap.
- WR is TP1 touch, PnL is the target-staggered unlevered move (core.realized_pnl, T-115 definition). `lev` PnL is exact-only — coverage per row readable via `n_with_leg`.
