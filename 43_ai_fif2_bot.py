# 43_ai_fif2_bot.py — FIF2: mirror the fleet's signals, but only when the tape is fast.
"""Vol-gated ladder mirror (T-2026-KYT-9050-112, operator decision Michi 2026-08-06).

FIF2 is the successor idea to FIF1 (bot 33), rebuilt on the T-110/T-111 evidence
chain instead of the S11 model. It mirrors fresh fleet signals from ``ai_signals``
and reposts the ones whose symbol is currently VOLATILE — because volatility, not
market context, is what predicts a fast, TP1-favoured resolution:

* T-110 (PR #279): ``sym_vol_4h`` predicts fast TP1 at AUC 0.79 train / 0.77
  test, 5/5 weeks consistent; TP1-first RISES with vol (31 % -> 52 %) and
  overtakes SL-first in the top decile, while low-vol trades leave ~30 % of
  positions unresolved for the whole 72 h horizon. BTC/BTCDOM context: refuted.
* T-111 (PR #280): with money attached, the q80 gate holds out-of-sample
  (+0.19 pp/trade single-TP, t=2.7, median hold 0.7 h) and the LADDER beats a
  single TP in every gated cell (+0.314 vs +0.192 pp/trade at q80) — so this
  bot posts the ladder, not the operator's original 100 %-out-at-TP1 shape.

Geometry — from the study, not the fleet default
------------------------------------------------
The T-104/T-105/T-111 chain priced the ``t104`` bracket on the TP grid, so the
mirror posts exactly what was measured: LONG TP 4 % / 5 % with SL 5 %, SHORT
TP 3 % / 4 % with SL 2 %, two rungs, 50/50 tranches with stop-to-breakeven
after TP1. The source signal's own bracket is deliberately NOT reused: T-111
measured this one.

**Precondition the bot cannot enforce itself:** the measured ladder edge
(+0.314 vs +0.192 pp/trade at q80) is priced with the stop moving to breakeven
once TP1 is taken — ``tools/portfolio_backtest.precompute`` models that via
``_breakeven_step``, and it decides what the runner half is worth when price
comes back. This bot only posts the two rungs and the initial SL; the breakeven
move is **Cornix channel configuration**. Without it the runner carries the full
SL instead of 0, and the live geometry is not the one that was measured. Check
the channel's breakeven setting BEFORE any Cornix-on — today the FIF channel is
not Cornix-executed, so this is a go-live precondition, not a live defect.

The gate threshold is a rolling quantile, never a constant
----------------------------------------------------------
T-111's committed threshold (0.5712) is a train-window quantile, not a law of
nature. The bot maintains the trailing distribution of ``sym_vol_4h`` over every
candidate it evaluates (14 days, persisted via ``core.state_utils`` so restarts
do not reset it) and gates at the q80 of that distribution. Until the state
holds ``MIN_REFIT_N`` samples the bot posts NOTHING and says so hourly — a
threshold computed from thin air would be exactly the hardcoding T-111 warns
against. ``confidence`` on every post is the vol's trailing percentile.

Containment and gates (why live-by-default is safe here)
--------------------------------------------------------
Posts go to ``CH_FIF2`` -> fallback ``CH_FIF1`` (the FIF channel). That channel
is currently NOT Cornix-executed — FIF1 runs negative edge and the operator
turned the channel off in Cornix — so live posts here produce forward
measurement, not fills (operator decision 2026-08-06). Kill switches, any one
of which suffices: ``FIF2_LIVE_POSTING=0`` (shadow-only), ``CH_FIF2=0``
(shadow-only via the ``_ch_override`` contract), or a ``("FIF2", <dir>)``
``_LIFECYCLE`` entry in ``core/shadow_gate``. If FIF2 shows positive live edge,
feeding its trades into bot 40 (roster seat) is a separate operator decision.

Known honest risks, carried over from T-111 and re-checked before any Cornix-on:
the backtest fills at signal price while a vol gate selects exactly the fast
tapes where bot 40 measured 18/101 mirrors >1 % away within 15 min — the shadow
phase exists to measure that fill gap; and the decision window was ~8 days.

Rule 5: the vol feature reads CLOSED 5m candles only (``include_forming=False``)
through the shared builder ``core/vol_features`` — the same code path the T-110
study validated. Freshness is checked against the candle clock (tz-aware UTC,
written by ``1_data_ingestion``), and a stale series is voided, never filled.

Watchdog: start_delay=291.
"""

import json
import logging
import os
import time

import numpy as np

from core import config as _kcfg
from core import shadow_gate
from core.candles import history_start, read_candles
from core.database import get_db_connection
from core.market_utils import get_max_leverage
from core.signal_post import has_open_ai_signal, log_prediction, post_ai_signal_gated
from core.state_utils import atomic_write_json
from core.time import utc_now
from core.vol_features import CANDLE_5M_S, VOL_WINDOW_5M, vol_now_pct

logging.basicConfig(level=logging.INFO, format="%(asctime)s - FIF2_BOT - %(message)s")
logger = logging.getLogger(__name__)

MODEL_ID = "FIF2"  # new tag, never FIF1's (hard rule 6)
TARGET_CHANNEL_ID = _kcfg.CH_FIF2
LIVE_POSTING = os.getenv("FIF2_LIVE_POSTING", "1") == "1"

# ── gate — the T-111 operating point, refit rolling instead of hardcoded ─────
GATE_QUANTILE = 0.8
REFIT_WINDOW_S = 14 * 24 * 3600  # trailing distribution the quantile is taken over
MIN_REFIT_N = 500  # below this the bot is in warm-up and posts nothing
STATE_PATH = os.getenv("FIF2_STATE_PATH", "fif2_gate_state.json")

# ── geometry — the measured t104 bracket, two rungs (see module docstring) ───
TP_PCTS = {"LONG": (4.0, 5.0), "SHORT": (3.0, 4.0)}
SL_PCT = {"LONG": 5.0, "SHORT": 2.0}

# ── mirror mechanics ─────────────────────────────────────────────────────────
POLL_SECONDS = 60
MAX_MIRROR_AGE_S = 240  # bot-40 parity: a stale mirror fills a different market
VOL_STALE_TOLERANCE_S = 900  # newest closed candle must be at most this old
STARVATION_LOG_EVERY_S = 3600
# Flood protection, same rationale as ODS1: a market-wide vol regime qualifies
# dozens of correlated symbols in one poll (T-111 measured ~376 gated posts/day
# at q80). Candidates are sorted highest-vol-first, so the cap keeps the trades
# the studies scored strongest and drops the marginal tail — logged, not silent.
MAX_EMITS_PER_CYCLE = int(os.getenv("FIF2_MAX_EMITS_PER_CYCLE", "5"))


def ladder(direction: str, entry: float) -> tuple[list[float], float]:
    """The measured t104 bracket as absolute prices. Targets ordered for Cornix
    (ascending for LONG, descending for SHORT — nearest rung first)."""
    sign = 1.0 if direction == "LONG" else -1.0
    targets = [entry * (1 + sign * p / 100.0) for p in TP_PCTS[direction]]
    sl = entry * (1 - sign * SL_PCT[direction] / 100.0)
    return targets, sl


def gate_threshold(vols: list[float]) -> float | None:
    """q80 of the trailing candidate distribution; None while warming up."""
    if len(vols) < MIN_REFIT_N:
        return None
    return float(np.quantile(vols, GATE_QUANTILE))


def vol_percentile(vols: list[float], v: float) -> float:
    """Trailing percentile of `v` — the posted confidence. Capped below 1."""
    if not vols:
        return 0.0
    return min(0.99, float(np.mean(np.asarray(vols) < v)))


def trim_samples(samples: list[list[float]], now_epoch: float) -> list[list[float]]:
    """Drop samples older than the refit window; keeps [[epoch, vol], ...]."""
    cutoff = now_epoch - REFIT_WINDOW_S
    return [s for s in samples if s[0] >= cutoff]


def load_state() -> list[list[float]]:
    """The persisted trailing distribution; a missing/corrupt file is an empty
    start (warm-up), never a crash."""
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        samples = [[float(a), float(b)] for a, b in data.get("samples", [])]
        logger.info(f"FIF2: state loaded — {len(samples)} vol samples from {STATE_PATH}")
        return samples
    except FileNotFoundError:
        logger.info(f"FIF2: no state at {STATE_PATH} — cold start, warm-up begins")
        return []
    except Exception as exc:  # noqa: BLE001 — a corrupt state file must not kill the bot
        logger.error(f"FIF2: state file unreadable ({exc}) — cold start")
        return []


def fetch_fresh_signals(conn) -> list[dict]:
    """Fleet signals younger than the mirror bound, self-echo excluded.

    ``ai_signals`` is the open book (monitor bot 8 removes rows on close); age
    is computed by the DB because ``open_time`` is naive PG-local (TZ contract
    R3) — a Python "now" would re-import the +3h offset family. FIF1's rows are
    included on purpose: the T-111 study population contained every model tag,
    and mirroring what was measured beats editorialising the universe.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, symbol, model, direction, entry1, price,
                   EXTRACT(EPOCH FROM (NOW() - open_time)) AS age_sec
              FROM ai_signals
             WHERE model <> %s
               AND EXTRACT(EPOCH FROM (NOW() - open_time)) <= %s
            """,
            (MODEL_ID, MAX_MIRROR_AGE_S),
        )
        rows = cur.fetchall()
    out = []
    for sid, symbol, model, direction, entry1, price, age_sec in rows:
        entry = float(entry1) if entry1 is not None else (float(price) if price is not None else None)
        if entry is None or entry <= 0 or direction not in TP_PCTS:
            continue
        out.append(
            {
                "id": int(sid),
                "symbol": symbol,
                "model": model,
                "direction": direction,
                "entry": entry,
                "age_sec": float(age_sec),
            }
        )
    return out


def sym_vol_4h(conn, symbol: str) -> float | None:
    """The gate feature at serve time: newest CLOSED-candle rolling std.

    Same math as the T-110 study (shared builder). Voided — never filled — when
    the series is short or the newest closed candle is older than the tolerance
    (candle ``open_time`` is tz-aware UTC from ``1_data_ingestion``).
    """
    df = read_candles(
        conn,
        symbol,
        "5m",
        limit=VOL_WINDOW_5M + 1,
        start=history_start("5m", VOL_WINDOW_5M + 1),
        include_forming=False,
        columns=("open_time", "close"),
    )
    if len(df) < VOL_WINDOW_5M + 1:
        return None
    newest = df["open_time"].iloc[-1]
    age = (utc_now() - newest.to_pydatetime()).total_seconds()
    if age > CANDLE_5M_S + VOL_STALE_TOLERANCE_S:
        return None
    return vol_now_pct(df["close"].to_numpy())


_last_starvation_log = 0.0


def _log_quiet(reason: str) -> None:
    """Say something when nothing posts — silence is how AIM2-TOPN hid for 24 days."""
    global _last_starvation_log
    if time.time() - _last_starvation_log < STARVATION_LOG_EVERY_S:
        return
    _last_starvation_log = time.time()
    logger.info(f"FIF2: no emissions — {reason}")


def emit(conn, cand: dict, vol: float, confidence: float) -> bool:
    """Post one mirrored signal with the measured ladder. Returns whether the
    gate actually emitted (falsy for SILENT/RETIRED legs and shadow dedups)."""
    targets, sl = ladder(cand["direction"], cand["entry"])
    lev = get_max_leverage(cand["symbol"], 20)
    posted = post_ai_signal_gated(
        conn,
        tag=MODEL_ID,
        direction=cand["direction"],
        channel_id=TARGET_CHANNEL_ID if LIVE_POSTING else 0,
        symbol=cand["symbol"],
        confidence=confidence,
        entry1=cand["entry"],
        entry2=cand["entry"],
        sl=sl,
        targets=targets,
        source_desc=f"vol-gated mirror of {cand['model']} (sym_vol_4h {vol:.2f}%, p{int(confidence * 100)})",
        n_show=len(targets),
        extra_info_lines=[
            f"Source: {cand['model']} #{cand['id']} {cand['direction']}",
            f"4h realized vol {vol:.2f}% — trailing percentile {int(confidence * 100)}",
            f"Leverage: {lev}",
        ],
    )
    if posted:
        logger.info(
            f"✅ FIF2 {cand['direction']} {cand['symbol']} @ {cand['entry']:.8f} "
            f"(src {cand['model']} #{cand['id']}, vol {vol:.2f}%, p{int(confidence * 100)}, {posted})"
        )
    return bool(posted)


def run_cycle(conn, samples: list[list[float]], seen: dict[int, float], bootstrap: bool) -> None:
    """One poll: sample every fresh candidate's vol, gate, emit up to the cap.

    ``samples`` and ``seen`` are mutated in place. The distribution deliberately
    samples EVERY evaluated candidate (not only gate-passers) — the quantile
    must describe the candidate population, exactly as T-111 fit it on all
    signals of the trailing window.
    """
    now_epoch = time.time()
    for sid in [k for k, ts in seen.items() if now_epoch - ts > 4 * MAX_MIRROR_AGE_S]:
        del seen[sid]

    fresh = [c for c in fetch_fresh_signals(conn) if c["id"] not in seen]
    for c in fresh:
        seen[c["id"]] = now_epoch
    if not fresh:
        return

    # feature first, for every new candidate — the distribution feeds the gate
    evaluated: list[tuple[dict, float]] = []
    for cand in fresh:
        vol = sym_vol_4h(conn, cand["symbol"])
        if vol is None:
            continue  # voided, never filled — and never sampled either
        samples.append([now_epoch, vol])
        evaluated.append((cand, vol))
    samples[:] = trim_samples(samples, now_epoch)

    if bootstrap:
        # First cycle after start: the book already existed before this process;
        # posting it would mirror signals of unknowable freshness history.
        logger.info(f"FIF2: bootstrap cycle — {len(evaluated)} candidates sampled, none posted")
        return

    threshold = gate_threshold([v for _, v in samples])
    if threshold is None:
        _log_quiet(
            f"warm-up: {len(samples)}/{MIN_REFIT_N} vol samples in the trailing "
            f"{REFIT_WINDOW_S // 86400}d window — no threshold, no posts"
        )
        for cand, vol in evaluated:
            log_prediction(
                conn,
                MODEL_ID,
                cand["symbol"],
                cand["direction"],
                cand["entry"],
                vol_percentile([v for _, v in samples], vol),
                posted=False,
            )
        conn.commit()
        return

    vols = [v for _, v in samples]
    evaluated.sort(key=lambda cv: cv[1], reverse=True)  # strongest tape first
    emitted = suppressed = 0
    for cand, vol in evaluated:
        confidence = vol_percentile(vols, vol)
        passed = vol >= threshold
        if passed and emitted >= MAX_EMITS_PER_CYCLE:
            # sorted highest-vol-first: what is dropped is the weakest of the
            # burst, not an arbitrary subset (no silent caps — counted + logged).
            # Count up, never assign: an assignment inside the loop is overwritten
            # by every later candidate and ends at the remaining tail length (1),
            # which would under-report a 20-candidate burst as a single drop.
            suppressed += 1
            passed = False
        did_post = False
        if passed and not has_open_ai_signal(conn, cand["symbol"], cand["direction"], MODEL_ID):
            # SAVEPOINT per candidate (ODS1 pattern): one bad symbol must not
            # void the batch, and psycopg2 needs the failed statement undone
            # before the next candidate can write.
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT fif2_cand")
            try:
                did_post = emit(conn, cand, vol, confidence)
                with conn.cursor() as cur:
                    cur.execute("RELEASE SAVEPOINT fif2_cand")
            except Exception as exc:  # noqa: BLE001 — isolate the failure to the candidate
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK TO SAVEPOINT fif2_cand")
                logger.error(f"FIF2: emit failed for {cand['symbol']}: {exc}")
        emitted += did_post
        # every evaluated candidate leaves a prediction row — the below-gate
        # ones are the shadow measurement that lets the gate be re-calibrated
        # from ml_predictions_master without a DB migration
        log_prediction(conn, MODEL_ID, cand["symbol"], cand["direction"], cand["entry"], confidence, posted=did_post)
    conn.commit()  # the caller commits (hard rule 8)
    if emitted or suppressed:
        logger.info(
            f"FIF2 cycle: {len(evaluated)} evaluated, {emitted} emitted, {suppressed} over the "
            f"per-cycle cap of {MAX_EMITS_PER_CYCLE}, threshold {threshold:.4f} (q{int(GATE_QUANTILE * 100)}, "
            f"n={len(samples)})"
        )
    else:
        _log_quiet(f"{len(evaluated)} candidates below the q{int(GATE_QUANTILE * 100)} threshold {threshold:.4f}")


def main() -> None:
    live_leg = {d: shadow_gate.leg_status(MODEL_ID, d) for d in ("LONG", "SHORT")}
    logger.info(
        f"FIF2 start — channel={TARGET_CHANNEL_ID} live={LIVE_POSTING} legs={live_leg} · "
        f"gate q{int(GATE_QUANTILE * 100)} over trailing {REFIT_WINDOW_S // 86400}d (min n={MIN_REFIT_N}) · "
        f"ladder LONG {TP_PCTS['LONG']}/SL {SL_PCT['LONG']}% SHORT {TP_PCTS['SHORT']}/SL {SL_PCT['SHORT']}% · "
        f"mirror age <= {MAX_MIRROR_AGE_S}s · cap {MAX_EMITS_PER_CYCLE}/cycle"
    )
    if not LIVE_POSTING:
        logger.warning("FIF2: FIF2_LIVE_POSTING=0 → shadow-only, no outbox rows.")
    samples = load_state()
    seen: dict[int, float] = {}
    bootstrap = True
    while True:
        conn = None
        try:
            conn = get_db_connection()
            n_before = len(samples)
            run_cycle(conn, samples, seen, bootstrap)
            bootstrap = False
            if len(samples) != n_before:
                atomic_write_json(STATE_PATH, {"samples": samples})
        except Exception as exc:  # noqa: BLE001 — a bot must survive one bad cycle
            logger.error(f"FIF2 cycle failed: {exc}")
            if conn is not None:
                conn.rollback()
        finally:
            if conn is not None:
                conn.close()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
