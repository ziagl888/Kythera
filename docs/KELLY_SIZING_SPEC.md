# Position-sizing spec — fractional Kelly (distilled from CloddsBot `kelly.ts`)

**Task:** T-2026-CU-9050-057 · **As of:** 2026-07-10 · **Status:** design spec, **no live code**
**Source:** `alsk1992/CloddsBot` → `src/trading/kelly.ts` (license **MIT** — Copyright (c) 2026 alsk1992; code reference + port permitted, attribution mandatory in the port header)
**Provenance:** repo audit 2026-07-10 (KB `mcp-41a50fe33552`)

---

## 0. Purpose & the one thing to know first

This spec distills the parametrics of the `kelly.ts` position-sizing engine and checks whether and where it can dock into Kythera. **The central finding up front, because it frames everything else:**

> **Kythera doesn't size any notional amount today. Cornix does.** Kythera emits Telegram signals (direction, **leverage**, Margin:Cross, entry/TP/SL) — *how much capital* is deployed per trade is decided by the Cornix money-management config, not Kythera. `kelly.ts`, by contrast, computes exactly that notional size (`positionSize = bankroll × kelly`). A 1:1 port would have **no lever to pull** in Kythera.

The usable substance of `kelly.ts` is therefore not the `positionSize` number, but the **adjustment cascade**: the logic that modulates a raw Kelly fraction by drawdown, streaks, volatility, category performance and sample size. This cascade can be mapped onto a lever that Kythera actually owns (leverage and/or orchestrator gating). Chapter 4 shows the three docking options; chapter 6 gives the recommendation.

---

## 1. What `kelly.ts` does — the parametrics

### 1.1 Config parameters (`DEFAULT_CONFIG`)

| Parameter | Default | Meaning |
|---|---|---|
| `baseMultiplier` | `0.25` | **Quarter-Kelly** — the full Kelly fraction is compressed to ¼ (the classic fractional-Kelly protection against estimation error in `p`). |
| `maxKelly` | `0.25` | Hard upper bound of the final fraction (never more than 25% of the bankroll on one trade). |
| `minKelly` | `0.01` | Hard lower bound (min. 1%, if anything is sized at all). |
| `lookbackTrades` | `20` | Rolling window for win rate / avg return / volatility. |
| `maxDrawdown` | `0.15` | Drawdown threshold (15%) at which the fraction is fully reduced. |
| `drawdownReduction` | `0.5` | Factor at/above `maxDrawdown` — the fraction is halved. |
| `winStreakBoost` | `1.25` | Max boost on a win streak (anti-martingale, "more after wins"). |
| `winStreakThreshold` | `3` | The boost kicks in from 3 wins in a row. |
| `volatilityScaling` | `true` | Vol-target scaling on/off. |
| `targetVolatility` | `0.10` | Target vol (10%); real vol above target ⇒ smaller, below target ⇒ larger. |

### 1.2 The raw Kelly formula (`getBaseKelly`)

```
f = (b·p − q) / b        mit b = odds, p = Win-Prob, q = 1 − p
```

- `p` comes either directly from a known win rate **or** is estimated from an `edge`: `p = clamp(0.5 + edge/2, 0.05, 0.95)`.
- `odds = 1` (binary default, a prediction-market origin). **For Kythera this is the most important new parameter** — see §3.2: crypto perp trades have an asymmetric reward/risk (TP distance vs. SL distance), so `b = R = |TP−Entry| / |Entry−SL|`, not 1.

### 1.3 The adjustment cascade (`calculate`, 9 steps)

The order matters — the factors multiply:

1. **Base:** `kelly = fullKelly × baseMultiplier` (quarter-Kelly).
2. **Confidence:** `× confidence` (model/signal confidence, 0..1).
3. **Drawdown:** from 5% drawdown linearly down, at ≥ `maxDrawdown` a fixed `× drawdownReduction`. Formula: `1 − (dd/maxDD)·(1−reduction)`.
4. **Win-streak boost:** from `winStreakThreshold` wins `× min(1.25, 1 + (streak−thr+1)·0.05)`.
5. **Loss-streak reduction:** from 2 losses `× max(0.5, 1 − losses·0.1)`.
6. **Volatility scaling:** `× clamp(targetVol/realVol, 0.5, 1.5)`.
7. **Category adjustment:** if a category has ≥ 5 trades and deviates ±10pp from the overall WR: boost up to `1.2` / reduction down to `0.7`.
8. **Sample size:** < 10 trades ⇒ `× (0.5 + n/10·0.5)` (less confidence with a thin history).
9. **Bounds:** `clamp(kelly, minKelly, maxKelly)`.

After that: `positionSize = bankroll × kelly`, plus a `confidence` score (0.4·sample + 0.3·performance + 0.3·(1−drawdown)) and `warnings[]`.

### 1.4 State the engine keeps

`bankroll`, `peakBankroll` (→ drawdown), `recentTrades[]` (ring buffer of the last `lookbackTrades`), `winStreak`/`lossStreak`, `categoryStats` (map category → {wins, total, winRate}). Fed via `recordTrade()` / `updateBankroll()` after every close.

---

## 2. What Kythera has today (as-is)

### 2.1 The sizing-relevant levers

| Lever | Where | What it does |
|---|---|---|
| **Leverage cap (market)** | `core/market_utils.py:get_max_leverage` | Caps the desired leverage against `max_leverage.json` per symbol (default 20x). |
| **Leverage cap (SL)** | `core/trade_utils.py:cap_leverage_to_sl` | R4 fix: caps leverage so liquidation never sits before the SL (`lev ≤ safety/sl_dist`, safety=0.5 ⇒ factor 2). |
| **Trade geometry** | `core/trade_utils.py:calculate_smart_targets` et al. | Entry/Entry2/SL/TP from S/R, Fib, HVN, FVG clusters + ATR caps (SL ≤ 15%, E2 ≤ 10%). |
| **Signal gating** | `28_signal_orchestrator.py` | Regime whitelist + dedupe; decides **whether** a signal gets posted at all. |
| **Notional / margin per trade** | **Cornix** (external) | **Not in Kythera.** Kythera has no `bankroll`, no order-size computation. |

### 2.2 The state substrate already exists

The most important attachment point: **Kythera already computes the performance history a Kelly cascade needs** — today, in `27_bot_regime_analyzer.py`:

- Per **bot × BTC regime × alt context × direction** over rolling windows `[7, 30, 90]` days:
  - `win_rate` (%), `avg_pnl_pct`, `median_pnl_pct`, `sharpe = avg_pnl/stddev`, `n_trades`
  - Persisted in `bot_regime_performance` (UPSERT), threshold `MIN_TRADES_FOR_DECISION = 30`.
- Trade outcomes are classified **PnL-based** (`win`/`loss`/`neutral`); neutrals (|pnl| ≤ 0.1% housekeeping, > 100% data bug) are dropped — cleaner than `targets_hit`.

This is an **almost exact mapping** onto `kelly.ts` state:

| `kelly.ts` | Kythera equivalent | Status |
|---|---|---|
| `recentWinRate` (lookback 20 trades) | `bot_regime_performance.win_rate` (window 7/30/90 days) | ✅ present, different window definition (time instead of trade count) |
| `recentVolatility` | `stddev` of the PnL (already computed inside `sharpe`) | ✅ present (not separately persisted, trivial to add) |
| `recentAvgReturn` | `avg_pnl_pct` | ✅ present |
| `categoryWinRates` (category = e.g. coin class) | Bot × regime × direction is the natural "category" | ✅ present, finer granularity |
| `bankroll` / `peakBankroll` / `currentDrawdown` | — | ❌ **missing** (Kythera tracks no capital) |
| `winStreak` / `lossStreak` | — | ❌ **missing** (derivable from `ai_signals` close history, not materialized) |

**Consequence:** the win-rate-/vol-/category-driven adjustments (steps 2,6,7,8) are **immediately buildable data-side** in Kythera. The capital-driven adjustments (steps 3,4,5 — drawdown, streaks) need either bankroll/streak tracking that Kythera doesn't run today, **or** a reinterpretation of "drawdown/streak" at the bot level (a rolling PnL curve per bot instead of account equity).

---

## 3. The port to Python — spec (not implementation)

### 3.1 Form-follows-function requirements (Kythera rules)

A later port **must**:

- Be a **pure module in `core/`** (e.g. `core/kelly_sizing.py`), so that — should Kelly ever feed into trainer/replay/backtest — serving == replay holds (hard rule 7 / trap 2). As long as Kelly only scales live leverage, this isn't yet a feature-builder coupling; but once it's used to score backtests, the one-source rule applies.
- Be a **pure function + dataclass config**, no object with hidden mutable state in the bot. The `kelly.ts` closure state (`recentTrades`, `winStreak`, …) is **not held in-process** in Kythera, but read per call from `bot_regime_performance` / `ai_signals` (the DB is the source of truth, not a bot-local ring buffer).
- Carry **MIT attribution** in the header (`# Portiert aus alsk1992/CloddsBot src/trading/kelly.ts (MIT). …`).
- Ship **default-off** (Batch-E/`z-fable-judgment` discipline: falsify cheaply first, then live code).

### 3.2 Necessary adjustments vs. `kelly.ts`

1. **`odds` is not 1.** Crypto perp: `b = R = geplante TP-Distanz / SL-Distanz`. Kythera knows both at signal time (`calculate_smart_targets` provides entry/SL/TP). Without this correction, the Kelly formula systematically underestimates R>1 trades.
2. **Multi-TP reality.** Kythera signals have up to 10 targets with partial exits (Cornix scales out). The effective `R` is a weighted blend across the TP ladder, not `TP1`. For a first pass: set `R` conservatively to TP1 (underestimates rather than overestimates → safer).
3. **"Category" = bot × regime × direction**, not coin category. The granularity already exists in `bot_regime_performance`.
4. **Reinterpret drawdown/streak** (see §2.2): on the rolling bot PnL curve instead of account equity — or in phase 1, drop it entirely and port only the data-side-available adjustments (WR/vol/category/sample/confidence).

### 3.3 Signature sketch (illustrative, not final)

```python
@dataclass(frozen=True)
class KellyConfig:
    base_multiplier: float = 0.25   # Quarter-Kelly
    max_kelly: float = 0.25
    min_kelly: float = 0.01
    target_volatility: float = 0.10
    vol_scaling: bool = True
    # Drawdown/Streak-Parameter nur, wenn Phase-2-State vorhanden

def kelly_fraction(
    win_rate: float,          # aus bot_regime_performance
    reward_risk: float,       # R = TP-Dist / SL-Dist  (NEU ggü. kelly.ts)
    confidence: float,        # Modell-/Signal-Konfidenz
    recent_vol: float,        # stddev der PnL
    n_trades: int,            # Sample-Size-Gate
    cfg: KellyConfig = KellyConfig(),
) -> float:
    """Reine Kelly-Fraktion in [min_kelly, max_kelly]. Kein State, kein I/O.
    Portiert aus alsk1992/CloddsBot src/trading/kelly.ts (MIT)."""
    ...
```

This fraction is **not yet an order size** — what happens to it is covered in chapter 4.

---

## 4. Docking options — what the Kelly fraction acts on

Since Kythera doesn't supply a notional size, the fraction has to be mapped onto a lever that Kythera owns. Three options:

### Option A — Kelly → leverage scaling
The fraction modulates the desired leverage **within** the existing envelope:
`lev = round(base_lev × (kelly / max_kelly))`, afterward passed unchanged through `get_max_leverage` **and** `cap_leverage_to_sl`.
- **Pro:** uses a lever Kythera already owns; the risk envelope (SL cap) stays hard; no Cornix change.
- **Con:** leverage ≠ position size under cross-margin — higher leverage only increases liquidation proximity, not necessarily the capital deployed, if Cornix runs a fixed margin/order size. The risk effect hangs on the Cornix config and is **not** cleanly "Kelly fraction = capital share". **The semantics must be clarified before building** (see §6, open question 1).

### Option B — Kelly → orchestrator gating (size-as-inclusion)
Instead of varying the size, vary the **posting density**: only post if `kelly ≥ threshold`; a low fraction means the signal drops out. This extends the existing regime whitelist in `28_signal_orchestrator` with a continuous Kelly threshold.
- **Pro:** entirely within Kythera's control; no notional/leverage semantics question; directly measurable against `bot_regime_performance`; fits the orchestrator's "whether to post at all" role.
- **Con:** strictly speaking this isn't *sizing*, it's *selection*. The Kelly core (continuous size) is lost; only the cascade is used as a quality score.

### Option C — Kelly → Cornix per-signal risk
If Cornix parses a per-message risk/size field (e.g. "Risk: X%"), the fraction could go directly into the signal block. **Unverified** — the Cornix message-format capability needs verification before this is an option. Hard rule 4 (exactly one Cornix-parsable message) remains binding.
- **Pro:** the only way to reach real notional sizing without giving Kythera a capital model.
- **Con:** hangs on unconfirmed Cornix functionality; touches the money path directly (double-trade risk class).

---

## 5. CloddsBot ↔ Kythera comparison (summary)

| Dimension | `kelly.ts` (CloddsBot) | Kythera today |
|---|---|---|
| Sizes what? | Notional (`bankroll × kelly`) | nothing (Cornix); only leverage + geometry |
| Raw Kelly | `(b·p−q)/b`, `b=1` (binary) | would need `b=R` (asymmetric) |
| Win-rate state | ring buffer 20 trades, in-process | `bot_regime_performance`, DB, window 7/30/90d ✅ |
| Vol state | stddev of the last 20 returns | `sharpe`/stddev present ✅ |
| Category | free (coin class), map in-process | bot×regime×direction, DB ✅ (finer) |
| Drawdown | account equity vs. peak | no equity tracking ❌ |
| Streaks | in-process counter | not materialized (derivable) ❌ |
| Fractional protection | quarter-Kelly + max/min clamp | analogously adoptable ✅ |
| Execution | directly to an exchange adapter | via Telegram→Cornix (indirect) |

**Core finding:** the *statistics half* of the engine (WR/vol/category/sample → fraction) is **almost free** data-side in Kythera. The *capital half* (drawdown/streak/notional) has **no foundation** in Kythera — that's where Cornix sits, and without a bankroll model the reference frame is missing.

---

## 6. Recommendation

**Short version: don't build Kelly as a notional sizer. Adapt the adjustment cascade as a bot-side quality/confidence score — and only behind a replay proof, not speculatively.** Rationale per `z-fable-judgment`:

- **Outcome:** do we want *variable position size* or *better selection*? A real notional sizer requires either Cornix to hand Kythera the sizing (option C, unverified) or Kythera to be given a capital model (a large, irreversible step toward owning execution). Both are **out of scope** for a 2h low-prio task and an **operator decision** (escalation §6 handoff: touches the money path/architecture).
- **Cheapest falsification first:** before Kelly moves any live lever, run the backtest: does a Kelly fraction (from `bot_regime_performance`), applied as a **post-hoc weighting** on the walk-forward replay PnL, actually improve the result? If not → no-op, done. If yes → which adjustments carry the effect (WR? vol? category?).
- **Recommended build-out (if the replay proof is positive):** **option B** (Kelly score as a continuous orchestrator threshold) — entirely within Kythera's control, no Cornix/notional semantics trap, directly measurable against existing data, default-off via a gate.
- **Option A (leverage scaling) only** once §6-question 1 (leverage↔capital semantics under the real Cornix config) is cleanly resolved — otherwise you're scaling liquidation proximity instead of risk.

### Open questions for Michi (escalation)

1. **Cornix money management:** fixed margin/order size per trade, or %-risk? Whether option A has any sizing effect at all, and whether option C exists, depends on this.
2. **Should Kythera ever get its own notional sizing** (= a step away from "Cornix does money management")? A strategic, irreversible direction question — not decidable within this task.
3. **Drawdown/streak definition:** account equity (doesn't exist) vs. rolling bot PnL curve (buildable) — only relevant if Kelly is meant to go beyond the statistics adjustments.

### Next concrete step (no live intervention)

A Batch-E study task (template T-2026-CU-9050-020, the HMM study): compute the raw Kelly fraction from `bot_regime_performance`, apply it as a weighting on the existing walk-forward replay PnL, measure the effect. Decides build vs. no-op in ~1 day — **before** a single line of live sizing code exists.

---

*Attribution: adapted from `alsk1992/CloddsBot` `src/trading/kelly.ts`, MIT License (Copyright (c) 2026 alsk1992). This spec is design documentation; it ports no code into the live path.*
