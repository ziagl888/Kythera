# Dashboard hardening (P0.8 / Z2-B4) — current state, code fix, open decision

**Status:** 2026-08-02 · **Task:** T-2026-KYT-9050-056 · **Ledger:** `AUDIT_TODO.md` P0.8 + P1.38
(CSRF part), task audit B4/Z2 · **Code:** `core/dashboard_security.py`, `dashboard.py`

This document records what was **measured** as exposed on the dashboard, what the corresponding PR
changes in the code, and which part of the hardening **could not** be built because it
requires exposure — dashboard exposure is on Michi's escalation list
(`CLAUDE.md` §Escalation, OPUS-HANDOFF §6).

---

## 1. Current state — measured, not copied from the record

All values collected on 2026-08-01/02 on srv02 itself (read-only, no intervention).

> ### ⚠️ Correction 2026-08-02 — the firewall rows in this table were wrong
>
> Re-measured at the merge of this PR, read-only:
>
> | What | Originally noted | Actually |
> |---|---|---|
> | Effective inbound default action | **Block** (all three profiles) | **`NotConfigured`** (all three profiles) |
> | Inbound allow rule matching TCP 5000 | **none** | **two** — `SMC Service` + `SNAC Service`, Enabled/Inbound/**Allow**/Public, `LocalPort: Any`, `RemoteIP: Any` (Symantec) |
> | Port 5000 reachable from the internet | **no** | **yes** — `logs/dashboard.log` counts **557** successful `GET / → 200` from foreign IPs since 04.07 |
>
> The original session's rule query ran into "Access is denied" (not elevated); from that it
> concluded "no allow rule". **A failed measurement is not a negative finding.**
> The session had even correctly flagged its own reachability test method as unsound —
> a connection from the box to its own public IP is treated by Windows as
> loopback — and then trusted the configuration anyway instead of the empirical evidence in its own log.
>
> **Consequence for this document:** §"What follows from this" below argues in several places with
> "the firewall blocks it" — that reasoning no longer holds. The measures derived from it remain
> correct; the hardening in this PR no longer stands behind a firewall — it carries the load alone.
>
> **Decision on this (2026-08-02, operator): the two Symantec rules stay active —
> WONTFIX, risk accepted (`T-2026-KYT-9050-070`).** This is no longer an open item and
> should not be reopened as a finding. Two things limit the impact independently of the
> firewall: Postgres (5432) is reachable, but `pg_hba.conf`
> only knows `127.0.0.1/32`, `::1/128`, and the local socket → outsiders are rejected,
> no data access; and starting with its next launch the dashboard binds to loopback by default,
> which removes the unauthenticated `POST /api/system/stop_all` from the network.
> Still exposed: 135 (RPC), 445 (SMB), 3389 (RDP), and 5985 (WinRM).
>
> Revisit only on: (a) a foreign hit on a control endpoint in the
> dashboard log, (b) a `pg_hba` line for an external IP, (c) Symantec uninstall.
> Reverting would still be a single command at any time, elevated, immediately reversible, **RDP unaffected**
> (RDP has its own rules): `Disable-NetFirewallRule -DisplayName "SNAC Service","SMC Service"`.

| What | Measurement | How measured |
|---|---|---|
| Legacy dashboard listener | **`0.0.0.0:5000`**, PID 100120, started 2026-08-01 19:34 | `Get-NetTCPConnection -State Listen` |
| Z1 dashboard shell | `127.0.0.1:8098`, PID 86852, running since 2026-07-20 | ditto |
| Analytics API | own process not bound; default in code `127.0.0.1:8099` | `tools/analytics_api.py:1647` |
| Firewall profiles | Domain/Private/Public **all enabled** | `Get-NetFirewallProfile` |
| ~~Effective inbound default action~~ | ~~**Block** (all three profiles, ActiveStore)~~ → **wrong, see correction above: `NotConfigured`** | `Get-NetFirewallProfile -PolicyStore ActiveStore` |
| ~~Inbound allow rule for TCP 5000~~ | ~~**none**~~ → **wrong: `SMC Service` + `SNAC Service` allow every port from every IP** | scan ran unelevated into "Access is denied" |
| Inbound allow rule for `python.exe`/`py.exe` | none — but moot, the Symantec rules do not filter on application | scan of all active inbound allow rules + application filter |
| Public address of the box | **45.134.39.167**, directly routed (no NAT) | `Get-NetIPAddress` |

**Route inventory of the legacy dashboard (11 routes, state before this PR):**

| Route | Method | Effect | Auth before |
|---|---|---|---|
| `/` | GET | HTML UI | none |
| `/api/status` | GET | reads fleet/system status (psutil) | none |
| `/api/logs/<script>` | GET | reads bot logs (strategy behaviour) | none |
| `/api/logs/<script>/stream` | GET | live log stream (SSE) | none |
| `/api/events` | GET | event stream (SSE) | none |
| `/api/process/<script>/start` | POST | **writes** (unpark marker) | none |
| `/api/process/<script>/stop` | POST | **writes** (park marker) | none |
| `/api/process/<script>/restart` | POST | **writes** (restart marker) | none |
| `/api/system/start_all` | POST | **writes** (all bots) | none |
| `/api/system/restart_all` | POST | **writes** (all bots) | none |
| `/api/system/stop_all` | POST | **writes** — parks the whole fleet persistently | none |

The park markers are files under `control/parked/` and **survive a reboot**: a
single `POST /api/system/stop_all` shuts the fleet down until someone removes the markers.
The dashboard itself executes nothing — the watchdog is the only actuator (`core/process_control.py`) —
which changes nothing about the outcome: it reads the markers on the next cycle (≤10 s).

### What follows from this (and what doesn't)

* **The port WAS reachable from the internet** (corrected 2026-08-02, see box above).
  The audit question "Is port 5000 reachable externally?"
  (`audit_reports/10_dashboard_tools.md`, question 1 of the DB phase) is thereby answered:
  **yes** — evidenced by 557 answered foreign requests in its own log, not by a
  firewall configuration. The protection did not depend on any setting; it did not exist.
* **Not verified:** a reachability probe from an **external** vantage point.
  Connecting from the box to its own public IP proves nothing — Windows
  treats that as loopback and does not apply the inbound filters the way it would for genuine
  foreign traffic. The statement above rests on the ruleset, not on a
  connection test from outside.
* **The protection was a single point of failure.** A single allow rule cancels it out —
  including the one Windows itself offers to create the first time a listener binds interactively.
* **Two attacks worked despite the firewall** (that is the actual finding):
  1. **CSRF via simple request.** Any webpage in a browser on the VPS can
     issue `POST http://127.0.0.1:5000/api/system/stop_all` unprompted. A form POST or a
     `fetch(..., {mode:'no-cors'})` needs no preflight; the response is opaque to the
     attacker, **but the side effect happens anyway**. According to
     T-2026-CU-9050-166, Firefox ran directly on the box at times.
  2. **DNS rebinding.** An attacker domain pointing to `127.0.0.1` reaches the same
     endpoints. A plain same-origin comparison does **not** help against this, because the attacker's
     `Host` and attacker's `Origin` match — only a host allowlist stops it.

---

## 2. What the code fix changes

`core/dashboard_security.py` (new) + wiring in `dashboard.py`. Three O(1) checks per
request, in this order — no DB, no process scan, no file access:

1. **Host allowlist** (all methods). `Host` must be on the allowlist
   (default: `localhost`, `127.0.0.1`, `::1`, bind address, machine name; extendable via
   `KYTHERA_DASHBOARD_ALLOWED_HOSTS`). → closes DNS rebinding.
2. **Token** (all methods, only when configured). Constant-time comparison against
   `KYTHERA_DASHBOARD_TOKEN`; header `X-Dashboard-Token`, cookie, or a one-time `?token=…`
   (which then sets an `HttpOnly`/`SameSite=Strict` cookie so the UI keeps working without the header).
3. **Origin** (state-changing methods only). A **present** `Origin` must match the host.
   A missing `Origin` remains allowed, so operator curl/PowerShell calls
   keep working; browsers always send it on cross-origin POSTs. → closes CSRF.

In addition:

* **Bind default `0.0.0.0` → `127.0.0.1`** (overridden by `KYTHERA_DASHBOARD_HOST`).
* **Fail-closed start policy.** The process does **not** start if (a) it binds to a
  non-loopback address without a token, or (b) an off-box hostname is on the allowlist without a token.
  Case (b) is the tunnel case and the reason the check does not hang only on the bind address:
  `cloudflared` connects to `127.0.0.1:5000`, so the bind address stays
  harmless while the dashboard is reachable worldwide.
* **Control endpoints validate the script name** against `SCRIPT_MAP` (404 instead of a marker file
  for an unknown name) — `audit_reports/10`, [LOW].

**Cost per request:** a few string comparisons and dict lookups. The expensive item on the
dashboard remains unchanged, `/api/status` (a `psutil.process_iter` sweep per fleet entry,
every 6 s per tab — P1.38, open). The guard produces **no** additional query and no
additional process scan.

**~~Behavioural neutrality of the bind change~~ — CORRECTED 2026-08-02, the bind change is
NOT neutral.** The original reasoning ("off-box access is not possible today, so
the loopback bind cannot cut off any existing access path") rests on the refuted
firewall assumption (see box above). Off-box access **is** possible and is being used — 537
answered foreign requests since 04.07. **The loopback bind therefore does cut off a
real, existing path:** starting with the next dashboard start, the dashboard will only be
reachable from an RDP session on the box (and no longer at all for outsiders — that is the point).
Remote access then requires `KYTHERA_DASHBOARD_HOST` **plus** `KYTHERA_DASHBOARD_TOKEN` in `.env`
(without a token the fail-closed policy refuses to start), or the tunnel from Z2. The success probe of
`tools/restart_fleet.ps1` (`Test-NetConnection -ComputerName localhost -Port 5000`) remains valid:
even today it returns `True` against a pure IPv4 listener, even though `localhost` resolves to
`::1` first — name resolution falls back to IPv4 (re-measured).

---

## 3. What the PR does NOT do

No deploy, no dashboard or fleet restart, no firewall rule, no port, no
reverse proxy, no `cloudflared`, no `.env` change, no change to the running
bind address. The running dashboard process (PID 100120) is untouched.

> **The fix takes effect only at the next start of the dashboard process.** After the merge
> this happens **without operator action**: the watchdog restarts the dashboard on a crash
> (`main_watchdog.check_dashboard`), and a reboot does so regardless. Anyone who wants to control
> the timing must deliberately restart the dashboard process (not the fleet — the watchdog brings
> up the dashboard on its own).

---

## 4. ~~Open decision for Michi~~ — DECIDED 2026-08-02: **D1**

> **Operator decision (T-2026-KYT-9050-074): "Dashboard sehe ich ohnehin nur via RDP."**
>
> This makes **D1** apply — loopback-only, no token. **There is nothing to do and nothing to
> configure:** neither `KYTHERA_DASHBOARD_HOST` nor `KYTHERA_DASHBOARD_TOKEN` belong in the
> `.env`. The default from this PR is already the desired state; starting with the next launch
> of `dashboard.py`, the UI will only be reachable from an RDP session on the box.
>
> **D3 (cloudflared + Access) is therefore cancelled**, not deferred. Remote access is not
> needed, and against "not reachable at all", a tunnel enlarges the attack surface.
> The runbook in §5 remains as a reference and is **not** executed.
>
> **Sole trigger to revisit:** the Z1 quick actions (audit item F4). A live lever in
> the web UI needs an auth layer — if F4 comes, the question comes back. Otherwise not.
>
> Implementation note: `dashboard.py` is **not** in `core/fleet.py` (its own scheduled task).
> The marker-based fleet restart does not cover it; the hardening only takes effect at the next
> start of this process.

The code part is complete and effective on its own. The original options matrix remains
in place for traceability — the decision is **D1**:

### D1 — loopback-only, no token (what applies automatically after the merge)

* **Cost:** 0. No configuration, no restart beyond the one that's coming anyway.
* **Gained:** The listener is no longer on the public interface. An
  accidental firewall allow rule no longer exposes anything. CSRF and rebinding are closed.
* **Residual risk:** Any process running **on the box** as any user can still
  call the control API without authentication (curl sends no `Origin`, host
  `localhost` is on the allowlist). That is unchanged from today. No
  remote access — the dashboard is only reachable via an RDP session.

### D2 — D1 + token (`KYTHERA_DASHBOARD_TOKEN` in `.env`)

* **Cost:** one `.env` line (**Michi gate**, hard rule 3) + a dashboard restart.
  Operation afterwards: call `http://localhost:5000/?token=…` once, the cookie carries the rest.
* **Gained:** closes the residual risk from D1 — local processes/sessions without a token can
  no longer reach `stop_all`.
* **Residual risk:** The token sits in plaintext in `.env` (like all other secrets)
  and is transmitted over plaintext HTTP on loopback. Whoever can read `.env` has it.

### D3 — exposure: `cloudflared` + Cloudflare Access (the actual Z2/B4 scope, **not built**)

* **Prerequisite:** own domain in Cloudflare (still open per the task doc) **and** D2 —
  the code refuses to start if an off-box hostname is allowlisted and no token
  has been configured.
* **Why not in this PR:** The tunnel is exposure by definition. A
  `cloudflared service install` on the live box, an access policy setup, and a
  dashboard restart are all live interventions of exactly the class that the assignment and
  `CLAUDE.md` rule out.
* **What it brings:** remote access (phone/on the go) without an open port — the connection is
  outbound-only. At the same time the hard precondition for the Z1 quick actions (F4): without
  an auth layer, no live lever in the web UI.
* **Residual risk, honestly quantified:** After D3, the ability to stop the fleet hangs on
  **two** factors — the access policy (wrongly
  scoped = open worldwide, a known failure mode in zero-trust setups) and the token.
  The token is the reason a misconfigured access policy alone is not enough;
  it is enforced by the start policy. In addition, D3 shifts trust to Cloudflare
  (TLS termination at the provider — acceptable for an ops dashboard, irrelevant for the `.env`
  secrets, because those never go over the tunnel).
* **Cannot be assessed without a live test:** whether `cloudflared` as a Windows service coexists
  without collision with the box's watchdog/scheduled-task landscape. Experience from
  T-2026-CU-9050-170 (Z1 dashboard task) says that long-running services on this box do
  **not** bind under S4U and need password logon — this presumably applies to the tunnel
  service as well, but it is unverified.

**Recommendation:** roll D2 in with the next dashboard restart that's coming anyway (cheap,
closes the last local hole). Decide D3 only once the domain is in place and the
timing for a live intervention fits — the security gain of D3 over D2 is
**negative** (more attack surface); the gain is pure convenience plus the F4 precondition.
This is the point where the "Z2 before Z1" ordering from the task audit needs a justification
that goes beyond "hardening first": **the dashboard is secured after D1/D2
even without a tunnel.** Z2 is a prerequisite for the Z1 quick actions, not for the hardening.

---

## 5. Runbook D3 (if decided) — not executed

Noted only for preparation; every step is a live intervention.

1. `KYTHERA_DASHBOARD_TOKEN=<zufälliger 32-Byte-Wert>` into `.env` (Michi).
2. `KYTHERA_DASHBOARD_ALLOWED_HOSTS=<tunnel-hostname>` into `.env` — otherwise the
   guard answers the tunnel with `403 host_not_allowed`.
3. Restart the dashboard process; the log must show `[token required]`.
4. Install `cloudflared`, map the tunnel to `http://127.0.0.1:5000` and register it as a Windows
   service (check the logon type analogous to T-2026-CU-9050-170, see above).
5. Set the Cloudflare access policy **before** the first public call (a tunnel without a
   policy is open), login policy for Michi; service tokens later for machines (idea I9).
6. Verify: (a) tunnel hostname without access login → rejected; (b) with login, without
   dashboard token → `401 token_invalid`; (c) with both → UI; (d) `http://45.134.39.167:5000`
   from outside → unreachable (previously reachable — that is the proof that the bind took
   effect, not a confirmation of the original state).

---

## 6. Correction to the record

The assignment for this task called the dashboard "the single largest DB load contributor on
the box". That attribution comes from T-2026-CU-9050-166 (2026-07-19) and it was already
corrected the following day by T-2026-CU-9050-179: the most expensive DB item (`candles ⋈ indicators`,
~245 ms/call) is the **AI bot feature loading path** `core/candles.read_candles_with_indicators`,
evidenced via `pg_stat_activity` (`user=dbfiller`).

For **this** dashboard the question is moot anyway: `dashboard.py` imports
no DB code at all and issues **zero** queries — the audit report already
noted this (`audit_reports/10_dashboard_tools.md`, "Explicit non-findings"), and the file's
import list confirms it (`psutil`, `flask`, `core.fleet`, `core.process_control`).
Its load is CPU (psutil sweeps), not DB. The Z1 shell dashboard
(`tools/dashboard/app.py`), in turn, reads exclusively from DuckDB and never from Postgres — by
module invariant.

The concern "a safeguard that generates queries per request makes an existing
problem worse" is answered anyway, just differently: the guard generates neither queries nor
process scans.
