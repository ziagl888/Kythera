# core/shadow_gate.py — fleet-wide shadow-posting gate (T-2026-CU-9050-125).
#
# PURPOSE: Every (model_tag, direction) leg that does NOT post live should instead
# of silence create a MONITORED shadow trade — so suppressed legs and not-yet-promoted
# retrains build a realised outcome history (closed_ai_signals) for later analysis,
# including regime-conditioned activation (whitelist v2 flip, T-2026-CU-9050-069).
#
# HOW IT IS SAFE — "monitored but unposted": A shadow trade is an ai_signals row
# WITHOUT a telegram_outbox row. The AI monitor (8_ai_trade_monitor) reads ai_signals
# unfiltered, tracks entry/TP/SL and writes a closed_ai_signals row on close — it
# contains NO posting code. A channel post happens exclusively via a telegram_outbox
# row. No outbox insert => never a post (verified T-2026-CU-9050-125). Details:
# docs/SHADOW_MODE_POSTING.md.
#
# SECURITY CONTRACT (hard rules 1/2/4):
#   * DEFAULT = LIVE. This module lists ONLY legs that are explicitly SHADOW or
#     RETIRED; everything else is live. The gate must NEVER turn an existing
#     live post into a shadow post — the wiring is purely ADDITIVE at the
#     non-post branch of each bot.
#   * Shadow trades carry model metadata model_id (rule 6). Live and shadow legs
#     of the same model are separated by `direction`; new generations carry a new
#     tag anyway (ATS2 vs. ATS1) → no collision in closed_ai_signals or with
#     has_open_ai_signal.
#   * Master kill switch KYTHERA_SHADOW_POSTING=0 turns OFF all shadow emission
#     (bots fall back to today's prediction-only behaviour).

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Lifecycle states of a (tag, direction) leg.
LIVE = "live"  # posts live (Cornix + ai_signals) — default
SHADOW = "shadow"  # creates a monitored shadow trade, no Cornix post
RETIRED = "retired"  # old generation, no longer emitted (history only)
SILENT = "silent"  # deliberately silenced leg: NO live post AND NO shadow —
# the model runs (the bot is unparked) but outputs nothing. Used to switch off an
# old leg (ATS1/ATB1) whilst the retrain (ATS2/ATB2) collects data in shadow
# (operator decision Michi, T-2026-CU-9050-127).

_DIRECTIONS = ("LONG", "SHORT")

# Directory of not-yet-promoted retrain artifacts (hard rule 2:
# model artifacts live in staging_models/ until Michi promotes them to repo root).
# Overrideable for tests via KYTHERA_STAGING_DIR.
STAGING_DIR = os.environ.get("KYTHERA_STAGING_DIR", "staging_models")


def shadow_posting_enabled() -> bool:
    """Master switch. Default ON; KYTHERA_SHADOW_POSTING=0 => completely off."""
    return os.environ.get("KYTHERA_SHADOW_POSTING", "1") == "1"


# ─────────────────────────────────────────────────────────────────────────────
# LIFECYCLE REGISTER  —  (TAG_UPPER, DIRECTION) → state
# ─────────────────────────────────────────────────────────────────────────────
# Only non-live legs are listed. If a leg is missing here, it is LIVE.
# Source of truth is the combination of docs/MODEL_INTENT.md (operator
# decisions) AND the actual gating in each bot. Every line carries
# its rationale — this is the auditable core of this feature by Michi.
#
# SHADOW legs fall into two classes:
#   (A) New generation, not yet promoted: the model exists only in
#       staging_models/ and runs PARALLEL to the still-live old tag
#       (ATS2 alongside ATS1, ATB2 alongside ATB1, …). See SHADOW_ARTIFACTS.
#   (B) Suppressed direction leg of an otherwise-live model: the model is
#       loaded but the direction does not (yet) go live.
_LIFECYCLE: dict[tuple[str, str], str] = {
    # ── (A) New-generation shadow candidates (staging, not promoted) ──
    # ATS2 (Bot 12): on 2026-07-23 promoted SHADOW→LIVE (T-2026-KYT-9050-033, audit
    # T-032 — ATS2 shadow beats the silenced ATS1 in both directions:
    # LONG +0.31%×612, SHORT thin). BOTH legs therefore NOT listed anymore ⇒ default
    # LIVE. Bot 12 now posts ATS2 via post_ai_signal_gated (T-033 rewire from
    # _emit_ats2_shadow), so the LIVE state creates real Cornix post.
    #   DEPLOY PRECONDITION (Michi, hard rule 2): promote ats2_model_{LONG,SHORT}.pkl
    #   from staging_models/ to repo root — the LIVE loader reads the root path
    #   (shadow_artifact_path), otherwise ATS2 loads nothing and stays silent. Thresholds
    #   are real (LONG 0.7825 / SHORT 0.9084), so NO flood risk.
    # ATB2: converging-channel rebuild (Bot 14). ATB1 is silenced (C).
    # ATB2 SHORT remains SHADOW (not deployable, data collection). ATB2 LONG is per
    # operator decision (T-2026-KYT-9050-037, Michi bot_results.xlsx) promoted SHADOW→LIVE
    # — threshold set BLIND to 0.60 (only n=17 shadow trades → no data-driven
    # operating point; ATB1's 0.80 is a different, incompatible model and NOT
    # transferable). Bot 14 routes ATB2 since T-037 via post_ai_signal_gated
    # (rewire from _emit_atb2_shadow → _emit_atb2), so LIVE creates real Cornix post to
    # CH_ATB_TARGET (has_open guard against rule-4 double post, pattern bot 12).
    # Explicitly LIVE (defence-in-depth, like RUB1 T-037).
    #   DEPLOY PRECONDITION (Michi, hard rule 2): promote atb2_model_LONG.pkl from
    #   staging_models/ (threshold 0.60) to repo root — the LIVE loader reads
    #   the root path, otherwise ATB2 LONG loads nothing and stays silent.
    ("ATB2", "LONG"): LIVE,
    ("ATB2", "SHORT"): SHADOW,
    # SRA2: meta-filter retrain (Bot 9). SRA1 stays live. SRA2 was "not
    # deployable" BECAUSE the label source closed_trades3 has been dead since Feb — a
    # pure TRAINING problem. Shadow serving bypasses this completely: the AI monitor
    # delivers fresh outcomes (closed_ai_signals) that the dead tracker no longer
    # provides → shadow REVIVED SRA2. SHORT threshold is null (every setup).
    # SRA2 LONG promoted to LIVE on 2026-07-21 (T-2026-CU-9050-185, @0.6424 → CH_AI_SR,
    # coexisting with SRA1). Artifact sra2_model_LONG.* promoted to repo root
    # (rule 2, operator decision Michi).
    # SRA2 SHORT promoted SHADOW→LIVE on 2026-07-23 (T-2026-KYT-9050-033, audit T-032:
    # SRA2 SHORT shadow +1.00%×222). NOT listed anymore ⇒ default LIVE; bot 9 posts
    # SRA2 SHORT already via post_ai_signal_gated (no rewire needed).
    #   DEPLOY PRECONDITIONS (Michi): (1) promote sra2_model_SHORT.json from
    #   staging_models/ to repo root (rule 2); (2) ⚠ optimal_threshold is NULL (meta:
    #   deployable false, val avg_net_pnl −0.079%) → LIVE would post on EVERY S/R SHORT
    #   candidate (Cornix flood, no prob gate). Before go-live set a threshold or
    #   retrain — otherwise promotion is a flood risk (flagged in report).
    # MAX2 (T-2026-KYT-9050-020): NO model — a fork of the SRA2 LONG emission in
    # bot 9 (_emit_max2) that posts the same SRA2 LONG trade coin-filtered (config.
    # MAIN_CHANNEL_COINS) to CH_MAIN and replaces the retired classic
    # "Main Channel" detector. Deliberately NOT listed ⇒ default LIVE
    # (operator decision Michi): collision-free with the SRA2 post to CH_AI_SR,
    # because CH_AI_SR is NOT Cornix-executed (no rule-4 double trade). LONG only
    # (SRA2 SHORT dead). Rollback to shadow = uncomment the following line
    # (no more Cornix, just monitored shadow trade):
    # ("MAX2", "LONG"): SHADOW,
    # FMR2: normalisation-exit retrain (K4, T-2026-CU-9050-148) alongside the FMR1 bot
    # (Bot 31). FMR1 remains unchanged under its own tag "FMR1"; FMR2 uses the SAME
    # funding extreme detector + `build_fmr1_row` feature row (FMR2_FEATURES ==
    # FMR1_FEATURES, only the label differs) → faithful parity. The retrain
    # was not deployable (both directions net-negative, AUC ~0.54) → shadow for
    # live counter-check; one model for both directions (side_short is a feature),
    # optimal_threshold 0.46. NO tag collision (FMR1 posts under "FMR1").
    ("FMR2", "LONG"): SHADOW,
    ("FMR2", "SHORT"): SHADOW,
    # ── (B) Challenger legs: the retrain challenges a LIVE leg already posting
    #        under the SAME tag → own generation tag, otherwise the shadow trade
    #        would block a LIVE post via the bot's active-trade check (violation of
    #        the purely-additive invariant). ──
    # RUB3 = rub2_model_LONG retrain vs. LIVE RUB LONG (bot 13 posts the legacy
    # models since T-037 again under "RUB1"). Operator decision Michi (rule 6).
    ("RUB3", "LONG"): SHADOW,
    # RUB3 SHORT (T-2026-KYT-9050-037, Michi bot_results.xlsx): explicitly parked →
    # SHADOW both directions. RUB3 actually only emits the LONG shadow candidate
    # (_emit_rub3_shadow, bot 13); the SHORT direction is inert (n=0/60d) — the
    # entry is primarily register hygiene to ensure RUB3 never accidentally goes SHORT live.
    ("RUB3", "SHORT"): SHADOW,
    # RUB4 (T-2026-CU-9050-164): funding-gated RUB LONG — the SAME RUB3
    # candidate, but only if fund_24h > +3 bps (ABR1 LONG gate). Experiment whether
    # the gate saves bleeding RUB LONG; RUB4 vs. RUB3 = gated vs. ungated
    # in report. Uses the RUB3 artifact (no own SHADOW_ARTIFACTS entry).
    ("RUB4", "LONG"): SHADOW,
    # EPD3 = epd2_model_{LONG,SHORT} retrain vs. LIVE EPD (bot 10 posts the legacy
    # model already under tag "EPD2" = EPD_LEGACY_TAG). Own tag "EPD3" — analogous to RUB3.
    # Bot 10 routes EPD3 via post_ai_signal_gated ⇒ LIVE creates Cornix to CH_PUMP_AI.
    # EPD3 SHORT: 2026-07-21 LIVE (@0.6737), 2026-07-23 parked LIVE→SHADOW (T-033,
    # audit T-032: [act] net −0.06%×3568 — edge gone). REMAINS SHADOW.
    #   ⚠ DEPLOY NOTE SHORT: as a SHADOW leg, shadow_artifact_path reads
    #   staging_models/epd3_model_SHORT.pkl — if the file is missing there, parking is silence (ok).
    # EPD3 LONG: per operator decision (T-2026-KYT-9050-037, Michi bot_results.xlsx)
    # promoted SHADOW→LIVE, threshold 0.76 (VOLUME cap: 2578 shadow trades showed ~0
    # edge + non-discriminating confidence [corr −0.04] → 0.76 = median, caps the
    # ~258/d flood to ~130/d; NO edge filter, deliberate operator decision). The
    # challenger-DISTINCT filename epd3_model_LONG.pkl (SHADOW_ARTIFACTS) prevents
    # collision with the legacy EPD2 loader slot (epd2_model_LONG.pkl), analogously SHORT.
    # Explicitly LIVE (defence-in-depth, like RUB1 T-037).
    #   DEPLOY PRECONDITION LONG (Michi, hard rule 2): promote epd3_model_LONG.pkl from
    #   staging_models/ (threshold 0.76) to repo root — otherwise EPD3 LONG loads
    #   nothing and stays silent.
    ("EPD3", "LONG"): LIVE,
    ("EPD3", "SHORT"): SHADOW,
    # ── (C) Silenced old legs (operator Michi, T-2026-CU-9050-127) ──
    # Bots 12/14 are unparked so ATS2/ATB2 run in shadow — but the
    # OLD models ATS1/ATB1 should NOT post live and also not shadow:
    # they go completely silent. The bot asks is_live() at the post branch; SILENT ⇒
    # not live ⇒ the entire ATS1/ATB1 output branch is skipped.
    ("ATS1", "LONG"): SILENT,
    ("ATS1", "SHORT"): SILENT,
    ("ATB1", "LONG"): SILENT,
    ("ATB1", "SHORT"): SILENT,
    # FIF1: replaced by TSM1 (SHORT → CH_FIF1) (T-2026-CU-9050-183). On 2026-07-23
    # revived SILENT→SHADOW (T-2026-KYT-9050-033, audit T-032: continue to monitor
    # FIF1 as monitored shadow rather than completely silent). Bot 33 had only a
    # LIVE-or-nothing branch; T-033 adds a SHADOW branch (post_shadow_ai_signal),
    # otherwise the revive creates no monitored trades. TSM1 remains the live successor
    # to CH_FIF1 (block (D)); the FIF1 shadow does not collide (own tag, no
    # Cornix). Complete silence again = set these two lines to SILENT.
    ("FIF1", "LONG"): SHADOW,
    ("FIF1", "SHORT"): SHADOW,
    # ── (D) Rule-based shadow forwarders (T-2026-CU-9050-149) ──
    # Study candidates K1/K2/K5/K7 are RULES, not models — no artifact in
    # SHADOW_ARTIFACTS. The bot calculates the signal itself and emits on the
    # RAW signal (ROM1 precedence), gated ONLY via this SHADOW line. All
    # backtests negative/weak → shadow = live counter-check, no rollout.
    # LIS1 (K5): post-listing drift fade, SHORT only (LONG blacklist is a
    # separate gate, operator matter). Bot 36 NEVER posts live (fail-safe: if the
    # leg is not SHADOW, the bot stays silent — the rule has no edge).
    ("LIS1", "SHORT"): SHADOW,
    # TSM1 (K1, SHORT), SKW1 (K7, LONG+SHORT), XSM1 (K2, LONG) and XSR1 (K2,
    # SHORT) were promoted to LIVE on 2026-07-20 (T-2026-CU-9050-183, operator
    # decision Michi from the 14:00 report review) — they therefore are NOT listed
    # here anymore (default LIVE). Routing: TSM1 SHORT → CH_FIF1 (replaces FIF1,
    # see block (C)); SKW1 LONG+SHORT + XSM1 LONG + XSR1 SHORT → CH_ATS (former ATS channel).
    # Live posting runs via signal_post.post_ai_signal_gated in bots 37/38/39;
    # withdrawal to shadow = record the respective (tag, dir) line here again with
    # SHADOW. LIS1 SHORT remains shadow-only (further falsified).
    #
    # ══════════════════════════════════════════════════════════════════════════
    # (E) Fleet reconfig after audit T-032 (T-2026-KYT-9050-033, operator decision
    #     Michi from bot_results.xlsx). All legs here bleed realised and are
    #     parked (Cornix off, monitored shadow on) or both legs silenced.
    #     These tags do NOT post via post_ai_signal_gated but legacy-direct;
    #     their bots consult the gate since T-033 at the emission point via
    #     core.signal_post.route_legacy_leg — the register entry becomes effective.
    #     Keys are UPPER-normalised (leg_status _norm()s the lookup, not the key).
    # ── Park SHORT →SHADOW (LONG remains LIVE), audit: SHORT leg net-negative ──
    # BR (Bot 7, pattern breakout): 2h/4h SHORT parked; LONG live. BR1H is the
    # PRE-rename tag (bot 7 posts 1h today as BR1Hv2 = BR1HV2, see (E) full block) —
    # the BR1H line is documentary (no active emitter anymore).
    ("BR1H", "SHORT"): SHADOW,
    ("BR2H", "SHORT"): SHADOW,
    ("BR4H", "SHORT"): SHADOW,
    # BB (Bot 25, SMC sniper): 1h/4h SHORT parked; LONG live. TD (same bot)
    # stays completely LIVE (audit KEEP) → no TD line.
    ("BB_1H", "SHORT"): SHADOW,
    ("BB_4H", "SHORT"): SHADOW,
    # QM (Bot 24, Quasimodo): 1h SHORT parked; LONG live. QM_4H the bot no longer
    # runs anyway (TIMEFRAMES=['1h']) → the QM_4H line is documentary.
    ("QM_1H", "SHORT"): SHADOW,
    ("QM_4H", "SHORT"): SHADOW,
    # ── Park LONG →SHADOW (SHORT remains LIVE), audit: LONG leg net-negative ──
    # MIS2 (Bot 11): 24h/72h/168h LONG parked (pump side lags behind the better
    # realising SHORT/dump side); SHORT live. MIS2-8H is in the full block.
    ("MIS2-24H", "LONG"): SHADOW,
    ("MIS2-72H", "LONG"): SHADOW,
    ("MIS2-168H", "LONG"): SHADOW,
    # EPD1: PRE-rename tag (bot 10 posts today EPD2 = EPD_LEGACY_TAG). No active
    # EPD1 emitter anymore → documentary; the park works via the EPD2 full block.
    ("EPD1", "LONG"): SHADOW,
    # ── Full →SHADOW (both legs), audit: both directions net-negative ──
    # EPD2 (Bot 10, legacy pump/dump direct post): both legs parked.
    ("EPD2", "LONG"): SHADOW,
    ("EPD2", "SHORT"): SHADOW,
    # MIS2-8H (Bot 11): both legs parked (8h horizon is negative anyway per study).
    ("MIS2-8H", "LONG"): SHADOW,
    ("MIS2-8H", "SHORT"): SHADOW,
    # ── (E-MIS1) MIS1 revive (T-2026-KYT-9050-034, operator decision Michi) ──
    # Bot 11 revives the MIS1 generation (pump_model_*_final.pkl) PARALLEL to MIS2
    # under own tags MIS1-* (audit T-032: MIS1 realised better). The GOOD legs
    # are default LIVE (NOT listed) and revive exactly the MIS2 legs parked by T-033:
    # MIS1-24H/72H/168H LONG (pump) + MIS1-8H SHORT (dump).
    # The WEAK MIS1 legs are parked here to SHADOW (monitored trade, no Cornix) —
    # MIS1-8H LONG (8h pump negative) + MIS1-24H/72H/168H SHORT (there
    # the MIS2 SHORT/dump side is the live-held generation, T-033). No
    # Cornix double post per leg: per (horizon, direction) exactly ONE generation
    # is live. Keys UPPER (leg_status _norm()s the lookup).
    ("MIS1-8H", "LONG"): SHADOW,
    ("MIS1-24H", "SHORT"): SHADOW,
    ("MIS1-72H", "SHORT"): SHADOW,
    ("MIS1-168H", "SHORT"): SHADOW,
    # RUB2 (Bot 13, rubberband — the RUB2 retrain generation): both legs parked →
    # SHADOW. Since the RUB1 revive (T-2026-KYT-9050-037, Michi bot_results.xlsx) bot 13
    # no longer posts under "RUB2" (LONG legacy + RUB2 SHORT retrain are replaced by the
    # original RUB1 legacy models) → these lines are now documentary:
    # they hold the benchmarked RUB2 generation as SHADOW in case an emitter picks it up again.
    # RUB3/RUB4 (LONG challenger) remain unchanged shadow (above).
    ("RUB2", "LONG"): SHADOW,
    ("RUB2", "SHORT"): SHADOW,
    # RUB1 (Bot 13, rubberband — the ORIGINAL legacy generation, both directions live
    # under tag RUB1, T-2026-KYT-9050-037 revive). EXCEPTION to the "list only non-live"
    # rule of this register: explicitly listed as LIVE for DEFENCE-IN-DEPTH (spec §2 #1
    # point 4). route_legacy_leg("RUB1", …) would already return LIVE by default, but
    # the explicit entry guarantees that the revived live generation never accidentally
    # ends up in the same park as the alongside-held SHADOW RUB2 generation.
    ("RUB1", "LONG"): LIVE,
    ("RUB1", "SHORT"): LIVE,
    # SRA1 (Bot 9, S/R legacy direct post): both legs parked. SRA2 is the successor
    # (LONG + SHORT now LIVE, see (A)) → SRA1 steps back to shadow.
    ("SRA1", "LONG"): SHADOW,
    ("SRA1", "SHORT"): SHADOW,
    # BB2_4H (Bot 25, BB retrain generation): both legs parked (audit RETIRE both ways).
    ("BB2_4H", "LONG"): SHADOW,
    ("BB2_4H", "SHORT"): SHADOW,
    # BR1D + BR1Hv2 (Bot 7): both legs parked (1d + the current 1h tag BR1HV2).
    ("BR1D", "LONG"): SHADOW,
    ("BR1D", "SHORT"): SHADOW,
    ("BR1HV2", "LONG"): SHADOW,
    ("BR1HV2", "SHORT"): SHADOW,
    # ABR2 (Bot 18, break&retest gen-2 — SHORT binary model + LONG funding gate, both
    # post as ABR2): both legs parked. ABR1 (legacy fallback tag) remains default
    # LIVE but is only emitted by the bot if binary metadata is missing (not today).
    ("ABR2", "LONG"): SHADOW,
    ("ABR2", "SHORT"): SHADOW,
    # "Main Channel" (classic): already retired via detector dispatch removal
    # (T-2026-KYT-9050-020, replaced by MAX2) → no emitter, no entry needed.
}

# RETIRED: tags that appear in closed_ai_signals history but are no longer emitted
# by any live bot. Pure report classification (part 2) — no posting effect. Direction
# does not matter here (both directions retired).
_RETIRED_TAGS: set[str] = {
    "AIM1",  # §9: AIM1 concept officially replaced by AIM2 (ranker/gate).
    # T-2026-KYT-9050-037 (Michi bot_results.xlsx) — both directions RETIRE:
    #   AIM2-TOPN: "too thin" (high-conviction top-N channel, bot 15). Keys UPPER
    #     (is_retired _norm()s). The bot 15 emitter does NOT consult this register
    #     (own AIM2_TOPN config gate) → the RETIRE line is register/report
    #     classification; the actual shutdown (config gate + restart) is an
    #     operator step (Michi). The associated live trades are deleted by a separate,
    #     individually approved DB DELETE (hard rule 1) — NOT in this code PR.
    "AIM2-TOPN",
    #   ATS1_ROBUST: "synthetic only" — dead/censored history tag in
    #     closed_ai_signals (SYNTHETIC/CENSORED-ONLY, no live emitter). Retire =
    #     pure report classification; the synthetic trades are deleted by the same
    #     operator-gated DB DELETE step.
    "ATS1_ROBUST",
    # MIS1 (T-2026-KYT-9050-034): NOT retired anymore — the MIS1 generation is
    # REVIVED (bot 11 loads pump_model_*_final.pkl again, operator decision Michi;
    # audit T-032: MIS1 realised better than MIS2). Lifecycle per (tag, direction)
    # is now controlled by the _LIFECYCLE block (E-MIS1) below: the good legs
    # (MIS1-24H/72H/168H LONG + MIS1-8H SHORT) are default LIVE, the weak ones
    # parked to SHADOW there.
    "MSI1",  # historical MIS typo family tag (bot_naming normalised → MIS1).
}


# ─────────────────────────────────────────────────────────────────────────────
# SHADOW ARTIFACTS  —  class (A) models from staging_models/
# ─────────────────────────────────────────────────────────────────────────────
# Per new tag the artifact filenames per direction. The bot loads them via
# load_shadow_artifact() alongside its live model and scores in parallel.
# If the file is missing (not staged), the loader returns None → the bot
# runs unchanged (no hard error).
SHADOW_ARTIFACTS: dict[str, dict[str, str]] = {
    "ATS2": {"LONG": "ats2_model_LONG.pkl", "SHORT": "ats2_model_SHORT.pkl"},
    "ATB2": {"LONG": "atb2_model_LONG.pkl", "SHORT": "atb2_model_SHORT.pkl"},
    "SRA2": {"LONG": "sra2_model_LONG.json", "SHORT": "sra2_model_SHORT.json"},
    # Challenger tags (see _LIFECYCLE (B)) — artifact filename still carries the
    # retrain generation, the tag above is the collision-free shadow tag.
    "RUB3": {"LONG": "rub2_model_LONG.pkl"},
    # EPD3 SHORT (T-2026-CU-9050-185) + LONG (T-2026-KYT-9050-037) are promoted LIVE →
    # challenger-DISTINCT root filenames epd3_model_{SHORT,LONG}.pkl so that the
    # promotion does NOT hijack the legacy EPD2 loader slot (bot 10: EPD2_ARTIFACT_PATHS
    # ["{SHORT,LONG}"]="epd2_model_{SHORT,LONG}.pkl") — otherwise the EPD2 live path
    # loads the same file and posts twice (tag EPD2 + EPD3, rule-4 double trade; review
    # T-185). The epd3_model_LONG.pkl rename (T-037) pulled the LONG slot that was
    # previously shadow-loaded under the epd2 name along when LONG went live (threshold 0.76 in artifact).
    "EPD3": {"LONG": "epd3_model_LONG.pkl", "SHORT": "epd3_model_SHORT.pkl"},
    # FMR2: a binary model for BOTH directions (side_short is a feature) →
    # same file per direction; not promoted, shadow only (T-2026-CU-9050-148/149).
    "FMR2": {"LONG": "fmr2_model.pkl", "SHORT": "fmr2_model.pkl"},
}


def _norm(tag: str) -> str:
    return (tag or "").strip().upper()


def is_retired(tag: str, direction: str = "") -> bool:
    """True if the tag belongs to a superseded generation. Prefix boundary check,
    because closed_ai_signals tags are families (``MIS1-8h``, ``MIS1-72H``) — but
    ``MIS2-8h`` must NOT match ``MIS1``."""
    t = _norm(tag)
    for rt in _RETIRED_TAGS:
        if t == rt or t.startswith(rt + "-") or t.startswith(rt + "_"):
            return True
    return False


def leg_status(tag: str, direction: str) -> str:
    """Lifecycle state of a leg. Default LIVE (security contract)."""
    if is_retired(tag):
        return RETIRED
    return _LIFECYCLE.get((_norm(tag), _norm(direction)), LIVE)


def is_live(tag: str, direction: str) -> bool:
    return leg_status(tag, direction) == LIVE


def is_shadow(tag: str, direction: str) -> bool:
    return leg_status(tag, direction) == SHADOW


def is_silent(tag: str, direction: str) -> bool:
    return leg_status(tag, direction) == SILENT


def shadow_artifact_path(tag: str, direction: str) -> str | None:
    """Path of the artifact of a class (A)/challenger tag, or None if not listed.

    Rule-2 promotion (T-2026-CU-9050-185): a LIVE leg loads its artifact from
    the repo ROOT (promoted there = live, operator decision), a SHADOW leg
    continues from ``staging_models/``. This way a single direction leg of a
    tag can go live (e.g. SRA2 LONG @ root) whilst the other remains shadow
    (SRA2 SHORT in staging) — the loader automatically picks the right file."""
    fname = SHADOW_ARTIFACTS.get(_norm(tag), {}).get(_norm(direction))
    if not fname:
        return None
    if is_live(tag, direction):
        return fname  # promoted to repo root
    return os.path.join(STAGING_DIR, fname)


def load_shadow_artifact(tag: str, direction: str):
    """Loads a class (A) shadow model from staging_models/ (fail-soft).

    Normalises BOTH fleet artifact formats to a lean shadow-contract
    dict ``{model, features, threshold}``:
      * ``.pkl`` — retrain_from_replay joblib dict (ats2/atb2/rub2/epd2/max1),
        keys ``model / features / optimal_threshold``.
      * ``.json`` — native XGB JSON + ``_meta.json`` sidecar (sra2/abr2).

    Important: production loaders (core.model_artifacts) reject this —
    ``build_contract`` does ``float(optimal_threshold)`` and CRASHES on
    non-deployable retrains (threshold ``null`` at ATB2/SRA2 SHORT/EPD2 LONG/
    RUB2 LONG). But we want to collect exactly those in shadow, so this tolerant
    loader: ``threshold=None`` is allowed (→ emission on EVERY candidate).

    Returns: ``{model, features, threshold}`` or None (tag unknown / file
    missing / load error — bot then runs without shadow leg, hard rule 2).
    """
    path = shadow_artifact_path(tag, direction)
    if not path or not os.path.exists(path):
        return None
    try:
        if path.endswith(".json"):
            import json

            import xgboost as xgb

            model = xgb.XGBClassifier()
            model.load_model(path)
            meta_path = path[:-5] + "_meta.json"
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
            return {"model": model, "features": list(meta["features"]), "threshold": meta.get("optimal_threshold")}
        import joblib

        art = joblib.load(path)
        return {"model": art["model"], "features": list(art["features"]), "threshold": art.get("optimal_threshold")}
    except Exception as e:  # pragma: no cover - defensive, bot must not die
        logger.warning("Shadow artifact %s/%s load failed (%s): %s", tag, direction, path, e)
        return None


def artifact_threshold(artifact) -> float | None:
    """Operating threshold from the contract artifact (``optimal_threshold``).

    None means: the model has NO valid operating point (e.g. ATB2 —
    too thin data, pick_threshold_safe refused). The bot then emits
    on EVERY detector event (the detector is the gate) to generate
    shadow data for later threshold selection. If a threshold is set,
    the bot only emits when prob >= threshold — faithful preview of
    live behaviour after promotion.
    """
    if not isinstance(artifact, dict):
        return None
    # Normalised shadow contract uses "threshold"; raw joblib dict (if
    # a bot loads joblib directly) carries "optimal_threshold".
    thr = artifact.get("threshold", artifact.get("optimal_threshold"))
    try:
        return float(thr) if thr is not None else None
    except (TypeError, ValueError):
        return None


def score_artifact(artifact, feature_row: dict) -> float:
    """RAW ``predict_proba[:, 1]`` of the contract artifact on a feature dict.

    Gate semantics is RAW: ``pick_threshold_safe`` (tools/retrain_from_replay.py)
    chooses ``optimal_threshold`` on the raw predict_proba; the supplied
    isotonic calibrator is reporting only (identical to bots 13/25). The feature
    contract (order + selection) comes from ``artifact["features"]``.
    """
    import pandas as pd

    feats = artifact["features"]
    X = pd.DataFrame([feature_row]).reindex(columns=feats).fillna(0)
    return float(artifact["model"].predict_proba(X)[0, 1])
