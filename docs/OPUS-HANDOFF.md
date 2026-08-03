# OPUS-HANDOFF — Operating Manual Kythera

**As of:** 2026-07-09 (ledger verification T-2026-CU-9050-028) · **Basis:** 2026-07-07, T-2026-CU-9050-021 (last Fable-5 day) · **Sister handoffs:** T-2026-CU-9000-296 (claude_skills), T-2026-CU-9000-297 (knowledge_scraper)

From 2026-07-08, Opus takes over task work in this repo. This document is the operating manual: work cycle, system synthesis, curated traps, quality bar, escalation rules. It supplements `CLAUDE.md` (hard rules, auto-loaded) and `docs/T-2026-CU-9050-021-opus-task-audit.md` (ranked backlog). **Read at session start, before the first edit.**

---

## §1 Context in three sentences

Kythera is Michi's live crypto trading bot (~29 bots, ~530 coins, Binance Futures, Windows VPS, PostgreSQL). 2026-06/07 saw a full audit (20 reports, 126 findings in `AUDIT_TODO.md`); the money-critical code bugs are largely fixed, what remains open are structural root causes (R1–R4), the retrain programme and the DB/perf block (Z0, TimescaleDB). There are **two machines**: the build machine (this checkout, no DB credentials) and the live VPS (bots + DB + real money + trainers in `Documents\_X`).

## §2 Canonical work cycle

0. **`git fetch origin` + check state** — before every prioritisation, before the first edit (trap 15).
1. **Choose a task** — backlog order from the task audit doc; for each task, first `read_doc` the KB task doc (the briefs there are an interpretation as of the stated date, the KB may have moved on).
2. **Start the KB task** (`/task-start T-2026-KYT-9050-NNN` — number range since 2026-07-21, see CLAUDE.md §Workflow; the old `T-2026-CU-9050-NNN` range is closed, new tasks via `add_task` with `customer/project_id="kythera"`), worktree, branch `feat/<t-id>`.
3. **Before solution ideas: decompose the problem into 4 questions** (skill `z-fable-judgment`): outcome / population / measurement / stop criterion. If one cannot be answered → ask, don't assume.
4. **KB-first:** `search_kb` for precedent (decisions, Kythera tasks in the KYT- **and** the closed CU-9050 corpus, audit reports). Almost everything here has a documented prior decision.
5. **Implement** — conventions from §4/§5. For bot edits, grep the DO-NOT/WARNING/forming/lookahead comments in the file first (~69 of them across 40 files — they mark the minefields).
6. **Verify** (§7) — CI is not enough.
7. **PR** (English, conventional commits — ziagl888 repo, no org title format), core reviews (z-code-reviewer + z-spec-compliance-review). **Merge via merge train (default since 2026-07-10):** after PASS, stamp `cu/reviews` on the head SHA, `gh pr edit <PR#> --add-label merge-train`, close the session — the daemon (`services/merge_train/` in knowledge_base_internal, Hetzner) merges serially and rebases each PR at most once. **DO NOT `gh pr merge` yourself** — parallel fleet sessions otherwise create the O(n²) rebase cascade over the CHANGELOG top insertion (the conflict mill of 2026-07-10). Bounce = label `merge-train:failed` + daemon comment; re-queueing needs a new commit + re-stamp + re-label (re-adding the label alone is a deliberate no-op, and a rebase/force-push discards the `cu/reviews` status too). After enqueueing, don't idle-poll for the merge — CHANGELOG/AUDIT_TODO follow-up belongs in the PR itself.
8. **Follow-up:** `CHANGELOG.md` entry (German, style like existing entries), flip the `AUDIT_TODO.md` checkboxes of the resolved findings (incl. ✅ date), KB task status, create a follow-up task if needed.

**A no-op is a valid done.** "Finding refuted", "the codebase already handles it", "effect stays absent (Stop-B)" are successful outcomes — document them instead of building pseudo-output.

## §3 System synthesis (what lives where)

- **Data flow:** Binance WS (`wss://fstream.binance.com/market/…` — legacy endpoints have been dead since 2026-04-23) → `1_data_ingestion` → per-coin tables (~9,297, `{sym}_{tf}`) → `2_indicator_engine` (~120 indicators) → strategy/AI bots → `28_signal_orchestrator` (regime whitelist, dedupe, ONE Cornix channel) → `telegram_outbox` → `4_telegram_bot` → Cornix → Binance. Monitors 5/8 score SL/TP.
- **Process lifecycle:** `main_watchdog.py` is the sole owner (since 8d3145f). Parking: marker `control/parked/<script>.py`; one-shot restart: `control/restart/<script>.py`. Health checks every 60s (`core/health_monitor.py`).
- **Regime layer:** bots 26 (detector, 5 BTC classes × 3 alt contexts) / 27 (per-bot performance → whitelist) / 28 (gating). Docs: `docs/REGIME_ORCHESTRATOR.md` (live, but mind spec drift P1.10).
- **ML programme:** since the audit the rule is: **labels only from walk-forward replay of the actual order geometry** (`tools/walkforward_sim.py`, first-touch TP1-vs-SL, fees) — never close-based proxies. Retrain pipeline: `tools/retrain_from_replay.py`, AIM2: `tools/aim2_build_dataset.py` + `aim2_train.py`. Model intents: `docs/MODEL_INTENT.md`. AIM2 design: `docs/AIM2_DESIGN.md` (bot 15, shadow-first via `AIM2_LIVE_POSTING`). Research bots 30–33 (PEX1/FMR1/TRM1/FIF1): `docs/NEW_IDEAS_BOTS.md`, gated via `NEW_IDEAS_LIVE_POSTING`, without an artifact they run in idle mode.
- **Parked by audit decision:** `14_ai_atb_bot.py`, `29_ufi1_bot.py`. **AIM1 is dead** (inverted calibration, P0.13 — decision: NO retrain, stays off; AIM2 is the replacement).
- **Staged-C refactor** (T-2026-CU-9050-007, premortem in `.local/refactor/` — gitignored): strangler fig within Kythera, TimescaleDB foundation alongside the existing stack, strategies individually behind parity gates. Phase 0 done, Phase 1 (regression guard) built **and armed** — 24 goldens + 24 fixtures + manifest have been git-tracked since `4765e25`, `guard.py verify` runs as a pre-commit hook (correction 2026-07-09: this paragraph and the task audit had incorrectly said "not armed"; the only thing still open is the disarm hardening P2.51). The old v4 repo is dead — do not revive it. Guiding principle from the premortem: **"Green means like-v2, never correct."**

## §4 The curated traps (what weaker models get wrong here)

1. **Forming candle (R1).** `is_closed` is NOT yet enforced in the DB (design: `docs/TIMESCALE_R1_MIGRATION.md`). Bots protect themselves individually — some via `iloc[-1]` on DESC-sorted frames (newest row = index 0!), some by dropping the last row. Whoever "cleans up" indexing without checking the sort order builds in look-ahead.
2. **Shared feature builders (X-R1 rule).** `core/{mis,aim2,rub,funding,research}_features.py` are imported by BOTH the bot AND the trainer/replay. A "harmless" refactor there shifts the feature distribution of a live model. The feature contract is hard: missing columns lead to a load error/idle, not `fillna(0)` (the P0.12 lesson).
3. **Idle mode ≠ broken.** A bot without a deployed artifact starts and does nothing (`loaded=False`). Do not "fix" that.
4. **Staging rule.** Training tools write only to `staging_models/`. A trainer that points at the live artifact path is a bug — even if it seems "more convenient".
5. **Cornix double parse.** A second message with an identical signal block = a duplicate position with real money. Info message without a Cornix block, always.
6. **Per-coin tables.** There is (still) no `candles` table. Table names are f-strings from `coins.json` — mind identifier hygiene (P3.3), don't loosen the symbol whitelist.
7. **coins.json has two writers** (`1_data_ingestion.update_trading_pairs` + `6_housekeeping.update_coins_json`) — filters must stay identical (quoteAsset=USDT + PERPETUAL), otherwise junk symbols leak fleet-wide (the "ETHU" incident).
8. **Caller-commit contract.** `core/signal_post.py`/cooldown helpers don't commit. Whoever forgets the caller commit persists nothing — or only partially.
9. **TZ minefield (R3 open).** Writers write UTC, various readers read naive-local. Before every fix touching time windows/cooldowns/stats, read the AUDIT_TODO TZ cluster (P2.1–P2.6) — single fixes without the R3 line (core/time.py, timestamptz) create new drift.
10. **Windows reality.** The live VPS is Windows: `platform=win32` (mypy), win32 process priorities, SIGBREAK, PG data dir `C:\PGDATA`, backups `D:\_BACKUP\db`, PowerShell 5.1 (no `&&` chains). `terminate()` is a hard kill (P2.48).
11. **CI gap.** CI = ruff/format + mypy + AST/import smoke + secret regex. No pytest, no guard, no backtests. Behavioural verification is the session's own obligation (§7).
12. **Ruff/mypy excludes.** `backtest/`, `tools/`, `strategies/`, `handlers/`, `trainers_x/`, `legacy_trainers/` are excluded — the lint bar is deliberately lower there. Don't "clean up" as an end in itself (boy-scouting only with touch context).
13. **AUDIT_TODO annotation ≠ truth.** Until 2026-07-09 this said "read the annotation first, then act". That's not enough: during verification (T-028) **one of the annotations itself turned out to be wrong** — P1.26 was marked as refuted, but is a real dead-code bug; the "proof" was cooldown rows from an older code version whose key the current code no longer even writes. Rule therefore: **read the annotation first, then verify against the code, then act.** A live-count proof ("N rows, so the path fires") is only valid if the current code actually writes that key.
14. **MIS2-SHORT limit entries** are still scored incorrectly by the trade monitor (unfilled +5%-entries must not count) — known follow-up, don't report as a new bug.
15. **The checkout can lag behind `origin`.** Several sessions work on the same repo in parallel; on 2026-07-09 the build checkout was 8 commits behind. Whoever prioritises from a stale checkout doesn't see fixes already in place and reworks them. **Before every prioritisation, `git fetch origin` + check state** — before the first edit, not after.
16. **The model tag comes from the artifact, never from a constant.** Hard rule 6 is not satisfied just because the source-code constant happens to match the current generation. Three bots (`11_ai_mis`, `13_ai_rub`, `24_quasimodo`) discard the loaded `meta.model_id` (P1.45) — on the next retrain, the generations silently merge in the per-bot statistics that the orchestrator gating decides on. Correct pattern: `18_ai_abr1_bot.py:520`. **Before every retrain rollout, check whether the post path actually reads `model_id`.**

## §5 Quality bar per deliverable

- **Code fix (bot/core):** root cause named (no symptom patch); affected DO-NOT comments respected; `backtest/test_*.py` of the touched surface green; ruff+mypy locally green; CHANGELOG entry; AUDIT_TODO checkbox flipped. For the money path (signal emission, monitor scoring, orchestrator gating): additionally proof in the PR text that live semantics change only as intended.
- **New bot/strategy:** shares feature/detection source with trainer+replay (one module in `core/`); posts to `CH_NEW_IDEAS` behind a default-off gate; artifact loading via `core/model_artifacts.py` (idle-mode-capable); its own `docs/MODEL_INTENT.md` section; cooldown + dedupe from day 1.
- **Trainer/ML:** labels from walk-forward replay; chronological split + purge gap; calibration + threshold on the validation slice; artifact to `staging_models/` with meta (`model_id`, feature names, versions); calibration report; **no rollout** — rollout recommendation to Michi.
- **Docs:** German for operational/audit docs (style AUDIT_TODO/CHANGELOG), English for code-adjacent docs; intent reconstructible for the following agent; shorter than the code it describes.

## §6 Escalation rules

**Stop immediately and ask Michi** (irreversible · money · external effect · gate flip):

- Artifact promotion from `staging_models/` into the live path; every retrain **rollout** (training itself + staging candidate + recommendation are fine).
- Gate flips: `AIM2_LIVE_POSTING`, `NEW_IDEAS_LIVE_POSTING`, orchestrator gating parameters, parking/unparking.
- Fleet restarts, `.env` changes, anything touching running processes on the VPS.
- Schema changes/migrations on live tables (esp. T-2026-KYT-9050-002, ex-CU-9050-018 — operator decisions are explicitly open there).
- Deleting data/tables (including "dead" ones — D5 only after approval).
- Two equally valid paths with strategic consequence (e.g. scope expansion of Staged-C).

**Don't escalate, just do it:** reversible code fixes in the worktree, tests, docs, ledger hygiene, analyses/studies without live intervention, staging training runs on the VPS within the Batch-E constraints (CPU-throttled, live tables read-only, no production pkls).

**3-attempts rule:** three fix attempts without new diagnostic information → stop, re-open the hypothesis space, escalate if needed. Guessing doesn't scale.

## §7 Verification matrix

| Change to … | Mandatory verification |
|---|---|
| `2_indicator_engine.py` / indicator path | `python tools/regression_guard/guard.py verify` (if armed; otherwise `smoke`) |
| `core/*_features.py` | corresponding `backtest/test_*_features.py` + affected trainer still loads the artifact (feature contract) |
| Bot signal logic | standalone test of the file (AST/import), affected `backtest/test_*.py`, grep for Cornix block duplication |
| Orchestrator/regime (26/27/28) | `backtest/test_signal_orchestrator.py`, `test_regime_detector.py`, `test_bot_regime_analyzer.py` |
| Monitors 5/8 | check scoring semantics against `audit_reports/17_monitor_replay_and_gaps.md` (63.4% agreement precedent) |
| Trainer/replay | mini run on a small coin set against `staging_models/`, calibration report |
| Everything | ruff + `ruff format --check` + mypy locally (= CI), let pre-commit run through (never `--no-verify`) |

On the build machine the DB is not reachable — DB-dependent verification (guard `extract`/`verify` armed, live queries, training runs) belongs in a VPS session.

## §8 How Fable thinks

The thinking patterns live in skill **`z-fable-judgment`** (problem decomposition into 4 questions, cheapest falsification first, recommendation over survey, default-off for unproven things, no-op/Stop-B discipline, 3-attempts rule). Kythera-specific calibration examples:

- **AIM1 (P0.13):** the obvious move would be "retrain the vocabulary". Decision: NO — the retrain would have reproduced the inverted volatility model. Pattern: root cause before mechanical fix; leaving a dead model off is a valid done. AIM2 was instead built cleanly from scratch (replay labels, shadow-first).
- **Walk-forward rule (P0.10):** 7/8 trainer families labelled idealised fills that the bots never actually trade. Decision: ONE shared simulator instead of eight individual fixes. Pattern: root-cause tooling before point fixes.
- **Staged-C instead of a v4 rewrite:** the big-bang rewrite had already died once (40 commits in 3 days → 3 months of silence). Decision: strangler fig within the existing codebase, guard first, WIP=1. Pattern: momentum risk beats architectural elegance.
- **Batch-E discipline:** every strategy idea is made cheaply falsifiable (replay decides, ~1 day) before live code exists — see T-2026-KYT-9050-003 (ex-CU-9050-020, HMM study) as a template.
