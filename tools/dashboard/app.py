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
import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from tools.analytics_api import (
    _serve,
    build_analytics_blueprint,
    connect_ro,
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
) -> dict[str, Any]:
    """Collapse the per-source freshness rows into one badge summary.

    Args:
        rows: the output of ``analytics_export.data_freshness`` (one dict per
            source, with ``synced_at`` = UTC wall clock and ``last_row_ts`` =
            naive-local wall clock, both ISO strings or None).
        now_utc: naive datetime standing for the current UTC wall clock; defaults
            to now. Injected in tests for a deterministic age.

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

    synced = [ts for ts in (_parse_ts(r.get("synced_at")) for r in rows) if ts is not None]
    last_rows = [ts for ts in (_parse_ts(r.get("last_row_ts")) for r in rows) if ts is not None]

    latest_sync = max(synced) if synced else None
    latest_row = max(last_rows) if last_rows else None

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


def _demo_panel_context(duckdb_path: str) -> dict[str, Any]:
    """Server-side render context for the success-rate demo panel.

    Opens a throttled read-only DuckDB connection, computes the rolling
    success-rate windows, and reshapes them into a per-bot table the template
    can iterate — plus a compact chart series proving the chart-lifecycle wiring.
    """
    con = connect_ro(duckdb_path)
    try:
        payload = success_rate_timeseries(con, windows=DEMO_WINDOWS)
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
        if DEMO_PRIMARY_WINDOW in row["windows"]
        and row["windows"][DEMO_PRIMARY_WINDOW]["winrate_pct"] is not None
    ]

    return {
        "as_of": payload["as_of"],
        "windows": list(DEMO_WINDOWS),
        "primary_window": DEMO_PRIMARY_WINDOW,
        "bots": bots,
        "chart_series": chart_series,
        "poll_seconds": PANEL_POLL_SECONDS,
    }


def _freshness_context(duckdb_path: str) -> dict[str, Any]:
    con = connect_ro(duckdb_path)
    try:
        rows = data_freshness(con)
    finally:
        con.close()
    return {"freshness": freshness_summary(rows), "badge_poll_seconds": BADGE_POLL_SECONDS}


def create_app(duckdb_path: str | Path, *, cache_enabled: bool = True):
    """Flask app for the Z1 dashboard shell.

    Mounts the read-only analytics JSON blueprint (``/api/analytics/*``) and
    serves the HTML shell + HTMX panels. ``duckdb_path`` is the single-writer
    analytics DuckDB file produced by ``tools/analytics_export.py``.
    """
    from flask import Flask, render_template

    app = Flask(__name__)  # templates/ and static/ resolve under this package dir
    path = str(duckdb_path)

    # Mount the existing read-only analytics endpoints (T-131 substrate).
    app.register_blueprint(build_analytics_blueprint(path, cache_enabled=cache_enabled))

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            panel_poll_seconds=PANEL_POLL_SECONDS,
            **_freshness_context(path),
        )

    @app.get("/panels/success-rate")
    def panel_success_rate():
        return render_template("panels/success_rate.html", **_demo_panel_context(path))

    @app.get("/panels/freshness")
    def panel_freshness():
        return render_template("_freshness_badge.html", **_freshness_context(path))

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
    parser.add_argument("--dev", action="store_true",
                        help="Flask dev server instead of Waitress (local smoke only)")
    return parser


def main(argv: Sequence[str] | None = None) -> None:  # pragma: no cover
    args = _build_parser().parse_args(argv)
    serve(create_app(args.duckdb), host=args.host, port=args.port, dev=args.dev)


if __name__ == "__main__":  # pragma: no cover
    main()
