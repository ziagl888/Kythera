# Architecture — Kythera

How the pieces fit together, why they're arranged this way, and where the load-bearing
contracts live. This is the map a follow-on developer (or agent) reads to reconstruct
intent before touching the fleet. For the operator-facing overview (fleet roster, setup,
operations) see [`README.md`](../README.md); for the day-to-day working rules and the
curated traps see [`OPUS-HANDOFF.md`](OPUS-HANDOFF.md).

---

## 1. Design in one paragraph

Kythera is a **shared-database actor system**. ~29 single-purpose bot processes run
independently under one supervisor and never call each other directly — they coordinate
entirely through a **PostgreSQL database** and a **Telegram outbox table**. Each bot reads
what it needs (candles, indicators, signals, regime state), writes what it produces
(signals, trades, predictions), and stays individually crash- and restart-safe. Exactly
one channel — the Signal Orchestrator's — is wired to Cornix, so a trade fires **once**
even though many bots emit informational signals. This decoupling is the central
architectural bet: it trades the efficiency of in-process calls for the resilience and
observability of a message bus you can query, replay, and restart piece by piece.

---

## 2. Two-machine reality (read this first)

There are **two** environments, and confusing them is the most expensive mistake possible
here because real money runs on one of them.

| | **Build machine** (dev) | **Live VPS** (production) |
|---|---|---|
| What runs | Claude Code, code edits, tests | The fleet (`main_watchdog.py`), PostgreSQL, real trades |
| `core/.env` | Empty stub — all values blank | Real DB creds + Telegram token + `-100…` channel ids |
| DB reachable? | **No** (no `DB_*` set anywhere) | Yes |
| Model artifacts | Repo checkout only | Trainers + live `.pkl`/`.json` in `Documents\_X` |
| Safe to do | Code, docs, DB-free tests, offline analysis | DB-bound verification, training (throttled), deploys |

Consequences that fall out of this split:

- DB-bound work (arming the regression guard, live queries, retraining) **cannot** run on
  the build machine — it has no credentials by design. It moves to a VPS session
  (task T-2026-CU-9050-011).
- The fleet is live and leveraged. **No dev session restarts bots, writes to the live DB,
  or overwrites production model artifacts.** Those are operator (Michi) decisions. See
  [`OPUS-HANDOFF.md`](OPUS-HANDOFF.md) §6 for the escalation boundary.

---

## 3. System overview

```
                          Binance Futures
                   (WS wss://fstream.binance.com/market/…  +  REST fallback)
                                    │
                                    ▼
        ┌───────────────────  1  Data Ingestion  ───────────────────┐
        │        (klines → per-coin candle tables; gap-aware         │
        │         catch-up + REST freshness fallback; ≤180 streams/conn)
        │                                                            ▼
        │                                              ╔═══════════════════════╗
        │   2  Indicator Engine  ─── ~120 indicators ─►║                       ║
        │                                              ║      PostgreSQL       ║
        │   19 Whale · 20 Funding · 10 Pump/Dump  ────►║   (the message bus)   ║
        │        (market intelligence)                 ║                       ║
        │                                              ║  candles · indicators ║
        │   3/7/9/11…33  Strategy & AI bots  ─signals─►║  ai_signals · outbox  ║
        │        (read indicators, emit signals)       ║  trades · cooldowns   ║
        │                                              ║  regime_* · funding   ║
        │   26 Regime Detector → 27 Analyzer ─whitelist►║                       ║
        │                                              ╚═══════════╤═══════════╝
        │                                                          │
        │                                     28  Signal Orchestrator
        │                                      (regime whitelist gate, de-dupe,
        │                                       routes to the ONE Cornix channel)
        │                                                          │
        │                                                          ▼
        │                                                  telegram_outbox
        │                                                          │
        │   5 Trade Monitor / 8 AI Trade Monitor ──────►  4  Telegram Bot
        │        (SL/TP, trailing, close → closed_*)               │
        │                                                          ▼
        └───────────────────────────────────  Telegram channels ──► Cornix ──► Binance
```

The whole graph is supervised by **`main_watchdog.py`** (§4) and observed by
**`dashboard.py`** (read-only Flask UI). `main_telegram_bot.py` is a separate interactive
command bot, run on its own, not by the watchdog.

**The one routing invariant:** only the orchestrator's trading channel reaches Cornix.
Every individual bot channel is informational. And per signal there must be **exactly one
Cornix-parseable message** — an info/HTML message must never repeat the Cornix block, or
the same trade fires twice (fleet-wide double-post bug, fixed 2026-07-06).

---

## 4. Process model — supervision, not orchestration

`main_watchdog.py` is the **single owner of process lifecycle** (since commit 8d3145f).
No other component starts, stops, or restarts a bot. It:

- **Staggers startup** (`start_delay`) so 29 processes don't hammer the DB and Binance at once.
- **Restarts crashed bots** with exponential backoff.
- **Recycles heavy engines** periodically (`restart_interval`) to bound memory/leaks.
- **Runs health checks every 60s** via `core/health_monitor.py`: candle staleness →
  auto-restart ingestion; CPU saturation and outbox failures → Telegram alert.

Runtime control is **file-marker based** (`core/process_control.py`), which keeps it
crash-safe and inspectable:

- `control/parked/<script>.py` — keep a bot **down** across restarts. Currently parked by
  audit decision: `14_ai_atb_bot.py`, `29_ufi1_bot.py`. `22_ip_pattern_bot.py` is disabled
  in the watchdog roster itself.
- `control/restart/<script>.py` — one-shot recycle on the next watchdog cycle (e.g. after
  an `.env` change).

Because the bots share nothing but the DB, this supervision model is what makes
"restart one bot" a safe, local operation.

---

## 5. The database as message bus

There is no in-memory broker; **the database is the message bus**. Tables fall into four
roles. (DDL is created lazily by whichever bot owns the table — a known structural weakness,
see §10 / R2.)

### Market data (written by ingestion + indicator engine)
| Table | Owner | Role |
|---|---|---|
| per-coin candle tables `{symbol}_{tf}` | `1_data_ingestion.py` | Raw OHLCV, one table per coin × timeframe (**~9,300 tables** today) |
| `indicators` | `2_indicator_engine.py` | ~120 technical indicators computed from candles |
| `funding_rates` | `tools/backfill_funding_rates.py` | Funding history (~430d × ~530 coins), shared feature source |

### Signals & trades (written by bots, consumed by monitors + orchestrator)
| Table | Role |
|---|---|
| `ai_signals` | Every emitted ML/strategy signal (the fleet's signal ledger; `model` column carries the tag) |
| `closed_ai_signals` | Terminal outcomes of AI signals (scored by `8_ai_trade_monitor.py`) |
| `active_trades_master` | Open classic-strategy trades (`3_detectors.py`) |
| `closed_trades_master` | Terminal classic trades (`5_trade_monitor.py`) |
| `ml_predictions_master` | Shadow predictions (logged, not posted) |
| `telegram_outbox` | The send queue — every posted message lands here first |
| `trade_cooldowns` | Per-bot / per-symbol cooldown + circuit-breaker state |
| `master_ai_processed_signals` | AIM2 meta-gate dedup ledger |
| `pump_dump_events` | Pump/dump detections (EPD1) |

### Regime & orchestration (written by 26/27, read by 28)
| Table | Role |
|---|---|
| `regime_history` | Raw per-tick regime classifications |
| `regime_current` | Debounced current regime (the one bots should gate on) |
| `bot_regime_performance` | Per-bot performance by regime |
| `bot_regime_whitelist` | Which bots are allowed to fire in the current regime |
| `orchestrator_open_trades` / `orchestrator_suppressed_signals` | Orchestrator bookkeeping |

### Note on `ai_signals` / `closed_ai_signals` / `ml_predictions_master`
These are central but have **no `CREATE TABLE` in the live code path** (only in
`legacy_trainers/`). That's structural debt (R2), not intent — treat their schema as
established-by-history and change it only deliberately.

---

## 6. Signal lifecycle — end to end

Follow one trade from candle to close. Every arrow is a DB read or write; nothing is an
in-process call between bots.

```
1. Ingestion writes a CLOSED candle          → {symbol}_{tf}
2. Indicator engine computes indicators        → indicators
       (only on closed candles — forming-candle discipline, §7 / R1)
3. A detector/AI bot reads indicators, decides  → emits row to ai_signals + telegram_outbox
       (via core/signal_post.py; the info message must NOT repeat the Cornix block)
4. Signal Orchestrator (28) gates the signal:
       - is the emitting bot on bot_regime_whitelist for regime_current?
       - de-dupe against recent signals / cooldowns
       - if it passes → routes ONE Cornix-parseable message to the trading channel
5. Telegram Bot (4) drains telegram_outbox        → posts to Telegram → Cornix → Binance
6. Trade Monitor (5) / AI Trade Monitor (8) track SL/TP/trailing
       → on close, write closed_trades_master / closed_ai_signals
```

Two rules make this correct:

- **Only closed candles are analyzed.** The forming (unclosed) candle repaints; using it
  is look-ahead. The only exception is a raw price check inside monitors 5/8. (This is
  audit root-cause **R1** — the `is_closed` contract is designed in
  [`TIMESCALE_R1_MIGRATION.md`](TIMESCALE_R1_MIGRATION.md) but not yet enforced in the DB,
  so each bot currently guards itself; candle indexing is sort-order-sensitive — never
  "simplify" it blindly.)
- **The orchestrator is the single gate to Cornix.** A bot's own channel is informational;
  gating stats and execution stats are therefore not the same population (a documented
  spec-drift, P1.10).

---

## 7. The `core/` shared layer

`core/` is imported by every bot and is where the **cross-cutting contracts** live.
Changing a `core/` module changes behavior fleet-wide — that's the point, and it's
load-bearing.

```
core/
  config.py           Central config. All secrets + CH_* channel routing from .env.
                      _required() fails fast on missing DB_PASSWORD / TELEGRAM_BOT_TOKEN.
                      Coupled thresholds live here as a single source (detector == housekeeping).
  database.py         ThreadedConnectionPool. get_db_connection() (conn.close() returns to
                      pool), liveness-probe on checkout. KYTHERA_DB_POOL_MIN/MAX override.
  logging_setup.py    setup_logging("BOT") → stdout (watchdog-readable) + rotating file.
  state_utils.py      atomic_write_json / atomic_read_json (tmp + os.replace). Never
                      hand-roll state file writes.
  process_control.py  Park/restart marker semantics (§4).
  health_monitor.py   The 60s fleet health check + Telegram alerts.

  ── shared feature builders (trainer == serving == replay) ──
  aim2_features.py    AIM2 meta-gate features
  mis_features.py     MIS1 multi-horizon features (leakage-free)
  research_features.py  Bots 30-33 (PEX1/FMR1/TRM1/FIF1)
  funding_features.py   As-of funding features (no look-ahead)
  rub_features.py       RUB rubberband features
  model_artifacts.py  Uniform artifact loader → bots run in IDLE mode if artifact absent
  signal_post.py      Shared outbox + ai_signals write for the research bots

  ── trading & market helpers ──
  trade_utils.py      Smart-target / SL math, cap_leverage_to_sl(), format_price()
  market_utils.py     Coin lists, check_cooldown()/update_cooldown(), Telegram send
  regime_logic.py     Regime classification helpers
  bot_naming.py       Canonical bot-name normalisation
  charting.py         mplfinance chart rendering
```

Four contracts you must not break:

1. **Shared feature builders (X-R1 rule).** `core/*_features.py` are imported by *both* the
   bot and its trainer/replay. Editing one silently shifts a live model's feature
   distribution on both sides. The feature contract is hard: a missing column is **not**
   `fillna(0)`'d — a mismatch means the artifact won't load (bot goes idle), not a silent
   wrong prediction (the P0.12 lesson).
2. **The caller commits.** `core/signal_post.py` and the cooldown helpers run inside the
   caller's open transaction and do **not** commit themselves. Forget the caller-side
   commit and nothing persists (or persists partially).
3. **Missing artifact ≠ crash.** `model_artifacts.py` lets a bot deploy before its model is
   trained — it runs idle (`loaded=False`). A bot that "does nothing" may just be idle.
4. **Atomic state only.** State goes through `state_utils.atomic_*`, never a raw
   `open('w')`.

---

## 8. Regime & orchestration layer

A meta-system sits over the fleet and decides *which* signals become trades (live since
v1.0, 2026-04-18; design in [`REGIME_ORCHESTRATOR.md`](REGIME_ORCHESTRATOR.md)):

```
26 Regime Detector  → classifies market: 5 BTC regimes × 3 alt-contexts (BTCDOM-based),
                       each independently debounced         → regime_history / regime_current
27 Bot Regime Analyzer → scores each bot's historical performance per regime
                       → bot_regime_whitelist
28 Signal Orchestrator → for each incoming signal: is this bot whitelisted for the current
                       regime? de-dupe, then route the single Cornix message
```

It **does not trade** — it is a pure filter/router that gates bot signals by historical
regime performance and auto-closes on regime change. (The spec calls it "pure signal
router" while the code also builds its own trades — a documented drift, P1.10.)

---

## 9. ML pipeline — the replay-label rule

Since the 2026 audit, **all** retraining obeys one rule: **labels come from walk-forward
replay of the real posted order geometry** (first-touch TP1-vs-SL, fees included) — never
from close-based proxy labels. That rule exists because 7/8 trainer families were found
labeling idealized fills the bots never actually trade (P0.10).

```
tools/walkforward_sim.py      replays a detector's OWN setup fn bar-by-bar over history
                              → labeled trades (JSONL)      [labels from replay, not monitors]
tools/retrain_from_replay.py  retrains TD/BB/ABR1/MIS1 successors
                              (chronological split + purge gap, isotonic calibration,
                               threshold chosen by replay PnL)
tools/aim2_build_dataset.py   same methodology for the AIM2 meta-gate
     + aim2_train.py          → results in audit_reports/20_aim2_training_results.md
```

Two hard disciplines around artifacts:

- **Staging only.** Training tools write to `staging_models/` — **never** the live artifact
  path (repo-root `*.pkl`/`*.json`/`*.joblib`). Promotion to live is an explicit operator
  decision, never part of a training run.
- **New tag on rework.** A reworked model posts under a **new** `model_id` (ABR2, EPD2,
  RUB2, MIS2, …) via artifact meta → `ai_signals.model`; the tracker matches by prefix.
  Old tags are never reused.

AIM1 was retired (its calibration was reliably inverted, P0.13) and replaced by **AIM2**
(a ranker/gate over all source signals, shadow-first via `AIM2_LIVE_POSTING`). Research
bots 30-33 post to `CH_NEW_IDEAS` behind `NEW_IDEAS_LIVE_POSTING` and run idle until an
artifact is deployed.

---

## 10. Known structural debt

The 2026 audit ([`../AUDIT_TODO.md`](../AUDIT_TODO.md)) found that four **structural
root-causes** generate ~60% of all individual findings. They are the highest-leverage work
and are called out here so new code doesn't deepen them:

- **R1 — Forming-candle contract.** `is_closed` not yet enforced in the DB; every bot
  guards itself. Designed fix: [`TIMESCALE_R1_MIGRATION.md`](TIMESCALE_R1_MIGRATION.md).
- **R2 — No single source for fleet/schema.** Process list duplicated (`dashboard.py` vs
  `main_watchdog.py`); `CREATE TABLE` scattered and drifting; `ai_signals` /
  `ml_predictions_master` have no DDL in the live path.
- **R3 — No central UTC policy.** Writers fix UTC, several readers read naive-local → TZ
  bugs in cooldowns / windows / stats. Fix any time-window code along the R3 line
  (`core/time.py`, `timestamptz`), not as isolated patches.
- **R4 — Central leverage-vs-SL cap.** `cap_leverage_to_sl()` exists but isn't rolled out
  to every signal-emitting bot yet.

Plus the deferred infra block: **Z0** (VPS CPU permanently ~100% — measure before fixing)
and the TimescaleDB migration (collapse ~9,300 per-coin tables → 2 hypertables). Neither
is a point fix; both are operator-gated.

---

## 11. Configuration & secrets

All secrets and routing live in `.env` (git-ignored) and are read through `core/config.py`
— **never hardcoded**:

- **Secrets:** `TELEGRAM_BOT_TOKEN`, `DB_*`, optional Binance API keys.
- **Channel routing:** every posting target is a `CH_*` variable; `0` disables it.
- **Rollout gates (default-off):** `AIM2_LIVE_POSTING`, `NEW_IDEAS_LIVE_POSTING` — new
  behavior ships shadow/idle until a measurement justifies flipping it (an operator step).
- **Guard:** a `gitleaks` pre-commit hook rejects hardcoded tokens and `-100…` channel ids.
  `--no-verify` is forbidden. `.local/` also holds real channel ids — never commit it.

---

## 12. Documentation map

| Document | What it is |
|---|---|
| [`README.md`](../README.md) | Operator overview: fleet roster, setup, operations, dev tooling |
| **`ARCHITECTURE.md`** (this file) | How the system fits together and why |
| [`docs/OPUS-HANDOFF.md`](OPUS-HANDOFF.md) | Working manual: cycle, curated traps, quality bar, escalation |
| [`AUDIT_TODO.md`](../AUDIT_TODO.md) | Living findings ledger (R/P0-P3/Z) from the 2026 audit |
| [`docs/AIM2_DESIGN.md`](AIM2_DESIGN.md) | The AIM2 meta-gate design + training results |
| [`docs/REGIME_ORCHESTRATOR.md`](REGIME_ORCHESTRATOR.md) | The regime detection + orchestration layer (live) |
| [`docs/TIMESCALE_R1_MIGRATION.md`](TIMESCALE_R1_MIGRATION.md) | Designed R1 + hypertable migration (not yet applied) |
| [`docs/MODEL_INTENT.md`](MODEL_INTENT.md) | Per-model intent and semantics |
| [`docs/NEW_IDEAS_BOTS.md`](NEW_IDEAS_BOTS.md) | The 30-33 research bots (PEX1/FMR1/TRM1/FIF1) |
| [`docs/MM_ORDER_LIFECYCLE_SPEC.md`](MM_ORDER_LIFECYCLE_SPEC.md) | Concept spec: MM order-lifecycle patterns for the open Hyperliquid-venue decision (pre-decision) |
| [`SETUP_ANLEITUNG.md`](../SETUP_ANLEITUNG.md) | Detailed step-by-step setup (German) |
| `audit_reports/` | The full 2026 audit: 20 reports + per-model dossiers |
