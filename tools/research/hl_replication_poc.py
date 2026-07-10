#!/usr/bin/env python3
"""Hyperliquid replication-scoring feasibility PoC (T-2026-CU-9050-058).

A standalone, DB-free spike that proves the three load-bearing claims of the
polybot "replication scoring" concept on Hyperliquid public data:

  1. DATA ACCESS   -- any trader's fill history is publicly queryable by address,
                      and the leaderboard supplies the address universe. No key,
                      no auth (verified live 2026-07-10).
  2. SIGNATURE     -- a perp-adapted port of polybot's distribution features
                      (coin / direction / maker-taker / size mix) extracted from
                      the raw fill stream.
  3. SCORE         -- polybot's exact L1-over-marginals -> 0..100 formula, plus a
                      temporal self-consistency ("replicability") measure that the
                      original score omits.

This is a research spike, NOT a fleet component. It talks only to Hyperliquid's
public REST surface, writes nothing, and imports nothing from ``core``. Run it
from the repo root:

    python tools/research/hl_replication_poc.py                 # top-2 vs each other
    python tools/research/hl_replication_poc.py 0xabc... 0xdef... # explicit pair

See docs/HYPERLIQUID_REPLICATION_EVAL.md for the full evaluation this backs.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections import Counter
from typing import Any

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
TIMEOUT = 30

# Size buckets in USD notional (px * sz). Coarse, log-spaced -- the exact edges
# are a tuning knob, not a contract.
SIZE_EDGES = [100, 500, 2_000, 10_000, 50_000, 250_000]


def _post(body: dict[str, Any]) -> Any:
    req = urllib.request.Request(
        INFO_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def fetch_fills(address: str) -> list[dict[str, Any]]:
    """Most-recent public fills for ``address`` (<=2000, no auth)."""
    return _post({"type": "userFills", "user": address})


def fetch_leaderboard(top_n: int = 5) -> list[str]:
    """Return the top-``top_n`` addresses by account value from the public board."""
    with urllib.request.urlopen(LEADERBOARD_URL, timeout=TIMEOUT) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    rows = data.get("leaderboardRows") or data.get("data") or data
    return [r["ethAddress"] for r in rows[:top_n]]


def _size_bucket(notional: float) -> str:
    for edge in SIZE_EDGES:
        if notional < edge:
            return f"<{edge}"
    return f">={SIZE_EDGES[-1]}"


def signature(fills: list[dict[str, Any]]) -> dict[str, Counter]:
    """Extract polybot-style marginal distributions from a perp fill stream.

    Perp-native adaptation of polybot's (market / outcome / exec / size) mix:
      coin      -- which instruments the trader touches
      dir       -- Open/Close x Long/Short (richer than Polymarket YES/NO)
      liquidity -- maker vs taker (``crossed``); a strategy fingerprint prop
      size      -- USD-notional buckets
    """
    coin: Counter = Counter()
    direction: Counter = Counter()
    liquidity: Counter = Counter()
    size: Counter = Counter()
    for f in fills:
        coin[f.get("coin", "?")] += 1
        direction[f.get("dir", "?")] += 1
        liquidity["taker" if f.get("crossed") else "maker"] += 1
        try:
            notional = float(f["px"]) * float(f["sz"])
        except (KeyError, ValueError, TypeError):
            notional = 0.0
        size[_size_bucket(notional)] += 1
    return {"coin": coin, "dir": direction, "liquidity": liquidity, "size": size}


def _normalize(counts: Counter) -> dict[str, float]:
    total = sum(counts.values())
    if total == 0:
        return {}
    return {k: v / total for k, v in counts.items()}


def _l1(p: dict[str, float], q: dict[str, float]) -> float:
    keys = set(p) | set(q)
    return sum(abs(p.get(k, 0.0) - q.get(k, 0.0)) for k in keys)


def replication_score(sig_a: dict[str, Counter], sig_b: dict[str, Counter]) -> float:
    """polybot's formula: mean L1 over normalized marginals -> 0..100.

    L1 on a probability distribution ranges [0, 2]; ``1 - avg/2`` maps identical
    distributions to 1.0 (score 100) and disjoint to 0.0 (score 0).
    """
    dims = ["coin", "dir", "liquidity", "size"]
    l1s = [_l1(_normalize(sig_a[d]), _normalize(sig_b[d])) for d in dims]
    avg = sum(l1s) / len(l1s)
    return max(0.0, 100.0 * (1.0 - avg / 2.0))


def self_consistency(fills: list[dict[str, Any]]) -> float:
    """Split a trader's fills chronologically and score the halves against each
    other. This is the "replicability" the raw polybot score omits: a genuinely
    reproducible strategy is *stable across time*, so its own two halves score
    high. A one-off lucky run does not.
    """
    if len(fills) < 20:
        return float("nan")
    ordered = sorted(fills, key=lambda f: f.get("time", 0))
    mid = len(ordered) // 2
    return replication_score(signature(ordered[:mid]), signature(ordered[mid:]))


def _fmt_top(counts: Counter, n: int = 4) -> str:
    total = sum(counts.values()) or 1
    return ", ".join(f"{k} {v / total:.0%}" for k, v in counts.most_common(n))


def main(argv: list[str]) -> int:
    if len(argv) >= 2:
        addr_a, addr_b = argv[0], argv[1]
    else:
        print("No pair given -- pulling top-2 leaderboard addresses...")
        top = fetch_leaderboard(top_n=2)
        addr_a, addr_b = top[0], top[1]

    print(f"A = {addr_a}")
    print(f"B = {addr_b}\n")

    fills_a = fetch_fills(addr_a)
    fills_b = fetch_fills(addr_b)
    print(f"fills: A={len(fills_a)}  B={len(fills_b)}")
    if not fills_a or not fills_b:
        print("At least one address has no public fills -- cannot score.")
        return 1

    sig_a, sig_b = signature(fills_a), signature(fills_b)
    print("\nA signature:")
    for dim, c in sig_a.items():
        print(f"  {dim:9s}: {_fmt_top(c)}")
    print("B signature:")
    for dim, c in sig_b.items():
        print(f"  {dim:9s}: {_fmt_top(c)}")

    cross = replication_score(sig_a, sig_b)
    sc_a = self_consistency(fills_a)
    sc_b = self_consistency(fills_b)
    print("\n--- scores (0..100) ---")
    print(f"A<->B similarity      : {cross:5.1f}")
    print(f"A self-consistency    : {sc_a:5.1f}   (temporal replicability)")
    print(f"B self-consistency    : {sc_b:5.1f}")
    print(
        "\nReading: similarity says how alike two traders are; self-consistency "
        "says how reproducible a single trader is. polybot ships only the former."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
