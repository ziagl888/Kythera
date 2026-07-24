# backtest/test_bot_variant_index.py
"""DB-freie Tests für tools/bot_variants/index.py (T-2026-KYT-9050-038, Phase 1).

Pinnt die Akzeptanzkriterien aus tools/bot_variants/SPEC.md:
  AK1  bekannte Tags → erwartete family/script/lifecycle
  AK2  unbekannter Tag + nicht-klassifizierbares File werden GEZÄHLT+gelistet
       (kein Silent-Drop, wie bot_catalog)
  AK3  build_index deterministisch/idempotent (kein now()/Zufall)
  AK4  geteilte Dateinamen (ein File unter >1 Tag) werden geflaggt
  AK5  gelistetes md5 == echtes md5 der Datei auf Platte

Run: pytest backtest/test_bot_variant_index.py -v
     (schnell: die Tests nutzen load_embedded=False → kein joblib/xgboost)
"""

from __future__ import annotations

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.bot_catalog as bc  # noqa: E402
import core.shadow_gate as sg  # noqa: E402
import tools.bot_variants.index as ix  # noqa: E402


@pytest.fixture(scope="module")
def index():
    # load_embedded=False: nur Sidecar-meta → schnell, deterministisch, ohne xgboost.
    return ix.build_index(load_embedded=False)


def _gen(index, tag):
    for g in index["generations"]:
        if g["tag"] == tag:
            return g
    raise AssertionError(f"generation {tag} not in index")


# ── AK1: bekannte Tags → family / script / lifecycle ─────────────────────────


@pytest.mark.parametrize(
    ("tag", "family", "script"),
    [
        ("RUB1", "RUB", "13_ai_rub_bot.py"),
        ("RUB2", "RUB", "13_ai_rub_bot.py"),
        ("ATB2", "ATB", "14_ai_atb_bot.py"),
        ("MIS1-8H", "MIS", "11_ai_mis_bot.py"),
        ("MIS2-24H", "MIS", "11_ai_mis_bot.py"),
        ("ABR2", "ABR", "18_ai_abr1_bot.py"),
        ("EPD2", "EPD", "10_pump_dump_detector.py"),
        ("SRA2", "SRA", "9_ai_sr_bot.py"),
    ],
)
def test_known_tag_family_and_script(index, tag, family, script):
    g = _gen(index, tag)
    assert g["family"] == family
    assert g["script"] == script


def test_family_for_tag_reverse_helper():
    # Der neue bot_catalog-Reverse-Helper, den der Index nutzt.
    assert bc.family_for_tag("RUB2") == "RUB"
    assert bc.family_for_tag("ABR2") == "ABR"  # longest-wins, nicht BR
    assert bc.family_for_tag("MIS1-8h") == "MIS"
    assert bc.family_for_tag("Main Channel") is None  # klassisch → kein Prefix
    assert bc.family_for_tag("TOTALLY_NEW_9000") is None


def test_lifecycle_matches_shadow_gate(index):
    # RUB1 wurde per T-037 in beiden Richtungen live revived.
    assert _gen(index, "RUB1")["lifecycle"] == {"LONG": "live", "SHORT": "live"}
    # ATB2 sammelt in beiden Richtungen Shadow.
    assert _gen(index, "ATB2")["lifecycle"] == {"LONG": "shadow", "SHORT": "shadow"}
    # MIS1-8H: LONG geparkt (shadow), SHORT (dump) live — pro Leg genau 1 Generation.
    assert _gen(index, "MIS1-8H")["lifecycle"] == {"LONG": "shadow", "SHORT": "live"}
    # AIM1 ist retired.
    assert _gen(index, "AIM1")["lifecycle"]["LONG"] == "retired"


def test_lifecycle_values_are_exactly_shadow_gate(index):
    # Der Index erfindet keinen Lifecycle-Zustand — er spiegelt leg_status().
    for g in index["generations"]:
        for direction, status in g["lifecycle"].items():
            assert status == sg.leg_status(g["tag"], direction)


def test_code_ref_head_iff_live(index):
    # Phase-1-Kontrakt: code_ref=HEAD genau dann, wenn eine Richtung live ist.
    for g in index["generations"]:
        has_live = any(v == sg.LIVE for v in g["lifecycle"].values())
        assert g["code_ref"] == ("HEAD" if has_live else None)


# ── AK2: kein Silent-Drop — unbekannte Tags + unklassifizierte Files ──────────


def test_unclassified_artifacts_counted_and_listed(index):
    # qm_xgboost_model_v2.pkl liegt im Root, ist aber keiner Generation zugeordnet.
    assert index["unclassified_count"] == len(index["unclassified_artifacts"])
    names = {u["filename"] for u in index["unclassified_artifacts"]}
    assert "qm_xgboost_model_v2.pkl" in names
    # Sidecars/Reports dürfen NICHT als unklassifiziert auftauchen.
    assert not any(n.endswith("_meta.json") for n in names)
    assert not any(n.endswith("_report.json") for n in names)


def test_threshold_sidecars_not_flagged_as_models(index):
    names = {u["filename"] for u in index["unclassified_artifacts"]}
    assert not any(n.startswith("threshold_") for n in names)


def test_unknown_tag_is_counted(monkeypatch):
    # Ein Tag im Lifecycle-Register ohne Fleet-Script muss GEZÄHLT werden.
    patched = dict(sg._LIFECYCLE)
    patched[("ZZZNEW9", "LONG")] = sg.SHADOW
    monkeypatch.setattr(sg, "_LIFECYCLE", patched)
    idx = ix.build_index(load_embedded=False)
    assert "ZZZNEW9" in idx["unknown_tags"]
    assert idx["unknown_tag_count"] == len(idx["unknown_tags"])
    g = _gen(idx, "ZZZNEW9")
    assert g["script"] is None
    assert any("unbekannter Tag" in n for n in g["notes"])


# ── AK3: Determinismus / Idempotenz ──────────────────────────────────────────


def test_build_index_deterministic():
    a = ix.build_index(load_embedded=False)
    b = ix.build_index(load_embedded=False)
    assert ix._dump_json(a) == ix._dump_json(b)
    assert ix.render_markdown(a) == ix.render_markdown(b)


def test_generations_sorted_by_tag(index):
    tags = [g["tag"] for g in index["generations"]]
    assert tags == sorted(tags)


def test_no_timestamp_in_output(index):
    # Kein now()/Datum in den Ausgabezeilen (sonst nicht idempotent).
    md = ix.render_markdown(index)
    assert "generiert" in md.lower()
    # trained_at (statischer Datei-Inhalt) ist ok; ein Render-Zeitstempel wäre es nicht.
    assert "generated_at" not in ix._dump_json(index)


# ── AK4: geteilte Dateinamen (Kollisions-Hazard) ─────────────────────────────


def test_shared_filenames_flagged(index):
    shared = {s["filename"]: set(s["tags"]) for s in index["shared_filenames"]}
    # rub2_model_LONG.pkl: RUB2-Retrain UND RUB3-Challenger nutzen dieselbe Datei.
    assert shared.get("rub2_model_LONG.pkl") == {"RUB2", "RUB3"}
    # epd2_model_LONG.pkl: EPD2-Retrain UND EPD3-LONG-Shadow — der im Spec genannte
    # Root-Kollisions-Hazard.
    assert shared.get("epd2_model_LONG.pkl") == {"EPD2", "EPD3"}


# ── AK5: md5 == echtes File-md5 ──────────────────────────────────────────────


def test_md5_matches_real_file(index):
    checked = 0
    for g in index["generations"]:
        for a in g["artifacts"]:
            if not a["exists"]:
                continue
            abspath = os.path.join(ix.REPO_ROOT, a["path"])
            with open(abspath, "rb") as fh:
                real = hashlib.md5(fh.read()).hexdigest()  # noqa: S324
            assert a["md5"] == real, a["filename"]
            checked += 1
    assert checked > 0  # es wurde tatsächlich etwas geprüft


def test_missing_artifact_has_null_md5(index):
    # EPD2 SHORT-Retrain (epd2_model_SHORT.pkl) liegt nicht auf Platte → MISSING.
    g = _gen(index, "EPD2")
    missing = [a for a in g["artifacts"] if a["filename"] == "epd2_model_SHORT.pkl"]
    assert missing and missing[0]["location"] == "MISSING"
    assert missing[0]["md5"] is None and missing[0]["exists"] is False
