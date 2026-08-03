# T-2026-KYT-9050-009 — VPS orchestration, audit remainder: as-is measurement of the remaining jobs

**Session:** 2026-08-01/02 on SRV02 (live host) · **Mode:** read-only against the live DB and live processes,
code changes only in the worktree · **No** deploy, **no** fleet restart, **no** gate flip,
**no** artifact promotion, **no** write query.

The assignment was the remaining chain from the predecessor task (jobs 7/8/10/11 + docs PR). Rule for this
session: **every item gets re-measured against today's code and today's environment first, then worked on.**
Five of the eight items turned against their file record in the process — in both directions.

---

## Result in one sentence per item

| Item | File record before | Measurement today | Consequence |
|---|---|---|---|
| **P0.7 remainder** | "No-op, the 5 corrupt trades are gone" | Corrupt trades gone **holds** — but the error class is open and keeps producing: 342/3463 SR and 12/188 MC trades since 01.07. with TP1 on the wrong side | Root cause found and **fixed** (code + test) |
| **P2.2** | Flip the checkbox | `module` is live `varchar(50)` (the ALTER ran) — but the TZ drift **still** stands in `26_regime_detector.py` | Checkbox stays **open**, annotation sharpened |
| **Query-9 (P2.25)** | "VPS follow-up open" | 1590 whitelist rows, **all** from the last hourly run, 0 raw names, 0 stale | ✔ verified |
| **P2.15** | "needs a real listing" | GRVTUSDT: real listing, detected on a **running** fleet, catch-up + current candles | ✔ verified (with one named remainder) |
| **Job 7 / B4 / Z2** | "waiting on the Cloudflare domain" | Dashboard now hangs open on the internet and gets scanned daily; `stop_all` without auth | Domain is **not** the only path → Michi decision |
| **Job 8 / Z0** | "VPS CPU permanently 100%, WICHTIGSTER PUNKT" | 10-min sample: 78% box average, of which ~34 points the measuring session itself | Metric is **not** solid as-is; measurement tool + clean measurement procedure delivered |
| **Job 10 / B7** | "MIS1 400d replay + retrains open" | MIS2 posts live, ATB2/ATS2 built; only **QM + SRA1** remain open | Job **obsolete** in the form it was assigned |
| **Job 11 / signal rates** | "measure deltas from 13./14.07." | Window is 3 weeks old, >20 restarts in between, data distorted by retention and survivorship | **not reconstructable** — closed with a rationale, not a number |

---

## 1 · P0.7: the annotation was half right — and dangerous because of it

**What the file said.** `P0.7 [x] … Offen: die 5 bestehenden aktiven Korrupt-Trades bereinigen`,
and in the predecessor task "AUDIT_TODO annotations (P0.7 **no-op**)".

**What was measured.** The 5 trades are indeed gone — `active_trades_master` (558 rows from
2026-02-24) contains **0** rows with the P0.7 signature (TP1 ≈ 0,75·entry LONG / 1,25·entry SHORT);
the closed archive has exactly **1**, last on **2026-05-27**, i.e. before the fix from 04.07.
So: no-op confirmed.

**What the file didn't see.** The signature is only one of two doors into the same damage.
If you count the error class instead of the signature — *TP1 on the wrong side of the entry* — then:

| Strategy | Trades since 01.07. | of which TP1 wrong-sided | Share |
|---|---|---|---|
| Support Resistance | 3.463 | **342** | 9,9 % |
| Main Channel | 188 | **12** | 6,4 % |
| 5 Percent / Fast In And Out / Volume Indicator | 17.792 | **0** | 0 % |

Most recent case: **2026-08-01 23:33 UTC**, i.e. hours before this measurement. One active case was
in the book at measurement time (`active_trades_master` id 211171, LABUSDT SHORT, entry 0,1591, TP1 0,15965 and
TP2 0,16020 **above** the entry).

That only the two zone-based strategies are affected, and the other three have exactly
zero cases, is the fingerprint of the cause.

**Root cause.** `find_support_resistance_zones()` filters its zones against the **close of the last
closed candle**; but the strategies build the target ladder against **`entry = live_price`**. Two
different reference prices — and the market moves between them. Once the live price has run past a
resistance zone, `sorted(zones, key=|zone − entry|)[:4]` picks exactly that zone as TP1,
and the downstream interpolation (`x = (t1 − entry)/4`) goes negative and drags TP2/TP3 down with
it. The guard `if t1 == 0: return None`, added on 2026-07-04, only covers the case of *no zones at all*
— the second door stayed open.

**Why this is more than an ugly ladder.** A TP1 on the loss side is scored by the monitor as
a hit:

| Bucket (SR, since 01.07.) | status ≥ 1 ("TP hit") |
|---|---|
| clean (3.121 trades) | 66,2 % (2.066) |
| TP1 wrong-sided (342 trades) | **96,5 %** (330) |

These trades go into the per-bot statistics as winners, and the orchestrator gating
(bot 27 → 28) decides on that basis. The error class is therefore not just geometry, it is a
measurement error in the control loop.

**Fix.** New shared helper `core.market_utils.select_zone_targets(zones, entry, direction)` —
filters the zones against **the price the ladder is computed against**, sorted nearest
first. Both strategies, both directions use it (4 call sites). The ladder is thereby
monotonic in the trade direction; the existing `t1 == 0` guard now also covers "no zone on the
profit side". Test: `backtest/test_zone_target_side.py` (8 cases, DB-free, incl.
LABUSDT regression).

**Live semantics, measured instead of claimed** (basis: closed trades 01.07.–01.08.):

| Strategy | Ladder changes | Signal drops entirely |
|---|---|---|
| Support Resistance | 350 / 3.463 = 10,1 % | 37 / 3.463 = **1,1 %** |
| Main Channel | 12 / 188 = 6,4 % | 4 / 188 = **2,1 %** |

Caveat on this estimate, so no one takes it for exact: the DB shows the ladder **after**
the interpolation, not the raw zone list. "Ladder changes" is therefore a **lower bound**
(a wrong-sided zone that `[:4]` already cut off is invisible); "signal drops" is
solid, because there all targets are wrong-sided. The fix only takes effect with the next
fleet restart — that is Michi's moment, not the PR's.

## 2 · Job 7 / B4 / Z2 — the blocker is not the one on file

File record: "waiting on the Cloudflare domain from Michi". Measured on 2026-08-02:

- `dashboard.py` still binds `0.0.0.0:5000`, unchanged.
- `cloudflared` is **not** installed (no binary, no service) — the domain is therefore not the
  only missing piece.
- `netstat` shows, at measurement time, an **ESTABLISHED connection from a foreign public IP**
  to the VPS's public address, port 5000. Who that was is not identified, and that isn't
  the point.
- `logs/dashboard.log` documents ongoing internet scans: `GET / HTTP/1.1" 200` to 66.132.172.102,
  exploit paths like `GET /v404/exec?jwt=…` from 34.79.154.21, TLS handshakes against the HTTP port,
  `POST /` and `POST /mcp` from rotating IPs. The dashboard page is being served to strangers.
- `grep -i auth dashboard.py` → **no match**. Reachable endpoints include
  `POST /api/system/stop_all`, `/api/system/restart_all` and `/api/process/<script>/stop`.

In the log history reviewed so far there is **no** foreign access to a control endpoint visible
— the scans stayed on `GET /`. That is luck, not a safeguard.

**Delivered (without behaviour change):** `dashboard.py` now reads the bind address from
`DASHBOARD_BIND_HOST`, **the default stays `0.0.0.0`** — a silent switch would have cut off Michi's
remote view on the next restart. Additionally a warning line on startup for as long as it isn't
bound to loopback, and a documented entry in `.env.example`.

**Open decision (Michi) → see §7, decision 1.**

## 3 · Job 8 / Z0 — the 100% doesn't survive the measurement, but neither does the measurement itself

Tool: `tools/ops/measure_cpu_baseline.ps1` (read-only, WMI perf counters instead of cumulative
`Get-Process .CPU` seconds — the trap from 2026-07-20).

Run 2026-08-02 00:27–00:37, 35 samples, 10 logical cores: **box average 78%** (not 100%; the
100 figure comes from 3-second samples).

| Item | % of box | Classification |
|---|---|---|
| python (fleet bots, 41 PIDs directly under the watchdog) | 18,5 | Fleet |
| python (remaining, mostly indicator-engine pool workers) | 10,6 | Fleet |
| postgres (120 distinct PIDs in the window) | 14,1 | DB |
| ccSvcHst (Symantec) | 5,5 | AV |
| System | 2,7 | OS |
| **claude / bash / powershell / conhost / git / Taskmgr / WmiPrvSE / py / python3** | **≈ 34** | **the measuring session itself** |

**The most important finding is the observer effect.** Around 34 percentage points of the 78% go to the
agent session and its tooling — `claude` alone 16,4 %, the sampler (WmiPrvSE) 4,4 %. Subtract
that, and the baseline load of fleet + DB + AV sits at **≈ 48–50 %**, i.e. at the Z0 target ("<50%") rather than
"permanently 100%". This figure is a **subtraction, not a measurement** — it works as a signal, not
as an acceptance criterion.

Two side observations from the same data: 120 distinct postgres PIDs in 10 minutes (connection
churn, matches the `_POOL_MIN` comment in `core/database.py`) and 142 short-lived `py`/`python3` PIDs
(~14 restarts/minute) whose parent process was already gone at snapshot time — **not
attributed**, deliberately not guessed at.

Per-bot attribution was not possible in this session: `Win32_Process.CommandLine` returns `$null`
for the fleet running elevated, and the watchdog only logs a PID for the dashboard.

**Recommendation:** run the sampler without an agent session (a scheduled task at a quiet hour),
only then is Z0 fit for acceptance. No fix derived from this measurement — "measure first, then
fix" also means: don't fix on a contaminated measurement.

## 4 · Job 10 / B7 — done in the form it was assigned

- **MIS1 400d replay + retrain:** executed. `mis2_model_{8,24,72,168}h_{pump,dump}.pkl` sit in the
  repo root (= live), and the models post: `ai_signals` carries open MIS2 signals with
  timestamps up to 2026-08-01.
- **ATB1 → ATB2, ATS1 → ATS2:** built (artifacts in `staging_models/` and root).
- **Adapter status `tools/walkforward_sim.py:1151`:** `ufi1, td, bb, abr1, mis1, rub, atb2, ats`;
  `tools/retrain_from_replay.py:1054` additionally `epd`.
- **Genuinely open:** **QM** and **SRA1** have no walk-forward adapter. SRA2 exists but runs
  via the meta-labeling path (`tools/retrain_sra2.py`, `closed_trades3`), not through the
  shared simulator.

Job 10 as "MIS1 replay behind the job queue" is thereby moot. The remainder is its own,
smaller task (2 adapters) and needs no VPS session.

## 5 · Job 11 — one half verified, the other no longer reconstructable

**P2.15 ("needs a real listing") — verified.** GRVTUSDT is the first real listing since the fix:

- `logs/DATA_INGESTION.log`, 2026-08-01 06:01:38: `🆕 1 neue Coins in coins.json: GRVTUSDT` — the
  last fleet restart before that was on **2026-07-30 07:25**, the next on 2026-08-01 19:33. The
  additive path therefore took effect on a **running** fleet.
- `candles`: GRVTUSDT/1h from 2026-07-31 15:00 to 2026-08-02 00:00, 34 candles — catch-up **before** the
  detection time and ongoing continuation.
- `GRVTUSDT_1h` exists and is empty. That is **not** a P2.15 defect but the C-gate state:
  all legacy per-coin tables end at 2026-07-16 (T-2026-KYT-9050-002).
- **Remainder, named:** the first `ticker_10s` row for GRVTUSDT only appears at 19:36:50, i.e. after the
  restart. The writer is `10_pump_dump_detector.py`, which still freezes its coin list at start —
  bot 10 was never part of the P2.15 scope (which covered `1_data_ingestion` and
  `chart_data_service`). Not a regression, but the gap is now documented instead of assumed.

**Signal rate deltas post-restart — closed as not reconstructable.** The assignment targeted
24–48h after the restart from 2026-07-12, i.e. 13./14.07. In between lie three weeks and
more than twenty restarts. Compounding this, and the real reason for the no:

- `ai_signals` is the **open** book; a "per day" count there measures how many of a
  day's signals are still open, not how many were created — the apparent rise from 102 → 1.061 between
  27.07. and 01.08. is pure survivorship.
- A deduplicated union with `closed_ai_signals` yields ~2.000/day, but is skewed downward for
  older days by retention (12.–14.07.: 937/494/346) and therefore **not** comparable across the
  window.

Only the classic side (closed archive, no retention in the window) is solid:
`closed_trades_master` sits stable at **~700–900 signals/day** over the last 21 days, with no
discernible break. A figure for the original delta is **not** invented here.

## 6 · Docs remainder + two orchestration findings

**P2.2 — checkbox stays open.** Live, `trade_cooldowns.module` today is `character varying(50)` and
`last_posted_at` is `timestamp with time zone` (verified via `information_schema`). So the ALTER
did run (approved per the predecessor task on 2026-07-12 — **not** observed by this session, only
its result) and hasn't been noted in the CHANGELOG anywhere so far; that is now caught up. The actual
P2.2 core, however, is **not** closed: `26_regime_detector.py:242` still creates `trade_cooldowns` with
`module TEXT` and `last_posted_at TIMESTAMP WITHOUT TIME ZONE`, while 11/24/25/30 say `VARCHAR(50)` +
`WITH TIME ZONE`. On a fresh DB, the bootstrap order therefore still decides
the cooldown semantics. Checkbox stays `[ ]`, annotation sharpened.

Addendum to that: `COOLDOWN_MODULE_MAX_LEN = 10` in `core/market_utils.py` justifies itself in the
comment with "the live table is varchar(10)". That premise has been wrong since the ALTER. The value was
**not** raised — that would change the cooldown keys on the money path; only the comment is
corrected.

**RSI-execute — already documented.** The assigned CHANGELOG entry already exists (entry
`[2026-07-12]`, with 88.426.142 cells / 3.831 tables / 9,6 h / idempotency follow-up 0). No second
entry, no pseudo-output.

**Finding A — `restart_fleet.ps1` aborts restarts after a successful pull.** Documented twice
(`logs/fleet_restart_20260726_232251.log`, `_20260801_192843.log`): `ERROR - Pull failed: From
https://github.com/ziagl888/Kythera`, followed by "Fleet untouched" and exit 1 — even though the pull
went through (HEAD 0e432d5 → e3181d5, confirmed as "nothing to pull" two minutes later in the
follow-up run). Cause: git writes progress to **stderr**; PowerShell 5.1 turns that into ErrorRecords as soon as the
stream gets merged into the pipeline — which happens when the operator calls the script with `2>&1`.
With `$ErrorActionPreference = 'Stop'`, the first progress line terminates it, and the exception
message is exactly the first stderr text. Reproduced in a scratch repo (old: abort with an
identical signature / new: clean run) and end-to-end on the real script via `-DryRun`.
Fix: merge stderr explicitly, demote errors for the duration of the call, **exit code as the sole
verdict**, stderr goes into the log as INFO. Genuine git errors still throw — now even with git's text
instead of just an exit code.

**Finding B — the local secret guard from hard rule 3 is not armed on SRV02.** Neither
`pre-commit` nor `gitleaks` is on the PATH (`Get-Command` → both "NOT ON PATH"; likewise `ruff`
and `mypy`, which are only available as Python modules). This means **no**
secret scan and **no** `guard.py verify` runs on commit on this host — both only exist as a CI regex, or
not at all. For this session, the equivalents were run by hand (see the PR text). This is a
host-setup issue, not a code issue: `--no-verify` was not used, there is simply nothing to
bypass.

---

## 7 · Open decisions for Michi

**Decision 1 — dashboard exposure (P0.8/Z2).** The port hangs open on the network today, gets scanned,
and `stop_all` is reachable unauthenticated. Three paths:

| Option | Effect | Cost |
|---|---|---|
| (a) `DASHBOARD_BIND_HOST=127.0.0.1` in `.env`, effective on the next restart | Surface closes immediately | Remote access gone until a tunnel is in place |
| (b) `cloudflared` + Cloudflare Access first, then (a) | Surface closes **and** remote access stays | needs domain + installation (~0,5–1 day) |
| (c) Windows firewall rule on port 5000 | Surface closes immediately, no code | Host change, outside this session's approval scope |

Recommendation: (a) immediately, (b) afterwards at leisure. The switch is built and default-off; **no**
flip from this session.

**Decision 2 — rollout of the P0.7 fix.** Only takes effect at the next fleet restart. Expected
effect: SR loses ~1,1 % of its signals entirely and corrects the target ladder for ~10 %; Main
Channel 2,1 % / 6,4 %. The signals that drop out are exactly the ones whose TP1 sat on the loss side.
A side effect that comes with it: the hit rate of "Support Resistance" will **drop** after the
rollout — the 96,5 % status≥1 of the wrong-sided trades were phantom hits. The orchestrator
gating will then see a real, lower value.

**Decision 3 — raise `COOLDOWN_MODULE_MAX_LEN` from 10 to 50?** Free on the DB side since the ALTER.
Changes cooldown keys on the money path (today `25_smc_ml_sniper` falls back to a static tag on
long tags). Not touched.

**Non-decision:** Job 8 doesn't need sign-off, it needs a clean measurement without a session.
Job 10 needs a small follow-up task (QM/SRA1 adapters), not a VPS session.
