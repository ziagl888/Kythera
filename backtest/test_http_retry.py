# backtest/test_http_retry.py — Tests für die gebudgetete Retry-/Backoff-Politik
# (core/http_retry.py, P2.14/P2.18). Läuft ohne DB und ohne Netz:
#   python backtest/test_http_retry.py

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.http_retry import (  # noqa: E402
    BACKOFF_CAP_S,
    BAN_MIN_BACKOFF_S,
    JITTER_MAX_S,
    MinIntervalThrottle,
    RetryBudget,
    backoff_seconds,
)


def NO_JITTER() -> float:
    return 0.0


def test_budget_max_attempts():
    b = RetryBudget(max_attempts=3, deadline_s=999)
    assert [b.attempt() for _ in range(5)] == [True, True, True, False, False]
    assert "max_attempts" in b.exhausted_reason()
    print("OK  RetryBudget: max_attempts kappt hart")


def test_budget_deadline():
    clock = {"t": 0.0}
    b = RetryBudget(max_attempts=100, deadline_s=60, now=lambda: clock["t"])
    assert b.attempt()
    clock["t"] = 59.9
    assert b.attempt()
    clock["t"] = 60.0
    assert not b.attempt(), "Deadline überschritten, aber attempt() erlaubt weiter"
    assert "deadline" in b.exhausted_reason()
    print("OK  RetryBudget: Wanduhr-Deadline kappt (ein stuck Symbol blockt nie ewig — P2.14)")


def test_418_never_below_ban_minimum():
    for consecutive in (1, 2, 3):
        for retry_after in (None, "5", "10"):
            w = backoff_seconds(418, consecutive, retry_after, rng=NO_JITTER)
            assert w >= BAN_MIN_BACKOFF_S, f"418-Backoff {w}s unter {BAN_MIN_BACKOFF_S}s (hämmert in den Ban)"
    assert backoff_seconds(418, 2, None, rng=NO_JITTER) == 2 * BAN_MIN_BACKOFF_S
    assert backoff_seconds(418, 1, "600", rng=NO_JITTER) == 600.0  # Header darf ERHÖHEN, nie senken
    print("OK  418: nie unter 120s, exponentiell, Retry-After nur nach oben")


def test_429_respects_retry_after():
    assert backoff_seconds(429, 1, "7", rng=NO_JITTER) == 7.0
    assert backoff_seconds(429, 1, None, rng=NO_JITTER) == 10.0
    assert backoff_seconds(429, 3, None, rng=NO_JITTER) == 40.0
    assert backoff_seconds(429, 1, "garbage", rng=NO_JITTER) == 10.0  # kaputter Header → Fallback
    print("OK  429: Retry-After respektiert, sonst exponentieller Fallback")


def test_error_backoff_bounded():
    assert backoff_seconds(None, 1, rng=NO_JITTER) == 2.0
    assert backoff_seconds(None, 20, rng=NO_JITTER) == BACKOFF_CAP_S  # Cap greift
    print("OK  Fehler-Backoff: exponentiell mit Cap")


def test_jitter_bounds():
    for _ in range(50):
        w = backoff_seconds(429, 1, "5")
        assert 5.0 <= w <= 5.0 + JITTER_MAX_S
    print("OK  Jitter: additiv, begrenzt")


def test_throttle_spacing():
    clock = {"t": 0.0}
    sleeps: list[float] = []

    def fake_sleep(s):
        sleeps.append(s)
        clock["t"] += s

    th = MinIntervalThrottle(now=lambda: clock["t"], sleep=fake_sleep, rng=lambda: 0.0)
    th.wait("binance", 1.0)  # frei → kein Sleep
    th.wait("binance", 1.0)  # 1s Abstand nötig
    th.wait("binance", 1.0)
    assert sleeps == [1.0, 1.0], sleeps
    th.wait("other", 1.0)  # anderer Bucket blockt nicht
    assert len(sleeps) == 2
    print("OK  MinIntervalThrottle: Mindestabstand je Bucket, Buckets unabhängig")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    test_budget_max_attempts()
    test_budget_deadline()
    test_418_never_below_ban_minimum()
    test_429_respects_retry_after()
    test_error_backoff_bounded()
    test_jitter_bounds()
    test_throttle_spacing()
    print("\nAlle http_retry-Tests bestanden.")
