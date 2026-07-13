"""Core helpers for the Kythera indicator regression guard.

Dependency-free, exact-float serialization; fixture extraction/synthesis;
golden computation against the REAL indicator engine; tolerance-aware
comparison; and manifest bookkeeping.

Deliberately imports only numpy + pandas at module load (both are hard deps of
the engine, so no new dependency is introduced). Parquet/pyarrow is avoided on
purpose — it is not installed in the live bot environment and we do not pip
into that environment. ``np.savez`` round-trips float64 bit-exactly; CSV does
not (verified), which would silently exceed the tolerance band.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os

import numpy as np
import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# Serialization — exact, dependency-free (np.savez columnar)
# ─────────────────────────────────────────────────────────────────────────────


def save_frame(path: str, df: pd.DataFrame) -> None:
    """Persist a DataFrame to ``path`` (.npz) preserving column order + dtypes.

    Columns are stored positionally (``c0``, ``c1`` …) with a ``__columns__``
    side array holding the names, so arbitrary column labels are safe. Float
    columns round-trip bit-exactly; object/datetime columns via numpy's pickled
    object arrays.
    """
    cols = list(df.columns)
    payload = {f"c{i}": df[cols[i]].to_numpy() for i in range(len(cols))}
    np.savez(path, __columns__=np.array(cols, dtype=object), **payload)


def load_frame(path: str) -> pd.DataFrame:
    """Inverse of :func:`save_frame`. Rebuilds the DataFrame with column order."""
    with np.load(path, allow_pickle=True) as z:
        cols = [str(c) for c in z["__columns__"]]
        data = {cols[i]: z[f"c{i}"] for i in range(len(cols))}
    # dict insertion order preserves the original column order (py3.7+).
    return pd.DataFrame(data)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — synthetic (offline smoke) and real (live-DB extraction)
# ─────────────────────────────────────────────────────────────────────────────

# Longest indicator window is the 200-period family; 100-bar trendline lookback.
# ~600 bars keeps every window fully warm with a healthy valid tail while the
# guard stays fast.
DEFAULT_BARS = 600

_TF_FREQ = {
    "30m": "30min",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "1d": "1D",
    "1w": "1W",
}


def fixture_name(symbol: str, timeframe: str) -> str:
    return f"{symbol}__{timeframe}.npz"


def parse_fixture_name(fname: str) -> tuple[str, str]:
    stem = fname[:-4] if fname.endswith(".npz") else fname
    symbol, timeframe = stem.rsplit("__", 1)
    return symbol, timeframe


def synthetic_ohlcv(symbol: str, timeframe: str, n: int, seed: int) -> pd.DataFrame:
    """Deterministic OHLCV for offline smoke tests (fixed seed → identical bytes).

    A positive geometric random walk with wick jitter — enough structure to
    exercise every indicator family (trend, channel, HVN/POC, S/R, Fib) without
    touching the database.
    """
    rng = np.random.default_rng(seed)
    steps = rng.standard_normal(n) * 0.01
    close = 100.0 * np.exp(np.cumsum(steps))
    up = np.abs(rng.standard_normal(n)) * 0.005
    dn = np.abs(rng.standard_normal(n)) * 0.005
    high = close * (1.0 + up)
    low = close * (1.0 - dn)
    open_ = np.concatenate([[close[0]], close[:-1]])
    volume = rng.uniform(10.0, 1000.0, size=n)
    freq = _TF_FREQ.get(timeframe, "1h")
    open_time = pd.date_range("2020-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "open_time": open_time,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "symbol": symbol,
        }
    )


def extract_ohlcv_from_db(conn, symbol: str, timeframe: str, n_bars: int) -> pd.DataFrame:
    """Pull the newest ``n_bars`` CLOSED rows of ``"{symbol}_{timeframe}"`` from the DB.

    Faithful ``SELECT *`` capture (mirrors what the engine reads), ascending.
    Injects a ``symbol`` column only if the raw table lacks one, since the
    engine's compute reads ``df['symbol']``.

    Now via core.candles with ``include_forming=False`` (R1, T-2026-CU-9050-107):
    a freshly extracted fixture no longer carries the still-forming candle. This
    only affects the DB ``extract`` path — ``verify``/``smoke`` run DB-free on the
    stored ``.npz`` and are untouched; existing goldens stay valid. A later
    refresh legitimately re-freezes the forming-excluded capture (documented R1
    change, NOT a red→green refresh, harte Regel 9).

    ``core.candles`` is imported lazily so module load stays numpy+pandas only
    (the DB-free guard paths must not pull psycopg2)."""
    from core.candles import read_candles  # lazy: only the DB path needs it

    df = read_candles(conn, symbol, timeframe, limit=n_bars, include_forming=False, columns=None)
    df = df.sort_values("open_time").reset_index(drop=True)
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Golden computation — runs the REAL engine, never a reimplementation
# ─────────────────────────────────────────────────────────────────────────────


def compute_indicators(engine, ohlcv: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Run the live engine's pure compute over one OHLCV frame → indicator frame."""
    return engine.calculate_indicators_optimized(ohlcv.copy(), timeframe)


# ─────────────────────────────────────────────────────────────────────────────
# Comparison — tolerance-aware, per-column
# ─────────────────────────────────────────────────────────────────────────────


def _is_numeric(arr: np.ndarray) -> bool:
    return np.issubdtype(arr.dtype, np.number)


def compare_frames(name, golden: pd.DataFrame, fresh: pd.DataFrame, tol: dict) -> list[dict]:
    """Compare a golden frame against a freshly-computed one.

    Returns a list of breach dicts (empty == clean). Numeric columns use
    per-column ``rtol``/``atol`` (default from ``tol['default']``); columns
    marked ``"exact"`` and all non-numeric columns must match bit-for-bit
    (NaN==NaN treated as equal).
    """
    breaches: list[dict] = []

    if list(golden.columns) != list(fresh.columns):
        only_g = [c for c in golden.columns if c not in set(fresh.columns)]
        only_f = [c for c in fresh.columns if c not in set(golden.columns)]
        breaches.append(
            {
                "fixture": name,
                "kind": "columns_mismatch",
                "detail": f"golden_only={only_g} fresh_only={only_f}",
            }
        )
        return breaches

    if len(golden) != len(fresh):
        breaches.append(
            {
                "fixture": name,
                "kind": "row_count_mismatch",
                "detail": f"golden={len(golden)} fresh={len(fresh)}",
            }
        )
        return breaches

    default = tol.get("default", {"rtol": 0.0, "atol": 1e-9})
    colspec = tol.get("columns", {})

    for col in golden.columns:
        g = golden[col].to_numpy()
        f = fresh[col].to_numpy()
        spec = colspec.get(col)

        if _is_numeric(g) and _is_numeric(f) and spec != "exact":
            band = spec if isinstance(spec, dict) else default
            rtol = float(band.get("rtol", default.get("rtol", 0.0)))
            atol = float(band.get("atol", default.get("atol", 1e-9)))
            gf = g.astype(np.float64)
            ff = f.astype(np.float64)
            close = np.isclose(gf, ff, rtol=rtol, atol=atol, equal_nan=True)
            bad = np.where(~close)[0]
            if bad.size:
                absdiff = np.abs(gf[bad] - ff[bad])
                worst = int(bad[int(np.argmax(absdiff))])
                breaches.append(
                    {
                        "fixture": name,
                        "kind": "numeric_drift",
                        "column": col,
                        "n_rows": int(bad.size),
                        "max_abs_diff": float(np.nanmax(absdiff)),
                        "rtol": rtol,
                        "atol": atol,
                        "worst_row": worst,
                        "golden": float(gf[worst]),
                        "fresh": float(ff[worst]),
                    }
                )
        else:
            # Exact / categorical / datetime — NaN==NaN counts as equal.
            eq = (g == f) | (_nan_mask(g) & _nan_mask(f))
            bad = np.where(~eq)[0]
            if bad.size:
                worst = int(bad[0])
                breaches.append(
                    {
                        "fixture": name,
                        "kind": "exact_mismatch",
                        "column": col,
                        "n_rows": int(bad.size),
                        "worst_row": worst,
                        "golden": _scalar(g[worst]),
                        "fresh": _scalar(f[worst]),
                    }
                )
    return breaches


def _nan_mask(arr: np.ndarray) -> np.ndarray:
    try:
        return pd.isna(arr)
    except (TypeError, ValueError):
        return np.zeros(len(arr), dtype=bool)


def _scalar(v):
    try:
        if isinstance(v, np.generic):
            return v.item()
    except Exception:
        pass
    return str(v)


# ─────────────────────────────────────────────────────────────────────────────
# Manifest
# ─────────────────────────────────────────────────────────────────────────────

MANIFEST_SCHEMA = 1


def build_manifest(
    *,
    engine_sha: str,
    tolerances_sha: str,
    fixtures: dict,
    golden: dict,
    git_commit: str,
    note: str,
) -> dict:
    return {
        "schema_version": MANIFEST_SCHEMA,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "git_commit": git_commit,
        "engine_file": "2_indicator_engine.py",
        "engine_sha256": engine_sha,
        "tolerances_sha256": tolerances_sha,
        "fixtures": fixtures,
        "golden": golden,
        "note": note,
    }


def write_json(path: str, obj: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def list_npz(directory: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    return sorted(f for f in os.listdir(directory) if f.endswith(".npz"))
