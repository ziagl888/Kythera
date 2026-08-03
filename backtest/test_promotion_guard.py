# backtest/test_promotion_guard.py
"""DB-free tests for tools/promotion_guard.py (T-2026-KYT-9050-057).

The guard automates the manual work from T-2026-CU-9050-185 / T-2026-KYT-9050-037:
a challenger artifact that, on promotion into the repo root, occupies the loader
slot of a FOREIGN generation (EPD3-SHORT → `epd2_model_SHORT.pkl`) must get a
challenger-distinct name. Pinned here:

  AK1  today's register state has NO FAIL (no LIVE leg on a foreign slot)
  AK2  the latent case RUB3-LONG → rub2_* is reported as WARN, with the
       foreign owner (RUB2) and the rename suggestion (rub3_model_LONG.pkl)
  AK3  THE ACTUAL CATCH: if RUB3-LONG is flipped to LIVE without renaming the
       artifact, the finding tips to FAIL and the CLI exits with exit 1
  AK4  challenger-distinct legs (EPD3, ATS2, ATB2, SRA2, FMR2) are clean
  AK5  regression: if EPD3-LONG falls back to the legacy name, it is
       immediately FAIL (the leg is live) — the 2026-07-21 incident must not recur
  AK6  the guard does not mutate `core.shadow_gate` (hard rule 7)

Run: pytest backtest/test_promotion_guard.py -v
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.shadow_gate as sg  # noqa: E402
import tools.promotion_guard as pg  # noqa: E402

# ── AK1/AK2: today's register state ──────────────────────────────────────────


def test_no_live_leg_reads_a_foreign_root_slot():
    assert [f for f in pg.scan() if f.severity == pg.FAIL] == []


def test_rub3_long_is_the_known_latent_warning():
    findings = {(f.tag, f.direction): f for f in pg.scan()}
    assert ("RUB3", "LONG") in findings, "RUB3-LONG still carries the rub2_* name — must be WARN"
    f = findings[("RUB3", "LONG")]
    assert f.severity == pg.WARN  # SHADOW (T-037 park) ⇒ latent, no live effect
    assert f.filename == "rub2_model_LONG.pkl"
    assert f.suggestion == "rub3_model_LONG.pkl"
    assert any("RUB2" in r for r in f.reasons)


def test_cli_is_green_today_but_strict_surfaces_the_latent_case(capsys):
    assert pg.main([]) == 0
    assert "RUB3/LONG" in capsys.readouterr().out
    assert pg.main(["--strict"]) == 1


# ── AK3: the actual catch ─────────────────────────────────────────────────────


def test_promoting_rub3_without_rename_is_caught(monkeypatch):
    """Flip RUB3-LONG live without renaming rub2_model_LONG.pkl: the bot would
    then read exactly the root slot that the RUB2 generation loads from."""
    monkeypatch.setitem(sg._LIFECYCLE, ("RUB3", "LONG"), sg.LIVE)
    assert sg.shadow_artifact_path("RUB3", "LONG") == "rub2_model_LONG.pkl"  # bare root name

    f = pg.check_leg("RUB3", "LONG")
    assert f is not None and f.severity == pg.FAIL
    assert f.suggestion == "rub3_model_LONG.pkl"
    assert [x.severity for x in pg.scan()][0] == pg.FAIL  # FAILs sort to the front


def test_cli_exits_nonzero_when_a_live_leg_collides(monkeypatch, capsys):
    monkeypatch.setitem(sg._LIFECYCLE, ("RUB3", "LONG"), sg.LIVE)
    assert pg.main([]) == 1
    assert "FAIL RUB3/LONG" in capsys.readouterr().out


def test_renaming_the_artifact_clears_the_finding(monkeypatch):
    """The rename suggested by the guard is the complete fix — exactly what
    was done by hand twice for EPD3."""
    monkeypatch.setitem(sg._LIFECYCLE, ("RUB3", "LONG"), sg.LIVE)
    monkeypatch.setitem(sg.SHADOW_ARTIFACTS["RUB3"], "LONG", "rub3_model_LONG.pkl")
    assert pg.check_leg("RUB3", "LONG") is None
    assert pg.scan() == []


# ── AK4/AK5: challenger-distinct legs + regression ───────────────────────────


@pytest.mark.parametrize(
    ("tag", "direction"),
    [("EPD3", "LONG"), ("EPD3", "SHORT"), ("ATS2", "LONG"), ("ATB2", "SHORT"), ("SRA2", "LONG"), ("FMR2", "SHORT")],
)
def test_tag_distinct_legs_are_clean(tag, direction):
    assert pg.check_leg(tag, direction) is None


def test_fmr2_sharing_one_file_across_its_own_directions_is_not_a_collision():
    # One model for both directions (side_short is a feature) — same TAG,
    # so no foreign loader slot.
    assert sg.SHADOW_ARTIFACTS["FMR2"]["LONG"] == sg.SHADOW_ARTIFACTS["FMR2"]["SHORT"]
    assert pg.check_leg("FMR2", "LONG") is None


def test_epd3_regression_pin(monkeypatch):
    """If EPD3-LONG fell back to the legacy filename, it would be FAIL immediately —
    the leg has been live since T-037, the slot belongs to bot 10 (EPD2_ARTIFACT_PATHS)."""
    monkeypatch.setitem(sg.SHADOW_ARTIFACTS["EPD3"], "LONG", "epd2_model_LONG.pkl")
    f = pg.check_leg("EPD3", "LONG")
    assert f is not None and f.severity == pg.FAIL
    assert any("EPD2" in r for r in f.reasons)
    assert f.suggestion == "epd3_model_LONG.pkl"


def test_unknown_tag_is_not_a_finding():
    assert pg.check_leg("TOTALLY_NEW_9000", "LONG") is None
    assert pg.check_leg("RUB4", "LONG") is None  # uses the RUB3 artifact, no entry of its own


# ── Building blocks ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tag", "filename", "expected"),
    [
        ("RUB3", "rub2_model_LONG.pkl", "rub3_model_LONG.pkl"),
        ("EPD3", "epd2_model_SHORT.pkl", "epd3_model_SHORT.pkl"),
        ("EPD3", "epd3_model_SHORT.pkl", "epd3_model_SHORT.pkl"),  # already distinct
        ("SRA2", "sra2_model_LONG.json", "sra2_model_LONG.json"),
        ("MIS2-8H", "mis2_model_8h_pump.pkl", "mis28h_model_8h_pump.pkl"),  # hyphen drops
    ],
)
def test_suggested_name(tag, filename, expected):
    assert pg.suggested_name(tag, filename) == expected


def test_slot_claims_join_legacy_and_challenger_registries():
    claims = pg.slot_claims()
    assert claims["rub2_model_LONG.pkl"] == {"RUB2", "RUB3"}
    assert claims["epd2_model_SHORT.pkl"] == {"EPD2"}  # alone since the T-185 rename
    assert claims["epd3_model_SHORT.pkl"] == {"EPD3"}
    assert "pump_dump_model.pkl" in claims  # legacy slot without a retrain naming scheme


def test_check_staging_filename_is_advisory_per_file():
    status, msg = pg.check_staging_filename("rub2_model_LONG.pkl")
    assert status == pg.WARN
    assert "RUB2, RUB3" in msg and "rub3_model_LONG.pkl" in msg
    assert pg.check_staging_filename("epd3_model_SHORT.pkl")[0] == pg.OK
    assert pg.check_staging_filename("etwas_fremdes.pkl")[0] == pg.OK  # unknown ⇒ no hazard


# ── AK6: hard rule 7 — the guard only reads ──────────────────────────────────


def test_guard_does_not_mutate_the_shared_gate():
    before = {t: dict(d) for t, d in sg.SHADOW_ARTIFACTS.items()}
    lifecycle_before = dict(sg._LIFECYCLE)
    pg.scan()
    pg.slot_claims()
    assert {t: dict(d) for t, d in sg.SHADOW_ARTIFACTS.items()} == before
    assert dict(sg._LIFECYCLE) == lifecycle_before


def test_legacy_artifact_slots_accessor_returns_a_copy():
    import tools.bot_variants.index as ix

    slots = ix.legacy_artifact_slots()
    slots["RUB2"]["LONG"].append("bogus.pkl")
    assert "bogus.pkl" not in ix.legacy_artifact_slots()["RUB2"]["LONG"]
