# CLAUDE.md — Kythera

Multi-bot crypto trading system (Binance Futures) on a Windows VPS. **Real money runs here.** The bots trade via Telegram signals → Cornix → Binance. Architecture, fleet overview and setup: `README.md`.

**Read before your first edit: `docs/OPUS-HANDOFF.md`** — the operating manual with the work cycle, curated pitfalls, quality bar and escalation rules. The ranked backlog lives in `docs/T-2026-CU-9050-021-opus-task-audit.md`; the living audit ledger is `AUDIT_TODO.md`.

## Hard rules (non-negotiable)

1. **No live intervention from a dev session.** No fleet restarts, no write queries against the live DB, no overwriting of production model artifacts. The build machine deliberately has NO DB credentials (`.env` is an empty stub) — DB-bound work only runs in a VPS session (T-2026-CU-9050-011).
2. **Model artifacts go to `staging_models/` only.** Promoting an artifact into the repo root (= live) is an explicit operator decision by Michi, never part of a training run.
3. **Secrets:** `.env` and `.local/` are gitignored and contain real tokens/channel IDs (`-100…`) — never commit them, never hardcode them in code or docs. gitleaks blocks this; `--no-verify` is forbidden.
4. **Exactly ONE Cornix-parsable message per signal.** The info/HTML message must not repeat the Cornix block (fleet-wide double-trade bug, fixed 2026-07-06).
5. **Analyse closed candles only** (forming candle/R1). Exception: pure price checks in monitors 5/8. Never "simplify" candle indexing without checking the sort order.
6. **Reworked models post under a new tag** (ABR2, EPD2, RUB2, MIS2, …) via `model_id` in the artifact meta. Never reuse old tags.
7. **Feature builders are shared** (`core/*_features.py`, trainer == serving == replay). Changes there change model behaviour on both sides — deliberate, but load-bearing.
8. **The caller commits transactions.** `core/signal_post.py` and the cooldown helpers do not commit themselves.
9. **Never silently refresh the regression guard** to turn red into green (`KYTHERA_GOLDEN_REFRESH=1` only with a documented reason).
10. **Everything in this repository is written in English.** Code, identifiers, comments, docstrings, log messages, exception texts, Telegram/HTML output, commit messages, PR bodies, Markdown docs, `CHANGELOG.md` and this file. German only survives inside verbatim quotes of external material (e.g. a third-party error string). The chat with Claude stays German — that is the only exception, and it never leaks into a file.

## Workflow

- Per task: KB task + worktree + branch `feat/<t-id>`, PR onto `main` (ziagl888 repo → autonomous merge path once the core reviews pass: z-code-reviewer + z-spec-compliance-review). **Merge default: merge-train** — after PASS, stamp `cu/reviews` + apply the `merge-train` label; the Hetzner daemon merges serially; never run `gh pr merge` yourself (details: OPUS-HANDOFF §2 step 7).
- **KB task ID range (since 2026-07-21):** Kythera tasks run in the `T-2026-KYT-9050-NNN` range under the canonical slug `kythera` (`add_task` with `customer="kythera"`, `project_id="kythera"`, task_id prefix `"T-2026-KYT-9050-"` → the server allocates NNN atomically; or `next_id(prefix="T-2026-KYT-9050-")`). The old `T-2026-CU-9050-NNN` range (customer `cloudunify_internal`) is **closed** — do not create new tasks there. Existing CU-9050 tasks keep their IDs; the 15 most recently open ones were migrated to KYT-002…016 (old docs tombstoned + pointer). Historical CU-9050 references in docs and code stay as provenance.
- Commits/PRs/code comments in English, author Michael Ziegler.
- CI only gates ruff/format, mypy, syntax/imports and the secret regex — **green CI ≠ correct.** Verify behaviour via `backtest/test_*.py` (standalone, DB-free) and `python tools/regression_guard/guard.py verify|smoke`.
- After every merge: add a `CHANGELOG.md` entry (English, like the existing ones), update the affected `AUDIT_TODO.md` checkboxes, update the KB task status.

## Escalation (stop and ask Michi)

Anything irreversible or money-affecting: artifact promotion/rollout of a retrain, fleet restart/deploy, DB migration or schema change on live tables, gate flips (`AIM2_LIVE_POSTING`, `NEW_IDEAS_LIVE_POSTING`, orchestrator gating), parking/unparking bots, `.env` changes, dashboard exposure. Details and edge cases: `docs/OPUS-HANDOFF.md` §6.
