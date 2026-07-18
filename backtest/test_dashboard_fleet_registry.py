# backtest/test_dashboard_fleet_registry.py
"""DB-free tests for the Z1 dashboard Fleet-Registry panel (Feature 1,
T-2026-CU-9050-152).

Mirrors backtest/test_dashboard_shell.py's style: the Flask app + route are
exercised with the test client; the pure data-shaping function
(``fleet_registry_rows``) and its two helper reads (``core.bot_catalog.
families_for_script``, ``core.process_control.parked_since``) are unit-tested
directly with synthetic/injected inputs — no Postgres, no live control/parked/
directory, no dependence on the repo's real *_meta.json content.

Run with: pytest backtest/test_dashboard_fleet_registry.py -v
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.bot_catalog as bot_catalog  # noqa: E402
import core.process_control as process_control  # noqa: E402
from tools import analytics_export  # noqa: E402
from tools.analytics_export import SOURCES_BY_NAME, AnalyticsExporter  # noqa: E402
from tools.dashboard import app as dashboard_app  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# core.bot_catalog.families_for_script — reverse of script_for_tag()
# ─────────────────────────────────────────────────────────────────────────────


def test_families_for_script_single_family():
    assert bot_catalog.families_for_script("13_ai_rub_bot.py") == ["RUB"]


def test_families_for_script_sniper_returns_both_generations():
    # 25_smc_ml_sniper.py posts under both the BB and TD families.
    assert bot_catalog.families_for_script("25_smc_ml_sniper.py") == ["BB", "TD"]


def test_families_for_script_mis_returns_both_tag_families():
    # 11_ai_mis_bot.py carries both the MIS family and its historical MSI typo.
    assert bot_catalog.families_for_script("11_ai_mis_bot.py") == ["MIS", "MSI"]


def test_families_for_script_xsm_returns_both_tag_families():
    # 39_ai_xsm1_bot.py posts under both the XSM and XSR (reversal) families.
    assert bot_catalog.families_for_script("39_ai_xsm1_bot.py") == ["XSM", "XSR"]


def test_families_for_script_classic_detector_returns_all_five():
    families = bot_catalog.families_for_script("3_detectors.py")
    assert families == sorted(["5Percent", "FastInOut", "Main Channel", "SR", "VolIndic"])


def test_families_for_script_unmapped_script_returns_empty():
    assert bot_catalog.families_for_script("1_data_ingestion.py") == []
    assert bot_catalog.families_for_script("no_such_script.py") == []


# ─────────────────────────────────────────────────────────────────────────────
# core.process_control.parked_since — read-only marker-mtime lookup
# ─────────────────────────────────────────────────────────────────────────────


def test_parked_since_none_when_not_parked(tmp_path, monkeypatch):
    monkeypatch.setattr(process_control, "_PARKED_DIR", tmp_path / "parked")
    assert process_control.parked_since("11_ai_mis_bot.py") is None


def test_parked_since_returns_marker_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr(process_control, "_PARKED_DIR", tmp_path / "parked")
    process_control.park("11_ai_mis_bot.py")
    since = process_control.parked_since("11_ai_mis_bot.py")
    assert since is not None
    assert abs(time.time() - since) < 30  # just-created marker

    process_control.unpark("11_ai_mis_bot.py")
    assert process_control.parked_since("11_ai_mis_bot.py") is None


def test_parked_since_degrades_on_transient_os_error(tmp_path, monkeypatch):
    """A non-FileNotFound OSError on .stat() (e.g. a transient Windows file
    lock) must degrade to None on the render path, never propagate a 500."""
    monkeypatch.setattr(process_control, "_PARKED_DIR", tmp_path / "parked")
    process_control.park("11_ai_mis_bot.py")

    def _boom(self):
        raise PermissionError("stat locked")

    monkeypatch.setattr(process_control.Path, "stat", _boom)
    assert process_control.parked_since("11_ai_mis_bot.py") is None


# ─────────────────────────────────────────────────────────────────────────────
# tools.dashboard.app._live_model_configs — root *_meta.json scan
# ─────────────────────────────────────────────────────────────────────────────


def test_live_model_configs_reads_model_id_and_threshold(tmp_path):
    (tmp_path / "rub2_model_SHORT_meta.json").write_text(
        '{"model_id": "RUB2", "direction": "SHORT", "optimal_threshold": 0.829, '
        '"model_type": "binary"}',
        encoding="utf-8",
    )
    configs = dashboard_app._live_model_configs(tmp_path)
    assert configs == {"RUB2": {"SHORT": {"threshold": 0.829, "model_type": "binary"}}}


def test_live_model_configs_skips_files_without_model_id(tmp_path):
    # Mirrors the real bt2_model_SHORT_meta.json orphan: no model_id field.
    (tmp_path / "bt2_model_SHORT_meta.json").write_text(
        '{"strategy": "abr1", "direction": "SHORT", "optimal_threshold": 0.75}',
        encoding="utf-8",
    )
    assert dashboard_app._live_model_configs(tmp_path) == {}


def test_live_model_configs_skips_unparseable_json(tmp_path):
    (tmp_path / "broken_meta.json").write_text("{not valid json", encoding="utf-8")
    assert dashboard_app._live_model_configs(tmp_path) == {}


def test_live_config_surfaces_through_prefix_match_end_to_end(tmp_path):
    """Integration guard for the prefix-vs-versioned-key HIGH bug: the real
    _live_model_configs() keys by the versioned model_id ("RUB2"), while
    families_for_script("13_ai_rub_bot.py") yields the generation-agnostic
    prefix ("RUB"). _config_label() must bridge that gap by PREFIX — an exact
    match would render "—" for every real bot despite live thresholds.

    Mutation check: with an exact-match _config_label this asserts red (label
    == "—"); with the prefix match it is green (threshold surfaces)."""
    (tmp_path / "rub2_model_SHORT_meta.json").write_text(
        '{"model_id": "RUB2", "direction": "SHORT", "optimal_threshold": 0.829, '
        '"model_type": "binary (1=TP1-first-touch)"}',
        encoding="utf-8",
    )
    configs = dashboard_app._live_model_configs(tmp_path)
    assert set(configs) == {"RUB2"}  # keyed by the versioned model_id, not "RUB"

    families = bot_catalog.families_for_script("13_ai_rub_bot.py")
    assert families == ["RUB"]  # generation-agnostic prefix

    label = dashboard_app._config_label(families, configs)
    assert label != "—"          # the bug rendered this as "—"
    assert "SHORT thr=0.829" in label


# ─────────────────────────────────────────────────────────────────────────────
# fleet_registry_rows() — the pure data-shaping function
# ─────────────────────────────────────────────────────────────────────────────

_FLEET = [
    {"name": "AI RUB1 Detector", "script": "13_ai_rub_bot.py", "group": "ai"},
    {"name": "AI ATS1 Detector", "script": "12_ai_ats_bot.py", "group": "ai"},
]


def test_fleet_registry_rows_active_bot_has_no_config_and_no_since():
    rows = dashboard_app.fleet_registry_rows(
        fleet=_FLEET, parked=set(), parked_since_fn=lambda s: None, configs={}
    )
    ats = next(r for r in rows if r["script"] == "12_ai_ats_bot.py")
    assert ats["model_tag"] == "ATS"
    assert ats["parked"] is False
    assert ats["config"] == "—"       # no meta.json config found -> dash, not fabricated
    assert ats["parked_since"] is None  # active bots never get a fabricated "since"


def test_fleet_registry_rows_parked_bot_shows_since_and_config():
    fixed_epoch = 1752600000.0  # 2025-07-15T... deterministic, not "now"

    def fake_since(script: str) -> float | None:
        return fixed_epoch if script == "13_ai_rub_bot.py" else None

    configs = {"RUB2": {"SHORT": {"threshold": 0.829, "model_type": "binary"}}}
    rows = dashboard_app.fleet_registry_rows(
        fleet=_FLEET, parked={"13_ai_rub_bot.py"}, parked_since_fn=fake_since, configs=configs
    )
    rub = next(r for r in rows if r["script"] == "13_ai_rub_bot.py")
    assert rub["model_tag"] == "RUB"
    assert rub["parked"] is True
    assert rub["config"] == "SHORT thr=0.829"
    assert rub["parked_since"] is not None

    ats = next(r for r in rows if r["script"] == "12_ai_ats_bot.py")
    assert ats["parked"] is False
    assert ats["parked_since"] is None  # not parked -> since is never computed/shown


def test_fleet_registry_rows_multi_direction_config_sorted_and_joined():
    configs = {
        "ATS2": {
            "LONG": {"threshold": 0.7, "model_type": "binary"},
            "SHORT": {"threshold": 0.8, "model_type": "binary"},
        }
    }
    rows = dashboard_app.fleet_registry_rows(
        fleet=_FLEET, parked=set(), parked_since_fn=lambda s: None, configs=configs
    )
    ats = next(r for r in rows if r["script"] == "12_ai_ats_bot.py")
    assert ats["config"] == "LONG thr=0.700, SHORT thr=0.800"


def test_fleet_registry_rows_threshold_none_renders_dash_not_crash():
    configs = {"RUB2": {"SHORT": {"threshold": None, "model_type": "binary"}}}
    rows = dashboard_app.fleet_registry_rows(
        fleet=_FLEET, parked=set(), parked_since_fn=lambda s: None, configs=configs
    )
    rub = next(r for r in rows if r["script"] == "13_ai_rub_bot.py")
    assert rub["config"] == "SHORT thr=—"


def test_fleet_registry_rows_covers_every_fleet_entry_with_real_fleet():
    # Real core.fleet.FLEET, but injected empty parked/configs so this does not
    # depend on the worktree's actual control/parked/ state or root meta.json
    # content — just proves every bot gets exactly one row, no crashes.
    rows = dashboard_app.fleet_registry_rows(parked=set(), parked_since_fn=lambda s: None, configs={})
    from core.fleet import FLEET

    assert len(rows) == len(FLEET)
    assert {r["script"] for r in rows} == {e["script"] for e in FLEET}


# ─────────────────────────────────────────────────────────────────────────────
# Flask route — /panels/fleet-registry
# ─────────────────────────────────────────────────────────────────────────────


def _empty_duckdb(tmp_path) -> str:
    duckdb_path = str(tmp_path / "analytics.duckdb")
    AnalyticsExporter(
        duckdb_path, str(tmp_path / "pq"),
        _EmptyFetcher(), sources=[SOURCES_BY_NAME["closed_ai_signals"]],
    ).run()
    return duckdb_path


class _EmptyFetcher:
    def fetch(self, spec, cursor, limit):
        return []


@pytest.fixture()
def client_with_synthetic_fleet(tmp_path, monkeypatch):
    """App wired so /panels/fleet-registry renders from synthetic, injected
    fleet/parked/config data — never the worktree's real fleet/control state."""
    monkeypatch.setattr(dashboard_app, "FLEET", _FLEET)
    monkeypatch.setattr(dashboard_app, "list_parked", lambda: {"13_ai_rub_bot.py"})
    monkeypatch.setattr(
        dashboard_app, "parked_since",
        lambda s: 1752600000.0 if s == "13_ai_rub_bot.py" else None,
    )
    monkeypatch.setattr(
        dashboard_app, "_live_model_configs",
        lambda repo_root: {"RUB2": {"SHORT": {"threshold": 0.829, "model_type": "binary"}}},
    )
    app = dashboard_app.create_app(_empty_duckdb(tmp_path))
    app.config.update(TESTING=True)
    return app.test_client()


def test_panel_route_returns_200_with_expected_rows(client_with_synthetic_fleet):
    resp = client_with_synthetic_fleet.get("/panels/fleet-registry")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "AI RUB1 Detector" in html
    assert "AI ATS1 Detector" in html
    assert "RUB" in html
    assert "SHORT thr=0.829" in html


def test_panel_route_distinguishes_parked_vs_active(client_with_synthetic_fleet):
    html = client_with_synthetic_fleet.get("/panels/fleet-registry").get_data(as_text=True)
    assert "Parked" in html
    assert "Active" in html
    # The parked bot's row carries a since-date; count of dash-only actives
    # differs — smoke check both states rendered distinctly.
    assert html.count("badge--stale") == 1  # only the parked bot (13_ai_rub_bot.py)
    assert html.count("badge--fresh") == 1  # only the active bot (12_ai_ats_bot.py)


def test_index_includes_fleet_registry_panel(client_with_synthetic_fleet):
    html = client_with_synthetic_fleet.get("/").get_data(as_text=True)
    assert 'hx-get="/panels/fleet-registry"' in html
    assert 'id="fleet-registry-body"' in html


def test_panel_route_never_touches_postgres(client_with_synthetic_fleet, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("fleet-registry route touched Postgres")

    monkeypatch.setattr(analytics_export.PostgresFetcher, "_connection", _boom)
    monkeypatch.setattr(analytics_export.PostgresFetcher, "fetch", _boom)
    assert client_with_synthetic_fleet.get("/panels/fleet-registry").status_code == 200


def test_panel_route_works_with_real_defaults(tmp_path):
    """No monkeypatching: exercises the real core.fleet.FLEET / list_parked /
    parked_since / root *_meta.json wiring end to end. This worktree has no
    control/parked/ directory, so every bot renders Active — the route must
    still succeed (list_parked() degrades to an empty set, not an error)."""
    app = dashboard_app.create_app(_empty_duckdb(tmp_path))
    app.config.update(TESTING=True)
    client = app.test_client()
    resp = client.get("/panels/fleet-registry")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "AI RUB1 Detector" in html or "RUB" in html  # real fleet renders something
    assert "Active" in html
