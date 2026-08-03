# T-2026-KYT-9050-042 Phase C — Trailing-Close Bot (own Telegram channel)

**As of:** 2026-07-26 · **Status:** built, reviewed, **NOT deployed** (fleet start is Michi's decision)
**Precursor:** T-041 Phase A (evaporation proven), PR #198 (slot budget, leg selection)

## Intent

A new fleet process (`40_trailing_close_bot.py`) mirrors the signals of the legs
selected in PR #198 into its **own Telegram channel** and closes them there via
trailing close, instead of letting them run to SL/TP. Michi hooks up Cornix
himself on this channel — so the trailing arm runs live against the hold arm of
the existing fleet, without a single existing bot changing its behaviour.

The bot is a **pure observer of the fleet**: it reads `ai_signals`, never writes
to it, and never closes an existing trade. Its only write permissions are
`telegram_outbox` (its own channel) and its own table `trailing_positions`.

## Operating point (operator decision Michi, 2026-07-26)

| Parameter | Value | Origin |
|---|---|---|
| Activation threshold `act` | **2.0%** unlevered peak | `trailing_slot_budget_live.json`, p95-safe fill |
| Giveback `x` | **10%** of peak | T-041/T-046 sweep |
| Legs | **33** (p95 selection at act=2) | `fills_by_act["2.0"]["p95"]["accepted"]` |
| Expectation | 49 204% net, avg 285 / p95 498 slots | ibid. |
| Slot cap | 500 (Cornix, per channel) | operator |

## Acceptance criteria (binary testable)

- [x] **AK1** — only legs from the roster are mirrored; a signal from a leg not
  selected produces no entry. *Test:* `test_roster_admits_only_selected_legs`
- [x] **AK2** — a leg whose `shadow_gate.leg_status` is not LIVE is not mirrored
  even if it's in the roster (registry beats roster).
  *Test:* `test_non_live_leg_is_never_mirrored`
- [x] **AK3** — the channel holds **at most one** position per symbol. A second
  signal on the same symbol is rejected with a reason, not silently dropped.
  *Test:* `test_second_signal_on_same_symbol_is_rejected`
  *Why:* Cornix' `Close <SYMBOL>` acts **symbol-wide** (`core/config.py:123`) — with
  two positions on one symbol, one's trailing exit would flatten the other too.
  That's the same conflict `28_signal_orchestrator.py:1562` resolves by deferring;
  here deferring is wrong, because a timely exit is the bot's entire purpose.
- [x] **AK4** — the channel never exceeds the slot cap; the bot decides the
  rejection by leg density, not Cornix by chance. *Test:* `test_slot_cap_rejects_by_density`
  *Why:* the chosen p95 selection has an occupancy peak of **2001** = 4× the cap
  (`trailing_slot_budget_live.md:82`). Without its own admission control, at the
  peak Cornix would decide which ~1500 trades get rejected.
- [x] **AK5** — the trail only fires once the peak has exceeded `act`; a trade that
  was never 2% in profit is never trailed. *Test:* `test_activation_floor_gates_the_trail`
- [x] **AK6** — the live trailing decision is identical to the study logic: the same
  mark sequence through `TrailingState` and through `core.wave_exit_sim.trailing_tp_trigger`
  yields the same trigger index. *Test:* `test_live_state_matches_wave_exit_sim` (rule 7)
- [x] **AK7** — the running peak survives a process restart (otherwise the trail
  re-arms on the fallen mark and a long-evaporated profit is never closed).
  *Test:* `test_peak_survives_restart`
- [x] **AK8** — if the fleet closes the source trade (SL/TP/timeout), the bot closes
  its mirror position as well. *Test:* `test_source_close_mirrors_into_a_close`
- [x] **AK9** — exactly **one** Cornix-parseable message per entry (hard rule 4); the
  info message and the exit message are not parseable as an entry.
  *Test:* `test_exactly_one_parsable_message_per_entry`
- [x] **AK10** — the Cornix block is **byte-identical** to the one `core.signal_post`
  produces for the same geometry (one source, rule 7 — the entry2 change from PR #197
  must never propagate on only one side). *Test:* `test_cornix_block_is_shared`
- [x] **AK11** — `TRAILING_BOT_LIVE_POSTING` is **default 0**: without an explicit
  operator flip the bot runs fully, tracks and logs, but writes **no** outbox row.
  An unset channel (`0`) also forces shadow.
  *Test:* `test_default_is_shadow_only`
- [x] **AK12** — the bot never writes to `ai_signals` and never closes a foreign trade.
  *Test:* `test_bot_never_writes_ai_signals` (source-code negative proof)

- [x] **AK13** — a source trade that was **already running** when the bot started is
  not mirrored, but recorded as pre-existing — and afterwards is never again treated
  as a new arrival.
  *Test:* `test_already_running_trades_are_recorded_not_mirrored`, `test_missing_open_time_counts_as_old`
  *Why:* the mirror takes over the geometry of the source signal, but Cornix fills at
  the **current** market. For a trade three days old, the trailing arm would then no
  longer measure the same trade as the hold arm — but that comparison is the bot's
  whole purpose. In the first shadow run (2026-07-26) this hit **465 positions in one
  shot**. The cutoff (`TRAILING_BOT_MAX_AGE_MIN`, default 15 min) deliberately covers
  a restart window, so trades that open during a restart aren't lost.
  *Test:* `test_the_age_cutoff_covers_a_restart_window`
- [x] **AK14** — rejections are logged **bundled per cycle**, not per candidate; the
  counters per reason stay visible. *Test:* `test_rejections_are_summarised_not_logged_per_item`
  *Why:* the rejection repeats every 10s cycle for as long as the source trade stays
  open — measured 34 691 lines in 33 min (~1.5M/day) in the **shared** watchdog log.

## Out of scope

- **No deploy, no fleet restart.** The watchdog reads `core/fleet.py` on import;
  the new entry is only supervised after a Michi-gated restart.
- **No Cornix configuration.** Whether and how Cornix gets hooked up to the channel is Michi's call.
- **No DCA/multi-entry.** The mirror posts single-entry (arm B, PR #197).
- **No own model.** The bot makes no entry decision — it mirrors.
- **No 5m/10s tightening of the study.** The bot sees real prices; the
  resolution limit from PR #198 only concerns the simulation.

## Why build instead of reuse (phase 0b)

No existing bot mirrors third-party signals into a second channel. Reused, however, are:
`core.wave_exit_sim.trailing_tp_trigger` (trailing semantics, pin-bound),
`core.signal_post` (Cornix block, shared via extraction),
`core.live_price.get_live_prices_batch` (one Binance call per poll),
`core.bot_naming.pretty_name` + `core.shadow_gate` (leg identity), the poll-loop
and reconnect shape from `8_ai_trade_monitor.py`.

## Known limits

- **Symbol uniqueness costs signals.** AK3 drops the second signal per symbol. The
  alternative would be one channel per leg (33 channels) — an operator decision, not code.
- **Slippage is not modelled live either**, but now real: the exits are small and
  frequent (at act=2%, median hold time 4.6 h).
- **The roster is a snapshot** of the register as of 2026-07-26. If `shadow_gate` changes
  a leg, AK2 kicks in (registry beats roster) — the roster itself is not automatically re-pulled.
