/*
 * chart_lifecycle.js — shared chart lifecycle manager for the Z1 dashboard.
 * Task 0 core deliverable (T-2026-CU-9050-151), mandated by z-council
 * (D-2026-CLD-111).
 *
 * WHY: the dashboard renders panels as HTMX fragments that are swapped in and
 * out on every poll. A chart (ECharts canvas/WebGL context, TradingView
 * Lightweight Charts instance, their resize listeners) that is not torn down
 * before its container leaves the DOM leaks the GPU context and the listeners —
 * across 9 panels polling every 30–60 s that compounds into a dead tab. This
 * helper is the single place that:
 *   1. lets panel code REGISTER a chart factory per `data-chart` name,
 *   2. MOUNTS matching charts when fresh content arrives (htmx:afterSwap + first
 *      paint),
 *   3. DISPOSES them (calling ECharts `.dispose()` / Lightweight `.remove()` /
 *      any custom disposer) before the old content is swapped out
 *      (htmx:beforeSwap) or cleaned up (htmx:beforeCleanupElement).
 *
 * The helper is deliberately library-agnostic: it never imports ECharts or
 * Lightweight Charts. A factory creates the chart and hands back how to dispose
 * it (a disposer function, or the instance itself when it exposes a standard
 * `.dispose()`/`.remove()`), and the helper owns only the lifecycle.
 *
 * Public API (window.ChartLifecycle):
 *   registerFactory(name, factory)  factory(el) -> disposer | instance | void
 *   track(el, disposerOrInstance)   attach an extra teardown to `el`
 *   init(root)                      mount charts in root's subtree (idempotent)
 *   dispose(root)                   tear down charts in root's subtree
 */
(function (window, document) {
  "use strict";

  var factories = {};        // name -> factory(el)
  var disposers = new Map(); // el -> [disposerFn, ...]  (presence == initialised)
  var CHART_SELECTOR = "[data-chart]";

  // Turn a factory's return value into a disposer function, or null.
  // Supports: a disposer function, an ECharts-like instance (.dispose), a
  // Lightweight-Charts-like instance (.remove), or nothing.
  function resolveDisposer(result) {
    if (typeof result === "function") return result;
    if (result && typeof result.dispose === "function") {
      return function () { result.dispose(); };
    }
    if (result && typeof result.remove === "function") {
      return function () { result.remove(); };
    }
    return null;
  }

  // All [data-chart] elements in root's subtree, including root itself.
  function chartEls(root) {
    var els = [];
    if (!root) return els;
    if (root.matches && root.matches(CHART_SELECTOR)) els.push(root);
    if (root.querySelectorAll) {
      root.querySelectorAll(CHART_SELECTOR).forEach(function (el) { els.push(el); });
    }
    return els;
  }

  function initEl(el) {
    if (disposers.has(el)) return; // idempotent: already mounted
    var name = el.getAttribute("data-chart");
    var factory = factories[name];
    if (!factory) return;
    // Register presence first so a factory error still marks the element mounted
    // (prevents a broken chart from re-initialising every poll).
    var arr = [];
    disposers.set(el, arr);
    try {
      var disposer = resolveDisposer(factory(el));
      if (disposer) arr.push(disposer);
    } catch (err) {
      console.error("[chart_lifecycle] init failed for '" + name + "'", err);
    }
  }

  function disposeEl(el) {
    var arr = disposers.get(el);
    if (!arr) return;
    disposers.delete(el);
    arr.forEach(function (fn) {
      try { fn(); } catch (err) { console.error("[chart_lifecycle] dispose failed", err); }
    });
  }

  var ChartLifecycle = {
    registerFactory: function (name, factory) { factories[name] = factory; },
    track: function (el, disposerOrInstance) {
      var disposer = resolveDisposer(disposerOrInstance);
      if (!disposer) return;
      var arr = disposers.get(el);
      if (!arr) { arr = []; disposers.set(el, arr); }
      arr.push(disposer);
    },
    init: function (root) { chartEls(root || document).forEach(initEl); },
    dispose: function (root) { chartEls(root || document).forEach(disposeEl); },
  };

  function swapTarget(evt) {
    return (evt.detail && evt.detail.target) || evt.target;
  }

  // Dispose charts leaving the DOM BEFORE the swap replaces them...
  document.addEventListener("htmx:beforeSwap", function (evt) {
    ChartLifecycle.dispose(swapTarget(evt));
  });
  // ...and mount the charts that arrive AFTER it.
  document.addEventListener("htmx:afterSwap", function (evt) {
    ChartLifecycle.init(swapTarget(evt));
  });
  // Elements htmx removes outside a swap (out-of-band, hx-swap="delete", …).
  document.addEventListener("htmx:beforeCleanupElement", function (evt) {
    if (evt.target) ChartLifecycle.dispose(evt.target);
  });
  // First paint of server-rendered charts.
  document.addEventListener("DOMContentLoaded", function () {
    ChartLifecycle.init(document);
  });

  window.ChartLifecycle = ChartLifecycle;
})(window, document);
