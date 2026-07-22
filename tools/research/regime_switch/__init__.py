"""regime_switch — DB-free study: does a probabilistic / soft regime weighting
reduce whipsaw and the TREND-hold defect vs. the live rule (debounce + §22
hysteresis), *without* degrading regime separation?

Self-contained (Stoic/GARCH pattern, T-2026-KYT-9050-029). Pulls BTC + BTCDOM
15m klines off ccxt (no DB, no credentials) and reuses the REAL classifiers from
``core.regime_logic`` (Hard Rule 7 — share, don't rebuild). Only ``compute_features``
(DB read) and the debounce state (DB persist) are ported to pure / in-memory form.

The pure functions import without ccxt; ``ccxt`` is imported lazily in ``ccxt_data``.
"""
