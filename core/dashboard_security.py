# core/dashboard_security.py
"""Bind policy and request guard for the operator dashboard (P0.8 + P1.38-CSRF).

MEASURED STARTING POINT (2026-08-01, srv02, T-2026-KYT-9050-056)
    ``dashboard.py`` bound ``0.0.0.0:5000`` (verified live: the listener was
    ``0.0.0.0:5000``) with no authentication and no Origin/Host validation on
    its control endpoints. Anyone reaching that port could ``POST
    /api/system/stop_all``, which writes persistent park markers — the fleet
    stays down across a reboot.

    What actually kept that endpoint off the internet was ONE setting, not the
    code: the Windows firewall (all three profiles enabled, effective
    ``DefaultInboundAction = Block``, and no inbound allow rule for
    ``python.exe`` or TCP 5000). The box has a directly-routed public IPv4, so
    a single allow rule — including the one Windows offers to create the first
    time an interactive process binds a listening socket — removes the entire
    protection.

    That firewall also does not defend the browser-driven path at all: a page
    open in a browser ON the VPS can issue a simple cross-origin
    ``POST http://127.0.0.1:5000/api/system/stop_all`` (no preflight, no CORS
    needed for the side effect to fire), and a DNS-rebinding page can reach the
    same endpoint with an attacker-controlled ``Host``. Both work today,
    firewall or not.

WHAT THIS MODULE DOES
    It is the code-side half of the fix and contains no I/O beyond reading
    configuration: three ordered, O(1) checks plus a fail-closed bind policy.
    Deliberately cheap — the dashboard's existing ``/api/status`` psutil sweeps
    stay the only expensive thing per poll, and the guard adds no query, no
    process scan and no per-request allocation of note.

      1. HOST ALLOWLIST (all methods). The ``Host`` header must resolve to a
         name/address on the allowlist. This is the DNS-rebinding defense: a
         rebinding page sends its OWN name in both ``Host`` and ``Origin``, so
         a pure same-origin comparison passes it and only the allowlist stops
         it.
      2. TOKEN (all methods, only when configured). Constant-time compare
         against ``KYTHERA_DASHBOARD_TOKEN``. Absent config = no token check;
         the bind policy below is what makes that safe.
      3. ORIGIN (state-changing methods only). A present ``Origin`` must match
         the request's own host — this is the classic-CSRF defense. An absent
         ``Origin`` is allowed so ``curl``/PowerShell operator calls keep
         working; browsers always send it on cross-origin POSTs.

    FAIL-CLOSED BIND POLICY: binding anywhere other than loopback requires a
    token. :func:`bind_policy_error` returns the reason, and ``dashboard.py``
    refuses to start on it. Exposing the dashboard therefore cannot silently
    re-create the unauthenticated-listener state that P0.8 described — the
    exposure decision (Cloudflare Tunnel + Access, Z2) stays Michi's, but it
    can no longer land without an auth layer underneath it.

NOT IN SCOPE HERE
    This module authenticates nothing beyond a shared secret. It is defense in
    depth UNDER the intended Zero-Trust layer (cloudflared + Cloudflare
    Access), not a replacement for it — see ``docs/DASHBOARD_SECURITY.md``.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import socket
from dataclasses import dataclass
from typing import Iterable, Mapping

# Header/cookie the dashboard accepts the shared secret in. The header form is
# what a script should use; the cookie exists so the browser UI keeps working
# after one `?token=…` visit without the token living in every later URL.
TOKEN_HEADER = "X-Dashboard-Token"
TOKEN_COOKIE = "kythera_dashboard_token"
TOKEN_QUERY_PARAM = "token"

# Env keys. All optional — the defaults are the safe ones (loopback bind, no
# exposure), so an unconfigured deployment is the hardened deployment.
ENV_HOST = "KYTHERA_DASHBOARD_HOST"
ENV_TOKEN = "KYTHERA_DASHBOARD_TOKEN"
ENV_ALLOWED_HOSTS = "KYTHERA_DASHBOARD_ALLOWED_HOSTS"

DEFAULT_BIND_HOST = "127.0.0.1"

# Methods that change state. GET/HEAD/OPTIONS are exempt from the Origin check
# only — the host allowlist and the token still apply to them (the log
# endpoints leak strategy behaviour, so read access is not "harmless").
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Names that are loopback without being parseable as an IP address.
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})

# Wildcard binds — "listen on every interface". Never added to the host
# allowlist (they are not names a client can send in a Host header anyway).
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::", "*", ""})


@dataclass(frozen=True)
class Decision:
    """Outcome of :func:`authorize` — allowed, or a status plus a machine-readable reason."""

    allowed: bool
    status: int = 200
    reason: str = "ok"


_ALLOWED = Decision(True)


def is_loopback_host(host: str | None) -> bool:
    """True for ``localhost``, ``127.0.0.0/8``, ``::1`` (with or without brackets).

    A wildcard bind (``0.0.0.0``/``::``) is NOT loopback: it listens on the
    public interface too, which is exactly the state P0.8 describes.
    """
    h = (host or "").strip().strip("[]").lower()
    if not h:
        return False
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def hostname_of(host_header: str | None) -> str:
    """Lowercased hostname from a ``Host`` header, port stripped, ``""`` if unusable.

    ``""`` is an intentional deny-signal: a request without a usable ``Host``
    cannot be matched against the allowlist, and guessing one would defeat the
    rebinding defense.
    """
    raw = (host_header or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("["):  # [::1]:5000 — bracketed IPv6 literal
        end = raw.find("]")
        return raw[1:end] if end > 1 else ""
    if raw.count(":") > 1:
        # A bare (unbracketed) IPv6 literal is not a valid Host header value.
        return ""
    return raw.split(":", 1)[0]


def origin_hostname(origin: str | None) -> str:
    """Lowercased hostname of an ``Origin`` header value, ``""`` when absent/opaque.

    ``Origin: null`` (sandboxed iframe, some redirects) yields ``""`` and is
    therefore treated as "present but unmatchable" by the caller — see
    :func:`authorize`, which distinguishes absent from unmatchable.
    """
    raw = (origin or "").strip()
    if not raw or raw.lower() == "null":
        return ""
    _, _, rest = raw.partition("://")
    if not rest:
        return ""
    authority = rest.split("/", 1)[0]
    # Strip any userinfo before splitting off the port.
    authority = authority.rpartition("@")[2]
    return hostname_of(authority)


def default_allowed_hosts(bind_host: str) -> frozenset[str]:
    """Host-header values accepted by default: loopback, the bind address, this machine's name.

    The machine's own hostname is included because reaching the dashboard as
    ``http://<vps-name>:5000`` from a session ON the box is an existing
    operator habit; it costs nothing against rebinding, where the attacker can
    only make their OWN domain resolve to a loopback/private address.
    """
    hosts = {"localhost", "127.0.0.1", "::1"}
    bind = (bind_host or "").strip().strip("[]").lower()
    if bind and bind not in _WILDCARD_BINDS:
        hosts.add(bind)
    try:
        node = socket.gethostname().strip().lower()
    except OSError:  # pragma: no cover - gethostname failing is not a reason to lock out
        node = ""
    if node:
        hosts.add(node)
    return frozenset(hosts)


def resolve_bind_host(env: Mapping[str, str | None] | None = None) -> str:
    """Bind address for the dashboard listener — loopback unless explicitly overridden."""
    env = os.environ if env is None else env
    raw = (env.get(ENV_HOST) or "").strip()
    return raw or DEFAULT_BIND_HOST


def resolve_token(env: Mapping[str, str | None] | None = None) -> str | None:
    """Configured shared secret, or ``None`` when unset/blank."""
    env = os.environ if env is None else env
    raw = (env.get(ENV_TOKEN) or "").strip()
    return raw or None


def resolve_extra_hosts(env: Mapping[str, str | None] | None = None) -> frozenset[str]:
    """The explicitly-configured extra host names from ``KYTHERA_DASHBOARD_ALLOWED_HOSTS``.

    Kept separate from :func:`resolve_allowed_hosts` because these entries are
    the tell-tale of an off-box deployment: a tunnel only works if its public
    hostname is on the allowlist, so a non-empty extras list means "something
    other than this machine is expected to reach the dashboard".
    """
    env = os.environ if env is None else env
    raw = (env.get(ENV_ALLOWED_HOSTS) or "").strip()
    names = {hostname_of(item.strip()) for item in raw.split(",")}
    return frozenset(n for n in names if n)


def resolve_allowed_hosts(bind_host: str, env: Mapping[str, str | None] | None = None) -> frozenset[str]:
    """:func:`default_allowed_hosts` plus the comma-separated ``KYTHERA_DASHBOARD_ALLOWED_HOSTS``.

    The extra entries are what a future tunnel hostname goes into (e.g.
    ``dash.example.com``) — adding one is a config change, never a code change.
    """
    return frozenset(set(default_allowed_hosts(bind_host)) | set(resolve_extra_hosts(env)))


def bind_policy_error(bind_host: str, token: str | None, extra_hosts: Iterable[str] = ()) -> str | None:
    """Reason why this configuration must not start, or ``None`` when it may.

    One rule, two ways of tripping it — anything reachable from off-box needs a
    shared secret:

      * a non-loopback BIND (the P0.8 state: ``0.0.0.0``), and
      * a configured extra HOST NAME while bound to loopback. That is the
        tunnel case, and it is not hypothetical: ``cloudflared`` connects to
        ``127.0.0.1:5000``, so the bind stays loopback and the first rule alone
        would wave a fully-exposed dashboard through. The tunnel's public
        hostname must be allowlisted for the tunnel to work at all, which is
        what makes it a reliable signal here.

    Cloudflare Access remains the intended primary auth layer; the token is the
    second line under it, so that a mis-scoped Access policy is not the only
    thing between the internet and ``stop_all``.
    """
    extras = {h for h in extra_hosts if h}
    if not is_loopback_host(bind_host) and not token:
        return (
            f"refusing to bind {bind_host!r}: a non-loopback dashboard needs {ENV_TOKEN} set. "
            f"Either leave {ENV_HOST} unset (loopback-only, the safe default) or configure a token. "
            "Exposing the dashboard itself (tunnel/Access) is an operator decision — see "
            "docs/DASHBOARD_SECURITY.md."
        )
    if extras and not token:
        return (
            f"refusing to start: {ENV_ALLOWED_HOSTS} names off-box host(s) {sorted(extras)!r} "
            f"(a tunnel front-end) but {ENV_TOKEN} is unset. An exposed dashboard must not rely on "
            "the tunnel's access policy alone — see docs/DASHBOARD_SECURITY.md."
        )
    return None


def token_matches(presented: str | None, expected: str | None) -> bool:
    """Constant-time token comparison. ``expected=None`` means "no token configured"."""
    if not expected:
        return True
    if not presented:
        return False
    return hmac.compare_digest(presented, expected)


def authorize(
    *,
    method: str,
    host_header: str | None,
    origin: str | None,
    presented_token: str | None,
    expected_token: str | None,
    allowed_hosts: Iterable[str],
    mutating_methods: Iterable[str] = MUTATING_METHODS,
) -> Decision:
    """Pure request verdict — see the module docstring for the three rules and their order.

    Order is load-bearing for the response: an unknown ``Host`` is rejected
    before the token is even looked at, so a rebinding probe learns nothing
    about whether a token is configured.
    """
    allowed = {h.strip().lower() for h in allowed_hosts if h and h.strip()}
    host = hostname_of(host_header)
    if not host or host not in allowed:
        return Decision(False, 403, "host_not_allowed")

    if not token_matches(presented_token, expected_token):
        return Decision(False, 401, "token_invalid")

    if method.upper() in {m.upper() for m in mutating_methods}:
        if origin is not None:
            # Present-but-unmatchable (``Origin: null``) is a deny: it is never
            # what the dashboard's own page sends.
            if origin_hostname(origin) != host:
                return Decision(False, 403, "origin_mismatch")

    return _ALLOWED
