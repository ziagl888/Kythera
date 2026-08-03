# Batch 3 Report — cooldown consolidation & ATB/master/forex

**Target files:** `14_ai_atb_bot.py`, `15_ai_master_bot.py`, `16_smc_forex_metals_bot.py`, `core/market_utils.py`

## Completed

### #33 — SMC forex cooldown check had a side effect (16_smc_forex_metals_bot.py)
The old `is_cooled_down` function executed the `INSERT ON CONFLICT` update command directly during the check — i.e. even if the subsequent `send_signal` had crashed, the table would already contain the cooldown entry (trade "sent" even though never posted). Now separated: `check_cooldown` (read-only query) before the send, `update_cooldown` only after a successful send.

### #34 — SMC forex cooldown keys without TF suffix (16_smc_forex_metals_bot.py)
Previously `module = f"SMC_{tf.upper()}_BOS"` → the same coin/direction could fire on `1h` and `4h` at the same time (dual signal). Now only `SMC_BOS` and `SMC_FVG` → cross-TF cooldown of 12h.

### #51 — cooldown consolidation
Two homegrown cooldown implementations removed and replaced by `core.market_utils.check_cooldown` + `update_cooldown`:
- `14_ai_atb_bot.py`: `is_cooled_down()` + `set_cooldown()` → `check_cooldown()` + `update_cooldown()`
- `16_smc_forex_metals_bot.py`: `is_cooled_down()` (mixed check+update) → separated `check_cooldown`/`update_cooldown` calls

This means only the market_utils helpers now exist centrally. All bots use the same timezone-aware logic, the same DB tables, and the same error handling.

### #28 — master bot symbol cleanup made more robust (15_ai_master_bot.py)
In two places: `str.replace('_.*', '', regex=True).str.replace('USDT', '', regex=False) + 'USDT'` replaced by `str.replace(r'_\d+[mhdwM]$', '', regex=True)`. The old logic was "self-healing" for standard coins (removed USDT and reattached it), but fragile for hypothetical edge cases such as coins with `USDT` inside the name. The new regex matches **only** the timeframe suffix at the end (e.g. `_1h`, `_4h`, `_30m`, `_1d`, `_1w`, `_1M`) and leaves the actual coin name untouched. Verified against 12 test cases.

## Already done

### #42 — Mayank asset cooldown
The Mayank bot was already migrated in **Batch 1** (fix #35) to `module_tag = f"MAYANK_{symbol_name}_{tf.upper()}"`. That is already cooldown per asset + TF + direction. No additional change required.

## Verification
- All 4 changed files parse cleanly
- No remaining project-wide calls to the old `is_cooled_down`/`set_cooldown` functions (verified via grep)
- Master bot regex verified against 12 test cases (standard coins + edge cases)

## Recommendations for a later review

- The ATB bot file still has a dead comment spot from the `set_cooldown` removal. Not critical, but worth tidying up in a later cleanup pass.
- The SMC forex cooldown duration (12h) is now cross-TF. If that turns out to be too restrictive (fewer signals than before), the duration can be reduced (`check_cooldown(conn, cd_key, display_name, 'LONG', 12)` → `8` or `6`).
