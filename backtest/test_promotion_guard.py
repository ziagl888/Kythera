# backtest/test_promotion_guard.py
"""DB-freie Tests für tools/promotion_guard.py (T-2026-KYT-9050-057).

Der Guard automatisiert die Handarbeit aus T-2026-CU-9050-185 / T-2026-KYT-9050-037:
ein Challenger-Artefakt, das bei der Promotion in den Repo-Root den Loader-Slot
einer FREMDEN Generation belegt (EPD3-SHORT → `epd2_model_SHORT.pkl`), muss einen
challenger-distinkten Namen bekommen. Gepinnt wird:

  AK1  der heutige Register-Stand hat KEIN FAIL (kein LIVE-Bein auf fremdem Slot)
  AK2  der latente Fall RUB3-LONG → rub2_* wird als WARN gemeldet, mit dem
       fremden Eigentümer (RUB2) und dem Rename-Vorschlag (rub3_model_LONG.pkl)
  AK3  DER FANG: wird RUB3-LONG auf LIVE geflippt, ohne das Artefakt umzubenennen,
       kippt der Befund auf FAIL und die CLI beendet mit exit 1
  AK4  challenger-distinkte Beine (EPD3, ATS2, ATB2, SRA2, FMR2) sind sauber
  AK5  Regression: fällt EPD3-LONG auf den Legacy-Namen zurück, ist es sofort FAIL
       (das Bein ist live) — der 2026-07-21er Vorfall darf nicht zurückkommen
  AK6  der Guard verändert `core.shadow_gate` nicht (harte Regel 7)

Run: pytest backtest/test_promotion_guard.py -v
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.shadow_gate as sg  # noqa: E402
import tools.promotion_guard as pg  # noqa: E402

# ── AK1/AK2: heutiger Register-Stand ─────────────────────────────────────────


def test_no_live_leg_reads_a_foreign_root_slot():
    assert [f for f in pg.scan() if f.severity == pg.FAIL] == []


def test_rub3_long_is_the_known_latent_warning():
    findings = {(f.tag, f.direction): f for f in pg.scan()}
    assert ("RUB3", "LONG") in findings, "RUB3-LONG trägt weiter den rub2_*-Namen — muss WARN sein"
    f = findings[("RUB3", "LONG")]
    assert f.severity == pg.WARN  # SHADOW (T-037-Park) ⇒ latent, kein Live-Effekt
    assert f.filename == "rub2_model_LONG.pkl"
    assert f.suggestion == "rub3_model_LONG.pkl"
    assert any("RUB2" in r for r in f.reasons)


def test_cli_is_green_today_but_strict_surfaces_the_latent_case(capsys):
    assert pg.main([]) == 0
    assert "RUB3/LONG" in capsys.readouterr().out
    assert pg.main(["--strict"]) == 1


# ── AK3: der eigentliche Fang ────────────────────────────────────────────────


def test_promoting_rub3_without_rename_is_caught(monkeypatch):
    """RUB3-LONG live schalten, ohne rub2_model_LONG.pkl umzubenennen: der Bot
    läse dann exakt den Root-Slot, aus dem die RUB2-Generation lädt."""
    monkeypatch.setitem(sg._LIFECYCLE, ("RUB3", "LONG"), sg.LIVE)
    assert sg.shadow_artifact_path("RUB3", "LONG") == "rub2_model_LONG.pkl"  # nackter Root-Name

    f = pg.check_leg("RUB3", "LONG")
    assert f is not None and f.severity == pg.FAIL
    assert f.suggestion == "rub3_model_LONG.pkl"
    assert [x.severity for x in pg.scan()][0] == pg.FAIL  # FAILs sortieren nach vorn


def test_cli_exits_nonzero_when_a_live_leg_collides(monkeypatch, capsys):
    monkeypatch.setitem(sg._LIFECYCLE, ("RUB3", "LONG"), sg.LIVE)
    assert pg.main([]) == 1
    assert "FAIL RUB3/LONG" in capsys.readouterr().out


def test_renaming_the_artifact_clears_the_finding(monkeypatch):
    """Der vom Guard vorgeschlagene Rename ist der vollständige Fix — genau das,
    was bei EPD3 zweimal von Hand gemacht wurde."""
    monkeypatch.setitem(sg._LIFECYCLE, ("RUB3", "LONG"), sg.LIVE)
    monkeypatch.setitem(sg.SHADOW_ARTIFACTS["RUB3"], "LONG", "rub3_model_LONG.pkl")
    assert pg.check_leg("RUB3", "LONG") is None
    assert pg.scan() == []


# ── AK4/AK5: challenger-distinkte Beine + Regression ─────────────────────────


@pytest.mark.parametrize(
    ("tag", "direction"),
    [("EPD3", "LONG"), ("EPD3", "SHORT"), ("ATS2", "LONG"), ("ATB2", "SHORT"), ("SRA2", "LONG"), ("FMR2", "SHORT")],
)
def test_tag_distinct_legs_are_clean(tag, direction):
    assert pg.check_leg(tag, direction) is None


def test_fmr2_sharing_one_file_across_its_own_directions_is_not_a_collision():
    # Ein Modell für beide Richtungen (side_short ist Feature) — derselbe TAG,
    # also kein fremder Loader-Slot.
    assert sg.SHADOW_ARTIFACTS["FMR2"]["LONG"] == sg.SHADOW_ARTIFACTS["FMR2"]["SHORT"]
    assert pg.check_leg("FMR2", "LONG") is None


def test_epd3_regression_pin(monkeypatch):
    """Fiele EPD3-LONG auf den Legacy-Dateinamen zurück, wäre es sofort FAIL —
    das Bein ist seit T-037 live, der Slot gehört Bot 10 (EPD2_ARTIFACT_PATHS)."""
    monkeypatch.setitem(sg.SHADOW_ARTIFACTS["EPD3"], "LONG", "epd2_model_LONG.pkl")
    f = pg.check_leg("EPD3", "LONG")
    assert f is not None and f.severity == pg.FAIL
    assert any("EPD2" in r for r in f.reasons)
    assert f.suggestion == "epd3_model_LONG.pkl"


def test_unknown_tag_is_not_a_finding():
    assert pg.check_leg("TOTALLY_NEW_9000", "LONG") is None
    assert pg.check_leg("RUB4", "LONG") is None  # nutzt das RUB3-Artefakt, kein eigener Eintrag


# ── Bausteine ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tag", "filename", "expected"),
    [
        ("RUB3", "rub2_model_LONG.pkl", "rub3_model_LONG.pkl"),
        ("EPD3", "epd2_model_SHORT.pkl", "epd3_model_SHORT.pkl"),
        ("EPD3", "epd3_model_SHORT.pkl", "epd3_model_SHORT.pkl"),  # schon distinkt
        ("SRA2", "sra2_model_LONG.json", "sra2_model_LONG.json"),
        ("MIS2-8H", "mis2_model_8h_pump.pkl", "mis28h_model_8h_pump.pkl"),  # Bindestrich fällt weg
    ],
)
def test_suggested_name(tag, filename, expected):
    assert pg.suggested_name(tag, filename) == expected


def test_slot_claims_join_legacy_and_challenger_registries():
    claims = pg.slot_claims()
    assert claims["rub2_model_LONG.pkl"] == {"RUB2", "RUB3"}
    assert claims["epd2_model_SHORT.pkl"] == {"EPD2"}  # seit dem T-185-Rename allein
    assert claims["epd3_model_SHORT.pkl"] == {"EPD3"}
    assert "pump_dump_model.pkl" in claims  # Legacy-Slot ohne Retrain-Namensschema


def test_check_staging_filename_is_advisory_per_file():
    status, msg = pg.check_staging_filename("rub2_model_LONG.pkl")
    assert status == pg.WARN
    assert "RUB2, RUB3" in msg and "rub3_model_LONG.pkl" in msg
    assert pg.check_staging_filename("epd3_model_SHORT.pkl")[0] == pg.OK
    assert pg.check_staging_filename("etwas_fremdes.pkl")[0] == pg.OK  # unbekannt ⇒ kein Hazard


# ── AK6: harte Regel 7 — der Guard liest nur ─────────────────────────────────


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
