"""Kythera indicator regression guard.

Phase 1 of the Staged-C refactor (KB task T-2026-CU-9050-009). A pre-commit
enforced regression guard over the *deterministic* seam of the live signal
path: the indicator engine (``2_indicator_engine.calculate_indicators_optimized``).

See ``README.md`` for the full rationale and usage.
"""
