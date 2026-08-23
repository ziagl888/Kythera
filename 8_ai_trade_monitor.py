import datetime
import json
import logging
import os
import time
import warnings

import pytz

# --- IMPORT CONFIGURATION FROM CORE ---
from core.bot_catalog import has_standard_leverage
from core.candles import read_candles
from core.database import get_db_connection
from core.market_utils import get_max_leverage
from core.state_utils import atomic_read_json, atomic_write_json

warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_MONITOR - %(message)s')
logger = logging.getLogger(__name__)

# T-2026-KYT-9050-150: cold-start catch-up. `last_checked` below is in-memory, so
# every process restart used to fall into the "no watermark -> newest candle only"
# branch and the whole downtime gap went unscored. We persist the end of each
# completed pass here and replay from it on the next start.
MONITOR_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_trade_monitor_state.json")
# Beyond this the gap is not replayed: an unbounded catch-up would score days of
# candles in one pass and is a book-repair job, not a monitor job.
MAX_CATCHUP_HOURS = 48.0
# Re-scan a little before the watermark. Re-scoring an already scored candle is a
# no-op (closed trades are gone from ai_signals), missing one is not.
CATCHUP_OVERLAP_MIN = 15
STATE_WRITE_INTERVAL_S = 60.0


def _resolve_catchup(wm_raw, now_utc):
    """Decide the cold-start replay start from the persisted watermark.

    Pure: no I/O, no logging. Returns (catchup_from | None, log_level, message).
    A None start means "score the newest candle only" - the pre-T-150 behaviour.
    """
    if not wm_raw:
        return None, "info", "cold start: no persisted watermark - scoring the newest candle only."
    try:
        wm = datetime.datetime.fromisoformat(wm_raw)
    except (ValueError, TypeError):
        return None, "warning", f"cold start: unreadable watermark {wm_raw!r} - scoring the newest candle only."
    if wm.tzinfo is None:
        wm = wm.replace(tzinfo=pytz.UTC)
    gap_h = (now_utc - wm).total_seconds() / 3600.0
    if gap_h < 0:
        return None, "warning", f"cold start: watermark {wm_raw} is in the future - ignoring it."
    if gap_h > MAX_CATCHUP_HOURS:
        return (
            None,
            "warning",
            f"cold start: {gap_h:.1f}h gap exceeds the {MAX_CATCHUP_HOURS}h catch-up cap - "
            "scoring the newest candle only. The gap stays unscored; repair the book out of band.",
        )
    start = wm - datetime.timedelta(minutes=CATCHUP_OVERLAP_MIN)
    return (
        start,
        "info",
        f"cold start: catch-up armed - replaying 5m candles from {start.isoformat()} ({gap_h:.2f}h gap).",
    )


def _catchup_floor(catchup_from, open_time):
    """Cold-start scan start for one trade - never before its own open_time."""
    if catchup_from is None:
        return None
    ot = open_time
    if ot is not None and ot.tzinfo is None:
        ot = ot.replace(tzinfo=pytz.UTC)
    if ot is not None and ot > catchup_from:
        return ot
    return catchup_from


def main():
    logger.info("=== 🤖 AI TRADE MONITOR STARTED (local DB mode) ===")

    conn = get_db_connection()

    # Schema safeguard: close_time column in closed_ai_signals (if missing from old version)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE closed_ai_signals
                ADD COLUMN IF NOT EXISTS close_time TIMESTAMPTZ DEFAULT NOW()
            """)
            # Limit entry support (MIS2-SHORT, 2026-07-06): entry_filled=FALSE
            # means "limit order not yet filled" — no scoring before fill;
            # expiry_hours = horizon for expiration (entry never reached) and
            # timeout exit (part of study-validated bracket geometry).
            cur.execute("ALTER TABLE ai_signals ADD COLUMN IF NOT EXISTS entry_filled BOOLEAN DEFAULT TRUE")
            cur.execute("ALTER TABLE ai_signals ADD COLUMN IF NOT EXISTS expiry_hours INTEGER")
            # Realized PnL report (T-2026-CU-9050-115): target prices + leverage
            # get lost on close otherwise (ai_signals row deleted, only
            # targets_hit remained) — without them the actually realised % return
            # (partial closes per target × leverage) is not reconstructable.
            # Additive columns, old rows remain NULL and are excluded from report
            # (exact-only, operator decision). ai_signals.lev: leverage is stamped
            # on the FIRST poll of this monitor (~10s after post), not just on
            # close — a max_leverage.json change during trade runtime can then not
            # corrupt the historical value (spec rationale), without touching the
            # ~14 signal emission sites.
            cur.execute("ALTER TABLE ai_signals ADD COLUMN IF NOT EXISTS lev TEXT")
            cur.execute("ALTER TABLE closed_ai_signals ADD COLUMN IF NOT EXISTS targets JSON")
            cur.execute("ALTER TABLE closed_ai_signals ADD COLUMN IF NOT EXISTS lev TEXT")
        conn.commit()
    except Exception as e:
        logger.warning(f"Could not migrate schema columns: {e}")
        conn.rollback()

    # Fail-fast instead of crash loop (review 2026-07-13): the close INSERT below
    # references targets/lev hard. If schema safeguard failed (lock, transient DB
    # error at boot), EVERY close from now on would fail at 10s intervals and
    # roll back batch updates of other trades — with just one warning log. Better
    # to die visibly: watchdog restarts with backoff, next boot retries migration.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name FROM information_schema.columns
            WHERE (table_name = 'closed_ai_signals' AND column_name IN ('targets', 'lev'))
               OR (table_name = 'ai_signals' AND column_name = 'lev')
            """
        )
        _have = {(row[0], row[1]) for row in cur.fetchall()}
    _need = {("closed_ai_signals", "targets"), ("closed_ai_signals", "lev"), ("ai_signals", "lev")}
    if _need - _have:
        logger.error(
            f"Schema safeguard incomplete, missing {sorted(_need - _have)} — "
            "poll/close path would be broken, shutting down for watchdog restart."
        )
        raise SystemExit(1)

    # FIX P2.7: in-memory watermark per trade ID (first stage, no DB schema change:
    # ai_signals has no suitable column). Remembers the open_time of the last
    # scored 5m candle; from there scans FORWARD over all new candles instead of
    # just checking the newest → SL/TP hits between polls/after stale phases don't
    # get lost. After process restart each trade starts at the newest candle (no
    # retroactive scoring of old trades).
    last_checked = {}

    # T-2026-KYT-9050-150: arm the cold-start catch-up. Safe in this process: the
    # monitor only writes the book - there is no Telegram/Cornix emission anywhere
    # in this file - so replaying a gap cannot fire late orders.
    catchup_from, _lvl, _msg = _resolve_catchup(
        (atomic_read_json(MONITOR_STATE_FILE, default={}) or {}).get("last_pass_utc"),
        datetime.datetime.now(pytz.UTC),
    )
    getattr(logger, _lvl)(_msg)
    _last_state_write = 0.0

    while True:
        try:
            # Synchronise with the 10-second cadence of the ingestion script
            now = datetime.datetime.now(pytz.UTC)
            seconds = now.second
            sleep_time = (10 - seconds % 10) if seconds % 10 != 0 else 10
            time.sleep(sleep_time)

            # FIX: if previous reconnect failed, try again.
            if conn is None:
                conn = get_db_connection()

            # Reset transaction view of the DB
            conn.commit()

            with conn.cursor() as cur:
                # Load ALL active AI trades
                cur.execute("""
                    SELECT id, symbol, model, direction, entry1, price, sl, targets, current_target_hit, open_time,
                           entry_filled, expiry_hours, lev
                    FROM ai_signals
                """)
                active_trades = cur.fetchall()

                # T-2026-CU-9050-115: stamp leverage on the FIRST poll (~10s after
                # post) — this freezes the value before max_leverage.json can drift.
                # UFI models (SL-capped post leverage) stay deliberately NULL; the
                # realized PnL report excludes them.
                stamped_lev = {}
                for t in active_trades:
                    if t[12] is None and has_standard_leverage(t[2]):
                        lev_now = get_max_leverage(t[1], 20)
                        cur.execute("UPDATE ai_signals SET lev = %s WHERE id = %s AND lev IS NULL", (lev_now, t[0]))
                        stamped_lev[t[0]] = lev_now
                if stamped_lev:
                    conn.commit()

            # FIX P2.7: clean up watermarks of no-longer-active trades (else dict
            # grows unbounded over process lifetime).
            active_ids = set(t[0] for t in active_trades)
            for tid in [k for k in last_checked if k not in active_ids]:
                del last_checked[tid]

            if not active_trades:
                continue

            # 1. Filter unique coins
            active_coins = set(t[1] for t in active_trades)
            coin_candles = {}
            stale_coins = set()

            # 2. Wick-aware: fetch high/low/close of 5m candles.
            #    SL/TP triggered intracandle, not just at candle close.
            #
            #    FIX P2.7: instead of just the newest candle, fetch ALL candles
            #    from the oldest watermark of trades on this coin (ascending), so
            #    hits between two polls don't get lost.
            #
            #    STALE GUARD: if the newest 5m candle is older than 30min, we
            #    mark the coin as stale. Trades on this coin are then NOT checked
            #    against stale prices — they stay open until either fresh data
            #    arrives or housekeeping closes the coin as DELISTED.
            #
            #    Why 30 minutes? Ingestion delivers 5m candles every 5 minutes.
            #    If a candle is missing >30 minutes, data is too uncertain to
            #    reliably detect SL/TP events — a price move could have triggered
            #    liquidations we never see.
            now_utc = datetime.datetime.now(pytz.UTC)
            stale_cutoff_seconds = 1800  # 30 min

            coin_min_wm = {}
            for t in active_trades:
                # T-2026-KYT-9050-150: on a cold start there is no last_checked yet -
                # fall back to the catch-up floor, else only one candle gets fetched
                # per coin and the per-trade filter below has nothing to work on.
                wm = last_checked.get(t[0]) or _catchup_floor(catchup_from, t[9])
                if wm is not None:
                    prev = coin_min_wm.get(t[1])
                    if prev is None or wm < prev:
                        coin_min_wm[t[1]] = wm

            # core.candles: 5m scoring candles, forming candle deliberately included
            # (monitors score SL/TP intracandle — contract 2: include_forming=True).
            # First run without watermark: just newest candle. Else the whole window
            # from watermark (start= is `>=` inclusive).
            for coin in active_coins:
                try:
                    start_wm = coin_min_wm.get(coin)
                    if start_wm is None:
                        df = read_candles(
                            conn,
                            coin,
                            "5m",
                            limit=1,
                            include_forming=True,
                            columns=("open_time", "high", "low", "close"),
                        )
                    else:
                        df = read_candles(
                            conn,
                            coin,
                            "5m",
                            start=start_wm,
                            include_forming=True,
                            columns=("open_time", "high", "low", "close"),
                        )
                    rows = list(df.itertuples(index=False, name=None))
                    if not rows:
                        continue
                    newest_open = rows[-1][0]
                    # Calculate age (open_time is TIMESTAMPTZ, now_utc is also TZ-aware)
                    if newest_open.tzinfo is None:
                        newest_open = newest_open.replace(tzinfo=pytz.UTC)
                    age_sec = (now_utc - newest_open).total_seconds()
                    if age_sec > stale_cutoff_seconds:
                        stale_coins.add(coin)
                        # Only debug log so monitor log doesn't explode
                        logger.debug(
                            f"⏸ {coin}: 5m-Candle {age_sec:.0f}s alt — skippe Trade-Checks (waiting for fresh data)"
                        )
                        continue
                    coin_candles[coin] = [
                        {
                            'open_time': r[0],
                            'high': float(r[1]),
                            'low': float(r[2]),
                            'close': float(r[3]),
                        }
                        for r in rows
                    ]
                except Exception:
                    conn.rollback()
                    pass

            if not coin_candles:
                continue

            # Stale coin summary: log once per hour at minute 0 so we see when
            # many coins have no fresh data (= sign of delisting or ingestion
            # problems).
            if stale_coins and now_utc.minute == 0 and now_utc.second < 10:
                logger.warning(
                    f"⏸ {len(stale_coins)} coin(s) with stale 5m data — trades "
                    f"on them stay open until housekeeping cleans: "
                    f"{sorted(stale_coins)[:10]}{'...' if len(stale_coins) > 10 else ''}"
                )

            # === NEW: BATCH PROCESSING VARIABLES ===
            BATCH_SIZE = 50
            updates_pending = 0

            with conn.cursor() as cur:
                for trade in active_trades:
                    (
                        trade_id,
                        symbol,
                        model,
                        direction,
                        entry1,
                        db_price,
                        current_sl,
                        targets_data,
                        targets_hit,
                        open_time,
                        entry_filled,
                        expiry_hours,
                        trade_lev,
                    ) = trade

                    candles_all = coin_candles.get(symbol)
                    if not candles_all:
                        continue

                    entry = float(entry1) if entry1 is not None else (float(db_price) if db_price is not None else None)
                    if entry is None or entry <= 0:
                        continue

                    # FIX P2.7: candle supply — forward from watermark in chronological order.
                    # `>=` not `>`, so the still-forming newest candle is re-checked each
                    # cycle as before (high/low grow intracandle). New trade (no watermark):
                    # just newest candle.
                    wm = last_checked.get(trade_id)
                    if wm is None:
                        # T-2026-KYT-9050-150: cold start replays the gap instead of
                        # collapsing onto the newest candle.
                        wm = _catchup_floor(catchup_from, open_time)
                    if wm is None:
                        trade_candles = candles_all[-1:]
                    else:
                        trade_candles = [k for k in candles_all if k['open_time'] >= wm]

                    # FIX: defensively convert targets_hit to int.
                    # Depending on DB schema (TEXT vs INTEGER) a string or int can arrive
                    # here — without cast `range(new_targets_hit, ...)` raises TypeError if
                    # schema was created as TEXT.
                    try:
                        hit_state = int(targets_hit) if targets_hit is not None else 0
                    except (ValueError, TypeError):
                        hit_state = 0
                    # P2.7: local state across candles (not DB re-read per cycle)
                    sl_state = current_sl

                    targets = None
                    if targets_data is not None:
                        targets = json.loads(targets_data) if isinstance(targets_data, str) else targets_data

                    # Limit entry status (MIS2-SHORT: entry = limit-sell +5% above
                    # signal price — scoring only AFTER fill, else phantom trades).
                    filled = True if entry_filled is None else bool(entry_filled)
                    expiry = int(expiry_hours) if expiry_hours is not None else None
                    ot_aware = open_time
                    if ot_aware is not None and ot_aware.tzinfo is None:
                        ot_aware = ot_aware.replace(tzinfo=pytz.UTC)

                    for candle in trade_candles:
                        last_checked[trade_id] = candle['open_time']

                        # close = market price of candle, for logging and legacy PnL.
                        # high/low for wick-aware SL/TP detection.
                        current_price = candle['close']
                        candle_high = candle['high']
                        candle_low = candle['low']

                        is_closed = False
                        close_reason = ""
                        close_price = current_price  # overridden if SL/TP triggers exactly at level
                        new_sl = sl_state
                        new_targets_hit = hit_state
                        db_was_changed = False  # helper variable for batch counter
                        tp_allowed = True

                        # Horizon age of this candle relative to signal
                        c_ot = candle['open_time']
                        if c_ot.tzinfo is None:
                            c_ot = c_ot.replace(tzinfo=pytz.UTC)
                        past_expiry = (
                            expiry is not None
                            and ot_aware is not None
                            and (c_ot - ot_aware) >= datetime.timedelta(hours=expiry)
                        )

                        if not filled:
                            if past_expiry:
                                # Entry never reached within horizon → expiry,
                                # PnL 0 (never in market). Consumers filter status.
                                is_closed = True
                                close_reason = "ENTRY_NOT_FILLED"
                                close_price = entry
                            elif (direction == "SHORT" and candle_high >= entry) or (
                                direction == "LONG" and candle_low <= entry
                            ):
                                filled = True
                                tp_allowed = False  # fill candle: conservatively SL only (like study)
                                cur.execute("UPDATE ai_signals SET entry_filled = TRUE WHERE id = %s", (trade_id,))
                                db_was_changed = True
                                logger.info(f"📥 {symbol} ({model}): limit entry {entry} filled.")
                            else:
                                continue  # no SL/TP scoring before fill

                        if is_closed:
                            pass  # ENTRY_NOT_FILLED → direct to close block C)
                        elif past_expiry:
                            # Study geometry: hard timeout at horizon end → exit on close
                            is_closed = True
                            close_reason = "HORIZON_TIMEOUT"
                            close_price = current_price
                        elif targets is None:
                            # LEGACY: simple % thresholds against close (no level info available)
                            if direction == "LONG":
                                pnl_pct = (current_price - entry) / entry * 100
                            else:
                                pnl_pct = (entry - current_price) / entry * 100

                            if pnl_pct >= 2.5:
                                is_closed = True
                                close_reason = "LEGACY TARGET HIT (+2.5%)"
                            elif pnl_pct <= -5.0:
                                is_closed = True
                                close_reason = "LEGACY FALLBACK SL (-5.0%)"

                        # B) MODERN TRADES (WITH TARGETS AND SL) — wick-aware
                        else:
                            if direction == "LONG":
                                # SL: LONG stopped if low below SL
                                if sl_state is not None and candle_low <= float(sl_state):
                                    is_closed = True
                                    close_reason = f"SL hit (SL: {sl_state})"
                                    close_price = float(sl_state)
                                elif tp_allowed:
                                    # TPs: LONG TP triggered if high above target
                                    for i in range(new_targets_hit, len(targets)):
                                        if candle_high >= float(targets[i]):
                                            new_targets_hit = i + 1
                                            if new_targets_hit == 1:
                                                new_sl = entry
                                            elif new_targets_hit > 1:
                                                new_sl = targets[new_targets_hit - 2]
                                        else:
                                            break
                                    if new_targets_hit == len(targets):
                                        is_closed = True
                                        close_reason = "ALL TARGETS HIT"
                                        close_price = float(targets[-1])

                            elif direction == "SHORT":
                                # SL: SHORT stopped if high above SL
                                if sl_state is not None and candle_high >= float(sl_state):
                                    is_closed = True
                                    close_reason = f"SL hit (SL: {sl_state})"
                                    close_price = float(sl_state)
                                elif tp_allowed:
                                    # TPs: SHORT TP triggered if low below target
                                    for i in range(new_targets_hit, len(targets)):
                                        if candle_low <= float(targets[i]):
                                            new_targets_hit = i + 1
                                            if new_targets_hit == 1:
                                                new_sl = entry
                                            elif new_targets_hit > 1:
                                                new_sl = targets[new_targets_hit - 2]
                                        else:
                                            break
                                    if new_targets_hit == len(targets):
                                        is_closed = True
                                        close_reason = "ALL TARGETS HIT"
                                        close_price = float(targets[-1])

                        # C) EXECUTE DATABASE UPDATES
                        if is_closed:
                            # T-2026-KYT-9050-150: stamp the close with the 5m candle
                            # that triggered it. NOW() booked wall-clock time, which is
                            # wrong by one poll gap in normal operation and by the whole
                            # downtime after a restart.
                            close_ts = c_ot.astimezone(pytz.UTC).replace(tzinfo=None)
                            pnl = (
                                (close_price - entry) / entry * 100
                                if direction == "LONG"
                                else (entry - close_price) / entry * 100
                            )

                            # FIX P2.8: DELETE ... RETURNING first — the insert into
                            # closed table only runs if WE actually removed the row.
                            # Otherwise two iterations/processes write the same trade
                            # twice to closed_ai_signals. Both in the same transaction
                            # (batch commit below).
                            cur.execute("DELETE FROM ai_signals WHERE id = %s RETURNING id", (trade_id,))
                            if cur.fetchone() is not None:
                                logger.info(
                                    f"🔒 AI trade {symbol} ({model}) closed! Reason: {close_reason} | PnL: {pnl:.2f}%"
                                )
                                # T-2026-CU-9050-115: persist targets + lev on close
                                # (basis of realized PnL report in bot 23).
                                # lev = same cap as at post sites (get_max_leverage(symbol, 20));
                                # bots with different post leverage (UFI1: SL-capped,
                                # P0.6/R4) get NULL instead of wrong 20x — report
                                # excludes NULL rows. Source: value frozen in
                                # ai_signals.lev on first poll (drift protection, see
                                # migration above). Fallback to close-time cap only for
                                # trades already open at deploy and closing in same
                                # iteration (bounded, transitional).
                                lev_text = trade_lev or stamped_lev.get(trade_id)
                                if lev_text is None and has_standard_leverage(model):
                                    lev_text = get_max_leverage(symbol, 20)
                                try:
                                    # Defensive: a corrupted target element must not
                                    # pull the close path into a 10s crash loop — NULL
                                    # means "row falls out of report" (like legacy), close
                                    # itself goes through.
                                    targets_json = json.dumps([float(t) for t in targets]) if targets else None
                                except (TypeError, ValueError):
                                    targets_json = None
                                cur.execute(
                                    """
                                    INSERT INTO closed_ai_signals (symbol, model, direction, entry, close_price, targets_hit, open_time, close_time, status, targets, lev)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                                    (
                                        symbol,
                                        model,
                                        direction,
                                        float(entry),
                                        float(close_price),
                                        int(new_targets_hit),
                                        open_time,
                                        close_ts,
                                        close_reason,
                                        targets_json,
                                        lev_text,
                                    ),
                                )
                                db_was_changed = True
                            else:
                                logger.warning(
                                    f"⚠️ AI trade {trade_id} ({symbol}) already closed — double-close prevented."
                                )
                            # Release watermark of closed trade
                            last_checked.pop(trade_id, None)

                        elif targets is not None and new_targets_hit > hit_state:
                            logger.info(
                                f"🎯 AI trade {symbol} ({model}) hit target {new_targets_hit}! SL moved to {new_sl:.6f}."
                            )
                            cur.execute(
                                """
                                UPDATE ai_signals
                                SET current_target_hit = %s, sl = %s
                                WHERE id = %s
                            """,
                                (new_targets_hit, new_sl, trade_id),
                            )
                            db_was_changed = True
                            hit_state = new_targets_hit
                            sl_state = new_sl

                        # === NEW: EXECUTE BATCH COMMIT ===
                        if db_was_changed:
                            updates_pending += 1
                            if updates_pending >= BATCH_SIZE:
                                conn.commit()
                                logger.info(f"💾 batch commit: {BATCH_SIZE} trades saved to database (memory cleared).")
                                updates_pending = 0

                        if is_closed:
                            break

            # Final commit for remaining trades (e.g. just 12, didn't reach 50 threshold)
            if updates_pending > 0:
                conn.commit()

            # T-2026-KYT-9050-150: the pass is through and every live trade carries a
            # watermark now - catch-up must not re-arm on the next iteration.
            if catchup_from is not None:
                logger.info("cold-start catch-up done - back to incremental scoring.")
                catchup_from = None

            # Persist the pass end (throttled) so the next cold start can replay the
            # gap. Written only after a clean pass; a crashed pass keeps the older
            # watermark, which replays a bit more rather than losing it.
            if time.monotonic() - _last_state_write >= STATE_WRITE_INTERVAL_S:
                atomic_write_json(MONITOR_STATE_FILE, {"last_pass_utc": now_utc.isoformat()})
                _last_state_write = time.monotonic()

        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error(f"Error in AI trade monitor: {e}")
            # FIX: on DB error rebuild connection instead of continuing with dead one.
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
        logger.info("🛑 AI trade monitor bot stopped manually (Ctrl+C). Shutting down cleanly...")
