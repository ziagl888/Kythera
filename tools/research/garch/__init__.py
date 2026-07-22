"""GARCH vol-targeting research package (T-2026-KYT-9050-021 / -022).

Dual layout: the modules are flat scripts (each runnable as a CLI, e.g.
``python tools/research/garch/compare.py --coin BTC/USDT``) AND re-exported here
so the fleet can ``from tools.research.garch import walkforward_garch, GarchSizer``.
The package dir is put on ``sys.path`` below so the flat intra-package imports
(``from garch_forecast import ...``) resolve under both entry points.

``arch`` and ``ccxt`` are imported lazily inside the functions that need them —
importing this package does not require either. See SPEC.md / README.md /
LICENSE.upstream.
"""

import os
import sys

# The flat modules import each other by bare name (from garch_forecast import
# ...), so the package dir must be importable. Accepted trade-off: this exposes
# generic top-level names (compare, vol_target, ...) — no collision exists in
# the repo today; keep the insert idempotent so re-import does not stack it.
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from ccxt_data import (  # noqa: E402
    fetch_ohlcv_df,
    load_prices_csv,
    normalize_prices,
)
from compare import (  # noqa: E402
    compare_coins,
    ema_crossover_signals,
    load_signals,
    perf_stats,
    run_comparison,
    verdict_from_stats,
    worst_month,
)
from garch_forecast import (  # noqa: E402
    HONESTY_NOTE,
    MAX_WINDOW,
    MIN_TRAIN,
    REFIT_EVERY,
    TRADING_DAYS_CRYPTO,
    TRADING_DAYS_EQUITY,
    GarchParams,
    GarchSizer,
    walkforward_garch,
)
from vol_target import (  # noqa: E402
    MAX_LEVERAGE,
    MIN_SIZE,
    apply_sizing,
    size_from_vol,
    size_series,
)

__all__ = [
    "walkforward_garch",
    "GarchSizer",
    "GarchParams",
    "size_from_vol",
    "size_series",
    "apply_sizing",
    "MAX_LEVERAGE",
    "MIN_SIZE",
    "MIN_TRAIN",
    "REFIT_EVERY",
    "MAX_WINDOW",
    "TRADING_DAYS_CRYPTO",
    "TRADING_DAYS_EQUITY",
    "HONESTY_NOTE",
    "fetch_ohlcv_df",
    "load_prices_csv",
    "normalize_prices",
    "run_comparison",
    "compare_coins",
    "verdict_from_stats",
    "perf_stats",
    "worst_month",
    "ema_crossover_signals",
    "load_signals",
]
