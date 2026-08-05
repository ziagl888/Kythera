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

Independently reproduced from the other side in T-2026-KYT-9050-104: bucketing
the fleet's *existing* short signals by the same 4h OI change puts the bottom
quintile at +0.739 / +0.552 pp against +0.21..+0.33 and -0.12..+0.04 in the rest
— the only finding in that study that survived both regime cohorts.

What that replication does NOT buy
----------------------------------
T-096 ran 2026-06-12 -> 08-04 and T-104 2026-06-13 -> 08-05: near-total overlap.
The two lines agree across *populations*, not across *time*, so the regime
coverage that T-096's own >=90d gate existed to provide is still missing. This
bot going live is the substitute — forward data on a new tape beats a second
backtest on the old one — and that is a deliberate operator trade, not a claim
that the evidence is complete.

The geometry is the weakest part of this file
---------------------------------------------
The study measured **horizon returns on implied prices with no stop** — a mean
drift of +0.41 % at 1h and +0.73 % at 4h. That is not a 3 % move, so the fleet's
usual bracket would sit far outside the effect and the edge would leak away long
before TP1. The bracket below is therefore sized *to the measured effect*, not
inherited from the fleet default and not tuned: TP1 1.0 %, TP2 1.5 %, SL 2.0 %.
Nothing in T-096 validates those three numbers — they are the smallest honest
translation of a drift into a bracket, and they are the first thing to re-derive
once this bot has live rows of its own.

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

Rule 5 note: this bot reads no candles. Price comes from the same ``oi_5m`` rows as
the OI itself (implied mark = ``oi_value_usdt / open_interest``), which is what the
study measured — joining candles would compare two different clocks.

Watchdog: start_delay=283.
"""

import datetime
import logging
import os
import time

from core import config as _kcfg
from core.database import get_db_connection
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
TP_PCTS = (1.0, 1.5)
SL_PCT = 2.0

POLL_SECONDS = 300
STALENESS_CAP_S = 45 * 60
STARVATION_LOG_EVERY_S = 3600


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


def find_candidates(series: dict[str, list[tuple[int, float, float]]], now_epoch: int) -> list[dict]:
    """Symbols where price rallied while open interest drained."""
    out = []
    for symbol, rows in series.items():
        now = _as_of(rows, now_epoch)
        then = _as_of(rows, now_epoch - LOOKBACK_S)
        if now is None or then is None:
            continue
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
    return out


_last_starvation_log = 0.0


def _log_starvation(scanned: int) -> None:
    """Say something when nothing qualifies — silence is how AIM2-TOPN hid for 24 days."""
    global _last_starvation_log
    if time.time() - _last_starvation_log < STARVATION_LOG_EVERY_S:
        return
    _last_starvation_log = time.time()
    logger.info(
        f"ODS1: 0 candidates from {scanned} symbols with fresh OI "
        f"(rule: px >= +{PX_RALLY_PCT}% AND 4h OI <= {OI_DROP_PCT}%). "
        f"If this persists, check 35_oi_collector — its cadence is a known defect (T-097)."
    )


def emit(conn, cand: dict) -> None:
    price = cand["price"]
    targets = [price * (1 - p / 100.0) for p in TP_PCTS]
    sl = price * (1 + SL_PCT / 100.0)
    lev = get_max_leverage(cand["symbol"], 20)
    posted = post_ai_signal_gated(
        conn,
        tag=MODEL_ID,
        direction="SHORT",
        channel_id=TARGET_CHANNEL_ID if LIVE_POSTING else 0,
        symbol=cand["symbol"],
        confidence=min(0.99, abs(cand["oi_chg"]) / 10.0),
        entry1=price,
        entry2=price,
        sl=sl,
        targets=targets,
        source_desc=f"OI divergence (px {cand['px_chg']:+.1f}%, 4h OI {cand['oi_chg']:+.1f}%)",
        n_show=len(TP_PCTS),
        extra_info_lines=[
            f"Rally {cand['px_chg']:+.1f}% on {cand['oi_chg']:+.1f}% open interest — short covering, not new money",
            f"Leverage: {lev}",
        ],
    )
    if posted:
        logger.info(
            f"✅ ODS1 SHORT {cand['symbol']} @ {price:.8f} (px {cand['px_chg']:+.1f}%, OI {cand['oi_chg']:+.1f}%)"
        )


def run_cycle(conn) -> None:
    now = utc_now()
    now_epoch = int(now.timestamp())
    series = load_oi_window(conn, now_epoch - LOOKBACK_S - STALENESS_CAP_S)
    candidates = find_candidates(series, now_epoch)
    if not candidates:
        _log_starvation(len(series))
        return
    emitted = 0
    for cand in candidates:
        if has_open_ai_signal(conn, cand["symbol"], "SHORT", MODEL_ID):
            continue
        if on_cooldown(conn, cand["symbol"], now):
            continue
        emit(conn, cand)
        emitted += 1
    if emitted:
        conn.commit()  # the caller commits (hard rule 8)
    logger.info(f"ODS1 cycle: {len(candidates)} candidates, {emitted} emitted, {len(series)} symbols with fresh OI")


def main() -> None:
    logger.info(
        f"ODS1 start — channel={TARGET_CHANNEL_ID} live={LIVE_POSTING} "
        f"rule px>=+{PX_RALLY_PCT}% & 4h OI<={OI_DROP_PCT}% · TP {TP_PCTS} SL {SL_PCT}% · "
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
