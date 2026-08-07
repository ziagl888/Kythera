# 40_trailing_close_bot.py — Trailing-close arm in its own Telegram channel.
"""
T-2026-KYT-9050-042 Phase C. Mirrors the signals of the 33 legs selected in PR #198
(``core.trailing_roster``) into its OWN channel and closes them there via
trailing-close instead of letting them run to SL/TP. Michi connects Cornix to this
channel — so the trailing arm runs live against the hold arm of the existing
fleet without any single existing bot changing its behaviour.

The bot decides NOTHING about entries. It mirrors what the fleet posts anyway,
and makes exactly one independent decision: when to close.

Why this is its own process
---------------------------
Trailing-exit is a DIFFERENT exit policy than the fleet's. Building it into the bots
would destroy the hold arm that we measure it against. As its own process with its own
channel it is a clean A/B arm: same entries, two exit rules, two curves.

Data flow
---------
``ai_signals`` (foreign, read-only) → roster+register filter → admission → entry in
``telegram_outbox`` (own channel) + own row in ``trailing_positions`` →
poll against live prices → trailing trigger OR source trade disappeared → ``Close
<SYMBOL>`` in the same channel.

The three traps that shape this bot
-----------------------------------
1. **Cornix' ``Close <SYMBOL>`` acts symbol-wide** (``core/config.py:123``). Two
   positions of the same symbol in the channel means: the trailing-exit of one flattens
   the other too. ``28_signal_orchestrator.py:1562`` solves the same conflict
   by deferring the close — here that would be wrong because timely exit
   is the whole point. So: at most ONE position per symbol in the channel.
2. **The chosen selection has an occupancy peak of 2001** = 4× the Cornix cap
   (``trailing_slot_budget_live.md:82``). Without its own admission control,
   Cornix at peak decides which ~1500 trades get rejected. The bot caps
   itself instead, by leg density rather than by arrival time.
3. **A scale-free trail is a micro-scalper.** "10% giveback from peak" fires
   even on a 0.5% peak. The activation threshold (2%, operator) is not a
   tuning parameter but the condition for the bot to trade rather than noise.

Price contract (rule 5)
-----------------------
This bot is a monitor in the exception sense: it does pure price checks against
the live ticker (``core.live_price``, one Binance call per poll for the whole fleet),
no indicator analysis. It reads no forming candle and derives no signal from any candle.

Safeguards
----------
``TRAILING_BOT_LIVE_POSTING`` is **default 0** and ``CH_TRAILING`` default unset:
without two deliberate operator entries the bot runs fully, tracks and logs,
but writes no outbox row. A deploy alone posts nothing.

Watchdog: start_delay=271.

Loss limitation (T-2026-KYT-9050-052, operator decision Michi 2026-07-28)
--------------------------------------------------------------------------
The trail can by construction only close winners — without a counterpart the
book unmixes to a pure loser book (live: 95% underwater in 9 h).
So two additional, strictly causal bounds (numbers: verdict
``staging_models/replay/trailing_arm_verdict_t052.md``):
  * **Time-stop** (``TIME_STOP_H``, default 24 h): never crossed the activation threshold
    → close to market, reason ``TIME_STOP``.
  * **Exposure cap** (``EXPOSURE_CAP``, default ±50): one direction may lead the other
    by at most 50 open mirrors — new entries in the overhang direction
    are rejected (no close, pure admission).

Invariants:
  * NEVER writes to ``ai_signals`` and NEVER closes a foreign trade — its only
    write rights are ``telegram_outbox`` (own channel) and ``trailing_positions``.
  * At most one open mirror position per symbol (Cornix-close is symbol-wide).
  * Open mirror positions ≤ ``SLOT_CAP``; direction overhang ≤ ``EXPOSURE_CAP``.
  * Exactly ONE Cornix-parseable message per entry (hard rule 4).
  * A leg without LIVE status in ``shadow_gate`` is never mirrored even if it
    is in the roster.
  * The time-stop decides only on the CURRENT peak state (causally) and never
    hits a sharp mirror — that belongs to the trail.
"""

import datetime
import json
import logging
import os
import time

from core import config as _kcfg
from core import shadow_gate
from core.database import PooledConnection, get_db_connection
from core.live_price import get_live_prices_batch
from core.market_utils import get_max_leverage
from core.signal_post import build_cornix_block
from core.trailing_roster import (
    ACTIVATION_PCT,
    EXPECTED_OCC_MEAN,
    EXPECTED_OCC_P95,
    RETRACE_FRAC,
    ROSTER,
    SLOT_CAP,
    SOURCE_REPORT,
    density,
    is_rostered,
    leg_key,
)
from core.trailing_state import TrailingState, mark_pct

logging.basicConfig(level=logging.INFO, format="%(asctime)s - TRAILING_BOT - %(message)s")
logger = logging.getLogger(__name__)

TARGET_CHANNEL_ID = _kcfg.CH_TRAILING
LIVE_POSTING = os.getenv("TRAILING_BOT_LIVE_POSTING", "0") == "1"
POLL_SECONDS = 10

# How old can a source trade be at most for mirroring it to still be the SAME
# trade?
#
# The mirror posts the entry price of the source signal, and Cornix places an
# order that only fills when the market reaches that price. If the market moves away
# in the meantime, Cornix never opens — while our book held the position as open
# and on trail sent a `Close` into the void. Measured on 2026-07-27: over a
# 15-min window 18 of 101 open mirrors were more than 1% away from market
# (median 0.40%, max 2.13%).
#
# 30s was the T-051 assumption ("a fresh signal is seen within one poll round")
# — measured it does not hold, varying by leg family:
#   * Tick legs (MIS2 pumps etc.): insert latency 30–120s (median 95s) —
#     the 30s window discarded ~85% of their signals (measurement 2026-07-29).
#   * Candle-cycle legs (MIS1-72h, TD_1H, AIM2, SRA2 — the LONG side):
#     deterministic ~185–195s (p25–p90 of 139 discarded: 184–193s;
#     measurement 2026-07-30) — a WALL right behind the 180s boundary, no
#     age distribution.
# 240s covers both families with buffer. The T-051 safeguard idea holds: market-
# entry + the plausibility riegel (market between SL and TP1) prevent
# mirroring runaway trades, and ROM1's hour-old re-forwards are filtered by the
# roster itself (EXCLUDED_AS_DUPLICATE) rather than randomly by this window.
MAX_MIRROR_AGE_SEC = float(os.getenv("TRAILING_BOT_MAX_AGE_SEC", "240"))

# How long to wait for the fill before the order is considered expired. After that
# the row is closed and a `Close` is posted so no stale order remains in Cornix
# that might fill days later.
FILL_TIMEOUT_MIN = float(os.getenv("TRAILING_BOT_FILL_TIMEOUT_MIN", "10"))

# Time-stop (T-2026-KYT-9050-052, operator decision Michi 2026-07-28): a mirror
# whose peak never crossed the activation threshold within this period is closed to market.
# Rationale from the book-health verdict: the trail can by construction only close winners
# — the never-sharp ones otherwise lie until catastrophic SL (live measured: SOURCE_CLOSED avg −4.8%,
# 34 SL hits in one day), and the book unmixes to a pure loser book (95% underwater in
# 9 h). The stop sells recoveries — that is its simulated, accepted cost
# (−11k over 5 months vs MaxDD −31% and half slot binding).
TIME_STOP_H = float(os.getenv("TRAILING_BOT_TIME_STOP_H", "24"))

# Flood protection: at most this many time-stop closes per 10s cycle. After a restart
# with stale inventory otherwise ~150 `Close` commands would be in the outbox in ONE cycle —
# the Telegram sender works FIFO, the backlog would delay all other fleet messages.
# This distributes cleanup over several minutes.
TIME_STOP_MAX_PER_CYCLE = int(os.getenv("TRAILING_BOT_TIME_STOP_MAX_PER_CYCLE", "25"))

# Grandfather date (operator decision Michi, 2026-07-28): the time-stop applies only
# to mirrors opened FROM this time onward. The stale inventory before rides
# on explicit operator risk to its natural SL/TP — the data (SOURCE_CLOSED avg −4.8%
# vs. time-stop at ~−2.4%) was seen and deliberately overridden. As a fixed cutoff
# rather than "computed at start" so a later restart doesn't silently exempt a new cohort.
TIME_STOP_SINCE = datetime.datetime.fromisoformat(
    os.getenv("TRAILING_BOT_TIME_STOP_SINCE", "2026-07-28T14:00:00+00:00")
)

# Net exposure cap per direction (T-2026-KYT-9050-052): a new entry whose
# direction already leads the opposite direction by EXPOSURE_CAP positions
# is not admitted. Not a market-state model (those were measured and discarded), but
# a structural bound: the book must not become arbitrarily one-sided. 0 = off.
EXPOSURE_CAP = int(os.getenv("TRAILING_BOT_EXPOSURE_CAP", "50"))

# How long a symbol remains locked after a POSTED close before it can be reused.
#
# The outbox delivers per channel strictly FIFO (4_telegram_bot.py, P0.1(d)), so Cornix
# gets `Close X` guaranteed before the new entry on X. But that only extends to the
# Telegram boundary: Cornix then places TWO market orders in opposite direction
# almost simultaneously at Binance. If the close is not settled there when
# the opposite position opens, the trailing close flattens the new position right away.
# Measured on 2026-07-27: XTZUSDT close + new entry in the SAME second (SHORT → LONG),
# ENAUSDT 3s apart (LONG → SHORT). Nothing went wrong — but that was luck, not design.
#
# The cost is a few delayed entries; with 33 legs over ~530 coins symbol
# collisions are frequent, the lock just shifts them.
SYMBOL_COOLDOWN_SEC = float(os.getenv("TRAILING_BOT_SYMBOL_COOLDOWN_SEC", "60"))

# How long a symbol stays locked after the trailing arm itself exited a position on
# it (T-2026-KYT-9050-115, operator decision Michi 2026-08-07). 0 = off.
#
# Distinct from SYMBOL_COOLDOWN_SEC, which is about Cornix settling a close. This
# one is about trade IDENTITY. The bot's "a once-trailed trade is done" lock
# (`read_mirrored_src_ids`, and the rationale on `open_mirrors`) is keyed on
# ``src_signal_id``, so it only ever recognises the SAME ai_signals row. A
# re-forwarding leg writes the same underlying trade under a NEW id and walks
# straight past it:
#
#   t        source signal posts, bot mirrors it
#   t+180s   the trail fires, mirror closes TRAIL — the source trade runs on
#   t+240s   the 60s cooldown has long expired, the symbol is free
#   t+250s   a re-forwarded row for that same trade, still inside
#            MAX_MIRROR_AGE_SEC of its OWN open_time, is admitted
#            -> re-entry into exactly the position the trail just exited
#
# The hole is not new and not specific to one leg: any two rostered legs firing on
# one symbol inside the window can produce it. FIF2's roster seat turns it from rare
# into routine, because that bot mirrors the fleet by construction AND its vol gate
# selects the fast tapes where a trailing exit within minutes actually happens.
#
# 1 h covers the whole re-forward window with wide margin — a re-forwarded row can
# only be admitted within MAX_MIRROR_AGE_SEC of its own open_time — while leaving a
# genuinely independent later signal on the symbol tradeable.
REENTRY_LOCK_H = float(os.getenv("TRAILING_BOT_REENTRY_LOCK_H", "1"))

# Exit reasons (land in trailing_positions.close_reason)
REASON_TRAIL = "TRAIL"
#: Time-stop: never crossed the activation threshold and older than TIME_STOP_H.
REASON_TIME_STOP = "TIME_STOP"
REASON_SOURCE_CLOSED = "SOURCE_CLOSED"
REASON_LEG_RETIRED = "LEG_RETIRED"
#: Not an exit but a record: this source trade was already running when the bot
#: started. It is never mirrored — the row exists only so it never
#: appears again as a new signal (same lock as a closed mirror).
REASON_PREEXISTING = "PREEXISTING"
#: Closed when switching from shadow to live: the row was open but never
#: posted, so it cannot correspond to any position in the channel.
REASON_SHADOW_CARRYOVER = "SHADOW_CARRYOVER"
#: Cornix never reached the entry — the order expired without ever filling.
REASON_NOT_FILLED = "ENTRY_NOT_FILLED"
#: The SL was hit — Cornix closes the position itself via the order on the
#: exchange. We book the exit only after and DELIBERATELY POST NOTHING: an extra
#: `Close` would at best be redundant and would claim an exit we did not
#: trigger (operator note Michi, 2026-07-27).
REASON_SL_HIT = "SL_HIT"

#: Exits that arm REENTRY_LOCK_H — the ones where the trailing arm CHOSE to leave a
#: position while the underlying trade was still alive at fleet level. Those are the
#: exits a re-forwarded row can undo, because the source trade is still open and
#: still being mirrored onward by other legs.
#:
#: The other reasons are deliberately not here. SL_HIT and SOURCE_CLOSED mean the
#: underlying trade is over, so there is nothing left to re-enter; ENTRY_NOT_FILLED
#: and SHADOW_CARRYOVER never held a position at all; PREEXISTING is a bookkeeping
#: marker, not an exit. Locking on those would cost entries without closing a hole.
REENTRY_LOCKING_REASONS = (REASON_TRAIL, REASON_TIME_STOP)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trailing_positions (
    id             BIGSERIAL PRIMARY KEY,
    src_signal_id  BIGINT      NOT NULL,
    symbol         VARCHAR(20) NOT NULL,
    model          TEXT        NOT NULL,
    direction      VARCHAR(10) NOT NULL,
    entry          DOUBLE PRECISION NOT NULL,
    peak_pct       DOUBLE PRECISION,
    opened_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at      TIMESTAMPTZ,
    close_reason   TEXT,
    close_mark_pct DOUBLE PRECISION,
    posted         BOOLEAN     NOT NULL DEFAULT FALSE
);
CREATE UNIQUE INDEX IF NOT EXISTS trailing_positions_src_uniq
    ON trailing_positions (src_signal_id);
CREATE UNIQUE INDEX IF NOT EXISTS trailing_positions_open_symbol_uniq
    ON trailing_positions (symbol) WHERE closed_at IS NULL;
"""

# Additively migrated (T-2026-KYT-9050-050): CREATE TABLE IF NOT EXISTS does not
# touch an existing table, so columns must come individually — same
# pattern as the schema safeguard in 8_ai_trade_monitor.
SCHEMA_ADD = [
    "ALTER TABLE trailing_positions ADD COLUMN IF NOT EXISTS filled_at TIMESTAMPTZ",
    "ALTER TABLE trailing_positions ADD COLUMN IF NOT EXISTS mirror_price DOUBLE PRECISION",
    "ALTER TABLE trailing_positions ADD COLUMN IF NOT EXISTS sl DOUBLE PRECISION",
]


def ensure_schema(conn) -> None:
    """Create own table. Does not touch any existing fleet table.

    The partial unique index on ``symbol WHERE closed_at IS NULL`` is
    symbol uniqueness as a DB guarantee, not just a code check: two
    positions of the same symbol would be a silent misfiring by Cornix'
    symbol-wide ``Close``, and against that a constraint is the more honest tool
    than a condition that a later refactor optimises away.
    """
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
        for stmt in SCHEMA_ADD:
            cur.execute(stmt)
    conn.commit()


def clear_unposted_carryover(conn) -> int:
    """Close open mirror rows without posting — live mode only.

    An open row with ``posted = FALSE`` cannot correspond to any position in the channel:
    it was never posted. In shadow mode that is the normal case
    and must remain (it IS the shadow book). But as soon as live posting
    starts, such a row is stale carryover from the shadow phase — and harmful:
    it holds its symbol (at most one position per symbol) and a slot,
    both for something that does not exist in the channel. When switching on 2026-07-26
    there were 460 rows, so 460 blocked symbols.

    Runs only at start, not in poll: in running live mode
    unposted open rows do not arise at all (insert and outbox rows are in
    the same transaction), so cleanup in the cycle would have nothing to do and
    could only harm.
    """
    if not (LIVE_POSTING and TARGET_CHANNEL_ID):
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE trailing_positions
            SET closed_at = NOW(), close_reason = %s
            WHERE closed_at IS NULL AND posted = FALSE
            """,
            (REASON_SHADOW_CARRYOVER,),
        )
        n = cur.rowcount
    conn.commit()
    if n:
        logger.info("🧹 Closed %d unposted shadow row(s) — freed symbols/slots.", n)
    return n


# ─────────────────────────────────────────────────────────────────────────────
# READ (foreign table — select-only)
# ─────────────────────────────────────────────────────────────────────────────


def read_source_signals(conn) -> tuple[dict[int, dict], set[int]]:
    """``(mirrorable source trades, ids of ALL open source trades)``.

    ``ai_signals`` is the table of the AI monitor (bot 8). It is read here only;
    the monitor remains its sole writer.

    The second set separates two cases that would otherwise look identical but are not:
    a source trade that the fleet closed (row gone), and one that is still
    running but filtered out by roster/register. Both end the mirror
    — with different reasons in the protocol.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, symbol, model, direction, entry1, price, sl, targets, lev,
                   EXTRACT(EPOCH FROM (NOW() - open_time)) AS age_sec
            FROM ai_signals
            """
        )
        rows = cur.fetchall()

    out: dict[int, dict] = {}
    all_open: set[int] = set()
    # Age is computed by the DB, not Python: `ai_signals.open_time` is naive and
    # written PG-locally (TZ contract R3). Comparing against a "now" computed in
    # Python would be exactly the offset error from the TZ cluster
    # P2.1–P2.6; `NOW() - open_time` cannot make it in the first place.
    for sid, symbol, model, direction, entry1, price, sl, targets, lev, age_sec in rows:
        all_open.add(int(sid))
        if not is_rostered(model, direction):
            continue
        tag, side = leg_key(model, direction)
        # Register beats roster: the roster is a snapshot from 2026-07-26,
        # shadow_gate is the live state. A leg turned off in the meantime
        # must not be mirrored further to a live channel.
        if not shadow_gate.is_live(tag, side):
            continue
        entry = float(entry1) if entry1 is not None else (float(price) if price is not None else None)
        if entry is None or entry <= 0:
            continue
        tgt = json.loads(targets) if isinstance(targets, str) else targets
        if sl is None or not tgt:
            # Without SL or targets no complete Cornix block can be
            # built — posting half an order geometry to a Cornix channel
            # is worse than not mirroring.
            logger.warning(f"⚠️ {symbol} ({model} {direction}): no SL/target — not mirrored.")
            continue
        out[int(sid)] = {
            "symbol": symbol,
            "model": model,
            "tag": tag,
            "direction": side,
            "entry": entry,
            "sl": float(sl) if sl is not None else None,
            "targets": [float(t) for t in (tgt or [])],
            "lev": lev,
            "density": density(model, direction),
            # None (open_time NULL) counts as arbitrarily old — if in doubt, don't mirror.
            "age_sec": float(age_sec) if age_sec is not None else float("inf"),
        }
    return out, all_open


def read_mirrored_src_ids(conn, src_ids: set[int]) -> set[int]:
    """Which of these source trades has the bot ever mirrored — open OR
    already closed?

    This is the lock against re-entry, and the case is the normal case,
    not the exceptional one: the trailing-exit fires typically WHILE the
    source trade is still running (that is exactly why the bot exists). Checked against
    only open mirrors, the same `ai_signals` row would look like a new signal at the next poll
    — and the bot would reopen all 10s until the fleet closes the source trade.

    Queried against the currently open source ids rather than the whole
    table so the check scales with the number of open trades, not with
    history.
    """
    if not src_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT src_signal_id FROM trailing_positions WHERE src_signal_id = ANY(%s)",
            (list(src_ids),),
        )
        return {int(r[0]) for r in cur.fetchall()}


def read_cooling_symbols(conn) -> set[str]:
    """Symbols for which a `Close` might currently be in progress at Cornix.

    Only POSTED closes cool down: a shadow close never sent a command
    and thus cannot collide with anything. The window is computed by the DB against its own
    ``NOW()`` — same rationale as the age filter (TZ contract R3).
    """
    if SYMBOL_COOLDOWN_SEC <= 0:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT symbol FROM trailing_positions
            WHERE posted AND closed_at IS NOT NULL
              AND closed_at > NOW() - make_interval(secs => %s)
            """,
            (SYMBOL_COOLDOWN_SEC,),
        )
        return {r[0] for r in cur.fetchall()}


def read_reentry_locked_symbols(conn) -> set[str]:
    """Symbols the trailing arm exited itself within ``REENTRY_LOCK_H``.

    Unlike ``read_cooling_symbols`` this does NOT filter on ``posted``. That column
    answers "could a Cornix order collide", which is the cooldown's question. This
    one asks "did our book already trade and leave this position", and a shadow
    mirror did exactly that — re-entering it would corrupt the shadow measurement
    the same way it would corrupt a live book. The window is computed by the DB
    against its own ``NOW()`` (TZ contract R3), in seconds so a fractional
    ``REENTRY_LOCK_H`` stays exact — ``make_interval(hours => …)`` takes an int and
    would silently floor 0.5 h to 0.
    """
    if REENTRY_LOCK_H <= 0:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT symbol FROM trailing_positions
            WHERE closed_at IS NOT NULL
              AND close_reason = ANY(%s)
              AND closed_at > NOW() - make_interval(secs => %s)
            """,
            (list(REENTRY_LOCKING_REASONS), REENTRY_LOCK_H * 3600.0),
        )
        return {r[0] for r in cur.fetchall()}


def read_open_mirrors(conn) -> dict[int, dict]:
    """Own open mirror positions, ``src_signal_id`` → row."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, src_signal_id, symbol, model, direction, entry, peak_pct, posted,
                   filled_at, mirror_price, opened_at, sl
            FROM trailing_positions
            WHERE closed_at IS NULL
            """
        )
        rows = cur.fetchall()
    return {
        int(src): {
            "id": int(rid),
            "symbol": symbol,
            "model": model,
            "direction": direction,
            "entry": float(entry),
            "peak_pct": float(peak) if peak is not None else None,
            "posted": bool(posted),
            # Old rows (before T-050) carry no mirror_price. They already ran as open
            # under the old logic and will continue to be treated that way — retroactively
            # declaring them unfilled would shut down ~100 live positions on a suspicion.
            "filled": filled is not None or mprice is None,
            "mirror_price": float(mprice) if mprice is not None else None,
            "opened_at": opened,
            "sl": float(sl) if sl is not None else None,
        }
        for rid, src, symbol, model, direction, entry, peak, posted, filled, mprice, opened, sl in rows
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST (own channel — exactly ONE parseable message per entry)
# ─────────────────────────────────────────────────────────────────────────────


def _post(conn, message: str) -> None:
    """Outbox row for own channel. Does not commit (caller contract)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
            (TARGET_CHANNEL_ID, message),
        )


def entry_messages(sig: dict) -> tuple[str, str]:
    """(Cornix block, HTML info) for a mirrored entry.

    The Cornix block comes from ``core.signal_post.build_cornix_block`` — the same
    source the fleet posts from. The info message deliberately does NOT repeat it:
    two parseable messages would be two positions (hard rule 4, the
    fleet-wide double-trade bug from 2026-07-06).
    """
    lev = sig["lev"] or get_max_leverage(sig["symbol"], 20)
    cornix = build_cornix_block(
        model_tag=f"{sig['tag']}-TRAIL",
        symbol=sig["symbol"],
        direction=sig["direction"],
        lev=lev,
        entry1=sig["entry"],
        sl=sig["sl"],
        targets=sig["targets"],
    )
    info = (
        "<pre>"
        + "\n".join(
            [
                f"<b>🪝 TRAILING MIRROR — {sig['tag']} {sig['direction']}</b>",
                f"<b>{sig['symbol']}</b>",
                f"<b>→ Trail: {RETRACE_FRAC:.0%} give-back once peak &gt; {ACTIVATION_PCT:.1f}%</b>",
                f"<b>→ Leg density: {sig['density']:.3f} % / slot-day</b>",
                f"<b>→ Time: {datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M')} UTC</b>",
            ]
        )
        + "</pre>"
    )
    return cornix, info


def close_messages(row: dict, reason: str, mark: float | None) -> tuple[str, str]:
    """(Close command, HTML info) for an exit.

    ``Close <SYMBOL>`` is Cornix' close command (``core/config.py:123``) and
    hits ALL trades of that symbol in the channel — the bot thus never holds two
    positions on one symbol. The command contains no entry fields and is
    therefore not parseable as a new signal.
    """
    if reason == REASON_TRAIL:
        why = "trailing stop"
    elif reason == REASON_TIME_STOP:
        why = f"time stop ({TIME_STOP_H:.0f}h below +{ACTIVATION_PCT:.0f}% activation)"
    else:
        why = "source trade closed"
    info = (
        "<pre>"
        + "\n".join(
            [
                f"<b>🔒 TRAILING CLOSE — {row['model']} {row['direction']}</b>",
                f"<b>{row['symbol']}</b>",
                f"<b>→ Reason: {why}</b>",
                f"<b>→ Mark: {'n/a' if mark is None else f'{mark:+.2f}%'} (unlevered)</b>",
            ]
        )
        + "</pre>"
    )
    return f"Close {row['symbol']}", info


# ─────────────────────────────────────────────────────────────────────────────
# ADMISSION
# ─────────────────────────────────────────────────────────────────────────────


def admit(
    candidates: list[tuple[int, dict]],
    held_symbols: set[str],
    free_slots: int,
    cooling: set[str] | None = None,
    open_by_dir: dict[str, int] | None = None,
    locked: set[str] | None = None,
) -> tuple[list, list]:
    """Who gets into the channel? Returns ``(admitted, rejected_with_reason)``.

    Five reasons, all hard:
      * ``SYMBOL_HELD`` — a mirror position is already running on this symbol, and
        Cornix' close is symbol-wide.
      * ``SYMBOL_REENTRY_LOCK`` — the trail exited a position on this symbol within
        ``REENTRY_LOCK_H``. Checked before the cooldown because a just-trailed symbol
        trips both and this is the more specific reason of the two.
      * ``SYMBOL_COOLING`` — a `Close` was just posted on this symbol, which
        may still be in progress at Cornix.
      * ``EXPOSURE_CAP`` — the candidate's direction already leads the opposite direction
        by ``EXPOSURE_CAP`` open positions. The book must not become arbitrarily
        one-sided (T-052: the one-sided LONG book WAS the account damage;
        the structural bound beat every market-state model in measurement).
      * ``SLOT_CAP`` — the channel is full. Sorted by leg density so
        in scarcity the same criterion decides that drove the selection:
        return per occupied slot-day.

    ``open_by_dir`` are the ALREADY open mirrors per direction; admitted
    candidates count immediately so a single cycle cannot overrun the cap.
    Rejections are returned, not swallowed — silent capping would later read
    as "everything mirrored".
    """
    admitted, rejected = [], []
    taken = set(held_symbols)
    cooling = cooling or set()
    locked = locked or set()
    dir_cnt = {"LONG": 0, "SHORT": 0, **(open_by_dir or {})}
    for sid, sig in sorted(candidates, key=lambda c: -c[1]["density"]):
        if sig["symbol"] in taken:
            rejected.append((sid, sig, "SYMBOL_HELD"))
            continue
        if sig["symbol"] in locked:
            # A re-forwarded row carries a new src_signal_id, so the src-keyed
            # re-entry lock cannot see that this is the trade we just trailed out of.
            # The symbol is the only identity both rows share.
            rejected.append((sid, sig, "SYMBOL_REENTRY_LOCK"))
            continue
        if sig["symbol"] in cooling:
            # A close is in progress on this symbol — racing it with an opposite order
            # is exactly the race we don't engage in.
            rejected.append((sid, sig, "SYMBOL_COOLING"))
            continue
        d = sig["direction"]
        other = "SHORT" if d == "LONG" else "LONG"
        if EXPOSURE_CAP > 0 and dir_cnt.get(d, 0) - dir_cnt.get(other, 0) >= EXPOSURE_CAP:
            rejected.append((sid, sig, "EXPOSURE_CAP"))
            continue
        if len(admitted) >= free_slots:
            rejected.append((sid, sig, "SLOT_CAP"))
            continue
        taken.add(sig["symbol"])
        dir_cnt[d] = dir_cnt.get(d, 0) + 1
        admitted.append((sid, sig))
    return admitted, rejected


# ─────────────────────────────────────────────────────────────────────────────
# ONE POLL CYCLE
# ─────────────────────────────────────────────────────────────────────────────


def record_preexisting(conn, stale: list[tuple[int, dict]]) -> None:
    """Record source trades as "seen, never mirrored".

    The row is immediately recorded as closed (``closed_at = NOW()``): it is
    not a mirror but a lock. ``read_mirrored_src_ids`` queries without
    ``closed_at`` filter, so this source trade never appears again as a new signal
    — the same mechanism that protects a trailed trade from re-entry. A closed
    entry also does not collide with the partial symbol index (which only applies to
    open rows), so it occupies no slot.
    """
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO trailing_positions
                (src_signal_id, symbol, model, direction, entry, peak_pct, posted,
                 closed_at, close_reason)
            VALUES (%s, %s, %s, %s, %s, NULL, FALSE, NOW(), %s)
            ON CONFLICT DO NOTHING
            """,
            [
                (sid, sig["symbol"], sig["model"], sig["direction"], sig["entry"], REASON_PREEXISTING)
                for sid, sig in stale
            ],
        )
    conn.commit()
    oldest = max(sig["age_sec"] for _sid, sig in stale)
    logger.info(
        "📎 Recorded %d source trade(s) as stale, not mirrored (oldest %.0f s, threshold %.0f s).",
        len(stale),
        oldest,
        MAX_MIRROR_AGE_SEC,
    )


def open_mirrors(
    conn,
    sources: dict[int, dict],
    mirrors: dict[int, dict],
    already: set[int],
    prices: dict[str, float] | None = None,
) -> int:
    """Mirror new source signals. Returns the number of opened positions.

    ``already`` are the source ids the bot has ever mirrored —
    open OR closed. Checking only against open mirrors would be the
    re-entry bug: after a trailing-exit the source trade usually
    keeps running, its row would look new again, and the bot would reopen every 10s.
    A once-trailed trade is done.
    """
    unseen = [(sid, sig) for sid, sig in sources.items() if sid not in mirrors and sid not in already]
    if not unseen:
        return 0

    # Stale inventory: was running before the bot could see it. NOT mirrored,
    # but recorded as a row so it never appears again as a new signal.
    stale = [(sid, sig) for sid, sig in unseen if sig["age_sec"] > MAX_MIRROR_AGE_SEC]
    if stale:
        record_preexisting(conn, stale)
    new = [(sid, sig) for sid, sig in unseen if sig["age_sec"] <= MAX_MIRROR_AGE_SEC]
    if not new:
        return 0

    held = {m["symbol"] for m in mirrors.values()}
    open_by_dir = {"LONG": 0, "SHORT": 0}
    for m in mirrors.values():
        open_by_dir[m["direction"]] = open_by_dir.get(m["direction"], 0) + 1
    admitted, rejected = admit(
        new,
        held,
        SLOT_CAP - len(mirrors),
        read_cooling_symbols(conn),
        open_by_dir,
        locked=read_reentry_locked_symbols(conn),
    )

    # Bundled rather than per candidate: the rejections repeat in EVERY
    # 10s cycle as long as the source trade is open. In the first shadow run
    # that was ~870 rows per cycle = ~1.5M/day in the shared watchdog log —
    # all other bots' logs would have drowned in it. The numbers stay
    # visible (no silent capping), individual cases go to DEBUG.
    if rejected:
        tally: dict[str, int] = {}
        for _sid, _sig, why in rejected:
            tally[why] = tally.get(why, 0) + 1
        logger.info(
            "⛔ %d nicht aufgenommen (%s)", len(rejected), ", ".join(f"{k} {v}" for k, v in sorted(tally.items()))
        )
        for _sid, sig, why in rejected:
            logger.debug("⛔ %s %s %s: %s", sig["symbol"], sig["tag"], sig["direction"], why)

    opened = 0
    live = bool(LIVE_POSTING and TARGET_CHANNEL_ID)
    prices = get_live_prices_batch() if prices is None else prices
    for sid, sig in admitted:
        # The market price at the moment of mirroring decides which side the
        # entry must be reached from. Without it the fill cannot be determined,
        # so better skip this cycle — the signal is still in the 90s window.
        market = prices.get(sig["symbol"])
        if market is None:
            logger.info("⏸ %s: no market price when mirroring — next cycle.", sig["symbol"])
            continue
        if not mirrorable_at(sig["direction"], market, sig["sl"], sig["targets"]):
            logger.info(
                "⛔ %s %s %s: market %s is outside SL/TP1 — no longer mirrorable.",
                sig["symbol"],
                sig["tag"],
                sig["direction"],
                market,
            )
            continue
        # ENTRY = MARKET (operator decision Michi, 2026-07-27). Previously the source
        # signal's entry was posted — but by the time the bot mirrors, the triggering
        # move has happened, and the market rarely comes back to that price: of 24
        # mirrors 5 filled (21%), for 15 of 18 cancellations the market never touched
        # the entry per 5m candles. The arm thus traded a selection it created itself —
        # favouring trades whose move reverts. Entering to market fills almost always
        # and lets both arms trade the same signals.
        sig["entry"] = market
        sig["mirror_price"] = market
        # WRITE FIRST, post only on actual insert — same pattern as
        # `DELETE ... RETURNING` in the AI monitor (P2.8). Otherwise
        # the outbox row would already be written if the insert fails on the unique index
        # (symbol already held, source already mirrored, second process) — and a post
        # without a corresponding row is a position no one will ever close.
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trailing_positions
                    (src_signal_id, symbol, model, direction, entry, peak_pct, posted,
                     mirror_price, filled_at, sl)
                VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, NOW(), %s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    sid,
                    sig["symbol"],
                    sig["model"],
                    sig["direction"],
                    sig["entry"],
                    live,
                    sig.get("mirror_price"),
                    sig["sl"],
                ),
            )
            created = cur.fetchone()
        if created is None:
            conn.rollback()
            logger.warning(f"⚠️ {sig['symbol']} {sig['tag']} {sig['direction']}: insert lost — not posted.")
            continue
        if live:
            cornix, info = entry_messages(sig)
            _post(conn, cornix)
            _post(conn, info)
        conn.commit()
        opened += 1
        logger.info(
            f"🪝 Mirror{'' if live else ' [SHADOW]'}: {sig['symbol']} {sig['tag']} {sig['direction']} @ {sig['entry']}"
        )
    return opened


def close_mirror(conn, row: dict, reason: str, mark: float | None, post: bool = True) -> None:
    """Close mirror position: close command + stamp own row.

    ``post=False`` for exits that Cornix triggered itself (SL) — there an own
    `Close` would be redundant, and it would claim an exit we did not
    trigger.
    """
    cmd, info = close_messages(row, reason, mark)
    if post and LIVE_POSTING and TARGET_CHANNEL_ID and row["posted"]:
        # Only close what was opened. A `Close` on a never-posted
        # position would be a command against a foreign trade in the live channel.
        _post(conn, cmd)
        _post(conn, info)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE trailing_positions
            SET closed_at = NOW(), close_reason = %s, close_mark_pct = %s
            WHERE id = %s AND closed_at IS NULL
            """,
            (reason, mark, row["id"]),
        )
    conn.commit()
    logger.info(
        "🔒 Close %s (%s %s) — %s @ %s",
        row["symbol"],
        row["model"],
        row["direction"],
        reason,
        "n/a" if mark is None else f"{mark:+.2f}%",
    )


def mirrorable_at(direction: str, market: float, sl: float, targets: list[float]) -> bool:
    """Does a market entry make sense at all on this geometry?

    The mirror enters at the current market but keeps SL and targets of the
    source signal at their ABSOLUTE prices — they are S/R levels, moving them
    would disconnect them from the levels, and the SL should be the same catastrophic
    stop as in the hold arm. That is exactly why this riegel is needed: if the market is
    already beyond TP1, the position would be at target in the same moment; if it is beyond
    SL it would be stopped out immediately. Neither is a trade, both are a fee.
    """
    if not targets or sl is None:
        return False
    tp1 = targets[0]
    if direction == "LONG":
        return sl < market < tp1
    return tp1 < market < sl


def sl_reached(direction: str, sl: float | None, price: float) -> bool:
    """Has the market reached the stop-loss?

    Cornix holds the SL as an order on the exchange and closes itself. We recognise it
    only to update our book — and to NOT send our own `Close`.
    """
    if sl is None:
        return False
    return price <= sl if direction == "LONG" else price >= sl


def sl_exit_mark(row: dict) -> float | None:
    """Realised mark of a SL hit — computed, not measured.

    The fill is at the stop level: that is where the order Cornix holds on the exchange
    sits. The value is thus known and is booked.

    It was booked as NULL until T-2026-KYT-9050-053, with the rationale "the book
    should not claim a value no one measured". That applies to a
    missing MARKET PRICE — not to the SL: the level is in the row. The consequence
    was a reporting defect that made exactly the worst exits invisible:
    over the clean series 66 hits were missing at avg −5.78% (Σ −381%), so a
    sum over ``close_mark_pct`` showed net −186% instead of −575%.

    Assumption, deliberate and like in the study for hard stops: **fill at stop level,
    no slippage.** A gap through the stop fills worse than booked here —
    so the value is the optimistic edge, not the expected value.

    Old rows without ``sl`` (before T-2026-KYT-9050-049) remain NULL: the level is
    not known there, and guessing it would be exactly the error the old rationale
    wanted to avoid.
    """
    if row.get("sl") is None:
        return None
    return mark_pct(row["entry"], row["sl"], row["direction"] == "LONG")


def has_filled(entry: float, mirror_price: float, price: float) -> bool:
    """Has the market reached the entry since mirroring?

    Cornix places an order at the posted entry and only opens when the market
    trades there — direction-independent, as Michi observed on 2026-07-27
    (ENAUSDT SHORT, entry 1.5% below market, remained unopened). The check models
    exactly that: the price must reach the entry from the side it was on when
    mirrored. It makes NO assumption about how Cornix treats LONG and SHORT
    differently — only that the price must touch the entry.
    """
    if mirror_price >= entry:
        return price <= entry
    return price >= entry


def poll_open_mirrors(
    conn,
    sources: dict[int, dict],
    mirrors: dict[int, dict],
    all_open: set[int] | None = None,
    prices: dict[str, float] | None = None,
) -> None:
    """Price poll over all open mirror positions (trailing + source close)."""
    if not mirrors:
        return
    if prices is None:
        prices = get_live_prices_batch()
    open_ids = all_open if all_open is not None else set(sources)
    missing = 0
    unfilled = 0
    time_stopped = 0

    for sid, row in mirrors.items():
        if sid not in sources:
            # Two different states, both end the mirror:
            #   * Row gone  → the AI monitor closed the source trade
            #     (SL/TP/timeout). The mirror must not hold a position the
            #     source strategy no longer holds — otherwise the A/B arm no longer
            #     measures the same trades.
            #   * Row exists but filtered → the leg fell out of roster/register.
            #     Then mirroring also stops, but for a different reason,
            #     and that must be distinguishable in the protocol.
            reason = REASON_LEG_RETIRED if sid in open_ids else REASON_SOURCE_CLOSED
            price = prices.get(row["symbol"])
            mark = None
            if price is not None:
                st = TrailingState(row["entry"], row["direction"] == "LONG", RETRACE_FRAC, ACTIVATION_PCT)
                mark = st.update(float(price))[1]
            # Without a price it is still closed — the source no longer holds the position,
            # so holding would be wrong. The mark then remains NULL instead of
            # a made-up 0.0: the book must not claim a value
            # no one measured.
            close_mirror(conn, row, reason, mark)
            continue

        price = prices.get(row["symbol"])
        if price is None:
            # No price means no decision. A position on a coin with no
            # tick stays open — closing it would be a statement about a market
            # we cannot see right now.
            #
            # DELIBERATELY WITHOUT single-query fallback (operator order Michi, 2026-07-26):
            # `core.live_price.get_live_price` would make one HTTP call PER position PER
            # poll. For the ~285 simultaneous positions expected at act=2% in 10s intervals
            # that is ~28 requests/s against fapi.binance.com —
            # a ban costs the whole fleet, a 10s-delayed trailing-exit
            # costs almost nothing. The next poll tries the batch again.
            missing += 1
            continue

        if row["filled"] and sl_reached(row["direction"], row["sl"], float(price)):
            # Cornix has already closed here. Just book the exit, post nothing —
            # but WITH the realised mark: the fill is at the stop level.
            close_mirror(conn, row, REASON_SL_HIT, sl_exit_mark(row), post=False)
            continue

        if not row["filled"]:
            # Not yet filled: Cornix does not have the position open, so there is
            # nothing to trail. A trail on an unfilled order creates exactly
            # the phantom-exit this task fixes.
            if has_filled(row["entry"], row["mirror_price"], float(price)):
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE trailing_positions SET filled_at = NOW() WHERE id = %s AND filled_at IS NULL",
                        (row["id"],),
                    )
                conn.commit()
                row["filled"] = True
                logger.info("✅ Fill: %s %s @ %s", row["symbol"], row["direction"], row["entry"])
            else:
                age = (datetime.datetime.now(datetime.timezone.utc) - row["opened_at"]).total_seconds() / 60
                if age > FILL_TIMEOUT_MIN:
                    # Expired. The close cleans up any stale order in Cornix
                    # so it does not fill days later and open a position
                    # no one watches anymore.
                    close_mirror(conn, row, REASON_NOT_FILLED, None)
                else:
                    unfilled += 1
            continue

        state = TrailingState(
            entry=row["entry"],
            is_long=row["direction"] == "LONG",
            retrace_frac=RETRACE_FRAC,
            activation=ACTIVATION_PCT,
            peak_pct=row["peak_pct"] if row["peak_pct"] is not None else float("-inf"),
        )
        should_close, mark, peak_advanced = state.update(float(price))

        if should_close:
            close_mirror(conn, row, REASON_TRAIL, mark)
            continue

        # Time-stop (T-052): NOT armed and older than the period → close to market.
        # Strictly causal — decided on the CURRENT peak state, never on
        # whether the trade would later arm (exactly this look-ahead beautified the
        # breakeven rules by 8× in the study, verdict addendum 4).
        # Armed mirrors belong to the trail: its stop sits above +1.8%, deep
        # losers cannot exist there. `opened_at` rather than `filled_at` because
        # old rows (before T-050) carry no filled_at; since market-entry (T-051)
        # both fall together anyway.
        if (
            TIME_STOP_H > 0
            and not state.armed
            and time_stopped < TIME_STOP_MAX_PER_CYCLE
            and row.get("opened_at") is not None
            # Grandfather: stale inventory before the cutoff rides (operator risk).
            and row["opened_at"] >= TIME_STOP_SINCE
        ):
            age_h = (datetime.datetime.now(datetime.timezone.utc) - row["opened_at"]).total_seconds() / 3600
            if age_h > TIME_STOP_H:
                close_mirror(conn, row, REASON_TIME_STOP, mark)
                time_stopped += 1
                continue

        if peak_advanced:
            # Peak is monotonic — only new highs change lasting state. This
            # keeps the write rate at a handful per position instead of one per
            # poll per position, and it is exactly the value without which a restart
            # would re-arm the trail below a long-standing peak.
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE trailing_positions SET peak_pct = %s WHERE id = %s",
                    (state.peak_pct, row["id"]),
                )
            conn.commit()
            row["peak_pct"] = state.peak_pct

    if time_stopped:
        logger.info(
            "⏱ Closed %d mirror(s) via time-stop (never above +%.0f%% in %.0f h).",
            time_stopped,
            ACTIVATION_PCT,
            TIME_STOP_H,
        )
    if unfilled:
        logger.info("⏳ %d mirror(s) still waiting for their fill (Cornix did not reach the entry).", unfilled)
    if missing:
        # Make visible that positions remained without decision this cycle —
        # a silent gap in the trailing would be worse than a loud one.
        logger.warning("⏸ %d position(s) without batch price — cycle skipped, no single query.", missing)


def main() -> None:
    mode = "LIVE" if (LIVE_POSTING and TARGET_CHANNEL_ID) else "SHADOW"
    logger.info(f"=== 🪝 TRAILING CLOSE BOT STARTED ({mode}) ===")
    logger.info(
        f"Roster: {len(ROSTER)} legs from {SOURCE_REPORT} · act={ACTIVATION_PCT}% · x={RETRACE_FRAC:.0%} · "
        f"cap={SLOT_CAP} (expected avg {EXPECTED_OCC_MEAN:.0f} / p95 {EXPECTED_OCC_P95:.0f}) · "
        f"re-entry lock {REENTRY_LOCK_H:.1f}h on {'/'.join(REENTRY_LOCKING_REASONS)}"
    )
    if mode == "SHADOW":
        logger.warning(
            "SHADOW: TRAILING_BOT_LIVE_POSTING=1 and CH_TRAILING are needed to post. "
            "The bot tracks and logs but writes no outbox row."
        )

    # Optional because the reconnect in the except block below may fail: then
    # the loop continues with conn=None and tries again at the next poll
    # instead of losing the process. The rest of the loop narrows via
    # the `if conn is None` guard.
    conn: PooledConnection | None = get_db_connection()
    ensure_schema(conn)
    clear_unposted_carryover(conn)

    while True:
        try:
            time.sleep(POLL_SECONDS)
            if conn is None:
                conn = get_db_connection()
            conn.commit()  # fresh transaction view, like monitor 8

            sources, all_open = read_source_signals(conn)
            mirrors = read_open_mirrors(conn)
            # Close first, then open — in this order so the `Close <SYMBOL>` goes out
            # guaranteed before a new entry on the same symbol: the outbox is strictly
            # FIFO per channel by id (4_telegram_bot.py, P0.1(d)/P1.3). Reversed it
            # would close the freshly opened trade flat again.
            # A symbol freed in this cycle can also be reused in it — but since
            # T-2026-KYT-9050-115 only when the trail did NOT choose the exit itself:
            # REENTRY_LOCKING_REASONS holds the symbol for REENTRY_LOCK_H, so what is
            # immediately reusable are the SL/SOURCE_CLOSED/never-filled exits.
            # ONE Binance batch call per cycle, shared by both steps.
            prices = get_live_prices_batch()
            poll_open_mirrors(conn, sources, mirrors, all_open, prices)
            open_mirrors(conn, sources, read_open_mirrors(conn), read_mirrored_src_ids(conn, set(sources)), prices)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error(f"Error in trailing-close bot: {e}", exc_info=True)
            try:
                if conn:
                    conn.rollback()
            except Exception:
                pass
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            try:
                conn = get_db_connection()
            except Exception as reconnect_err:
                logger.error(f"Reconnect failed: {reconnect_err}")
                conn = None
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 Trailing close bot stopped manually (Ctrl+C).")
