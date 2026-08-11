# Scan engine for cluster A — design and decision template (T-2026-KYT-9050-136)

**Status:** design document, no code. **Verdict up front: DEFER the engine, act on two cheaper
findings the study turned up.** The reasoning is in §2 and the decision points are in §7.

Cluster A is the last open block of the fleet consolidation. Cluster B (four rule-based shadow
scanners → `45_shadow_scanner_runner.py`, T-133) and cluster C (five DB pollers →
`46_signal_consumer_runner.py`, T-135) are merged, as is the shared candle snapshot
(`candle_snapshot_service.py`, T-132). All three are **restart-gated**: `FLEET` is read at watchdog
import, so nothing in this document is live until Michi restarts the fleet.

Cluster A is the nine scanners that walk the whole coin list: **7** (Pattern), **11** (MIS),
**12** (ATS1), **13** (RUB1), **14** (ATB1), **18** (ABR1), **24** (QM), **25** (BB/TD sniper),
**34** (MAX1).

## 0. The honesty anchor

The scan engine was conceived to kill read redundancy: nine bots walking the same ~527 coins and
re-reading overlapping windows of the same rows, 60–75 % of the candle queries duplicated across
bots, `candles JOIN indicators` the #1 statement in `pg_stat_statements`.

**That job is done, and the engine did not do it.** The snapshot service removes the duplication at
the read layer without touching a single bot, because `core/candles.py` is the one access point.
§2.1 puts a number on what is left.

So this document does not ask "should we merge nine processes to save DB reads". It asks the only
question still open: **is nine interpreters' worth of RAM, connections and scheduler pressure worth
giving up crash isolation and per-bot restart for?** That is a much narrower trade, and the answer
depends on measurements that only exist after the rollout restart. Where a number can be derived
from the code it is derived here; where it cannot, §2.4 defines the exact measurement instead of
guessing.

Sources: the bot files at `b476493`, `core/fleet.py`, `core/candles.py`, `core/candle_snapshot.py`,
`core/hosted_fleet.py`, `core/shadow_scanners.py`, `core/signal_consumers.py`, `main_watchdog.py`,
`2_indicator_engine.py`, `docs/ARCHITECTURE.md` §1/§4, the CHANGELOG entries for T-132/133/135, and
the Z0 CPU baseline of 2026-08-02 (`AUDIT_TODO.md` Z0).

---

## 1. IST inventory

`coins.json` in this checkout holds **527** symbols (the live file is rewritten daily by
`6_housekeeping`; T-132 documents ~523 — the arithmetic below uses 527 and is insensitive to the
difference).

### 1.1 The table

| Bot | Tag | Trigger | Reads per pass (per coin) | Rows/read | Feature work | Model artifacts | RAM-relevant imports | State |
|---|---|---|---|---|---|---|---|---|
| 7 | BR… | `minute == 3`, 4 TFs gated by hour (1h always, 2h even, 4h ÷4, 1d at 00) | `read_candles` ×1 per due TF | 168 | pandas rolling pivots + `scipy.stats.linregress` | none | `mplfinance`, `matplotlib.pyplot` (**no `Agg`**), scipy | `active_patterns.json` + two module-level sets |
| 11 | MIS1/MIS2 | `minute == 11` | `read_candles_with_indicators` ×1 | 100 | `core.mis_features.add_advanced_features`, per coin | **16** (8 × MIS2 ≈ 6.2 MB + 8 × MIS1 ≈ 13.2 MB on disk) | joblib, numpy, pandas | none (DB cooldowns) |
| 12 | ATS1/ATS2 | `minute == 13` | `read_candles_with_indicators` ×1 | 500 | `core.ats_features.build_ats_features` | 2 (≈ 4.4 MB) | joblib, pandas | none |
| 13 | RUB1/RUB3 | `minute == 10` | `read_candles` (95 d) + `read_indicators` (limit 1) = **2** | ~2280 / 1 | `core.rub_features` + `core.funding_features` | 2 (≈ 1.5 MB) | joblib, numpy, pandas | none |
| 14 | ATB1/ATB2 | `minute == 3` — **parked** (`docs/ARCHITECTURE.md` §4) | `read_candles` ×1, plus 3 extra reads per chart event | 1700 | `pandas_ta`, `scipy.signal`, `scipy.stats`, `core.atb2_features` | 2 (≈ 1.2 MB) | matplotlib (`Agg`), pandas_ta, scipy | `trendline_state.json` |
| 18 | ABR1/ABR2 | `minute == 2` | `read_candles` ×1 (+ a cached funding REST call per *setup*, 30 min TTL) | 240 | `pandas_ta` indicator recompute **per coin** | 2 XGBoost boosters (`bt2_*.json`, ≈ 5.6 MB) + calibrator | xgboost, pandas_ta, scipy, requests | in-memory funding cache |
| 24 | QM_1H | **`sleep(180)` — every 3 min, ~20×/h** | `read_candles_with_indicators` ×1 | 100 | `scipy.signal.argrelextrema` + pivot walk | 1 (≈ 0.7 MB) | mplfinance, matplotlib (`Agg`), scipy | none |
| 25 | BB/TD ×1h,4h | **`sleep(180)` — every 3 min, ~20×/h**, **2 TFs** | `read_candles_with_indicators` ×**2** | 150 each | pivot/breaker geometry, scipy | 4 (≈ 2.7 MB) | mplfinance, matplotlib (`Agg`), scipy | none |
| 34 | MAX1 | `minute == 15` | `read_candles` (90 d) + `read_indicators` (limit 1) = **2** | ~2160 / 1 | `core.rub_features` + cached funding | 1 (≈ 0.75 MB) | numpy, pandas | none |

### 1.2 The three things the table does not say loudly enough

**(a) Bots 24 and 25 are not hourly scanners.** The task brief describes cluster A as "minute slots
2/3/10/11/13/15". Seven of the nine fit that; **24 and 25 run `while True: scan_market();
time.sleep(180)`** — a full 527-coin sweep roughly every three minutes, and bot 25 does it over two
timeframes. They are not a footnote: they are ~87 % of cluster A's read volume (§2.1) and they
collide with every hourly slot. Every design choice below turns on this.

**(b) Bot 14 is parked.** `docs/ARCHITECTURE.md` §4 lists `14_ai_atb_bot.py` as parked by audit
decision. It is still a fleet entry (it would start the moment the marker is removed), so it belongs
in the inventory — but it contributes zero live load today and it is the single heaviest read in the
cluster (1700 rows × 527). Any measurement taken now must say whether 14 was up.

**(c) None of the nine writes its own log file.** All nine call
`logging.basicConfig(level=INFO, format=…)` with no `filename` and never touch
`core/logging_setup.setup_logging`. The watchdog starts them with
`subprocess.Popen([sys.executable, script])` and no `stdout` argument, so their stdout is the
watchdog's stdout, which `launch_watchdog.cmd` redirects into
`logs\watchdog_debug_<stamp>.log`. Consequences in §4.1 — this is the reason the per-bot heartbeat
question is not "how do we keep what we have" but "we never had it".

### 1.3 Import-time behaviour (matters only for a shared process)

| Bot | Runs at import | Hazard in a shared runner |
|---|---|---|
| 24, 25 | `joblib.load` of every model, and **`exit(1)` on failure** | `SystemExit` is a `BaseException`. `load_scanners()` in `45_shadow_scanner_runner.py` catches `Exception` — a missing QM artifact would take the **whole engine** down at startup, not just QM. |
| 34 | `ARTIFACT = load_artifact(...)` at module level | Benign (no exit); artifact-missing is an idle mode. |
| 7, 14, 24, 25 | `matplotlib` / `mplfinance` import | One global matplotlib state for four chart producers. 14/24/25 set `matplotlib.use('Agg')`; **7 does not** — in one process the backend is whoever imported first. `plt.close('all')` in 24/25 closes *every* figure in the process, including one another bot is mid-way through building. Harmless under strictly sequential dispatch, a real bug the moment anything runs concurrently. |
| all nine | `logging.basicConfig` | First call wins — the T-133/135 `%(name)s` trick handles this. |

### 1.4 Where the seam is

The runner pattern needs `run_scan()` (T-133) or `startup()` + `run_poll()` (T-135) separated from
the loop.

| Bot | Existing entry point | Work to split |
|---|---|---|
| 34 | `run_scan()` | **none** — already runner-shaped. |
| 11 | `check_mis_models()` | rename/alias only; `main()` also calls `load_pump_models` / `load_mis1_models` / `startup_feature_selfcheck` → a `startup()`. |
| 12 | `check_tsi_crossovers()` | as 11 (`load_models` → `startup()`). |
| 13 | `check_rubberband_conditions()` | as 11. |
| 14 | `run_trendline_detector()` | as 11 (`load_models_and_coins` + `load_trendline_state`). |
| 24, 25 | `scan_market()` | seam exists, but the **`exit(1)` at import** must become a raise first (§5.2). |
| 7 | `analyze_patterns(current_hour)` | takes the hour as an argument — the runner would have to pass it, or the function derives it. `load_active_patterns()` → `startup()`. |
| 18 | **inline in `main()`** | the only bot whose scan body lives in the loop; needs a genuine extraction (`for symbol in coins: process_abr_logic(...)` plus the connection lifecycle). |

Nothing here is hard. It is the T-135 split, eight times, on files that are considerably larger than
the five pollers were — and unlike cluster C, seven of these nine post to a live channel, so each
split needs the same AST-parity evidence T-135 produced.

---

## 2. The honest cost/benefit

### 2.1 What the snapshot service already removed

Per-hour DB read counts for cluster A, derived from the code (527 coins, bot 14 counted separately
because it is parked):

| Bot | Reads/hour | Timeframes |
|---|---|---|
| 18 | 527 | 1h |
| 7 | 527 (1h) + 264 (2h) + 132 (4h) + 22 (1d) ≈ **944** | 1h, 2h, 4h, 1d |
| 13 | 1 054 | 1h |
| 11 | 527 | 1h |
| 12 | 527 | 1h |
| 34 | 1 054 | 1h |
| 24 | 20 × 527 = **10 540** | 1h |
| 25 | 20 × 527 × 2 = **21 080** | 1h + 4h |
| **subtotal (live today)** | **≈ 36 250 / h** | |
| 14 (parked) | 527 | 1h |

With `KYTHERA_CANDLE_SNAPSHOT=1` and the default `KYTHERA_SNAPSHOT_TIMEFRAMES=1h`:

* served from RAM: everything on 1h that fits the lookbacks (2400 candles / 500 indicator rows) —
  ≈ **25 300 reads/h**, i.e. **~70 %**;
* still on the DB: bot 7's 2h/4h/1d passes (≈ 417/h) and **bot 25's entire 4h leg (10 540/h)**;
* the service's own cost: 527 symbols × 1 TF × 2 kinds = **1 054 reads/h**.

Net: **≈ 36 250 → ≈ 12 000 DB reads/h for cluster A, a ~67 % cut, with zero change to any bot.**

**A finding that falls out of this and does not need the engine:** bot 25's 4h leg alone is ~88 % of
the remaining DB reads. Adding `4h` to `KYTHERA_SNAPSHOT_TIMEFRAMES` would take cluster A to
≈ 2 400 reads/h (**~93 %** off the original) — at the cost of roughly doubling the store
(T-132 documents ~250 MB indicators + ~70 MB candles at the 1h defaults). That is a snapshot-rollout
decision, not an engine decision. See D0 in §7.

### 2.2 What an engine would still buy

Everything below is **process-count arithmetic**, not work reduction. A scan burns the same CPU in
one interpreter as in nine.

| Item | Today (9 processes, 8 live) | Engine (1 process) | Confidence |
|---|---|---|---|
| Python interpreters | 9 | 1 | exact |
| Fleet entries | 38 total, 9 of them cluster A | 30 (one engine) or 31 (two shards) | exact |
| Idle DB connections | pool `minconn=2` per process → **18** | 2 | exact (`core/database.py`) |
| Peak DB connections | up to `maxconn=8` × 9 = 72 | ≤ 8 | exact bound, real peak unmeasured |
| Resident model artifacts | ~36 MB on disk across the nine; **unchanged** in an engine — all nine rosters still load | same | no saving here |
| Per-process baseline RAM | 9 × (CPython + numpy + pandas + scipy + sklearn/xgboost + matplotlib) | 1 × | **needs M2** |
| Watchdog supervision cost | 9 × `poll()` + heartbeat probe per 10 s cycle | 1 × | small either way |

The honest headline: the only material win is **8 × the per-process import baseline of a
scientific-Python stack**, plus 16 idle Postgres connections. On this box, `pandas` + `numpy` +
`scipy` + `xgboost` + `matplotlib` in one interpreter is typically 150–300 MB RSS, so the plausible
range is **1.2–2.4 GB of RAM returned** — but that is a literature range, not a measurement of this
VPS, and it is exactly what M2 exists to replace.

Postgres connection churn is worth naming: the Z0 measurement of 2026-08-02 saw **120 distinct
postgres PIDs in 10 minutes** at 14.1 % of the box. 16 fewer idle backends is a real but modest dent
in that.

### 2.3 What an engine would cost

**(a) The architecture bet.** `docs/ARCHITECTURE.md` §1 states it: one process per bot trades
in-process efficiency for "resilience and observability of a message bus you can query, replay and
restart piece by piece", and §4 closes with "because the bots share nothing but the DB, this
supervision model is what makes 'restart one bot' a safe, local operation". Nine of the fleet's
heaviest bots in one interpreter is the largest single withdrawal from that account so far.
Clusters B and C were cheap withdrawals — four shadow-only scanners doing minutes of work per week,
and five pollers doing indexed queries. Cluster A is not comparable in weight.

**(b) GIL serialisation.** Feature building is pandas/numpy — mostly GIL-holding Python-level work;
`predict_proba` releases the GIL only inside the C kernels, which for single-digit-millisecond
predictions is not where the time goes. Today bots 7 and 14 both fire at minute 3 and genuinely run
on two cores; bots 24 and 25 overlap everything roughly every third minute. In one interpreter that
concurrency becomes queueing. This is not fatal — see §3, the slots have room — but it converts
`max(durations)` into `sum(durations)` at every collision.

**(c) Crash isolation is lost at the granularity that matters.** The bot-29 `try/except` pattern
from T-133/135 contains an *exception*. It does not contain: a wedged socket, a `predict_proba` on a
poisoned frame that runs for minutes, a native segfault (the fleet has had one: `psutil.open_files`
0xC0000005, T-025), or a memory blow-up. Today any of those costs one bot. In an engine they cost
nine — including bot 34, whose signals feed a Cornix-executed path, and bot 25, which posts live.

**(d) Per-bot restart disappears.** `control/restart/<script>.py` and the dashboard's restart button
follow the fleet list. Hosted bots keep their **park** marker (the runner checks it per dispatch) but
not their restart: restarting hosted bot 24 means restarting the engine, i.e. all nine. Clusters B
and C accepted this for bots whose restart cost was a skipped weekly slot.

**(e) A shared failure in the artifact path.** §1.3: today a corrupt `qm_xgboost_model_1h.pkl` kills
bot 24 and the watchdog backs it off. In an engine, unfixed, it kills the engine at import.

### 2.4 The measurements — exact definitions

None of the following can be taken from the build machine. All of them are read-only observations on
the live VPS, and **M1 and M6 need no code change at all**.

**M1 — per-bot scan duration (the load-bearing one).**
Source: `logs\watchdog_debug_<stamp>.log` (every bot's stdout lands there; §1.2c). Timestamps are
`basicConfig`'s `%(asctime)s`, i.e. **local time on the VPS (UTC+03)** — irrelevant, because only
differences are used. Read the file with `grep -a` / `-Encoding utf8`: it carries mixed encodings
and emoji, and a naive read has silently dropped lines before.

| Bot | Start pattern | End pattern |
|---|---|---|
| 18 | `ABR1_BOT - Starting ABR1 Scan\.\.\.` | `ABR1_BOT - ABR1 Scan stopped\.` |
| 7 | `PATTERN_DET - .*Time trigger reached` | `PATTERN_DET - .*Pattern scan stopped` |
| 14 | `AI_ATB_BOT - .*Starting Trendline Break/Bounce Scan` | `AI_ATB_BOT - .*ATB1 Trendline Scan stopped` |
| 13 | `AI_RUB_BOT - .*Starting Rubberband \(RUB1\) scan` | `AI_RUB_BOT - .*RUB1 Model Check stopped` |
| 11 | `AI_MIS_BOT - .*Starting MIS1 Model Check` | `AI_MIS_BOT - .*MIS1 Model Check stopped` |
| 12 | `AI_ATS_BOT - .*Starting TSI Sniper` | `AI_ATS_BOT - .*ATS1 Model Check stopped` |
| 34 | `AI_MAX1_BOT - .*MAX1 scan over` | `AI_MAX1_BOT - .*MAX1 scan finished` |
| 24 | `QM_SNIPER - .*Starting QM scan for timeframe` | `QM_SNIPER - Radar scan stopped` |
| 25 | `SMC_SNIPER - .*Starting SMC scan \(BB & TD\) for timeframe: 1h` | `SMC_SNIPER - Radar scan stopped` |

Window: **≥ 24 h** after the rollout restart (≥ 24 samples per hourly bot, ≥ 400 for 24/25).
Report **median and p95 per bot**, and state whether bot 14 was parked during the window.
Derived quantities: `S_h = Σ median(hourly bots)`, `S_c = median(24) + median(25)`.

**M2 — RSS per bot.** Elevated PowerShell (the fleet runs elevated; `Win32_Process.CommandLine` is
`$null` unelevated, which is exactly the trap from the T-009 measurement):
`Get-CimInstance Win32_Process -Filter "Name LIKE '%python%'"` → join `CommandLine` against the nine
script names, read `WorkingSetSize`. Sample ≥ 10 times over ≥ 30 min (RSS after a scan ≠ RSS at
idle) and report the **max** per bot. Deliverable: `Σ RSS(nine) − max RSS(single)` = the RAM an
engine returns.

**M3 — connections.** `SELECT count(*) FROM pg_stat_activity WHERE datname = current_database();`
before and after the switchover. The static expectation is −16 idle backends; the query confirms it
and catches a pool that is not behaving as the code says.

**M4 — snapshot hit/miss per bot.** After the gate flip, count refusal reasons in the bot logs
(`core/candle_snapshot` logs one line per reason per 60 s). A sustained `not_covered` for a bot means
its (symbol, tf) is not in the store — expected for bot 7's 2h/4h/1d and bot 25's 4h, an error for
anything else. A sustained `window_not_covered` means a lookback is too small and that bot has
silently become a permanent DB fallback.

**M5 — snapshot sweep duration.** `logs/CANDLE_SNAPSHOT.log`, line
`sweep: N entries in X.Xs (M still behind the last close) — store: … MB`. Threshold: the sweep must
finish inside the minute, because the first scanner fires at minute 2. Also read the store size
against free RAM **before** widening the timeframe list (D0).

**M6 — indicator-engine cycle duration.** `2_indicator_engine.py` already logs
`🏁 Complete indicator cycle completed in X seconds` and warns from 25 min. This bounds the earliest
usable slot for every indicator-reading scanner (11/12/13/24/25/34) and therefore the top of the
engine's scheduling window.

---

## 3. The shard question

### 3.1 What replaces the minute slots

Today the minute slots **are** the load spreading: minutes 2, 3, 3, 10, 11, 13, 15 keep the hourly
scanners off each other's cores, and 24/25 float freely on a 180 s timer. In a single-process engine
the spreading mechanism is **sequential dispatch itself** — the slots become the *order*, not the
*isolation*. That is fine as long as the work fits; the question is whether it does.

### 3.2 The arithmetic, with the thresholds pre-registered

Definitions from M1: `d_i` = median scan duration, `S_h` = sum over the hourly bots (7, 11, 12, 13,
18, 34, plus 14 if unparked), `S_c = d₂₄ + d₂₅`.

Three constraints, in decreasing order of how binding they are:

* **C1 — the 180 s cadence (the tight one).** In one process, a QM/SMC cycle that becomes due while
  an hourly scan is running waits for it. The cycle budget is
  `S_c + (the hourly scan it can land behind) ≤ 180 s`. Since it can land behind the *longest*
  hourly scan, the honest form is `S_c + max(d_hourly) ≤ 180 s`.
* **C2 — slot spacing.** Consecutive hourly slots are 60 s apart (2→3, 10→11) and 120 s apart
  (11→13, 13→15). With sequential dispatch an overrun pushes its successor, so a scan longer than
  its gap makes lateness structural rather than occasional. A T-133-style `CATCH_UP` grace turns
  that into "late", not "dropped" — but the drift accumulates within the cluster of slots.
* **C3 — hour budget.** `S_h + 20·S_c ≤ 3600 s`, with headroom. At 100 % the engine is a permanently
  busy interpreter with no room to catch up after a restart.

**Pre-registered decision rule (fix this before looking at M1, not after):**

| Measured | Verdict |
|---|---|
| `S_h + 20·S_c ≤ 1800 s` (≤ 50 % of the hour) **and** `max(d_i) ≤ 60 s` **and** C1 holds | **one process is viable** |
| C1 fails (`S_c + max(d_hourly) > 180 s`) but `S_h + 20·S_c ≤ 2700 s` | **two shards, split by cadence** (§3.3) |
| `S_h + 20·S_c > 2700 s` (> 75 %) | **do not consolidate** — no headroom, and the isolation loss buys nothing |
| any single `d_i > 300 s` | **do not consolidate that bot** — one scan would own the interpreter for a twentieth of the hour |

The rule is written down here so a marginal measurement cannot be argued into a "yes" afterwards.

### 3.3 If shards: split by cadence, never by coin

**Bot shards (recommended shape if sharding is needed):**

* **A1 — hourly slots:** 7, 11, 12, 13, 14, 18, 34. Slot-driven, `core/shadow_scanners.py`
  arithmetic applies almost verbatim.
* **A2 — continuous snipers:** 24, 25. Interval-driven, `core/signal_consumers.py` arithmetic
  applies almost verbatim.

This split is not a compromise, it is the natural seam: it is the same seam that separates the two
existing runner substrates, it removes C1 entirely (the 180 s cadence no longer competes with hourly
scans), and it puts the two matplotlib-heavy chart producers that share global state (24, 25) in one
process with the third (7, 14) in the other — reducing, not eliminating, the coupling from §1.3.

**Coin shards are contraindicated, and there is a concrete reason.** Bot 34 (MAX1) is
**cross-sectional**: `core/max1_gate.select_signals` ranks *all* candidates by probability, dedupes
per symbol and applies a hard 24 h cap read back from `ml_predictions_master`. Split the coin
universe across N shards and you get N independent top-N rankings, each reading the same
`posts_last_24h` at the same moment — the daily cap can be overshot by up to N×, and the ranking is
no longer "the strongest coin in the market" but "the strongest coin in this shard". A coin shard
would have to be paired with a cross-shard selection barrier, which is a distributed-systems problem
this fleet has no reason to acquire. Bot 7's pattern state and bot 14's trendline state are
per-symbol JSON and would survive a coin split; MAX1 would not.

### 3.4 A note on what sharding does *not* fix

Two shards still lose per-bot restart and still merge crash domains — just into two groups of four
or five instead of one group of nine. The RAM saving drops from 8 baselines to 7. Sharding is a fix
for the *scheduling* problem, not for the *isolation* cost.

---

## 4. Per-bot heartbeat

### 4.1 What exists today (and why it is weaker than it looks)

`main_watchdog.check_heartbeat` resolves each supervised process's **own open `.log` file**
(mapping-free, via an isolated child process because `psutil.open_files()` has native-crashed the
watchdog before), and flags the process when that file's mtime has not advanced for
`KYTHERA_WATCHDOG_HANG_LIMIT_S` (default 20 min). Auto-restart is default-off; a hang WARNs.

A process with **no observable log file is exempt** — it can never be false-restarted. And per §1.2c,
**none of the nine cluster-A bots writes its own log file.** Their stdout is inherited from the
watchdog and lands in the shared `watchdog_debug_<stamp>.log`. So the probe either finds no `.log`
at all (→ exempt) or finds the *shared* file, whose mtime advances constantly because 38 processes
write into it (→ never stale, effectively exempt).

**Either way the conclusion is the same: cluster A has no working hang detection today.** This is a
finding about the current fleet, not about the engine, and it is worth fixing independently of every
decision in this document. Which of the two branches is true is a one-line check on the VPS
(`_probe_open_log_files(<pid of bot 24>)`); it changes the fix, not the conclusion.

### 4.2 What an engine needs

An engine makes the observability problem both worse and better.

*Worse:* nine bots behind one PID. A process-level hang signal can no longer say **which** bot is
wedged, and — the case the task brief names — a scan wedged at coin 47 with siblings still logging
would leave the process log advancing while one bot silently stops producing slots.

*Better:* a runner is code we control, so a heartbeat can be written deliberately instead of
inferred from an accident of file handles.

### 4.3 Proposal: a progress-marker file per hosted bot

Mechanism, deliberately boring and consistent with the fleet's existing file-marker control plane
(`control/parked/`, `control/restart/`):

* The runner writes `control/heartbeat/<script>.json` — `{"phase", "slot", "progress", "total",
  "updated_at"}` — at three points: **before dispatch** (`phase="start"`), **every N coins**
  (`phase="scan"`, `progress=i`, `total=len(coins)`; N ≈ 25 → ~21 writes per 527-coin scan, one
  small atomic `os.replace` each, negligible), and **after dispatch** (`phase="idle"`).
* The per-coin ticks are the runner's, not the bot's — the bot modules stay untouched. That means
  the runner must drive the coin loop, which the current bots do not allow (they own their own loop
  over `coins.json`). Two honest options:
  * **(i) coarse heartbeat only** — start/end per scan, no progress. Detects "scan never finished",
    not "wedged at coin 47". Costs nothing and touches no bot file.
  * **(ii) fine heartbeat** — requires a callback seam in each bot's coin loop, i.e. a real change to
    eight bot files that the T-133/135 "bots are not modified" property does not cover.
* **Watchdog side:** `check_heartbeat` gains a hosted-bot pass. For each script in
  `ALL_HOSTED_SCRIPTS`, if `phase != "idle"` and `now − updated_at > timeout(script)`, warn naming
  the **bot** (not the runner). `timeout` per bot ≈ `3 × p95(d_i)` from M1, floored at 5 min. The
  existing default-off `HANG_AUTORESTART` semantics carry over unchanged — a money-path process is
  not restarted by a heuristic without an operator flag.
* **Watchdog-side, the other half:** a *missed slot* is a different failure from a *wedged scan*.
  The runner already knows both (`slot_action` returns `EXPIRED`), and T-133 logs it as a warning.
  For cluster A that warning should be promoted to a heartbeat field, because for bot 34 a silently
  skipped slot is a lost trading hour.

**Recommendation: (i) now, (ii) only if M1 shows scans long enough that "wedged mid-scan" is a
realistic 20-minute-invisible failure** — i.e. if any `p95(d_i)` exceeds ~120 s. Below that, a
coarse start/end heartbeat with a `3 × p95` timeout catches a wedge within minutes anyway.

**And independently of the engine:** giving the nine bots their own `logs/<TAG>.log` via
`core/logging_setup.setup_logging` would make the *existing* heartbeat work for them today, at the
cost of one changed line per bot. That is the cheapest observability win in this document (D1).

---

## 5. Substrate — what T-133/135 gives us and what is missing

### 5.1 Reusable as-is

| Piece | Fit for cluster A |
|---|---|
| `core/hosted_fleet.py` — registry + `expand_hosted` | **Exact fit.** Add the engine's `RUNNER_SCRIPT → HOSTED_SCRIPTS` entry and `core/bot_catalog.active_scripts()`, `tools/fleet_realized_audit.py` and the watchdog orphan-reap set all follow. This is precisely the generalisation T-135 built. |
| `core/shadow_scanners.py` slot arithmetic (`last_slot` / `slot_action` / `initial_slots` / `CATCH_UP`) | **Fits the seven hourly bots.** `HOURLY` cadence with minute 2/3/10/11/13/15. `initial_slots` (skip the slot you start inside) is the right switchover semantics here too — a runner starting inside minute 11 next to a not-yet-reaped bot 11 would be a duplicate post. |
| `core/signal_consumers.py` interval arithmetic (`due_specs` / `reschedule` / `lateness` / `sleep_seconds`) | **Fits bots 24 and 25** — `interval=180`, re-armed from completion, exactly `scan(); sleep(180)`. |
| The importlib loader + per-bot `try/except` + per-bot park-marker check | Reusable verbatim. |
| Per-bot park markers | Preserved. `control/parked/24_quasimodo_bot.py` still silences QM alone. |

The substrate is in better shape than expected: **cluster A needs no new scheduling vocabulary**, it
needs *both* existing ones in one process (or one per shard, which is §3.3's argument restated).

### 5.2 What the substrate does not have

1. **A per-scan timeout.** Neither runner bounds a dispatch. For four weekly shadow scans and five
   indexed polls that was acceptable. For a 527-coin scan holding a DB connection it is not: one
   wedged read stalls all nine indefinitely, and the coarse heartbeat (§4.3) only *reports* it.
   A watchdog thread that can interrupt a wedged scan does not exist in CPython in any safe form —
   the honest options are (a) accept "report, then let the operator/watchdog restart the engine", or
   (b) run each scan in a child process (which gives back isolation and gives up most of the RAM
   saving — i.e. it undoes the reason for the engine). **Recommend (a), stated explicitly as a
   limitation.**
2. **Process recycling.** `2_indicator_engine.py` and `3_detectors.py` carry
   `restart_interval = 21600` (6 h) precisely because long-lived heavy Python processes accumulate.
   Cluster A has four matplotlib/mplfinance producers and eight model-holding modules in one
   interpreter; the engine is the strongest recycling candidate in the fleet. **Any engine fleet
   entry must carry `restart_interval`**, and it must be aligned so the recycle never lands inside a
   scan minute — 6 h from a start at `start_delay=323` is not aligned to anything. A recycle needs a
   quiet minute (e.g. minute 45–55) or the engine must refuse to exit mid-dispatch.
3. **Import-time `exit(1)` containment.** §1.3. Either the two bot files change (`exit(1)` →
   `raise RuntimeError`) or `load_scanners()` catches `BaseException`. **The bot files are the right
   place** — `exit(1)` inside an importable module is a defect independent of the engine.
4. **A shared per-cycle price map.** Bots 11, 24 and 25 each call
   `core.live_price.get_live_prices_batch()` (one Binance REST call each). In one process these
   could share a short-TTL cache — but that changes each bot's price freshness, so it is a
   **behaviour change and must not be smuggled in** with a consolidation. Note it, do not do it.
5. **Matplotlib containment.** `plt.close('all')` is process-global (§1.3). Under strictly
   sequential dispatch it is safe; that safety must be written down as a constraint, because it
   silently forbids ever making the engine multi-threaded.
6. **Connection ownership.** Every cluster-A scan already opens and closes its own connection
   (`get_db_connection()` … `conn.close()`), which is the property that made T-133/135 safe. Bot 18
   and bot 24 set `conn.autocommit = True`; bot 11 deliberately does not. These stay per scan — the
   runner must hold no connection, exactly as today.

---

## 6. Migration plan

### 6.1 Why a shadow parallel run is impossible

The obvious de-risking move — run the engine next to the nine bots for a week and compare — is
**forbidden by Hard Rule 4 and by the fleet's dedup design**. Seven of the nine post to live
channels. Two schedulers on the same `trade_cooldowns` rows and the same `ai_signals` active-trade
checks race: the checks are `SELECT`-then-`INSERT` within a per-coin transaction, and two processes
scanning the same minute can both pass the check before either commits. The result is a duplicate
Cornix-parsable message, which is the exact fleet-wide bug fixed on 2026-07-06. This is also why
`main_watchdog.FLEET_SCRIPTS` deliberately keeps hosted script names in the orphan-reap set: T-133
and T-135 both treat "old process survives next to its runner" as a money-path hazard, not an
inconvenience.

**The alternative that is safe:** a **dry-run engine mode** that loads every module and runs the
scheduler with dispatch disabled. It proves import compatibility (§1.3), measures the merged
process's RSS (M2 for the engine side) and exercises the heartbeat, while executing zero scans and
posting nothing. It cannot validate scan timing under merge — that only the real switchover shows —
but it removes every import-order and memory surprise before a single signal is at stake.

### 6.2 Staged rollout

Each stage is one PR, one restart, and one observation window. **Migrating a bot = removing its
`FLEET` entry and adding it to the engine roster**, which is exactly the T-133/135 switchover shape,
and the old process is reaped at the restart.

| Stage | Content | Why this order | Rollback |
|---|---|---|---|
| **0** | M1/M2/M5/M6 collected over ≥ 24 h post-rollout-restart | the decision rule of §3.2 needs numbers | n/a — read-only |
| **1** | Engine skeleton + dry-run mode, fleet entry parked at birth, roster empty | proves imports, RSS, heartbeat; posts nothing | remove the entry |
| **2** | The three lightest hourly bots by M1 — expected 34, 11, 18 (each one read/coin, no charts), final order from M1 | smallest blast radius; 34 is the only one already runner-shaped; all three are single-read scanners | park the engine, unpark the three bots, restart |
| **3** | 12, 13 (and 14 if unparked) | joins/wide windows, still no chart path | as stage 2 |
| **4** | 7 (charts, 4 timeframes, no `Agg`) | first matplotlib producer — its backend behaviour must be observed alone | as stage 2 |
| **5** | 24, 25 — **only if C1 holds, otherwise as a second engine (shard A2)** | the cadence collision lives here; keeping it last means every earlier stage is reversible without touching it | as stage 2 |

Between stages: one full observation window (≥ 24 h) with M1 re-measured **from the engine's log**,
so the merged durations are compared against the pre-merge ones rather than assumed.

**Rollback is symmetric and cheap at every stage**, which is the property that makes the staging
worth its cost: restore the `FLEET` entries, empty the engine roster (or park the engine), restart.
The bot files remain standalone-runnable throughout — that is a hard requirement carried over from
T-133/135, and stages 2–5 must not break it.

### 6.3 Consequences for the catalog and the reports

* **`core/fleet.py`** — nine entries out, one or two in. `start_delay` must be the highest in the
  list (`backtest/test_fleet_definition.py::test_start_delays_are_monotonic`), and the freed delays
  stay free (the T-133/135 convention).
* **`backtest/test_fleet_definition.py::EXPECTED_WATCHDOG_VIEW`** — the golden must be updated in the
  same PR, with the comment convention the two previous consolidations established. Note this anchor
  once drifted six bots behind `FLEET` unnoticed because `backtest/` runs in no CI job; a stage that
  forgets it is not caught by CI.
* **`core/hosted_fleet.HOSTED_SCRIPTS_BY_RUNNER`** — one new key per engine/shard. Everything
  downstream (`core/bot_catalog.active_scripts`, `tools/fleet_realized_audit.resolve_active_scripts`,
  the watchdog orphan-reap set) follows automatically. **This is the step that must not be
  forgotten**: without it every migrated bot's legs drop into the "inactive" bucket of every
  realized report the moment the fleet switches over.
* **`main_watchdog.FLEET_SCRIPTS`** — inherits the reap set from `ALL_HOSTED_SCRIPTS`; no change
  beyond the registry.
* **Dashboard** — the nine lose their own rows and their restart buttons. The park path becomes
  `control/parked/<script>.py`. Same trade clusters B and C already made, but for nine bots the
  operator notices.
* **`core/bot_catalog._AI_FAMILY_TO_SCRIPT`** — unchanged. Tags keep mapping onto the *bot* script;
  only `active_scripts()` knows about hosting. That contract is already written down in the module
  header and must stay.

---

## 7. Decision template

Each point: recommendation, alternatives, and the measurement that would flip it.

### D0 — Add `4h` to the snapshot timeframe list?

*Independent of the engine; the largest remaining read block.*

* **Recommendation: yes, after checking M5 against free RAM.** Bot 25's 4h leg is ~10 540 DB
  reads/h — ~88 % of what cluster A still sends to Postgres with the gate on. Adding `4h` takes
  cluster A from ~12 000 to ~2 400 reads/h.
* **Alternatives:** (a) leave it — accept 10.5k reads/h; (b) add `4h` with a reduced
  `KYTHERA_SNAPSHOT_INDICATOR_LOOKBACK` (bot 25 needs 150 rows, not 500) so the store grows by far
  less than double.
* **Flips on:** M5's `store: … MB` line against free RAM on the VPS. If the 1h store already sits
  uncomfortably, take alternative (b) — a 4h store at lookback 200 is a fraction of the 1h one.

### D1 — Give the nine bots their own log files?

*Independent of the engine; fixes an observability hole that exists today.*

* **Recommendation: yes.** One line per bot (`logging.basicConfig(...)` →
  `core.logging_setup.setup_logging("<TAG>")`) makes the *existing* watchdog hang detection work for
  the nine heaviest bots in the fleet, and makes M1 a per-file grep instead of an archaeology
  exercise on a 38-process shared stdout.
* **Alternatives:** (a) do nothing — accept that a wedged scanner is invisible for as long as it
  stays wedged; (b) redirect each bot's stdout to its own file in `start_process` instead — one
  change in the watchdog rather than nine in the bots, but it changes process startup for the whole
  fleet at once.
* **Flips on:** nothing measurable — this is a defect, not a trade-off. The only open question is
  (a) vs (b), and (b) touches the money path's supervisor for all 38 processes, which is why the
  per-bot change is recommended.

### D2 — Build the scan engine at all?

* **Recommendation: DEFER until M1 and M2 exist.** The benefit that justified the design is gone
  (§2.1: the snapshot already removed ~67 %, and D0 would take it to ~93 %). What is left is
  8 × interpreter baseline and 16 idle DB connections, against the largest withdrawal from the
  one-process-per-bot bet the fleet has made. **Deciding this without M1/M2 would be deciding it on
  a literature range for RSS and a guess for scan duration.** That is not a Stop-B abstention for
  its own sake: M1 needs no code change and one 24 h window after the rollout restart that is going
  to happen anyway.
* **Alternatives:** (a) build it now and measure afterwards; (b) never — declare cluster A
  permanently one-process-per-bot and close the item; (c) build only shard A1 (hourly bots) and
  leave 24/25 standalone.
* **Flips to YES on:** M2 showing `Σ RSS(nine) − max RSS(single)` ≥ ~1.5 GB **and** the §3.2 rule
  landing in the "one process" or "two shards" band **and** free RAM on the VPS being the actual
  binding constraint on the box. If RAM is not tight, the RAM saving is not a benefit — it is a
  number.
* **Flips to NO (option b) on:** any `d_i > 300 s`, or `S_h + 20·S_c > 2700 s`, or M2 coming back
  under ~800 MB total saving.

### D3 — If yes: one process or shards?

* **Recommendation: two bot shards, split by cadence** (A1 = 7/11/12/13/14/18/34, A2 = 24/25) —
  *unless* M1 shows C1 comfortably satisfied, in which case one process is simpler and one fewer
  fleet entry.
* **Alternatives:** (a) one process for all nine; (b) three shards; (c) coin shards.
* **Flips on:** C1 (`S_c + max(d_hourly) ≤ 180 s`) from M1. **Coin shards are ruled out on code
  evidence, not on measurement** — MAX1's cross-sectional ranking and 24 h cap break under a coin
  split (§3.3), and no measurement changes that.

### D4 — Heartbeat granularity?

* **Recommendation: coarse (start/end per scan) with a per-bot timeout of `3 × p95(d_i)`, floored at
  5 min**, written to `control/heartbeat/<script>.json` and read by a new hosted-bot pass in
  `check_heartbeat`. Keep `HANG_AUTORESTART` default-off, as today.
* **Alternatives:** (a) fine per-coin progress — needs a callback seam in eight bot files and gives
  up the "bots are not modified" property; (b) no heartbeat — rely on process-level detection, which
  cannot name the wedged bot.
* **Flips to fine (a) on:** any `p95(d_i) > 120 s` in M1. A scan that long can be wedged for a
  meaningful fraction of its slot without the coarse marker noticing.

### D5 — Process recycling for the engine?

* **Recommendation: yes — `restart_interval` on the engine's fleet entry, aligned to a quiet
  minute**, following the `2_indicator_engine.py` / `3_detectors.py` precedent (both 21 600 s). Four
  matplotlib producers and eight model-holding modules in one long-lived interpreter is the
  strongest recycling case in the fleet.
* **Alternatives:** (a) no recycling — rely on `plt.close('all')` and the existing per-bot hygiene;
  (b) recycle on an RSS threshold instead of a timer (new mechanism, none exists today).
* **Flips on:** RSS drift of the engine over a week (an extension of M2 — same query, sampled
  daily). Flat RSS makes (a) defensible; a rising curve makes the timer mandatory. Note the
  alignment problem is real: `restart_interval` is measured from process start, so a 6 h recycle
  will eventually land inside a scan minute unless the engine refuses to exit mid-dispatch.

### D6 — What happens to the `exit(1)` at import in bots 24/25?

* **Recommendation: fix it now, engine or no engine.** `exit(1)` in a module body means the file
  cannot be imported by anything — a runner, a test, a replay tool — without taking the importer
  down. Replace with a raise and let the caller decide.
* **Alternatives:** (a) catch `BaseException` in the runner's loader instead — hides the defect in
  the consumer; (b) leave it and never import those two modules.
* **Flips on:** nothing. This is a defect.

---

## 8. Summary for the operator

1. The read redundancy the scan engine was meant to kill **is already gone** — the snapshot service
   did it, without touching a bot. ~67 % of cluster A's DB reads, and ~93 % if D0 is taken.
2. The engine's remaining benefit is **8 interpreters' RAM and 16 idle DB connections**. Its cost is
   crash isolation, per-bot restart, and GIL serialisation of nine heavy scans.
3. **Two of the nine (24, 25) are not hourly scanners** — they sweep every 3 minutes and are ~87 % of
   the cluster's read volume. They are the reason the shard question exists, and they are the
   natural second shard.
4. **Cluster A has no working hang detection today** because none of the nine writes its own log
   file. That is fixable in one line per bot and is worth doing regardless (D1).
5. The engine decision itself should wait for two measurements that cost nothing but a 24 h window
   after the rollout restart (M1, M2). The decision rule is pre-registered in §3.2 so the numbers
   decide, not the effort already spent.
