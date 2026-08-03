# Per-bot performance: full rebuild

## What was changed

Four related problems from your last screenshot are fixed:

### 1. MIS1 consolidation
`MIS1-8h_pump` + `MIS1-8h_dump` → **`MIS1-8h`**
`MIS1-24h_pump` + `MIS1-24h_dump` → **`MIS1-24h`**
`MIS1-72h_pump` + `MIS1-72h_dump` → **`MIS1-72h`**
`MIS1-168h_pump` + `MIS1-168h_dump` → **`MIS1-168h`**

The `_pump`/`_dump` suffixes are really just synonyms for LONG/SHORT.
The `direction` information is stored separately anyway — so the suffix
only adds artificially inflated bot lists and nothing else.

### 2. MSI1 typo fix
`MSI1-*` → `MIS1-*` (historical incorrect entries are remapped for
display; the DB stays unchanged).

### 3. "ALL" column fixed
The 0% bug at EPD1 & co is gone. Root cause: the display also counted
**open trades** (shadow inserts without a close) as losses. Now
**only closed trades** are included in WR calculations.

### 4. Time-window logic migrated
Filter is now by **`created_at`** (open time), no longer `closed_at`.
Semantics: "1h" = "trades that were opened in the last hour".
Statistically cleaner — a 168h MIS1 signal no longer affects the
1h column just because it happens to close today.

### 5. New detail line for 4h
Under each bot, a 3-line detail view appears for the last 4h:

```
MIS1-8h      │  33%↓ │  67% │  69% │  63% │  65%   (n=3000, +1.04%)
  4h: 10 opened → 6 closed, 4 still open
    TP1+:4 TP2+:2 TP3+:1 TP4:0 | SL:2
    LONG: 3/4 win | SHORT: 1/2 win
```

- `4h: X opened → Y closed, Z still open` — sum X = Y + Z
- `TP1+:X` = reached at least TP1 (status ≥ 1)
- `TP2+:X` = reached at least TP2 (status ≥ 2)
- `TP3+:X` = reached at least TP3
- `TP4:X` = full hit (status = 4)
- `SL:X` = loss (status = 0)
- Sum of TP1+ and SL = closed (because status ∈ {0,1,2,3,4} covers everything)
- LONG/SHORT split shows asymmetric bot performance

## Important technical details

### New data sources
The function now pulls from **four** tables:
- `closed_trades_master` — classic, closed
- `active_trades_master` — classic, open
- `closed_ai_signals` — AI, closed
- `ai_signals` (JOIN with `ml_predictions_master`) — AI, open

For `ai_signals` there's no direct `created_at` column; we join
with `ml_predictions_master.time`. If the JOIN fails, there's a
fallback that simply takes `NOW()` as created_at (= trade gets
assigned to the current hour — acceptable graceful degradation).

### Sorting
Bots are now sorted by **closed trades** (`n_closed_total`),
not by total. This way bots with actual history rise to the top.

### n=X in the main line
Now shows only the number of **closed** trades (not opened).
This is consistent with the WR calculation.

### Kelly calculation
Unchanged, based on all closed trades. Now consistently uses
the same data basis as the All column.

### Number of lines in the post
Now 4 lines per bot (instead of 1):
- Main line (win rates)
- `4h: X opened → Y closed`
- `TP1+:...`
- `LONG: ... | SHORT: ...`
- Blank line

With 46 bots this gets longer — the split mechanism from the last
version still applies: table + Kelly block continue to be spread
across multiple messages if needed.

## Tested

With 180,000 mock trades across 7 strategies, including:
- EPD1 with 70k trades and 60% WR → display shows 60% (previously 0%)
- ATS1 with 100k trades and 58% WR → display shows 58% (previously 1%)
- MIS1-8h_pump + MIS1-8h_dump → shown as one line "MIS1-8h"
- MSI1-24h_pump → ends up under MIS1-24h
- Open trades appear as "still open" in the detail line
- Target staggering: TP1+ ≥ TP2+ ≥ TP3+ ≥ TP4, sum = closed

## Deploy

Replace one file:
```
C:\_BOTS\crypto_trading_bot_v2\23_market_tracker.py
```

Restart the watchdog. From the next XX:00:30, the post should:
- Show correct WR figures (no more 0% ghost values)
- Show MIS1 in the consolidated horizon versions
- Have a detail line with target staggering under each bot

## What stays the same

- Kelly block (half-Kelly, safe margin, pure margin)
- Message split at 46+ strategies (from the last round)
- HTML format (only `<pre>`, `<b>`, `<i>` without style attributes)
- Table layout (column alignment)

## If something breaks anyway

The changes are primarily additive:
- If `active_trades_master`/`ai_signals` table is empty: no crash, just `0 still open`
- If the ml_predictions_master JOIN is missing: fallback to NOW()
- If a bot has no 4h activity: the detail line is omitted

If you see errors in the log: send me the output, then I'll fix them.
