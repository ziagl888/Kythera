# backtest/test_dashboard_security.py
"""Dashboard hardening: bind policy, host allowlist, token, CSRF (T-2026-KYT-9050-056).

Pins the three rules from ``core/dashboard_security.py`` — such that each test
would have been RED at the pre-fix state:

  * ``0.0.0.0`` without token no longer starts (P0.8: exactly this state ran
    live, protected only by Windows firewall default rule).
  * a foreign page in the operator's browser can no longer trigger ``stop_all``
    (origin check) — this worked before despite the firewall because a simple
    request needs no preflight.
  * DNS rebinding fails on the host allowlist, NOT on the origin check: the
    rebinding attacker sends their own name in both headers, a pure same-origin
    comparison lets them through. The test below pins exactly this order.
  * control endpoints no longer accept unknown script names
    (audit_reports/10, [LOW]).

DB-free and fleet-free: ``dashboard.py`` is loaded via importlib with mocked
psutil/threading/process_control (real Flask so the guard runs over the real
request path). No park marker is written — for that ``core.process_control`` is
mocked and additionally checked for non-invocation.

Run with: pytest backtest/test_dashboard_security.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

from core.dashboard_security import (  # noqa: E402
    ENV_ALLOWED_HOSTS,
    ENV_HOST,
    ENV_TOKEN,
    TOKEN_COOKIE,
    TOKEN_HEADER,
    authorize,
    bind_policy_error,
    default_allowed_hosts,
    hostname_of,
    is_loopback_host,
    origin_hostname,
    resolve_allowed_hosts,
    resolve_bind_host,
    resolve_extra_hosts,
    resolve_token,
    token_matches,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOWED = frozenset({"localhost", "127.0.0.1", "::1"})


# ── Pure helpers ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("127.5.5.5", True),  # the whole 127/8 loopback net
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("0.0.0.0", False),  # the P0.8 bind — a wildcard is not loopback
        ("::", False),
        ("", False),
        ("45.134.39.167", False),  # this VPS's public IPv4
        ("example.com", False),
    ],
)
def test_is_loopback_host(host, expected):
    assert is_loopback_host(host) is expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("localhost:5000", "localhost"),
        ("localhost", "localhost"),
        ("SRV02:5000", "srv02"),
        ("[::1]:5000", "::1"),
        ("[::1]", "::1"),
        ("::1", ""),  # unbracketed IPv6 is not a valid Host value → deny-signal
        ("", ""),
        (None, ""),
    ],
)
def test_hostname_of(raw, expected):
    assert hostname_of(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://localhost:5000", "localhost"),
        ("https://evil.example", "evil.example"),
        ("http://user:pw@evil.example:8080", "evil.example"),
        ("null", ""),  # sandboxed iframe — present but unmatchable
        ("", ""),
        (None, ""),
        ("garbage", ""),
    ],
)
def test_origin_hostname(raw, expected):
    assert origin_hostname(raw) == expected


def test_default_allowed_hosts_covers_loopback_and_bind():
    hosts = default_allowed_hosts("192.168.1.10")
    assert {"localhost", "127.0.0.1", "::1", "192.168.1.10"} <= hosts
    # A wildcard bind is not a name a client can send — it must not leak in.
    assert "0.0.0.0" not in default_allowed_hosts("0.0.0.0")


def test_resolve_config_defaults_are_the_hardened_ones():
    assert resolve_bind_host({}) == "127.0.0.1"
    assert resolve_token({}) is None
    assert resolve_token({ENV_TOKEN: "   "}) is None
    assert resolve_bind_host({ENV_HOST: " 0.0.0.0 "}) == "0.0.0.0"
    assert resolve_token({ENV_TOKEN: " s3cret "}) == "s3cret"


def test_resolve_allowed_hosts_extends_via_env():
    hosts = resolve_allowed_hosts("127.0.0.1", {ENV_ALLOWED_HOSTS: "dash.example.com, other:443 ,"})
    assert "dash.example.com" in hosts
    assert "other" in hosts
    assert "localhost" in hosts


def test_token_matches():
    assert token_matches(None, None) is True  # nothing configured → nothing to check
    assert token_matches("anything", None) is True
    assert token_matches(None, "expected") is False
    assert token_matches("wrong", "expected") is False
    assert token_matches("expected", "expected") is True


# ── Bind policy (fail closed) ───────────────────────────────────────────────


def test_loopback_without_token_is_allowed_to_start():
    assert bind_policy_error("127.0.0.1", None) is None


def test_public_bind_without_token_is_refused():
    """The exact live pre-fix configuration — 0.0.0.0 and no auth."""
    err = bind_policy_error("0.0.0.0", None)
    assert err is not None
    assert ENV_TOKEN in err


def test_public_bind_with_token_is_allowed():
    assert bind_policy_error("0.0.0.0", "s3cret") is None


def test_tunnel_hostname_without_token_is_refused():
    """The Z2 shape: cloudflared → 127.0.0.1, so the BIND alone looks harmless."""
    err = bind_policy_error("127.0.0.1", None, ["dash.example.com"])
    assert err is not None
    assert "dash.example.com" in err
    assert ENV_TOKEN in err


def test_tunnel_hostname_with_token_is_allowed():
    assert bind_policy_error("127.0.0.1", "s3cret", ["dash.example.com"]) is None


def test_resolve_extra_hosts_reports_only_configured_names():
    assert resolve_extra_hosts({}) == frozenset()
    assert resolve_extra_hosts({ENV_ALLOWED_HOSTS: " dash.example.com , other:443 ,, "}) == frozenset(
        {"dash.example.com", "other"}
    )


# ── authorize(): the three ordered rules ────────────────────────────────────


def _authz(**kw):
    base = dict(
        method="GET",
        host_header="localhost:5000",
        origin=None,
        presented_token=None,
        expected_token=None,
        allowed_hosts=ALLOWED,
    )
    base.update(kw)
    return authorize(**base)


def test_plain_loopback_get_is_allowed():
    assert _authz().allowed is True


def test_unknown_host_is_rejected():
    d = _authz(host_header="evil.example:5000")
    assert (d.allowed, d.status, d.reason) == (False, 403, "host_not_allowed")


def test_missing_host_header_is_rejected():
    assert _authz(host_header=None).reason == "host_not_allowed"


def test_dns_rebinding_is_caught_by_the_allowlist_not_the_origin_check():
    """Rebinding sends a MATCHING Origin — only the host allowlist stops it."""
    d = _authz(method="POST", host_header="evil.example:5000", origin="http://evil.example:5000")
    assert (d.status, d.reason) == (403, "host_not_allowed")


def test_cross_site_post_is_rejected():
    """The attack that works today despite the firewall: a page in the operator's browser."""
    d = _authz(method="POST", origin="http://evil.example")
    assert (d.allowed, d.status, d.reason) == (False, 403, "origin_mismatch")


def test_same_origin_post_is_allowed():
    assert _authz(method="POST", origin="http://localhost:5000").allowed is True


def test_post_without_origin_is_allowed_for_cli_clients():
    """curl/PowerShell send no Origin; browsers always do on cross-origin POSTs."""
    assert _authz(method="POST", origin=None).allowed is True


def test_opaque_origin_on_post_is_rejected():
    assert _authz(method="POST", origin="null").reason == "origin_mismatch"


def test_get_is_exempt_from_the_origin_check_but_not_from_the_rest():
    assert _authz(method="GET", origin="http://evil.example").allowed is True
    assert _authz(method="GET", origin="http://evil.example", expected_token="s").status == 401


@pytest.mark.parametrize("presented,status", [(None, 401), ("wrong", 401)])
def test_token_is_required_when_configured(presented, status):
    d = _authz(presented_token=presented, expected_token="s3cret")
    assert (d.allowed, d.status, d.reason) == (False, status, "token_invalid")


def test_correct_token_passes():
    assert _authz(presented_token="s3cret", expected_token="s3cret").allowed is True


def test_host_check_runs_before_the_token_check():
    """A rebinding probe must not learn whether a token is configured."""
    d = _authz(host_header="evil.example", presented_token=None, expected_token="s3cret")
    assert (d.status, d.reason) == (403, "host_not_allowed")


# ── Wired into the real Flask app ───────────────────────────────────────────


@pytest.fixture(scope="module")
def dash():
    """Load dashboard.py with mocked psutil/threading/process_control, real Flask.

    ``core.process_control`` is a MagicMock, so no park/restart marker can be
    written by these tests; ``threading`` is mocked so the module-level
    ``_stats_poller`` daemon never starts.
    """
    for key in (ENV_HOST, ENV_TOKEN, ENV_ALLOWED_HOSTS):
        os.environ.pop(key, None)
    spec = importlib.util.spec_from_file_location("dashboard_under_test", os.path.join(_ROOT, "dashboard.py"))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        "sys.modules",
        {"psutil": mock.MagicMock(), "threading": mock.MagicMock(), "core.process_control": mock.MagicMock()},
    ):
        spec.loader.exec_module(module)
    return module


@pytest.fixture
def client(dash):
    dash.app.config["TESTING"] = True
    return dash.app.test_client()


def test_default_bind_is_loopback(dash):
    """The P0.8 one-liner: the listener no longer defaults to every interface."""
    assert dash.BIND_HOST == "127.0.0.1"
    assert dash.AUTH_TOKEN is None


def test_stop_all_via_cross_site_post_is_blocked_and_parks_nothing(dash, client):
    dash.park.reset_mock()
    resp = client.post("/api/system/stop_all", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "origin_mismatch"
    dash.park.assert_not_called()


def test_stop_all_from_the_dashboards_own_page_still_works(dash, client):
    dash.park.reset_mock()
    resp = client.post("/api/system/stop_all", headers={"Origin": "http://localhost"})
    assert resp.status_code == 200
    assert dash.park.call_count == len(dash.PROCESSES)


def test_unknown_script_is_404_and_writes_no_marker(dash, client):
    dash.park.reset_mock()
    resp = client.post("/api/process/not_a_real_bot.py/stop")
    assert resp.status_code == 404
    dash.park.assert_not_called()


def test_known_script_still_parks(dash, client):
    dash.park.reset_mock()
    script = dash.PROCESSES[0]["script"]
    resp = client.post(f"/api/process/{script}/stop")
    assert resp.status_code == 200
    dash.park.assert_called_once_with(script)


def test_rebinding_host_is_blocked_on_the_real_app(dash, client):
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403


def test_token_gates_every_route_when_configured(dash, client):
    with mock.patch.object(dash, "AUTH_TOKEN", "s3cret"):
        assert client.get("/").status_code == 401
        assert client.get("/", headers={TOKEN_HEADER: "wrong"}).status_code == 401
        assert client.get("/", headers={TOKEN_HEADER: "s3cret"}).status_code == 200


def test_query_token_sets_the_session_cookie(dash, client):
    with mock.patch.object(dash, "AUTH_TOKEN", "s3cret"):
        resp = client.get("/?token=s3cret")
        assert resp.status_code == 200
        cookie = resp.headers.get("Set-Cookie", "")
        assert TOKEN_COOKIE in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie
        # The cookie now carries the token — a follow-up XHR needs no header.
        assert client.get("/api/logs/nope.py").status_code == 404  # authenticated, then 404


def test_no_cookie_is_set_for_a_wrong_query_token(dash, client):
    with mock.patch.object(dash, "AUTH_TOKEN", "s3cret"):
        resp = client.get("/?token=wrong")
        assert resp.status_code == 401
        assert TOKEN_COOKIE not in resp.headers.get("Set-Cookie", "")
