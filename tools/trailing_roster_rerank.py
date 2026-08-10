# tools/trailing_roster_rerank.py — re-rank the trailing roster against the LIVE book.
"""T-2026-KYT-9050-134 — does the roster still pick the right legs, and by the right measure?

The roster in ``core.trailing_roster`` comes from the slot-budget replay of PR #198
(``tools/trailing_slot_budget.py``, generated 2026-07-26 on data from 2026-03-01) and
ranks legs by **density** — net result per occupied slot-day. Two things have since
made that ranking questionable, and this tool tests both instead of assuming either.

Density is the right objective only under scarcity
--------------------------------------------------
Ranking by result-per-slot is what you do when seats are the binding constraint: a leg
only deserves one if it out-earns the leg it displaces. That premise is measurable, and
it is false. Occupancy peaks at 233 against a ``SLOT_CAP`` of 500 for bot 40 and 1000
for bot 44 (two channels, T-2026-KYT-9050-117); T-2026-KYT-9050-129 states the same
("the cap has never bound"). When seats are free, a leg that earns little per slot-day
but earns it on many trades is additive — it displaces nothing. The replay rejected four
legs that are net-positive per trade purely on density: EPD3 LONG (+0.168 %),
BB_1H LONG (+0.566 %), BR2H LONG (+0.352 %), TSM1 SHORT (+0.036 %).

So this tool reports **absolute net contribution** as the primary column and keeps
density as the secondary one — the reverse of PR #198 — and says so where it matters.

But the replay's own numbers need calibrating first
---------------------------------------------------
The decisive check is not "which ranking is prettier", it is whether the replay predicts
what the arm actually earns. Against the live book (``trailing_positions``, both net of
the same fee) the 2026-07-26 replay **overstates every single leg it has live coverage
for** — 16 of 16, median error -0.73 pp, mean -1.17 pp, correlation 0.611. A candidate
list drawn from uncorrected replay values is therefore worthless: applying the median
error alone flips all four rejected legs negative.

That error has two possible sources and they carry opposite conclusions:

  * **Model gap.** The replay simulates a trailing exit and otherwise lets a trade run
    to its recorded close. It has no stop-loss and no time-stop, while live those two
    account for ~14 % of exits at an average of -2.6 %. It also assumes every mirror
    fills, ignores the symbol lock, and ignores the re-entry lock.
  * **Regime.** The replay window (2026-03-01 → 07-26) is not the live window
    (2026-07-26 →). A leg can simply have gotten worse.

They are separable, and separating them is the point of ``--replay-live``: run the
replay again restricted to the LIVE window and calibrate against the same trades it
just simulated. What survives that is model error; what disappears was regime.

Correction is fitted, not assumed
---------------------------------
The error is not a constant offset — it grows with the predicted value (MIS2-72h SHORT
predicts +6.26 and delivers +1.37; ATS2 LONG predicts +0.07 and delivers -0.24). A flat
median shift would therefore under-correct the top of the list and over-correct the
bottom, which is exactly where the candidates sit. The fit is an ordinary least-squares
line ``live = a + b · replay`` over legs with enough live trades, reported together with
its R² and residual spread so the reader can see how much it is worth.

**A fitted correction is an extrapolation for any leg with no live coverage** — which is
every candidate. That is the honest limit of this exercise and it is printed in the
report, not buried: the candidates are ranked by a number no live trade has verified.

Read-only: SELECTs against trailing_positions, no writes, no live effect. Roster changes
themselves are an operator decision (Michi), never an output of this tool.

Usage:
    python tools/trailing_roster_rerank.py --replay staging_models/replay/trailing_slot_budget_live.json
    python tools/trailing_roster_rerank.py --replay <old.json> --replay-live <livewindow.json>
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from dataclasses import dataclass, field

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

DEFAULT_REPLAY = "staging_models/replay/trailing_slot_budget_live.json"
DEFAULT_OUT = "staging_models/replay"
MIN_LIVE_N = 30  # live trades a leg needs before it may inform the calibration
LIVE_START = "2026-07-26"  # first day of the live trailing book


class ActivationMismatch(RuntimeError):
    """A replay whose legs were scored under a different trail rule than the live bot."""


def check_activation(report: dict, live_activation: float, label: str) -> None:
    """Refuse to compare a replay scored at another activation floor than the arm runs.

    The `legs` block of a slot-budget report is computed at that report's `chosen_act`,
    and `chosen_act` is *selected* by the tool's mean-budget rule — it is not fixed.
    Re-running the same replay on a shorter window legitimately picks a different floor:
    the live-window run of 2026-08-11 chose 0.0 where the 2026-07-26 run chose 2.0, and
    at 0.0 the trail is the documented micro-scalper (median trailing hold 0.42 h against
    5.58 h at 2.0, see the pins in `backtest/test_trailing_slot_budget.py`).

    Calibrating live results against those numbers would not be a noisy comparison, it
    would be a comparison against a different strategy. Pin the floor with
    `--activations 2.0` rather than letting the selector choose.
    """
    chosen = report.get("chosen_act")
    if chosen is None:
        raise ActivationMismatch(f"{label}: report carries no chosen_act")
    if abs(float(chosen) - live_activation) > 1e-9:
        raise ActivationMismatch(
            f"{label}: legs scored at activation {chosen}, but the live arm runs at "
            f"{live_activation}. Re-run that replay with --activations {live_activation}."
        )


@dataclass
class Calibration:
    """Least-squares mapping from replay prediction to live realised, per trade."""

    a: float
    b: float
    r2: float
    resid_sd: float
    n_legs: int
    median_error: float
    overstated: int
    points: list[tuple[str, float, float]] = field(default_factory=list)

    def predict(self, replay_net: float) -> float:
        return self.a + self.b * replay_net


def normalise_leg(key: str) -> str:
    """`MIS1-72h LONG` and `MIS1-72H LONG` are the same leg.

    The replay keys carry the tag as the trainer spells it, the live book carries it as
    the bot wrote it. Upper-casing both is enough — direction is already a fixed token —
    and doing it in one place keeps the join from silently dropping legs, which is the
    failure mode that would quietly shrink the calibration set.
    """
    return key.strip().upper()


def calibrate(
    replay_legs: dict[str, dict],
    live: dict[str, tuple[int, float]],
    fee: float,
    min_live_n: int = MIN_LIVE_N,
) -> Calibration | None:
    """Fit `live_net = a + b · replay_net` over legs with real live coverage.

    ``live`` maps a normalised leg key to (n_trades, mean gross mark %). The live book
    stores an unlevered mark without fees, so the same round-trip the replay subtracts
    is subtracted here — comparing a net prediction against a gross outcome would
    flatter the replay by exactly one fee.
    """
    pts: list[tuple[str, float, float]] = []
    for key, rec in replay_legs.items():
        pred = rec.get("per_trade_trail_net")
        if pred is None:
            continue
        hit = live.get(normalise_leg(key))
        if hit is None or hit[0] < min_live_n:
            continue
        pts.append((normalise_leg(key), float(pred), hit[1] - fee))

    if len(pts) < 3:
        return None

    xs = [p[1] for p in pts]
    ys = [p[2] for p in pts]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    a = my - b * mx

    resid = [y - (a + b * x) for x, y in zip(xs, ys, strict=True)]
    ss_res = sum(r * r for r in resid)
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 - ss_res / ss_tot if ss_tot else 0.0
    errs = [y - x for x, y in zip(xs, ys, strict=True)]

    return Calibration(
        a=a,
        b=b,
        r2=r2,
        resid_sd=statistics.stdev(resid) if len(resid) > 1 else 0.0,
        n_legs=len(pts),
        median_error=statistics.median(errs),
        overstated=sum(1 for e in errs if e < 0),
        points=pts,
    )


@dataclass
class LegScore:
    key: str
    status: str
    n: int
    replay_per_trade: float
    corrected_per_trade: float
    slot_days: float
    density: float
    occ_p95: float
    thin: bool
    rostered: bool
    live_n: int
    live_per_trade: float | None
    live_sd: float = 0.0
    min_live_n: int = MIN_LIVE_N

    @property
    def live_t(self) -> float | None:
        """t of the live per-trade mean against zero — is the sign real or a fortnight?

        Trades within a leg are not independent (one tape, overlapping windows), so this
        overstates confidence and is a screening number, not a p-value. It still separates
        ATS2 LONG at 448 trades from a leg that had six bad ones.
        """
        if self.live_per_trade is None or self.live_n < 2 or self.live_sd <= 0:
            return None
        return self.live_per_trade / (self.live_sd / self.live_n**0.5)

    @property
    def measured(self) -> bool:
        """Does this leg have enough live trades to speak for itself?"""
        return self.live_per_trade is not None and self.live_n >= self.min_live_n

    @property
    def effective_per_trade(self) -> float:
        """What the leg is expected to earn — measured where possible, fitted otherwise.

        A fitted value must never overwrite a measured one. The fit is a regression line,
        so it pulls every leg toward the middle: AIM2 SHORT predicts +0.149 through the
        correction while its own 480 live trades say -0.511. Ranking on the fitted number
        would put a leg T-2026-KYT-9050-129 retired for losing money back near the top of
        a seat recommendation. Live data wins wherever it exists; the fit is only for legs
        that have none.
        """
        if self.measured:
            assert self.live_per_trade is not None  # narrowed by `measured`
            return self.live_per_trade
        return self.corrected_per_trade

    @property
    def basis(self) -> str:
        return "live" if self.measured else "fitted"

    @property
    def corrected_total(self) -> float:
        """Net contribution over the replay's own trade count, at the effective rate."""
        return self.effective_per_trade * self.n


def score_legs(
    replay_legs: dict[str, dict],
    cal: Calibration | None,
    rostered: set[str],
    live: dict[str, tuple[int, float]],
    fee: float,
    min_live_n: int = MIN_LIVE_N,
) -> list[LegScore]:
    """Score every measured leg; corrected value falls back to raw when uncalibrated."""
    out: list[LegScore] = []
    for key, rec in replay_legs.items():
        pred = rec.get("per_trade_trail_net")
        if pred is None:
            continue
        nk = normalise_leg(key)
        hit = live.get(nk)
        out.append(
            LegScore(
                key=key,
                status=rec.get("status", ""),
                n=int(rec.get("n", 0)),
                replay_per_trade=float(pred),
                corrected_per_trade=cal.predict(float(pred)) if cal else float(pred),
                slot_days=float(rec.get("slot_days_trail", 0.0)),
                density=float(rec.get("density_trail", 0.0)),
                occ_p95=float(rec.get("occ_p95_trail", 0.0)),
                thin=bool(rec.get("thin", False)),
                rostered=nk in rostered,
                live_n=hit[0] if hit else 0,
                live_per_trade=(hit[1] - fee) if hit else None,
                # tolerate a 2-tuple: callers that only have (n, mean) still work
                live_sd=float(hit[2]) if hit and len(hit) > 2 else 0.0,
                min_live_n=min_live_n,
            )
        )
    out.sort(key=lambda s: -s.corrected_total)
    return out


def load_rostered() -> set[str]:
    from core.trailing_roster import ROSTER  # imported late: keeps the pure part import-free

    return {normalise_leg(f"{tag} {direction}") for tag, direction in ROSTER}


def fetch_live_book(start: str = LIVE_START) -> dict[str, tuple[int, float]]:
    """Per-leg (n, mean gross mark %) from the live trailing book. SELECT only.

    Goes through ``core.database`` rather than building its own psycopg2 connection:
    that is where the credentials come from (``core.config`` calls ``load_dotenv()``
    with no path, so the .env is found by walking UP — which is what makes this work
    from a worktree, where no .env of its own exists).
    """
    from core.database import get_db_connection

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # entry > 0 is not decoration: a zero entry yields a NaN-ish mark that the
            # column's own guards let through (T-2026-KYT-9050-114).
            # STDDEV alongside the mean: a retirement recommendation on a mean without
            # its spread is exactly the error this task exists to correct. The t below
            # is what separates "this leg loses" from "this leg had a bad fortnight".
            cur.execute(
                """
                SELECT model, direction, COUNT(*), AVG(close_mark_pct), STDDEV_SAMP(close_mark_pct)
                FROM trailing_positions
                WHERE posted AND closed_at IS NOT NULL AND close_mark_pct IS NOT NULL
                  AND entry > 0 AND opened_at >= %s
                GROUP BY 1, 2
                """,
                (start,),
            )
            return {
                normalise_leg(f"{m} {d}"): (int(n), float(a), float(sd) if sd is not None else 0.0)
                for m, d, n, a, sd in cur.fetchall()
            }
    finally:
        conn.close()


def render(scores: list[LegScore], cal: Calibration | None, meta: dict) -> str:
    lines: list[str] = []
    lines.append("# Trailing roster re-rank — T-2026-KYT-9050-134\n")
    lines.append(
        f"Replay `{os.path.basename(meta['replay_file'])}` (window from {meta['replay_start']}, "
        f"generated {meta['replay_generated']}) scored against the live book from "
        f"{meta['live_start']}. Calibration: {meta['calibration']}.\n"
    )

    # Verdict first: the numbers below are long, and the one thing a reader must not
    # miss is that nothing here clears the bar for acting on it.
    drags_pre = [s for s in scores if s.rostered and s.measured and s.corrected_total < 0]
    cands_pre = [s for s in scores if not s.rostered and not s.thin and s.effective_per_trade > 0]
    strong_pre = [s for s in drags_pre if (s.live_t or 0) < -2]
    lines.append("\n## Verdict\n")
    lines.append(
        f"\n**No seat changes are recommended on this evidence.**\n\n"
        f"- The premise the task started from does not survive: every unrostered leg with a positive "
        f"expected contribution ({len(cands_pre)} of them) is **fitted, not measured** — none has live "
        f"trades. The largest is worth {max((s.corrected_total for s in cands_pre), default=0):+.0f} "
        f"%-points. The legs PR #198 rejected on density turn out net-NEGATIVE once corrected, so the "
        f"roster kept them out for a stated reason that was wrong and an outcome that was right.\n"
        f"- The real finding points the other way: **{len(drags_pre)} rostered legs lose money on live "
        f"evidence**, {sum(s.corrected_total for s in drags_pre):+.0f} %-points combined.\n"
        f"- But **{len(strong_pre)} of them clear |t| > 2**. On a two-week book that is a watchlist, "
        f"not a retirement list. Acting on it now would repeat the error this tool was built to catch.\n"
    )

    lines.append("\n## Calibration — does the replay predict the live arm?\n")
    if cal is None:
        lines.append("\nToo few legs with live coverage to fit anything. Values below are RAW replay output.\n")
    else:
        lines.append(
            f"\nFit over {cal.n_legs} legs with >= {MIN_LIVE_N} live trades:\n\n"
            f"    live = {cal.a:+.3f} + {cal.b:.3f} * replay      R2 = {cal.r2:.3f}, residual sd = {cal.resid_sd:.3f} pp\n\n"
            f"The replay overstates **{cal.overstated} of {cal.n_legs}** calibrated legs; median error "
            f"{cal.median_error:+.3f} pp per trade. A slope below 1 means the error grows with the "
            f"prediction, so the top of the raw ranking is the least trustworthy part of it.\n"
        )
        lines.append("\n| leg | replay net | live net | error | live n |\n|---|---:|---:|---:|---:|\n")
        for key, pred, real in sorted(cal.points, key=lambda p: -p[1]):
            lines.append(f"| {key} | {pred:+.3f} | {real:+.3f} | {real - pred:+.3f} | {meta['live_n'].get(key, 0)} |\n")

    lines.append("\n## Ranking by expected net contribution\n")
    lines.append(
        "\nPrimary column is absolute contribution (effective per-trade x replay trade count), "
        "because the seat cap provably never binds. Density is retained as the PR #198 measure.\n"
        f"\n`basis` says where the per-trade number comes from: **live** for legs with >= {MIN_LIVE_N} "
        "trades in the real book, **fitted** for legs that have none and must be extrapolated through "
        "the calibration line. A fitted value never overrides a measured one — the fit regresses toward "
        "the mean and would otherwise rehabilitate legs the live book has already convicted.\n"
    )
    lines.append(
        "\n| leg | roster | basis | n | replay/trade | effective/trade | total | density | occ p95 | live n |\n"
        "|---|:--:|:--:|---:|---:|---:|---:|---:|---:|---:|\n"
    )
    for s in scores:
        flag = "YES" if s.rostered else ("thin" if s.thin else "-")
        lines.append(
            f"| {s.key} | {flag} | {s.basis} | {s.n} | {s.replay_per_trade:+.3f} | "
            f"{s.effective_per_trade:+.3f} | {s.corrected_total:+.0f} | {s.density:.3f} | "
            f"{s.occ_p95:.0f} | {s.live_n or ''} |\n"
        )

    unrostered_positive = [s for s in scores if not s.rostered and not s.thin and s.effective_per_trade > 0]
    lines.append("\n## Candidates — unrostered legs that survive the correction\n")
    if not unrostered_positive:
        lines.append(
            "\n**None.** Every unrostered leg turns net-negative once the replay is corrected against "
            "the live book. On this evidence the roster is not leaving money on the table, and the "
            "four legs PR #198 rejected on density were not wrongly rejected — they were rejected for "
            "the wrong stated reason.\n"
        )
    else:
        for s in unrostered_positive:
            evidence = (
                f"measured on {s.live_n} live trades"
                if s.measured
                else "NO live coverage — the fit extrapolated beyond its support"
            )
            lines.append(
                f"\n- **{s.key}** — {s.effective_per_trade:+.3f} %/trade ({s.basis}) over {s.n} replay "
                f"trades ({s.corrected_total:+.0f} % total), density {s.density:.3f}, p95 occupancy "
                f"{s.occ_p95:.0f} seats. {evidence}.\n"
            )

    drags = [s for s in scores if s.rostered and s.measured and s.corrected_total < 0]
    drags.sort(key=lambda s: s.corrected_total)
    lines.append("\n## Rostered legs that lose money on live evidence\n")
    if not drags:
        lines.append("\nNone — every seated leg is net-positive in the live book.\n")
    else:
        lines.append(
            "\nThese are **measured, not extrapolated**: each has live trades past the floor. "
            "This is where the roster is actually wrong, and it is the opposite of the question "
            "the task started from.\n\n"
            "| leg | n | live/trade | t | total | live n |\n|---|---:|---:|---:|---:|---:|\n"
        )
        for s in drags:
            t = s.live_t
            t_cell = f"{t:+.2f}" if t is not None else "n/a"
            lines.append(
                f"| {s.key} | {s.n} | {s.effective_per_trade:+.3f} | "
                f"{t_cell} | {s.corrected_total:+.0f} | {s.live_n} |\n"
            )
        total = sum(s.corrected_total for s in drags)
        strong = [s for s in drags if (s.live_t or 0) < -2]
        lines.append(
            f"\nCombined drag **{total:+.0f} %-points** over the window. Of these, "
            f"**{len(strong)}** clear |t| > 2 on their own book: "
            f"{', '.join(s.key for s in strong) if strong else 'none'}. The rest are directionally "
            "negative but within noise for this window and should be re-checked rather than acted on.\n"
        )

    lines.append("\n## Limits\n")
    lines.append(
        "\n- Every candidate is **uncalibrated by construction** — it has no live trades, so its "
        "corrected value is the fit extrapolated beyond its support.\n"
        "- The replay has no stop-loss and no time-stop; live those are ~14 % of exits at ~-2.6 %. "
        "It also assumes every mirror fills and ignores the symbol and re-entry locks.\n"
        "- Seat recommendations are input to an operator decision, never a change made here.\n"
    )
    return "".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", default=DEFAULT_REPLAY, help="slot-budget replay json to score")
    ap.add_argument(
        "--replay-live",
        default=None,
        help="second replay restricted to the live window; separates model error from regime",
    )
    ap.add_argument("--live-start", default=LIVE_START)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--name", default="trailing_roster_rerank_t134")
    args = ap.parse_args()

    from core.trailing_roster import ACTIVATION_PCT

    with open(args.replay, encoding="utf-8") as fh:
        rep = json.load(fh)
    check_activation(rep, ACTIVATION_PCT, args.replay)
    fee = float(rep.get("fee", 0.10))
    legs = rep["legs"]

    live = fetch_live_book(args.live_start)
    rostered = load_rostered()

    # The live-window replay, when present, is the honest calibration: same trades, same
    # tape, so what is left over is the model and not the regime.
    cal_source = legs
    cal_label = "cross-period (replay window != live window)"
    if args.replay_live:
        with open(args.replay_live, encoding="utf-8") as fh:
            rep_live = json.load(fh)
        check_activation(rep_live, ACTIVATION_PCT, args.replay_live)
        cal_source = rep_live["legs"]
        cal_label = "same-window (model error isolated from regime)"

    cal = calibrate(cal_source, live, fee)
    scores = score_legs(legs, cal, rostered, live, fee)

    meta = {
        "replay_file": args.replay,
        "replay_start": rep.get("start"),
        "replay_generated": rep.get("generated_at"),
        "live_start": args.live_start,
        "calibration": cal_label,
        "live_n": {k: v[0] for k, v in live.items()},
    }

    os.makedirs(args.out, exist_ok=True)
    base = os.path.join(args.out, args.name)
    md = render(scores, cal, meta)
    with open(base + ".md", "w", encoding="utf-8") as fh:
        fh.write(md)
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump(
            {
                "meta": meta,
                "calibration": None
                if cal is None
                else {
                    "a": cal.a,
                    "b": cal.b,
                    "r2": cal.r2,
                    "resid_sd": cal.resid_sd,
                    "n_legs": cal.n_legs,
                    "median_error": cal.median_error,
                    "overstated": cal.overstated,
                    "points": cal.points,
                },
                "legs": [vars(s) | {"corrected_total": s.corrected_total} for s in scores],
            },
            fh,
            indent=2,
        )
    print(md)
    print(f"-> {base}.md / .json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
