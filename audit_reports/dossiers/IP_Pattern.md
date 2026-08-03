# Dossier: IP / Pattern Detector (Bots 7 + 22)

> Chart-based pattern signal generators: `7_pattern_detector.py` produces, among others, the break-and-retest family **BR1H/BR2H/BR4H (+BR1D)** — without an ML gate; `22_ip_pattern_bot.py` is nearly invisible in the audit sources. **Note (16): BR family D.** Core verdict: break-and-retest raw, at 4× the signal volume of its ML sibling BB, and **Σ −4,106 net** — the direct comparison BB_4H (+ML, +565) vs. BR (without ML, −4,106) is the best in-vivo argument in the repo for an ML gate; immediate action: close the BR1H SHORT side.

## 1. Fact sheet

| | |
|---|---|
| Bots | `7_pattern_detector.py` + `22_ip_pattern_bot.py` — chart-based, no ML |
| Signals/TF | **BR1H / BR2H / BR4H** (Report 14) + **BR1D** (Step 2 row "BR1H/2H/4H/1D"). **Source clarification:** Report 16, section 6 ("verified in code"): `BR1H/2H/4H` = break-and-retest **without** ML **from the pattern detector (7, not from 25!)** — the BR family therefore belongs to Bot 7, not to the SMC-ML sniper |
| Role | Report 16, section 7: "pattern detector 7 is not an intelligence layer, but a (net negative) signal layer" |
| Bot 22 | mentioned in the sources read only via P3.11 (chart directory growth); no signals/tags, no performance data, no note of its own — coverage gap |
| Leverage | not quantified in the sources |

## 2. Live balance (active era 24.02.–03.07., deduplicated)

| Family | n | WR | avg PnL | Median | Σ net |
|---|---|---|---|---|---|
| BR4H / BR2H / BR1H (Report 14) | 11,756 | 58–60% | −0.1…−0.3% | ≈0 | **−4,106** |
| BR1H/2H/4H/1D (Step 2 count) | 12,034 | 57–60% | — | — | — |

- **Direction asymmetry (E1):** BR1H **LONG 65.5% vs. SHORT 49.5% WR** — the SHORT side drags the family down; Report 15/S1: "BR1H LONG only".
- Regime drift: the BR/BB family was strongly negative Mar–Apr, positive from May onward (mini-n, regime gating now filters it out almost entirely).
- No calibration data (no ML → no confidence).
- Step 2 (P0.1 context): identical "PatternDetector" messages 2–3× within 60 min in trading channels → upstream double generation (detector refire).
- Caveat (Report 17): monitor-generated, only 63.4% replay agreement (P1.2/P2.7); for the AI fleet a replay is retroactively impossible because of N4 (deleted `ai_signals` targets).

## 3. Findings

| ID | Level | Severity | One-liner | Status |
|---|---|---|---|---|
| 16-Verdict | Concept | HIGH | BR = break-and-retest without an ML gate at 4× the signal volume of BB → Σ −4,106; the same idea with an ML gate (BB_4H) is positive | ✔ (Report 14/16) |
| E1/S1 | Live | HIGH | BR1H SHORT 49.5% WR vs. LONG 65.5% — the SHORT side is reliably harmful | ✔ (DB, deduplicated) |
| Step2-Dup | Bot | MEDIUM | Upstream double generation: md5-identical PatternDetector messages 2–3× within 60 min in trading channels (detector refire; not an outbox retry duplicate) | ✔ (Step 2) |
| P3.11 | Infra | LOW | Chart directories grow unbounded (`7:27-28`, `22:29-30`) — check whether housekeeping actually clears these dirs | ~ (unverified) |
| Gap | Audit | — | Bot 22 (`22_ip_pattern_bot.py`) has no findings, tags or figures in any of the sources — neither assessed nor cleared | ~ (open) |

## 4. Dependencies & cross-cutting risks

- **R1 (forming candle):** fleet-wide look-ahead/repaint root cause; BR signal generation hangs off the same `{sym}_{tf}` tables with partial candles.
- **Monitor caveat (Report 17):** BR WR/PnL are monitor-generated (P1.2 trailing SL never trails, P2.7 missed hits) — per-trade truth is unreliable, and the orchestrator's whitelist gates on exactly these numbers.
- **Regime gating:** BB/BR are now nearly filtered out by gating (Report 14) — any re-activation should only be assessed after the P0.4 whitelist fix.
- **S1 (Report 15):** BR1H is an explicit building block of the "direction-gated portfolio" (BR1H LONG only).

## 5. Remediation plan

**Immediate (no retrain, pure config):** close the BR1H SHORT side (S1; Report 16 recommendation 8.2). Review/park the BR family overall (Report 14 D.3: "net negative; possibly keep only the LONG side of BR1H"). Address detector-refire deduplication (cause of the duplicate messages).

**Structure:** the proven BB-vs-BR contrast suggests not switching off the BR raw signals, but placing them as an **event source under an ML gate** (pattern S11 from Report 15: meta-classifier over a large labelled signal stream) — after V1–V3 (R1 fix, dedup index, first-touch simulator) and a monitor rewrite.

**Open questions:** what exactly does `22_ip_pattern_bot.py` do and does it generate its own signals/tags? (unanswered in any source). Does Bot 7 generate other pattern tags besides BR? Does housekeeping actually clear the chart directories (P3.11)? Report the BR1D numbers separately (Step 2 counts it in, Report 14 doesn't).

## 6. Evidence

- `AUDIT_TODO.md` P3.11 (+P0.1 annotation on the upstream double generation)
- `audit_reports/14_bot_performance_db.md` (BR row: n=11,756, Σ −4,106, BR1H LONG 65.5%/SHORT 49.5%)
- `audit_reports/STEP2_DB_VERIFICATION.md` (BR1H/2H/4H/1D n=12,034; PatternDetector duplicate messages)
- `audit_reports/15_strategy_proposals.md` (E1, S1: BR1H LONG only)
- `audit_reports/16_strategy_concept_evaluation.md` (BR→Bot 7 tag clarification; ranking #17; section 7 intelligence layer)
- `audit_reports/17_monitor_replay_and_gaps.md` (monitor caveat, N4)
