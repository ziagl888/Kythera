# 42_ai_ods1_bot.py — ODS1: short the rally that open interest did not pay for.
"""OI divergence short (T-2026-KYT-9050-106, operator decision Michi 2026-08-05).

A rally whose open interest *fell* is short covering, not new money. When the
squeeze runs out of shorts to squeeze there is nothing left holding the price up,
and it mean-reverts. That is the only one of three OI mechanics that survived
``staging_models/replay/oi_event_study_t096.md``:

  DIVERGENCE SHORT (px >= +3 %, 4h OI <= -2 %)
      net +0.41/event @1h (t=3.2), +0.73 @4h, WR 58-61 %, n=580,
      8 of 9 weeks positive, monotone in threshold strictness
  DIVERGENCE LONG   — dead across every variant
  SPIKE-FADE        — refuted; fading a fresh OI build-up gets run over (-2.56 @24h)
  OI x FUNDING      — refuted at the pre-registered thresholds

This bot stands on T-096 ALONE
------------------------------
An earlier version of this docstring cited T-2026-KYT-9050-104 as an independent
reproduction from the other side (bucketing the fleet's existing short signals by
the same 4h OI change: bottom quintile +0.739 / +0.552 pp against +0.21..+0.33
and -0.12..+0.04 in the rest). **That support is withdrawn on two counts**, both
measured on 2026-08-06 while reviewing PR #274:

  * Not independent in time. T-104's replay window starts 2026-07-11
    (`reports/leg_composition_replay.json` -> `export_meta.since`), so T-096's
    window (06-12 -> 08-04) *contains* it. The handover's "T-104 ran 13.06.-05.08."
    was wrong. Two studies on the same tape agree across populations, never across
    time.
  * Look-ahead in the feature itself. T-104 reads signal instants from
    `closed_ai_signals.open_time`, a naive column, `AT TIME ZONE 'UTC'`. Measured
    per model against the 5m candle the entry must fall inside: EPD3 95.0 % vs
    11.7 %, BR1Hv2 40.7 % vs 10.9 %, MIS1-72H 59.6 % vs 11.5 % all favour
    Bucharest (+3h); only ROM1 is real naive UTC (86.8 % vs 8.0 %). 84 % of the
    SHORT population feeding that gate is therefore stamped 3h late, so its
    "4h OI change before the signal" actually spans [t-1h, t+3h] — it straddles
    the signal and contains post-signal OI. An OI drop measured partly *after*
    entry can be positions closing, i.e. consequence rather than cause.

Also corrected: "the only finding in that study that survived both regime
cohorts" was false. At T-104's own TP3/SL2, 18 legs are sign-stable positive in
both cohorts (5 under the n>=40 filter its own section 4 applies), including
EPD3-SHORT on n=6864/2352.

What remains is T-096: one study, one tape, its own >=90d regime gate unmet. The
bot going live is the deliberate operator substitute for that missing coverage —
forward data on a new tape — and is now explicitly a one-pillar bet, not a
replicated one.

The geometry is the weakest part of this file
---------------------------------------------
The study measured **horizon returns on implied prices with no stop** — a mean
drift of +0.41 % at 1h and +0.73 % at 4h. That is not a 3 % move, so the fleet's
usual bracket would sit far outside the effect and the edge would leak away long
before TP1. The bracket below is therefore sized *to the measured effect*, not
inherited from the fleet default and not tuned: TP1 1.0 %, TP2 2.0 %, SL 2.0 %.
Nothing in T-096 validates those three numbers — they are the smallest honest
translation of a drift into a bracket, and they are the first thing to re-derive
once this bot has live rows of its own.

Sizing the bracket to the effect is right; it does NOT license ignoring how the
ladder is executed. TP2 shipped at 1.5 % and was corrected to 2.0 % inside the
first hour of live trading, because at a 0.5 % gap the second rung never fired
separately — Cornix splits the size 50/50 and both rungs resolved in the same
move. The measured effect sets where TP1 goes; the venue sets how far apart the
rungs have to be to *be* rungs. See `MIN_TP_GAP_PCT`.

The exit that actually matches the measurement is a **time stop**, which Cornix
cannot express but ``40_trailing_close_bot.py`` can (``TIME_STOP_H``, default
24 h). That is the substantive reason this leg belongs in the trailing channel and
not only in its own — see ``core/trailing_roster``.

Operational dependencies
------------------------
* ``oi_5m`` is not a 5-minute table. Measured 2026-08-05: median cadence 5.0 min
  until 2026-07-06, **10.0 min from 2026-07-13 onward** (p90 20 min) — the
  collector degraded and stayed degraded (T-2026-KYT-9050-097). Every lookup here
  is as-of with a hard staleness cap, and a stale point is **voided, never
  forward-filled**: filling would manufacture exactly the OI change being traded.
* If the collector stalls, this bot goes quiet — and it has to say so. AIM2-TOPN
  ran gated live for 24 days with zero rows and zero log lines before anyone
  looked (T-2026-KYT-9050-101). Hence ``_log_starvation``.

Posting: ``CH_ODS1`` with fallback to ``CH_NEW_IDEAS`` — the cohort channel is the
test environment (operator decision 2026-07-06, OPUS-HANDOFF §5), and
``NEW_IDEAS_LIVE_POSTING`` defaults to 1 like the rest of the cohort, so this bot
is live on deploy by design rather than by omission.

Rule 5 note: no candles in the DECISION path. Price comes from the same ``oi_5m``
rows as the OI itself (implied mark = ``oi_value_usdt / open_interest``), which is
what the study measured — joining candles would compare two different clocks. The
POSTING path deliberately uses two other price sources, neither of which re-enters
the entry rule: ``post_ai_signal`` renders a 240-minute mini-chart per signal
(display-only), and the entry anchor is the live ticker at posting time (see
``emit`` and ``DRIFT_CONSUMED_FRAC_OF_TP1``). "Reads no candles" flat would be
false, and so would "decides on the ticker".

Watchdog: start_delay=283.
"""

import datetime
import logging
import os
import time
from typing import TypedDict

from core import config as _kcfg
from core.database import get_db_connection
from core.live_price import get_live_price, get_live_prices_batch
from core.market_utils import get_max_leverage
from core.signal_post import has_open_ai_signal, post_ai_signal_gated
from core.time import utc_now

logging.basicConfig(level=logging.INFO, format="%(asctime)s - ODS1_BOT - %(message)s")
logger = logging.getLogger(__name__)

MODEL_ID = "ODS1"
TARGET_CHANNEL_ID = _kcfg.CH_ODS1
LIVE_POSTING = os.getenv("NEW_IDEAS_LIVE_POSTING", "1") == "1"

# ── entry rule, verbatim from the T-096 operating point ──────────────────────
PX_RALLY_PCT = 3.0  # rally over the lookback that qualifies
OI_DROP_PCT = -2.0  # 4h OI change that makes it a squeeze rather than a trend
LOOKBACK_S = 4 * 3600
COOLDOWN_H = 24  # per symbol, first event wins — the study deduped the same way
MIN_OI_USDT = 3_000_000  # study universe: median OI >= $3M

# ── geometry — see module docstring, the least-supported part of this file ────
#
# TP2 widened 1.5 -> 2.0 on 2026-08-06 (operator decision Michi), within the first
# hour of go-live. At 1.0/1.5 the two rungs sat 0.5 % apart, and Cornix splits the
# position 50/50 across the ladder — so the second rung was not a separate event:
# whoever reached TP1 took TP2 in the same move. Two of the first four live trades
# closed "ALL TARGETS HIT" within minutes. The leg was effectively trading a single
# target at ~1.25 % while the book recorded a full ladder success, which biases the
# very live evaluation this bot went live to produce.
#
# Measured across the fleet on signals since 2026-08-04, ODS1 was the ONLY leg
# under a 1 % minimum gap (and the smallest TP1 of all):
#     EPD3 2.63 %/1.82 %  ROM1 3.09 %/2.01 %  ATS2 2.02 %/1.38 %
#     TSM1 2.00 %/1.24 %  AIM2 7.21 %/3.67 %     (TP1 distance / min gap)
#
# TP1 stays on the measured T-096 drift (+0.41 % @1h, +0.73 % @4h) — that number is
# the reason this bracket is tight at all. TP2 at 2.0 % equals the SL distance, so
# the second rung is a symmetric 1R. `MIN_TP_GAP_PCT` pins the rule that was
# missing: the old guards checked the ceiling and the ordering, never the spacing.
TP_PCTS = (1.0, 2.0)
SL_PCT = 2.0
MIN_TP_GAP_PCT = 1.0  # fleet convention; tightest compliant leg is TSM1 at 1.24 %

# ── the entry anchor: posting time, not detection time ───────────────────────
#
# The DECISION stays on the oi_5m clock (above). The GEOMETRY does not, and until
# T-2026-KYT-9050-115 it did: the bracket hung off `cand["price"]`, the implied
# mark of the newest OI point at or before now. `_as_of` accepts that point up to
# STALENESS_CAP_S old, and the collector's cadence is a 10-min median / 20-min p90
# (T-097), so the anchor was typically 0-10 min stale and could be 45 min stale.
#
# TP1 is 1.0 %. On exactly the tape this bot selects (+3 % over 4 h) a 10-minute-old
# anchor can sit 0.5-1 % away — half to all of the TP1 distance. A bracket that is a
# pure PERCENTAGE translation of a measured drift has to hang off the price the
# position is actually opened at, or the posted risk/reward is not the one that was
# priced.
#
# Bot 40 made the same correction on 2026-07-27 (operator decision Michi): of 24
# mirrors 5 filled, and for 15 of 18 cancellations the market never touched the
# posted entry — the arm traded a selection it had created itself. What differs is
# what moves along: bot 40 re-anchors only the ENTRY and keeps the source's SL and
# targets at their ABSOLUTE prices, because those are S/R levels (`mirrorable_at`).
# ODS1's rungs are not levels, so entry and bracket are re-anchored together.
#
# How much of the measured drift may already be behind us. T-096 priced a horizon
# return FROM the event instant and priced no entry delay, so this number is NOT
# measured — it is a bound on how far the trade may drift from the one that was
# studied before it stops being that trade. Expressed as a fraction of TP1 so it
# stays tied to the geometry it protects: 0.5 x 1.0 % = 0.50 % here.
#
# It sits far above the measurement noise between the two price sources it
# compares: the implied mark (oi_value_usdt / open_interest) and the ticker's last
# price are two views of the same number, separated by the mark-vs-last basis,
# typically well under 0.1 %.
#
# Skips are counted and logged, never silent.
DRIFT_CONSUMED_FRAC_OF_TP1 = float(os.getenv("ODS1_DRIFT_CONSUMED_FRAC", "0.5"))

POLL_SECONDS = 300
STALENESS_CAP_S = 45 * 60
STARVATION_LOG_EVERY_S = 3600

# Flood protection. The entry rule describes a MARKET-WIDE event: a BTC-led rally
# that liquidates shorts satisfies "px >= +3 % and 4h OI <= -2 %" on dozens of
# correlated alts in the same 5-minute poll. Without a bound one cycle can emit
# the whole qualifying universe (527 symbols in coins.json) into a Cornix-executed
# channel — and because ODS1 holds a roster seat, bot 40 mirrors each signal into
# CH_TRAILING as well, so the burst lands in TWO channels against a per-channel
# cap of 500. EPD3-SHORT was estimated low once and delivered ~484/day.
#
# `find_candidates` already sorts strictest-divergence-first, so truncation keeps
# the events T-096 measured as the strongest rather than an arbitrary subset.
# Same shape as `40_trailing_close_bot.TIME_STOP_MAX_PER_CYCLE`.
MAX_EMITS_PER_CYCLE = int(os.getenv("ODS1_MAX_EMITS_PER_CYCLE", "5"))


class Candidate(TypedDict):
    """One qualifying divergence event.

    A TypedDict rather than a bare dict so the ranking key below stays typed:
    on a plain ``dict`` the values widen to ``object`` and ``oi_chg - px_chg``
    is not a supported operation.
    """

    symbol: str
    price: float
    px_chg: float
    oi_chg: float


def _as_of(rows: list[tuple[int, float, float]], t: int) -> tuple[float, float] | None:
    """(open_interest, implied_price) at or before ``t``, or None if too stale.

    Voiding beats filling: an interpolated OI point would manufacture the very
    divergence this bot trades (P0.12 — stale rows are voided, never filled).
    """
    best = None
    for ts, oi, px in rows:
        if ts > t:
            break
        best = (ts, oi, px)
    if best is None or t - best[0] > STALENESS_CAP_S:
        return None
    return best[1], best[2]


def load_oi_window(conn, since_epoch: int) -> dict[str, list[tuple[int, float, float]]]:
    """Per symbol, the OI series with its implied mark price, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT symbol, extract(epoch FROM ts)::bigint, open_interest, oi_value_usdt
                 FROM oi_5m
                WHERE ts >= to_timestamp(%s) AND open_interest > 0 AND oi_value_usdt > 0
                ORDER BY symbol, ts""",
            (since_epoch,),
        )
        rows = cur.fetchall()
    series: dict[str, list[tuple[int, float, float]]] = {}
    for symbol, ts, oi, value in rows:
        series.setdefault(symbol, []).append((int(ts), float(oi), float(value) / float(oi)))
    return series


def on_cooldown(conn, symbol: str, now: datetime.datetime) -> bool:
    """A second event on the same symbol inside COOLDOWN_H is the same idea twice.

    Checks both tables because the monitor moves a trade out of ``ai_signals``
    when it closes — looking only at the open book would let a fast round trip
    re-fire within the cooldown.
    """
    cutoff = now - datetime.timedelta(hours=COOLDOWN_H)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM ai_signals WHERE model = %s AND symbol = %s AND timestamp > %s LIMIT 1",
            (MODEL_ID, symbol, cutoff),
        )
        if cur.fetchone():
            return True
        cur.execute(
            "SELECT 1 FROM closed_ai_signals WHERE model = %s AND symbol = %s AND open_time > %s LIMIT 1",
            (MODEL_ID, symbol, cutoff.replace(tzinfo=None)),
        )
        return cur.fetchone() is not None


def find_candidates(series: dict[str, list[tuple[int, float, float]]], now_epoch: int) -> tuple[list[Candidate], int]:
    """Symbols where price rallied while open interest drained.

    Returns ``(candidates, usable)`` where ``usable`` counts the symbols whose
    BOTH endpoints survived the staleness cap. The caller needs that number and
    not ``len(series)``: a symbol with a single 3-hour-old row is present in
    ``series`` but contributes nothing, so reporting the dict size would claim
    fresh coverage during exactly the collector outage this bot must announce.
    """
    out: list[Candidate] = []
    usable = 0
    for symbol, rows in series.items():
        now = _as_of(rows, now_epoch)
        then = _as_of(rows, now_epoch - LOOKBACK_S)
        if now is None or then is None:
            continue
        usable += 1
        oi_now, px_now = now
        oi_then, px_then = then
        if oi_then <= 0 or px_then <= 0 or oi_now * px_now < MIN_OI_USDT:
            continue
        px_chg = (px_now / px_then - 1.0) * 100.0
        oi_chg = (oi_now / oi_then - 1.0) * 100.0
        if px_chg >= PX_RALLY_PCT and oi_chg <= OI_DROP_PCT:
            out.append({"symbol": symbol, "price": px_now, "px_chg": px_chg, "oi_chg": oi_chg})
    # Strictest divergence first. T-096's threshold matrix was monotone in
    # strictness, so when the channel is busy the most extreme events are the
    # ones worth a slot.
    out.sort(key=lambda c: c["oi_chg"] - c["px_chg"])
    return out, usable


_last_starvation_log = 0.0


def _log_starvation(usable: int, loaded: int) -> None:
    """Say something when nothing qualifies — silence is how AIM2-TOPN hid for 24 days.

    Both numbers matter and they mean different things. ``usable`` is the count
    that survived the staleness cap on BOTH endpoints; ``loaded`` is how many
    symbols the window query returned at all. ``usable`` collapsing while
    ``loaded`` stays high IS the collector-outage signature — reporting only the
    latter would print reassuring coverage during the failure this log exists to
    surface.
    """
    global _last_starvation_log
    if time.time() - _last_starvation_log < STARVATION_LOG_EVERY_S:
        return
    _last_starvation_log = time.time()
    logger.info(
        f"ODS1: 0 candidates — {usable} of {loaded} symbols had two OI points inside the "
        f"{STALENESS_CAP_S // 60}min staleness cap "
        f"(rule: px >= +{PX_RALLY_PCT}% AND 4h OI <= {OI_DROP_PCT}%). "
        f"If usable stays far below loaded, check 35_oi_collector — its cadence is a known defect (T-097)."
    )


def drift_consumed_pct(decision_price: float, market: float) -> float:
    """How much of the measured drift is already behind us, in percent.

    Positive means the market has moved the way the trade wants to go — for a
    SHORT, that the price has already fallen since the OI point the rule fired
    on, so part of the +0.41 %/@1h this bot exists to capture is spent.
    """
    return (decision_price / market - 1.0) * 100.0


def max_drift_pct() -> float:
    """The consumed-drift bound as an absolute percentage (see the constant)."""
    return DRIFT_CONSUMED_FRAC_OF_TP1 * TP_PCTS[0]


def entry_anchor(conn, symbol: str) -> float | None:
    """The live price at posting time, or None when it cannot be established.

    ``conn`` is accepted for symmetry with the rest of the cycle but is
    deliberately NOT handed to ``get_live_price``: its DB fallback calls
    ``conn.rollback()`` on a query error, which is connection-wide and would
    discard every signal this cycle already wrote before the single commit at the
    end. Losing the fallback costs one skipped candidate; taking it could cost the
    whole batch. HTTP-only is the cheaper failure.
    """
    price = get_live_price(symbol)
    return float(price) if price else None


def emit(conn, cand: Candidate, market: float) -> bool:
    """Post one divergence short, anchored at ``market``. Returns whether the gate
    actually emitted.

    ``market`` is the live price at POSTING time — see the anchor rationale above
    ``DRIFT_CONSUMED_FRAC_OF_TP1``. The whole bracket is derived from it, so what
    goes out is the geometry the position actually opens into rather than the one
    the OI point implied minutes ago.

    The return value is not cosmetic: ``post_ai_signal_gated`` returns falsy for a
    SILENT/RETIRED leg or a shadow dedup, and counting those as emissions would
    both overstate the cycle log and commit an empty transaction.
    """
    targets = [market * (1 - p / 100.0) for p in TP_PCTS]
    sl = market * (1 + SL_PCT / 100.0)
    drift = drift_consumed_pct(cand["price"], market)
    lev = get_max_leverage(cand["symbol"], 20)
    posted = post_ai_signal_gated(
        conn,
        tag=MODEL_ID,
        direction="SHORT",
        channel_id=TARGET_CHANNEL_ID if LIVE_POSTING else 0,
        symbol=cand["symbol"],
        confidence=min(0.99, abs(cand["oi_chg"]) / 10.0),
        entry1=market,
        entry2=market,
        sl=sl,
        targets=targets,
        source_desc=f"OI divergence (px {cand['px_chg']:+.1f}%, 4h OI {cand['oi_chg']:+.1f}%)",
        n_show=len(TP_PCTS),
        extra_info_lines=[
            f"Rally {cand['px_chg']:+.1f}% on {cand['oi_chg']:+.1f}% open interest — short covering, not new money",
            f"Entry anchored at posting time — {drift:+.2f}% of the drift already run since the OI point",
            f"Leverage: {lev}",
        ],
    )
    if posted:
        logger.info(
            f"✅ ODS1 SHORT {cand['symbol']} @ {market:.8f} (px {cand['px_chg']:+.1f}%, "
            f"OI {cand['oi_chg']:+.1f}%, drift {drift:+.2f}%)"
        )
    return bool(posted)


def run_cycle(conn) -> None:
    now = utc_now()
    now_epoch = int(now.timestamp())
    series = load_oi_window(conn, now_epoch - LOOKBACK_S - STALENESS_CAP_S)
    candidates, usable = find_candidates(series, now_epoch)
    if not candidates:
        _log_starvation(usable, len(series))
        return
    # One batch call per cycle with candidates, not one per symbol (P2.44). Fetched
    # here rather than at the top of the cycle because ODS1 polls every 300 s and
    # most cycles qualify nobody — an unconditional ticker call would be pure cost.
    prices = get_live_prices_batch()
    if not prices:
        # Not fatal: the per-candidate fallback resolves an anchor over HTTP, and the
        # emit cap bounds that to at most MAX_EMITS_PER_CYCLE calls. Logged anyway —
        # a persistent batch outage changes the cost profile of every cycle, and a
        # price path that quietly degrades is how a bot stops posting unnoticed.
        logger.warning("ODS1: batch ticker returned nothing — falling back to per-symbol price lookups")

    emitted = 0
    suppressed = 0
    chased = 0
    unpriced = 0
    for idx, cand in enumerate(candidates):
        if emitted >= MAX_EMITS_PER_CYCLE:
            # Candidates are sorted strictest-first, so what is dropped here is
            # the weakest divergence of the burst, not an arbitrary tail. Logged
            # rather than silent: a cap that truncates without saying so reads as
            # "the market only offered five" (no silent caps). The dropped ones
            # are deferred, not lost — the 4h divergence persists across polls and
            # they leave no cooldown row, so they re-qualify next cycle.
            suppressed = len(candidates) - idx
            break
        if has_open_ai_signal(conn, cand["symbol"], "SHORT", MODEL_ID):
            continue
        if on_cooldown(conn, cand["symbol"], now):
            continue
        # The anchor is resolved AFTER the open/cooldown filters so the HTTP
        # fallback is only ever paid for a candidate that would really post.
        market = prices.get(cand["symbol"]) or entry_anchor(conn, cand["symbol"])
        if not market or market <= 0:
            # No anchor, no post. Voiding beats falling back to the OI-implied
            # price: that is precisely the stale number this change removed, and
            # reintroducing it on the error path would make the defect intermittent
            # instead of gone.
            unpriced += 1
            continue
        drift = drift_consumed_pct(cand["price"], market)
        if drift >= max_drift_pct():
            # The move already happened between the OI point and now. Entering here
            # buys the tail of an effect that was measured from its start.
            chased += 1
            continue
        # SAVEPOINT per candidate. A bare conn.rollback() here would be
        # connection-wide and discard every signal already written this cycle —
        # the opposite of isolating the failure. psycopg2 also leaves the
        # connection in INERROR after a failed statement, so SOMETHING must undo
        # it before the next candidate can write; the savepoint undoes exactly
        # the failed one.
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT ods1_cand")
        try:
            if emit(conn, cand, market):
                emitted += 1
            with conn.cursor() as cur:
                cur.execute("RELEASE SAVEPOINT ods1_cand")
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not void the batch
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT ods1_cand")
            logger.error(f"ODS1: emit failed for {cand['symbol']}: {exc}")
    if emitted:
        conn.commit()  # the caller commits (hard rule 8)
    logger.info(
        f"ODS1 cycle: {len(candidates)} candidates, {emitted} emitted, "
        f"{suppressed} over the per-cycle cap of {MAX_EMITS_PER_CYCLE}, "
        f"{chased} with >{max_drift_pct():.2f}% of the drift already run, "
        f"{unpriced} without a live anchor, "
        f"{usable} of {len(series)} symbols with fresh OI"
    )


def main() -> None:
    logger.info(
        f"ODS1 start — channel={TARGET_CHANNEL_ID} live={LIVE_POSTING} "
        f"rule px>=+{PX_RALLY_PCT}% & 4h OI<={OI_DROP_PCT}% · TP {TP_PCTS} SL {SL_PCT}% · "
        f"entry anchored at posting time, skip above {max_drift_pct():.2f}% consumed drift · "
        f"cooldown {COOLDOWN_H}h · staleness cap {STALENESS_CAP_S // 60}min"
    )
    if not LIVE_POSTING:
        logger.warning("ODS1: NEW_IDEAS_LIVE_POSTING=0 → shadow-only, no outbox rows.")
    while True:
        conn = None
        try:
            conn = get_db_connection()
            run_cycle(conn)
        except Exception as exc:  # noqa: BLE001 — a bot must survive one bad cycle
            logger.error(f"ODS1 cycle failed: {exc}")
            if conn is not None:
                conn.rollback()
        finally:
            if conn is not None:
                conn.close()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
