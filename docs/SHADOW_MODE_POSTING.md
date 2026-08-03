# Shadow-mode posting — fleet-wide (T-2026-CU-9050-125)

**Goal:** every `(model_tag, direction)` leg that does NOT post live
should, instead of silence, produce a **monitored shadow trade** — a trade
with a genuinely realized outcome in `closed_ai_signals`, but **without** a
post to a live channel (Cornix/Telegram). That way, suppressed legs and
not-yet-promoted retrains build up an outcome history that can later
honestly measure them — including the **regime-conditioned** unlock
(whitelist-v2 flip T-2026-CU-9050-069): a LONG leg that is globally
negative but positive in `TRANSITION` can only be evidenced with shadow
trades.

Motivated by Michi (2026-07-14): after the recompute the fleet is
effectively short-only for the direction models; every suppressed leg
posts nothing today and thus has **no trade record** on which a later
decision could be based.

---

## 1. Why it is safe — "monitored but unposted"

A shadow trade is an **`ai_signals` row without a `telegram_outbox` row**.

Verified (T-2026-CU-9050-125):
1. The AI monitor `8_ai_trade_monitor.py` reads `ai_signals` **unfiltered**
   (no channel/post gate), tracks entry-fill/TP/SL against the live price
   and writes a `closed_ai_signals` row on close. It contains **no**
   posting code whatsoever (no `send_telegram`, no `telegram_outbox`
   insert).
2. A channel post happens **exclusively** via a `telegram_outbox` row
   (drained by `4_telegram_bot.py`).

⇒ write `ai_signals`, leave out `telegram_outbox` = tracked-but-never-posted.
The monitor delivers the realized outcome; no signal ever reaches a channel.

**Safety contract (hard rules 1/2/4):**
- **DEFAULT = LIVE.** `core/shadow_gate.py` only lists legs explicitly
  marked SHADOW/RETIRED; everything else is live. The gate must **never**
  turn an existing live post into a shadow post — the wiring is purely
  **additive** on the not-posted branch of each bot.
- The code lands without anything going live: shadow never posts to a
  channel, artifacts stay in `staging_models/` (hard rule 2), promotion +
  fleet restart remain Michi's decision.
- The master kill switch `KYTHERA_SHADOW_POSTING=0` turns off **all**
  shadow emission (bots fall back to today's prediction-only behaviour).

---

## 2. Two shadow classes

| Class | What | Model source | Example |
|---|---|---|---|
| **(A) New generation** | A retrain runs PARALLEL to the still-live old tag | `staging_models/<tag>_model_<DIR>.pkl` (contract artifact) | ATS2 next to live ATS1, ATB2 next to live ATB1 |
| **(B) Suppressed direction leg** | An otherwise-live model whose one direction does not (yet) go live | already loaded in the bot | e.g. a hard-parked leg |

Both share the same emit primitive (`post_shadow_ai_signal`) and the same
`(tag, direction)` lifecycle classification. The only difference is
whether the bot additionally needs to load a staging artifact (A) or
already has the model (B).

> **Important re. the current state (docs/MODEL_INTENT.md):** the fleet is
> NOT blanket "SHORT-only live". Many LONG legs are deliberately
> live (RUB-LONG on legacy @0.75, ABR2-LONG via the funding gate, classic
> SR/Main/VolIndic/FastInOut-LONG). The genuine shadow candidates are
> primarily class (A) — the not-yet-promoted retrains (ATS2, ATB2, EPD2,
> SRA2, RUB2-LONG retrain, TD2/BB2, QM2 …) — plus the few hard-parked
> legs. That's why default-live + a **per-leg justified** registry is
> mandatory, not a blanket "all-LONG-to-shadow" switch.

---

## 3. Mechanics

### `core/shadow_gate.py`
- `leg_status(tag, direction) -> live|shadow|retired` — default **live**.
  `_LIFECYCLE` lists only non-live legs (with a rationale per line),
  `_RETIRED_TAGS` the superseded generations (AIM1, MIS1, …; a pure
  reporting classification, no posting effect).
- `SHADOW_ARTIFACTS` — class-(A) tags → artifact filenames per direction.
  `load_shadow_artifact(tag, dir)` loads from `staging_models/` (fail-soft:
  if the artifact is missing, the bot keeps running without the shadow leg
  — artifact presence is Michi's promotion decision).
- `score_artifact(artifact, feature_row)` — **raw** `predict_proba[:,1]`
  (the isotonic calibrator is reporting-only; `pick_threshold_safe` picks
  the threshold on the raw proba — identical to bot 13/25).
  `artifact_threshold(artifact)` reads `optimal_threshold` (None ⇒ no
  operating point, see below).

### `core/signal_post.py :: post_shadow_ai_signal(...)`
Writes **only** the `ai_signals` row (no `telegram_outbox`) plus the
`ml_predictions_master` shadow row (`posted=False`, deduped via
`log_prediction`). Dedupes against open trades (`has_open_ai_signal`),
does **not** commit (rule 8: the caller closes the transaction). Tracks
exactly `targets[:n_show]` (P2.31 parity — the monitor scores the
published TPs).

### Emit rule per leg
```
prob = score_artifact(shadow_model, features)
thr  = artifact_threshold(shadow_model)
if thr is None:                 # kein Operating-Point (z. B. ATB2, zu dünn)
    emit shadow trade           # Detektor IST das Gate → jedes Setup sammeln
elif prob >= thr:               # getreue Vorschau des Live-Verhaltens
    emit shadow trade
else:                           # unter Threshold: nur Prediction-Log wie heute
    log_prediction(posted=False)
```

---

## 4. Tag & lifecycle convention → report + tracker

- Shadow trades carry the artifact's **`model_id` meta** (rule 6): new
  generations have a new tag anyway (ATS2 vs. ATS1) → no collision with
  `has_open_ai_signal` or in `closed_ai_signals`.
- The lifecycle classification is **per `(tag, direction)`** — so
  direction alone separates a live SHORT leg from a shadowed LONG leg of
  the same model, without a schema change on live tables.
- The sentiment report (part 2, `23_market_tracker.py`) reads
  `leg_status(...)` and groups into **active / shadow / retired**.
  `tools/track_shadow_model.py` still reads the realized shadow rows by
  tag prefix from `closed_ai_signals`.

---

## 5. Reference wiring (in this PR)

### Bot 12 — ATS2 (class A)
The live path already builds the shared `build_ats_features` vector (ATS2
parity). ATS2 scores the **same** `X_live` on the same TSI crossover
event → a faithful preview. `_emit_ats2_shadow()` runs before the ATS1
band logic (independent of the ATS1 decision), builds the identical
HVN/S-R geometry at `prob >= 0,7825` and writes a shadow trade under tag
`ATS2`.

### Bot 14 — ATB2 (class A)
ATB2 has its **own** detector (`core/atb2_features.py`, confirmed pivots
+ closed breakout, one source with `walkforward run_atb2`).
`_emit_atb2_shadow()` does an R1-clean `read_candles(include_forming=False)`
read (≥1500 candles, EMA200-SMA-seed parity), `find_channel_breakout()`
on the last closed candle, `measured_move_targets()` and writes a shadow
trade under tag `ATB2` on every setup (ATB2's `optimal_threshold` is
**null** — data too thin, has to collect shadow first). Runs
independently of the ATB1 trendline logic.

Both encapsulate every error — the live path (ATS1/ATB1) must never be
affected.

---

## 6. Fleet-wide rollout — per-bot checklist

The same purely-additive pattern applies to every further leg:

1. **Verify the current gating:** does the leg post live today? (read the
   bot code, do NOT guess — default-live only protects as long as
   the registry is correct.) Only enter legs that are genuinely not live.
2. **Maintain the registry:** `_LIFECYCLE[(TAG, DIR)] = SHADOW` with a
   rationale; for class (A) additionally `SHADOW_ARTIFACTS[TAG]` +
   artifact to `staging_models/`.
3. **Wire the emit:** on the bot's not-posted branch,
   `post_shadow_ai_signal(...)` following the emit rule (§3), encapsulated
   in its own try/except.
4. **Feature parity:** use the model's shared (GETEILTEN) builder (rule
   7) — trainer == serving. No new feature path.
5. **Test:** a DB-free unit test (pattern `backtest/test_shadow_gate.py`):
   no `telegram_outbox`, `ai_signals` written, `posted=False`.

**Candidate roster (class A/B, not promoted — source: roster validation
2026-07-14 + MODEL_INTENT + staging inventory) — ALL wired up in this
PR:**

| Bot | Shadow tag | Artifact (staging) | Class | Collision? |
|---|---|---|---|---|
| 12 | **ATS2** | `ats2_model_{L,S}.pkl` | A | no (live = ATS1) |
| 14 | **ATB2** | `atb2_model_{L,S}.pkl` | A | no (live = ATB1) |
| 9 | **SRA2** | `sra2_model_{L,S}.json` | A | no (live = SRA1) |
| 13 | **RUB3** | `rub2_model_LONG.pkl` | B | **yes → challenger tag** (live-LONG posts "RUB2") |
| 10 | **EPD3** | `epd2_model_{L,S}.pkl` | B | **yes → challenger tag** (live posts "EPD2") |

**Challenger-tag convention (RUB3/EPD3):** if the retrain challenges a
LIVE leg that already posts under the same (DEMSELBEN) tag, the shadow
gets its own generation tag (operator decision Michi, rule 6). The
reason isn't just attribution: these bots' active-trade check
(`model IN (tag, legacy_tag)`) would otherwise let a shadow trade block a
LIVE position — a violation of the purely-additive invariant. RUB2-SHORT
stays live "RUB2"; the live EPD stays "EPD2". The artifact filename
still carries the retrain generation (`rub2_*`, `epd2_*`); only the
written tag is the collision-free challenger.

**Non-candidates:** TD2_4H / BB2_4H — already promoted/live (2026-07-14
deploy). QM2 — doesn't exist yet (the QM rework is a future task).

No silent cap: the roster covers every not-yet-promoted retrain with a
loadable staging artifact; exceptions are named above with a reason.

**Monitor load:** shadow trades increase the working set of
`8_ai_trade_monitor` (more open `ai_signals` rows). Watch the open-row
count on a broad rollout; the `has_open_ai_signal` dedup per
`(symbol, dir, tag)` limits the multiplication per leg.

---

## 7. Ops / promotion

- **Activation** is tied to a fleet restart (Michi, hard rule 1) — shadow
  emission only starts after the affected bots restart. No behaviour
  change until then.
- **Promoting a shadow leg → live:** copy the artifact from
  `staging_models/` to the repo root (hard rule 2, Michi), remove the
  registry entry (the leg goes back to default-live) or switch over the
  bot's live serving path, bump the tag to the new generation if
  applicable. Exclusively an operator decision.
- **⚠ PROMOTIONS-FALLE (promotion trap; natural-tag shadow
  ATS2/ATB2/SRA2):** these shadow tags are identical (IDENTISCH) to the
  future live-generation tag. When promoting into the live slot, the
  `_LIFECYCLE` entry MUST be removed **first**. Otherwise the bot's
  active-trade check (e.g. bot 9: `model IN (artifact_tag, legacy_tag)` →
  `IN ('SRA2','SRA1')`) hits the still-open SRA2 **shadow** rows and
  suppresses the first live SRA2 post (violation of the additive
  invariant, found in the adversarial review T-125). Not triggerable by
  the code alone — only by a promotion without a registry update. The
  challenger tags RUB3/EPD3 are free of this (own tag ≠ live tag); bots
  12/14 likewise (hardcoded live tags ATS1/ATB1). Remember: **promotion =
  copy the artifact AND delete the `_LIFECYCLE` line, in one step.**
- **Deactivating:** `KYTHERA_SHADOW_POSTING=0` (fleet-wide) or remove the
  registry entry (single leg back to the bot's default-live behaviour).
