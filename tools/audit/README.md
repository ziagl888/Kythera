# tools/audit — analysis scripts for audit steps 2–8

Read-only scripts used to gather the findings in `audit_reports/` (2026-07-03, live VPS).
All DB scripts read credentials from env vars (`DB_NAME`, `DB_USER`, `DB_PASSWORD`, host/port
localhost:5432 hardcoded) and open the connection **readonly**. Interpreter: the live venv
(`crypto_trading_bot_v2\.venv`) has all dependencies (psycopg2, pandas, xgboost, joblib).

| Script | Step | Purpose / report |
|---|---|---|
| `ast_diff.py` | Diff | AST comparison Kythera ↔ live directory (formatting-invariant) |
| `ast_diff_commit.py` | Diff | AST comparison live ↔ any Kythera commit (`python ast_diff_commit.py <sha>`) |
| `step2_analysis.py` | 2 | Calibration, per-model WR, vocabulary check, regime flaps → `STEP2_DB_VERIFICATION.md` |
| `step2_part2.py` | 2 | RSI formula check, POC broadcast, coverage/gap census, whale files |
| `step4_results.py` | 4 | Per-bot/strategy results (first version, incl. duplicate discovery) → report 14 |
| `step4b_results.py` | 4 | Deduplicated results + classic evaluation (authoritative numbers) → report 14 |
| `step5_hypotheses.py` | 5 | Confluence, regime, AIM1 fade, FIFO tail hypotheses → report 15 |
| `step6_orchestrator.py` | 6 | Gate rates, whitelist quality, regime durations, auto-close evaluation → report 16 |
| `step7_monitor_replay.py` | 7 | First-touch replay of monitor scoring against 5m candles → report 17 |
| `inspect_models.py` | 3 | MIS1 pkl introspection (features, classes, thresholds) → report 13 |
| `live_parity.py` | 3 | MIS1 end-to-end parity test bot feature build ↔ models (reads DB) |
| `tree_splits.py` | 3 | Split-count/threshold analysis of the MIS1 boosters (ticker leakage proof) |
| `finding_ids.py` | — | Ledger tool, **DB-free**: `check` (duplicate guard, runs as a pre-commit hook) and `next --severity P1` (next free finding ID) |

Notes:
- `finding_ids.py` is not an analysis script of the audit steps but the tool for `AUDIT_TODO.md` itself.
  Before adding a new finding: `python tools/audit/finding_ids.py next --severity P1`.
  Only the checkbox line (`- [ ] **P1.45 …`) defines a finding — prose references don't count.
- The numbers in the reports are snapshots from 2026-07-03; re-running produces current values.
- `step7_monitor_replay.py` only works for periods where 5m candles are available (~30 days retention).
- `step4b_results.py` is the reference for performance numbers (deduplicated); `step4_results.py` is historical only.
