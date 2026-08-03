#!/usr/bin/env python3
# tools/promotion_guard.py — challenger promotion name guard (T-2026-KYT-9050-057).
#
# THE PROBLEM (review finding from T-185 / PR #170, latent ever since):
# `core.shadow_gate.shadow_artifact_path` returns the staging path for a SHADOW
# leg, but for a LIVE leg the BARE root filename — the promotion is therefore
# defined solely by the register flip (SHADOW→LIVE) + copying the file into the
# repo root. `SHADOW_ARTIFACTS` historically carries, for challenger tags, the
# filename of the retrain GENERATION, not the tag's own:
#
#     "RUB3": {"LONG": "rub2_model_LONG.pkl"}   # ← tag RUB3, file rub2_*
#
# If such a leg goes live, the challenger reads the root slot of a FOREIGN
# generation — and, worse, the promotion places the challenger file in exactly
# the slot the legacy bot loads its live model from. For EPD3-SHORT this was
# real on 2026-07-21 (`epd2_model_SHORT.pkl` = bot 10's `EPD2_ARTIFACT_PATHS["SHORT"]`):
# the EPD2 live path would have loaded the same model and posted it under two
# tags (rule-4 duplicate trade, real money). It was fixed BY HAND at the time by
# renaming the root artifact challenger-distinctly to `epd3_model_SHORT.pkl`; for
# EPD3-LONG (T-037) the same manual work a second time.
#
# WHAT THIS GUARD DOES: it automates exactly that manual work — it checks, for
# every leg in `SHADOW_ARTIFACTS`, whether its promotion target (the root
# filename) is challenger-distinct, and otherwise suggests the tag's own name. A
# LIVE leg with a foreign slot is a FAIL (exit 1), a still-SHADOW leg a WARN
# (latent promotion blocker). Today: RUB3-LONG is WARN, not FAIL — RUB3 is
# parked on SHADOW per T-2026-KYT-9050-037, so the hazard is live but not
# triggered. Flipping to LIVE without a rename turns it into FAIL.
#
# Invariants:
#   * READ-ONLY and purely registry-based: no DB access, no network, no
#     filesystem scan, no promotion (hard rules 1/2). The verdict is therefore
#     independent of which artifacts sit on THIS machine.
#   * NO behaviour change to `core.shadow_gate` (hard rule 7: the gate is
#     imported by bots AND trainer/replay). The guard READS the register.
#   * The tag→root-filename bridge for the legacy/live generations comes from
#     `tools.bot_variants.index.legacy_artifact_slots()` — an already-tested
#     source instead of a second curated dict.
#
# Invocation:
#   python tools/promotion_guard.py            # findings + exit 1 on FAIL
#   python tools/promotion_guard.py --strict   # WARNs also count as exit 1

from __future__ import annotations

import argparse
import os
import sys
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.shadow_gate as shadow_gate  # noqa: E402
import tools.bot_variants.index as variant_index  # noqa: E402

OK = "PASS"
WARN = "WARN"
FAIL = "FAIL"


def _norm(tag: str) -> str:
    """Tag normalisation as in the register (keys are UPPER there)."""
    return (tag or "").strip().upper()


class Finding(NamedTuple):
    """One finding per (tag, direction). ``severity`` is WARN or FAIL."""

    tag: str
    direction: str
    filename: str
    severity: str
    reasons: tuple[str, ...]
    suggestion: str

    def as_line(self) -> str:
        return (
            f"{self.severity} {self.tag}/{self.direction}: '{self.filename}' — "
            + "; ".join(self.reasons)
            + f" → rename challenger-distinct: '{self.suggestion}'"
        )


def slot_claims() -> dict[str, set[str]]:
    """Root filename → set of tags claiming this loader slot.

    Union of (a) the curated legacy/live registry (the root slots the fleet
    bots load their models from) and (b) `SHADOW_ARTIFACTS` (the class-(A)/
    challenger tags). A filename with >1 tag is the collision hazard: on
    promotion, ONE artifact lands in a slot TWO generations read from."""
    claims: dict[str, set[str]] = {}
    for tag, dirmap in variant_index.legacy_artifact_slots().items():
        for filenames in dirmap.values():
            for filename in filenames:
                claims.setdefault(filename, set()).add(tag.upper())
    for tag, dirmap in shadow_gate.SHADOW_ARTIFACTS.items():
        for filename in dirmap.values():
            claims.setdefault(filename, set()).add(tag.upper())
    return claims


def tag_prefix(tag: str) -> str:
    """The filename prefix a tag's own artifact must carry.

    Convention of the already-fixed cases: tag `EPD3` → `epd3_model_SHORT.pkl`.
    Hyphens of horizon tags (`MIS2-8H`) are dropped so the prefix stays a
    valid filename token."""
    return _norm(tag).replace("-", "").lower()


def suggested_name(tag: str, filename: str) -> str:
    """Challenger-distinct suggestion: replace the leading generation token.

    'rub2_model_LONG.pkl' + RUB3 → 'rub3_model_LONG.pkl'. If the name carries
    no '_' token (legacy single files), the prefix is prepended."""
    prefix = tag_prefix(tag)
    head, sep, tail = filename.partition("_")
    if sep and head.lower() != prefix:
        return f"{prefix}_{tail}"
    if sep:
        return filename
    return f"{prefix}_{filename}"


def check_leg(tag: str, direction: str, claims: dict[str, set[str]] | None = None) -> Finding | None:
    """Checks ONE (tag, direction) leg from `SHADOW_ARTIFACTS`. None = clean.

    Two independent pieces of evidence for "not challenger-distinct":
      (1) OWNERSHIP — the filename is claimed by at least one FOREIGN tag
          (the hard evidence: some other loader really reads it).
      (2) CONVENTION — the filename does not carry the tag's own prefix. Also
          catches the case where the foreign loader is (not yet) in any registry.

    The severity comes from the lifecycle: a LIVE leg reads the foreign root
    slot NOW (FAIL), a SHADOW/SILENT/RETIRED leg only after the next promotion
    (WARN — a latent blocker the operator must see before the flip)."""
    filename = shadow_gate.SHADOW_ARTIFACTS.get(_norm(tag), {}).get(_norm(direction))
    if not filename:
        return None
    claims = slot_claims() if claims is None else claims

    reasons: list[str] = []
    foreign = sorted(claims.get(filename, set()) - {_norm(tag)})
    if foreign:
        reasons.append(f"root slot is also read by {', '.join(foreign)}")
    if not filename.lower().startswith(tag_prefix(tag) + "_"):
        reasons.append(f"filename does not carry the tag's own prefix '{tag_prefix(tag)}_'")
    if not reasons:
        return None

    live = shadow_gate.is_live(tag, direction)
    severity = FAIL if live else WARN
    reasons.append(
        "leg is LIVE ⇒ shadow_artifact_path already delivers this root name"
        if live
        else f"leg is {shadow_gate.leg_status(tag, direction)} ⇒ latent today, blocks the next promotion"
    )
    return Finding(
        tag=_norm(tag),
        direction=_norm(direction),
        filename=filename,
        severity=severity,
        reasons=tuple(reasons),
        suggestion=suggested_name(tag, filename),
    )


def scan() -> list[Finding]:
    """All legs from `SHADOW_ARTIFACTS`, stably sorted (FAIL first)."""
    claims = slot_claims()
    findings = [
        f
        for tag in sorted(shadow_gate.SHADOW_ARTIFACTS)
        for direction in sorted(shadow_gate.SHADOW_ARTIFACTS[tag])
        if (f := check_leg(tag, direction, claims)) is not None
    ]
    findings.sort(key=lambda f: (f.severity != FAIL, f.tag, f.direction))
    return findings


def check_staging_filename(filename: str, claims: dict[str, set[str]] | None = None) -> tuple[str, str]:
    """Promotion preview for ONE staging file (entry point for the verifier).

    The verifier only sees the file, not the intent — whoever promotes
    `rub2_model_LONG.pkl` may mean the RUB2 generation OR the RUB3 challenger.
    Hence deliberately WARN instead of FAIL here: the finding names the
    competing tags, the verdict stays with the operator. The unambiguous case
    (register says LIVE) is `scan()` and a FAIL there."""
    claims = slot_claims() if claims is None else claims
    tags = sorted(claims.get(filename, set()))
    if len(tags) < 2:
        return OK, f"root slot '{filename}' is only claimed by {tags[0] if tags else '—'}"
    hint = " / ".join(f"{t} → {suggested_name(t, filename)}" for t in tags)
    return WARN, (
        f"root slot '{filename}' is claimed by {len(tags)} tags ({', '.join(tags)}) — "
        f"a promotion would serve both loaders from ONE file (rule-4 duplicate-post hazard). "
        f"Rename the challenger challenger-distinct: {hint}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Challenger promotion name guard: does a challenger artifact hijack a foreign loader slot?"
    )
    parser.add_argument("--strict", action="store_true", help="Also treat WARNs as an error (exit 1)")
    args = parser.parse_args(argv)

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")

    findings = scan()
    if not findings:
        print("PASS promotion-guard: all challenger artifacts carry a challenger-distinct root name.")
        return 0
    for f in findings:
        print(f.as_line())
    fails = [f for f in findings if f.severity == FAIL]
    if fails:
        print(f"\n{len(fails)} FAIL — a LIVE leg loads from a foreign root slot. Rename the artifact (rule 4/6).")
        return 1
    if args.strict:
        print(f"\n{len(findings)} WARN and --strict — treated as an error.")
        return 1
    print(f"\n{len(findings)} WARN, no FAIL — latent promotion blockers, no live effect today.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
