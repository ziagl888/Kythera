## [2026-08-11] The trailing roster, scored against the live book instead of against itself (T-2026-KYT-9050-134)

The roster in `core/trailing_roster.py` ranks legs by **density** — net result per occupied
slot-day — because PR #198 treated the 500 Cornix seats as the binding constraint. That premise is
measurable and it is false: occupancy peaks at 233 against 500 seats for bot 40 and 1000 for bot 44,
and T-129 records the same ("the cap has never bound"). Under free seats a leg that earns little per
slot-day but earns it often displaces nothing. `tools/trailing_roster_rerank.py` (new) therefore
ranks by absolute net contribution and keeps density as the secondary column. **Read-only, no live
effect** — seat changes remain an operator decision.

* **The replay overstates the live arm on every leg it can be checked against.** Re-running
  `trailing_slot_budget.py` on the live window and calibrating against `trailing_positions` gives
  `live = -0.197 + 0.272 * replay` (R2 0.61), overstated in **17 of 19** legs, median error
  -0.63 pp per trade. Because the same-window run reproduces the bias, it is a **model gap, not a
  regime change**: the replay simulates a trailing exit and otherwise lets a trade run to its
  recorded close, with no stop-loss and no time-stop, while live those are ~14 % of exits at ~-2.6 %.
* **The premise the task started from does not survive.** The four legs PR #198 rejected on density
  (EPD3 LONG, BB_1H LONG, BR2H LONG, TSM1 SHORT) are net-POSITIVE in raw replay output and
  net-NEGATIVE once corrected — EPD3 SHORT, never measured before because it went live after the
  2026-07-26 run, projects to -1281 %-points over the window. They were kept out for a stated reason
  that was wrong and an outcome that was right.
* **The real finding points the other way:** nine *rostered* legs lose money on live evidence,
  -661 %-points combined, led by ATS2 LONG (-261 over 448 live trades) and FIF2 (-234 across both
  directions). **None clears |t| > 2**, so this is a watchlist and not a retirement list — acting on
  a two-week book is the error the tool was built to catch. Re-check once the book is deeper.
* **Two guards, both from live near-misses.** A slot-budget report scores its `legs` block at its own
  selected `chosen_act`; the live-window re-run picked 0.0 where the original picked 2.0, and at 0.0
  the trail is the micro-scalper pinned in `test_trailing_slot_budget` (median hold 0.42h against
  5.58h). A mismatch is now refused outright. Second, live evidence always outranks the fitted
  correction: the regression pulls toward the mean and would otherwise have put AIM2 SHORT — retired
  by T-129 for losing money, 480 live trades at -0.511 — back near the top of a seat recommendation.
* **Pins:** `backtest/test_trailing_roster_rerank.py`, 13 checks, DB-free.

## [2026-08-10] Shared candle-snapshot service — "fetch once, serve many" for the nine hourly scanners (T-2026-KYT-9050-132)

Bots 7, 11, 12, 13, 14, 18, 24, 25 and 34 walk the same ~523 coins at staggered minutes and read
overlapping 1h windows of the same rows; 60-75 % of the ~6-7k candle queries per minute are
duplicates across bots, and `candles JOIN indicators` is the #1 statement in `pg_stat_statements`
(3.0M calls @ 36.5 ms). One process now reads each (symbol, tf) once per candle period and serves
the bots' slices from RAM. **Gated off — `KYTHERA_CANDLE_SNAPSHOT` defaults to 0, so this diff
changes nothing about live behaviour.** Turning it on is an operator decision (.env + fleet restart).
With the gate off the process is *dormant*, not merely unused: no sweep, no store, no DB
connection, no listening socket — just a sleep loop with a heartbeat line every 15 minutes.
Sweeping for nobody would have cost ~1046 DB reads/h and ~320 MB for zero consumers.

* **`candle_snapshot_service.py` (new, fleet entry `start_delay=307`, group `core`):** in-memory
  frames + localhost TCP, line-based JSON — the `chart_data_service.py` pattern. The refresh loop
  asks *in RAM* which entries are behind the newest closed candle and re-reads only those, so a
  poll over a warm store costs zero DB work and the load is one read per (symbol, tf, kind) per
  period. Entries that stay behind (thin/delisted coins, ingestion lag) back off for 300 s instead
  of being retried every poll. No disk snapshot on purpose: a store restored from before a restart
  is stale by definition, and stale is the one thing this service must never serve.
* **Client hooked inside `core/candles.py` only** (`read_candles`, `read_indicators`,
  `read_candles_with_indicators` → `core/candle_snapshot.py`). No bot script and no
  `core/*_features.py` builder is touched — that is what the module header has promised since
  Phase A ("swap the internals without touching a single bot"). With the gate off the flag check
  short-circuits before the client module is even imported.
* **Identical by construction, not by inspection.** `_fetch_df` builds every frame as
  `pd.DataFrame(cur.fetchall(), columns=…)`; the snapshot path ships the same ROW VALUES and
  rebuilds with the same call, so values *and* dtypes match. Slicing reproduces `_windowed_select`
  (window → DESC → LIMIT → ASC), including the `LIMIT 0` case that `iloc[-0:]` would have turned
  into "the whole frame". A value JSON cannot carry losslessly (a `Decimal`) is refused, never
  coerced. A NaN travels as `null`: in a frame `_fetch_df` built, a NaN can only be pandas'
  rendering of a SQL NULL (psycopg2 yields `None`), and whether a column lands as `float64`/NaN or
  `object`/None is decided per frame — so the wire carries the NULL and lets the decode reproduce
  the coercion of the slice the bot actually asked for. Otherwise an all-NULL single-row slice (a
  sparse indicator read with `limit=1`) would arrive as `float64`/NaN where the SQL path yields
  `object`/None, and every downstream `is None` check would flip.
* **Five doors it will not walk through**, each a transparent fallback to the DB path with a
  throttled log line: `include_forming=True` (the store holds closed candles only — contract 2),
  an uncovered (symbol, tf, kind), a window the frame cannot prove it spans, a joined read whose
  candle rows do not all have an indicator row (the SQL LEFT JOIN would emit NULLs), and any
  transport error at all.
* **Staleness is checked twice.** The service refuses a frame older than the newest closed candle,
  and the client re-checks the returned watermark against its *own* clock — T-2026-KYT-9050-068
  taught that "the backend says it is fine" is exactly the assurance that cannot stand alone.
* **Coverage is honest about its holes.** The lookback defaults (2400 candles / 500 indicator rows,
  both env-tunable) are sized to bot 14's `limit=1700`, the 95-day windows of bots 13/34 and bot
  12's `limit=500` joined read — pinned by a test, because shrinking them turns those bots into
  permanent fallbacks *silently*. A read with neither `limit` nor `start` is never served. The
  store costs ~250 MB of indicators + ~70 MB of candles at the defaults; the measured figure is
  logged after every sweep — check it against free RAM before flipping the gate.
* **Answering never sits on the event loop.** Slicing and JSON-encoding a 500 × 121 joined response
  is ~500 ms of CPU; run on the loop it would serialise the fleet, and the second caller of a pair
  would wait out the first one's encode and then trip the client's 2 s timeout — a fallback to
  exactly the DB load this service exists to remove. The handler runs in a worker thread, which is
  safe because it only reads: a refresh *replaces* a store entry rather than writing into a frame,
  and `stats`/`memory_bytes` walk a snapshot taken under the store lock.
* **A refusal never becomes an exception in the bot.** An unparsable `CANDLE_SNAPSHOT_PORT` falls
  back to the default port, an unreadable frame watermark from the peer falls back to the DB path,
  and a response line that never terminates is cut off at 32 MB instead of growing the *bot's*
  receive buffer without bound.
* **Tests (DB-free):** `backtest/test_candle_snapshot_protocol.py` (wire round trip, microseconds,
  NULL/text columns, NULL fidelity against the SQL oracle for an all-NULL and a mixed slice;
  slicing against a SQL oracle; coverage; staleness; the joined composition),
  `backtest/test_candle_snapshot_client.py` (a real service socket on an ephemeral port, driven
  through `core/candles.py`; gate-off no-op; every fallback door; transport guards), and
  `backtest/test_candle_snapshot_service.py` (due-list, backoff, sweep fan-out, gate-off dormancy,
  two concurrent requests that overlap rather than serialise, a store read racing a sweep).
  `backtest/test_candles.py` 60/60, `backtest/test_fleet_definition.py` 8/8, regression guard
  24/24 without refresh.

## [2026-08-09] There is no FIF channel — the four remaining sites now say where the post lands (T-2026-KYT-9050-119)

Follow-up `#T118-2`. T-118 fixed the sites that were WRONG about execution; these four were right
about execution and only stale in naming, so they were left out of that diff on purpose. Each
named "the FIF channel" as if it were a destination. It is not one: `CH_FIF2` → `CH_FIF1` →
`CH_NEW_IDEAS`, and with both overrides unset the chain simply ends at the cohort channel.
Documentation only — **no behaviour, no restart**; `core/fleet.py` is AST-identical to `main`
modulo comments (verified).

* **Swept:** `core/fleet.py` (FIF2 fleet entry), `.env.example` (`FIF2_LIVE_POSTING`),
  `README.md` (fleet table, bot 43) and `docs/NEW_IDEAS_BOTS.md` (FIF1-successor note). Each now
  carries the resolved chain. The `CH_FIF1` per-bot override stays documented where it is genuinely an
  override (`docs/NEW_IDEAS_BOTS.md` VPS checklist and the channel-collision note); it is the
  *fallback destination* wording that was misleading, not the variable.
* **A second cap that never bound.** Both `core/fleet.py` and `.env.example` called the channel's
  non-execution "the containment", inherited from T-112 and carried forward by the first cut of
  this PR. It contains nothing. FIF2 has held a trailing roster seat since T-115
  (`core/trailing_roster.py`), so bot 40 mirrors these signals into `CH_TRAILING`, which IS
  Cornix-executed — the bot's own docstring has said so since T-118. Both sites now name the
  channel as the quiet path and the seat as the money path. Same defect class as T-118's own
  "a cap that cannot bind is not a second cap", found by review on this diff.
* **Retracted, not deleted:** `docs/NEW_IDEAS_BOTS.md`'s FIF1 deploy note said "Cornix tracking
  of the posted signals is the validation". That was accurate on 2026-07-07 and stopped being
  accurate with the operator's 2026-08-08 statement. The sentence is kept verbatim under a dated
  retraction pointing at T-118 — same handling as the T-115/T-116 retractions, so a later reader
  sees both what was believed and when it stopped being true.
* **A measurement corrected en route:** the scoping pass reported `.env.example` as clean and
  filed that as a ledger correction. It was not clean. Git Bash rewrote
  `git show origin/main:.env.example` into `origin\main;.env.example` (MSYS path conversion on
  the leading dot), `2>/dev/null` swallowed the resulting `fatal`, and an empty grep read as a
  negative finding. The phrasing was there at line 25 and is fixed. Recorded in `AUDIT_TODO.md`
  because the failure mode outlives this diff: a swallowed stderr is a failed measurement, not
  evidence of absence.

## [2026-08-09] Squeeze-flatten replay — close ALL against-side bot-40 positions at squeeze onset (T-2026-KYT-9050-123)

Michi's clarified intent behind the T-122 detector: when a market-wide short squeeze fires,
close ALL open shorts immediately (long flush → all longs). This lifts the T-122 scope cut.

* **Shared episode detector:** `market_breadth_minutes` + `squeeze_episodes` in
  `tools/funding_liq_gate_study.py` (per-minute rolling 15-min side-breadth union, the
  pre-registered H3s/H3l cuts — single source for gates, export and replay).
* **Snapshot extension:** `tools/gate_snapshot_export.py` computes episodes at export time and
  pulls targeted `ticker_10s` slices ([start−10min, end+5min], all symbols) into new
  `ticker_slices` + `episodes` tables — episodes are rare, so the price table stays affordable
  (~800k rows / +29 MB for 6 days).
* **`tools/squeeze_flatten_replay.py`:** every against-side position open at onset
  (= episode start + 1 min, no lookahead) is counterfactually closed at its symbol's last
  ticker print ≤ onset (180 s tolerance; uncovered positions excluded AND counted — ticker_10s
  is a gappy ~40s tape, T-035: fine for marks, not touches). First-episode-wins; T-121
  accounting reused (slot credit, per-close_reason decomposition). 6 DB-free tests.
* **First smoke (6.2 liq days — NOT evidence):** 44 episodes (13 squeeze / 31 flush), 584 of
  1,546 positions flattened, price coverage 100%. Raw Δ −240.6, incl. slot credit −74.7, BOTH
  halves negative — the T-052 pattern again: TRAIL truncation (−529) dwarfs the saved
  SL/SOURCE_CLOSED/TIME_STOP damage (+289). Conclusive run ~2026-08-24.

## [2026-08-09] Market-wide short-squeeze / long-flush detector, directional H3 gates (T-2026-KYT-9050-122)

Michi: can we detect a MARKET-WIDE short or long squeeze? Yes — split the T-120 market-breadth
feature by liquidation side: short squeeze = many distinct symbols printing BUY forced orders
(shorts covered) with BUY-dominant imbalance; long flush = the mirror.

* **Probe finding (6 collector days):** the big liquidation waves are mostly SYMMETRIC (median
  |side imbalance| 0.07 at the total-breadth q99 — both sides get liquidated in violent
  two-sided volatility); strictly one-sided (≥2×) squeezes are a minutes-per-days rarity
  (2 short-squeeze episodes totalling 3 min, 1 long-flush episode of 7 min).
* **New pre-registered features + gates in `tools/funding_liq_gate_study.py`:**
  `mkt_syms_buy_15m` / `mkt_syms_sell_15m` / `mkt_liq_imb_15m`, and H3s (veto SHORT entries
  during a market short squeeze: BUY breadth ≥ 110 ∧ imbalance ≥ +0.25) / H3l (veto LONG
  entries during a market long flush: SELL breadth ≥ 130 ∧ imbalance ≤ −0.25) — side-breadth
  q95 cuts from the feature marginals only (T-116 discipline). ~1.2% / ~2.3% of minutes active.
* **Scope cut (documented):** no market-squeeze exit-trigger arm in the T-121 replay yet — the
  counterfactual needs a per-symbol price at an arbitrary market-trigger time, which the
  snapshot lacks. Future extension via a price table in the snapshot.
* Conclusive evaluation ~2026-08-24 with T-095/T-120/T-121. Prior-art caution: market-regime
  overlays were historically no-edge (T-029/T-031) — detection is cheap, tradeability is open.

## [2026-08-09] Liq-cascade EXIT replay for bot 40 — close open trades into a cascade? (T-2026-KYT-9050-121)

Michi's follow-up to the T-120 entry gates: "liquidations start running against my open
position → close immediately as protection". That uses IN-TRADE information (untested — the
entry-gate negatives don't cover it), and it fixes the T-052 accounting flaw: a closed loser
also FREES ITS SLOT, credited here at the book's net-per-slot-day (T-042 metric).

* **`tools/liq_exit_replay_study.py`:** counterfactual replay on the trailing book from the
  T-120 snapshot (no new export; the triggering forced order's own `avg_price` is the exit
  print). Trigger = the pre-registered T-120 cascade cuts, only strictly inside the position's
  open interval. Variants per cut: V0 unconditional, V1 only if ≥50% levered in the red
  (−2.5% unlevered @20×), V2 ≥100% (−5.0%) — conditions re-arm on later cascades. Results
  decompose per realized close_reason so "SL damage avoided" vs "TRAIL wins truncated" are
  separate numbers (the T-052 trap is measured, not assumed). 5 DB-free tests.
* **First smoke (6.0 liq days, 1,421 mirrors — NOT evidence):** V0 wrecks the book
  (Δ −147 on 15m/n≥2; TRAIL truncation −708 vs SL+SOURCE_CLOSED saved +540). Michi's
  conditional variants narrow it to roughly flat raw (−2…−20) and slightly positive only
  after the slot credit (+2…+21), with val/test halves disagreeing — flush-before-bounce is
  visibly real on this book. Conclusive run ~2026-08-24 with T-095/T-120.

## [2026-08-09] Funding × forced-liquidation entry-gate pilot, snapshot-driven (T-2026-KYT-9050-120)

After T-134 (funding alone: direction-confirmed, magnitude-weak), T-094 (OI gate: NO EDGE) and
T-096 (OI×funding events: refuted), the open cell is the funding × liquidation INTERACTION —
untestable until `liq_events` (collector 41, live since 2026-08-03) has ≥ 21 days of coverage.
This builds the full pilot now so the conclusive run (~2026-08-24, with T-095) is two commands.

* **Extraction-first (operator requirement):** `tools/gate_snapshot_export.py` pulls the four
  study tables (deduped `closed_ai_signals`, `trailing_positions` book, `funding_rates`,
  `liq_events`) read-only into ONE DuckDB file under `.local/`; the study
  (`tools/funding_liq_gate_study.py`) runs DB-free on the snapshot. Deliberately not the Z1
  AnalyticsExporter — its keyset cursor needs a unique `id` column these tables lack.
* **Pre-registered gates**, paired gate-on/off per direction, candidate only if kept-WR AND
  kept-raw-expectancy improve in BOTH chrono halves (Rule 8): H1 crowded-side flush/squeeze
  veto (extreme funding in trade direction + liq cascade against it), H2a/b cascade-against
  15/60 min, H3 market-wide cascade. Liq features are counts/clusters/recency only — the
  `!forceOrder` stream is a 1 order/s/symbol SAMPLE; notional sums are secondary. Shared
  builders reused (`core.funding_features`, `FEE_PER_SIDE`).
* **Guards:** `MIN_LIQ_DAYS=21` refuses a verdict on thin coverage; `--smoke` runs the plumbing
  and stamps NOT CONCLUDABLE. 14 DB-free synthetic-fixture tests (no-lookahead window edges,
  SELL/BUY direction mapping, DST-aware localization, candidate math, snapshot tz roundtrip).
* **First smoke run (VPS, 2026-08-09, 6.0 liq days):** pipeline green end-to-end (10,439 fleet
  trades + 1,412 bot-40 mirrors in-window, 100% funding coverage). It caught one degenerate
  pre-registration: the market always has liquidations printing (median 78 distinct symbols per
  15 min), so H3's original ≥5-symbol cut skipped 100% of entries. Recalibrated to ≥140 (q90 of
  the observed FEATURE marginal, ~10% skip) — distribution-derived, never outcome-derived; the
  T-116 discipline holds, amended in the spec doc before any conclusive evidence exists.

## [2026-08-08] The trailing bot becomes two arms — bot 44 posts everything, twice the seats (T-2026-KYT-9050-117)

Operator decision (Michi): keep bot 40 exactly as it is for its channel, and add an unfiltered
twin that mirrors ALL roster trades. Bot 44 is bot 40's module re-executed under
`TRAILING_BOT_PROFILE=free` — a thin wrapper, so every behaviour change keeps being made once,
in bot 40, and reaches both arms by construction.

* **What the free profile drops:** the ±50 exposure cap (T-052's structural bound, deliberately
  removed so the arm measures the unfiltered book) and bot 40's single-channel scarcity.
  Density-ranked slot admission stays active, but its budget is the SUM over TWO channels
  (`CH_TRAILING_FREE_A/B`, Cornix caps 500 per channel ≈ 1000 seats) — the roster still peaks
  near ~2000 in the top ~5% of hours, and then the densest legs win here too.
* **Channel mechanics:** `assign_channels()` places each admitted entry into the channel with
  the fewest open positions (balanced books), the position row records its `channel_id`, and
  every close posts into exactly that channel — Cornix' `Close` acts symbol-wide PER channel,
  anywhere else it would flatten a different trade or none. One position per symbol holds
  ACROSS both channels (operator decision: no double exposure per coin).
* **Separate books:** the twin writes `trailing_free_positions` and posts under `-TRAILF`.
  Sharing bot 40's table was never an option — both arms mirror the same source ids, and the
  unique indexes would silently eat whichever bot polls second.
* **Containment until the real channels exist:** `TRAILING_FREE_LIVE_POSTING` defaults 0 AND
  both channels fall back to `CH_SHADOW_TEST` (not Cornix-executed) — a deploy alone posts
  nothing that trades. Fleet entry at delay 299; supervised only after a watchdog restart
  (operator gate).
* **Pins:** 13 new free-profile tests (caps off ONLY there, symbol-global uniqueness, balancer
  behaviour, per-channel cap never overfilled, channel-faithful closes, thin-wrapper source
  pin, unknown-profile crash). Bot 40's 63 existing pins run unchanged — the trail profile is
  byte-for-byte the old behaviour.
## [2026-08-08] Correction: ODS1's and FIF2's own channels are NOT Cornix-executed (T-2026-KYT-9050-118)

Operator correction (Michi): `CH_NEW_IDEAS` is not Cornix-executed, and neither is the FIF
channel. Documentation only — **no behaviour, no restart**; both bot files are AST-identical to
`main` modulo docstrings and comments (verified).

Two places in the repo asserted that ODS1's **own** channel is Cornix-executed:
`42_ai_ods1_bot.py`'s `MAX_EMITS_PER_CYCLE` comment and the matching test docstring in
`backtest/test_ods1_entry.py`. Both date from T-2026-KYT-9050-106. `CH_ODS1` is unset and
resolves to `CH_NEW_IDEAS` (`core/config.py`), so posts there produce forward measurement, not
fills.

* **The cap is still right, for one of the two reasons it gave.** The other half of the same
  sentence is the real execution path and is unchanged: ODS1 holds a roster seat, so bot 40
  mirrors each signal into `CH_TRAILING`, and that channel *is* executed. A burst still lands
  against a per-channel cap of 500 there.
* **Retracted (2026-08-07 alarm).** FIF2's containment holds after all. Its docstring said
  the FIF channel is not
  Cornix-executed, and that is true — the alarm I raised on 2026-08-07, that this containment
  was untrue in the live configuration, is withdrawn. The imprecision was only that no distinct
  FIF channel
  exists: `CH_FIF2` → `CH_FIF1` → `CH_NEW_IDEAS`, so FIF2 shares the cohort channel with ODS1.
  Now stated that way, with the roster seat named as the single path out of the containment.
* **Retracted.** T-115 and T-116 repeatedly framed ODS1 geometry changes as "money-affecting,
  operator sign-off required" on the strength of a Cornix-executed channel that does not exist.
  That was an inference from "live on deploy by design rather than by omission" — which says the
  bot posts, not that it is filled — and both PR reviews inherited the framing from the PR body
  rather than checking it. The escalation was narrower than claimed; the part that genuinely
  touches money is the roster seat into `CH_TRAILING`.
* **ODS1's bracket stays unchanged** (operator, 2026-08-08), consistent with T-116's NO-CHANGE
  verdict.

## [2026-08-08] ODS1 bracket re-derived from its own replay — NO CHANGE (T-2026-KYT-9050-116)

ODS1 came out net negative in its first two live days (45 closed, Σ −120 % of stake, Ø −2.67 %,
WR 64.4 %), and its own docstring named the bracket as "the first thing to re-derive once this
bot has live rows of its own". Done — and the answer is **do not change TP1 1.0 / TP2 2.0 /
SL 2.0**, on "no evidence to move them", not on "they were right".

The 45 live rows cannot answer the question: they were produced *under* the current bracket and
say nothing about what a different one would have done. So `tools/ods1_bracket_study.py` replays
the entry rule over the OI history — through `find_candidates`/`_as_of` loaded out of
`42_ai_ods1_bot.py` itself, so the rule under test is the rule that runs — and scores every
admissible bracket path-dependently on the same 5m paths. 1217 deduped events from 14 113
simulated polls, 919 fit / 279 holdout / 19 purged, split 2026-07-25 with a 24 h purge gap. Entry is the 5m
close at the event instant (the posting-time proxy), never the OI-implied mark that
T-2026-KYT-9050-115 removed.

* **The fit ranking does not carry over.** 43 of the 49 alternatives beat the live cell in the
  fit window — it ranks 44th of 50 — and **not one** survived: holdout t falls to 0.11–0.67 and
  the highest holdout t anywhere on the surface is 0.99. The paired holdout of the fit winner
  (2/4/4) against the live bracket, on the same 279 events, is +0.089 pp/trade at **t = 0.60**.
  Shipping that winner would have been an overfit, not a fix.
* **The winner crowds the grid boundary.** 11 of the top 12 cells sit on `SL 4.0`, the widest stop
  offered; rank 5 (2.0/3.0/3.0) is the exception. Reported by the tool itself, not left to the
  reader.
* **A hypothesis was refuted.** "Wide stop wins" was expected to mean "this is not a bracket, it
  is just holding for 24 h" — which would have pointed at bot 40's time stop instead. The exit
  mix says otherwise: only 3.2–9.5 % of trades in the top-12 cells reach the mark-out. It is a bracket
  question, and the bracket answer is "no evidence".
* **The incumbent is measured more harshly than everything that beats it**: among the 43 cells
  that outrank it on fit, intra-bar ambiguity runs 0.49–2.14 % against its 2.88 %, and every
  ambiguous bar is resolved against the trade. (Across all 50 cells the range is 0.49–5.51 % and
  three exceed the live cell, so "harshest of all" would be false — the restricted claim is the
  one the argument needs.)
* **Confound named, not ignored.** Every live row up to 2026-08-07 was posted around an anchor
  that could be 45 min stale against a 1.0 % TP1. The geometry cannot be judged from a book
  carrying that. Re-run after ~2–3 weeks of post-anchor rows (`#T116-2`).
* **Corrections after review, recorded rather than quietly fixed.** The first cut's purge gap
  purged nothing — `n_fit + n_hold` equalled `n_events` exactly, which is the arithmetic receipt
  that no event was dropped; `PURGE_H` merely shifted the holdout start while the gap cohort
  stayed in fit. Direction was conservative (leakage can only flatter the challenger, which still
  failed), so the verdict never moved, but four documents asserted a mechanism that did not run.
  Three further claims were wrong against the study's own JSON — "worst cell in the fit window"
  (it is 44th of 50, six are worse), "every top cell sat on the widest stop" (11 of 12), and
  "measured most harshly of all" — each erring toward flattering this verdict. The verdict
  markdown had also dropped the single rank that contradicted its own boundary argument, a
  hand-transcription artefact; the table is now generated from the JSON.

Not built on, and recorded so nobody else does: ODS1 shows +0.303 %/trade unlevered in bot 40's
trailing arm (n=33) against ≈ −0.13 % in its own channel — but 30 of the 33 closed as
`SOURCE_CLOSED`, so it is not an exit effect, and the entry-anchor explanation could **not** be
verified. `trailing_positions.src_signal_id` does not join to `closed_ai_signals.id` (own
sequence — the join yields 255 % "price differences" from id collisions), and a symbol+time join
returns n=0, pointing at the writer-dependent timezone domain of T-2026-KYT-9050-107. The
association stands; the mechanism does not.

Verification: `backtest/test_ods1_bracket_study.py` (14) pins the SHORT path arithmetic, the
look-ahead boundary (the event bar cannot resolve its own trade), pessimistic intra-bar
resolution, dropped-vs-zero handling for missing paths, and the identity of the bisect windowing
that makes the replay runnable — the last one is load-bearing for the whole "replay == serving"
claim. `test_ods1_entry.py` (30) and `guard.py verify` green.

## [2026-08-07] Entry anchored at posting time (ODS1/FIF2), FIF2 roster seat, re-entry lock (T-2026-KYT-9050-115)

Operator finding (Michi): bots 42 (ODS1) and 43 (FIF2) built their brackets from a price that
was true when the rule *fired*, not when the signal was *posted*. Both geometries are pure
percentage translations of a measured effect, so a bracket hung off a price the position is
not opened at posts a risk/reward that was never priced. Bot 40 made exactly this correction
on 2026-07-27 — of 24 mirrors 5 filled, and for 15 of 18 cancellations the market never
touched the posted entry, so the arm traded a selection it had created itself.

* **ODS1 was the worse case.** Its anchor was the OI-implied mark (`oi_value_usdt /
  open_interest`) of the newest `oi_5m` row at or before now — accepted up to
  `STALENESS_CAP_S` (45 min) old, on a collector running a 10-min median / 20-min p90 cadence
  (T-097). TP1 is **1.0 %**, so a 10-minute-old anchor on this bot's own tape (+3 % over 4 h)
  could sit half a TP1 away. **FIF2** anchored on the source signal's `entry1`: the price the
  originating bot saw when *it* fired, up to `MAX_MIRROR_AGE_S` plus that leg's insert latency
  earlier (30–120 s tick legs, a ~185–195 s wall for candle-cycle legs).
* **What moves with the anchor differs from bot 40 on purpose.** Bot 40 re-anchors only the
  entry and keeps the source's SL/targets at their ABSOLUTE prices, because those are S/R
  levels (`mirrorable_at`). ODS1's rungs and FIF2's t104 ladder are percentages, so entry and
  bracket are re-anchored together.
* **Decision paths are untouched.** ODS1 still decides on the `oi_5m` clock and FIF2 still
  gates on closed 5m candles through `core/vol_features` — only the posting geometry moved.
  Mixing clocks in the entry *rule* is what both module docstrings rule out, and the ODS1
  Rule-5 note now names the ticker as the second posting-path price source.
* **A drift bound, expressed as a fraction of TP1** (`DRIFT_CONSUMED_FRAC_OF_TP1`, 0.5): skip
  when the market has already run half of TP1 the trade's way since the decision price — ODS1
  0.50 %, FIF2 2.0 % LONG / 1.5 % SHORT. Re-anchoring means entering later, and past that
  bound the trade is the tail of the effect rather than the effect. **Not measured** — neither
  study priced an entry delay — so it is a loose bound, tied to the geometry it protects so
  re-pricing TP1 cannot silently change it. Bot 40's `mirrorable_at` degenerates to a no-op
  here, because a percentage ladder moves with its own anchor.
* **Unpriced is voided, never filled.** No live anchor ⇒ no post, and specifically no fallback
  to the stale price this change removed — that would make the defect intermittent instead of
  gone. One `get_live_prices_batch()` per cycle (P2.44); ODS1 resolves the per-symbol HTTP
  both bots return on an empty batch rather than degrading into one call per coin, and single
  gaps in an otherwise valid payload are capped by `MAX_PRICE_FALLBACKS_PER_CYCLE`. Neither
  hands the connection to `get_live_price`: its DB fallback calls
  `conn.rollback()`, which is connection-wide and would discard everything the cycle wrote
  before its single commit.

**FIF2 takes a trailing roster seat** (LONG + SHORT, operator decision Michi). Placeholder
densities below every measured leg and above ODS1; the column doubles as eviction order, so an
unmeasured leg yields its seat first. FIF2 is a re-forwarder like ROM1 but is not excluded for
it: where the source leg is itself rostered, `admit` resolves the overlap by density sort +
`SYMBOL_HELD` and the measured leg wins. What the seat buys is the complement — a vol-gated
admission path for legs that never earned one (EPD3, TSM1, BB_1H, BR2H, FIF1).

**The seat exposed a latent defect in bot 40, now fixed** (`REENTRY_LOCK_H`, default 1 h). The
"a once-trailed trade is done" lock is keyed on `src_signal_id`, so it only recognises the SAME
`ai_signals` row. A re-forwarder writes the same underlying trade under a NEW id and walks past
it: mirror opens at t, trails out at t+180 s, the 60 s `SYMBOL_COOLDOWN_SEC` expires, and a
re-forwarded row still inside `MAX_MIRROR_AGE_SEC` is admitted — a re-entry into exactly the
position the trail just exited. The hole is not new and not FIF2-specific (any two rostered
legs on one symbol can produce it); FIF2's seat turns it from rare into routine, because its
vol gate selects the fast tapes where a trailing exit within minutes actually happens. The lock
is symbol-scoped, arms only on `TRAIL`/`TIME_STOP` (SL_HIT and SOURCE_CLOSED mean the
underlying is over; ENTRY_NOT_FILLED and SHADOW_CARRYOVER never held a position), rejects as
`SYMBOL_REENTRY_LOCK`, and unlike the cooldown does **not** filter on `posted` — a shadow
mirror also traded and left that position.

**Review round (same PR).** Two independent reviews found the same defect in the first cut, and
it is worth recording because the code asserted the opposite: ODS1's comment and this entry both
claimed the per-symbol price fallback was "bounded by the emit cap". It was not — `unpriced` and
`chased` each `continue` **without** incrementing `emitted`, so the cap could never break that
loop. An empty batch usually means the same host is failing, so the per-symbol calls fail too,
and the bot would have fired one 5 s request per candidate across the up-to-527-symbol universe,
inside a 300 s poll, during exactly the outage or 429 that escalates into an IP ban. Fixed by
adopting FIF2's doctrine (empty batch → log and return; candidates leave no cooldown row and
re-qualify next poll) **plus** an explicit `MAX_PRICE_FALLBACKS_PER_CYCLE` counter for single
gaps in an otherwise good batch — a counter is checkable, an argument from another counter was
not. Also fixed: `SYMBOL_REENTRY_LOCK` was missing from `tools/trailing_intake_audit.ADMIT_GATES`,
so the one tool that answers "which gate is binding" would have silently dropped the new gate;
FIF2's `unpriced` count could go unreported on quiet cycles; the two bots' `drift_consumed_pct`
used different denominators; and `entry_anchor` was deduplicated into
`core/live_price.posting_anchor`, so the "never hand `conn` to `get_live_price`" rule has one
owner and a signature that takes no connection at all.

**Two docstring corrections that are not cosmetic.** `43_ai_fif2_bot.py` still stated the roster
seat as conditional on positive live edge — the seat was granted one day after the bot deployed,
i.e. deliberately ahead of that measurement, now recorded as an operator override rather than
left as a contradiction. And its breakeven paragraph still read "the FIF channel is not
Cornix-executed, so this is a go-live precondition, not a live defect": the roster seat routes
FIF2's `sl`/`targets` through bot 40 into `CH_TRAILING`, which **is** Cornix-executed, so that
precondition is now live. In that channel the trail is the primary exit while the rungs sit at
Cornix as partial take-profits — a combination neither T-111 nor the PR #198 slot-budget run
scored.

Verification: `backtest/test_ods1_entry.py` (29), `test_fif2_bot.py` (18),
`test_trailing_close_bot.py` (67) all green per file, plus `guard.py verify|smoke`. Six
mutations confirm the new pins bite: reverting either anchor, disabling either drift guard,
defanging `locked` in `admit`, and widening the lock to `SL_HIT` each fail. Note that these
suites must be run **per file** — running them together fails 9 tests on unmodified `main` too
(cross-file `core.config` stub pollution, pre-existing).

## [2026-08-07] Realised-PnL report: the open book, marked to market (T-2026-KYT-9050-114)

Every window of the 4h realised report is filtered on **close time**, so a leg whose winners
are still running shows only its fast SL closes. The report was therefore structurally
pessimistic for every slow leg — and nearly triggered a park decision on half a book: BR4H
SHORT read `30d Σ −1995.6 % │ Ø −6.30 % │ n=317` while its **53 open positions marked to
+1907.7 %**, i.e. the whole 30d book was flat, not bleeding. Same shape across the BR family
(BR1D, BR2H, BR1Hv2) and, inverted, the honest bleeders: SRA1 is negative on closes *and* on
its open book.

`job_realized_pnl_report` now reads both open tables (`ai_signals` + `active_trades_master`,
mirroring the two closed sources), marks them with **one** `get_live_prices_batch()` call and
scores them through the same `realized_pnl_pct` as the closed part. Per bot two new lines,
per lifecycle block a footer total:

```
BR4H
  30d : Σ  -2154.2% │ Ø   -6.75% │ n=319
  open: Σ  +1872.1% │ Ø  +36.00% │ n=52  ⚠ unrealized
  Σall: Σ   -282.1% │ Ø   -0.76% │ n=371
```

* **Both directions of the fix stay visible.** `open` is unrealized and can evaporate (T-041);
  the line is marked as such and the legend says so. The point is not that the open book is
  profit — it is that a verdict read off the closed half alone is read off half the book.
* **`None` ≠ `{}`.** A failed batch ticker drops the open book for that run (pre-114 output,
  one warning, no per-coin HTTP fallback — that would be ~530 serial requests inside a
  scheduler job); `{}` means "ticker fine, nothing open" and still renders a `—`. Printing a
  silent 0 for an unavailable ticker would read as "nothing running", the exact misreading
  this feature removes. Same reason `_format_block_total` returns `None` without an open book
  rather than totalling half a book.
* **No 30d cap on the open book** (operator decision Michi): a position older than the window
  counts in full — a stale open position is itself a signal (dead slot), and `n` per line
  keeps it transparent.
* **Nothing vanishes.** Bots with an open book but no close in the window are unioned into the
  block (they had no entry in `stats` at all); the block sort moved from 30d to 30d+open, which
  is identity when the open book is absent. Open rows dropped as unfilled / no price / not
  scoreable are counted and logged, never silently skipped.
* Label column widened 3 → 4 so `open`/`Σall` align with `8h`/`30d` in the monospace block.

Verified by a dry run against the live DB with `send_telegram` stubbed: 7 chunks, max 3851
chars (limit 4096), 3698 of 3729 open positions marked, all three block totals rendered in
their section's last chunk. 15 new DB-free pins in `backtest/test_market_tracker_realized.py`
(48/48). Report-only change — no bot, no gate, no lifecycle entry touched. **Live only after a
fleet restart (operator).**

## [2026-08-06] FIF2 built: bot 43, the vol-gated ladder mirror (T-2026-KYT-9050-112)

Operator go (Michi, 2026-08-06) on the T-111 verdict. New `43_ai_fif2_bot.py` mirrors fresh
fleet signals from `ai_signals` (bot-40-style ingest: age <= 240 s, self-echo excluded in the
query, in-memory seen-set, bootstrap cycle posts nothing) and reposts the ones whose symbol
clears the **rolling q80 of `sym_vol_4h`** — with the **measured t104 ladder** (LONG TP 4/5 %,
SL 5 %; SHORT TP 3/4 %, SL 2 %), not the operator's original single-TP shape, because T-111
measured the ladder beating it in every gated cell.

Design points, each pinned by `backtest/test_fif2_bot.py` (11 tests):

* **Shared vol builder.** `core/vol_features.py` now owns `rolling_std_pct` /`vol_now_pct`;
  `tools/tp1_speed_study.py` imports it (`rolling_std is rolling_std_pct` is asserted) — the
  bot serves the exact number the studies validated (hard rule 7), reading CLOSED 5m candles
  only via `read_candles(..., include_forming=False, start=history_start(...))`, stale series
  voided, never filled.
* **Rolling threshold, never a constant.** T-111's 0.5712 was a train-window quantile. The bot
  keeps the trailing 14-day distribution of every evaluated candidate (persisted through
  restarts via `core.state_utils.atomic_write_json`), gates at its q80, and posts NOTHING
  below `MIN_REFIT_N=500` samples — warm-up is logged hourly, not silent (the AIM2-TOPN
  lesson). `confidence` = the vol's trailing percentile; every evaluated candidate leaves an
  `ml_predictions_master` row (posted or not), so the gate can be recalibrated later without
  a schema change.
* **Flood cap.** `FIF2_MAX_EMITS_PER_CYCLE=5`, strongest tape first, suppressed count logged
  (T-111 measured ~376 gated posts/day; a market-wide vol regime must not dump the whole
  correlated universe into the channel).
* **Containment.** Posts to `CH_FIF2` → fallback `CH_FIF1` (the FIF channel, currently NOT
  Cornix-executed — FIF1 runs negative edge, the operator turned the channel off). Kill
  switches: `FIF2_LIVE_POSTING=0`, `CH_FIF2=0`, or a `_LIFECYCLE` entry. Tag `FIF2` (hard
  rule 6). ODS1-style savepoint-per-candidate, one commit per cycle (hard rule 8).

Registration (ODS1 checklist, PR #276 pattern): `core/fleet.py` (delay 291 — 283 stays free
for ODS1 so monotonicity holds under either merge order), `core/config.py` (`CH_FIF2`),
`core/bot_catalog.py` (`FIF2` before `FIF`, longest prefix wins), `.env.example`, README bot
table, `docs/NEW_IDEAS_BOTS.md` pointer. Also re-pinned `EXPECTED_WATCHDOG_VIEW` in
`backtest/test_fleet_definition.py`, which had silently drifted six bots behind FLEET
(36–41 never grew the anchor; the test was red the whole time — backtest/ runs in no CI job).

**Not in this PR, operator actions:** watchdog restart to supervise bot 43 (new FLEET entry is
only picked up on watchdog restart), optional `CH_FIF2` in `.env`, Cornix stays off on the FIF
channel until the shadow phase measured the fill gap (T-111's named risk: a vol gate selects
exactly the fast tapes where bot 40 measured 18/101 mirrors >1 % away), and any bot-40 roster
seat for FIF2 is a separate decision.

**Found in the pre-merge review, fixed in this PR:** the flood cap's suppression counter was
assigned inside the candidate loop, so each later candidate overwrote it and the cycle line
reported the remaining tail — a 20-candidate burst with 5 emitted logged "1 over the cap"
instead of 15. The emission decision was never wrong, only the number that makes the drop
visible, which is exactly what "no silent caps" is supposed to guarantee; the cap test was
named "…and counts the rest" but asserted only the emission count, so it could not see it.
Counted up now, with the log line itself asserted. Second finding, documentation: the measured
ladder edge is priced with the stop moving to breakeven after TP1 (`_breakeven_step` in the
simulator) — the bot cannot enforce that, it is **Cornix channel configuration**, and the
docstring pointed at an "ops section" that did not exist. Without it the runner carries the
full SL instead of 0, so the live geometry would not be the measured one: named as a go-live
precondition on the bot itself. Third, the bot's test loader stubbed `sys.modules` through
`mock.patch.dict`, which restores the whole snapshot on exit and therefore drops every module
imported inside the block — including numpy's C submodules, whose re-import raises "cannot
load module more than once per process" on Python 3.14. Any suite run that reached this file
before another numpy-using test died on collection; the loader now swaps only the two keys it
stubbed. Since ODS1 (PR #276) merged first, `EXPECTED_WATCHDOG_VIEW` also gained its entry —
the reserved 283/291 split held, as intended.

## [2026-08-06] FIF2 decision backtest: the vol gate carries, the single-TP exit costs (T-2026-KYT-9050-111)

Operator proposal: replace FIF1 with a bot that mirrors fleet signals passing the T-110
volatility gate and posts them with a single TP (100 % out at TP1). New
`tools/fif2_single_tp_backtest.py` runs the go/no-go: {ungated, q80, q90 train-quantile
vol gate} x {single-TP, T-105 ladder}, t104 geometry primary, chronological 70/30 split,
fees included; ladder cells reuse `tools.portfolio_backtest.precompute` verbatim. The
pre-registered bar: gated single-TP must be positive on TEST **and** beat its ladder twin
per slot-hour.

**Half the bar holds.** On test (t104): gated single-TP is genuinely positive — q80
+0.192 pp/trade (t=2.7, WR 51.3 %), q90 +0.220 (t=2.2), median hold collapses to ~0.7 h
and average concurrency to ~48 of 500 slots, versus an ungated book that is negative on
train and marginal on test. The gate also rescues the negative symmetric_tight geometry
(-0.153 -> +0.197). The T-110 finding survives contact with money.

**But the ladder twin wins every gated cell** — per trade (+0.314 vs +0.192 at q80) and
per slot-hour (+0.091 vs +0.063; same ordering at q90 and under symmetric_tight). "100 %
out at TP1" gives away the runner exactly on the high-vol trades the gate selects.
Portfolio sanity run (t104 single-TP q80, 1000 EUR, 5 EUR, 5x, T-105 admission):
+3.51 %, maxDD -0.88 %, no binding constraint.

**Verdict: do not build the bot as proposed; build it with the ladder exit.** The gate is
the value; the single-TP exit destroys ~40 % of it. Caveats before any go-live, stated
loudly: the test split is ~8 days / 2 ISO weeks; entries fill at signal price and a
vol-gated bot is MORE exposed to fill slippage than the fleet average (bot 40 measured
18/101 mirrors >1 % away within 15 min); domain fit 68 %. Rework posts under a NEW tag
(FIF2) per hard rule 6; FIF1 replacement is an operator decision.

Plus `backtest/test_fif2_single_tp.py` (5 tests: TP-first pays tp-fee, tie books the full
stop, horizon mark exit, hold time = touching candle's close, slot-hour = sum/sum).

## [2026-08-06] TP1-speed study: the symbol's own volatility decides, BTC context does not (T-2026-KYT-9050-110)

Operator question: can market context at signal time (BTC, BTCDOM, OI, funding, forced liqs)
predict which trades reach TP1 fast? Economically this is the T-105 slot-turnover question —
the bot-40 book is margin/slot-bound, so a trade that takes TP1 in an hour returns its margin
~70x faster than one that limps to the 72 h horizon.

New `tools/tp1_speed_study.py` (DB-free, on the corrected T-104/T-105 exports). Pre-registered:
label = TP1 strictly before SL (tie -> SL) within 4 h under the t104 geometry (12 h secondary);
features as-of the last CLOSED candle (hard rule 5), chronological 70/30 split, per-week AUC
consistency; Bonferroni bar stated up front. 43,319 covered signals, base rate 16.0 %.

**Findings (test-set confirmed, 5/5 weeks consistent):**

* **`sym_vol_4h` — the signal symbol's own 4 h realised vol — AUC 0.79 train / 0.77 test.**
  Decile spread 1.9 % -> 47.7 % fast-TP1. The tautology check (does vol just speed up
  everything, SL included?) says no: TP1-first RISES with vol (31 % -> 52 %) and in the top
  decile TP1-first (51.9 %) overtakes SL-first (47.8 %), while low-vol trades leave ~30 %
  of positions unresolved at the horizon — 72 h of dead slot.
* **`oi_pct_30d` AUC 0.62/0.61** — the only OI feature above noise; likely partly vol in
  disguise, not independently established.
* **BTC and BTCDOM context: nothing.** All six features AUC 0.48–0.52 — the operator's
  BTC-conditioning hypothesis is refuted on this window.
* SHORT hits fast TP1 more than LONG (18.3 % vs 13.7 %) — geometry-mechanical (3 % vs 4 %
  target), not a finding.

Gaps stated, not buried: funding and forced-liq features need the VPS DB or an API backfill
(`liq_events` only exists since 2026-08-03); signals cluster in time so the effective N is
far below 43k. **This is an association verdict, not a deployable gate.** The deployable
follow-up is explicitly NOT run here: vol-conditioned admission in the T-105 portfolio
simulator, measuring PnL per slot-hour — flagged as the next task if wanted.

Two further gaps, found in the pre-merge review and named rather than buried: the task brief
also listed a **time-of-day** feature, which was never built — nothing here says whether the
signal hour matters. And **no significance test is computed**: the Bonferroni line quoted in
the pre-registration (p < 0.0019) is never applied, so the verdicts rest on AUC magnitude plus
per-week sign consistency alone. For `sym_vol_4h` (AUC 0.79 at n ~ 30k) that is academic; for
the 0.54–0.56 band it is precisely the "signal vs suggestive" line, so that band stays
suggestive. Neither gap moves the headline — both are follow-up work.

Plus `backtest/test_tp1_speed_study.py` (7 tests: same-candle tie books as SL, forming
candle never enters features, hit time = touching candle's close, NaN on missing history,
tie-averaged AUC).

## [2026-08-06] Profit-securing sweep: no skim/refill ratio beats withdrawing profits (T-2026-KYT-9050-109)

Follow-up to T-108, which pinned the two corners of the capital-split scheme (symmetric =
disguised half-size bucket, pure ratchet = starved compounding base). This sweeps the middle:
`tools/capital_split_backtest.py --sweep` runs the full skim x refill grid (4x5, dynamic 1 %
sizing, 50:50 split) on both geometries, with two new protection metrics per cell
(`min_total_equity`, `reserve_low_water`).

**Result: the transfer knob does not touch anything that matters.**

* On the positive geometry (`t104`) every one of the 20 cells lands between −0.82 % and
  +0.59 % against the plain half-size bucket's **+1.50 %** — and the grid has no monotone
  structure in either direction: the cell-to-cell spread is admission-path noise (which
  trades clear the margin check shifts with the sizing path), not signal.
* The equity floor does not improve either: reference `min_total_equity` 986.85 vs
  984.4–986.5 across all cells.
* Real irreversibility exists only at `refill = 0` (`reserve_low_water` pinned at 500 by
  construction, final reserve up to 614 on the losing tape) — but that is just "withdrawn
  profits are safe", available from any single account with a withdrawal rule, and it costs
  the compounding base (available-bucket maxDD up to −25.5 %).
* On the negative geometry the split softens total maxDD slightly (−2.6 to −3.0 vs −3.31)
  purely because dead margin means a smaller book — trading less, not protection.

Verdict: **profit securing via intra-account transfer rules is a dead end on this harness.
If the operator wants banked profits, the honest mechanism is a plain withdrawal rule
(sweep profits above the starting capital off the trading account); the trade-off is the
same one the ratchet column prices.** Same T-105 caveats: one refit, ~5 tradeable days.

## [2026-08-06] Capital-split money management: the 50:50 reserve is a disguised half-size single bucket (T-2026-KYT-9050-108)

Operator proposal, simulated on the T-105 walk-forward harness before anyone wires it into a
bot: 1000 EUR split 50:50 into an available and a locked reserve bucket, trades at 1 % of the
available side (5 EUR), 50 % of every win skimmed into the reserve, 50 % of every loss refilled
back out of it.

**The transfer rules are symmetric, so the scheme is arithmetically a single bucket at half the
size fraction.** Every closed trade moves `0.5 * pnl` into each bucket; while the reserve is
solvent both track `500 + 0.5 * cum_pnl` exactly — confirmed to the cent on the real tape
(`final_available == final_reserve` in every run, zero capped refills). The reserve neither
ratchets nor protects: it fills and drains at the same rate.

**Where it does differ, it only costs.** The reserve is dead margin — only the available half
backs open positions, so concurrent occupancy halves (peak 100 vs 204) and margin rejections
double (777 vs 445). On the only positive geometry (`t104`, L 4/5 S 3/2, 5x, ~5 tradeable
days) that admission haircut cuts the same-sized single bucket's +1.50 % to **+0.08 %**; under
negative geometry it "loses less" (−1.04 % vs −1.54 %) only by trading less. A one-way ratchet
variant (skim without refill) does bank 55 EUR into the reserve but starves the compounding
base (available maxDD −11.1 % vs −1.5 %) and lands at −0.02 % total.

New: `tools/capital_split_backtest.py` (two-bucket variant of the T-105 event loop, same
admission rules and export gate), `backtest/test_capital_split_backtest.py` (5 invariants:
bucket symmetry, single-bucket equivalence, dead-margin divergence, reserve floor, ratchet
one-sidedness), `reports/capital_split_backtest.json`. Verdict for the proposal as stated:
**not deployable as a protection mechanism — it is a sizing choice, and the honest way to get
its risk profile is to trade 0.5 % of one bucket.** Same eight-week caveat as T-105: one refit,
~5 tradeable days, entry fills optimistic.

## [2026-08-06] ODS1's TP ladder had a dead second rung — TP2 widened to 2.0 % (T-2026-KYT-9050-113)

Found by the operator within the first hour of ODS1 trading live, which is the shortest path
from "shipped" to "caught" this fleet has had.

ODS1 posted `TP_PCTS = (1.0, 1.5)` — the two rungs **0.5 % apart**. Cornix splits the position
50/50 across the ladder, so at that spacing the second rung is not a separate event: whoever
reaches TP1 takes TP2 in the same move. Two of the first four live trades closed "ALL TARGETS
HIT" within minutes. The leg was effectively trading a single target at ~1.25 % while the book
recorded a full ladder success — which biases exactly the live evaluation ODS1 went live to
produce.

Measured across the fleet on signals since 2026-08-04, ODS1 was the **only** leg under a 1 %
minimum gap, and also carried the smallest TP1 distance of any leg:

| leg | TP1 distance | min gap |
|---|--:|--:|
| AIM2 | 7.21 % | 3.67 % |
| ROM1 | 3.09 % | 2.01 % |
| EPD3 | 2.63 % | 1.82 % |
| ATS2 | 2.02 % | 1.38 % |
| TSM1 | 2.00 % | 1.24 % |
| **ODS1** | **1.00 %** | **0.50 %** |

**Fix (operator decision, Michi): `TP_PCTS = (1.0, 2.0)`.** TP1 stays on the measured T-096
drift (+0.41 % @1h, +0.73 % @4h) — that number is why the bracket is tight at all. TP2 now sits
1.0 % beyond it and equals the SL distance, i.e. a symmetric 1R second rung. The rejected
alternative was collapsing to a single target: more honest to the measurement (T-096 measured a
drift, not a ladder) but it discards the staged exit entirely.

**Why the guards did not catch it.** They pinned the ceiling (`max(TP_PCTS) <= 2.0`) and the
ordering, and never the *spacing*. The bracket was translated faithfully from the study and the
fleet convention was simply never checked — sizing a bracket to the measured effect is right,
but it does not license ignoring how the venue executes the ladder. `MIN_TP_GAP_PCT = 1.0` now
pins the rule, with the fleet table recorded in the test so the convention lives where it is
enforced, and a companion guard rejects a last rung beyond the stop.

The threshold constant is pinned to its exact value as well: mutation showed the guard could
otherwise be defanged by lowering `MIN_TP_GAP_PCT` instead of widening the ladder — the same
hole found in `DOMAIN_FIT_MIN` on PR #274 one commit earlier.

Takes effect at the next fleet restart. Signals already posted under the old ladder keep their
geometry; Cornix holds them as published.

## [2026-08-05] Bot 40's `SOURCE_CLOSED` exit stays — the replay refutes the case for removing it (T-2026-KYT-9050-106)

Closes `AUDIT_TODO#T106-1`. **No code in bot 40 was changed.** The finding is that a
change other work was queued behind should not be made.

The observational case looked strong: 433 `SOURCE_CLOSED` positions exiting at −3.69 %
against `TIME_STOP` at −1.31 %, and removing the rule flipping the arm's book from
−0.40 to +0.72 pp/trade. It carried its own caveat — the buckets are not randomly
assigned — and that caveat turned out to be the whole story.

**Two defects.** First, it pools across `TIME_STOP_SINCE` (2026-07-28 14:00Z). Bot 40
went live 07-26, so the first two days of `SOURCE_CLOSED` come from a window where the
rival policy *could not fire at all*. Split, the gap is 1.41 pp, not 2.4 pp. This is the
same cutoff, and the same pooling trap, that the T-104/T-105 handover warns about two
sections above the item. Second, `SOURCE_CLOSED` fires when the source trade leaves
`ai_signals` — i.e. because the source hit its stop. Membership is conditioned on the
outcome, the same defect as bucketing by `closed_ai_signals.status`.

**The load-bearing error:** the cut removes those trades from the book, which assumes
they never happened. They happened, and they were already 2.75 % underwater when the
source closed. Removing the exit rule does not remove the loss — it defers it.

**Paired replay** over the 174 post-cutoff rows, each position its own control, 5m
candles, `core.trailing_state.TrailingState` reused rather than reimplemented so the
trail semantics cannot drift: **+0.141 pp/trade, SE 0.160, t = 0.88, CI [−0.17, +0.45]**.
Null. Stable at +0.149 with censored rows dropped and +0.059 under an optimistic
intra-candle ordering — every modelling choice was set against the hold-longer policy,
so the null is not a friendly assumption. 36 % of the cohort (63/174) simply walks on to
the stop it was already sitting on, −7.885 → −8.238.

The pre-cutoff cohort is excluded on purpose: 143 of its 268 rows carry `sl IS NULL`
(pre-T-049), so stop exits cannot be modelled there and a replay would let losers run
unbounded, flattering the hold-longer policy by construction.

**One argument checked and dropped — then corrected in review, twice, against itself.**
The first version of this entry said holding longer costs 100.5 extra slot-days, which at
the arm's +0.432 pp/slot-day would make the change net-negative under a binding cap, and
that concurrency "peaks at 97 of 500". Both figures were wrong.

* The 100.5 charged the full 7-day replay horizon to the 11 right-censored positions,
  which had only 0.5–17.1 h of forward data. Correctly: **28.0 slot-days (0.16/position)
  = 12.1 pp** against a +24.6 pp replayed gain — so even under a binding cap the change
  would be net **positive (+12.5 pp)**, the opposite of what was claimed.
* The 97 was wrong twice: it counted only mirrors *opened* after the cutoff, which is not
  occupancy, and its filter `close_reason NOT IN (…)` silently dropped all 102 still-open
  mirrors, because `NULL NOT IN (…)` is `NULL` rather than `true`. Every mirror open
  during the window: **peak 190 of 500** (302 all-time, mean ≈ 102); 173 counting only
  rows that have since closed.

A third slip is recorded here for the same reason: "0.15/position" was itself wrong
(28.0/174 = 0.16) — a bad derived number inside the paragraph correcting bad derived
numbers. Caught by the re-review, not by this study.

The verdict is unchanged (190/500 = 38 % is still not binding, so freed slots have no
alternative use), but the slot argument no longer supports keeping the rule and must not
be quoted as if it did. Both errors sat in the one paragraph this entry singled out for
its own honesty, which is the reason they are corrected here in full rather than edited
away.

**The null is underpowered, not absolute.** Variants A–C vary intra-candle ordering and
censoring; the never-varied peak-lag rule is worth about the whole effect — relaxing it
gives +0.270 (t 1.64), or +0.315 (t 1.93) without the carried peak. No variant either
side ran flips the sign. So: eight days, n=174, direction consistently positive, never
significant. That still refutes the observational +1.12 pp/trade by an order of
magnitude; it does not establish that `SOURCE_CLOSED` earns its keep.

Separately, `SOURCE_CLOSED` is not primarily a PnL rule: the mirror must not hold a
position the source no longer holds, or the A/B arm stops measuring the same trades
(`40_trailing_close_bot.py:850-854`). With the PnL case null there is nothing on the
other side of that trade. Does **not** close `#T52-3`.

Verdict: `docs/T-2026-KYT-9050-106-source-closed-replay.md`.

## [2026-08-05] ODS1 registered with the fleet — the bot existed, the fleet did not know (T-2026-KYT-9050-106)

`42_ai_ods1_bot.py` shorts a rally that open interest did not pay for — the only one of
three OI mechanics that survived the T-096 event study (+0.41/event @1h, t = 3.2, n = 580,
8 of 9 weeks positive).

The bot, its channel constant and its roster seat landed earlier on this branch. Nothing
made the fleet aware of it — a file is not a process. Now registered in the three places
that matter: `core/fleet.py` (the only startup registry, `start_delay=283`, appended last
because `test_start_delays_are_monotonic` pins list order to ascending delay),
`core/bot_catalog.py` (tag → script; the catalog guard requires the mapped script to
exist in `FLEET`, so both edits belong in one commit), and `tools/bot_variants/index.py`
(ODS1 is rule-based with no model artifact, so without a `_RULE_ONLY_GENERATIONS` entry
it is undiscoverable and drops silently out of the index).

**Evidence status — corrected 2026-08-06, and it got weaker.** This entry originally
claimed T-104 independently reproduced the mechanism from the other side. That support is
**withdrawn**, on two counts found while reviewing PR #274:

* *Not independent in time.* T-104's replay starts 2026-07-11 (`export_meta.since` in its
  own committed report), so T-096's window (06-12..08-04) **contains** it. The handover's
  "T-104 ran 13.06.–05.08." was simply wrong, and this entry repeated it.
* *Look-ahead in the feature.* T-104 reads signal instants from
  `closed_ai_signals.open_time` — a naive column — `AT TIME ZONE 'UTC'`. Measured per
  model against the 5m candle the entry must fall inside: EPD3 95.0 % vs 11.7 %, MIS1-72H
  59.6 % vs 11.5 %, BR1Hv2 40.7 % vs 10.9 % all favour Bucharest (+3 h); only ROM1 is real
  naive UTC (86.8 % vs 8.0 %). So **84 % of the SHORT population feeding that gate is
  stamped 3 h late**, and its "4 h OI change before the signal" actually spans
  [t−1 h, t+3 h] — straddling the signal, containing post-signal OI. A drop measured partly
  after entry can be positions closing: consequence, not cause.

Also corrected: "the only finding in that study that survived both regime cohorts" was
false — at T-104's own TP3/SL2, 18 legs are sign-stable positive in both cohorts (5 under
the n≥40 filter its §4 applies), including EPD3-SHORT on n=6864/2352.

ODS1 therefore stands on **T-096 alone**: one study, one tape, its own ≥90 d regime gate
unmet. Going live is the operator's deliberate substitute (forward data on a new tape),
now explicitly a one-pillar bet rather than a replicated one. Nothing in the bot's own code
is affected — it computes its OI lookup as-of against `oi_5m` at the real instant, with a
staleness cap and no forward fill. The bracket (TP 1.0/1.5 %, SL 2.0 %) is sized to the measured drift
rather than inherited from the fleet default, and is the first thing to re-derive once
ODS1 has live rows of its own; the roster density sits at the bottom (0.010) on purpose,
because the column is the eviction order and an unmeasured leg yields its seat first.

**Not live yet.** `FLEET` is read at watchdog import, so the entry is inert until the
operator restarts the fleet (hard rule 1). `backtest/test_fleet_definition.py`'s
`EXPECTED_WATCHDOG_VIEW` golden was left untouched: it has been stale since T-149 and was
already red before this branch (missing bots 36-41, now 36-42). Refreshing it here would
be a silent guard reset (hard rule 9).
## [2026-08-06] Walk-forward portfolio simulator — four defects, and every headline reversed (T-2026-KYT-9050-105)

The simulator answers the sizing question T-104 could not: per-trade expectancy cannot see a
slot cap, so it needs an event-driven run with occupancy, margin and the venue's ceilings.
It shipped with four defects, all found by the core review, all of which had to be fixed
before any number here means anything.

* **`exit_ts` falsy-zero.** `_exit_step` legitimately returns index 0 when TP or SL is touched
  on the first closed candle; `if step_exit` read that as "never exited" and pinned the trade
  to the full 72 h horizon. A five-minute round trip was booked as 72 h of slot occupancy —
  and because tighter geometries produce more first-candle touches, the bias fell unevenly
  across exactly the variants being compared.
* **Walk-forward look-ahead.** `select_legs` filtered the trailing window on `open_ts` but
  scored with the fully realised `pnl_pct`, so at each weekly refit the last 72 h was selected
  on outcomes that had not happened. Membership is now on `exit_ts`.
* **Max drawdown understated twice.** The curve began after the first close, so initial capital
  was never a peak (a −50 % single trade reported 0.0); and `sorted(equity)` reordered
  same-timestamp exits by balance, smoothing intra-instant dips away. Drawdown is the number an
  operator sizes on, so a floor is the wrong direction, not a conservative one.
* **A breakeven runner was pinned to the horizon.** When TP1 fires and the second rung does
  not, PnL credited breakeven while occupancy said "held 72 h" — the two halves of one trade
  disagreeing, inflating hold time for the modal winner under tight geometry.

Plus: `binding_constraint` reported `exposure_cap` when nothing bound at all, and the equity
curve — the task's first-named deliverable — was computed and discarded.

**Re-run on the corrected T-104 export (the mixed-domain `open_time` fix, same PR series), the
conclusions invert.** At 800 USD capital, 1.6 USD size, 5x:

| geometry | before | after |
|---|--:|--:|
| `symmetric_tight` (the old winner) | +13.54 % | **−1.04 %** |
| `symmetric_wide` | +2.72 % | −1.11 % |
| `t104` (L 4/5, S 3/2) | +4.85 % | **+0.70 %** |
| `inverted` (falsification control) | +13.25 % | **−3.47 %** |

**What caused the reversal — corrected twice, because the first two versions of this table were
both wrong.** The original entry said "trades collapse from ~9,000 to ~800 once leg selection may
no longer see the future", attributing the reversal to the look-ahead fix. The first correction
replaced that with a 2×2 whose cells came from an intermediate code state. Re-measured against
the code actually committed here (`symmetric_tight`, 800 USD / 1.6 USD / 5x):

| | code before the fixes | code as committed |
|---|--:|--:|
| **old export** (55,852 signals, since 06-13) | 9,352 / **+13.54 %** | 12,206 / **+14.71 %** |
| **new export** (43,330 signals, since 07-11) | 747 / **−1.16 %** | 801 / **−1.04 %** |

Swapping the export moves the headline **−14.70 pp** (before the fixes) or **−15.75 pp** (after).
The four code fixes move it **+1.17 pp** on the old export and **+0.12 pp** on the new one.

So: **the reversal is the export, not the code** — by two orders of magnitude, and the
conclusion is unchanged from the first correction even though its numbers were not. The fixes
are real and order-preserving; they are not what flipped the sign. (+13.54 % is exactly this
PR's original headline, which is what pins the pre-fix baseline.)

Writing a fresh mis-attribution into a permanent record, in a PR whose entire subject is a
mis-attributed finding, is why this is spelled out rather than quietly edited — twice now.

**"Walk-forward" is one refit, and the geometry ranking is withdrawn.** On the corrected export
`TRAIN_WEEKS = 3` consumes most of the sample: the window runs 2026-07-10 → 08-06, the single
refit fires at 07-31, and **90.9 % of RECORDS (39,380 of 43,319) sit in the un-tradeable
warm-up** — 80.3 % of the elapsed TIME, which is what the report's `warmup_share_pct`
field reports; the two are different denominators and both are worth knowing — which is why every cell shows ~42,000 `rejected.leg` against ~800 trades. The
out-of-sample span is **5.16 days** with 6–7 daily P&L observations. `TRAIN_WEEKS` was sized for
the 51-day export; the corrected one is 24 days.

A four-way geometry ordering spanning −3.47 % to +0.70 % over five days has no statistical
content, so the claim "t104's asymmetric geometry is the only variant that is not negative" is
**withdrawn as a ranking**. What the run supports is the weaker and sufficient statement: **no
configuration returns anything worth acting on over this window.** The report now emits
`n_refits`, `tradeable_days` and `warmup_share_pct` so this cannot be read off silently again,
and `trades_per_day` is computed over the tradeable span (155.3/d for `symmetric_tight`, not the
30.8/d the full-window divisor produced — a 5× understatement of the number that has to be read
against the Cornix 500-slot cap).

The tool now also **refuses a defective input**: it runs the same timestamp-domain gate on the
export it is handed and aborts below the threshold. Its `--in` default pointed at the *old*
`t105_raw.npz` (domain fit 0.236), so running with defaults silently regenerated the withdrawn
+13.49 % with nothing in the output naming the source. The report header now carries `input`,
`input_since` and `input_domain_fit`.

Tests 12 → 22, each defect pinned and mutation-verified — including three guards that had to be
rewritten because the first versions were tautological: the `exit_ts` guard (twice), the
same-timestamp drawdown guard (the loser closed first, so `sorted()` was a no-op and the
mutation stayed green), and the breakeven guard (candles straddled the entry on both sides, so
the LONG/SHORT swap was invisible).

## [2026-08-05] Fleet-wide leg composition replay — and what the Cornix backtest actually measured (T-2026-KYT-9050-104)

> **Corrected 2026-08-06 (T-2026-KYT-9050-107) — the headline finding did not survive.**
> This study read `closed_ai_signals.open_time` as UTC. That column is naive and
> **mixed-domain**: `28_signal_orchestrator` (ROM1) writes explicit UTC, the other 13 writers
> leave it to `DEFAULT now()`, which stamped session-local Europe/Bucharest until the R3 pool
> flip took effect at the staggered fleet restart (measured: Bucharest through 2026-08-02
> ~17:00, mixed to ~20:00, UTC after). So ~77 % of signals were placed 3 h late (33,177 of 43,152 closed rows).
>
> **Consequence: the OI short-side filter is retracted.** Re-run on the corrected export, same
> geometry and quintiles, `oi_chg_4h | SHORT` bottom quintile goes **+0.739 → +0.324**
> (pre) and **+0.552 → −0.065** (post), while the *top* quintile becomes the best bucket
> (+0.340 / +0.230) and the AUCs move from 0.463/0.458 to 0.501/0.518 — a coin flip. The
> apparent edge was the defect: at 3 h late the "4 h OI change before the signal" actually
> spans [t−1h, t+3h], straddling the signal and carrying post-signal open interest. An OI drop
> measured partly *after* entry can simply be the position closing.
>
> §4's direction claim survives qualitatively but not in magnitude: at TP3/SL2 the corrected
> book is **66 % LONG / 34 % SHORT**, not 76 % SHORT. §6 (short legs are regime-unstable) is
> the section the correction leaves standing — 8 of 9 SHORT legs still flip negative across the
> cutoff.
>
> **This also removes ODS1's second evidence pillar** (bot 42, T-2026-KYT-9050-106), which had
> already withdrawn its reliance on this result for other reasons. T-096 is unaffected —
> `tools/oi_event_study.py` builds its events from `oi_5m` and never touches the signal tables.
>
> The export now carries a writer-aware conversion and a **hard gate**: it fails when the
> recorded entry stops falling inside the candle at its claimed instant. That check reads
> 68.2 % on the corrected export — over the closed rows alone 0.686 against 0.267 for the
> defective all-UTC read — so the old behaviour could not ship through it. Sections 1, 2, 3, 5 and "Data quality" are marked in the doc as ad-hoc
> session observations — no committed code or artifact produces them, which the first version
> did not state. The numbers below in this entry are from the ORIGINAL run and are superseded
> by the doc.

A Cornix backtest of the AIM and Drawdown channels over 01.-05.08. spanned +109.6 % to -50.3 %
across four position sizes. The rows reconcile against their stated capital; comparing them to
each other does not. Cornix sizes off the **available** balance, so mean position size saturates
(1.07 % at a 5 % setting, 1.24 % at 10 %) while the effective sample collapses from 299 to 131.
Only the fixed-amount run allocates uniformly. Above ~1 % the percentage setting buys no
exposure, only concentration. **(Ad-hoc session measurement, not reproducible from the committed
tools — see the provenance note in the doc.)**

New: `tools/leg_composition_replay.py` and `tools/oi_gate_eval.py`, both split into a read-only
DB `export` and a DB-free `replay`/`gate` half, so the expensive part runs off the live VPS
(which sits at a measured 97 % CPU) and the analysis is reproducible without credentials.
11 standalone tests in `backtest/test_leg_composition_replay.py` pin the replay conventions —
first touch wick-aware, entry candle excluded, TP+SL in one candle books as SL, unresolved
marked to market, and the regime cohorts never pooled.

Findings over 42,277 signals / 3.86M 5m candles (docs/T-2026-KYT-9050-104-leg-composition.md):

* **Direction edge is a property of the exit geometry, not the market.** At TP 4 % / SL 5 % the
  positive-expectancy legs compose a book 95 % LONG; at TP 3 % / SL 2 % they compose it 76 %
  SHORT. Same data, same period. Down-moves are faster, so shorts want a tight target and a
  tight stop while longs want room.
* **The 2026-07-28 14:00Z regime split is mandatory.** `EXPOSURE_CAP` took the book from 83 % to
  51 % LONG and -1.342 to +0.191 pp/trade, and removed the tail (worst day -549 pp before,
  -37.7 pp after). Pooling the cohorts had produced a confident and wrong verdict about
  MIS1-72H, which post-cutoff runs +0.38 pp over 149 trades.
* **Short legs are regime-unstable** — the same legs carry 14,806 signals in positive legs over
  11.-28.07. and 4,220 in negative ones over 28.07.-02.08. Direction balance therefore has to be
  a channel-level constraint, never an emergent property of an expectancy ranking.
* ~~**OI as a short-side filter.**~~ **RETRACTED 2026-08-06 — struck, not superseded.** This
  bullet claimed the bottom quintile of the 4h OI change was "the one result that survives both
  cohorts" (+0.739 / +0.552 pp) and that it "independently reproduces T-096's DIVERGENCE-SHORT".
  Both the result and the reproduction claim are withdrawn: on the corrected export the bottom
  quintile reads **+0.324 / −0.065**, the *top* quintile is the better bucket (+0.340 / +0.230),
  and the twelve AUCs span 0.498–0.535 — so "no AUC above 0.56" is also stale, and the effect is
  a coin flip. The apparent edge was the timestamp defect described in the notice above.
  It is struck here rather than left under that notice on purpose: the notice disclaims
  *numbers*, and what stood here was a *conclusion*.

Data quality, measured rather than assumed: `oi_5m` is not a 5-minute table — median cadence was
5.0 min until 06.07. and 10.0 min from 13.07. onward, so T-2026-KYT-9050-097 now has a date.
Lookups are as-of with a 45-min staleness cap and 597 signals were voided rather than filled.
`liq_events` starts 03.08., so no liquidation feature is testable yet (T-095).

No live change: roster, Cornix configuration and fleet are untouched. Sizing and trade count are
explicitly **not** answered here — per-trade expectancy misses the ladder, fills and fees (the
book reports +0.69 pp/trade for AIM2 where Cornix implies +0.054) and needs a portfolio
simulation, walk-forward because the fleet grew 6x over the window.

## [2026-08-04] The T-101 startup log computed its floor from a placeholder (T-2026-KYT-9050-103)

Found by verifying the fleet restart rather than by a test — which is the point of the entry.
The live startup log read:

    AIM2-TOPN active — N=1, effective floor=0.85 (configured 0.85, artifact 0.80, bot floor 0.70)
    AIM2 artifact loaded: 86 features, threshold 0.67 (effective gate 0.70, floor 0.70)

`artifact 0.80` against a real threshold of 0.67. The TOPN startup block reads
`ARTIFACT["threshold"]` but sat **above** `load_model()`, so it saw the module-level
initialisation default (`"threshold": 0.80`) rather than the value from the pkl. The number it
printed was therefore derived from a placeholder — the precise failure class
T-2026-KYT-9050-101 was written to eliminate, relocated instead of removed.

It is invisible right now only by coincidence: `max(0.85, 0.80, 0.70)` and
`max(0.85, 0.67, 0.70)` are both 0.85. Let a retrain land a threshold above the configured
floor and the startup line would advertise 0.85 while the gate used the higher value — and the
next person diagnosing a starving leg would be reading a number the bot never applied.

**No money path.** `process_master_trades()` recomputes `topn_min` per cycle from the loaded
artifact; the runtime gate was always correct. This is diagnostics only.

**Why the tests did not catch it, and what changed:** every guard around this line pinned its
*shape* — that the effective floor is printed, that the shared helper computes it, that no
hand-rolled `max(...)` returns. None pinned its *ordering* relative to `load_model()`. A green
21-test suite and five red mutations all passed straight over it. The new guard compares source
positions: the startup `effective_min_prob` call must appear after the first `load_model()`.
Verified red against the pre-fix layout.

Takes effect at the next fleet restart. The running fleet keeps printing the placeholder-derived
line until then, harmlessly while 0.85 dominates.

## [2026-08-04] AIM2-TOPN floor lowered to 0.85 — operator decision, and a correction to yesterday's rate estimate (T-2026-KYT-9050-102)

Closes `AUDIT_TODO#T101-2`. Operator decision (Michi): set `AIM2_TOPN_MIN_PROB=0.85` in the
live `.env`, so the leg T-2026-KYT-9050-101 found silent for 24 days can actually post.

**Correction to the T-101 entry above.** It said lowering to 0.85 would mean "~4.4 posts/day,
roughly 30× the intended rate". That was wrong, and the error is worth naming because it
misrepresented the decision: the `Posts/Day` column of `tools/aim2_topn_calibrate.py` is
`passed / days` — the number of **eligible candidates**, not posts. The tool states this
itself ("Set `AIM2_TOPN_N` as backstop independently"). `select_topn` caps selections at
`n - posts_last_24h`, and with `AIM2_TOPN_N` unset the default `N = 1` applies: **at most one
post per rolling 24 h**, no matter how many candidates clear the floor.

So at 0.85 the leg posts ~1/day — the lower edge of the 1–3/day design band, not thirty times
above it. The floor change makes the selection *selective* for the first time: it picks the
strongest of ~4.4 candidates a day instead of ~0.4.

`DEFAULT_MIN_PROB` in `core/aim2_topn.py` deliberately stays **0.95**: a fresh deployment
without an operator-set floor should still start maximally selective. The calibrated value
belongs in the environment, which is exactly where the module docstring always said it should
come from.

**No fleet restart was performed.** `load_config()` reads the environment at process start, so
the running bot keeps the 0.95 floor until the next restart. After it, three things confirm
the change: the startup line must read `effective floor=0.85 (configured 0.85, artifact 0.67,
bot floor 0.70)`, the throttled starvation WARNING from T-101 must stop appearing, and the
first `AIM2-TOPN` rows must show up in `ai_signals`/`ml_predictions_master`. If they do not
appear within ~48 h, then the still-unexplained sub-question from `#T101-2` — six candidates
cleared 0.95 in July and nothing fired — is the live problem rather than the floor.

## [2026-08-04] AIM2-TOPN was gated LIVE and silent for 24 days — the floor was never calibrated, and nothing said so (T-2026-KYT-9050-101)

Works off `AUDIT_TODO#T100-2`, opened while verifying T-2026-KYT-9050-100. AIM2-TOPN has
**zero** rows all-time in `ai_signals`, `closed_ai_signals` and `ml_predictions_master`, and
**zero** selection log lines across every watchdog log — against 3,050 base AIM2 posts in the
same period. The gate is genuinely on (`AIM2-TOPN active — N=1, min_prob=0.95, posting=LIVE`
from 2026-07-11 12:25).

Ruled out in order: the rolling 24h cap (`count_topn_posts_24h` correctly filters on
`TOPN_TAG`, so `remaining = 1`), the code (unchanged since it landed in one commit, f452dcb
on 2026-07-10; the live checkout equals `main`), and the control flow (pool init → candidate
loop → block, no early return in between).

**The cause is that `DEFAULT_MIN_PROB = 0.95` was never calibrated.** The module docstring
says it should be set from `tools/aim2_topn_calibrate.py`; it never was. That tool, run
read-only over 29,006 scored candidates of the last 30 days, says: 0.80 → **55.5** posts/day,
0.85 → **4.37**, 0.88 → **0.43**, 0.95 → **0.43**, 0.99 → 0.23. The distribution falls off a
**10× cliff between 0.85 and 0.88**, so *no* threshold produces the 1–3 posts/day the module
was specified for — the tool reports exactly that. And the 0.43/day at the live floor is
arithmetically just the 13 predictions ≥ 0.95 that exist at all, **all** of them between
2026-07-08 and 07-13, none in the 22 days since (p99 of the last 30 days: 0.84).

**The second defect is the one worth fixing in code: nothing said so.** The only TOPN log
line was at startup, so a live leg producing nothing for 24 days was indistinguishable from a
healthy quiet one. Two changes, both observability, no gate touched:

* `core.aim2_topn.effective_min_prob` — the floor was computed **twice**. The runtime gate
  took `max(configured, artifact_threshold, AIM2_MIN_PROB)`; the startup log printed the raw
  configured `min_prob`. They agree today only by coincidence of the current artifact (0.95 >
  0.67 > 0.70). With a stricter artifact the log would have advertised a floor the bot was not
  using, and the leg would starve against a number nobody ever sees. One function, both sites.
* A **throttled WARNING** when the leg is enabled, candidates were scored, and none reached
  the floor — hourly, because the scan runs every ~60 s and this log is read with `grep -a`.
  Warning rather than info on purpose: an enabled leg producing nothing is a defect state, not
  a status update.

The pre-existing source guard pinned the inline `max(...)` expression — which is precisely
what allowed the second computation to exist and drift. It now pins the shared function and
additionally forbids a hand-rolled floor at either call site: strictly stronger, not weaker.

4 new pins in `backtest/test_aim2_topn.py`, 5 mutations verified red. One of those mutations
caught a vacuous guard of my own making: `re.search(r"_log_topn_starvation.*?logger\.warning",
SRC, re.S)` happily matches an unrelated `logger.warning` later in the module, so downgrading
the call to `info` survived it. Now scoped to the function body — same class as the
small-int `is` guard T-099 had to fix.

**Not done, deliberately — all operator decisions (OPUS-HANDOFF §6):** no `.env` change, no
floor change, no gate flip, no retirement. The choice is between lowering to ~0.85 and
accepting ~4.4 posts/day (roughly 30× the intended rate), retraining so the upper tail is
populated again, or retiring the leg (`#T101-2`). One sub-question stays open and needs a
runtime trace rather than more code reading: in the ~2-day overlap where the gate was on and
six candidates did clear 0.95, nothing fired and nothing was logged.

## [2026-08-04] AIM2 persists what it publishes — the last emitter of the P2.31 class (T-2026-KYT-9050-100)

Follow-up to T-2026-KYT-9050-099 (`AUDIT_TODO#T99-2`). ROM1 was the loud half of the
persist ≠ publish class; AIM2 (`15_ai_master_bot`) was the quiet one and the last emitter
still carrying it. Like the four bots the T-2026-CU-9050-083 sweep fixed, it builds its own
inline Cornix block and its own `ai_signals` insert — and it was simply missed: the block
posted `targets[:3]`, the insert stored the whole `calculate_smart_targets` list.

Measured on 2,389 closed AIM2 trades since the P2.31 fix: **89.4 %** persisted more than the
three published targets, **46 %** stored exactly ten, and **6.4 %** of 2,633 trades were
scored `targets_hit > 3` — credit for take-profits Cornix never received. `n_show = 3` now
drives both sites.

**This is deliberately NOT the ROM1 recipe.** AIM2's geometry comes from
`calculate_smart_targets`, which already thins by 1 × ATR: measured median gaps **2.56 %**
(TP1→TP2) and **2.84 %** (TP2→TP3), and the whole TP1..TP3 ladder spans under 1 % in
**0.00 %** of signals. The #T98-1 problem does not exist on this leg, and double-thinning is
exactly what `test_smart_targets_legs_are_untouched` forbids. `n_show` is a local literal
rather than the shared `N_PUBLISHED_TARGETS` for the same reason — binding AIM2's Cornix
message to the thinner's target count would let a future thinning change rewrite it silently.

**The risk profile is materially lower than T-099's, and worth stating plainly: the Cornix
message is byte-identical either way.** Only the persisted list shrinks. Nothing about what
is traded changes; this moves the book. For new rows monitor 8 then fires ALL TARGETS HIT at
TP3 (**16.5 %** of trades reach it), confines the SL trail to published rungs, and the
`targets_hit > 3` class disappears at the source. On today's geometry the recorded exit of
the cohort stopping exactly at TP3 (n=251) rises **+8.98 % → +11.97 %**, while the cohort
that ran past TP3 (n=158) falls **+17.38 % → +13.78 %** — phantom upside, since Cornix was
fully out at TP3. Realised PnL is **unchanged** for both: at k=n=3 the close leg carries
weight 0 in `weighted_move_pct`.

`core.realized_pnl.PUBLISHED_TARGET_COUNT` keeps both entries, which are now historical-only:
`[:3]` is identity on rows written from here on and still the posted three on every older row,
so one lookup stays right on both eras. The table has stopped describing live bot behaviour
and become a permanent decoder for the archive — that is a reason to document it, not to
prune it.

Bot 15 joins the `_BOTS` map of `backtest/test_published_targets.py` (the guard that has
covered bots 9/11/12/13 since 083 and should have covered this one), plus 2 new pins in
`test_traded_targets.py`; 4 mutations verified red. **Live only after a fleet restart.**

**Found while verifying, not fixed here (`#T100-2`):** `AIM2-TOPN` is gated live in `.env`
(`AIM2_TOPN_ENABLED=1`, `AIM2_TOPN_LIVE_POSTING=1`, channel set) but has **never** written a
row to `ai_signals`, `closed_ai_signals` or `ml_predictions_master` — all-time. Even the
shadow branch would leave prediction rows, so the top-N block never fires. The conservative
`DEFAULT_MIN_PROB = 0.95` floor does not explain it alone: AIM2 produced 13 calibrated
predictions ≥ 0.95 in the last 30 days. Its persist == publish property is therefore verified
structurally (it posts through `post_ai_signal`, which slices at the insert) but not in data —
there is none. Filed as its own finding.

## [2026-08-04] ROM1's ladder: the obvious fix would have broken the measurement it was meant to protect (T-2026-KYT-9050-099)

Follow-up to T-2026-KYT-9050-098, which gave the own-geometry legs a 1 % floor between published
TPs and deliberately left ROM1 (`28_signal_orchestrator`) out. ROM1 was the worst remaining
offender: measured on **8,144** closed signals since the P2.31 fix, the median gap TP1→TP2 is
**1.00 %** and TP2→TP3 **0.94 %**, **50.1 %** and **52.0 %** of those gaps sit under 1 %, and in
**20.7 %** of signals the whole TP1..TP3 ladder spans less than 1 % — three Cornix tranches inside
one tick.

It was left out because ROM1 is not just a publisher of a slice, it **persists a different one**:
it writes its own `INSERT INTO ai_signals` (bypassing `core/signal_post`), stores `t_cands[:20]`
and posts 3. That is the last open persist ≠ publish gap from P2.31 / T-2026-KYT-9050-012, and it
is what makes the one-line reuse of `thin_targets` actively dangerous:
`core.realized_pnl.traded_targets` reconstructs the traded ladder as `targets[:3]` — the first
three **persisted** targets. Thin only what gets posted, and the posted three stop being the first
three; `traded_targets` then returns the wrong prices, silently, for the fleet's highest-volume
leg. Exactly the class T-012 exists to fix.

Three options, and what separates them is precisely their effect on monitor 8 for **running**
trades. **(b)** thinning only the message is the one option that leaves monitor 8 **untouched** —
the persisted 20 stay, so ALL-TARGETS still needs 20 rungs and the SL trail still steps through
levels Cornix never received. That reads as the conservative choice and is the opposite: the
scoring drifts *further* from what is actually traded, and it needs the persisted row to record
*which* targets were posted — a new column on a live table, operator-gated, with `traded_targets`
wrong until it ships. **(c)** moving ROM1 onto the shared `core/signal_post` path lands on the
**same** monitor-8 semantics as (a) (`post_ai_signal` persists `targets[:n_show]`, so ALL-TARGETS
and the trail sit on the same three rungs), but adds regressions: `post_ai_signal` drops the entry2
line ROM1 deliberately still publishes (T-042 arm B measured its DCA as neutral), adds a second
HTML/chart message to the trading channel, and does not set `open_time` explicitly — which would
re-break the naive-UTC contract the P1.8 follow-up fixed, killing the ±60 s `sync_closed_trades`
match again.

Implemented **(a)**: thin before persist *and* post. `thin_targets(t_cands[:20], …,
keep=ROM1_PUBLISHED_TARGETS)` sits inside `compute_rom1_trade_params`, so the replay side
(`tools/rom1_counterfactual.py`, `tools/whitelist_v2_flip_eval.py`) inherits the geometry instead
of growing a second thinner. `ROM1_PUBLISHED_TARGETS` is now bound to the shared
`N_PUBLISHED_TARGETS`, and one `rom1_published_targets()` helper feeds both the Cornix TP loop and
the `ai_signals` insert — persist == publish is structural, not a coincidence of two call sites.

**What this changes for monitor 8, named up front.** The monitor scores `len(stored)`, so
ALL TARGETS HIT now fires at TP3 instead of at a rung Cornix never received. Over **9,123** closed
ROM1 trades since 2026-07-11, **6.4 %** reach TP3 while only **0.4 %** close as ALL TARGETS HIT
today. For the cohort stopping exactly at TP3 (n=386) the recorded exit rises from **+4.58 %** to
the TP3 level **+5.40 %**; for the **1.65 %** that ran on past TP3 along the phantom ladder
(n=151) it falls from **+7.07 %** to **+5.91 %** — upside that was never real, because Cornix was
fully out at TP3. The realised-PnL number for that whole cohort is **unchanged**: at k=n=3 the
close leg carries weight 0 in `weighted_move_pct`. SL trailing and `_compute_trailed_sl` — which
posts a real Cornix `SL` command on a regime change — now index published levels only, and the
**1.8 %** of trades scored `targets_hit > 3` disappear at the source. **TP1 never moves**
(`thin_targets` always keeps the first candidate), so the break-even trail keeps its trigger
price; only TP2/TP3 reach deeper into the pool. Trades closing at TP3 free their
`orchestrator_open_trades` row earlier, a mild throughput increase against the Cornix slot cap.

`core.realized_pnl.PUBLISHED_TARGET_COUNT` keeps its ROM1 entry **on purpose**: `[:3]` is identity
on rows written from now on and still the posted three on every historical 20-target row, so one
lookup stays right on both eras and no report needs a cutoff date. Dropping it as "fixed now"
would re-inflate the whole history back to a 20-leg position model. AIM2 (`15_ai_master_bot:589`)
still persists its full list and remains the live case for the shim (`#T99-2`).

10 new DB-free pins across `backtest/test_tp_spacing.py`, `test_signal_orchestrator.py` and
`test_traded_targets.py` (129 green in those three suites), 5 mutations verified red — including
the "thin only the message" variant, which is pinned as a counter-example rather than just
avoided. **Live only after a fleet restart; the geometry change on the money path is the
operator's call.**

## [2026-08-04] TP ladders: the 1 % rule existed for the first hop only — the own-geometry legs published three tranches inside one tick (T-2026-KYT-9050-098)

Operator finding (Michi). `hvn_sr_trade_geometry` filters its target candidates against the
**entry** (`x > entry1 * 1.01`) and `ensure_min_tp_distance` only guarantees the **last** target
is ≥5 % away. Between TP1, TP2 and TP3 there was no rule at all — the published ladder was just
the head of the raw S/R level list.

Measured on `closed_ai_signals` since the P2.31 fix (2026-07-11), and the split is clean along
the generator: the legs going through `calculate_smart_targets` (which thins by 1 × ATR) have
**2–7 %** of neighbouring gaps under 1 % and a ~12 % ladder span; the legs building their own
geometry have **54 %** (EPD3), **59–60 %** (SRA2/ATS2/TSM1) and **69 %** (MAX1), with a TP1→TP3
span of only **1.45–2.09 %**. In **23–34 %** of those signals the *entire* ladder spans less than
1 %, and 16–24 % of trades hit all three at once — three Cornix tranches resolving as a single
exit. Fleet-wide, 39.5 % of all neighbouring gaps are under 1 %, 12 % under 0.2 %.

New shared helper `core.trade_utils.thin_targets` supplies the missing TP-to-TP floor
(`MIN_TP_GAP_PCT = 1.0`), applied **before** `ensure_min_tp_distance` so the 5 % backstop still
fires on the thinned ladder. Two properties carry the change. It measures each gap against the
last **kept** target, not the previous candidate — a run of near-identical levels passes every
pairwise check individually and still clusters. And it only thins when the candidate pool is
**deeper** than what gets published (`len(candidates) <= keep` returns unchanged), which is the
operator's own condition: a target is only ever skipped when there is a further-out level to take
its place. That guard is what keeps the `n_show=len(targets)` emitters out — bots 7/18/24/25 and,
deliberately, bot 10's **EPD2 legacy path**, whose published ladder *is* its pool. Bot 10 now has
one thinned path (EPD3, 3 of up to 20) and one untouched.

Wired into the ten own-geometry emitters (9, 10-EPD3, 12, 13, 14, 34, 36, 37, 38, 39) through the
shared `N_PUBLISHED_TARGETS` constant rather than a per-bot literal — a per-site number is exactly
how the entry-side 1 % ended up applied on one hop and not the other. ROM1 (bot 28) is **out of
scope**: it persists 20 and publishes 3, and trimming its candidate list would change monitor 8's
scoring semantics for running trades — the same reason T-2026-KYT-9050-012 corrected only the
measurement there.

**The replay side was pulled along in the same change**, because a bot that thins while its
replay does not is exactly the "fixed one side of the contract, forgot the other" class
`AUDIT_TODO` names as the repo's dominant root cause — every study of such a leg would then
measure a ladder the fleet no longer posts. Thinned too:
`tools/walkforward_sim.py` (`run_rub1` → bot 13, `run_ats` → bot 12, whose docstring claims
"== hourly live scan of bot 13"), `tools/tsmom_study.py` (TSM1), `tools/xs_momentum_study.py`
(XSM1), `tools/listing_drift_study.py` (LIS1) and `tools/wick_reversal_study.py` (models the
deployable geometry). `tools/epd2_build_dataset.py` deliberately stays unthinned — it is built
for the EPD2 **legacy** leg, the one bot-10 path that publishes its full list. Re-running any of
those studies now yields different numbers than the published runs; that is the fleet having
changed, not a defect.

15 DB-free pins in `backtest/test_tp_spacing.py`, five mutations verified to turn them red
(thinning disabled, gap measured against the previous candidate, the pool-depth guard removed, a
call site reverted to the raw slice, the EPD2 legacy path thinned along).

**A second consequence, unbounded:** where the pool has no third separated level, the published ladder gets SHORTER (2 TPs, or 3 with the 5 % backstop as the last rung) instead of merely wider — and Cornix splits the position across whatever it is given, so the size per tranche changes with it. How often that happens cannot be read off the tape either: the pool is not persisted. `ensure_min_tp_distance` is the floor that keeps it from collapsing to a single TP.

**Not measured: the PnL effect.** The candidate pool is not persisted (`ai_signals.targets` holds
the published slice only, P2.31), so how far TP2/TP3 actually move and what that does to realised
return needs a replay that recomputes the level pool per signal. The change is structurally
correct and pinned, not empirically validated. It is a live geometry change on the money path and
takes effect only at the next fleet restart — an operator decision.
## [2026-08-04] T-096 addendum: regime conditioning of DIVERGENCE-SHORT — bear-market objection measured, BTC-7d gate pre-registered (T-2026-KYT-9050-096)

Operator question after the study merged: "does this only work because we are in a bear
market?" Answer, measured on the same frozen events: the sample was not a bear market (BTC
net +0.2 %, BTCDOM −3.3 %), the edge survives every BTCDOM regime, and the only thing that
kills the 1h/4h horizon is a sustained BTC-7d uptrend — where it goes to ~0, it does not
invert (worst 2×2 cell −0.11 @4h; 24h stays positive everywhere). Short-term heat helps
(strongest 4h edge on BTC-24h-hot days, +1.02, t=2.6). The LONG mirror has no regime pocket
either — in BTC-up tape it loses outright (−1.45 @24h). Consequence, registered BEFORE the
out-of-sample data exists: the ≥90d re-run (~2026-09-10) evaluates DIVERGENCE-SHORT with a
causal **BTC 7d ≤ +2 %** gate (keeps ~71 % of events and essentially all PnL in-sample) and
must not tune it. Full tables: addendum in `staging_models/replay/oi_event_study_t096.md`.
Docs-only change, no code touched.

## [2026-08-04] K9 harvest: OI event study — 2 of 3 mechanics refuted, DIVERGENCE-SHORT is a candidate (T-2026-KYT-9050-096)

The three model ideas seeded with the K9 OI collector (`MODEL_CANDIDATES_SPEC_2026-07.md:416-419`)
ran as one read-only event study over the accumulated `oi_5m` history (2026-06-12 → 08-04,
234-symbol universe ≥ $3M median OI, hourly as-of grid, 24h cooldown, fees 0.10%/RT,
pre-registered thresholds — `tools/oi_event_study.py`). **SPIKE-FADE is refuted** (net
−0.46/event @4h, −2.56 @24h — fading fresh OI build-ups wins often and then gets run over in
the tail; the mean–median gap is tail-driven, so no naive inversion either). **OI×FUNDING
squeeze is refuted** at the pre-registered thresholds (net −1.44 @4h, t=−2.0; if anything the
crowded side continues — observation, not a claim). **The survivor is DIVERGENCE-SHORT**: a
≥2–3% rally whose 4h OI fell ≥2% (short-covering rally, no new money) mean-reverts — at px≥3%
net **+0.41/event @1h (t=3.2), +0.73 @4h (t=3.2), WR 58–61%, n=580, 8 of 9 weeks positive**,
monotone across the threshold matrix; the LONG mirror is dead everywhere (matches the
fleet-wide directional-edge finding). No deployment: the sample is ~7.6 weeks of one regime
(the spec's own gate was ≥60d) — the frozen script re-runs at ≥90d (~2026-09-10) as an
out-of-sample confirmation before any bot exists. Side finding with its own task
(**T-2026-KYT-9050-097**): `35_oi_collector`'s effective cadence degraded from the designed
5m to 10–30 min since mid-July plus a 45h outage on the 07-12 restart night — the 5m table is
silently becoming a 15–30m table, which would blur exactly the 1h-horizon edge found here.
Verdict: `staging_models/replay/oi_event_study_t096.md`. No live code, no DB writes.

## [2026-08-04] Bot 40: the trail really is a 2 %-scalper — and arming it at TP1 fixes the book but costs a third of the capital efficiency (T-2026-KYT-9050-093)

Two operator suspicions, measured against the live arm and then against the tape.

**"The trailings close too early" — true as an observation, dead as a lever.** Over the deployed
regime (mirrors since `TIME_STOP_SINCE`, 851 closed) the trail exits at a median peak of
**2,30 %**; **84 %** of the 494 `TRAIL` exits never saw a 3 % peak and only 4,5 % saw 5 %. And the
market kept going: for **94,7 %** of those exits a better mark was reachable within the next 24 h,
median **+3,48 pp** left on the table (mean +6,72, p90 +18,73). That number is a
favourable-excursion upper bound, not an achievable alternative — and the simulation shows why
that distinction matters: **every** later activation earns less. On the July–August window
(9 149 trades) act=5 makes 5 870 and act=10 makes 4 550 against act=2's 6 539, because the same
patience that lets winners run also keeps losers on the book (226 → 429 average slots). The
give-back `x` was refuted the same way in T-052; the activation is now refuted too.

**"Would it work if the trail started at TP1?"** New in `tools/trailing_book_health.py`: a
per-trade activation (`act_tp1`) taken from the source signal's own first target, plus the
`ts24`/`cap ±50` combinations, plus `--tp1-only`/`--tp1-impute`. TP1 is not automatically the
later bar the question assumes — **23,8 %** of roster trades carry a TP1 below 2 % (median
3,18 %; MIS2 shorts sit at 19–21 %, ATS2/SRA2/MAX1 at 1,7–1,9 %), so both the bare rule and a
`max(TP1, 2 %)` floor were measured. Under the configuration the bot actually runs the answer is
clear: **net 5 835 vs. 6 379 (−8,5 %), net per average slot 59 vs. 91 (−35 %), MaxDD 279 vs. 184
(+52 %)** — bought with the healthiest full-population book of the whole T-052 series
(**−0,11 %** against −1,26 %, 55 % vs. 69 % underwater, L/S 60/38 instead of 48/22). A second run
over the full March–August window with independently sourced (imputed) geometry lands on
**−11 % / −34 % / +48 %** and book −0,98 % vs. −1,65 % — two tapes, two TP1 sources, the same
answer to within a percentage point. Against the 800-USD envelope, where net-per-slot sets
position size, that is a pay cut, so the recommendation is **do not flip** — and to revisit it at
the 2-channel stage, where absolute net becomes the binding metric again and TP1's uncapped
+21 % over act=2 starts to matter. The 2 %-floor variant lands within 0,7 % on every metric.

The underlying gradient is worth recording, because it settles the "too early" question in
general: over the full window act=5 makes 59 305 and act=10 makes 71 554 against act=2's 46 521 —
patience **does** buy gross return, at 2–3× the bound capital (530/760 vs. 261 average slots) and
a falling exchange rate (112/94 vs. 178 net per average slot). TP1 sits on that same gradient at
126. With one channel and 800 USD, none of it is reachable.

**The blocker that shaped the method.** `closed_ai_signals.targets` is only populated from
~June 2026 (**0 %** Mar–May, 2 % Jun, 77 % Jul, 100 % Aug — 19,4 % since 2026-03-01), because the
monitor deletes `targets` on close. A TP1 rule run over T-052's five-month window would have
fallen back to act = 2 % on four fifths of the population and reported a `trail-a2` clone under a
TP1 label. The tool no longer allows that silently: the fallback count is printed and stored in
the JSON, and `--tp1-only` restricts the *whole* sweep to covered trades so every rule scores the
same trades. The full-period run is available as an explicitly-labelled imputation
(`--tp1-impute`, per-leg median: 9 270 real, 38 282 imputed, 214 on the act=2 fallback) and is a
period-robustness check, not a second measurement of the rule.

**Side finding on the second suspicion.** The mirrored SL sits a median **7,03 %** below entry
(p90 14,87 %, max 53,34 % = −1 067 % of margin at 20×) — inherited S/R geometry from the source
legs, not a bot defect: `mirrorable_at` keeps the absolute SL on purpose. AIM2 SHORT alone
carries a third of the arm's SL damage (11 of 42 hits, −100,9 of −304,2 pp) on volume. And the
T-052 addendum-6 verdict on a −5 % SL cap turns out to be **regime-dependent**: rejected on
March–July (−33 % net, MaxDD worse), it is nearly free on July–August (net −7 %, **MaxDD −32 %**,
net-per-slot unchanged) because the sub-−5 % dippers ended at avg −4,76 % instead of −2,74 %.
Flagged for a re-run with September data, not acted on — the arm is untouched by this task.

Read-only throughout: no live intervention, no change to `40_trailing_close_bot.py`, no gate
touched. Verdict `staging_models/replay/trailing_tp1_activation_verdict_t093.md`, runs
`trailing_book_health_tp1_jul.{md,json}` / `_tp1_imputed.{md,json}`, 6 additional DB-free pins
(32 total in `backtest/test_trailing_book_health.py`).
## [2026-08-04] Bot 40 OI/liquidation entry gate: NO EDGE — study + verdict, no live change (T-2026-KYT-9050-094)

Operator question: bot 40's SL hits run 100–200% levered into the red while trail wins bank
at 20–40% — can open interest or forced liquidations gate the bad entries out? Study over the
full realized mirror book (1264 posted+filled closes, 2026-07-26 → 08-04, `tools/oi_liq_gate_study.py`):
**no.** Entry-time OI/implied-price deltas (1h/4h/24h, `oi_5m`) carry zero signal for deep
losses — AUC 0.455–0.498 pooled and within the worst leg; all 18 gate variants move a −458
book by noise (−26…+46). The deterministic fallback, an SL-distance admission cap, is actively
harmful: far stops belong to the trail *winners* (median 8.15% vs 6.18% for SL hits), so every
cap costs more trail profit than it saves. A liquidation gate is **not concludable yet** —
`liq_events` (collector 41) only started 2026-08-03; follow-up T-2026-KYT-9050-095 re-runs the
study with liq features once ≥3 weeks of overlap exist (~08-24), the script's `MIN_LIQ_DAYS`
guard lifts automatically. Key reframe for the operator: the bleed is the **pre-time-stop
launch cohort** (−620 over 409 closes, 60 of its 69 SL hits on 07-27 alone, grandfathered by
explicit decision); the post-cutoff config is **net +161.7 unlevered over 855 closes** with a
~5% SL rate. The win/loss asymmetry is the designed trail-vs-catastrophic-stop geometry
(T-052), not a defect. Verdict: `staging_models/replay/oi_liq_gate_verdict_t094.md`. No bot
code touched, no live intervention.

## [2026-08-03] SMC-sniper forming-candle guards: blind for three weeks, rewired onto the core.candles contract (T-2026-KYT-9050-083)

Five regression tests that guard hard rule 5 for `25_smc_ml_sniper.py` had been red since
commit 80d0e09 (2026-07-13). They were text-pattern guards asserting the OLD mechanism — the
in-bot slice `c_highs, c_lows = highs[:-1], lows[:-1]` and the `len(df) - 2` anchor — while
T-2026-CU-9050-111 had moved the protection one layer down into
`read_candles_with_indicators(..., include_forming=False)`. The frame no longer contains the
forming bar at all, so the slice is gone and `len(df) - 1` is the newest closed candle. **The
bot's candle behaviour was and is correct; what failed was the alarm.** Nobody noticed because
`backtest/` is touched by no CI job at all — it sits in both the ruff and the mypy excludes, and
no workflow runs pytest (green CI ≠ correct). For three weeks a real forming-candle regression
would have shipped silently. That gap is structural, outlives this fix and is now tracked
separately as **T-2026-KYT-9050-089** (a CI job for the DB-free `backtest/test_*.py`, which has
to deal with a pre-existing red tail of ~55 failures first).

The guards now assert the contract that actually holds, and the load-bearing one is no longer
a text pattern: `test_scan_market_reads_closed_candles_only` runs the real `scan_market`
against a recording reader and asserts `include_forming=False` on every candle read (a
sub-100-row frame makes the loop skip right after the read, so nothing is scored or posted).
The remaining source guards were re-pointed at the current anchors — `len(df) - 1` for the
pivot edge filter and the BB feature row, `n_closed = len(df)` for the breakout window — and
now also assert the INVERSE: a re-introduced `[:-1]` slice or a `len(df) - 2` offset would
today drop the newest CLOSED candle rather than the forming one, so both are explicitly
forbidden. The behavioural repaint fixture is unchanged.

**Review correction (T-2026-KYT-9050-088).** Both core reviews caught the same defect in the
first version of this change, and it was the very failure mode the task exists to fix: two of
the rewritten guards searched the unanchored pattern `include_forming\s*=\s*False`, and the
`scan_market` body contains that string THREE times — once as the real kwarg and twice inside
explanatory comments. Mutating only the call site therefore left both guards green. The
author's own mutation evidence hid it, because that run used
`sed 's/include_forming=False/include_forming=True/'` and rewrote the comments along with the
call. Both patterns are now anchored to the start of a line (a comment line begins with `#`
and can never match) and carry the inverse assertion against `include_forming=True`. Two more
guards were sharpened in the same pass: the raw-SELECT check gained `re.DOTALL` — without it
the multi-line triple-quoted form, which is how every query in this repo is written, slipped
straight through — plus a direct `cur.execute(`/`pd.read_sql` check, and the `n_closed`
pattern now tolerates a trailing comment while still rejecting `len(df) - 1`.

Mutation matrix, re-measured against the live source with the call site isolated from the
comments: `include_forming=False → True` **3** tests red · `last_closed len(df)-1 → -2` **2** ·
slice reintroduced **3** · BB anchor `len(df)-1 → -2` **2** · `n_closed → len(df)-1` **1** ·
multi-line raw `SELECT` injected **1**. **51 tests pass across the six SMC test files**
(previously stated as 47 across five: that count was correct for the five files listed —
`test_sniper_tag.py` was simply missing from the list). `regression_guard verify` OK (24
fixtures). No production code changed; every mutation was reverted with an md5-verified restore.

Two guards were sharpened once more after the re-review, both against false-RED classes the
first fix introduced: the raw-`SELECT` check now matches against the **comment-stripped** body,
because `re.DOTALL` alone spans the ~90 prose comment lines in `scan_market` ("we select the …"
on one line, "… from the frame" twenty lines later), and the `include_forming` anchor accepts
`,?\s*$` so the kwarg is also matched as the LAST argument of the call, without a trailing
comma. Neither could ship today — ruff-format's magic trailing comma restores the current form —
but a guard that false-reds on correct code trains people to ignore it.

One claim is deliberately NOT presented as mutation-proven: the new money-path sentinel
(`evaluate_and_trade` patched to record instead of trade) does not fire when the 100-row floor
is lowered, because two further accidents of the fixture — the synthetic frame lacks the
columns the scorer reads, and `PIVOT_WINDOW=10` finds no pivots in 10 rows — keep the path
unreachable anyway. The sentinel is a last line of defence against a future refactor, not a
demonstrated guard, and the test says so.
## [2026-08-03] Bots 16/17: transient yfinance failures silently dropped ~5% of forex/metal timeframes (T-2026-KYT-9050-084)

`16_smc_forex_metals_bot.py` and `17_mayank_bot.py` pull their forex/metal candles from Yahoo.
yfinance catches its own errors, logs `1 Failed download: ['EURUSD=X']: TypeError("'NoneType'
object is not subscriptable")` **onto the calling bot's logger** and returns an EMPTY frame —
so the bot does not crash, it silently skips that (ticker, timeframe) for the whole cycle.
Measured on the live VPS: bot 16 lost **17 of ~308 pulls across 4 scan cycles** (~77 per cycle
= 11 tickers × 7 timeframes), bot 17 lost 3; an earlier log window shows 246. Falsified as a
systematic breakage first — every interval/period pair the bots use (15m/30d, 30m/30d, 1h/60d,
1d/200d, 1wk/400d across EURUSD=X, JPY=X, GC=F, SI=F) returns data when requested on its own,
so the pattern is transient and rate-limit-shaped, consistent with two bots bursting at Yahoo.

New shared helper `core/yfinance_fetch.download_with_retry`: three attempts with a 1.5s/3.0s
backoff, treating an empty frame and a raised exception as the same signal (yfinance swallows
its own, so both mean "no data"). On final failure it returns an empty frame — the callers'
`if df.empty: return df` skip path is byte-identical to before — but logs a WARNING naming
**ticker AND timeframe**, neither of which appears in yfinance's own line. That is the actual
fix: a silent skip stops being indistinguishable from a healthy cycle.

Deliberately its own module rather than `core/market_utils.py`: that one is imported by most of
the fleet, and a module-level `import yfinance` there would let a broken yfinance install take
down bots that never touch Yahoo. The import stays at module level (a missing install must break
bots 16/17 at START, not mid-cycle), so the DB- and network-free test stubs yfinance when absent
— the repo runs two python environments and a guard that only runs in one of them is exactly the
failure mode of the companion task T-083.

**Circuit breaker + jitter (T-2026-KYT-9050-088, from both core reviews).** A plain retry has an
unpleasant feedback property: it multiplies load exactly when the cause is overload. Under a
BROAD outage — as opposed to the observed ~5% transient regime — bot 16's 77 pulls would add
~346s of pure sleep and 154 extra requests per 15-minute cycle against an endpoint that is
already refusing. So consecutive total failures are now counted, and after `CIRCUIT_TRIP_AFTER`
(5) the retry switches itself off for the rest of the cycle: one attempt per pull, no backoff,
no amplification. A single success closes it again — the endpoint is demonstrably answering —
and both bots call `reset_retry_budget()` at the top of their scan so a fresh cycle always gets
a fair chance and a recovery is never hidden. Five is above the transient rate (five failures in
a row essentially never happen by chance at 5%) and far below a full cycle, so it separates
"unlucky" from "Yahoo is down". The backoff is now jittered (×0.5–1.5, same expected wait): not
against a thundering herd — each bot is a single sequential process on an offset schedule — but
so retries do not march lockstep into the same recovery window.

Two more review findings fixed: the `max(1, attempts)` clamp covered only the loop bound, so an
exhausted pull could report *"no data after -3 attempts"* — one `budget` is now bound once and
used by the loop, the backoff guard and both log lines. And both bots' `logging.basicConfig`
lacked `%(levelname)s`, which made the new WARNING indistinguishable from an INFO line in the
log file; verified in a forced-failure run that it now reads
`SMC_BOT - WARNING - YFinance EURUSD=X (1h): no data after 3 attempt(s) …`.

**Per-cycle retry budget (second review round).** The breaker only sees a CONTIGUOUS run of
failures, so a partial outage walks straight past it: a measured 4-fail/1-ok pattern over bot
16's 77 pulls never trips it and still costs 124 extra requests and 279s of sleep — 80 % of the
unmitigated worst case. `CYCLE_RETRY_BUDGET` (40) now caps retry attempts per cycle regardless
of how the failures are distributed, and `reset_retry_budget()` refills it. 40 leaves the
observed regime untouched (~5 % of 77 pulls ≈ 8 retries) and does not bite until roughly a 25 %
failure rate. The two brakes are deliberately independent: the breaker reacts fast to an outage,
the budget bounds the shape the breaker cannot see.

25 tests, green on the fleet interpreter, the dev interpreter and standalone. Every branch
mutation-tested with an md5-verified restore: breaker never trips → 4 red · open breaker keeps
the full retry → 1 · jitter removed → 1 · clamp only on the loop bound → 2 · **cycle reset a
no-op → 10** · success does not clear the failure run → 1 · budget never exhausts → 2 · budget
counts first attempts → 7 · reset does not refill the budget → 8 · budget warning repeats → 1 ·
reset summary line silenced → 1.

Two of those numbers are corrections, both found by mutating the guards rather than trusting
them. The scattered-failure test first caught **nothing** when the success-path reset was
mutated away — it checked the breaker only at the end, where the closing success had already
masked the flapping; the assertion now runs after every failure. And the budget-warning latch
first caught nothing either, because the fixture landed on an EVEN remainder, where the warning
fires once even without the latch; the fixture now lands on an odd remainder so a single call
crosses the limit. The earlier "cycle reset a no-op → 2" was simply stale — measured before the
first of those test fixes.

Also fixed from the review: the `_rand` default (`random.uniform`) was never executed by any
test, so a typo in the jitter band would only have surfaced in production — now covered by a
20-sample band check; the reset summary line is covered; and one assertion message claimed a
monotonicity that only holds under the stubbed jitter (with real jitter consecutive sleeps
overlap and the second CAN be shorter) — reworded. Live smoke through the real call chain: bot
16 EURUSD=X 1h/4h → 1419/371 rows. `mypy` clean, `ruff` clean, `regression_guard verify` OK.
**Ops note: the running bot processes keep the old code until the fleet is restarted.**
## [2026-08-03] EPD3 SHORT go-live @0.6737 (T-2026-KYT-9050-085) — with a retracted finding, see T-2026-KYT-9050-092

Explicit operator decision: the T-033 park of the EPD3 SHORT leg is lifted, the leg posts
Cornix to `CH_PUMP_AI` under tag EPD3 at its artifact threshold 0.6737. Two changes —
`_LIFECYCLE[("EPD3","SHORT")]` SHADOW → LIVE, and a re-promotion of
`epd3_model_SHORT.pkl` from `staging_models/` to the repo root. The re-promotion is pure
provenance: the booster is bit-identical to the previous root copy (same sha256 over
`save_raw("json")`, same 16 features, same threshold), the only delta is `meta.model_id`,
which the stale root file still carried as `EPD2` while staging had the corrected `EPD3`
dump from T-057. A LIVE leg loads from root, so the wrong-tag file could not stay there
(hard rule 6).

**RETRACTED (T-2026-KYT-9050-092, same day).** This entry originally claimed as its headline
finding that EPD3 LONG's last emission *ever* was 2026-07-25 09:23:51 — two minutes before the
timestamp of the `epd3_model_LONG.pkl` promoted by T-037 (09:25) — and that bot 10 therefore
posted nothing live at all for nine days. **That is false.** The measurement was taken from
`ml_predictions_master`, which is the *shadow* logging path (`core/signal_post.py`:
`post_shadow_ai_signal` writes it, the live `post_ai_signal` does not). EPD3 LONG went LIVE
with the T-037 promotion at 09:25 and simply stopped writing to that table two minutes later.
The two-minute gap that looked like a causal smoking gun is one event — the promotion —
observed twice: once as the last shadow row, once as the file mtime.

Ground truth from the live tables and the bot log: EPD3 LONG posted every single day.
`ai_signals` inserts per day — 07-27: 10, 07-28: 60, 07-29: 43, 07-30: 65, 07-31: 33,
08-01: 35, 08-02: 61, and 70 on 08-03 by 19:55 (a running day, still climbing); the bot log
for 08-03 up to that point shows 57 LONG against 108 SHORT `placed in outbox`. No
starvation, no silent bot.

What survives is the structural observation, verified by reading the code rather than the
database: `_emit_epd3_shadow` takes `max()` across both directions on *raw* probabilities and
then checks only the winner's threshold. The two thresholds differ (0.76 vs 0.6737) and raw
scores of two different models are not comparable, so a leg above its own cut can be dropped
when the other scores higher while being below its own. **The effect size of that is
unmeasured** — the discarded events are not logged anywhere, which is why the follow-up
T-2026-KYT-9050-086 now asks for the counter *before* the behaviour change.

**Measurement rule that follows from the error:** `ml_predictions_master` only ever sees
SHADOW legs. A live leg's throughput must be read from `ai_signals` / `closed_ai_signals` or
from the bot log. Watched through the shadow table, a leg going live is indistinguishable
from a leg dying.

Risks put to the operator before implementation and reaffirmed: ~360 Cornix signals/day at
0.6737 plus 422 open shadow SHORTs going live at once, against a Cornix cap of 500 slots per
channel; the threshold cannot throttle this without leaving the deployable band (val curve
0.6266 → +0.079 %, 0.6737 → +0.088 %, 0.7001 → −0.027 %); and the shadow record (WR 81.6 %,
avg +16.3 %/stake, n=5691) is *not* evidence — it reproduces the T-009 phantom-win defect.

Two review findings are documented in-code rather than fixed: `realized_lifecycle_bucket`
buckets closed trades by the leg's *current* lifecycle, so the flip moves ~5.7k historical
shadow trades into the ACTIVE block of the 4h realised-PnL report (T-2026-KYT-9050-087); and
`KYTHERA_SHADOW_POSTING=0` silences real Cornix posting for both EPD3 legs despite their LIVE
status, keeping its shadow-era name. Refuted during review: bot 40 does **not** mirror the
422 open shadow SHORTs to a live channel — `is_rostered("EPD3","SHORT")` is False and gates
before the `is_live` check.

Verified live: bot 10 restarted 15:44:39 logging `EPD3 models loaded: LONG (live), SHORT
(live)`, first real post `EPD3 signal for HEMIUSDT SHORT placed in outbox` at 15:49:14.
Note for monitoring: `ml_predictions_master` is the *shadow* logging path, so a live leg no
longer appears there — track EPD3 SHORT throughput via `ai_signals`.

## [2026-08-03] LQE1 fix: collector streamed against a dead legacy path — routed /market/ws + silent-subscription guard (T-2026-KYT-9050-082)

The T-077 collector used `wss://fstream.binance.com/ws/!forceOrder@arr` — dead since the
Binance USDT-M WebSocket migration (System Upgrade Notice 2026-03-06; legacy `/ws` and
`/stream` stopped pushing `/market`-category channels on 2026-04-23). The failure mode is a
perfect trap: the legacy path CONNECTS, ACKs subscriptions (`LIST_SUBSCRIPTIONS` confirms
them) and then pushes nothing, forever — the collector idled 5.5 h believing the market was
calm. Diagnosed by elimination: fresh connections got zero frames even for `btcusdt@aggTrade`
and `markPrice@1s` on legacy paths while spot (`stream.binance.com`) and COIN-M (`dstream`)
flowed normally; `/market/ws/!forceOrder@arr` delivered a real liquidation within a minute.
Every other fleet WS consumer (1_data_ingestion, 19_whale_logger, chart_data_service,
99_smc_paper_bot) had already been migrated to `/market/stream` — only the new collector
regressed to the pre-migration URL.

Two changes: (1) `WS_URL` → `wss://fstream.binance.com/market/ws/!forceOrder@arr`, with the
migration pinned by a regression test; (2) **silent-subscription guard** — the incident
proved that eternal silence is indistinguishable from a dead subscription, so a stream with
no frame for `MAX_SILENCE_S` (1 h; market-wide liquidations normally arrive within minutes)
now forces a reconnect with a WARNING instead of idling forever. 14 DB-free tests total.
Ops note: the running collector process keeps the dead URL until it is restarted
(restart marker after the live checkout has pulled this merge).

## [2026-08-03] MPS3: near-band gate re-run — 10x-shell artifact confirmed, literal spread trade now refuted artifact-free (T-2026-KYT-9050-081)

Michi's objection to MPS1/MPS2 held: the tier weights {10x: 0.4, …} let the 10x shell win the
densest-cluster vote structurally — the BTC bands sat at exactly ±9.5 % (10x minus mmr), a
weighting ARTIFACT. `tools/mps1_event_study.py` now takes `--leverage-tiers`, `--tier-weights`,
`--out-prefix`, `--study-label` (defaults byte-identical to MPS1; 3 new DB-free tests), and the
gate study was re-run with the near high-leverage config {25, 50, 100} / {0.4, 0.3, 0.3} —
BTC bands land at ±3.3–3.6 %, the population MartyParty actually trades (his 25x–100x color
bands). Results (`staging_models/mps3_event_study.md`, 527 symbols, 34,763 events vs 42,327
controls — 3.6× the MPS1 event population):

* **Formal gate: EDGE on BOTH sides — but economically thin.** Up net 4h: val +0.198 %
  (t = 3.0) vs test **+0.022 % (t = 0.3)**; down: val +0.071 % / test +0.092 %, with 24h down
  NEGATIVE. The only signal with substance stays the UP side at longer horizons (24h test
  +0.400 %, t = 2.5) — consistent with MPS1's far-band finding.
* **The literal band-to-band spread trade is now refuted artifact-free:** even with reachable
  opposite bands (~2–7 % away), every (side × half × SL-tolerance) cell is net NEGATIVE
  (win rates 7–20 %, means −0.002 % … −0.27 %). The MPS1 refutation was partly
  band-width-conditional; this one is not.
* Verdict for the family stays PARKED: the near-band gate formally passes but the test-half
  4h means sit at noise level, the down side is fragile (24h negative), and everything remains
  in-sample on the same ~7-week window (T-007). The one candidate worth revisiting WITH real
  out-of-sample data is unchanged: upper-band touch → SHORT on 8–24h horizons — now measurable
  against `liq_events` ground truth as the collector history grows.

## [2026-08-03] MPS2: upper-band SHORT under house geometry — NO-DEPLOY, parked (T-2026-KYT-9050-078)

Follow-up 1 from the MPS1 gate study (T-073): the only surviving candidate — SHORT after a
touch of the upper liquidation-cluster band — backtested under OUR deployable geometry
(`tools/mps2_short_backtest.py`, K11/wick-study wiring: smart targets from the as-of trailing
30d 15m S/R frame → `ensure_min_tp_distance`(5 %) → `simulate_exit` first-touch ladder, 3 TPs,
taker fees, SL-first on ambiguity, 4d scan cap; no re-entry while the prior geometry exit is
open; event semantics imported from the gate study so the two cannot drift).

**Result (527 symbols, 5,652 trades, chrono split 07-08): NO-DEPLOY.** Val +0.18 % net
(t = 1.3), test **−0.05 %** (t = −0.26) — the pre-registered gate (both halves > 0 at
n ≥ 100) fails on the test half. TP1 win rate is a seductive 77 % on BOTH halves while
avg R sits at ~0.01–0.02: the frequent small TP1 wins are paid for by rare full-SL losses
against clusters ~10 % away — exactly the WR-over-expectancy trap Rule 8 exists for.
What is falsified is the COMBINATION drift × house geometry (5 % min-TP far above the
~0.16 % 4h drift), not the drift itself; and the run shares the gate study's window, so it
was an in-sample deployability check to begin with (T-007 lesson). Parked. Anything further
needs genuine out-of-sample weeks from the running `oi_5m`/`liq_events` collectors — the
verdict may be revisited once the window has grown materially. 8 DB-free tests pin event
detection (as-of prior-bar band, warm-up/last-bar exclusion), fold/stats shapes and every
verdict failure mode.
## [2026-08-03] The code-age canary hung the fleet restart: psutil's `name`, not `create_time` (T-2026-KYT-9050-079)

The canary shipped in T-2026-KYT-9050-071 sat in `restart_fleet.ps1`'s preflight and blocked a
marker restart for **ten minutes**. It is advisory — it must never be able to do that.

**The measured cause, after a first wrong diagnosis.** The initial suspect was `create_time`
(a handle per process on Windows). Timing the primitives on SRV02 under load says otherwise:

| Call | Result |
|---|---|
| `psutil.pids()` | 2.6 s → 361 PIDs |
| `process_iter(["pid"])` | 9.6 s → 293 processes |
| `process_iter(["pid","name"])` | **46 processes in 45 s** (aborted) |
| `process_iter(["pid","name","ppid"])` | **42 processes in 45 s** (aborted) |

The expensive attribute is **`name`** — roughly a second per process, because resolving it falls
back to opening a handle for the elevated and protected ones. Narrowing the attribute list does
not help: the name *is* the filter. Unit tests could not have caught this; they fake psutil and
measure no wall clock.

**The fix.** `tools/ops/fleet_code_age.py` drops psutil and fetches the whole process table in a
single CIM query (`Get-CimInstance Win32_Process -Filter "Name LIKE 'python%' OR Name = 'py.exe'"`),
returning PID, ParentProcessId and creation time at once — the same mechanism `restart_fleet.ps1`
already uses next door. Measured against the live box: **6.8 s** for the full verdict where the
previous version had not returned after 68 s.

Hardening around it:

* `restart_fleet.ps1` runs the canary in a `Start-Job` with `-AgeCanaryTimeoutSec` (default 60 s,
  against 16 s measured). On overrun the job is stopped, a WARN is logged and **the restart
  continues** — an advisory check never holds the critical path open again.
* A failed or timed-out query returns an empty table, which `assess()` reports as `no_fleet`
  (exit 0), never as `stale`. A failed measurement must not manufacture an alarm.
* Rows that cannot be parsed are dropped rather than guessed at.
* The creation time is handed to `ConvertTo-Json` as a double and never round-tripped through a
  string. An earlier revision of this change used `[double]::Parse(x.ToString(Invariant))` to
  "force" invariant formatting — `Parse` without an explicit culture reads the *current* one, so
  on a de-DE box `1785626975.5` parses as `17856269755` (reproduced). Every process would then
  look newer than HEAD and the canary would silently never fire again. Caught in review.

`backtest/test_fleet_code_age.py` grows to 14 tests, pinning the one-call contract, the timeout,
the empty-table-is-not-stale direction, the WQL name filter and `ConvertTo-Json`'s single-row
unwrapping.

**The fleet was untouched by ordering, not by design:** the restart markers are written before the
canary runs, so the hung preflight cost time, not a restart.

## [2026-08-03] Root cause of the detached watchdog task: the boot clock jump, not a failure (T-2026-KYT-9050-076)

The state that cost 13 hours of undeployed code on 2026-08-02 — task `State=Ready` with
`LastTaskResult=15` while a live watchdog supervises the fleet, so `Stop-ScheduledTask` grabs
nothing — is now root-caused. New doc: `docs/WATCHDOG_TASK_DETACH.md`.

**Nothing is broken.** Exit 15 is the correct propagation of a deliberate kill:

1. The box boots with a clock three hours behind; the boot trigger fires and launcher #1 starts
   watchdog #1.
2. At **05:29:44** Windows time sync corrects the clock **+3 h** (UTC `23:29:43Z` →
   `02:29:44Z`) — System log, Kernel-General event 1.
3. The forward jump makes the Task Scheduler **re-fire the boot trigger**. `svchost` PID 1748
   starts `launch_watchdog.cmd` again as cmd PID 6232, the same second as the jump.
   `MultipleInstances=IgnoreNew` does not suppress it.
4. Watchdog #2 finds the `Global\KytheraWatchdog` mutex held and runs the documented
   mutex-deadlock recovery from T-2026-CU-9050-127: `_reap_orphans` → `psutil.terminate()`.
5. **`psutil.Process.terminate()` on Windows yields exit code 15** — measured on this host
   (psutil 7.2.2), not inferred. `main_watchdog.py` itself only ever exits 0 or 1.
6. Launcher v6 propagates it faithfully, so the task records the **reaped** instance's code and
   falls back to `Ready` — while the survivor runs on, detached.

The self-healing did its job: exactly one fleet came up, no double Cornix signals (P0.2 held).
The only casualty is the Scheduler's ownership link, and with it the UAC-free stop path.

**The fix is a boot-trigger delay.** The trigger currently has none; firing at boot+2 min lets
the time service settle before the task starts, so there is no jump left to re-fire. Elevated
one-time re-registration, command in the doc — including the warning that `Set-ScheduledTask`
silently drops the Principal (T-025), so `RunLevel=Highest` must be verified afterwards.

**Deliberately not claimed:** that this happens at *every* reboot. 2026-08-02 is the only boot
since 2026-07-08, so there is exactly one observation. The mechanism is boot-specific and recurs
whenever the correction is large enough — "every reboot" is not measured. One loose end is left
open and named rather than guessed: watchdog #2's output lands in run #1's debug log and no
second debug file exists; the launcher-v5 locked-redirect failure mode is the obvious suspect but
is unproven, and nothing depends on it.

`tools/restart_fleet.ps1` now names the pattern and points at the doc when it detects the state,
instead of leaving the next operator to rediscover it.

## [2026-08-03] LQE1: forceOrder liquidation collector — ground truth for the MPS heatmap calibration (T-2026-KYT-9050-077)

Follow-up 2 from the MPS1 study (T-073): the liquidation heatmap is an estimate without
ground truth — Binance streams real forced liquidations live via the websocket
`!forceOrder@arr`, but there is NO REST endpoint for history (allForceOrders removed 2021).
Every day without a collector is history lost for good (ticker_10s/
K9 lesson). New:

* **`core/liq_events.py`** — hypertable `liq_events` following oi_5m conventions (TIMESTAMPTZ
  UTC-aware, 1-day chunks, compression after 3 days segmentby=symbol, 730-day retention,
  ON CONFLICT DO NOTHING against double delivery after reconnects). `value_usdt` = z·ap
  (executed notional, not order size); malformed events are dropped, never
  zeroed (P0.12).
* **`41_liq_collector.py`** — its own lean process (separate failure domain):
  websockets sync client, reconnect loop with backoff (Binance's 24h rotation is
  normal operation; back off immediate closes instead of hammering every second),
  batched inserts every 10 s / 500 rows, connection PER flush from the pool (P1.33), buffer cap
  10k rows with visible data loss instead of unbounded memory, kill switch
  `KYTHERA_LIQ_PERSIST=0` (idles supervised). Live smoke test: connection + parse verified
  (without DB write).
* **`core/fleet.py`** — registration (group=logger, start_delay=279, at the end of the listing —
  monotonicity regression; 247 collided with TSM1). As with K9:
  the new entry is only supervised after a **watchdog restart** — activation
  is an operator decision (Michi) and NOT part of this PR.

**Documented data contract:** Binance throttles the stream to max. ONE order per
second PER SYMBOL — `liq_events` is a SAMPLE (most underestimated in cascades),
useful for cluster localisation, NOT for volume sums. 12 DB-free tests pin the
DDL/insert contract, event parsing and the collector's buffer invariants.
## [2026-08-03] bot_regime_performance keeps a daily snapshot history (T-2026-KYT-9050-072)

`bot_regime_performance` is a snapshot: exactly one row per
`(bot, regime, alt_context, direction, window_days)`, overwritten on every analyzer run —
measured 2026-08-02, **zero** cells with more than one row. So the cell statistics the whitelist
gate decided on at the time of any past event are gone, and **no gate variant — v1, v2 or a
future one — can be checked against its own past.**

That is not an abstract gap. T-2026-KYT-9050-007 had to score today's statistics against
yesterday's traffic and could not separate the parameter effect from cell drift; its
out-of-sample run is a leakage test, the best available approximation and not the right test.
T-031 hit the same wall and closed the historical whitelist as not reconstructible.

`compute_performance` now appends the same rows it upserts to a new
`bot_regime_performance_history`, keyed by the calendar day (UTC). Several runs a day collapse
into one row per cell per day. **Thirty days from now the v2 question is answerable for the first
time with real historical gate inputs.**

Three deliberate choices:

* **The write runs in a SAVEPOINT.** This history is a measurement aid; the upsert beneath it
  feeds the live gate. A failure here — permissions, disk — must not drag the main write into the
  rollback, and without the savepoint it would, because both sit in the same transaction. A
  failure logs and returns 0; it never raises into `compute_performance`.
* **The rows are the identical tuples** the main upsert writes. One source, not a second
  computation that can drift apart from the gate it is supposed to explain.
* **No commit of its own** — the caller owns the transaction (hard rule 8).

Retention defaults to 400 days (`KYTHERA_REGIME_HISTORY_RETENTION_DAYS`), a year of hindsight
plus buffer; a shorter window would recreate the very gap this table exists to close, just later.
Volume is ~6900 rows/day (~2272 cells x 3 windows), ~2.5M/year.

The table is created idempotently on first write, so nothing has to be provisioned by hand. It is
additive: no existing table is altered, and nothing live reads it yet.

Verified: `backtest/test_regime_performance_history.py` 10/10 (DB-free), pinning the savepoint,
the no-raise contract, the day key and the retention fallback; `test_bot_regime_analyzer` 46/46
unchanged; ruff + format + mypy clean; regression guard 24/24 without refresh.

## [2026-08-02] "Merged is not live": canary + UAC-free marker restart (T-2026-KYT-9050-071)

On 2026-08-02 the live checkout sat **45 commits** behind `origin/main`, while the fleet
happily traded on the previous evening's code — including an undelivered money-path fix
(T-009, TP1 on the loss side). **Nothing sounded an alarm.** It only surfaced because
a bot had been throwing the same error for 17 days and someone happened to look at the log.

The gap forms silently: **a reboot starts the fleet without a pull.** Only `restart_fleet.ps1`
pulls — a reboot looks like a restart and is not one.

**New: `tools/ops/fleet_code_age.py`.** Compares the start time of the running fleet processes
against the HEAD commit. If a process is older than HEAD, it cannot be running HEAD. Read-only,
no DB, no elevation, and wired into `restart_fleet.ps1`'s preflight (purely advisory, the
exit code does not change).

Two refinements, both learned expensively on the same day and pinned as a test:

* **The process set is the watchdog's children**, not "Python whose parent is Python".
  The broader set includes a trainer's or a backfill's workers, and a single
  long-lived unrelated job pulls the verdict backwards — measured: a funding backfill worker from
  02:29 made a fleet restarted at 19:30 look 13h stale.
* **The watchdog is found structurally** (the Python process with the most Python children),
  never via the command line — that is unreadable for the elevated fleet from a
  non-elevated session, so a name match silently finds nothing.

And a distinction that makes the difference: **"1 of 41" is not "41 of 41".** The
one-of case is the normal case here, because `main_watchdog` starts `dashboard.py` via its own
`start_dashboard()` and not via `core.fleet.FLEET`.

**New: `restart_fleet.ps1 -MarkerRestart`.** The UAC-free path the script did not previously
know. It writes a `control/restart/<script>` marker per FLEET entry; the **running**
watchdog recycles the bot on its next cycle. Exactly the mechanism behind the dashboard button —
and the only one that works when the fleet is detached from its scheduled task
(State=Ready with live processes, the state after a reboot). Until now the script only
refused in this case; that is why 45 merged commits sat unused for 13h.

Deliberately **without `unpark`** — unlike `dashboard.restart_process`. A parked bot is
parked on purpose, and silently arming it during a code rollout is exactly the
surprise this script is meant to prevent. It then does not consume its marker
(`main_watchdog` checks `is_parked` **before** `consume_restart`), which leaves a harmless leftover
file; the script names it.

Limits, both real and named in the header as well as the log: the watchdog does not restart
**itself** (changes to `main_watchdog.py`/`core/fleet.py` still need the task), and
`dashboard.py` stays put. The abort branch for the detached state now names both
paths — the unelevated marker path and the elevated sequence to return task ownership,
watchdog first.

**Corrected:** the log line claimed `dashboard.py` is in `FLEET_SCRIPTS`. It is not.
The success criterion on port 5000 still holds (the watchdog brings the dashboard back), but
the stated reasoning was wrong — and that is exactly why `-MarkerRestart` does not cover the
dashboard.

Verified: `backtest/test_fleet_code_age.py` 10/10 (DB-free), PowerShell parses cleanly
(`[Parser]::ParseFile`), `-DryRun` run end-to-end, ruff + format + mypy clean, guard 24/24.
The canary against the live checkout currently correctly reports **"1 of 41"** — the dashboard,
which has deliberately been running on old code since the marker restart.
## [2026-08-03] Dashboard templates translated, together with their tests (T-2026-KYT-9050-075)

The 12 Jinja templates under `tools/dashboard/templates/` had been left German by the Python
sweep, so the dashboard UI was German while everything behind it was English. 14 lines
translated — empty-state messages, the freshness badge tooltip, the page title, two digest
headings.

**The point of this commit is that templates and tests move together.** The tests substring-match
the rendered HTML, so translating one side alone breaks them silently — and no AST guard applies
here, because Jinja is not Python. The only proof is running them.

The Python sweep had listed **four** coupled assertions. The full test run found **eight**:

| Test | Asserted on |
|---|---|
| `test_dashboard_coin_drilldown.py` (×3) | `"Unbekannter Coin"`, `"Noch keine entschiedenen Trades"` |
| `test_dashboard_success_rate_panel.py` (×2) | `"Keine Bots ausgewählt"`, `"keine entschiedenen Trades"` |
| `test_dashboard_regime_heatmap.py` | `"Noch keine Regime-zugeordneten"` |
| `test_dashboard_leaderboard.py` | `"keine entschiedenen Trades"` |
| `test_dashboard_shell.py` | `"keine entschiedenen Trades"` |

Four of them only surfaced in the full run, because they live in test files that a targeted run
of the "obviously affected" modules did not include. One escaped a repo-wide grep because it
writes the sentence in lower case and without the leading "Noch". A repo-wide check afterwards
confirms no assertion is left matching a translated template string.

## [2026-08-03] Python sources translated to English (T-2026-KYT-9050-075)

225 `.py` files, comments, docstrings and string literals German → English. **No behaviour
change intended, and it is proven rather than asserted.**

**Proof 1 — logic inside each file.** An AST guard parses every file before/after, collapses
string constants to a placeholder, compares f-string interpolations as a sorted multiset and
diffs the normalised trees. 224/225 identical. The single exception is documented below.

**Proof 2 — behaviour across files.** The guard checks each file alone and is therefore blind
to a test asserting on another module's message. Six such couplings surfaced. One of them took
down pytest collection entirely: `tools/dca_all_bots.py` emits `DCA-HURTS` now, while
`backtest/test_dca_all_bots.py` — a script that `sys.exit()`s on failure — still checked for
`DCA-SCHADET`. Collection aborted with `INTERNALERROR`, so 1999 tests silently became 1. Three
further couplings were already broken *before* this sweep (`test_bot_variant_archive.py`,
`test_atomic_write_json.py`, `test_retrain_model_id.py` asserted German strings whose producers
had been English for a while) and are repaired here.

**Proof 3 — completeness.** Progress was first measured by counting umlauts, which misses
"nur die letzte Zeile". A word-based detector found 57 further German comment/string tokens in
14 files. Both detectors now run in the verification pass.

Test result against a baseline measured on the pre-sweep commit **in a worktree with the same
`.env` visibility**: 55 failures before, 54 after, **0 newly caused**, 1 previously-red test now
green. `ruff check` and `ruff format` match the baseline exactly (the sweep had made 10 files
fail `format`; fixed).

Deliberately not done: the Jinja templates under `tools/dashboard/templates/` are still German,
so four dashboard test assertions were kept German to match their producer — translating those
templates is a separate pass, and those assertions must move with it. `dashboard.py` had its log
classifier changed from `'kritisch'` to `'critical'`: all eight modules that emitted the German
word are now English, so the check had become dead code and critical lines would have stopped
being highlighted. The test name `test_missing_leg_stats_are_not_bewertbar` keeps its German word
— renaming a test is a code change, not a translation.

Single guard exception: `db_schema_analysis.py:505`, ASCII banner padding `" " * 38` → `* 37`,
because "ANALYSE" → "ANALYSIS" is one character longer and the box is a fixed 80 columns. Rendered
widths measured before/after: `[80, 80, 80, 80]` both times. An intermediate agent version had
`* 36` and produced a misaligned `[80, 79, 80, 80]`.

## [2026-08-02] English-only policy for the whole repository (T-2026-KYT-9050-075)

`CLAUDE.md` gains hard rule 10: everything written into this repository is English — code,
identifiers, comments, docstrings, log messages, exception texts, Telegram/HTML output, commit
messages, PR bodies, Markdown docs and this changelog. The chat with Claude stays German; that
is the only exception and it never lands in a file. `CLAUDE.md` itself was translated in the
same commit, and the post-merge instruction "add a `CHANGELOG.md` entry (German, like the
existing ones)" now reads English. This entry is the first one under the new rule; the existing
German entries stay untouched for now and are part of the follow-up sweep.

No behaviour change. The inventory that motivates the sweep: ~5,000 German hits across 224 `.py`
files (~2,410 of them pure comment lines, ~1,171 lines with German inside string literals or
docstrings across 191 files), plus ~6,470 lines across 128 Markdown files, of which
`CHANGELOG.md` alone carries 2,541. Two findings bound the risk of the follow-up: a grep for
German literals in comparisons (`==`, `!=`, `in`, `startswith`, `endswith`, `.get`) returns
**zero** hits — no program logic hangs off a German string — and only two German strings sit
inside Telegram HTML (`10_pump_dump_detector.py:932/934`), so hard rule 4 has a very small
contact surface. Model artifact metadata (`model_archive/**/manifest.json`, `*_meta.json`) is
provenance and stays untouched.

## [2026-08-02] Staging-MAX1 removed: it was not a candidate but the retired live generation (T-2026-KYT-9050-017)

`staging_models/max1_model_SHORT.pkl` + `_meta.json` are gone. No code, no live intervention —
the root artifacts (= live) stay untouched.

**The ticket had attributed the numbers to the wrong file.** It stated that the *staging* MAX1
carried `n_train=15012` and was therefore built against an old RUB2 generation. Measured, it is
the other way round — and the git history makes it unambiguous:

| Time | File | n_train | Threshold |
|---|---|---:|---:|
| 07-07 `07c8874` | RUB2-SHORT deploy | 31894 | 0.829 |
| 07-11 `eafc0c2` | MAX1 root promote | 31894 | 0.829 |
| **07-20 `14e1c6f`** | **both root files** | **15012** | **0.7929** |
| 07-20 `14e1c6f` | staging MAX1 created | 31894 | 0.829 |

So the staging artifact is the **previous live generation**, set aside during the sync on 20.07. —
not a challenger. Live MAX1 and live RUB2 are mutually consistent
(both 15012/0.7929), as `tools/make_max1_artifact.py` requires (it copies RUB2-SHORT
**verbatim** and only changes the identity fields).

**Why removed instead of regenerated or kept** — the three ticket options, worked through:

* *Regenerating against live RUB2* turns the staging file into a byte duplicate of
  live MAX1. A candidate identical to the incumbent is not one.
* *Co-committing the old `rub2_model_SHORT.pkl` for provenance* is already satisfied: both
  forms of this generation are in the history (`07c8874` as RUB2, `eafc0c2` as MAX1). A
  second copy in `staging_models/` adds nothing.
* Leaves: **remove.** `staging_models/` is by convention the set of promotion *candidates*,
  and the tooling reads it that way (`verify_staging_artifacts.py`,
  bot variant index). A retired live generation in this slot claims a candidacy
  that does not exist. Nothing is lost — the state is recoverable at any time via the
  commits named above.

**Not touched, but named:** the sync on 20.07. switched live RUB2/MAX1 to a generation
with a **smaller** training set (15012 instead of 31894) and worse val expectation (mean net 0.169
instead of 0.248%) — on the test window it is the other way round (Σ 565 instead of 432%). The
commit does not give a reason for the switch. That is a live-model question and an operator
decision, not clean-up; noted here only.

**Not a defect, though it looks like one:** `.env` carries `MAX1_MIN_PROB=0.829` — the threshold of
the old generation. Per `make_max1_artifact.py`, MIN_PROB is deliberately a throttle value
independent of the artifact ("so Michi can adjust it without regenerating anything"), and
0.829 > 0.7929 makes MAX1 more selective, not inconsistent.

Verified: `tools/promotion_guard.py` unchanged 1 WARN / 0 FAIL (RUB3/LONG, pre-existing);
`verify_staging_artifacts.py` no longer mentions MAX1 and ends with the same exit code as on
`main` (1, due to missing `meta.model_id` in the `td_`/`bb_` staging artifacts — pre-existing,
cross-checked).

## [2026-08-02] Z2 struck: no Cloudflare tunnel, the dashboard stays loopback-only (T-2026-KYT-9050-074)

No code. Operator decision: **"I only ever see the dashboard via RDP anyway."** That makes
audit point **Z2** (cloudflared + Cloudflare Access) moot and answers the last open
dashboard question from T-2026-KYT-9050-009 and -056 — **D1** from the
option matrix in `docs/DASHBOARD_SECURITY.md` §4 applies.

**Nothing to do.** The loopback default from PR #237 is already the desired state;
neither `KYTHERA_DASHBOARD_HOST` nor `KYTHERA_DASHBOARD_TOKEN` belong in `.env`. From the
next start of `dashboard.py`, the UI is reachable only from an RDP session on the box.

**Why struck and not deferred:** without a remote-access need, the tunnel buys nothing and
enlarges the attack surface compared to "not reachable at all" — an access policy as an additional
failure path (a known failure mode for zero-trust setups), TLS termination at the provider, one
more service on the box. The runbook in §5 stays as reference and is not executed.

**Only trigger for revisiting:** the Z1 quick actions (F4). For live leverage in the web UI
an auth layer stays a precondition; if F4 does not land, Z2 does not either.

To distinguish from **T-2026-KYT-9050-070** (Symantec firewall rules, its own wontfix): that
decision leaves 135/445/3389/5985 open, this one only concerns the dashboard's remote-access
path. The loopback bind acts independently of that — it takes port 5000 off the network,
regardless of what the firewall allows.

Implementation note: `dashboard.py` is **not** in `core/fleet.py` (its own scheduled task), so the
marker-based fleet restart on 02.08. did not cover it. The hardening takes effect on the
next start of this process.

## [2026-08-02] Persisted ≠ traded: ROM1 and AIM2 were undercounted in every realized report (T-2026-KYT-9050-012)

The realized position model splits the stake into `n` equal legs, with `n` = the number of
targets **persisted** in `ai_signals`. That is only correct as long as an emitter stores exactly
what it posts to Cornix. **Two do not** — and they are the two with the most
volume: `28_signal_orchestrator` (ROM1) persists `t_cands[:20]` and posts 3;
`15_ai_master_bot` (AIM2) persists the full list and posts 3. Both write their **own**
`INSERT INTO ai_signals` and were therefore not caught by the P2.31 fix (2026-07-11, bots 9/11/12/13
plus `post_ai_signal`).

**Measured over 30 days:** ROM1 persists the full 20 targets in 199 of 250 signals. Over 7,774
closed trades the model calculates on ~20 instead of 3 legs and **underestimates ROM1 by a factor
of 1.41** — Σ 17,700 → 24,955%, mean 2.277 → 3.210%/trade, median 1.51 instead of 5.18%. AIM2: factor
1.05 over 2,350 trades. In addition **139 of 7,774** ROM1 trades had `targets_hit > 3`, i.e.
credit for TPs Cornix never received.

**Operator decision: fix only the measurement.** The bots stay untouched — trimming the
persisted list would change monitor 8's scoring semantics for **open** trades
(SL trailing and the ALL-TARGETS close condition would see 3 instead of 20 stages). No
restart needed, no money path touched.

Implemented as **one** source: `core/realized_pnl.PUBLISHED_TARGET_COUNT` + `traded_targets()`.
`weighted_move_pct`, `realized_pnl_pct` and `unlev_move` optionally accept the model and
cut **before** the leg count — `n` *is* the position model, so the cut must sit
before it; `targets_hit` gets capped automatically along with it via the existing `min(k, n)`
cap. **Byte-identical to before without `model`**, no caller changes silently.

Followed up: `23_market_tracker` (T-115 report), `tools/fleet_realized_audit`,
`tools/whitelist_v2_realized_eval`. On the last one the fix almost became a silent no-op:
its leg dict only carried `pretty_name(model)`, not the raw DB tag the lookup goes against —
the tag is now carried along as well.

**Impact:** all ROM1/AIM2 numbers in reports and roster decisions were too small. This
retroactively affects today's T-007 verdict too — but there sign-neutrally, because both
gate sides are affected equally.

**Correction to the ticket:** T-012 was rated `low` with the reasoning "ROM1 sync dead since 04.07,
path dormant." ROM1 is highly active (250 signals in 7 days, most recent during the measurement). The
second ticket premise — "ROM1-specific" — held up, though: an initial measurement across all
models assumed `n=3` for every emitter and produced spectacular numbers including a
sign flip for BR2H. Checked in code, `7_pattern_detector` and
`25_smc_ml_sniper` post `n_show=len(targets)` and `11_ai_mis` five — there is no gap there, the
numbers were a measurement artefact.

Verified: `backtest/test_traded_targets.py` 13/13 (DB-free), 83 tests in neighbouring suites
unchanged green, ruff + format + mypy clean, regression guard 24/24 without refresh. The one
failure (`test_fleet_realized_audit::test_lifecycle_bucket`, `shadow` vs `inactive`) is
**pre-existing** — cross-checked against `main`, identical there. All measurements read-only.

## [2026-08-02] whitelist_v2 recalibrated: no parametrisation survives out-of-sample — Stop-B, v1 stays (T-2026-KYT-9050-007)

The task was to **recalibrate** the v2 gate rather than flip it or leave it: change the Wilson
bound and break-even threshold and re-measure against the realized forwards. New read-only
tool `tools/whitelist_v2_recalibration.py`, verdict in
`docs/T-2026-KYT-9050-007-whitelist-v2-recalibration.md`. **No flip, no restart, no
write query.**

**The proposed lever is the wrong one.** Across 45 configurations, the break-even moves the
opening rate by **1.7pp**, the shrinkage strength by **1.3pp** — the lower bound's z-multiplier,
by contrast, by **10 to 47pp**. v2 is not an expectancy gate with a threshold,
it is a confidence gate; the confidence *is* the gate. And even at the most permissive end
v2 opens only ~53% of cells against v1's 94%: the −55% throughput loss from PR #239 is
structural, not a tuning artefact.

**In-sample, one region looked excellent** (window 07-11 → 08-02, 8,367 legs, v1 reference
mean +0.033%/trade): `z 0,67 / k 10 / be 0,1` doubles throughput to 13.9%, raises the
retained expectancy to **mean +0.558%** and removes traffic with **mean −0.076%**. Read as
a backtest that would be a hit.

**Out-of-sample it inverts completely.** Window 04-18 → 07-03, i.e. **before** the
30-day fit window of the cell statistics, 4,356 legs, 99.9% leg coverage, v1 reference
mean +0.689%/trade: **42 of 45 configurations remove winners.** The mean of the
blocked legs sits at **+0.55 to +0.60%/trade** across the whole grid — v2 would have cut
around **80% of the realized ROM1 profit** in every parametrisation and kept 3–6% of
the volume. The same cell `z 0,67 / be 0,1` goes from "removes losers" to
**"removes WINNERS"** (mean blocked +0.594). The three exceptions keep 95% of the traffic —
a gate that does not gate.

**Why it looked so good in-sample:** a selection effect. The cell statistics come from the last
30 days, the in-sample scored legs sit in exactly that window. A cell passes
because its most recent trades did well — and those same trades then get counted as evidence.
The same error class PR #239 named on the trigger leg, here via the detour of
parametrisation.

**New compared to PR #239:** this out-of-sample run was impossible there. The flip evaluation
needs `orchestrator_open_trades.wl_reason`, which has only been populated since early July. This
tool decides the cell **fresh** from `bot_regime_performance` and does not need `wl_reason`
— which makes 4,359 forwards from April to early July evaluable. The "zero
out-of-sample" gap of the original report is closed, with a negative finding.

**Limit, measured on every run rather than claimed:** `bot_regime_performance` is a snapshot
(**0** cells with more than one row). Both runs use today's cell statistics and
differ only in whether the scored trades sit inside the fit window. For a
leakage test that is the right split, for a rollout justification it is not enough.
Follow-up task **T-2026-KYT-9050-072** (historization) makes the question honestly
answerable for the first time in 30 days.

**Side finding, fixed along the way:** `wait_for_cpu_headroom` in `tools/whitelist_v2_realized_eval.py`
aborted on the `--force-on-busy` path with `UnicodeEncodeError` — an emoji on the only
line of this path, on a cp1252 console. The documented emergency-exit option died exactly
when the operator had requested it. Now ASCII.

Verified: `backtest/test_whitelist_v2_recalibration.py` 9/9 (DB-free),
`test_whitelist_v2_realized_eval` + `test_whitelist_v2_flip_eval` 47/47 unchanged green, ruff +
format clean, mypy on the new file **0** errors (the 47 in imported modules are
pre-existing, baseline on `main` identical), regression guard 24/24 without refresh. Both runs
read-only against the live DB (`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`),
BELOW_NORMAL, `TELEGRAM_BOT_TOKEN` deliberately set to an invalid placeholder.
## [2026-08-02] MPS1: liquidation heatmap + max-pain event study — spread strategy refuted, short drift after upper-band touch survives (T-2026-KYT-9050-073)

Testing MartyParty's thesis "trade the spread between 24hr Max Pains" (futures max pain =
liquidation cluster) on our own data. New: `tools/mps1_liq_heatmap.py` — a DB-free
Coinglass-like estimator of liquidation levels from 5m OI deltas (`oi_5m`, K9) × leverage tiers
{10, 25, 50, 100}: seed/consume/decay/expire, 24h window, band = densest cluster above/below
spot. 13 DB-free tests pin the seed math, consumption, decay, expiry, OI alignment and
truncation invariance (no look-ahead). Built performance-aware: incremental histogram
on an absolute log-bin grid + vectorised band post-pass instead of per-bar histogram (~2.3s
instead of ~16s per symbol — the driver was numpy call overhead, not arithmetic).

**Event study as edge gate** (`tools/mps1_event_study.py`, read-only, BELOW_NORMAL, verdict
pre-registered): 527 symbols, 12.06.–02.08., 9,754 band-touch events against 50,567 controls
(fresh 24h extremes with no nearby cluster), chrono split on 08.07. Result
(`staging_models/mps1_event_study.md`):

* **Gate formally EDGE (up):** short reversion after touching the UPPER band is net positive on
  val (+0.14%) AND test (+0.16%, 4h) and beats the control; on 24h the test effect grows
  to +1.04% net (t=2.7). The t-values at the 4h gate horizon are weak though (~0.8).
* **Down side dead:** no long bounce at the lower band, clearly negative on 24h (−0.6…−0.8%) —
  consistent with the fleet finding "the edge is direction-dependent" (short side).
* **The literal spread strategy is refuted:** entry at the band, TP at the opposite band, SL
  tolerance 0.5–2% → win rate 3–8%, mean net PnL negative in EVERY cell. The opposite band
  (~±10%) is practically never reached before the SL.
* The density thesis (denser cluster = stronger magnet) does not hold (sign flips val/test).

Honest limits: ~7 weeks of OI history = ONE regime (T-007 lesson), the leverage mix is an
assumption, survivorship (coins.json). Consequence: NO bot, no deploy — the only candidate for a
follow-up backtest is the short side (touch upper band → short, 4–24h horizon) with
our geometry. Side finding with repeat risk: `astype("int64")/1e9` on
`datetime64[us]` frames (pandas 3.x/Py 3.14) silently produced kiloseconds and put ALL events
in the val half on the first run — `core.time.epoch_seconds` exists for exactly this and is
now wired in.

## [2026-08-02] C-Gate follow-up: the last two raw SELECTs rewired, legacy backend gets a staleness guard (T-2026-KYT-9050-068)

`16_smc_forex_metals_bot.py` and `21_btc_smc_strategy.py` were the only remaining
live spots building raw SQL against the per-coin tables (`SELECT ... FROM "{symbol}_{tf}"`),
bypassing `core.candles`. That was viable as long as the per-coin tables were
authoritative. Since the write-primary cutover on 2026-07-16 nobody writes them anymore —
both bots spent 17 days reading a frame frozen at `open_time 2026-07-16 16:00 UTC` and
stayed silent. Empty input produced **no** output rather than wrong output: luck, not design.

**Both now read via `read_candles(..., include_forming=False)`.** Michi's decision from
02.08.: arm them **live** again, do not park them — from the next fleet restart they post
again.

**The trap in the rewiring.** Both discarded the newest row via `.iloc[:-1]`, because their raw
SELECT brought in the running candle. `include_forming=False` already filters that in the query —
so the drop had to **go**, otherwise it would have discarded the newest **closed** candle and
delayed every signal by one candle. In bot 16 the same drop sat at the **shared**
call site of both sources and had to be kept for the yfinance path, which still delivers its
forming candle. Exactly the kind of simplification hard rule 5 forbids.

**Correction to the T-002 finding:** bot 16 was only half affected. Only the METALS group
(`source="database"`, eight Binance symbols) reads the DB; the FOREX group fetches live via
`yfinance` and never touched the per-coin tables. The table `EURUSD=X_1h` (frozen
since 2026-02-25) is unused legacy data, `GC=F_1h` does not exist at all. A forex ingester,
briefly considered, would have been work for a problem that does not exist.

**Side finding, not fixed:** `XAUUSDT` and `XAGUSDT` also end in the hypertable on
2026-07-06 — two of the eight METALS pairs remain without fresh data, independent of the C-Gate.

**The loaded weapon is defused.** `KYTHERA_CANDLES_SOURCE=legacy` was considered a trivial
rollback; since the cutover, flipping it back would have **silently** put the fleet on 17-day-old
candles. New staleness guard in `core/candles.py`: on a process's first legacy read it
measures how old the newest candle in the backend actually is, and above the threshold
(`KYTHERA_CANDLES_LEGACY_MAX_AGE_MIN`, default 180 min) the read aborts with `CandleSourceError`
instead of delivering frozen prices. One probe per process, then cached; hyper reads
pay nothing. Two deliberate decisions: an **unmeasurable** probe (table missing,
permissions, test fixture) does NOT count as staleness — it warns and steps back; and
`latest_open_time` stays unprotected, because that is the freshness query itself.

**Found by its own test:** the guard initially also cached the **negative** verdict and then
silently returned it — first read blocked, every following one served dead data again, i.e.
exactly the bug it is meant to prevent. Only surfaces when the test files are run
**together**. A `stale` verdict now raises on every subsequent call as well.

Verified: `backtest/test_c_gate_bot_readers.py` 15/15 (DB-free), ruff + format + mypy
(`--python-version 3.12`) clean, regression guard 24/24 without refresh. The failures in
`test_candles_db_parity.py` when run together are **pre-existing** and not from this
change: an unchanged repo test file (`test_atb1_posted_flag.py`) produces even more of them in the
same combination — the cause is the repo-wide `os.environ.setdefault("DB_PASSWORD",
"test")` in bot-loading tests plus the missing `.env` in the worktree, not the guard.
**Only effective after a fleet restart.**

## [2026-08-02] Symantec firewall rules: exposure measured, operator decision WONTFIX (T-2026-KYT-9050-070)

No code. Closes the point the correction to P0.8 had raised: two
Symantec rules (`SMC Service`, `SNAC Service` — Enabled/Inbound/**Allow**/Public,
`LocalPort: Any`, `RemoteIP: Any`) allow inbound TCP on any port from any IP;
`DefaultInboundAction` is `NotConfigured` on all three profiles. The box hangs off a
public IP directly on the interface, no NAT.

**Measured (read-only, reconfirmed after the reboot on 02.08.):** exposed listeners
135/445/3389/5000/5432/5985; `logs/dashboard.log` counts **537** answered
`GET / HTTP` requests since 04.07., including live third-party access and scans (`/.env.production`,
`/v404/exec?jwt=…`); on 02.08. up to nine simultaneous SMB connections from a foreign
IP at times. **Not verifiable** from the agent session: whether these SMB connections were
authenticated — `Get-SmbSession` runs unelevated into "Access is denied", the security log yields
nothing for 4624/4625. What is documented is that someone held the sockets, not that a logon
succeeded.

**Michi's decision: the rules stay, risk accepted.** No open action item left.
Two things act as a limit independent of the firewall: `pg_hba.conf` only knows
`127.0.0.1/32`, `::1/128` and the local socket → the live trading DB rejects outsiders; and the
loopback bind from T-056 takes the unauthenticated `stop_all` off the network on the next
dashboard start. Revisit only on a foreign hit against a control endpoint, a
`pg_hba` line for an external IP, or a Symantec uninstall.

**Side finding that follows from the same correction:** the claim "the bind change is
behaviour-neutral" in `docs/DASHBOARD_SECURITY.md` rested on the refuted firewall assumption
and is withdrawn. The loopback bind cuts a real, existing access path — after the
next dashboard start the UI is reachable only from an RDP session. Remote access
then needs `KYTHERA_DASHBOARD_HOST` **plus** `KYTHERA_DASHBOARD_TOKEN` (without a token the
fail-closed policy refuses to start) or the tunnel from Z2.

## [2026-08-02] Dashboard hardening: loopback bind, host allowlist, CSRF guard, fail-closed start policy (T-2026-KYT-9050-056)

P0.8 was "dashboard without auth on `0.0.0.0`". The measurement on the box confirms the bind
(`0.0.0.0:5000`, PID 100120). Full measurement table and evidence commands:
`docs/DASHBOARD_SECURITY.md`.

> **Correction (2026-08-02, added at merge time).** This entry originally claimed that
> **the port is not externally reachable** — Windows Firewall active on all three profiles,
> inbound default `Block`, no allow rule for TCP 5000. **That is refuted.**
> `DefaultInboundAction` is `NotConfigured` on all three profiles, and two
> Symantec rules (`SMC Service`, `SNAC Service` — Enabled/Inbound/Allow/Public) allow
> inbound TCP on **any** port from **any** IP. `logs/dashboard.log` counts
> **557 successful `GET / → 200` to foreign IPs** since 04.07. The session had correctly
> flagged its own check method as unfit (Windows treats a connection from the box to its own
> public IP as loopback) and then trusted the configuration over the empirical evidence
> anyway — **a failed measurement is not a negative finding.** The code section below
> stands unchanged and becomes more urgent through the correction, not moot.

**The actual finding: two attacks worked despite the firewall.** A form POST or
`fetch(…, {mode:'no-cors'})` from any page in a browser **on** the VPS reaches
`POST /api/system/stop_all` without a preflight — the response is opaque, but the side effect
(persistent, reboot-proof park markers) still happens. And DNS rebinding reaches the same
endpoints, against which a plain same-origin comparison does nothing, because the attacker's
`Host` and `Origin` match. Both are independent of the exposure question and were open.

**New `core/dashboard_security.py`** — three O(1) checks per request, no DB, no
process scan: host allowlist (against rebinding), an optional constant-time token
(`KYTHERA_DASHBOARD_TOKEN`, header/cookie/one-time `?token=`), an origin check on
state-changing methods (against CSRF; a missing `Origin` stays allowed so
curl/PowerShell keep working). Bind default `0.0.0.0` → `127.0.0.1`. Control endpoints
validate the script name against `SCRIPT_MAP` (404 instead of a marker file for unknown names,
`audit_reports/10` [LOW]).

**Fail-closed start policy:** the process does not start if it is bound to non-loopback
**or** an off-box hostname is allowlisted, as long as no token is set. The second branch
is the tunnel case — `cloudflared` connects to `127.0.0.1:5000`, so the bind address stays
harmless while the dashboard would be reachable worldwide. Exposure can therefore no longer
silently reproduce the P0.8 state.

**No live intervention:** no restart, no port, no firewall rule, no `cloudflared`, no
`.env` change. The running dashboard process is untouched; the fix takes effect on the next
dashboard start (watchdog crash-restart or reboot — without operator action). The
bind change demonstrably does not cut any existing access path (off-box access is not
possible today; the `restart_fleet.ps1` success probe runs over `localhost` and already
falls back to IPv4 today). Verification: 58 new tests in `backtest/test_dashboard_security.py`,
`guard.py verify` 24/24 without refresh, ruff/format/mypy clean in CI form (the one mypy
finding in `1_data_ingestion.py:19` is pre-existing, an optional `orjson` import).

**Open and deliberately not built — Michi's decision:** `cloudflared` + Cloudflare Access (Z2/B4,
precondition of the Z1 quick actions F4). The three options with quantified residual risk are in
`docs/DASHBOARD_SECURITY.md` §4. Short version: the dashboard is secured after this PR even
without a tunnel; the tunnel brings convenience and the F4 precondition, but increases the
attack surface net. Recommendation: bring the token (`.env`, Michi gate) along at the
next dashboard restart, decide the tunnel separately.

**Side finding, record correction:** the task premise "the dashboard is the single biggest
DB load contributor" comes from T-2026-CU-9050-166 and was already corrected by T-2026-CU-9050-179
(the expensive `candles ⋈ indicators` join is the AI-bot feature loading path).
`dashboard.py` does not import any DB code at all and issues zero queries — its load is
CPU (psutil sweeps, P1.38, still open).
## [2026-08-02] whitelist_v2 flip measured against real forwards: no PnL evidence, −55% throughput, zero out-of-sample (T-2026-KYT-9050-007)

The task was to make the v1→v2 flip of the whitelist gate decidable — against the
**realized** trades, not a replay of the rule. New read-only VPS tool
`tools/whitelist_v2_realized_eval.py` + decision template
`docs/T-2026-KYT-9050-007-whitelist-v2-flip-decision.md`. **No flip, no restart, no
write query** — the gate still reads `whitelisted` unchanged (Michi's decision, OPUS-HANDOFF §6).

**The tool only swaps the scoring layer.** Gate semantics and divergence classes are
imported from `tools/whitelist_v2_flip_eval.py` (T-069), the realized math from
`core/realized_pnl.py` + `tools/fleet_realized_audit.py` (T-115/T-032) — one source of truth, not
a rebuild. What's new is the yardstick: instead of a counterfactual replay, the **actually closed,
monitor-scored trade**. Two legs, deliberately kept separate: the **trigger leg** (the source
bot's own trade, exists on BOTH gate sides → symmetric) and the **ROM1 leg** (the real
money, structurally only on the forwarded side).

**Volume picture (window 2026-07-11 → 08-01, 22,660 gate events, 14,234 cell-decided).**
v2 would **additionally block 4,848 signals** and **additionally pass 264**; gate rate
36.28% → 4.07%, ROM1 forwards/day **377 → 168 (−55%)**. At cell level v2 blocks **1,395 of
1,590 cells (87.7%)** and opens **three** (AIM2/TREND_UP/ALT_NEUTRAL/SHORT,
QM_4H/HIGH_VOLA/ALT_WEAK/LONG, SRA2/CHOP/ALT_NEUTRAL/SHORT).

**The 89% default-open premise is correct — but does not describe the traffic.** 1,410 of
1,590 cells (88.7%) carry `insufficient_data`, as reported in step 6. On the traffic it is
the other way round: **81.8% of the additionally blocked events (3,964 of 4,848) came via the
merit path `wr_above_overall`**, only 18.2% via the crutch. The flip does not clean up empty
cells, it overrides decisions v1 made on a data basis.

**The finding flips on the money leg.** On the trigger leg v2 looks good (clean subset:
3,160 decided trades, Σ −274.9%, mean −0.187%/trade net) — **but this is exactly the leg
v2 was fit on**: `27_bot_regime_analyzer` builds `bot_regime_performance` from the
trigger trades of the last 30 days, and `_v2_whitelist_decision` decides purely from their
`avg_pnl_pct`/`pnl_stddev`. On the ROM1 leg — the same signals, but the geometry that was
actually traded — **nothing** survives: **Σ +2.0% over 21 days** on 1,342
decided trades and **Σ −61.6% over 7 days**. Sign unstable, magnitude noise. That is
P1.10 in numbers. The "v2 opens things up" side hangs on **one leg** (AIM2-SHORT; still
7 decided trades in the 7-day window) and is **in principle** not measurable in ROM1 money —
these signals were never traded.

**Out-of-sample there is not a single solid data point.** The run ending before the fit
window (05-15 → 07-02) contains **0** `v2_would_block` events: `orchestrator_open_trades.wl_reason`
is only populated from early July onward (B8), the entire forwarded side of that era carries NULL.
The one divergent class present there (190 events, exclusively EPD1-SHORT) is 100%
drift-contaminated. **The T-031 finding "historical whitelist not reconstructible" is re-
checked and confirmed** — both tables are UPSERT-only without history, and bot 28 logs only
the v1 path per signal. The concrete conclusion: a flip would be switchable, but not cleanly
evaluable in retrospect. The cheapest remedy is an additive log column in
`get_whitelist_decision` (option C in the template) — **not built**, because money path + restart.

**Three measurement traps, each moved a number in testing.** (1) **Two time domains in the
same column:** orchestrator tables and ROM1 rows carry UTC, the bots' own rows in
`closed_ai_signals`/`closed_trades_master` still carry `Europe/Bucharest` wall-clock (+3h) — a
join that ignores this matches **0.0%**; the tool decides the reading per day from the data
and reports both hit counts. (2) **Drift contaminates the class, not just the accuracy:**
where today's v1 cell no longer matches the recorded decision, the "divergence" compares two
v1 states — reported separately as `v1_agree`/`v1_drifted`; the drift-contaminated half carried
79% of the naive headline number. (3) **`closed_trades_master` is NOT the realized source for
ROM1** (0 rows there, measured) — ROM1 and all AI bots live in
`closed_ai_signals`, deduplicated via the report-14 survivor key.

Side finding (its own lever, nothing to do with v1-vs-v2): **60% of the ROM1 legs are
censored** — 6,500 `CLOSED_REGIME_CHANGE` against 4,421 lifecycle-closed trades in 60 days.
Only 40% of the forwarded trades reach an evaluable outcome at all.

Verification: `backtest/test_whitelist_v2_realized_eval.py` 25/25 (DB-free, standalone),
`test_whitelist_v2_flip_eval.py` 22/22 unchanged green, `ruff check .` + `ruff format --check .`
clean in CI form, pre-commit incl. gitleaks and `guard.py verify` green. The three runs ran
under job lock at measured 72.7/90.4/96.9% system CPU (`--force-on-busy`, BELOW_NORMAL,
read-only); reports in `staging_models/replay/whitelist_v2_realized_eval_*.md`.
## [2026-08-02] VPS audit remainder re-measured: P0.7 has a second door, the dashboard hangs open on the network, Z0 measures itself (T-2026-KYT-9050-009)

The task was the remaining chain of the VPS orchestration (jobs 7/8/10/11 + doc PR). Session
rule: every point is re-measured against today's code and today's live environment before it
is worked on. Five of eight points turned against their paper trail — in both directions.
Full report with all the numbers: `docs/T-2026-KYT-9050-009-vps-audit-rest.md`. Read-only against
the live DB, no restart, no gate flip, no deploy.

### Fixed
- **`strategies/strat_support_resistance.py` + `strat_main_channel.py` — P0.7 had a second door,
  and it stood open.** The DB remainder of the finding is done (0 rows with the P0.7 signature in
  `active_trades_master`, the last in the archive 2026-05-27, i.e. before the fix from 04.07.) —
  but the error class keeps producing: **342 of 3,463 support-resistance and 12 of 188
  main-channel trades since 01.07. went out with TP1 on the wrong side of the entry**, the newest
  on 2026-08-01 23:33, one was sitting active in the book. Cause: `find_support_resistance_zones`
  filters its zones against the close of the last closed candle, but the target ladder is built
  against `entry = live_price`. If the live price runs past a resistance zone,
  `sorted(zones, key=|zone−entry|)` picks exactly that one as TP1, and the interpolation
  `x = (t1−entry)/4` goes negative and drags TP2/TP3 along with it — the same failure shape as
  P0.7, just through a different door. The guard `if t1 == 0` built on 2026-07-04 covers only
  "no zones at all". That the three non-zone-based strategies (5 Percent, Fast In And Out, Volume
  Indicator; 17,792 trades combined) have **zero** cases is exactly the fingerprint of the cause.
  Not just geometry: 96.5% of these trades closed with `status ≥ 1` ("TP hit") against 66.2%
  of the clean ones — a TP on the loss side gets "hit" on the very first move against the position.
  These phantom hits sit in the per-bot statistics the orchestrator gating decides on.
  Fix: new shared helper `core.market_utils.select_zone_targets(zones, entry, direction)`
  filters against the same price the ladder is built against (both strategies × both directions,
  4 spots); the ladder is now also monotonic in the trade direction. Measured rollout impact:
  support resistance loses 1.1% of its signals entirely and corrects 10.1% of its ladders, main
  channel 2.1% / 6.4% — the dropped ones are exactly those whose TP1 sat on the loss side.
  Side effect that comes with it: "support resistance"'s hit rate will **fall** after the rollout.
  **Only effective after a fleet restart — Michi's decision.**
  Test: `backtest/test_zone_target_side.py` (8 cases, DB-free, incl. LABUSDT live regression).
- **`tools/restart_fleet.ps1` — "Pull failed" on a successful pull.** Documented twice
  (`logs/fleet_restart_20260726_232251.log`, `_20260801_192843.log`): `ERROR - Pull failed: From
  https://github.com/ziagl888/Kythera`, followed by "Fleet untouched" and exit 1 — even though the
  pull went through (HEAD 0e432d5 → e3181d5, confirmed as "nothing to pull" two minutes later in the
  next run). git writes progress to stderr; PowerShell 5.1 turns that into ErrorRecords as soon as
  the stream gets merged into the pipeline — which happens when the operator invokes the script
  with `2>&1`. With `$ErrorActionPreference = 'Stop'` the very first progress line already
  terminates, and the exception message is exactly that first stderr text. `Invoke-Git` now
  explicitly merges stderr, demotes errors for the duration of the call and makes the
  **exit code the sole verdict**; progress lines land as INFO in the log instead of terminating.
  Real git errors still throw, now with git's text instead of just an exit code. Verified in a
  scratch repo (before: abort with identical signature, after: clean run) and end-to-end on the
  real script via `-DryRun` under `2>&1`.

### Added
- **`tools/ops/measure_cpu_baseline.ps1`** — read-only CPU sampler for the Z0/C3 programme
  (WMI perf counters instead of cumulative `Get-Process .CPU` seconds). First run, 10 min / 35
  samples / 10 cores: **box mean 78%**, not 100%. And the biggest single item in the measurement
  is the measurement itself: **~34 percentage points come from the agent session itself**
  (claude 16.4%, the sampler via WmiPrvSE 4.4%). Fleet python 18.5% + 10.6% pool workers,
  postgres 14.1% (120 different PIDs in 10 min = connection churn), Symantec 5.5%. Without an
  observer the baseline load would sit at ≈48–50%, i.e. at the Z0 target — but that is a
  subtraction, not a measurement. The observer effect is therefore stated in the
  tool's docstring: a Z0 acceptance run needs a run **without** a session. Per-bot attribution
  is not possible unelevated (`Win32_Process.CommandLine` is `$null` for the elevated fleet) and
  is deliberately not guessed at.
- **`DASHBOARD_BIND_HOST`** (`dashboard.py`, `.env.example`) — bind address as an operator knob,
  **default unchanged `0.0.0.0`**, plus a startup warning as long as it is not bound to loopback.
  Reason: as-is measurement for Z2/B4. `cloudflared` is not even installed on SRV02, the port
  hangs open on the internet (an ESTABLISHED connection from a foreign IP at measurement time),
  `dashboard.log` documents ongoing scans incl. `GET / → 200` to strangers, and
  `grep -i auth dashboard.py` finds nothing — `POST /api/system/stop_all` is exposed unauthenticated. Flipping
  to `127.0.0.1` closes this immediately, but costs remote access until the tunnel is in place:
  Michi's decision, not a PR decision.

### Verified (no code needed)
- **Query 9 (P2.25)** run live: `bot_regime_whitelist` = 1590 rows, **all** from the last
  hourly run, 0 raw-name keys, 0 stale rows. Both DELETE criteria fire.
- **P2.15 against a real listing**: GRVTUSDT was detected on 2026-08-01 06:01:38 on the
  **running** fleet (last restart before that 30.07.), `candles` carries it from 2026-07-31 15:00
  to current. The empty `GRVTUSDT_1h` is the C-Gate state, not a defect. Remainder: the first
  `ticker_10s` row only arrived after the restart — the writer is `10_pump_dump_detector.py`,
  never part of the P2.15 scope.
- **P2.2**: live, `trade_cooldowns.module` is today `character varying(50)` — the ALTER ran
  and was never documented anywhere. **The checkbox stays open regardless**: `26_regime_detector.py:242`
  still creates `module TEXT` + `TIMESTAMP WITHOUT TIME ZONE`, so the bootstrap order still
  decides on a fresh DB unchanged. The `COOLDOWN_MODULE_MAX_LEN = 10` comment was justified with
  the now-wrong premise "live is varchar(10)" — comment corrected, value deliberately not raised
  (changes cooldown keys on the money path).
- **Job 10 / B7 obsolete in the commissioned form**: MIS2 posts live (open signals through 01.08.),
  ATB2/ATS2 are built; adapters exist for `ufi1, td, bb, abr1, mis1, rub, atb2, ats` (+`epd`).
  What's really still open is only **QM and SRA1** — a small follow-up task, not a VPS session.
- **Job 11 / signal rate delta**: closed as **not reconstructible**. The target window
  (13./14.07.) sits three weeks and >20 restarts in the past; `ai_signals` is the open book (a
  day-count there measures survivorship, not rate — the apparent rise 102 → 1,061 is exactly
  that), and the deduplicated union with `closed_ai_signals` is retention-skewed for older days.
  Only the classic side holds up: `closed_trades_master` sits stably around ~700–900
  signals/day with no detectable break. A number for the delta is not invented.
- **RSI execute** was already in the CHANGELOG (entry `[2026-07-12]`, 88,426,142 cells / 3,831
  tables / 9.6h) — no second entry.

### Operations
- **The local secret guard from hard rule 3 is not active on SRV02**: neither `pre-commit` nor
  `gitleaks` are on the PATH (even `ruff`/`mypy` only as Python modules). On this host, committing
  runs **no** secret scan and **no** `guard.py verify`; only the CI regex remains. For this
  session the equivalents were run by hand. No `--no-verify` — there is simply nothing to bypass
  here. A host setup point, not a code point.

## [2026-08-01] The C-Gate has been live for 16 days, not dormant — current state measured, two bots read frozen tables (T-2026-KYT-9050-002)

The task was to check the readiness of the dormant phase 2/4 slices and measure the volume
picture for Michi's start-time decision. **The finding overturns the premise: the
C-Gate is already running.** Report with all the numbers:
`docs/T-2026-KYT-9050-002-c-gate-status.md`. No flag set, no restart, no
write query — everything read-only against the live DB.

**The switch-over is dated, not estimated.** `.env` carries
`KYTHERA_CANDLES_SOURCE=hyper`, `WRITE_PRIMARY=hyper`, `DUAL_WRITE=1`. All per-coin tables
end exactly at `open_time = 2026-07-16 16:00 UTC`, right before the watchdog restart
`watchdog_debug_20260716_192326.log` (16:23 UTC), and `core/candles.py` skips the per-coin
write at `write_primary=hyper`. **Phase 3 (parity cron ≥5–7 days) was skipped in the
process** — the read cutover and write-primary flip ran in the same restart. The
gate cannot be caught up after the fact: `candles_parity.py` compares legacy against hyper, and
the legacy side has been empty for 16 days (every live run reports `rows old=0`). The tool
itself is intact — self-check and dry run run green under both session 3.14 **and** fleet 3.13.

**The UTC flip was NOT activated along with it** (`3ba3bbd` is not an ancestor of the running
`e3181d5`) — the coupling feared in the task brief did not occur. It was never
structurally possible either: `open_time` is `timestamptz` on 9,804 of 9,806 per-coin tables,
so the backfill cast is never session-TZ-dependent; the only two naive columns sit in
`ai_signals`/`closed_ai_signals`.

**Volume picture — the design-doc assumptions are outdated.** Legacy per-coin: **64GB**
(not 25GB), of which 54GB are indicator tables. Hypertables: `candles` 45.0M rows /
9,954MB, `indicators` 18.6M rows / 20GB. Database total 98GB, C: has **78GB free**
(not ~160GB). **Compression is not active on either hypertable** — 0 of 128
chunks each, no policy; the expected gain remains unrealized to this day. As an anchor, a
measurement from the same DB is used instead of an estimate: `oi_5m` compresses 652MB → 78MB
(8.35×); for the 108-column float table `indicators` that is an upper bound, the honest range
sits at 30GB → 4–10GB. The real number costs a probe chunk (reversible) and is therefore
an operator decision. Backfill coverage: 9,669 of 9,683; the 14 missing are
without exception `GRVTUSDT`, whose legacy tables are empty — **no history lost**.

**The one real defect: two bots have been reading frozen tables for 16 days.**
`16_smc_forex_metals_bot.py:87` and `21_btc_smc_strategy.py:136` are the only
remaining raw SELECTs against per-coin tables in live code. Both were deliberately deferred in
`CANDLE_CALL_SITES.md` ("index-coupled, only with an offset rework") — the
deferral assumed the legacy tables would stay authoritative, which ceased to hold on 07-16.
Blast radius verified and **consequence-free so far**: `CH_SMC_METALS` last posted
on 2026-07-16 09:35 UTC (9 posts in the entire outbox window), `CH_BTC_SMC` has zero posts.
That is luck, not design — both emit a Cornix-parsable block. **No fix
committed:** both bots are de facto stalled; hooking them back up to live data is an
unpark (OPUS-HANDOFF §6) and therefore Michi's decision — fix or deliberately park.

**Rollback is no longer trivial**, contrary to the design promise. `KYTHERA_CANDLES_SOURCE=legacy`
today silently puts the fleet on 16-day-old candles; a rollback of the read side would need
a backward backfill first. The asymmetry was predicted in `core/candles.py` and has
now materialized. Three decisions with numbers remain open (enable
compression, drop the legacy tables — 64GB, but only after a `pg_dump` restore test **and**
removing the `CREATE TABLE` loop in `6_housekeeping.py:67`, otherwise they grow back —
plus the verdict on the two bots), plus a pre-defined replacement parity plan G1–G6
against the existing data instead of the live stream.

## [2026-08-01] TD2/BB2/QM2 retrain: NO-GO for all four — and the rerun artifacts had been silently overwritten (T-2026-KYT-9050-006)

Up for evaluation were the artifacts of the post-Wilder rerun from 14.07. **They no longer exist.**
`tools/retrain_from_replay.py:423` and `smc_ml_trainer.py:376` write the same path in the same
staging directory; a legacy trainer run on 14.07. between 05:21 and 05:24 overwrote all four
freshly retrained TD/BB artifacts. Two independent pieces of evidence: the pkl mtimes sit
**after** the corresponding `retrain_*_stats.json` (by 2.6h for `td_4h`), and today's files
carry `meta.trainer = 'smc_ml_trainer.py'`, `optimal_threshold = 0.3`, **no**
`calibrator_isotonic` and **no** `meta.model_id` — keys `save_artifact` always writes.
Side effect: the replay-retrained generation was live from 06.–13.07. (tags `TD2_4H`/`BB2_4H`
in `ml_predictions_master`) and fell back to the old tags without a ledger trace when it was
overwritten.

**A verdict was reached anyway** — the metrics (`retrain_*_stats.json`) survived, and they are
negative across all four: **TD_1H** anti-calibrated (live gate bucket 0.8–1.0 → mean **−2.28%**,
the worst of seven; bucket 0.0–0.3 → +4.04%; validation already −78.2 — `pick_threshold`
still returns a threshold, because it lacks the deployability abort of `pick_threshold_safe`);
**BB_1H** takes **98.6%** of the test events at threshold 0.40, the gate is a no-op;
**BB_4H** filter-only as in batch E (test Σ −686); **TD_4H** selects with WR 59.2% **below**
the base rate 60.7%. The Wilder rewrite made the cohort worse — the only
promotion recommendation of the 12.07. report (TD2_4H, then +185.8) no longer holds.

**Live counter-check** (`closed_ai_signals`, deduplicated, unlevered staggered move per leg,
03.–08.2026): the only replay-retrain generation ever gone live, `BB2_4H`, books **−1.57%/leg
over 99 legs**, the legacy artifact next to it **+0.25%/leg over 3,076 legs**. TD_1H +0.91%,
TD_4H +1.08%, QM_1H +0.07%, BB_1H −0.26%. **The TD/BB replay-retrain line is therefore closed
as NO-GO**; the live inventory stays unchanged. Open and explicitly unresolved: on the
direction axis, the live realization contradicts the 540d study in **sign** (live carries
LONG, in the replay LONG is p≈1.0 negative) — that devalues replay PnL as a promotion criterion
for this bot family until it is clarified whether the cause is the population or the exit
economics.

**QM2 deliberately excluded** (no replay path built): `walkforward_sim.py:1151` and
`retrain_from_replay.py:980` both do not know `qm`; the main benefit of a replay retrain — the
threshold calibrated on validation — never reaches bot 24 at all, because `24_quasimodo_bot.py:45`
hard-gates on `MIN_CONFIDENCE = 0.65` and never reads `optimal_threshold`; QM_4H is parked in code
(`:42`); and QM_1H books zero-EV live (+0.07%/leg, 31 posts in 5 weeks).

**Code change** (countermeasure, no behaviour change to the bots):
`core/staging_guard.assert_no_foreign_overwrite` refuses to overwrite an artifact with a foreign
`meta.trainer` — wired into `retrain_from_replay`, `smc_ml_trainer` and
`qm_ml_trainer`. Deliberately fail-open (missing/unreadable provenance never blocks), override
`KYTHERA_ALLOW_TRAINER_OVERWRITE=1`. `backtest/test_staging_guard.py` (8 tests, DB-free) pins the
real 07-14 case in both directions.

The `bfill` in `24_quasimodo_bot.py:140-141` and `25_smc_ml_sniper.py:311-312` is **untouched**
(the ticket lines `:126`/`:220` are stale) — it only falls with an artifact rollout, and that
is not happening. Nothing promoted, no deploy, no gate flip, no restart. Details and the
Rollout-Tabelle: `docs/T-2026-KYT-9050-006-td-bb-qm-retrain-verdict.md`.
## [2026-08-01] EPD detector retrain on the post-P1.39 definitions: not feasible (calendar) — revisit 2026-11-09 (T-2026-KYT-9050-004)

The task was to retrain `pump_dump_model.pkl` on the time-based feature windows in place since
P1.39. **Result: no artifact — and none that should be produced.**
The blocker is the calendar, not data quality. Report:
`docs/T-2026-KYT-9050-004-epd-retrain-feasibility.md`, numbers
`staging_models/replay/epd4_feasibility.{json,md}` + `staging_models/retrain_epd4_stats.json`.

**Cut point re-counted rather than taken as given:** the hourly
`pump_dump_events` count on 2026-07-10 (UTC) breaks on the hour of the
bot-10 restart (17:08:29Z) from 56–170/h to 10–33/h. The restart armed P1.39, the
T-035 rate normalization and the revived hourly warm-up together; the jump
is a **gate** effect, not a feature effect (the coverage floor removes the events that
arose from a one-sample denominator).

**The dataset is clean, just too short.** `epd2_build_dataset.py --since '2026-07-10
17:00:00'` writes 4,698 of 4,712 events (0.3% loss: 11 `no_candles`, 3 `no_ticker`,
otherwise none), 4,327 labelled, span **22.0d**. `chrono_split` gives val and test each
the 15% quantile band = 3.3d, and the 7d purge gap (= the builder's label horizon) eats it
entirely: `split 1664/0/0` (LONG) and `1364/0/0` (SHORT). ≥50 rows per slice needs
~50d span, an operating point with headroom ~122d.

**The hard upper bound is `ticker_10s`, not the cut point.** The builder has taken the entry
from the hypertable since T-2026-CU-9050-035, and that starts on 2026-07-07 11:19Z — three
days BEFORE the cut point. The Feb–July dataset (85 031 events, the basis of EPD2/EPD3) is
**no longer reproducible** with today's builder; the requested cut at the
cut point therefore costs three days. Retention 365d ⇒ the window grows.

**The task's premise does not survive measurement.** A two-sample KS per feature
(14d before vs 14d after) sits, for all four affected inputs, **inside the null band**
of neighbouring 14d window pairs from the pre-cut history: 0.036–0.174 against a null-band
median of 0.058–0.080 and a null-band max of 0.204–0.434. And the deployed
`epd3_model_LONG.pkl`, fit on pre-cut data, keeps discriminating on the post-cut events
out-of-sample (AUC(TP1) 0.586, calibration monotonic 38.3 → 66.7% TP1; SHORT 0.537). There is
no sign of a serving model broken by the definition change — bot 10 keeps running
unchanged. Caveat: only marginal distributions measured.

**Tag EPD4 reserved, deliberately not yet registered.** EPD1/EPD2/EPD3 are taken
(checked against `bot_variants/index.legacy_artifact_slots()`, `shadow_gate.SHADOW_ARTIFACTS`,
`_LIFECYCLE`, `_RETIRED_TAGS` and the DB history); `epd4_model_*.pkl` does not claim any
foreign loader slot. A `shadow_gate` entry without an artifact would be dead configuration —
what is pinned instead is the occupancy itself, including the trap that the gate default is
LIVE. **P1.45 deliberately not wired further:** the artifact path has been reading `model_id`
for a while; on the challenger path the tag must stay a constant, because the **live**
running `epd3_model_LONG.pkl` carries `model_id='EPD2'` in its meta (a known defect blocked by
the `retag_artifact.py` version guard) — a rewire would post it under the old tag and
blind `has_open_ai_signal`.

**Code:** `--model-id` on `tools/retrain_from_replay.py` and `tools/retrain_pump.py`; the
tag sets `meta.model_id` **and** the filename prefix together (`artifact_slot`,
identical to `promotion_guard.tag_prefix`) — letting them drift apart is exactly the
slot-hijack bug from 2026-07-21. Default `EPD2` ⇒ unchanged run. The degenerate
split now reports the arithmetic (span, band, gap, density, missing days) instead of just
"skipped" and stores it machine-readably in `retrain_<slot>_stats.json`.

**Open ahead of an EPD4 go-live (not verified in this task):** the serving population
is 2.7× (LONG) resp. 5.4× (SHORT) denser than the training population — the builder's 900s
dedup mirrors an alert throttle whose timer is only reset on the live-trade branch and is
inert for a leg that does not post live.

Verification: `backtest/test_retrain_model_id.py` (14/14, DB-free),
`backtest/test_epd_tag.py` + `test_promotion_guard.py` + `test_epd3_artifact_model_id.py`
(43/43), `ruff check .` + `ruff format --check .`, `regression_guard verify` (24/24).
All DB access read-only, training/build runs under the job lock.

## [2026-08-01] Bot 30 (PEX1) failed on EVERY scan — aware/naive mix in `spike_time` (T-2026-KYT-9050-061)

`30_ai_pex1_bot.detect_spike_time_offset_h` subtracted a naive `now` from `MAX(spike_time)` and
thereby threw `can't subtract offset-naive and offset-aware datetimes` — inside the `try` block
**before** the event loop, so the whole cycle dies. **Re-counted independently** in the four most
recent `logs/watchdog_debug_*` (as of 2026-08-01 20:18): **5,876 failures, 0 successful scans**,
the most recent error minutes old; the ticket counted 8,166 in its earlier log window. The bot
loads its artifact cleanly and still does nothing, for at least since 2026-07-19.

**Root cause:** `pump_dump_events.spike_time` is `timestamp WITH time zone` on the live DB
(measured read-only 2026-08-01) — the repo DDL in `10_pump_dump_detector.py:1409` says `TIMESTAMP`,
the table was altered at some point. psycopg2 therefore returns **aware** datetimes, while the
offset heuristic was written for a naive column. The note in T-2026-KYT-9050-005 that the
function "heals itself after the flip" is thereby refuted and corrected in `docs/UTC_POLICY.md` §4.

**Fix on the R3 line:** an aware value IS an instant — no domain guessing, offset 0, and
`spike_time_to_utc_naive()` normalizes both domains in **one** place (aware → UTC instant
without tzinfo, naive → the measured offset subtracted). The same normalization applies in
`process_event`, where the same mix would have been waiting. Two latent triggers were also
defused along the way: the boot sentinel for an empty table is now aware, and the watermark no
longer needs `max(sentinel, spalte)` (the query returns ASC and filters strictly `> watermark`).

**Careful, gate:** as soon as the scan is alive, PEX1 posts **live** — checked directly: `.env`
carries `NEW_IDEAS_LIVE_POSTING=1`, the code default is `"1"` anyway, and `pex1_model.pkl` is
git-tracked and deployed. Whether that is desired is a gate flip and Michi's decision — nothing
was changed here.

Verification, DB-free: `backtest/test_pex1_spike_time_domain.py` (13/13, aware **and** naive
through the offset detector, normalization and the age arithmetic). Counter-check: the same
aware case against the predecessor state (`git show HEAD:30_ai_pex1_bot.py`) throws exactly the
live error message.

## [2026-08-01] R3 part 2: pool on `timezone=UTC`, P2.3 writer and six compensations → one constant (T-2026-KYT-9050-005)

The second half of the UTC policy. Until now the VPS session TZ (`Europe/Bucharest`) decided
how Postgres casts between `timestamptz` and the naive legacy columns — i.e. what `NOW()` writes
into a naive column and how it is compared against an aware value. The flip to UTC is
therefore **not a one-liner**: it moves the writers, and six readers had been correctly
subtracting the +2/+3h. Both had to land in **one** changeset, otherwise each half would create
new drift on its own (P0.13 class, train/serve skew).

**What's in it.** (1) `core/database._connect_options()` carries `-c timezone=UTC`. (2)
`3_detectors.write_signal_atomic` stamps `utc_now_naive()` instead of naive server local time — one
call for both columns (`time`, `posted`), P2.3. This is mandatory, not optional: `33_ai_fif1_bot`
compares `time` against `NOW()` on the DB side; without the writer fix its 1h/24h burst density
would have been broken by the flip instead of fixed. (3) The six drift compensations
(`15_ai_master_bot` `to_utc_naive`/`since_local` plus `research_dataset_common`, `aim2_build_dataset`,
`fif1_build_dataset`, `pex1_build_dataset`, `retrain_sra2`) no longer compute anything themselves —
they go through `core.time.legacy_naive_to_utc` / `utc_to_legacy_naive`. (4) The docstrings that
claimed "PG local time" have been updated.

**One exception, with justification.** `pex1_build_dataset.spike_time_to_utc` still localizes —
but only if `detect_offset_h` has **measured the offset from the data**. A measurement cannot be
made wrong by the flip (it comes out as 0 afterward), and for the live table the branch is dead
anyway, because `spike_time` is `timestamptz`. Deleting it would only have cost the ability to read
old dumps. The DST recipe still sits in one central place regardless (`assume_legacy=True`, the
only such call in the repo).

**The history decision has NOT been made** — it belongs to Michi (`docs/UTC_POLICY.md` §6).
The code keeps both paths open, at exactly one spot: `core.time.R3_CUTOVER_UTC`
(`None` = uniform UTC, i.e. the world after a backfill; set = rows before it are read as
Bucharest). The earlier objection "then every trainer carries a permanent branch" no longer
holds. Numbers for it, measured read-only: a backfill touches **≈2.00M rows / 420
MiB** across six tables (`ml_predictions_master` 1.13M, `closed_ai_signals` 477k,
`closed_trades_master` 383k, `closed_trades3` 8.2k, `ai_signals` 3.2k, `active_trades_master` 539);
the `regime_*`/`orchestrator_*` cluster stays out. Residual fuzziness of both paths: 113 values in
the ambiguous autumn hour, and an additional ≤3h band around the restart for the cutover.

**Three inventory lines were wrong and are corrected** (measured live, not assumed):
`pump_dump_events.spike_time` and `master_ai_processed_signals.processed_at` are `timestamptz`;
`closed_ai_signals.close_time` is **naive**, not `timestamptz` as claimed (the `CREATE TABLE
IF NOT EXISTS` never widened it — the same trap as P2.2). And `regime_history.ts` provably
carries naive-**UTC**: the local wall clock 03:00–03:59 does not exist on 2026-03-29, and the
column has 12 rows there. Side finding, not fixed along with it: `tools/breadth_study.py:428`
localizes exactly this column as Bucharest — its as-of join is already 2–3h off today (its own
task, because a fix would change the study's result).

**Everything only takes effect with the fleet restart**, process by process. Restart effect: rows
from before the restart appear +2/+3h in the future, affecting the short windows (60min trade
monitor, 1h/24h FIF1, 5d AIM2 stream); FIF1 posts nothing from this because the startup marking in
`main()` ticks off everything that falls inside the window on the first poll. Verification
DB-free: `backtest/test_r3_utc_flip.py`
(12/12), plus the touched surface (`test_aim2_topn`, `test_aim2_event_source_symmetry`, `test_time`,
`test_detector_*`, `test_research_bots_live_price`, `test_pump_dump_time_windows`) and
`regression_guard verify` 24/24 without refresh.

## [2026-08-01] RUB2 replay↔live skew: hypothesis refuted, root cause was the measurement window (T-2026-KYT-9050-008)

The 070 finding needed clarifying: for the same (symbol, candle) signals, live confidence
and replay prob correlated at **−0.37** — same model, same candle, so a feature skew. Suspected
were the funding features.

**The funding hypothesis is refuted.** All six funding features can be reconstructed
**bit-exact** from today's `funding_rates` for **all 229** matched signals (mean|Δ| = 0.0 in
every column). The nine RUB features check out too — `dist_to_trend` and `slope_trend` exactly,
the rest down to float32 storage rounding. There is no feature skew between serving and replay.

**The −0.37 was the measurement window.** The overlap period 06./07.07. sits on the go-live day
of RUB2-SHORT (`07c8874`, the switch point is visible in the data: 07.07. ~07:00 UTC). On 06.07.
the tag `RUB2` still carried the old 9-feature legacy path. Pooled across the generation boundary,
a correlation measures the model change, not the skew: 06.07. −0.45, from 08.07. **+0.97 to
+1.00**, from 12.07. **exactly** identical on 92–100% of rows. 229 pairs across 128 coins instead
of the 49 from T-070.

**Two premises of the ticket were already outdated.** The replay was regenerated on 14.07. —
after the funding backfill (11.07.) and after the two look-ahead fixes in
`walkforward_sim` (10.07.) — and RUB2 was retrained from that same run (threshold 0.7929
instead of 0.829) and promoted to root. Step 2 of the task was thus already done before the
session started; **no sim run was executed**. The July model now only lives on in
`staging_models/max1_model_SHORT.pkl` — the probe selects the artifact by fit, not by filename.

**The residual difference from 07.–11.07. is dated.** mean|Δp| 0.009–0.015, jumping to 0.0003 on
12.07.: `logs/rsi_rewrite_execute_20260712.log` documents an **executed** Wilder-RSI rewrite of
the indicator history (3831 tables, 88.4M cells). The bot read span-RSI at scoring time, the
replay from 14.07. reads the overwritten Wilder value for the same candles. That is the
mixed-history risk predicted in P2.12 — measured here for the first time: **≈1 percentage point**
of probability. The AUDIT_TODO line "execute stays C-Gate" was stale and is corrected.

**Step 3 (MAX1 calibration) turns with it.** The statement "live sees 0.93+ at ~1.1 posts/day"
comes from the same pre-deploy lines. RUB2-SHORT has **never** reached 0.93 live: max 0.876 in the
generation @0.829, max 0.920 after the 14.07. retrain, zero rows above that in 1543 predictions.
This also brings replay and live into agreement on the distribution (test-slice p99 0.841 / max
0.874 against live p99 0.865 / max 0.876) — **the replay curve is trustworthy for gate calibration
again**. MAX1's current state checked directly (`.env` + DB, 01.08.): `MAX1_LIVE_POSTING=1`,
`MIN_PROB=0.829`, `MAX_PER_DAY=100000`, 308 posts since 11.07., max confidence 0.9199 — the
documented default of 0.93 would have produced zero posts in 21 days. Reconciling the regime is
an operator decision, only noted here.

**Latent defect found and fixed.** `tools/walkforward_sim.py` built the epoch axis of the
RUB regression with `open_time.astype("int64") / 1e9` — not a unit conversion, but a bet on
the column's **resolution**. Under the fleet environment (pandas 2.3.2, `datetime64[ns]`) it is
correct; under pandas ≥3.0 (`datetime64[us]`) the axis shrinks by a factor of 1000 and
`slope_trend` — one of the 15 model inputs — comes out 1000× too large, while `dist_to_trend`
right next to it still fits. Exactly that happened to this session on its first reconstruction
run. Fixed with `core.time.epoch_seconds()` (normalized to ns before the division), applied in
`walkforward_sim` and the three study tools with the same pattern — **byte-identical** to the
prior state under the fleet interpreter, verified. Pinned in `backtest/test_epoch_seconds.py`,
mutation-tested.

New: `tools/rub2_replay_skew_probe.py` (read-only, five layers: match → artifact attribution →
feature reconstruction → funding bound → threshold curve). `core/funding_features.py` deliberately
**untouched** — the root cause does not sit there. No retrain, no promotion, no gate flip,
no restart, no write query against live tables. Report:
`docs/T-2026-KYT-9050-008-rub2-replay-skew.md`.
## [2026-08-01] Correlation layer over vol-targeting: DEFERRED, substrate missing (T-2026-KYT-9050-023)

A premise check instead of a design. The task calls for a portfolio correlation layer **over**
the GARCH vol-targeting (rationale in the ticket: the independent per-coin throttle ignores
cross-coin correlation, leaving concentrated beta in the 538-coin book). What was checked first
was whether this lower layer even exists — **it does not**, for two independent reasons.
Result: **no design built**, file `docs/T-2026-KYT-9050-023-correlation-layer-deferred.md`.
Read-only, no code touched.

- **The vol-targeting layer is not wired in.** `tools/research/garch/` is referenced outside the
  package only by `backtest/test_garch_*.py` (+ one comment in `test_stoic123_signals.py`) and the
  CHANGELOG. No bot (`NN_*.py`) and no `core/*.py` imports it — the bots
  import from `core/`, never from `tools/` (verified via grep, not via documentation).
- **No gate exists.** The live `.env` carries 60 keys (names only read, no values):
  credentials, 44 `CH_*`, and the gates `AIM2_LIVE_POSTING`, `NEW_IDEAS_LIVE_POSTING`,
  `AIM2_TOPN_*`, `MAX1_*`, `TRAILING_BOT_LIVE_POSTING`, `KYTHERA_CANDLES_*`. No `GARCH_*`,
  no `VOL_TARGET_*`, no `SIZING_*`. Nothing to flip — the layer is not default-off,
  it simply is not there.
- **T-030 had explicitly retired it.** `T030_live_verdict_report.md`: pooled Sharpe Δ +0.009,
  median across 9 bots +0.013 against a +0.10 threshold → NO-PULL, verbatim recommendation *"do
  not wire GARCH vol-targeting into any bot's sizing"*. The task's premise (a rolled-out layer
  with a residual bug) is therefore stale.
- **Second, independent finding: Kythera does not size any positions at all.**
  `build_cornix_block` (`core/signal_post.py:63-84`) emits direction, leverage, margin, entry,
  TPs, SL — **no** size/notional line. `lev` is `get_max_leverage(symbol, 20)`, i.e. the
  exchange cap from `max_leverage.json`, not a per-trade risk decision (exception UFI1 = parked).
  Position sizing is a Cornix-side operator setting. A "per-position throttle" would
  therefore have no output field; creating one changes what Cornix does with real money → Michi.
- **What actually controls concentration live is slot-based, not beta-based:**
  `has_open_ai_signal` (one open signal per symbol×direction×tag), `SLOT_CAP = 500`
  (`core/trailing_roster.py:49`, Cornix channel cap) and the regime whitelist 26/27/28 —
  that is already measured and optimized in T-042/T-052.
- **Re-entry condition** (recorded in the file): only once (1) Kythera itself sets a
  position size **and** (2) an effective per-position throttle is rolled out. Before that,
  the next sensible question is not "build a layer" but the plain measurement of whether the
  book is even correlated strongly enough for beta concentration to bind. Cost note for that
  case: 538×538 ≈ 145k pairs per rebalance, numerically worthless without shrinkage at T ≪ N —
  and SRV02 sat constantly at 100% CPU across three 2s samples on 2026-08-01 (10 logical cores,
  `Get-Counter`).
## [2026-08-01] K2 study machinery: market-neutral frame + tape-causal stage-2 entry (T-2026-KYT-9050-013)

The two machine defects found in the K2 review (T-2026-CU-9050-143, PR #133) and documented
there as known limitations, in `tools/xs_momentum_study.py`, are fixed. **The study's verdict
does not change** — it stays `weak/inconsistent-spread (not deployable)`: it rests on the
`absolute` cells, whose scoring is untouched (a closed-form test pins that). What is repaired
is the measurement machine, not the result. No bot, no gate, no artifact touched.

**(1) The `market_neutral` frame was a no-op.** The beta subtraction sat on the SIGNAL
(`sig = sig_abs − btc_sig`) — a per-rebalance **scalar** shift, hence argsort-invariant — while
the PnL used absolute coin returns. Result: all 60 `market_neutral` cells were byte-identical to
their `absolute` twin; beta was never removed (re-counted in the checked-in full-run artifact:
60/60, and reproduced synthetically before the fix). The adjustment now sits on the **returns**
(`fwd − (btc_H/btc_0 − 1)` over the same holding window, K5 convention) — exactly what spec §K2
point 2 always called for ("market-neutral (coin return minus BTC return)"). The signal
subtraction stays as a precondition ("a BTC signal must exist"), now commented as such. Since
both frames rank identically, the fix only changes the **scoring**, never the selection; the
top-minus-bottom spread is beta-invariant and stays the same across frames. The cost basis is
deliberately kept identical to `absolute` (one round trip) so the frames stay comparable
cell-by-cell — fee and funding of the BTC hedge leg are NOT modelled and are flagged as such in
the report.

**(2) The stage-2 entry sat ~1 daily bar too early (look-ahead).** `load_1d` floored `open_time`
to `'D'`, so `dates[t]` is the day's **open**; but the ranking signal is `close[t]`. The entry
at the first 1h close from `dates[t]` therefore traded ~23h before the signal was even
observable (the entry candle opened a full 24h too early — measured in the test). The anchor is
now `dates[t] + 86400`: the first 1h close from the day's close. Only the confirmatory stage 2
was affected, never the stage-1 verdict. The same trap as in T-052 (the be family, 59k→7k after
the correction): pin conditions tape-causally.

**Staleness protection instead of a silent relabel:** `STUDY_SEMANTICS_VERSION` (now 2) travels
into the report meta. A report rendered from a run with older semantics carries a
**STALE banner** naming both defects instead of claiming it was produced under the new
arithmetic. That is exactly the case for the checked-in full-run artifact (527 coins,
2026-07-17): re-rendered DB-free via `--reverdict` (cells/stage 2/verdict byte-identical, text
only) — its `market_neutral` and stage-2 numbers stay pre-fix. **A full run under v2 is an open
follow-up step** (DB-heavy ⇒ one job slot on the VPS, deliberately not run in this session).

New DB-free tests `backtest/test_xs_momentum_study.py` (9): both defects were FIRST reproduced
this way (60/60 identical resp. 24h entry lead), then fixed. They additionally pin that the
`absolute` frame stays unchanged (bot 39's signal contract, cell F84|raw|absolute), that selection
and spread are frame-invariant, and stage 2's resume semantics.

## [2026-08-01] Research bots 30/31/32: entry anchor on get_live_price (T-2026-KYT-9050-011)

The open follow-up from block 5 of the R1 migration (T-2026-CU-9050-112) is closed. Since
`core.research_features.fetch_context_frame` reads with `include_forming=False`,
`df["close"].iloc[-1]` is the last **closed** 1h candle — as an entry-price anchor that is stale
by up to ~59 min. Exactly this anchor was used by `30_ai_pex1`, `31_ai_fmr1` and `32_ai_trm1`.
They now fetch it via `core.live_price.get_live_price(symbol, conn)` (Binance REST, fallback to
the newest 5m close) — the path of the block-4 bots 11/22/24/25 and exactly what `core.candles`
contract 2 prescribes: **detection on closed candles, price separate**. `33_ai_fif1` was never
affected (takes `sig["entry"]`).

The feature side is untouched: the feature candle still comes from the `searchsorted` floor-1
join over `open_time`, the frame stays ASC and closed-only. **Only** the price shifted, the one
feeding entry/SL/targets (`calculate_smart_targets`) and the prediction log's `entry_price`
column.

**New: if `get_live_price` returns None** (Binance dead AND the DB fallback empty), the signal
is skipped instead of posted with `None` — previously this case could not occur at all, because
the frame always had a close. The cooldown semantics deliberately stay unchanged: 30/31 also set
the cooldown on the None path (it mirrors the unconditional training dedup and hangs off
scoring, not posting), 32 continues to set it only on the post path. For 31, the FMR2 shadow leg
also drops out on the None path — it uses the same anchor. For 32, `fetch_context_frame` stays in
place as a data-freshness guard (BTCUSDT 1h join present and not staler than
`CONTEXT_MAX_STALENESS_H`), even though it no longer supplies a price there; the TRM1 features
come from `regime_history` anyway.

**Live state (corrected 2026-08-01, originally logged incorrectly):** the bots are **not**
gated and 30 is **not** un-deployed. The gate is open — `.env` carries
`NEW_IDEAS_LIVE_POSTING=1`, the code default is `1` anyway
(`os.getenv("NEW_IDEAS_LIVE_POSTING", "1") == "1"` in 30/31/32/33), and the bots log
`Posting: LIVE` on startup. `pex1_model.pkl` is git-tracked and gets loaded (`✅ Artefakt geladen:
pex1_model.pkl — 13 Features, Threshold 0.70, Tag PEX1, Kalibrator: ja`, most recently 2026-08-01
07:32); only 31 and 32 run artifact-less in idle mode (`Artefakt fehlt: fmr1_model.pkl` resp.
`trm1_model.pkl`). The change still produces no live delta — but for a different reason: the
PEX1 scan aborts in **every** cycle (`PEX1-Scan-Fehler: can't subtract offset-naive and
offset-aware datetimes`), inside the `try` block **before** the event loop
(`detect_spike_time_offset_h` subtracts a naive `now` from `MAX(spike_time)`; `fetch_new_events`
compares in SQL). This means `process_event` — where this fix sits — is currently never reached.
Evidence from `logs/watchdog_debug_*`: a steady ~1 failure/min from 2026-07-19 21:37:56 through
2026-08-01 16:10:10, the four most recent logs alone carry 8166 failures, not a single successful
scan (the onset could be earlier — not checked further back). Logged as
**T-2026-KYT-9050-061**; once the abort there is fixed, this fix takes effect live immediately. It
is therefore not precautionary but correct and live, just currently stuck behind a dead scan. Newly
pinned in `backtest/test_research_bots_live_price.py` (8 tests, DB-free): anchor == live price
instead of frame close, None ⇒ no post and no prediction log, per-bot cooldown semantics, 32's
freshness guard and a source pin against regression.
## [2026-08-01] Challenger promotion name guard + EPD3 `model_id` re-dump (T-2026-KYT-9050-057)

A challenger promotion used to be exactly two manual steps: flip the register line in
`core/shadow_gate.py` from `SHADOW` to `LIVE` and copy the artifact into the repo root.
`shadow_artifact_path` returns the **bare root filename** for a LIVE leg — and
`SHADOW_ARTIFACTS` historically carries the filename of the retrain *generation* for challenger
tags, not the tag's own name (`"RUB3": {"LONG": "rub2_model_LONG.pkl"}`). Whoever promotes this
way puts the challenger file into exactly the slot the **legacy loader** reads its live model
from: both tags score the same model and post it twice (hard rule 4, real money). For
**EPD3-SHORT** that was real on 2026-07-21 — `epd2_model_SHORT.pkl` is bot 10's
`EPD2_ARTIFACT_PATHS["SHORT"]` — and was averted by hand via the challenger-distinct name
`epd3_model_SHORT.pkl`; for **EPD3-LONG** (T-037) the same manual work a second time.

**New: `tools/promotion_guard.py`** — exactly this manual work, automated. For every leg in
`SHADOW_ARTIFACTS` the guard checks whether its promotion target is challenger-distinct, and
otherwise names the foreign owner plus the rename suggestion (`RUB3 → rub3_model_LONG.pkl`). Two
independent pieces of evidence: the root slot is claimed by a foreign tag (hard evidence — another
loader really does read it), or the filename does not carry the tag's own prefix (also catches
a loader that is in no registry at all). The tag→filename bridge comes from
`tools/bot_variants/index.py` (new accessor `legacy_artifact_slots()`) — an already-tested
source instead of a second curated dict.

**The severity comes from the lifecycle, not the filename.** A **LIVE** leg on a foreign slot is
FAIL (exit 1, promotion stop) — it is already reading the foreign root name. A leg still parked is
WARN: a latent blocker, no live effect. So the guard is green today and turns red exactly the
moment someone flips without a rename. It is wired into three places:
as a pre-commit hook (`kythera-promotion-name-guard`, blocks this commit), as check 8 in
`tools/verify_staging_artifacts.py` (register scan at the end + WARN for each staging file whose
name is claimed by >1 tag), and as a CLI.

`core/shadow_gate.py` stays **unchanged** — the gate is imported by both bots *and*
trainer/replay (hard rule 7); a changed return value of `shadow_artifact_path` would have shifted
serving behaviour too. The guard reads the register, it does not write into it.

**Open and deliberately not fixed along with it:** `RUB3-LONG` still points at `rub2_model_LONG.pkl`
(WARN). As long as RUB3 is parked on SHADOW per T-037, the slot is not under threat; the rename
belongs in the promotion step (artifact + register in one move, operator decision), not in a
hygiene PR that would otherwise move a shadow load path without need.

**Part 2 (EPD3 artifact `model_id`) is delivered along with it — and one justification for it was
wrong.** The promoted `epd3_model_SHORT.pkl` carried an embedded `meta.model_id='EPD2'` (hard
rule 6). This is inert live, because bot 10 explicitly passes the tag `EPD3` at the call site and
`shadow_gate.load_shadow_artifact` normalizes to `{model, features, threshold}`, so it never reads
`model_id` at all — but `core.model_artifacts.build_contract` takes the posting tag **only**
from `meta.model_id`, and `tools/verify_staging_artifacts.py` checks exactly that field.

An earlier version of this entry claimed that re-dumping would be a lossy cross-version round
trip, because the embedded `IsotonicRegression` was pickled with scikit-learn 1.9.0 while the
fleet environment has 1.7.1. **That was wrong for this artifact and had the measurement
swapped:** `py -3.13` has sklearn **1.7.1**, `py -3.14` has **1.9.0**, and
`epd3_model_SHORT.pkl` carries an embedded **1.7.1** — it loads under the fleet Python 3.13
without a single warning. The re-dump there is a *same-version round trip*, not a downgrade. What
is pickled with 1.9.0 is the **LONG** artifact (in the 3.14 env, T-037); there the argument holds,
and only there. The second part of the old justification — a real retrain needs the DB, replay
labels and CPU — is correct, but answers a question the task did not ask: this was about
re-serialization.

**New: `tools/retag_artifact.py`.** Loads a format-A dict artifact, sets exclusively
`meta.model_id` and writes to `staging_models/`. Two non-disableable guards: the target must sit
in STAGING (hard rule 2 — the root promote stays Michi's decision), and the artifact must load
*warning-free* under the running interpreter — if sklearn raises an
`InconsistentVersionWarning`, the tool aborts and names the version under which the re-dump
would be clean. This exact guard would have prevented the reasoning error above; it fires on
`epd3_model_LONG.pkl` and refuses. After writing, the tool verifies the result against the source
(scores on a fixed probe matrix, calibrator curve, features, threshold, meta diff) and reports
success only with **exactly one** difference.

`staging_models/epd3_model_SHORT.pkl` now carries `model_id='EPD3'`, nothing else new; pinned
in `backtest/test_epd3_artifact_model_id.py` (loads via `core.model_artifacts` as `EPD3`, all
other fields identical to the root artifact, all mechanical checks of the staging verifier green).
The root artifact is **untouched** — the promote is an operator decision. Two honest
loose ends: `epd3_model_LONG.pkl` has the same tag defect but cannot be re-dumped here because of
the 1.9.0 serialization, and `verify_staging_artifacts.py` only globs `epd2_model_*.pkl` for EPD,
so it does not even see the EPD3 files in a CLI run (AUDIT_TODO #T57-5/#T57-6).
## [2026-08-01] SHORT legs scored under the trail rule — the yardstick was the problem (T-2026-KYT-9050-062)

Operator task: more shorts for bot 40. Four candidates checked, and the most important
finding at first was that **my own yardstick was unfair**.

**The bug:** measuring a leg against the **full** index move of its holding window
penalizes every take-profit leg by construction — it exits at TP1 while the
tape keeps running. In a market down −50%, that made almost every SHORT side come out
negative, with no way to separate "bad selection" from "TP caps the trend."
The LONG analysis from T-054 is not affected by this (longs in a falling market run
into the SL, not the TP).

**The rebuild:** `tools/short_leg_trail_value.py` puts **both sides under the same
trail rule** (act 2%, x 10%) — the leg on its coin path, the benchmark on the
index path over the same window. The leg's own TP policy thus drops out on both sides.
The index carries a synthetic high/low (median hourly ratios), because a
trail fires on wicks and a close-only benchmark would have flattered every leg.
Trail mechanics, the dedup loader and candles come from `trailing_slot_budget` resp.
`wave_buildup_study` — no second implementation (rule 7).

**Two of my own misjudgements that the fair yardstick corrects:**

- **The density ranking is not an artefact.** In T-060 I had dismissed the MIS2 legs
  as micro-scalpers whose roster rank only comes from an almost-empty slot-days
  denominator. Wrong: under the arm's exit rule they reach **+5.5 to +8.1**
  residual at t = 3.5 to 9.5. The 26.07. selection picked the right legs.
- **TSM1 SHORT was the wrong recommendation.** Represented across two rounds on the
  basis of volume (107 signals/day, live, only out because the slot cap never binds)
  and never measured on quality: **−0.72 at t = −4.58**, negative under both yardsticks.

**What holds under both yardsticks** — and therefore stands: TSM1 SHORT and EPD3 SHORT
are negative. The T-032 park of EPD3 stands; the EPD family has no edge in any generation.

**The actual insight:** edge and volume are decoupled on the SHORT side.
MIS2 delivers 0.9–6.3 signals/day at residual +7, TSM1 107/day at −0.72, EPD3 337/day
at −0.38. There is no high-volume SHORT leg with a positive edge, except AIM2
(already in the roster) and ROM1 (re-forwarder, double counting). **More volume with the
existing legs can only be bought at the cost of quality** — the cap problem from T-060
cannot be solved via the SHORT supply, only via the grandfather cohort.

Verdict with all limits: `staging_models/replay/short_leg_trail_verdict_t062.md` —
in particular that the MIS2 means sit at n = 47–132 and are likely
fat-tailed (the sign and ranking hold, the magnitude not yet).
12 DB-free pins, 5 mutations documented. **No code change to any bot.**

## [2026-08-01] Bot 40's live turnover measured against the study (T-2026-KYT-9050-047)

The question from T-042 phase C: does live turnover systematically exceed the simulated one? If
so, the slot arithmetic of the PR #198 study (mean 285 / p95 498) would be too optimistic **and**
the fee load higher than the 0.10% taker round trip the 49 204% were calculated with.

**Answer: no — and the trigger for the question was a bootstrap artefact.** The "~80 trail fires
per hour at ~460 open positions" are exactly 80 fires in **1.2 hours on 26.07. between
19 and 20 UTC**: the first shadow cycle mirrored an already-running book all at once, those
mirrors inherited a peak above the activation threshold and fired on the first poll. In
live operation it is **4.0 fires per hour**, busiest single hour 21.

**Holding time live is if anything LONGER than simulated.** Median 6.00h across the 999 closed
live positions, with the 96 open ones right-censored **[6.71; 7.40]h** — against **6.59h** from
the same study, mix-matched to the live leg counts. The study headline's 4.6h is
a median **over legs**, not a median over trades: there MIS2-168h SHORT (3 live mirrors) counts
as much as MIS1-72h LONG (370). For the five largest legs — 65% of the book — the arm holds
1.24× to 1.64× longer than the simulation. Both measurement biases here run in the direction of
"live is faster" (censoring after 5.6 days; the 24h time stop, which the study does not know),
and the result still comes out the other way.

**The slot arithmetic was too pessimistic, not too optimistic.** Occupancy live mean 126 · p95 221
· max 291, settled over the last 48h at **mean 106** — against a roster-matched expectation
of **251.6** (the study's mean 284.6 includes the ROM1 legs, meanwhile excluded as re-forwarders,
with 33 seats; mean occupancy is a sum of indicator functions and therefore exactly
additive, p95 is not). The Cornix cap of 500 was never within reach. The cause of the gap is the
**intake** — 195 positions/day live against 365 simulated —, not the turnover: the live bot has
four admission filters the simulation did not have (one mirror per symbol, 240s freshness,
symbol cooldown, exposure cap), plus the ROM1 exclusion and legs that are not
LIVE in the meantime — EPD1 SHORT, the study's second-largest leg, has not mirrored a single time.

**Turnover per occupied slot-day — the mix-robust measure and the unit the fee accrues
in:** live 1.405 against 1.291 (study aggregate) resp. 1.146 (mix-matched) = **1.09–1.23×**.
Fee accordingly **0.141% against 0.129% per slot-day**. The share under fee sits at 38%
against an expected 25%, but comes entirely from `SOURCE_CLOSED` (77%) and `SL_HIT` (100%) —
i.e. from the tape, not from turnover frequency; `TRAIL` sits at 0%, which is by construction
(an armed trail closes at the earliest at 0.9 × 2.0% = 1.8%).

**The resolution suspicion is real and quantified: around 20 minutes.** The study evaluates on
15m candle extremes with a strictly prior peak, the bot on 10s prices. Instead of asserting that,
the new tool replays the **imported** study rule (rule 7, no second implementation) on
**the same** live mirrors: on the arm's own exits (n = 586) the 15m exit lands
**median +0.33h, p95 +0.63h** after the live exit, in **10%** of
cases even **earlier** (a wick the 10s poll never printed), in 17% in the same candle.
Slot cost of the finer grid: **≤ 33.1 slot-days = +4.7%** (a lower bound — 325 censored
cases are not counted). Price difference at the same candle: **+0.02 percentage points per trade**.

**New: `tools/trailing_live_vs_study.py`** (read-only, built on `trailing_arm_report.py` and
`trailing_slot_budget.py`) + 17 DB-free pins in `backtest/test_trailing_live_vs_study.py`,
report at `docs/T-2026-KYT-9050-047-live-vs-study-report.md`. Two fallacies are pinned in it,
because the first version of the evaluation made both: a median over closed rows only reports
the arm as faster than it is (hence the censoring interval), and a replay that only reaches the
live exit shoves 88% of the arm's exits into a "the study would have held" — the trigger sits
almost always in exactly the candle a flush window excludes. That is why `same-bar` is its own,
named outcome.

**Recommendation to the operator (#T52-3): keep `act = 2 %`.** The hypothesis that would have
motivated a change is refuted; the one measured deviation path costs ≤5% slot-days. And
for the direction of a later change: **lowering** `act` would not use the free capacity,
it would enlarge it (shorter holding time → less occupancy). The empty half of the channel
is an intake topic, and the bottleneck is not capacity anyway but yield — the live book
stands at −906 percentage points net, cause is the tape (T-054), not a leg defect.
## [2026-08-01] Bot 40 intake analysis: the bottleneck is the exposure cap, not the window (T-2026-KYT-9050-060)

Operator task: the trade count should rise, but **nothing gets changed before a
complete analysis is in hand**. That paid off exactly — the obvious measure would have
been the wrong one.

**The reflex was the freshness window.** The rejected signals sit at p10 = 243s,
p90 = 256s against a 240s cutoff, **707:24 LONG-heavy** — a wall, not an
age profile, the same pattern as back at 180s. A 300s window would admit 706 of them.

**The binding bottleneck is a different one.** A candidate must pass five stages, and only
two leave a DB row — measuring against the DB alone therefore systematically sees
the wrong gate. Reconstructed from the fleet log: `EXPOSURE_CAP` fires on **mean 3.2 → 6.0 →
6.6** candidates per cycle with a rising trend, `SLOT_CAP` on the other hand **not
once** in three days. The book sticks at **+42 to +52** skew against the ±50 ceiling; the
LONG headroom sits consistently between **0 and 8**. LONG candidates are therefore already
rejected *after* they pass the freshness test — a wider window only shifts rejections from
`PREEXISTING` to `EXPOSURE_CAP`.

**The identity that flips the recommendation:** the cap limits the *difference*, so with the
ceiling engaged `Kapazität = 2 × min(LONG, SHORT) + Cap` — currently 2 × 21 + 50 = **92**.
Every additional SHORT position raises the LONG ceiling by one. **The SHORT side throttles
the total volume**, not the LONG side, where the conspicuous rejections sit.

**Recommendation, ordered by impact:** (A) **Add TSM1 SHORT to the roster** — 66 signals/day,
live, density 525, discarded back then **solely because of the slot cap**, which has never
bound since; capacity ~92 → ~150. (B) Re-evaluate the grandfather cohort: **28 of the 30
mirrors are LONG** and permanently occupy **28 of the 50 units** of LONG headroom, i.e. 56%
— the decision made the same day was made without this number. (C) The window **afterward**,
as a quality rather than volume measure: `admit()` sorts by leg density, a 300s window gives
the same LONG budget roughly five times as many candidates to choose from. (D) Do **not**
raise the cap — T-052 measured that the one-sided LONG book was the account damage.

**Adverse selection ruled out:** the rejected LONGs deliver mean +2.39% on the source trade
against +1.28% for the admitted ones (t ≈ 1.3, not significant) — the 240s cutoff does not
select the better signals.

New: `tools/trailing_intake_audit.py` (read-only, log + DB) and the verdict
`staging_models/replay/trailing_intake_verdict_t060.md` with the honest limits — above
all, that the log gates measure **pressure**, not counts: rejections repeat
in every 10s cycle, "mean 6.6" means "6.6 candidates are currently pending," not "6.6
signals/day lost." 13 DB-free pins, 5 mutations documented. **No code change to the bot.**

## [2026-08-01] SL backfill executed + grandfather cohort stays (T-2026-KYT-9050-058)

Two addenda to the merge of PR #218 — no code, just record.

**The SL mark backfill has run** (operator sign-off Michi, after the merge as agreed):
**67 rows, Σ −387.3%**, mean −5.78%, median −5.18%, worst −12.73%, not a single one
refused. Independently re-checked rather than trusting the script: all **90** `SL_HIT` rows now
carry a mark, Σ **−559.3%** — exactly −172.0 pre-existing plus −387.3 backfilled. No
row booked as a win (the wrong-side guard would have refused it), and the rejection rows
(`PREEXISTING`, `SHADOW_CARRYOVER`, `ENTRY_NOT_FILLED`) stayed untouched NULL. This
finishes repairing the reporting defect from T-049/T-053: a sum over `close_mark_pct`
no longer reads a factor of 3 too optimistic, and specifically no longer on the losses.

**Operator decision on #T54-3: the grandfather cohort keeps running.** The cutoff
`TRAILING_BOT_TIME_STOP_SINCE=2026-07-28T14:00Z` stays, there is no cleanup wave. Since that
is the status quo in the code, **no** behaviour change follows deliberately — the entry records
that the alternative was examined and rejected, not overlooked. Accepted price: 30
mirrors with a median age of 108 h, none of them armed, Σ −81 % open book, one blocked
symbol slot each; their exit now runs only via SL or fleet close.

## [2026-08-01] Trailing arm report + SL mark backfill (T-2026-KYT-9050-054)

The bot 40 evaluation had run ad hoc until now. Two join traps ended an entire
analysis run with a confidently wrong verdict — both belong in the repo as a
contract, not in a scratchpad:

- **`closed_ai_signals.id` is its OWN sequence, not `ai_signals.id`.** A join via
  `trailing_positions.src_signal_id` looks plausible and is almost pure noise: of
  4611 rows joined that way, only **9** even had the same symbol. The result was
  "holding would have delivered +2 600 837%" and an inverted verdict. The source trade is
  now matched via (symbol, model, direction) + time, and **every match is checked
  against the entry price** (3% tolerance — the mirror buys at market up to 240 s later).
- **`closed_ai_signals.open_time`/`close_time` are naive in PG local time (+03)**, while
  `trailing_positions.opened_at` is `timestamptz`. Normalising the mirror side with
  `AT TIME ZONE 'UTC'` shifts it by −3 h and matches nothing (2 of 461).
  The comparison is against `opened_at::timestamp`. `candles.open_time` is tz-aware, so the
  benchmark path is unaffected.

**New: `tools/trailing_arm_report.py`** (read-only). Realized including SL reconstruction,
open-book mark-to-market, counterfactual **only on the arm's own exits**
(`TRAIL`/`TIME_STOP`; for `SOURCE_CLOSED`/`SL_HIT` the arm equals holding by construction, and
including them dilutes the measured effect towards zero), split into resolved and
still-open — the resolved subset is biased towards fast closes, i.e. stop-outs, and
flatters the arm if read on its own.

**Market context is not incidental here.** A few days after go-live, the market went into a
dump. Without it, the book reads as LONG −644 against SHORT −5 percentage points and suggests
exactly one conclusion — turn off LONG. The report therefore carries an equal-weighted altcoin
index (median hourly return across the coin universe; median, so a fresh listing with
+300% doesn't become the market itself) and attributes every trade against the index movement
**of its own holding window**. Result across the live series: the index fell **−8.3%**,
the LONG side realized −596 against **−858 market-implied**, i.e. **+262 residual** —
the LONG legs beat the market, they did not lose. The SHORT side sits at
**−101 residual**, below what the falling tape gave it for free. Attribution with
beta = 1; alts typically run hotter, so the market share is a **lower** bound and
the leg residual an upper bound.

**New: `tools/backfill_trailing_sl_marks.py`.** Repairs the **67** SL rows from before
the T-053 fix (up to 2026-07-30 07:50) that carry `close_mark_pct = NULL` even though the
fill is known exactly: **Σ −387.3%, avg −5.78%, median −5.18%, worst −12.73%**.
A `SUM(close_mark_pct)` without them reads **−188 instead of −575%** gross — a factor of 3, and
optimistic on exactly the losses. Value from `core.trailing_state.mark_pct`
(rule 7, same source as the live path and the report), assumption **fill at stop level with no
slippage** = optimistic edge. Dry run is the default; writing requires `--apply` **and**
`--yes-write-live-db`, runs in ONE transaction, repeats the guard in the `UPDATE`
`WHERE` (a mark the bot books itself between preview and write wins) and rolls
back if the row count hit differs from the preview. A stop that reconstructs into a
**gain** is refused rather than written — that can only mean
`sl` sits on the wrong side of `entry`.

**Side finding, independent of the exit rule:** the grandfather cutoff keeps **30** mirrors
exempt from the time stop (median age 108 h, max 126 h, **none of them sharp**, Σ −81%). They
can structurally never be trailed and each block their symbol's only mirror slot.

31 DB-free pins (19 + 12). All nine core assertions individually proven via mutation test
(SL reconstruction removed, entry guard off, attribution sign, index median→mean,
first observation as return, direction sign in the backfill, wrong-side stop written,
guard removed from the `UPDATE`, candidate query widened) — every mutation was caught.

## [2026-07-30] Trailing bot: book SL hits with a mark (T-2026-KYT-9050-053)

Reporting defect, self-inflicted in T-049: the SL hit was booked with
`close_mark_pct = NULL`. The reasoning at the time — "the book shouldn't assert a value
nobody measured" — applies to a missing **market price**, not to an
SL: there the fill sits at the stop level, and the level is right there in the row.

**Consequence:** the worst exits were missing from every sum. Across the clean series (from
cutoff 2026-07-27 15:32:16), that was **66 hits averaging −5.78%** (median −5.14%, down to
−12.73%), Σ **−381%**. A query over `close_mark_pct` therefore showed a net **−186%
instead of −575%** — a factor of 3 too optimistic, and precisely on the losses.

- The mark comes from `core.trailing_state.mark_pct(entry, sl, is_long)` — the same source
  as in live trailing (rule 7). Assumption deliberate, and as in the study, for hard stops:
  **fill at stop level, no slippage** — a gap realizes worse, so the value is
  the optimistic edge.
- Legacy rows without `sl` (pre-T-049) stay NULL: there the level is unknown, and guessing it
  would be exactly the mistake the old reasoning was trying to avoid.
- **No behaviour intervention:** `post=False` stays (Cornix closes on its own), exits, roster
  and gates unchanged.

**Side fix in the test runner that explains three mismeasurements:** the standalone runner only
caught `AssertionError`. A pin that fails via **crash** (TypeError on a None)
tore down the entire run — all later pins went unreported, and a
`grep "^FAIL"` came back empty, which reads like "all green". This is exactly what made a
correct pin look toothless three times in T-049/T-053. It now
catches `Exception` and reports a crash as a failure.

61 pins. All three assertions individually proven via mutation test (NULL fallback,
inverted direction, missing legacy-row guard) — with an assertion that the mutation takes.

**Open, to be released separately:** backfilling the 66 historical SL rows would be a
write query against the live DB (hard rule 1) — only with SELECT preview and explicit
release, not part of this PR.

## [2026-07-30] Bot 40: freshness window recalibrated to 240 s — the candle-cycle wall (T-2026-KYT-9050-052)

After the 180-s fix, the shorts flowed (11/2 admitted/rejected), but the LONGs stayed
locked out: **139 rejected longs in 6 h with p25–p90 = 184–193 s** — not an age
distribution but a wall right behind the boundary. Cause: the candle-cycle legs (MIS1-72h, TD_1H,
AIM2, SRA2 — the LONG side of the roster) have a deterministic ~3:10 min pipeline latency,
the tick legs (shorts) only ~95 s. **`TRAILING_BOT_MAX_AGE_SEC` default 240 s** covers both
families; pins extended (the 190-s case must be admitted). Effective after fleet restart (Michi).

## [2026-07-29] Bot 40: freshness window 30→180 s + ROM1 removed from the roster (T-2026-KYT-9050-052, operator go-ahead)

**Operator finding "too few shorts are getting through" confirmed and root cause measured:** the
chain signal → orchestrator → `ai_signals` insert genuinely takes 30–120 s (median 95 s) — the
T-051 window of 30 s therefore discarded ~85% of genuine roster signals as PREEXISTING (~130 in
12 h, only ~1.5/h admitted). The conspicuous ~3h group was exclusively ROM1 (re-forwards
with original open_time). Fix: **`TRAILING_BOT_MAX_AGE_SEC` default 180 s** (covers the
measured latency; market entry + SL/TP1 barrier continue to carry the T-051 protection) and **ROM1
LONG/SHORT explicitly removed from the roster** (`EXCLUDED_AS_DUPLICATE` — double-counting finding;
until now the window filtered out ROM1 only by accident, which would become unreliable with the
wider window). Boot log now counts `len(ROSTER)` (31). Pins updated to the new contract (the 95-s
case = admitted, 600 s = not; ROM1 exclusion pinned). Expectation: admissions ×5–7, on both sides;
the cap regulates the balance. **Effective after fleet restart (Michi).**

## [2026-07-29] Trailing arm: SL-cap question answered — −5% cap makes things massively worse (T-2026-KYT-9050-052)

Operator question (trigger CHRUSDT at −160% margin, source SL 12.2% = −243% @20x): should the
SL be capped at 5% movement going forward? Measured: **42% of all trades dip below −5%, of which
29.7% end up positive and 42.1% better than −5** (avg of the dippers −2.74 vs. the realized −5.00) —
the recoveries are real and large. Rule result: the cap costs **−33% net on the deployed
configuration (18 930 → 12 687) and even worsens MaxDD** (588 → 653). On the
pure trail −42%. → **Rejected.** The CHR pain is a legacy-book phenomenon (the time stop
caps the never-sharp positions in time under the new regime); the lever against single-position
margin erosion is position size. Tie-break of the stop rules sharpened to SL-first (review finding
PR #206, 2 new pins, 21 total). Details in verdict addendum 6.

## [2026-07-28] Trailing arm: mover question answered — do NOT filter coins with ±50%/24 h (T-2026-KYT-9050-052)

Operator question (trigger COTI +57.9%/24 h): should such coins be ignored above a percentage
threshold? Measured (`tools/trailing_book_health.py`, 24h pre-move causal per entry, buckets per
direction + 4 gate variants): **movers are the arm's best trades, per trade** — pump shorts
+2.96/trade (MIS edge, n=297), dump shorts +9.11 (n=41), even pump longs +3.97 under the
trail; the only toxic cell (LONG into a fresh dump, hold −3.53) is neutralized by the trail
(+0.38). All four gates (abs 30/50%, chase 20/50%) lose net without a DD gain →
**rejected, no filter.** Buckets thin (n=41–73) but consistent; details in verdict addendum 5.

## [2026-07-28] Bot 40: grandfather cutoff for the time stop (T-2026-KYT-9050-052, operator decision)

**Operator decision, Michi:** the legacy book keeps riding, on its own explicit risk, toward its
natural SL/TP — the data situation (SOURCE_CLOSED avg −4.8% vs. time stop at ~−2.4%)
was seen and deliberately overruled. Implementation: `TRAILING_BOT_TIME_STOP_SINCE` (default
**2026-07-28T14:00Z**, a fixed cutoff date instead of process start, so a later restart doesn't
silently exempt a new cohort) — the time stop only applies to mirrors opened from then on.
This removes the legacy-book cleanup wave on restart; the exposure cap and trail apply
unchanged to everything. Pin `test_grandfathered_legacy_book_rides_past_the_time_stop`; the
existing time-stop pins patch the cutoff date (real-time fixtures would otherwise precede it).

## [2026-07-28] Bot 40: time stop 24 h + exposure cap ±50 (T-2026-KYT-9050-052, operator go-ahead Michi)

**Rebuild following the T-052 verdict** (rule choice: best density per bound capital at 800-USD
starting capital, 1 channel): the trail act=2 stays, plus two strictly causal guards:
- **Time stop** (`TRAILING_BOT_TIME_STOP_H`, default 24): a mirror that never crossed the
  activation threshold is closed at market (`TIME_STOP`). Decides only on the
  peak level as of NOW — a mirror that turns sharp during the deadline poll belongs to the trail
  (boundary semantics pinned). Flood protection `TIME_STOP_MAX_PER_CYCLE=25`: the legacy-book
  cleanup after the restart spreads over minutes instead of clogging the FIFO outbox.
- **Exposure cap** (`TRAILING_BOT_EXPOSURE_CAP`, default ±50): new entries on the overhang
  side are rejected (reason `EXPOSURE_CAP`, logged in bulk) — a net guard,
  an admitted counter-trade re-opens the cap (pinned).
6 new pins in `backtest/test_trailing_close_bot.py` (time-stop population, causality
boundary case, rate limit, cap semantics). **Effective only after fleet restart (Michi).** On the
first cycle after that, the time stop clears out the never-sharp legacy book (~150 mirrors, 25/
cycle) — deliberate cleanup of the losing book. Sizing recommendation (Cornix, operator):
m ≈ equity/400 at p95 ≈ 130 slots; 2-channel expansion from ~2000 USD equity after a fresh
run under cap 1000.

## [2026-07-28] Trailing arm: CORRECTION — look-ahead in the breakeven rules, recommendation revised (T-2026-KYT-9050-052)

**The be+ts results of the two previous entries are withdrawn.** The time stop in
`exit_breakeven` checked whether it turned sharp over the ENTIRE trade lifetime instead of up to the
deadline — late winners escaped the stop the live bot would have set (look-ahead;
found while porting the rule into the bot logic, before money was allocated to it).
Fixed causally + pinned (`test_breakeven_timestop_is_causal_for_late_armers`), run 8:
be5+ts24 drops from 58 994 to **7 004**, be5+ts24@1000 from 53 068 to **3 843** — the entire
lead was the artifact. The be family and the channel-scaling curve are rejected;
the trail/ts/cap/hold numbers were always causal and stand unchanged.

**Revised recommendation** (operator context: 800 USD available, 1 channel, sizing by
occupancy): **trail act=2 + time stop 24 h + exposure cap ±50** — per bound capital slot
the best rule (278 pct-pts/avg slot = 1.9× pure trail) at absolute MaxDD 588 (1/7 of the
trail); p95 occupancy 130 allows ~3.8× larger positions within the same margin budget.
Before the 2-channel expansion (~from 2000 USD), a fresh run under cap 1000 (candidate trail act=5).
Details: verdict addendum 4.

## [2026-07-28] Trailing arm: 3-channel re-check — the second channel is the jump (T-2026-KYT-9050-052)

Run 7 (`cap=1500`): be5+ts24@1500 = 56 639 (+3 571 / +6.7% vs. 2 channels; 98%
admitted), hold@1500 = 53 113 at 3× MaxDD. Scaling curve 500→1000→1500→∞:
34.5k → 53.1k (+18.6k) → 56.6k (+3.6k) → 59.0k. **Recommendation stays 2 channels + be5+ts24**
— the third channel buys +6.7% for 50% more exposure capacity and a third
integration target; the capital-neutral alternative is larger sizing across two channels.

## [2026-07-28] Trailing arm: two-channel scenario — be5+ts24 @ 1000 wins (T-2026-KYT-9050-052)

**Operator idea:** split trades across 2 channels → 1000 slots. With least-loaded assignment
that's exactly a global cap of 1000 (run 6, `run_total_cap(cap=1000)`). **Result: be5+ts24@1000 =
53 028 final equity** (90% of the uncapped potential, 95% of the trades get a slot)
— +39% vs. today's trail (38.2k) with a healthy book (+3.27%), +23% vs.
hold@1000 (43.2k) at 40% of its MaxDD (5 676 vs. 14 193); monthly +11.0/+14.7/+11.3/
+9.6/+6.8. **Final priority-upside recommendation: two channels + be5+ts24** (ratchet from +5%
on entry + 24h time stop + slot caps). Rebuild sketch in verdict addendum 3; rebuild/channel/sizing/
restart Michi-gated (#T52-3).

## [2026-07-28] Trailing arm: deployable priority comparison under the 500-slot cap (T-2026-KYT-9050-052)

**Operator question:** priority upside — who had the best performance March–July at period end,
computed comparably? Runs 4+5 (`tools/trailing_book_health.py`, now 29 rules, 16 pins):
breakeven variants (be2/be5, ±cap) and **all major candidates under the hard
Cornix 500-slot cap** (`run_total_cap`) plus equal-capital columns (net/avg slot, DD/avg slot).

**Result (deployable):** 1. Trail act=2 (today) 38.2k — the best absolute number, but the
structurally sick book (−2.73%, 78% underwater). 2. **be5+ts24@500 34.5k** — ~90% of that at the
lowest MaxDD of the major rules (4 124), the healthiest book of the measurement series (+3.36%)
and the smoothest monthly path; uncapped even 59.0k ≈ hold. 3. Hold@500 26.8k (the cap costs
hold 54%). The be2 threshold is too early (21.8k), directional caps choke off the breakeven approach.
**Recommendation priority upside: be5+ts24** (ratchet from +5% on entry + 24h time stop + existing
slot cap); details + rebuild sketch in verdict addendum 2. Rebuild/restart Michi-gated (#T52-3).

## [2026-07-28] Trailing arm: x-sweep, SL trailing, and market-regime gates — addendum (T-2026-KYT-9050-052)

**Live event:** the clean market-entry book separated within ~9 h from avg −0.23% to
−2.50% (95% underwater, 199:3 LONG) — the structural mechanism, confirmed live. On
operator instruction, bot 40 was parked at 05:29 ("stop sending stops") and
unparked again at 05:43 ("new trades can go out"); in between it was verified read-only that the
restart is safe (1 SOURCE_CLOSED, 0 immediate trail exits).

**Eight new rules in `tools/trailing_book_health.py`** (now 23, 15 DB-free pins): x-sweep
(20/30%), time-stop+cap combination, SL trailing (breakeven ratchet from +2%, alone and with
time stop), book-feedback gate (own book as a regime sensor), and BTC direction gate.
Answers to the operator questions: (a) "closing too fast" does NOT hold for the return x —
x=20/30% loses on both axes; anyone who doesn't want to cap runners takes the SL trailing:
**be2+ts24 has the healthiest book of all rules at +2.85%** (28.8k net). (b) Market-regime
gates are **dominated** by the exit rules (the feedback gate chokes off, the BTC gate loses to the
time stop) — consistent with the ROM/HMM/SOFT history. (c) Dump protection: **time stop 24 h +
cap ±50 = MaxDD 588** (7× better than the live trail). Recommendation + numbers:
`staging_models/replay/trailing_arm_verdict_t052.md` (addendum). Rebuild stays Michi-gated.

## [2026-07-27] Trailing arm: exit rules measured on the open book — verdict (T-2026-KYT-9050-052)

**Trigger:** bot 40's open book separated on its own within a day (measured ~19:30:
128 LONG / 5 SHORT, avg −1.84%, 91/128 negative — the hold arm of the same legs: 789/324,
shorts at avg +3.90%). The trail only turns sharp above peak > 2% and, by construction, can
**only close winners**; the arm trailed away its own short hedge
(88 × TRAIL @ avg +3.13%). Realized sums look good in the meantime — that was the methodological
hole in PR #198.

**New:** `tools/trailing_book_health.py` measures every exit rule on both sides: realized
(net, density/slot-day) AND open book (counts per direction, avg mark, underwater share,
equity MaxDD = realized + open MTM; daily time series in the JSON). 15 rules across March–July,
44 144 roster trades, **ROM1 excluded** — its re-forwards accounted for 10 334% of the
49 204% expectation from PR #198 (double counting); without ROM1 the same trail expects 39 116%.
Pins: `backtest/test_trailing_book_health.py` (9 tests, DB-free).

**Verdict** (`staging_models/replay/trailing_arm_verdict_t052.md`): no `act` heals the book
(act=2/5/10 → avg mark −2.73/−1.96/−0.89%, never positive) — a winners-only exit necessarily
produces a losers-only book. But there are rules that can, against realized return:
**time stop 24 h** on never-sharp trades (book −1.17%, best density 1.431, MaxDD −31%,
price −11k net) and **exposure cap ±50** (MaxDD 683 instead of 4 377, but only caps it).
Hard stop, portfolio trail and short-only trail are measured and rejected. The underlying problem
remains: the arm runs additively to the hold arm on the same account and preferentially holds
its losers twice over. Recommendation to Michi: time stop 24 h in bot 40 OR park the arm and keep
pursuing T-041 as a partial close in the fleet (dominates hold on both net AND MaxDD).
Read-only session, no intervention in the running bot.

## [2026-07-27] Trailing bot: entry at market instead of at the source entry (T-2026-KYT-9050-051)

**Measurement of the first clean live hours:** of 24 mirrored signals, only **5
(21%)** filled. Checked against 5m candles: for **15 of the 18**
cancellations, the market had **never touched** the source entry — it ran away and didn't come
back. That's not a bug, it's inherent in the process: by the time the bot sees a signal, the
triggering move has already happened.

**Why this damages the experiment:** the arm was thereby trading a selection it generates
itself — only trades whose move retraces. That's not a neutral subset,
it's presumably the worse half. The 49 204% expectation from PR #198 assumes tradable
trades across all legs.

- **Entry = current market** at mirror time (operator decision Michi). Fills virtually always,
  both arms trade the same signals.
- **SL and targets keep their absolute prices.** They are S/R levels; shifting them along would
  detach them from the levels, and the SL is meant to be the same catastrophe stop as in the
  hold arm. The R:R shifts slightly — the honest price of the later entry.
- **Plausibility barrier:** if the market is already beyond TP1 or beyond the SL, it is not
  mirrored. Otherwise the position would be at target or stopped out in the same instant — not a
  trade, just a fee.
- **Window 90 s → 30 s.** It no longer protects the entry's reachability, but
  the **currency of the decision**: a signal ten minutes old, entered at today's market,
  is a different trade from the one in the hold arm.
  is a different trade from the one in the hold arm.
- **No more `Close` on the SL** (operator note, Michi): Cornix holds the stop as an order on the
  exchange and closes it itself. We only book the exit afterwards — our own `Close` would be
  redundant and would assert an exit we didn't trigger. To support this, the SL is now carried
  along in `trailing_positions`.
- The market entry fills by construction, `filled_at` is set on insert. This also removes
  the false-cancellation rate of the fill check (3 of 18 were wrong: the market HAD touched the
  entry, our 10-s sample just missed it, while Cornix fills on every tick).

52 pins. All four new assertions individually proven via mutation test, each with an assertion
that the mutation actually takes hold at all.

**For T-2026-KYT-9050-047:** the measurement series restarts with this deploy. Everything before
it is either phantom (before 03:51) or selection-biased (03:51 up to here).

## [2026-07-27] Trailing bot: tight mirror window + real fill tracking (T-2026-KYT-9050-050)

**Finding (Michi, live):** Cornix had never opened some trades because the entry wasn't
reached — and the bot had already sent the close anyway. `ENAUSDT` MAX1 SHORT
was posted with `CMP Entry 0.08867` while the market stood at `0.09000`: Cornix waited
for a pullback that never came, while our book was busy trailing the position out in the meantime.

**Scope:** across the open book, **18 of 101** mirrors sat more than 1% away from the market
(median 0.40%, max 2.13%). The booked trailing exits for these positions are phantom —
and this book is the basis for the A/B comparison against the hold arm.

**Cause:** the age limit from T-048 was set to 15 minutes. It was chosen that wide on purpose,
so a fleet restart (~5 min) wouldn't swallow signals — but in that time the market runs
far enough that the posted entry no longer connects.

**Two fixes, both required by the operator:**

- **Window to 90 s** (`TRAILING_BOT_MAX_AGE_SEC`, previously 15 min). The poll runs every 10 s,
  so a fresh signal is normally seen within one round; the entry is thus effectively at market.
  Price: signals during a fleet restart drop out — deliberate, since
  a trade Cornix never opens is worse than one we skip.
- **Fill tracking.** A mirror position only counts as open once the market has **touched** the
  entry (`filled_at`); before that it is neither trailed nor closed. The check is
  deliberately **direction-agnostic**: the price must reach the entry from the side it was on
  at mirror time (`mirror_price`). It therefore makes no assumption about how Cornix
  treats LONG and SHORT differently — that couldn't be verified from here anyway.
  If the fill doesn't happen within `TRAILING_BOT_FILL_TIMEOUT_MIN` (10 min), the row is
  closed as `ENTRY_NOT_FILLED` and a `Close` is posted, so no stale order is left sitting
  in Cornix that fills days later after all.

Legacy rows without `mirror_price` still count as filled — shutting down roughly 100 live
positions on a suspicion would be the worse mistake. The numbers BEFORE this commit are not
usable for evaluating T-2026-KYT-9050-047.

44 pins (7 new). All three assertions individually proven via mutation test — **with an
assertion that the mutation actually takes hold at all**: a mutation loop without this check had
previously delivered a false-negative "pin has teeth".

## [2026-07-27] Trailing bot: symbol cooldown after a close (T-2026-KYT-9050-049)

Finding from the trade audit after ~3 h of live operation (Michi): between a `Close <SYMBOL>`
and a new entry on the same symbol there was **no waiting period at all** — `XTZUSDT` close and
new entry in the SAME second (SRA2 SHORT → MIS1-72h LONG), `ENAUSDT` 3 s apart
(ATS2 LONG → MAX1 SHORT), both with a direction change.

The outbox delivers strict FIFO per channel, so Cornix is guaranteed to get the close first.
But that only holds up to the Telegram boundary: Cornix then places **two market orders in
opposite directions almost simultaneously** on Binance. If the close hasn't settled there yet
when the counter-position opens, the trailing close flattens the new position right back out
again. That didn't happen (the book checks out: 123 entries = 123 positions, 21 closes =
21 closed, **no** close against a second open position) — but it was luck.

**Fix:** a symbol stays locked for `TRAILING_BOT_SYMBOL_COOLDOWN_SEC`
(default 60 s) after a POSTED close. New rejection category `SYMBOL_COOLING` alongside
`SYMBOL_HELD`/`SLOT_CAP`, counted in the per-cycle summary line. Only posted closes trigger
cooldown — a shadow close never sent a command. The window is computed by the DB against its
own `NOW()` (TZ contract R3).

**Learned from the mutation test:** the first pin version checked `admit` and
`read_cooling_symbols` individually — both correct, but disconnecting the wiring in
`open_mirrors` turned **no** pin red. The same class of gap the PR #198 review had flagged
in `leg_stats`/`simulate`: primitives pinned, composition not. A
wiring pin has been added (37 pins) and proven via mutation.

## [2026-07-27] Trailing bot: per-coin price fallback disabled (T-2026-KYT-9050-048)

Operator instruction, Michi, right after go-live: on a failure of
`get_live_prices_batch()`, bot 40 **no longer** falls back to `get_live_price(symbol)` per position.

**Why:** the fallback makes an HTTP call **per open position per poll**. At the ~285 concurrent
positions expected for act=2%, on a 10s cadence that's ~28 requests/s against
`fapi.binance.com`. A ban hits the **entire** fleet; a trailing exit delayed by one poll
costs almost nothing. In the first live hour, the batch failed 2× —
inconsequential at 20 positions, not at 285.

- **Bot 40 only.** `core/live_price.get_live_price` stays unchanged — the detectors and
  bots 11/22/24/25 keep using it.
- No batch price ⇒ the position is skipped this cycle (the rule "no price means
  no decision" was already in the bot). A warning summary line per cycle makes the gap
  visible — a silent lapse in trailing would be worse than a loud one.
- **Special case, source gone:** the mirror is closed anyway (holding would be
  wrong), but with `close_mark_pct = NULL` instead of a made-up 0.0 — the book shouldn't
  assert a value nobody measured.
- Ban guard pinned (32 pins): the symbol must no longer be reachable in the bot, the source
  must contain no more calls, and a dead batch must produce zero decisions and
  zero writes across 30 positions. Proven via mutation test.

## [2026-07-26] Trailing bot: clear out shadow leftovers when going live (T-2026-KYT-9050-042)

When switching from shadow to live, **460 open mirror rows** stood in
`trailing_positions`, all with `posted = FALSE`. They correspond to no position in
the channel — they were never posted — but each still blocked its symbol (at most one
position per symbol) and a slot. The live channel would therefore have started with 460
phantom slots and rejected genuine signals.

**Fix:** on startup, the bot closes open, never-published rows as
`SHADOW_CARRYOVER` — but **only in live mode**. In shadow operation, exactly these rows
are the book; clearing them there would erase the record on every restart. Only on
startup, not in the poll: during ongoing live operation, unpublished open rows never
arise in the first place (the insert and the outbox row sit in the same transaction).

This also requires no manual intervention in the live DB — the rule lives in the code, is
pinned (30 pins), and applies to every future shadow→live switch.

## [2026-07-26] Trailing bot: do not mirror the legacy book + log flood (T-2026-KYT-9050-042, addendum)

Two defects that **only the first shadow run on the live VPS** exposed — both structurally
invisible in DB-free tests, both found because the live gate defaults to OFF
and the bot could run along for 33 minutes without consequence.

- **The legacy book got mirrored.** On startup, the bot treated **all 465 already-open**
  source trades as new arrivals — some days old. The mirror inherits the
  geometry of the source signal (entry, SL, TPs), but Cornix fills at the **current**
  market: for a trade three days old, the trailing arm therefore no longer measures the same
  trade as the hold arm, and that exact comparison is the whole point of the bot. With the gate
  open, this would have been 465 bad entries in one sweep. **Fix:** age limit
  `TRAILING_BOT_MAX_AGE_MIN` (default 15 min, deliberately covering a restart window);
  older source trades get a closed `PREEXISTING` row — the same lock that
  protects an already-trailed-out trade from re-entry, so they are also never again
  considered a new arrival. The same class as **P2.7** in the AI monitor ("no
  retroactive scoring of legacy trades after a process restart"). Age is computed by the **DB**
  (`NOW() - open_time`), not Python — `ai_signals.open_time` is naive/PG-local, and a
  Python-side comparison would be the same offset bug as in the TZ cluster P2.1–P2.6.
- **Rejections flooded the shared watchdog log.** Every rejected candidacy was logged
  individually, and it repeats on **every 10s cycle** for as long as the source trade
  is open: measured **34 691 lines in 33 minutes** (~870 per cycle, extrapolated to
  ~1.5M/day) — the logs of every other bot would have drowned in it. **Fix:** a
  summary line per cycle with counters per reason, individual cases to DEBUG. The numbers stay
  visible (no silent capping).

4 new DB-free pins (now 28); both fixes proven via **mutation test** — reintroducing the
defect turns exactly the associated pin red.

## [2026-07-26] Trailing close bot with its own channel (T-2026-KYT-9050-042, Phase C)

The trailing arm from T-041 becomes its own fleet process: **`40_trailing_close_bot.py`**
mirrors the 33 legs selected in PR #198 into its **own Telegram channel** and
closes them there via trailing close (operator operating point, Michi: activation **2%**,
retracement **10%**), instead of letting them run to SL/TP. Michi wires Cornix to it himself.
This runs the trailing arm live against the hold arm of the existing fleet — **without
a single existing bot changing its behaviour**. Spec: `docs/T-2026-KYT-9050-042-trailing-bot-spec.md`.

- **The bot decides nothing about entries.** It reads `ai_signals` (foreign table,
  SELECT only), mirrors whatever the fleet posts anyway, and makes exactly one
  decision of its own: when to close. Its only write permissions are
  `telegram_outbox` (own channel) and its own table `trailing_positions` —
  it **never** closes a foreign trade and **never** writes to `ai_signals`.
- **Two admission gates, both from the data, not from caution:**
  (1) **At most one position per symbol.** Cornix's `Close <SYMBOL>` acts symbol-wide
  (`core/config.py:123`) — two positions on one symbol would mean one's trailing exit
  flattens the other. `28_signal_orchestrator.py:1562` resolves the same
  conflict by deferring the close; here that would be wrong, because the timely
  exit is the whole point.
  (2) **Its own slot cap (500).** The chosen p95-safe selection has an
  occupancy peak of **2001** = 4× the Cornix cap. Without its own control,
  Cornix would decide at peak which ~1500 trades get rejected; the bot therefore caps
  itself — **by leg density**, i.e. by the same criterion that drove the selection.
  Rejections are logged, not swallowed.
- **Write first, then post.** The entry only goes into the outbox once the
  `INSERT ... RETURNING id` has actually produced a row — the same pattern as
  `DELETE ... RETURNING` in the AI monitor (P2.8). The first version posted first and
  caught the conflict afterwards with `ON CONFLICT DO NOTHING`; since the trailing exit
  typically fires **while the source trade is still running**, the same
  `ai_signals` row looked new again on the next poll — the bot would have re-posted the entry
  every 10 seconds until the fleet closed the source trade. Found in the
  core review of its own code, fixed, and covered by a pin that turns red if the defect
  is reintroduced (verified via mutation test).
- **One source for the Cornix block.** `core/signal_post.build_cornix_block` is
  **extracted** from `post_ai_signal` (a pure extraction, byte-identity pinned) and is used by
  both sides. A private copy in the bot would be the path by which the entry2 removal
  from PR #197 reaches one publisher but not the other.
- **One source for the trailing semantics (rule 7).** `core/trailing_state.TrailingState` is
  the streaming form of `core.wave_exit_sim.trailing_tp_trigger`; a parity pin ties
  the two together (same mark sequence → same trigger index). The running peak is persisted —
  without it the trail would re-arm after a restart below a peak already reached, and
  exactly the evaporated gain the bot is built against would never close.
- **Default is shadow, doubly so.** `TRAILING_BOT_LIVE_POSTING=0` **and** an unset
  `CH_TRAILING`: the bot runs, tracks and logs fully, but writes no
  outbox row. A deploy alone posts nothing.
- **24 DB-free pins** (`backtest/test_trailing_close_bot.py`) across all 12 acceptance criteria,
  including the three where the alternative is a money bug: one parsable message
  per entry (hard rule 4), symbol uniqueness, and "a deploy posts nothing".

**Follow-up fixes to the slot-budget analysis** from its own core reviews (same PR), all **without numeric impact** (the report
was deterministically re-rendered from its JSON — not a single value moved):
the "honest limits" now name the occupancy peak of 2001 and the candle mask that isn't
flush on the exit side; the holding-duration column is labelled as "across legs" (it is a
median of leg medians, not trade-weighted); four new pins cover `leg_stats` and the
candle windowing — the two functions with the highest defect risk were unpinned.

**Not part of this PR (Michi):** channel ID into `.env`, live-gate flip, fleet restart,
wiring Cornix to the channel. See `AUDIT_TODO.md` #T42-1..#T42-5.

## [2026-07-26] Single-entry posting — entry-2 line removed fleet-wide (T-2026-KYT-9050-042)

Operator decision (Michi) based on the data from T-043/T-044/T-045: the
DCA add-on at `entry2` **hurts** — arm B (`entry1` only, original SL) raises the
per-trade Sharpe on **15 of 17** bots with a solid sample (median drag
B−A **+0.073**), consistent across S/R, pump/dump, momentum and pattern, and
stronger on the longest window (MIS1-168H, 2.5 months) (0.06 → **0.16**). Arm B now goes
live — **exclusively via posting**:

- The `🏦 Entry 2` line disappears from the Cornix block in `core/signal_post.py`
  (the shared poster) and the inline builders in 7, 9, 10, 11, 12, 13, 14, 15, 16,
  18, 25, and the manual `/open` handler. Cornix therefore fills the full size
  on `entry1`. The info/HTML messages from 11 and 25 correspondingly show only
  one entry line now.
- **The geometry stays untouched:** `entry2` is still computed, the SL
  stays behind it, `ai_signals.entry2` is still written (the monitor,
  the realized report and the outbox harnesses read it). No model, no threshold,
  no gate touched.
- **ROM1 (`28_signal_orchestrator`) is the exception** and keeps posting its `entry2`:
  one of the two bots without DCA damage (drag +0.014), whose add-on
  gives measurable MaxDD protection (10 → 14.5% without it). ROM1 computes its `entry2`
  itself anyway and doesn't hang off the upstream bots; its gating parser
  (`parse_cornix_signal`) doesn't require an entry-2 line — now explicitly pinned.
- `backtest/test_single_entry_posting.py`: 7 DB-free pins (source pins per module,
  ROM1 carve-out, exactly one entry line in the actually-built Cornix text,
  `ai_signals.entry2`/SL unchanged) + `test_parse_cornix_signal_single_entry`.
  Spec: `docs/T-2026-KYT-9050-042-single-entry-spec.md`.

**Operator side (not in the code):** splits the margin across the entry targets
for Cornix, runs the fleet with half instead of full position size after this change —
the Cornix configuration has to follow suit. The whole thing only goes live with the
fleet restart (Michi's decision).

## [2026-07-26] Slot budget for the trailing bot channel (T-2026-KYT-9050-042, Phase C pre-stage)

Michi: Cornix caps a channel at **500 simultaneously open trades** (the cap applies
PER channel — the new trailing bot channel has its own budget). So before building the bot,
the question: which legs earn a slot there? **Read-only.** New tool
`tools/trailing_slot_budget.py` scores per **(leg, direction)** against
`shadow_gate.leg_status` — return *and* slot demand, hold vs. trailing each — and
fills the channel greedily by net density, with exactly computed concurrency of the
selection. Report `staging_models/replay/trailing_slot_budget_live.{md,json}`.

- **The selection metric is return per occupied slot-day**, not per-trade Sharpe: under
  a hard cap, every leg displaces another. Under hold behaviour,
  `MIS1-72h LONG` alone claims ~283 of the 500 slots.
- **Two measurement traps, both pinned** (`backtest/test_trailing_slot_budget.py`, 11 DB-free):
  (1) The inherited `trail_capture` logic scores peak and trigger on the SAME candle —
  a candle that both encloses the entry and both arms and breaks the trail, and the trade
  flies out on candle 0. Harmless for T-041 (only the return was used, optimism
  documented), fatal here, because the exit TIMING is what counts the slots: the first
  run gave a median holding duration of 0 h across ALL 43 legs. Fix = strictly prior peak.
  (2) A scale-free trail is a micro-scalper: "10% retracement from peak" fires even
  on a 0.5% peak. Fix = **activation threshold** (`core.wave_exit_sim` has the
  parameter, T-035/T-046 left it at 0), here swept instead of assumed.
- **Values are net** per repo convention (0.10% taker round trip, `tools/audit/step4_results.py`).
- **Fleet-wide finding:** trailing beats holding only from act≈10% (73 897 vs 64 478 net);
  at act=0 it destroys a third of the return. **Finding for the channel:** there
  the low threshold wins, because it frees up slots faster — **act=2%: 33 legs,
  49 204% net at avg 285 slots / p95 498**, i.e. 76% of the fleet return with 23% of the
  slots. Conservative act=1%: all 37 legs, p95 426, 46 064%.
- Not admitted (would have blown the cap): `BB_1H LONG`, `BR2H LONG`,
  `EPD3 LONG`, `TSM1 SHORT`.

Honest limits in the report: 15m resolution (the DCA-faithful T-035 harness is the next
tightening), slippage not modelled, today's roster state applied to the whole history,
greedy not provably optimal, and even the p95-safe selection has a
peak of 2001 — in the top 5% of hours, Cornix rejects. Pure analysis; the
selection and the bot build are Michi's decision.

## [2026-07-26] Trailing close finalized on the high-fidelity harness (T-2026-KYT-9050-046)

Michi: finalize the trailing-close finding from T-041 = on the T-035 high-
fidelity harness (5m wick + 10s resolver + DCA-faithful cornix3 geometry) instead of the
first-order 1h reconstruction. **Read-only.** T-035's overlay (a) only scored the
leveraged **sum** (fat-tail/−100% clamp artifact → looked NO-EDGE);
the new `--mode trailing` in `tools/wave_exit_overlay.py` scores it **risk-
adjusted** (per-trade leveraged Sharpe + compounding MaxDD fixed 2%, as in
T-041), hold vs. trailing X-sweep, per max-outbox window.

- **S/R bots confirmed:** AIM2 (n=491) Sharpe 0.19→**0.35**, MaxDD 9.5→**2.8%**;
  SRA2 (n=116) 0.02→**0.15**, MaxDD 11.6→**2.6%**. The T-041 reversal holds DCA-faithfully.
- **Pump/dump NOT:** EPD3 (n=1157) Sharpe 0.15→**0.08** (trailing caps the
  explosive pump winners). → **bot-type dependent** (like entry2-as-SL): reversion/
  S/R benefit, trend/pump does not. Corrects T-041's first-order ranking (which
  still saw EPD3 as a winner).
- Harness extension: `run_validate` gets a `run_overlay` flag + exposes
  `arts`. `backtest/test_trailing_risk.py`: 7 DB-free pins. Reports
  `staging_models/replay/trailing_risk_test_{aim2,sra2,epd3}.*`.

Trailing-close deploy (for S/R bots) = a separate operator decision (Michi). Pure
read-only analysis.

## [2026-07-26] DCA effect confirmed fleet-wide — A vs B across 24 bots (T-2026-KYT-9050-045)

Michi: run the DCA test (arm A=DCA vs. B=single-entry1) on ALL bots with enough
outbox geometry. **Read-only.** New aggregator `tools/dca_all_bots.py`
auto-discovers `telegram_outbox` coverage per `closed_ai_signals.model`
(≥30 entry2 Cornix messages) + max window, calls the merged T-043 harness
(`run_validate` + `analyse_entrysl`) per bot, bundles into **one** ranking. Reason
for per-bot windows (T-044): `closed_ai_signals` has no entry2 → its realized
is already ≈ arm B; the DCA effect lives only in the Cornix text (per-bot retention),
entry2/SL aren't fixed offsets → not reconstructable.

- **DCA hurts on 15/17 solid bots (n≥30), median drag B−A +0.073** — consistent
  across S/R, pump/dump, momentum, pattern detectors (MIS2-8H +0.12, RUB2 +0.11,
  AIM2/TD_1H +0.10, MIS1-72H/168H +0.07, SRA2 +0.07, EPD3 +0.06).
- **Exceptions (neutral):** ROM1 (2507 trades/2.5 mo, drag +0.014 but DCA gives
  MaxDD protection 10→14.5%) + BR1Hv2. entry2-as-SL (C) beats B only on 2/17
  (bot-type dependent). Side finding: BR2H/BR1Hv2 with catastrophic MaxDD (42–62%).
- `backtest/test_dca_all_bots.py`: 8 DB-free pins (drag/verdict/fleet count).
  Report `staging_models/replay/dca_all_bots.*`.

Dropping DCA = a fleet-wide evidenced deploy candidate (with MaxDD surcharge) —
a signal-emission/fleet change, **Michi's decision**. Pure read-only analysis.

## [2026-07-26] Dropping DCA confirmed on a longer window (T-2026-KYT-9050-044)

Follow-up to T-043: cross-checked the "DCA hurts" finding on a **longer period**.
**Read-only**, merged T-043 harness (`--mode entrysl`) on
wider outbox-geometry windows — `telegram_outbox` reaches further back than
the ~7d assumed in T-035 (AIM2 back to 11.07. = 2 weeks; **MIS1-168H back to 14.05. =
2.5 months**, 527 real Cornix trades — the only bot with a month-scale window).
`ai_signals` is the small live table, not a history source.

- **"DCA hurts" is robust and STRONGER over the long period.** AIM2 2 weeks
  (n=600): Sharpe A 0.18 → B 0.27 (identical to the 8-day run). MIS1-168H **2.5
  months** (n=342, several market regimes): Sharpe A **0.06 → B 0.16** (almost 3×) —
  dropping the entry2 add-on holds up over time.
- **entry2-as-SL (C) is bot-type dependent.** On MIS1 (momentum) the clear
  winner on **both** axes (Sharpe 0.22 / MaxDD 13%); on AIM2 (mean-reversion
  S/R) only a drawdown tool. Mechanism: a tight stop@entry2 helps trend bots (a dip to
  entry2 = the trend broke), hurts mean-reversion (a dip turns back).
- Reports: `staging_models/replay/entry2_sl_test_aim2.*` (now the 2-week window,
  supersedes the 8-day run) + new `entry2_sl_test_mis1-168h.*`.

Pure read-only analysis; dropping DCA = a separate deploy candidate (Michi's decision).

## [2026-07-25] entry2-as-SL vs DCA — 3-arm test on the T-035 harness (T-2026-KYT-9050-043)

Follow-up to T-041. Michi's question: the bots trade DCA (entry1 market + entry2
add-on limit, SL behind entry2) — would a single entry with the entry2 level as SL
be better? **Read-only**, on the T-035 high-fidelity harness (5m wick + 10s resolver
+ immutable Cornix geometry, ~7d window, AIM2/SRA2/EPD3). New `--mode entrysl`
in `tools/wave_exit_overlay.py` + an SL selector in `replay_record`; a 3-arm breakdown
on the entry2-present set, risk-adjusted (per-trade Sharpe +
compounding MaxDD, risk helper reused from T-041):

- **A = real DCA** (baseline), **B = single entry1 + original SL**, **C = single
  entry1 + SL@entry2** (Michi's idea).
- **Robust finding (all 3 bots): DCA hurts.** entry1 without the add-on (B)
  raises the Sharpe every time (AIM2 0.19→0.27, SRA2 −0.09→+0.03, EPD3 0.14→0.20) —
  the add-on at entry2 averages down into the losers. **Trade-off:** B increases
  the MaxDD (less averaging = larger single-trade swing) → a risk/return
  trade-off, no free lunch.
- **entry2-as-SL (C): no robust win.** Better than B on Sharpe on none of the bots;
  the drawdown effect is inconsistent (AIM2 16→11% better, EPD3 14→24% worse —
  a tight stop cuts off pump/dump winners).
- `backtest/test_wave_exit_sim.py`: pin `test_sl_at_entry2_is_tighter` (DB-free).
  Reports `staging_models/replay/entry2_sl_test_{aim2,sra2,epd3}.{md,json}`.

Honest limits: ~7d/outbox window, the entry2 set (not all trades publish
entry2), compounding sequential-after-close. The DCA effect is a separate
deploy candidate (Michi's decision) — this is pure read-only analysis.

## [2026-07-25] Wave buildup / trailing close — realized-vs-unrealized study (T-2026-KYT-9050-041)

Follow-up to T-035 (wave-exit overlay, PR #185). Checked Michi's observation: the
curated bots hold trades open for days, realize losses in full, let
gains evaporate. **Read-only, no live intervention.** New, DB-free-tested
tool `tools/wave_buildup_study.py` reconstructs the aggregated open
unrealized wave + realized results from RECORDED trades (`closed_ai_signals`,
report-14 survivor key) + candles — **without** the Cornix geometry (which T-035 capped
at ~7d outbox retention), flat leverage 20x.

- **Phase A (AIM+SRA, full history, `--mode study`):** premise hard-confirmed —
  avg realized +38% vs. avg wick peak +184% (**+146% giveback**); 85% of the losers
  had once stood at ≥+10%, 36% ≥+100% positive. Cooldown idea **refuted** (day 2–7
  after the wave peak not weaker). **Trailing close 10–15% risk-adjusted
  superior:** per-trade Sharpe (lev) **+0.20 → +0.53**, compounding MaxDD
  **74% → 12%** — T-035's "hold wins" was a leveraged-**sum** artifact
  (uncapped fat tails). Report `staging_models/replay/wave_buildup_study_aimsra.*`.
- **Phase B (all bots, 15m, `--mode rank`, 91.547 trades):** per-bot ranking
  hold vs. trailing. **Trailing close 10% raises the Sharpe on 15/17 bots and lowers
  the compounding MaxDD drastically fleet-wide** (MIS 100%→10%, BR 99.9%→35%,
  EPD 83%→7%, Sniper 79%→6%, ROM 63%→9%, AIM 57%→8%); only UFI1 (low-lev,
  SL-capped) + MAX neutral. Report `staging_models/replay/wave_buildup_rank_allbots.*`.
- `backtest/test_wave_buildup_study.py`: 16 DB-free pins (signed move, causal
  trailing capture, Sharpe, compounding + MaxDD).

Honest limits in the report: first-order (no DCA/TP laddering, entry1-only),
20x assumption, wick + trigger optimism, compounding sequential-after-close
(ignores concurrency) → absolute multiples not literal, the ratio + MaxDD
are the signal. Next step: confirm the finalists on the T-035 high-fidelity harness
(5m + 10s + DCA-faithful); **Phase C = trailing bot → Telegram (T-042)**.

## [2026-07-25] Live-root artifacts ATS2 + SRA2-SHORT tracked in git (T-2026-KYT-9050-040)

Five model artifacts that the live main checkout runs from the repo root had never been
committed (previously only promoted on disk). Verified read-only: **not**
gitignored, **byte-identical** (md5) to the `staging_models/` versions (= the
deployable artifacts), and repo convention tracks root-live artifacts
(`sra2_model_LONG.*` was committed, SHORT was not). Now added:

- `ats2_model_LONG.pkl`, `ats2_model_SHORT.pkl` (ATS2 — live per T-037 #5 / bot_results.xlsx)
- `sra2_model_SHORT.json` (+`_calib.pkl`, +`_meta.json`) (SRA2-SHORT — live-intended, xlsx "ACTIVE SHORT")

Pure binary tracking, **no code, no behaviour change** — the commit
only documents the promotion already made (hard rule 2, operator Michi).
ATS2 doesn't go live through this commit, only through the fleet restart.
`coins.json` (runtime double writer) deliberately **not** committed.

## [2026-07-25] Bot-variant archive (phase 2) + stage/compare tooling (phase 3) (T-2026-KYT-9050-039)

Follow-up to T-038 (index, PR #188): phase 2 (D2+D4 archive) + phase 3 (D3
tooling) built on the read-only index. **No live intervention**, read-only
except `model_archive/` (+ `staging_models/` only via `stage --apply`).

- **Phase 2 — `tools/bot_variants/archive.py`** produces, per generation,
  **`model_archive/<family>/<tag>/manifest.json`** (model_id, threshold,
  deployable, features, trained_at, trainer, md5, `source_origin`,
  **`source_commit`** = git SHA of the artifact bytes, lifecycle per direction +
  **`lifecycle_history`** from `git log -S<tag> -- core/shadow_gate.py`,
  provenance, **`code_ref`**) + a generated `model_archive/ARCHIVE.md`.
  48 manifests, deterministic/idempotent (`--check` = no drift).
- **D4 `code_ref`:** symbolic `HEAD` for active generations (logic in the
  current tree), a concrete historical SHA for retired logic
  (`git log -S<datei> -- <script>` — the T-037 anchor, RUB1-SHORT sat at
  `07c8874^`). No volatile HEAD SHA in the manifest ⇒ drift-free.
- **Decision on large artifacts (spec §3 D2):** **reference-based** instead of
  a full copy — all ~48 MB of artifacts are already git-tracked (root+staging),
  the manifest (md5 + `source_commit`) makes every generation byte-exact
  reproducible via `git show <source_commit>:<path>`; a binary copy
  would needlessly double the repo. `--copy-binaries [--max-copy-mb]` (opt-in)
  produces a self-contained export on demand (md5-verified, size skips
  are listed — no silent skip).
- **Phase 3 — `tools/bot_variants/stage.py`:** a live-swap helper in the T-037 pattern.
  Default DRY-RUN prints the plan (artifact→`staging_models/`, code_ref checkout,
  register flip, restart as an operator step); `--apply` copies **only** into
  `staging_models/` (md5-verified), **never** into repo root/live (hard rule 2),
  no restart.
- **Phase 3 — `tools/bot_variants/compare.py`:** generation A/B simulation **DB-free**
  over the existing replay infra (`retrain_from_replay.load_replay` +
  raw `predict_proba` + operating threshold). Metrics per generation: `n`,
  avg/sum `net_pnl_pct`, `win_rate`, `max_drawdown_pct`; winner by avg PnL. Does NOT
  rebuild `walkforward_sim` (DB-bound), only consumes its `*_replay_*.jsonl`.
- **Tests:** `backtest/test_bot_variant_archive.py` (manifest schema, code_ref
  active→HEAD / retired→git-log-S, md5==source, copy size skip, stage plan +
  --apply-staging-only) + `backtest/test_bot_variant_compare.py` (evaluate/MaxDD
  against a known curve, threshold=None fallback, feature-contract error paths,
  winner logic). Guard `verify`+`smoke` green.

## [2026-07-24] Artifact promotion EPD3-LONG + ATB2-LONG to repo root (operator, T-2026-KYT-9050-037)

Operator promotion (Michi, hard rule 2): the two LONG deploy artifacts staged in PR #187
are lifted **byte-identical** from `staging_models/` into the repo root, where the
LIVE loader reads them (the register flip in #187 makes `shadow_artifact_path` → root path):
- `epd3_model_LONG.pkl` (md5 `3375ccf5…`, `optimal_threshold=0.76`)
- `atb2_model_LONG.pkl` (md5 `b8c46fa5…`, `optimal_threshold=0.60`)

A pure file move (no code, no model change) — content identical to the core-reviewed
staging pkls from #187 (md5 parity verified). **Only takes effect with the
fleet restart** (Michi): until then the LIVE loader loads from root, but the running
bot processes don't yet have the artifacts in memory. ⚠ Honest framing unchanged:
EPD3-LONG has ~0 live edge (volume cap 0.76), ATB2-LONG is blind (n=17, 0.60) — operator-
willed live experiments.

## [2026-07-24] Bot-variant index (phase 1) — read-only discovery across all generations (T-2026-KYT-9050-038)

Phase 1 (D1) of the bot-variant index/archive: a **read-only discovery tool**
that brings the scattered as-is state (root/staging artifacts + lifecycle register +
fleet-script mapping + git) per **bot × generation** into a deterministically
regenerable join view. Foundation for the later live swap
(T-037 pattern) and A/B sim of every generation. **No live intervention**, no
DB writes, no model promotion — reads/joins only (hard rules 1/2/7).

- **`tools/bot_variants/index.py`** — joins `core.bot_catalog`
  (tag→family/script), `core.shadow_gate` (lifecycle per (tag, direction) +
  `SHADOW_ARTIFACTS`), artifact meta (sidecar `*_meta.json` or embedded),
  the filesystem (root/staging/archive), and git. Output:
  **`docs/bot_variants_index.md`** (human-readable) + **`model_archive/index.json`**
  (machine-readable). CLI: `--write` / `--check` (drift) / `--stdout` /
  `--no-model-meta` (fast, without joblib).
- **Deterministic/idempotent:** all collections stably sorted, **no**
  `now()`/randomness in the output lines ⇒ running it twice = byte-identical
  (`--check` finds no drift right after `--write`).
- **No silent drop** (like `bot_catalog`): unknown tags (no fleet script)
  and unclassifiable model files are **counted and listed**
  (currently 6 unclassified root artifacts, including `qm_xgboost_model_v2.pkl`,
  `master_trade_model_xgboost_combined_signals.pkl`).
- **Shared filenames made visible** (root collision hazard): the index flags
  `rub2_model_LONG.pkl` (RUB2+RUB3) and `epd2_model_LONG.pkl` (EPD2+EPD3) — the
  EPD3-LONG-on-legacy-filename case named in the spec.
- **48 generations** captured (RUB1–4, EPD1–3, MIS1/2 × 4 horizons, ATS1_Robust/
  ATS2, ATB1/2, BB/TD/QM per timeframe, ABR2 (on disk as `bt2_model_*.json`!),
  SRA1/2, AIM1/2, MAX1/2, FIF1, PEX1, FMR2, ROM1/UFI1/TRM1 …) including `md5`,
  location, `deployable`/`threshold` (where meta exists) and a conservative
  `code_ref` (`HEAD` when live; exact git SHA per legacy generation follows in phase 2).
- **`core/bot_catalog.py`:** additive public helper **`family_for_tag()`**
  (reverse of `script_for_tag`, same pretty_name+longest-prefix logic) — the
  index's family grouping; no change to any existing caller's behaviour.
- **Tests:** `backtest/test_bot_variant_index.py` (family/script/lifecycle,
  unknown-tag counting, idempotency, shared filenames, md5==source) +
  `family_for_tag` cases in `backtest/test_bot_catalog.py`. Guard `verify`+`smoke`
  green. Phase 2 (archive layout + manifests + code_ref SHAs) and phase 3
  (stage/compare tooling) are follow-ups.
## [2026-07-24] Deploy EPD3-LONG + ATB2-LONG live (operator override, blockers #3/#4) (T-2026-KYT-9050-037)

Lifts the LONG legs #3/#4 deferred as **BLOCKED** in the T-037 spec on
**explicit operator decision (Michi, "deploy as required")** — despite
the data pointing the other way. Read-only DB diagnosis beforehand (VPS): the blocker in both
cases was `optimal_threshold=None` (no training operating point).

- **Edge analysis EPD3-LONG (T-036 playbook, 2578 real shadow trades, 10 d):** mean net
  **≈0%/trade** (median +0.83%, WR 78%, but fat losers), and the confidence
  **anti-discriminates** (corr −0.04; higher threshold → worse expectancy).
  **No deployable threshold edge** — the opposite of SRA2-SHORT (T-036). Deployed by
  operator decision as a **volume-capped live experiment**.
- **ATB2-LONG:** only **n=17** closed shadow trades → statistically undecidable;
  threshold **set blind at 0.60** (ATB1s 0.80 is an incompatible model, not
  transferable).

Implementation (class A gate + B code + staged artifacts, root-promote + restart = Michi):
- **`core/shadow_gate.py`:** `("EPD3","LONG"): LIVE` + `("ATB2","LONG"): LIVE` (explicit,
  defense-in-depth); the respective opposite direction stays SHADOW. `SHADOW_ARTIFACTS["EPD3"]
  ["LONG"]` → **`epd3_model_LONG.pkl`** (challenger-distinct, prevents the collision with
  the legacy EPD2 loader slot `epd2_model_LONG.pkl` → double post, analogous to the SHORT fix
  from PR #185).
- **Bot 14 (`14_ai_atb_bot.py`) rewire:** `_emit_atb2_shadow` → `_emit_atb2` now routes ATB2
  through `post_ai_signal_gated` (bot 12's `_emit_ats2` pattern). Previously shadow-only (the
  `if not is_shadow(...)` guard would have silently swallowed a LIVE leg → nothing posted).
  **has_open guard** before the gated post (the LIVE branch has no has_open/cooldown
  check; the 1h breakout candle stays the newest for ~1 h → otherwise a double post per scan, rule 4).
  Live Cornix to `CH_ATB_TARGET`. Bot 10 (EPD3) already routed through the gated router → only
  register + artifact (+ 2 stale comments fixed).
- **Staging artifacts (hard rule 2):** `staging_models/epd3_model_LONG.pkl` (a copy of the
  epd2-LONG retrain, `optimal_threshold=0.76`) + `staging_models/atb2_model_LONG.pkl`
  (`optimal_threshold=0.60`), each with a `threshold_provenance` note (operator-set, not
  trained). Models byte-identical (only threshold + meta note). **Staging only** — the
  root move (= live) is Michi's step.

Verified DB-free: `backtest/test_shadow_gate.py` (new `test_t037_epd3_long_atb2_long_
deployed` + threshold-artifact test), new `backtest/test_atb2_deploy.py` (static
rewire guard: gated router + has_open + LIVE-and-SHADOW gate), guard `verify` 24/24 +
`smoke` green, ruff/format clean. **Only takes effect once Michi root-promotes the two
artifacts + fleet restart.** ⚠ Honest framing: EPD3-LONG has ~0 live edge, ATB2-LONG
has no data basis — both are operator-willed live experiments, not evidenced edges.

## [2026-07-24] Fleet reconfig from bot_results.xlsx — RUB1 revive + gate flips + retires (T-2026-KYT-9050-037)

Implements Michi's wish column from `bot_results.xlsx` against the live register state
(PR core, class A gate flips + class B bot-13 code). The DB deletes (#6/#7) and
all live effects (restart/deploy) deliberately stay outside this code PR
(hard rule 1 / escalation §6). DB diagnosed strictly read-only.

- **#1 RUB1 revive (bot 13, `13_ai_rub_bot.py`).** Both directions back on the
  **original legacy reversion models**, live under the original tag **RUB1**:
  - `RUB_LONG_TAG="RUB2"` → **`RUB_TAG="RUB1"`** (reverts the T-030 LONG rename);
    `module_tag = RUB_TAG` for both directions (no more direction-dependent
    meta.model_id lookup).
  - **Legacy SHORT branch reactivated** (reverts the PR-#9 removal): `MODEL_SHORT`
    loads `short_reversion_model.joblib`, scores raw predict_proba on the 9
    rub features (NO funding) at `REVERSION_THRESH_SHORT=0.85` — **parity** with
    the pre-PR-#9 RUB1 logic (git `07c8874^`), no newly invented threshold.
  - The **RUB2 retrain** (`rub2_model_SHORT.pkl`, artifact contract via
    `load_artifact`/`maybe_reload`) is removed → **benched**: RUB2 stays in the
    register as `SHADOW` (both directions, for documentation). The RUB3/RUB4-LONG shadow
    challenger (`_emit_rub3_shadow`) runs **unchanged**.
  - **Transitional dedup** (rule 4) stays intact: the active-trade check +
    cooldown additionally bind `RUB_LEGACY_TAG="RUB2"`, so across the tag change
    RUB2 → RUB1 no open RUB2 position gets double-posted (has_open guard).
  - Exactly **ONE Cornix message** per signal (unchanged); the legacy models are
    **md5-unchanged** (`0227bb4a…` / `16ca3711…`, rule 7).
- **#2 RUB3-SHORT → SHADOW** (`core/shadow_gate.py`): `("RUB3","SHORT"): SHADOW`
  added (RUB3 really only emits the LONG shadow; SHORT is inert → register hygiene).
- **#1 register (defense-in-depth):** `("RUB1","LONG"): LIVE` + `("RUB1","SHORT"): LIVE`
  entered explicitly (a deliberate exception to the "only list NON-live" rule), so
  the revived live generation is guaranteed to route live, while RUB2 next to it is SHADOW.
- **#6/#7 retire (register part):** `AIM2-TOPN` ("too thin") + `ATS1_Robust`
  ("synthetic only") into `_RETIRED_TAGS` → `leg_status == RETIRED` for both directions.
  Purely register/report classification; **no DB delete** (a separate operator step,
  preview-first). The AIM2-TOPN emitter (bot 15) doesn't consult the register — actually
  switching it off needs the config gate flip + restart (Michi).
- **#8 main channel — clarified (for the record):** no live emitter anymore (the
  classic detector via T-020 retired, replaced by **MAX2**). No register entry needed.
- **Blockers (deferred, documented in the PR body + `AUDIT_TODO.md`):** #3 ATB2-LONG
  (`optimal_threshold=None`, not in root) and #4 EPD3-LONG (filename collision with
  legacy EPD2 → double-post hazard) — both need a deployable, challenger-distinct
  root artifact first (hard rule 2, Michi's decision).

Register assert (spec §5): `leg_status('RUB1','LONG'/'SHORT'), ('RUB2','LONG'),
('RUB3','SHORT')` → **`live live shadow shadow`**. Verified DB-free: extended
`backtest/test_rub_tag.py` (tag==RUB1, both directions, legacy models loaded, ONE
Cornix message, threshold parity, md5 assert) + new T-037 tests in
`backtest/test_shadow_gate.py`; regression guard `verify` (24/24) + `smoke` green.
**Code only** — taking effect (RUB1 live, RUB3/retires silent, ATS2 load) only with the
fleet restart (Michi). Blockers/operator steps in `AUDIT_TODO.md`.

## [2026-07-23] SRA2-SHORT optimal_threshold=0.58 set — flood hazard closed (T-2026-KYT-9050-036)

Closes the deploy hazard flagged in T-033/034: SRA2-SHORT had been promoted to LIVE,
but `staging_models/sra2_model_SHORT_meta.json` carried
`optimal_threshold: null` (a stale training verdict, label source `closed_trades3`
dead since Feb) → LIVE would post on EVERY S/R-SHORT candidate (Cornix flood)
and the strict LIVE loader `build_contract` crashes on `float(None)`. Read-only
threshold analysis on the **real 188 SRA2-SHORT trades** (2026-07-15..23,
`ml_predictions_master.confidence` ↔ `closed_ai_signals` outcome, `weighted_move_pct`
−fee): SRA2-SHORT is positive at **every** threshold (+0.9%/trade, 88% WR —
confirms the T-032 audit's +1.00% shadow; the `deployable=false` was a
training artifact, not the live reality), the confidence barely discriminates.
Operator decision, Michi: **0.58** — cuts the noise-adjacent low-conf tail
(~78% volume, +0.71% mean/88% WR, frequency ~23→18/day), keeps the full edge.
Only `optimal_threshold` + a provenance line changed in the staging meta (the value
comes from live trades, not from the dead-label training); `val_stats` stays as the
historical training verdict. Pinned via `backtest/test_sra2_short_threshold.py`
(meta = 0.58 float + `load_shadow_artifact('SRA2','SHORT').threshold == 0,58` end to end).
**Staging only** (hard rule 2) — go-live = Michi pulls artifact+meta+calib to root
+ restart. Caveat: 8d window/one market phase = a starting operating point.

## [2026-07-23] Deploy preconditions for T-033 — MIS1 revive + EPD3 staging + SRA2-SHORT diagnosis (T-2026-KYT-9050-034)

Interactive, operator-accompanied session (Michi live). Makes the three deploy
preconditions flagged in T-033 actionable — DB strictly read-only (`set_session(readonly=True)`,
SELECTs only), staging artifacts only (no root move, hard rule 2), no restart/
deploy/env flip. Report: `staging_models/replay/deploy_preconditions_t034.md`.

- **MIS1 revive (package 1) — EXACT restoration, no retrain (operator decision, Michi).**
  Bot 11 loads the unchanged MIS1 artifacts (`pump_model_*_final.pkl` +
  `threshold_*_final.pkl`, repo root) back in — PARALLEL to MIS2 under tags `MIS1-*`.
  Feature feed via `add_advanced_features(include_legacy=True)`: the 71-column
  superset covers the 67 MIS1 features EXACTLY (0 missing verified across all 8 models)
  AND the 63 clean MIS2 features (additively neutral, ONE feature build per coin). Geometry
  stays generation-faithful (`_mis_geometry`): MIS1 = `calculate_smart_targets` on both directions
  (immediate CMP entry); MIS2-SHORT keeps the DUMP_RULES bracket. MIS2 emission stays byte-neutral
  through the shared `_post_mis_live_leg`/`_process_mis_candidates` path. Lifecycle in the
  `shadow_gate` register: MIS1 removed from `_RETIRED_TAGS`; good legs default to LIVE
  (MIS1-24H/72H/168H LONG + MIS1-8H SHORT), weak ones SHADOW — reviving exactly the MIS2 legs
  parked by T-033, exactly ONE live generation per (horizon, direction) (no Cornix
  double post). Two old bugs deliberately NOT reproduced (hard rules): Cornix block in
  HTML (rule 4) and full targets instead of `[:5]` (P2.31).
- **EPD3-SHORT staging (package 3):** `epd3_model_SHORT.pkl` (root) copied to
  `staging_models/` so the shadow loader finds it (verified) — the EPD3-SHORT park
  now produces real shadow history instead of silent nothing.
- **SRA2-SHORT diagnosis corrected (package 2, no code):** the T-033 "flood hazard"
  concern was a misdiagnosis — the realized shadow history shows the ungated leg
  as PROFITABLE (+1.06%/trade, n=232, matches the audit's +1.00%×222). The −0.079% val
  signal came from the dead `closed_trades3` Feb label source. A threshold is neither
  needed nor determinable from the data (base rate 90% WR); a funding gate saves no
  edge (broadly positive across all zones), only trims volume. → deployable ungated, the open
  question is Cornix volume (~29 posts/day) — an operator decision.

Tests (DB-free): `backtest/test_mis1_revive.py` (load + threshold + 67-feature coverage +
geometry branching), `test_shadow_gate.py::test_mis1_revive_lifecycle`, `test_mis_tag.py`
adapted to the shared processor. ruff + mypy clean; MIS/shadow/signal suites green.
Both core reviews PASS (z-code-reviewer APPROVED after 1 LOW fix, z-spec-compliance PASS).
Deploy (MIS1 live, SRA2-SHORT/EPD3 artifact moves, fleet restart) = operator decision.

## [2026-07-23] Fleet reconfig following audit T-032 — lifecycle flips per bot × direction (T-2026-KYT-9050-033)

Operator-approved rewiring of the money path based on the T-032 realized
audit — a pure code change: no deploy, no live DB write, no artifact root
moves (all deploy preconditions flagged to Michi). Report:
`staging_models/replay/fleet_reconfig_t033.md`.

**Core finding (mechanism mapping):** the plan was conceived as a pure `shadow_gate` register
flip, but only part of the legs run through `post_ai_signal_gated` (there
the flip alone suffices). The majority of the legs to be parked (BR/BB/QM pattern, SRA1, RUB2,
legacy EPD2, ABR2, MIS2) post **legacy-direct** and didn't consult the gate at all
— a pure register entry would have been a silent no-op there. Solution: a
central, purely additive router `core.signal_post.route_legacy_leg` (default LIVE ⇒
byte-identical; SHADOW ⇒ monitored `ai_signals` without Cornix; SILENT/RETIRED ⇒ no-op),
which the legacy bots (7/9/10/11/13/18/24/25) call at their emission point. The
`shadow_gate` register is thereby the single lifecycle source for both gated and
legacy bots.

- **Promote SHADOW→LIVE:** ATS2 (both legs — bot 12 `_emit_ats2_shadow`→`_emit_ats2`
  rewired onto `post_ai_signal_gated`), SRA2-SHORT (bot 9 already gated).
- **Park SHORT→SHADOW (LONG live):** BR2H/BR4H (bot 7), BB_1H/BB_4H (bot 25), QM_1H
  (bot 24).
- **Park LONG→SHADOW (SHORT live):** MIS2-24H/72H/168H (bot 11), EPD3-SHORT (bot 10,
  register flip).
- **Fully →SHADOW (both legs):** EPD2, MIS2-8H, RUB2, SRA1, BB2_4H, BR1D, BR1Hv2,
  ABR2. "Main channel" already retired (T-020).
- **Revive SILENT→SHADOW:** FIF1 (bot 33 rewired from `post_ai_signal` to `post_ai_signal_gated`
  so SHADOW produces monitored trades).
- **Already in the target state (RETIRE/SILENT):** AIM1 (retired), ATS1/ATB1 (silent) —
  no code change.

**Deploy preconditions (Michi, rule 2 — NOT part of this task):** ATS2 +
SRA2-SHORT artifacts from `staging_models/` → repo root; ⚠ SRA2-SHORT has
`optimal_threshold=null` → set a threshold before go-live (otherwise Cornix flood on
every S/R-SHORT candidate); copy the EPD3-SHORT artifact into
`staging_models/` for real shadow history; fleet restart. **Not actionable (flagged):** MIS1 revive
— bot 11 no longer loads any MIS1 generation (`kein Legacy-Fallback`); a plain
`leg_status` flip would be a fake with no emitter → its own rebuild task.

Verification: `test_shadow_gate.py` (register goldens deliberately refreshed to the T-033 state
+ new `route_legacy_leg` tests, 23 green), `test_signal_post_gated.py` (the SILENT example
FIF1→ATS1 updated), `test_bot_catalog.py`/`test_published_targets.py`/
`test_signal_orchestrator.py`, ruff + `ruff format --check` (0.15.17) clean. Pre-existing
red `test_fleet_definition::test_watchdog_view_is_unchanged` (the watchdog golden is missing
bots 36–39) NOT touched by this task.
## [2026-07-23] Wave-exit overlay — high-fidelity sim + rule-based auto-close (7d preview, T-2026-KYT-9050-035)

Interactive, read-only VPS session to check Michi's wave hypothesis (closing at the
unrealized wave peak instead of letting it evaporate). Two phases, pure
analysis, no live intervention. New pure engine + harness + DB-free tests, artifacts
(markdown + JSON) into `staging_models/replay/`.

**Phase 1 — high-fidelity replay harness + validation (`core/wave_exit_sim.py`,
`tools/wave_exit_overlay.py`, 10 DB-free tests):** plays a signal (multi-entry DCA,
SL, laddered TPs) through wick-aware candles (fill → laddered TPs + trailing SL,
monitor-faithful). The % math stays in `core.realized_pnl` (rule #7). Three discovery
findings drove the design: (1) **geometry only from the immutable Cornix text**
(`telegram_outbox`) — `ai_signals.sl` gets overwritten by the monitor during trailing.
(2) **`ticker_10s` is unsuitable as an exit detector** — a ~40s snapshot with gaps
(coverage median 0.25), misses ~81% of SL-touch events → a pure tick sim distorts
realized ~2.7×. (3) **5m candles are complete & wick-aware** (12× finer than the
1h live monitor) → chosen as the touch backbone; the 10s ticks remain only as an
order resolver for SL-vs-TP ordering within a 5m candle.
- **Validation (AIM2, 673 legs, matched immutable Cornix geometry vs. recorded
  `closed_ai_signals`):** targets_hit **exactly 97.9%**, win/loss **99.3%**, unlev
  per-trade **corr 0.994**, leveraged sum +9%. The harness is faithful (deliberately *finer*
  than the 1h ground truth). Coverage 673/1285 limited by outbox retention
  (skews the set toward more recent trades — disclosed honestly).

**Phase 2 — rule-based auto-close overlay (sweep, L/S separated):** baseline =
hold-to-TP/SL (`cornix3`, real-money DCA/3-TP). (a) Per-trade trailing TP (X% retrace
from the trade MTM peak), (c) portfolio circuit breaker (close-ALL at Y% retrace of
the aggregate wave). Metric = REALIZED locked-in with/without leverage + WR + MaxDD.
- **CORE FINDING (robust across the whole X/Y sweep + both directions):** no overlay
  beats hold on the **leveraged** headline (baseline +8256% vs. (a) 4539–5115% /
  (c) 4165–4718%) — the leveraged sum is dominated by a few fat-tail wave hits, which
  every overlay caps. **Unlevered**, overlays are better (+176% →
  ~245–282%, they cut the underwater tails); (c) pushes the MaxDD wave down
  **~8× smaller** (41.3 → 5.0–6.4) against ~44% less leveraged upside. The
  leveraged loss sits almost entirely in **LONG**. **Verdict: NO-EDGE** on the
  return metric (wave capture = market timing, not caught out of sample —
  confirms T-029/031/032: the edge is direction-, not timing-based); (c) is at best
  debatable as a pure portfolio drawdown safeguard. WR(TP1) under overlays is misleading.

**Reviews + multi-bot extension:** z-spec-compliance = PASS, z-code-reviewer =
ISSUES → all findings fixed (HIGH: `overlay_c` stale-peak — the circuit breaker
didn't reset the peak when the open book emptied → spurious flattening of new
trades; effect on the aggregate ~0, but fixed via a pure
`core.wave_exit_sim.portfolio_circuit_breaker` + regression test; plus CPU/
consistency LOWs). The overlay was then extended to **EPD3 (604 legs)** + **SRA2 (29)**:
NO-EDGE holds robustly on the two meaningful bots (AIM2 674 + EPD3 604 — on
EPD3 the overlays even hurt unlev), SRA2 is below the n≥30 threshold (only
illustrative). (c) pushes the drawdown down ~3–9× across all three. Two report bugs
fixed that made the multi-bot runs correct in the first place: bot-specific
Cornix footer format (`%(AIM2)%` vs. `AI module EPD3` → `%model%`) and the
hardcoded AIM2 core-finding text (now data-driven + a THIN guard).

Read-only (SELECTs only), BELOW_NORMAL, coin-windowed reads. No deploy, no
restart, no artifact root moves. The operator decides on consequences.

## [2026-07-23] Fleet realized-trade audit (DB-direct) + regime-gate edge test — retire candidates (T-2026-KYT-9050-032)

DB-bound (strictly read-only) VPS fleet audit in two phases, pure analysis +
recommendation to Michi (retire = escalation, no live intervention). Two new tools +
DB-free tests each, artifacts (markdown + JSON) into `staging_models/replay/`.

**Phase A — `tools/fleet_realized_audit.py` (11 DB-free tests):** a reviewable
control table of every bot's realized edge straight from the DB, per **day ×
direction × lifecycle (active/shadow/retired)**. Sources: `closed_ai_signals`
(AI) + `closed_trades_master` (classic), deduplicated on the report-14 survivor
key (the 357k duplicate trap). Edge metric = target-staggered unlevered move
(`core.realized_pnl.weighted_move_pct`, correct for laddered-TP bots) net − fee;
leveraged realized PnL + R-multiple (classic, with sl) as secondary; synthetic
LEGACY closes (±2.5%) quarantined; lifecycle from `core.shadow_gate`.
- **Retire candidates (net-negative edge, n≥30):** classic volume bots
  `FastInOut`, `VolIndic`, `5Percent`, `Main Channel` (both directions net
  negative — the biggest bleeds, `FastInOut` alone ≈ −419k% leveraged), the
  pattern/sniper/rubberband **SHORT** legs (`BR1H/BR2H/BR4H`, `BB_1H/BB_4H`,
  `QM_1H/QM_4H`, `BR1Hv2`), `AIM1` (retired, confirmed dead). **Keepers:** `AIM2`,
  `SRA2`, `ROM1`, `MAX1`, `RUB2-SHORT`, `RUB1`, `EPD1-SHORT`, `TD_*`, `MIS1-72h/168h`.
  Core pattern: the losing families have LONG edge and SHORT bleed (direction,
  not regime).

**Phase B — `tools/regime_gate_edge_test.py` (6 DB-free tests):** would a
BTC regime gate save the negative-edge bots? Every trade is bound as-of to the
RULE-recon/SOFT regime (T-029/T-031 infra from `regime_history`); favourable
regimes are learned on the first half of the trades and applied **out-of-sample**
on the second half (no in-sample self-deception).
- **Verdict: NO negative-edge leg is saved (0 RESCUED).** 15 legs bleed in
  EVERY regime (no favourable regime exists → the gate blocks everything), 4 get
  mitigated but stay negative. Positive-edge legs gain only modestly
  (mostly <+0.3%/trade, some at a low kept-fraction). **Core finding: the edge
  is direction-, not regime-based** → the lever is the direction/retire
  decision, not a BTC regime gate (aligns with T-029/T-031, η²≈0).
- **Join limits (honest):** `closed_ai_signals` has no `sl` → no R-multiple for
  AI; targets+lev thin for alt tags → leveraged exact-only; monitor outcomes
  ~63% first-touch-faithful → sign/cohort diffs are more robust than the absolute level;
  the historical per-bot whitelist isn't reconstructable (T-031) → phase B measures
  the regime axis alone (an upper bound), not the live whitelist mechanics.

No fleet code touched, no DB write, no retire/gate flip (pure analysis).
CPU courtesy: `set_low_priority` + a soft headroom check (phases A/B took
priority, VPS at times 100%). Verified: 17/17 DB-free tests green, ruff clean.

## [2026-07-23] SOFT regime-gate counterfactual on real ROM1 forwards — NO-EDGE (churn confirmed) (T-2026-KYT-9050-031)

DB-bound (strictly read-only) VPS follow-up to T-029: measures whether the
SOFT regime timeline that won DB-free there (EMA-smoothed classifier confidence)
delivers a PnL uplift on **real** ROM1 forwards, or only saves churn. New
`tools/soft_regime_counterfactual.py` (+ 11 DB-free tests): reconstructs the RULE
(a port of `apply_debounce._step_debounce`) AND the SOFT timeline (`build_soft_timeline`
from T-029) **directly from `regime_history`** — no candle re-read needed, because
`raw_features` JSON carries `vola_p75/p40` — and buckets real orchestrator
forwards by SOFT-vs-RULE regime agreement.
- **Verdict NO-EDGE for a proven PnL uplift** (churn confirmed): SOFT(hl≈16h)
  cuts RULE switches by **87%** (170→23/30d, reproducing the T-029 whipsaw win
  live); the reconstruction hits `regime_at_open` on 91.8% of forwards. Forwards
  where SOFT≠RULE win **6.0pp less** (56.4% vs. 62.4% TP/SL, p=0.001) — a
  genuine direction signal.
- **Why NO-EDGE anyway:** (a) the WR gap only becomes significant at heavy smoothing
  (hl≥16h, gating ~half the flow), at ≤8h ~2pp/p>0.2; (b) first-touch
  replay PnL is negative in BOTH buckets (agree −0.06% / disagree −0.21%/trade)
  = confirms T-029's η²≈0, you're choosing between losers; (c) **"SOFT disagrees" ≠
  "SOFT would have suppressed"** — that would need the historical whitelist, which
  every analyzer cycle overwrites completely (no history → unreconstructable);
  the only proxy (current snapshot) is circular + contrarian. → **Only a
  live shadow A/B of a SOFT gate can decide this** (a gate decision,
  Michi-gated).
- **Join limits (honest):** prob↔outcome isn't reliably joinable → outcome via
  trade `status` (CLOSED_TP/SL), not realized PnL; `orchestrator_suppressed_signals`
  has no `alt_context` → whitelist re-flip only on the forwarded side; SOFT only smooths the
  BTC axis; the CLOSED_REGIME_CHANGE auto-closes (majority of exits) carry no
  TP/SL label and are excluded.

No fleet code touched, no DB write, no orchestrator/gate change (pure
analysis). A suppressed-§5b number rerun was deferred by the CPU-courtesy gate (VPS
100%) — the verdict doesn't depend on it. Verified: 11/11 DB-free
tests green (RULE port ≡ debounce, SOFT-from-raw_features, as-of no-lookahead,
z-test), ruff clean; both core reviews PASS in the autonomous run (the z-spec gap
on the suppressed side was addressed). Follow-up candidate: live shadow A/B.

## [2026-07-23] GARCH vol-targeting LIVE verdict on real trades (T-2026-KYT-9050-030)

Answers the open half of T-022: **does GARCH vol-targeting help at Kythera?**
Read-only study on the live VPS (SRV02, `cryptodata@localhost`,
`set_session(readonly=True)` + `statement_timeout`, SELECT only) — measures, wires
nothing up. **No fleet/DB write, no artifact promotion, no gate flips, no
live wiring.** New driver `tools/research/garch/t030_live_verdict.py` + report
`T030_live_verdict_report.md` + result JSON.

- **Population:** 16.613 realized trades (only genuine geometry exits; the
  synthetic `LEGACY … (±2.5%)` rows excluded) across the empirically
  confirmed edge-positive bots (AIM2, EPD1/EPD3, MIS1 family, RUB2-SHORT, MAX1),
  318 coins with ≥510 daily candles (46% trade coverage), GARCH forecast as-of entry
  (lookahead-free, shared candle reader) via `walkforward_garch`.
- **Fair test:** `target_vol` calibrated to the sample median forecast (99% ann.)
  → multiplier centered on 1.0 (median 1.00, p10–p90 0.70–1.34) = genuine
  regime reallocation, **not** a uniform deleverage. The naive `target=15%` default
  is a 6.6× size cut (Sharpe-Δ +0.0006) — documented as a sensitivity trap.
- **Verdict: NO-PULL (immaterial MIXED).** Pooled Sharpe 0.1515 → 0.1601
  (**Δ +0.009**), median across 9 bots **Δ +0.013** — an order of magnitude below the
  +0.10 threshold. **No** edge-positive bot passes the test. σ drops (−8%), but
  the mean drops almost proportionally → risk-adjusted flat; win rate invariant
  (sign unchanged). Cause: GARCH forecasts magnitude, not direction — on
  already edge-positive signals, inverse-vol sizing merely reshuffles notional, without
  concentrating capital on the winning trades.
- **Recommendation:** **No gated live-wiring follow-up.** T-022 answered, the idea
  retired cheaply (aligns with the combo-study finding: the edge sits in the regime/
  exit infra, not in the sizing overlay). Correlation layer T-023 stays separate.
## [2026-07-23] Regime-weighting study: soft confidence smoothing beats the live rule, HMM refuted (T-2026-KYT-9050-029)

DB-free research study (`tools/research/regime_switch/`, Stoic/GARCH pattern,
NO-EDGE-tolerant) on the HMM regime thread. Question: does a
probabilistic (HMM) or soft (confidence-weighted) regime timeline reduce
whipsaw + the TREND-hold defect compared to ROM1's live rule (debounce +
§22 hysteresis), WITHOUT losing regime discriminative power? No fleet code
touched, no DB write. The **real** `core.regime_logic` classifiers are
imported (hard rule 7); only `compute_features` (DB read → a pure ccxt-klines
reconstruction) and the debounce state (DB persist → in-memory state machine)
were ported — the latter pinned to the real `apply_debounce` via a fake
`regime_current` connection.
- **Four timelines** over identical, causal features: RAW (no damping) /
  RULE (live baseline) / HMM (3-state GaussianHMM, causal forward filter, no
  intra-block lookahead) / SOFT (EMA-smoothed classifier confidence).
- **Finding (307d BTC/BTCDOM off ccxt):** (1) **RULE ≈ RAW** (450 vs. 491
  switches/30d) — the 5-min-cadence debounce is almost a no-op, only the
  TREND hysteresis dampens. (2) **SOFT dominates RULE monotonically** — half-life sweep:
  more smoothing → simultaneously fewer switches AND better discriminative power, no
  trade-off (hl64: 23 sw/30d = −95%, η²@24h 0.0129 = 4× RULE); a modification to the
  existing rule-based detector, no ML. (3) **The causal HMM refutes
  the thread** — without Viterbi-smoothing lookahead it whipsaws harder than RAW (620)
  and its named states invert (BULL → −255% ann fwd) = the
  transition lag the author himself warns against.
- **Honest limit:** absolute discriminative power stays tiny (η² < 1.5% at every
  horizon); every live RULE state (including TREND_UP) has a negative forward return
  = a vol-badness gradient, not direction. Measures timeline/separation, NOT
  PnL on real bot forwards — that is DB-bound (`tools/rom1_counterfactual.py`,
  VPS). This study is the pre-stage gate. **Verdict: EDGE for SOFT** as a
  churn win, not as a direction edge. Any ROM1 change (bot 28) stays
  Michi-gated.

Verified: `backtest/test_regime_switch_study.py` (4 DB-free tests green —
debounce port ≡ live `apply_debounce`, feature causality, reconstruction
math, metric limits), one full run + half-life sweep (reproducible from
the klines cache), ruff 0.15.17 clean. Follow-up: DB-bound VPS counterfactual
of the SOFT timeline against real forwards.
## [2026-07-22] Stoic-1-2-3 direction module + multi-timeframe backtest (T-2026-KYT-9050-024)

New self-contained research package `tools/research/stoic123/`: translates the discretionary
"Stoic Edge System / 1-2-3 Sequence" into a **deterministic, lookahead-free**
signal generator + a multi-timeframe backtest with an OOS split and an
edge/no-edge verdict. Emits a `date,signal` CSV that plugs directly into the
GARCH harness (`compare.py --signals`, T-022). A direction system (which
direction) — complementary to the GARCH sizing (how much). **No fleet/live/DB code
touched; nothing deployed.**

- **`rules.py` (phase 1)** — EMA/SMA, Wilder ATR, a close-based "meaningful
  break" at k·ATR (no wick), a base/consolidation detector, an **as-of HTF location
  gate** (merge_asof against HTF `close_time`, only fully closed HTF candles).
- **`state_machine.py` (phase 2)** — a causal state machine `WAIT → Step1(Break
  beide MAs) → Step2(Retest+Base, Boundary FIXIERT) → Step3(Boundary-Break+Close
  = Entry)`, stop-and-reverse exit. The **5 distortions** as explicit guards +
  tests (wick-not-close, HTF-invented, boundary-after-break, skipped-retest,
  repaint); prefix stability proves lookahead/repaint freedom.
- **`signals.py`** — position series → `signals.csv` (compare.py contract).
- **`backtest.py` (phase 3)** — ccxt MTF fetch (forward-paginated history),
  a 0.6 OOS split, a 24-combo sensitivity sweep, inline metrics (Sharpe/MaxDD/
  win rate/trades/worst-month), edge/no-edge verdict, an optional direct GARCH
  hookup.
- **Verification:** 29 DB-free tests (`backtest/test_stoic123_*.py`) green; a real
  ccxt run on BTC/ETH/SOL (4h/1d).
- **Verdict (honest):** after the lookahead fix (see below), all three coins are
  **INSUFFICIENT** — OOS Sharpe BTC 0.82 / ETH −0.12 / SOL −0.2, each < 10 OOS trades.
  The strict 1-2-3 sequence is **too rare** on 4h/1d, the marginal edge sits
  at the loose end of the parameter range; **no robust edge on this small sample**.
  Follow-up candidate: a larger coin sample / finer timeframe for more trades.
- **Review finding (HIGH, fixed):** the first HTF-gate version matched against the
  HTF **open** time → an LTF candle read the still-forming HTF candle (distortion #2,
  exactly the trap the module is meant to prevent). Fix = match against HTF `close_time`.
  Empirical proof of the leak's reality: the fix flipped SOL from a
  (leak-inflated) EDGE @Sharpe 0.76 to INSUFFICIENT @−0.2 — the validation
  discipline the module itself preaches. Both core reviews addressed.

## [2026-07-22] GARCH vol-targeting module + validation harness lifted in (T-2026-KYT-9050-021, -022)

New self-contained research package `tools/research/garch/`, ported from the repo audit
`milesdeutscher/garchmethod` (verdict **ADAPT**, MIT — `LICENSE.upstream`
kept). GARCH answers *how much* (magnitude/sizing), never *which
direction* — orthogonal to the signal engine, composed as `signal × size_multiplier`.
**No fleet/live/DB code touched; nothing deployed.**

- **`garch_forecast.walkforward_garch()`** — walk-forward GARCH(1,1) vol forecast,
  lookahead-free (prefix-stable, proven by test). Kythera adaptations vs.
  upstream: a **rolling-window cap** (`max_window`, default 1500; `None` = upstream's
  expanding window), an **injectable `fit_fn`** (the DB-free tests run without
  `arch`), regime calm/normal/storm.
- **`vol_target`** — `size_from_vol`/`size_series` (= `target/forecast`, capped
  `[0.25, 2.0]`, NaN/≤0 → `MIN_SIZE`) + `apply_sizing` (a composition seam, never
  flips the sign).
- **`GarchSizer`** — a stateful per-coin sizer for the live 538-coin path:
  parameter cache + refit only on schedule, reproduces the walk-forward
  forecast series bar-for-bar (parity + refit count tested).
- **`ccxt_data`** — OHLCV → `date,close` contract (replaces yfinance).
- **`compare.py` (T-022)** — fixed-vs.-vol-targeted harness + a `compare_coins`/
  `verdict_from_stats` gate (Sharpe delta + max-DD/worst-month risk axis →
  PULLS/MIXED/NO-PULL/NO-DATA). Timing discipline `next_ret = ret.shift(-1)`.
  `--signals date,signal` CSV = the plug for a `signals.csv` (e.g. Stoic-1-2-3,
  T-2026-KYT-9050-024).
- **Deps:** `arch`/`ccxt` in `requirements-garch.txt`, **NOT** in the fleet's
  `requirements.txt` (lazy imports, the lockfile stays clean).
- **Verification:** 28 DB-free tests (`backtest/test_garch_*.py`) green; a real
  `arch`+`ccxt` smoke test on Binance BTC/USDT (40.6% ann. vol, regime calm, 0.37×).
  Both core reviews PASS (z-code-reviewer: 0 CRITICAL/HIGH, 2 MEDIUM + LOW
  addressed; z-spec-compliance: AC1–AC11 met).
- **Gate/limit:** the *real* Kythera signal verdict (does vol-targeting help at
  Kythera?) is DB-bound (hard rule 1) → runs in a VPS session with
  real signals; here the harness is validated on ccxt prices + demo/proxy signals.
  Live-wiring into a bot is deliberately out of scope (a separate,
  operator-gated task). Correlation layer = T-2026-KYT-9050-023 (backlog).
## [2026-07-22] Watchdog launcher crash (0xC0000005) fixed + outer-net self-heal (T-2026-KYT-9050-025)

The launcher of the "Kythera Watchdog" task died intermittently with
`0xC0000005` (ACCESS_VIOLATION, a native segfault; `logs/watchdog_launch.log`
2026-07-19 20:08 + 2026-07-22 12:50). Consequence: the scheduled task flipped
`Running→Ready`, the spawned fleet kept running detached as orphans, and the
**outer supervision net was gone** — if a bot then dies, nothing restarts it
(task `Ready`, no living watchdog). On 2026-07-22 → ~1h of unsupervised
orphan fleet + manual recovery.

- **Root cause (via `-X faulthandler`):** both crashes carry the same stack —
  `psutil.open_files()` → `main_watchdog._resolve_heartbeat_log` →
  `check_heartbeat`. The native psutil `open_files()` enumeration (handle dup +
  `NtQueryObject`) access-violated on this Windows/Py-3.13 host. A native
  segfault is **not** catchable via try/except → it tore down the entire watchdog.
  Timing (~20 min after start = `HANG_LIMIT_S`) confirms it: the first heartbeat
  resolution per bot after the grace phase.
- **Fix `main_watchdog.py`:** the `open_files()` enumeration now runs in a
  **throwaway child process** (`_probe_open_log_files`). A crash there only reaches
  the parent as a non-zero exit, a hang is bounded by a 10s timeout — in
  both cases the process is treated as *unresolvable → exempt* (like a bot without
  a log). The supervisor can no longer die from this call; behaviour otherwise
  unchanged (mapping-free, `logs/` preference). Additional win: the previously
  unbounded in-process hang of `open_files()` is also eliminated.
- **Fix `launch_watchdog.cmd` (v5→v6):** propagates the Python exit code
  (`set WD_EXIT=%ERRORLEVEL%` before the ledger echo, then `exit /b %WD_EXIT%`). v5
  **always** reported exit 0 to the task because of the trailing `echo` — a crash
  was invisible to both monitoring AND restart-on-failure.
- **Outer-net self-heal (operator-gated, NOT applied):**
  `tools/watchdog_selfheal_task.ps1` (dry-run default) + `docs/WATCHDOG_SELFHEAL.md`
  configure restart-on-failure (`RestartCount=3`/`RestartInterval=PT1M`) on
  the task, keeping all other settings. Only fires on a genuine failure
  (non-zero exit) — a `Stop-ScheduledTask` stays stopped. Collision-free with
  the mutex + `MultipleInstances=IgnoreNew` + `_terminate_orphan_fleet` (P0.2) +
  `restart_fleet.ps1` (analysis in the doc).

Verified: `backtest/test_watchdog_hang.py` 19/19 (new cases: crash exit,
timeout, spawn failure → exempt; pure selection logic), watchdog suite 51/52
(the one red one is the pre-existing `test_fleet_definition::test_watchdog_view_is_unchanged`,
a stale golden from T-149, untouched by this PR), ruff+mypy clean, batch exit-code
propagation tested in isolation (42→42), self-heal script dry-run run against the live task.
Live effect = watchdog restart (Michi-gated) + elevated task-config apply.

## [2026-07-22] Classic main-channel bot retired, replaced by MAX2 (SRA2-LONG trade → CH_MAIN) (T-2026-KYT-9050-020)

The classic "Main Channel" detector (`strategies/strat_main_channel.py`, graded C−/−77 PnL,
"duplicate of Support Resistance" in the concept audit) is retired
and replaced by **MAX2**. MAX2 is NOT its own model and not a new process,
but an inline fork of the SRA2-LONG emission in bot 9 (`_emit_max2` in
`9_ai_sr_bot.py`): whenever SRA2 fires LONG (prob≥threshold) for a coin in
`config.MAIN_CHANNEL_COINS`, the SAME trade (same prob + entry/SL/target
geometry) is additionally posted under tag `MAX2` to `CH_MAIN`. The only filter =
the 37-coin whitelist, exactly like the retired bot (operator decision, Michi).
- **LONG-only:** SRA2 SHORT is a dead shadow leg (threshold=None, label source
  `closed_trades3` dead since 23.02) → no tradable SHORT edge.
- **MAX2 default-LIVE** (`leg_status("MAX2","LONG")`=LIVE, deliberately NOT listed
  in the `_LIFECYCLE` register): collision-free with the existing SRA2 post
  to `CH_AI_SR`, BECAUSE `CH_AI_SR` is NOT Cornix-executed (informational/
  orchestrator, operator-confirmed) — otherwise this would be a rule-4 double trade on
  the 37 coins. Rolling back to shadow = activate the commented-out register line
  `("MAX2","LONG"): SHADOW`.
- **Own tag ⇒ own cooldown/dedup namespace** via `has_open("MAX2")`
  (rule 6); MAX2 blocks/isn't touched by the SRA2 active-trade check.
- **Retirement in `3_detectors.py`:** dispatch + the `analyze_main` import +
  the `MAIN_CHANNEL_COINS` import removed, `'Main Channel'` out of the 1h strategy
  roster (`_strategies_for` + dispatch). `strategies/strat_main_channel.py`
  stays in place, unused (operator decision). `MAIN_CHANNEL_COINS`/`CH_MAIN`
  stay — now consumed by the MAX2 fork.

Verified: new `backtest/test_max2_forward.py` (11 checks: fork wiring,
geometry-reuse ordering, own dedup namespace, gate guard, retirement in
3_detectors, MAX2-LONG=default-LIVE), `test_sra_tag.py` green after an anchor fix
(`_emit_sra2_shadow` had shadowed the first `get_indicators_at_time`
occurrence since T-125 → the process_ai_trade anchor search now starts from the active-trade check),
detector tests 4/4 + 21/21, `regression_guard verify` 24 fixtures, ruff clean.
One `test_shadow_gate` case stays red = a pre-existing env failure (xgboost pickle
load on the build machine, byte-identical to origin/main). Live effect =
watchdog restart (Michi-gated); deploy precondition met (CH_AI_SR not
Cornix-executed → no double trade).

## [2026-07-21] Bot 10 (EPD): hot-path window scans folded, redundant ISO parse + deque copy removed (T-2026-KYT-9050-019)

CPU optimization of the pump/dump detector (bot 10) — per the per-bot measurement of
2026-07-21, the fleet's top consumer (bursty, p90 ~30% / max ~46%). Four
behaviour-preserving changes in the per-tick-×-527-coin hot path of
`process_coin_logics`, restart-gated (no live semantics change), building
on the epoch cache from T-165:
- **Anchor via a cached epoch float** instead of `_parse_bucket_ts`: `bucket_anchor =
  _bucket_epoch(data[-1])` — the newest bucket carries `'e'` from creation in
  the main loop, and the earlier ISO `fromisoformat` ran per coin/tick for nothing (the one
  anchor spot T-165 missed). `latest_age_sec` from epochs.
- **Read the deque directly** instead of the `list(ONE_MINUTE_DATA[symbol])` copy per tick
  (`data` is only ever touched via `data[-1]` and `reversed(data)`, never sliced).
- **Hour scan + the 6 price-move lookbacks in ONE reverse pass**
  (`_scan_hour_and_lookbacks`) instead of 1×`_find_bucket_range(3600)` + 6×
  `_find_bucket_before` — ~886 → ~362 bucket iterations/coin/tick. Constructed
  byte-identical to the individual calls and pinned with a 3000-case fuzz test + band-edge +
  empty/None tests (`hour_buckets` feeds `avg_volume` = a model input AND
  the `pump_dump_events` insert gate — any deviation would be a silent rule-7 skew).

The P1.39 timestamp window, T-035 nearest bands, `now`=wall clock (staleness/
cooldowns/`spike_time`) and the "no invented substitute bucket" rule stay
untouched. Deliberately NOT in scope: an incremental hourly aggregate instead of a
rescan (needs its own regression guard, a follow-up task). Verified:
`test_pump_dump_time_windows.py` 21/21 (18 + 3 new equivalence), core reviews
(z-code-reviewer + z-spec-compliance-review) PASS, `regression_guard verify`
(24 fixtures) + `smoke` clean, ruff clean. Deploy = watchdog restart (Michi-gated).
## [2026-07-21] Doku: KB-Task-Nummernkreis auf T-2026-KYT-9050-NNN umgestellt (T-2026-KYT-9050-001)
Pure documentation change, no behaviour/code effect. Kythera tasks run from
now on under the canonical slug `kythera` in the ID range `T-2026-KYT-9050-NNN`
instead of the closed `T-2026-CU-9050-NNN` block (operator decision, Michi,
2026-07-21). Updated accordingly:
- `CLAUDE.md` §Workflow: new bullet with the numbering-range convention (add_task
  `customer/project_id="kythera"`, prefix `T-2026-KYT-9050-`) + note that
  the old range is closed and historical CU-9050 references stay in place as
  provenance.
- `docs/OPUS-HANDOFF.md` §2: the `/task-start` template switched to KYT,
  precedent search across both corpora; two active task references (escalation §6
  + the batch-E precedent) updated to the migrated IDs (018→KYT-002,
  020→KYT-003).
- `docs/T-2026-CU-9050-021-opus-task-audit.md`: a migration banner with the full
  mapping (15 open tasks migrated to KYT-002…016, the rest done/wontfix; the KB is
  the single source of truth). The filename stays unchanged as a path reference.

## [2026-07-21] WS2 batch 2 (deployable-only): SRA2-LONG + EPD3-SHORT live (T-2026-CU-9050-185)

Second batch of shadow→live promotions. Only the two legs WITH a valid
operating point go live, coexisting with their legacies:
- **SRA2 LONG** (@0.6424) → CH_AI_SR (next to SRA1). Artifact `sra2_model_LONG.*`
  promoted from `staging_models/` to the repo root (rule 2, operator decision Michi).
- **EPD3 SHORT** (@0.6737) → CH_PUMP_AI (next to EPD2). Artifact promoted to the
  repo root as `epd3_model_SHORT.pkl` — deliberately a challenger-DISTINCT
  filename, so it does NOT hijack the legacy EPD2 loader slot `epd2_model_SHORT.pkl`
  (bot 10 `EPD2_ARTIFACT_PATHS["SHORT"]`); otherwise the EPD2 live path would
  load the same file and post SHORT twice (a rule-4 double trade — review finding
  T-185, fixed). The pkl's embedded `meta.model_id` is still "EPD2"
  (cosmetic: the tag "EPD3" is passed explicitly at the call site, and the
  distinct filename prevents the legacy adoption; a clean rebuild with
  model_id="EPD3" remains follow-up work, a re-dump avoided here due to the py3.14↔3.13
  mismatch).

SRA2 SHORT and EPD3 LONG stay SHADOW — they have **no deployable edge**
(not just no threshold): SRA2-SHORT's label source `closed_trades3` has been
frozen since 2026-02-23 and yields no positive-edge threshold across 3027 events;
EPD3-LONG had "no positive month". A retrain would only reproduce the `threshold=
None` — hence no retrain (saves VPS CPU, verified read-only on the live DB).

Mechanics: `shadow_gate.shadow_artifact_path` now resolves depending on is_live — a
LIVE leg loads its artifact from the repo root (= live, rule 2), a SHADOW leg
continues from `staging_models/`; so a single directional leg of a tag can go live
while the other stays shadow. Bots 9/10 emit through the
`post_ai_signal_gated` router from T-183 (LIVE → Cornix, SHADOW → monitored) — the
"best-direction" selection in bot 10 only lets the live SHORT fire if the
model favours SHORT above threshold. Bot 9 got an explicit `has_open`
duplicate guard for the live leg (post_ai_signal doesn't check that itself).

Activation is Michi-gated: restart of bots 9/10. Tests: shadow_gate registry +
promotion path resolution (live⇒root/shadow⇒staging) green (77 passed). The
pre-existing `test_sra_tag::test_active_trade_check` failure (a stale test anchor,
bot 9 identically red at baseline) is not a regression from this diff.
## [2026-07-21] Bot-11 inference vectorization: 4,216 → 8 predict_proba calls/scan (T-2026-CU-9050-186)

A fleet CPU audit found bot 11 (`11_ai_mis_bot.py`, MIS2) to be the only bot with a genuine vectorization hole: it unconditionally scores EVERY one of the 527 coins with
8 models via `predict_proba` on a **1-row DataFrame** — 527×8 = **4,216 individual calls per scan**. Measured, a 1-row `predict_proba` costs ~66ms almost
entirely per-call overhead (sklearn name validation + DMatrix build), a batch across 527 rows ~54ms **total** (0.10ms/row) — ~600× per coin. Behaviour-neutral fix:

- **`check_mis_models` restructured into three phases.** Phase A still builds the features **per coin** (`add_advanced_features` with rolling windows must NEVER be
  concatenated across coin boundaries) and collects the finished 1-row frames. Phase B scores per model in **one** `predict_proba` over the stacked coin matrix
  (a new pure helper `_score_models_batched`). Phase C builds candidates + posting **unchanged, per coin** (same 0.25 gate, calibrator, threshold ranking, cooldown,
  outbox/ai_signals/master log, per-coin transaction with rollback).
- **Byte-identical probabilities:** XGBoost scores row-independently → a batch call delivers exactly the same per-coin probability as the individual call; the
  name-based feature selection per model fixes the column order identically. A coin failure in phase A (no frame / no live price) doesn't end up in the matrix
  and doesn't shift the index redistribution. On a batch error (e.g. a corrupt row) the helper falls back to the old per-row path for that model
  → error semantics unchanged: a broken row only loses its own prediction (NaN), all others score.
- **Only the inference is batched**, no touch to `core/mis_features` (rule 7, shared with trainer/sim). `predict_proba` calls/scan **4,216 → 8**; a micro-benchmark on the
  real 8 mis2 models is 11× faster even on the saturated box (substantially more on an unloaded one).

Verified: new `backtest/test_mis_batch_inference.py` (5 — batch≡individual parity, row order, batch-error fallback, single-row NaN, multi-model columns),
`test_mis_features.py` 7/7, regression guard 24/24, ruff/mypy green. Active after a bot-11 restart (no live intervention, no trading decision).

## [2026-07-20] WS2 batch 1: 4 study forwarders live + FIF1 parked (T-2026-CU-9050-183)

First batch of shadow→live promotions from Michi's 14:00 report review. Four previously
shadow-only rule forwarders go live, FIF1 gets replaced by TSM1:
- TSM1 SHORT → CH_FIF1 (replaces FIF1). FIF1 (bot 33) now gates its live post on
  `shadow_gate.is_live` and is parked via the new `("FIF1", *) = SILENT` register
  entries — not `CH_FIF1=0`, which would also kill TSM1's inherited target channel.
- SKW1 LONG+SHORT, XSM1 LONG, XSR1 SHORT → CH_ATS (former ATS channel).

Centralized in the new `signal_post.post_ai_signal_gated`: routes a (tag, direction)
leg through `shadow_gate` — LIVE → `post_ai_signal` (Cornix + outbox + ai_signals, exactly
ONE Cornix message, rule 4), SHADOW → `post_shadow_ai_signal` (monitored), SILENT/
retired → no-op. A promotion is thereby a pure `_LIFECYCLE` flip; bots
37/38/39 now only call the gate router (early guard `leg_status ∈ {LIVE, SHADOW}`).
Pure rule forwarders (class D, no artifact) → no rule-2 promotion step needed.

NOT in this batch: EPD3 and SRA2 (coexistence decision, Michi). Both load their
model from `staging_models/`; a live post from there would violate rule 2 — they need an
artifact promotion staging→root + load-from-root rewiring (follow-up batch).

Activation is Michi-gated: the flips only take effect after deploy/restart of bots
33/37/38/39. Tests: a new `post_ai_signal_gated` routing test (LIVE/SHADOW/SILENT on
real legs) + the three bot tests switched to live (27 green). The pre-existing
`test_sra_tag` failure (bot 9, untouched) is not a regression from this diff.

## [2026-07-20] Retired/silenced models removed from the active per-bot report blocks (T-2026-CU-9050-182)

The 4h sentiment-tracker post (`23_market_tracker.py`, `job_per_bot_performance`) kept listing retired
generations (AIM1, MIS1-*) and silenced legacy legs (ATS1/ATB1) in the three active
blocks PER-BOT PERFORMANCE, HALF-KELLY POSITION SIZING and MODELS A–Z (compact) — even though the
realized-PnL report had long since separated them into their own RETIRED block.

Fix, display-only: a new module-scope pure helper `is_display_retired(tag)` applied at ONE point to the
shared `strategy_short` source (upstream of all three blocks). A tag is display-retired
if BOTH direction legs have `shadow_gate.leg_status ∈ {RETIRED, SILENT}` — the conservative per-tag
lift of the per-leg bucket from the realized report (a tag with one remaining LIVE/SHADOW leg stays
visible). SHADOW and LIVE tags stay deliberately visible, because the shadow performance is the decision basis
for the upcoming model promotions. No posting/money effect.

Tests: `backtest/test_market_tracker_lifecycle.py` +4 cases (retired/silenced out, shadow/live in,
MIS2-prefix boundary, raw pre-normalization forms), 11/11 green; all market_tracker tests 72/72 green.
Both core reviews (z-code-reviewer, z-spec-compliance-review) PASS; two LOW notes (docstring precision,
pretty↔raw test path) incorporated.

## [2026-07-20] TimescaleDB chunk exclusion on the AI-bot feature reads (T-2026-CU-9050-180)

The fleet's dominant DB read — `read_candles_with_indicators` (candles⋈indicators) — ran on the
hyper path WITHOUT a lower time bound. TimescaleDB could therefore exclude NO chunks: every read scanned
all 126 chunks of `candles` (9 GB) + `indicators` (19 GB). In `pg_stat_statements` this was query #1
(≈28% of total DB executor time, ~215–245 ms/call, 337k calls) — on the saturated VPS it drove
Postgres to ~4.3 cores (analysis T-166/T-173/T-179 + root-cause session).

Fix, behaviour-neutral: new helper `core/candles.history_start(tf, n_candles, *, anchor=None, safety=3,
min_days=60)` returns a lower `start` bound that safely covers the newest `n_candles` closed candles
(`max(n·TF_SECONDS·safety, min_days)`, tz-aware UTC). The read helpers already return the newest
`limit` candles (`ORDER BY open_time DESC LIMIT`), so any sufficiently far-back
`start` bound returns BYTE-IDENTICAL rows — it acts purely as a chunk-exclusion hint. Applied at the five
hot call sites:
- `11_ai_mis_bot.py` (1h, 100), `12_ai_ats_bot.py` (1h, 500 — covers the OBV `iloc[0]` baseline),
  `24_quasimodo_bot.py` (tf, 100), `25_smc_ml_sniper.py` (tf, 150);
- `15_ai_master_bot.py` (as-of, `limit=1`, `anchor=end`): candidates are filtered to the last
  `CANDIDATE_WINDOW_MIN`=60 min, `end`≈now → the 60-day floor can never shorten the lookup.

Deliberately NOT touched: `core/research_features.fetch_context_frame` (carries an `as_of` parameter whose
window semantics need to be clarified separately — backfill/replay path), `core/breadth_features` (already
takes `start=`), `core/ats_features` (no real call site). Rule-7 boundary: the shared read path itself
stays unchanged; only the call sites set the bound.

Proof (EXPLAIN, live, read-only): the same query without `start` = 252 per-chunk index scans; with
`open_time >= now()-60d` = 18 (~14× fewer chunks). Behavioural parity mathematically (window ≥ n·TF) +
tests. Residual: a coin trading extremely sparsely (< 1/safety of the wall-clock cadence over min_days)
would get its newest candles INSIDE the window instead of further back — but every call site already has
a minimum row guard (`len(df) < N`), so such a coin gets skipped, not misscored.

Verified: `backtest/test_candles.py` (59, 7 new for `history_start`), parity/feature/detector suites
(115 passed), regression guard 24/24, ruff/mypy green. Active after the next fleet restart (operator gate);
no live intervention, no schema/index change.

## [2026-07-20] Z1 leaderboard: risk metrics made deterministic — (src, id) tiebreaker in the outcomes order (T-2026-CU-9050-177)

BEHAVIOUR CHANGE (deliberate, the point of the task): the leaderboard risk metrics
`max_drawdown_pp`/`max_loss_streak` are now deterministic/stable — previously they flickered
run-to-run (real: one bot −83.0 vs. −80.3 pp between two polls with no data change), because
`ORDER BY bot, closed_at` on duplicate-`closed_at` rows (8,696 tie groups in `closed_ai_signals`,
898 in `closed_trades`) left the tie order up to DuckDB's parallel scan (threads=2), and both
metrics are path-dependent. The displayed values therefore change once, relative to the previous
random states; the three pure aggregates (rolling / success-rate / regime-matrix) and the
order-invariant leaderboard fields (n, wins, winrate, pnl_sum_pct, expectancy_pct) are untouched.

- `tools/analytics_api.py`: `_outcomes_cte` now carries the tiebreaker pair `(src, id)` per row —
  `id` = a monotonically increasing serial Postgres PK of the respective outcome table (insertion order;
  the same column the export keyset cursor already uses as a uniqueness tiebreaker — the best
  DETERMINISTIC ordering the schema provides), `src` = the union-branch rank
  (needed because the id spaces of both tables overlap: 371k collisions in the live export).
  **Limit:** `id` order guarantees NO genuine close chronology where upstream batch-stamps
  `closed_at` — a known ~340k-row legacy reclassify block in `closed_ai_signals` shares
  ONE timestamp; there the risk metrics are deterministic order artifacts (stable, but
  not chronologically robust; affects ATS1/EPD1/MIS1-pump ~85-93% of their history). `open_time`
  as a tiebreaker for the legacy branch = a possible follow-up.
  `bot_trade_rows` + `_leaderboard_rows_streamed` order by `ORDER BY bot, closed_at, src, id` — a
  TOTAL order; this also makes the numpy fast path ≡ pure fallback unconditionally bit-identical (both
  consume the same deterministic row stream), not only on tie-free data.
- Proof on the real DuckDB (threads=2, a fresh `connect_ro` connection per run as in the
  poll path): before, 23 of 68 bots had diverging risk metrics across 10 runs (e.g. ATS1
  −80,386.27 pp/streak 97 vs. −83,011.02 pp/streak 80); after, 10/10 runs bit-identical
  (0 of 71 bots diverging).
- Tests (`backtest/test_analytics_query_parity.py`): a new acceptance test (red before the fix, verified via
  `git stash`) with value-different duplicate-`closed_at` rows stored physically outside the
  id order + a cross-table id collision on the same bot/timestamp (pins `src`) —
  10 runs identical AND matching the hand-computed id-order expectations; parity fixture ties
  (ids 5/6) sharpened to value-different, so the whole parity suite tests tie-sensitively;
  the numpy≡fallback test relaxed from "on tie-free fixture" to unconditional. T-175 determinism
  caveats in docstrings/SPEC.md lifted accordingly (`tools/dashboard/SPEC.md` §deterministic
  leaderboard risk metrics).
## [2026-07-19] Classic-detector scan optimization — column projection, bundled VolIndic read, cycle snapshots + active-trade prefilter (T-2026-CU-9050-172)

Behaviour-invariant DB/CPU relief for the classic detector cycle (`3_detectors.py`, ~530 coins,
5 strategies). Hard invariant per operator directive: identical signal dicts given identical DB state
and identical price inputs — all affected guards are read-only + AND-combined (the P2.44 argument),
only the query shape and evaluation timing change.

- `3_detectors.py`: the indicator read now projects 27 instead of ~120 columns (`DETECTOR_INDICATOR_COLUMNS`;
  secured by P2.43 — a test enforces projection ⊇ all strategy column reads AND ⊆ the engine DDL);
  a whole-coin prefilter skips coins whose entire set of (strategy, direction) pairs in the TF is already
  WORKING, BEFORE the indicator read; ONE aggregated ⏱ INFO log per cycle (snapshot/read/
  scan-per-strategy/write durations, coins/skips/signals) instead of per-coin spam.
- `core/market_utils.py`: new `DetectorCycle` — 1× `active_trades_master` snapshot (WORKING) as a set,
  1× `trade_cooldowns` snapshot per module (lazy), a generic memo for the coin-independent
  `check_recent_trades` (1 query per (direction, hours, count) instead of per coin); the cycle's own
  signal writes are mirrored back via `note_signal_written` (in-cycle view ≡ the old DB read).
- `strategies/strat_volume_indicator.py`: the two spike reads (5d window + 10d baseline) are now ONE
  contiguous 15d read with a pandas split — window boundaries preserved exactly (baseline end =
  `open_time_1st_hit − 30m`; edge cases spike@i==0 / empty baseline / empty window parity-tested);
  the both-directions-active skip now runs BEFORE the spike computation.
- `strategies/strat_{5_percent,fast_in_out,support_resistance,main_channel}.py`: an optional
  `cycle` parameter (fallback without a cycle = old individual queries, byte-identical); SR/main check the
  direction fixed by the hit side before the first-hit scan/480-row OHLCV read (P2.44 reorder).
- DELIBERATELY LEFT OUT (spec deliverable 2, TF-differentiated row limit): the
  `first_valid_index` fallback on `support_price` (5%/FastInOut) can, as long as the
  T-061 head-nulling recompute is incomplete and pre-P1.12 broadcast rows sit inside the 480-row window,
  legitimately reach deeper than 50 rows — a smaller 30m frame couldn't be proven
  behaviour-invariant there. `limit=480` stays for both TFs; the 1h two-stage logic is likewise left out.
- Cooldown contract (P1.16: 12h, tag `VolIndic`, write via `write_signal_atomic` in the same txn) and
  the `write_signal_atomic` transaction contract untouched; DB index creation only documented as a
  recommendation in the code (partial index `active_trades_master(strategy,coin,direction) WHERE status='WORKING'`;
  `closed_trades_master(direction,posted)`) — execution is a VPS session, Michi-gated.
- Adversarial-review fixes (vote 2): (a) `conn.rollback()` in the read-except of `3_detectors.py` — a
  failed indicator read (missing table/column) would otherwise have poisoned the transaction for all
  subsequent coins of the cycle (InFailedSqlTransaction; a pattern latent on main too); (b) the
  15d split is a THREE-WAY split (`>= 1st_hit` / `<= 1st_hit − 30m`) instead of a complement — a
  contract-violating, non-30m-aligned bar in `(1st_hit−30m, 1st_hit)` fell into NEITHER of the old
  windows and now also stays excluded in the bundled read (a parity test was added).

Query balance per 30m cycle (N≈530, documented in the code): before ≈ N×3 reads (including the ~120-column
`SELECT *`) + guard point queries ≈ 1,600+; after ≈ N×2 lean reads + ~5 snapshot/memo queries.
Verified: new `backtest/test_detector_scan_optimization.py` (19 tests: spike-window parity
old↔new including a 60-case random sweep, snapshot≡individual-query parity including naive TZ normalization,
signal-dict parity across all 5 strategies on both paths, projection ⊇/⊆, wiring/prefilter),
regression guard 24/24 golden, ruff/format/mypy green. Test hygiene along the way (all three failures pre-existing
on unmodified main, in the same repo worktree): `test_window_features` stubs the cursor seam instead of
`pd.read_sql_query` (stale since the T-108 migration); `test_candles::test_candle_source_resolves_known_backends`
asserted the backend default on a clean env (the dotenv upward search finds the operator `.env` with
`KYTHERA_CANDLES_SOURCE=hyper` from worktrees); `test_published_targets` now also restores the
`core` package attribute after its sys.modules surgery — the split (attribute ≠ sys.modules) let
`test_shadow_gate`'s `_shadow_test_channel` monkeypatch run into nothing whenever the operator `.env` sets a
`CH_SHADOW_TEST` (instance A got patched, instance B got called → the echo-outbox assert went red).
Deploy/restart of the detector is NOT part of this task (restart-gated, Michi).
## [2026-07-19] Indicator engine CPU optimization — early skip + persistent worker connections + compute micro-opts, byte-identical (T-2026-CU-9050-174)

The engine recomputed all 527 coins × 6 TFs (~3,160 tasks, NUM_WORKERS=3) in full every 30 min, even though
for most (symbol, TF) pairs no new CLOSED candle existed since the last cycle (1d, for example, has
new work in only 1 of 48 cycles). All three findings from the 2026-07-19 review implemented;
DB end state provably byte-identical, trade characteristics unchanged.

- Finding 1 (biggest lever, ~2/3 of the cycle's work): an early skip in `process_coin_task` — after the
  watermark read, additionally `latest_open_time(kind='candles', include_forming=False)`; if the newest
  closed candle is no newer than the watermark, an immediate return BEFORE `read_candles`.
  End-state identity argument (review-corrected): the FINAL write of every row always happens in
  a new-candle cycle (the last one whose 5-candle save window still covers it) — and
  new-candle cycles always trip the predicate and run exactly as before; superseded reference bars
  get nulled identically. The skipped intermediate cycles only rewrote the save window with
  a warmup window shifted by one candle — values the next new-candle cycle overwrote anyway.
  **Accepted, bounded deviations** (each heals by the next candle close; operator-reviewed at rollout):
  (a) the window-global columns of the reference bar stay frozen at the first
  post-close computation instead of shifting once to the shifted window ~30 min later;
  (b) an in-place correction of an already-closed candle (outage-recovery catch-up, as on
  2026-07-13) only gets recomputed at the next candle close instead of the next 30-min cycle
  (up to 1 day/1 week for 1d/1w); (c) if a period boundary falls exactly between the skip probe and
  `read_candles`, the candle slips one cycle. Edge cases intact: late ingestion (a new
  closed candle > watermark) → recompute; housekeeping gap invalidation (indicator rows
  deleted, watermark jumps back) → recompute. The per-TF `updated` release
  (`update_timeframe_state`) stays unchanged in the orchestrator loop.
- Finding 2: ONE persistent DB connection per pool WORKER (`initializer=_init_worker`, lazy connect,
  reconnect-on-error) instead of a pool checkout/return per task (~3,160 checkouts/cycle, every return
  with a ROLLBACK round trip + liveness probe). Transaction hygiene (review finding): a `finally`
  ends the task transaction on EVERY exit path (`get_transaction_status()` check, client-side) —
  the persistent connection never holds an open transaction (and its AccessShareLocks across
  distinct per-coin tables) across tasks, and a partial write left behind by a BaseException can never
  get co-committed by the following task; if the rollback fails, the
  connection is discarded and the next task reconnects. Commit stays with the caller (hard rule 8).
  Also `get_indicator_definitions()` once per worker and a positive cache for the two
  `table_exists` probes per task (only hits are cached — newly created tables are still found).
- Finding 3 (micro-opts, bit-identical): (a) `calc_macd` reuses the EMA_9/12/21/26 series from the
  EMA block instead of four fresh `ewm` computations; (b) a redundant `df.sort_values('open_time')` in
  `calculate_indicators_optimized` removed (all callers deliver ASC: read_candles contract 1,
  guard fixtures, recompute `ORDER BY`); (c) true range via `np.fmax.reduce` instead of
  `pd.concat(...).max(axis=1)` — deliberately fmax instead of maximum, so the NaN-skip semantics of the first
  bar (`close.shift()`) are preserved; (d) `exe.map(..., chunksize=8)` amortizes the
  IPC round trips over ~527 tasks/TF.
- DELIBERATELY NOT touched: `lookback_candles=1000` (EWM convergence EMA/SMMA_200/KAMA/TSI, hard
  rule 7), `NUM_WORKERS=3` (VPS CPU-saturated, T-166), the KAMA residual loop (inherently sequential).

Verification: regression guard 24/24 golden WITHOUT a refresh + `smoke` green; additionally
a bit-identity proof old↔new across all 24 fixtures (111 columns, float64 bit-pattern comparison:
100% identical). New DB-free test `backtest/test_indicator_engine_skip.py` (8 tests: the complete
skip decision table including first-run/gap-rewind, transaction end on every exit path,
positive cache, discard on a broken connection). backtest: test_gap_continuity, test_wilder_rsi,
test_window_features (engine part), test_candles_schema, test_fleet_definition, test_watchdog_backoff
green; the S/R-reader part of test_window_features and test_candles' backend-flag test fail
identically on unmodified main (a stale `pd.read_sql_query` stub since the read_candles rewiring,
resp. a test-isolation leak — not a regression, follow-up candidates). ruff + format green. Reviews:
3-vote z-code-reviewer (findings resolved: transaction-hygiene `finally`, doc corrections, tests) +
spec-compliance PASS. Expected ~60-70% less engine load/cycle + Postgres relief (fewer
checkouts/reads). Active after the next engine restart (Michi-gated).
## [2026-07-19] Z1 dashboard: DuckDB analytics queries 2.8–8.5x faster + panel-data cache (T-2026-CU-9050-175)

Profile-first against the real served DB (~824k outcome rows, ~580k decisive): the two 11-second
aggregates (`bot_leaderboard`, `rolling_success_rate_series`) transferred EVERY decisive trade as a
Python dict across the DuckDB boundary — exactly the cold-start timeout (>10s) observed at deploy.
Optimized query-side, result-preserving. **Parity scope, honest:** the three pure count/sum
aggregates (`rolling_success_rate_series`, `success_rate_timeseries`, `bot_regime_matrix`) are verified
**bit-identical** old-vs-new on the real DB (JSON-identical). For `bot_leaderboard` this holds for
the order-INVARIANT fields (n, wins, winrate, pnl_sum_pct, expectancy_pct); the two order-DEPENDENT
risk metrics (`max_drawdown_pp`, `max_loss_streak`) keep the **SAME pre-existing run-to-run
non-determinism class** as the old code — not bit-identical by nature (see the FINDING below), but
no NEW non-determinism vs. the old code. Measured (real `analytics.duckdb`, fresh connection per call,
min/3):

| Query | before | after |
|---|---|---|
| `bot_leaderboard` | 11,509 ms | 4,098 ms |
| `rolling_success_rate_series` (w=30) | 11,812 ms | 1,395 ms |
| `success_rate_timeseries` (7/30/90) | 1,620 ms | 1,318 ms |
| `bot_regime_matrix` (ASOF) | 2,400 ms | 2,120 ms |

- `tools/analytics_api.py`: `rolling_success_rate_series` now aggregates the daily buckets in DuckDB
  (`GROUP BY bot, d`, pure integer counts → exact parity by construction) instead of pulling ~580k rows into
  Python; `_daily_buckets_by_bot`/`bot_trade_rows` remain as a reference pipeline. `bot_leaderboard`
  now runs over a streamed-column path (`_leaderboard_rows_streamed`): a 3-column
  projection (`closed_at` stays sort-key only, never 580k materialized datetimes), a lazy-optional
  numpy `fetchnumpy` fast path with a bit-identical pure-Python fallback — both call `_leaderboard_row`'s
  own math verbatim (`_leaderboard_row_from_columns`: builtin `sum()`, a naive
  drawdown loop). `success_rate_timeseries` computes all windows in ONE scan (FILTER aggregates over the
  widest window; bot inclusion per window reconstructed via an any-row count) instead of one scan per
  window. `bot_regime_matrix`: an `ASOF JOIN` (inner) instead of `ASOF LEFT JOIN + WHERE` (provably
  row-identical, `regime_sorted` is NULL-free) + a redundant inner `ORDER BY ts` removed.
- `tools/dashboard/app.py`: a panel-data cache (`_PollCache`, a file-freshness token — the same pattern as
  the existing blueprint cache): with an unchanged export file, every 30s HTMX poll is served from memory
  (no connection, no scan; steady-state ~0 ms). Only DuckDB-derived data is cached
  (payload + `data_freshness` rows); the "synced N min ago" age is still computed per request from the wall
  clock, the fleet registry (file-based) deliberately uncached. `cache=None` default = exactly the old
  behaviour.
- Parity net: new `backtest/test_analytics_query_parity.py` (23 tests) — reference implementations
  (old query shapes) vs. new on tmp-DuckDB fixtures (both outcome tables + regime_history), including
  edge cases: closed_at ties, bot filter, window duplicates/subsets, an explicit `as_of`, an empty
  substrate, numpy≡fallback on tie-free fixture data (scope named honestly, see above) PLUS a
  tie-robust implementation-equivalence test over ONE shared row stream, a cache hit without reconnect
  AND cache invalidation on a new file token (a genuine re-export). 208 dashboard+parity tests green
  (`pytest backtest/test_dashboard_*.py backtest/test_analytics_query_parity.py`), ruff 0.15.17
  check+format clean.
- FINDING (pre-existing, left unchanged): the OLD `bot_leaderboard` was run-nondeterministic on
  `closed_at` ties, because `ORDER BY bot, closed_at` has no deterministic tiebreaker and
  DuckDB's parallel scan (threads=2) orders ties between duplicate/same-instant rows in `closed_ai_signals`
  differently per run (reproduced for real: 6/10 runs diverge, e.g. `max_drawdown_pp` ATS1 −83,003
  vs. −80,303 pp, `max_loss_streak` ±24 on an identical file). Affects only the two path-dependent
  risk metrics; the count/sum fields are order-invariant and stable. This also holds for numpy-path-vs-
  fallback: every `bot_leaderboard` call re-runs the query → its own tie stream, so the
  numpy≡fallback equality can only be guaranteed per row stream (not across separate calls). The
  optimization **deliberately** leaves `ORDER BY bot, closed_at` unchanged (same non-determinism
  class, none new). A deterministic tiebreaker would CHANGE these money-relevant metrics and is
  therefore a **separate follow-up task (behaviour change)** — deliberately NOT part of this PR.

## [2026-07-19] Z1 ops script updated — dashboard task to password-logon + cmd.exe launcher (T-2026-CU-9050-170)

Live verification of the Z1 deploy found: `tools/ops/register_kythera_dashboard_tasks.ps1` registered the dashboard task as **S4U** — that does NOT work. The short export batch runs fine under S4U, but the long-running waitress dashboard server never binds port 8098 in the session-0 S4U context (tested: no bind even after 35s). The fleet watchdog uses `LogonType=Password` for the same reason. Second finding: a scheduled-task action can't launch a `.cmd` directly (no CreateProcess on `.cmd`) → it has to run via `cmd.exe /c "<launcher>"`. Both fixed live (dashboard now runs in session 0, HTTP 200) and pulled back into the committed script here.

- `tools/ops/register_kythera_dashboard_tasks.ps1`: the dashboard task (A) is now **password-logon** (`Read-Host` password prompt → `-User`/`-Password`, `-RunLevel Highest`) + action **`cmd.exe /c`** on a **logging launcher `.cmd`** written at runtime (redirects stdout/stderr into `staging_models/analytics/dashboard_scheduled.log`, so a startup error is visible). The export task (B) **stays S4U** (a short batch, no password needed). Header/`.NOTES` + footer updated accordingly. Stays **registration-only** (no live cutover — CLAUDE.md hard rule 1). Ops script only, no Python/fleet code; `.ps1` parse-verified (no DB-free test possible).

## [2026-07-19] Ingestion batch flush — one execute_values instead of ~3,185 individual INSERTs/s (T-2026-CU-9050-169)

Implements measures 1–3 of the T-168 ingest report: the 3s DB flusher wrote every candle as its OWN
statement with its own SAVEPOINT/RELEASE pair — measured live at ~3,185 individual INSERTs/s + ~6,400
SAVEPOINTs/s (≈2.6s DB executor time/s) and the bulk of the ingestion's ~59% client CPU. Now
the entire buffer goes out as ONE `execute_values` batch on the hyper write primary; DB end state
provably identical (same statement, same `IS DISTINCT FROM` no-op guard, same
forming→closed flip semantics).

- `core/candles.py`: a new bulk API `upsert_candles_many()` (hyper-only, row shape = `_CANDLES_HYPER_UPSERT`
  column order with closed per row, bool-strictly validated, doesn't commit — contract 3) +
  `candles_write_primary()` as a public accessor. Purely additive, the individual path unchanged.
- `1_data_ingestion.py`: `_flush_to_db` uses the batch on `WRITE_PRIMARY=hyper` (1 round trip, 1 commit);
  on a batch error, rollback + fall back to a group flush with SAVEPOINT isolation per
  (symbol, tf, closed) group (instead of per row — the real error class "missing table" is groupwide
  anyway); the legacy primary goes straight into the group path. A persistent flusher connection instead of
  connect/close every 3s (reconnect-on-error, the monitors' pattern). Optional orjson for WS parsing
  (stdlib fallback, inert until installed).
- DELIBERATELY NOT: the flush interval (measure 4 = an operator decision), candle-close semantics,
  client-side dedup of forming updates, the `KYTHERA_CANDLES_*` flags, catch-up overlap (the T-168 no-go list).

Micro-benchmark (DB-free, 9,550-row flush): ~28,650 statements/590ms client CPU → 20 execute_values pages/
1ms (614×). Verified: new `backtest/test_ingestion_batch_flush.py` (11 tests: batch≡individual at the
SQL level, choreography including fallback isolation + connection reset), regression guard 24/24 golden,
ruff/mypy green; the 14 pre-existing candles-suite failures are environment-related (identical on
unmodified main: live hyper flags + legacy tables stale since the write-primary switch). Active after
the next ingestion restart (Michi-gated).
## [2026-07-19] Confidence-posting floors from realized-trade analysis — AIM2 0.70 / BB 0.50 / SRA1 0.70 (T-2026-CU-9050-171)

Threshold analysis over realized trades (T-2026-CU-9050-170, read-only): `closed_ai_signals`
(deduplicated per the audit key) ⨝ `ml_predictions_master` confidence (nearest-time ±10 min) = 32.4k trades
03–07/2026 with bootstrap CI95. Finding: for three bots, the posting segment below a confidence floor is
zero-EV — fewer trades at equal/higher PnL is a pure gate question there. PR #157:

- **New `core/prob_floor.py`:** `load_prob_floor(env_var, default)` — an env-overridable floor,
  clamped [0,1], garbage/NaN/Inf → default. Invariant everywhere: the effective gate = `max(Artefakt-Threshold,
  Floor)` — a floor can only tighten, never undercut the artifact's operating point.
- **AIM2 (bot 15):** `AIM2_MIN_PROB` (default **0.70**) in both the live gate AND the TOPN floor. Below p=0.70, in
  both artifact eras, zero-EV (avg +0.18%, CI [−0.27, 0.67], ~72% of the volume); from 0.70 avg 1.0–2.2%/trade,
  WR +6 pp.
- **BB sniper (bot 25):** `BB_MIN_PROB` (default **0.50**) above the loaded artifact/hardcoded threshold
  (artifacts carry 0.30). Below p=0.5, zero-EV (~95% of volume); above it, avg 1.2–1.9%/trade. **TD deliberately
  without a floor** — confidence there isn't selective on realized trades, the channel is net positive.
- **SRA1 (bot 9):** `SRA_LEGACY_THRESHOLD` 0.65 → **0.70**. The 0.65–0.70 band was net negative (avg −0.10%);
  from 0.70, 62% of the trades remain with MORE total PnL (302 vs. 274) and WR 52 → 55.5%.
- **Untouched (deliberately):** QM (bot 24, operator decision stays live), TD legs, all shadow floors
  (AIM2 0.25, sniper 0.25, SRA 0.35) — data collection below the gates keeps running in full. No
  artifact changes, no gate flips. **Only takes effect with the next fleet restart.**

Side observation (no code in this PR): RUB2 live doesn't yet confirm the held-out validation
(avg −0.15%, n=209; TP1 WR 67.5% vs. realized win rate 48.8%) — the lever there is exit management,
not the gate.

Verified: `backtest/test_prob_floor.py` (new, 29 with `test_aim2_topn.py`) — parsing semantics + static
gate wiring of all three bots (floor-only-tightens, TD-without-floor, shadow floors pinned); regression
guard smoke OK; ruff/mypy green. Reviews: z-code-reviewer 3-vote APPROVED + z-spec-compliance PASS.

## [2026-07-19] Z1 export atomic publish — retry budget 1s → ~30s against dashboard polling (T-2026-CU-9050-167)

The atomic publish in `tools/analytics_export.py` (`publish_duckdb`, T-163) failed in practice
(verified 2026-07-19) under active HTMX polling of the Z1 dashboard: the dashboard opens the served DuckDB
per request read-only and polls across several panels quasi-continuously → `os.replace(<served>.tmp → served)`
threw `PermissionError`/`WinError 5` on Windows. The old retry budget of **5×200ms = ~1s** found no gap in
that time → publish FAILED after 5 attempts, served stayed on the old snapshot. A registered
30-min export task would thus **never** have published to the dashboard. No data loss — the error-path safety held
(build DB + `.tmp` intact, served untouched).

- **Budget significantly raised + configurable:** new constants `DEFAULT_PUBLISH_RETRIES=120` /
  `DEFAULT_PUBLISH_RETRY_DELAY_S=0.25` → **~30s total budget** instead of 1s. Since the dashboard closes its
  read handle per request, the served file is free >90% of the time; a wide window reliably hits a
  gap. Signature `publish_duckdb(..., retries=, retry_delay_s=)` stays backward compatible.
- **CLI flags:** `--publish-retries` / `--publish-retry-delay` pass the budget through to `publish_duckdb` —
  operator tuning without a code change.
- **Self-healing documented:** if every attempt fails, the next run republishes the same fresh
  data from the persistent build DB → a missed publish is never data loss, only delayed
  (exit code ≠ 0 stays). Retry WARNINGs are throttled (first 3 + every 20th) instead of ~120-fold spam.

Verified: `backtest/test_analytics_export_publish.py` (17, including new: 30 locked attempts → publish
succeeds within the default budget; a guard against regressing to 5) + `test_analytics_export.py` (25), ruff `check`/
`format --check` green. SPEC.md (`tools/dashboard/`) extended with the budget rationale.
## [2026-07-19] Bot-10 CPU optimization — epoch window scans, shared hour window, compact state dump (T-2026-CU-9050-165)

The pump/dump detector re-parsed the ISO timestamp (`fromisoformat`) on EVERY window lookup of every
bucket — at 527 coins × up to 1440 buckets × ~10 scans per 10s tick, the bot's dominant CPU item:
measured at **4.4s of the 10s tick budget** (dev machine, full inventory). On the saturated VPS
this is the most plausible driver of the bimodal bucket cadence from T-035 (p90 = 70s instead of 10s) — the bot
couldn't keep up with its own tick. All behaviour-neutral:

- **Epoch key:** buckets carry an `e` field (epoch seconds of the grid stamp) from creation; all
  `_find_bucket_*` helpers compare floats instead of datetime objects (`_bucket_epoch`, a lazy-parse-once cache
  for legacy state files and test fixtures; anchors accept datetime OR epoch via `_anchor_epoch`).
- **Hour window once per tick:** the volume-explosion path (A2) and the ML path (B) each ran their
  own, identical 3600s scan (same anchor, same data) — now computed once, shared.
- **State dump slimmed down:** `1minute.json`/`pump_dump_state.json` compact instead of `indent=2` (previously >100MB
  and ~9s of pure serialization EVERY 5 MINUTES); the bucket deque 1440 → 720 (`BUCKET_DEQUE_MAXLEN`): the widest
  window is 3600s+20s tolerance, the older half was never reached by any time-based lookup.

Steady state afterwards: **1.68s/tick (2.6×)**; the first tick after restart fills the epoch cache once.
Verified: `backtest/test_pump_dump_time_windows.py` (18) + `test_epd2_entry_from_ticker.py` +
`test_shadow_prediction_cooldown.py` (9), regression guard 24/24 golden, ruff/mypy green. Active after
a bot-10 restart. Monitors 5/8 deliberately NOT touched (a much smaller item; a batched candle read checked
as a follow-up).
## [2026-07-19] RUB4 — funding-gated RUB-LONG as a shadow experiment (T-2026-CU-9050-164)

The RUB-LONG leg is bleeding (live RUB2-LONG −2.5%/trade, shadow RUB3-LONG −3.7%). A retrospective over 123
closed RUB-LONG trades: the ABR1 funding gate (`fund_24h > +3 bps`) turns the aggregate positive
(−2.90% → **+1.61%**), but only **6/123** trades pass it → promising, but too thin to switch live.
So it's forward-validated as a **pure shadow experiment** (Michi's decision: shadow only, live untouched).

- `13_ai_rub_bot.py`: new tag **RUB4** — emits the SAME RUB3 candidate in `_emit_rub3_shadow`
  (same model, same geometry, same entry), but ONLY if `funding_gate_open(feats["fund_24h"])`
  (strictly `> 3.0 bps`, the ABR1-LONG threshold; `fund_24h` is already computed in the funding features). A pure
  `funding_gate_open` function (DB-free testable). Purely additive, never live, fails safe to silence if RUB4 isn't
  SHADOW. The report thus compares **gated (RUB4) vs. ungated (RUB3)** directly.
- `core/shadow_gate.py`: `("RUB4","LONG") → SHADOW`. No own `SHADOW_ARTIFACTS` entry — RUB4 uses the
  RUB3 artifact (`SHADOW_RUB3_LONG`); `bot_catalog` maps RUB4 to bot 13 via the `RUB` prefix.
- Tests: `backtest/test_rub4_funding_gate.py` (gate boundaries, registration, tag→bot; ABR1 threshold). Also a
  **test-hermeticity fix** (a consequence of T-150): `test_shadow_gate.py` now disables the CH_SHADOW_TEST echo via an autouse
  fixture, so the "never telegram_outbox" invariant also stays green when a CH_SHADOW_TEST is set in the
  environment/.env (otherwise falsely red locally under the live checkout). 63/63 green.

Active after a fleet restart. If RUB4 turns forward-positive (enough n), promoting the gate onto the live RUB-LONG
is a separate operator decision.
## [2026-07-19] Atomic export publish + committed Z1 ops scripts (T-2026-CU-9050-163)

The analytics export (`tools/analytics_export.py`) had until now held the exclusive DuckDB write lock directly
on the **served** DuckDB (`staging_models/analytics/analytics.duckdb`), which the Z1 dashboard opens per request
read-only → during a run, the data panels errored transiently (during the 2.5h first run the
dashboard was completely dead data-wise). The export now operates on a **persistent build DB**
(`analytics.duckdb.build`, opened RW, carries the watermark → incrementality preserved exactly from the first run/seed onward) and
**publishes atomically**: `shutil.copy2(build, <served>.tmp)` → `os.replace(<served>.tmp, served)`
(atomic on the same volume). The served path is **never opened RW** by the export → dashboard reads
are never blocked.

- `tools/analytics_export.py`: a new DB-free-testable `publish_duckdb(build, served, *, retries=5,
  retry_delay_s=0.2)` + a `build_db_path()` helper. Windows sharing-violation retry (up to 5 attempts,
  200 ms pause, `log.info`/`warning`/`error` per attempt); if the publish fails after all retries,
  the build DB **and** `.tmp` stay intact, served stays untouched (no corruption risk) and `main()`
  returns exit code ≠ 0. `os.replace` is module-level monkeypatchable. Defensive guard: `build == served`
  → no-op (no self-replace/data loss). The served default path, all flags, the parquet write and the
  watermark semantics unchanged.
- **Rollout seed (`seed_build_db`)**: the switch to the persistent build DB is the first split from the old
  single-file layout. `main()` therefore seeds `analytics.duckdb.build` once, BEFORE the export, from the
  existing served DB (`shutil.copy2`, a clear `log.info` line), if the build DB is missing but the served DB
  exists → the persisted `_export_watermark` is preserved, no multi-hour full re-export from
  live Postgres. A genuine first run (both missing) stays a full export into an empty build DB. The
  human-readable "Exported N rows" summary print now runs AFTER the publish (on a publish failure a clear
  `publish PENDING — served NOT updated` marker + warning line), so an operator never reads a
  success-looking output while the served DB hasn't actually been updated.
- `tools/ops/register_kythera_dashboard_tasks.ps1` (new, **registration-only**): reproducible,
  committed registration of the two Windows scheduled tasks — "Kythera Z1 Dashboard" (waitress
  @127.0.0.1:8098, AtStartup, S4U, restart x3/1min) and "Kythera Analytics Export"
  (`-m tools.analytics_export`, every 30 min, S4U, `IgnoreNew` = no overlapping run, 2h limit). The
  script **only registers** — it stops no process, starts no task and doesn't touch the running fleet
  (CLAUDE.md hard rule 1: no live intervention/fleet restart from a committed dev artifact). Cutover
  (stopping the manual instance + `Start-ScheduledTask`) is a separate, deliberate operator step, which the
  script only prints as an advisory line. The header documents the elevation requirement, the registration-only nature
  and the S4U fallback (→ LogonType Password).
- `backtest/test_analytics_export_publish.py` (new, 15 tests, DB-free with tmp DuckDB): build→served copy,
  first-run bootstrap, retry-on-lock (monkeypatch `os.replace`, retry counter verified), all-retries-
  fail (served untouched, build DB intact, `.tmp` stays), `build == served` no-op, integration
  (AnalyticsExporter→build DB→publish→queryable served DuckDB) + migration tests for the rollout seed
  (served with a watermark, `.build` missing → seed preserves the cursor, the follow-up export pulls 0 rows instead of the full history).

## [2026-07-18] Read-only event feed — Z1 dashboard feature 9, last panel (T-2026-CU-9050-161)

Ninth and last feature block of the Z1 dashboard rewrite: a chronological (newest first)
event feed that consolidates regime transitions (`regime_history`) and notable trades (biggest wins/losses from
`closed_ai_signals`/`closed_trades`) into one typed list. S10 is deliberately a
"simple intervention log", not an annotation editor — a WRITING annotation feature would be a
mutation endpoint and thus F4-/Z2-gated (CLAUDE.md hard rule: no mutations/live levers in the
web UI ahead of Cloudflare Access). **No POST/write endpoint built** — operator-written annotations
are a documented Z2 follow-up (auth + CSRF + its own persistence store need to exist first).

- `tools/analytics_api.py`: additive `event_feed(con, window_hours, *, as_of=None, bots=None)` +
  the helper `_regime_transition_events()` (reuses the same `lag()` logic as
  `_regime_changes_in_window`, feature 8, but delivers the full from→to instead of just the count),
  `_notable_trade_events()` (reuses the coin-aware CTE `_outcomes_cte_with_coin`, feature 7/8;
  biggest wins/losses split via `is_win`, never via sorted `pnl_pct` with an overlap risk on
  few trades) and `_latest_event_anchor()` (data-anchored `as_of`, with a fallback to
  `regime_history` if no outcome table exists). A half-open window (`> as_of-Nh AND
  <= as_of`), identical to `overnight_digest`. Events sorted chronologically DESCENDING. Existing
  aggregates (`_regime_changes_in_window`, `_outcomes_cte_with_coin`, `overnight_digest`, …) unchanged in
  content.
- `tools/dashboard/app.py`: `/panels/event-feed` route, `resolve_event_feed_window` (default 24h,
  alternative 168h/7 days; unknown `?window=` → default, no 500), `_event_feed_context`,
  a `PANEL_SOURCES` entry (`closed_ai_signals`/`closed_trades`/`regime_history`).
- `templates/panels/event_feed.html` (new) + wired in as the last panel in `index.html`;
  `static/css/app.css` additive (event-feed list styles).
- `tools/dashboard/SPEC.md`: feature 9 documented, including the out-of-scope follow-up for writing
  annotations (Z2-gated).
- `backtest/test_dashboard_event_feed.py` (new, 17 tests): DB-free tests including a real
  integration test (AnalyticsExporter→DuckDB→Flask route→HTML) + mutation checks (sort direction
  desc→asc, window boundary `>`→`>=`) both manually verified red; additionally a test confirming
  that `POST /panels/event-feed` returns 405 (no write verb exists).
## [2026-07-18] Filter non-ASCII meme symbols out of the universe (T-2026-CU-9050-162)

Bugfix: Binance occasionally lists USDT perps with non-ASCII symbols (Chinese characters, e.g. `龙虾USDT`,
`我踏马来了USDT`, `币安人生USDT` — 3 of 530). The one coins.json writer (`core/coins.py::filter_usdt_perpetuals`,
P2.16) took the Binance `symbol` verbatim (only quote/status/contractType checked) → these symbols ended up in
`coins.json`. Every candle-reading bot that loads `coins.json` DIRECTLY (bot 14/ATB and ~12 others that
bypass `load_coins`'s `[A-Z0-9]+` filter) passed them on to `core.candles.read_candles` → `validate_symbol`
threw **"invalid symbol for table identifier"** on every scan (among other things, the ATB2 shadow emission failed for these coins,
log noise).

**Fix at the ONE writer:** `filter_usdt_perpetuals` now additionally applies the already-existing shape predicate
`looks_like_usdt_perp` (`[A-Z0-9]+USDT`) → non-ASCII bases drop out at the source, fleet-wide, without
changing the ~13 direct readers. Also robust for future meme listings. 2 new tests
(`backtest/test_coins_writer.py`: a non-ASCII symbol gets dropped, `looks_like_usdt_perp` False/True; 10/10 green).
Active after deploy + fleet restart (the coins.json writer in `1_data_ingestion` writes the cleaned list at the
next ingestion start). No signal/trading path touched.

## [2026-07-18] Overnight digest home page — Z1 dashboard feature 8 (T-2026-CU-9050-160)

Eighth feature block on the T-151 shell: a digest summary right at the top of the home page
(above the fleet registry) that, for a configurable window (default "overnight" = last 8h,
`?window=` switch), shows at a glance what happened — aggregated net PnL (sum %),
trade count, overall win rate as stat tiles, plus the top bot/flop bot of the window and the
notable trades (biggest win, biggest loss with coin + bot).

- `tools/analytics_api.py`: additive `overnight_digest(con, window_hours, *, as_of=None, bots=None)` +
  `_regime_changes_in_window()` (genuine regime transitions via `lag()`, only when
  `regime_history` exists). A half-open window over `close_time` (`> as_of-Nh AND <= as_of`); `as_of=None`
  → data-anchored on `max(closed_at)`. Reuses the decisive-trade CTE (`_outcomes_cte_with_coin`)
  — existing aggregates (`bot_trade_rows`/`bot_leaderboard`/`success_rate_timeseries`/`bot_regime_matrix`)
  unchanged in content. Empty window/empty substrate → an all-None degrade (no 500).
- `tools/dashboard/app.py`: `/panels/overnight-digest` route, `resolve_digest_window` (an unknown
  `?window=` → default, no 500), `_digest_context`; the data-status badge source is closed_ai_signals.
- `templates/panels/overnight_digest.html` (new) + wired in right at the top of `index.html`; `static/css/app.css`
  digest tiles.
- `backtest/test_dashboard_digest.py` (new): DB-free tests including a real integration test
  (AnalyticsExporter→DuckDB→Flask index→HTML) + mutation checks (window boundary `>`→`>=`, top/flop sort).
- **Transparency note:** `ruff format` (CI pin 0.15.17) mechanically re-wrapped some *existing* lines
  in `analytics_api.py`/`app.py` while formatting (pure whitespace, no logic; `tools/` is
  excluded from the CI format check anyway) — left at the 0.15.17-canonical form instead of reverted.

## [2026-07-18] Coin drilldown with a level chain — Z1 dashboard feature 7 (T-2026-CU-9050-159)

Seventh feature block on the T-151 shell: a level chain — a coin selector (lists only coins with at least
one decisive trade) -> price line + trade markers + a trade table for the selected coin. Full
OHLCV candles are explicitly NOT in scope (the 25GB candle export was deferred in T-131 and isn't in the
DuckDB substrate) — documented as a follow-up, gated on the candle export.

- `tools/analytics_api.py`: new additive functions `coins_with_trades()` + `coin_trade_series()` over
  their own coin-aware CTE (`_outcomes_cte_with_coin`) — the same `MICRO_PNL_PCT`/`MAX_ABS_PNL_PCT` thresholds as
  `_outcomes_cte`, but with a coin/entry/exit/target-hit projection that the existing CTE doesn't carry. The
  existing aggregate functions (`_outcomes_cte`, `bot_trade_rows`, `bot_leaderboard`,
  `success_rate_timeseries`, `bot_regime_matrix`) remain byte-for-byte unchanged. `targets_hit` is `None`
  for a `closed_trades` row (the table has no such column) — never a fabricated 0.
- `tools/dashboard/app.py`: new route `GET /panels/coin-drilldown`, a new `PANEL_SOURCES` entry
  (`closed_ai_signals` + `closed_trades`), `_resolve_coin()` (no `?coin=` -> the first available coin;
  an unknown/empty value -> a clean notice instead of an error), `_coin_chart_series()` (entry->exit points per trade
  over `close_time`, deterministically monotone on time collisions) + win/loss markers.
- `tools/dashboard/templates/panels/coin_drilldown.html` (new) + `index.html`: a new panel with a coin selector,
  a lightweight-charts price line + markers and a trade table (close time, bot/model, direction, entry, exit, PnL,
  target hit); an empty/unknown coin and an empty substrate degrade cleanly (no 500).
- `tools/dashboard/static/js/panels.js`: a new lightweight-charts factory `coin-price-line` (vendored 4.2.3,
  `createChart`/`addLineSeries`/`setMarkers`), registered via `chart_lifecycle.js` — disposal via
  `chart.remove()` (lightweight-charts' own API, NOT ECharts' `.dispose()`).
- `backtest/test_dashboard_coin_drilldown.py` (new, 24 tests): realistic fixtures over BOTH outcome tables
  (`closed_ai_signals` + `closed_trades`), an integration test over the real
  `AnalyticsExporter`→DuckDB→route→HTML chain, a mutation check confirmed (manually verified): a removed
  coin filter turns `test_coin_trade_series_wrong_coin_filter_yields_different_trades` red. Tested: the coin list
  only with decisive trades, coin filter correct, unknown/empty coin clean, empty substrate clean,
  no Postgres, chart-factory registration including the `chart.remove()` disposal contract.
- `tools/dashboard/SPEC.md`: a new feature-7 section (AC1-AC8, out-of-scope, scope of consent).
- `ruff check .`/`ruff format --check .` green; `regression_guard verify` green (24 fixtures); all 176
  existing + new dashboard/analytics tests green. `git diff --stat`: additions only (443 lines, 0
  deletions) — existing `analytics_api` aggregate functions confirmed unchanged.
- **Follow-up:** full OHLCV candles (candlesticks) are gated on the candle export deferred in T-131 (25GB).
## [2026-07-18] Bot x regime performance heatmap — Z1 dashboard feature 6 (T-2026-CU-9050-158)

Sixth feature block on the T-151 shell: an ECharts heatmap (rows = bots, columns =
regime states from `regime_history`, cell value = win rate or avg PnL/trade, switchable via a toggle). For
every (bot, regime) cell, the DECISIVE trades of the bot whose `close_time` falls into the time window
in which that regime state was active are counted.

- `tools/analytics_api.py`: a new additive function `bot_regime_matrix()` — reuses
  `_outcomes_cte`/`_bot_filter`/`_existing_outcome_tables` (the same DECISIVE-trade definition as
  `bot_trade_rows`/`success_rate_timeseries`, unchanged). Assigns every trade to its active regime state
  via a DuckDB `ASOF LEFT JOIN` against `regime_history` (`ON closed_at >= ts`: the last classified
  regime entry BEFORE/AT the trade time — `regime_history` is an append-only log, a state applies from
  its `ts` until the next entry). Trades before the very first regime entry have no ASOF match and are
  excluded from the matrix instead of being booked into a fabricated "UNKNOWN" column. A
  (bot, regime) cell with no trades is missing from `cells` entirely (no null-value placeholder).
- `tools/dashboard/app.py`: a new route `GET /panels/regime-heatmap`, a new `PANEL_SOURCES` entry
  (`regime_history` + `closed_ai_signals` for the data-status badge), a new context function
  `_regime_heatmap_context()` (reshapes the matrix into a table-fallback form + a sparse ECharts
  heatmap series) and a local metric toggle (`resolve_regime_heatmap_metric`, win rate/avg PnL, an unknown
  value silently falls back to win rate).
- `tools/dashboard/templates/panels/regime_heatmap.html` (new) + `index.html`: a new panel with a
  metric switcher, a data-status badge, an ECharts heatmap AND a table fallback (empty cells as "—", never
  fabricated).
- `tools/dashboard/static/js/panels.js`: a new ECharts heatmap factory `bot-regime-heatmap`, registered via
  `chart_lifecycle.js` (dispose/re-init on an HTMX swap); a sensible color scale per metric (win rate
  0-100% sequential, avg PnL diverging around 0), a sparse series (a missing cell = no entry, no
  fabricated null value).
- `backtest/test_dashboard_regime_heatmap.py` (new): realistic fixtures (real `closed_ai_signals` +
  `regime_history` column names from `tools/analytics_export.py`, several bots x several regime states,
  repeated regime labels that merge into ONE column). An integration test over the real
  `AnalyticsExporter`→DuckDB→route→HTML chain. A mutation check confirmed (manually verified): a trade
  falling exactly on the regime boundary must fall into the NEW window — flipping the ASOF inequality (`>=` → `>`) turns
  `test_bot_regime_matrix_boundary_trade_joins_new_regime_window` red. Tested: correct assignment,
  a missing cell stays absent (no null value), a trade before the first regime entry excluded, an empty
  substrate degrades cleanly, no Postgres. `ruff check .`/`ruff format --check .` green; `regression_guard
  verify` green (24 fixtures, 3.13 interpreter with numpy+duckdb); all existing + new dashboard/
  analytics tests green (152 passed).

## [2026-07-18] Global success-metric toggle — Z1 dashboard feature 5 (T-2026-CU-9050-157)

Fifth feature block on the T-151 shell: a shell-global success-metric toggle (win rate /
expectancy / net PnL) in the base layout determines which figure the panels highlight. Implemented as
a `?metric=` query param, which the leaderboard panel reads — the chosen metric is shown as the highlighted
column AND used as the default sort. Sensible default (net PnL = the existing
`DEFAULT_LEADERBOARD_SORT`); an unknown `metric` value silently falls back to the default (no 500);
panels that don't know the metric ignore the toggle harmlessly.

- `tools/dashboard/app.py`: new constants `METRICS`/`DEFAULT_METRIC`/`METRIC_LABELS`/`METRIC_SORT_BY` and
  two pure, Flask-/DuckDB-free functions — `resolve_metric(raw)` (unknown/None → `DEFAULT_METRIC`) and
  `metric_sort_by(metric)` (mapping winrate→winrate, expectancy→expectancy_pct, netto-pnl→pnl_sum_pct, every
  value a key from `analytics_api._LEADERBOARD_SORT_KEYS`). `_leaderboard_context` gets an additive
  `metric` parameter (calls `bot_leaderboard(sort_by=metric_sort_by(metric))` and passes `metric`/`metric_label`
  through to the template). The `index()` and `panel_leaderboard()` routes resolve `?metric=` exactly once; the
  shell bakes the resolved value into the leaderboard panel's own hx-get URL, so that load + the `every Ns` poll
  keep the same metric (no extra round trip, no client-JS state).
- `tools/dashboard/templates/base.html`: a new toggle control (`.metric-toggle`) with three GET links to `/` with
  `?metric=…`, the active option marked. `index.html`: `metric` baked into the leaderboard hx-get URL.
  `leaderboard.html`: `metric_label` in the as-of line, a `metric-highlight` class on the active metric column
  (header + cells), consistent with the sort.
- Folded-in review-nit cleanups: (1) `static/css/app.css` — its own `--loss` token for `.pnl-negative`
  (instead of the `--stale` freshness token, semantically decoupled, same color value → no visual break);
  `--live` (byte-identical to `--accent`) removed, `.badge--live` now uses `var(--accent)`. (2) module function
  `panel_freshness()` → renamed to `panel_freshness_summary()` (collided by name with the nested
  route handler `def panel_freshness()` in `create_app()`); all four panel-context callers and the
  freshness tests adapted, behaviour-preserving. (3) test gap (T-154-MEDIUM) closed — `sort_by="winrate"`
  and `sort_by="n"` with a divergent fixture (order ≠ pnl default), so an ignored `sort_by`
  stands out.
- `backtest/test_dashboard_metric_toggle.py`: a new DB-free test suite with a realistic
  `closed_ai_signals` fixture whose three metrics rank the same three bots in THREE different orders
  (mutation check: a wrong/ignored `metric`→`sort_by` mapping renders one of the other
  orders). Tested: pure mapping (all three metrics + default + unknown), an integration test over
  the real `AnalyticsExporter`→DuckDB→route→HTML chain (sort + highlight per metric), shell-toggle rendering and
  the default fallback without a 500, no Postgres. `ruff check`/`format --check` green; `regression_guard verify` green
  (24 fixtures, 3.13 interpreter with numpy); all existing + new dashboard/analytics tests green (111
  passed — the rename breaks nothing).

## [2026-07-18] Per-panel data-status indicator — Z1 dashboard feature 4 (T-2026-CU-9050-156)

Fourth feature block on the T-151 shell: until now ONE shell-global badge (base layout) showed the
data status of the most recent sync across ALL sources. Now each of the four panels (success rate,
success-rate time comparison, leaderboard, fleet registry) shows the data status of ITS OWN source(s) — with
several sources, the OLDEST (worst-case), never a mix. The global badge stays
unchanged (an additive refinement).

- `tools/dashboard/app.py`: `freshness_summary()` gets two additive optional parameters — `sources`
  (filters the freshness rows down to named source names before aggregation) and `worst_case` (aggregates on
  the OLDEST instead of the previous default FRESHEST source when `True`). Both defaults reproduce the
  previous behaviour exactly — no existing caller affected. A new pure function `panel_freshness(rows,
  panel, *, now_utc=None)` resolves the panel's source(s) via the new constant `PANEL_SOURCES` (success rate/time comparison/
  leaderboard → `closed_ai_signals`+`closed_trades`, fleet registry → an empty tuple = no DuckDB sync) and
  delegates to `freshness_summary(..., worst_case=True)`; an unknown panel name raises `ValueError` instead of
  silently papering over it with a fallback. A new constant
  `FILE_BASED_FRESHNESS` for the file-based fleet registry (no fabricated timestamp). All four
  panel-context functions now deliver a `freshness` entry.
- `tools/dashboard/templates/_panel_freshness_badge.html`: a new parametrized badge partial (takes the
  panel-local `freshness` variable), wired into `success_rate.html`, `success_rate_timeseries.html`,
  `leaderboard.html`, `fleet_registry.html`. Renders "as of HH:MM, synced N min ago", "Live" (file-based)
  or "—" (missing freshness — never fabricated), updates on the panel's existing poll interval
  (no extra HTMX round trip). The existing global badge
  (`_freshness_badge.html`/`base.html`) stays untouched. `static/css/app.css`: a new `--live` accent color,
  `.badge--live`, `.panel__freshness`.
- `backtest/test_dashboard_freshness.py`: 12 DB-free tests with realistic freshness-row fixtures
  (real `closed_ai_signals`/`closed_trades` column names) — source-filter and worst-case-aggregation tests,
  panel→source mapping including the fleet-registry special case and an unknown panel name (`ValueError`),
  a mutation check for age-from-`synced_at`-instead-of-`last_row_ts` as well as for a wrong source
  assignment (oldest-wins must apply regardless of WHICH of the two sources is staler), missing
  freshness → `—` end-to-end over a real (empty) DuckDB, and a mandatory integration test (a real
  `AnalyticsExporter` with two sources at DIFFERENT `synced_at` → a real DuckDB → real
  `/panels/*` routes → rendered HTML shows the correct, panel-specific data status per panel,
  including proof that the fleet registry visibly differs). `ruff check`/`format --check`
  green (3.14 interpreter), `regression_guard verify` green (24 fixtures, 3.13 interpreter with numpy), all 86
  existing + new dashboard tests green.

## [2026-07-18] Success-rate time-comparison panel — Z1 dashboard feature 3 (T-2026-CU-9050-155)

Third feature panel on the T-151 shell: the full time-comparison version of the T-151 demo panel — an
ECharts line time series of the ROLLING 7/30/90d win rate per selected bot over time, with
a bot multiselect (multiple bots → multiple lines) and a window switcher, instead of just one current bar.
Builds additively on the existing T-131 substrate — `success_rate_timeseries()` stays unchanged.

- `tools/analytics_api.py`: new pure functions `_daily_buckets_by_bot()` (decisive trades grouped per
  bot/calendar day) and `_rolling_series_for_bot()` (a two-pointer sliding window over a bot's days — a trailing
  `window`-day sum per day, no re-summing per point), plus `rolling_success_rate_series()` (the public
  API, reuses `bot_trade_rows()` — the same DECISIVE-trade definition as `success_rate_timeseries` and
  `bot_leaderboard`). New constants `TIMESERIES_WINDOWS = (7, 30, 90)` / `DEFAULT_TIMESERIES_WINDOW = 30`.
  No new JSON API endpoint — the panel route calls the function directly (the pattern used by other panel routes).
- `tools/dashboard/app.py`: a new route `/panels/success-rate-timeseries` (does NOT collide with the existing
  `/panels/success-rate` demo, which stays unchanged) + `_success_rate_timeseries_context()`. A new
  `_selected_bots()` helper distinguishes "no filter submitted" (the first `load`, all bots) from "the user
  explicitly deselected every checkbox" (a genuine empty selection, detected via a hidden `filtered` form field) —
  otherwise a deliberate empty selection would silently revert to showing all bots again via `_bot_filter`'s
  "empty == no filter" convention.
- `tools/dashboard/templates/panels/success_rate_timeseries.html`: a self-refreshing HTMX widget — the
  fragment replaces itself via `hx-swap="outerHTML"`, so its own `hx-get`/`hx-trigger` attributes bake the
  CURRENT selection into the poll query on every form change (bot checkboxes, window radios) — a
  polling interval therefore never resets the user's selection. `templates/index.html`: the new "success-rate
  time comparison" panel wired in.
- `tools/dashboard/static/js/panels.js`: a new ECharts line factory `winrate-timeseries` (one line per bot,
  a `type: "time"` x-axis, `connectNulls`), registered via the existing `chart_lifecycle.js` helper
  (dispose/re-init on an htmx swap). `static/css/app.css`: new `.panel__filters`/`.panel__filter-group`/
  `.panel__filter-option` classes for the multiselect/window form.
- `backtest/test_dashboard_success_rate_panel.py`: 22 DB-free tests with a deliberately DIVERGENT fixture (RUB2:
  3 early wins followed by 4 recent losses → 7d/30d/90d rolling win rate 0% / 20% / ~42.9% on the same day;
  ABR2: an independent, likewise divergent pattern 66.7% / 75% / 80%) — pure-function tests for the
  sliding window, a mandatory integration test (a real `AnalyticsExporter` → a real DuckDB → a real route →
  rendered HTML/JSON chart series), explicit multiselect and window-switch tests with the exact
  divergent expected values (mutation check: a swapped window computation or a swapped
  bot filter makes at least one of these six values wrong), a test that the existing `/panels/success-rate`
  demo route stays untouched. `ruff check`/`format --check` green (3.14 interpreter), `regression_guard verify`
  green (24 fixtures, 3.13 interpreter with numpy), all 52 existing dashboard tests still green.

## [2026-07-18] Leaderboard + risk-metrics panel — Z1 dashboard feature 2 (T-2026-CU-9050-154)

Second real feature panel on the T-151 shell: per active bot (a model tag with at least one decisive trade
in the DuckDB substrate) a performance ranking — realized PnL (Σ%), win rate, expectancy (avg %/trade), trade count,
plus two risk metrics (max drawdown of the additive PnL curve, longest loss streak). Sorted by PnL
descending (default), `sort_by` optionally on `expectancy_pct`/`winrate`/`n`. Fully DB-free — only reads the
existing `flagged` CTE (neutral/housekeeping trades are excluded just as at the success-rate endpoint).

- `tools/analytics_api.py`: new pure functions `bot_trade_rows()` (ordered decisive-trade rows per bot,
  the same `is_decisive`/`is_win` definition as `success_rate_timeseries`), `_leaderboard_row()` (PnL sum,
  win rate, expectancy, max drawdown, loss streak from an ordered trade list — no I/O, testable in isolation)
  and `bot_leaderboard()` (grouping + sorting). Max drawdown as its own, lean pure-stdlib implementation
  (`_max_drawdown_pp`, absolute percentage points below the running peak, the same formula/convention as
  `tools.wf_significance.max_drawdown_pct`, T-2026-CU-9050-053) instead of a numpy import — this worktree has
  two separate Python interpreters (3.14 with duckdb+Flask, 3.13 with numpy+Flask), a numpy import would have broken the
  existing dashboard tests under 3.14. A new endpoint `/api/analytics/leaderboard` (pattern: the
  success-rate endpoint, same poll cache, 400 on an invalid `sort_by`).
- `tools/dashboard/app.py`: new route `/panels/leaderboard` + `_leaderboard_context()`. "Active bot" here
  deliberately means "has at least one decisive trade in the substrate" — NOT compared against the fleet-registry
  parked status (that's feature 1's responsibility, out of scope per SPEC.md).
- `tools/dashboard/templates/panels/leaderboard.html` + `templates/index.html`: a new panel with
  `hx-trigger="load, every {{ panel_poll_seconds }}s"`, a table with PnL/win rate/expectancy/trades/max drawdown/
  loss streak. New CSS classes `.pnl-positive`/`.pnl-negative` for sign color-coding.
- `backtest/test_dashboard_leaderboard.py`: 15 DB-free tests with realistic `closed_ai_signals` fixtures
  (real column names from `analytics_export.py`, realistic model tags RUB2/ABR2/MIS2) — unit tests for
  `bot_trade_rows`/`_leaderboard_row`/`_max_consecutive_losses`, sort tests (mutation check: the sort direction
  flipped → the test goes red, verified), a mandatory integration test (a real `AnalyticsExporter` → a real DuckDB
  → a real route → a rendered HTML table, including exclusion of a neutral-only bot), a Postgres-touch guard,
  index wiring. `ruff check`/`format --check` green, `regression_guard verify` green (24 fixtures).

## [2026-07-18] Fleet-registry panel — Z1 dashboard feature 1 (T-2026-CU-9050-152)

First real feature panel on the T-151 shell: per bot, model tag · live config (core parameters) · status
(active/parked) · parked-since. Fully DB-free — only `core/fleet.py` (FLEET), `control/parked/` markers and
root-level `*_meta.json` artifacts are read, no Postgres access.

- `core/bot_catalog.py`: new `families_for_script(script)` — the inverse of `script_for_tag()`. Delivers all
  model families/classic strategy names a fleet script posts (e.g. `25_smc_ml_sniper.py` → `["BB",
  "TD"]`, `3_detectors.py` → all 5 classic names).
- `core/process_control.py`: new `parked_since(script)` — a pure read-only stat on the marker file's mtime, no
  writing/deleting. The only DB-free source for "parked since when"; "active since when" doesn't exist
  file-based (no unpark event is persisted) and is deliberately NOT fabricated — renders as "—".
- `tools/dashboard/app.py`: a new route `/panels/fleet-registry` + a pure function `fleet_registry_rows()`
  (fully injectable for tests) plus `_live_model_configs()` (scans ONLY root-level `*_meta.json` — the LIVE
  artifacts, `staging_models/` deliberately excluded, CLAUDE.md rule 2) and `_config_label()`. The marker mtime
  is rendered via the sanctioned `core.time.from_unix_ts` UTC converter (R3/DTZ-compliant).
- `tools/dashboard/templates/panels/fleet_registry.html` + `templates/index.html`: a new panel with
  `hx-trigger="load, every {{ panel_poll_seconds }}s"`, reusing the freshness-badge look (`.badge`/
  `.badge--fresh`/`.badge--stale` for active/parked). A new CSS modifier class `.datatable--fleet` (left-aligned
  for text columns instead of the numeric right-align default).
- `backtest/test_dashboard_fleet_registry.py`: 19 DB-free tests — `families_for_script`/`parked_since` unit
  tests, `fleet_registry_rows()` with synthetic/injected inputs (parked vs. active, a multi-direction config,
  a missing config → "—", never a fabricated "active since when"), the route returns 200 with correct rows, the panel wired
  into the index, no Postgres touch, plus a smoke test against the real repo defaults (no
  `control/parked/` directory in this worktree → all bots render active, no crash).
## [2026-07-18] Shadow-visibility preview switched to English (T-2026-CU-9050-153)

A small follow-up fix to T-150: the shadow-preview message in the test channel (`_shadow_preview_message`) is now
English instead of German (operator request — the channel posts are supposed to be in English). The format deliberately
stays **NOT Cornix-parsable** (no `Entry:`/`Targets:`/`Stop Loss:`, no signal structure): "👻 SHADOW PREVIEW —
NOT a trade signal, no Cornix / Model … · Coin … · Side … / Ref-Entry … · Ref-SL … · Ref-TPs … / (monitored in
ai_signals only — never reaches a trading channel)". Test assertions (`backtest/test_shadow_test_channel.py`) adapted to
the English strings + the non-Cornix trigger check (4/4 green). A pure string change, no logic/
safety change. Activation: already live via CH_SHADOW_TEST — takes effect after the next fleet restart.
## [2026-07-17] Z1 dashboard shell — task 0 foundation (T-2026-CU-9050-151)

Load-bearing shell of the Z1 dashboard per framework gate **D-2026-CLD-111** (Flask + HTMX + interval polling; no
FastAPI, no SPA, no Node build on-box). Builds on the T-131 DuckDB substrate — NEVER reads live Postgres.
The old `dashboard.py` stays untouched (a parallel new analytics surface).

- `tools/dashboard/app.py`: a Flask app factory `create_app(duckdb_path)` that mounts the read-only analytics
  blueprint (`/api/analytics/*`), renders the HTMX shell layout + ONE demo panel (success rate per bot) and carries
  a pure `freshness_summary()` function. TZ trap (R3) respected: "synced N min ago" is computed STRICTLY from
  `synced_at` (UTC), never from the naive-local `last_row_ts`. The waitress entrypoint binds **127.0.0.1**
  (never 0.0.0.0 — P0.8), serving via the shared `analytics_api._serve` (no duplicate).
- `tools/analytics_api.py`: an additive, behaviour-preserving extraction of `build_analytics_blueprint()` out of
  `create_app` (same URLs/cache) — so the dashboard app mounts the endpoints instead of running a second server.
  All 25 existing T-131 tests stay green.
- `static/js/chart_lifecycle.js` (a council core deliverable): a library-agnostic chart-lifecycle manager —
  registers chart instances and calls `dispose()`/`remove()` on `htmx:beforeSwap` + re-init on
  `htmx:afterSwap`, so no canvas/WebGL contexts + listeners leak across the later 9 panels. `panels.js`
  registers the ECharts `winrate-bars` factory against it.
- `static/vendor/`: vendored (self-hosted, no CDN requests) htmx 2.0.4, TradingView Lightweight Charts 4.2.3,
  Apache ECharts 5.6.0 + a `README.md` with exact versions/sources. Responsive `static/css/app.css`.
- `backtest/test_dashboard_shell.py`: 14 DB-free tests (a synthetic DuckDB via `AnalyticsExporter`) cover
  AC1–AC7 — blueprint mounted, shell/demo panel/badge render, `chart_lifecycle.js` shipped, freshness
  age from synced_at only, 127.0.0.1 bind, no import/route triggers Postgres (a subprocess check).

## [2026-07-17] Shadow-visibility echo into an optional test channel (T-2026-CU-9050-150)

Optional pure visibility of shadow trades in Telegram, with **zero trade risk** — at Michi's request. New
env config `CH_SHADOW_TEST` (default 0 = off, fully backward compatible: without it, shadow stays DB-only). If
it's set, `core.signal_post.post_shadow_ai_signal` **additionally** echoes, per shadow trade, exactly ONE deliberately
**non-Cornix-parsable** preview to precisely this channel — never to the trading channel, never in Cornix format.

- `core/config.py`: `CH_SHADOW_TEST = _ch("CH_SHADOW_TEST")` (0 = off). The channel ID belongs in the VPS `.env`
  (rule 3, never hardcode).
- `core/signal_post.py`: `_shadow_test_channel()` (a lazy config read, testable) + `_shadow_preview_message()`
  (a preview as "👻 SHADOW PREVIEW — NOT a trade signal", ref-entry/SL/targets as text, NO Cornix trigger
  keywords/signal structure). The echo runs inside the open caller transaction (rule 8, no commit here).
  **Triple-safe:** (1) Cornix listens EXCLUSIVELY on the trading channel per `REGIME_TRADING_CHANNEL_ID` — the
  test channel is outside that; (2) the message isn't parsable even if intercepted; (3) a hard code barrier
  in `_shadow_test_channel()` — if CH_SHADOW_TEST accidentally == the trading channel, the echo is suppressed
  (return 0 + a warning), the "never the trading channel" invariant is enforced in code (a review fix). Central →
  all shadow legs (LIS1/TSM1/SKW1/XSM1/XSR1/FMR2 + ATS2/ATB2/SRA2/RUB3/EPD3) echo uniformly.
- `backtest/test_shadow_test_channel.py`: 3 DB-free tests — default-off writes NO outbox (backward compat),
  set writes EXACTLY ONE row to precisely THIS channel, the preview isn't Cornix-parsable.

Activation: `CH_SHADOW_TEST=<id>` in the VPS `.env` + a watchdog restart (operator gate). The Telegram bot must
be a member/admin of the target channel.

## [2026-07-17] FMR2 (K4) shadow leg on the FMR1 bot — monitored, never posted (T-2026-CU-9050-149)

FMR2 set up as a class-(A) shadow next to the live FMR1 bot (bot 31), so the normalization-exit retrain
collects a realized outcome history against real live prices. **Shadow = confirmation, not rollout** —
the backtest (T-148) wasn't deployable; the shadow leg NEVER posts a trade (no `telegram_outbox`),
it only writes `ai_signals`/`closed_ai_signals` under tag `FMR2` and `ml_predictions_master(posted=False)`. The
FMR1 live path is unchanged and never affected (its own tag, its own dedup, everything encapsulated in try/except).

- `core/shadow_gate.py`: FMR2 in `_LIFECYCLE` (LONG+SHORT → SHADOW) and `SHADOW_ARTIFACTS` (one binary model
  `fmr2_model.pkl` for BOTH directions — `side_short` is a feature). FMR1 stays default-LIVE (no row).
- `31_ai_fmr1_bot.py`: `_emit_fmr2_shadow()` scores the SAME `build_fmr1_row` feature row (FMR2_FEATURES ==
  FMR1_FEATURES) with the FMR2 model and emits per the §3 shadow rule (threshold set → only at
  `prob ≥ thr`; otherwise `log_prediction(posted=False)`). Fail-soft: if the artifact is missing, the bot keeps running as a plain
  FMR1 bot. Call happens BEFORE the FMR1 post logic, fully encapsulated.
- `backtest/test_shadow_gate.py`: 3 DB-free tests — FMR2 SHADOW on both sides (FMR1 stays LIVE), one pkl for
  both directions, and an end-to-end load/score/gate of the real artifact (skips if the VPS pkl is missing).

As with ATS2/ATB2/RUB3/EPD3, the real `fmr2_model.pkl` is NOT in git, it lives in `staging_models/` on the
VPS (placement = an operator step, hard rule 2). Activating the leg needs a bot-31 restart (Michi-gated).
## [2026-07-17] Rule-based shadow forwarders K1/K7/K2 — bots 37/38/39 (T-2026-CU-9050-149)

Three more rule-based (artifact-less) shadow forwarders for the study-candidate cohort, all **pure
shadow bots with no live post** (forwarder class (D) in `core/shadow_gate.py`: tag → SHADOW, NO artifact; the
bot computes the rule itself and emits on the raw signal; fails safe to silence, never live). No model, no
deployable edge — the live counter-check is via monitored, never-posted trades (`ai_signals` without `telegram_outbox`).

- **TSM1 (K1) — bot 37, event-driven, SHORT ONLY:** a 4h time-series momentum crossing (`4h|L12|k0.5`) — ROC[t]=
  close/close[t−12]−1 crosses from outside to inside the ±0.5σ band (σ=90d rolling std). The study is
  paper-refuted, but the loss comes entirely from the LONG leg; SHORT is positive in every cell
  (not refuted) → only `("TSM1","SHORT")` is registered. Shared `hvn_sr_trade_geometry` (== the study label),
  market fill, runs every 4h (00:29/04:29/… UTC). A pure `short_crossing` predicate, DB-free tested.
- **SKW1 (K7) — bot 38, weekly, BOTH legs:** cross-sectional skew rotation via the shared
  `core/moment_features.build_moment_panel` (rule 7) — LONG the bottom, SHORT the top `mom_skew_7d` decile
  (ρ=−0.88). A validated feature, no turnkey edge. A liquidity filter (bottom dollar-vol tercile excluded),
  MIN_COINS_PER_WEEK, Monday 00:31 UTC. Pure `select_deciles`, tested.
- **XSM1/XSR1 (K2) — bot 39, weekly, two competing hypotheses:** a raw F=84d return-decile rotation;
  XSM1 LONG (momentum) AND XSR1 SHORT (reversal) on the SAME top decile, independently monitored. The study is
  weak/inconsistent/overfit (0 robust cells). BTC out of the tradable set, a liquidity filter, Monday 00:37
  UTC. Pure `select_top_decile`, tested.

**Documented divergence (SKW1/XSM1/XSR1):** the studies measure a WEEK-timeout hold exit; the
shadow monitor tracks first-touch TP/SL (shared geometry). The shadow PnL is thereby a direction-faithful
first-touch validation, NOT the study's timeout PnL — deliberate, since the monitor knows no timeout exit (no
monitor rework onto live money). Fleet registration (`core/fleet.py` bots 37/38/39 group ai; `core/bot_catalog.py`
prefixes TSM/SKW/XSM/XSR); DB-free tests per bot. Activation needs a watchdog restart (Michi-gated; under
100% CPU, check capacity first — the weekly cross-section scans + decile shadows are load-relevant).

## [2026-07-17] LIS1 (K5) shadow forwarder — post-listing drift fade, bot 36 (T-2026-CU-9050-149)

First rule-based shadow forwarder of the study-candidate cohort: a new bot 36 (`36_ai_lis1_bot.py`)
fades freshly listed coins SHORT on day 3 after the Binance onboardDate — as a **pure shadow bot with no
live post**. There is no model and no deployable edge (study K5: fade-SHORT is fragile, only the day-3 cell
materially positive, a high WR ~0.70 but a deep left tail); the bot validates the signal live via monitored,
never-posted trades (`ai_signals` without `telegram_outbox`, tag `LIS1`).

- **The artifact-less forwarder class (D)** in `core/shadow_gate.py`: `("LIS1","SHORT") → SHADOW`, but NO
  entry in `SHADOW_ARTIFACTS` — the bot computes the rule itself and emits on the raw signal
  (the ROM1 precedent), no `score_artifact`. Fail-safe: if the leg isn't SHADOW (e.g. accidentally promoted),
  the bot stays silent — it NEVER posts live (the rule has no edge).
- **Signal parity with the study** (`tools/listing_drift_study.py::fade_events`, cell d3|l0.0, n=152): the trigger =
  a pure age event (the coin reaches day 3, unconditional); onboardDate from `GET /fapi/v1/exchangeInfo` (cache
  `staging_models/listing_onboard_dates.json`, fallback to the first 1h candle); geometry = the shared
  `hvn_sr_trade_geometry` (SHORT SL/targets, `ensure_min_tp_distance(min_pct=0.05)`, 3 TPs); market fill
  (`entry1==entry2`). Documented divergence: the live fill is the current close (≤1h after the day-3 anchor);
  the age window `[3d, 4d)` (no backfill of old coins); a geometry load floor of 48 1h rows (day-3-suitable,
  NOT the study's 120-row full-history floor). The LONG blacklist result (age < 180d ⇒ no LONG) is a
  separate gate and is deliberately NOT implemented (operator decision, Michi).
- **Fleet registration:** `core/fleet.py` (bot 36, group `ai`, start_delay 239) + `core/bot_catalog.py`
  (prefix `LIS` → bot 36). 6 DB-free tests (`backtest/test_lis1_bot.py`): SHADOW-without-artifact, tag→script,
  `in_fade_window` boundaries, `process_coin` gating/shadow emit (never live). Activation needs a
  watchdog restart (Michi-gated; under 100% CPU, check capacity first).

## [2026-07-17] FMR2 (K4) phase 1 — full retrain after staging: NOT deployable (T-2026-CU-9050-148)

The merged FMR2 builder/scaffold (PR #132) was run after operator approval: the full V2 dataset +
retrain + evaluation — read-only, nothing live. **Verdict: not deployable.**

- **Dataset:** 12,165 events (V2 normalization-exit labeling, since 2026-02-25; funding_cs_pctl ≥0.95 → SHORT /
  ≤0.05 → LONG; dedup 24 h), weighted WR 0.453, baseline avg PnL **−0.475%/trade**.
- **Model** (binary, 15 FMR2 features, chrono train 8229 / val 1580 / test 1811, purge 3 d, isotonically calibrated):
  **AUC val 0.548 / test 0.540** (barely above chance).
- **Val operating point** (thr 0.46): ΣPnL **−27.5%**, avg −0.048%/trade — already in-sample negative.
- **Gate uplift, test:** baseline avg −0.478%/trade → with the gate avg **−0.251%/trade** (WR 0.472, n 741/1811). The model
  HALVES the loss, but does NOT turn it positive.
- **Direction split** (full event set): SHORT avg −0.538% / LONG avg −0.418% — **both negative**, no hidden
  positive part.

**Conclusion:** the V2 normalization exit fixes the FMR1 first-touch flaw conceptually, but the funding-MR edge
doesn't exist on 2026 data (no positive expectation on either direction). **Phase 2 (bot-31 exit loop)
NOT started** — no deployable standalone. The artifact `staging_models/fmr2_model.pkl` + report stay in staging only;
no promotion into the repo root (P1.35, operator gate).

## [2026-07-17] K4 · FMR2 — funding-extreme MR with a normalization exit (builder + V2 labeling + retrain scaffold, CODE-PREP) (T-2026-CU-9050-146)

Made the S8 thesis cleanly testable: FMR1 labeled first-touch TP/SL and thereby NEVER tested the
actual idea ("hold until funding normalization OR a time stop") — exactly the FMR1 flaw
(report 15 V2 diagnosis). FMR2 now labels the normalization/timeout exit. Three additive
extensions of the existing research pipeline (reuse/extend, nothing reinvented):

- `core/research_features.py`: a shared exit predicate `fmr2_funding_normalized` (ONE source for
  the builder AND a future bot, X-R1) — SHORT normalizes as soon as `funding_cs_pctl < 0.80` OR
  `funding_z_30d < 1.0`, LONG symmetrically (`> 0.20` / `> −1.0`); named constants
  (`FMR2_SHORT/LONG_EXIT_*`, `FMR2_TIME_STOP_SETTLEMENTS = 9` = 3 days, `FMR2_CATASTROPHE_SL_PCT = 15.0`);
  `fmr2_catastrophe_sl`; `FMR2_FEATURES == FMR1_FEATURES` (identical entry contract, only the label
  changes). Native-NaN fail-safe (an indeterminate normalization does NOT close early), as-of, R1.
- `tools/fmr1_build_dataset.py`: a new `--label-version v2` path (`simulate_normalization_exit`) — the label =
  the sign of the net PnL at the **exit price of the settlement candle** (close), NOT first-touch TP/SL; the hard
  catastrophe SL stays as a touch-based first-touch net; `funding_z_30d` recomputed per settlement as-of
  (formula identical to `funding_stats`), `funding_cs_pctl` from a precomputed cross-section.
  V1/FMR1 stays bit-identical as the default (`--label-version v1` → `fmr1_events.jsonl`), V2 → `fmr2_events.jsonl`.
- `tools/new_models_train.py`: FMR2 in `STRATEGIES` (`kind=binary`, `features=FMR2_FEATURES`, `purge_days=3`
  >= the 9-settlement horizon) — reuses the existing chrono-split/purge/`pick_threshold` path,
  artifact `staging_models/fmr2_model_*.pkl` with `meta.model_id=FMR2`.

New DB-free test `backtest/test_fmr2_exit.py` (9/9 green): the predicate (SHORT/LONG/NaN/thresholds) + the walk
(time-stop@9, normalization exit, catastrophe-SL first-touch, open_at_end, settlement-close pricing).
Smoke: a mini dataset (600 synthetic events) → the retrain scaffold end-to-end exit 0, artifact
`staging_models/fmr2_model_smoke.pkl` (`model_id=FMR2`, 15 features) + build report `staging_models/fmr2_build_report.md`.
**CODE-PREP: no real retrain, no bot** — the full retrain (the one-job rule) and the bot-31 exit loop
(a close command → `telegram_outbox`, `closed_ai_signals status='CLOSED_FUNDING_NORMALIZED'`, `CH_FMR1`) are
operator-gated (Michi) and NOT executed.

## [2026-07-17] K2 · XSM1/XSR1 — cross-section momentum/reversal study (full run) (T-2026-CU-9050-143)

New read-only study script `tools/xs_momentum_study.py` built (code-prep, **full run pending** —
belongs in an orchestrator-gated one-job slot, only a smoke test here). A two-stage cross-section study
on 1d candles across the ~430d history: formation window F∈{7,14,28,56,84}d × holding window
H∈{7,14,28}d, a weekly rebalance grid, per cell **two signal variants** (raw F-return /
anchored = distance to the formation low, F5) × **two reference bases** (absolute / market-neutral =
coin−BTC) × **two directions** (XSM1-LONG top decile / XSR1-SHORT top-decile reversal) = 120 cells.
A liquidity filter (bottom volume tercile excluded via the median quote volume over F). Stage 1
= decile spreads close-to-close over H, net of fees (rule 10, `walkforward_sim.FEE_PER_SIDE`) plus
short-side funding from `funding_rates` (short receives +Σ funding_rate, pays on negative funding).
A chrono val/test split (BTC-1d midpoint), cell selection ONLY on val. Stage 2 (val-positive
cells only) = event replay with our geometry (`get_hvn_and_sr_levels(df=as-of)` → `simulate_exit`,
entry = the first 1h close after rebalance, strictly as-of). A stop criterion → the no-op verdict holds.
A resume/checkpoint machinery (streaming accumulators O(cells), atomic state in the OS temp dir NOT in the
repo, `--resume`) following the pattern of `tools/tsmom_study.py`; a RAM guard + peak RSS in the report.
**FULL RUN (527 coins, 120 cells):** verdict **`weak/inconsistent-spread (not deployable)`** — 58 cells
val-positive, 8 "passing" (val>0 AND test>0), but **0 robust**. The 8 passing cells are NOT
val+test-consistent: the val leg ~0 (≤0.075%/rebalance) against a large test leg (0.75–3.11%) — the classic
overfitting signature, no tradable edge; test WR < 0.5 (tail-driven) and the best-on-val cell
(val +4.74%) **flips out-of-sample negative (test −1.61%)**. The structure does NOT replicate robustly.

**Verdict-consistency fix:** the original `derive_verdict` already labeled "xs-edge-found" at val>0 AND
test>0 — that ignores the spec requirement of a "val+test-**consistent** net spread" and labels overfit
noise as edge. New: `MIN_ROBUST_NET_PCT = 0,3 %/Rebalance` (~3× the 0.10% round-trip fee), BOTH halves
must clear the floor ⇒ tiers `xs-edge-found` / `weak/inconsistent-spread` / `no-op`. A new `--reverdict`
mode (deterministically re-derives the verdict+report from an existing JSON, NO DB re-fold — used since the
fix came after the expensive run). Survivorship (rule 9, strongest here) documented, `fill_method=None`.
Artifacts into `staging_models/` (rule 2/7). Nothing deployed/promoted — follow-up tasks per direction would be
an operator decision (Michi), not licensed here.

**Known limitations (found in review, NOT verdict-relevant — the net result stays negative, noted as
follow-ups):** (1) the `market_neutral` frame is a no-op — the BTC-signal subtraction is a
per-rebalance scalar shift (argsort-invariant) and the PnL is absolute, so all 60 `market_neutral` cells are
byte-identical to `absolute` (beta removal NOT tested; the fix would be to beta-adjust returns/spread). (2) stage 2
(diagnostics only) enters ~1 daily bar too early (`dates[t]`=daily open via `floor('D')`, but the signal is `close[t]`)
⇒ look-ahead in the replay; the stage-1-driven verdict is unaffected (the fix = entry `dates[t]+86400`).
## [2026-07-17] K5 · LIS1 post-listing drift cohort study + fade replay (full run) (T-2026-CU-9050-144)

New read-only study script `tools/listing_drift_study.py` (K5, candidate LIS1) — code-prep, the
full-universe run stays pending for the orchestrator-gated one-job slot. Tests the thesis (F10)
that freshly listed USDT perps underperform in the first weeks/months. Elements: the listing date
per coin via ONE `GET /fapi/v1/exchangeInfo` (`onboardDate`, ms epoch UTC), cached to
`staging_models/listing_onboard_dates.json` (the only external HTTP call, public, no keys) — on a
network error, a fallback to the first 1h candle per coin (the source documented per coin). Cohort = onboardDate
inside the data window (strictly after the ~1-year retention floor, otherwise the drift isn't observable).
Forward returns day 0 → {7,30,90,180} on 1d candles, **absolute AND market-neutral** (minus BTC over
the same window — the beta confound resolved); distribution, median, %-positive, n per horizon. Fade-replay
SHORT day {3,7,14} × limit {+0%,+5%} via `simulate_exit` (first-touch, taker fee) with **mandatory
funding-cost accounting** — a SHORT is CREDITED on positive funding (longs pay shorts),
i.e. `+Σ funding_rate` over the hold (the sign set deliberately; fresh perps with extreme funding
can pay the short side). Small-n is disclosed honestly (n per cohort/horizon/cell,
no faked significance). A minimal deliverable even without a short edge: a quantified recommendation
"coin age < X days ⇒ no LONG" (implementation = a gating change ⇒ Michi). A resume/checkpoint machinery
following the `tools/tsmom_study.py` pattern (per-coin streaming accumulators, an atomic checkpoint of the
processed set into the OS temp — never into the repo — every N coins, `--resume`, a RAM guard <500 MB, peak RSS in the
report). Reuses the exit/geometry/funding stack (no new fee/geometry code). Read-only,
SELECT-only, BELOW_NORMAL; artifacts only into `staging_models/`.

**FULL RUN (cohort n=152 listings, `small_n_flag=false`):** verdict `fade-short-candidate (needs follow-up
bot task)`. **The post-listing drift is REAL, large, and consistent:** the market-neutral median (minus BTC,
beta-adjusted, beta does NOT flip the sign) day 7 **−8.3%**, day 30 −22.0%, day 90 −34.3%, day 180
−34.1%; only ~25–36% of listings positive. ⇒ **A robust finding for the risk filter "coin age < ~X days
⇒ no LONG" (minimal deliverable, strongly supported).** The fade-SHORT (entry day {3,7,14} × limit {+0%,+5%},
`simulate_exit` + funding), by contrast, is **MARGINAL and fragile:** positive medians (+3.5–6.7%) and a high WR
(0.59–0.70), BUT avg near zero to weakly positive (2 of 6 cells avg-negative) with a fat short left tail
(p5 −20 to −32%). The "candidate" label rests on the **day-3** cells (avg +1–2%) — the
neighboring **day-7** cell flips avg negative (−1.1%), a sign change across an entry day ⇒
instability; on top of that, the day-3 geometry rests on only ~72 h of 1h candles (a thin S/R base). So a
noisy "candidate", NOT a proven edge. Both deliverables (the LONG blacklist gating
resp. a fade-SHORT bot per direction) = an **operator decision (Michi); NOTHING deployed/promoted here.**
Listing data via the `exchangeInfo` GET (`onboardDate`), cached, fallback the first 1h candle.

## [2026-07-17] K11 · WSH1 — wick-reversal stop-hunt event study (full run) (T-2026-CU-9050-145)

New read-only study script `tools/wick_reversal_study.py` (15m candles; 5m retention too short, 15m ≈ 1 year).
A parametrized event grid: `lower_wick ≥ k·ATR14` (k∈{1.5,2,3}) × `volume ≥ m·vol_sma20` (m∈{3,5}) ×
close recovery ≥ 50% of the candle range — a long lower wick → LONG bounce, an upper wick → SHORT (mirrored);
entry = the **close of the closed event candle** (rule 5). ATR14/vol_sma20 are trailing and deliberately exclude the
event candle (`rolling.mean().shift(1)`), otherwise the wick inflates its own threshold.
**Two populations:** (a) all deduplicated events, (b) a cascade subset ≤ 60 min after a
`pump_dump_events` entry (time column `spike_time` TIMESTAMPTZ/UTC, window `[entry−60min, entry]`; b ⊆ a).
Labels via the existing geometry machinery (`get_hvn_and_sr_levels(df=as-of)` → `hvn_sr_trade_geometry` →
`ensure_min_tp_distance` → `simulate_exit`, strictly as-of, the exit scan starting only from the following candle, no lookahead leaks).
A chrono val/test split (calendar midpoint of the BTCUSDT 15m window), cell selection **only** on val; a stop criterion:
no cell val+test-positive ⇒ refuted (a valid no-op done, no forced positive). A resume/checkpoint
machinery following the `tsmom_study.py` pattern (streaming accumulators O(cells), atomic temp+rename state in the
OS temp dir, `--resume`/`--state-path`/`--checkpoint-every`/`--progress-every`/`--skip-cpu-check`, a RAM guard
< 500 MB, peak RSS in meta, encoding-safe prints against a cp1252 crash).

**FULL RUN (527 coins, 24 cells):** verdict **`no-op/WSH1-falsified`** — NO cell passes the
stop criterion (val>0 AND test>0 at n_test≥50). Only 3 of 24 cells are even val-positive, and the
strongest (`cascade|k3.0|LONG` val +0.35%/+0.29%) **flip out-of-sample negative** (test −0.28%/−0.25%) —
a high hit rate (WR 0.63–0.68) but net-negative, the classic overfitting/tail pattern (as in K1). The
interim checkpoint verdicts showed "edge-found" on subpopulations, which washed out at the full population.
The wick-reversal geometry does NOT replicate on our stack; nothing deployed/promoted. Report →
`staging_models/wick_reversal_study.{json,md}`. The PEX1 lesson upheld: the information sits in the intraday window,
no falling back to 1h context.

## [2026-07-17] Merge train: a CHANGELOG.md union merge driver against serial rebase conflicts (T-2026-CU-9050-142)

Fixed a recurring "merge-train failed" (2 PRs were stuck). Cause: every merge prepends a
CHANGELOG.md entry to the **top** of the same file — the Hetzner merge-train daemon
rebases every PR serially, so two simultaneous PRs are guaranteed to collide on the identical
top hunk. `.gitattributes` had no rule for CHANGELOG.md. Fix: `CHANGELOG.md merge=union`
(git's own union driver) — on conflict, git keeps **both** blocks instead of aborting the rebase,
parallel changelog appends resolve themselves automatically. The rule only needs to live on `main` (the
daemon rebases *onto* main), so it also fixes the currently stuck PRs on a re-trigger. Deliberately
**not** applied to `AUDIT_TODO.md` (checkbox toggles on existing lines — union would keep both the
checked and unchecked variant there). Verified via a synthetic two-branch rebase:
without the rule a conflict, with the rule clean + both entries preserved. Order of two simultaneous
entries not guaranteed (cosmetic). A follow-up option (a separate task): `changelog.d/` fragments for
a zero-conflict guarantee.

## [2026-07-16] K7 · MOM/SKW1 — realized-moments feature block + skewness study + retrain hookup (FULL RUN) (T-2026-CU-9050-141)

New shared X-R1 builder `core/moment_features.py` (canonical for the study, the trainer and later a bot —
no train/serve skew, as with `core/funding_features.py`/`core/breadth_features.py`): realized
**vol/skew/kurtosis** from **15m** candles (deliberately 15m instead of 5m — 5m has only ~1 month of retention, 15m ~1
year), rolling windows {24h, 7d} = {96, 672} closed bars, 6 features
(`mom_rv_24h/7d`, `mom_skew_24h/7d`, `mom_kurt_24h/7d`, paralleling the 6 funding features). As-of only on
closed candles (R1, `include_forming=False`, no lookahead); a **native-NaN policy** (P1.20 — missing
values stay NaN/`None`, NEVER `fillna(0)`); missing mandatory **columns** → `MomentFeatureError` (the X-R1 contract).
**TRAP (§K7, F6):** this is REALIZED SKEWNESS (the third moment), NOT a max/lottery feature — MAX shorts
are contraindicated in crypto; deliberately no "max return in the window" is built.

New read-only `tools/skewness_study.py` (§K7): weekly decile sorts on realized skewness —
market-neutral (coin − BTC), a liquidity filter (bottom dollar-volume tercile dropped per week),
funding costs on the short side (reusing `core/funding_features.load_funding`, the raw
`funding_rate` summed over the holding week), direction short-high-positive-skew vs. long-low-skew,
fees on both legs (`walkforward_sim.FEE_PER_SIDE`); RV/kurtosis deciles as a by-product; a chronological
val/test split (the sign must survive both halves — rule 8). BELOW_NORMAL + CPU-headroom guards
like the sister studies (K3/K6). A full-run report into `staging_models/skewness_study.{json,md}`
(the verdict + load-bearing tradeability caveats). A new `--reverdict` mode: deterministically re-derives the verdict + report
from an existing full-run JSON (NO DB re-fold) — used when `derive_verdict` was fixed after the expensive
run (the numbers are deterministic; the live DB was under load, no justification for another 527-coin read).

An additive retrain hookup in `tools/retrain_from_replay.py`: a new **DEFAULT-OFF** `--features moments` flag
(`FEATURE_HOOKS`/`resolve_extra_features`/`with_extra_features`) attaches the `MOMENT_FEATURES` block to the
feature contract of every strategy — modeled on the baked-in funding block. **Strictly additive:** without
the flag, `extra_features` is empty and the retrain is byte-identical to before (a no-op hookup, all 7 runners
passed through with an `extra_features=()` default). Attaching the names triggers NO retrain — the replay writer
still has to deliver the moment columns first (queued).

**FULL RUN (527/530 coins, 51 weeks, 15,923 rows after the liquidity filter):** verdict
`skw1-robust-spread`. The primary SKW1 L/S spread (`mom_skew_7d`, SHORT high-positive-skew / LONG low-skew,
market-neutral, liquidity-filtered, funding-/fee-accounted) is **net +2.50%/week** and stays positive in BOTH
chrono halves (val +2.51%/35 wk, test +2.48%/16 wk; 64.7% weeks positive), decile monotonicity
ρ=−0.88 **smooth across all 10 deciles** (a broad cross-section, no outlier spike).

**Verdict bug found & fixed:** the first clean run wrongly wrote `no-op/no-skew-spread` — `derive_verdict`
checked a top-level `n_weeks` that on the success path sits in `spread["all"]` (top-level only on the degenerate
return), thus burying a real spread as a false no-op. The guard on `"all" not in spread` corrected; the verdict re-derived via
`--reverdict` deterministically from the (verified) full-run numbers. Also made a
cp1252 stdout crash on the WARN of a coin symbol with non-ASCII characters encoding-safe (ASCII sanitize).

**Independent artifact check (T-133 orchestration, 2026-07-16):** stale price/survivorship/look-ahead
ruled out — `price_asof` has a staleness guard (`MAX_STALE=1d`, NaN → the row drops), the active
`coins.json` universe has ZERO mid-window delistings (survivorship even biases the short leg DOWNWARD),
as-of is clean, the BTC term cancels in the L/S spread. **The structure is real.**

**⚠ A LOAD-BEARING CAVEAT — real structure ≠ tradable edge:** the +2.50%/week is net ONLY of fees + realized
funding — NO slippage, market impact, borrow availability, or short-liquidation risk modeled. It's a
weekly full-decile-rebalance short-term-reversal sort on the most illiquid high-skew alts (only the bottom
dollar-vol tercile dropped), the LONG leg (low-skew = freshly crashed) is tail-/bounce-driven (WR < 0.5 in
EVERY decile). The headline **overstates** the realizable PnL after microstructure costs. **Therefore: `core/moment_features.py`
is now a VALIDATED retrain input (the §K7 intent fulfilled), NOT a deployable standalone spread. A
`--features moments` retrain and any deployment are an operator decision (Michi) — NOTHING deployed/promoted here.**
The retrain hookup unchanged (byte-identical to bc3069f), no retrain run. ruff green
(`core/moment_features.py` + `tools/skewness_study.py`).

## [2026-07-16] R1/TimescaleDB C-gate phase 5 prep — active bypass readers onto core.candles + a reversible write-primary flag (T-2026-CU-9050-139)

Preparation for the phase-5 table drop (~9.3k per-coin `{SYM}_{tf}[_indicators]`): every **running**
piece of code that still read the per-coin tables via raw SQL now reads through `core.candles` (hyper-capable since
T-128, live since the read cutover 2026-07-16) — otherwise it would break on the drop. Every site's byte
parity against the old raw SQL verified (read-only live VPS; indicators at **float4** precision — the intended
P3.12 `REAL→double` upgrade, which the read cutover already carried out fleet-wide).

**Read rewiring (7 files):**
- **34_ai_max1_bot** (LIVE MAX1): `score_symbol`'s 90d closes + the last closed indicator row →
  `read_candles`/`read_indicators` (`include_forming=False`).
- **23_market_tracker** (LIVE bot 23): 7 `_30m` reads in 5 report functions → `read_candles`; SUM/CASE/
  MAX/MIN move into pandas over the `Decimal` OHLCV (float parity), `include_forming=True` (a monitor,
  rule 5), the `[t7,t4)` exclusive end via a pandas filter.
- **14_ai_atb_bot**: the info-chart `SELECT *` + the 95d ATB1 detection → `core.candles` (`include_forming=True`,
  the forming candle deliberately stays in).
- **tools/walkforward_sim** (trainer): `load_mis1_frame` + `load_rub_frame` h⋈i joins →
  `read_candles_with_indicators`; **train==serving parity** (rule 7) verified against 11_ai_mis.
- **core/mis_features**: `MIS_SQL_INDICATOR_SELECT` (an i.-prefixed SQL fragment) → the shared
  `MIS_INDICATOR_COLUMNS` + `MIS_RENAME_MAP` — **ONE source** for the bot (11) AND the trainer (walkforward);
  11_ai_mis consolidated onto it.
- **tools/audit/live_parity**: the JOIN → `core.candles` (ASC → the old `iloc[::-1]` drops out).

**Write-primary flag (reversible, default off):** a new `KYTHERA_CANDLES_WRITE_PRIMARY ∈ {legacy, hyper}`
(`_write_primary()`, read-at-call-time). `legacy` (default) = today's behaviour, byte-exact. `hyper` =
`upsert_candles`/`upsert_indicators` write the `candles`/`indicators` hypertables **as primary** and
**skip** the per-coin write (DUAL_WRITE moot) — the phase-5 perf-trial mode (reads are already
hyper). A rollback asymmetry documented (a legacy gap → a backfill needed before a read rollback).

Verification: `test_candles.py` +2 resolver tests (default/legacy/hyper/unknown-reject); `test_candles_db_parity.py`
+1 DB-gated write-parity test behind `KYTHERA_CANDLES_WRITE_PARITY` (hyper-primary → the hypertable, **not** legacy;
a rollback = zero persistence). Regression guard smoke+verify green, ruff/format/mypy green (CI-relevant files).
**Out of scope** (deliberately breaks at the drop): `legacy_trainers/*`, `db_schema_analysis.py`, `tools/audit/step7_monitor_replay.py`
(a TZ-forensic throwaway). The `WRITE_PRIMARY=hyper` flip + fleet restart and the table drop itself stay Michi-gated.
## [2026-07-16] K1 · TSM1 — time-series momentum on 6h aggregates (read-only, no model) (T-2026-CU-9050-138)

New `tools/tsmom_study.py` (read-only) tests the K1 hypothesis (§K1, evidence F8 / arXiv 2602.11708v1,
"2.41 net Sharpe" — an overfitting suspicion from monthly re-optimization): does an ROC-lookback
momentum signal on 6h candles have a positive net edge fleet-wide — even with OUR geometry
(smart targets + a fixed SL) instead of the paper's ATR trailing? **A fixed grid, NO re-fitting over
time** (exactly the paper's overfitting vector): L ∈ {8,12,16,24,32} bars × threshold k ∈ {0, 0.5σ, 1.0σ}
(σ = a rolling 90d std dev of ROC_L, as-of) on a 6h resample (UTC anchors 00/06/12/18, only full
closed windows) AND native 4h candles (a resample artifact check) = 30 grid cells. Signal =
an ROC_L band crossing (sign = direction); dedupe per coin/direction/cell max. 1 open event
(re-entry only after the geometry exit). Labels TWICE per event: (a) our geometry
`get_hvn_and_sr_levels(df=as-of) → hvn_sr_trade_geometry → ensure_min_tp_distance → simulate_exit`
(first-touch TP-vs-SL on 1h candles, a round-trip taker fee — the deployable truth); (b) a paper
approximation = a time exit after H ∈ {8,16,28} bars with a wide 15% catastrophe SL. A val/test chrono split
(a fixed calendar divider = the midpoint of the BTC 1h window, 2026-01-13); the threshold touched ONLY on val, test touched once.
Shared contracts reused (nothing reinvented): `core/trade_utils` (geometry),
`walkforward_sim` (`simulate_exit`, `FEE_PER_SIDE=0.0005` → 0.10% round trip, `set_low_priority`,
`check_cpu_headroom`), `core/candles.read_candles` (closed candles only, R1). The CPU check deliberately bypassed via
`--skip-cpu-check` (VPS 100% CPU-saturated; read-only + BELOW_NORMAL, documented).
The VPS watchdog reaps stray python.exe (~every few minutes, exit 1) → the study refactored to streaming
(accumulators O(cells), NOT O(events)) + resumable (`--resume` + a state checkpoint every 25 coins
into the OS temp, NEVER into the repo) + a relaunch wrapper; **peak RSS 291 MB** over the full population (the
first uninterrupted run had died OOM-like at ~75 coins from the event list + DataFrame slice cache).

**Verdict: no-op / the paper refuted for our stack.** FULL population: **527 coins, 1,178,990 events**,
period 2025-07-14 … 2026-07-16 (NO sampling). NONE of the 30 grid cells pass the stop criterion
(val AND test positive net PnL at n_test ≥ 200) — only 3 cells are even val-positive, and ALL three flip
negative in test: the best val cell 4h|L12|k0.5 val **+0.128%** (n=11,171) → test **−0.053%** (n=32,902);
6h|L8|k0.5 val +0.028 → test −0.046; 6h|L32|k0.0 val +0.010 → test −0.107. WR is high fleet-wide
(~0.66–0.68), but avg net PnL is uniformly negative — the classic rule-8 case (a high hit rate,
bigger losers). Geometry-(a)-vs-paper-(b) divergence (the cost of the Cornix substitution): avg net (a)
−0.13% vs. (b) −0.20/−0.32/−0.27% per H; our geometry does +0.07/+0.19/+0.15 pp
BETTER per event than the paper's time exit (smart targets/a fixed SL cap losses better than the 15%-catastrophe
SL), but BOTH are net-negative; the correlation (a)↔(b) only 0.40/0.35/0.23 → the geometry substitution
materially changes the per-trade outcomes. Both labeling paths agree: the momentum paper does NOT replicate
on 2025–26 USDT perps with our exit stack. No follow-up task "bot TSM1". A negative result
is the success here — not chasing the paper's monthly refitting. Survivorship (rule 9): the population =
coins tradable today in `coins.json`, delisted pairs are missing. Only closed candles (R1), σ/ROC
trailing/as-of; exact quantiles (median/p5/p95) deliberately left out (incompatible with the O(cells)
memory budget, not verdict-bearing — n, WR and avg net are exact). Results in
`staging_models/tsmom_study.{json,md}` (rule 2: staging only).
## [2026-07-16] K6 · BRD — market breadth/dispersion feature builder + study (CODE-PREP, full run pending) (T-2026-CU-9050-140)
## [2026-07-16] K6 · BRD — market breadth/dispersion: full-run verdict "weak/mixed, not deployable" (T-2026-CU-9050-140)

Shared X-R1 builder `core/breadth_features.py` + a read-only study `tools/breadth_study.py` (§K6).
The builder computes as-of over the USDT-perp universe (1d candles + `_indicators`, EMA50/EMA200) eleven
breadth/dispersion features: the share of coins > EMA200 / > EMA50, the median 7d return, the advance/decline ratio,
return dispersion vs. BTC, plus a **TOTAL3 price proxy WITHOUT BTC/ETH**, both equal- AND volume-weighted
(level base 100, distance to the 90d regression, a 90d breakout). **An honesty note:** no genuine
market-cap weights — the price index over ~530 perps is a PROXY. **Efficiency:** ONE query per coin
(`load_universe_panels`), the cross-section scaffold built ONCE in-memory (`build_breadth_panel`), as-of =
an O(log n) lookup into the daily panel; R1 (closed candles only, D+1d ≤ ts, no lookahead). The X-R1 contract:
missing COLUMNS ⇒ `BreadthFeatureError`, NEVER `fillna(0)`; missing VALUES = excluded from the
cross-section (not zeroed). Survivorship-safe: `pct_change(fill_method=None)` (no forward-fill of
delisted coins → no fabricated 0-returns), dispersion without a BTC own-column.

**Resume/checkpoint machinery** (the live watchdog reaps foreign Python reproducibly — the same pattern as
K1/`tsmom_study.py`): the checkpoint unit is the per-coin daily panel (the kill-prone loading phase = ONE
DB query per coin); every 25 coins, the compact panel store + the processed set are written atomically into a
transient JSON state in the OS temp (never in the repo). `--resume` skips already-loaded
coins and folds the rest; a kill between checkpoints only reloads the <25-coin tail (idempotent,
per symbol → no double counting). Phase 2 (build+analysis) is re-entrant; the RAM guard aborts below 500 MB
memory capped (~18 MB store, peak RSS 187 MB), state deleted on a clean exit. The full run ran
read-only + BELOW_NORMAL in ONE pass (527/530 coins, 3 delisted; no watchdog kill needed).

**VERDICT (§K6, honest, three-valued): `weak/mixed-breadth-signal (not deployable)`.** Data basis:
21,604 RUB-LONG events (`rub_replay_365d.jsonl`, streamed, no new sim), 873 daily breadth rows,
71,588 `regime_history` rows. (a) The decisive head-to-head — a win-logit RUB-LONG (net_pnl>0),
chrono 70/30 — raises the test AUC from 0.580 (BTC regime only) to 0.622 with breadth (Δ **+0.042**,
n_test=3,641, overlap 12,134 from the regime_history start 18.01.). But the support is missing: only **2 of 11**
features are OOS sign+magnitude-stable (`brd_adv_decline_ratio`, `total3_vw_dist_reg90d`, both
marginal), **6 flip** sign val→test (an overfit signature, e.g. `total3_ew_level`
val +0.075 → test −0.224). (b) The independent `regime_history` TREND_UP test CONTRADICTS: breadth
LOWERS the test AUC 0.824 → 0.677 (Δ **−0.147**). Two OOS tests disagree ⇒ no clean, robust edge —
a near-no-op for §K6. RUB-LONG averages negative across the months (avg net −0.62%, WR 0.45). **The builder
stays as infrastructure** (HMM T-020, whitelist rework §23); a RUB-LONG breadth gate is NOT
licensed and would be an operator decision anyway (Michi). Artifacts: `staging_models/breadth_study.{json,md}`
(the full run, no SMOKE header). Shared contracts reused
(`read_candles_with_indicators` include_forming=False, `LEGACY_WRITER_TZ`, `walkforward_sim`).
## [2026-07-16] K15 · SRX — scratch-reload-exit study on ABR events (read-only, no model) (T-2026-CU-9050-137)

New `tools/scratch_exit_study.py` (read-only) tests OFFLINE the practitioner thesis (§K15, KB
`ingest-9f6511a5f951`) that on break-&-retest setups, a "scratch-reload" exit beats the fixed SL:
scratch the position immediately if a 4h candle closes BACK beyond the broken level `level_price`
(a small loss + fees), re-entry on the next cross + retest of the same level, max.
N ∈ {2,4,8} cycles, a 14-day window per event — instead of taking a full 4–12% SL hit. **No
new detector, no new walkforward run:** the event population is the existing ABR1 replay
`_X/staging_models/replay/abr1_replay_365d.jsonl` (288,281 events, 526 coins), streamed row by row
(378 MB never in RAM). Variant (a) = the already-simulated first-touch `net_pnl_pct` of the record (NOT
re-simulated, per spec); (b)/(c) only replace the loss side. The trigger field is deliberately
`level_price` (the broken line), not the fill price `entry`. Grid: (b) a hard SL, touch-based,
(c) a hard SL, close-based (its own cell, with an explicit liquidation caveat — close-based stops
underestimate the touch-based liquidation risk under leverage). Efficiency: 4h candles per coin loaded ONCE
across all 14d windows (526 coin queries instead of 288k), simulation in-memory, ONE pass per
SL mode delivers all N via cap derivation. Shared contracts reused: `walkforward_sim`
(`FEE_PER_SIDE=0.0005` → 0.10% round trip per leg, no invented fee; `set_low_priority`/
`check_cpu_headroom`). `signal_time` is naive-UTC (the writer = a UTC instant), `open_time` TIMESTAMPTZ →
robustly converted to UTC-naive; closed candles only (R1).

**Verdict: no-op / thesis refuted.** 288,211 events simulated (the FULL population, 70 without a 4h candle
skipped, NO sampling). Variant (b) beats (a) in NO cell and NO chrono half:
avg net (a) −0.10% vs. (b) −0.41…−0.52% per N; Δ(b−a) consistently negative in val AND test
(e.g. N=4: val −0.49, test −0.33). The hoped-for tail trade doesn't happen — the scratch mainly caps
the WINNERS (p95 6.5–6.8% vs. a baseline of 10.7%, because early scratch exits ahead of TP1 cut off the big
runs), while the loss tail even GROWS on stacked re-entries (p5 down to −10.3%
in (c)·N8 vs. a baseline of −9.03%). The aux cell (pure TP1-vs-touch-SL geometry without a scratch, WR
55.8%, median +2.1, avg −0.16%) shows: the malus comes from the scratch mechanic itself, not from
TP1-instead-of-ladder. Monthly and chrono splits confirm: (b)/(c) sit almost throughout below (a).
Cornix fit/bot wiring is moot as a result — the trade monitor knows neither scratch exits nor
re-entries anyway; the study is deliberately offline, nothing goes into a bot. Survivorship (rule 9): the population
= coins tradable today in `coins.json`, delisted pairs are missing → the loss tail for ALL variants is
equally optimistic, the (b)-vs-(a) comparison stays internally consistent. Results in
`staging_models/scratch_exit_study.{json,md}` (rule 2: staging only).

## [2026-07-16] K8 · SET — settlement/time-of-day study across the fleet (read-only, no model) (T-2026-CU-9050-135)

New `tools/settlement_timing_study.py` (read-only) tests the K8 hypothesis (F9): does entry proximity
to the funding settlements (00/08/16 UTC), resp. time of day, affect the expectancy of our
trades? Purely time-derived, **no funding join needed** — per trade, (a) the signed
entry offset to the next settlement (−240…+240 min in 30-min buckets) and (b) the entry hour UTC are
computed, then expectancy per bucket × direction × model tag: n, WR, avg net PnL (round-trip fee in,
winsorized AND raw), median, a simple bootstrap CI (1000 resamples, no significance theater),
a monthly split and a chrono val/test halving. Shared contracts reused:
`walkforward_sim.FEE_PER_SIDE=0.0005` (round trip 0.10%, no invented fee) and
`core/time.LEGACY_WRITER_TZ` — `open_time` is naive-local Bucharest (TZ cluster P2.1–P2.6) and is
converted to UTC **DST-correctly** (a constant offset would smear every offset across the 29.03 DST jump
by one hour). Deduped `closed_ai_signals` on (symbol, model, direction, open_time)
with the lowest id: 445,750 raw → 88,267 dedup (all with a valid UTC time analyzed).

**Verdict: timing-edge-found — but sign-stable, magnitude-weak & strongly attenuating.** 34 stable
prefer/avoid windows (18 fleet-wide), defined as sign-consistent across BOTH chrono halves with
n≥300 and a magnitude floor |Δ|≥0.5pp/trade vs. the group×direction baseline (winsorized means,
so no single legacy tail flips a bucket). The pattern is direction-coherent (LONG prefers
evening hours 17–23 UTC, SHORT avoids night/early 00–04 UTC; SHORT avoids the 0–30 min AFTER a
settlement), but the **strength collapses out-of-sample**: median |Δ| val 3.18pp → test 1.00pp
(val/test ≈ 3.18×) — the K3 attenuation finding repeats. Hence low conviction: at best useful
as a per-bot scan-minute shift, **not a hard gate**. Major confounders documented (rule
9): the population is conditioned on trades actually opened/closed — including the
**per-bot scan schedule**, which clusters entries at particular minutes/hours; a time-of-day "effect"
can be a composition/scan confound rather than genuine microstructure. WR alone isn't decisive
(rule 8). Results in `staging_models/settlement_timing_study.{json,md}` (rule 2: staging only).

## [2026-07-16] K3 · FRL — funding-risk-layer study across the fleet (read-only, no model) (T-2026-CU-9050-134)

New `tools/funding_risk_study.py` (read-only) tests the K3 hypothesis: do fleet SHORTs have
systematically worse expectancy under extreme-positive funding (a squeeze), symmetrically LONGs under
extreme-negative funding — and does this **generalize the ABR2 gate** (LONG only `fund_24h > +3 bps`,
a SHORT veto `> +1.5 bps`) fleet-wide? Analyzes the **complete (prescriptive) §K3 feature list**:
fund_24h, fund_72h, fund_7d_cum plus a **genuine cross-section percentile** `cs_pctl` (a coin at
entry time ranked against ALL other coins' as-of fund_24h — the ABR2 construct; NOT the
per-symbol self-history `fund_pctl_90d` of the builder). Uses the shared contracts:
`core/funding_features` (an as-of builder), `walkforward_sim.FEE_PER_SIDE=0.0005` (round trip 0.10%,
no invented fee) and `core/time.LEGACY_WRITER_TZ` (open_time = naive Bucharest → converted DST-correctly
to UTC, no constant offset across the 29.03 DST jump). Deduped `closed_ai_signals` on
(symbol, model, direction, open_time) with the lowest id: 445,685 raw → 88,202 dedup, 82,667 with
as-of funding (82,826 with cs_pctl). Means are shown both winsorized (1/99 pct, tail-safe) AND raw
— the raw/median values show the SHORT squeeze tail uncut.

**Verdict: direction-confirmed, magnitude-weak** (the ABR *direction* generalizes fleet-wide, a
hard fleet-wide extreme-zone veto is NOT licensed). Primary test = a per-trade Spearman of
fund_24h↔net-PnL, per direction, per chrono half, with a **magnitude floor** (|ρ|≥0.03): the
sign is ABR-conformant and stable across both halves for ALL four features (LONG>0, SHORT<0),
but the strength is weak (|ρ|≈0.06–0.12 in the val half) and **attenuates toward zero in the test
half** — the magnitude floor is held across both halves only by **cs_pctl SHORT**
(−0.057 val / −0.059 test, remarkably stable; the cross-section is more robust than absolute
funding). Raw means reveal the squeeze: SHORT@extreme-positive val −16.98% vs. a baseline of +3.57%,
but flips in the test half to +2.44% → not both-halves-stable. The ABR2 gate check is fleet-wide
direction-conformant (LONG in-gate > out-gate, SHORT in-veto < out-veto). The Q4 quintile collapses (ties
at the default funding rate — documented, not silently dropped). Results in
`staging_models/funding_risk_study.{json,md}` (rule 2: staging only). A known bias: survivorship
(530 funding vs. 716 signal symbols).

## [2026-07-16] Hyper-read backend in core/candles.py — C-gate phase 4 (dormant behind a flag) (T-2026-CU-9050-128)

The one remaining code blocker for the read cutover. `core/candles.py` now reads from the two
hypertables `candles`/`indicators` (filtered by `symbol, tf`) instead of the ~9.3k per-coin tables when
`KYTHERA_CANDLES_SOURCE=hyper` — **dormant**, the default stays
`legacy` → zero live effect until Michi flips it (+ a restart, trivially rollbackable). No bot touched
(the phase-C design intent): the core.candles read call sites route automatically.

**A hyper path** for `read_candles`, `read_indicators`, `read_candles_with_indicators`,
`latest_open_time` + a shape helper `indicator_column_names`. The old `_assert_legacy_backend()`
(threw for anything ≠ legacy) becomes `_candle_source()`: validates the flag, dispatches the reads and
lets WRITES/DELETES keep running on `hyper` — those always write the legacy tables, the
hypertables are kept fresh by the separate `KYTHERA_CANDLES_DUAL_WRITE` mirror (which has to stay ON
across the phase-4→5 window). A source flip thus only switches what the fleet READS, without
stopping ingestion.

**Exact legacy semantics preserved** (a behaviour-neutral cutover): the forming filter stays
**clock-based** (`open_time < period_start(tf, now())`), not the `is_closed` column — that
can lag the clock in an edge-candle race and would drop a row the legacy read keeps
(a parity break). `tf`/`is_closed` are genuine hypertable columns the per-coin tables lack →
excluded from every projection (the legacy shape + ordinal order; `indicator_column_names` drops
them, so `SELECT *` reads stay byte-equal). The JOIN read fences BOTH sides in
`(SELECT … OFFSET 0)` subqueries: joining two hypertables on the partition column lets
TimescaleDB pick a merge join over the ordered-append paths, which throws server-side
`mergejoin input data is out of order` — the fence removes those paths.

**`table_exists`/`list_coin_tables` stay phase-agnostic** (no hyper branch): they probe the
per-coin RELATIONS that exist under both backends until the phase-5 drop. A
`SELECT DISTINCT symbol, tf` over the 40M-row hypertable measured >20 s (the
chunk partitioning also defeats a loose index scan) and would block the 6_housekeeping
retention, which in hyper-read mode deletes the legacy tables anyway. After the phase-5 drop
both return empty/False — exactly the documented end behaviour.

Acceptance (live VPS, read-only): `backtest/test_candles_db_parity.py` proves **hyper read ==
legacy read** for BTC/ETH/SOL + smaller coins across several TFs, with/without forming, various
windows/limits — candles byte-for-byte, indicators at **float4 precision** (the legacy REAL columns
carry fewer bits than the hyper `double`; that's the intended P3.12 upgrade, not drift — the
float32 cast reproduces the REAL bit-exactly, a genuine value difference still stands out).
28 coin/TF candle reads + 21 with indicators green. DB-free: `test_candles.py` (source resolver,
an unknown-backend reject, hyper validation before the connection). Regression guard smoke+verify green,
ruff/format/mypy green. The flip itself (`SOURCE=hyper` + a fleet restart) stays Michi-gated.

## [2026-07-16] Z1 analytics substrate: an incremental DuckDB/parquet export + the first success-rate endpoint (T-2026-CU-9050-131)

First implementation task of Z1 stage 1 (ideation council T-129, curated by Michi 2026-07-16).
Builds the **single analytics data path** of the upcoming dashboard (assessment option A): the
dashboard never reads live PG directly anymore, instead a columnar substrate that a task-scheduler job
(not a bot process, the watchdog stays owner) fills incrementally.

- **`tools/analytics_export.py`** — a watermark-driven export of four sources
  (`closed_trades_master`, `closed_ai_signals` including ROM1, `ml_predictions_master`, `regime_history`)
  into DuckDB tables + date-partitioned parquet (`<src>/dt=YYYY-MM-DD/data.parquet`). Only
  **closed** rows (`posted`/`close_time IS NOT NULL`, no `ENTRY_NOT_FILLED`). Incremental
  via a **keyset cursor `(ts, id)`** with a strict `>` bound — no skipping on equal timestamps, no
  duplicates, without an import dedup. LIMIT batches + a per-session `statement_timeout` (a CPU-blip guard).
  Watermark + batch commit atomically (crash-safe resume). A **data-status field** per source
  (`last_row_ts` + `synced_at` [UTC] + `rows_total`) as a first-class output for the panel indicator.
  R3 discipline: naive-local legacy timestamps are passed through verbatim, never reinterpreted as UTC.
- **`tools/analytics_api.py`** — the first endpoint (a thin Flask blueprint, the framework decision T-130
  still open): a success-rate time series (rolling 7/30/90d, a bot multiselect, a daily series), reads **only**
  the DuckDB file. Outcome PnL-based like the realized-PnL report (23_market_tracker) — neutral on
  housekeeping/micro/outlier, win rate over decisive trades. User input parametrized (no
  SQL injection), a read-only connection per request.
- **Timescale forward-compatibility:** sources as a swappable `SourceSpec` config; candles deliberately
  out of scope (a follow-up task, only a 5m base TF).

Verification (DB-free, the build machine has no credentials): `backtest/test_analytics_export.py` 15/15 —
a synthetic fetcher (mirrors the PostgresFetcher SELECT contract) + a real DuckDB/parquet materialization;
covers a watermark tie, batching==single-batch, the closed filter, freshness, the rolling window, a DB-free import.
ruff/mypy green (tools/backtest are CI-excluded — checked locally). Both core reviews PASS. `duckdb>=1.0`
new in `requirements.txt` (a native parquet reader/writer, no pyarrow). **A real run only in a VPS session.**
## [2026-07-14] Fleet-wide shadow-mode posting + a 3-way report + regime-gating evidence (T-2026-CU-9050-125)

Three connected parts. **Nothing goes live** — shadow never posts into a channel,
artifacts stay in `staging_models/`, activation needs a fleet restart (Michi).

**Part 1 — shadow-mode posting (fleet-wide).** Every non-promoted retrain leg now produces
a MONITORED shadow trade instead of silence: an `ai_signals` row WITHOUT `telegram_outbox` →
the AI monitor (bot 8, contains no posting code) tracks it through to a realized close in
`closed_ai_signals`, without a single character ever reaching a channel (verified). New:
`core/shadow_gate.py` (a per-`(tag,direction)` lifecycle with **default-LIVE** — the gate must never
silence an existing live post; a tolerant loader for BOTH artifact formats including the
`null`-threshold retrains that the production loaders reject) + `core.signal_post.post_shadow_ai_signal`.
Wired: **ATS2** (bot 12), **ATB2** (bot 14), **SRA2** (bot 9), **RUB3** (bot 13,
`rub2_model_LONG`), **EPD3** (bot 10, `epd2_*`). RUB3/EPD3 = **collision-free challenger tags**
(rule 6, operator decision, Michi): the live legs already post under `RUB2`/`EPD2`, a shadow
under the same tag would block a live post via the active-trade check. SRA2/EPD3 show
the core point: "not deployable" was a TRAINING problem (a dead label source) — shadow REVIVED them,
because the AI monitor delivers the fresh outcomes. Purely additive, every shadow path error-encapsulated
(the live path is never affected). Master switch `KYTHERA_SHADOW_POSTING`. Spec: `docs/SHADOW_MODE_POSTING.md`.
A known inherited caveat (EPD3): the epd2 artifact was fitted on slightly shifted feature defs
(P1.41 / T-035), drift only on gap ticks — applies to the shadow just as it would to a possible live EPD2.

**Part 2 — sentiment-report 3-way breakdown.** `23_market_tracker.py:job_realized_pnl_report`
now breaks down the realized PnL into **ACTIVE (live) / SHADOW (tracked, never live) / RETIRED
(old tags)** per `(tag,direction)` via `shadow_gate.leg_status`. Classification as a pure,
testable function `realized_lifecycle_bucket`.

**Part 3 — regime-conditioned gating (evidence, no live change).** `docs/REGIME_CONDITIONED_GATING_EVAL.md`
+ read-only `tools/regime_conditioned_gating_scan.py`: yes, globally-negative sources do run
regime-positive — but the point estimate baits (ATS1-LONG/TRANSITION est +1.45%, lb −0.26 →
v2 correctly blocks). **18 cells** under globally-negative legs survive the v2 EB shrinkage
(e.g. BR1H-LONG/HIGH_VOLA lb +1.39% n_eff 1505). A vehicle already exists (the v2 whitelist) →
recommendation: flip the T-069 switch live on FRESH data + a `per_source × regime` cross-table
in the AIM2 report; no new gate, no blanket shutoff.

Verification: `backtest/test_shadow_gate.py` (14) + `backtest/test_market_tracker_lifecycle.py` (5)
new, DB-free; the full report/shadow suite 67/67, ruff/format green, regression guard smoke green.
Live effect only after a fleet restart (Michi); promoting a shadow leg stays an operator decision.

## [2026-07-14] v1-vs-v2 whitelist-flip evaluation built (048 shadow gate, T-2026-CU-9050-069)

New read-only VPS tool `tools/whitelist_v2_flip_eval.py` — the data basis for Michi's
flip decision v1→v2 of the whitelist gate (shadow columns from T-048, live since the T-068 deploy
2026-07-11). Answers the four T-069 questions: **(1)** a divergence matrix v1×v2 over the
`bot_regime_whitelist` snapshot (including lb distributions from `reason_v2`), **(2)** a counterfactual PnL
of the real gate traffic since deploy, bucketed by flip class (`v2_would_block` /
`v2_would_open` / agreement) — replay exclusively via the T-047 mechanics (`score_row`/
`load_1h`/`aggregate` imported, X-R1: no rebuilt geometry), **(3)** a volume effect
(gate rates, a ROM1-forwards/day forecast), **(4)** a summary JSON + a console report as the
decision basis — the recommendation + the flip stay with the VPS session + the operator (stop-B applies).

- Fallback paths (`no_whitelist_entry`, `whitelist_stale:*`, `*fallback*`, NULL) are untouched by the
  flip (bot 28 only swaps the 4D-cell read) → classified as `unaffected`, never scored.
- The **v1-drift metric** quantifies the snapshot approximation (bot 28 doesn't log v2 per signal,
  the whitelist table is UPSERT-only): the recorded v1 decision vs. today's snapshot.
- Prereq checks (bot-27 freshness, v2 coverage) + per-tag counters make the outage gap from
  2026-07-13 visible; `cell_missing`/`v2_missing` are counted instead of silently dropped.
- Docs/spec: `docs/WHITELIST_V2_FLIP_EVAL.md` (AC1–AC8, methodology caveats, a VPS how-to).

Verification: `backtest/test_whitelist_v2_flip_eval.py` 18/18 (a pure classification layer,
DB-free), `backtest/test_rom1_counterfactual.py` unchanged green; ruff/format green, repo mypy
green (`tools/` deliberately excluded, trap 12). The run itself needs the live DB → VPS ~17./18.07.

## [2026-07-14] ATS/TSI trainer (bot 12) rebuilt DB-based → ATS2 staging + trainer==serving parity (T-2026-CU-9050-121)

The fleet's last CSV-based legacy trainer (bot 12 TSI sniper) is switched onto the modern
replay pattern: DB → features → walk-forward label → train → staging, repeatable at
any time, R1-clean via `core.candles`, **no more CSV intermediate step**. Model artifacts
ONLY into `staging_models/` (ATS2, hard rule 2/6) — **no rollout** (Michi-gated).

**Finding correction to the task brief (verified against `audit_reports/13_x_ml_trainers.md` + the
bot inference):** the brief's legacy-trainer mapping was wrong on both axes — `BT1-*`
feeds bot 14 (ATB, parked; `BT1-ML-Trainer.py` is dead code), `BT3-*` feeds bot 13 (RUB).
**Bot 10 (pump)** loads a 10s-tick model (`vol_ratio/p_chg_60s/…` from the ticker buffer, NOT
reconstructable from `core.candles`) and already has a DB retrain with EPD2. **Only bot 12
(ATS/TSI)** was a genuine `core.candles` target — scope focused on ATS2 accordingly
(an operator decision via AskUserQuestion), the EPD2 path audited.

- **New `core/ats_features.py`** — a shared feature/detection builder (the X-R1 rule): `ATS_FEATURES`
  (a 29-column contract), `ats_cross` (a TSI signal-line crossover), `build_ats_features` (OBV/VWAP +
  29 features), `assert_features_alive`. Lifted out of `12_ai_ats_bot`'s inline logic.
- **`12_ai_ats_bot.py` rewired** onto the shared builder + `core.trade_utils.hvn_sr_trade_geometry`
  (byte-identical to the previous inline geometry) — **behaviour-neutral**: the bot keeps loading
  `model_tsi_*_robust.pkl`, live semantics unchanged. The 5th HVN/SR geometry clone drops out.
- **A walk-forward adapter** `tools/walkforward_sim.py --strategy ats` — a crossover check per closed
  1h candle, OBV-baseline parity over the 500-candle window, label = first-touch
  TP1-before-SL of the posted HVN/SR geometry via `simulate_exit` (fees included).
- **Trainer** `tools/retrain_from_replay.py --strategy ats` — a binary model per direction, a chronological
  70/15/15 split + a 7d purge, `pick_threshold_safe`, isotonic calibration → `ats2_model_{LONG,SHORT}.pkl`
  + `_meta.json` (`model_id=ATS2`). **A one-command wrapper** `tools/retrain_ats.py --days/--since`.
- **Parity test `backtest/test_ats_features.py`** (hard rule 7) — proves `build_ats_features` ==
  the earlier bot-12 serving construction (a verbatim reference copy), across several seeds AND
  window lengths (the OBV baseline depends on the window start); + a feature contract, `ats_cross`,
  an alive guard, a DB-free adapter smoke test. 5/5 green.
- **The EPD2/pump path audited** — already DB-based (`pump_dump_events` + `ticker_10s` + `core.candles`,
  R1-clean), CSV-free, staging output; no fix needed (the 10s-tick features aren't candle-based
  reconstructable). New for symmetry: a one-command wrapper `tools/retrain_pump.py --days/--since`.
- `docs/MODEL_INTENT.md` §6 (ATS2 infrastructure) + §7 (EPD2 audit) updated.

Verification: `backtest/test_ats_features.py` 5/5, `backtest/test_atb2_features.py` 10/10 (the adapter import
unchanged), ruff/format/mypy green on the CI-checked files (`core/ats_features.py`,
`12_ai_ats_bot.py`); `tools/` deliberately stays outside the lint bar (trap 12, not reformatted).

## [2026-07-13] ROM1 regime auto-closes in the realized-PnL report: the bot-28 close writer persists targets+lev (T-2026-CU-9050-116)

Follow-up to T-115 on operator instruction ("rom trades should be in there too"): the **second**
`closed_ai_signals` writer — the regime auto-close in `28_signal_orchestrator.py`
(`force_close_trades_for_regime_change`, status `CLOSED_REGIME_CHANGE`) — wrote no
targets/lev; under the exact-only rule of the realized-PnL report, ROM1 auto-closes therefore stayed
permanently invisible. Now: the SELECT for ROM1 rows also fetches `targets` + `ai_signals.lev` (the first-poll
stamp from T-115), the close INSERT passes both through; a lev fallback for unstamped
transitional rows = `get_max_leverage(symbol, ROM1_DESIRED_LEVERAGE)` (ROM1 always posts the
20x standard cap). **Deploy ordering secured:** a deterministic `information_schema` probe —
if bot 28 runs before the bot-8 migration, it keeps closing in the legacy format (a close takes priority
over report visibility), instead of disabling the regime close. The housekeeping writer
(`6_housekeeping`, DELISTED) is deliberately left untouched — neutral, filtered out by the report.

Verification (DB-free): `backtest/test_signal_orchestrator.py` 88/88 — two existing
regime-close tests extended to the new column contract (targets/lev passthrough), two new
tests (a legacy INSERT before the bot-8 migration; the lev fallback onto the ROM1 default) + a fix for a
pre-existing red test (since T-109, `_get_last_close_price` reads via `core.candles.read_candles` — the mock
patched). ruff/mypy green, a guard smoke OK. Deploy at the same Michi gate as T-115 (plus
a bot-28 restart).

## [2026-07-13] R1/TimescaleDB C-gate phase 2 (build) — dual write + backfill + 1d/1w WS removal (T-2026-CU-9050-119)

Second DB-migration phase of the R1+TimescaleDB switch (umbrella T-2026-CU-9050-018,
D-2026-CLD-109), building on phase 0. **Three reversible, dormant code slices** — each its
own PR + both core reviews PASS. **Activation (flipping the flag + a fleet deploy + running the backfill
+ parity observation → phase 3) stays fully operator-gated;** no
slice changes live behaviour on merge. Reads stay legacy until the phase-4 cutover.

- **2a — dual write (PR #110, merged):** with `KYTHERA_CANDLES_DUAL_WRITE` set (default
  OFF), `core.candles.upsert_candles`/`upsert_indicators` write the `candles`/`indicators`
  hypertables IN ADDITION to the old tables — a second INSERT in the caller's transaction
  (committed together). **No bot change** (the `closed` flag + `tf` came into
  the signatures in part 1 for exactly this). candles: `tf` + R1's `is_closed`, `ON CONFLICT
  (symbol,tf,open_time)` with `is_closed` in the SET AND `IS DISTINCT FROM` (forming→closed flips
  in-place, an unchanged re-upsert = a no-op, no WAL churn). indicators: `tf` + `is_closed`=true
  (the engine, post-R1, only computes on closed candles).
- **2b — backfill copy (PR #111, enqueued):** `tools/candles_backfill.py` copies the per-coin
  HISTORY into the hypertables once (a complement to the forward-only dual write). Idempotent
  (`ON CONFLICT DO NOTHING` — never overwrites a forward-written row), resumable
  (a progress file, a commit per table). Per-row `is_closed = (open_time < period_start(tf,now))`
  instead of the `…, true` sketch from §3 (the old tables carry the forming candle). Indicators
  copy/cast (NO recompute — D-109 #4; the old indicators keep the forming-contamination value).
  Default = a dry-run plan (9,669 target tables enumerated, read-only), `--execute` writes.
- **2c — 1d/1w WS removal (PR #112, enqueued):** `1_data_ingestion` no longer streams 1d/1w
  over WebSocket (`WS_TIMEFRAMES` = `TIMEFRAMES` − {1d,1w} in both `@kline` builders) — saving
  ~1,300 streams (an IP-throttle risk). The REST/catch-up path is UNCHANGED (still iterates
  the full `TIMEFRAMES`), 1d/1w still arrive via REST (with catch-up-cycle latency, accepted
  per D-109 #3). WS stays for 5m–4h.

**Verification:** DB-free tests (flag parsing, backfill progress/guard, the WS/REST split);
DB-gated byte tests behind `KYTHERA_CANDLES_WRITE_PARITY` (dual write + backfill write into
the real hypertables, via `conn.rollback()` zero persistence — the hypertables verified empty);
guard smoke+verify 24/24; `core.candles`/`1_data_ingestion` ruff/format/mypy clean, whole-repo
`ruff check .` green. **Open:** activation (every step Michi-gated) + phases 3–5.

## [2026-07-13] R1/TimescaleDB C-gate phase 0 — empty candles/indicators hypertables created (T-2026-CU-9050-118)

First **DB-migration phase** of the R1+TimescaleDB switch (umbrella T-2026-CU-9050-018, decisions
**D-2026-CLD-109**): creating the two **empty** target hypertables. Pure storage preparation — `core.candles`
keeps reading the OLD per-coin tables (`KYTHERA_CANDLES_SOURCE=legacy`), no bot is touched, a rollback is
trivial (`DROP TABLE` — nothing reads the new tables until the phase-4 cutover). Run on the live VPS,
a DDL step → Michi's approval before stamping + execution.

- **New module `core/candles_schema.py`** — an idempotent `ensure_hypertables(conn)` following the pattern of
  `core/oi_5m.ensure_schema` (self-committing, rollback-on-failure). Runner `python -m core.candles_schema`
  (default = a DB-free dry-run print; `--execute` flips the live DDL live).
- **`candles`** (9 columns, §1 of the migration design): `symbol, tf, open_time, open, high, low, close, volume,
  is_closed`, PK `(symbol, tf, open_time)`. `tf` is now a genuine column (was implicit in the per-coin
  table name), `is_closed` is the R1 contract (`DEFAULT false`).
- **`indicators`** (113 columns): `symbol, tf, open_time, is_closed, close` + the **108** indicator columns from
  `2_indicator_engine.get_indicator_definitions()` — **derived at build time from the ONE canonical source**
  (importlib), so the hypertable never drifts from what the engine/writer produce (report #18).
- **Decisions (D-2026-CLD-109):** **REAL→double precision** for all numeric indicator columns
  (verified: 0 `float4` in `indicators`; `trend_direction` stays `text`), **retention unlimited** (no
  policy). **Compression deliberately deferred to phase 5** (operator decision, 2026-07-13) — phase 0 only creates
  the tables + hypertable + index. `create_hypertable(...,'open_time',chunk_time_interval=>'7 days')` in the
  classic form (as `core/oi_5m` does on TS 2.26.3; equivalent to the `by_range()` from §1).
- **Verified live:** both hypertables present (1 dim `open_time`, 7-day chunks), empty, no
  compression/retention jobs, column parity against the legacy `BTCUSDT_1h_indicators` (new: exactly `{tf, is_closed}`,
  no legacy column lost). DB-free tests (`backtest/test_candles_schema.py`, 5×), guard smoke+verify 24/24,
  ruff/format/mypy clean. Both core reviews PASS (z-code-reviewer APPROVED, z-spec-compliance 9/9 ACs).
- Phase-0 gate `backtest/test_candles_db_parity.py` = **11/12**; the one failure
  (`test_include_forming_false_drops_only_forming_rows`) is a **now-anchored freshness assertion** that fails against
  a running ingestion outage (the window `[now−10·Δ, now]` empty, since the data ends at 07:25) — **not a
  phase-0 regression** (legacy reads, orthogonal to the empty hypertables).

**Open (Michi-gated):** the retrain rollout (part 2) + C-gate phases 2–5 (dual write including 1d/1w WS removal,
backfill, ≥5–7 days of parity, the read cutover, cleanup/drop of the ~9.7k old tables). The R1-AUDIT box only closes
with phase 5.

## [2026-07-13] R1/TimescaleDB block 6 part 1 — DB writer onto core.candles + 4 API gaps (T-2026-CU-9050-114)

The last code block of the R1+TimescaleDB migration (umbrella T-2026-CU-9050-018): the candle/indicator **writers**
now write and read via `core.candles`. **Pure code rewiring (part 1)** — the DB migration itself
(the retrain rollout + the C-gate) is part 2/3 and stays Michi-gated. Built on the live VPS, a live-write change
→ not enqueued autonomously, Michi's approval needed before `cu/reviews` + merge-train.

- **Four new `core/candles.py` functions (signatures frozen):** `latest_open_time(kind='indicators')`
  (the indicator watermark), `delete_candles_before(cutoff, *, kind)` (retention `<`), `delete_indicators_from(start)`
  (gap invalidation `>=`), `list_coin_tables(tf=None, *, kind=None)` (a form-based table enumeration via
  `_parse_coin_table`, replacing the raw `information_schema` scans + the `"trades"/"telegram"` substring blacklist).
- **`1_data_ingestion`:** `get_latest_open_time`→the API; `insert_fast`→`upsert_candles` with a **closed/forming split**
  at `period_start` (two calls); `_flush_to_db`→`upsert_candles(closed=k['x'])` — the **WS buffer now carries the
  genuine Binance closed flag** (the first entry point of `is_closed` via the WS path), a SAVEPOINT-per-row preserved via a second
  cursor on the same transaction.
- **`2_indicator_engine` (the highest R1 impact):** the core fix — the read site now only computes indicators on
  **closed** candles (`include_forming=False`, hard rule 5); the indicator `MAX`→`latest_open_time(kind='indicators')`;
  `write_indicators`→`upsert_indicators`, the commit moved to the caller (hard rule 8).
- **`6_housekeeping`:** the gap scan→`include_forming=False`; the gap filler→`upsert_candles(closed=True)`
  (`DO NOTHING`→`DO UPDATE … IS DISTINCT FROM`); retention→`list_coin_tables` + `delete_candles_before(kind)`;
  indicator invalidation→`delete_indicators_from`. DDL stays inline (goes away in phase C).
- **Review fix:** the gap filler counted rows **sent** instead of rows **written** (`upsert_candles` returns
  `len(rows)`), which defeated the `== 0` guard on unfillable gaps (Binance's `endTime` sends the already-
  present right-edge candle along). Fixed by excluding that edge candle (`>`→`>=`) — the counter now reflects
  genuine fills.
- **Verification:** DB-free (py_compile/ruff/mypy/regression guard smoke 6 + verify 24, `backtest/test_candles.py`
  47/47, 16 new); **DB parity on the live VPS** (`cryptodata`): read-only byte tests green, delete-byte tests via
  session-local `TEMP … ON COMMIT DROP` tables (gated behind `KYTHERA_CANDLES_WRITE_PARITY`, default read-only)
  green with no schema leak. Both core reviews PASS (z-code-reviewer 3-vote, z-spec-compliance 18/18 ACs). PR #104.

Open (Michi-gated): block 6 **parts 2/3** — park the ML fleet → retrain on R1-clean labels → a version bump
→ C-gate phases 0–5 (hypertable DDL/dual write/backfill/cutover/cleanup).

## [2026-07-13] Realized-PnL report for active bots in the sentiment tracker + targets/lev persistence on an AI close (T-2026-CU-9050-115)

New 4h report in the sentiment-tracker channel (`CH_MARKET_DATA`): per **active** bot, the
**actually realized, leveraged** % return of the closed trades — sum % and avg % per trade
per window **8h/24h/3d/7d/30d**, windowed by **close time** (deliberately different from the existing
per-bot post, which filters by open time). Position model (operator spec): the stake split evenly across
the N published targets, every reached target realizes 1/N at the target price, the
rest closes at the close price (SL/timeout); the whole thing × leverage, a loss clamp at −100%.

- **A data-model gap closed (`8_ai_trade_monitor`):** on close, target prices and
  leverage were lost (the ai_signals row gets deleted, only `targets_hit` remained). Two **additive** columns
  `closed_ai_signals.targets` (JSON) + `.lev` (TEXT) via the existing schema-safeguard pattern
  (`ADD COLUMN IF NOT EXISTS` on startup); the close insert copies the published targets along and
  stamps `lev = get_max_leverage(symbol, 20)` — identical to every post site. **Exception UFI1**
  (SL-capped leverage, P0.6/R4): gets `NULL` instead of a wrong 20x.
- **`core/realized_pnl.py` (new, DB-free):** `parse_leverage` / `weighted_move_pct` /
  `realized_pnl_pct` — exact-only (invalid/missing values ⇒ `None`, never an approximation), an outlier bound
  ±100% pre-leverage, as in the per-bot post.
- **`core/bot_catalog.py` (new):** a central mapping of model tag/strategy name → fleet script
  (a family **prefix**, survives a tag rotation ABR1→ABR2; trap 16) + an active filter
  (`core/fleet.FLEET` minus `control/parked` markers). Unknown tags are **visibly**
  left out (a log + a footer line), never silently dropped.
- **`23_market_tracker.py`:** a new job `job_realized_pnl_report` [XX:02:30, posts at
  `hour % 4 == 0`], uses the existing dedup (report-14 key) and chunking infrastructure. AI rows
  count **only with persisted targets+lev** (operator decision, 2026-07-13: no approximation for
  legacy data — the AI windows fill in from deploy, 30d full after 30 days); classic bots
  (`closed_trades_master` has always carried target1-4+lev) are exact from day 1 across the full history.
  TZ-correct via a matched clock (trap 9): AI age via `LOCALTIMESTAMP − close_time`
  (naive local time, P1.8), classic via `NOW() AT TIME ZONE 'UTC' − posted` (naive UTC).
  Excluded: `ENTRY_NOT_FILLED`, housekeeping closes (DELISTED/CLEANUP/ORPHAN), parked bots.
  A pure info message, no Cornix block (hard rule 4).

**Review hardening (3× z-code-reviewer N-vote, every finding verified + fixed):** (1) HIGH —
classic housekeeping closes (`6_housekeeping` also writes `DELISTED` into
`closed_trades_master.status`) would have been counted as a full leveraged move → a shared
`_is_neutral_close` filter for BOTH sources. (2) HIGH — `closed_trades_master.posted` lands
as **local time** via the session TZ cast (UTC_POLICY §3, P2.6 still open), not as naive UTC → the classic
clock switched to `LOCALTIMESTAMP` (otherwise a −3h window shift + a silent drop of fresh closes);
negative ages are now counted + warned about instead of silently dropped. (3) An outlier gate additionally
on the RAW close leg (staggering dilutes a data-bug leg by N/(N−k)). (4) Migration-pending
detection via an `information_schema` probe instead of an exception-string match (which would have masked every
DB error as "migration pending"). (5) Bot 8 fails fast if targets/lev are missing after the schema
safeguard (instead of a 10s crash loop on the close path) + a `json.dumps` guard. (6) Sniper prefixes
`BB`/`TD` instead of `BB_`/`TD_` (the retrain generation `TD2_4H` would have been unmapped).

**A deliberate, documented deviation (operator info):** the spec wanted `ai_signals.lev` persisted at
the signal post via `core/signal_post.py` — what's implemented instead is a stamp on the
**first bot-8 poll (~10s after posting)** into the new column `ai_signals.lev` (an UPDATE only if
NULL), copied along on close. Fulfills the same rationale (a `max_leverage.json` change
during a trade's lifetime can no longer corrupt the historical value), without touching the ~14
signal-emission sites + their migration ordering; the remaining skew is only a cache-
generation difference poster↔bot-8 within the 10s window. UFI1 (SL-capped leverage) deliberately gets
NULL lev and never appears in the report; ROM1 regime auto-closes (bot-28 sync, currently dead)
write no targets/lev and stay excluded — follow-up candidates.

Verification (build machine, DB-free): 111 new tests green (`backtest/test_realized_pnl.py` 36,
`test_bot_catalog.py` 40 including a fleet-consistency check, `test_market_tracker_realized.py` 35);
existing market-tracker tests 27/27; ruff/format/mypy green; regression guard `smoke` OK.
Full suite: 9 failures identically pre-existing on `main` (sniper_retest/window_features), no
regression. **Reviews:** z-code-reviewer 3× independently (findings fixed, see above) +
z-spec-compliance 3× independently. **Deploy gate (Michi):** a bot-8 and bot-23 restart; the AI query
degrades gracefully until the bot-8 migration (a warn log, the classic part posts).
## [2026-07-13] TimescaleDB-R1 Phase 1 Block 5: shared feature builders research_features + regime_logic onto closed candles (T-2026-CU-9050-112)

Block 5 of the R1 migration (`docs/CANDLE_CALL_SITES.md` §4 "Stand Block 5"): the **two shared
feature builders** now read via `core.candles` with `include_forming=False` — each together with
its trainer/replay callers in the same commit (hard rule 7: Trainer == Serving == Replay). Two
commits with opposite risk profiles.

- **5a `core/research_features.fetch_context_frame`** (research bots 30-33) — raw DESC f-string SQL
  → `read_candles_with_indicators(include_forming=False)`; the `.iloc[::-1]` reversal **is dropped**
  (the API returns ASC — if it stayed in, the frame would be DESC again and `searchsorted` would land
  wrong; the INVERSE of the Block-2 trap). `CONTEXT_IND_COLS` is now **one source** in
  `core/research_features` (derived from `CONTEXT_SQL_SELECT`), imported by
  `tools/research_dataset_common.load_candles_ctx` → live and offline/training frame columns
  byte-identical by construction. **Feature parity = no-op** (the feature candle is chosen via
  `searchsorted` over open_time, independent of the forming row). **But not a full no-op:** bots
  30/31/32 take `live_price = df["close"].iloc[-1]` as the entry anchor — previously the forming 1h
  candle (≈live), now the last CLOSED one (stale by up to ~59 min). Bot `33_ai_fif1` (the only one
  deployed) **is not affected** (uses `sig["entry"]`). 30/31/32 are gated
  (`NEW_IDEAS_LIVE_POSTING`, worthless/blocked) → no real-money impact; migration to `get_live_price`
  as follow-up **T-2026-CU-9050-113**.
- **5b `core/regime_logic.compute_features`** (`26_regime_detector` live + `backtest/backfill_regime_history`
  replay, one function) — both 15m reads (`BTCUSDT_15m`/`BTCDOMUSDT_15m`) → `read_candles(include_forming=False)`.
  **Live gating change:** the forming 15m candle no longer drives `classify_regime → apply_debounce
  → regime_current → Orchestrator-Whitelist`. Backfill needs `end=` — **note, the original
  handoff mechanism was wrong:** the `include_forming` cutoff is **DB-`now()`-based**, so it does
  NOT drop the candle forming at a historical `as_of`; the correct form is
  `end=last_closed_open_time("15m", as_of)` (API `end` inclusive → the candle forming at `as_of`
  drops out). Live: no `end`. This makes a regenerated `regime_history` closed-candle-correct.
  Explicit float cast on `high/low/close` (+ BTCDOM `close`) — `core.candles` returns raw
  NUMERIC/Decimal (the Block-4 bot-22 trap).
- **Thresholds unchanged (§5 q6):** R1 deliberately lowers regime transition rates; no constant
  (TREND/CHOP thresholds, ATR multipliers, debounce counts, percentiles) was adjusted to match —
  that is post-retrain operator business.

Verification (build machine, DB-free, fleet Python 3.13.12): `ruff`/`format --check`/`mypy` green on
`core/research_features.py` + `core/regime_logic.py`; `backtest/test_feature_lookahead.py` 20/20 (two
`fetch_context_frame` tests migrated to the fake reader + new `compute_features` read-contract test
that pins live-without-`end` vs. backfill-`end=last_closed_open_time`); `test_regime_detector` +
`test_bot_regime_analyzer` 79/79; regression guard `smoke`+`verify` 24/24. **Reviews:** z-code-reviewer
3/3 PASS (independent N-vote) + z-spec-compliance PASS (7/7). PR #102 merged (Michi go-ahead).
**Post-merge VPS (open):** `backfill_regime_history.py` re-run → `regime_history` closed-correct →
TRM1 retrain (train + serve read the same table, sequential jobs).

## [2026-07-13] TimescaleDB-R1 Phase 1 Block 4 (tranche 2 complete): 22/24/25/11 + core/live_price.py onto closed candles (T-2026-CU-9050-111)

Completion of Block 4 (R1 live in the bot; `docs/CANDLE_CALL_SITES.md` §4 "Stand Block 4 —
Tranche 2 komplett"). The four remaining AI bots now read via `core.candles` with
`include_forming=False`; **Block 4 is thereby complete on the code side** (only `14_ai_atb`
remains excluded → ATB2 track T-106).

- **Sourcing decision (Michi):** the `get_live_price` helpers from `3_detectors.py` (numerically
  named, not importable) are lifted 1:1 into **`core/live_price.py`**; `3_detectors`
  re-exports both names (the batch-ticker test moves onto the real `requests` module).
  Finding: for `22`/`24`/`25`, `current_price` feeds the **detection gate** (level proximity/
  retest), not only the entry → the price must be known **during** the scan. Hence a
  **batch ticker upfront** (`get_live_prices_batch()` 1 call/cycle, `price_map.get(sym) or
  get_live_price(sym, conn)` per coin) instead of ~N HTTP calls. The §5 principle "price only after
  detection" thus applies only in a limited sense — 1 batch call/cycle, no per-coin overhead.
- **`22_ip_pattern`** — `read_candles(include_forming=False, limit=300)`, the DESC reversal is
  dropped, pivots repaint-free on the closed frame. Explicit float cast on OHLC (`core.candles`
  returns raw NUMERIC/Decimal → otherwise a `Decimal − float` crash in the QML gate).
- **`24_quasimodo`** — `read_candles_with_indicators(include_forming=False)`, the `[:-1]` drop
  is dropped. Offset shift: `touched_recently k=1..3→0..2`, `feature_idx len−2→len−1` (same
  closed candle). `candle_columns` without `symbol`.
- **`25_smc_ml_sniper`** (heaviest rework) — all end-relative offsets +1: `last_closed
  len−2→len−1`, TD freshness gates `PIVOT_WINDOW+2→+1`, `n_closed len−1→len` (breakout/follow-
  through including the last closed candle), BB anchor `extract_ml_features len−2→len−1`.
  Chart tuples stay `(len−1, …, current_price)`. TD pivot indices (`p3`) unchanged.
- **`11_ai_mis`** — `read_candles_with_indicators(include_forming=False)` in `_fetch_mis_frame`;
  `df.rename` reproduces the three `MIS_SQL_INDICATOR_SELECT` aliases (frame byte-identical to
  `tools/walkforward_sim.py`), constant untouched. Feature row `iloc[-2:-1]→iloc[-1:]`.
- **Contract 2 (`core/candles.py`)** updated to match: `11`/`12` are no longer forming readers.

Verification (build machine, DB-free, fleet Python 3.13.12): `py_compile` + `ruff check`/
`ruff format --check` + `mypy` green on all 5 files; `test_detector_batch_ticker.py` 4/4;
regression guard `verify` 24/24 after each bot. **Live behaviour change (22/24/25) → Michi go-ahead
before enqueue; 24h A/B post-merge VPS; thresholds only after retrain (§5 q6).**

## [2026-07-13] TimescaleDB-R1 Phase 1 Block 4 (tranche 2, partial): 12_ai_ats + 7_pattern_detector onto closed candles (T-2026-CU-9050-111)

Continuation of Block 4 (R1 live in the bot; `docs/CANDLE_CALL_SITES.md` §4). The two
**offset-rework bots** without live-CMP deferral now read via `core.candles` with
`include_forming=False`; the remaining four (`22`/`24`/`25`/`11`) follow in a focused
follow-up.

- `12_ai_ats`: `read_candles_with_indicators(include_forming=False, limit=500)`,
  the DESC reversal is dropped. The TSI crossover detection already ran on `iloc[-2]`
  (closed) → without the forming candle, the newest closed one is `iloc[-1]`, so
  `current_idx −2→−1`, `prev_idx −3→−2` (same detection candle). Entry stays from
  the closed candle (operator exception). Transitional: the 500-candle OBV baseline
  shifts by one candle, negligible until the ATS retrain (§5 q6).
- `7_pattern_detector`: `read_candles(include_forming=False, limit=168)`, the DESC reversal
  is dropped. The breakout candle was `len(df)−2` (closed) → now `len(df)−1`. The
  `iloc[:-4]` pivot buffer stays (index `len−4` is NaN-flagged anyway by `rolling(9,center)`);
  the edge pivot only loses its previous forming repaint.

Verification (build machine, DB-free, fleet Python 3.13.12): `py_compile` +
`ruff check`/`ruff format --check` + `mypy` green on both files.
`docs/CANDLE_CALL_SITES.md` §4 "Stand Block 4 — Tranche 2 Teilmenge".

## [2026-07-13] TimescaleDB-R1 Phase 1 Block 4 (tranche 1): AI-bot direct readers onto closed candles (T-2026-CU-9050-111)

Fourth rewiring block of the R1 migration (`docs/CANDLE_CALL_SITES.md` §4,
umbrella T-018) — this is where **R1 goes live in the bot**. Implemented per Michi's guiding
principle: **detection runs on closed candles** (`include_forming=False`), the
**live price is only needed for signal generation** and is then fetched separately via
`get_live_price` (no longer from the forming candle in the analysis frame). Because
of the money-path risk this is cut into two tranches; **no autonomous merge** —
sign-off by Michi before enqueue.

**Tranche 1** — six direct readers without offset rework/live-CMP conversion now read
via `core.candles` with `include_forming=False`: `13_ai_rub` (both reads; no-op,
the previous `< date_trunc('hour',NOW())` filter is identical for 1h to the
closed cutoff), `15_ai_master.load_market_row` (as-of `< floor(ts)`, no-op via
`end = floor − timeframe_delta`), `9_ai_sr.get_indicators_at_time` (as-of
`end=trade_ts`; tightened at the edge — a trade in the middle of the running hour
previously got partial indicators), `10_pump_dump.get_indicators_at_time` (real
R1 change: `DESC LIMIT 1` without a bound read the forming indicator row),
`18_ai_abr1` (self-test + live; `include_forming=False` == previous
`open_time < current_hour_utc` cut, `limit=` replaces `.tail()`),
`29_ufi1.load_daily_ohlcv` (real R1 change: the unbounded read pulled in the
forming 1d candle; 29 already fetches the live price separately via `get_live_price`).

The dict readers (9/10/13-ind) now build the feature dicts from `df.iloc[-1].to_dict()`
instead of `dict(zip(cur.description, row))`. The real R1 changes (10, 29) **deliberately
lower the signal rates** — the 24h A/B is a post-merge VPS observation,
thresholds are only tuned after the retrain (§5, question 6).

**Operator decisions recorded** (`CANDLE_CALL_SITES.md` §5): close grace
`0`; guiding principle detection=closed / live price=`get_live_price`-at-generation
consistent for all Block-4 bots incl. 11/12 (supersedes the first
§5.5 "True+Split" interim state).

**Tranche 2 (follow-up task):** `7_pattern_detector`, `12_ai_ats` (offset reworks),
`22_ip_pattern`/`24_quasimodo`/`25_smc_ml_sniper` (live-CMP deferral) and
`11_ai_mis` (closed features + `get_live_price` entry + alias reproduction).
`14_ai_atb` remains excluded (parked → ATB2 track T-106).

Verification (build machine, DB-free, fleet Python 3.13.12): `py_compile` of all 6
files, `ruff check`/`ruff format --check`/`mypy` green, regression guard
`smoke` (6 fixtures) + `verify` (24/24) green. `docs/CANDLE_CALL_SITES.md` §4
"Stand Block 4 — Tranche 1".

## [2026-07-13] TimescaleDB-R1 Phase 1 Block 3: monitors + orchestrator + price fallbacks explicit onto core.candles (T-2026-CU-9050-109)

Third rewiring block of the R1 migration (`docs/CANDLE_CALL_SITES.md` §4,
umbrella T-018). The seven remaining price/scoring readers on the money path now read
via `core.candles` with **explicit `include_forming=True`** — the deliberate
"make the `True` visible and reviewable BEFORE the first `False` lands on the
money path" block. Pure read-only code rewiring, no DB schema touched.

**Unlike Block 2, behaviour-preserving:** `include_forming=True` = no forming
filter, so the candles read are byte-identical to today (newest row incl.
forming). No signal-rate change, no money-path semantics change. Still
money-path files → **no autonomous merge, sign-off by Michi before enqueue**
(Block 2 precedent).

Rewired: `5_trade_monitor` + `8_ai_trade_monitor` (SL/TP scoring, 5m — first
run newest candle, otherwise from the watermark `>=`-inclusive; the list-of-dicts
structure stays untouched via `df.itertuples`, only the read goes through the API), `28_signal_
orchestrator._get_latest_price` + `._get_last_close_price`, `3_detectors.get_live_
price` DB fallback, `29_ufi1_bot.get_live_price` (1h, parked), `6_housekeeping.
_fetch_last_close_or_entry`, `core/health_monitor` DATA_STALE canary (→ `latest_
open_time(include_forming=True)`).

Two nuances documented: (1) **inventory drift corrected** — the orchestrator
sites were at `:449`/`:1063`, not at the `:352`/`:787` noted in the inventory;
`:1063` (`_get_last_close_price`) was not inventoried at all. (2) **health_monitor
age** moves from DB-side `NOW() − max(open_time)` to Python `now() −
latest_open_time`; both share the same wall clock on the VPS, the sub-second
difference is irrelevant against the minute-scale limit `STALE_LIMIT_S`. The SAVEPOINT-
wrapped price reads (28/6) keep their SAVEPOINT — `read_candles` merely opens
a second cursor on the same connection.

Verification (build machine, DB-free): `py_compile` + import smoke of all 7 files,
`ruff check`/`ruff format --check`/`mypy` green on `core/` + root bots, regression
guard `smoke` (6 fixtures) + `verify` (24 goldens) green. Live A/B is by construction
a no-op (byte-identical reads). `docs/CANDLE_CALL_SITES.md` §4 "Stand Block 3". Still
open are Block 4 (AI-bot direct readers — the first `False` on the money path) and Block 6/
C-gate (DB writer `is_closed`).

## [2026-07-13] TimescaleDB-R1 Phase 1 Block 2: strategies + 3_detectors + shared helpers onto core.candles (T-2026-CU-9050-108)

Second rewiring block of the R1 migration (`docs/CANDLE_CALL_SITES.md` §4,
umbrella T-018). Seven read sites on the **live signal path** now read via
`core.candles` with `include_forming=False` — closed candles, ASC. Pure
read-only code rewiring, no DB schema touched. Unlike Block 1 (offline),
this block changes real live behaviour, hence **no autonomous merge** —
sign-off by Michi before enqueue.

Rewired: `core/trade_utils.calculate_smart_targets` + `get_hvn_and_sr_levels`
(highest fan-in — the forming 1h candle previously fed the swing/HVN/FVG/S-R/Fib
level pool of **all** AI bots), `core/market_utils.calculate_obv`,
`strategies/strat_main_channel`, `strat_support_resistance`, `strat_volume_indicator`
and `3_detectors.run_detectors_for_timeframe` (the indicator frame of the 5 classic strats).

Two sharp traps addressed: (1) **DESC→ASC ordering** (OPUS-HANDOFF trap 1) —
`3_detectors` today hands a DESC frame to five strategy consumers, all of which
index `iloc[0]`=newest (`strat_main_channel/support_resistance/5_percent/
fast_in_out` + volume indicator). The detector read goes through the API (ASC + forming-
free) and is flipped back into exactly the DESC frame via `.iloc[::-1]` — **zero
consumer reindex**, the only behaviour change is `iloc[0]` = newest CLOSED
instead of forming candle. (2) **Strict `<` bounds** in the volume indicator
stay byte-faithful: `end = grenze − timeframe_delta("30m")` reproduces `open_time <
grenze` exactly (period-aligned open_times). `get_hvn_and_sr_levels` reproduces
`NOW() − INTERVAL '95 days'` as `utc_now() − 95d` (≤1h DST nuance immaterial for the
warmup lower bound).

Verified on the VPS against `cryptodata` (read-only SELECTs only, 150 coins):
mechanics 149/149 green — reads return ASC, forming excluded (`newest open_time
< period_start`), the detector re-flip returns DESC with `iloc[0]` = newest
closed candle, and the closed frame is byte-identical to the old query.
**The live signal-rate comparison is not measurable on this snapshot**: at check
time the fleet ingestion was ~2.4h behind (newest 1h candle 04:00 UTC), so there
is no forming candle to exclude, and historical forming snapshots are overwritten on
close. Tip-candle sensitivity as a proxy (newest closed vs. second-newest):
the restrictive 5%/fast gates flip 0/298, the S/R hit precondition
25/149 (~17%), the AI-bot level pools shift for 69-83% of coins
(avg ~4.6% relative level shift). The real 24h live A/B belongs in the follow-up
observation (fleet up + shadow) and the threshold re-tuning after retrain (Report 16) — not in
this block. Regression guard `smoke`+`verify` green (24/24), ruff/format/mypy green on
`core/` + `3_detectors.py` (`strategies/` is ruff-excluded). C-gate (hypertable/
backfill) and the AI-bot direct readers (Block 3/4) remain later blocks.

## [2026-07-13] TimescaleDB-R1 Phase 1 Block 1: offline tooling rewired onto core.candles (T-2026-CU-9050-107)

First rewiring block of the R1 migration (`docs/CANDLE_CALL_SITES.md` §4,
umbrella T-018). 12 offline read sites now read via `core.candles` instead of raw
f-string SQL, all with `include_forming=False` — closed candles, ASC. Pure
read-only code rewiring with no live signal path; no DB schema touched.

Rewired: `core/charting.py` (cosmetic 5m overlay), `tools/mis1_move_labels.py`
(+ transitively `mis2_dump_geometry_study`), `tools/regime_rules_study.py`,
`tools/retrain_sra2.py`, `tools/research_dataset_common.py` (+ transitively
fif1/fmr1/pex1/trm1), `tools/aim2_build_dataset.py`, `tools/epd2_build_dataset.py`,
`qm_ml_trainer.py`, `smc_ml_trainer.py`, `qm_backtest.py`, `smc_pattern_backtester.py`,
`backtest/smc_btc_backtest{,_v2,_v3}.py`, `tools/regression_guard/rgcore.py`.

R1 also takes effect offline: the QM/SMC trainers and the regime study previously
ran without an upper time cutoff and computed/trained on the forming candle too —
the same look-ahead class the walk-forward sim lost in T-037. The
new helper `candles_window_start(since, lookback_days)` in `research_dataset_common`
reproduces the earlier `%s::timestamptz - INTERVAL 'N days'` TZ-faithfully in Python
(one source for the window boundary; aim2/epd2 import it). The regression guard
`extract` now captures fixtures forming-free from now on — only the DB extract path,
`verify`/`smoke` stay DB-free and green (no rule-9 refresh).

Deliberately not rewired (documented in `docs/CANDLE_CALL_SITES.md`):
`fib_backtest.py` (pg_tables case-variant probe collides with the uppercase API —
its own API gap), `tools/audit/step7_monitor_replay.py` (TZ forensics throwaway script,
zero behaviour benefit against risk to the shift logic), `trainers_x/BT2-Datagrepper`
(frozen provenance, like `legacy_trainers`).

Verified on the VPS against `cryptodata` (read-only SELECTs): all readers return
ASC, the forming candle is excluded (`newest open_time < period_start`); guard
`smoke`+`verify` green, ruff/format green on the non-excluded files. The
block is pure code rewiring — the signal-rate re-tuning after retrain
(Report 16) and the C-gate (hypertable/backfill) remain later blocks.
## [2026-07-12] TimescaleDB-R1 Phase 0: byte-equality gate for core/candles.py green against the live DB (T-2026-CU-9050-018)

Phase-0 code part of the R1-plus-TimescaleDB migration. The substance part was
already merged (`core/candles.py` + `tools/candles_parity.py` + the call-site inventory from
T-034; the P3.3 validation in `load_coins` from T-096) — the one thing still open was
the Phase-0 gate marked "open — VPS" in `docs/CANDLE_CALL_SITES.md` §6 from
the design doc: **"API reads byte-identical to direct SQL"**. This gate is now
executable and green.

New: `backtest/test_candles_db_parity.py`. Two layers following the pattern of
`candles_parity.py`: (1) a DB-free canonicalization core (`canonical_cell`/
`canonical_rows` — normalizes the representational differences between
a pandas DataFrame and raw psycopg2 tuples: Timestamp↔datetime, NaN↔None,
int↔float promotion, 12-significant-digit floor against REAL/double noise)
with its own tests that run everywhere and secure the comparator itself, so
that a green DB run can never be a false positive from a broken comparator;
(2) 7 DB tests against the OLD per-coin tables: `read_candles`/`read_indicators`
byte-identical to hand-written direct SQL, `limit` returns the newest n in
ASC, `include_forming=False` drops exactly the forming rows (R1 core), the JOIN read
leaves the candle side unchanged, `latest_open_time` == `MAX(open_time)`. Without
DB credentials, the `conn` fixture skips the DB tests cleanly (`pytest.skip`) —
never a fabricated pass.

Run in a dedicated VPS owner session against `cryptodata` (BTCUSDT_1h,
8,777 rows): 10/10 green, exclusively read-only SELECTs — **no write, no
DDL, no hypertable creation** (the TimescaleDB extension + hypertable DDL +
dual-write/backfill remain a C-gate with Michi, after the T-061 rerun queue). This
concludes the Phase-0 code part; the API signatures (`read_candles`/
`read_indicators` with `include_forming` default `False`, `True` only for price checks
5/8) are frozen from now on — the parallel ATB2 session (T-104) builds against them.

## [2026-07-12] Docs: candidate addendum K13/K15 + K6-TOTAL3 from leaderboard research and operator videos (T-2026-CU-9050-105)

Second research round worked into the handoff docs (operator sign-off
Michi). `docs/MODEL_CANDIDATES_SPEC_2026-07.md`: new candidate **K13 HLW**
(Hyperliquid whale position collector + feature/lag study — per verified
research, Hyperliquid is the only venue with permanent public
per-address transparency; the Binance leaderboard is only a gray-market scraper, Bybit has
no read API; skill persistence is academically documented but never replicated for
crypto → deliberately a collector+study instead of a copy bot, bot no. 36 reserved), new
candidate **K15 SRX** (scratch-reload-exit study on ABR/BR events: exit at
candle close below entry, re-entry on cross+retest, max N cycles vs. a fixed
SL; plus touch- vs. close-SL grid cell — extracted from Michi's
YouTube videos, KB ingest-9f6511a5f951), K6 extended with the **TOTAL3 proxy** as
a mandatory breadth feature (alt index ex BTC/ETH, KB
ingest-c1e5112dea7f), order/task scoping updated.
`reports/model_ideas_research_2026-07.md`: §6 addendum with the
leaderboard findings F14-F19 (incl. the refuted 96.5% IRL claim and
the unverified whale-copy hype) and the video evaluation. Pure docs.

## [2026-07-12] K9/OIC: open-interest collector — hypertable oi_5m + 35_oi_collector.py + 30d backfill tool (T-2026-CU-9050-103)

Implementation of the time-critical candidate K9 from
`docs/MODEL_CANDIDATES_SPEC_2026-07.md` (Binance REST only holds ~30d of
OI history — every day without a collector is irrecoverably lost).
Three building blocks: **(1)** `core/oi_5m.py` — hypertable `oi_5m`
(`ts TIMESTAMPTZ, symbol, open_interest, oi_value_usdt, PK (ts, symbol)`),
Timescale jobs chunks 1d / compression after 3d (segmentby=symbol) /
retention 730d, batched insert with `ON CONFLICT DO NOTHING`, shared
payload parser for both writers (ticker_10s blueprint). **(2)**
`35_oi_collector.py` — its own lightweight process (separate failure domain,
BELOW_NORMAL): every 5 min a sweep over coins.json via
`/futures/data/openInterestHist` (period=5m, limit=1; unlike
`/fapi/v1/openInterest` this also returns the USDT valuation and grid-stamped
timestamps → real dedup keys), requests distributed across the sweep
(~530 req/5min against the 1000/5min IP limit of the /futures/data endpoints),
429/418 backoff via `core/http_retry`, kill switch `KYTHERA_OI_PERSIST=0`
(default on, idles supervised). Registered in `core/fleet.py`
(group=logger, start_delay=231; +2 PG idle connections, mind P1.34).
**(3)** `tools/oi_backfill.py` — one-off paginated ~30d initial backfill
(backwards via endTime, self-terminating; idempotent against the running
collector; dry-run smoke on BTCUSDT: 8,639 points ≈ exactly the 30d window).
Tests: `backtest/test_oi_5m.py` (DDL/insert/parsing contract, DB-free) +
the fleet anchor in `test_fleet_definition.py` extended. **Operator gate open:**
process START on the VPS = fleet intervention (the watchdog reads FLEET on import
→ watchdog restart needed) and the one-off backfill run — both Michi.
## [2026-07-12] P2.12 follow-up: --rsi-rewrite mode for recompute_indicators.py — RSI history single-domain Wilder (T-2026-CU-9050-099)

Tool for step (2) of the P2.12 sequence (the Wilder engine switch T-095 has been
active since the fleet restart on 2026-07-12 01:03 — the `rsi_*` history has since
been TWO-DOMAIN: old=ewm(span), new=Wilder). The new mode rewrites the five
`rsi_*` columns across the entire history with the Wilder recompute — deliberately
NOT position-stable (domain migration, the opposite of the T-061 trade-off,
contrasted in the docstring of both modes). Safeguards: `--dry-run` (default,
read-only session) measures changed cells and avg/max delta; a tail guard against
the bot-2 race; batched unnest UPDATEs (parameterized, NaN→NULL); idempotent
(cells ≤1e-3 RSI points apart are skipped — the second run is a
no-op); its own resume-state file; and an **engine-parity self-check** with a
witness series that hard-rejects a pre-T-095 checkout — the history can
never accidentally be written back onto span.

Read-only smoke against the live DB (`--sample 6`): 134,717 cells across 5 tables,
avg delta 5.43 RSI points (max 27.2) — consistent with the step-2 measurement (avg 4.8).
Verification: `backtest/test_rsi_rewrite_plan.py` (9 tests, DB-free, incl.
idempotency, tail, and parity boundary); the existing head-nulling tests
remain green untouched. **The execute was a C-gate** — Michi sign-off
2026-07-12 ~05:00, executed the same day: full dry run 88.4M cells
(avg delta 5.52 points), execute 88,426,142 cells across 3,831 tables in 9.6h,
0 errors, idempotency follow-up 0 cells — the history has since been single-domain
Wilder. Retrain chain re-run afterwards (the T-061 retrain from 2026-07-12 00:13
still ran on the ewm history), only then promotion.

## [2026-07-12] ATB2: rebuild of the trendline bot as a converging-channel pipeline (T-2026-CU-9050-104)

Rebuild of the dead ATB1 (bot 14, parked, audit note D, sum −172 net,
event mismatch) from scratch per `docs/MODEL_INTENT.md` §11. ATB2 no longer
trades single trendlines from a 90d close regression line, but
**converging channels** (wedge/triangle/pennant) from confirmed swing pivots
with a closed breakout. Newly built and tested DB-free (no live intervention,
no artifact on the live path — the bot remains parked until a validated verdict):
`core/atb2_features.py` (shared detection/feature source for bot + simulator
+ trainer, X-R1 rule: no-repaint pivots, §11 channel criteria, 5
WillyAlgoTrader setup features + channel geometry as XGB features,
measured-move targets, `assert_features_alive`, ATR/RSI/EMA deterministic from
OHLCV instead of pandas_ta-version-dependent), walk-forward adapter `run_atb2`
(`--strategy atb2`, label = first-touch TP1-before-SL of the measured-move
geometry via `simulate_exit` incl. fees; smart targets of the same candle as `smart_*`
comparison, §11) and retrain runner `run_atb` (`--strategy atb2`, per
direction, chronological 3-way split + 3d purge, isotonic, threshold via
`pick_threshold_safe` on validation, artifact + `_meta.json` to
`staging_models/` with `model_id=ATB2`). Fixes the X-R findings of the dead
BT1 trainer: event mismatch (X-R1), label without SL path (X-R1/X-R5),
split leakage across overlapping windows (X-R3), test-set threshold (X-R2),
silent feature death (X-R6). Verification: `backtest/test_atb2_features.py`
(9 DB-free tests, incl. end-to-end adapter) + DB-free retrain smoke
(600 synthetic events → `model_id=ATB2` artifacts, threshold correctly None with
a too-small val slice). Run book + deploy verdict criteria: `docs/ATB2_REBUILD.md`.
**Open (follow-up, gated):** label/train run on the VPS (behind the T-061 queue,
sequential jobs); bot serving rewire + P1.45 tag fix + unparking only after
a deployable out-of-time verdict (C-gate Michi).

## [2026-07-12] Docs: model-ideas research report + candidate specs as Opus handoff (T-2026-CU-9050-102)

Two new documents from the deep-research run on 2026-07-12 (101-agent workflow,
19 sources, 25 claims adversarially verified: 20 confirmed / 5 refuted):
`reports/model_ideas_research_2026-07.md` (citable findings report — BIS
funding/carry, momentum→reversal structure, TSMOM-6h preprint, post-listing
drift, inverted MAX effect, realized moments, settlement timing; incl.
a refuted-list and open questions) and `docs/MODEL_CANDIDATES_SPEC_2026-07.md`
(implementation-ready specs for 12 candidates K1-K12 in 3 tiers: TSM1,
XSM1/XSR1, funding-risk layer, FMR2, LIS1, BRD, MOM/SKW1, SET, OI collector
(time-critical — REST only holds 30d), WHI, WSH1, TRM2 re-submission; plus
documented anti-candidates). Each spec carries a hypothesis, data situation,
step plan with concrete tools/conventions (walkforward_sim, simulate_exit,
pick_threshold_safe, X-R1 builder, staging-only, one-job rule),
stop criteria after batch E, and escalation points — written as a handoff
so a follow-up agent can cut the coding tasks without follow-up questions.
Pure docs, no code/behaviour change.

## [2026-07-12] ROM1: SL-based leverage cap removed — cross margin, fixed 20x via get_max_leverage (T-2026-CU-9050-101)

Operator decision Michi: the ROM1 trades run on Binance in **cross margin**
(the Cornix message has always posted `Margin: Cross`), so liquidation depends
on the entire wallet and not on the ~1/lev price distance of the
isolated calculation. The R4 wrapper `cap_leverage_to_sl` in
`compute_rom1_trade_params` (28_signal_orchestrator.py) therefore unnecessarily
pushed down leverage on wide SLs (8% SL → 6x instead of 20x). New:
`leverage = get_max_leverage(coin, ROM1_DESIRED_LEVERAGE)` — only the
per-coin Binance cap from `max_leverage.json` now applies (coins without 20x
still automatically get their lower cap). Same rationale as the
documented MIS2 decision ("cross margin, small positions on a large
account — deliberately NO cap_leverage_to_sl"). The 15% SL-distance cap (P2.27)
and the remaining `cap_leverage_to_sl` sites (bots 21/29, isolated class
P0.5/P0.6) remain untouched; the R4 annotation in AUDIT_TODO updated
accordingly. Tests in `backtest/test_signal_orchestrator.py` switched to the
new semantics (the LONG case now asserts 20x instead of a 6x cap).

## [2026-07-12] R2(b): docs/schema.sql — canonical DDL reference from the live DB (T-2026-CU-9050-098)

Closes the schema part of root cause R2 (the fleet part (a) came with
`core/fleet.py`, T-091). `docs/schema.sql` is a curated
`pg_dump 17.6 --schema-only --no-owner --no-privileges` of the live DB `cryptodata`
from 2026-07-12: all 44 application tables — including, for the first time, the
previously completely DDL-less `ai_signals` (13 writers) and `ml_predictions_master`
(9 writers) — plus `BTCUSDT_1h`/`BTCUSDT_1h_indicators` as a representative
template of the per-coin family. The 9,789 generated tables (per-coin,
quarterly futures, yfinance forex `=X`, `_GOLD` metals, plus the
CJK-named junk-symbol tables from the P2.16 dual-writer leak class,
whose deletion remains a D5 operator gate) are documented as name families in
the file header instead of individually dumped. The file is deliberately a
**reference, not a migration**: the executing DDL remains the
`CREATE TABLE IF NOT EXISTS` sites in the bots, every live `ALTER` remains an
operator decision (§6). The dump's `\restrict` token removed so that a
regeneration diffs cleanly; the regeneration command is in the header.
Read-only job from the VPS orchestration T-2026-CU-9050-097 (job 9), ran
in parallel with the P1.13 dry run.
## [2026-07-11] P3 hygiene batch — load_coins consolidation, symbol validation, log rotation, pins, spec-drift docs (T-2026-CU-9050-096)

Pure cleanup batch from the AUDIT_TODO P3 section (P3.1-P3.8, P3.10, P3.11), each
item in its own PR. No money-path behaviour changed; where an item touches
behaviour, called out conservatively and individually. Regression guard stays
green without a refresh, the full `backtest/` suite 691 green.

**P3.1/P3.3 — load_coins consolidation + central symbol validation.** The six
copies of `load_coins` with semantic drift (chart_data_service, fib_backtest,
walkforward_sim, qm_backtest, smc_ml_trainer, qm_ml_trainer) now go through
`core.market_utils.load_coins` with new `usdt_only`/`uppercase` flags that reproduce
their local filtering. The canon centrally validates every symbol against
`[A-Z0-9]+` (drop+ERROR log, never a silent keep) — this closes all ~40 f-string
table names in one place (P3.3). No-op on the live coins.json (530 upper-case
USDT perps), so `1_data_ingestion` (T-092) sees an identical list.
Also: dead `write_to_active_trades`/`write_to_telegram_outbox` in `3_detectors`
removed (grep-verified caller-less, `write_signal_atomic` is the path), the
three byte-identical `_apply_keepalive` moved into `core/ws_utils` (the local
`import sys` stays against the mypy `platform=win32` unreachable), the TIMEFRAMES
redeclaration in `6_housekeeping` → `core.config`. DB-free test
`backtest/test_symbol_validation.py` (8 cases).

**P3.2 — log rotation.** `indicator_calculation.log` and `watchdog.log` moved from
`FileHandler` to `RotatingFileHandler` (10 MB × 3) at the **same** path — deliberately
not `setup_logging`, whose `logs/<name>.log` renaming breaks the readers
(the watchdog hang check reads `indicator_calculation.log`; the dashboard + health_monitor
read `watchdog.log`). The append-only `logs/dashboard.log` pipe (no logging
handler) gets capped to the last half above 20 MB via the new `truncate_oversized_logs`
in the 03:00 housekeeping.

**P3.4 — dependency pins.** Major pins for pandas (`>=3.0,<4`), python-telegram-bot
(`>=22,<23`), xgboost (`>=3.0,<4`) — the current state pinned, no upgrade. New
`requirements.lock.txt` = the dependency closure of requirements.txt against the
installed state (52 packages), **not** `pip freeze` (the global env carries ~230
unrelated Cu-tooling/editable installs). Header flags it as incomplete:
yfinance + pandas_ta are not installed on the DB-free build machine (T-011)
→ the authoritative full lock belongs in a VPS session.

**P3.5 — formatting / blocking IO / info leak.** The whale_logger price display
`:.2f` → `format_price` (sub-cent coins otherwise showed "$0.00"; purely informational,
no Cornix block). `open_handler`: the blocking `get_live_price`
(`requests.get`) is offloaded from the async handler via `asyncio.to_thread`,
plus an `@None` attribution fix (fallback to full_name). `describe_project`:
full-source-dump info leak documented in the docstring + runtime warning,
ignore set extended to `.git`/`.local`/`__pycache__`/`node_modules`.

**P3.6 — backtest limitations documented (docs part).** "Known limitations"
blocks in smc_pattern_backtester (FEE_RATE declared but never referenced +
survivorship + no capital/concurrency model), fib_backtest, qm_backtest, plus
a bfill-leak note at the call site of both ML trainers. No logic change. The
`[DB]` part (are delisted tables still there?) remains open.

**P3.7 — coin-level exceptions made visible.** The coin×TF loop in
24_quasimodo + 25_smc_ml_sniper swallowed errors at `logger.debug`; aligned to the
bot-29 pattern: `logger.error(..., exc_info=True)` + `conn.rollback()`,
so that a poisoned transaction does not abort every subsequent coin.

**P3.8 — matplotlib Agg.** `matplotlib.use('Agg')` before the pyplot import in
17/24/25 (otherwise crashed headless on the VPS), one line each, pattern from bot 16.

**P3.10 — spec-drift docs (verified against code first).** Two audit claims
corrected: (a) `regime_current` is set on the FIRST check/cold start, not
after the second; (b) the per-cell ↑/↓ markers are not implemented at all
(`_cell` only returns `{wr}%`, the legend is orphaned); (c) the "fallback rate in the
status post" is not missing, it just aggregates all fallback reasons instead of isolating
`regime_unstable`. Scheduler comments in 18_abr1/12_ats/13_rub named the
wrong trigger minute (10/8/12 vs. code 2/13/10) → comments corrected, the
`now.minute` guards (money path) untouched. `ml_predictions_master.trade_id` =
hardcoded 0 everywhere except 9_ai_sr_bot → documented at core/signal_post.

**P3.11 — chart directory growth.** Housekeeping cleaned up `generated_charts` and
`charts`, but not `institutional_charts` (22_ip_pattern_bot) → unbounded
growth. Added to the 03:00 cleanup (same outbox reference protection logic).

## [2026-07-11] Data-pipeline robustness — gap-continuity check, coin refresh without restart, chart_data_service watchdog (T-2026-CU-9050-092)

Three data-pipeline findings from the audit ledger (P2.13, P2.15, P2.20), all
secured with DB-free tests in `backtest/`; the regression guard stays green without
a golden refresh (the golden fixtures are gap-free, the new gap check
never fires there — it lives in the DB worker, not in `calculate_indicators_optimized`).

**P2.13 — the indicator engine rolled the window across candle gaps.** `2_indicator_engine.py`
loads a long lookback to warm up the rolling windows, but only persists
the newest tail. If candles were missing (WS outage, ingestion hang), a
"200-period MA" was computed across the real time discontinuity — garbage indicators exactly
for coins with patchy data. `find_contaminating_gap` skips symbol/TF this
cycle (instead of computing across the hole) — but **only** if the gap lies within
`MAX_INDICATOR_LOOKBACK` (200) bars before a row to be written. An
old, rolled-out gap does not freeze the coin (whose `MAX(open_time)` would
otherwise never advance). The nightly gap filler (`6_housekeeping`) fills the gap,
the next cycle computes without gaps again — self-heal. The engine was reworked in
P1.12/T-084 (`_as_of_now_window_globals`) and P1.13/T-054 (NaN warmups);
the fix works against the current structure, not the old line numbers.

**P2.15 — coin list frozen at process start.** `1_data_ingestion.py` and
`chart_data_service.py` froze the coin list at start — coins newly
listed on Binance got no data until the next restart. Both now periodically
re-read `coins.json` (updated daily at 03:00 UTC by `6_housekeeping`
— no third writer, respects P2.16) and pull in new symbols **additively**.
`chart_data_service`: its own WS worker per batch of new coins. `1_data_ingestion`
(full version, operator decision Michi): tables + a one-off 730d catch-up +
its own WS worker, coordinated across the three concurrent loops (catch-up,
freshness, WS fleet) via a shared `tracked` set that the loops snapshot
per cycle — new coins thereby also get 12h catch-up and
freshness coverage. Conservative: removed coins are never torn down (stream
teardown remains a restart matter), a torn/empty `coins.json` read is a no-op
(a coin is never dropped live from ingestion).

**P2.20 — chart_data_service without a message watchdog + a synchronous 12MB snapshot.**
`async for msg in ws` had no timeout — a silent connection (Binance accepts
the handshake but sends 0 messages) hung the worker forever, without ever
reconnecting. `_consume_with_watchdog` fetches every message with
`asyncio.wait_for(ws.recv(), 120)` and returns after 120s of silence → reconnect.
The ~12MB JSON snapshot + `os.replace` ran synchronously on the event loop and
blocked all WS consumers every 60s; the dump now runs in a thread
(`asyncio.to_thread`), the interval was widened to 300s (only the consistent
buffer snapshot is briefly copied under the lock).

## [2026-07-11] SMC sniper: unconfirmed edge pivots dropped — deliberate strategy change (P1.46 remainder, T-2026-CU-9050-093)

`25_smc_ml_sniper.py` finds swing pivots via `scipy.signal.argrelextrema` with the
default `mode='clip'`. At the right edge, `clip` compares a candidate against the
repeated edge value instead of real neighbours — a pivot in the last
`PIVOT_WINDOW` (10) closed candles is thus accepted with **fewer** than 10 real
right neighbours. Such an edge pivot is unconfirmed: if the next candle closes
above its level, the point was never a pivot — the published Three-Drive or the
breaker-block level (and thus the SL/TP computed from it) repainted **after**
the signal (money path, bot 25 posts live) had already gone out. P1.46
dropped the *forming* candle; this residual repaint at the right edge deliberately
stayed open, because the TD freshness gate (`len(df) - p3 <= PIVOT_WINDOW + 2`)
specifically looks for these fresh edge pivots — the full bot-24 filter would not have
been a drop-in.

This is an **operator-approved strategy change** (Michi, 2026-07-11), not a
bugfix. **Option B** was implemented: a shared edge filter directly after
`argrelextrema` discards pivots with fewer than `PIVOT_WINDOW//2 = 5` confirming
closed candles to the right (`peak_idx[peak_idx <= last_closed - PIVOT_WINDOW//2]`,
analogously `trough_idx`; `last_closed = len(df) - 2`, since the forming candle is already
excluded). **One** filter feeds both consumers (TD gate + `find_breaker_setup`), the
edge policy is thereby consistent. The full filter (option A, `PIVOT_WINDOW`
confirmation like bot 24) was rejected — it would have emptied out the TD freshness gate.

Signal-rate delta, measured DB-free against the regression-guard fixtures
(`tools/sniper_edge_pivot_delta.py`, current geometry incl. T-089 `find_breaker_setup`,
4 coins × 1h/4h, 3,608 scan points): **breaker block unchanged (0.0%)** — a
breaker requires breakout + follow-through *after* the pivot and is thus structurally
already confirmed; the entire effect lies in **Three-Drive** (−40% LONG / −47%
SHORT). **Overall −5.9%** (221 → 208 geometry triggers, purely subtractive — no new
trigger). Option A would have cut TD by ~90% (overall −11.8%) and effectively
shut down the detector. The residual repaint window is thereby **halved**
(≤5 instead of ≤10 candles), deliberately not zero: TD needs the fresh reversal entry.

Retrain coupling: the deployed artifacts TD2/BB2 are fitted on the **old** pivot
policy. Until the retrain rollout (operator decision), serving sees a slightly
shifted TD pattern distribution; BB is unaffected (0% delta). A retrain should relabel
on the new policy.

Verification DB-free: `backtest/test_sniper_edge_pivots.py` (new, 7/7 — edge-pivot
repaint mechanics, filter threshold exactly at `PIVOT_WINDOW//2`, ordering before the
pivot-count gate, guards for P1.46 forming-drop and T-089 `find_breaker_setup`).
`test_sniper_forming`/`test_sniper_retest_level`/`test_sniper_tag` unchanged green
(combined 24/24). ruff + format + mypy green.

## [2026-07-11] Monitors track exactly the published targets (P2.31, T-2026-CU-9050-083)

AI signal bots 9/12/13 (SRA1/ATS1/RUB1) publish TP1-3 in the Cornix block, bot 11
(MIS1) TP1-5 — so the subscriber sees 3 or 5 targets respectively. What was stored in
`ai_signals.targets`, however, was the **full** computed zone list (up to 20 from
`ensure_min_tp_distance(t_cands[:20], …)`). The AI trade monitor (`8_ai_trade_monitor.py`)
scores `range(new_targets_hit, len(targets))` against exactly what is stored, and
reports `ALL TARGETS HIT` at `len(targets)` — it has no target limit of its own. As a
result, it evaluated up to 10-20 phantom TPs that were never published. The win definition and
the trailing-SL semantics (SL moves to `targets[new_targets_hit-2]`) ran on targets
outside the signal — live statistics did not match Cornix reality.

Fix: at each bot's `ai_signals` insert site, the target list is now capped to the
published count (`json.dumps(targets[:n_show])`). `n_show` (3 or 5) is
now a named local value right at the target computation and feeds **both**
the Cornix loop and the insert — a single source, so that tracking == publication
never drifts apart again. The Cornix block itself does **not** change (rule 4):
the loop previously used `targets[:3]`/`[:5]`, now `targets[:n_show]` with an identical value,
the published message string is byte-identical. This is exclusively about the
tracking row. The monitor stays untouched — capping at the source is the correct
lever, because `n_show` lives in the publishing code and the monitor does not know the
published count at all. Pulled into scope: `core/signal_post.post_ai_signal` (research bots
30-33) had the same pattern (Cornix `targets[:n_show]`, insert the full list) on the same
`ai_signals`→monitor-8 path — likewise capped to `targets[:n_show]`.

Existing data in the DB is left untouched (a history correction would be a VPS job).
DB-free guard `backtest/test_published_targets.py`: behavioral against the real
insert path of `post_ai_signal` (stored == published Cornix targets == n_show) plus
structural guards for the four bots and the monitor scoring loop; fails on the
pre-fix state (stored 8, published 3).
## [2026-07-11] Fleet process list centralized: `core/fleet.py` as single source (R2(a)/P1.38 partial aspect, T-2026-CU-9050-091)

The process list existed twice and had drifted: `main_watchdog.py`
(`PROCESSES_TO_RUN`, authoritative, with `start_delay`, the full fleet) vs. `dashboard.py`
(`PROCESSES`, with `group`, but **without** bots 26-34). The dashboard therefore showed
only part of the running fleet and had to be manually updated by hand for every new bot.

**Fix:** the new `core/fleet.py` defines the fleet **once** (name/script/group/
`start_delay`/`restart_interval`); watchdog and dashboard import the same list.
The watchdog reads name/script/start_delay/restart_interval (ignores `group`), the
dashboard reads name/script/group/restart_interval (ignores `start_delay`) — the field
irrelevant to one consumer is a no-op for the other.

**No behaviour change on the watchdog:** identical processes, start order and
staggered delays as previously inline (`backtest/test_fleet_definition.py` pins the
authoritative projection byte-for-byte). The lifecycle mechanics — single-instance mutex/
orphan sweep/CTRL_BREAK (P0.2/P2.48) and supervision/backoff/heartbeat (P1.37/P2.47) —
are **not** touched; only the LIST was centralized.

**Visible change only in the dashboard:** it now automatically shows the full fleet
incl. the previously missing bots 26-34. Their display `group` was deliberately chosen from
the existing set (`core`/`ai`/`strategy`/`logger`) — the regime/orchestrator/
UFI1 bots 26-29 as `strategy`, the research/MAX1 bots 30-34 as `ai` —, so no
unstyled badge and no new filter category appear in the dashboard. `22_ip_pattern_bot.py`
remains (as always commented out in the watchdog) excluded from the fleet.

**Ledger:** R2(a) annotated with a partial checkmark (R2(b), the `schema.sql` topic, remains open —
needs VPS/DB); the P1.38 partial aspect "process list drifts" checked off, the three remaining
dashboard fixes (CSRF, log-streaming handle, `/api/status` psutil sweeps) remain open.
A guard from `backtest/test_max1_gate.py` was moved from `main_watchdog.py` to `core/fleet.py`
(the registration now lives there).

## [2026-07-11] AIM2 serving: candidate window 60 min + table-agnostic conv dedup key (P2.35, T-2026-CU-9050-090)

Three audit findings from wave 5 on the AIM2 master gate (`15_ai_master_bot.py`).
**Context:** AIM1 stays OFF per P0.13 (no retrain); the code runs as the AIM2 carrier
(shadow-first behind `AIM2_LIVE_POSTING`, `docs/AIM2_DESIGN.md`). The fixes apply to the
AIM2 path. The ledger line numbers of P2.35 (as of 07-03) come from the old
AIM1 code — re-located against the current AIM2 rebuild.

**(a) Candidate window 30 → 60 min.** The AIM2 rebuild had already moved the original
5-min window to 30 min and introduced a persistent dedup table
(`master_ai_processed_signals`). Remaining delta per the brief: 60 min. The window
only bounds staleness (how old a signal may still be traded); double
processing after downtime is prevented by the dedup table, not the window width — the
widening is therefore safe.

**(b) Context/swarm self-counting — already correct, no change.** The suspicion
"context aggregates count the candidate itself" no longer applies to AIM2:
`swarm_stats` (serving) strictly filters `ts < Kandidaten-ts`, and `load_signal_stream`
excludes AIM1/AIM2/AIM2-TOPN from the stream. The trainer (`aim2_build_dataset.py`)
does the exact same thing with `searchsorted(side="left")` + an identical model exclusion.
**Both sides are identical → no change, and explicitly NO retrain coupling**
(rule 7 not touched: no model input feature changes). A DB-free test now
pins the invariant mechanically.

**(c) conv dedup key is now table-agnostic (root cause instead of symptom).** The
dedup key was `(signal_type, id)` with `signal_type="conv_signal"` for both active and
closed_trades_master. Both tables, however, have **their own SERIAL sequences**, and a
conv signal moves from active to closed within seconds — with a **new id** at an
unchanged open `time` (`5_trade_monitor.close_trade` copies the identity fields
1:1). The per-table `id` is therefore unfit as a dedup key (the same diagnosis
already documented by `33_ai_fif1_bot.signal_key`). Two error classes: (1) the
closed form (new id, `time` still within the 60-min window) gets re-scored as a
fresh candidate → **double post** — the normal case for fast strategies like "Fast In And Out";
(2) unrelated active/closed rows with a coincidentally equal id displace each
other from the processed set → silent loss of a legitimate signal (the collision
mentioned in the ledger). The brief's proposed fix "distinct signal_types" (separating
active vs. closed) fixes only (2), not (1). Fix therefore via a migration-stable
identity key: `conv_signal_identity(source, symbol, direction, time, entry)` → a BIGINT-
safe md5 hash; ai keeps the stable `ml_predictions_master.id`. Schema of the
dedup table unchanged (TEXT/BIGINT stay) → no live migration; old
`conv_signal` rows in the processed set are ignored once after deploy (bounded,
shadow-only).

Takes effect on the live gate flip; shadow-only today. DB-free tests:
`backtest/test_aim_context_features.py` (conv identity stable across active→closed,
id collision resolved, ai namespace separated, swarm self-exclusion, window ≥60).
Verification: full `backtest/` suite green (611), ruff/format/mypy clean.
## [2026-07-11] 21_btc_smc cooldown/dedupe + 20_funding_bot extreme threshold 75→95/85 (T-2026-CU-9050-088)

Two independent audit findings from wave 5.

**P2.46 — `21_btc_smc_strategy.py` had no cooldown/dedupe.** The bot scans
hourly and posts as soon as an EMA21+FVG pivot-retest setup is "fully closed".
Without a lock, the same setup requalified on gap-filler lag in the next scan —
the identical Cornix signal went out a second time ~1h apart (a double position
with real money). Fix: every post now goes through the central `trade_cooldowns`
system in `send_cornix_signal`. The cooldown check runs before the outbox insert; after a
successful post the cooldown is set in the **same commit** as the insert
(`update_cooldown(..., commit=False)` + one `conn.commit`), so signal and
dedupe marker persist atomically — a partial commit would have enabled exactly the
re-posting the fix prevents (the T-024 lesson). Tag `BTCSMC_1H` (9 characters, fits
into `trade_cooldowns.module` varchar(10)); cooldown 12h — the fleet default for sub-daily
TFs (P1.27 pattern, cf. bot 16) and above the 1h candle duration, so the 1h-offset
double signal is reliably blocked. The P0.5 fixes (cap_leverage_to_sl) remain untouched.

**P2.40 — the funding "extreme" alert fired in the normal state.** `20_funding_logger_bot.py`
posts a TOP20 "FUNDING EXTREME ALERT" whenever a share of the top-20 coins is one-sidedly
positive/negative funded. The old lower bound was 75%. The funding baseline, however, is
slightly positive (~+0.01%), so routinely ~75%+ of the top 20 are positive — the
75 trigger reported "EXTREME" almost permanently. Operator decision (Michi 2026-07-11):
lower bound to 95/85. The threshold logic is extracted into the pure helper
`classify_funding_extreme(pos_pct)` (testable, edge cases pinned).
**Deliberate signal-rate change:** the funding bot now alerts less often — only
for genuinely one-sided funding (≥95/85%), no longer during the slightly-positive everyday state.
Affects only the info channel `CH_MARKET_DATA` (sentiment post, no Cornix trade).

DB-free tests: `backtest/test_btc_smc_cooldown.py` (cooldown wiring: active→no post,
free→exactly one outbox insert + atomic cooldown upsert, DB error→non-post),
`backtest/test_funding_threshold.py` (95/85 edge cases incl. "75 no longer fires"),
tag length statically pinned in `backtest/test_cooldown_tags.py`.

## [2026-07-11] SMC/Mayank/sniper — weekend refire, FVG age, SL/RR (P2.45) + break-and-retest level (P2.39) (T-2026-CU-9050-089)

Four signal-quality fixes on the three SMC bots (16/17/25) from the wave-5 dispatch
(T-2026-CU-9050-075). All four exclusively make signals DROP OUT or correct
WHICH level is scored — no new position, no new post path.

**P2.45(a) — weekend/stale-candle gate (16 + 17).** Forex/metals stand still on weekends:
the last closed yfinance candle freezes and keeps satisfying the structure/FVG
condition for days, while the 12h cooldown beneath it expires → the bot refired on
the same frozen candle on every cooldown expiry. New: a pure helper
`is_stale_candle(open_time, tf, now)` — a signal may only fire if less than
**two candle durations** have passed since the close of the last candle. The two-candle
tolerance forgives a single yfinance live lag; a weekend exceeds it by a multiple
on intraday TFs. Gate implemented as a `continue` in `run_smc_analysis`/`analyze_strategy`.
**The 24/7 crypto path (METALS: BTC/ETH/…) is never stale → nothing changes there.**
Deliberately left conservatively open: a 1d/1w signal can still refire once across a weekend,
before the 2-duration threshold kicks in — the dominant regression was the intraday-12h refire.

**P2.45(b) — FVG age limit (16).** `find_unmitigated_fvgs` got `max_age=FVG_MAX_AGE` (50 bars):
an FVG that was never mitigated otherwise stayed triggerable across the entire 300-candle history. Conservative
(1h ≈ 2d, 4h ≈ 8d, 1d = 50d); older gaps count as stale.

**P2.45(c) — SL/RR sanity (17).** Mayank posted SL = last low*0.998 and TP = next pivot
without any check whether the stop survives under leverage or whether the next TP beats the risk.
New: pure helper `passes_sl_rr_guard(entry, sl, tp1, direction)` before the send in both branches —
rejects stops farther than 15% from entry (liquidation risk, same cap as the ROM1 path P2.27)
and setups whose next TP does not offer at least 0.5× the risk as reward (a sanity floor, no
normal pivot ladder is trimmed). SL/TP are FVG-independent per scan (from `curr_low`/`curr_price`),
so a fail blocks the scan (`break`).

**P2.39 — break-and-retest picks the wrong level (25).** The breaker block blindly scored
`peak_idx[-2]`/`trough_idx[-2]`; if the fresh retest belonged to a different swing (the newest
one or an older one), the bot checked a level the price was never at — and missed the
real setup. New: pure helper `find_breaker_setup(...)` walks the pivots from newest to oldest and takes
the first one whose level (a) lies within the retest band (±0.5%) around the current price, (b) was broken
by a close within the last `MAX_BB_AGE`=20 closed candles, and (c) afterwards ran ≥0.3%
follow-through. Freshness, follow-through, and band thresholds are identical to the old code — only the
level **selection** changes. Feature timing deliberately left at the retest bar (`len(df)-2`) and
documented (the BB model's pattern anchor); a change there would be a strategy redesign and doesn't belong here.

The existing SMC fixes remain untouched and green: P1.26/P1.27 (16, FVG dead-code range +
forming drop + TF cooldown) and P1.46 (25, forming pivots) — `test_smc_fvg_dead_code.py`,
`test_sniper_forming.py`, `test_sniper_tag.py` all remain green. Tested DB-free:
`backtest/test_smc_weekend_refire.py` (14/14) + `backtest/test_sniper_retest_level.py` (9/9), each with
a divergence canary against the pre-fix logic. Full backtest suite: 612 passed.

## [2026-07-11] 14_ai_atb_bot.py — ATB1 unknown-state observe-only + main-loop hardening (T-2026-CU-9050-086)

Two robustness fixes on the parked bot 14 (ATB1). Only take effect once unparked —
the fixes are risk-free but had to be in place before unparking (OPUS-HANDOFF §3).

P2.36 (unknown state = observe-only): after a state loss (`trendline_state.json`
missing or corrupt), TRENDLINE_STATE falls back to {}, every coin got
`prev_relation="unknown"`. The old inline break check listed "unknown" in every
condition — on the first cycle after a state loss, EVERY coin above/below
its trendline thus fired a fresh BREAK event (a mass event flood with real money; the
old comment openly admitted the bug). Event classification is now extracted into the pure
`classify_trendline_event`: `prev_relation=="unknown"` returns `None`.
The first cycle only rebuilds the relation and emits nothing; the caller
still writes `prev_relation` unchanged, real transitions (below→above etc.)
fire from the following cycle onward. Persistence alone would not have been enough (the file can be missing),
the observe-only guard is the actual protection.

P2.37 (main-loop exception handling + conn hygiene): the scan in
`run_trendline_detector` now runs inside `try/finally` — `conn.close()` and
`save_trendline_state()` also run on a mid-scan abort (previously: connection leak
+ discarded state). The `main()` loop only caught `KeyboardInterrupt`; any scan exception
killed the process. Now a broad `except Exception` with an ERROR log + 30s backoff instead of
process death (pattern: `3_detectors.main()`, P1.15). The per-coin rollback (P1.23) and the
forming-candle slice (P1.22) remain untouched.

Tested DB-free in `backtest/test_atb_unknown_state.py` (observe-only invariant +
a differential assertion against the pre-fix flood logic; fails on the pre-fix state).

## [2026-07-11] Watchdog: graceful shutdown instead of hard terminate() (P2.48) + atomic_write_json Windows fix (P2.49) (T-2026-CU-9050-087)

Two process/persistence findings from the wave-4 dispatch (T-2026-CU-9050-075).

**P2.48 — hard terminate() orphaned the ProcessPool workers.** `main_watchdog.kill_process`
called `p.terminate()` — on Windows an immediate `TerminateProcess` without graceful shutdown.
Critical: the indicator engine's ProcessPool workers (`2_indicator_engine.py`,
`ProcessPoolExecutor`) survived the parent kill as orphans and kept computing →
double-compute window. New: every bot (and the dashboard) starts in ITS OWN
process group (`CREATE_NEW_PROCESS_GROUP`); the stop sends a `CTRL_BREAK_EVENT` to the
ENTIRE group — this reaches the bot AND its worker children, unlike `terminate()`, which
only hits the bot itself. Afterwards it waits `GRACEFUL_STOP_TIMEOUT_S` (default 10s, env-overridable),
then hard-kills. If `CTRL_BREAK` is not deliverable (no console attached —
scheduled-task start, or process already gone), the path falls back to `terminate()` and
logs it — never worse than before. The dedicated group also prevents a
stop signal from also hitting the watchdog console. P0.2 (mutex + orphan sweep) and the
scheduled-task restart path (T-074, `restart_fleet.ps1` stops via `Stop-ScheduledTask` +
orphan reap on the next start) remain untouched — the process-group isolation
improves their teardown ordering, does not regress it.

**P2.49 — atomic_write_json silently discarded updates on Windows.** `core/state_utils.py`
used a FIXED `.tmp` name (two parallel writers on the same path corrupted each other
on the same temp file) and let `os.replace` fail under a broad `except` when a
reader held the target file open → the update was SILENTLY lost. New: a unique temp name via
`tempfile.mkstemp` in the target directory (same filesystem → `os.replace` stays atomic,
pattern `core/coins.py` #68) + a short retry (5×50ms) on `PermissionError`; if it stays
blocked, it is LOGGED (no more silent loss) and the temp file cleaned up.

DB-free tests: `backtest/test_atomic_write_json.py` (12: roundtrip, unique-tmp, retry path,
permanent-failure logging, cleanup), `backtest/test_watchdog_shutdown.py` (8: process-group flag,
CTRL_BREAK vs. SIGTERM per platform, hard-kill escalation, CTRL_BREAK fallback). The regression suites
`test_watchdog_backoff.py`/`test_watchdog_hang.py` (P1.37/P2.47) remain green. **Honest evidence status:**
the actual process-group signal delivery and the ProcessPool worker teardown are only observable against
a real Windows console — what is unit-testable is that the RIGHT signal is sent in the RIGHT
order; live verification (no orphaned worker after kill_process) is a
VPS step.

## [2026-07-11] Detector cycle: batch ticker instead of 538 individual calls + volume-indicator fixes (P2.44 + P2.42, T-2026-CU-9050-085)

Two findings from the detector path, both from the wave-4 dispatch (T-2026-CU-9050-075).

**P2.44 — HTTP load & gate ordering.** `3_detectors.py` made one Binance klines
call per coin per scan cycle (~530 serial requests). New: `get_live_prices_batch()`
fetches all symbols in ONE `/fapi/v1/ticker/price` call; the loop reads `price_map.get(symbol)`
and falls back to the old per-coin HTTP→DB path only for missing symbols (freshly delisted)
or on batch failure — no coin is skipped, a batch failure degrades cleanly
to the old behaviour. Additionally in `strat_volume_indicator.analyze_coin`: the expensive
90d×30m HVN read ran as the FIRST gate for every coin. The four gates (spike, active trade,
cooldown, HVN) are all side-effect-free, AND-linked reads → reordered to cheap-before-expensive,
the HVN read now runs LAST and only if a signal would otherwise be emittable.
The signal set is invariant to the evaluation order. The P1.16 cooldown contract
(12h lock, tag `VolIndic`, write via detector with `commit=False`) remains untouched —
only the read-only `check_cooldown` was moved earlier.

**P2.42 — volume-spike classification & HVN gate.** Three deliberately signal-changing fixes
(ledger mandate): (a) the spike selection now iterates backwards — the MOST RECENT spike within the
5-day window decides instead of the oldest one (the old forward loop broke at the
first/oldest spike); (b) a spike on the first in-period candle (`i==0`) has no
in-period predecessor and is now discarded instead of silently classified as sell; (c) the
HVN gate now bins prices to 0.1% levels before aggregating volume — the old
`groupby('close')` on raw float prices never accumulated a level on fine-tick coins
(every candle its own price) and effectively never fired there. The classification and
HVN logic was extracted into the pure functions `_classify_latest_volume_spike` /
`_is_near_high_volume_node` (identical behaviour, DB-free testable).

DB-free tests: `backtest/test_volume_indicator_spikes.py` (9, with pre-fix reference asserts),
`backtest/test_detector_batch_ticker.py` (4). The `[DB]`-marked live-load/effect measurement
(CPU baseline, changed signal rate) remains a VPS step.
## [2026-07-11] Orchestrator: startup whitelist reconciliation (P2.24) + whitelist-cleanup write side (P2.25) (T-2026-CU-9050-082)

Two regime-gating findings closed, both via the in-memory resp.
write side of a problem only half-defused since T-046.

**P2.24 — regime change during orchestrator downtime never caught up.**
`check_regime_change_and_close` fires only on an OBSERVED in-memory flip
(current `regime_current` ≠ the `_last_known_regime` remembered at the last poll).
At process start this baseline is empty, so the first poll only seeds it and
returns — a regime change that happened DURING the downtime is never
caught up, and every open trade keeps running under a regime that may
no longer whitelist it. Fix: `run_startup_reconciliation` runs once before the
main loop and checks all OPEN trades in `orchestrator_open_trades` against the
CURRENT whitelist — no remembered regime needed. The close/trail body is extracted into
`_close_non_whitelisted_open_trades` and shared with the regime-change handler:
only ROM1's own trades (the table by construction contains only ROM1,
the DB-side force close is `model='ROM1'`-filtered, P1.9), the existing
close path, no new mechanism. Startup additionally seeds the baseline, so
the first periodic check does not fire on the boot state, and it only posts
a status message if it actually closed/trailed something — no
status-channel spam on every watchdog restart. Fail-safe: its own short connection,
an error here never blocks the loop start.

**P2.25 (write side) — stale `bot_regime_whitelist` rows never cleaned up.** The
read side has been defused since T-046 (cells >48h → overall fallback). Open was the
write side: `cleanup_stale_performance_rows` only cleaned the perf table, the
raw-name rows in `bot_regime_whitelist` (frozen since 19.04., exactly the ones the
orchestrator read) were left lying around. The new `cleanup_stale_whitelist_rows` runs in
`run_analysis` right next to it, before `compute_whitelist`. Two disjoint, OR-linked
DELETE criteria (`build_whitelist_cleanup_query`): (A) raw-name keys
`pretty_name(bot_name) <> bot_name` — provably orphaned, deleted regardless of age like
in the perf table; (B) `computed_at` older than `WHITELIST_RETENTION_DAYS` (14d) —
normalized rows of retired bots. 14d deliberately conservative: the read gate (48h) has
already devalued everything older anyway, active bots get rewritten in the same run.
Scan/delete errors are swallowed (return 0, no commit) — the hourly run
never crashes on the cleanup.

Verification DB-free: `backtest/test_orchestrator_startup_check.py` (6) +
`backtest/test_whitelist_cleanup.py` (6), plus the existing orchestrator/
analyzer suites green (144 total). ruff/format/mypy locally green. Live verification
(restart follow-up; step-2 query 9) remains a VPS-session follow-up.
## [2026-07-11] 23_market_tracker.py — Telegram chunker splits over-budget blocks, full-history load + async jobs documented as risk (P2.41, T-2026-CU-9050-081)

Remaining cleanup of P2.41 on the market tracker, four partial findings from the
07-03 ledger re-located against the current code and handled differentially.

The real robustness bug (d): the message chunker `_build_chunks` could emit a
single bot/table block that alone exceeded the budget as ONE
>4096-character chunk. `send_telegram` only writes to `telegram_outbox`;
the dispatcher `4_telegram_bot` silently drops an over-limit message — the entire
per-bot post would have vanished unnoticed. A new `_hard_split_block` fallback splits
an over-budget block first on line, and as a last resort on character boundaries; the
budget is computed against the larger of the two headers (first/follow-up chunk). Every
emitted chunk is now guaranteed ≤4096. Normal entries lie far under the
budget — the fallback only kicks in for a pathological entry, but then the
post goes out as multiple messages instead of vanishing. The three chunker helpers were
lifted from nested (in `job_per_bot_performance`) to module level for this, so they are
DB-free testable (pure, no closure state).

(c) regime-fit query without rollback: already fixed via P1.43/T-029
(`_get_regime_fit_label` rolls back, `_regime_conn` in try/finally) — verified against the code,
no remainder open (no-op).

Deliberately NOT changed, documented as known risks in the code (ledger spirit,
document risks early instead of blindly optimizing): (a) the hourly full-history load
of the `closed_*` tables is mandatory for the all-time column + the survivor pick of
the DISTINCT ON — a time filter would be a behaviour change to the statistics (operator
decision). (b) the `async` jobs do blocking sync DB I/O — cosmetic `async`
on a serial, time-staggered scheduler; a real async conversion would be a rewrite
and would trade a harmless scheduling delay for a pool-starvation risk
(pool max 8/process).

Verification: `backtest/test_market_tracker_chunker.py` (new, DB-free, 13/13),
`test_market_tracker_conn.py` unchanged 7/7 (helper move without behaviour change),
`test_market_tracker_opened.py` 7/7. ruff/format/mypy locally green. Takes effect on the next
regular restart, no deploy.

## [2026-07-11] Watchdog hang detection + statement_timeout/keepalives in the DB pool (T-2026-CU-9050-077, P2.47)

Step-2 finding: data ingestion was dead for 6h with a green watchdog — the fleet traded
on stale data. Root cause twofold: (1) the watchdog only checks process EXISTENCE, a
live-but-wedged bot stays "green"; (2) the DB pool had no statement_timeout
and no TCP keepalives, a bot hanging on a dead socket blocks forever without
dying.

`core/database.py`: every pooled connection now gets a `statement_timeout` (default
300s, caps runaway queries/hangs server-side) and libpq TCP keepalives (idle 30s /
interval 10s / count 3 — a silently dropped VPS↔Postgres socket fails fast
instead of hanging). The default is deliberately **300s, not 30s**: this DB has
`closed_trades_master`/`closed_ai_signals` without usable indexes (full-table scans), an
hourly market tracker that loads full history, and housekeeping across ~9.7k tables
(audit_reports/18). Legitimate queries >30s are thus likely; a 30s cap would
trigger `QueryCanceled` in the broad excepts of many bots → silent degradation, exactly the
error class this audit is fighting. 300s kills real runaways/hangs and spares the
hourly analytics. A **tightening to 30s** is an operator decision — only
**after** the Z0 query-runtime measurement on the VPS. All values are named constants and
env-overridable; `statement_timeout` can be disabled per process via
`KYTHERA_DB_STATEMENT_TIMEOUT_MS=0` — the escape hatch for long
trainer/housekeeping queries. The pre-existing `lock_timeout` stays. **No**
timezone flip (R3/UTC_POLICY.md deliberately out of scope).

`main_watchdog.py`: new generic heartbeat (`check_heartbeat`). A live process
whose own log file has not advanced for `HANG_LIMIT_S` (default 20 min) counts as
wedged → WARNING. The log is resolved **mapping-free** from the process's open file handles
(cached once per process lifetime, no fragile script→logname table); a
bot without an observable log is EXEMPT and can never be falsely restarted. A
freshly (re-)started bot has a full grace window. Auto-restart is **default OFF**
(money path — WARNING only by default, operator decides); opt-in via
`KYTHERA_WATCHDOG_HANG_AUTORESTART=1`, the restart then rides on the existing
crash backoff (P1.37, no `time.sleep` on the supervision path). Data staleness itself
is still covered DB-side by `core/health_monitor` (candle age → auto-restart of
ingestion); this patch adds the generic process signal on top of that. DB-free tests:
`backtest/test_db_pool_options.py`, `backtest/test_watchdog_hang.py`.

Open (deliberately not in this patch, see PR): comprehensive per-bot heartbeat
coverage requires every bot to log reliably per cycle — heterogeneous across
the fleet (only some use `core.logging_setup`, some log only to stdout). The
heartbeat today only applies to bots with an observable log; the extension is a
follow-up topic rather than improvised scope growth.

## [2026-07-11] Regression-guard disarm hardened (P2.51) + cooldown-tag test extended to the MIS horizons (P3.13) (T-2026-CU-9050-076)

Two small hardenings from the ledger, both DB-free, no live intervention.

**P2.51 — the guard no longer silently disarms on deleted goldens.**
`tools/regression_guard/guard.py::mode_verify` returned a blanket
"NOT ARMED … Pass" + exit 0 on an empty `golden/` — even if the `manifest.json` was still present.
This meant that deleting the goldens (or losing them in a merge) switched
the guard off unnoticed, while the pre-commit hook stayed green. Fix: the manifest is
the "was-once-armed" marker (writes `refresh` next to the goldens) — if it is
present but there are no goldens, `verify` now ends with **exit 1** instead of a pass.
The genuinely-never-armed state (no manifest) remains the legitimate
pre-live-DB-freeze pass, and the reverse case (goldens without fixtures → exit 1,
`:139-140`) is untouched.

**P3.13 — MIS-horizon tags in the cooldown length net.** The MIS bot posts its
cooldown under a *derived* tag `f"{generation}-{horizon}"`
(`11_ai_mis_bot.py:301`), not a string literal — the existing
literal sweep in the test never saw it. `MIS2-168H` sits at 10 characters flush against
`varchar(10)` (the error class from T-2026-CU-9050-024). The test now parses
`MODEL_GENERATION` + the `MIS_CHANNELS` horizons from the bot source and
reconstructs the tag, so that a new generation (`MIS10`) or a longer
horizon tears the assertion — instead of landing silently in the swallowed `ValueError` of
the `COOLDOWN_MODULE_MAX_LEN` guard.

### Fixed
- `tools/regression_guard/guard.py`: manifest present but goldens missing →
  exit 1 (P2.51).

### Tests
- `backtest/test_regression_guard_disarm.py` (new, DB-free): three cases for the
  disarm semantics. Case 1 (manifest without goldens → exit 1) is a genuine
  bug witness — against the pre-fix state it demonstrably fails with an
  AssertionError "got 0"; case 2 (never armed → pass) and case 3 (goldens without
  fixtures → exit 1) pin the neighbouring invariants. The armed-compute path
  remains covered by `guard.py smoke`.
- `backtest/test_cooldown_tags.py`: `test_mis_horizon_tags_fit` added (P3.13).
## [2026-07-11] Post-merge review of P1.13: RSI-flat case documented, NaN-parity imputation in the EPD legacy path and in bot 24/25 (T-2026-CU-9050-060)

Three independent reviewer runs over the merged state of PR #43
(T-2026-CU-9050-054) — verdict unanimously APPROVED, the fix itself is correct
and symmetric, no rollback. But four evidenced inaccuracies, which this
entry corrects resp. whose fixes it documents.

**F1 — RSI is also permanently NaN BEYOND the warm-up when the price window
is fully constant** (illiquid coin, new-listing lead-in, trading halt):
`up = down = 0` on every row → `rs = 0/0 = NaN` → RSI NaN on every row,
not just at the head. The first price move ends the NaN state for good:
`ewm(adjust=False)` afterwards holds `roll_up` (after an up move) resp. `roll_down` (after a
down move) at > 0 forever — a pure up series then reads RSI = 100, not NaN;
the NaN state thus applies exactly to fully constant windows. Decision
(review recommendation): leave NaN as-is deliberately — "no RSI
defined" is honest, a 50 would again be fabrication. Structurally consequence-free:
a frozen window produces 0 pivots (`argrelextrema` on a constant is
empty), bot 24 needs ≥4 alternating pivots, bot 25 ≥3 peaks/troughs —
both `continue` before the ML path; the raw consumers (`strat_*`) compare
NaN → False → no signal. Now documented as a comment in `calculate_rsi` at the code.
WMA/BOLL/DONCHIAN do NOT have this case (`rolling().std()` of a constant
is 0, not NaN) — there, NaN really is only the warm-up head.

**F2/F5 — scope correction to the PR-#43 text:** "exclusively
warm-up head rows" was too narrow in two ways: (a) the F1 case lies outside the
warm-up; (b) the deepest golden breach is `wma_200` at row 198 — a
199-row warm-up is not a "head row". The reviewer count is furthermore
**821** NaN breaches per fixture, not 816 (difference: 5
RSI row-0 transitions). The golden fixtures (BTC/ETH/SOL/DOGE, liquid)
can structurally never trigger the RSI-flat case — "golden covers the scope"
only holds for the warm-up part, not for the illiquid part of the ~538-strong fleet.
Also affected are ALL `rsi_*` (6/9/12/14/24) and `wma_*` columns, not
just `rsi_14`/`wma_21` as in the PR body (all consumers impute — uncritical).

### Fixed
- `10_pump_dump_detector.py` (F3): the LEGACY EPD path (only applies without
  a deployed EPD2 artifact — i.e. today) built the positional feature array
  without any NaN handling. **The F3 premise of the original review ("sklearn
  raises on NaN, the exception handler safely suppresses it") has been
  falsified here** — verified against the production pkl: the model is an
  `XGBClassifier`, XGBoost natively treats NaN as missing and delivers a
  prediction over untrained default branches. A NaN `rsi_14`
  (new-listing warm-up post-P1.13) could therefore produce a LIVE signal from an input
  the trainer never produced. Fix: imputation per the NULL contract of the legacy trainer
  itself (`legacy_trainers/zzz.py:7609-7617`:
  rsi→50, everything else→0; the ema distances collapse there via ema:=price to 0) —
  train/serve parity per the same principle as the `fillna(0)` in the EPD2 branch
  (whose own `train_binary` contract remains untouched). The
  serving values are identical to what the model has seen its entire
  pre-P1.13 life — new listings keep being scored, with 50
  instead of NaN; no signal that was previously impossible becomes possible. Deliberately NOT a
  blanket `fillna(0)`: rsi=0 would mean "extremely oversold" and would be
  out-of-distribution for this model.
- `24_quasimodo_bot.py` / `25_smc_ml_sniper.py` (F4): the feature build before
  `predict_proba` gets the same non-finite imputation (inf/NaN → 0) as all
  `core/*_features.py` builders — and like their own trainers, which fit AND score on
  `.fillna(0)` frames (`qm_ml_trainer.py:321/353/378`,
  `smc_ml_trainer.py:328/344/365`): exact train/serve parity. Here too
  XGB does not raise on NaN, but scores over untrained default branches — a
  silent skew. Contrary to the first assumption, the path is already reachable
  today: `ffill().bfill()` only leaves NaN in all-NaN columns, and those
  arise not only with frozen windows (0 pivots — the bots bail
  earlier), but also when the LEFT JOIN finds no indicator rows for the entire
  100/150-candle window (engine outage,
  coverage gap) — price pivots then still exist. On the
  all-finite path, the model input is unchanged.
- New standalone test `backtest/test_nan_feature_guards.py` pins both
  contracts (legacy NULL imputation rsi→50/rest→0, 0 imputation in bot 24/25)
  and the XGBoost-NaN premise against the production pkl (skips without
  artifact/xgboost).

**Still open (VPS resp. C-gate, unchanged from T-054):** (1) the
population count "how many coins are below ~170 candles per TF" needs
a VPS session — it quantifies the recompute effect. (2) recompute →
TD2/BB2/QM2 retrain → only remove the `bfill` in bot 24/25 at the
artifact rollout, never in isolation. Caution after the recompute: serving imputes the
warm-up rows (bfill) and feeds them, the trainer discards them via `dropna`
(`tools/walkforward_sim.py:245`) — the claim "no train/serve skew" from the
PR-#43 text only holds for the pre-recompute state.


## [2026-07-11] core/coins.py — ONE atomic coins.json writer (P2.16) + Binance-perp shape guard for delisted cleanup (P2.17) (T-2026-CU-9050-079)

**P2.16:** `coins.json` had two writers — `1_data_ingestion.update_trading_pairs`
(on every ingestion start) and `6_housekeeping.update_coins_json` (nightly at 03:00 +
on start) — each with its own copy of the filter and a non-atomic
`open('w')` + `json.dump`. Two hand-maintained filter copies drift (the ETHU incident
2026-07-06), and the direct write leaves an empty/partial
`coins.json` visible for the duration of the dump, which any reader (delisted cleanup, gap filler, `load_coins`)
can read mid-write. New: `core/coins.py` is the ONE writer — a single filter definition
(`quoteAsset=USDT` + `status=TRADING` + `PERPETUAL`) and an atomic write via
a tmp file + `os.replace` (fsync, tmp in the target directory → same filesystem, atomic
on Windows too). Both callers now call `refresh_coins_json`; a fetch failure
writes nothing at all (no truncate), ingestion continues falling back to the on-disk list.
*Annotation correction (trap 13):* the filter divergence "incl. quarterlies" was
already closed after the ETHU incident (both already `PERPETUAL`, CHANGELOG 2026-07-06) —
open were only the duplicated filter definition and the non-atomic write.

**P2.17:** delisted cleanup closed EVERY open trade whose symbol was not in
`coins.json` — including non-Binance-perp junk (metals `XAUUSD`, cross-pair `ETHBTC`,
forex) that had gotten in via the old loose filter or a momentary coins.json wobble
→ nightly false closes at PnL 0. New: the selection (classic +
AI) additionally requires the Binance-USDT-perp shape (`core.coins.looks_like_usdt_perp`,
`<BASE>USDT` uppercase-alnum). Only genuinely delisted USDT perpetuals still get closed;
`XAUUSD`/`ETHBTC` & co. remain untouched. The single writer from P2.16 also removes
the audit-noted "universe wobbles with dual coins.json writers" root cause.

**Addendum (orchestrator review T-075): empty-universe guard.** The new central
writer — unlike the old housekeeping path (`if symbols:` before the write) — had
no protection against an empty list. A 200 response with an empty or missing
`symbols` key (`filter_usdt_perpetuals` uses `.get('symbols', [])`) returns `[]` →
`write_coins_json_atomic([])` would cleanly, atomically empty `coins.json`. Consequence: the
ingestion would bring up the WS fleet with 0 coins (the on-disk fallback only kicks in on
an exception), and the nightly `cleanup_delisted_trades` would close ALL open
USDT-perp trades as delisted (the P2.17 shape guard does not protect against this — real perps
have the shape). New: `refresh_coins_json` refuses the write on an empty list
(`raise RuntimeError('empty universe — refusing to write coins.json')`) — so
ingestion automatically falls back to on-disk and housekeeping skips the
refresh, just like on a fetch failure.

No live intervention (ENVIRONMENT: BUILD). DB-free tests: `backtest/test_coins_writer.py`
(filter parity, atomicity, fetch failure leaves the file untouched, empty/missing
`symbols` field → write refused, file unchanged) +
`backtest/test_delisted_cleanup.py` (shape guard accepts real perps, rejects the
named false-close symbols). ruff/format/mypy green.
## [2026-07-11] P1.12: window-global indicators only onto the newest CLOSED candle (as-of-now) + 4 S/R readers made consistent (T-2026-CU-9050-084)

The window-global indicators (a trendline/channel fit, an HVN/POC histogram, an
S/R pivot scan, a Fibonacci span) were previously broadcast as a constant resp. a
back-projected line onto EVERY row of the computation window — look-ahead in the stored
history (step-2 evidence: 149 distinct POC / 236 distinct support across 5000 old rows; a
row 5000 candles old carried today's level). `2_indicator_engine.calculate_indicators_optimized`
now writes them ONLY onto the newest CLOSED candle (as-of-now reference row) and
NULLs them on the forming candle and all older rows (a production-proven NaN-write path
like P1.13/T-054; `trend_direction` as a real SQL NULL). 27 columns are affected: the entire
trend/channel block, POC/HVN, SUPPORT/RESISTANCE_PRICE, and all FIB_*.

Operator decision Michi 2026-07-11 — **variant B** instead of the literal dispatch spec
"last row": written onto the newest CLOSED candle, not the absolute
last (forming) row. Reason: verification showed that the newest indicator row IS the
forming candle (WS ingestion buffers every kline tick without a `k['x']` filter,
`1_data_ingestion.py:693`) and all serving readers read the newest CLOSED candle anyway.
So they keep reading the identical value, hard rule 5 (forming candle) stays honoured —
the rule-5-vs-as-of-now collision is resolved rather than improvised.

Reader inventory corrected: the dispatch named 3 readers, there are actually five S/R consumers.
`strat_support_resistance`/`strat_main_channel` (iloc[1]) and `strat_5_percent`/`strat_fast_in_out`
(iloc[0] = forming!) now robustly read the level from the newest non-NULL row
(`first_valid_index`) instead of from a fixed position index — with a forming candle present, exactly
the same value; without a forming candle, the reader stays on the closed reference row
instead of silently reading a nulled row. `12_ai_ats_bot` remains unchanged: it reads iloc[-2]
(= newest closed = reference) and has a frame-wide `fillna(0)`; under variant B
no feature-semantics change (the ATS shift feared under the dispatch default variant A
does not apply).

Verified (DB-free): `backtest/test_window_features.py` (engine invariant for the forming and
the purely historical case, one guard per reader, fails on the pre-fix state); regression-guard
golden refreshed (rule 9, documented reason) — the 648 breaches are exactly the 27
window-global columns × 24 fixtures on the non-head rows, not a single per-row column;
serving verification across the 4 real 1h fixtures: signal-rate delta of the classic strats = 0
(level byte-identical before/after), and without the reader fix, 5-Percent on SOL would have lost all 993
sweep signals.

Known risks / follow-up tasks (deliberately NOT in this PR):
- **9_ai_sr_bot (+ `core/sra_features`)** reads the indicators with `open_time <= t_time` WITHOUT
  a floor guard → can hit the forming candle and hold NaN (no fillna). Under variant B,
  support/resistance/r_squared/trend_direction become NaN there for forming reads (XGB-native, foreseen by the task as
  a known risk). Root cause is the missing floor guard (R1) — follow-up task: switch to the
  newest CLOSED candle (like `15_ai_master_bot`) + SRA retrain.
- **15_ai_master_bot / `core/aim2_features`** reads strictly `open_time < floor(ts)` → newest
  closed = reference → safe, no change. **24_quasimodo / 25_smc_ml_sniper** only read
  `trend_direction` from a closed row with ffill+bfill → robust (bot 25's TD pivot gets
  the backfilled newest-closed value). **27_bot_regime_analyzer** reads none of these columns.
- Existing rows in the DB keep the old broadcast value; the history cleanup is a
  separate VPS job (not here).
## [2026-07-11] 2_indicator_engine.py — calculate_rsi migrated to real Wilder RSI (deliberate migration, T-2026-CU-9050-095, P2.12)

Operator-approved deliberate migration (Michi 2026-07-11). `calculate_rsi` previously smoothed
the average gain/loss with `ewm(span=period)` — that is α=2/(period+1), so
for period=14 it behaves like a Wilder-7.5 RSI (span=p corresponds to Wilder period (p+1)/2). The
stored RSI_14 therefore ran ~4.8 points hotter than real Wilder (step-2 measurement,
P2.12), which is why the 70/30 bands (and the rsi_9-55/75 gates) fired too often. ATR and
`calculate_smma` in the same file were already correctly Wilder — RSI now follows suit:
`ewm(alpha=1/period, adjust=False)`. Pinned against an independent, hand-rolled Wilder RMA
recursion (`backtest/test_wilder_rsi.py`, bit-exact ≤1e-9); the old span formula
falls out as a regression. The NaN warm-up behaviour (P1.13/T-054: the first row flows through as NaN
instead of a fabricated 50/100) and the flat case (constant price → 0/0 → NaN, T-060)
are preserved — only α changes, not the NaN handling.

**Regression-guard golden deliberately refreshed (rule 9):** exactly 120 `numeric_drift`
breaches, exclusively RSI_6/9/12/14/24 across all 24 fixtures, zero non-RSI columns —
the change is fully encapsulated, no engine output column derives from rsi. `guard.py
verify` green afterwards, `smoke` green.

**Signal-rate delta** (`tools/wilder_rsi_signal_delta.py`, 24 guard fixtures, 12,468
closed bars, isolating only the RSI portion of the gates): the 70/30 extremes fall the
most — RUB2 overbought (rsi_14>70) −4.84 pp (9.28→4.44%, ~−52% rel.), oversold
(rsi_14<30) −5.61 pp (12.28→6.67%, ~−46%). That is exactly the measured "70/30 fire
too often". The SHORT gates decline moderately (strat_5_percent −2.61 pp, fast_in_out −2.40 pp),
the central 55-75 LONG bands stay ~flat (±0.7 pp). This is intentional — the
migration lowers the signal rates; the 55/70/75 thresholds are NOT re-tuned here
(that only follows after the retrain, P1.13 doctrine).

**Coupling — not isolated in live effect (C-gate, VPS, OPUS-HANDOFF §6):**
- *Retrain:* `rsi_14` is a direct model input of TD2/BB2/QM2 (`ABSOLUTE_INDICATORS`),
  rsi_6/9/12/14/24 of MIS2, rsi_9/14/24 of SRA2, rsi_6/14 of AIM2, rsi_14 of the
  research bots; derived features (mis `rsi_*_delta_1`, `rsi_14_above_50`,
  `rsi_14_cross_above_30`, TD/BB Three-Drive RSI pivot monotonicity) shift along with it.
  The deployed artifacts saw the old span RSI → retrain on the shifted
  distribution before trusting it.
- *Mixed history (like the R3 pool flip):* from deploy onward the DB history carries two RSI
  domains (old span pre-deploy, Wilder post-deploy); until a VPS recompute, trainers read
  mixed values. Important: the T-061 tool `recompute_indicators.py` only nulls warm-up
  heads and deliberately does NOT recompute values (a full recompute is not position-stable,
  up to 48% mid-band drift on rsi_14 even with the same formula). A Wilder recompute
  of the rsi_* columns is therefore a genuine full recompute — not a trivial T-061
  extension, but a larger operator decision.

Sequencing (P1.13 doctrine, "never isolated"): (1) code fix + golden refresh [this PR],
(2) VPS recompute rsi_* → single-domain, (3) TD2/BB2/QM2 + MIS2/SRA2/AIM2/research retrain,
(4) only then re-tune the 55/70/75 thresholds. AUDIT_TODO P2.12 stays open until
recompute+retrain.
## [2026-07-11] tools/restart_fleet.ps1 — UAC-free fleet-restart cycle via the "Kythera Watchdog" task (T-2026-CU-9050-074)

Lesson from the 00:32 mass crash (console of the manually started watchdog closed,
watchdog dead, 15 orphaned bots, dashboard down) and the subsequent UAC odyssey:
recovery actions needed elevation, but UAC prompts don't reliably reach Michi's desktop with
multiple RDP sessions. Since T-068 the scheduled task
"Kythera Watchdog" exists (user Michael, password logon, run level highest) — its own
user may start and stop it WITHOUT elevation; the task scheduler applies the
elevated token. The new operator script runs the complete cycle unelevated:
`git pull --ff-only` FIRST (if it fails, the fleet stays untouched, incl.
a branch guard: only pulls on `main`), then `Stop-ScheduledTask`, then
`Start-ScheduledTask` with verification (task state, bot count via the
unelevated-visible Python parent fingerprint, dashboard port 5000). The script itself kills
NO processes — orphans that survive the tree stop are reaped by the next watchdog start
(`_terminate_orphan_fleet`, P0.2). `-DryRun` for the preflight (verified: task
visible, 37 bot processes recognized, exit 0), `-SkipPull` for a restart without a pull.
The 3-voter review closed three false-success paths: (1) stop verification via a
PID snapshot BEFORE the stop (the parent fingerprint is structurally blind to
orphans after watchdog death), (2) success criterion = task state `Running` AND dashboard
port (an orphaned old dashboard on 5000 would otherwise fake
success on an import-crashed watchdog), (3) fleet-outside-the-task (the 00:32 pattern:
manually started watchdog) → abort instead of a mutex no-op restart. Exit codes 0/1/2/3/4 documented
(4 = fleet stopped, start failed → fleet DOWN, manual task start).
Caution: the stop path (task ACL) is untested until the first real run — on
"Access denied" the ACL needs a one-off elevated fix. Fleet restart remains an
operator decision (OPUS-HANDOFF §6); the script never runs automatically.
## [2026-07-11] QM2 retrain preparation: qm_ml_trainer.py now writes model_id (T-2026-CU-9050-061, step 2)

Preparation for the QM2 retrain after the P1.13 recompute (step 1 of this task
is live: 3.07M warm-up head rows nulled). The task names `retrain_from_replay.py`
for TD2/BB2/QM2 — but neither that nor `walkforward_sim.py` knows `qm`. Quasimodo
(bot 24) has its own trainer, `qm_ml_trainer.py`, which reads the (now recomputed)
`_indicators` tables, uses its own walk-forward trade sim for labels,
runs `fillna(0)` (parity with the bot serving imputation since PR #62), and already
writes to `staging_models/`. Operator decision (Michi 2026-07-11): QM2 via
this existing trainer instead of a `retrain_from_replay` extension.

The only gap was rule 6: `qm_ml_trainer.py` wrote **no** `model_id`, so
a QM2 retrain would have been silently posted as a derived `QM_1H` and merged with the
QM1 statistics on which the orchestrator gating decides. Fix: the trainer now
writes `meta['model_id'] = f"QM2_{tf.upper()}"` (convention like
`retrain_from_replay`: `QM2_1H`). Bot 24 already reads the field (T-030) and only
falls back to `QM_1H` for old artifacts without `model_id`; its comment is
updated to the new actual state. No behaviour change for existing
artifacts — only new QM retrains carry the tag.



Operator sign-off Michi 2026-07-11: MAX1 (bot 34) goes into shadow operation. The artifact
generated on the VPS, `max1_model_SHORT.pkl` (+ `_meta.json`), was promoted from `staging_models/` into
the repo root and committed here (deploy convention like RUB2, 07c8874) — a byte copy
of the RUB2-SHORT model under the tag MAX1, load-verified on the VPS with sklearn 1.7.1:
`True MAX1 0.829 15 True`. `MAX1_LIVE_POSTING` stays OFF (shadow-only, no Cornix posting);
flipping it live is a separate operator step.

### Gate numbers for the shadow start (operator goal: maximum hit rate)
`.env` on the VPS: `MAX1_MIN_PROB=0.85`, `MAX1_MAX_PER_DAY=3` — deliberately NOT the
default 0.93. Rationale (T-2026-CU-9050-070, KB `mcp-a65a1da76492`): the live curve
(06.–11.07., 44 posted/28 closed) shows the highest WR in the band 0.829-0.85 (81-82%,
n=21-28), while ≥0.88 makes the WR **fall** (60-71%) and only average PnL rises — high
thresholds buy expectancy, not hit rate. Also, the ≥0.88 candidates cluster
in funding episodes (the 24h cap then delivers ~0.7/day instead of 3). All n<30 — the
shadow phase measures exactly the cap-bound selection WR; final numbers follow after.

### Finding on the side (own follow-up task T-2026-CU-9050-071)
The replay curve (rub_replay_365d) is unusable for gate calibration: matched
signal pairs live↔replay correlate at −0.37, replay OOS never reaches prob ≥ 0.93 across 59 days.
Feature skew serving vs. replay, prime suspect funding features.

## [2026-07-11] P1.13 recompute: a full recompute is NOT position-stable — tool for head-row nulling (T-2026-CU-9050-061, step 1)

First step of the P1.13 follow-up task: bring the warm-up head rows of the existing coins to
the new NaN state (the live fix from T-054/PR #43 only affects new listings).
This PR delivers the **tool** and the underlying analysis finding; the actual
live-DB write is a separate, operator-gated step (C-gate, not yet executed).

### Finding (measured, not asserted)
The obvious approach — recompute and upsert every `_indicators` table — is
**not position-stable**. `2_indicator_engine` writes incrementally (a 1000-candle
window per run, across months, partly from older engine states), and today's engine
does not reproduce the stored mid-band values. Measured on a 30-table
sample: a full recompute would change **~79,000 mid-band cells** (worst case
+707% on `rsi_14`), not just the ~18,900 warm-up head rows. Cause: window-global
features (`TRENDLINE_*`, `HVN`, `POC`, `FIB_*`) are scalars over the whole window, long
ewm indicators (`EMA_200`, `SMMA_200`) converge slowly from the starting point. A full
recompute would have shifted the serving distribution of the entire fleet and decoupled training from
serving — the opposite of the task goal.

### Solution
`tools/recompute_indicators.py` nulls **only** the warm-up head rows of the four P1.13 families
(`WMA_*`, `RSI_*`, `BOLL_*_20`, `DONCHIAN_*`): the engine determines the warm-up boundary (the
rows it now returns as NaN), but what gets written is exclusively NULL at these
positions — never a freshly computed mid-band value. This makes the operation position-stable
by construction (mid-band = unchanged serving values). The retrain needs exactly that:
the nulled head rows drop out of the training data in replay via `dropna()` (since T-045).
Runs alongside the live bot 2 (only nulls historical rows the incremental writer never
touches), low priority, idempotent, resumable. `--dry-run` (default) writes nothing
and documents the head/mid-band split; `--execute` is operator-gated.

Verification: `backtest/test_recompute_head_nulling.py` (5 tests, standalone, DB-free) pins
the boundary — head rows get nulled, mid-band deviations only reported, never written,
newest rows (bot-2 race) excluded. Dry run across 30 tables confirms ~49 min with
3 workers for the full run. **Still open (separate steps):** the live execute, the
TD2/BB2/QM2 retrain, and — only at the artifact rollout — the `bfill` removal in
`24_quasimodo_bot.py:126` / `25_smc_ml_sniper.py:220`.
## [2026-07-11] MAX1: standalone high-conviction clone of RUB2-SHORT for the main channel (T-2026-CU-9050-067)

RUB2-SHORT is the fleet's strongest short edge (live since 06.07.: 24 closes,
79% TP1 WR, +4.2% avg PnL — T-2026-CU-9050-044), but fires ~9×/day. Michi's goal
for the main channel is **1-3 trades/day with a very high hit rate**. Instead of
throttling RUB2 (T-2026-CU-9050-050 → **wontfix**: RUB2 remains unchanged in
its channel), the same model now also runs as its own bot,
**`34_ai_max1_bot.py`**, with a selective gate and its own tag `MAX1`.

Throttle in `core/max1_gate.py` (a pure, DB-free selection): a high
minimum probability (`MAX1_MIN_PROB`, default **0.93** — never below the
artifact threshold 0.829) as the actual selector, plus a **hard rolling
24h cap** (`MAX1_MAX_PER_DAY`, default **3**) as a backstop. Per scan:
collect candidates, deduplicate per symbol, rank deterministically, cut to the
free slots. The 24h counter reads shadow **and** live from `ml_predictions_master`,
so the cap applies exactly the same in shadow as it does live.

Detection, features (9 rub + 6 funding) and trade geometry come from the
**shared** builders (`core/rub_features.py`, `core/funding_features.py`,
`hvn_sr_trade_geometry`) — imported, untouched (X-R1). `13_ai_rub_bot.py`
remains unchanged. Cooldown/dedupe/open-trade namespaces are separated by tag:
MAX1 and RUB2 do not block each other, double exposure on the same coin is
the deliberate consequence (documented in `docs/MODEL_INTENT.md` §8a).

Artifact: `tools/make_max1_artifact.py` produces a copy of the RUB2-SHORT model
with `meta.model_id=MAX1` into `staging_models/` (model, feature contract,
calibrator, val operating point verbatim — only the identity changes, hard
rule 6). The posting tag comes from this meta, never from a constant (trap 16).

Nothing flipped live: `MAX1_LIVE_POSTING` is **default OFF** (shadow-only),
without a deployed artifact bot 34 runs in idle mode, and the promotion from
`staging_models/` is Michi's operator decision (OPUS-HANDOFF §6). Exactly ONE
Cornix-parsable message per signal via `core.signal_post.post_ai_signal`
(hard rule 4). Watchdog registration: `start_delay=223`.

Verification: `backtest/test_max1_gate.py` (21 new tests — selection/cap/
default-off gate/tag-from-meta/Cornix single message/cooldown separation), the full
suite 458 green, ruff/format/mypy green, the artifact loads via `core/model_artifacts`
(tag MAX1, 15 features, threshold 0.829, calibrator yes).
## [2026-07-10] EPD and SRA get the active-trade check; EPD's funding load gets cached (T-2026-CU-9050-055)

Two follow-up findings from T-2026-CU-9050-042, on operator mandate (Michi, 2026-07-10).
This closes the P1.48 error class fleet-wide: **all** posting
AI bots now check before signaling whether a trade on the coin is already open.

**The position guard.** Neither `10_pump_dump_detector` (EPD) nor `9_ai_sr_bot`
(SRA) touched `ai_signals` in a read. What they had were frequency locks:

- EPD: `pd_state["last_alert_time"]`, 900 seconds — and an **in-memory** timer.
  An EPD trade regularly survives a quarter hour; after that, the same
  coin was allowed to fire again, and Cornix opened a **second full position** alongside it.
- SRA: the 4h cooldown plus the `trade_id` duplicate check. The latter only guards
  against the same setup — not against a **new** S/R setup on a coin on
  which an SRA trade is already running.

Both now get `SELECT 1 FROM ai_signals WHERE symbol/direction/model IN
(tag, legacy_tag)` and skip the signal on a hit. For **EPD**, the check runs
*after* the prediction — the direction only emerges from the `argmax` — but
*before* the shadow/post branch, so it suppresses the shadow row as well, same as with MIS/RUB.
Operator decision: `symbol+direction` like the siblings, no
direction-agnostic key, so a reversal on the same coin remains allowed.
For **SRA**, the direction is already fixed from `active_trades_master`, so the check
sits before the indicator fetch and `predict_proba` and also saves work. The
legacy tag travels along in both binds (transitional dedup across the EPD3/
SRA2 generation switch); the cooldown and the 900s timer remain untouched alongside it.

**The funding load — and a correction to T-042's own note.** It said,
the load fires "per qualifying tick, because the `vol_ratio>=5` pre-filter
holds it up". That was inaccurate: the 900s timer does indeed lock **before**
the ML stretch. The repeat case is different — the timer is **only set on the
live-trade branch**, so a coin in the shadow band (0.25..threshold) passes the gate on
every 10s tick and pulled the query every time.

"Only load funding on trades" is **not buildable**: the 6 funding columns are
model **input**, they produce the `prob` that decides in the first place whether it becomes a
trade. The order cannot be reversed. What is possible is deduplicating the
repetition: `core/funding_features.funding_features_cached` caches per symbol until the
next settlement that could actually change the result.

The key here comes from the **data**, not the wall clock — and that is
exactly the point where the first draft of this fix was wrong. It cached per
started hour, on the assumption that Binance settles on full hours. An
adversarial review disproved that with two executed counter-examples:
`tools/backfill_funding_rates.py` writes `funding_time` to millisecond precision,
nothing enforces the hour grid (a settlement at 12:30 stayed
invisible until 13:00); and the 120s ingestion guard was a bet on an SLA — a row
that landed after 150s was ignored for the rest of the hour.

Now an entry is valid until the settlement that can next change the result:
the next `funding_time` that is already in the history (regardless of which
minute it sits on), or — behind the last row — the last settlement plus the
interval estimated from the most recent spacings (8h/4h/1h per pair). If the
due row has not yet been ingested, the entry is already expired and gets
reloaded on every call until it appears; its `funding_time` then pushes the boundary
further out. The cache **corrects itself**, instead of betting on a schedule.

The interval estimate takes the **minimum** of the most recent spacings, not the median
— a second finding of the re-review. The error directions are not equally costly: an
estimate that is too short costs an extra DB round trip, one that is too long leaves the
cache sitting past a real settlement and delivers a stale value. If
a coin shortens its cadence (8h → 1h) or an ingestion gap distorts the spacings,
a median (or the last spacing) overestimates by hours; the minimum
structurally cannot.

This puts value neutrality back on the invariant instead of an assumption:
`funding_features_asof` depends on the timestamp **exclusively** via the
`searchsorted` cut, and all aggregates are suffixes (`rates[-3:]`,
`rates[-270:]`) — the moving `since` lower bound does not enter. What would break
parity would be a **naive time TTL**: that can span across a settlement boundary.
The T-042 entry below warned exactly about that and wrongly concluded from it that a cache
was not a drop-in at all.

Verification DB-free: `backtest/test_funding_cache.py` first pins the invariant
itself (as-of constant between two settlements, and moving across one —
both above `MIN_HISTORY`, otherwise the tests compared two empty dicts),
then both disproved counter-examples, then the cache behaviour. Extended:
`test_epd_tag.py` (15), `test_sra_tag.py` (13). Mutation-tested: the clock-bound
hour key, a boundary derived from the last row instead of the next set,
a median or last-spacing estimator, and a `searchsorted` cut on `right`
(look-ahead on an exact timestamp tie) all fail. The second and the
third mutation were real bugs in the first two attempts at this fix.

**Live semantics deliberately change** at exactly one point per bot: a signal on
a coin on which a trade of the same direction is already open drops out. First
position, free coin, opposite direction, and the computed funding value stay
unchanged. No rollout, no artifact touched, no DB change.

**Along the way (boy scout, pre-existing since T-042):** `CACHE_SINCE_DAYS` raised from 95 to 110.
The funding load windowed on 95 days, but the 270-sample window of
`fund_pctl_90d` needs exactly 90 at an 8h cadence — only 5 days of buffer. A coin with
a >5d cumulative funding gap got <270 samples live and deviated minimally from the
trainer (full history) in that one feature. 110d gives 20 days of gap buffer.
Does not touch the cache value identity (cache and `asof` see the same frame).

## [2026-07-10] ROM1: regime auto-close differentiated — trail winners instead of blind-closing (T-2026-CU-9050-049, B6)

On a regime change, the orchestrator (`28_signal_orchestrator.py`) closed
every non-whitelisted open trade via a market `Close` — per report 16 (B6),
this capped ~49% of trades **while in profit** (median PnL 0%, churn +
fees + censored statistics).

New, behind the default-OFF gate `TRAIL_WINNERS_ON_REGIME_CHANGE`
(env `KYTHERA_REGIME_TRAIL_WINNERS=1`): a trade **in profit** is no longer
closed, but its stop-loss is moved via a Cornix **SL update message**
(`SL <SYMBOL> <preis>`, symbol-addressed like `Close`) to **break-even** resp.
the **last reached TP level**; the trade keeps running. Losers
continue to be market-closed.

A/B measurable via the new column `orchestrator_open_trades.regime_close_action`
(`REGIME_CHANGE_CLOSED` vs. `REGIME_CHANGE_TRAILED`, plus `regime_action_at`).
The TRAILED tag survives the later final close (the lifecycle sync leaves it
untouched), so the cohort remains identifiable for the 4-6-week live comparison via the
tracker path (evaluation query documented in
`docs/REGIME_ORCHESTRATOR.md`).

Safety: the SL update message is a single-line command semantics and
**never** a second Cornix-parsable signal (hard rule 4, unit-tested against
`parse_cornix_signal`). Since `Close <coin>` acts symbol-wide, a coin with
a trailed winner is **not** additionally market-closed in the same pass.

No deploy, no live flip: the gate is default OFF, the additive
`ensure_schema` column (B8 precedent) only takes effect on the next VPS restart —
activating the experiment is an operator decision (OPUS-HANDOFF §6).

Verification: `backtest/test_signal_orchestrator.py` (11 new tests, 86/86),
`test_regime_detector.py` + `test_bot_regime_analyzer.py` (79/79),
`regression_guard verify` OK (24/24), ruff/format/mypy green. Proof of effect
live (VPS).
## [2026-07-10] ATB1: posted flag mirrors the live trade, not hard False (T-2026-CU-9050-062, P1.47)

`14_ai_atb_bot.py` logged every prediction from `ml_prob >= 0.25` to
`ml_predictions_master`, hard with `posted=False` — including the ones actually
traded (`ml_prob >= threshold`). The live trade itself (`send_signal`)
only writes to `ai_signals`, so there was never a `posted=True` row.

Consequence since P1.44: the market tracker's `created_at` join (`m.posted = TRUE`)
never matched a single ATB1 row, open ATB1 positions permanently fell back to
`NOW()` and appeared eternally fresh in the opened buckets. Unlike
ATS1/RUB1/MIS1/SRA1, which write `posted=True` on their live branch.

The flag now comes from `_atb1_posted_flag(ml_prob, threshold)` — `True` exactly
when the prediction triggers the trade. Extracted as a pure function because
`run_trendline_detector` as a whole is not drivable; this makes the boundary
(`threshold`, **not** the 0.25 shadow gate) testable and guarded
against a later "simplification".

Effect display-only — Kelly/WR pull `created_at` from
`closed_ai_signals.open_time`, not from the join. No deploy; ATB1 is
parked, the fix takes effect on the next restart. This was the open
requirement before unparking bot 14.

Verification: `backtest/test_atb1_posted_flag.py` (new, standalone, DB-free,
5/5). Honest about evidentiary strength: the five tests check the new helper — on
the pre-fix state it is missing, so they error (`AttributeError`) instead of
behaviourally measuring the insert bug — the insert call itself is only indirectly covered
(`run_trendline_detector` as a whole is not drivable). Their value is the
forward guard on the helper boundary: `test_boundary_is_not_the_025_shadow_gate`
pins that the boundary is `threshold` and not the 0.25 shadow gate, and
`test_returns_plain_bool_not_numpy` (numpy input) secures the `bool()` wrapper
for psycopg2. ruff + format + mypy green.

---
## [2026-07-10] Merge-train onboarding: Kythera PRs now merged by the daemon, not the session (T-2026-CU-9050-063)

Kythera now runs on the merge-train (`services/merge_train/` in
knowledge_base_internal, Hetzner): after passing the core reviews, the
session stamps `cu/reviews`, sets the label `merge-train`, and closes out — the daemon
merges serially and rebases each PR at most once. Reason: on 2026-07-10
there were at times 6+ parallel sessions against main; every CHANGELOG top-insertion
collided with every other, and whoever merged themselves paid 1-2 manual
conflict rounds per PR (an O(n²) rebase cascade — exactly the case the train
was built for). Operationally activated: labels `merge-train`/`merge-train:failed`
in the repo, `MERGE_TRAIN_REPOS` on Hetzner extended by `Kythera`, service
restarted. No deploy hook (build repo, nothing runs post-merge).
Docs: `docs/OPUS-HANDOFF.md` §2 step 7 (incl. bounce/re-queue rules) and
`CLAUDE.md` workflow. This PR is itself the first train run — its merge by the
daemon is the end-to-end verification incl. daemon PAT access to the repo.
## [2026-07-10] AIM2 trainer: meta-gate tags excluded from load_events — F6 symmetry with serving (T-2026-CU-9050-065)

Follow-up from T-2026-CU-9050-051. The serving side (`15_ai_master_bot.load_signal_stream`)
excludes AIM1/AIM2/AIM2-TOPN from the candidate/swarm stream (F6 self-feedback),
but the trainer `tools/aim2_build_dataset.py` only filtered `model_name <> 'AIM1'`. A
future AIM2 retrain would thus have labeled its own meta-gate outputs (AIM2 has been posting since 06.07.,
AIM2-TOPN as soon as it's live) as training events — the same leakage that has long
been fixed serving-side, and a violation of the AIM2_DESIGN §3 invariant "identical definition as in
the trainer".

### Changed
- `tools/aim2_build_dataset.py`: `load_events` now pulls `model_name NOT IN ('AIM1', 'AIM2', %s)`
  with the tag from `core.aim2_topn.MODEL_TAG` — symmetry with serving established, tag
  single-sourced (no second literal).

### Added
- `backtest/test_aim2_event_source_symmetry.py` (DB-free, standalone): statically pins that
  trainer and serving carry the same meta-gate exclusion and that neither still uses the old
  `<> 'AIM1'` filter.

No live intervention, no retrain rollout — pure definition correction for the next
training run. Verified: new test green, `guard.py verify` (24 fixtures), ruff+mypy green.

## [2026-07-10] Spike: replication scoring (polybot) evaluated on Hyperliquid public fills (T-2026-CU-9050-058)

Feasibility eval of whether polybot's "replication scoring" concept
([ent0n29/polybot](https://github.com/ent0n29/polybot), MIT, Java) is reproducible for Kythera on
**Hyperliquid public fills**. Lead from the repo audit 2026-07-10
(KB `mcp-41a50fe33552`). **No fleet code touched** — pure research spike.

Result (verdict in `docs/HYPERLIQUID_REPLICATION_EVAL.md`): **technically feasible
and cheap, strategically optional and tied to the open Hyperliquid venue
decision.** Data access, signature extraction, and score were **verified live**
(2026-07-10), the cited numbers are real PoC output, not an estimate.

### Added
- `tools/research/hl_replication_poc.py` — standalone, DB-free, stdlib-only, no
  `core` import, writes nothing. Proves the three load-bearing claims: (1)
  every trader's fill history is publicly retrievable keyless per address (leaderboard =
  40,376-address universe), (2) polybot's four distribution features port 1:1
  onto perp fills (coin/dir/maker-taker/size — the perp schema is **richer** than
  polybot's Polymarket source), (3) polybot's exact formula (mean L1 over marginals
  → 0-100) runs unchanged. Adds a **self-consistency** measurement (temporal
  reproducibility of a *single* trader), which the raw polybot score omits.
- `docs/HYPERLIQUID_REPLICATION_EVAL.md` — the full eval: data access + limits
  (2000 fills/call, 10k-history ceiling/address), signature mapping,
  score critique (similarity ≠ reproducibility; marginals ignore
  sequence/joint), fit with Kythera's existing replay/regime/feature-builder stack,
  and the secondary goal ClickHouse ingestion → **reject, a Timescale hypertable
  is enough** for append-only low-volume fills.

Verified: PoC live against `api.hyperliquid.xyz/info` + leaderboard blob (HTTP 200,
2000 fills/address, score output plausible), ruff check + format locally green.

## [2026-07-10] Fractional-Kelly sizing spec distilled from CloddsBot (T-2026-CU-9050-057)

From the repo audit 2026-07-10 (`alsk1992/CloddsBot`, MIT), the `kelly.ts` parametrics distilled as a
position-sizing spec for Kythera: `docs/KELLY_SIZING_SPEC.md`. Pure design docs,
**no live code**.

### The framing finding
Kythera today sizes **no** notional size at all — Cornix does that. Kythera only supplies leverage
(`get_max_leverage` + `cap_leverage_to_sl`), trade geometry, and the orchestrator gating. A
1:1 port of `kelly.ts` (`positionSize = bankroll × kelly`) would have no lever in Kythera to
pull on. What is usable is therefore not the size figure, but the **adjustment cascade**
(drawdown, win/loss streaks, vol scaling, category performance, sample size, quarter-Kelly).

### What the spec shows
The state substrate for the statistical adjustments (win rate, vol/Sharpe, "category" =
bot×regime×direction) already exists in `bot_regime_performance` (`27_bot_regime_analyzer`,
windows 7/30/90d) — data-side almost free. What's missing: bankroll/peak/drawdown and streaks
(no capital model in Kythera). Three docking options documented (A: leverage scaling,
B: orchestrator gating/size-as-inclusion, C: Cornix per-signal risk — unvetted), plus the
perp adaptation `b = R = TP-Dist/SL-Dist` instead of binary `odds=1`.

### Recommendation
Do not build a notional sizer. First a batch-E study task (template T-2026-CU-9050-020): apply the Kelly
fraction from `bot_regime_performance` as a post-hoc weighting onto the walk-forward replay PnL
and measure the effect — **before** a single line of live sizing code exists. On positive
evidence, option B (default-off gate). Open operator questions (Cornix money management, whether Kythera
ever gets its own notional sizing) escalated to Michi.
## [2026-07-10] AIM2-TOPN: "top 1-3 of the day" as a high-conviction channel, default off (T-2026-CU-9050-051)

From T-2026-CU-9050-031, path 2: the structural route to "daily 1-3 trades, very
high win rate". AIM2 already ranks the entire fleet and posts everything above its
~34% pass threshold (≈110/day). AIM2-TOPN is the **second, selective consumer of
the same scores**: instead of "everything above the line", at most **N (1-3) of the strongest
candidates of the day** into a **dedicated channel/tag** (`AIM2-TOPN`, rule 6),
separate from the base AIM2 posting.

### Added
- `core/aim2_topn.py` — pure, DB-free selection logic (`select_topn`,
  `load_config`) plus the routing tag `AIM2-TOPN` (≤ 10 characters, fits into the
  cooldown module key). "Top N of the day" is only known ex-post, so it is
  approximated via a high **minimum probability** (never below the
  base gate threshold) plus a **hard rolling 24h cap** N. Rolling
  instead of calendar day — no midnight burst (23:50 + 00:10 = 2·N in 20 min).
- `tools/aim2_topn_calibrate.py` — **read-only** threshold calibration from
  `master_ai_processed_signals.ml_confidence`: which `min_prob` historically
  delivers ~1-3/day. Writes nothing, flips nothing live (VPS only, needs DB).
- `backtest/test_aim2_topn.py` (DB-free, standalone): cap, min-prob floor,
  parity/trusted filter, (coin,direction) dedupe, deterministic tie-break,
  config defaults/clamping, and the static wiring check (gate default-off,
  TOPN tag excluded from the stream, no flip of the money gates).
- `CH_AIM2_TOPN` in `core/config.py` (plain `_ch`, 0 = unset ⇒ shadow-only,
  **no** fallback to the AIM2 channel).

### Changed
- `15_ai_master_bot.py`: collects the strong, trustworthy
  candidates each cycle, selects the top N under the 24h cap after the loop, and
  posts via the audited `core.signal_post.post_ai_signal` (exactly ONE
  Cornix message, rule 4). The `AIM2-TOPN` tag is excluded from AIM2's own
  candidate/swarm stream (F6 self-feedback).

### Gates (all default off — flipping live is Michi's decision)
- `AIM2_TOPN_ENABLED=0` (master switch; off ⇒ **zero** behaviour change to
  base AIM2 — statically tested), `AIM2_TOPN_LIVE_POSTING=0` (shadow-first),
  `AIM2_TOPN_N=1`, `AIM2_TOPN_MIN_PROB=0.95`. `AIM2_LIVE_POSTING` and
  `NEW_IDEAS_LIVE_POSTING` remain untouched.

Design: `docs/MODEL_INTENT.md` §9a. Verified: `backtest/test_aim2_topn.py`
(17 green), `guard.py verify` (24 fixtures), ruff+mypy locally green.
## [2026-07-10] ROM1 whitelist v2 as a shadow column: net expectancy instead of WR + hierarchical shrinkage + B9 censoring correction (T-2026-CU-9050-048)

The gate rework from report 16 (recommendations 6+7), built **exclusively as a
shadow column**. The live gate remains unchanged on v1 — flipping it live is
Michi's decision after the counterfactual comparison (T-2026-CU-9050-047), not
part of this task.

### Why
The 4D whitelist has two structural flaws (report 16): **B1** — 89% of
fresh cells are `insufficient_data` and get waved through default-open
(n < 30 does not decide but waves through); **B2** — median 7 trades/cell,
the WR point estimator is too noisy, and a 55%-WR bot with tiny wins +
large losses is net a loser that the pure WR gate lets through.

### What v2 does differently (shadow)
`compute_whitelist` (27_bot_regime_analyzer) writes a second decision next to the
v1 one: `whitelisted_v2` = the **lower confidence bound of net expectancy
(avg_pnl_pct) above break-even**, estimated with empirical-Bayes shrinkage across
the hierarchy bot×regime×alt → bot×regime → bot×ALL. A sparse cell borrows
strength from the higher-level mean (weight n/(n+k)), a cell with no evidence at all
stays at the neutral prior and is **not** whitelisted — this kills the
default-open crutch (B1). The needed columns (`avg_pnl_pct`, `pnl_stddev`) already existed
in `bot_regime_performance` and had been ignored so far. All the knobs
(break-even floor, prior strength k, z-multiplier) are named constants with
conservative starting values — they get calibrated on the VPS DB before any flip,
not pinned down here. The new columns are additive (`ALTER … IF NOT EXISTS`),
the live gate (`get_whitelist_decision`) keeps reading `whitelisted`.

### B9 censoring correction
`CLOSED_REGIME_CHANGE` trades now count with their **real PnL at the
close time** as win/loss instead of a blanket neutral — the auto-close is the
exit of the trade, not external housekeeping. Previously this exactly censored the
losses realized via regime changes and biased the measured ROM1 WR upward
(report 16 B9). Applied consistently at all four classification
sites (`27_bot_regime_analyzer._classify_outcome`, `28_signal_orchestrator._classify_outcome_by_pnl`,
both classifiers in `23_market_tracker`), so that report WR and whitelist WR do not
diverge. `DELISTED/CLEANUP/ORPHAN` remain neutral; near-0% regime closes
continue to be caught by the micro-PnL filter. In practice only `model='ROM1'` carries this
marker (P1.9), so the correction touches no other bot's statistics and **not**
the live gate (which gates on the trigger bots, never on ROM1). **Note for Michi:**
the ROM1 WR shown in VPS reports/market tracker visibly drops as a result —
that is a measurement correction, not a regression loss.

### Discipline
No gate flip, no live activation, no live intervention. B1/B2 remain live in
effect (v1) until Michi flips after the counterfactual comparison. Verification:
`backtest/test_bot_regime_analyzer.py` (new tests for the shrinkage math: formula pin
against the constants, monotonicity in n and spread, prior-fallback hierarchy,
B1 no-default-open, expectancy block despite WR; plus B9 classification) and
`test_signal_orchestrator.py` green (46 + 75 tests), ruff/format/mypy clean,
regression guard `verify` unchanged (24 fixtures, no indicator path touched).
The live v1↔v2 comparison needs a VPS DB session.
## [2026-07-10] The gate's value becomes measurable: ROM1 counterfactual scorer for suppressed signals (T-2026-CU-9050-047)

Until now the benefit of the orchestrator gate was simply **unknown**. The 4D gate
is 89% default-open, and the +8pp ROM1 win rate is distorted by three co-directional
biases — there was no figure for what a suppression saved or
cost. This task delivers the measurement tool (report 16, §8).

### What the scorer does
`tools/rom1_counterfactual.py` computes the hypothetical outcome for every row in
`orchestrator_suppressed_signals`: which ROM1 geometry the orchestrator would have
posted at the signal time, and how that trade would have played out in the first-touch replay
(`tools.walkforward_sim.simulate_exit`) — wick-aware, SL-first,
monitor trailing, fees. Aggregated per suppression reason
(`bot_not_whitelisted:wr_below_overall`, `orchestrator_cooldown`, …): win rate,
net PnL, R. **Positive net PnL on the suppressed side = the gate left money
on the table.**

### Both sides of the same gate
`--side forwarded` scores the let-through side from `orchestrator_open_trades`,
bucketed by `wl_reason` (the B8 column from T-2026-CU-9050-046) — i.e. per
gate PATH: a real 4D cell vs. `no_whitelist_entry` (default-open) vs. fallback.
`--side both` puts both sides side by side at the same horizon. Only this
comparison answers whether the gate path separates winners from losers, or whether the
+8pp WR is an artifact of the default-open rate. The `dedupe` reasons
(same/opposite_direction_open, cooldown) are separated into their own `bucket_class` —
they measure position hygiene, not the 4D verdict, and would otherwise be
misleading.

### Discipline
Pure measurement/scorer layer: no gate flip, no live activation, read-only
DB session, SELECT-only, never commits. R1-clean — the decision candle is the
last one closed at the signal time, the exit scan starts on the candle after
(`as_of_index`). The geometry comes from **one** source: `compute_rom1_trade_params`
got optional as-of parameters `price=`/`df=` (the same P0.10 pattern as
`get_hvn_and_sr_levels(df=)`), so the replay posts exactly the live geometry —
no copy-paste skew (X-R1). The actual run needs a VPS session
(price data/DB); what's delivered is the tooling plus DB-free tests.

Verification: `backtest/test_rom1_counterfactual.py` (19 tests, standalone/DB-free)
covers as-of indexing/no look-ahead, horizon capping, skip accounting, and
aggregation; `test_signal_orchestrator.py` got the as-of path plus a
live-vs-as-of parity test. `guard.py verify` green.

---
## [2026-07-10] The 10s grid is a fiction under load: pump/dump window normalized, dead volume gate repaired (T-2026-CU-9050-035)

The EPD2 retrain this task was set up for **did not** happen — the
data-status check (step 1) blocked it and in the process exposed two latent
regressions from P1.39.

### Why no retrain
`pump_dump_events` contains **zero** rows of the new feature definition. P1.39 is
indeed merged, but bot 10 had been running uninterrupted since the
fleet start on 08.07. at the time of measurement and still held the old module code. The log banner
"ML model loaded" looks like a startup event, but is actually an *hourly*
cache reload (`load_pump_model()`, TTL 3600s): its cadence drifts monotonically over 24h
from 13:41 to 13:44, without the reset that a process restart would
force. The time cut recommended in the task therefore returns an empty dataset.
The retrain is waiting on a bot-10 restart (operator decision).

### Measurement
Against 421 350 real anchors from the live `1minute.json` (6h window): the
bucket cadence is **bimodal** — median 10s, but p90 = 70s, and only 62.7% of
the spacings are under 15s. The detector polls ~530 symbols per REST round trip;
under load, simply no bucket materializes per 10 seconds.

Two defects followed from this that would only have become live at the next restart:

- **`p_chg_60s` lost 38.7% of all ticks.** `WINDOW_EDGE_GUARD = 5` requires
  a bucket at exactly `anchor-60s ± 5s`; this only resolved for 61.3% of the anchors,
  the rest returned unscored.
- **The volume-explosion alert was dead.** The constant `360` moved from
  `len(volumes_10s) >= 360` — a warm-up check across the *entire* 1440-slot deque,
  practically always true — to `len(hour_vols) >= 360`, where the same number
  demands a density of one bucket per 10s across a full hour. Real density:
  ~193/h. The gate held for **0 of 421 350** anchors.

### Fix
`_find_bucket_nearest` picks the bucket with the actual **real** distance to the target that is
smallest within an age band and returns that distance along with it. `p_chg_60s`
and `p_chg_3m` normalize the observed move to a rate per 60s resp.
180s; `buy_pres` and `volat` share the same actual span. On a dense
grid this is the identity (scaling 60/dt: median 1.00, p10 0.75), under load
it reports the rate the window actually supports. Coverage `p_chg_60s`:
61.3% → **97.7%**. The hourly warm-up now gates on the covered time span
plus a sample floor instead of a bucket count.

Deliberately **not** switched to `tolerance=20`: crediting an 80s-old bucket as "60s"
would be the weakened return of exactly the bug that P1.39
eliminated.

### Retrain coupling
The four model inputs shift again as a result — deliberately, and before the
restart, so EPD3 gets fitted directly on the final definition instead of
twice. The precondition for a clean rollout remains T-2026-CU-9050-030
(P1.45): `module_tag` is a source-code constant, the detector reads no
artifact meta — an EPD3 artifact would otherwise silently post under the old tag.

### Entry estimator updated
`p_chg_60s` is thereby a rate and **no longer** a realized move. The builder,
however, read the column as a move (`entry1 = close × (1 + p_chg/100)`) — and because
the window length is not persisted anywhere, the raw move cannot be
reconstructed from the event log (hard rule 7). The entry now comes from `ticker_10s`, the
actually traded price: across the last three days, 7053 of 7055
gated events find a tick within 60s, across all 404 event symbols. If the
tick is missing, the row drops out (`no_ticker`) instead of being estimated — an
unknown entry must become a missing label, not a wrong one. A `--since`
before the first tick aborts loudly, instead of silently halving the dataset.

Verification: `backtest/test_pump_dump_time_windows.py` (18 tests) +
`backtest/test_epd2_entry_from_ticker.py` (5 tests), standalone and DB-free.
Six fail on their respective pre-fix state, among them the three
behavioral witnesses (70s cadence is not scored at all; volume explosion never
fires; single-sample baseline gets scored). The rest run green on both
states and prove that the dense path stays unchanged. `backtest/` overall
316 green, regression guard `verify` + `smoke` green. Takes effect on the next
regular restart, no deploy.

---
## [2026-07-10] Concept spec: MM order-lifecycle patterns for the open Hyperliquid venue decision (T-2026-CU-9050-056)

Pure docs/concept work, no code on the fleet. From the repo audit on 2026-07-10
(KB `mcp-41a50fe33552`), `lihanyu81/polymarket_lp_tool` was flagged as the cleanest
MM order-lifecycle architecture. Since the repo carries **no LICENSE**
(all-rights-reserved), the result is a **pattern harvest in our own words** —
no code copied, ported, or vendored; if this is ever built, it will be clean-room from
this spec.

### Added
- **`docs/MM_ORDER_LIFECYCLE_SPEC.md`** — distills 14 named, transferable
  patterns (reconciliation-instead-of-state-machine, cumulative-watermark fill detection,
  per-side quote diff, cancel-then-repost vs. modify, WS user/market separation,
  priority cascade, reprice speed limits, tick regime, midpoint filter, fill risk,
  structural deleverage, vol gate, hysteresis monitor). Each pattern is mapped from the
  Polymarket-CLOB assumption onto a **Hyperliquid perp order book**
  (mapping table, §7), incl. the three prediction-market assumptions to strip
  ((0,1) price domain, reward band, binary-condition pairing) and the six gaps that
  the source does **not** cover and that need to be designed independently (continuous
  inventory skew, funding awareness, mark/oracle/last, event-risk gate, latency budget,
  maker economics). Conclusion: recommendation "feasible, but only a green light for a
  shadow/paper prototype" plus five open questions for the venue decision.
- **Docs map line** in `docs/ARCHITECTURE.md` §12 (reference to the new spec,
  marked as pre-decision).

**No live relevance:** the spec builds nothing, flips no gate, touches no bot. Any
eventual MM prototype runs shadow/paper first per the spec and is — like every money path
— an operator decision (`OPUS-HANDOFF.md` §6).

---

## [2026-07-10] Orchestrator gate: staleness gate on the 4D cell, `wl_reason` on the forward, docs correction (T-2026-CU-9050-046)

Three findings from the ROM1 deep review, all at the same blind spot: **the
let-through side of the gate was unobservable.** `orchestrator_suppressed_signals`
only logs what was blocked. Why a signal *went through* — a real 4D cell,
`no_whitelist_entry`, or fallback — was recorded nowhere. That is exactly why P0.4
(bot-name mismatch, every signal ran through as `no_whitelist_entry`) could run for
months without being noticed: a silently open gate looks from the outside like a
generous one.

### Added
- **`wl_reason` column on `orchestrator_open_trades`** (B8). `ensure_regime_schema`
  creates it for new DBs and adds it to existing ones via
  `ALTER TABLE … ADD COLUMN IF NOT EXISTS`; `insert_orchestrator_open_trade`
  writes the decision `get_whitelist_decision` actually made.
  Rows from before that stay `NULL` and are counted separately
  in the statistics, instead of guessing a path.
- **Gate-path line in the hourly regime status** (P0.4 remainder). Over the last 24h:
  share of default-open / fallback / real 4D decision. From a 20% bypass
  share (default-open + fallback combined) onward, the line carries a `⚠️`.
- Four tests in `backtest/test_signal_orchestrator.py` (fresh cell decides,
  stale cell falls back, `computed_at IS NULL` counts as stale, `wl_reason` lands
  in the INSERT).

### Changed
- **`get_whitelist_decision` distrusts old cells** (P0.4 remainder/P2.25): a
  `bot_regime_whitelist` cell older than 48h (`WHITELIST_MAX_AGE_HOURS`, two
  analyzer cycles) no longer decides — instead the overall fallback kicks in,
  reason `whitelist_stale:<fallback_reason>`. A missing `computed_at` counts as
  stale. **Semantics change on the money path:** per the audit, the live cells are frozen at
  `computed_at=19.04.`, the fallback lets through below 30 trades — today
  blocked bot/direction pairs can therefore open up. That is the point of the fix,
  but it is a volume-increasing change. `force_close_trades_for_regime_change` uses
  the same function and consequently also closes trades per the fallback logic.
- **`docs/REGIME_ORCHESTRATOR.md`** (P1.10): the docs claimed the system "does not
  trade itself" and was a pure signal router. That has been wrong since the
  ROM1 geometry — a let-through bot signal is only the trigger, `compute_rom1_trade_params`
  discards entry/SL/targets of the original. The consequence (gating statistics ≠
  execution statistics) is now documented there.

**Deploy order:** restart bot 26 before bot 28 — 26 creates the column in
`ensure_regime_schema`, 28 writes it. On a regular fleet start this is
covered (`start_delay` 160 vs. 175). If only 28 starts against a DB without the
column, the INSERT fails and the transaction rolls back: a lost signal,
no Cornix post without tracking.

Not part of this PR: the P1.8 hardening (explicit `open_time`) already came with
T-2026-CU-9050-052. The 72h age bound on
`is_opposite_direction_open` also discussed there was **deliberately rejected** — it would have
freed up a real ROM1 position open for over 72h and let the opposite direction post
against it. Dead OPEN rows are cleared by the corpse reaper in `sync_closed_trades`.
## [2026-07-10] The indicator engine no longer fabricates warm-up values — NaN flows through like KAMA (T-2026-CU-9050-054)

P1.13, verified against the code (trap 13): `2_indicator_engine.py` filled the
warm-up windows of the rolling indicators with `.fillna(0)` resp. `.fillna(50)` —
`wma_*` (`calculate_wma`), `rsi_*` (`calculate_rsi`), `boll_*_20`, and
`donchian_*`. For a young coin, `extract_ml_features` in
`24_quasimodo_bot.py`/`25_smc_ml_sniper.py` reads from this
`donchian_upper_20_dist_pct = (0-close)/close*100 = -100.0`: five of the eleven
price features are hard-pinned to −100 during the first ~20 bars and encode
"young coin" instead of a distance measure. Symmetric in bot and replay (no
train/serve skew), but garbage on both sides.

**Fix:** the undefined warm-up rows now flow through as NaN — exactly like
`calculate_kama` has always done. All affected columns are `REAL` (like
`kama_*`), so the NaN write path is already proven in production. On the
read side, nothing changes forcibly: the bots continue to impute the
head rows via their existing `ffill().bfill()` (turning `-100` into a meaningful
distance to the first real value), replay has discarded them since
T-2026-CU-9050-045 via `dropna()`. The blast radius was checked across all
`_indicators` consumers: every ML feature path imputes (`fillna(0)`,
`ffill/bfill`, or an `isfinite` guard); the only raw consumers (strategy bots
`strat_*`) read the newest 480 candles (warm-up is purely historical) and their
AND-chained NaN comparisons only block strictly more, so they never produce a
signal. `ma_*` was deliberately left untouched (no active consumer, no
distance feature) — outside the verified surface.

Regression guard: the golden was deliberately refreshed
(`KYTHERA_GOLDEN_REFRESH=1`). The 816 breaches are exclusively the
warm-up head rows of the four families (golden `0`/`50` → fresh `NaN`), no
other column drifts — the diff in `golden/` proves exactly that.

**Still open (operator/Michi, C-gate, NOT part of this PR):** the fix is a
DB-writer change and only takes live effect via a recompute of the indicator tables
(today the engine only writes warm-up head rows on the first run of a
new listing). Afterwards a TD2/BB2/QM2 retrain belongs on the shifted
feature distribution, and **only at the artifact rollout** may the `bfill` in
`24_quasimodo_bot.py:126`/`25_smc_ml_sniper.py:220` be removed — never
in isolation.
## [2026-07-10] Finding IDs in the ledger: duplicate guard as a pre-commit hook (T-2026-CU-9050-059)

On 09./10.07., three freshly created findings simultaneously carried the ID **P1.46**.
Several sessions worked in parallel on `AUDIT_TODO.md`, each read the ledger, took
what looked like the next free number, and wrote it back — a classic
read-modify-write race with no allocator. PR #34/#36 manually renumbered to P1.47/P1.48;
the root cause remained.

### Added
- `tools/audit/finding_ids.py` with two subcommands. **`check`** reports duplicately
  assigned IDs and returns exit 1 — this is the safety net. **`next --severity P1`**
  deterministically prints the next free number (max+1 per severity) — this is
  the convenience. Like the KB's `next_id()`, `next` is a snapshot and
  **not a reservation**: two simultaneous calls get the same number.
  What keeps the collision off `main` is `check`.
- **pre-commit hook `kythera-finding-id-guard`** (next to the regression guard) —
  the collision is caught at commit time, not only in review. If
  `AUDIT_TODO.md` is missing, the hook runs fail-open instead of blocking the commit.
- `backtest/test_finding_ids.py` (DB-free, standalone).

The load-bearing distinction is **definition vs. reference**: findings are cited
in prose all across the ledger ("orthogonal to P1.44"), so a naive `grep` on
`P\d+\.\d+` would report dozens of false duplicates and the guard would be
disabled within a day. A finding is **exclusively** defined on its checkbox
line (`- [ ] **P1.45 …`). That is exactly what a dedicated test checks.

The existing set stays unchanged (125 findings, no duplicates; next free IDs:
P1.49, P2.52). No renumbering.
## [2026-07-10] wf_significance MaxDD deconfounded: absolute drawdown in %-points instead of peak normalization (T-2026-CU-9050-053)

Fix for the finding from T-2026-CU-9050-040. `tools/wf_significance.py:max_drawdown_pct`
normalized the drawdown to the running peak (`(equity − peak) / peak`). On the
fleet-wide multi-coin replays, additive equity does not support that: 8.8-20.2
simultaneous signals per timestamp get chained as sequential individual bets,
equity falls far below zero, and the ratio ends up measuring the
random peak height instead of loss clustering.

Fix: the DD is now computed **absolutely in %-points** below the peak
(`equity − peak`, without normalization; the +100 base cancels out). The observed
and permuted path are thereby measured in exactly the same way. The side finding
(`np.where(peak > 0, peak, 1.0)` silently switched units and the ×100 scaling
at peak ≤ 0) resolves by construction — without division there is no guard
needed anymore. Option chosen: absolute DD instead of an overlap-respecting
equity path; the latter would need capital-allocation assumptions that the replay JSONL
does not support (boundary named in `docs/WF_SIGNIFICANCE.md`: a path-clustering statistic,
not a genuine portfolio drawdown).

Verified against the real artifact (200 permutations, seed 42): rub/LONG flips from
p = 1.000 ("atypically lenient") to 0.005 (obs. −55.208 vs. median −17.182),
ufi1/SHORT from 0.035 to 0.005. `backtest/test_wf_significance.py` mechanically pins
the peak-height invariance and the non-positive-peak case (mutation-tested:
both fail against the old formula — −25% vs. −45.45% resp. −4000). The reading aid
in `docs/WF_SIGNIFICANCE.md` has been sharpened accordingly.

**No deploy statement in the batch-E table changes.** It rests on statistic 1
(random control) and 3 (bootstrap CI), both order-invariant and unaffected by the
DD fix; the DD statistic was already marked "not operationally readable" and went
into no deploy call.
## [2026-07-10] P1.8 follow-up fix: ROM1 lifecycle sync had been silently dead since 04.07. — open_time now explicit naive UTC + twin-based corpse reaper instead of age bounds (T-2026-CU-9050-052)

The VPS verify session T-2026-CU-9050-044 confirmed the P0 suspicion from the
ROM1 deep review: the P1.8 fix from 04.07. (±60s match against
`ai_signals.open_time`) did not repair the sync — it silently killed it.
`insert_rom1_signal` did not set `open_time` — the DB default `now()` stamps
session-TZ Europe/Bucharest local time into the naive timestamp column,
a constant +10.799 s (+3 h) offset against the naive-UTC `opened_at` of the tracking row. The
±60s window could never match: the last `lifecycle_sync` close was exactly at the
deploy time 04.07. 11:10, after that 395 accumulated OPEN rows (208 older than
72 h) and `opposite_direction_open` suppressions rising from 4/day to 165/day (166
suppressions across 79 coins demonstrably caused by corpse rows).

Fix in `28_signal_orchestrator.py`: (1) `open_time` is now set explicitly as
naive UTC (`core.time.utc_now_naive`, the same source semantics as the
`opened_at` of the twin row; monitor 8 treats `open_time` as UTC anyway). This makes
`ai_signals.open_time` a mixed domain (ROM1=UTC, rest=
session-local via default) — documented in `docs/UTC_POLICY.md` §3, unifying it
remains the R3 flip. (2) New **corpse reaper** at the START of
every lifecycle-sync pass (decay thereby no longer depends on the health of the
match loop): an OPEN row whose `ai_signals` twin no longer exists
(the trade was closed but never synced — exactly the corpse class) gets
set to `CLOSED_NEUTRAL` / `close_reason='corpse_reaper'` after a minimum age of 72 h.
The twin check is **row-anchored** (±60 s around `opened_at`, both
rows are created in one transaction) — a live trade on the same
coin+direction thus does NOT shield a stacking-era corpse. For the
legacy population (open_time stamped in session local time), there is a
second window via the **hard-coded historical writer TZ**
`Europe/Bucharest` (deliberately not `current_setting('TimeZone')`: a
future R3 flip of the session TZ must not un-shield live legacy positions;
DST is handled via `AT TIME ZONE` per timestamp). This
legacy window applies **symmetrically** in the sync match loop as well as in the
reaper's anti-censoring clause — otherwise a legacy trade that closes AFTER the
deploy would lose its real WIN/LOSS to the reaper; this way the
match loop also recovers the real outcomes of the old corpses.
The window is collision-free because the 4h cooldown per coin+direction structurally
excludes two same-direction trades ~3 h apart (pinned by test,
incl. the window constant `LIFECYCLE_SYNC_WINDOW_SEC` for all
anchor windows). Anti-censoring clause: if a syncable
`closed_ai_signals` row already exists (in either of the two windows), the
reaper skips — the match loop classifies the real WIN/LOSS outcome, never the
reaper (this closes the monitor-commit race for >72h trades). `closed_at` of
reaped rows is the reap time, not the real close time —
duration evaluations must exclude `close_reason='corpse_reaper'`.
The main loop now isolates the three stages individually (try/except + rollback
per stage): a poison row in the regime check or gating can no longer
permanently starve the lifecycle sync (and thereby the only decay path). The money path
stays fail-closed here: if the regime stage fails, the gating pass is
skipped (no new exposure while the auto-closes are disrupted), and an
outer catch-all keeps the process alive. The two-window predicate builds ONE helper
(`_anchor_window_predicate`) for all three SQL sites; the historical
writer TZ lives canonically in `core/time.py` (`LEGACY_WRITER_TZ`). Empirically
verified against the live DB (read-only): 0 of 409 OPEN rows have more than
one close candidate across both windows (no cross-match in the existing data), and
the complete first pass across 440k `closed_ai_signals` rows takes 1.8 s
(4.4 ms/row) — no loop blocking.
Pure bookkeeping, no Telegram post. This means the corpses truly disappear
from the OPEN inventory — they no longer block the direction checks, no longer feed
the regime-change auto-close with spurious `Close` commands, and are
no longer rescanned on every sync pass. (3) The direction checks
deliberately remain WITHOUT a time bound: an age bound (including the existing 72h bound
from P2.26 in `is_same_direction_open`, removed here) also lifts the protection for
REAL >72h positions — ROM1 sets no `expiry_hours`, a legitimate
position can stay open indefinitely, and without the block, the opposite direction would
flip the live position (review finding from PR #40). The liveness criterion is
now the twin, not the clock. Deliberate trade-off: a STUCK twin
(the monitor cannot score the coin) still blocks — protection over availability;
the decay path for that is the housekeeping's delisted cleanup.

Verification after deploy: `lifecycle_sync` closes reappear
(>0/day), the OPEN-older-than-72h inventory (208 rows as of 10.07., growing toward
395) is worked down in the first sync pass — old corpses with an existing close row
get their REAL outcome via the match loop (`lifecycle_sync`), only
unmatched remnants go out neutrally as `corpse_reaper` — and afterward the
inventory stays ~0; NO `Close`-command burst on the next regime flip. Seven new
tests pin the INSERT column + naive-UTC value, the bound-free direction checks,
the reaper contract (reaper-first, row-anchored twin window, hard-coded
legacy TZ in both subqueries, anti-censoring clause, no outbox write), the
legacy window in the match loop, and the cooldown invariant that makes the
legacy window collision-free — `backtest/test_signal_orchestrator.py`;
suites test_regime_detector/test_bot_regime_analyzer remain green.

## [2026-07-10] Significance layer over the real batch-E replays: layer confirmed, MaxDD statistic refuted (T-2026-CU-9050-040)

The VPS remainder of T-2026-CU-9050-027 D3: `tools/wf_significance.py` ran read-only
over `mis1_replay_400d`, `rub_replay_365d`, `abr1_replay_365d`, and
`ufi1_replay_365d` (`--group-by strategy+direction`, n=1000, seed 42), results
in `docs/WF_SIGNIFICANCE.md`.

**The layer behaves as specified.** The control mean hits the round-trip fee drag
in all seven groups (−0.0961 … −0.1006 against an expected −0.10),
and the trade-weighted aggregates reproduce the simulator's `*_summary.json`
exactly (WR, avg_r, avg_pnl). The run is deterministic.

Substantively, the replays measure the **raw detector**, not the deployed model:
abr1/SHORT has a raw edge and abr1/LONG is significantly worse than a
directionless random trader (matches the live picture), while rub is
raw-negative in both directions even though RUB2-SHORT runs live —
there, model selection carries the edge. mis1/SHORT is a null edge despite p = 0.001
(CI lower bound 0.0006).

**Refuted:** the reading rule for `p_value_dd_worse`. `max_drawdown_pct` normalizes
to the running peak, but the additive equity of these fleet-wide replays
chains 8.8-20.2 simultaneous signals per timestamp as sequential
individual bets and falls far below zero (rub/LONG: 72% of the path negative). The
ratio then measures the random peak height instead of loss clustering: with
absolute DD in %-points, rub/LONG flips from p = 1.000 ("atypically lenient
path") to p = 0.005 (worse than 199 of 200 random orderings) — the
previous rule would have set the DD budget exactly backwards. Statistic 2 is
marked "not operationally readable" in the docs; the fix is T-2026-CU-9050-053.
Statistics 1 and 3 are order-invariant and unaffected.
## [2026-07-10] EPD and SRA load their artifact via the shared contract (T-2026-CU-9050-042)

The last two instances of the P1.45 error class: a post path writes a
hardcoded model tag instead of reading `model_id` from the artifact meta
(hard rule 6). Unlike with MIS/RUB/QM, though, the tag here was only the
symptom — underneath was a **format break between retrain output and
the live load path**, and that had to go first.

**Finding correction to the task doc (trap 13):** `retrain_sra2.py` does *not* emit a
dict artifact, but native XGB JSON + `_meta.json`/`_calib.pkl` — the same
format as ABR2. The format mismatch existed only for EPD; SRA merely lacked
the meta read. Verified against the code, not taken over from the annotation.

Three steps, one bot per commit:

- **`core/model_artifacts.py`** gets `load_artifact_json()`. The
  XGB-JSON sidecar loader was until now baked into
  `18_ai_abr1_bot._load_model_contract`; now it returns the same
  contract dict as `load_artifact()` (dict-pkl). Without `_meta.json`, the
  named legacy contract applies (tag + threshold from constants, `features=None`),
  with meta the tag, threshold, and feature contract come from the artifact. A
  non-binary `model_type` in the binary slot is rejected instead of silently
  reading the wrong `predict_proba` column. `maybe_reload` now dispatches by
  the file extension — routed through the pkl loader, a JSON artifact would never
  have been re-read and would have silently kept serving the old generation after a deploy.

- **SRA** (`9_ai_sr_bot.py`): loaded its `.json` models raw into an
  `xgb.XGBClassifier` and posted both directions under the constant `SRA1`.
  The tag now comes from the meta, likewise the threshold. Additionally a
  **serving parity break** that would have spoiled an SRA2 rollout: bot and
  trainer used the same column names with **different formulas** —
  `pct_ema9` was `(close-ema9)/close` in the bot, `(close-ema9)/ema9` in the trainer —
  and `macd_dif_pct`/`macd_dea_pct`/`atr_pct` was not built by the bot at all. The
  builder now lives once in `core/sra_features.py`, imported by bot and
  trainer (X-R1 rule). The legacy vector stays untouched alongside it — it is
  the contract of the model deployed today. A missing artifact idles the
  direction instead of running `exit(1)` into the watchdog restart loop (trap 3).

- **EPD** (`10_pump_dump_detector.py`): live runs a **raw 3-class** model
  with a positional 10-feature array (success = class 2/0, threshold hard 0.60).
  The EPD2 artifact, in contrast, is **binary per direction**, with 16 named features
  incl. the 6 funding columns and threshold/`model_id` in the meta. Both paths
  now coexist: without an artifact the legacy branch keeps running bit-identical,
  with an artifact it wins and brings its tag + threshold along. The funding features
  are pulled **as-of the event** (`funding_features_asof`, like
  `tools/epd2_build_dataset.py:231`), per trigger behind the
  `vol_ratio>=5` pre-filter. Missing funding **history** becomes 0 like
  `fillna(0)` in the trainer (serving parity); a missing feature **name**,
  in contrast, still rejects the artifact and idles the bot (P0.12).

**Known performance risk (documented, not optimized — only kicks in with a
deployed EPD2 artifact):** the funding load is a DB round trip per
qualifying 10s tick, not per signal. The pre-filter `vol_ratio>=5` holds
as long as the volume event runs, and the shadow branch deliberately does not reset
the 900s timer (P1.41) — a coin in the shadow band thus pulls the query on
every tick, market-wide in parallel across all affected coins. A TTL cache would
**not** be a trivial fix here: it would shift the as-of time of the funding features
and break exactly the trainer parity this commit establishes. To be clarified
before the EPD2 rollout (measurement, then possibly moving the load behind a time gate that
does not change the as-of time).

**Transitional dedup**, per bot wherever it actually locks: the post tag is
simultaneously the dedupe key, and it flips on a generation switch. SRA checks
the master-log duplicate check (otherwise an SRA2 rollout would treat every
already-processed trade as new and post it again) and the cooldown against the
old tag. EPD's only tag-coupled lock is the shadow-log dedupe; for that,
`core/signal_post.log_prediction` now accepts an optional `legacy_tag` —
what gets written is always under the current tag. All other callers are
untouched (default `None`).

**Live semantics unchanged.** No artifact is deployed, so both run on
the legacy contract: same tags, same thresholds, same feature vectors,
same dedupe queries (the transitional binds collapse when the tags are identical).
Verification DB-free: `backtest/test_model_artifacts.py` (10),
`test_sra_tag.py` (11), `test_epd_tag.py` (12) — loader and dedupe behaviour
genuinely executed (fake cursor), the rest are static nets; all mutation-tested.
No rollout, no artifact touched, no DB change.

**Blast radius of the `core/` changes** (shared code, hence spelled out): (1)
`log_prediction` is additive — `legacy_tag` has the default `None` and leaves the
old single-tag query byte-identical, bots 30-33 are untouched. (2)
`maybe_reload` now passes `default_tag` on the daily reload instead of the
**currently loaded** tag as the fallback. For `13_ai_rub_bot.RUB2_SHORT`
(a hand-built contract dict without `default_tag`), `.get()` falls back exactly to
`artifact["tag"]` — precisely the expression the old `maybe_reload`
used, so bit-identical. For bots 30-33 (contract via `load_artifact`),
the difference only kicks in when an artifact carries **no** `model_id` in
its meta on reload; previously the reload then inherited the tag of the generation it
was just replacing. That is the actual bugfix at this spot, not
collateral damage — in normal operation (the trainer always writes `model_id`) the
path is dead.

**Open for Michi:** (1) the EPD2/SRA2 rollout is now unblocked — operator decision.
(2) Two new findings of the same class as P1.48: neither EPD nor SRA has an
active-trade check against `ai_signals`; EPD's only re-fire lock is an
in-memory 900s timer that does not survive a process restart.
(The `P1.46` number conflict among three sessions had already been resolved via
PR #36 at merge time on `main` — sniper keeps P1.46, ATB1 became P1.47, RUB became P1.48.)
## [2026-07-10] Second look-ahead in `walkforward_sim.load_joined`: `bfill()` removed (T-2026-CU-9050-045)

Side finding from the blast-radius analysis for T-2026-CU-9050-037. `load_joined` called
`bfill()` in addition to `ffill()`. The `ffill` closes interior gaps from the
past and is harmless; the `bfill` filled the remaining **head rows from
the future**.

> **Correction 2026-07-10 (after code review of `2_indicator_engine.py:335-448`):** the
> original version of this entry justified the fix with "the warm-up columns are
> NULL (`ema_200` needs 200 bars, the Donchian channels 20)". **That is wrong.** The
> engine delivers these columns filled: `ema_*`, `macd_*`, `atr_14`, `tsi_*` are
> `ewm(adjust=False)` and defined from row 0; `wma_21`, `donchian_*_20`, `boll_*_20`
> carry `.fillna(0)`, `rsi_14` carries `.fillna(50)`. The fix remains correct, but its
> mechanism is different — corrected below. The error class is trap 13 from
> `docs/OPUS-HANDOFF.md`, one level deeper: the loader was checked against the code, the
> data producer behind it was not.

Exactly **one** of the fifteen columns that `load_joined` reads is genuinely
empty in the DB: **`kama_21`**. `calculate_kama` (`2_indicator_engine.py:344-350`) deliberately
does not fill — rows 0-19 are NaN, row 20 carries the SMA bootstrap. `bfill` thus had
exactly one target: it wrote this bootstrap value backwards into the 20 rows before it, i.e.
future into the past. `run_td_bb` only starts at `t = WINDOW-1 = 149`,
but the feature candle is the **pivot index** (`lo_b + p3`), and that reaches down to row 0
for small `t`. Unlike the forming-candle finding from T-037 — which
self-quarantines because its records get no label and `load_replay` discards
them — this leak thus landed in **labeled** training rows of the td/bb replays
(models TD2/BB2, bot 25). Affected are coins whose listing falls within the
replay window; for older coins the frame contains no NaN and `bfill` was a no-op.

**The larger neighbouring finding this fix does NOT address:** the `.fillna(0)` columns
are not NaN and survive the `dropna()`. For a young coin, the first ~20
bars carry `donchian_upper_20 = 0.0`, and `extract_ml_features` turns that into
`donchian_upper_20_dist_pct = -100.0`. Five of the eleven price features are hard
pinned there. That is **P1.13** in `AUDIT_TODO.md` ("`fillna(0)` on warm-up windows writes
fabricated indicator values", fix: let NaN flow through as KAMA does) and belongs
before the next TD2/BB2/QM2 retrain, because it shifts the feature distribution of both bot
**and** replay equally.

Fix: `to_numeric` moved before `ffill`, `bfill` removed with no replacement, the
remaining NaN head rows are dropped. An event without real indicators is not a
training datum. `backtest/test_feature_lookahead.py` mechanically pins this
(mutation-tested: with `bfill` the test fails).

**Not touched, deliberately:** `25_smc_ml_sniper.py:220` and `24_quasimodo_bot.py:126`
carry the same line. But they window `DESC LIMIT 150` resp. `100` **from now on** — there,
`bfill` fills from rows the bot has already seen anyway, so no look-ahead
relative to decision time, just a silent imputation of the feature vector. And
it only fires when the first 20 candles of the coin's history lie within the window, i.e. the
coin has ≤ ~170 candles (`1h`: 4-7 days old; `4h`: 17-28 days) — for the large majority
of coins, `bfill` there is a no-op.

More important than the line itself is its **coupling to the retrain**: since this commit,
the replay discards the 20 head rows, the live bot keeps imputing them. The next
TD2/BB2/QM2 trained from the replay has never seen them. The bots must therefore
**not** be aligned in isolation, but only **together with the artifact rollout**
— otherwise exactly the train/serve skew arises that T-037/T-045 are fighting against. Money path,
operator decision (`docs/OPUS-HANDOFF.md` §6).
## [2026-07-10] `legacy_trainers/` is not throwaway material — operator question §5.8 closed (docs)

`docs/CANDLE_CALL_SITES.md` listed `legacy_trainers/` in three places as "dead
code" and "deletable". Both are misleading and stood in the same paragraph as the
already corrected `db_schema_analysis.py` misfinding (T-2026-CU-9050-039).

The correct picture: no running process imports the scripts, and they are deliberately
not runnable (credentials replaced by `os.getenv(...)` placeholders). That is
exactly their purpose. They are the **only reproduction basis for the eight live-
loaded model artifacts** — `legacy_trainers/README.md` maps every trainer to
its artifact and bot (MIS1→11, ABR1→18, ATS1→12, RUB1→13, SRA1→9,
AIM1→15, EPD1→10, ATB1→14), and the folder was created in `7b5ec89` explicitly
as "frozen provenance". Their preserved defects (label geometry,
split leakage, in-sample thresholds, feature skews) explain the behaviour of the
live models and are the reference the retrain program measures against.

For the migration they are irrelevant — they will **not** be rewired, and
after phase C they never run again anyway. That is an argument against
rewiring, not one for deleting; the old text conflated the two.

**Decision: `legacy_trainers/` stays.** Operator question §5.8 is thereby
answered in both parts and no longer blocks phase 1. A `NICHT LÖSCHEN` note
now also sits at the top of `legacy_trainers/README.md`, where a follow-up agent looks
first. No code touched.
## [2026-07-10] Four red tests on main fixed (T-2026-CU-9050-038)

CI only gates ruff/format, mypy, syntax/imports, and the secret regex — pytest never
runs anywhere. Four tests in the `backtest` suite were therefore red unnoticed,
some since the initial import. During T-2026-CU-9050-034 they showed up when the
full suite was run. Each was diagnosed against the code, none silently skipped
or deleted.

- **`test_bot_naming::test_similar_but_not_matching`** — the test held onto the
  MIS1-only contract, while `core/bot_naming.py` was deliberately generalized to
  `MIS\d+` in `99e9de3` (hard rule 6: retrains post under a new tag).
  The function's docstring already documents `pretty_name("MIS2-72H") == "MIS2-72h"`.
  The test was updated; the actual invariant (generations
  do not mix) is preserved as its own test.
- **`test_bot_regime_analyzer::test_regime_lookup_for_trade`** — dead on arrival: it
  imported a never-existing module `src_27` and recomputed its assertions
  inline, without ever calling the production code. Replaced with real tests
  against `27_bot_regime_analyzer._compute_stats` (aggregate, empty input,
  Sharpe guard at n=1).
- **`test_signal_orchestrator::test_identify_bot_channel_fallback`** — tested
  the environment instead of the code. `core.config._ch()` returns `0` for unset
  channels; on the build machine (empty `.env` stub) all five keys of
  `CHANNEL_TO_BOT_FALLBACK` thereby collapsed to `0`, and the last entry won.
- **`test_signal_orchestrator::test_compute_rom1_trade_params_long`** — the
  R4 audit fix pulled `cap_leverage_to_sl()` into the ROM1 path, but the test only mocked
  `get_max_leverage`. `params["leverage"]` was therefore a `MagicMock` from
  the mocked `core.trade_utils`. The test now uses the real function
  and checks the actual cap (`"6x"`: an 8% SL distance caps the
  desired 20x).

### Live semantics
One production change: `CHANNEL_TO_BOT_FALLBACK` is now built via
`_build_channel_fallback()` and drops the `0` sentinel for unset
channels. On the VPS, all five `CH_*` are real, distinct Telegram IDs
— the map is unchanged there. The filter only kicks in in the degenerate case:
instead of a disabled bot resolving to a **foreign** bot name, `identify_bot`
now returns `None`. Since `identify_bot` is only ever called with real
channel IDs (`28:659`), live behaviour does not change.

### Side findings (fixed along the way)
`test_signal_orchestrator.py` and `test_bot_regime_analyzer.py` only collected
successfully when, by chance, an alphabetically earlier test file had already set
`DB_PASSWORD` resp. `TELEGRAM_BOT_TOKEN`; both now seed their own dummies.
`test_abr1_detection.py` broke on collection: `pandas_ta` is in
`requirements.txt:18` and installed on the VPS, but not installable on this
Python-3.14 build machine (pulls in `numba`, no cp314 wheel, the
source build fails). The hard collection error was replaced by a named
`pytest.importorskip` — a pure environment issue, not a code bug.

### Verification
`python -m pytest backtest -q` → full suite green, exactly one skip (the named
pandas_ta `importorskip`); additionally every file in the suite now runs green
individually (the import-order coupling is gone).
ruff, `ruff format --check`, and mypy clean.
`python tools/regression_guard/guard.py smoke` OK — the guard was not
refreshed. The new guard test against `_build_channel_fallback` is mutation-
checked: remove the `if cid` filter and it turns red.
## [2026-07-10] RUB gets its siblings' active-trade check (T-2026-CU-9050-043)

`13_ai_rub_bot.py` was the only AI bot without a position guard: its only
re-fire lock was the 4h cooldown (`:252`), and the entire file only touched
`ai_signals` in a write (INSERT `:376`). A cooldown limits signal
**frequency**, not the number of simultaneously open positions. A mean-reversion
trade regularly survives its four hours — after that, the same coin was allowed
to fire again in the same direction, and Cornix opened a **second full position
with its own SL** next to the first. MIS (`:318`), QM, and the sniper (`:116`) have
had the guard all along; RUB lacked it with no documented reason. This is also the
reason why the transitional dedup from T-2026-CU-9050-030 had to fall back to
the cooldown for RUB — there simply was no check it could belong to.

Operator decision made in advance (Michi, 2026-07-10): not intentional
averaging-down, but a bug.

Fix:

- Before the (expensive) ML prediction, the bot now checks
  `SELECT 1 FROM ai_signals WHERE symbol/direction/model IN (%s, %s)` and
  skips the signal on a hit — pattern from `11_ai_mis_bot.py`.
- The bind uses the same **direction-dependent** tag that the post path also
  writes (LONG `RUB_LONG_TAG`, SHORT `RUB2_SHORT["tag"]` from the artifact meta),
  plus `RUB_LEGACY_TAG` as a transitional dedup: the tag is simultaneously the
  dedupe key, and it flips on the RUB3 rollout. Without the old tag in the `IN`,
  an open RUB2 position would no longer block a RUB3 signal on the same coin —
  exactly the second live position this guard prevents. As long as the tags
  match (today), the `IN` is a no-op.
- The cooldown stays **unchanged** as a frequency lock alongside it (like with
  MIS, both run in parallel). Its now-wrong comment ("does not check ai_signals")
  was updated to match.
- `backtest/test_rub_tag.py`: two new DB-free tests (guard present + skip;
  binding to `module_tag` **and** `RUB_LEGACY_TAG`). Mutation-tested — remove the
  legacy tag from the bind resp. remove the check entirely ⇒ both fail.

**Live semantics deliberately change here**, unlike with T-030: signals on a
coin on which a RUB trade of the same direction is already open now drop out. The
first position, every signal on a free coin, and the opposite direction remain
untouched; the cooldown path is bit-identical. No DB change, no rollout.

**Open for a VPS session:** the retrospective measurement of how often
`(symbol, direction, model='RUB2')` was actually open multiple times simultaneously
(`ai_signals` / `closed_ai_signals`, read-only). Not blocking for the fix.
## [2026-07-10] Duplicate `db_schema_analysis.py` cleaned up (T-2026-CU-9050-039, P3.1)

`tools/db_schema_analysis.py` deleted. The root copy is canonical and stays
unchanged; the fleet is unaffected (the script is a read-only
DBA tool over the PostgreSQL system catalog, not a bot path).

The initial assumption that both files were **byte-identical**
(`docs/CANDLE_CALL_SITES.md` §2) was **wrong** and is now corrected there:

- The root copy carries the ruff cleanup from `052ba4c` (import sorting,
  `zip(..., strict=False)`, formatting); the `tools/` copy was unchanged
  since the initial import `b6735d9`.
- The `tools/` copy was furthermore **not runnable**: its
  `sys.path.insert(0, dirname(__file__))` pointed to `tools/`, where there is no `core/`
  — `from core.database import …` always failed, it aborted with
  "core.database not found". `audit_reports/10_dashboard_tools.md:47`
  and `AUDIT_TODO.md` P3.1 had already described this correctly.

No change needed to `pyproject.toml` or `.github/workflows/typecheck.yml`:
both exclude entries name the root file, which stays (`tools/` is excluded
wholesale anyway).
## [2026-07-10] Watchdog backoff no longer blocks fleet supervision (T-2026-CU-9050-029, P1.37)

`time.sleep(delay)` sat in the per-process body of the monitor loop. For up to
900 seconds, this froze the **entire** watchdog: no other bot was
supervised, no park marker honoured, no dashboard restart consumed,
no health check run. The watchdog is the fleet's only actor — a single
crash-looping bot thereby took the supervision of all ~29 others down with it.

Second bug on the same lines: after the sleep, `start_process` ran
unconditionally. Whoever parked the bot during the 900s watched the watchdog
revive it anyway.

The delay is now a **per-process deadline** (`_restart_not_before`). The
loop keeps spinning and only skips the affected bot. The order of the
branches is load-bearing and documented on the function: park beats everything
(and discards a pending deadline), a dashboard restart beats the
backoff, only then does the deadline apply. Because the park check thereby runs
again every 10s cycle, a park during the backoff window keeps the bot
down — the second bug falls out through the same restructuring.

The backoff curve itself is unchanged (0/15/60/300/900s after crashes in the
last hour) and pinned by a test.

**Refactor with touch context:** the per-process body now lives in
`supervise_process(p_info, current_time)`. Every `continue` became a `return` —
equivalent for a loop body. Without this extraction, the deadline is
not testable without running `main()` with its lock, orphan kill, and staggered
fleet start.

**Evidentiary status, honestly:** `backtest/test_watchdog_backoff.py` (new, standalone,
DB-free, 6/6) are regression guards on the new behaviour, **not** witnesses
of the old bug — on the pre-fix state they error, because `supervise_process`
did not exist yet. The old bug is directly readable in the pre-fix code
(`main_watchdog.py:443-447`). So it never comes back, the fixture patches
`time.sleep` with a mock that raises: any future blocking wait in the
supervision path turns the suite red.

Takes effect on the next regular watchdog restart, no deploy.

---
## [2026-07-10] SMC sniper: pivots no longer on the running candle (T-2026-CU-9050-036, P1.46)

`25_smc_ml_sniper.py` reads 150 candles `DESC`, flips to ASC — and until now let
`scipy.signal.argrelextrema` run over the **full** frame. The
last row is the forming candle. Its high/low moves, so the
pivot set repainted **within** the running candle: the three drives of a
three-drive and the level of a breaker block shifted after the
signal had already been posted. The sibling bots have been dropping the forming candle since
July (`24:138` from P1.24, `16:334` from P1.27, `21:126`); 25 was the only
gap — and the only one of the four that posts live on the money path (hard rule 5).

Fix: `c_highs, c_lows = highs[:-1], lows[:-1]` before the two
`argrelextrema` calls, pattern like `24_quasimodo_bot.py:138`. The
pivot indices stay aligned to the full arrays (`highs[p1]`, `rsis[p1]`
still work unchanged), and all `len(df)-1`/`len(df)-2` offsets — the
BB feature row, the breakout window, the freshness gates — remain
untouched. A `df.iloc[:-1]` on the frame would have shifted exactly these offsets by one
candle; that deliberately did not happen and is pinned by a test.

`current_price = closes[-1]` stays **live**: it is the CMP the entry
is set against, plus the trigger for BB level proximity — not an analytical
input. The R1 end state (`include_forming=False` also for the price side)
depends on operator questions 4/6 from `docs/CANDLE_CALL_SITES.md` and on
migration block 4.

Signal-rate delta, replayed DB-free against the regression-guard fixtures
(4 coins × 1h/4h, 3,608 scan points, each a 150-candle window with the last
row as the forming candle; the geometry trigger before the ML gate and
cooldown is counted). Reproducible via `python tools/sniper_forming_delta.py`:

| Pattern | before | after | both | only before | only after |
|---|---|---|---|---|---|
| BB LONG | 58 | 57 | 50 | 8 | 7 |
| BB SHORT | 65 | 61 | 56 | 9 | 5 |
| TD LONG | 11 | 10 | 10 | 1 | 0 |
| TD SHORT | 20 | 19 | 17 | 3 | 2 |
| **Sum** | **154** | **147** | **133** | **21** | **14** |

So **−4.5%** trigger rate; 21 triggers drop out, 14 are added (the
shifted pivot set changes `peak_idx[-2]` and thereby the BB level). The
replay measures exactly the code delta (line in vs. out); the real
live repaint is larger, because there the forming candle is only partially
filled. R1 lowers the signal rates — that is the purpose; re-tune thresholds
only after the retrain.

Deliberately **not** fixed along with this: `argrelextrema(mode='clip')` still lets
unconfirmed pivots through at the right edge (the `max_confirmed_idx` filter from P1.24).
For 25 this is not a drop-in — the TD freshness gate
(`len(df) - p3 <= PIVOT_WINDOW + 2`) specifically looks for these edge pivots. A filter
there would be a strategy change, not a bugfix, and belongs in a
separate task.

Verification: `backtest/test_sniper_forming.py` (new, 4/4, DB-free — incl. a
numerical test that reproduces the repaint mechanism itself),
`backtest/test_sniper_tag.py` (4/4), `guard.py smoke` green, ruff + mypy green.
Takes effect on the next regular restart, no deploy.
## [2026-07-10] Pump/dump window time-based instead of index-based (T-2026-CU-9050-029, P1.39)

The detector sliced its windows via list indices: `prices[-7:]` only meant
"the last 60 seconds" if every 10s bucket arrived. On a
WS gap — most likely exactly during a spike, when the socket is busiest —
"-7" spanned minutes, and the model scored a silently stretched window.

On top of that, a second, independent bug: `volumes_10s` was **filtered** on
`v10s_valid`, `prices` was not. `volumes_10s[-18:]` and `prices[-18:]`
therefore pointed at different points in time as soon as a single bucket was invalid.

Both sections (the volume-explosion alert and the ML feature path) now route via
`_find_bucket_before` / `_find_bucket_range`, which select by timestamp —
the same helpers the price-spike path has long used. The flat
`prices`/`volumes_10s` lists are removed without replacement: that both were
unused after the rework proves that no index arithmetic was left over.

If the bucket from 60s ago is missing, the tick is **skipped** instead of
writing a fabricated `0` into the model as a feature — a 0 is a measured value,
not "unknown".

### Anchor instead of the wall clock
All bucket lookups measure against `bucket_anchor` (the stamp of the most recent
bucket), not against `now`. The stamps are floored to the 10s grid, `now`
is the call time — and the detector iterates ~530 coins after a
REST round trip, so the offset drifts across the batch too. Measured against
`now`, the 60s window silently shrank to 6, then 5 buckets from an offset of 5s
onward: `buy_pres`/`volat` described ~50 seconds, while `p_chg_60s`
still measured a real 60 seconds. Three features that are supposed to describe
the same span did not. Against the anchor, every target time sits exactly on
a grid point, and `WINDOW_EDGE_GUARD = 5` now only absorbs
parse noise. Found in the `z-code-reviewer` pass, not by the first
test round — that one synthesized buckets with offset 0.

Also switched over were the three pre-existing lookups of the
price-spike path: two time bases for sibling lookups of the same function
would be worse than one wrong one. Deliberately **not** switched, because they are genuine
wall-clock semantics: the staleness check, both alert cooldowns, and
`pump_dump_events.spike_time`.

### Measurement
In the test's gap scenario, the old index arithmetic reported `p_chg_60s = +100.0`
— it reached across a 10-minute hole to a bucket at half the price. The
time-based variant reports the true `0.0`. Exactly such values previously also
landed in `pump_dump_events`.

### ⚠ Retrain coupling
`vol_ratio`, `p_chg_60s`, `buy_pres`, and `volat` are model inputs **and**
get logged to `pump_dump_events` in this form, which `tools/epd2_build_dataset.py`
trains on. The deployed EPD2 artifact was fitted on the old definition;
until the retrain rollout, serving runs against a slightly shifted distribution.
On gap-free ticks, old and new are identical (control tests prove this),
the drift exclusively affects gap ticks — but there the old value was
wrong, not just different. Operator decision Michi 2026-07-09; follow-up task
**T-2026-CU-9050-035** (EPD2 retrain on the new feature definitions).

Verification: `backtest/test_pump_dump_time_windows.py` (new, standalone,
DB-free, 6/6). Four tests fail on the pre-fix state; the remaining two run
green on both states, proving that the gap-free path is unchanged. Takes effect
on the next regular restart, no deploy.

---

## [2026-07-09] "Opened" count deduplicated, EPD2 shadow inserts throttled (T-2026-CU-9050-029, P1.44 + P1.41, PR #23)

Two halves of the same defect: the writer produced shadow rows without a
throttle, the reader counted them — and additionally counted posted AI signals
twice. The per-bot statistic is the decision basis for the
orchestrator gating, so an inflated "Opened" count is a
money-path defect.

### P1.44 — reader: opens come from `ai_signals`, not from the prediction log
`ml_predictions_master` is an append-only log — nothing anywhere in the repo
deletes from it. `closed_ai_signals` holds the same signals after closing, and
both frames landed in `df_all_created`. Every AI signal that opened **and**
closed within the window thus counted twice. On top of that, the log carried
shadow rows (`posted=False`) that were never traded.

The classic side never had this problem: the monitors DELETE from
`active_trades_master` resp. `ai_signals` on close and INSERT into the
`closed_*` table — active ∪ closed is thus disjoint. The AI side now
mirrors that: `ai_signals` ∪ `closed_ai_signals`. Both posts share a
`_load_open_ai_signals()` helper; the drift between the summary and
per-bot post was the actual root cause.

**Rejected alternative** (operator decision): `ml_predictions_master WHERE
posted=TRUE` as the source. The log is **deduplicated** (4h per module/coin/
direction), not complete — a legitimate re-post within that window would have no
row, and opens would be **under**counted.

### P1.41 — writer: EPD2 shadow inserts now go through `log_prediction()`
The shadow branch (`0.25 ≤ p < 0.60`) inserted on every qualifying
10s tick. The 900s gate above it does not slow it down: `last_alert_time` is only reset on the
live-trade branch. A coin that permanently predicts in the shadow band
therefore never throttled (up to 8640 rows/day/symbol). Instead of a
new cooldown, the branch now uses `core.signal_post.log_prediction()`, which
already deduplicates 4h per module/coin/direction — the same path used for bots 30-33.
The timer is **deliberately not** set here: it also gates real signals, and a
reset would suppress live EPD2 trades of the same coin for 900s.

### Live semantics
Deliberately changed: with 1 open + 1 closed AI signal in the window,
"Opened" now reports **2 instead of 3**, and a shadow prediction no longer
shows up as an opened signal at all. Closed counts, win rate, and Kelly math
remain untouched — `df_all_closed` still pulls exclusively from the
`closed_*` tables. Takes effect on the next regular restart, no deploy.

Known, not fixed here: `log_prediction` dedupes against `NOW()` (PG local time)
on UTC rows. This shifts the effective window but still throttles. Belongs in the
R3/TZ cluster (P2.1-P2.6) and must not be touched there via a point fix.

Verification: `backtest/test_market_tracker_opened.py` (new, 7/7) and
`backtest/test_shadow_prediction_cooldown.py` (new, 4/4), both standalone and
DB-free. The core test fails on the pre-fix state with 3L instead of 2L — it measures
the double counter instead of dying on an exception.
## [2026-07-10] Look-ahead in the walk-forward simulator closed (T-2026-CU-9050-037)

`tools/walkforward_sim.py` has been the **only label source of the entire
retrain program** since P0.10. Its two main loaders `load_ohlcv` (`:174`) and
`load_joined` (`:204`) read up to `NOW()` with no upper bound — the running candle
arrived as closed in the replay. Every model trained from this learned on a
candle it did not yet know at decision time (hard rule 5).
The sibling loaders `load_mis1_frame` (`:635`) and `load_rub_frame` (`:759`)
in the same file have always cut off correctly.

Fix:

- Both loaders now go through **`core.candles`** (`read_candles` /
  `read_candles_with_indicators`, `include_forming=False`) instead of raw
  f-string SQL. This makes the candle API's TF-generic epoch cutoff apply.
  Deliberately **not** copied the neighbours' `date_trunc('hour', NOW())`: the
  loaders also read `1d` and `4h`, where an hour trunc would have left the running
  candle in. Side effect: ASC contract and identifier hygiene (P3.3).
- `backtest/test_feature_lookahead.py` gets two DB-free tests that check for all
  used timeframes (1h/4h/1d) that the forming candle does not land in the
  replay frame. Mutation-tested: with `include_forming=True` they fail.

First step of block 1 of the rewiring order in
`docs/CANDLE_CALL_SITES.md` §4 (offline tooling first, `walkforward_sim` ahead of the rest).
No live signal path touched, no DB change.

**Open for Michi:** whether already-rolled-out models were trained on the old,
poisoned labels — and whether staging retrains therefore need to be re-evaluated.
This session trained nothing and rolled out nothing (C-gate).
## [2026-07-09] Significance layer over the walk-forward replay output (T-2026-CU-9050-027 D3)

A replay summary says "+38 R over 365d" — `tools/wf_significance.py` newly
answers the follow-up question of whether this edge is distinguishable from noise
before a candidate is discussed toward the live gate. Purely additive on top of the trade
JSONL from `tools/walkforward_sim.py`; pattern from HKUDS/Vibe-Trading (MIT,
`validation.py` + `bench_runner_strict.py`), adapted rather than copied:

- **Random control (sign-flip):** null distribution from direction flips of the SAME
  trades incl. fee drag (`flip(net) = -net - 2*fee_rt`) → p-value + delta against
  the directionless random trader, deliberately not a test against 0.
- **Order permutation for the MaxDD** (is loss clustering typical of randomness?).
  The vt permutation test on Sharpe was deliberately NOT adopted — for
  per-trade %-PnL, Sharpe is order-invariant, the test would be degenerate.
- **Bootstrap CIs** for per-trade Sharpe (deliberately not annualized), avg_r,
  TP1 WR.

Deterministic (seed 42). Verification DB-free: `backtest/test_wf_significance.py`
(6/6, incl. edge-vs-noise discrimination, fee drag in the null, CLI
determinism). Docs: `docs/WF_SIGNIFICANCE.md`. Open (VPS session): a run over
a real batch-E replay output — artifacts only live on the VPS.
Multiple testing (FDR/deflated Sharpe) deliberately remains out of scope (its own task).

---
## [2026-07-09] Look-ahead perturbation test over the shared feature builders (T-2026-CU-9050-027 D1, PR #19)

Hard rules 5 (only closed candles) and 7 (shared feature builders,
trainer == serving == replay) had so far only been secured by convention and ~69
DO-NOT/forming/look-ahead comments. New: `backtest/
test_feature_lookahead.py` (standalone, DB-free) makes them mechanically checkable —
pattern harvested from HKUDS/Vibe-Trading (MIT), `tests/factors/test_lookahead.py`.

- **Frame/as-of builders** (`mis.add_advanced_features[_multi]`, research
  candle context + PEX1/FMR1/FIF1 rows, `funding_features_asof`): all
  input columns from the perturbation row onward are poisoned with NaN/1e10 — the rows
  before must stay bit-close (1e-9) invariant. Canary assertions prove
  that the poisoning genuinely reaches the builder; a boundary test proves
  that a funding settlement exactly AT ts strictly stays out.
- **Window-/row-scoped builders** (`rub_trend`/`build_rub_features`,
  `build_trm1_row`, `funding_stats`, `regime_features`, `aim2.build_feature_row`):
  by signature without a future axis (caller slices) — checked are
  determinism, input non-mutation, and the internal window bounds (TRM1's 12-window,
  funding's 90-window).
- **`fetch_context_frame`** (R1 core, DB-free via a stub cursor): a forming
  candle of the current hour in the table changes neither the chosen
  feature candle (the floor-1 join) nor its features; the staleness guard (>3h)
  returns None.

**Result: no future leak found** — a valid no-op done. Detection power was
separately falsified (artificial `shift(-1)`/`iloc[idx+1]` leaks as well as two
mutation injections into real builders are caught). Known cosmetic
drive-by: `core/funding_features.py:70` throws a tz UserWarning (semantics
correct, UTC vs. UTC) — not fixed, a shared builder (rule 7).

---
## [2026-07-09] Central UTC policy laid down: `core/time.py` + ruff DTZ (T-2026-CU-9050-032, R3)

Kythera does not have one time source, but twenty. Writers write partly naive
server-local time, partly aware UTC, partly Postgres's `NOW()`; readers interpret
the same columns as UTC. The VPS runs on `Europe/Bucharest`, so that
drifts apart by +2/+3h — in cooldowns, trade windows, and burst counters, i.e. on the
money path. The audit's individual fixes never closed the cluster, because
each of them invented a new domain.

This entry lays down the policy, **without changing live semantics**:

- **`core/time.py`** — `utc_now()` (aware), `utc_now_naive()` for the legacy
  `TIMESTAMP WITHOUT TIME ZONE` columns, `to_utc()`, `as_naive_utc()`,
  `from_unix_ts()`. From now on the only sanctioned time source.
- **ruff rule group `DTZ`** (`pyproject.toml`). A new `datetime.now()` without
  `tz` now fails CI instead of silently opening another domain. The two
  deliberately naive existing files (`3_detectors`, `30_ai_pex1_bot`) carry a
  `# noqa: DTZ…` with a rationale — visible remaining debt instead of a silent exception.
- **`docs/UTC_POLICY.md`** — the column inventory, the existing set of drift
  compensations, the order of the remainder, and `docs/migrations/2026-07-r3-timestamptz.sql` as
  a prepared, **not executed** DDL.

Adjusted to the new time source: `15_ai_master_bot` (deprecated `utcnow()` →
`utc_now_naive()`, identical) and `core/market_utils.check_cooldown`
(hand-written normalizer → `to_utc()`, identical). Two spots change a
visible but inconsequential output: `2_indicator_engine` now writes the
state token and the scheduler log line in UTC — the token is an opaque
string comparison for `3_detectors`, and the minute trigger is invariant to
a full-hour-offset TZ; `check_funding` no longer renders its UTC epoch
as local time.

`backtest/test_time.py` pins the semantics of the new time source DB-free, including
a run under `TZ=Europe/Bucharest` set — exactly the error class
"runs locally, drifts on the VPS".

### Why the pool flip is NOT in this
Originally, `-c timezone=UTC` was supposed to go into the connection pool too. The
session TZ decides how Postgres casts between `timestamptz` and the naive columns —
the flip thus fixes P2.5 and P2.6, **but flips six spots that already correctly
compensate for the drift today**: `15_ai_master_bot.to_utc_naive()` and the
five dataset builders in `tools/` (`research_dataset_common`, `aim2_build_dataset`,
`fif1_build_dataset`, `pex1_build_dataset`, `retrain_sra2`). The trainers read
history; after the flip every naive column carries both domains, and neither "always
compensate" nor "never compensate" is correct. This is the train/serve skew
that AIM2 was built against (P0.13).

The flip therefore belongs in its own window, together with the P2.3 writer fix,
the six compensations, and the operator decision backfill-vs-cutover for
the history. `docs/UTC_POLICY.md` §4-§6 is the handoff for that.

---

## [2026-07-09] SMC-16 FVG entry was unreachable (T-2026-CU-9050-033, P1.26)

`find_unmitigated_fvgs` in `16_smc_forex_metals_bot.py` scanned for mitigation
via `range(fvg['index'] + 1, len(df))` — **including** the current candle
(`curr_idx = len(df) - 1`) — and discarded a BULLISH FVG as soon as `low <= top`.
The entry trigger subsequently checks exactly this predicate on the same
candle (`16:436`, symmetrically BEARISH via `high >= bottom` in `16:464`). Any
FVG that would have triggered the entry had thus by construction already dropped out of
`bull_fvgs`/`bear_fvgs`: the FVG entry could never fire in either direction. The
proof rests purely on the code — the FVG path writes exclusively the
literal `"SMC_FVG"` as its cooldown key (`16:437,465`, the only two
writers of this key), and 0 live rows exist for that (the 83 found
`SMC_1H_FVG`/`SMC_4H_FVG` rows come from an older, TF-prefixing
code version — the trap that the earlier refutation of this finding
foundered on).

The scan now ends before the current candle (`range(fvg['index'] + 1, curr_idx)`).
The current candle is the entry trigger, not the mitigator.

### Live semantics
The only behaviour change: FVG entries become possible. Candles **before** the
current one still mitigate unchanged, the FVG detection itself is untouched, and
the two trigger conditions (`price > bottom * 0.999` resp.
`price < top * 1.001`), cooldown, Cornix message, and chart stay as they
were. The BOS/CHoCH path is not affected.

### Verification
New guard test `backtest/test_smc_fvg_dead_code.py` (11 cases): a tap on the
current candle survives the scan (both directions), a tap on an earlier
candle still mitigates, the entry trigger as a whole is reachable, plus a
divergence canary that rebuilds the old `range()` and proves it
kills exactly the triggering FVGs — a revert of the fix turns the test red.
## [2026-07-09] MIS/RUB/QM post under the artifact's `model_id` instead of a source-code constant (T-2026-CU-9050-030, P1.45, PR #24)

Afterburner to the sniper fix from PR #16: the same error-class sweep found three
more post paths that load their artifact but discard `meta.model_id` and
post under a constant. **Today the tag happens to be correct in each case** —
so this was not an operational bug, but a live mine under the next
retrain rollout: MIS3/RUB3/QM2 would have silently landed under the old tag, would have
mixed with the predecessor generation in `ai_signals` and in the per-bot win rate,
and the orchestrator gating would have decided on the new generation's whitelist
based on the old one's performance (a violation of versioning rule 6).

### Fixed
- `11_ai_mis_bot.py` — **each of the eight horizon artifacts now carries its own
  generation from `meta.model_id`**; the winning candidate builds the posting tag
  (`f"{best_generation}-{best_horizon}"`). A partial rollout (72H already MIS3, the rest
  MIS2) thus tags every signal with the generation of the model that fired,
  and gets logged as a mixed generation on load. The file names
  `mis2_model_*.pkl` deliberately remain **generation-free slot names**
  (operator decision 2026-07-09) — precisely why `meta.model_id` is the only
  generation marker. If it is missing, `MODEL_GENERATION` acts as a fallback, but with
  `logger.error` instead of silently.
- `13_ai_rub_bot.py` — **the tag is now direction-dependent**: SHORT takes
  `RUB2_SHORT["tag"]` (= `meta.model_id`, always correctly computed by `load_artifact`
  and discarded until now), LONG keeps the named constant
  `RUB_LONG_TAG`. LONG runs the legacy model `long_reversion_model.joblib` with
  no meta at all and posts under `RUB2` per operator decision (2026-07-06) —
  wiring the SHORT artifact tag there would have labeled a signal with the generation
  of a model that never ran.
- `24_quasimodo_bot.py` — **preemptively, before QM2 exists**: the loader now prefers
  `meta.model_id` (today `qm_ml_trainer.py` writes none → the derived tag
  `QM_1H`, logged as such), and `send_cornix_signal` no longer derives the tag a
  second time from `tf`, but instead receives `module_tag` as a **mandatory keyword** —
  the sniper pattern: a call site that forgets it loudly fails with
  `TypeError`, instead of silently writing the old tag. The orchestrator has
  recognized `QM2_1H` since `ff8e01e` already.

### Fixed — transitional dedup (review finding, would have turned the tag fix into a money trap)
The posting tag **is simultaneously the dedupe key**. It flips on a generation switch —
and thereby a still-open position of the old generation would no longer have
blocked the same coin/direction: the new run would have opened a **second live position**
next to it. Exactly the trap PR #16 defused for the sniper via
`model IN (neuer Tag, Alt-Tag)`. Closed per bot at the spot that actually
locks there:

- `11_ai_mis_bot.py` / `24_quasimodo_bot.py` — the active-trade check extended to
  `model IN (%s, %s)`.
- `13_ai_rub_bot.py` — RUB has **no** active-trade check against `ai_signals`; its
  4h cooldown is the only re-fire lock. It now additionally checks against
  `RUB_LEGACY_TAG`. (The missing open-position check is a pre-existing state, not
  part of this task.)

`legacy_tag` in each case is **exactly the tag the bot would have posted before this fix** —
not an operator constant, not dead code. As long as the source-code constant and
the artifact generation match, both tags are identical and the clause is a
no-op.

Guard tests (static, DB-free — a runtime guard would be swallowed by the fleet-wide
broad `except` blocks, a lesson from T-2026-CU-9050-024):
`backtest/test_mis_tag.py`, `backtest/test_rub_tag.py`,
`backtest/test_quasimodo_tag.py`. All three are mutation-tested: reverting
one fix line each turns the corresponding test red. **No
live-semantics change** — with the deployed artifacts, the three tags still
read `MIS2-<Horizont>`, `RUB2`, `QM_1H` unchanged, and the dedup clauses are
equivalent in effect to the prior state when the tags are identical.

### Open (deliberately not in this PR)
- `retrain_from_replay.py:723` (EPD2) and `retrain_sra2.py:281` (SRA2) write
  dict artifacts **with** `model_id`, while the live bots `10_pump_dump_detector`
  and `9_ai_sr_bot` load **raw** models and read no meta — the
  retrain output format diverges from the live load format. When wiring up
  EPD2/SRA2, the tag must come from the new `model_id`, otherwise instances 4
  and 5 of the same error class arise. Remains as a P1.45 side finding in the ledger.
## [2026-07-09] Candle API `core/candles.py` + call-site inventory + parity tool (T-2026-CU-9050-034, C1 preparation)

Preparation for the R1/TimescaleDB migration (`docs/TIMESCALE_R1_MIGRATION.md`,
T-2026-CU-9050-018). **Purely new additions — no existing call site was
rewired, no dual-write, no backfill, no cutover, no
schema change.** The fleet runs unchanged.

New:

- **`core/candles.py`** — the central access API over the per-coin tables,
  through which all candle/indicator access is meant to run in phase 1. Four
  contracts: reads **always** return ASC (today ASC and
  DESC frames are mixed, `iloc[-1]` means something different depending on the file);
  `include_forming=False` is the default and switches R1 live bot by bot;
  writes **do not commit** (caller-commit contract like `core/signal_post.py`);
  symbol/timeframe are validated and quoted via `psycopg2.sql.Identifier`
  (P3.3, optional `coins.json` whitelist).
- **`docs/CANDLE_CALL_SITES.md`** — an inventory of every spot in the repo that
  touches a candle or indicator table, with today's forming-candle behaviour,
  the R1 blast radius, a proposed rewiring order, and the open
  operator questions.
- **`tools/candles_parity.py`** — parity comparison old vs. hypertable
  (row count, `max(open_time)`, OHLCV checksum) as a gate for migration phase
  3. The comparison core is DB-free and runnable via `--self-check` on the
  build machine; real runs need the VPS.
- **`backtest/test_candles.py`** — 29 DB-free tests.

The target schema's `is_closed` contract does not exist in the old tables.
Phase A derives it from the clock (`open_time < period_start(tf, now())`),
computed DB-side, via epoch arithmetic instead of `date_trunc()` — the latter depends
on the session timezone and would have cut differently depending on the bot process (R3).
For `1w` the cutoff is anchored to Monday; epoch 0 is a Thursday,
Binance weekly candles open Monday 00:00 UTC.

Open (operator, see `docs/CANDLE_CALL_SITES.md` §5): retention, `REAL` →
`double precision` (P3.12), 1d/1w streaming, close grace period. **R1 lowers the
signal rates — that is the purpose. Re-tune thresholds only after the retrain.**
## [2026-07-09] HTTP hardening of the Binance REST paths (T-2026-CU-9050-027 D2, P2.14 + P2.18)

New `core/http_retry.py` (pure policy without I/O, injectable clock/sleep →
DB-/network-free testable): `RetryBudget` (max_attempts AND a wall-clock deadline),
`backoff_seconds` (429 with Retry-After respect, 418 never below 120s and
exponential — a Retry-After header may only increase the ban wait time),
`MinIntervalThrottle` (minimum spacing + jitter per host bucket). Pattern following
HKUDS/Vibe-Trading `loaders/_http.py`/`retry_with_budget` (MIT), not a drop-in.

- **P2.14 (`1_data_ingestion.fetch_ohlcv_batch`):** the `while True` loop
  could loop forever on a stuck symbol and hammered into a ban on 418 with
  Retry-After+2s. Now: budgeted retry (8 attempts/300s per
  symbol×TF batch, only FAILED attempts count — success pages paginate freely),
  418 backoff ≥120s exponential. On an exhausted budget, the already
  fetched partial data is used; the next 12h run resumes at MAX(open_time).
- **P2.18 (`6_housekeeping._fetch_klines_from_binance`):** the gap filler had
  no 429/418 handling at all (`raise_for_status` → None) and could pull an
  418 IP ban in a burst across ~9k tables, which also hits the
  trading endpoints. Now: 429 → Retry-After-aware budgeted backoff; 418 →
  a process-wide ban window (all further gap-fill calls return None immediately
  until it expires, instead of hammering on); the next nightly run catches
  the gaps up); throttle 0.25s/request against the burst.

Live semantics: success paths unchanged (same URLs, same parse paths);
all deltas lie on error paths that previously retried endlessly or got banned.
Takes effect on the next regular restart, no deploy. Verification:
`backtest/test_http_retry.py` (7/7, standalone), ruff+mypy green on all three
files. The freshness fallback (`run_freshness_job`) keeps its own,
already capped rate-limit handling — deliberately not touched (limit=2 calls,
weight harmless).

---

## [2026-07-09] Market tracker returns pool connections on the error path (T-2026-CU-9050-029, P1.43, PR #18)

`23_market_tracker.py` acquired the connection bare in two places and called
`conn.close()` as the **last statement in the try body** — on a raising query,
control jumped straight into `except: log; return`, `close()` never ran, the
pool slot was gone. The pool caps at 8 connections per process, so
~8 DB hiccups permanently drain the tracker: the process stays
"healthy" under the watchdog and silently stops posting anything. The cause is the
acquire/release form, not the queries.

Both spots now use `with get_db_connection() as conn:` — the form the
five remaining `job_*` functions in the same file already had.

### Fixed on the same fault line
- **The `ai_signals` fallback ran inside the aborted transaction.** Postgres
  aborts the entire transaction on a failed statement; the
  fallback would have died with `InFailedSqlTransaction` — it thus never
  actually fell back. A `rollback()` was added before it.
- **`_get_regime_fit_label` poisoned the shared connection.** The function
  swallows its exception and returns `---`, but the caller shares ONE
  connection across ~25 bots. Without `rollback`, the transaction stayed
  aborted, and the first failed lookup degraded the regime-fit column for
  **all subsequent** bots to `---`.
- **The Kelly/regime-fit loop** indexes into the Kelly dict; a `KeyError`
  skipped `_regime_conn.close()`. Now wrapped in `try/finally`.

### Live semantics
On the success path nothing changes: the connection is released at the identical
point (after the last read, before the pandas processing), with the same
`rollback()` + `putconn()`. All deltas lie on paths that previously lost a
pool slot or died on `InFailedSqlTransaction`. Takes effect on the next
regular restart, no deploy.

Verification: `backtest/test_market_tracker_conn.py` (new, standalone, DB-free,
7/7) — the 4 bug tests demonstrably fail on the pre-fix state, the 3
control tests run green on both states.

---

## [2026-07-09] Ledger made true — control documents verified against the code (T-2026-CU-9050-028)

No code fix. The two control documents (`docs/OPUS-HANDOFF.md`,
`docs/T-2026-CU-9050-021-opus-task-audit.md`) were at the 07-07 state and
did not know about the work from 07-08/07-09 — anyone reading them as a backlog
would prioritize on outdated grounds.

### Verified instead of flipped
- **P1.26 stays open — the annotation was wrong.** It had marked the finding
  as refuted ("83 SMC_*_FVG cooldown rows, the path fires"). Against the code: the
  mitigation scan in `16_smc_forex_metals_bot.py:164` runs
  `range(fvg['index']+1, len(df))`, i.e. **including** `curr_idx = len(df)-1`,
  and marks BULLISH as mitigated at `low[j] <= fvg['top']`. The trigger
  (`:430`) checks the same predicate on the same candle. An FVG that would
  trigger the entry is thereby by construction already removed from `bull_fvgs` →
  **the FVG entry can never fire.** Resolution of the evidence contradiction: the
  current code repo-wide only writes the literal key `"SMC_FVG"`
  (`:431,459`); the 83 found rows are called `SMC_1H_FVG` etc. and come
  from an older, TF-prefixing version. The dead-code proof rests purely
  on the code and needs no DB.
- Flipped after re-checking: **P1.5** (the column is INTEGER, plus a
  defensive cast in `8_ai_trade_monitor.py:216-219`), **P1.11** (the buffer key has
  long been `(sym, tf, open_time)`, `1_data_ingestion.py:662` — it had been wrongly
  listed as an A2 item), **P1.18** (feature selection is name-based,
  `11_ai_mis_bot.py:245`; the fix only takes effect on the next bot restart),
  **P2.50** (the guard is armed, 24 goldens + 24 fixtures since `4765e25`, `verify`
  as a pre-commit hook).
- **P2.2 stays open:** the TZ dimension is resolved, the column width is
  not. `CREATE TABLE IF NOT EXISTS` never widens, the drift cements
  itself. Noted as an **indication** of origin (not proof): the only place
  in the repo with `module VARCHAR(10)` is a commented-out legacy DDL block in
  `legacy_trainers/zzz.py:13443`; the executing DDL is not in the repo. The
  clean fix is a live `ALTER` (operator decision).

### Error-class sweep from PR #14 and #16 (the actual value)
- *Silent signal death via column overflow:* **no second active instance.**
  All 18 `trade_cooldowns.module` writers resolved down to the tag value;
  the longest tag is 9 characters (`MAYANK_4H`, `MIS2-168H`), all distinct, no
  truncation collision. Residual risk noted as **P3.13** (the tag-length test only covers
  Mayank; the `COOLDOWN_MODULE_MAX_LEN` guard raises `ValueError` and
  would be swallowed by the same broad `except` blocks — the load-bearing
  safeguard is the DB-free static test).
- *Post path ignores the artifact's `model_id`:* **no second actively wrongly
  firing instance, but three latent ones** → new finding **P1.45**.
  `11_ai_mis_bot.py` (constant `MODEL_GENERATION="MIS2"`, plus hardcoded
  `mis2_*.pkl` file names), `13_ai_rub_bot.py` (`load_artifact` computes the
  tag correctly, the bot discards it), and `24_quasimodo_bot.py` (a structural
  twin of the sniper: a derived `f"QM_{tf}"` can never match a QM2 — and
  the orchestrator has already been QM2-capable since `ff8e01e`). Today the tags
  happen to match; **at the next retrain rollout the generations silently merge**
  in the per-bot statistics on which the orchestrator gating decides.
  → blocks MIS3/RUB3/QM2, scheduled as **A2b** before B7/C2.

### Changed
- `AUDIT_TODO.md` — five checkboxes corrected, A2 items annotated with code
  evidence from 07-09, new findings **P1.45**, **P2.51**, **P3.13**.
- `docs/T-2026-CU-9050-021-opus-task-audit.md` — as of 07-09; tasks 022-026 +
  PR #12 added retroactively; **A1 done**; **A2 trimmed down to the verified
  remainder** (five instead of six items — PRs #13/#15 did not resolve any of
  that, their dedup only affects the closed tables); **A2b** new;
  **B5 struck** (the guard had long been live); **B7 trimmed by MIS1** (adapter
  `run_mis1` exists, only the execution is outstanding).
- `docs/OPUS-HANDOFF.md` — as of 07-09; cycle step 0 (`git fetch` before
  prioritizing); trap 13 sharpened (annotations themselves can be wrong —
  verify against the code); new traps 15 (stale checkout) and 16 (model tag comes
  from the artifact, never from a constant); guard status corrected.

### Side finding
- **P2.51** (new): `tools/regression_guard/guard.py:132-137` — `mode_verify`
  returns "NOT ARMED … Pass" and exit 0 on missing goldens, without checking whether
  `manifest.json` exists. Whoever deletes `golden/` or loses it in a merge
  disarms the guard unnoticed; the pre-commit hook stays green. The reverse
  case (goldens without fixtures) is correctly handled with exit 1.

### KB
- `T-2026-CU-9050-016` (batch E) corrected from `open` to `done`: all
  criteria named in the task (P0.10-P0.13, P1.29-P1.31, P1.35) are delivered and
  backed by report-19 numbers. QM/ATS1/ATB1/SRA1 were never done criteria of this
  task, but the VPS follow-up scope mapped as B7.

---

## [2026-07-09] PR #16 — SMC sniper: retrain trades posted under the old tag (T-2026-CU-9050-026)

Trigger: operator impression "the SMC no longer posts any trades". Finding: it
does trade — just invisibly.

### Fixed
- `25_smc_ml_sniper.py` — **`send_cornix_signal` now passes through the
  artifact's `model_id` instead of recomputing the tag as `{strategy}_{tf}`.**
  `evaluate_and_trade` correctly used `BB2_4H`/`TD2_4H`
  (cooldowns, ml_predictions), but the signal/trade write ran under
  `BB_4H`/`TD_4H` — the retrain generation was merged with the old
  generation in ai_signals and all downstream stats (per-bot post, A-Z post,
  regime analyzer) (rule-6 violation). Evidence: 97 of the 115
  open `BB_4H` rows carry confidence ≥ 0.63 (= the BB2 threshold), 88
  closes since the BB2 deploy on 06.07. Operator decision: fix it, NO
  rewrite of the mistagged old rows (would be a live write).
  Guard test: `backtest/test_sniper_tag.py`.
- `28_signal_orchestrator.py` — **`BOT_IDENTIFICATION_PATTERNS` made
  generation-open** (a review finding that would have sabotaged the tag fix):
  the patterns only matched `BB_`/`TD_` and the literal list only
  `RUB1/ABR1/...` — a `BB2_4H` signal would have been HARD
  suppressed as `bot_unidentified`, instead of (as intended) passing default-open through the
  whitelist. Now `BB\d*_`, `TD\d*_`, `QM\d*_`, and
  `(MIS|ATS|RUB|ATB|AIM|ABR|EPD|SRA)\d+` — this simultaneously closes the
  open RUB2 attribution finding from PR #9 (RUB2 has been posting since 07.07.
  live and depended on the `🧠 …Strategy` footer fallback). Only with this fix does
  it hold that: a new tag starts in the regime whitelist without history
  (default-open) — deliberately accepted.
- `25_smc_ml_sniper.py` — **transitional dedup**: the active-trade check
  now checks `model IN (neuer Tag, Alt-Tag)` — the ~115 open, mistagged
  rows still block re-fires on the same coin/direction
  (otherwise a second live position next to the old one). `module_tag` is now
  a mandatory keyword parameter (a forgotten tag → a loud TypeError instead of
  a silent old tag). Orchestrator tests extended for generation tags.

### Side findings (no code change needed)
- `16_smc_forex_metals_bot.py` (SMC_15M/30M/4H in the A-Z post) is by design
  info-only — the code in this repo never had an ai_signals path; the
  February trades come from a legacy script. If the bot should provide tracked
  trades again, that is a separate task (operator decision).
- Mayank posts info signals without position tracking (the refire bug already
  fixed in PR #14).
## [2026-07-09] PR #15 — market-tracker dedup key v2: report-14 key, all-time/Kelly now genuinely clean (T-2026-CU-9050-025)

### Fixed
- `23_market_tracker.py` — **dedup key switched from (…, entry, close_price,
  open_time, close_time) to `(symbol/coin, strategy, direction, open_time)`**
  — the unique-index key that report 14 recommends.
  Live measurement after the PR-#13 deploy: 439.325 raw AI rows → the old
  key only collapsed to 360.682, the report-14 key shows
  **81.842 real trades**. Reason: the ~357k migration/LEGACY duplicates
  (Feb 2026: 372.794 → 15.339) are re-closes of the SAME trade with a different
  close_time/close_price — the old key saw them as different
  trades. All-time WR and Kelly were thus still distorted; the short
  windows (1h-7d) and the regime analyzer (30d) were clean (0 duplicates in
  the last 30 days; outside Feb/March 2026 the key is unique in
  normal operation, raw == distinct in every month). Survivor per
  group: the earliest close (the original outcome; the re-close artifact came
  later), then the highest targets_hit. Both jobs, both tables (classic:
  ~11k duplicates under the same key — all verified with identical entries,
  no legitimate ladder trades affected).
- `23_market_tracker.py` — **unified query structure after review**: dedup
  now runs FIRST across the full table in all four queries, window and
  price-validity filters (`entry/close_price > 0`, now also in the
  summary job) sit outside it. This means the survivor choice no longer depends on the
  filter, and a future re-close event cannot flush months-old trades into the
  24h window as "freshly closed". Key/sort order now live in
  module constants (`AI_DEDUP_KEY` etc.) instead of four copies. Live
  verified: identical result set (81.837 groups) for both
  structures against the current data.

### Deliberately NOT changed
- `tools/track_shadow_model.py` keeps its narrower natural key — it is
  applied to fresh tags (EPD2 etc.), where no migration duplicates
  exist; functionally identical.
- The unique index itself + purging the duplicate rows remains a DB migration →
  operator decision (report 14 recommendation #1).
## [2026-07-09] PR #14 — cooldown tags blow past varchar(10): volume indicator signal-dead, Mayank refire (T-2026-CU-9050-024)

`trade_cooldowns.module` is `character varying(10)` on the live DB (verified via
`information_schema`). The repo DDLs say VARCHAR(50)/TEXT — the
live table is older, `CREATE TABLE IF NOT EXISTS` never widens
(DDL drift, P2.2 extended). Two writers used longer tags:

### Fixed
- `strategies/strat_volume_indicator.py` — **`module_tag` shortened from `'Volume
  Indicator'` (16 characters) to `'VolIndic'` (8).** The P1.16 fix
  (2026-07-04) therefore threw `StringDataRightTruncation` on EVERY signal
  attempt — before the `return` of the signal dict. Consequence: the
  volume indicator posted **zero signals** from 04.07. to 09.07., and
  because `analyze_fast` runs in the same per-coin try and `write_signal_atomic`
  comes only afterward, in cycles with a simultaneous
  fast-in-and-out signal **that signal was also lost** (collateral damage
  from the P1.15 isolation; `check_cooldown` never found a row → every 30m cycle
  crashed again). Discovered in the watchdog log during the PR-#13 deploy.
  Operator decision: fix it, the bot posts again. No row migration needed —
  no write with the long tag ever got through.
- `strategies/strat_volume_indicator.py` + `3_detectors.py` — **the cooldown
  moves into `write_signal_atomic`**: the strategy no longer writes it
  itself, but requests the cooldown via `signal['cooldown_module']`;
  the detector writes it in the SAME transaction as
  active_trades_master + outbox (rule 8: transactions are committed by the
  caller). A self-commit in the strategy would have persisted the 12h lock even on a
  failed signal write; a `commit=False` in the strategy was
  also not atomic (review finding round 2: the commit of an
  EARLIER signal in the same per-coin cycle — e.g. fast in and out —
  would have taken the pending cooldown along with it).
- `17_mayank_bot.py` — **same bug class, worse effect:**
  `module_tag = f"MAYANK_{symbol}_{tf}"` (≥14 characters) threw AFTER the
  outbox insert → the cooldown never persisted → **the same FVG setup was
  reposted every scan round** for as long as the setup persisted. New tag
  `f"MAYANK_{tf}"` (≤10); the symbol is already in the `coin` key column anyway,
  the (module, coin, direction) uniqueness stays identical.

### Added
- `core/market_utils.py` — **length guard `COOLDOWN_MODULE_MAX_LEN = 10`** in
  `check_cooldown`/`update_cooldown`: an oversized tag now immediately throws a
  speaking `ValueError` in EVERY environment (dev/staging DBs from the
  repo DDLs would never have reproduced the live error, CI would have stayed green).
- `25_smc_ml_sniper.py` — **load fallback for an artifact `model_id` > 10
  characters**: an oversized tag from the pkl meta would throw the new guard on
  EVERY evaluation (the per-symbol except silently swallows it → the bot posts
  nothing). Now: a loud `logger.error` + fallback onto the static
  `{strategy}_{tf}` tag. Current artifacts (BB2/TD2) fit.
- `backtest/test_cooldown_tags.py` — a DB-free standalone test: guard throws,
  VolIndic/Mayank tags fit, the VolIndic cooldown runs atomically via
  `write_signal_atomic` (no strategy self-write), a fleet-wide scan for
  oversized literal tags (root + strategies/ + core/).

### Follow-up
- AUDIT_TODO: P1.16 extended with a regression annotation (incl. the FIO
  collateral), P2.2 extended with the width-drift dimension. Recommendation to the
  operator: `ALTER TABLE trade_cooldowns ALTER COLUMN module TYPE VARCHAR(50)`
  at the next opportunity (live schema change → escalation, T-2026-CU-9050-018).
## [2026-07-09] PR #13 — market tracker: per-bot WR correctness + compact A-Z model post (T-2026-CU-9050-023)

Trigger: an operator question about whether the per-bot success rates in the
sentiment tracker channel are correct. Answer: the classification logic
(PnL-based, neutrals excluded) was clean, but three data problems distorted the numbers.

### Fixed
- `23_market_tracker.py` — **dedupe on the natural key, server-side
  via `SELECT DISTINCT ON` in both jobs** (`job_signal_summary` +
  `job_per_bot_performance`). `closed_ai_signals` has no unique index and
  carries ~357k duplicate rows from migration/LEGACY re-close (report 14) — n,
  all-time WR, and Kelly were inflated, and the duplicates had so far been
  transferred to the client side in full, hourly. The `ORDER BY` tiebreaker
  (`targets_hit DESC`/`status DESC`) makes the surviving row deterministic
  (duplicates differ exactly in status/targets_hit). Same
  key as `tools/track_shadow_model.py`.
- `23_market_tracker.py` — **`close_price=0` rows (v1 era, pre-2026-03) are
  removed from the WR.** The PnL formula scored such SHORTs as +100% wins and LONGs
  as −100% losses — both within the 100% outlier bound, so they flowed in.
  Per-bot job: SQL filter `entry > 0 AND close_price > 0`. Summary job: rows
  with a present but unusable price are now NEUTRAL instead of running into the
  status/targets fallback (which would have revived the known LEGACY
  `targets_hit=0` writer bug that the PnL path is meant to bypass).
- `23_market_tracker.py` — **direction case normalized** (`upper(btrim(...))`
  in the dedup key and in the select list; pandas normalization as
  belt-and-braces for the open frames). Historical lowercase `short` rows
  previously got the LONG sign in the PnL and dropped out of the
  LONG/SHORT splits.

### Added
- `23_market_tracker.py` — **new compact "MODELS A-Z" post** in the
  sentiment tracker channel: one line per model (24h/7d/all WR, avg PnL,
  decided n), sorted alphanumerically — model generations (ABR1/ABR2,
  RUB1/RUB2, MIS1/MIS2, …) sit directly beneath one another. Sent between
  the main table and the Kelly block; chunking via the existing `_build_chunks`
  (new `separator` parameter instead of a copy-paste helper).

### Verified
- ruff + `ruff format --check` + mypy green (CI 6/6).
- Offline smoke runs of both jobs with a mocked DB: natural-key dedupe
  (duplicates with differing status collapse), lowercase direction
  correctly scored, a `DELISTED`-only bot shows n=0, a LEGACY `close=0` row
  neutral instead of loss, A-Z sort order + send order table→compact→Kelly.
- A DB-bound follow-up check (plausibility against
  `tools/track_shadow_model.py`) belongs in a VPS session after deploy.

### Deliberately NOT changed
- No unique index/purge on `closed_ai_signals` — a DB migration on
  live tables is an operator decision (report 14 recommendation #1,
  T-2026-CU-9050-018).
- P1.44 (opened counts double AI trades + count shadow predictions) remains
  open — a separate finding, not part of this fix.
## [2026-07-07 evening] PR #10 — review fixes for the PR-#9 findings (correctness)

### Fixed
- `core/model_artifacts.py` — **`maybe_reload` no longer discards a loaded artifact
  on a failed reload.** Previously, the daily reload unconditionally replaced the
  in-memory model with the result of `load_artifact`;
  a transient error (file lock during an operator copy, an AV scan, a half-
  written deploy) would thus mute a live side until the next
  24h window (RUB2-SHORT: `if not RUB2_SHORT["loaded"]: continue`, no
  legacy fallback). New: if the reload fails AND the file still exists,
  the loaded artifact stays active (`loaded_at` still advances →
  no retry per tick). Only if the file is GONE (an operator undeploy) is
  the not-loaded state adopted. Behaviour test verified inline.
- `10_pump_dump_detector.py` — **`ticker_10s` timestamp floored to the 10s
  mark.** The new `UNIQUE(symbol, ts)` index could not actually prevent the
  motivating dual-writer class (a duplicate detector start), because every
  process stamped a raw `datetime.now(utc)` per tick → two instances
  produced `ts` values with µs jitter, `ON CONFLICT DO NOTHING` never triggered. Now
  identical, grid-aligned `ts` per 10s window → dedup works.
- `core/ticker_10s.py` — **one-off migration (dedup DELETE + `CREATE UNIQUE
  INDEX`) commits immediately in its own transaction**, before the idempotent
  compression/retention-policy statements. Otherwise a later
  policy error would have rolled back dedup + index along with it, and the
  expensive full-table DELETE would have run again on EVERY start — after
  `COMPRESS_AFTER` against compressed chunks, where DELETE/`CREATE UNIQUE INDEX`
  are restricted.
- `tools/retrain_from_replay.py` — **`load_replay` now fails loudly on `null`
  features or a `null` `net_pnl_pct`, instead of silently defaulting to 0.0/`{}`.**
  Such rows are replay-writer bugs; as 0.0-PnL rows they diluted
  the validation economy on which `pick_threshold_safe` chooses the
  LIVE gate threshold (a deployable-looking artifact on corrupted economics).
- `13_ai_rub_bot.py` — **`RUB2_SHORT` init switched to the full `load_artifact`
  contract shape** (instead of a partial dict without `threshold`/`features`/`loaded_at`):
  defuses KeyError traps before `load_models()` and forces
  the first reload load via `loaded_at=0.0`.
- `core/config.py` — **`_ch` treats an empty/whitespace value as unset**
  (→ 0) instead of crashing on `int("")`. A templated `.env` line such as
  `CH_MAIN=` would otherwise have torn down every bot at import time
  (audit_reports/01_core_infra.md LOW).

### Verified
- ruff (CI set) clean, mypy 65 files clean, regression guard `verify` OK,
  standalone suite 149 passed (the 3 red tests — `test_bot_naming`,
  `test_bot_regime_analyzer`, `test_signal_orchestrator::…rom1…` — are
  pre-existing on `main`, not a PR-#10 regression).

### Open follow-ups (documented, not merge-blocking)
- **`backtest/backfill_regime_history.py`** still calls `classify_regime` without
  `prev_regime` → enter-only semantics ≠ the live detector (hysteresis). On a
  re-run, `regime_history` mixes two classifier semantics. Fix: thread a
  rolling `prev_regime` through the loop like in the detector.
- **`tools/regime_rules_study.py`** does not model the deployed hysteresis in
  the vectorized `classify()` → future grid runs evaluate a no-hysteresis
  variant.
- **Bots 25/18** (`25_smc_ml_sniper.py`, `18_ai_abr1_bot.py`) still load artifacts
  by hand without a feature-contract check/reload; bot 25 `exit(1)` instead of
  idling on a missing artifact. Candidate for `core/model_artifacts.load_artifact`.
- **The RUB2 feature contract** is composed separately in bot 13
  (`RUB_FEATURES + FUNDING_FEATURES`) and the trainer (`RUB2_FEATURES`) — a shared
  constant in `core` (like `PEX1_FEATURES` in `core/research_features.py`) would be the
  one source (rule 7). Divergence currently fails loudly via `load_artifact`, not
  silently, hence a follow-up.
- **`13_ai_rub_bot.py` `since=now-95d`** duplicates the `rates[-270:]` window of
  `funding_features_asof` as a magic constant (currently covers it; should be coupled via
  a shared constant).
## [2026-07-07 midday] Detector rework §22 LIVE — mid-vola trend rule with hysteresis

### Changed
- `core/regime_logic.py` — **mid-band trend rule V2 K=1.5 + hysteresis**
  (operator pick from `tools/regime_rules_study.py`, 7 variants over 430d):
  in the band P40..P75, |ret_4h| ≥ 1.5×ATR_4h% now implies TREND_UP/DOWN; an existing
  TREND holds until |ret_4h| < 1.0×ATR (`prev_regime` param, fed from
  `regime_current`); TREND targets now need 3 instead of 2 debounce checks.
  Old: TREND was structurally dead (3 episodes in 430d, all <1h, because
  ATR<P40 ∧ |ret|>1.5% almost mutually exclude each other); TRANSITION was a 41%
  residual class. New (validated, stateful with the real classify function):
  TREND_UP/DOWN each ~10% of the time (median 1.5h, flaps 21-25%), TRANSITION
  20.8%. Economics check: RUB-LONG in TREND_UP +1.65%/trade (n=1,378),
  9/13 months positive (negative only Oct/Nov 25 + Jan 26 — deep bear months).
- `26_regime_detector.py` — reads the effective regime before
  classification and passes it through as `prev_regime` (hysteresis).
- Tests: `backtest/test_regime_detector.py` +7 (mid-band, hysteresis
  both directions, HIGH_VOLA precedence, TREND debounce-3) — 27 passed.
- Deploy safety checked: missing whitelist cells for the new
  TREND states default to open (no mass auto-close); cells now start
  collecting evidence. Follow-up: §23 analyzer rework (shrinkage instead of
  default-open), then possibly an explicit TREND_UP gate for RUB-LONG (§8).

## [2026-07-07 midday] New-ideas cohort trained — FIF1 deployed, detector study started

### Added
- **All 4 new-ideas datasets built + trained** (results table in
  `docs/NEW_IDEAS_BOTS.md`): PEX1 without selection value (AUC~0.55,
  degenerate threshold), FMR1 with no foundation (val AUC 0.498 = random),
  TRM1 blocked upstream (classes 0/5/1589 — the detector never holds TREND,
  step-6 finding; re-submit after the detector rework), **FIF1 the only
  candidate** (val OP +0.044%/trade thin; test gate −0.08→+0.331%/trade,
  WR 75.3%, n=893/18,011).
- **FIF1 DEPLOYED** (operator 2026-07-07): `fif1_model.pkl` (thr 0.67) in the
  repo root, bot 33 recycled — posts LIVE in CH_NEW_IDEAS
  (`NEW_IDEAS_LIVE_POSTING=1`, the AIM2 validation pattern). Review in 4-6 weeks.
- `tools/regime_rules_study.py` — **detector-rework step 1 (MODEL_INTENT
  §22)**: a rule-variant replay over the full BTC-15m history. Current rule
  V0 vs. mid-band trend rule with a fixed threshold (V1, grid 1.5/2.0/2.5%)
  vs. vol-scaled |ret_4h| ≥ K×ATR (V2, grid 0.75/1.0/1.5); evaluated
  via episode statistics (does TREND occur? does it flap?) AND an economics overlay
  (avg PnL of the RUB-LONG/ABR1-LONG replay events per regime state — the
  regime-gate use case from §8). Debounce approximation 2 bars; read-only.
## [2026-07-07] RUB2-SHORT deployed — bot 13 onto the artifact contract

### Added
- `13_ai_rub_bot.py` — **SHORT now runs on the RUB2 artifact** (`rub2_model_SHORT.pkl`,
  an explicit copy from staging_models, P1.35): contract like bot 25
  (model/features/optimal_threshold from the pkl dict), a 15-feature contract
  (9 rub + 6 funding as-of from `funding_rates` via `core/funding_features`,
  lazy per event), missing funding history ⇒ 0 like `fillna(0)` in the trainer
  (serving parity), threshold 0.829 on raw predict_proba (safe-picker
  semantics). Falls back to the legacy model @0.85 if the artifact is missing.
  LONG unchanged, legacy @0.75 (RUB2-LONG not deployable — the val curve is
  consistently negative; details in MODEL_INTENT §8).
- Scheduled task **"Kythera Funding Backfill"** (hourly, :35, as user) →
  `Documents\kythera_funding_backfill.bat` calls `tools/backfill_funding_rates.py`
  incrementally — keeps `funding_rates` fresh for RUB2 serving (the table had
  no live writer; state before the fix: 18h stale).
- Scheduled task **"Kythera Fleet Autostart"** (ONSTART +2 min, SYSTEM) →
  `Documents\start_kythera_fleet.bat` — a consequence of the VPS outage on
  2026-07-07 (~04:42-08:18, provider-side): nothing restarted the fleet.

### Fixed
- `tools/pex1_build_dataset.py` `spike_time_to_utc` — **DST mixed-offset bug**
  (hit both the PEX1 AND EPD2 builders): `pd.to_datetime(errors="coerce")` without
  `utc=True` locks the offset of the first row for timestamptz series;
  all rows with a different offset (after the EET→EEST switch on 2026-03-29)
  were coerced to NaT and discarded by the `dropna` — the first EPD2 run thus
  lost ALL events after 29.03. (38,974 instead of the expected ~3× as many;
  a span of 32 instead of 132 days). Awareness is now checked on the raw value and
  aware series are parsed with `utc=True`. Dataset rebuilt.
- `tools/retrain_from_replay.py` `run_epd` — a guard against degenerate
  chrono splits (an empty val slice ⇒ an `iso.fit` crash on a truncated
  first dataset); also `--strategy epd` NEW: the EPD2 trainer
  (16-feature contract = 10 bot-10 live features + 6 funding, its own loader
  for the builder schema ts/label/features, 7d purge, safe threshold,
  artifacts `staging_models/epd2_model_{LONG,SHORT}.pkl`).

### Context (retrain results, morning of 2026-07-07)
- The RUB replay 365d/530 coins done (resumed after the VPS outage from
  coin 433); `retrain_from_replay.py --strategy rub --days 365`: **SHORT
  deployable** @0.829 (test 680/4,725, WR 81.9% vs. baseline 79.1%, +0.64%/trade net),
  **LONG not deployable** (all val thresholds −0.9…−1.2%/trade).
  A monthly split of the replay supports the operator thesis of regime dependence:
  LONG unfiltered is clearly positive in old bull months (Aug/Sep 25:
  +3.9/+2.4%/trade; Apr 26: +3.0), disastrous in bear months (Oct/Nov 25:
  −3.6/−4.8; Jan 26: −3.4) → LONG needs a REGIME gate, not an
  event-ranking gate (linked to T-2026-CU-9050-020, the HMM study).
## [2026-07-06 night] Replay adapter for RUB2 and EPD2 retrain

### Added
- `tools/walkforward_sim.py --strategy rub` — **RUB adapter**: replays the rubberband pre-filter per closed 1h candle (95d regression as-of, 4h cooldown per direction like live). Detection/feature logic lifted into `core/rub_features.py` — **ONE source for bot 13 AND replay** (bot refactored, X-R1); geometry as-of via `get_hvn_and_sr_levels(df=…)` (new df param, the P0.10 pattern) + `hvn_sr_trade_geometry` (new in core/trade_utils — canonicalized bot-10/13 geometry). The feature dict includes the 6 funding features.
- `tools/epd2_build_dataset.py` — **EPD2 adapter**: EPD is 10s-tick based, the detector logs (`pump_dump_events`, 241k rows since 2025-12) ARE the events. Mirrors bot-10 semantics (vol_ratio≥5 both sides, direction = ride along, 900s dedupe, post-spike entry, HVN/SR geometry as-of), label via `simulate_exit` (skip-entry-hour, 7d); uses the exact event-time indicators where present (~30% of rows), otherwise a 1h join; + funding features. Smoke: 364 events/5 coins, both directions, 0 fails.

### Fixed
- `tools/pex1_build_dataset.py` — TZ crash: `spike_time` is `timestamptz` (aware UTC), the offset heuristic expected naive local time → `detect_offset_h`/`spike_time_to_utc` now correctly handle aware timestamps (would also have crashed the PEX1 run).
- `tools/backfill_funding_rates.py` — **head check in resume**: resuming only from MAX(funding_time) was blind to missing older history (BTC/ETH/BCH only had 30d after the 30d smoke test; the full run never fetched the head). A missing head is now detected and backfilled (idempotent); the 3 coins are refilled.

## [2026-07-06] Research bots 30-33: PEX1 / FMR1 / TRM1 / FIF1 (report 15 — S6/S8/S10/S11)

### Added
- **Four new ML bots** as a cohort in the shared channel `CH_NEW_IDEAS` (attribution per model tag; `NEW_IDEAS_LIVE_POSTING=0` → shadow-only). Without deployed artifacts, all four run in idle mode. Design + VPS runbook: `docs/NEW_IDEAS_BOTS.md`.
  - `30_ai_pex1_bot.py` — **PEX1** pump-exhaustion short (S6): consumes `pump_dump_events` (vol_ratio ≥ 5 mirrored live as in training, pumps only), short-only, smart-target geometry.
  - `31_ai_fmr1_bot.py` — **FMR1** funding-extreme mean reversion (S8): cross-section from one `premiumIndex` request, percentile extremes (≥95% SHORT / ≤5% LONG), history live via REST — independent of the backfill cron.
  - `32_ai_trm1_bot.py` — **TRM1** transition resolution (S10): a 3-class model over `regime_history` features, posts BTCUSDT trades in the predicted resolution direction (only on a debounced TRANSITION).
  - `33_ai_fif1_bot.py` — **FIF1** FIFO filter (S11): a standalone A/B over the fast-in-and-out stream (10-min time window + content-key dedupe across active+closed — catches fast resolvers, prevents idle catch-up backlogs), posts gate passers with ORIGINAL geometry; every candidate is logged as a shadow row.
- Shared building blocks (one source for bot/builder/trainer, the X-R1 rule): `core/research_features.py` (scale-free feature contracts), `core/model_artifacts.py` (artifact loader + idle mode), `core/signal_post.py` (atomic outbox+ai_signals posting, no Cornix block in the info message).
- Training pipeline for the VPS (step 2): `tools/pex1|fmr1|trm1|fif1_build_dataset.py` (labels exclusively via `simulate_exit`, floor-1 join, live gates mirrored) + `tools/new_models_train.py --strategy <s>` (batch-E methodology: chrono split with purge, isotonic on val, threshold per replay PnL, artifact ONLY to staging — P1.35).
- Registration: `main_watchdog.py` (start_delay 191-215), `core/config.py` `CH_NEW_IDEAS`, `.env.example` (`CH_NEW_IDEAS`, `NEW_IDEAS_LIVE_POSTING`), the README fleet table.

## [2026-07-06 late evening] ABR-LONG funding gate (experiment)

### Added
- `18_ai_abr1_bot.py` — **LONG now only opens via the funding gate**: `fund_24h > +3 bps` (mean of the last 3 funding settlements, live via Binance REST, fail-closed, 30-min cache). Basis: a feature recheck on the operator hypothesis (report 21 addendum 2) — 16 setup mechanics + 6 funding features; the only out-of-sample survivor is the funding rule (+1.12%/trade, 74% WR, n=119/year on 100 coins; test +0.69%, n=17). Posts as ABR2 incl. the funding value in the info message; review after 4-6 weeks/≥30 trades. Break volume (the textbook criterion), incidentally, showed ZERO discriminatory power.
- `tools/backfill_funding_rates.py` + table `funding_rates` — the full Binance funding history (430d × 530 coins), resumable/idempotent; the basis for funding features in trainers/studies.
- `18_ai_abr1_bot.py` — **SHORT funding veto**: `fund_24h > +1,5 bps` blocks the signal despite the model gate (a mirror test on 33.5k SHORT events: the zone is −1.2%/trade in BOTH train AND test — exactly where the LONG gate opens → cross-validation). Fail-open: without funding data, the model signal applies. The SHORT info message now also shows the funding value.
- `core/funding_features.py` — **shared funding feature builder** (6 features, as-of, no look-ahead): canonical definitions from report 21 addendum 2 for upcoming retrains (RUB2/EPD2 earmarked in docs/MODEL_INTENT.md §7/§8) — one source instead of copy-paste skew, analogous to `core/mis_features.py`.

## [2026-07-06 evening] MIS2-SHORT live — dump side with study-validated bracket geometry

### Added
- `tools/mis2_dump_geometry_study.py` — a two-stage geometry study of the dump side (results `staging_models/mis2_dump_geometry_study*.json`): V1 (market entry, SL ≤8%) consistently negative — diagnosis: the selected coins spike upward before the dump (8h: TP rate 54%, but 38% SL breaches at +8%). V2 with operator input ("more SL distance") + bounce entry: **limit sell +5% above signal price + wide SLs turn 24h/72h/168h positive** (+0.49/+0.72/+0.27%/trade; 8h stays negative).
- `11_ai_mis_bot.py` — `DUMP_RULES` per horizon: entry limit +5%, single TP from the signal price (8H −5%, 24H −10%, 72H −15%, 168H −16.7%), SL from entry (5/16/12/12%). Dump models (close basis) deployed with operating point = top-2% val quantile (the safe picker had returned "not deployable" — operator decision for a live proof incl. 8H, documented in docs/MODEL_INTENT.md §1).

### Operator decisions
- **20x is posted** (cross margin, small positions on a large account) — deliberately NO `cap_leverage_to_sl` for MIS2-SHORT, even though SL is 12-16% beyond the isolated liquidation distance.
- All 4 dump horizons as trades (no warn channel); each timeframe has its own rules.

### Known follow-up
- The trade monitor knows nothing about limit entries: MIS2-SHORT signals whose +5% entry never fills (12-22% per the study) must not be scored as trades — a monitor adjustment remains open.

## [2026-07-06 evening] ABR2-LONG bypass reverted

### Fixed
- `1_data_ingestion.py` — **coins.json dual-writer conflict**: `update_trading_pairs()` (runs on every ingestion start) only filtered `status=TRADING` + non-USDC and let Binance new products into the coin list: quote assets "U"/"USD1" (→ the broken symbol **ETHU**), cross pairs (ETHBTC), quarterly futures (`_260925`), TRADIFI_PERPETUAL (stocks/metals like COSTUSDT/XAUUSDT) — together 657 instead of 530 symbols, consumed by the entire fleet (the ABR2 incident). Filter now identical to `6_housekeeping.update_coins_json` (quoteAsset=USDT + PERPETUAL); coins.json regenerated cleanly once (530).

### Changed
- `18_ai_abr1_bot.py` — **LONG-always bypass reverted** (operator decision reverted after ~60 LONG signals in 3h across 657 coins): the gate is active again for both directions; the LONG artifact (v2, threshold 0.3 ≈ open) replaced by the legacy 3-class model (no meta.json → the blocker contract @ 0.60). Rationale: report 21 — the setup unfiltered −0.59%/trade, break-even WR ~63%, ML/regime/management with no saving lever. SHORT (the ABR2 binary contract @ 0.75) unchanged live. `docs/MODEL_INTENT.md` §2 updated.

## [2026-07-06] Live-intervention batch after the intent walkthrough (docs/MODEL_INTENT.md)

### Fixed
- **Fleet-wide double-post bug** (operator report: Cornix recognized both messages as signals): the chart/info message had the Cornix block embedded AND the Cornix message went out separately to the same channel → two positions per signal. Fixed in **8 bots**: 18 (ABR), 7 (BR family), 13 (RUB), 9 (SR), 11 (MIS), 12 (ATS), 24 (QM), 25 (TD/BB), 29 (UFI1). New working rule: exactly ONE Cornix-parsable message per signal.
- `25_smc_ml_sniper.py` — the BB_1H parking gap closed: the parking only sat in the LONG branch, SHORT kept firing (a report-19 side finding).

### Changed (operator decisions from the intent walkthrough)
- **Versioning rule**: reworked models/bots post under a new tag (`model_id` in the artifact meta → `ai_signals.model`): **ABR2** (binary contract), **EPD2**, **RUB2**, **BR1Hv2**, **TD2_4H**, **BB2_4H**, future MIS2 etc. The tracker switched to prefix matching (`23_market_tracker.get_category`, `core/bot_naming` MIS\d+); cooldowns remain cross-version.
- `10_pump_dump_detector.py` — **EPD2**: the direction gate removed (both sides trade; the vol_ratio gate stays).
- `13_ai_rub_bot.py` — **RUB2**: LONG gate open again (intent: a symmetric idea).
- `7_pattern_detector.py` — **BR1Hv2**: the SHORT gate removed (both directions, until the BR ML gate is in place).
- `18_ai_abr1_bot.py` — **LONG always posts** (operator decision; the LONG model has no selection value even on clean events — confidence is informational); SHORT gate onto the v2 artifact.
- `25_smc_ml_sniper.py` — the model contract from the artifact (optimal_threshold, calibrator, meta.model_id) instead of hardcoded thresholds.
- `29_ufi1_bot.py` — **UFI1 reactivated** as-is (a deliberate operator decision, "lottery ticket", the objection documented in docs/MODEL_INTENT.md §10).

### Deployed (staging → bot directory, old artifacts in `staging_models/archive_2026-07-06_pre_v2_deploy/`)
- **ABR2** LONG+SHORT (retrained on 62k events of the repaired detector — distribution-matched to the new live detector).
- **TD2_4H** (threshold re-pick 0.58 via `pick_threshold_safe`: test 87 trades, 64.4% WR, +0.81%/trade).
- **BB2_4H** (re-pick 0.63; remains a filter with neutral PnL expectation).

## [2026-07-05] AIM1 retired — rebuilt as the AIM2 master meta-gate

### Added
- `docs/AIM2_DESIGN.md` — the rebuild plan per report 15 S7: AIM2 as a ranker/gate over all source signals (not a standalone alpha generator), label = first touch of the as-of reconstructed smart-targets geometry, rollout gates.
- `core/aim2_features.py` — ONE feature builder for trainer AND serving (market floor−1, regime, swarm without AIM1/AIM2 = the F6 fix, source identity from the DB vocabulary + trailing WR). No more train/serve skew (the P0.13 failure mode is structurally dead).
- `tools/aim2_build_dataset.py` — 241k events (43k posted AI + 198k conv, FIFO/volume deterministically undersampled), replay labels via `simulate_exit`, a `--skip-entry-hour` look-ahead probe. TZ re-measurement: all signal writers stamp PG local time (Europe/Bucharest) → UTC conversion (the AIM1 bot compared local against UTC, ≈3h offset).
- `tools/aim2_train.py` — chrono 70/15/15 + 7d purge, isotonic on val, threshold per replay PnL; artifact only to staging (P1.35).
- `audit_reports/20_aim2_training_results.md` — results: AUC test 0.686, monotonic calibration, gate uplift OOT −0.69% → **+1.92%/trade** @ 34% pass; fold 2 (Apr-May) +0.17%; no test month negative; dumb source baselines fail (uplift = real intra-source selection); look-ahead probe 0.7% flips symmetric.

### Changed
- `15_ai_master_bot.py` — fully onto AIM2: shared builder, calibrated probability, parity guard (an OOD sentry), daily model reload, candidates only `posted=true`, self-exclusion from the swarm, `ai_signals.model='AIM2'`. **Shadow-first:** posting only with `AIM2_LIVE_POSTING=1` (activated per operator sign-off on the evening of 05.07. — the channel is not traded, Cornix tracks it as validation).
- The AIM1 dossier marked historical; AIM1 statistics remain closed off under `model='AIM1'`.

## [2026-07-04/05] Binance WS root cause + ingestion hardening + health monitor

### Fixed
- **THE root cause of the WebSockets being "silent" since April:** Binance shut down the legacy futures WS URLs (`/stream`, `/ws`) as of **23.04.2026**; unrouted connections handshake OK but push nothing. All WS consumers (`1_data_ingestion.py`, `19_whale_logger_bot.py`, `chart_data_service.py`, `99_smc_paper_bot.py`) migrated to `wss://fstream.binance.com/market/stream`. The whale logger started writing files again from that point (the first since 18.04.).
- `1_data_ingestion.py` — a series of hardening changes: 180 streams/connection (HTTP-414 and a silent cap), backoff reset only on the first DATA message (`got_data`), backoff also on the silent-break path (previously ~900 connects/h), startup stagger, process priorities (ingestion ABOVE_NORMAL, catch-up children BELOW_NORMAL via ProcessPoolExecutor), gap-aware catch-up (24h instead of 730d with existing history).

### Added
- `1_data_ingestion.py` — **REST freshness fallback**: fills candle gaps TF-first (5m/30m/1h) via REST as long as the WS delivers no data; automatically goes to sleep once the WS is alive again.
- `core/health_monitor.py` + watchdog integration (60s): DATA_STALE (12 min → auto-restart of ingestion, 120-min cooldown), CPU_SATURATED (90%/5min), OUTBOX_FAILING/STUCK; alerts to `TELEGRAM_ALERT_CHAT_ID`.

## [2026-07-03/04] Audit immediate measures + DB operations

### Changed (portfolio, per audit reports 13-16)
- Parked via `control/parked/`: `14_ai_atb_bot.py` (ATB1), `29_ufi1_bot.py` (UFI1), temporarily `15_ai_master_bot.py` (AIM1 → replaced by AIM2 on 05.07.).
- Direction gates: EPD1 LONG-only + the `vol_ratio ≥ 5` gate, RUB1 LONG-only, BR1H SHORT-only; ATS1 band [0.60, 0.80); ROM1 15% SL cap; `cap_leverage_to_sl` in `core/trade_utils.py` (also understands "20x" strings).
- `3_detectors.py` — fast-in-and-out reactivated at explicit operator request (audit note F remains documented).

### Infra (VPS, not code)
- The PostgreSQL data dir migrated to `C:\PGDATA`; `pg_stat_statements` enabled; `wal_compression=pglz`; 2,380+ `(open_time DESC)` indexes, dedup/model indexes; 485 junk tables removed; `telegram_outbox` VACUUM FULL.
- The very first DB backups ever: `tools/backup_db.ps1` as a nightly scheduled task (03:30, `pg_dump -Fc` → `D:\_BACKUP\db`, retention 7 daily + 4 weekly).
- The TimescaleDB hypertable migration designed (`docs/TIMESCALE_R1_MIGRATION.md`), start after a stable fleet phase (task T-2026-CU-9050-018).

## [2026-07-05] ABR1 detector rework + binary model contract

### Fixed
- `18_ai_abr1_bot.py` — **direction coupling of the retest**: the old logic used `is_retest_long OR is_retest_short` as a pure touch gate and took the direction solely from the break — a high touch from below on a resistance broken upward (= a failed breakout, the training LOSS class) was signaled as LONG (mirror-image for SHORT). Now: LONG requires a low touch from above AND a close above the level, SHORT mirror-image (the trainer semantics).
- `18_ai_abr1_bot.py` — **hold check + first touch**: closes between the break and the retest must stay on the break side; only the first band touch after the break counts (as the trainer labels it). A dip + re-break anchors on the fresh break.
- `18_ai_abr1_bot.py` — **R07-ABR1-b**: `find_pivot_levels` without edge padding — only confirmed pivots remain (PIVOT_WINDOW candles on both sides), no more repainting edge levels.
- `18_ai_abr1_bot.py` — **R07-ABR1-a**: only the most recently closed candle is a retest candidate now (previously entries could be stale by up to 3h).

### Added
- `18_ai_abr1_bot.py` — `find_break_retest_setups()`: shared detection for the bot AND the walk-forward simulator (one source, no skew) incl. 5 setup-geometry features (`setup_dist_close_level_pct`, `setup_break_strength_pct`, `setup_candles_since_break`, `setup_level_age_candles`, `setup_retest_wick_pct`) — previously the B&R setup itself was invisible to the model.
- `18_ai_abr1_bot.py` — **R13-ABR1-5**: the model contract (features, threshold, success_proba column) is loaded from the artifact's `*_meta.json` instead of hardcoded; both binary models (retrain_from_replay) and legacy 3-class models are supported. An optional isotonic calibrator (`*_calib.pkl`) for the displayed confidence (the gate runs on the raw probability).
- `backtest/test_abr1_detection.py` — 9 unit tests covering all error classes of the old logic (synthetic candle series).

### Changed
- `tools/walkforward_sim.py` + `tools/retrain_from_replay.py` — MIS1 horizons extended from {72,168}h to all four live horizons {8,24,72,168}h (the bot runs 8 models; 8h/24h would otherwise have stayed on the old, defective trainings). The 400d replay needs to run again for this; the old one lives in `replay/archive_2026-07-05_mis1_h72_168/`.
- `tools/walkforward_sim.py` — the ABR1 adapter now uses `find_break_retest_setups()` from the bot module; geometry features land in the replay feature dict.
- `tools/retrain_from_replay.py` — `ABR1_FEATURES` = 18 indicator + 5 geometry features (`ABR1_FEATURES_LEGACY` for the old-model comparison); the `features` list goes into meta.json; the isotonic calibrator is persisted as `bt2_model_*_calib.pkl` (previously lost for abr1).

## [2026-06/07] Audit "Kythera 2026" (steps 1-10)

- `AUDIT_TODO.md` + `audit_reports/01…20` + model dossiers: a complete code/DB/ML audit across all 9 model families incl. live-DB verification (step 2), trainer provenance (step 3, all trainers sanitized in `legacy_trainers/`), bot performance from the live DB (step 4), regime-orchestrator analysis (step 6), a concept evaluation of all strategies (report 16), batch-E retrains on replay labels (report 19: `tools/walkforward_sim.py` + `tools/retrain_from_replay.py`, shared feature builders `core/mis_features.py`).
- Core findings incl.: AIM1 calibration inverted (P0.13), UFI1 +278R was a crisis-month artifact (P0.11, walk-forward proven), forming-candle serving (R1), TZ mix (R3), labels ≠ live geometry as a cross-cutting root cause (X-R1).
## [2026-04-18] Regime-Orchestrator (v1.0)

### Added
- `26_regime_detector.py` — Classifies BTC regime every 5 min (5 classes) + Alt-Context (3 classes, BTCDOM-based). Debounce on both axes independently. Hourly status posts + regime-change alerts.
- `27_bot_regime_analyzer.py` — Hourly Bot×Regime×AltContext×Direction performance. Two-stage whitelist: standard (WR≥Overall) + counter-trend (≥60% AND ≥Overall+10pp). Daily cross-table post 07:00 UTC.
- `28_signal_orchestrator.py` — Signal gating every 500ms. 4D whitelist check, overall fallback on detector failure. Auto-close on regime change. ROM1 tracking in ai_signals (automatically picked up by 8_ai_trade_monitor). A3 cooldown (4h).
- `core/regime_logic.py` — Shared classification logic (compute_features, classify_regime, apply_debounce).
- `backtest/backfill_regime_history.py` — One-off 90-day backfill (idempotent).
- 3 test files in `backtest/`
- 6 new DB tables: regime_history, regime_current, bot_regime_performance, bot_regime_whitelist, orchestrator_open_trades, orchestrator_suppressed_signals
- `docs/REGIME_ORCHESTRATOR.md`, `INSTALL_REGIME_ORCHESTRATOR.md`

### Changed
- `core/config.py` — REGIME_TRADING_CHANNEL_ID = <CH_REGIME_TRADING>, REGIME_STATUS_CHANNEL_ID = <CH_MARKET_DATA>
- `main_watchdog.py` — 3 new processes (start_delay 160/167/175)
- `23_market_tracker.py` — `Regime Fit:` line in Kelly post (graceful degradation)

# CHANGELOG — Crypto Bot Deep-Review & Fix Round

This review went through the entire codebase (46 Python files, 24 trading bots, Binance Futures integration, Telegram outbox, PostgreSQL storage) and found/clarified **91 analysis points** in total. Of these:

- **57 real bugs fixed**
- **20 points clarified as false alarms from initial analysis** (code was correct, my initial assessment too pessimistic)
- **6 points explicitly descoped by the user** (Master-Bot Dedupe, BTC SMC 100×, Handler-Auth, Cross-Bot-Limit etc.)
- **5 points documented as too invasive for this round** (schema change, retraining required)
- **3 points clarified as asyncio-non-critical/unreproducible**

## Fixes by topic

### 🔧 Trade-signal correctness (critical)
- **#1 SHORT RSI bug** (strat_fast_in_out, strat_5_percent): `>=75 OR <=45` → only `<=45`. The code generated SHORT signals on high RSI **AND** low RSI simultaneously → regularly a dumb trade direction
- **#3 RSI fillna parens**: `100 - (100/(1+rs)).fillna(0)` → `(100-100/(1+rs)).fillna(50)`. Previously, RSI was incorrectly shown as 100 (max overbought) where no data was present → false SHORTs
- **#13 AI SR bot cooldown**: `pd.Timestamp.utcnow().tz_localize(None)` crashed on newer pandas versions. Migrated to `market_utils.check_cooldown`
- **#15 Master-bot all_ai_models concat typo**: `'MIS1' 'MSI1-8h_pump'` (missing comma + swapped letters) concatenated → an invalid model name in ml_predictions_master
- **#19/#18 ATB `except: return True`**: the cooldown check returned "yes, may trade" on a DB hiccup → signal spam. Now safe-defaults to `False`
- **#32 ATS bot** OBV normalization: `obv - obv.iloc[0]` so the OBV values are not dominated by the arbitrary starting point of the history
- **#38 Smart targets SL fallback**: a `min/max` cap added so SL is guaranteed to sit inside (LONG) or outside (SHORT) entry2
- **#58 SMC ML sniper BB**: `MAX_BB_AGE=20` + a genuine 0.3% break-through (previously, 200-candle-old stale BBs could still trigger a signal)
- **#59 SMC ML sniper TD**: `MAX_TD_SPAN=50` candles (previously: unbounded)
- **#60 BTC SMC**: `ORDER BY ASC` → `DESC + reverse` (historical data was being read in the wrong order)
- **#65/#66 IP pattern bot**: `ALERTED_QMS` persistent, pattern ID with a Unix timestamp instead of a runtime counter
- **#55/#56 Quasimodo**: `MIN_CONFIDENCE 0.40→0.65`, `ZONE_TOLERANCE 0.01→0.005`, touch+bounce validation

### 🗄️ DB robustness
- **#4 Atomic write**: `active_trades_master` + `telegram_outbox` in one transaction instead of two separate ones (prevents a chart without a trade)
- **#8/#16 Monitor connection**: auto-reconnect in the trade monitor and AI monitor on a DB hiccup (previously: the bot kept looping with a dead connection)
- **#10 Trade monitor datetime**: `datetime.now()` → `datetime.now(timezone.utc)` in close_trade
- **#14 DB flusher SAVEPOINT**: per-row error tolerance, a single failed insert no longer takes down the whole batch
- **#48 telegram_outbox cleanup**: a nightly DELETE of sent entries older than 7 days (previously the table grew unbounded)
- **#60 BTC SMC** ORDER BY (above)

### 🎯 Cooldown consolidation
- **#33/#34/#51** three separate `is_cooled_down`/`set_cooldown` duplicates removed (SMC Forex, ATB, others), all now use `core.market_utils.check_cooldown`/`update_cooldown`
- **#34** SMC Forex cooldown keys without a TF suffix → a cross-TF block (1h and 4h no longer simultaneously on the same coin)
- **#17 RUB** cooldown check moved BEFORE the ML prediction (CPU savings)
- **#13 AI SR** its own timezone-crashing cooldown removed
- **#35 Mayank** 12h cooldown per asset+TF+direction added
- **#42** Mayank asset cooldown (already resolved by #35)

### 📊 Indicator engine & strategies
- **#5** a duplicate lookback block in the indicator_engine (caused incremental runs to ALWAYS load 3000 instead of 1000 candles)
- **#6 Trendline** made NaN-robust for constant prices, division-by-0 on `y[0]==0` caught
- **#12 Volume indicator** `df.loc[index-1]` → `iloc` with `reset_index` (a KeyError on filter-induced index gaps)
- **#45 indicator_state.json** atomic write via tmp+fsync+os.replace (prevents half-written reads)
- **iloc fix in strat_fast_in_out**: a DESC-sorted DF, `iloc[-1]` → `iloc[0]` for ATR access
- **#11 Support/resistance assignment**: by proximity (the nearest one below price = support, the nearest above = resistance) instead of by time

### 🤖 AI bots (feature robustness)
- **#20 ATB** NaN/Inf safeguard before predict_proba (`replace([inf,-inf],nan).fillna(0)`)
- **#24 RUB get_f** handles NaN/Inf, not just None
- **#25 ABR1** X_event NaN/Inf safeguard
- **#27 MIS1** thresholds explicitly logged on load (drift detection)
- **#36 AI monitor** targets_hit defensively cast to int()
- **#74 ABR1 SUCCESS_CLASS_IDX=0**: a warning comment added — **please verify manually against the training notebook!**
- **#75 ABR1** asymmetric thresholds documented (LONG=0.60, SHORT=0.80)
- **#76 ABR1** a redundant `minute != 0` filter removed (1h candles always have minute=0)
- **#52** get_hvn_and_sr_levels centralized (5 bit-identical copies → 1 in core/trade_utils.py)

### 💬 Telegram outbox & charts
- **#21 active_patterns.json** atomic write
- **#31 Housekeeping** respects outbox references (no longer deletes charts that still need to be sent)
- **#67 Chart-path race**: `int(time.time()*1000)` a millisecond timestamp in the file name (ms instead of s)
- **#68/#87 mark_sent/mark_failure**: only delete a chart if no other unsent outbox entries still reference the file

### 🛠️ Infra (watchdog, dashboard, housekeeping)
- **#69 Watchdog** exponential backoff `[0, 15, 60, 300, 900]s` based on crashes in the last hour
- **#70 Dashboard** stdout/stderr into `logs/dashboard.log` instead of DEVNULL
- **#85 update_model** threshold files (`threshold_*.pkl`) explicitly skipped + a `hasattr(model, 'save_model')` check
- **#88 core/state_utils.py** new: atomic_write_json + atomic_read_json as central helpers

### 📈 Market tracker & logger
- **#71/#73** category mapping corrected (TD/BB/QM as PATTERN instead of INDICATOR/VOLUME)
- **#72** volume approximation: `close` → `(open+close)/2` (reduces intra-candle movement error)
- **#81 Whale logger** `format_usd` now correctly handles negative values (`-$1.5M` instead of `$-1500000`)
- **#82 Funding logger** `check_top20_positive_pct` returns None instead of 50.0 on empty data
- **#83 Funding logger** `calc_diff_bps` returns None on missing history, the display shows "N/A"

### ❌ Deleted
- **99_smc_paper_bot.py** removed (a paper-trading bot that never ran live)
- The corresponding line in `main_watchdog.py` removed

## ⚠️ Important notes for the deploy

### Immediate checks before deploy
1. **Verify ABR1 SUCCESS_CLASS_IDX manually**: `18_ai_abr1_bot.py` line 45 — currently set to `0`, the standard XGBoost convention would be `1`. Please check against your training notebook. If `y=1` there stands for winning trades, the value MUST be changed to `1`.

### Check short-term (first run after deploy)
2. **Funding-logger Telegram output**: on the very first run when no 1h/24h history is present, `N/A` strings should now be shown instead of `+0.0bps`/`50.0%`. This is intentional.
3. **Market-tracker categorization**: TD/BB/QM/SMC signals now appear in the PATTERN category instead of INDICATOR/VOLUME. The statistics shift once.
4. **Dashboard log**: `logs/dashboard.log` should be created and written to. If the dashboard crashes, the traceback will be in there.
5. **SMC Forex cooldowns**: now cross-TF (12h). If signals come significantly less often, the duration can be reduced to 8h (code location `check_cooldown(conn, cd_key, display_name, 'LONG', 12)`).

### Medium-term (performance backlog, not now)
6. **#50** Market-tracker 10k queries: would require a unified `ohlcv_30m` table (an ingestion schema change). Performance backlog.
7. **#88** 7 more state files could be consolidated onto `core.state_utils`. Low priority.

### Not fixed, out of scope (deliberately)
- #22 Master-bot dedupe (a separate per-source assessment intended)
- #62 BTC SMC 100× leverage (deliberate high-risk)
- #77/#78 Open-handler auth (private env, intentional)
- #89 Cross-bot position limit (bots run selectively)
- #2 check_recent_trades (fine as-is)
- #53 TSI parameter order (verified: the EWMA composition is bit-identical)

## Final statistics

| Category | Count |
|---|---|
| Real bugs fixed | **57** |
| Clarified as false alarms | 20 |
| User-explicit out-of-scope | 6 |
| Too invasive for this round | 5 |
| Asyncio-non-critical | 3 |
| **Total reviewed** | **91** |

| Python files in the project | Syntax-clean after fixes |
|---|---|
| 47 | 47 ✅ |

## Files with substantial changes

```
core/
  market_utils.py              (FIX #51 zentral nutzbar)
  trade_utils.py               (+ get_hvn_and_sr_levels, ensure_min_tp_distance)
  state_utils.py               (NEU)
  update_model.py              (#85)

1_data_ingestion.py            (#14 SAVEPOINT)
2_indicator_engine.py          (#5, #6, #45)
3_detectors.py                 (#4 atomic signal write)
4_telegram_bot.py              (#68/#87 chart ref-counting)
5_trade_monitor.py             (#8 reconnect)
6_housekeeping.py              (#31, #48)
7_pattern_detector.py          (#21 atomic)
8_ai_trade_monitor.py          (#8, #36)
9_ai_sr_bot.py                 (#13, #52)
10_pump_dump_detector.py       (#38, #52)
11_ai_mis_bot.py               (#11, #15, #27)
12_ai_ats_bot.py               (#32, #38, #52)
13_ai_rub_bot.py               (#17, #24, #38, #52)
14_ai_atb_bot.py               (#18/#19, #20, #51, #52)
15_ai_master_bot.py            (#15, #28)
16_smc_forex_metals_bot.py     (#33, #34, #51)
17_mayank_bot.py               (#35)
18_ai_abr1_bot.py              (#25, #74, #75, #76)
19_whale_logger_bot.py         (#81)
20_funding_logger_bot.py       (#82, #83)
21_btc_smc_strategy.py         (#60)
22_ip_pattern_bot.py           (#65, #66)
23_market_tracker.py           (#71, #72, #73)
24_quasimodo_bot.py            (#55, #56)
25_smc_ml_sniper.py            (#58, #59)
main_watchdog.py               (#69, #70)
strategies/
  strat_fast_in_out.py         (#1)
  strat_5_percent.py           (#1)
  strat_main_channel.py        (#11)
  strat_volume_indicator.py    (#12)
```

Individual batch reports in `reports/batch_1_report.md` … `reports/batch_6_report.md`.
