# Which SHORT leg deserves a place in the trailing channel? (T-2026-KYT-9050-062)

**Brief (Michi, 2026-08-01):** more shorts for bot 40 — check EPD first, then MIS,
then TSM1; and fix the yardstick once it turned out to be unfair.

**Answer in one sentence:** the edge sits in the **thin** legs (MIS2 family,
+5.5 to +8.1 points residual), the volume in the **bad** ones (TSM1 −0.72 at
107 signals/day) — more shorts and better shorts are different goals here.

Measurement window 2026-06-01 → 08-01. Tools: `tools/short_leg_trail_value.py` (the
load-bearing yardstick) and `tools/epd_short_generation_study.py` (precursor, see §2).

---

## 1. Two yardsticks, and why the first one was unfair

The first approach scored a leg as **realised − the full index move over the
holding window**. That sounds like "how much of the move was captured" and is,
by construction, unfair in a trending market: a take-profit leg exits at TP1
while the tape keeps running. Over a period with a **−50% index**, that made
almost every SHORT leg come out negative — a result that can't tell apart
"bad selection" from "TP caps the trend".

The second approach puts **both sides under the same trail rule**
(act = 2%, x = 10%):

| | |
|---|---|
| Leg | trail on the coin's own price path = what bot 40 would have earned by mirroring |
| Benchmark | the same trail on the **index** path over the same window |
| Residual | leg − benchmark |

That takes each leg's own TP policy out on both sides, and legs with
different exit styles become comparable. In exchange, the index carries a
**synthetic high/low** (median hourly high and low ratio) — a trail fires
on wicks, and a close-only benchmark would have flattered every leg.

---

## 2. Result under the fair yardstick

| Leg | n | Leg/trade | Market | **Residual** | t_clust | Gate | Sig./day |
|---|---:|---:|---:|---:|---:|---|---:|
| MIS2-168h | 47 | 9.074 | 0.994 | **+8.081** | 8.74 | live, roster | 0.9 |
| MIS2-72h | 132 | 7.968 | 1.033 | **+6.934** | 9.52 | live, roster | 6.3 |
| MIS2-24h | 117 | 7.569 | 0.766 | **+6.802** | 4.73 | live, roster | 2.0 |
| MIS2-8h | 122 | 5.836 | 0.302 | **+5.534** | 3.48 | **shadow** | — |
| MIS1-24h | 61 | 5.901 | 0.964 | **+4.937** | 3.81 | **shadow** | 3.4 |
| MIS1-8h | 104 | 3.434 | 0.889 | +2.546 | 3.79 | live, roster | 5.0 |
| RUB1 | 257 | 3.595 | 1.252 | +2.343 | 3.17 | live, roster | 8.6 |
| MIS1-72h | 119 | 3.127 | 1.007 | +2.119 | 2.27 | **shadow** | 6.3 |
| AIM1 | 523 | 2.874 | 1.515 | +1.359 | 5.59 | — | — |
| ROM1 | 4 514 | 1.352 | 0.492 | +0.859 | 2.43 | **duplicate** | 148 |
| AIM2 | 1 643 | 1.842 | 1.156 | +0.685 | 4.95 | live, roster | 37.7 |
| … | | | | | | | |
| EPD3 | 7 271 | 0.295 | 0.674 | **−0.379** | −5.90 | shadow | 337 |
| BR2H | 907 | 0.608 | 1.227 | −0.619 | −4.03 | shadow (SHORT) | 27 |
| TSM1 | 1 308 | 0.220 | 0.937 | **−0.717** | −4.58 | **live** | 107 |
| BB_4H | 684 | 0.569 | 1.308 | −0.739 | −3.47 | shadow (SHORT) | 10 |
| BR1Hv2 | 1 331 | 0.419 | 1.238 | −0.819 | −5.18 | shadow | 50 |

**Negative under both yardsticks** — and therefore reliable: **TSM1 SHORT** (−0.72,
t −4.58) and **EPD3 SHORT** (−0.38, t −5.90). The T-032 parking of EPD3 stands.

---

## 3. Two of my own past misjudgements the fair yardstick corrects

**(a) "The density ranking is an artefact."** In T-060 I had argued the
MIS2 legs only sit at the top of the roster because their slot-days denominator
is near zero (0.8–2.4 slot-days over five months), and are therefore pure
micro-scalpers. **Wrong.** Under the arm's exit rule they earn their place:
+6.8 to +8.1 residual at t = 4.7 to 9.5. The selection from 26.07. picked the
right legs.

**(b) "TSM1 SHORT is the clean lever."** Recommended over two rounds — based on
volume (107 signals/day, live, previously only left out because of the never-binding
slot cap) and **without measuring quality**. TSM1 is significantly negative
under both yardsticks. Exactly the mistake the EPD and MIS passes had avoided.

**(c) Partial correction:** I had dismissed the MIS1 shadow legs as "noise with a
sign" (eight legs tested, one over t = 2). Under the fair yardstick they hold up:
MIS1-24h +4.94 (t 3.81), MIS1-72h +2.12 (t 2.27).

---

## 4. The tension this exposes

**Edge and volume are decoupled on the SHORT side.** The MIS2 family delivers
0.9 to 6.3 signals/day at residual +7; TSM1 delivers 107/day at −0.72; EPD3
delivers 337/day at −0.38. A high-volume SHORT leg with a positive edge does not
exist, with two exceptions: **AIM2** (37.7/day, +0.69, already in the roster) and
**ROM1** (148/day, +0.86) — the latter, however, is a re-forwarder whose mirroring
double-counts the original legs (T-052, `EXCLUDED_AS_DUPLICATE`).

That is the real answer to "we need more shorts": with the legs currently
available, **more volume comes only at the expense of quality**.

---

## 5. Honest limits

- **The MIS2 means stand on n = 47–132 and are probably fat-tailed.**
  A leg that shorts after pumps lives off a few coins that crash 30%.
  For **sign and ranking** the measurement carries; whoever wants to rely on
  **magnitude** needs median and quantiles first. Not computed.
- **Shadow legs know no slippage.** All numbers are upper bounds; MIS2-8h,
  MIS1-24h and MIS1-72h sit in shadow and are affected by this.
- **The index is not tradeable.** It is a yardstick for selection quality, not
  an alternative the operator could actually have chosen.
- **Inference clustered at the day level.** The trades overlap heavily (same
  coins, same hours); nominal n treats one market move as many observations.
  The t-values above are the conservative ones.
- **1 725 trades fell out of scoring**, because their window lay outside the
  index coverage.
- **`closed_ai_signals` is only readable deduplicated** (357k-dup blob +
  synthetic LEGACY prices). Read raw, EPD1 SHORT reports a median of
  +21.2%/trade over 46 729 rows; deduplicated it's 2 793 trades. Contract from
  `wave_buildup_study.load_trades`.

---

## 6. Recommendation

1. **Nothing to TSM1 SHORT.** The roster slot would be negatively occupied. The
   point from T-060 ("bring in TSM1") is thereby settled — it was based on
   quantity without quality.
2. **EPD shorts stay parked.** All three generations without an edge; EPD3
   significantly negative under both yardsticks.
3. **Candidates for a gate flip, if more SHORT volume is wanted:**
   MIS1-24h (+4.94, t 3.81), MIS1-72h (+2.12, t 2.27), MIS2-8h (+5.53, t 3.48) —
   ~13 signals/day together. Small volume, but a measured edge. Clarify §5 point 1
   first (distribution instead of mean).
4. **The exposure cap therefore stays bound.** The T-060 volume problem is not
   solvable with the legs on hand without buying quality. Whoever really wants to
   raise throughput has to address the grandfather cohort (28 LONG occupy
   56% of the cap headroom, #T54-3) — not the SHORT supply.

Gate flip and roster change are operator decisions and need a
fleet restart. Not part of this task.
