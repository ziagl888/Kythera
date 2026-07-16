# K7 · SKW1 — realized-skewness study (T-2026-CU-9050-141)

_Generated 2026-07-16T23:18:44.183618+00:00 · read-only · status=complete · coins_loaded=527/530 · weeks=51 · rows=15923 · peak RSS 163.2 MB_

**VERDICT: skw1-robust-spread**

- SKW1 L/S net spread (short high-positive-skew, long low-skew; market-neutral, liquidity-filtered, funding-costed, fees): FULL **0.02504**/week · val 0.02513 · test 0.02484 (≥0.001/week AND both halves >0 required: True)

> The SKW1 L/S net spread (short high-positive-skew, long low-skew) is +0.02504/week and stays net-positive in BOTH chrono halves (val 0.02513, test 0.02484) — an OOS sign-stable, funding/fee-aware edge. Deployment as a short-candidate filter remains an operator decision (Michi); the moment feature-block is now also a validated retrain input.

Realized SKEWNESS sort (not MAX/lottery — §K7 F6). Realized moments from 15m closed bars (R1); native NaN, never fillna(0) (P1.20). Survivorship-biased (active USDT-perps only); funding for coins without history contributes 0 (documented, not imputed).

## SKW1 long/short spread — primary sort `mom_skew_7d`

- fee drag/week: 0.002 · weeks used: 51
| slice | n wk | gross | funding | **net** | median net | weeks net+ |
|---|--:|--:|--:|--:|--:|--:|
| all | 51 | 0.03172 | -0.004685 | **0.02504** | 0.0143 | 0.6471 |
| val_first70pct | 35 | 0.03359 | -0.006465 | **0.02513** | 0.01007 | 0.6 |
| test_last30pct | 16 | 0.02764 | -0.000791 | **0.02484** | 0.02176 | 0.75 |

## Byproduct — L/S spread on 24h-skew

- `mom_skew_24h`: FULL net 0.0128 · val 0.01599 · test 0.00583 (51 wk)

## Decile sorts — mean market-neutral forward return (incl. RV/kurtosis byproduct)

### `mom_skew_7d` (51 weeks ≥ 20 coins · monotonicity ρ(decile,mn-ret)=-0.8808)

| decile | n | avg mn-ret | WR |
|--:|--:|--:|--:|
| 0 | 1614 | 0.01508 | 0.4195 |
| 1 | 1589 | 0.00924 | 0.3807 |
| 2 | 1598 | -0.00328 | 0.383 |
| 3 | 1584 | -0.00317 | 0.3788 |
| 4 | 1592 | 0.00143 | 0.3631 |
| 5 | 1594 | -0.00505 | 0.3827 |
| 6 | 1593 | -0.00316 | 0.376 |
| 7 | 1589 | -0.00512 | 0.3763 |
| 8 | 1598 | -0.00919 | 0.3736 |
| 9 | 1572 | -0.01493 | 0.341 |

### `mom_skew_24h` (51 weeks ≥ 20 coins · monotonicity ρ(decile,mn-ret)=-0.1716)

| decile | n | avg mn-ret | WR |
|--:|--:|--:|--:|
| 0 | 1614 | -0.00123 | 0.368 |
| 1 | 1589 | -0.00329 | 0.3858 |
| 2 | 1598 | 0.00873 | 0.378 |
| 3 | 1584 | -0.007 | 0.3826 |
| 4 | 1592 | -0.00182 | 0.3668 |
| 5 | 1594 | -0.00785 | 0.3695 |
| 6 | 1593 | 0.00204 | 0.3666 |
| 7 | 1589 | -0.00218 | 0.3845 |
| 8 | 1598 | 0.0081 | 0.4036 |
| 9 | 1572 | -0.01354 | 0.3702 |

### `mom_rv_7d` (51 weeks ≥ 20 coins · monotonicity ρ(decile,mn-ret)=0.6966)

| decile | n | avg mn-ret | WR |
|--:|--:|--:|--:|
| 0 | 1614 | -0.00413 | 0.391 |
| 1 | 1589 | -0.00836 | 0.36 |
| 2 | 1598 | -0.00952 | 0.3717 |
| 3 | 1584 | -0.01129 | 0.3718 |
| 4 | 1592 | -0.00989 | 0.3706 |
| 5 | 1594 | -0.00792 | 0.3821 |
| 6 | 1593 | -0.0067 | 0.376 |
| 7 | 1589 | 0.01433 | 0.3946 |
| 8 | 1598 | 0.00033 | 0.3767 |
| 9 | 1572 | 0.02572 | 0.381 |

### `mom_kurt_7d` (51 weeks ≥ 20 coins · monotonicity ρ(decile,mn-ret)=0.2567)

| decile | n | avg mn-ret | WR |
|--:|--:|--:|--:|
| 0 | 1614 | -0.00247 | 0.3928 |
| 1 | 1589 | -0.00653 | 0.3726 |
| 2 | 1598 | -0.01001 | 0.3755 |
| 3 | 1584 | -0.00764 | 0.3649 |
| 4 | 1592 | 0.02193 | 0.3693 |
| 5 | 1594 | -0.00865 | 0.3758 |
| 6 | 1593 | -0.0106 | 0.3867 |
| 7 | 1589 | -0.00356 | 0.3908 |
| 8 | 1598 | 0.0043 | 0.3861 |
| 9 | 1572 | 0.0055 | 0.3607 |

## Caveats

- **Verdict basis**: §K7 stop-criterion — a stable OOS net spread across BOTH chrono halves ⇒ SKW1 is a candidate; no stable spread ⇒ SKW1 dead. Either way the moment feature-block stays/becomes a retrain input (§K7); any standalone deployment is an operator decision (Michi), never licensed by this study.
- **⚠ REAL structure ≠ tradeable edge (load-bearing):** the net spread is net of ONLY flat taker fees (FEE_PER_SIDE, both legs round-trip) + realized funding — it models NO slippage, market impact, borrow availability, or short-liquidation risk. This is a weekly full-decile-rebalance short-term-reversal sort on the most illiquid, highest-skew alts (only the bottom dollar-volume tercile is dropped), and the LONG (low-skew = recently-crashed) leg's mean is fat-right-tail / bounce-driven (WR < 0.5 in every decile). The headline net/week therefore OVERSTATES realizable PnL after microstructure costs — treat it as a validated FEATURE signal for retrains, NOT a turnkey deployable spread. Independent verification: the T-133 orchestration investigation (2026-07-16) ruled out stale-price / survivorship / look-ahead artifacts (structure is real); tradeability after real costs is unproven and is the operator's call.
- **Survivorship**: cross-section over active USDT-perps only; delisted coins missing.
- **Funding**: coins without funding history contribute 0 (documented, not imputed).
- Weekly grid anchored on BTC's 15m span; coins with shorter history contribute NaN (skipped) for early stamps, and a coin without a valid forward week (staleness > 1d) drops that stamp.
- CPU-check override: --skip-cpu-check=True (read-only, BELOW_NORMAL).
- Resume machinery: per-coin weekly-row checkpoint every 15 coins to OS-temp state (survives watchdog kills); memory bounded, state removed on clean exit.