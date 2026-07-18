# tools/dashboard/app.py — Z1 dashboard shell (Task 0, T-2026-CU-9050-151)
"""Flask + HTMX shell for the Z1 dashboard.

WHY THIS EXISTS
    The framework gate (D-2026-CLD-111, z-council) fixed the stack: Flask +
    HTMX + interval polling, no FastAPI, no SPA, no on-box Node build. This
    module is Task 0 — the load-bearing shell every later feature panel plugs
    into. It stands up a Flask app factory that MOUNTS the read-only analytics
    blueprint from ``tools/analytics_api.py`` (the T-131 DuckDB substrate), a
    responsive HTMX base layout, the shared chart-lifecycle JS helper, one demo
    panel (success rate) as an end-to-end proof, a data-freshness badge, and a
    waitress serving entrypoint bound to 127.0.0.1.

    It lives PARALLEL to the legacy ``dashboard.py`` (bot control / SSE log
    stream) — that file is untouched; this is the new analytics surface.

DB-FREE BY DESIGN
    Like the substrate it builds on, every read path goes through DuckDB only —
    never live Postgres (Gutachten-Option A: analytics must not compete with
    ingestion on the VPS). Importing this module opens no connection of any
    kind; each request opens its own throttled read-only DuckDB connection via
    ``analytics_api.connect_ro`` and closes it. The build machine has no DB
    credentials, so the whole shell is testable offline
    (backtest/test_dashboard_shell.py).

TIMEZONE (R3 minefield — see tools/analytics_export.py TIMEZONE note)
    The freshness rows carry ``synced_at`` as a UTC wall clock and
    ``last_row_ts`` as a naive Europe/Bucharest wall clock. :func:`freshness_summary`
    computes the "Sync vor N min" age STRICTLY from ``synced_at`` against a UTC
    ``now`` — it never subtracts across the two spaces. ``last_row_ts`` is only
    ever rendered as a wall-clock label ("Stand HH:MM"), never differenced.

Invariants:
    * No import-time or request-time Postgres connection — DuckDB is the only
      data source, exactly as in the analytics substrate.
    * The serving entrypoint binds 127.0.0.1 only; a public bind is a deliberate
      operator decision made at the reverse proxy (Cloudflare Access), never here
      (P0.8 lesson).
    * "Sync vor N min" is derived only from ``synced_at`` (UTC); ``last_row_ts``
      (naive-local) is never mixed into an age computation.
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import json
from pathlib import Path
from typing import Any, Callable, Sequence

from core.bot_catalog import families_for_script
from core.fleet import FLEET
from core.process_control import list_parked, parked_since
from core.time import from_unix_ts
from tools.analytics_api import (
    DEFAULT_DIGEST_WINDOW_HOURS,
    DEFAULT_TIMESERIES_WINDOW,
    TIMESERIES_WINDOWS,
    _serve,
    available_bots,
    bot_leaderboard,
    bot_regime_matrix,
    build_analytics_blueprint,
    coin_trade_series,
    coins_with_trades,
    connect_ro,
    overnight_digest,
    rolling_success_rate_series,
    success_rate_timeseries,
)
from tools.analytics_export import data_freshness

# The demo panel shows two of the substrate's rolling windows; the third (90d)
# stays available on the JSON API but keeps the shell's proof panel compact.
DEMO_WINDOWS: tuple[int, ...] = (7, 30)
# The window whose winrate the demo panel headlines (must be in DEMO_WINDOWS).
DEMO_PRIMARY_WINDOW = 30

# Default poll cadence for the demo panel (seconds). Matches the 30–60 s band
# the substrate's server cache is tuned for (D-2026-CLD-110).
PANEL_POLL_SECONDS = 30
BADGE_POLL_SECONDS = 60


def _parse_ts(value: str | None) -> datetime.datetime | None:
    """Parse an ISO timestamp from a freshness row, or None. Never raises on the
    render path — a malformed stored value degrades to "unknown", not a 500."""
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None


def freshness_summary(
    rows: Sequence[dict[str, Any]],
    *,
    now_utc: datetime.datetime | None = None,
    sources: Sequence[str] | None = None,
    worst_case: bool = False,
) -> dict[str, Any]:
    """Collapse the per-source freshness rows into one badge summary.

    Args:
        rows: the output of ``analytics_export.data_freshness`` (one dict per
            source, with ``synced_at`` = UTC wall clock and ``last_row_ts`` =
            naive-local wall clock, both ISO strings or None).
        now_utc: naive datetime standing for the current UTC wall clock; defaults
            to now. Injected in tests for a deterministic age.
        sources: additive filter (Feature 4, T-2026-CU-9050-156) — when given,
            ``rows`` is narrowed to entries whose ``source`` is in this set
            BEFORE aggregation, so a panel backed by only some of the exported
            sources can get its OWN freshness rather than the fleet-wide one.
            ``None`` (the default) reproduces the original unfiltered
            behaviour exactly — every pre-existing caller is unaffected.
        worst_case: additive toggle (Feature 4, T-2026-CU-9050-156). The
            shell-global badge (default ``False``) reports the FRESHEST
            (most-recently-synced) source across ``rows`` — "is the pipeline
            alive at all". A per-panel badge that combines several sources
            needs the opposite: the data a panel renders is only as fresh as
            its STALEST contributing source, so ``True`` picks the OLDEST
            ``synced_at``/``last_row_ts`` instead — never an average, never
            the freshest. Default ``False`` reproduces the original
            behaviour exactly.

    Returns a JSON-serialisable dict:
        {
          "stand": "HH:MM"|None,        # latest data wall-clock (naive-local)
          "stand_date": "YYYY-MM-DD"|None,
          "sync_age_min": int|None,     # minutes since the latest synced_at (UTC)
          "synced_at": ISO|None,
          "sources": int,               # number of sources contributing
          "label": str,                 # ready-to-render "Stand …, Sync vor … min"
        }

    The age is computed ONLY from ``synced_at`` (UTC) vs ``now_utc`` (UTC); the
    naive-local ``last_row_ts`` is never differenced against a UTC clock — it is
    a display label only (TIMEZONE invariant).
    """
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    elif now_utc.tzinfo is not None:
        now_utc = now_utc.replace(tzinfo=None)

    if sources is not None:
        wanted = set(sources)
        rows = [r for r in rows if r.get("source") in wanted]

    synced = [ts for ts in (_parse_ts(r.get("synced_at")) for r in rows) if ts is not None]
    last_rows = [ts for ts in (_parse_ts(r.get("last_row_ts")) for r in rows) if ts is not None]

    pick = min if worst_case else max
    latest_sync = pick(synced) if synced else None
    latest_row = pick(last_rows) if last_rows else None

    sync_age_min: int | None = None
    if latest_sync is not None:
        # Clamp at zero: a small clock skew must not render a negative age.
        age_s = (now_utc - latest_sync).total_seconds()
        sync_age_min = max(0, int(age_s // 60))

    stand = latest_row.strftime("%H:%M") if latest_row is not None else None
    stand_date = latest_row.strftime("%Y-%m-%d") if latest_row is not None else None

    parts: list[str] = []
    if stand is not None:
        parts.append(f"Stand {stand}")
    if sync_age_min is not None:
        parts.append(f"Sync vor {sync_age_min} min")
    label = ", ".join(parts) if parts else "Kein Datenstand"

    return {
        "stand": stand,
        "stand_date": stand_date,
        "sync_age_min": sync_age_min,
        "synced_at": latest_sync.isoformat() if latest_sync is not None else None,
        "sources": len(rows),
        "label": label,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Per-panel data-freshness (Feature 4, T-2026-CU-9050-156)
# ─────────────────────────────────────────────────────────────────────────────

# Panel route name -> the analytics_export source name(s) that panel actually
# reads. Both success-rate panels and the leaderboard aggregate outcomes
# across BOTH outcome tables (analytics_api._OUTCOME_TABLES: closed_ai_signals
# + closed_trades), so all three share the same two-source tuple. An empty
# tuple means "no DuckDB-synced source at all" — currently only
# fleet-registry, which reads core.fleet.FLEET + filesystem markers directly
# on every request and never goes through the T-131 export.
PANEL_SOURCES: dict[str, tuple[str, ...]] = {
    "success-rate": ("closed_ai_signals", "closed_trades"),
    "success-rate-timeseries": ("closed_ai_signals", "closed_trades"),
    "leaderboard": ("closed_ai_signals", "closed_trades"),
    "fleet-registry": (),
    # Feature 6 (T-2026-CU-9050-158): the heatmap joins decisive trades against
    # regime_history, so ITS staleness matters too, not just the outcome
    # table's — worst_case=True (panel_freshness_summary's default) already
    # picks whichever of the two is older.
    "regime-heatmap": ("closed_ai_signals", "regime_history"),
    # Feature 7 (T-2026-CU-9050-159): the coin drill-down reads the same two
    # outcome tables as the leaderboard/success-rate panels (via the coin-
    # aware CTE), so its freshness shares that pair.
    "coin-drilldown": ("closed_ai_signals", "closed_trades"),
    # Feature 8 (T-2026-CU-9050-160): the overnight digest reads the same two
    # outcome tables (via the coin-aware CTE, like the coin drill-down) PLUS
    # regime_history for its (optional) regime-transition count — all three
    # matter for the panel's own worst-case freshness.
    "overnight-digest": ("closed_ai_signals", "closed_trades", "regime_history"),
}

# fleet-registry has no DuckDB sync to report a synced_at/last_row_ts for —
# rendering a fabricated timestamp would misrepresent a file-based read as a
# stale-or-fresh export. This fixed marker is the only value that reaches the
# template for such a panel (panel_freshness_summary() below never falls
# through to freshness_summary() for it).
FILE_BASED_FRESHNESS: dict[str, Any] = {
    "stand": None,
    "stand_date": None,
    "sync_age_min": None,
    "synced_at": None,
    "sources": 0,
    "label": "Live (dateibasiert)",
    "file_based": True,
}


def panel_freshness_summary(
    rows: Sequence[dict[str, Any]],
    panel: str,
    *,
    now_utc: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Per-panel data-freshness summary — the panel-specific refinement of the
    shell-global :func:`freshness_summary` badge.

    Named distinctly from the nested ``/panels/freshness`` route handler
    inside :func:`create_app` (T-2026-CU-9050-157 nit cleanup — the two used
    to share the name ``panel_freshness``, a collision waiting to bite the
    first refactor that moved either one into the other's scope).

    Narrows ``rows`` (the full ``analytics_export.data_freshness`` output) down
    to ``panel``'s own source(s) via :data:`PANEL_SOURCES`, then delegates to
    ``freshness_summary(..., worst_case=True)`` — so a panel backed by two
    sources with different sync times always shows the STALER one, never an
    average or the fresher one (Z1-curation requirement: worst-case per panel,
    the opposite of the shell-global badge's freshest-wins default).

    A panel registered with an empty source tuple (``fleet-registry``) has no
    DuckDB sync to report at all and resolves to :data:`FILE_BASED_FRESHNESS`
    without touching ``rows``. An unknown ``panel`` name raises ``ValueError``
    rather than silently degrading to that same file-based marker — a wrong
    panel→source mapping must be loud, not swallowed.
    """
    if panel not in PANEL_SOURCES:
        raise ValueError(f"panel_freshness_summary: unknown panel {panel!r}")
    sources = PANEL_SOURCES[panel]
    if not sources:
        return dict(FILE_BASED_FRESHNESS)
    return freshness_summary(rows, now_utc=now_utc, sources=sources, worst_case=True)


def _demo_panel_context(duckdb_path: str) -> dict[str, Any]:
    """Server-side render context for the success-rate demo panel.

    Opens a throttled read-only DuckDB connection, computes the rolling
    success-rate windows, and reshapes them into a per-bot table the template
    can iterate — plus a compact chart series proving the chart-lifecycle wiring.
    """
    con = connect_ro(duckdb_path)
    try:
        payload = success_rate_timeseries(con, windows=DEMO_WINDOWS)
        freshness_rows = data_freshness(con)
    finally:
        con.close()

    # Reshape windows → one row per bot with each window's winrate/n.
    by_bot: dict[str, dict[str, Any]] = {}
    for window in DEMO_WINDOWS:
        for entry in payload["windows"].get(window, []):
            row = by_bot.setdefault(entry["bot"], {"bot": entry["bot"], "windows": {}})
            row["windows"][window] = {
                "n": entry["n"],
                "wins": entry["wins"],
                "winrate": entry["winrate"],
                "winrate_pct": (round(entry["winrate"] * 100, 1) if entry["winrate"] is not None else None),
            }
    bots = [by_bot[b] for b in sorted(by_bot)]

    # Chart series: primary-window winrate per bot (for the ECharts bar chart the
    # lifecycle helper mounts/disposes). Bots with no decisive trade in the
    # primary window are omitted from the series.
    chart_series = [
        {"bot": row["bot"], "winrate_pct": row["windows"][DEMO_PRIMARY_WINDOW]["winrate_pct"]}
        for row in bots
        if DEMO_PRIMARY_WINDOW in row["windows"] and row["windows"][DEMO_PRIMARY_WINDOW]["winrate_pct"] is not None
    ]

    return {
        "as_of": payload["as_of"],
        "windows": list(DEMO_WINDOWS),
        "primary_window": DEMO_PRIMARY_WINDOW,
        "bots": bots,
        "chart_series": chart_series,
        "poll_seconds": PANEL_POLL_SECONDS,
        "freshness": panel_freshness_summary(freshness_rows, "success-rate"),
    }


def _freshness_context(duckdb_path: str) -> dict[str, Any]:
    con = connect_ro(duckdb_path)
    try:
        rows = data_freshness(con)
    finally:
        con.close()
    return {"freshness": freshness_summary(rows), "badge_poll_seconds": BADGE_POLL_SECONDS}


# ─────────────────────────────────────────────────────────────────────────────
# Fleet-registry panel (Feature 1, T-2026-CU-9050-152)
# ─────────────────────────────────────────────────────────────────────────────

# Repo root: tools/dashboard/app.py -> tools/dashboard -> tools -> <root>.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _live_model_configs(repo_root: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Model-tag family (upper-cased ``model_id``) -> {direction: {threshold, model_type}}.

    Scans ROOT-level ``*_meta.json`` only — those are the LIVE artifacts
    (CLAUDE.md rule 2: promotion into the repo root is the operator's live
    decision; ``staging_models/`` is pre-promotion and would misrepresent what
    a bot currently serves, so it is deliberately excluded here). Files
    without a ``model_id`` field (legacy/orphaned artifact dumps such as
    ``bt2_model_SHORT_meta.json``) are skipped rather than guessed from the
    filename — a wrong family match would be worse than an absent one.
    """
    configs: dict[str, dict[str, dict[str, Any]]] = {}
    for meta_path in sorted(repo_root.glob("*_meta.json")):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        model_id = data.get("model_id")
        if not model_id:
            continue
        family = str(model_id).upper()
        direction = data.get("direction") or "?"
        configs.setdefault(family, {})[direction] = {
            "threshold": data.get("optimal_threshold"),
            "model_type": data.get("model_type"),
        }
    return configs


def _config_label(families: Sequence[str], configs: dict[str, dict[str, dict[str, Any]]]) -> str:
    """Human-readable Kernparameter summary across a bot's model families.

    Matching is family-PREFIX based, not exact: ``families_for_script`` yields
    generation-agnostic prefixes (``"RUB"``, ``"MAX"``) while ``configs`` is
    keyed by the full versioned ``model_id`` from the artifact meta (``"RUB2"``,
    ``"MAX1"``) — a rotation-stable join, the same convention core.bot_catalog
    uses for tag→script (OPUS-HANDOFF Falle 16). An exact ``get`` would never
    hit and every real bot would render "—" despite live thresholds existing.

    When several config keys share a family prefix (e.g. a RUB2 and a RUB3
    both live), all their directions are merged. Returns "—" when no config
    key matches any family — never fabricated (DB-free contract: an
    unavailable field renders as a dash, not synthesised from an unrelated
    source)."""
    directions: dict[str, dict[str, Any]] = {}
    prefixes = tuple(f.upper() for f in families)
    for key, by_direction in configs.items():
        if key.upper().startswith(prefixes):
            directions.update(by_direction)
    if not directions:
        return "—"
    parts: list[str] = []
    for direction in sorted(directions):
        thr = directions[direction].get("threshold")
        thr_label = f"{thr:.3f}" if isinstance(thr, (int, float)) else "—"
        parts.append(f"{direction} thr={thr_label}")
    return ", ".join(parts)


def fleet_registry_rows(
    *,
    fleet: Sequence[dict[str, Any]] | None = None,
    parked: set[str] | None = None,
    parked_since_fn: Callable[[str], float | None] | None = None,
    configs: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Per-bot fleet-registry row: model tag(s), live Kernparameter, parked
    status and (for parked bots only) the parked-since wall-clock label.

    Every dependency is injectable so this is testable without touching the
    filesystem or a running fleet. Defaults resolve the real read-only
    sources: ``core.fleet.FLEET``, ``core.process_control.list_parked`` /
    ``parked_since``, and the root-level ``*_meta.json`` artifacts — no
    Postgres, ever (this module's DB-free invariant).

    "Since when active" has no file-based source (only an unpark event would
    carry it, and that is not recorded anywhere) — deliberately left out
    rather than guessed; only "parked since" is ever populated.
    """
    if fleet is None:
        fleet = FLEET
    if parked is None:
        parked = list_parked()
    if parked_since_fn is None:
        parked_since_fn = parked_since
    if configs is None:
        configs = _live_model_configs(_REPO_ROOT)

    rows: list[dict[str, Any]] = []
    for entry in fleet:
        script = entry["script"]
        families = families_for_script(script)
        is_parked = script in parked
        since_label: str | None = None
        if is_parked:
            ts = parked_since_fn(script)
            if ts is not None:
                # Marker mtime is a POSIX epoch (timezone-agnostic); render via
                # the sanctioned UTC converter (core.time, R3 policy) rather
                # than a bare fromtimestamp() — DTZ006-safe and unambiguous.
                since_label = from_unix_ts(ts).strftime("%Y-%m-%d %H:%M UTC")
        rows.append(
            {
                "name": entry["name"],
                "script": script,
                "group": entry.get("group"),
                "model_tag": " / ".join(families) if families else "—",
                "config": _config_label(families, configs),
                "parked": is_parked,
                "parked_since": since_label,
            }
        )
    return rows


def _fleet_registry_context() -> dict[str, Any]:
    return {
        "rows": fleet_registry_rows(),
        "poll_seconds": PANEL_POLL_SECONDS,
        # File-based panel — no DuckDB freshness rows exist to filter, hence
        # the empty list (panel_freshness_summary ignores rows entirely for a
        # panel registered with an empty PANEL_SOURCES tuple).
        "freshness": panel_freshness_summary([], "fleet-registry"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Global success-metric toggle (Feature 5, T-2026-CU-9050-157)
# ─────────────────────────────────────────────────────────────────────────────

# The three metrics the shell-global toggle switches between, in the display
# order the Z1 curation specifies (Winrate / Expectancy / Netto-PnL).
METRICS: tuple[str, ...] = ("winrate", "expectancy", "netto-pnl")

# Netto-PnL is the sensible default — it is also the metric behind
# analytics_api.DEFAULT_LEADERBOARD_SORT ("pnl_sum_pct"), so an unrendered or
# absent toggle reproduces today's leaderboard behaviour exactly.
DEFAULT_METRIC = "netto-pnl"

METRIC_LABELS: dict[str, str] = {
    "winrate": "Winrate",
    "expectancy": "Expectancy",
    "netto-pnl": "Netto-PnL",
}

# metric -> the analytics_api.bot_leaderboard() sort_by key it maps onto. Kept
# in lockstep with analytics_api._LEADERBOARD_SORT_KEYS.
METRIC_SORT_BY: dict[str, str] = {
    "winrate": "winrate",
    "expectancy": "expectancy_pct",
    "netto-pnl": "pnl_sum_pct",
}


def resolve_metric(raw: str | None) -> str:
    """Normalise a raw ``metric`` query-string value to a known metric key.

    Unknown or missing values fall back to :data:`DEFAULT_METRIC` — the
    global toggle must never 500 or propagate a bogus value into a downstream
    sort_by/highlight decision. Mirrors the permissive-fallback contract
    ``analytics_api.bot_leaderboard`` already uses for its own ``sort_by``.
    """
    return raw if raw in METRIC_SORT_BY else DEFAULT_METRIC


def metric_sort_by(metric: str) -> str:
    """The ``bot_leaderboard`` ``sort_by`` key an (already-resolved) metric
    maps onto. Pure lookup, testable without Flask/DuckDB — the mapping logic
    the SPEC calls out as separately verifiable. Falls back to the default
    metric's sort key for a stray unresolved value rather than raising — the
    route layer is the one place a bad ``metric`` must be caught (via
    :func:`resolve_metric`); this stays permissive like ``bot_leaderboard``
    itself.
    """
    return METRIC_SORT_BY.get(metric, METRIC_SORT_BY[DEFAULT_METRIC])


# ─────────────────────────────────────────────────────────────────────────────
# Leaderboard + risk-metrics panel (Feature 2, T-2026-CU-9050-154)
# ─────────────────────────────────────────────────────────────────────────────


def _leaderboard_context(duckdb_path: str, *, metric: str = DEFAULT_METRIC) -> dict[str, Any]:
    """Server-side render context for the leaderboard panel.

    Thin wrapper over ``analytics_api.bot_leaderboard`` — same throttled
    read-only DuckDB connection pattern as the other panel contexts in this
    module. "Active bot" here means "has at least one decisive trade in the
    substrate" (see ``bot_leaderboard`` docstring); it is NOT cross-referenced
    against the fleet-registry's parked status — that is Feature 1's concern
    (SPEC.md Out of Scope).

    ``metric`` (Feature 5, T-2026-CU-9050-157) is the RESOLVED global toggle
    value (already run through ``resolve_metric`` by the caller) — it both
    selects ``bot_leaderboard``'s ``sort_by`` via ``metric_sort_by`` and rides
    along in the returned context so the template can highlight the matching
    column.
    """
    con = connect_ro(duckdb_path)
    try:
        payload = bot_leaderboard(con, sort_by=metric_sort_by(metric))
        freshness_rows = data_freshness(con)
    finally:
        con.close()
    return {
        "bots": payload["bots"],
        "sort_by": payload["sort_by"],
        "metric": metric,
        "metric_label": METRIC_LABELS[metric],
        "poll_seconds": PANEL_POLL_SECONDS,
        "freshness": panel_freshness_summary(freshness_rows, "leaderboard"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Success-rate time-comparison panel (Feature 3, T-2026-CU-9050-155)
# ─────────────────────────────────────────────────────────────────────────────


def _selected_bots(raw_bots: Sequence[str], *, filtered: bool, all_bots: Sequence[str]) -> list[str]:
    """Resolve the bot-multiselect filter, distinguishing "no filter submitted
    yet" (first ``load``, no query string at all) from "user explicitly
    unchecked every box" (a real, submitted, empty selection).

    ``rolling_success_rate_series``/``bot_trade_rows`` treat an EMPTY bots list
    the same as ``None`` (their shared ``_bot_filter`` convention: falsy ==
    unfiltered) — appropriate for a query parameter that is simply absent, but
    wrong for this form: a user who deliberately unchecks every bot must see
    "no bots selected", not silently get every bot back. ``filtered`` (a hidden
    form field, present on every form submission but absent on the initial
    parameterless ``hx-get`` this panel's own polling constructs) is what makes
    that distinction observable at the route layer.
    """
    if filtered:
        return list(raw_bots)
    return list(all_bots)


def _success_rate_timeseries_context(
    duckdb_path: str, *, raw_bots: Sequence[str], filtered: bool, window: int
) -> dict[str, Any]:
    """Server-side render context for the success-rate time-comparison panel.

    Builds the rolling ``window``-day win-rate line series (one line per
    selected bot) via ``analytics_api.rolling_success_rate_series`` — additive
    to the T-131 substrate, same DECISIVE-trade definition, same throttled
    read-only DuckDB connection pattern as every other panel context here.
    """
    con = connect_ro(duckdb_path)
    try:
        all_bots = available_bots(con)
        selected = _selected_bots(raw_bots, filtered=filtered, all_bots=all_bots)
        # An explicit empty selection must never reach the query layer: passing
        # bots=[] there is indistinguishable from bots=None ("no filter") per
        # _bot_filter's convention, which would silently show every bot again.
        payload = (
            rolling_success_rate_series(con, bots=selected, window=window)
            if selected
            else {"window": window, "bots": [], "series": {}}
        )
        freshness_rows = data_freshness(con)
    finally:
        con.close()

    chart_series = [
        {
            "bot": bot,
            "points": [
                {
                    "date": p["date"],
                    "winrate_pct": (round(p["winrate"] * 100, 1) if p["winrate"] is not None else None),
                }
                for p in payload["series"][bot]
            ],
        }
        for bot in payload["bots"]
    ]
    all_dates = sorted({p["date"] for s in chart_series for p in s["points"]})

    return {
        "as_of": all_dates[-1] if all_dates else None,
        "window": window,
        "windows_available": list(TIMESERIES_WINDOWS),
        "all_bots": all_bots,
        "selected_bots": selected,
        "chart_series": chart_series,
        "poll_seconds": PANEL_POLL_SECONDS,
        "freshness": panel_freshness_summary(freshness_rows, "success-rate-timeseries"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Bot x Regime performance heatmap (Feature 6, T-2026-CU-9050-158)
# ─────────────────────────────────────────────────────────────────────────────

# The two cell metrics the heatmap can render, mirroring analytics_api's
# REGIME_MATRIX_METRICS field names but with the toggle's own short query-
# string keys (kept independent of Feature 5's global METRICS/METRIC_SORT_BY —
# this panel highlights a heatmap cell, not a leaderboard sort key).
REGIME_HEATMAP_METRICS: tuple[str, ...] = ("winrate", "expectancy")
DEFAULT_REGIME_HEATMAP_METRIC = "winrate"

REGIME_HEATMAP_METRIC_LABELS: dict[str, str] = {
    "winrate": "Winrate",
    "expectancy": "Ø-PnL je Trade (%)",
}

# metric -> the bot_regime_matrix() cell field it reads.
REGIME_HEATMAP_METRIC_FIELD: dict[str, str] = {
    "winrate": "winrate",
    "expectancy": "expectancy_pct",
}


def resolve_regime_heatmap_metric(raw: str | None) -> str:
    """Normalise a raw ``metric`` query-string value for the heatmap toggle.

    Unknown/missing values fall back to :data:`DEFAULT_REGIME_HEATMAP_METRIC` —
    same permissive-fallback contract as ``resolve_metric`` (Feature 5) and
    ``bot_leaderboard``'s own ``sort_by``: a bad value degrades, never 500s.
    """
    return raw if raw in REGIME_HEATMAP_METRIC_FIELD else DEFAULT_REGIME_HEATMAP_METRIC


def _regime_heatmap_context(duckdb_path: str, *, metric: str = DEFAULT_REGIME_HEATMAP_METRIC) -> dict[str, Any]:
    """Server-side render context for the Bot x Regime heatmap panel.

    Thin wrapper over ``analytics_api.bot_regime_matrix`` — same throttled
    read-only DuckDB connection pattern as every other panel context here.
    Reshapes the matrix into TWO renderable forms so the template never has to
    do lookup logic itself:
      * ``table_rows`` — the no-JS table fallback, one row per bot with one
        cell per regime IN THE SAME COLUMN ORDER as ``regimes`` (``None`` for
        a (bot, regime) pair with no decisive trade — rendered as "—", never a
        fabricated zero).
      * ``chart_data`` — the ECharts heatmap's sparse ``[regime_idx, bot_idx,
        value]`` series. A missing cell contributes NO entry at all (ECharts
        heatmap renders an absent coordinate as empty, not a synthesised 0).
    ``winrate`` values are surfaced as a 0-100 percentage (matching the other
    panels' winrate rendering convention); ``expectancy_pct`` is already a
    %-value and passes through unscaled.
    """
    con = connect_ro(duckdb_path)
    try:
        payload = bot_regime_matrix(con)
        freshness_rows = data_freshness(con)
    finally:
        con.close()

    field = REGIME_HEATMAP_METRIC_FIELD[metric]
    bots = payload["bots"]
    regimes = payload["regimes"]
    cells = payload["cells"]

    def _display_value(cell: dict[str, Any] | None) -> float | None:
        if cell is None or cell.get(field) is None:
            return None
        value = cell[field]
        return round(value * 100, 1) if field == "winrate" else value

    table_rows = [
        {
            "bot": bot,
            "cells": [
                {
                    "n": (cells.get(bot, {}).get(regime) or {}).get("n"),
                    "value": _display_value(cells.get(bot, {}).get(regime)),
                }
                for regime in regimes
            ],
        }
        for bot in bots
    ]

    chart_data: list[list[Any]] = []
    for bi, bot in enumerate(bots):
        for ri, regime in enumerate(regimes):
            value = _display_value(cells.get(bot, {}).get(regime))
            if value is not None:
                chart_data.append([ri, bi, value])

    return {
        "bots": bots,
        "regimes": regimes,
        "table_rows": table_rows,
        "chart_data": chart_data,
        "metric": metric,
        "metric_label": REGIME_HEATMAP_METRIC_LABELS[metric],
        "metrics": REGIME_HEATMAP_METRICS,
        "metric_labels": REGIME_HEATMAP_METRIC_LABELS,
        "is_winrate": field == "winrate",
        "poll_seconds": PANEL_POLL_SECONDS,
        "freshness": panel_freshness_summary(freshness_rows, "regime-heatmap"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Coin drill-down panel (Feature 7, T-2026-CU-9050-159)
# ─────────────────────────────────────────────────────────────────────────────

# Deliberate scope boundary (see tools/dashboard/SPEC.md Feature 7 "Out of
# Scope"): full OHLCV candlesticks are NOT rendered here — the 25GB candle
# export was deferred in T-131 and is not in the DuckDB substrate. This panel
# draws only the decisive-trade PRICE PATH (entry->exit points per trade,
# connected in close-time order), never a fabricated market candle.


def _resolve_coin(raw_coin: str | None, coins: Sequence[str]) -> str | None:
    """Resolve the requested ``?coin=`` query value to one of ``coins``.

    No query value at all (``raw_coin is None`` — the very first, param-less
    load) defaults to the FIRST available coin, so the drill-down shows
    something useful immediately rather than an empty "please choose" state.
    An EXPLICIT but unknown/empty value (present in the query string, just
    not a coin with decisive trades) resolves to ``None`` — the caller then
    renders a clean "unknown coin" hint, never silently substituting a
    different coin's data for the one that was asked for.
    """
    if raw_coin is None:
        return coins[0] if coins else None
    return raw_coin if raw_coin in coins else None


def _epoch_utc(iso_ts: str) -> int:
    """Naive-local ISO datetime string -> UTC-labelled epoch seconds.

    ``closed_at`` timestamps in this substrate are naive-local wall clocks
    (TIMEZONE contract, see analytics_export/app.py module docstrings) — this
    helper does NOT apply any zone conversion, it just maps the wall-clock
    FIELDS onto an epoch via ``calendar.timegm`` (which treats a struct_time
    as UTC without consulting the OS zone). That keeps the mapping a pure,
    machine-independent function of the stored fields, appropriate for a
    Lightweight Charts ``UTCTimestamp`` x-axis that only needs strictly
    increasing, internally-consistent ordering — never a real UTC instant.
    """
    return calendar.timegm(datetime.datetime.fromisoformat(iso_ts).timetuple())


def _coin_chart_series(trades: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the Lightweight Charts line-series points + trade markers for one
    coin's ordered decisive trades.

    Each trade contributes TWO line points — an "entry" point just before its
    ``closed_at`` and an "exit" point AT its ``closed_at`` — so the rendered
    line traces the entry->exit price move of every decisive trade in close-
    time order (SPEC.md: "Preislinie (Entry->Exit-Punkte ueber close_time)").
    Lightweight Charts requires strictly increasing point times; consecutive
    trades whose entry/exit epochs would collide or go backwards are bumped
    forward by whole seconds deterministically, so ordering never depends on
    real-world trade spacing.

    Markers sit at each trade's exit point, colour/shape-coded win (green,
    up-arrow) vs. loss (red, down-arrow) — the "Win/Loss-farbige Marker je
    Trade" requirement.
    """
    points: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []
    last_t: int | None = None
    for trade in trades:
        exit_t = _epoch_utc(trade["closed_at"])
        entry_t = exit_t - 1
        if last_t is not None and entry_t <= last_t:
            entry_t = last_t + 1
        if exit_t <= entry_t:
            exit_t = entry_t + 1
        points.append({"time": entry_t, "value": trade["entry"]})
        points.append({"time": exit_t, "value": trade["close_price"]})
        markers.append(
            {
                "time": exit_t,
                "position": "aboveBar" if trade["is_win"] else "belowBar",
                "color": "#3fb950" if trade["is_win"] else "#d29922",
                "shape": "arrowUp" if trade["is_win"] else "arrowDown",
                "text": f"{trade['bot']} {trade['pnl_pct']:+.2f}%",
            }
        )
        last_t = exit_t
    return points, markers


def _coin_drilldown_context(duckdb_path: str, *, raw_coin: str | None) -> dict[str, Any]:
    """Server-side render context for the coin drill-down panel.

    ``raw_coin`` is the UNRESOLVED ``?coin=`` query value (``None`` when the
    param is absent at all) — resolution against the actual coin list happens
    here via :func:`_resolve_coin`, mirroring every other panel context's
    "resolve inside the context, never trust the raw query value past this
    point" contract.
    """
    con = connect_ro(duckdb_path)
    try:
        coins = coins_with_trades(con)
        selected_coin = _resolve_coin(raw_coin, coins)
        payload = coin_trade_series(con, selected_coin) if selected_coin else {"coin": raw_coin, "trades": []}
        freshness_rows = data_freshness(con)
    finally:
        con.close()

    trades = payload["trades"]
    chart_points, chart_markers = _coin_chart_series(trades)

    return {
        "coins": coins,
        "selected_coin": selected_coin,
        "requested_coin": raw_coin,
        "trades": trades,
        "chart_points": chart_points,
        "chart_markers": chart_markers,
        "poll_seconds": PANEL_POLL_SECONDS,
        "freshness": panel_freshness_summary(freshness_rows, "coin-drilldown"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Overnight-digest landing summary (Feature 8, T-2026-CU-9050-160)
# ─────────────────────────────────────────────────────────────────────────────

# Window choices the digest's own toggle switches between (trailing hours).
# 8h is the "Overnight" default the SPEC names explicitly; 24h/168h (7 days)
# are the additive "…|…" options the SPEC allows without prescribing more.
DIGEST_WINDOW_OPTIONS: tuple[int, ...] = (8, 24, 168)

DIGEST_WINDOW_LABELS: dict[int, str] = {
    8: "Overnight (8h)",
    24: "24h",
    168: "7 Tage",
}


def resolve_digest_window(raw: str | None) -> int:
    """Normalise a raw ``?window=`` query value (``"8h"``, ``"24"``, ``None``, …)
    to one of :data:`DIGEST_WINDOW_OPTIONS`, in trailing HOURS.

    Accepts an optional trailing ``h`` suffix (``"8h"``) or a bare integer
    (``"8"``); anything that does not parse to one of the known window
    options — missing, malformed, or simply a number nobody offered as a
    toggle choice — falls back to :data:`DEFAULT_DIGEST_WINDOW_HOURS` rather
    than raising, the same permissive-fallback contract every other
    query-string resolver in this module already uses (``resolve_metric``,
    ``resolve_regime_heatmap_metric``): a bad value must degrade, never 500.
    """
    if not raw:
        return DEFAULT_DIGEST_WINDOW_HOURS
    text = raw.strip().lower()
    if text.endswith("h"):
        text = text[:-1]
    try:
        hours = int(text)
    except ValueError:
        return DEFAULT_DIGEST_WINDOW_HOURS
    return hours if hours in DIGEST_WINDOW_OPTIONS else DEFAULT_DIGEST_WINDOW_HOURS


def _digest_context(duckdb_path: str, *, window_hours: int = DEFAULT_DIGEST_WINDOW_HOURS) -> dict[str, Any]:
    """Server-side render context for the overnight-digest landing panel.

    Thin wrapper over ``analytics_api.overnight_digest`` — same throttled
    read-only DuckDB connection pattern as every other panel context here.
    """
    con = connect_ro(duckdb_path)
    try:
        payload = overnight_digest(con, window_hours)
        freshness_rows = data_freshness(con)
    finally:
        con.close()
    return {
        "digest": payload,
        "window_hours": window_hours,
        "window_options": DIGEST_WINDOW_OPTIONS,
        "window_labels": DIGEST_WINDOW_LABELS,
        "poll_seconds": PANEL_POLL_SECONDS,
        "freshness": panel_freshness_summary(freshness_rows, "overnight-digest"),
    }


def create_app(duckdb_path: str | Path, *, cache_enabled: bool = True):
    """Flask app for the Z1 dashboard shell.

    Mounts the read-only analytics JSON blueprint (``/api/analytics/*``) and
    serves the HTML shell + HTMX panels. ``duckdb_path`` is the single-writer
    analytics DuckDB file produced by ``tools/analytics_export.py``.
    """
    from flask import Flask, render_template, request

    app = Flask(__name__)  # templates/ and static/ resolve under this package dir
    path = str(duckdb_path)

    # Mount the existing read-only analytics endpoints (T-131 substrate).
    app.register_blueprint(build_analytics_blueprint(path, cache_enabled=cache_enabled))

    @app.get("/")
    def index():
        # Feature 5 (T-2026-CU-9050-157): the global success-metric toggle.
        # Resolved here (never a bogus value past this point) and threaded
        # into the leaderboard panel's own poll URL so its "every Ns" HTMX
        # polling keeps using the SAME metric the shell was loaded with,
        # without a separate round-trip or JS state.
        metric = resolve_metric(request.args.get("metric"))
        return render_template(
            "index.html",
            panel_poll_seconds=PANEL_POLL_SECONDS,
            metric=metric,
            metrics=METRICS,
            metric_labels=METRIC_LABELS,
            **_freshness_context(path),
        )

    @app.get("/panels/success-rate")
    def panel_success_rate():
        return render_template("panels/success_rate.html", **_demo_panel_context(path))

    @app.get("/panels/freshness")
    def panel_freshness():
        return render_template("_freshness_badge.html", **_freshness_context(path))

    @app.get("/panels/fleet-registry")
    def panel_fleet_registry():
        return render_template("panels/fleet_registry.html", **_fleet_registry_context())

    @app.get("/panels/leaderboard")
    def panel_leaderboard():
        # Feature 5 (T-2026-CU-9050-157): baked into this panel's own hx-get
        # URL by index.html (?metric=...), so both the initial load and every
        # subsequent poll resolve the same way.
        metric = resolve_metric(request.args.get("metric"))
        return render_template("panels/leaderboard.html", **_leaderboard_context(path, metric=metric))

    @app.get("/panels/success-rate-timeseries")
    def panel_success_rate_timeseries():
        filtered = "filtered" in request.args
        raw_bots = request.args.getlist("bots")
        try:
            window = int(request.args.get("window", DEFAULT_TIMESERIES_WINDOW))
        except (TypeError, ValueError):
            window = DEFAULT_TIMESERIES_WINDOW
        if window not in TIMESERIES_WINDOWS:
            window = DEFAULT_TIMESERIES_WINDOW
        return render_template(
            "panels/success_rate_timeseries.html",
            **_success_rate_timeseries_context(path, raw_bots=raw_bots, filtered=filtered, window=window),
        )

    @app.get("/panels/regime-heatmap")
    def panel_regime_heatmap():
        # Feature 6 (T-2026-CU-9050-158): local metric toggle (winrate/
        # expectancy), baked into this panel's own hx-get URL exactly like
        # Feature 5's global toggle does for the leaderboard.
        metric = resolve_regime_heatmap_metric(request.args.get("metric"))
        return render_template("panels/regime_heatmap.html", **_regime_heatmap_context(path, metric=metric))

    @app.get("/panels/overnight-digest")
    def panel_overnight_digest():
        # Feature 8 (T-2026-CU-9050-160): local window toggle (8h/24h/7 Tage),
        # baked into this panel's own hx-get URL — same self-updating pattern
        # as the regime-heatmap metric toggle and success-rate-timeseries
        # window switcher.
        window_hours = resolve_digest_window(request.args.get("window"))
        return render_template("panels/overnight_digest.html", **_digest_context(path, window_hours=window_hours))

    @app.get("/panels/coin-drilldown")
    def panel_coin_drilldown():
        # Feature 7 (T-2026-CU-9050-159): raw_coin stays None when the query
        # param is absent entirely (vs. an explicit empty string) — that
        # distinction is what lets _resolve_coin default to the first coin on
        # a param-less first load while still showing a clean "unknown coin"
        # message for a genuinely bad ?coin= value.
        raw_coin = request.args.get("coin")
        return render_template("panels/coin_drilldown.html", **_coin_drilldown_context(path, raw_coin=raw_coin))

    return app


def serve(
    app: Any,
    *,
    host: str,
    port: int,
    dev: bool = False,
    serve_fn: Callable[..., None] | None = None,
) -> None:
    """Serve ``app`` via the shared analytics serving path (waitress in prod, the
    Flask dev server under ``--dev``). Thin delegate to ``analytics_api._serve``
    so there is one serving contract, not two."""
    _serve(app, host=host, port=port, dev=dev, serve_fn=serve_fn)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the Z1 dashboard shell (Flask + HTMX, read-only DuckDB)")
    parser.add_argument("--duckdb", default="staging_models/analytics/analytics.duckdb")
    # Never bind public: access is via the reverse proxy / Cloudflare Access (P0.8).
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8098)
    parser.add_argument("--dev", action="store_true", help="Flask dev server instead of Waitress (local smoke only)")
    return parser


def main(argv: Sequence[str] | None = None) -> None:  # pragma: no cover
    args = _build_parser().parse_args(argv)
    serve(create_app(args.duckdb), host=args.host, port=args.port, dev=args.dev)


if __name__ == "__main__":  # pragma: no cover
    main()
