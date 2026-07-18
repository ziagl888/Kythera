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

  // Same idea as readSeries() but for a second, arbitrarily-shaped JSON blob
  // (a <script> referenced via data-meta rather than data-series) — the
  // regime-heatmap factory needs both the sparse cell series AND axis labels.
  // Returns {} on any problem so callers can destructure with defaults.
  function readMeta(el) {
    var id = el.getAttribute("data-meta");
    if (!id) return {};
    var node = document.getElementById(id);
    if (!node) return {};
    try {
      return JSON.parse(node.textContent) || {};
    } catch (err) {
      console.error("[panels] bad meta JSON for '" + id + "'", err);
      return {};
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

  // Bot x Regime performance heatmap (Feature 6, T-2026-CU-9050-158). Series
  // shape: sparse [[regimeIndex, botIndex, value], ...] — a (bot, regime) pair
  // with zero decisive trades contributes NO entry at all, so ECharts renders
  // it as a genuinely empty cell rather than a fabricated 0 (visualMap's
  // "min"/"max" only ever spans the values that DO exist). Meta shape:
  // {bots: [...], regimes: [...], is_winrate: bool, metric_label: str} — see
  // app.py's _regime_heatmap_context().
  ChartLifecycle.registerFactory("bot-regime-heatmap", function (el) {
    var series = readSeries(el);
    var meta = readMeta(el);
    var bots = meta.bots || [];
    var regimes = meta.regimes || [];
    if (!window.echarts) {
      el.innerHTML =
        '<p class="muted">Diagramm nicht verfügbar (ECharts-Vendor-Datei fehlt).</p>';
      return null;
    }
    var chart = window.echarts.init(el);
    var values = series.map(function (d) { return d[2]; });
    // Winrate is a bounded 0-100% scale (sequential colour ramp); Ø-PnL/Trade
    // is unbounded and can be negative (diverging colour ramp centred on 0) —
    // the SPEC's "sinnvolle Farb-Skala (winrate 0-100% bzw. PnL divergierend)"
    // requirement, decided purely from meta.is_winrate (never guessed from the
    // data range, which could be all-positive or all-negative by chance).
    var visualMap = meta.is_winrate
      ? {
          min: 0, max: 100, calculable: true, orient: "horizontal", left: "center", bottom: 0,
          inRange: { color: ["#d29922", "#8a94a3", "#3fb950"] },
        }
      : {
          min: values.length ? Math.min(0, Math.min.apply(null, values)) : -1,
          max: values.length ? Math.max(0, Math.max.apply(null, values)) : 1,
          calculable: true, orient: "horizontal", left: "center", bottom: 0,
          inRange: { color: ["#d29922", "#8a94a3", "#3fb950"] },
        };
    chart.setOption({
      grid: { left: 96, right: 24, top: 16, bottom: 64 },
      tooltip: {
        formatter: function (p) {
          var bot = bots[p.value[1]];
          var regime = regimes[p.value[0]];
          var label = meta.is_winrate ? p.value[2] + "%" : p.value[2] + "% Ø/Trade";
          return bot + " × " + regime + ": " + label;
        },
      },
      xAxis: { type: "category", data: regimes, splitArea: { show: true } },
      yAxis: { type: "category", data: bots, splitArea: { show: true } },
      visualMap: visualMap,
      series: [{
        type: "heatmap",
        data: series,
        label: { show: true, formatter: function (p) { return p.value[2]; } },
      }],
    });
    var onResizeHm = function () { chart.resize(); };
    window.addEventListener("resize", onResizeHm);
    return function () {
      window.removeEventListener("resize", onResizeHm);
      chart.dispose();
    };
  });

  // Coin drill-down panel (Feature 7, T-2026-CU-9050-159): a decisive-trade
  // PRICE PATH (entry->exit points per trade, connected in close-time order)
  // via TradingView Lightweight Charts (vendored 4.2.3, static/vendor/), with
  // win/loss-coloured markers per trade. NOT full OHLCV candles — out of
  // scope (T-131 candle export pending), see tools/dashboard/SPEC.md Feature 7.
  // Series shape: [{time, value}, ...] (paired entry/exit points, already
  // ordered/deduped for strictly-increasing time by app.py's
  // _coin_chart_series()). Meta shape: {markers: [{time, position, color,
  // shape, text}, ...]} — see app.py's _coin_drilldown_context().
  ChartLifecycle.registerFactory("coin-price-line", function (el) {
    var points = readSeries(el);
    var meta = readMeta(el);
    if (!window.LightweightCharts) {
      el.innerHTML =
        '<p class="muted">Diagramm nicht verfügbar (Lightweight-Charts-Vendor-Datei fehlt).</p>';
      return null;
    }
    var chart = window.LightweightCharts.createChart(el, {
      width: el.clientWidth,
      height: el.clientHeight || 280,
      layout: { background: { color: "transparent" }, textColor: "#8a94a3" },
      grid: {
        vertLines: { color: "rgba(138,148,163,0.1)" },
        horzLines: { color: "rgba(138,148,163,0.1)" },
      },
      timeScale: { timeVisible: true, secondsVisible: false },
    });
    var series = chart.addLineSeries({ color: "#4c9aff", lineWidth: 2 });
    series.setData(points);
    if (meta.markers && meta.markers.length) {
      series.setMarkers(meta.markers);
    }
    var onResizeCd = function () { chart.applyOptions({ width: el.clientWidth }); };
    window.addEventListener("resize", onResizeCd);
    // Lightweight Charts' own disposer is chart.remove() — NOT ECharts'
    // .dispose() (chart_lifecycle.js's resolveDisposer() supports both via
    // duck-typing, but this factory returns an explicit teardown function,
    // same pattern as the two ECharts factories above, so the resize
    // listener is torn down alongside the chart itself).
    return function () {
      window.removeEventListener("resize", onResizeCd);
      chart.remove();
    };
  });
})(window, document);
