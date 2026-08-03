# T-2026-KYT-9050-042 — Single-Entry Posting (drop the Entry-2 line, except ROM)

**Status:** Requirement 1 of T-042 (operator mandate from Michi, 2026-07-26).
**Type:** Fleet-wide change to signal emission. No deploy in this PR.

## Intent

The fleet trades DCA: market entry on `entry1`, add-on limit on `entry2`, SL
behind `entry2`. T-043/T-044/T-045 showed, risk-adjusted, that precisely this
add-on hurts — Arm B (only `entry1`, original SL) raises the Sharpe for
**15 of 17** bots with a robust sample (median drag B−A **+0.073**),
consistently across S/R, pump/dump, momentum and pattern detectors, and on the
longest available window (MIS1-168H, 2.5 months) even amplified (Sharpe
0.06 → 0.16).

Michi's decision: switch Arm B live — and specifically **only via the
posting**. Signal calculation stays unchanged in all bots; only the
`🏦 Entry 2` line disappears from the posted message, so Cornix fills the full
size on `entry1`.

**Exception ROM1** (`28_signal_orchestrator.py`): keeps its `entry2`. ROM1 was one
of the two bots in T-045 without DCA damage (drag +0.014), and the DCA gives it
measurable MaxDD protection (10 → 14.5% without). ROM1 calculates its `entry2`
itself anyway (`ROM1_ENTRY2_OFFSET_PCT`) and builds its own message — it doesn't
depend on the upstream bots.

## Acceptance Criteria (binary testable)

- [x] **AK1** — None of the Cornix emission sites other than ROM1 still write a
  `🏦 Entry 2` line: `core/signal_post.py`, `7_pattern_detector.py`,
  `9_ai_sr_bot.py`, `10_pump_dump_detector.py`, `11_ai_mis_bot.py`,
  `12_ai_ats_bot.py`, `13_ai_rub_bot.py`, `14_ai_atb_bot.py`,
  `15_ai_master_bot.py`, `16_smc_forex_metals_bot.py`, `18_ai_abr1_bot.py`,
  `25_smc_ml_sniper.py`, `handlers/open_handler.py`.
  *Test:* source pins per file + behaviour test against `post_ai_signal` (the
  built message contains exactly one entry line).
- [x] **AK2** — ROM1 keeps posting `Entry 2`.
  *Test:* source pin on `28_signal_orchestrator.py` + the existing
  `build_rom1_cornix_message` tests stay green.
- [x] **AK3** — Geometry and DB unchanged: `entry2` is still calculated, the
  SL stays where it was (behind `entry2`), and `ai_signals.entry2` is still
  written.
  *Test:* behaviour test checks the `INSERT INTO ai_signals` parameters of
  `post_ai_signal` / `post_shadow_ai_signal` for an unchanged `entry2`.
- [x] **AK4** — the non-Cornix info/HTML messages no longer show `Entry 2`
  (`11_ai_mis_bot.py`, `25_smc_ml_sniper.py`).
  *Test:* source pins.
- [x] **AK5** — the ROM1 gating tolerates messages **without** an Entry-2 line:
  `parse_cornix_signal` still parses them and returns `entry == entry1`.
  *Test:* new pin in `backtest/test_signal_orchestrator.py`.
- [x] **AK6** — rule 4 unviolated: still exactly ONE Cornix-parsable message
  per signal.
  *Test:* existing double-post pins stay green.
- [x] **AK7** — `python -m pytest backtest/test_*.py` and
  `python tools/regression_guard/guard.py verify|smoke` with no new failures
  versus the prior state. Measured against a baseline run on `origin/main`
  (f59cf8a) in its own worktree: **baseline 41 failed / 1521 passed**, **branch
  42 failed / 1528 passed**. Failure-list diff: **no** new regression, the only
  extra line is `test_watchdog_hang::test_probe_real_child_finds_an_open_log`
  — a flake from the two parallel-running suites (it probes the open files of
  a real child process; isolated 20/20 green, and the test reads none of the
  changed files). Guard: `smoke` OK (6 fixtures, perturbation caught),
  `verify` OK (24 fixtures) — no golden refresh needed, the indicator engine is
  untouched.

## Out of Scope

- **Cornix sizing** — if Cornix splits the margin across the entry targets, the
  fleet runs at half size instead of full size after this change. That's Cornix
  config and is up to the operator (Michi), not the code.
- **ROM1** — remains completely untouched.
- **`legacy_trainers/zzz.py`** — frozen provenance, never executed.
- **RRR/`avg_entry` display** in `11_ai_mis_bot.py` / `25_smc_ml_sniper.py`: the
  info message still computes its RRR against `(entry1+entry2)/2`. After this
  change that number is no longer the one actually traded — calculations stay
  untouched per the mandate, so this was deliberately left as-is and noted as a
  follow-up.
- **Trailing-close bot** (Phase C / requirement 2 of T-042) — a separate step.
- **Deploy / fleet restart** — Michi's decision, not part of this PR.
