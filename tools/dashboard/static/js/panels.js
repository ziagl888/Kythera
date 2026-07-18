/*
 * panels.js — Z1 dashboard panel chart factories (T-2026-CU-9050-151).
 *
 * App-specific glue: registers each panel's chart factory with the shared,
 * library-agnostic ChartLifecycle helper. The helper owns mount/dispose across
 * HTMX swaps; the factories here own "how to build this particular chart with
 * this particular library". Keeping the two apart is what lets the lifecycle
 * helper stay reusable across all 9 future panels.
 */
(function (window, document) {
  "use strict";

  if (!window.ChartLifecycle) {
    console.error("[panels] ChartLifecycle helper missing — load chart_lifecycle.js first");
    return;
  }

  // Read a panel's embedded JSON series (a <script type="application/json"> the
  // server rendered next to the chart mount point). Returns [] on any problem.
  function readSeries(el) {
    var id = el.getAttribute("data-series");
    if (!id) return [];
    var node = document.getElementById(id);
    if (!node) return [];
    try {
      return JSON.parse(node.textContent) || [];
    } catch (err) {
      console.error("[panels] bad series JSON for '" + id + "'", err);
      return [];
    }
  }

  // Success-rate demo panel: a per-bot winrate bar chart via Apache ECharts.
  ChartLifecycle.registerFactory("winrate-bars", function (el) {
    var series = readSeries(el);
    if (!window.echarts) {
      // Vendored ECharts not present yet (placeholder build) — degrade to a
      // readable note rather than a blank box. The table beneath still carries
      // the numbers, so the panel stays useful.
      el.innerHTML =
        '<p class="muted">Diagramm nicht verfügbar (ECharts-Vendor-Datei fehlt).</p>';
      return null;
    }
    var chart = window.echarts.init(el);
    chart.setOption({
      grid: { left: 48, right: 16, top: 16, bottom: 48 },
      tooltip: { trigger: "axis" },
      xAxis: {
        type: "category",
        data: series.map(function (d) { return d.bot; }),
        axisLabel: { rotate: series.length > 6 ? 45 : 0 },
      },
      yAxis: { type: "value", name: "Winrate %", max: 100 },
      series: [{
        type: "bar",
        data: series.map(function (d) { return d.winrate_pct; }),
      }],
    });
    // ECharts charts do not auto-resize; keep it in sync while mounted and let
    // the lifecycle helper remove the listener on dispose (via the returned
    // teardown) so nothing leaks across swaps.
    var onResize = function () { chart.resize(); };
    window.addEventListener("resize", onResize);
    return function () {
      window.removeEventListener("resize", onResize);
      chart.dispose();
    };
  });

  // Success-rate time-comparison panel (Feature 3, T-2026-CU-9050-155): one
  // rolling-winrate line per selected bot. Series shape: [{bot, points:
  // [{date, winrate_pct}, ...]}, ...] — see rolling_success_rate_series() /
  // _success_rate_timeseries_context() for the data source.
  ChartLifecycle.registerFactory("winrate-timeseries", function (el) {
    var series = readSeries(el);
    if (!window.echarts) {
      el.innerHTML =
        '<p class="muted">Diagramm nicht verfügbar (ECharts-Vendor-Datei fehlt).</p>';
      return null;
    }
    var chart = window.echarts.init(el);
    chart.setOption({
      grid: { left: 56, right: 24, top: 32, bottom: 48 },
      tooltip: { trigger: "axis" },
      legend: { top: 0, data: series.map(function (s) { return s.bot; }) },
      xAxis: { type: "time" },
      yAxis: { type: "value", name: "Winrate %", max: 100, min: 0 },
      series: series.map(function (s) {
        return {
          name: s.bot,
          type: "line",
          showSymbol: false,
          connectNulls: true,
          data: s.points.map(function (p) { return [p.date, p.winrate_pct]; }),
        };
      }),
    });
    var onResizeTs = function () { chart.resize(); };
    window.addEventListener("resize", onResizeTs);
    return function () {
      window.removeEventListener("resize", onResizeTs);
      chart.dispose();
    };
  });
})(window, document);
