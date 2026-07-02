#!/usr/bin/env python
"""Kythera indicator regression guard - CLI entrypoint.

Protects the *deterministic* seam of the live signal path
(``2_indicator_engine.calculate_indicators_optimized``) against silent drift
while the Staged-C refactor mutates code in place.

Modes
-----
  verify   (default)  Recompute indicators from the frozen OHLCV fixtures and
                      compare to the immutable golden snapshot. Any breach exits
                      non-zero → blocks the commit. If no golden snapshot exists
                      yet, the guard is "not armed" and passes (exit 0) - this is
                      the state until the real live-DB freeze happens.
  refresh             Recompute and OVERWRITE the golden snapshot + manifest.
                      Gated behind KYTHERA_GOLDEN_REFRESH=1 (or --force) so a
                      refresh is a deliberate, justified act visible in the diff.
  status              Print armed/not-armed + provenance.
  extract             Pull fresh OHLCV fixtures from the LIVE DB (needs real
                      creds; no offline shim). One-time, decays → run when ready.
  smoke               Self-contained end-to-end check on synthetic fixtures in a
                      temp dir (proves the machinery without touching the DB).

Determinism: BLAS threads are pinned to 1 in-process (set before numpy is
imported) so float reductions (dot products, std, linregress) are
reduction-order-stable across machines. The compute path itself contains no RNG,
so no PYTHONHASHSEED pin or process re-exec is needed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
FIXTURES_DIR = os.path.join(HERE, "fixtures")
GOLDEN_DIR = os.path.join(HERE, "golden")
MANIFEST_PATH = os.path.join(GOLDEN_DIR, "manifest.json")
TOLERANCES_PATH = os.path.join(HERE, "tolerances.json")
ENGINE_PATH = os.path.join(REPO_ROOT, "2_indicator_engine.py")

# Modes that only need the PURE compute path - safe to satisfy the engine's
# import-time credential check with harmless placeholders. `extract` is NOT here:
# it needs real DB creds and must never be shimmed.
_COMPUTE_MODES = {"verify", "refresh", "status", "smoke"}

# BLAS thread pins. Set in os.environ BEFORE numpy is first imported (the engine
# and rgcore both pull numpy) so float reduction order is stable. No re-exec /
# PYTHONHASHSEED needed: the compute path has no RNG and column order is
# insertion-based, not hash-based.
_THREAD_ENV = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}

_PLACEHOLDER_CREDS = {
    "DB_PASSWORD": "guard-offline-noop",
    "TELEGRAM_BOT_TOKEN": "guard-offline-noop",
    "DB_HOST": "127.0.0.1",
    "DB_NAME": "guard-offline",
    "DB_USER": "guard-offline",
    "DB_PORT": "5432",
}


def _pin_blas_threads() -> None:
    """Force single-threaded BLAS so float reductions are order-stable. Must run
    before the first numpy import to take effect."""
    for key, val in _THREAD_ENV.items():
        os.environ.setdefault(key, val)


def _apply_offline_cred_shim() -> None:
    """Set placeholder creds only where absent, so importing the engine (which
    hard-requires DB_PASSWORD + TELEGRAM_BOT_TOKEN at import) succeeds offline.
    The compute path never opens a connection nor sends a message."""
    for key, val in _PLACEHOLDER_CREDS.items():
        if not os.environ.get(key):
            os.environ[key] = val


def _load_engine():
    """Import the digit-prefixed engine module by path, with REPO_ROOT on the
    path so its ``core.*`` absolute imports resolve."""
    import importlib.util

    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    spec = importlib.util.spec_from_file_location("kythera_indicator_engine", ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load engine module from {ENGINE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _load_tolerances(core):
    if os.path.exists(TOLERANCES_PATH):
        return core.read_json(TOLERANCES_PATH)
    return {"default": {"rtol": 0.0, "atol": 1e-9}, "columns": {}}


# ─────────────────────────────────────────────────────────────────────────────
# Modes
# ─────────────────────────────────────────────────────────────────────────────


def mode_verify(core) -> int:
    golden_files = core.list_npz(GOLDEN_DIR)
    fixture_files = core.list_npz(FIXTURES_DIR)

    if not golden_files:
        print(
            "[guard] NOT ARMED - no golden snapshot yet "
            "(run `extract` then `refresh` once the live DB is reachable). Pass."
        )
        return 0

    if not fixture_files:
        print("[guard] ERROR - golden snapshot present but no fixtures. Inconsistent state.")
        return 1

    engine = _load_engine()
    tol = _load_tolerances(core)
    all_breaches = []
    checked = 0

    for gname in golden_files:
        _symbol, timeframe = core.parse_fixture_name(gname)
        fix_path = os.path.join(FIXTURES_DIR, gname)
        if not os.path.exists(fix_path):
            all_breaches.append(
                {"fixture": gname, "kind": "missing_fixture", "detail": "golden has no matching fixture"}
            )
            continue
        ohlcv = core.load_frame(fix_path)
        golden = core.load_frame(os.path.join(GOLDEN_DIR, gname))
        fresh = core.compute_indicators(engine, ohlcv, timeframe)
        all_breaches.extend(core.compare_frames(gname, golden, fresh, tol))
        checked += 1

    if all_breaches:
        print(f"[guard] REGRESSION - {len(all_breaches)} breach(es) across {checked} fixture(s):")
        for b in all_breaches[:40]:
            print("  - " + _fmt_breach(b))
        if len(all_breaches) > 40:
            print(f"  ... and {len(all_breaches) - 40} more")
        print("\n[guard] The indicator output drifted from the golden snapshot.")
        print("        If this change is INTENTIONAL, refresh the snapshot deliberately:")
        print("          KYTHERA_GOLDEN_REFRESH=1 python tools/regression_guard/guard.py refresh")
        print("        and commit the changed golden/ with a justification in the message.")
        return 1

    print(f"[guard] OK - {checked} fixture(s) match the golden snapshot.")
    return 0


def mode_refresh(core, force: bool) -> int:
    if not force and os.environ.get("KYTHERA_GOLDEN_REFRESH") != "1":
        print("[guard] refresh is gated. The golden snapshot is immutable by default.")
        print("        To refresh deliberately (and commit the diff with a reason):")
        print("          KYTHERA_GOLDEN_REFRESH=1 python tools/regression_guard/guard.py refresh")
        print("        or pass --force.")
        return 2

    fixture_files = core.list_npz(FIXTURES_DIR)
    if not fixture_files:
        print("[guard] no fixtures to compute a golden from. Run `extract` (live DB) or `smoke` first.")
        return 1

    engine = _load_engine()
    os.makedirs(GOLDEN_DIR, exist_ok=True)

    fixtures_meta, golden_meta = {}, {}
    for fname in fixture_files:
        _symbol, timeframe = core.parse_fixture_name(fname)
        fix_path = os.path.join(FIXTURES_DIR, fname)
        ohlcv = core.load_frame(fix_path)
        golden = core.compute_indicators(engine, ohlcv, timeframe)
        gpath = os.path.join(GOLDEN_DIR, fname)
        core.save_frame(gpath, golden)
        fixtures_meta[fname] = {"sha256": core.sha256_file(fix_path), "rows": int(len(ohlcv))}
        golden_meta[fname] = {"sha256": core.sha256_file(gpath), "rows": int(len(golden)), "cols": int(golden.shape[1])}
        print(f"  froze golden {fname}  ({len(golden)} rows x {golden.shape[1]} cols)")

    manifest = core.build_manifest(
        engine_sha=core.sha256_file(ENGINE_PATH),
        tolerances_sha=core.sha256_file(TOLERANCES_PATH) if os.path.exists(TOLERANCES_PATH) else "",
        fixtures=fixtures_meta,
        golden=golden_meta,
        git_commit=_git_commit(),
        note="Golden snapshot of calculate_indicators_optimized over the frozen "
        "OHLCV fixtures. Refresh only with a justified commit message.",
    )
    core.write_json(MANIFEST_PATH, manifest)
    print(
        f"[guard] refreshed {len(fixture_files)} golden snapshot(s) + manifest. "
        "Review the diff and commit with a reason."
    )
    return 0


def mode_status(core) -> int:
    golden_files = core.list_npz(GOLDEN_DIR)
    fixture_files = core.list_npz(FIXTURES_DIR)
    armed = bool(golden_files)
    print(f"[guard] armed: {armed}")
    print(f"[guard] fixtures: {len(fixture_files)}  golden: {len(golden_files)}")
    if os.path.exists(MANIFEST_PATH):
        m = core.read_json(MANIFEST_PATH)
        print(f"[guard] golden generated_at: {m.get('generated_at')}")
        print(f"[guard] golden git_commit:  {m.get('git_commit')}")
        print(f"[guard] engine_sha256:      {m.get('engine_sha256', '')[:16]}...")
        cur = core.sha256_file(ENGINE_PATH)
        if m.get("engine_sha256") and m["engine_sha256"] != cur:
            print(
                f"[guard] NOTE engine changed since freeze (current {cur[:16]}...) - "
                "expected while refactoring; `verify` decides if OUTPUT drifted."
            )
    else:
        print("[guard] no manifest - not armed.")
    return 0


def mode_extract(core, coins, timeframes, n_bars) -> int:
    # NO cred shim: real creds required. The engine import itself reads
    # core.config, which hard-requires DB creds - so a missing-creds host fails
    # here, with a friendly message rather than a raw traceback.
    try:
        engine = _load_engine()
    except Exception as exc:  # noqa: BLE001 - surface the real reason, no traceback
        print(f"[guard] extract needs real DB creds and none are available: {type(exc).__name__}: {exc}")
        print("        Run this on a host where core/.env carries real DB creds.")
        return 1
    try:
        conn = engine.get_db_connection()
    except Exception as exc:  # noqa: BLE001 - surface the real reason
        print(f"[guard] extract needs the live DB and it is not reachable: {type(exc).__name__}: {exc}")
        print("        Run this on a host where core/.env carries real DB creds.")
        return 1

    os.makedirs(FIXTURES_DIR, exist_ok=True)
    written = 0
    try:
        for symbol in coins:
            for timeframe in timeframes:
                try:
                    ohlcv = core.extract_ohlcv_from_db(conn, symbol, timeframe, n_bars)
                except Exception as exc:  # noqa: BLE001
                    print(f"  skip {symbol} {timeframe}: {type(exc).__name__}: {exc}")
                    continue
                if ohlcv.empty:
                    print(f"  skip {symbol} {timeframe}: no rows")
                    continue
                path = os.path.join(FIXTURES_DIR, core.fixture_name(symbol, timeframe))
                core.save_frame(path, ohlcv)
                written += 1
                print(f"  froze fixture {symbol} {timeframe}  ({len(ohlcv)} rows)")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    print(
        f"[guard] extracted {written} fixture(s). Next: "
        "`KYTHERA_GOLDEN_REFRESH=1 python tools/regression_guard/guard.py refresh` "
        "to freeze the golden, then commit fixtures/ + golden/."
    )
    return 0 if written else 1


def mode_smoke(core) -> int:
    """End-to-end machinery check on synthetic fixtures in a temp dir."""
    import shutil
    import tempfile

    engine = _load_engine()
    tol = _load_tolerances(core)
    tmp = tempfile.mkdtemp(prefix="kythera_guard_smoke_")
    try:
        coins = ["SMOKEUSDT", "TESTUSDT"]
        tfs = ["1h", "4h", "1d"]
        # 1) synthesize fixtures, 2) freeze golden, 3) verify clean, 4) perturb → dirty
        goldens = {}
        for idx, (sym, tf) in enumerate((s, t) for s in coins for t in tfs):
            ohlcv = core.synthetic_ohlcv(sym, tf, 400, seed=1000 + idx)
            name = core.fixture_name(sym, tf)
            golden = core.compute_indicators(engine, ohlcv, tf)
            goldens[name] = (ohlcv, golden, tf)

        # exact serialization round-trip
        for name, (_ohlcv, golden, _tf) in goldens.items():
            p = os.path.join(tmp, name)
            core.save_frame(p, golden)
            reloaded = core.load_frame(p)
            breaches = core.compare_frames(name, reloaded, golden, tol)
            assert not breaches, f"serialization not exact for {name}: {breaches[:2]}"

        # clean verify: recompute == golden
        clean = 0
        for name, (ohlcv, golden, tf) in goldens.items():
            fresh = core.compute_indicators(engine, ohlcv, tf)
            breaches = core.compare_frames(name, golden, fresh, tol)
            assert not breaches, f"recompute drifted (non-deterministic?!) for {name}: {breaches[:2]}"
            clean += 1

        # dirty verify: a perturbed input MUST be caught
        name0, (ohlcv0, golden0, tf0) = next(iter(goldens.items()))
        perturbed = ohlcv0.copy()
        perturbed.loc[perturbed.index[-1], "close"] *= 1.001  # 0.1% nudge on last bar
        fresh_bad = core.compute_indicators(engine, perturbed, tf0)
        breaches = core.compare_frames(name0, golden0, fresh_bad, tol)
        assert breaches, "perturbation was NOT caught - guard is blind"

        print(
            f"[guard] SMOKE OK - {clean} fixtures froze+verified clean; "
            f"serialization exact; perturbation caught ({len(breaches)} breach(es))."
        )
        return 0
    except AssertionError as exc:
        print(f"[guard] SMOKE FAIL - {exc}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fmt_breach(b: dict) -> str:
    k = b.get("kind")
    if k == "numeric_drift":
        return (
            f"{b['fixture']} :: {b['column']}  drift on {b['n_rows']} row(s), "
            f"max|d|={b['max_abs_diff']:.3e} (atol={b['atol']:g}, rtol={b['rtol']:g}); "
            f"row {b['worst_row']}: golden={b['golden']:.6g} fresh={b['fresh']:.6g}"
        )
    if k == "exact_mismatch":
        return (
            f"{b['fixture']} :: {b['column']}  {b['n_rows']} exact mismatch(es); "
            f"row {b['worst_row']}: golden={b['golden']!r} fresh={b['fresh']!r}"
        )
    return f"{b.get('fixture')} :: {k} - {b.get('detail', '')}"


def _load_coin_selection():
    """Default coin/timeframe fixture breadth. Timeframes mirror the engine's
    INDICATOR_TIMEFRAMES; coins are a small liquid+illiquid mix. Override via
    --coins / --timeframes on `extract`."""
    return (
        ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"],
        ["30m", "1h", "2h", "4h", "1d", "1w"],
    )


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    parser = argparse.ArgumentParser(description="Kythera indicator regression guard")
    parser.add_argument(
        "mode", nargs="?", default="verify", choices=["verify", "refresh", "status", "extract", "smoke"]
    )
    parser.add_argument("--force", action="store_true", help="refresh: bypass the env gate")
    parser.add_argument("--coins", default="", help="extract: comma-separated symbols")
    parser.add_argument("--timeframes", default="", help="extract: comma-separated timeframes")
    parser.add_argument("--bars", type=int, default=0, help="extract: bars per fixture")
    args = parser.parse_args(argv)

    # Pin BLAS threads before the first numpy import (rgcore below pulls numpy).
    _pin_blas_threads()

    if args.mode in _COMPUTE_MODES:
        _apply_offline_cred_shim()

    # import our helper lib after path setup. Named rgcore (NOT core) to avoid
    # shadowing the repo's own `core/` package, which the engine imports.
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import rgcore as core  # noqa: E402  (intentional: after sys.path setup)

    if args.mode == "verify":
        return mode_verify(core)
    if args.mode == "refresh":
        return mode_refresh(core, force=args.force)
    if args.mode == "status":
        return mode_status(core)
    if args.mode == "smoke":
        return mode_smoke(core)
    if args.mode == "extract":
        def_coins, def_tfs = _load_coin_selection()
        coins = [c.strip() for c in args.coins.split(",") if c.strip()] or def_coins
        tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()] or def_tfs
        n_bars = args.bars or core.DEFAULT_BARS
        return mode_extract(core, coins, tfs, n_bars)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
